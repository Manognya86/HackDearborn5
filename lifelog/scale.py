"""Scale mode: bulk history generated SERVER-SIDE (INSERT ... SELECT over generate_series), so the
benchmark runs against months of 1-minute readings instead of the 14-day demo seed.

Synthetic users are flagged users.synthetic: their history counts everywhere Tiger is measured
(hypertable, aggregates, columnstore, benchmark) but they stay out of the outage demo and simulator."""
import time
from datetime import datetime, timedelta, timezone

from . import db, seed

DAYS = 90
FALLBACK_BYTES_PER_ROW = 130   # measured on Tiger Cloud: uncompressed readings chunk incl. indexes


def bytes_per_row() -> float:
    """Uncompressed size per reading, measured on the newest full rowstore chunk."""
    row = db.one("""
        SELECT pg_total_relation_size(format('%I.%I', c.chunk_schema, c.chunk_name)) AS b,
               c.range_start, c.range_end
        FROM timescaledb_information.chunks c
        WHERE c.hypertable_name = 'readings' AND NOT c.is_compressed AND c.range_end <= now()
        ORDER BY c.range_start DESC LIMIT 1""")
    if row:
        n = db.one("SELECT count(*) AS n FROM readings WHERE ts >= %s AND ts < %s", (row["range_start"], row["range_end"]))["n"]
        if n > 10000:
            return row["b"] / n
    return FALLBACK_BYTES_PER_ROW


def estimate(n_items: int, days: int = DAYS) -> dict:
    rows = n_items * days * 1440
    bpr = bytes_per_row()
    return {"rows": rows, "bytes_per_row": round(bpr, 1), "uncompressed_bytes": int(rows * bpr),
            # readings_5m: one row per item per 5 minutes; ~10x smaller after columnstore is typical here
            "cagg_rows": n_items * days * 288, "compressed_bytes_guess": int(rows * bpr / 10)}


def readings_size() -> int:
    return db.one("SELECT hypertable_size('readings') AS b")["b"]


def create_items(n: int, days: int = DAYS) -> list[int]:
    """N synthetic users around Dearborn (PostGIS points, same box as the seed), one medicine each."""
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=days)
    with db.conn() as c:
        c.execute("DELETE FROM users WHERE synthetic")  # cascades items and alerts
        rows = c.execute("""
            WITH u AS (
                INSERT INTO users (name, is_me, can_host, lat, lon, geom, synthetic)
                SELECT 'Scale user ' || g, FALSE, FALSE, lat, lon, ST_SetSRID(ST_MakePoint(lon, lat), 4326), TRUE
                FROM (SELECT g, 42.295 + random() * 0.045 AS lat, -83.27 + random() * 0.11 AS lon
                      FROM generate_series(1, %s) g) s
                RETURNING id)
            INSERT INTO items (user_id, product_id, nickname, started_at)
            SELECT u.id, p.id, 'Scale: ' || split_part(p.name, ' (', 1), %s
            FROM u CROSS JOIN LATERAL (SELECT id, name FROM products WHERE u.id > 0
                                       ORDER BY random() LIMIT 1) p
            RETURNING id""", (n, start)).fetchall()
    return [r["id"] for r in rows]


# One day of 1-minute readings for every synthetic item. Fridge products sit around 4.5C with door
# openings; room products (EpiPen) follow a daily indoor cycle. About 4% of item-days get an excursion
# (fridge failure / hot car) of 1-3 hours so the burn math has something to do.
DAY_SQL = """
WITH it AS (
    SELECT i.id, (p.model->>'target_max_c')::float8 <= 10 AS fridge,
           random() < 0.04 AS ex, %(day)s::timestamptz + make_interval(mins => (random() * 1200)::int) AS ex_start,
           make_interval(mins => 60 + (random() * 120)::int) AS ex_len,
           12 + random() * 18 AS ex_peak
    FROM items i JOIN users u ON u.id = i.user_id JOIN products p ON p.id = i.product_id
    WHERE u.synthetic)
INSERT INTO readings (ts, item_id, temp_c, source)
SELECT ts, it.id,
       round((CASE
           WHEN it.ex AND ts >= it.ex_start AND ts < it.ex_start + it.ex_len
               THEN CASE WHEN it.fridge THEN it.ex_peak ELSE it.ex_peak + 15 END
           WHEN it.fridge
               THEN 4.5 + 0.6 * sin(extract(epoch FROM ts) / 1800)
                    + CASE WHEN random() < 0.01 THEN 2 + random() * 3 ELSE 0 END
           ELSE 22 + 1.5 * sin(2 * pi() * (extract(hour FROM ts AT TIME ZONE 'America/Detroit') - 9) / 24)
       END + (random() - 0.5) * 0.5)::numeric, 2)::float8,
       'synthetic'
FROM it CROSS JOIN generate_series(%(day)s::timestamptz, %(end)s::timestamptz - INTERVAL '1 minute',
                                   INTERVAL '1 minute') ts
"""


def generate(n: int, days: int = DAYS, log=print) -> dict:
    t_all = time.perf_counter()
    items = create_items(n, days)
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    log(f"  {len(items)} synthetic items; generating {days} days of 1-minute readings server-side ...")
    day, k, rows = start, 0, 0
    while day < end:
        nxt = min(day + timedelta(days=1), end)
        with db.conn() as c:  # one transaction per day
            rows += c.execute(DAY_SQL, {"day": day, "end": nxt}).rowcount
        k += 1
        if k % 10 == 0 or nxt == end:
            log(f"    day {k}/{days}: {rows:,} rows ({time.perf_counter() - t_all:.0f} s)")
        day = nxt

    log("  refreshing readings_5m then readings_1d over the full range ...")
    t = time.perf_counter()
    with db.conn(autocommit=True) as c:
        w = start
        while w < end:  # 15-day windows keep each refresh transaction bounded
            db.call_refresh(c,"CALL refresh_continuous_aggregate('readings_5m', %s::timestamptz, %s::timestamptz)",
                      (w, min(w + timedelta(days=15), end)))
            w += timedelta(days=15)
        db.call_refresh(c,"CALL refresh_continuous_aggregate('readings_5m', %s::timestamptz, time_bucket('5 minutes', now()))", (start,))
        db.call_refresh(c,"CALL refresh_continuous_aggregate('readings_1d', time_bucket('1 day', %s::timestamptz), "
                  "time_bucket('1 day', now()))", (start,))
    refresh_s = time.perf_counter() - t

    before = readings_size()
    log(f"  converting history to columnstore (hypertable now {before / 2**20:,.0f} MiB) ...")
    t = time.perf_counter()
    chunks = seed.compress_history()
    compress_s = time.perf_counter() - t
    after = readings_size()
    seed.check_alerts()
    total = db.one("SELECT count(*) AS n FROM readings")["n"]
    return {"synthetic_items": len(items), "rows_inserted": rows, "readings_total": total,
            "bytes_before": before, "bytes_after": after, "compression_ratio": round(before / after, 1) if after else None,
            "chunks_converted": chunks, "refresh_s": round(refresh_s, 1), "compress_s": round(compress_s, 1),
            "total_s": round(time.perf_counter() - t_all, 1)}
