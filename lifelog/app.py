"""LIFELOG Home API. Run: .venv/Scripts/uvicorn lifelog.app:app --reload"""
import json
from datetime import datetime, timedelta

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import assistant, auth, config, db, engine, forecast_ml, gem, medicines, offline, realtime, seed, services, sim
from .models import StabilityModel

app = FastAPI(title="LIFELOG Home")
app.mount("/static", StaticFiles(directory=config.ROOT / "static"), name="static")

# Paths anyone may call without signing in: sign-in itself, health, what a share link / receipt QR / sensor needs.
_PUBLIC_PREFIXES = ("/api/auth/", "/api/health", "/api/receipts/", "/v1/", "/api/schema/", "/static/")
_PUBLIC_EXACT = {"/", "/login", "/sw.js", "/manifest.webmanifest", "/docs", "/openapi.json", "/favicon.ico"}


def _is_public(path: str, method: str) -> bool:
    if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES) or path.startswith(("/s/", "/r/")):
        return True
    return method == "GET" and path.startswith("/api/share/")   # caregiver view of a share link


class AuthMiddleware:
    """Session cookie -> user. Every signed-in request then runs its queries as the restricted database role with
    lifelog.user_id set (db.conn), so Postgres row-level security decides what this person can see."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = Request(scope).cookies.get(auth.COOKIE)
        user = None
        if token and config.DATABASE_URL:
            try:
                user = await run_in_threadpool(auth.session_user, token)
            except Exception:  # noqa: BLE001  (database down: public pages still work)
                user = None
        if user is None and not _is_public(scope["path"], scope["method"]):
            return await JSONResponse(status_code=401, content={"detail": "Please sign in."})(scope, receive, send)
        scope.setdefault("state", {})["user"] = user
        tok = db.set_user(user["id"] if user else None)
        try:
            await self.app(scope, receive, send)
        finally:
            db.reset_user(tok)


app.add_middleware(AuthMiddleware)


def _user(request: Request) -> dict:
    return request.state.user


def _pharmacist(request: Request) -> dict:
    u = request.state.user
    if not u or u["role"] != "pharmacist":
        raise HTTPException(403, "Only a pharmacist account can do this.")
    return u


@app.exception_handler(gem.GeminiUnavailable)
async def _no_gemini(_: Request, exc: gem.GeminiUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/")
def index(request: Request):
    if not request.state.user:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(config.ROOT / "static" / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(config.ROOT / "static" / "login.html")


# ------------------------------------------------------------------ accounts
class LoginIn(BaseModel):
    email: str
    password: str


class SignupIn(LoginIn):
    name: str = ""


def _signed_in(request: Request, user: dict) -> JSONResponse:
    res = JSONResponse({"ok": True, "user": {"id": user["id"], "name": user["name"]}})
    res.set_cookie(auth.COOKIE, auth.create_session(user["id"]), max_age=auth.SESSION_DAYS * 86400, httponly=True,
                   samesite="lax", secure=request.url.scheme == "https", path="/")
    return res


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request):
    u = auth.login(body.email, body.password)
    if not u:
        raise HTTPException(401, "That email and password don't match an account.")
    return _signed_in(request, u)


@app.post("/api/auth/signup")
def auth_signup(body: SignupIn, request: Request):
    try:
        u = auth.signup(body.name, body.email, body.password)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _signed_in(request, u)


@app.post("/api/auth/demo")
def auth_demo(request: Request, role: str = "patient"):
    u = auth.demo_user(role)
    if not u:
        raise HTTPException(404, "Demo sign-in is turned off on this server.")
    return _signed_in(request, u)


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    res = JSONResponse({"ok": True})
    res.delete_cookie(auth.COOKIE, path="/")
    return res


@app.get("/api/auth/options")
def auth_options():
    return {"demo": config.ENABLE_DEMO_LOGIN}


@app.get("/api/me")
def me_(request: Request):
    u = _user(request)
    return {**u, "items": db.one("SELECT count(*) AS n FROM items")["n"]}


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(config.ROOT / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.on_event("startup")
def _start_realtime():
    realtime.start()
    services.warm_benchmark()


def _item_or_404(item_id: int, raw: bool = False) -> dict:
    d = services.item_detail(item_id, raw)
    # row-level security already hides other people's items; this second check covers databases without it
    if not d or (db.current_user() and d["item"]["user_id"] != db.current_user()):
        raise HTTPException(404, "item not found")
    return d


def _llm_state(d: dict) -> dict:
    """Compact state for Gemini: no full timeline."""
    return {"medicine": d["item"]["nickname"], "model": d["item"]["model"], "state": d["state"],
            "top_budget_consumers": d["burners"], "data_gaps": d["gaps"], "what_if": d["whatif"],
            "similar_patterns": d["patterns"]}


# ------------------------------------------------------------------ health
@app.get("/api/health")
def health():
    """Liveness for the demo and the host: database, TimescaleDB, background jobs, LISTEN thread. No secrets."""
    out = {"gemini": bool(config.GEMINI_API_KEY) and not config.GEMINI_DISABLED, "model": config.GEMINI_MODEL, "database": False, **realtime.status()}
    try:
        out["extensions"] = {r["extname"]: r["extversion"] for r in
                             db.query("SELECT extname, extversion FROM pg_extension")}
        out["database"] = True
        out["timescaledb_version"] = out["extensions"].get("timescaledb")
        jobs = db.one("""SELECT count(*) AS jobs, coalesce(sum(s.total_failures), 0) AS failures,
                                count(*) FILTER (WHERE s.last_run_status = 'Failed') AS failing
                         FROM timescaledb_information.jobs j LEFT JOIN timescaledb_information.job_stats s USING (job_id)
                         WHERE j.job_id >= 1000""")
        out.update(jobs=jobs["jobs"], job_failures=int(jobs["failures"]), jobs_failing_now=jobs["failing"])
        out["watermark"] = services.cagg_watermark()
        out["row_level_security"] = db.rls_available()
    except Exception as e:  # noqa: BLE001
        out["db_error"] = str(e).splitlines()[0]
    out["ok"] = out["database"] and out.get("jobs_failing_now") == 0 and out["listen_thread_alive"]
    return out


# ------------------------------------------------------------------ items
@app.get("/api/items")
def items(mine: bool = True):
    if mine:
        return services.list_items(db.me())
    with db.system():   # neighbors' medicines, for the community outage view
        return services.list_items(None)


@app.get("/api/items/{item_id}")
def item(item_id: int, raw: bool = False):
    return _item_or_404(item_id, raw)


@app.get("/api/items/{item_id}/compare")
def compare(item_id: int):
    """Late-data demo: budget as served by the continuous aggregate vs recomputed from raw readings."""
    d = _item_or_404(item_id)
    m = d["item"]["model"]
    return {"aggregate": services.summary(item_id, m), "raw_truth": services.summary(item_id, m, raw=True),
            "watermark": services.cagg_watermark(),
            "log": db.query("SELECT * FROM ingest_log WHERE item_id = %s ORDER BY id DESC LIMIT 5", (item_id,))}


class ItemDates(BaseModel):
    opened_at: datetime | None = None
    expires_on: str | None = None   # YYYY-MM-DD
    lot: str | None = None


@app.patch("/api/items/{item_id}")
def update_item(item_id: int, body: ItemDates):
    _item_or_404(item_id)
    db.execute("UPDATE items SET opened_at = %s, expires_on = %s::date, lot = %s WHERE id = %s",
               (body.opened_at, body.expires_on or None, (body.lot or "").strip() or None, item_id))
    services.check_alerts()
    return {"ok": True}


@app.get("/api/items/{item_id}/history")
def item_history(item_id: int, days: int = 30):
    _item_or_404(item_id)
    return services.history(item_id, max(1, min(days, 90)))


@app.get("/api/items/{item_id}/recalls")
def item_recalls(item_id: int):
    d = _item_or_404(item_id)
    brand = d["item"]["product_name"].split()[0]
    return services.recalls(brand, d["item"].get("lot"))


class ShareIn(BaseModel):
    label: str | None = None


@app.post("/api/share")
def share(body: ShareIn):
    return {"token": services.create_share(db.me(), body.label)}


@app.get("/api/shares")
def shares():
    return db.query("SELECT token, label, created_at FROM shares WHERE user_id = %s AND NOT revoked ORDER BY created_at DESC",
                    (db.me(),))


@app.delete("/api/share/{token}")
def revoke_share(token: str):
    db.execute("UPDATE shares SET revoked = TRUE WHERE token = %s AND user_id = %s", (token, db.me()))
    return {"ok": True}


@app.get("/api/share/{token}")
def shared(token: str):
    with db.system():   # the token itself is the permission
        v = services.shared_view(token)
    if not v:
        raise HTTPException(404, "This link is no longer active.")
    return v


@app.get("/s/{token}")
def share_page(token: str):
    return FileResponse(config.ROOT / "static" / "share.html")


@app.get("/sw.js")
def service_worker():
    return FileResponse(config.ROOT / "static" / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/api/items/{item_id}/accuracy")
def accuracy(item_id: int):
    _item_or_404(item_id)
    return services.accuracy(item_id)


@app.get("/api/items/{item_id}/export.csv")
def export_csv(item_id: int):
    """Exposure log a pharmacist can open in a spreadsheet: one row per 5-minute bucket."""
    import csv
    import io
    d = _item_or_404(item_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["bucket_start_utc", "avg_temp_c", "min_temp_c", "max_temp_c", "zone", "budget_left_pct"])
    for r in d["timeline"]:
        w.writerow([r["t"].isoformat(), f"{r['temp']:.2f}", f"{r['min']:.2f}", f"{r['max']:.2f}", r["zone"],
                    f"{r['remaining'] * 100:.2f}"])
    name = "".join(ch if ch.isalnum() else "-" for ch in d["item"]["nickname"]).strip("-").lower()
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="lifelog-{name}.csv"'})


@app.get("/api/stream")
def stream():
    """Server-Sent Events: 'readings' when new data lands (throttled to 1/s), 'alert' when an alert
    opens, changes or resolves. Driven by Postgres LISTEN/NOTIFY triggers."""
    import json as _json
    import queue as _queue
    import time as _time

    mine = {i["id"] for i in db.query("SELECT id FROM items")}   # row-level security: your items only

    def gen():
        q = realtime.subscribe()
        last = 0.0
        try:
            yield "retry: 3000\nevent: hello\ndata: {}\n\n"
            while True:
                try:
                    ev = q.get(timeout=15)
                except _queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if ev.get("item_id") is not None and ev["item_id"] not in mine:
                    continue
                if ev.get("type") == "readings":
                    if _time.time() - last < 1.0:
                        continue
                    last = _time.time()
                yield f"event: {ev.get('type', 'message')}\ndata: {_json.dumps(ev, default=str)}\n\n"
        finally:
            realtime.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/items/{item_id}/repair")
def repair(item_id: int):
    res = services.repair_late(item_id)
    services.check_alerts()
    return res


def _fallback_note(e: Exception) -> str:
    return f"Gemini wasn't used: {str(e).split('. ')[0].rstrip('.')}."


@app.post("/api/items/{item_id}/advice")
def advice(item_id: int, lang: str = "en"):
    d = _item_or_404(item_id)
    try:
        return {**gem.advise(_llm_state(d), lang), "ai": True}
    except gem.GeminiUnavailable as e:
        return {**offline.advise(d), "notice": _fallback_note(e)}


@app.post("/api/items/{item_id}/visual")
async def visual(item_id: int, photo: UploadFile = File(...), lang: str = "en"):
    d = _item_or_404(item_id)
    return gem.visual_check(await photo.read(), photo.content_type or "image/jpeg", d["item"]["model"], lang)


@app.post("/api/items/{item_id}/letter")
def letter(item_id: int, lang: str = "en"):
    d = _item_or_404(item_id)
    outage = db.one("""SELECT o.id, o.name, o.started_at, o.est_restore_at FROM outages o
                       JOIN users u ON ST_Contains(o.area, u.geom) JOIN items i ON i.user_id = u.id
                       WHERE i.id = %s ORDER BY o.started_at DESC LIMIT 1""", (item_id,))
    m = d["item"]["model"]
    ctx = {"product": d["item"]["product_name"], "nickname": d["item"]["nickname"],
           "remaining_budget_pct": round(d["state"]["remaining"] * 100, 1),
           "exposure_timeline": [e for e in d["episodes"] if e["zone"] != "Labeled storage"],
           "label_rules": {k: m.get(k) for k in ("target_quote", "bands", "freeze_quote", "above_limit_quote",
                                                  "above_limit_is_assumption")},
           "outage": outage, "data_gaps": d["gaps"], "generated_at": services.now()}
    rc = services.create_receipt(d)          # pharmacists can verify the numbers in the letter
    ctx.update(receipt_code=rc["code"], receipt_url=f"/r/{rc['code']}")
    try:
        return {"letter": gem.refill_letter(ctx, lang), "ai": True, "receipt": rc}
    except gem.GeminiUnavailable as e:
        return {"letter": offline.refill_letter(ctx), "ai": False, "notice": _fallback_note(e), "receipt": rc}


class TripIn(BaseModel):
    itinerary: str


@app.post("/api/items/{item_id}/trip")
def trip(item_id: int, body: TripIn, lang: str = "en"):
    _item_or_404(item_id)
    try:
        plan = gem.parse_trip(body.itinerary, services.now())
    except gem.GeminiUnavailable as e:
        raise HTTPException(503, "Trip check needs Gemini to read free-text plans, and Gemini is unavailable right now. "
                                 "Everything else keeps working. " + str(e).split(". ")[0] + ".")
    sim = services.simulate_trip(item_id, plan["legs"])
    try:
        sim["advice"] = gem.trip_advice(sim, lang)
    except gem.GeminiUnavailable:
        worst = max(sim["legs"], key=lambda l: l["budget_used"], default=None)
        sim["advice"] = (f"- The riskiest part is **{worst['summary']}** (up to {worst['peak_temp_c']}°C).\n"
                         "- Carry it in an insulated case with a cold pack, never in a parked car or checked bag."
                         if worst else "- No risky legs found.")
    return sim


# ------------------------------------------------------------------ products / labels
@app.post("/api/products/extract")
async def extract(label: UploadFile = File(...)):
    data = await label.read()
    return gem.extract_label(data, label.content_type or "application/pdf")


class LookupIn(BaseModel):
    name: str


@app.post("/api/products/lookup")
def lookup(body: LookupIn):
    """Add a medicine by name: the official FDA label from openFDA first (exact text, no guessing); if openFDA
    has no usable storage section, Gemini searches the web with Google Search + URL Context grounding."""
    hit = services.openfda_lookup(body.name)
    if hit:
        name = f"{hit['brand']} ({hit['generic']})"
        try:
            model, method, notice = gem.extract_label_text(name, hit["text"]), "openfda", None
        except gem.GeminiUnavailable as e:   # official text still found: read it with rules, flag it for checking
            model, method, notice = offline.parse_label_text(name, hit["text"]), "openfda_rules", _fallback_note(e)
        return {"model": model, "method": method, "notice": notice, "source_label": hit["source_label"],
                "source_url": hit["source_url"], "sources": [{"title": hit["source_label"], "url": hit["source_url"]}],
                "label_text": hit["text"][:1500],
                # every quote checked word-for-word against the label text it came from, and every number against its quote
                "verification": medicines.check_model(model, hit["text"])}
    try:
        found = gem.search_label(body.name)
    except gem.GeminiUnavailable as e:
        raise HTTPException(503, f"'{body.name}' isn't in openFDA, and searching the web needs Gemini, which is unavailable "
                                 f"right now. Try the brand name, or add it from a label photo later. {str(e).split('. ')[0]}.")
    if "NOT FOUND" in found["text"][:200] or not found["text"].strip():
        raise HTTPException(404, f"No official storage information found for '{body.name}'. Try uploading a photo of the label.")
    model = gem.extract_label_text(body.name, found["text"])
    first = found["sources"][0] if found["sources"] else {"title": "Google Search", "url": None}
    return {"model": model, "method": "google_search", "source_label": f"{first['title']} (found by Gemini web search)",
            "source_url": first["url"], "sources": found["sources"], "label_text": found["text"][:1500],
            "verification": medicines.check_model(model, found["text"])}


class NewItem(BaseModel):
    model: StabilityModel
    nickname: str
    source_label: str | None = None
    source_url: str | None = None


@app.post("/api/products")
def create_product(body: NewItem):
    m = body.model.model_dump()
    pid = db.one("""INSERT INTO products (name, model, source, source_label, source_url)
                    VALUES (%s, %s, 'gemini', %s, %s) RETURNING id""",
                 (m["product_name"], json.dumps(m), body.source_label, body.source_url))["id"]
    iid = db.one("INSERT INTO items (user_id, product_id, nickname, started_at) VALUES (%s,%s,%s, now() - interval '1 hour') RETURNING id",
                 (db.me(), pid, body.nickname))["id"]
    return {"product_id": pid, "item_id": iid}


# ------------------------------------------------------------------ ingest & voice
class Reading(BaseModel):
    ts: datetime
    temp_c: float
    humidity: float | None = None


class IngestIn(BaseModel):
    item_id: int
    readings: list[Reading]
    source: str = "sensor"
    auto_refresh: bool = True


@app.post("/api/ingest")
def ingest(body: IngestIn):
    _item_or_404(body.item_id)   # readings are only accepted for your own medicines
    return services.ingest(body.item_id, [r.model_dump() for r in body.readings], body.source, body.auto_refresh)


@app.post("/api/voice")
async def voice(item_id: int = Form(...), text: str | None = Form(None), audio: UploadFile | None = File(None)):
    d = _item_or_404(item_id)
    mine = [{"id": i["id"], "nickname": i["nickname"]} for i in items()]
    try:
        report = gem.parse_voice(services.now(), mine,
                                 audio=await audio.read() if audio else None,
                                 mime_type=(audio.content_type if audio else "audio/wav") or "audio/wav", text=text)
    except gem.GeminiUnavailable as e:
        if not text:
            raise HTTPException(503, "Voice recordings need Gemini, which is unavailable right now. Type what happened instead: "
                                     "that works without AI.")
        report = {**offline.parse_report(text), "notice": _fallback_note(e)}
    pts = []
    t_now = services.now()
    for ev in report["events"]:
        start = t_now - timedelta(minutes=ev["minutes_ago_start"])
        end = min(t_now, start + timedelta(minutes=max(1, ev["duration_minutes"])))
        pts += [{"ts": t, "temp_c": ev["estimated_temp_c"]} for t in engine.daterange(start, end, timedelta(minutes=1))]
    if pts:
        db.execute("DELETE FROM readings WHERE item_id = %s AND ts >= %s AND ts <= %s",
                   (item_id, min(p["ts"] for p in pts), max(p["ts"] for p in pts)))
        res = services.ingest(item_id, pts, source="voice", auto_refresh=True)
        db.refresh_readings(min(p["ts"] for p in pts) - timedelta(minutes=5), t_now)
    else:
        res = {"inserted": 0}
    return {"report": report, "ingest": res, "item": d["item"]["nickname"]}


# ------------------------------------------------------------------ outage rescue
@app.get("/api/rescue")
def rescue():
    with db.system():
        return {**services.rescue(), "live": services.outage_feed_status()}


@app.post("/api/rescue/plan")
def rescue_plan(lang: str = "en"):
    with db.system():
        r = services.rescue()
    if not r["outages"]["features"]:
        return {"plan": "There's no power outage right now, so there is nothing to dispatch.", "ai": False}
    at_risk = [p for p in r["people"] if p["at_risk"]] or r["people"][:5]
    outage = r["outages"]["features"][0]["properties"]
    try:
        return {"plan": gem.rescue_plan(at_risk[:10], outage, lang), "ai": True}
    except gem.GeminiUnavailable as e:
        return {"plan": offline.rescue_plan(at_risk[:10], outage), "ai": False, "notice": _fallback_note(e)}


# ------------------------------------------------------------------ porch heat index
@app.get("/api/porch")
def porch():
    with db.system():
        return services.porch_stats()


@app.post("/api/porch/brief")
def porch_brief():
    with db.system():
        stats = services.porch_stats()
    try:
        return {"brief": gem.porch_brief(stats), "ai": True}
    except gem.GeminiUnavailable as e:
        return {"brief": offline.porch_brief(stats), "ai": False, "notice": _fallback_note(e)}


# ------------------------------------------------------------------ alerts (written by the check_alerts job)
@app.get("/api/alerts")
def get_alerts(mine: bool = True, include_resolved: bool = False):
    if mine:
        return services.alerts(mine_only=True, include_resolved=include_resolved)
    with db.system():
        return services.alerts(mine_only=False, include_resolved=include_resolved)


@app.post("/api/alerts/check")
def run_alert_check():
    services.check_alerts()
    return services.alerts(mine_only=True)


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: int):
    db.execute("UPDATE alerts SET acked = TRUE WHERE id = %s AND item_id IN (SELECT id FROM items WHERE user_id = %s)", (alert_id, db.me()))
    return {"ok": True}


# ------------------------------------------------------------------ under the hood
@app.get("/api/tiger")
def tiger(fresh: bool = False):
    with db.system():
        return {**services.tiger_stats(fresh), "listeners": realtime.subscriber_count()}


# ------------------------------------------------------------------ live sensor simulator
@app.post("/api/sim/{action}")
def simulator(action: str):
    with db.system():
        return _simulator(action)


def _simulator(action: str):
    if action == "start":
        return sim.start()
    if action == "stop":
        return sim.stop()
    return sim.status()


# ------------------------------------------------------------------ Ask LIFELOG (Gemini function calling)
class AskIn(BaseModel):
    question: str
    history: list[dict] = []


@app.post("/api/ask")
def ask(body: AskIn, lang: str = "en"):
    return assistant.ask(body.question, body.history, lang)


# ------------------------------------------------------------------ per-medicine forecasting models
@app.get("/api/items/{item_id}/ml")
def item_ml(item_id: int):
    _item_or_404(item_id)
    return forecast_ml.get(item_id)


@app.post("/api/items/{item_id}/ml/train")
def item_ml_train(item_id: int):
    _item_or_404(item_id)
    res = forecast_ml.train(item_id)
    services.check_alerts()
    return res


@app.post("/api/ml/train-all")
def ml_train_all():
    with db.system():
        res = forecast_ml.train_all()
    services.check_alerts()
    return res


@app.get("/api/ml")
def ml_overview():
    """Every trained model: which candidate won for which medicine, and by how much."""
    return db.query("""SELECT m.item_id, i.nickname, p.name AS product, m.model, m.metrics, m.risk, m.trained_at
                       FROM ml_models m JOIN items i ON i.id = m.item_id JOIN products p ON p.id = i.product_id
                       WHERE i.user_id = %s ORDER BY i.id""", (db.me(),))


def _retrain_loop():
    import time as _t
    _t.sleep(20)
    while True:
        try:
            forecast_ml.train_all()
            services.check_alerts()
        except Exception:
            pass
        _t.sleep(3600)


@app.on_event("startup")
def _start_retraining():
    import threading as _th
    if config.DATABASE_URL and not config.DISABLE_BACKGROUND_TRAINING:
        _th.Thread(target=_retrain_loop, daemon=True, name="ml-retrain").start()


# ------------------------------------------------------------------ dose tracking
class DoseIn(BaseModel):
    taken_at: datetime | None = None
    note: str | None = None


@app.post("/api/items/{item_id}/doses")
def add_dose(item_id: int, body: DoseIn):
    _item_or_404(item_id)
    if body.taken_at and body.taken_at > services.now() + timedelta(minutes=5):
        raise HTTPException(422, "A dose can't be in the future.")
    return services.log_dose(item_id, body.taken_at, (body.note or "").strip() or None)


@app.get("/api/items/{item_id}/doses")
def list_doses(item_id: int):
    _item_or_404(item_id)
    return services.doses(item_id)


@app.delete("/api/doses/{dose_id}")
def remove_dose(dose_id: int):
    db.execute("DELETE FROM doses WHERE id = %s AND item_id IN (SELECT id FROM items WHERE user_id = %s)", (dose_id, db.me()))
    return {"ok": True}


# ------------------------------------------------------------------ pharmacist review + evidence
@app.get("/api/review")
def review_queue(request: Request):
    _pharmacist(request)
    with db.system():   # sample checklists come from any patient's item of that product
        return services.review_queue()


class ReviewIn(BaseModel):
    status: str            # approved | changes_requested
    credentials: str | None = None
    note: str | None = None


@app.post("/api/review/{product_id}")
def submit_review(product_id: int, body: ReviewIn, request: Request):
    u = _pharmacist(request)
    if body.status not in ("approved", "changes_requested"):
        raise HTTPException(422, "status must be approved or changes_requested")
    if body.status == "changes_requested" and not (body.note or "").strip():
        raise HTTPException(422, "Say what should change.")
    with db.system():
        return services.submit_review(product_id, u, body.status, (body.credentials or u.get("credentials") or "").strip() or None,
                                      (body.note or "").strip() or None)


class EvidenceDecision(BaseModel):
    approve: bool


@app.post("/api/evidence/{evidence_id}")
def decide_evidence(evidence_id: int, body: EvidenceDecision, request: Request):
    u = _pharmacist(request)
    with db.system():
        r = services.decide_evidence(evidence_id, body.approve, u)
    if not r:
        raise HTTPException(404, "unknown evidence")
    return r


@app.get("/api/products/{product_id}/evidence")
def product_evidence(product_id: int):
    return {"evidence": services.evidence_for(product_id), "review": services.review_status(product_id)}


# ------------------------------------------------------------------ live utility outages
@app.get("/api/outages/status")
def outages_status():
    return services.outage_feed_status()


@app.post("/api/outages/refresh")
def outages_refresh():
    if not config.LIVE_OUTAGES:
        raise HTTPException(409, "Live outage import is turned off (LIVE_OUTAGES=0).")
    return services.import_live_outages()


def _outage_loop():
    import time as _t
    _t.sleep(5)
    while True:
        try:
            services.import_live_outages()
        except Exception as e:  # noqa: BLE001
            services._outage_state["error"] = type(e).__name__
        _t.sleep(600)


@app.on_event("startup")
def _start_outages():
    import threading as _th
    if config.DATABASE_URL and config.LIVE_OUTAGES:
        _th.Thread(target=_outage_loop, daemon=True, name="live-outages").start()


# ------------------------------------------------------------------ exposure receipts (verifiable)
@app.post("/api/items/{item_id}/receipt")
def receipt(item_id: int):
    return services.create_receipt(_item_or_404(item_id))


@app.get("/api/receipts/{code}")
def get_receipt(code: str):
    with db.system():   # the receipt code is the permission
        r = services.verify_receipt(code)
    if not r:
        raise HTTPException(404, "No exposure receipt with this code.")
    return r


@app.get("/r/{code}")
def receipt_page(code: str):
    return FileResponse(config.ROOT / "static" / "receipt.html")


# ------------------------------------------------------------------ developer platform
class DeviceIn(BaseModel):
    item_id: int
    label: str | None = None


@app.post("/api/devices")
def add_device(body: DeviceIn):
    _item_or_404(body.item_id)
    return {"token": services.create_device(body.item_id, body.label)}


@app.get("/api/devices")
def list_devices():
    return services.devices()


@app.delete("/api/devices/{token}")
def remove_device(token: str):
    db.execute("UPDATE devices SET revoked = TRUE WHERE token = %s AND item_id IN (SELECT id FROM items WHERE user_id = %s)", (token, db.me()))
    return {"ok": True}


class DeviceReading(BaseModel):
    temp_c: float
    ts: datetime | None = None
    humidity: float | None = None


class DeviceBatch(BaseModel):
    readings: list[DeviceReading] | None = None
    temp_c: float | None = None


@app.post("/v1/readings")
def device_readings(body: DeviceBatch, request: Request):
    """Public ingest for sensors and partner apps: Authorization: Bearer <device token>. Accepts one reading
    ({"temp_c": 4.2}) or a batch ({"readings": [{"ts": ..., "temp_c": ...}]}); late batches are repaired."""
    auth = request.headers.get("authorization", "")
    dev = services.device_for(auth.removeprefix("Bearer ").strip()) if auth.lower().startswith("bearer ") else None
    if not dev:
        raise HTTPException(401, "Missing or unknown device token (Authorization: Bearer <token>).")
    rows = body.readings or ([DeviceReading(temp_c=body.temp_c)] if body.temp_c is not None else [])
    if not rows:
        raise HTTPException(422, "Send temp_c or readings.")
    now_ = services.now()
    res = services.ingest(dev["item_id"], [{"ts": r.ts or now_, "temp_c": r.temp_c, "humidity": r.humidity} for r in rows],
                          source="device", auto_refresh=True)
    db.execute("UPDATE devices SET last_seen = now(), readings = readings + %s WHERE token = %s",
               (res.get("inserted", 0), dev["token"]))
    return res


class WebhookIn(BaseModel):
    url: str


@app.post("/api/webhooks")
def add_webhook(body: WebhookIn):
    if not body.url.startswith(("https://", "http://")):
        raise HTTPException(422, "The URL must start with https:// or http://")
    return services.create_webhook(body.url)


@app.get("/api/webhooks")
def list_webhooks():
    return services.webhooks()


@app.delete("/api/webhooks/{hook_id}")
def remove_webhook(hook_id: int):
    db.execute("UPDATE webhooks SET active = FALSE WHERE id = %s AND user_id = %s", (hook_id, db.me()))
    return {"ok": True}


@app.get("/api/schema/stability-model")
def stability_schema():
    """The open format LIFELOG uses for a medicine's storage rules (JSON Schema)."""
    return StabilityModel.model_json_schema()


