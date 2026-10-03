import math

from lifelog import engine
from lifelog.seed import PRODUCTS

INS = PRODUCTS["insulin"]
EPI = PRODUCTS["epi"]


def test_target_range_burns_nothing():
    assert engine.burn_rate(5, INS) == 0


def test_room_band_matches_label_budget():
    assert math.isclose(engine.burn_rate(20, INS), 1 / 672)
    assert math.isclose(engine.hours_left(1.0, 20, INS), 672)


def test_above_limit_doubles_every_10c():
    assert math.isclose(engine.burn_rate(30.0001, INS), 1 / 8, rel_tol=1e-3)
    assert math.isclose(engine.burn_rate(40, INS) / engine.burn_rate(30.0001, INS), 2, rel_tol=1e-3)


def test_freeze_consumes_everything():
    assert engine.burn_rate(-1, INS) == math.inf
    assert engine.step_burn(-1, INS, 1 / 12) == 1.0
    assert engine.hours_left(0.9, -1, INS) == 0.0


def test_cold_ok_vs_not():
    assert engine.burn_rate(1, INS) == 0           # fridge running cold is fine for insulin
    assert engine.burn_rate(5, EPI) == 1 / 8       # epinephrine should not be refrigerated


def test_zone_labels():
    assert engine.zone_of(5, 5, INS) == "Labeled storage"
    assert engine.zone_of(20, 20, INS) == "Room-temp allowance"
    assert engine.zone_of(40, 39, INS) == "Above labeled limit"
    assert engine.zone_of(4, -0.5, INS) == "Frozen"


def test_whatif_best_option_first_and_preserves():
    w = engine.whatif(0.8, 35, INS)
    assert w[0]["remaining_after"] >= w[-1]["remaining_after"]
    now = next(o for o in w if o["option"] == "Leave it where it is")
    assert now["remaining_after"] == 0.0             # 12 h at 35C exhausts it
    assert w[0]["preserved_vs_now"] > 0.7


def test_fingerprint_shape_and_similarity():
    a = engine.fingerprint([4.5] * 100)
    b = engine.fingerprint([4.5] * 80 + [40] * 20)
    assert len(a) == 12 and math.isclose(sum(a[:10]), 1)
    assert a != b


def test_outage_fridge_warms_gradually():
    glp1 = PRODUCTS["glp1"]
    fridge = engine.outage_hours_left(1.0, 5, 32, glp1)
    room = engine.outage_hours_left(1.0, 32, 32, glp1)
    assert room is not None and room < 10
    assert fridge is None or fridge > room + 5
