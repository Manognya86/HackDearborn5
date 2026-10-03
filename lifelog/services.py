"""Business logic on top of Tiger Data. SQL does the heavy lifting; Python shapes results."""
from datetime import datetime, timedelta, timezone

from . import db, engine, weather

LATE_GRACE = timedelta(minutes=10)


def now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ items
def list_items(user_id: int | None = None) -> list[dict]:
    rows = db.query("""
        SELECT i.id, i.nickname, i.started_at, i.user_id, u.name AS user_name, u.is_me,
               p.id AS product_id, p.name AS product_name, p.model, p.source AS product_source
        FROM items i JOIN users u ON u.id = i.user_id JOIN products p ON p.id = i.product_id
        WHERE (%(u)s::int IS NULL OR i.user_id = %(u)s)
        ORDER BY u.is_me DESC, i.id""", {"u": user_id})
    for r in rows:
        r.update(summary(r["id"], r["model"]))
    return rows


def summary(item_id: int, model: dict, raw: bool = False) -> dict:
    last = db.one("""
        SELECT t.bucket, t.avg_temp, t.min_temp, t.zone, t.used
        FROM item_timeline(%s, %s) t ORDER BY t.bucket DESC LIMIT 1""", (item_id, raw))
    if not last:
        return {"remaining": 1.0, "current_temp": None, "zone": "No data", "hours_left": None,
                "last_reading": None, "stale_minutes": None}
    remaining = max(0.0, 1.0 - last["used"])
    stale = (now() - last["bucket"]).total_seconds() / 60 - 5
    return {
        "remaining": remaining,
        "current_temp": last["avg_temp"],
        "zone": last["zone"],
        "hours_left": engine.hours_left(remaining, last["avg_temp"], model),
        "last_reading": last["bucket"],
        "stale_minutes": max(0.0, stale),
    }


def item_detail(item_id: int, raw: bool = False) -> dict | None:
    item = db.one("""
        SELECT i.*, p.name AS product_name, p.model, p.source AS product_source, u.name AS user_name
        FROM items i JOIN products p ON p.id = i.product_id JOIN users u ON u.id = i.user_id
        WHERE i.id = %s""", (item_id,))
    if not item:
        return None
    m = item["model"]
    timeline = db.query("SELECT * FROM item_timeline(%s, %s)", (item_id, raw))
    episodes = db.query("SELECT * FROM item_episodes(%s, %s)", (item_id, raw))
    state = summary(item_id, m, raw)
    gaps = []
    for a, b in zip(timeline, timeline[1:]):
        if b["bucket"] - a["bucket"] > timedelta(minutes=20):
            gaps.append({"from": a["bucket"] + timedelta(minutes=5), "to": b["bucket"],
                         "hours": (b["bucket"] - a["bucket"]).total_seconds() / 3600})
    if timeline and state["stale_minutes"] and state["stale_minutes"] > 20:
        gaps.append({"from": timeline[-1]["bucket"] + timedelta(minutes=5), "to": now(),
                     "hours": state["stale_minutes"] / 60, "ongoing": True})
    burners = sorted([e for e in episodes if e["burn"] > 0.0005], key=lambda e: -e["burn"])
    cur = state["current_temp"] if state["current_temp"] is not None else (m["target_min_c"] + m["target_max_c"]) / 2
    return {
        "item": item,
        "state": state,
        "timeline": [{"t": r["bucket"], "temp": r["avg_temp"], "min": r["min_temp"], "max": r["max_temp"],
                      "remaining": 1 - r["used"], "zone": r["zone"]} for r in timeline],
        "episodes": episodes,
        "burners": burners[:8],
        "gaps": gaps,
        "whatif": engine.whatif(state["remaining"], cur, m),
        "patterns": similar_patterns([r["avg_temp"] for r in timeline[-12 * 72:]]),
    }


def similar_patterns(temps: list[float], k: int = 3) -> list[dict]:
    if not temps:
        return []
    fp = engine.vector_literal(engine.fingerprint(temps))
    return db.query("""
        SELECT name, description, outcome, round((1 - (fingerprint <=> %s::vector))::numeric, 3) AS similarity
        FROM pattern_library ORDER BY fingerprint <=> %s::vector LIMIT %s""", (fp, fp, k))