@app.get("/api/products/{product_id}/model")
def product_model(product_id: int):
    p = db.one("SELECT id, name, model, source, source_label, source_url FROM products WHERE id = %s", (product_id,))
    if not p:
        raise HTTPException(404, "unknown product")
    return p


@app.get("/api/products")
def list_products():
    return db.query("SELECT id, name, source, source_label, source_url FROM products ORDER BY name")


# ------------------------------------------------------------------ demo controls
@app.post("/api/demo/{scenario}")
def demo(scenario: str, auto_refresh: bool = True):
    with db.system():
        return _demo(scenario, auto_refresh)


def _demo(scenario: str, auto_refresh: bool):
    if scenario == "reset":
        if not config.reset_allowed():
            raise HTTPException(403, f"Reset is turned off: this server uses a shared database ({config.db_host()}) and "
                                     "reset erases everything in it. Set ALLOW_DEMO_RESET=1 in .env if you really mean it.")
        sim.stop()
        seed.apply_schema()
        res = seed.run(with_weather=True)
        forecast_ml.train_all()
        services.check_alerts()
        return res
    if scenario == "hot_car":
        res = seed.scenario_hot_car(seed.me_item("EpiPen"))
    elif scenario == "late_upload":
        iid = seed.me_item("Insulin")
        res = {"item_id": iid, **services.ingest(iid, seed.late_upload_points(iid), "sensor", auto_refresh)}
    elif scenario == "storm":
        res = seed.scenario_storm()
    elif scenario == "clear_storm":
        res = seed.scenario_clear_storm()
    else:
        raise HTTPException(404, "unknown scenario")
    services.check_alerts()
    return res
