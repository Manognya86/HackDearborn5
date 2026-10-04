"""Bring an existing database up to the current schema WITHOUT touching its data (no reseed, no truncate).
Use this on the shared Tiger Cloud service; scripts/setup_db.py reseeds and is for empty or local databases."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lifelog import config, seed  # noqa: E402

print(f"Migrating schema on {config.db_host()} (data is kept) ...")
seed.apply_schema()
print("Done: tables, functions, continuous aggregates, triggers and jobs are up to date.")
