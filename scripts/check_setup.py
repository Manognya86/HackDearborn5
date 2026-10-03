"""Verify Gemini and the database are wired up."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lifelog import config  # noqa: E402

ok = True


def check(name, fn):
    global ok
    try:
        print(f"[ OK ] {name}: {fn()}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[FAIL] {name}: {e}")


def gemini():
    from lifelog import gem
    r = gem.client().models.generate_content(model=config.GEMINI_MODEL, contents="Reply with exactly: LIFELOG online")
    return f"{config.GEMINI_MODEL}: {r.text.strip()}"


def database():
    from lifelog import db
    ext = {r["extname"]: r["extversion"] for r in db.query("SELECT extname, extversion FROM pg_extension")}
    avail = {r["name"] for r in db.query(
        "SELECT name FROM pg_available_extensions WHERE name IN ('timescaledb','vector','postgis')")}
    return f"installed={ext} available={sorted(avail)}"


check("Gemini API", gemini)
check("Database", database)
sys.exit(0 if ok else 1)
