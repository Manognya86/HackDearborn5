"""Live trend forecast: engine math (no database) and the SQL trend + early-warning alert (needs DATABASE_URL)."""
import math
from datetime import datetime, timedelta, timezone

import pytest

from lifelog import config, engine
from lifelog.seed import PRODUCTS

INSULIN = PRODUCTS["insulin"]   # 2-8°C, do not freeze


def test_steady_forecast_equals_constant_temperature_hours_left():
    f = engine.trend_forecast(0.8, 25.0, 0.1, 0.9, 6, INSULIN)
    assert f["trend"] == "steady"
    assert f["hours_left"] == engine.hours_left(0.8, 25.0, INSULIN)


def test_noisy_trend_is_treated_as_steady():
    assert engine.trend_forecast(1.0, 5.0, 3.0, 0.2, 6, INSULIN)["trend"] == "steady"   # poor fit
    assert engine.trend_forecast(1.0, 5.0, 3.0, 0.9, 2, INSULIN)["trend"] == "steady"   # too few buckets


def test_newton_warming_crosses_the_limit_when_the_closed_form_says():
    # T(h) = 22 - 17 e^(-h/tau), tau = 17 / 3 h; crosses 8°C at tau * ln(17/14)
    f = engine.trend_forecast(1.0, 5.0, 3.0, 0.95, 6, INSULIN, ambient_t=22.0)
    expected = (17 / 3) * math.log(17 / 14) * 60
    assert f["trend"] == "warming" and abs(f["minutes_to_limit"] - expected) <= 1.5
    assert f["hours_left"] is not None  # at 22°C it lives on the 28-day room allowance


def test_linear_trend_is_capped_without_a_known_ambient():
    f = engine.trend_forecast(1.0, 5.0, 2.0, 0.95, 6, INSULIN)   # reaches 7°C after 1 h, then holds
    assert f["minutes_to_limit"] is None and f["hours_left"] is None


def test_cooling_trend_predicts_freezing():
    f = engine.trend_forecast(1.0, 3.0, -4.0, 0.9, 6, INSULIN)
    assert f["minutes_to_freeze"] == 45
    assert f["hours_left"] is not None and f["hours_left"] <= 0.76  # frozen = budget gone


def test_observed_tau_from_live_slope():
    assert engine.observed_tau(4.0, 4.0, 0.9, 6, 32.0) == pytest.approx(7.0)
    assert engine.observed_tau(4.0, 0.2, 0.9, 6, 32.0) is None          # not trending
    # a faster measured warming than the 6 h assumption shortens the outage forecast
    fast = engine.outage_hours_left(1.0, 4.0, 32.0, INSULIN, tau_h=1.0)
    assumed = engine.outage_hours_left(1.0, 4.0, 32.0, INSULIN)
    assert fast < assumed


@pytest.mark.skipif(not config.DATABASE_URL, reason="no DATABASE_URL")
def test_live_warming_ramp_raises_an_early_warning():
    from lifelog import db, services
    user = db.one("SELECT id FROM users WHERE NOT is_me AND NOT synthetic ORDER BY id LIMIT 1")
    prod = db.one("SELECT id FROM products WHERE name ILIKE 'Lantus%' LIMIT 1")
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    iid = db.one("""INSERT INTO items (user_id, product_id, nickname, started_at) VALUES (%s, %s, 'forecast test', %s)
                    RETURNING id""", (user["id"], prod["id"], end - timedelta(hours=1)))["id"]
    try:
        # 4.0 -> 7.0°C over the last 30 minutes: 6°C/h, about 10 minutes from the 8°C limit
        pts = [{"ts": end - timedelta(minutes=30 - k), "temp_c": 4.0 + 0.1 * k} for k in range(31)]
        services.ingest(iid, pts, auto_refresh=True)
        tr = services.item_trend(iid)
        assert tr["n"] >= 4 and tr["slope_c_per_h"] == pytest.approx(6.0, rel=0.15) and tr["r2"] > 0.9
        kinds = {r["kind"] for r in db.query("SELECT kind FROM current_conditions() WHERE item_id = %s", (iid,))}
        assert "warming_trend" in kinds
        d = services.item_detail(iid)
        assert d["forecast"]["trend"] == "warming" and d["forecast"]["minutes_to_limit"] <= 30
    finally:
        db.execute("DELETE FROM readings WHERE item_id = %s", (iid,))
        db.execute("DELETE FROM ingest_log WHERE item_id = %s", (iid,))
        db.execute("DELETE FROM items WHERE id = %s", (iid,))
        db.refresh_readings(end - timedelta(minutes=35), end)
