"""Pass/fail report for the Tiger Data integration on the configured DATABASE_URL.

    .venv/Scripts/python scripts/verify_tiger.py            # full run (waits ~70 s for scheduled jobs)
    .venv/Scripts/python scripts/verify_tiger.py --no-wait  # skip the wait for the check_alerts job

Late-data and LISTEN/NOTIFY checks use a temporary probe item (owned by a neighbor, not "You")
that is deleted at the end, so this is safe to run against the demo database."""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg  # noqa: E402

from lifelog import config, db, services  # noqa: E402

failures: list[str] = []


def report(ok: bool | None, name: str, detail: str = "") -> None:
    tag = {True: "PASS", False: "FAIL", None: "WARN"}[ok]
    if ok is False:
        failures.append(name)
    print(f"[{tag}] {name}" + (f": {detail}" if detail else ""))


def check_hypertables() -> None:
    names = {r["hypertable_name"] for r in db.query("SELECT hypertable_name FROM timescaledb_information.hypertables")}
    for h in ("readings", "weather_hourly"):
        report(h in names, f"hypertable {h}")


def check_caggs() -> None:
    caggs = {r["view_name"]: r for r in db.query(
        "SELECT view_name, materialized_only FROM timescaledb_information.continuous_aggregates")}
    for v in ("readings_5m", "readings_1d"):
        report(v in caggs, f"continuous aggregate {v}")
    if "readings_5m" in caggs:
        report(caggs["readings_5m"]["materialized_only"] is False, "readings_5m real-time (materialized_only = false)")


JOB_SQL = """
    SELECT j.job_id, j.proc_name, j.hypertable_name, s.last_run_status, s.total_runs, s.total_successes,
           s.total_failures
    FROM timescaledb_information.jobs j LEFT JOIN timescaledb_information.job_stats s USING (job_id)
    WHERE j.job_id >= 1000 ORDER BY j.job_id"""


def job_errors(job_id: int) -> str:
    try:
        rows = db.query("""SELECT finish_time, err_message FROM timescaledb_information.job_errors
                           WHERE job_id = %s ORDER BY finish_time DESC LIMIT 1""", (job_id,))
        return rows[0]["err_message"] if rows else ""
    except Exception:  # noqa: BLE001
        return ""


def check_jobs() -> list[dict]:
    jobs = db.query(JOB_SQL)

    def find(procs, table=None):
        return [j for j in jobs if j["proc_name"] in procs and (table is None or j["hypertable_name"] == table)]

    wanted = [
        ("refresh policy readings_5m", find({"policy_refresh_continuous_aggregate"}, "readings_5m")),
        ("refresh policy readings_1d", find({"policy_refresh_continuous_aggregate"}, "readings_1d")),
        ("columnstore/compression policy readings", find({"policy_compression", "policy_columnstore"}, "readings")),
        ("retention policy readings", find({"policy_retention"}, "readings")),
        ("retention policy weather_hourly", find({"policy_retention"}, "weather_hourly")),
        ("check_alerts job", find({"check_alerts"})),
    ]
    for name, found in wanted:
        report(len(found) == 1, f"job: {name}", f"{len(found)} found" if len(found) != 1 else f"job_id {found[0]['job_id']}")
    for j in jobs:
        label = f"job {j['job_id']} {j['proc_name']} ({j['hypertable_name'] or '-'})"
        detail = f"last={j['last_run_status']} runs={j['total_runs']} failures={j['total_failures']}"
        if j["last_run_status"] == "Failed":
            report(False, label, detail + f" error={job_errors(j['job_id'])!r}")
        elif (j["total_failures"] or 0) > 0:
            report(None, label, detail + " (failed before, last run succeeded)")
        elif j["last_run_status"] is None:
            report(None, label, detail + " (not run yet)")
        else:
            report(True, label, detail)
    return jobs


# ------------------------------------------------------------------ probe item
def make_probe() -> int:
    user = db.one("SELECT id FROM users WHERE NOT is_me ORDER BY id LIMIT 1") or db.one("SELECT id FROM users LIMIT 1")
    product = db.one("SELECT id FROM products WHERE name ILIKE 'Lantus%' ORDER BY id LIMIT 1") or db.one(
        "SELECT id FROM products ORDER BY id LIMIT 1")
    if not user or not product:
        raise RuntimeError("no users/products: run scripts/setup_db.py first")
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=8)
    return db.one("""INSERT INTO items (user_id, product_id, nickname, started_at)
                     VALUES (%s, %s, 'verify_tiger probe', %s) RETURNING id""", (user["id"], product["id"], start))["id"]


