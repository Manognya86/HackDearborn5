"""Accounts and sessions. Passwords are stored only as PBKDF2-SHA256 hashes; sessions are random tokens in an
HttpOnly cookie. What a signed-in user can see is enforced by row-level security (sql/security.sql)."""
import hashlib
import hmac
import secrets
from datetime import timedelta

from . import config, db

COOKIE = "lifelog_session"
ITERATIONS = 310_000
SESSION_DAYS = 14


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, it, salt, digest = (stored or "").split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(calc.hex(), digest)
    except ValueError:
        return False


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with db.system():
        db.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, now() + %s)",
                   (token, user_id, timedelta(days=SESSION_DAYS)))
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    with db.system():
        return db.one("""SELECT u.id, u.name, u.email, u.role, u.credentials FROM sessions s JOIN users u ON u.id = s.user_id
                         WHERE s.token = %s AND s.expires_at > now()""", (token,))


def end_session(token: str | None) -> None:
    if token:
        with db.system():
            db.execute("DELETE FROM sessions WHERE token = %s", (token,))


def login(email: str, password: str) -> dict | None:
    with db.system():
        u = db.one("SELECT id, name, password_hash FROM users WHERE lower(email) = lower(%s)", (email.strip(),))
    return u if u and verify_password(password, u["password_hash"]) else None


def signup(name: str, email: str, password: str) -> dict:
    if len(password) < 8:
        raise ValueError("Use at least 8 characters for the password.")
    if "@" not in email:
        raise ValueError("Enter a valid email address.")
    with db.system():
        if db.one("SELECT 1 AS x FROM users WHERE lower(email) = lower(%s)", (email.strip(),)):
            raise ValueError("An account with this email already exists. Sign in instead.")
        return db.one("""INSERT INTO users (name, is_me, can_host, lat, lon, geom, email, password_hash, role)
                         VALUES (%s, FALSE, FALSE, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, 'patient')
                         RETURNING id, name""",
                      (name.strip() or email.split("@")[0], config.HOME_LAT, config.HOME_LON, config.HOME_LON,
                       config.HOME_LAT, email.strip(), hash_password(password)))


def demo_user(role: str) -> dict | None:
    """'Try the demo' for judges: the seeded demo patient or demo pharmacist (ENABLE_DEMO_LOGIN, default on)."""
    if not config.ENABLE_DEMO_LOGIN:
        return None
    with db.system():
        if role == "pharmacist":
            return db.one("SELECT id, name FROM users WHERE role = 'pharmacist' ORDER BY id LIMIT 1")
        if role == "customer":
            return db.one("SELECT id, name FROM users WHERE lower(email) = %s", (config.DEMO_ACCOUNTS["customer"][0],))
        return db.one("SELECT id, name FROM users WHERE is_me ORDER BY id LIMIT 1")   # the owner demo
