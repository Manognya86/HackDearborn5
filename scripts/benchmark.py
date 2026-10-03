"""Tiger Data by the numbers: prints a Markdown table to paste into slides / Devpost.

    .venv/Scripts/python scripts/benchmark.py            # median of 3 runs after a warm-up
    .venv/Scripts/python scripts/benchmark.py --runs 5"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lifelog import db, services  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
ap = argparse.ArgumentParser()
ap.add_argument("--runs", type=int, default=3)
args = ap.parse_args()


def mib(b) -> str:
    return "–" if b is None else f"{b / 2**20:,.1f} MiB"


def ms(v) -> str:
    return f"{v / 1000:,.2f} s" if v >= 1000 else f"{v:,.1f} ms"


ver = db.one("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")["extversion"]
pg = db.one("SHOW server_version")["server_version"]
counts = db.one("""SELECT (SELECT count(*) FROM readings) AS readings, (SELECT count(*) FROM items) AS items,
                          (SELECT count(*) FROM readings_5m) AS r5m, (SELECT count(*) FROM readings_1d) AS r1d,
                          (SELECT min(ts) FROM readings) AS first, (SELECT max(ts) FROM readings) AS last,
                          hypertable_size('readings') AS bytes,
                          (SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'readings') AS chunks""")
c = services._compression_stats()
before, after = c.get("before_compression_total_bytes"), c.get("after_compression_total_bytes")
print(f"Measuring (median of {args.runs} runs after a warm-up) ...", file=sys.stderr)
b = services.benchmark(args.runs)
days = (counts["last"] - counts["first"]).days if counts["first"] else 0

print(f"### Tiger Data by the numbers (TimescaleDB {ver}, PostgreSQL {pg.split()[0]})\n")
print("| Metric | Value |")
print("|---|---|")
print(f"| Sensor readings (1-minute) | {counts['readings']:,} across {counts['items']} medicines, {days} days |")
print(f"| `readings` hypertable | {counts['chunks']} daily chunks, {mib(counts['bytes'])} on disk |")
if before and after:
    print(f"| Columnstore (compressed chunks) | {mib(before)} → {mib(after)} (**{before / after:.1f}×** smaller) |")
print(f"| `readings_5m` / `readings_1d` rollups | {counts['r5m']:,} / {counts['r1d']:,} rows |")
print()
print("| Query | Continuous aggregate | Raw readings | Speedup |")
print("|---|---:|---:|---:|")
for label, k in (("Life budget, every medicine", "budget_all_items"), ("30-day calendar, every medicine", "calendar_30d")):
    x = b[k]
    print(f"| {label} | {ms(x['continuous_aggregate_ms'])} ({x['aggregate_rows_read']:,} rows) "
          f"| {ms(x['raw_readings_ms'])} ({x['raw_rows_scanned']:,} rows) | **{x['speedup']}×** |")
print(f"\nMedian of {b['runs']} runs after a warm-up, measured from this machine to Tiger Cloud (includes network round trip).")
