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


def test_spike_inside_bucket_counts_per_reading():
    """A 1-minute 25C spike in a 3C bucket averages 7.4C (in range): burns nothing by average, but does per reading."""
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    temps = [3.0, 3.0, 25.0, 3.0, 3.0]
    exact = engine.exact_budget_used([(t0 + timedelta(minutes=i), t) for i, t in enumerate(temps)], INS)
    naive = engine.step_burn(sum(temps) / len(temps), INS, engine.BUCKET_H)
    assert naive == 0 and exact > 0


def test_validation_rejects_impossible_and_flags_jumps():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    rows = [{"ts": t0, "temp_c": 4.5}, {"ts": t0 + timedelta(minutes=1), "temp_c": 999},
            {"ts": t0 + timedelta(minutes=2), "temp_c": 40.0}, {"ts": t0 + timedelta(minutes=2), "temp_c": 5.0}]
    ok, rejected, flagged = engine.validate_readings(rows)
    assert len(rejected) == 2 and len(ok) == 2 and len(flagged) == 1


def test_gap_widens_uncertainty_and_silence_changes_status():
    worst = engine.gap_worst_case(1.0, [{"hours": 6, "last_temp": 4.5}], INS)
    assert worst < 1.0
    st = engine.status_of({"remaining": 1.0, "stale_minutes": 360}, worst, "Labeled storage")
    assert st["code"] == "CHECK"
    assert engine.status_of({"remaining": 1.0, "stale_minutes": 0}, 1.0, "Labeled storage")["code"] == "USE"
