"""Reminders, caregivers, power reports, audit log, account export/delete and translations.
Needs DATABASE_URL with schema applied and demo data seeded."""
import io
import json
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from lifelog import auth, config, db, services

pytestmark = pytest.mark.skipif(not config.DATABASE_URL, reason="no DATABASE_URL")


def owner():
    with db.system():
        return db.one("SELECT id FROM users WHERE is_me ORDER BY id LIMIT 1")["id"]


@pytest.fixture()
def temp_user():
    u = auth.signup("Temp", f"temp.{date.today()}.{id(object())}@example.com", "password-123")
    yield u
    with db.system():
        db.execute("DELETE FROM users WHERE id = %s", (u["id"],))


def test_reminders_list_the_soonest_use_by_first(temp_user):
    uid = temp_user["id"]
    with db.system():
        pid = db.one("SELECT id FROM products ORDER BY id LIMIT 1")["id"]
        iid = db.one("""INSERT INTO items (user_id, product_id, nickname, started_at, expires_on)
                        VALUES (%s,%s,'Refill pen', now(), %s) RETURNING id""", (uid, pid, date.today() + timedelta(days=2)))["id"]
    tok = db.set_user(uid)
    try:
        r = services.reminders(uid)
    finally:
        db.reset_user(tok)
    assert r[0]["item_id"] == iid and r[0]["urgency"] == "now"


def test_caregiver_link_shows_the_other_persons_medicines(temp_user):
    token = services.create_share(owner(), "test")
    tok = db.set_user(temp_user["id"])
    try:
        services.add_care_link(temp_user["id"], f"https://lifelog.example/s/{token}", "Test parent")
        people = services.care_people(temp_user["id"])
        assert people[0]["active"] and people[0]["view"]["items"]
        with pytest.raises(ValueError):
            services.add_care_link(temp_user["id"], "https://lifelog.example/s/not-a-real-token", None)
    finally:
        db.reset_user(tok)
        with db.system():
            db.execute("DELETE FROM shares WHERE token = %s", (token,))


def test_reporting_power_out_switches_on_the_outage_model(temp_user):
    uid = temp_user["id"]
    s = services.report_power(uid, True)
    with db.system():
        assert s["reported_out"] and services.outage_for(uid) is not None
    s = services.report_power(uid, False)
    with db.system():
        assert not s["reported_out"] and services.outage_for(uid) is None


def test_pharmacist_decisions_are_logged_and_reach_receipts():
    with db.system():
        ph = db.one("SELECT id, name FROM users WHERE role = 'pharmacist' LIMIT 1")
        pid = db.one("SELECT id FROM products ORDER BY id LIMIT 1")["id"]
        services.submit_review(pid, ph, "approved", "PharmD (test)", "checked in a test")
        rr = services.rules_review(pid)
    assert rr["review"]["status"] == "approved" and rr["decisions"][0]["action"] == "review_approved"
    assert rr["decisions"][0]["by"] == ph["name"]


def test_export_and_delete_my_account(temp_user):
    uid = temp_user["id"]
    with db.system():
        pid = db.one("SELECT id FROM products ORDER BY id LIMIT 1")["id"]
        iid = db.one("INSERT INTO items (user_id, product_id, nickname, started_at) VALUES (%s,%s,'Export pen', now() - interval '1 hour') RETURNING id",
                     (uid, pid))["id"]
        db.execute("INSERT INTO readings (ts, item_id, temp_c) VALUES (now() - interval '10 minutes', %s, 4.5), (now() - interval '5 minutes', %s, 4.6)",
                   (iid, iid))
    z = zipfile.ZipFile(io.BytesIO(services.export_account(uid)))
    data = json.loads(z.read("account.json"))
    assert data["account"]["id"] == uid and data["medicines"][0]["nickname"] == "Export pen"
    assert z.read(f"readings/medicine_{iid}.csv").decode().count("\n") == 3
    res = services.delete_account(uid)
    assert res["readings"] == 2
    with db.system():
        assert not db.one("SELECT 1 AS x FROM readings WHERE item_id = %s", (iid,))
        assert not db.one("SELECT 1 AS x FROM users WHERE id = %s", (uid,))


def test_every_language_has_every_interface_phrase():
    d = json.loads((Path(__file__).resolve().parent.parent / "static" / "i18n.json").read_text(encoding="utf-8"))
    keys = set(d["en"])
    for lang in ("es", "ar", "bn"):
        assert set(d[lang]) == keys, (lang, keys ^ set(d[lang]))
        assert all(d[lang][k] for k in keys), lang


def test_evidence_proposals_cover_four_medicines():
    from lifelog.seed import EVIDENCE
    assert set(EVIDENCE) == {"epi", "insulin", "novolog", "humalog"}
    for ev in EVIDENCE.values():
        assert ev["citation"] and ev["url"].startswith("https://") and ev["proposed"] > 8
