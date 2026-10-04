"""Business logic on top of Tiger Data. SQL does the heavy lifting; Python shapes results."""
import json
import threading
from datetime import datetime, timedelta, timezone

from . import db, engine, realtime, weather

LATE_GRACE = timedelta(minutes=10)


def now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ items
def list_items(user_id: int | None = None) -> list[dict]:
    rows = db.query("""
        SELECT i.id, i.nickname, i.started_at, i.user_id, i.opened_at, i.expires_on, i.lot, u.name AS user_name, u.is_me,
               p.id AS product_id, p.name AS product_name, p.model, p.source AS product_source
        FROM items i JOIN users u ON u.id = i.user_id JOIN products p ON p.id = i.product_id
        WHERE (%(u)s::int IS NULL OR i.user_id = %(u)s)
        ORDER BY u.is_me DESC, i.id""", {"u": user_id})
    for r in rows:
        r.update(summary(r["id"], r["model"]))
        gap = [{"hours": r["stale_minutes"] / 60, "last_temp": r["current_temp"]}] if (r["stale_minutes"] or 0) > 20 else []
        r["worst_case"] = engine.gap_worst_case(r["remaining"], gap, r["model"])
        r["dates"] = engine.dates_info(r["opened_at"], r["expires_on"], r["model"].get("in_use_days"), now())
        r["status"] = engine.status_of(r, r["worst_case"], r["zone"], r["dates"])
        r["forecast"] = live_forecast(r["id"], r["model"], r, r["user_id"])
        r["forecast"].pop("path", None)
    return rows


def item_trend(item_id: int) -> dict:
    return db.one("SELECT * FROM item_trend(%s)", (item_id,)) or {}


def outage_for(user_id: int) -> dict | None:
    return db.one("""SELECT o.id, o.indoor_temp_c, o.est_restore_at FROM outages o JOIN users u ON ST_Contains(o.area, u.geom)
                     WHERE o.active AND u.id = %s LIMIT 1""", (user_id,))