# ------------------------------------------------------------------ ingest (late-data aware)
def cagg_watermark() -> datetime | None:
    """Materialization watermark of readings_5m: buckets before this are served from the materialized
    table, so late rows older than it stay invisible until refreshed."""
    try:
        row = db.one("""
            SELECT _timescaledb_functions.to_timestamp(_timescaledb_functions.cagg_watermark(h.id)) AS wm
            FROM _timescaledb_catalog.continuous_agg ca
            JOIN _timescaledb_catalog.hypertable h ON h.id = ca.mat_hypertable_id
            WHERE ca.user_view_name = 'readings_5m'""")
        return row["wm"] if row else None
    except Exception:
        return None


def ingest(item_id: int, readings: list[dict], source: str = "sensor", auto_refresh: bool = True) -> dict:
    if not readings:
        return {"inserted": 0}
    rows = [(r["ts"], item_id, float(r["temp_c"]), r.get("humidity"), source) for r in readings]
    with db.conn() as c, c.cursor() as cur:
        with cur.copy("COPY readings (ts, item_id, temp_c, humidity, source) FROM STDIN") as cp:
            for row in rows:
                cp.write_row(row)
    min_ts = min(r[0] for r in rows)
    max_ts = max(r[0] for r in rows)
    if isinstance(min_ts, str):
        min_ts, max_ts = datetime.fromisoformat(min_ts), datetime.fromisoformat(max_ts)
    wm = cagg_watermark()
    late = bool(wm and min_ts < wm) or (wm is None and min_ts < now() - LATE_GRACE)
    refreshed = False
    if late and auto_refresh:
        db.refresh_readings(min_ts, max_ts)
        refreshed = True
    db.execute("""INSERT INTO ingest_log (item_id, source, n, min_ts, max_ts, late, refreshed)
                  VALUES (%s,%s,%s,%s,%s,%s,%s)""", (item_id, source, len(rows), min_ts, max_ts, late, refreshed))
    return {"inserted": len(rows), "late": late, "refreshed": refreshed,
            "min_ts": min_ts, "max_ts": max_ts, "watermark": wm}


def repair_late(item_id: int) -> dict:
    """Refresh every window that received late data but wasn't refreshed."""
    rows = db.query("SELECT id, min_ts, max_ts FROM ingest_log WHERE item_id=%s AND late AND NOT refreshed",
                    (item_id,))
    for r in rows:
        db.refresh_readings(r["min_ts"], r["max_ts"])
        db.execute("UPDATE ingest_log SET refreshed = TRUE WHERE id = %s", (r["id"],))
    return {"repaired_batches": len(rows)}


# ------------------------------------------------------------------ outage rescue
def outages_geojson() -> dict:
    rows = db.query("""SELECT id, name, est_restore_at, indoor_temp_c, ST_AsGeoJSON(area)::json AS geom
                       FROM outages WHERE active""")
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": r.pop("geom"), "properties": r} for r in rows]}


def rescue() -> dict:
    """Rank everyone inside an active outage by hours of medicine life left vs hours to restore."""
    rows = db.query("""
        SELECT u.id AS user_id, u.name, u.lat, u.lon, o.id AS outage_id, o.name AS outage,
               o.est_restore_at, o.indoor_temp_c, i.id AS item_id, i.nickname, p.name AS product_name, p.model,
               (SELECT json_agg(x) FROM (
                    SELECT name, kind, lat, lon,
                           round((ST_Distance(geom::geography, u.geom::geography) / 1609.34)::numeric, 2) AS miles
                    FROM (
                        SELECT r.name, r.kind, r.lat, r.lon, r.geom FROM refuges r WHERE r.has_fridge
                        UNION ALL
                        SELECT h.name || ' (neighbor host)', 'neighbor', h.lat, h.lon, h.geom
                        FROM users h WHERE h.can_host AND h.id <> u.id
                    ) cand
                    WHERE NOT EXISTS (SELECT 1 FROM outages o2 WHERE o2.active AND ST_Contains(o2.area, cand.geom))
                    ORDER BY cand.geom <-> u.geom LIMIT 3) x) AS refuges
        FROM outages o
        JOIN users u ON ST_Contains(o.area, u.geom)
        JOIN items i ON i.user_id = u.id
        JOIN products p ON p.id = i.product_id
        WHERE o.active""")
    t = now()
    people = []
    for r in rows:
        s = summary(r["item_id"], r["model"])
        hrs_restore = max(0.0, (r["est_restore_at"] - t).total_seconds() / 3600)
        hl = engine.outage_hours_left(s["remaining"], s["current_temp"], r["indoor_temp_c"], r["model"])
        people.append({
            "user_id": r["user_id"], "name": r["name"], "lat": r["lat"], "lon": r["lon"],
            "item_id": r["item_id"], "medicine": r["nickname"], "product": r["product_name"],
            "remaining": s["remaining"], "current_temp": s["current_temp"],
            "hours_left": hl, "hours_to_restore": hrs_restore,
            "at_risk": hl is not None and hl < hrs_restore,
            "refuges": r["refuges"] or [], "outage": r["outage"],
        })
    people.sort(key=lambda p: (not p["at_risk"], p["hours_left"] if p["hours_left"] is not None else 1e9))
    users = db.query("SELECT id, name, lat, lon, can_host, is_me FROM users")
    refuges = db.query("SELECT name, kind, lat, lon FROM refuges")
    return {"people": people, "outages": outages_geojson(), "users": users, "refuges": refuges}


