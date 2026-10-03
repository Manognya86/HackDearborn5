"""End-to-end validation of every endpoint against a running LIFELOG (local uvicorn or the Docker container).

    .venv/Scripts/python scripts/validate_app.py                      # http://localhost:8000, all features
    .venv/Scripts/python scripts/validate_app.py --no-gemini          # skip the Gemini-backed buttons
    .venv/Scripts/python scripts/validate_app.py --demo               # also run the demo scenarios (changes demo data)
    .venv/Scripts/python scripts/validate_app.py --base http://host:port

Writes go to a temporary "smoke test" medicine that is deleted at the end. Gemini features PASS when Gemini
answers, are reported DEGRADED when Gemini is overloaded / out of quota but the app returns its clear 503
message, and FAIL on a crash (500), a hang or a wrong shape. Exit code 1 if anything FAILs."""
import argparse
import json
import struct
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
from lifelog import db  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
ap.add_argument("--no-gemini", action="store_true")
ap.add_argument("--demo", action="store_true")
ap.add_argument("--gemini-gap", type=float, default=15, help="seconds between Gemini calls (free-tier keys allow only a few per minute)")
args = ap.parse_args()
cx = httpx.Client(base_url=args.base, timeout=240)
results: list[tuple[str, str, str]] = []


def check(name, fn, gemini=False):
    t = time.perf_counter()
    try:
        detail = fn() or ""
        status = "PASS"
    except GeminiDown as e:
        status, detail = "DEGRADED", str(e)
    except Exception as e:  # noqa: BLE001
        status, detail = "FAIL", f"{type(e).__name__}: {str(e)[:300]}"
    ms = (time.perf_counter() - t) * 1000
    results.append((status, name, detail))
    print(f"[{status:8}] {name} ({ms:,.0f} ms){': ' + str(detail)[:160] if detail else ''}", flush=True)


class GeminiDown(Exception):
    pass


def ok(r: httpx.Response, gemini=False):
    if gemini and r.status_code == 503:
        detail = r.json().get("detail", "")
        if "Gemini" in detail:
            raise GeminiDown(detail[:200])
    if r.status_code >= 400:
        raise AssertionError(f"HTTP {r.status_code}: {r.text[:300]}")
    ctype = r.headers.get("content-type", "")
    return r.json() if "json" in ctype else r.text


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


