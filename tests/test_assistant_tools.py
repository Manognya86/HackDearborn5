"""The chatbot's tools return exactly what the app shows, in explicit units. Needs DATABASE_URL with demo data."""
import pytest

from lifelog import config

pytestmark = pytest.mark.skipif(not config.DATABASE_URL, reason="no DATABASE_URL")


def test_list_is_most_urgent_first_and_in_percent():
    from lifelog import assistant, db, services
    me = db.one("SELECT id FROM users WHERE is_me")["id"]
    meds = assistant.list_my_medicines()
    app = {i["id"]: i for i in services.list_items(me)}
    assert {m["id"] for m in meds} == set(app)
    ranks = [assistant.SEVERITY[app[m["id"]]["status"]["code"]] for m in meds]
    assert ranks == sorted(ranks)
    for m in meds:
        assert m["budget_left_percent"] == round(app[m["id"]]["remaining"] * 100, 1)
        assert m["status_label"] == app[m["id"]]["status"]["label"]
        assert "trend" in m and "dates" in m


def test_details_match_the_detail_page():
    from lifelog import assistant, services
    iid = assistant.list_my_medicines()[0]["id"]
    t, d = assistant.get_medicine_details(iid), services.item_detail(iid)
    assert t["budget_left_percent"] == round(d["state"]["remaining"] * 100, 1)
    assert t["status_label"] == d["status"]["label"]
    assert len(t["what_if_next_12h"]) == len(d["whatif"])
    assert assistant.get_medicine_details(999999) == {"error": "unknown item"}


def test_calendar_and_simulation_tools():
    from lifelog import assistant, engine, services
    iid = assistant.list_my_medicines()[0]["id"]
    cal = assistant.get_exposure_calendar(iid, 7)
    assert 1 <= len(cal) <= 7 and all("budget_used_percent" in c for c in cal)
    sim = assistant.simulate_exposure(iid, 45.0, 2)
    assert sim["remaining_after"] < sim["remaining_now"]
    d = services.item_detail(iid)
    assert sim["hours_left_at_that_temp"] == engine.hours_left(d["state"]["remaining"], 45.0, d["item"]["model"])


def test_alerts_and_outage_tools_run():
    from lifelog import assistant
    assert isinstance(assistant.get_alerts(), list)
    assert "active_outages" in assistant.get_outage_situation()


def test_system_prompt_has_the_current_time_and_rules():
    from lifelog import assistant
    s = assistant._system("ar")
    assert "Dearborn" in s and "every number" in s and "Arabic" in s
