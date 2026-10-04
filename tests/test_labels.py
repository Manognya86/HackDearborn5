"""Every medicine's rules against its real FDA label text (saved in data/labels/, refreshed by scripts/verify_labels.py).
No database or network needed."""
import copy

import pytest

from lifelog import engine, medicines as M, offline


@pytest.mark.parametrize("key", list(M.PRODUCTS))
def test_every_quote_is_word_for_word_in_the_fda_label(key):
    r = M.check_model(M.PRODUCTS[key], M.label_text(key))
    assert not r["unverified"], r["unverified"]
    assert not r["number_issues"], r["number_issues"]
    assert len(r["verified"]) >= 3


@pytest.mark.parametrize("key", list(M.PRODUCTS))
def test_source_link_points_at_the_saved_label_version(key):
    lab, src = M.label(key), M.SOURCES[key]
    assert src["set_id"] == lab["set_id"] and src["effective"] == lab["effective_time"]
    assert lab["effective_time"][:4] in src["label"] and lab["set_id"] in src["url"]


def test_the_checker_catches_a_changed_quote_and_a_wrong_number():
    m = copy.deepcopy(M.PRODUCTS["humira"])
    m["bands"][0]["quote"] = m["bands"][0]["quote"].replace("14 days", "28 days")
    m["discard_rules"].append("Keep HUMIRA in the freezer.")
    r = M.check_model(m, M.label_text("humira"))
    assert {u["field"] for u in r["unverified"]} == {"band: Room-temp allowance", "discard rule"}
    m = copy.deepcopy(M.PRODUCTS["humira"])
    m["bands"][0]["budget_hours"] = 28 * 24          # quote still says 14 days
    assert M.check_model(m, M.label_text("humira"))["number_issues"]


@pytest.mark.parametrize("key", list(M.PRODUCTS))
def test_rule_based_reader_gets_the_same_numbers_as_the_checked_rules(key):
    """The no-AI fallback reads the same storage section that 'add by name' gets from openFDA."""
    f = M.label(key)["fields"]
    text = f.get("storage_and_handling") or f.get("how_supplied") or ""
    text = text if len(text) <= 6000 else text[max(text.lower().find("storage") - 300, 0):][:6000]
    got, want = offline.parse_label_text(key, text), M.PRODUCTS[key]
    assert (got["target_min_c"], got["target_max_c"]) == (want["target_min_c"], want["target_max_c"])
    assert got["freeze_discard"] == want["freeze_discard"]
    real = [b for b in want["bands"] if not b.get("budget_is_assumption")][:1]
    assert [(b["max_c"], b["budget_hours"]) for b in got["bands"]][:1] == [(b["max_c"], b["budget_hours"]) for b in real]
    assert M.check_model(got, M.label_text(key))["all_verified"]


@pytest.mark.parametrize("key", list(M.PRODUCTS))
def test_engine_uses_each_allowance_exactly_as_labeled(key):
    m = M.PRODUCTS[key]
    mid = (m["target_min_c"] + m["target_max_c"]) / 2
    assert engine.burn_rate(mid, m) == 0.0
    for b in m["bands"]:
        t = (b["min_c"] + b["max_c"]) / 2
        if not any(o is not b and o["min_c"] <= t <= o["max_c"] for o in m["bands"]):
            assert engine.burn_rate(t, m) == pytest.approx(1 / b["budget_hours"])   # the whole allowance, used evenly
    top = max([m["target_max_c"]] + [b["max_c"] for b in m["bands"]])
    assert engine.burn_rate(top + 10, m) == pytest.approx(2 / m["above_limit_budget_hours"])
    if m["freeze_discard"]:
        assert engine.step_burn(-1.0, m, 0.1) == 1.0


def test_twenty_two_medicines_across_classes():
    assert len(M.PRODUCTS) == 22 and set(M.SHORT) == set(M.PRODUCTS) == set(M.BRANDS)
