"""Accounts, row-level security, dose tracking, evidence review and the live outage importer.
Needs DATABASE_URL with schema applied and demo data seeded (no network: HTTP calls are faked)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from lifelog import auth, config, services

pytestmark = pytest.mark.skipif(not config.DATABASE_URL, reason="no DATABASE_URL")


def test_password_hash_round_trip_and_salting():
    h1, h2 = auth.hash_password("correct horse"), auth.hash_password("correct horse")
    assert h1 != h2                        # random salt
    assert auth.verify_password("correct horse", h1)
    assert not auth.verify_password("wrong horse", h1)
    assert not auth.verify_password("x", None)
    assert not auth.verify_password("x", "garbage")


def test_polyline_decoder_matches_googles_reference_example():
    pts = services.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert pts == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


@pytest.fixture()
def two_users():
    from lifelog import db
    a = auth.signup("RLS A", f"rls.a.{datetime.now().timestamp()}@example.com", "password-a1")
    b = auth.signup("RLS B", f"rls.b.{datetime.now().timestamp()}@example.com", "password-b1")
    pid = db.one("SELECT id FROM products ORDER BY id LIMIT 1")["id"]
    ia = db.one("INSERT INTO items (user_id, product_id, nickname, started_at) VALUES (%s, %s, 'A pen', now()) RETURNING id",
                (a["id"], pid))["id"]
    yield a, b, ia
    with db.system():
        db.execute("DELETE FROM users WHERE id IN (%s, %s)", (a["id"], b["id"]))


def test_row_level_security_hides_other_peoples_medicines(two_users):
    from lifelog import db
    a, b, ia = two_users
    tok = db.set_user(b["id"])
    try:
        assert db.one("SELECT count(*) AS n FROM items WHERE id = %s", (ia,))["n"] == 0
        assert services.item_detail(ia) is None
        db.execute("UPDATE items SET nickname = 'hacked' WHERE id = %s", (ia,))   # silently matches nothing
        with pytest.raises(Exception):   # and the app role can't read password hashes at all
            db.one("SELECT password_hash FROM users LIMIT 1")
    finally:
        db.reset_user(tok)
    tok = db.set_user(a["id"])
    try:
        row = db.one("SELECT nickname FROM items WHERE id = %s", (ia,))
        assert row["nickname"] == "A pen"
        assert [i["id"] for i in services.list_items(a["id"])] == [ia]
    finally:
        db.reset_user(tok)


def test_dose_records_status_at_that_moment_and_rechecks_it():
    from lifelog import db
    item = db.one("SELECT i.id FROM items i JOIN users u ON u.id = i.user_id WHERE u.is_me ORDER BY i.id LIMIT 1")["id"]
    when = datetime.now(timezone.utc) - timedelta(days=2)
    d = services.log_dose(item, when, "test dose")
    try:
        assert d["status_at_dose"] in ("USE", "USE_SOON", "CHECK", "ASK_PHARMACIST", "DO_NOT_USE")
        assert 0.0 <= d["budget_at_dose"] <= 1.0
        listed = next(x for x in services.doses(item) if x["id"] == d["id"])
        assert listed["changed"] is False     # nothing changed in between
        # the budget at a past moment is never lower than the budget now (it only goes down)
        assert d["budget_at_dose"] >= services.state_at(item, datetime.now(timezone.utc))["remaining"] - 1e-9
    finally:
        db.execute("DELETE FROM doses WHERE id = %s", (d["id"],))


def test_approving_evidence_replaces_the_assumption(monkeypatch):
    from lifelog import db
    monkeypatch.setattr(services, "_recalculate_all", lambda: None)   # the full refresh is exercised elsewhere
    model = {"product_name": "Test", "target_min_c": 2, "target_max_c": 8, "bands": [], "freeze_discard": True,
             "freeze_c": 0, "above_limit_budget_hours": 8.0, "above_limit_is_assumption": True}
    pid = db.one("INSERT INTO products (name, model, source) VALUES ('Evidence test', %s, 'demo') RETURNING id",
                 (json.dumps(model),))["id"]
    try:
        ev = db.one("""INSERT INTO evidence (product_id, current_value, proposed_value, citation, finding, derivation)
                       VALUES (%s, 8, 300, 'Test 2026', 'finding', 'derivation') RETURNING id""", (pid,))["id"]
        r = services.decide_evidence(ev, True, {"id": None, "name": "Tester"})
        assert r["status"] == "approved"
        m = db.one("SELECT model FROM products WHERE id = %s", (pid,))["model"]
        assert m["above_limit_budget_hours"] == 300 and m["above_limit_is_assumption"] is False
        assert "Test 2026" in m["above_limit_quote"]
    finally:
        db.execute("DELETE FROM products WHERE id = %s", (pid,))


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def test_live_outage_import_parses_kubra_and_closes_restored_areas(monkeypatch):
    from lifelog import db, forecast_ml
    ring = "_p~iF~ps|U_ulLnnqC_mqNvxq`@_p~iF~ps|U"   # a closed triangle
    areas = {"file_data": [{"title": "99999", "desc": {"cust_a": {"val": 12}, "etr": "ETR-NULL",
                                                       "start_time": "2026-10-03T10:00:00-0400"}, "geom": {"a": [ring]}}]}
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp({"data": {"interval_generation_data": "data/x"}} if "currentState" in url else areas)

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(forecast_ml, "outdoor", lambda lat, lon: {})
    monkeypatch.setattr(config, "OUTAGE_FEEDS", [("TEST", "inst", "view")])
    try:
        res = services.import_live_outages()
        assert res["areas"] == 1 and res["customers"] == 12 and res["error"] is None
        row = db.one("SELECT customers_out, etr_known, active FROM outages WHERE external_id = 'TEST:99999'")
        assert row == {"customers_out": 12, "etr_known": False, "active": True}
        services.import_live_outages()            # re-import updates in place
        assert db.one("SELECT count(*) AS n FROM outages WHERE external_id = 'TEST:99999'")["n"] == 1
        areas["file_data"] = []                   # power restored
        services.import_live_outages()
        assert db.one("SELECT active FROM outages WHERE external_id = 'TEST:99999'")["active"] is False
    finally:
        db.execute("DELETE FROM outages WHERE source = 'test-live'")
