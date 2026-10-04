"""Text-message and phone-call alerts through Twilio's REST API, for people who aren't watching an app: an older
patient with a flip phone, or a caregiver. Without TWILIO_* settings every message is recorded as a dry run, so the
demo shows exactly what would have been sent and to whom."""
import re
from datetime import timedelta
from xml.sax.saxutils import escape

import httpx

from . import config, db

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/{kind}.json"
CRITICAL_KINDS = {"frozen", "above_limit", "budget", "outage", "expired", "in_use_over", "predicted_excursion"}
REMINDER_DAYS = (7, 3, 1)


def normalize_phone(raw: str) -> str:
    """US numbers without a country code get +1; anything else must already be in +<country><number> form."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("+"):
        num = "+" + re.sub(r"\D", "", digits)
    elif len(digits) == 10:
        num = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        num = "+" + digits
    else:
        raise ValueError("Enter a phone number like (313) 555-0123 or +13135550123.")
    if not re.fullmatch(r"\+[1-9]\d{7,14}", num):
        raise ValueError("That phone number doesn't look right.")
    return num


def _auth() -> tuple[str, str] | None:
    """API key + secret when set (Twilio's recommendation), otherwise the account SID + auth token."""
    if config.TWILIO_API_KEY_SID and config.TWILIO_API_KEY_SECRET:
        return config.TWILIO_API_KEY_SID, config.TWILIO_API_KEY_SECRET
    if config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN:
        return config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN
    return None


def configured() -> bool:
    return bool(config.TWILIO_ACCOUNT_SID and _auth() and config.TWILIO_FROM)


def _send(channel: str, to: str, body: str) -> tuple[str, str | None, str | None]:
    if not configured():
        return "dry_run", None, None
    kind = "Messages" if channel == "sms" else "Calls"
    data = {"To": to, "From": config.TWILIO_FROM}
    if channel == "sms":
        data["Body"] = body
    else:   # read it aloud twice, slowly enough for a landline
        said = escape(body.replace("LIFELOG:", "This is LIFELOG."))
        data["Twiml"] = f'<Response><Say voice="alice">{said}</Say><Pause length="1"/><Say voice="alice">{said}</Say></Response>'
    try:
        r = httpx.post(API.format(sid=config.TWILIO_ACCOUNT_SID, kind=kind), data=data,
                       auth=_auth(), timeout=15)
        if r.status_code >= 300:
            return "failed", None, f"HTTP {r.status_code}: {r.json().get('message', r.text[:120])}"
        return "sent", r.json().get("sid"), None
    except httpx.HTTPError as e:
        return "failed", None, type(e).__name__


def deliver(contact: dict, channel: str, body: str, *, kind: str, item_id: int | None = None,
            alert_id: int | None = None) -> dict:
    status, sid, err = _send(channel, contact["phone"], body)
    return db.one("""INSERT INTO notifications (user_id, contact_id, item_id, alert_id, kind, channel, to_phone, body,
                                                status, provider_id, error)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                  (contact["user_id"], contact["id"], item_id, alert_id, kind, channel, contact["phone"], body,
                   status, sid, err))


def _link() -> str:
    return f" {config.PUBLIC_URL.rstrip('/')}" if config.PUBLIC_URL else ""


def on_alert(ev: dict) -> None:
    """Called for every alert event (realtime.py). A critical alert that just opened goes to each of the owner's
    contacts once: a text, and a phone call if they asked for calls."""
    if ev.get("resolved") or ev.get("severity") != "critical" or ev.get("kind") not in CRITICAL_KINDS:
        return
    with db.system():
        item = db.one("SELECT id, user_id, nickname FROM items WHERE id = %s", (ev.get("item_id"),))
        if not item:
            return
        contacts = db.query("SELECT * FROM alert_contacts WHERE user_id = %s AND active", (item["user_id"],))
        body = f"LIFELOG: {ev.get('message', '')}{_link()}"[:600]
        for c in contacts:
            for channel in ("sms", "voice"):
                if not c[channel]:
                    continue
                if db.one("""SELECT 1 AS x FROM notifications WHERE contact_id = %s AND kind = 'alert' AND alert_id = %s
                             AND channel = %s""", (c["id"], ev.get("id"), channel)):
                    continue
                deliver(c, channel, body, kind="alert", item_id=item["id"], alert_id=ev.get("id"))


def send_reminders() -> int:
    """Refill / use-by reminders by text at 7, 3 and 1 days before a medicine's use-by date (expiry or in-use limit).
    Each threshold is sent once per medicine and contact."""
    from . import engine, services
    sent = 0
    with db.system():
        for c in db.query("SELECT * FROM alert_contacts WHERE active AND reminders AND sms"):
            for it in db.query("""SELECT i.id, i.nickname, i.opened_at, i.expires_on, p.model FROM items i
                                  JOIN products p ON p.id = i.product_id WHERE i.user_id = %s""", (c["user_id"],)):
                d = engine.dates_info(it["opened_at"], it["expires_on"], it["model"].get("in_use_days"), services.now())
                if not d.get("use_by") or d["days_left"] < 0:
                    continue
                due = [n for n in REMINDER_DAYS if d["days_left"] <= n]
                if not due:
                    continue
                kind = f"reminder-{min(due)}"
                if db.one("SELECT 1 AS x FROM notifications WHERE contact_id = %s AND kind = %s AND item_id = %s",
                          (c["id"], kind, it["id"])):
                    continue
                days = max(0, int(d["days_left"] + 0.999))
                body = (f"LIFELOG: {it['nickname']} must be used by {d['use_by'].strftime('%b %d')} "
                        f"({d['use_by_reason']}), {'today' if days == 0 else f'in {days} day' + ('s' if days != 1 else '')}. "
                        f"Time to ask your pharmacy for a refill.{_link()}")
                deliver(c, "sms", body, kind=kind, item_id=it["id"])
                sent += 1
    return sent


def recent(user_id: int, limit: int = 30) -> list[dict]:
    return db.query("SELECT * FROM notifications WHERE user_id = %s ORDER BY id DESC LIMIT %s", (user_id, limit))


def since(minutes: int) -> timedelta:
    return timedelta(minutes=minutes)
