"""LIFELOG Home API. Run: .venv/Scripts/uvicorn lifelog.app:app --reload"""
import json
from datetime import datetime, timedelta

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import assistant, config, db, engine, gem, realtime, seed, services, sim
from .models import StabilityModel

app = FastAPI(title="LIFELOG Home")
app.mount("/static", StaticFiles(directory=config.ROOT / "static"), name="static")


@app.exception_handler(gem.GeminiUnavailable)
async def _no_gemini(_: Request, exc: gem.GeminiUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/")
def index():
    return FileResponse(config.ROOT / "static" / "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(config.ROOT / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.on_event("startup")
def _start_realtime():
    realtime.start()
    services.warm_benchmark()


def _item_or_404(item_id: int, raw: bool = False) -> dict:
    d = services.item_detail(item_id, raw)
    if not d:
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
    out = {"gemini": bool(config.GEMINI_API_KEY), "model": config.GEMINI_MODEL, "database": False}
    try:
        out["extensions"] = {r["extname"]: r["extversion"] for r in
                             db.query("SELECT extname, extversion FROM pg_extension")}
        out["database"] = True
        out["watermark"] = services.cagg_watermark()
    except Exception as e:  # noqa: BLE001
        out["db_error"] = str(e)
    return out


# ------------------------------------------------------------------ items
@app.get("/api/items")
def items(mine: bool = True):
    me = db.one("SELECT id FROM users WHERE is_me")
    return services.list_items(me["id"] if (mine and me) else None)


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
    me = db.one("SELECT id FROM users WHERE is_me")
    return {"token": services.create_share(me["id"], body.label)}


@app.get("/api/shares")
def shares():
    return db.query("SELECT token, label, created_at FROM shares s JOIN users u ON u.id = s.user_id WHERE u.is_me AND NOT revoked ORDER BY created_at DESC")


@app.delete("/api/share/{token}")
def revoke_share(token: str):
    db.execute("UPDATE shares SET revoked = TRUE WHERE token = %s", (token,))
    return {"ok": True}


@app.get("/api/share/{token}")
def shared(token: str):
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


@app.post("/api/items/{item_id}/advice")
def advice(item_id: int, lang: str = "en"):
    return gem.advise(_llm_state(_item_or_404(item_id)), lang)


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
    return {"letter": gem.refill_letter(ctx, lang)}


class TripIn(BaseModel):
    itinerary: str


@app.post("/api/items/{item_id}/trip")
def trip(item_id: int, body: TripIn, lang: str = "en"):
    _item_or_404(item_id)
    plan = gem.parse_trip(body.itinerary, services.now())
    sim = services.simulate_trip(item_id, plan["legs"])
    sim["advice"] = gem.trip_advice(sim, lang)
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
        model = gem.extract_label_text(f"{hit['brand']} ({hit['generic']})", hit["text"])
        return {"model": model, "method": "openfda", "source_label": hit["source_label"], "source_url": hit["source_url"],
                "sources": [{"title": hit["source_label"], "url": hit["source_url"]}], "label_text": hit["text"][:1500]}
    found = gem.search_label(body.name)
    if "NOT FOUND" in found["text"][:200] or not found["text"].strip():
        raise HTTPException(404, f"No official storage information found for '{body.name}'. Try uploading a photo of the label.")
    model = gem.extract_label_text(body.name, found["text"])
    first = found["sources"][0] if found["sources"] else {"title": "Google Search", "url": None}
    return {"model": model, "method": "google_search", "source_label": f"{first['title']} (found by Gemini web search)",
            "source_url": first["url"], "sources": found["sources"], "label_text": found["text"][:1500]}


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
    me = db.one("SELECT id FROM users WHERE is_me")
    iid = db.one("INSERT INTO items (user_id, product_id, nickname, started_at) VALUES (%s,%s,%s, now() - interval '1 hour') RETURNING id",
                 (me["id"], pid, body.nickname))["id"]
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
    return services.ingest(body.item_id, [r.model_dump() for r in body.readings], body.source, body.auto_refresh)


@app.post("/api/voice")
async def voice(item_id: int = Form(...), text: str | None = Form(None), audio: UploadFile | None = File(None)):
    d = _item_or_404(item_id)
    mine = [{"id": i["id"], "nickname": i["nickname"]} for i in items()]
    report = gem.parse_voice(services.now(), mine,
                             audio=await audio.read() if audio else None,
                             mime_type=(audio.content_type if audio else "audio/wav") or "audio/wav", text=text)
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
    return services.rescue()


@app.post("/api/rescue/plan")
def rescue_plan(lang: str = "en"):
    r = services.rescue()
    at_risk = [p for p in r["people"] if p["at_risk"]] or r["people"][:5]
    outage = r["outages"]["features"][0]["properties"] if r["outages"]["features"] else {}
    return {"plan": gem.rescue_plan(at_risk[:10], outage, lang)}


# ------------------------------------------------------------------ porch heat index
@app.get("/api/porch")
def porch():
    return services.porch_stats()


@app.post("/api/porch/brief")
def porch_brief():
    return {"brief": gem.porch_brief(services.porch_stats())}


# ------------------------------------------------------------------ alerts (written by the check_alerts job)
@app.get("/api/alerts")
def get_alerts(mine: bool = True, include_resolved: bool = False):
    return services.alerts(mine_only=mine, include_resolved=include_resolved)


@app.post("/api/alerts/check")
def run_alert_check():
    services.check_alerts()
    return services.alerts(mine_only=False)


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: int):
    db.execute("UPDATE alerts SET acked = TRUE WHERE id = %s", (alert_id,))
    return {"ok": True}


# ------------------------------------------------------------------ under the hood
@app.get("/api/tiger")
def tiger(fresh: bool = False):
    return {**services.tiger_stats(fresh), "listeners": realtime.subscriber_count()}


# ------------------------------------------------------------------ live sensor simulator
@app.post("/api/sim/{action}")
def simulator(action: str):
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


# ------------------------------------------------------------------ demo controls
@app.post("/api/demo/{scenario}")
def demo(scenario: str, auto_refresh: bool = True):
    if scenario == "reset":
        sim.stop()
        seed.apply_schema()
        return seed.run(with_weather=True)
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
