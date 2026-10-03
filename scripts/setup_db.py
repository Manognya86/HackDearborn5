"""Create extensions, tables, continuous aggregate, SQL functions, policy, and seed demo data."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lifelog import seed  # noqa: E402

no_weather = "--no-weather" in sys.argv
print("Applying schema + functions ...")
seed.apply_schema()
print("Seeding demo data ...")
print(seed.run(with_weather=not no_weather))