def drop_probe(item_id: int) -> None:
    span = db.one("SELECT min(ts) AS a, max(ts) AS b FROM readings WHERE item_id = %s", (item_id,))
    db.execute("DELETE FROM readings WHERE item_id = %s", (item_id,))
    db.execute("DELETE FROM ingest_log WHERE item_id = %s", (item_id,))
    db.execute("DELETE FROM items WHERE id = %s", (item_id,))  # cascades its alerts
    if span and span["a"]:
        db.refresh_readings(span["a"], span["b"])  # drop its materialized buckets


def budget_used(item_id: int, raw: bool) -> float:
    row = db.one("SELECT used FROM item_timeline(%s, %s) ORDER BY bucket DESC LIMIT 1", (item_id, raw))
    return row["used"] if row else 0.0


def check_late_data(item_id: int) -> None:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    gap_start, gap_end = now - timedelta(hours=6), now - timedelta(hours=5, minutes=30)
    base = []
    t = now - timedelta(hours=8)
    while t <= now:
        if not (gap_start <= t < gap_end):
            base.append({"ts": t, "temp_c": 4.5})
        t += timedelta(minutes=1)
    services.ingest(item_id, base, source="sensor", auto_refresh=True)
    agg0, raw0 = budget_used(item_id, False), budget_used(item_id, True)
    report(abs(agg0 - raw0) < 1e-9, "late data: baseline aggregate = raw", f"{agg0:.4f} vs {raw0:.4f}")

    late = [{"ts": gap_start + timedelta(minutes=k), "temp_c": 40.0} for k in range(30)]
    res = services.ingest(item_id, late, source="sensor", auto_refresh=False)
    report(bool(res.get("late")), "late data: ingest detects rows behind the watermark",
           f"min_ts={res.get('min_ts'):%H:%M} watermark={res.get('watermark')}")
    agg1, raw1 = budget_used(item_id, False), budget_used(item_id, True)
    report(raw1 - agg1 > 1e-3, "late data: before refresh aggregate and raw disagree",
           f"aggregate used {agg1:.4f}, raw used {raw1:.4f}")

    services.repair_late(item_id)
    agg2, raw2 = budget_used(item_id, False), budget_used(item_id, True)
    report(abs(agg2 - raw2) < 1e-9 and agg2 > agg1, "late data: after targeted refresh they agree",
           f"aggregate used {agg2:.4f}, raw used {raw2:.4f}")


def check_listen(item_id: int) -> None:
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as c:
        c.execute("LISTEN lifelog")
        t0 = time.perf_counter()
        services.ingest(item_id, [{"ts": datetime.now(timezone.utc), "temp_c": 4.6}], auto_refresh=False)
        got = None
        for n in c.notifies(timeout=5.0):
            if '"readings"' in n.payload:
                got = time.perf_counter() - t0
                break
    report(got is not None, "LISTEN/NOTIFY: reading insert notifies channel lifelog",
           f"{got * 1000:.0f} ms" if got is not None else "no notification within 5 s")


def check_alert_job(wait: bool) -> None:
    if wait:
        print("... waiting 70 s for the scheduled check_alerts job")
        time.sleep(70)
    j = db.one("""SELECT s.total_successes, s.last_run_status, s.last_successful_finish
                  FROM timescaledb_information.jobs j JOIN timescaledb_information.job_stats s USING (job_id)
                  WHERE j.proc_name = 'check_alerts'""")
    ok = bool(j and (j["total_successes"] or 0) >= 1 and j["last_run_status"] == "Success")
    report(ok if wait else (ok or None), "check_alerts job ran successfully",
           f"successes={j and j['total_successes']} last={j and j['last_run_status']}")
    n = db.one("SELECT count(*) AS n FROM alerts")["n"]
    report(n > 0, "alerts table has rows", f"{n} rows")


def main() -> int:
    wait = "--no-wait" not in sys.argv
    if not config.DATABASE_URL:
        print("DATABASE_URL is not set in .env")
        return 2
    v = db.one("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    print(f"TimescaleDB {v['extversion'] if v else 'MISSING'} on {config.DATABASE_URL.split('@')[-1].split('/')[0]}")
    check_hypertables()
    check_caggs()
    check_jobs()
    probe = make_probe()
    try:
        check_late_data(probe)
        check_listen(probe)
    finally:
        drop_probe(probe)
    check_alert_job(wait)
    if wait:
        print("--- job status after waiting")
        check_jobs()
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
