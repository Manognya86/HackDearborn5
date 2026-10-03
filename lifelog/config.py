import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Continuous-aggregate policy window. Late data older than this is NOT picked up by the
# background policy -- the ingest API does a targeted refresh instead.
POLICY_START_OFFSET = os.getenv("POLICY_START_OFFSET", "2 hours")

# Demo neighborhood: Dearborn, MI
HOME_LAT, HOME_LON = 42.3175, -83.2307