def png_bytes(w=48, h=48) -> bytes:
    raw = b"".join(b"\x00" + bytes([200, 200, 200]) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def label_pdf() -> bytes:
    text = ["DEMOPEN (examplumab) injection, prefilled pen", "16 HOW SUPPLIED/STORAGE AND HANDLING",
            "Store in a refrigerator at 2C to 8C (36F to 46F) in the original carton to protect from light.",
            "Do not freeze. Do not use if it has been frozen.",
            "If needed, the pen may be stored at room temperature up to 30C (86F) for up to 14 days.",
            "Discard if not used within 14 days at room temperature."]
    stream = "BT /F1 11 Tf 40 760 Td 14 TL " + " ".join(f"({t}) Tj T*" for t in text) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n"
    return out.encode("latin-1")


# ------------------------------------------------------------------ pages and health
check("GET / (app shell)", lambda: must("<html" in ok(cx.get("/")).lower(), "no html"))
check("GET /manifest.webmanifest", lambda: must(ok(cx.get("/manifest.webmanifest")), "empty"))
check("GET /sw.js", lambda: must("cache" in ok(cx.get("/sw.js")).lower(), "no service worker"))


def health():
    h = ok(cx.get("/api/health"))
    must(h["database"] and h["ok"], f"health not ok: { {k: h.get(k) for k in ('database', 'jobs_failing_now', 'listen_thread_alive')} }")
    must(h["listen_connected"], "LISTEN not connected")
    return f"TimescaleDB {h['timescaledb_version']}, {h['jobs']} jobs, gemini key {'set' if h['gemini'] else 'MISSING'}"


check("GET /api/health", health)

# ------------------------------------------------------------------ medicines (Tiger)
items = []


def list_items():
    global items
    items = ok(cx.get("/api/items"))
    must(items, "no medicines: run scripts/setup_db.py")
    for i in items:
        for k in ("remaining", "status", "forecast", "dates", "zone"):
            must(k in i, f"item {i['id']} missing {k}")
        must(0 <= i["remaining"] <= 1, "remaining out of range")
    return f"{len(items)} medicines: " + ", ".join(f"{i['nickname'][:18]} {i['remaining'] * 100:.1f}% {i['status']['label']}" for i in items)


check("GET /api/items", list_items)
check("GET /api/items?mine=false", lambda: f"{len(ok(cx.get('/api/items', params={'mine': 'false'})))} medicines")
IID = items[0]["id"] if items else 1


def detail():
    d = ok(cx.get(f"/api/items/{IID}"))
    for k in ("state", "timeline", "episodes", "whatif", "forecast", "burn_last_hour", "precautions", "stats", "patterns", "status"):
        must(k in d, f"missing {k}")
    must(d["timeline"], "empty timeline")
    return f"{len(d['timeline'])} buckets, trend {d['forecast']['trend']}, {len(d['precautions'])} precautions"


check("GET /api/items/{id}", detail)
check("GET /api/items/{id}?raw=true", lambda: must(ok(cx.get(f"/api/items/{IID}", params={"raw": "true"}))["timeline"], "empty"))


def compare():
    c = ok(cx.get(f"/api/items/{IID}/compare"))
    must(abs(c["aggregate"]["remaining"] - c["raw_truth"]["remaining"]) < 0.01 or c["log"], "aggregate != raw with no late data")
    return f"aggregate {c['aggregate']['remaining']:.4f} vs raw {c['raw_truth']['remaining']:.4f}, watermark {c['watermark']}"


check("GET /api/items/{id}/compare", compare)
check("GET /api/items/{id}/history", lambda: f"{len(ok(cx.get(f'/api/items/{IID}/history', params={'days': 30})))} days")


def accuracy():
    a = ok(cx.get(f"/api/items/{IID}/accuracy"))
    return json.dumps(a)[:150]


check("GET /api/items/{id}/accuracy", accuracy)
check("GET /api/items/{id}/recalls (openFDA)", lambda: json.dumps(ok(cx.get(f"/api/items/{IID}/recalls")))[:120])
check("GET /api/items/{id}/export.csv", lambda: must(ok(cx.get(f"/api/items/{IID}/export.csv")).count("\n") > 10, "short csv"))
check("GET /api/items/99999 -> 404", lambda: must(cx.get("/api/items/99999").status_code == 404, "expected 404"))

# ------------------------------------------------------------------ alerts, rescue, porch, tiger, stream
check("GET /api/alerts", lambda: f"{len(ok(cx.get('/api/alerts')))} open")
check("GET /api/alerts (all, resolved)", lambda: f"{len(ok(cx.get('/api/alerts', params={'mine': 'false', 'include_resolved': 'true'})))} rows")
check("POST /api/alerts/check", lambda: ok(cx.post("/api/alerts/check")))
check("GET /api/rescue", lambda: f"{len(ok(cx.get('/api/rescue'))['people'])} people in outages")
check("GET /api/porch", lambda: f"{len(ok(cx.get('/api/porch')))} zip x carrier rows")


def tiger():
    t = ok(cx.get("/api/tiger"))
    b = t["benchmark"]
    must(b["budget_all_items"]["continuous_aggregate_ms"] > 0, "no benchmark")
    return (f"calendar {b['calendar_30d']['speedup']}x, budget {b['budget_all_items']['speedup']}x, "
            f"{len(t['jobs'])} jobs, {len(t['hypertables'])} hypertables")


check("GET /api/tiger", tiger)
check("POST /api/sim/status", lambda: ok(cx.post("/api/sim/status")))


def stream():
    with cx.stream("GET", "/api/stream", timeout=10) as r:
        for line in r.iter_lines():
            if line.startswith("event: hello"):
                return "hello received"
    raise AssertionError("no hello event")


check("GET /api/stream (SSE)", stream)

# ------------------------------------------------------------------ caregiver share
token = {}


def share():
    token["t"] = ok(cx.post("/api/share", json={"label": "smoke test"}))["token"]
    v = ok(cx.get(f"/api/share/{token['t']}"))
    must(v, "empty shared view")
    must("<html" in ok(cx.get(f"/s/{token['t']}")).lower(), "no share page")
    must(any(s["token"] == token["t"] for s in ok(cx.get("/api/shares"))), "not listed")
    ok(cx.delete(f"/api/share/{token['t']}"))
    must(cx.get(f"/api/share/{token['t']}").status_code == 404, "revoked link still works")
    return "create, view, list, revoke"


check("share link lifecycle", share)

# ------------------------------------------------------------------ writes on a temporary medicine
probe = {}
MODEL = {"product_name": "Smoke test pen", "form": "pen", "target_min_c": 2, "target_max_c": 8,
         "target_quote": "Store at 2-8C (smoke test)", "freeze_discard": True, "freeze_c": 0, "freeze_quote": "Do not freeze",
         "cold_ok": True, "bands": [{"label": "Room", "min_c": 8, "max_c": 30, "budget_hours": 672, "quote": "30C for 28 days"}],
         "above_limit_budget_hours": 8, "above_limit_is_assumption": True, "above_limit_quote": "Not stated on label",
         "in_use_days": 28, "visual_checks": [], "discard_rules": [], "notes": ""}


def create():
    r = ok(cx.post("/api/products", json={"model": MODEL, "nickname": "smoke test pen"}))
    probe.update(r)
    # start the budget 8 h back so a batch arriving 6 h late (outside the 2 h refresh-policy window) counts
    db.execute("UPDATE items SET started_at = now() - INTERVAL '8 hours' WHERE id = %s", (r["item_id"],))
    return f"item {r['item_id']}"


def ingest():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    pts = [{"ts": (now - timedelta(minutes=40 - k)).isoformat(), "temp_c": 4.0 + 0.1 * k} for k in range(41)]
    pts += [{"ts": pts[-1]["ts"], "temp_c": 5.0}, {"ts": now.isoformat(), "temp_c": 999}]   # duplicate + impossible
    r = ok(cx.post("/api/ingest", json={"item_id": probe["item_id"], "readings": pts}))
    must(r["inserted"] == 41 and r["rejected"] == 2, f"expected 41 inserted / 2 rejected (duplicate + 999C), got {r}")
    late = [{"ts": (now - timedelta(hours=6, minutes=k)).isoformat(), "temp_c": 35.0} for k in range(20)]
    r2 = ok(cx.post("/api/ingest", json={"item_id": probe["item_id"], "readings": late, "auto_refresh": False}))
    must(r2["late"] and not r2["refreshed"], f"late batch not detected: {r2}")
    c = ok(cx.get(f"/api/items/{probe['item_id']}/compare"))
    must(c["raw_truth"]["remaining"] < c["aggregate"]["remaining"], "late rows should be hidden before repair")
    ok(cx.post(f"/api/items/{probe['item_id']}/repair"))
    c = ok(cx.get(f"/api/items/{probe['item_id']}/compare"))
    must(abs(c["raw_truth"]["remaining"] - c["aggregate"]["remaining"]) < 1e-9, "repair didn't reconcile")
    return f"validation ok; late batch hidden then repaired ({c['aggregate']['remaining'] * 100:.2f}%)"


def trend():
    d = ok(cx.get(f"/api/items/{probe['item_id']}"))
    f = d["forecast"]
    must(f["trend"] == "warming" and 4 < f["slope_c_per_h"] < 8, f"expected ~6C/h warming, got {f['trend']} {f['slope_c_per_h']}")
    kinds = {a["kind"] for a in d["alerts"]} | {r["kind"] for r in db.query(
        "SELECT kind FROM current_conditions() WHERE item_id = %s", (probe["item_id"],))}
    must("warming_trend" in kinds, f"no warming_trend alert ({kinds})")
    return f"warming {f['slope_c_per_h']:.1f}C/h, leaves range in {f['minutes_to_limit']} min, alert raised"


def patch_dates():
    ok(cx.patch(f"/api/items/{probe['item_id']}", json={"opened_at": None, "expires_on": "2020-01-01", "lot": "SMOKE1"}))
    d = ok(cx.get(f"/api/items/{probe['item_id']}"))
    must(d["status"]["code"] == "DO_NOT_USE" and d["dates"]["expired"], "expired item should be Do not use")
    return "expired -> Do not use"


check("POST /api/products (create medicine)", create)
if probe:
    check("POST /api/ingest (validation, late data, repair)", ingest)
    check("live trend + warming_trend alert", trend)

# ------------------------------------------------------------------ Gemini-backed features
if not args.no_gemini:
    G = dict(gemini=True)
    _check = check

    def check(name, fn, gemini=False):  # noqa: F811  pace Gemini calls under the free-tier per-minute limit
        _check(name, fn)
        time.sleep(args.gemini_gap)
    check("POST /api/ask (chatbot)", lambda: (lambda r: (must(r["answer"].strip(), "empty answer"),
          f"{'OFFLINE (no AI) ' if r.get('offline') else ''}model={r.get('model')} tools={[c['name'] for c in r['tool_calls']]} :: {r['answer'][:120]!r}")[1])(
        ok(cx.post("/api/ask", json={"question": "Which of my medicines is in the worst shape, and why?", "history": []}), **G)))
    check("POST /api/items/{id}/advice", lambda: (lambda r: f"{r.get('verdict')}: {r.get('headline', '')[:100]}")(ok(cx.post(f"/api/items/{IID}/advice"), **G)))
    check("POST /api/items/{id}/letter", lambda: must(len(ok(cx.post(f"/api/items/{IID}/letter"), **G)["letter"]) > 200, "short letter"))
    check("POST /api/items/{id}/trip", lambda: (lambda r: f"{len(r.get('legs', []))} legs; {str(r.get('advice', ''))[:90]!r}")(
        ok(cx.post(f"/api/items/{IID}/trip", json={"itinerary": "Tomorrow 9am drive Dearborn to Chicago, 4 hours, pen in a cooler, then hotel."}), **G)))
    check("POST /api/items/{id}/visual (photo check)", lambda: json.dumps(ok(cx.post(f"/api/items/{IID}/visual",
          files={"photo": ("pen.png", png_bytes(), "image/png")}), **G))[:140])
    check("POST /api/products/extract (label PDF)", lambda: (lambda m: (must(m["target_max_c"] == 8 and m["freeze_discard"], f"wrong model: {m}"),
          f"{m['product_name']}: {m['target_min_c']}-{m['target_max_c']}C, bands {[(b['max_c'], b['budget_hours']) for b in m['bands']]}")[1])(
        ok(cx.post("/api/products/extract", files={"label": ("label.pdf", label_pdf(), "application/pdf")}), **G)))
    check("POST /api/products/lookup (openFDA + Gemini)", lambda: (lambda r: f"{r['method']}: {r['model']['product_name']} {r['model']['target_min_c']}-{r['model']['target_max_c']}C")(
        ok(cx.post("/api/products/lookup", json={"name": "Trulicity"}), **G)))
    if probe:
        check("POST /api/voice (text report)", lambda: (lambda r: f"{len(r['report']['events'])} events, {r['ingest'].get('inserted', 0)} readings")(
            ok(cx.post("/api/voice", data={"item_id": str(probe["item_id"]), "text": "The smoke test pen sat on the kitchen counter for the last 20 minutes."}), **G)))
    check("POST /api/rescue/plan", lambda: must(ok(cx.post("/api/rescue/plan"), **G)["plan"].strip(), "empty plan"))
    check("POST /api/porch/brief", lambda: must(ok(cx.post("/api/porch/brief"), **G)["brief"].strip(), "empty brief"))

    check = _check
if probe:
    check("PATCH /api/items/{id} (dates)", patch_dates)

# ------------------------------------------------------------------ demo scenarios (optional, changes demo data)
if args.demo:
    check("POST /api/demo/hot_car", lambda: ok(cx.post("/api/demo/hot_car")))
    check("POST /api/demo/late_upload?auto_refresh=false", lambda: (lambda r: must(r.get("late"), f"not late: {r}"))(ok(cx.post("/api/demo/late_upload", params={"auto_refresh": "false"}))))
    check("POST /api/demo/storm", lambda: ok(cx.post("/api/demo/storm")))
    check("GET /api/rescue during storm", lambda: (lambda r: (must(r["people"], "nobody in outage"), f"{sum(p['at_risk'] for p in r['people'])} at risk; {r['people'][0]['warming_model']}")[1])(ok(cx.get("/api/rescue"))))
    check("POST /api/demo/clear_storm", lambda: ok(cx.post("/api/demo/clear_storm")))
    check("POST repair late windows (insulin)", lambda: ok(cx.post(f"/api/items/{[i for i in items if 'Insulin' in i['nickname']][0]['id']}/repair")))

# ------------------------------------------------------------------ cleanup
if probe:
    span = db.one("SELECT min(ts) AS a, max(ts) AS b FROM readings WHERE item_id = %s", (probe["item_id"],))
    db.execute("DELETE FROM readings WHERE item_id = %s", (probe["item_id"],))
    db.execute("DELETE FROM ingest_log WHERE item_id = %s", (probe["item_id"],))
    db.execute("DELETE FROM items WHERE id = %s", (probe["item_id"],))
    db.execute("DELETE FROM products WHERE id = %s", (probe["product_id"],))
    if span and span["a"]:
        db.refresh_readings(span["a"], span["b"])
    print("cleanup: temporary medicine removed")

n = {s: sum(1 for r in results if r[0] == s) for s in ("PASS", "DEGRADED", "FAIL")}
print(f"\n{n['PASS']} passed, {n['DEGRADED']} degraded (Gemini unavailable, handled), {n['FAIL']} failed")
for s, name, d in results:
    if s != "PASS":
        print(f"  {s}: {name}: {str(d)[:200]}")
sys.exit(1 if n["FAIL"] else 0)
