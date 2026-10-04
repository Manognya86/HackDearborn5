import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# GEMINI_DISABLED=1 turns every Gemini call off (no request leaves the machine): for testing without spending quota
GEMINI_DISABLED = os.getenv("GEMINI_DISABLED", "").strip().lower() in ("1", "true", "yes")


# hourly retraining of the per-medicine forecasting models (set DISABLE_BACKGROUND_TRAINING=1 to turn off)
DISABLE_BACKGROUND_TRAINING = os.getenv("DISABLE_BACKGROUND_TRAINING", "").strip().lower() in ("1", "true", "yes")


# "Try the demo" sign-in buttons for judges (set ENABLE_DEMO_LOGIN=0 for a real deployment)
ENABLE_DEMO_LOGIN = os.getenv("ENABLE_DEMO_LOGIN", "1").strip().lower() not in ("0", "false", "no")
# Demo accounts that can also sign in with email + password (passwords only from .env, never committed)
DEMO_ACCOUNTS = {
    role: (os.getenv(f"DEMO_{role.upper()}_EMAIL", f"{role}@lifelog.example").strip().lower(),
           os.getenv(f"DEMO_{role.upper()}_PASSWORD", "").strip() or None)
    for role in ("owner", "customer", "pharmacist")
}
# import live DTE outage areas every 10 minutes (set LIVE_OUTAGES=0 to turn off)
LIVE_OUTAGES = os.getenv("LIVE_OUTAGES", "1").strip().lower() not in ("0", "false", "no")
# Text and phone-call alerts (Twilio). Leave empty to record messages as a dry run instead of sending them.
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM = os.getenv("TWILIO_FROM", "").strip()          # your Twilio phone number, +1...
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()            # link added to texts, e.g. https://lifelog.example.org
# extra Kubra StormCenter feeds as "name:instance_id:view_id;..." (e.g. Consumers Energy, IDs from its outage map)
OUTAGE_FEEDS = [("DTE", "4fbb3ad3-e01d-4d71-9575-d453769c1171", "8ed2824a-bd92-474e-a7c4-848b812b7f9b")] + [
    tuple(f.split(":")) for f in os.getenv("EXTRA_OUTAGE_FEEDS", "").split(";") if f.count(":") == 2]


def db_host() -> str:
    from urllib.parse import urlparse
    return urlparse(os.getenv("DATABASE_URL", "")).hostname or "unknown"


def reset_allowed() -> bool:
    """'Reset & seed data' erases the whole database: only on a local one, unless ALLOW_DEMO_RESET=1."""
    if os.getenv("ALLOW_DEMO_RESET", "").strip().lower() in ("1", "true", "yes"):
        return True
    return db_host() in ("localhost", "127.0.0.1", "::1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Tried in order when the main model is overloaded (503), out of quota (429) or unavailable (404)
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.7-flash,gemini-3.5-flash,gemini-3.1-flash-lite").split(",")
                          if m.strip()]
DATABASE_URL = os.getenv("DATABASE_URL", "")
# Per-request Gemini timeout: a slow or rate-limited Gemini fails fast with a clear message
GEMINI_TIMEOUT_S = float(os.getenv("GEMINI_TIMEOUT_S", "60"))

# Continuous-aggregate policy window. Late data older than this is NOT picked up by the
# background policy -- the ingest API does a targeted refresh instead.
POLICY_START_OFFSET = os.getenv("POLICY_START_OFFSET", "2 hours")

# Demo neighborhood: Dearborn, MI
HOME_LAT, HOME_LON = 42.3175, -83.2307