# ------------------------------------------------------------------ trip pre-check
SETTING_TEMP = {
    "indoors": lambda air: 22.0,
    "outdoors": lambda air: air,
    "parked_car": lambda air: air + 20 if air > 10 else air + 5,
    "on_body": lambda air: 31.0,
    "carry_on": lambda air: 23.0,
    "checked_bag": lambda air: min(22.0, max(5.0, air)),
    "cooler": lambda air: 10.0,
    "fridge": lambda air: 5.0,
}


def simulate_trip(item_id: int, legs: list[dict]) -> dict:
    d = item_detail(item_id)
    m = d["item"]["model"]
    remaining = d["state"]["remaining"]
    out = []
    for leg in legs:
        start = datetime.fromisoformat(leg["start_iso"])
        end = datetime.fromisoformat(leg["end_iso"])
        if start.tzinfo is None:
            start, end = start.replace(tzinfo=timezone.utc), end.replace(tzinfo=timezone.utc)
        air_by_hour, src = {}, "gemini estimate"
        loc = weather.geocode(leg["place"])
        if loc:
            try:
                air_by_hour = weather.hourly(loc[0], loc[1], start.date(), end.date())
                src = "open-meteo forecast"
            except Exception:
                air_by_hour = {}
        f = SETTING_TEMP.get(leg["setting"], SETTING_TEMP["outdoors"])
        temps = []
        for t in engine.daterange(start, end, timedelta(minutes=15)):
            air = air_by_hour.get(t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0),
                                  leg["typical_temp_c"])
            temps.append(f(air))
        before = remaining
        if temps:
            remaining = engine.project(remaining, temps, m, 0.25)[-1]
        out.append({**leg, "weather_source": src, "avg_temp_c": round(sum(temps) / len(temps), 1) if temps else None,
                    "peak_temp_c": round(max(temps), 1) if temps else None,
                    "budget_used": before - remaining, "remaining_after": remaining})
    return {"product": d["item"]["product_name"], "start_remaining": d["state"]["remaining"],
            "end_remaining": remaining, "legs": out,
            "label": {k: m.get(k) for k in ("target_quote", "bands", "freeze_quote", "above_limit_quote")}}


# ------------------------------------------------------------------ porch heat index
def porch_stats() -> list[dict]:
    return db.query("""
        WITH d AS (
            SELECT d.id, d.zip, d.carrier,
                   count(w.ts) FILTER (WHERE mailbox_temp(w.temp_c, w.ts) > 30) AS hot_hours,
                   max(mailbox_temp(w.temp_c, w.ts)) AS peak
            FROM deliveries d
            LEFT JOIN weather_hourly w
              ON w.zip = d.zip AND w.ts >= date_trunc('hour', d.delivered_at) AND w.ts < d.picked_up_at
            GROUP BY d.id, d.zip, d.carrier
        )
        SELECT z.zip, z.name, z.lat, z.lon, d.carrier,
               count(*) AS deliveries,
               round(avg(d.hot_hours)::numeric, 2) AS avg_hot_hours,
               round(avg((d.hot_hours >= 2)::int)::numeric * 100, 1) AS pct_over_2h,
               round(max(d.peak)::numeric, 1) AS peak_mailbox_c
        FROM d JOIN zips z USING (zip)
        GROUP BY z.zip, z.name, z.lat, z.lon, d.carrier
        ORDER BY avg_hot_hours DESC""")