def live_forecast(item_id: int, model: dict, state: dict, user_id: int) -> dict:
    """Trend forecast from the real-time aggregate. A fridge medicine warms toward the room (or toward the
    outage's indoor temperature when the power is out); for anything else the ambient is unknown."""
    tr = item_trend(item_id)
    out = outage_for(user_id)
    ambient = out["indoor_temp_c"] if out else (engine.ROOM_C if model["target_max_c"] <= 10 else None)
    if state.get("stale_minutes") and state["stale_minutes"] > 20:  # silent sensor: no live trend
        tr = {**tr, "slope_c_per_h": None}
    f = engine.trend_forecast(state["remaining"], state["current_temp"], tr.get("slope_c_per_h"), tr.get("r2"),
                              tr.get("n"), model, ambient)
    f["ambient_c"] = ambient
    f["ambient_source"] = "outage indoor temperature" if out else "room temperature (assumed)" if ambient else None
    return f


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
        SELECT i.*, p.name AS product_name, p.model, p.source AS product_source, p.source_label, p.source_url,
               u.name AS user_name
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
            gaps.append({"from": a["bucket"] + timedelta(minutes=5), "to": b["bucket"], "last_temp": a["avg_temp"],
                         "hours": (b["bucket"] - a["bucket"]).total_seconds() / 3600})
    if timeline and state["stale_minutes"] and state["stale_minutes"] > 20:
        gaps.append({"from": timeline[-1]["bucket"] + timedelta(minutes=5), "to": now(),
                     "last_temp": timeline[-1]["avg_temp"], "hours": state["stale_minutes"] / 60, "ongoing": True})
    burners = sorted([e for e in episodes if e["burn"] > 0.0005], key=lambda e: -e["burn"])
    cur = state["current_temp"] if state["current_temp"] is not None else (m["target_min_c"] + m["target_max_c"]) / 2
    worst = engine.gap_worst_case(state["remaining"], gaps, m)
    whatif = engine.whatif(state["remaining"], cur, m)
    alerts_open = db.query("SELECT * FROM alerts WHERE item_id = %s AND resolved_at IS NULL", (item_id,))
    forecast = live_forecast(item_id, m, state, item["user_id"])
    hour_ago = now() - timedelta(hours=1)
    burn_last_hour = sum(r["burn"] for r in timeline if r["bucket"] >= hour_ago)
    dates = engine.dates_info(item["opened_at"], item["expires_on"], m.get("in_use_days"), now())
    status = engine.status_of(state, worst, state["zone"], dates)
    return {
        "item": item,
        "state": state,
        # the chart shows the last 3 days; episodes, stats and accuracy use the whole history
        "timeline": [{"t": r["bucket"], "temp": r["avg_temp"], "min": r["min_temp"], "max": r["max_temp"],
                      "remaining": 1 - r["used"], "zone": r["zone"]} for r in timeline
                     if r["bucket"] >= now() - timedelta(days=3)],
        "dates": dates,
        "episodes": episodes,
        "burners": burners[:8],
        "gaps": gaps,
        "whatif": whatif,
        "forecast": forecast,
        "burn_last_hour": burn_last_hour,
        "worst_case": worst,
        "status": status,
        "precautions": precautions(item, state, gaps, whatif),
        "patterns": similar_patterns([r["avg_temp"] for r in timeline[-12 * 72:]]),
        "stats": item_stats(item_id, item["started_at"]),
        "zones": zone_minutes(episodes),
        "alerts": alerts_open,
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
    for r in readings:
        if isinstance(r["ts"], str):
            r["ts"] = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
    readings, rejected, flagged = engine.validate_readings(readings)
    if not readings:
        return {"inserted": 0, "rejected": len(rejected), "rejections": rejected[:10]}
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
    db.execute("""INSERT INTO ingest_log (item_id, source, n, min_ts, max_ts, late, refreshed, rejected, flagged)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
               (item_id, source, len(rows), min_ts, max_ts, late, refreshed, len(rejected), len(flagged)))
    realtime.alerts_soon()
    return {"inserted": len(rows), "late": late, "refreshed": refreshed, "rejected": len(rejected),
            "flagged": len(flagged), "rejections": rejected[:10], "flags": flagged[:10],
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
        JOIN users u ON ST_Contains(o.area, u.geom) AND NOT u.synthetic
        JOIN items i ON i.user_id = u.id
        JOIN products p ON p.id = i.product_id
        WHERE o.active""")
    t = now()
    people = []
    for r in rows:
        s = summary(r["item_id"], r["model"])
        hrs_restore = max(0.0, (r["est_restore_at"] - t).total_seconds() / 3600)
        tr = item_trend(r["item_id"])
        tau = engine.observed_tau(s["current_temp"], tr.get("slope_c_per_h"), tr.get("r2"), tr.get("n"), r["indoor_temp_c"])
        hl = engine.outage_hours_left(s["remaining"], s["current_temp"], r["indoor_temp_c"], r["model"], tau_h=tau)
        people.append({
            "user_id": r["user_id"], "name": r["name"], "lat": r["lat"], "lon": r["lon"],
            "item_id": r["item_id"], "medicine": r["nickname"], "product": r["product_name"],
            "remaining": s["remaining"], "current_temp": s["current_temp"],
            "hours_left": hl, "hours_to_restore": hrs_restore,
            "warming_c_per_h": tr.get("slope_c_per_h"),
            "warming_model": f"measured: tau {tau:.1f} h" if tau else f"assumed: tau {engine.FRIDGE_TAU_H:.0f} h"
                             if s["current_temp"] is not None and s["current_temp"] < r["indoor_temp_c"] - 5 else "at room temperature",
            "at_risk": hl is not None and hl < hrs_restore,
            "refuges": r["refuges"] or [], "outage": r["outage"],
        })
    people.sort(key=lambda p: (not p["at_risk"], p["hours_left"] if p["hours_left"] is not None else 1e9))
    users = db.query("SELECT id, name, lat, lon, can_host, is_me FROM users WHERE NOT synthetic")
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


# ------------------------------------------------------------------ exposure statistics
def item_stats(item_id: int, started_at: datetime) -> dict:
    s = db.one("SELECT * FROM item_stats(%s)", (item_id,)) or {}
    try:  # timescaledb_toolkit hyperfunction: time-weighted mean over the raw readings
        twa = db.one("""SELECT average(time_weight('LOCF', ts, temp_c)) AS twa FROM readings
                        WHERE item_id = %s AND ts >= %s""", (item_id, started_at))["twa"]
    except Exception:
        twa = None
    return {**s, "time_weighted_avg_c": twa}


def zone_minutes(episodes: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for e in episodes:
        z = out.setdefault(e["zone"], {"zone": e["zone"], "minutes": 0.0, "burn": 0.0})
        z["minutes"] += e["minutes"]
        z["burn"] += e["burn"]
    return sorted(out.values(), key=lambda z: -z["minutes"])


# ------------------------------------------------------------------ alerts
def alerts(mine_only: bool = False, include_resolved: bool = False) -> list[dict]:
    return db.query("""
        SELECT a.*, i.nickname, u.name AS user_name, u.is_me
        FROM alerts a JOIN items i ON i.id = a.item_id JOIN users u ON u.id = i.user_id
        WHERE (%(all)s OR a.resolved_at IS NULL) AND (NOT %(mine)s OR u.is_me)
        ORDER BY a.resolved_at IS NOT NULL, a.severity = 'warning', a.created_at DESC
        LIMIT 100""", {"all": include_resolved, "mine": mine_only})


def check_alerts() -> None:
    with db.conn(autocommit=True) as c:
        c.execute("CALL check_alerts()")


# ------------------------------------------------------------------ under the hood
def _try(sql: str, params=None):
    try:
        return db.query(sql, params)
    except Exception as e:  # noqa: BLE001
        return [{"error": str(e).splitlines()[0]}]


def _compression_stats() -> dict:
    for fn in ("hypertable_columnstore_stats", "hypertable_compression_stats"):  # new name first (2.18+)
        row = _try(f"SELECT * FROM {fn}('readings')")
        if row and "error" not in row[0]:
            return row[0]
    return {}


BUDGET_SQL = """SELECT i.id, (SELECT used FROM item_timeline(i.id, %s) ORDER BY bucket DESC LIMIT 1) FROM items i"""
# 30-day exposure calendar for every medicine: the hierarchical rollup vs the same numbers from raw readings
CALENDAR_AGG_SQL = """SELECT day, item_id, avg_temp, min_temp, max_temp, burn, buckets FROM readings_1d
                      WHERE day >= time_bucket('1 day', now()) - INTERVAL '29 days'"""
CALENDAR_RAW_SQL = """
    WITH b AS (
        SELECT time_bucket('5 minutes', r.ts) AS bucket, r.item_id, avg(r.temp_c) AS avg_temp,
               min(r.temp_c) AS min_temp, max(r.temp_c) AS max_temp, avg(least(burn_rate(r.temp_c, p.model), 1e6)) AS avg_rate
        FROM readings r JOIN items i ON i.id = r.item_id JOIN products p ON p.id = i.product_id
        WHERE r.ts >= time_bucket('1 day', now()) - INTERVAL '29 days'
        GROUP BY 1, 2)
    SELECT time_bucket('1 day', bucket) AS day, item_id, avg(avg_temp), min(min_temp), max(max_temp),
           sum(least(1.0, avg_rate * 5 / 60.0)) AS burn, count(*) AS buckets
    FROM b GROUP BY 1, 2"""


def _median_ms(sql: str, params=None, runs: int = 3) -> float:
    import statistics
    import time
    db.query(sql, params)  # warm-up
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        db.query(sql, params)
        times.append((time.perf_counter() - t) * 1000)
    return round(statistics.median(times), 1)


def benchmark(runs: int = 3) -> dict:
    """Median of `runs` timings after a warm-up: continuous aggregate vs the same answer from raw readings."""
    def pair(agg_sql, agg_params, raw_sql, raw_params, raw_rows, agg_rows):
        a, r = _median_ms(agg_sql, agg_params, runs), _median_ms(raw_sql, raw_params, runs)
        return {"continuous_aggregate_ms": a, "raw_readings_ms": r, "speedup": round(r / a, 1) if a else None,
                "raw_rows_scanned": raw_rows, "aggregate_rows_read": agg_rows}

    window = "ts >= time_bucket('1 day', now()) - INTERVAL '29 days'"
    return {
        "runs": runs,
        "budget_all_items": pair(BUDGET_SQL, (False,), BUDGET_SQL, (True,),
                                 db.one("SELECT count(*) AS n FROM readings r JOIN items i ON i.id = r.item_id")["n"],
                                 db.one("SELECT count(*) AS n FROM readings_5m")["n"]),
        "calendar_30d": pair(CALENDAR_AGG_SQL, None, CALENDAR_RAW_SQL, None,
                             db.one(f"SELECT count(*) AS n FROM readings WHERE {window}")["n"],
                             db.one(f"SELECT count(*) AS n FROM ({CALENDAR_AGG_SQL}) x")["n"]),
    }


_bench_cache: dict = {}
_bench_lock = threading.Lock()
BENCH_TTL_S = 600


def cached_benchmark(fresh: bool = False) -> dict:
    """The raw side scans every reading, which takes minutes at scale: reuse the last result for
    BENCH_TTL_S, and let only one measurement run at a time."""
    import time
    with _bench_lock:
        if fresh or not _bench_cache or time.time() - _bench_cache["at"] > BENCH_TTL_S:
            _bench_cache.update(at=time.time(), result={**benchmark(), "measured_at": now()})
        return _bench_cache["result"]


def warm_benchmark() -> None:
    """Measure in the background at startup so Under the hood opens instantly."""
    def run():
        try:
            cached_benchmark()
        except Exception:  # noqa: BLE001  (no database yet: the page measures on demand)
            pass
    threading.Thread(target=run, daemon=True, name="benchmark-warmup").start()


def tiger_stats(fresh: bool = False) -> dict:
    bench = cached_benchmark(fresh)
    return {
        "hypertables": _try("""
            SELECT h.hypertable_name AS name, h.num_chunks AS chunks, h.compression_enabled,
                   hypertable_size((quote_ident(h.hypertable_schema) || '.' || quote_ident(h.hypertable_name))::regclass) AS bytes,
                   CASE h.hypertable_name WHEN 'readings' THEN (SELECT count(*) FROM readings)
                        WHEN 'weather_hourly' THEN (SELECT count(*) FROM weather_hourly) END AS rows
            FROM timescaledb_information.hypertables h ORDER BY bytes DESC"""),
        "compression": _compression_stats(),
        "caggs": _try("""SELECT view_name, materialized_only, materialization_hypertable_name
                         FROM timescaledb_information.continuous_aggregates"""),
        "jobs": _try("""
            SELECT j.job_id, j.proc_name, j.hypertable_name, j.schedule_interval::text AS every,
                   s.last_run_status, s.last_run_started_at, s.next_start, s.total_runs, s.total_failures
            FROM timescaledb_information.jobs j
            LEFT JOIN timescaledb_information.job_stats s USING (job_id)
            WHERE j.job_id >= 1000 ORDER BY j.job_id"""),
        "extensions": _try("SELECT extname, extversion FROM pg_extension ORDER BY extname"),
        "watermark": cagg_watermark(),
        "benchmark_ms": {"continuous_aggregate": bench["budget_all_items"]["continuous_aggregate_ms"],
                         "raw_readings": bench["budget_all_items"]["raw_readings_ms"]},
        "benchmark": bench,
    }


# ------------------------------------------------------------------ official label lookup (openFDA)
OPENFDA = "https://api.fda.gov/drug/label.json"


def openfda_lookup(name: str) -> dict | None:
    """Find the manufacturer's FDA label and return its storage section verbatim. Repackager labels often
    omit storage details, so candidates without real storage text are skipped."""
    import re

    import httpx
    q = name.strip().replace('"', "")
    for field in ("openfda.brand_name", "openfda.generic_name"):
        try:
            r = httpx.get(OPENFDA, params={"search": f'{field}:"{q}"', "limit": 10}, timeout=15)
            if r.status_code != 200:
                continue
            results = r.json().get("results", [])
        except httpx.HTTPError:
            return None
        for lab in results:
            text = " ".join(lab.get("storage_and_handling", []) or lab.get("how_supplied", []) or [])
            text = re.sub(r"\s+", " ", text)
            if len(text) < 200 or not re.search(r"refrigerat|store (at|between|in)|°C", text, re.I):
                continue
            start = max(text.lower().find("storage"), 0)
            ofda = lab.get("openfda", {})
            eff = lab.get("effective_time", "")
            return {
                "brand": (ofda.get("brand_name") or [q])[0], "generic": (ofda.get("generic_name") or [""])[0],
                "manufacturer": (ofda.get("manufacturer_name") or [""])[0], "set_id": lab.get("set_id"),
                "effective": eff, "text": text[start:start + 6000],
                "route": ", ".join(ofda.get("route") or []).lower(),
                "source_label": f"{(ofda.get('brand_name') or [q])[0]} ({', '.join(ofda.get('route') or []).lower() or 'route n/a'}) "
                                f"prescribing information, FDA label version {eff[:4]}-{eff[4:6]}-{eff[6:]}",
                "source_url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={lab.get('set_id')}",
            }
    return None


# ------------------------------------------------------------------ 30-day calendar (hierarchical aggregate)
def history(item_id: int, days: int = 30) -> list[dict]:
    return db.query("""SELECT day, avg_temp, min_temp, max_temp, burn, buckets FROM readings_1d
                       WHERE item_id = %s AND day >= time_bucket('1 day', now()) - %s * INTERVAL '1 day'
                       ORDER BY day""", (item_id, days - 1))


# ------------------------------------------------------------------ FDA recalls (openFDA enforcement)
_recall_cache: dict = {}


def recalls(brand: str, lot: str | None = None) -> dict:
    """Drug recalls for this brand from openFDA's enforcement reports, newest first, cached 6 hours.
    A recall is flagged when it lists your lot number, or when its reason is temperature-related."""
    import time

    import httpx
    key = brand.lower()
    hit = _recall_cache.get(key)
    if not hit or time.time() - hit[0] > 6 * 3600:
        rows, total, error = [], 0, None
        try:
            r = httpx.get("https://api.fda.gov/drug/enforcement.json",
                          params={"search": f'openfda.brand_name:"{brand}"', "sort": "report_date:desc", "limit": 10}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                total, rows = data["meta"]["results"]["total"], data["results"]
            elif r.status_code != 404:   # 404 = no recalls on record
                error = f"openFDA returned {r.status_code}"
        except httpx.HTTPError as e:
            error = str(e)
        hit = (time.time(), {"total": total, "rows": rows, "error": error})
        _recall_cache[key] = hit
    data = hit[1]
    out = []
    for r in data["rows"]:
        codes = r.get("code_info", "")
        d = r.get("report_date", "")
        out.append({
            "recall_number": r.get("recall_number"), "status": r.get("status"), "classification": r.get("classification"),
            "reason": r.get("reason_for_recall"), "report_date": f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d,
            "product": (r.get("product_description") or "")[:220], "lots": codes[:220], "firm": r.get("recalling_firm"),
            "lot_match": bool(lot and lot.lower() in codes.lower()),
            "temperature_related": "temperature" in (r.get("reason_for_recall") or "").lower(),
        })
    return {"brand": brand, "total": data["total"], "error": data["error"], "recalls": out,
            "active_lot_match": any(x["lot_match"] and x["status"] == "Ongoing" for x in out)}


# ------------------------------------------------------------------ verifiable exposure receipts
def _snapshot(item_id: int, as_of) -> dict:
    """Everything a pharmacist needs to trust, up to `as_of`, in a form that doesn't change unless the
    underlying readings do: per-day time and budget used outside labeled storage, total used, reading count."""
    days = db.query("""
        SELECT to_char(date_trunc('day', bucket), 'YYYY-MM-DD') AS day, zone, count(*) * 5 AS minutes,
               round(sum(burn)::numeric, 6)::float8 AS burn, round(max(max_temp)::numeric, 2)::float8 AS peak_c
        FROM item_timeline(%s) WHERE bucket < %s AND zone <> 'Labeled storage'
        GROUP BY 1, 2 HAVING sum(burn) >= 0.0005 ORDER BY 1, 2""", (item_id, as_of))  # skip door-opening blips
    used = db.one("SELECT used FROM item_timeline(%s) WHERE bucket < %s ORDER BY bucket DESC LIMIT 1", (item_id, as_of))
    n = db.one("SELECT count(*) AS n FROM readings WHERE item_id = %s AND ts < %s", (item_id, as_of))["n"]
    return {"used": round(used["used"], 6) if used else 0.0, "readings": n, "exposure_days": days}


def _digest(item: dict, as_of, snap: dict) -> str:
    import hashlib
    import json as _json
    body = {"item_id": item["id"], "product": item["product_name"], "lot": item.get("lot"),
            "as_of": as_of.isoformat(), **snap}
    return hashlib.sha256(_json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def create_receipt(d: dict) -> dict:
    """Freeze the exposure record up to the last complete 5-minute bucket and store its fingerprint."""
    item = d["item"]
    t = now()
    as_of = t.replace(second=0, microsecond=0) - timedelta(minutes=t.minute % 5)
    snap = _snapshot(item["id"], as_of)
    digest = _digest(item, as_of, snap)
    code = digest[:4].upper() + "-" + digest[4:8].upper() + "-" + digest[8:12].upper()
    m = item["model"]
    payload = {"product": item["product_name"], "nickname": item["nickname"], "lot": item.get("lot"),
               "since": item["started_at"].isoformat(), "remaining": round(1 - snap["used"], 4), **snap,
               "label": {"target_quote": m.get("target_quote"), "bands": [b["quote"] for b in m.get("bands", [])]},
               "source_url": item.get("source_url")}
    db.execute("""INSERT INTO receipts (code, item_id, as_of, payload, digest) VALUES (%s,%s,%s,%s,%s)
                  ON CONFLICT (code) DO NOTHING""", (code, item["id"], as_of, json.dumps(payload, default=str), digest))
    return {"code": code, "as_of": as_of, "digest": digest, "url": f"/r/{code}"}


def verify_receipt(code: str) -> dict | None:
    r = db.one("""SELECT r.*, i.id AS iid, i.lot, p.name AS product_name FROM receipts r
                  JOIN items i ON i.id = r.item_id JOIN products p ON p.id = i.product_id WHERE r.code = %s""",
               (code.upper(),))
    if not r:
        return None
    snap = _snapshot(r["item_id"], r["as_of"])
    now_digest = _digest({"id": r["iid"], "product_name": r["product_name"], "lot": r["lot"]}, r["as_of"], snap)
    same = now_digest == r["digest"]
    why = None
    if not same:
        why = ("Readings for this period changed after the receipt was made: late sensor data arrived, or the record "
               "was edited." if snap["readings"] != r["payload"]["readings"] else
               "The calculated exposure for this period changed after the receipt was made.")
    return {"code": r["code"], "created_at": r["created_at"], "as_of": r["as_of"], "payload": r["payload"],
            "digest": r["digest"], "matches": same, "reason": why}


# ------------------------------------------------------------------ developer platform
def create_device(item_id: int, label: str | None) -> str:
    import secrets
    token = "llg_" + secrets.token_urlsafe(20)
    db.execute("INSERT INTO devices (token, item_id, label) VALUES (%s, %s, %s)", (token, item_id, label))
    return token


def device_for(token: str) -> dict | None:
    return db.one("SELECT * FROM devices WHERE token = %s AND NOT revoked", (token,)) if token else None


def devices() -> list[dict]:
    return db.query("""SELECT d.token, d.label, d.created_at, d.last_seen, d.readings, i.id AS item_id, i.nickname
                       FROM devices d JOIN items i ON i.id = d.item_id JOIN users u ON u.id = i.user_id
                       WHERE u.is_me AND NOT d.revoked ORDER BY d.created_at DESC""")


def create_webhook(url: str) -> dict:
    import secrets
    secret = "whsec_" + secrets.token_urlsafe(24)
    hook = db.one("INSERT INTO webhooks (url, secret) VALUES (%s, %s) RETURNING id, url, created_at", (url, secret))
    return {**hook, "secret": secret}


def webhooks() -> list[dict]:
    return db.query("SELECT id, url, created_at, last_status, last_at FROM webhooks WHERE active ORDER BY id DESC")


def deliver_webhooks(event: dict) -> None:
    """POST an alert to every active webhook, signed with HMAC-SHA256 of the body (X-Lifelog-Signature)."""
    import hashlib
    import hmac
    import json as _json

    import httpx
    hooks = db.query("SELECT id, url, secret FROM webhooks WHERE active")
    if not hooks:
        return
    item = db.one("""SELECT i.nickname, p.name AS product FROM items i JOIN products p ON p.id = i.product_id
                     WHERE i.id = %s""", (event.get("item_id"),)) or {}
    body = _json.dumps({"type": "alert", "id": event.get("id"), "kind": event.get("kind"), "severity": event.get("severity"),
                        "message": event.get("message"), "resolved": event.get("resolved"), "item_id": event.get("item_id"),
                        "medicine": item.get("nickname"), "product": item.get("product"), "sent_at": now().isoformat()})
    for h in hooks:
        sig = hmac.new(h["secret"].encode(), body.encode(), hashlib.sha256).hexdigest()
        try:
            r = httpx.post(h["url"], content=body, timeout=5,
                           headers={"Content-Type": "application/json", "X-Lifelog-Signature": f"sha256={sig}",
                                    "X-Lifelog-Event": "alert"})
            status = str(r.status_code)
        except httpx.HTTPError as e:
            status = type(e).__name__
        db.execute("UPDATE webhooks SET last_status = %s, last_at = now() WHERE id = %s", (status, h["id"]))


# ------------------------------------------------------------------ caregiver share links
def create_share(user_id: int, label: str | None = None) -> str:
    import secrets
    token = secrets.token_urlsafe(16)
    db.execute("INSERT INTO shares (token, user_id, label) VALUES (%s, %s, %s)", (token, user_id, label))
    return token


def shared_view(token: str) -> dict | None:
    """What a caregiver sees: status of each medicine and open alerts. Read-only, no locations."""
    sh = db.one("""SELECT s.*, u.name FROM shares s JOIN users u ON u.id = s.user_id
                   WHERE s.token = %s AND NOT s.revoked""", (token,))
    if not sh:
        return None
    items = [{k: i[k] for k in ("nickname", "product_name", "remaining", "current_temp", "zone", "stale_minutes",
                                "status", "dates", "worst_case")} for i in list_items(sh["user_id"])]
    alerts_ = db.query("""SELECT a.message, a.severity, a.created_at FROM alerts a JOIN items i ON i.id = a.item_id
                          WHERE i.user_id = %s AND a.resolved_at IS NULL ORDER BY a.severity, a.created_at DESC""",
                       (sh["user_id"],))
    return {"name": sh["name"], "label": sh["label"], "items": items, "alerts": alerts_, "updated": now()}


# ------------------------------------------------------------------ precautions
_forecast_cache: dict = {}


def home_forecast() -> list[dict]:
    """Next 48 h of hourly temperature at the user's home, cached for an hour."""
    import time
    me = db.one("SELECT lat, lon FROM users WHERE is_me")
    if not me:
        return []
    hit = _forecast_cache.get("v")
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    try:
        t0 = now()
        hours = weather.hourly(me["lat"], me["lon"], t0.date(), (t0 + timedelta(days=2)).date())
        data = [{"t": k, "temp_c": v} for k, v in sorted(hours.items()) if t0 <= k <= t0 + timedelta(hours=48)]
    except Exception:
        data = []
    _forecast_cache["v"] = (time.time(), data)
    return data


def precautions(item: dict, state: dict, gaps: list[dict], whatif: list[dict]) -> list[dict]:
    """Plain-language precautions: situation first (most urgent), then the label's own rules."""
    m, out = item["model"], []
    fridge = m["target_max_c"] <= 10
    zone = state["zone"]
    d = engine.dates_info(item.get("opened_at"), item.get("expires_on"), m.get("in_use_days"), now())
    if d["expired"]:
        out.append({"level": "critical", "text": "It's past its printed expiration date. Don't use it; ask your pharmacy for a replacement."})
    elif d["in_use_over"]:
        out.append({"level": "critical", "text": f"Its in-use period is over (the label allows {round(d['in_use_days'])} days after opening). Throw it away and start a new one."})
    elif d.get("use_by") and d["days_left"] <= 3:
        out.append({"level": "warning", "text": f"Use it by {d['use_by'].strftime('%b %d')} ({d['use_by_reason']}). Get a new one ready."})
    if zone == "Frozen":
        out.append({"level": "critical", "text": "It has frozen. Don't use it, even after it thaws. Ask your pharmacist for a replacement."})
    if zone == "Above labeled limit":
        best = whatif[0] if whatif else None
        out.append({"level": "critical", "text": f"It's too warm right now. {best['option']} as soon as you can." if best
                    else "It's too warm right now. Move it somewhere cooler."})
    if any(g.get("ongoing") for g in gaps):
        out.append({"level": "warning", "text": "The sensor has stopped reporting. Check its battery and that it's within Bluetooth range."})
    if db.one("""SELECT 1 AS x FROM outages o JOIN users u ON ST_Contains(o.area, u.geom)
                 WHERE o.active AND u.id = %s""", (item["user_id"],)):
        out.append({"level": "warning", "text": "Power is out. Keep the fridge door closed: a closed fridge stays cold for about 4 hours."
                    if fridge else "Power is out. Keep it in the coolest room, away from windows."})
    if state["remaining"] < 0.5:
        out.append({"level": "warning", "text": "More than half its life budget is used. Check with your pharmacist before the next dose; the refill letter can help with a replacement."})
    # forecast-driven heads-up for the next 48 h
    fc = home_forecast()
    if fc:
        hot = max(fc, key=lambda f: f["temp_c"])
        cold = min(fc, key=lambda f: f["temp_c"])
        if hot["temp_c"] >= 27:
            car = hot["temp_c"] + 20
            hl = engine.hours_left(state["remaining"], car, m)
            when = hot["t"].astimezone(timezone(timedelta(hours=-4))).strftime("%a %I %p").replace(" 0", " ") if hasattr(hot["t"], "astimezone") else ""
            out.append({"level": "warning", "text": f"Hot weather ahead ({hot['temp_c']:.0f}°C {when}). A parked car can reach about {car:.0f}°C"
                        + (f", which would use this medicine's remaining budget in about {hl * 60:.0f} minutes." if hl is not None and hl < 2
                           else f", which would use this medicine's remaining budget in about {hl:.1f} hours." if hl is not None else ".")
                        + " Don't leave it in the car."})
        if cold["temp_c"] <= 0 and m.get("freeze_discard"):
            out.append({"level": "warning", "text": f"Freezing weather ahead ({cold['temp_c']:.0f}°C). Don't leave it in a car or mailbox; frozen medicine must be thrown away."})
    # the label's own rules
    if fridge:
        out.append({"level": "info", "text": f"Store in the fridge at {m['target_min_c']:.0f}–{m['target_max_c']:.0f}°C, in the middle shelf, away from the back wall where it can freeze."})
    else:
        out.append({"level": "info", "text": f"Keep at {m['target_min_c']:.0f}–{m['target_max_c']:.0f}°C. Don't refrigerate it unless the label says so." if not m.get("cold_ok", True)
                    else f"Keep at {m['target_min_c']:.0f}–{m['target_max_c']:.0f}°C."})
    if m.get("freeze_discard"):
        out.append({"level": "info", "text": "Never freeze it. If it has been frozen, don't use it."})
    for v in m.get("visual_checks", []):
        out.append({"level": "info", "text": f"Before each use: {v[0].lower() + v[1:]}."})
    import re as _re
    for rule in m.get("discard_rules", []):
        text = rule.rstrip(".") + "."
        if not item.get("opened_at") and _re.search(r"in-use|in use|after (first )?(use|opening)|once .*opened", rule, _re.I):
            text = "Once you start using it: " + text[0].lower() + text[1:]
        out.append({"level": "info", "text": text})
    out.append({"level": "info", "text": "Carry it in an insulated case when you leave home, and keep it out of direct sunlight."})
    return out


# ------------------------------------------------------------------ accuracy report
def accuracy(item_id: int) -> dict:
    """Cross-checks the life budget four ways and shows how sensitive it is to the main assumption."""
    d = item_detail(item_id)
    m, started = d["item"]["model"], d["item"]["started_at"]
    used = lambda raw, naive: (db.one("SELECT used FROM item_timeline(%s, %s, %s) ORDER BY bucket DESC LIMIT 1",
                                      (item_id, raw, naive)) or {"used": 0.0})["used"]
    raw_rows = db.query("SELECT ts, temp_c FROM readings WHERE item_id = %s ORDER BY ts", (item_id,))
    readings = [(r["ts"], r["temp_c"]) for r in raw_rows]
    methods = {
        "aggregate_exact": 1 - used(False, False),
        "raw_exact_sql": 1 - used(True, False),
        "python_independent": 1 - engine.exact_budget_used(readings, m, started),
        "aggregate_bucket_average": 1 - used(False, True),
    }
    sens = []
    for h in (4, 8, 24, 72):
        mm = {**m, "above_limit_budget_hours": h}
        sens.append({"above_limit_budget_hours": h, "remaining": 1 - engine.exact_budget_used(readings, mm, started)})
    log = db.one("""SELECT coalesce(sum(n), 0) AS accepted, coalesce(sum(rejected), 0) AS rejected,
                           coalesce(sum(flagged), 0) AS flagged, count(*) FILTER (WHERE late) AS late_batches
                    FROM ingest_log WHERE item_id = %s""", (item_id,))
    return {"item": d["item"]["nickname"], "methods": methods,
            "max_disagreement": max(methods["aggregate_exact"], methods["raw_exact_sql"], methods["python_independent"])
            - min(methods["aggregate_exact"], methods["raw_exact_sql"], methods["python_independent"]),
            "averaging_error": methods["aggregate_bucket_average"] - methods["aggregate_exact"],
            "sensitivity": sens, "assumption_on_label": not m.get("above_limit_is_assumption", True),
            "gaps": d["gaps"], "worst_case": d["worst_case"], "remaining": d["state"]["remaining"],
            "readings": len(readings), "ingest": log}
