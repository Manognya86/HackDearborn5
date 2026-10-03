"""Create extensions, tables, continuous aggregate, SQL functions, policy, and seed demo data.
Idempotent: safe to run repeatedly (each run reseeds from scratch).

    .venv/Scripts/python scripts/setup_db.py                # demo seed (14 days, 26 medicines)
    .venv/Scripts/python scripts/setup_db.py --scale 50     # + 50 synthetic medicines x 90 days of 1-minute readings
    .venv/Scripts/python scripts/setup_db.py --no-weather   # skip the Open-Meteo archive download
Scale sizes above 50 need --yes (check the printed storage estimate first)."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lifelog import scale, seed  # noqa: E402

SAFE_SCALE = 50

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--no-weather", action="store_true", help="synthetic porch weather instead of Open-Meteo")
ap.add_argument("--scale", type=int, default=0, metavar="N", help="add N synthetic medicines with 90 days of history")
ap.add_argument("--days", type=int, default=scale.DAYS, help="days of history per synthetic medicine (default 90)")
ap.add_argument("--yes", action="store_true", help=f"allow --scale above {SAFE_SCALE}")
args = ap.parse_args()

print("Applying schema + functions ...")
seed.apply_schema()

if args.scale:
    est = scale.estimate(args.scale, args.days)
    print(f"Scale estimate: {est['rows']:,} readings x {est['bytes_per_row']} B = "
          f"~{est['uncompressed_bytes'] / 2**30:.2f} GiB uncompressed, "
          f"~{est['compressed_bytes_guess'] / 2**20:,.0f} MiB after columnstore; readings_5m ~{est['cagg_rows']:,} rows")
    if args.scale > SAFE_SCALE and not args.yes:
        sys.exit(f"--scale {args.scale} is above {SAFE_SCALE}: check the estimate against your service's storage, "
                 f"then re-run with --yes")

print("Seeding demo data ...")
print(seed.run(with_weather=not args.no_weather))

if args.scale:
    print(f"Scale mode: {args.scale} synthetic medicines ...")
    r = scale.generate(args.scale, args.days)
    mib = 2**20
    print(f"  readings total      {r['readings_total']:,} ({r['rows_inserted']:,} synthetic)")
    print(f"  hypertable size     {r['bytes_before'] / mib:,.1f} MiB rowstore -> {r['bytes_after'] / mib:,.1f} MiB "
          f"after columnstore ({r['compression_ratio']}x)")
    print(f"  aggregate refresh   {r['refresh_s']} s; columnstore conversion {r['compress_s']} s; total {r['total_s']} s")
    print("Note: Demo controls -> Reset returns to the small seed; re-run this command to restore scale data.")
