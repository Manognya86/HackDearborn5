"""Alerts job and exposure statistics. Needs DATABASE_URL with schema applied and demo data seeded."""
import math

import pytest

from lifelog import config

pytestmark = pytest.mark.skipif(not config.DATABASE_URL, reason="no DATABASE_URL")


def test_mkt_of_constant_temperature_is_that_temperature():
    from lifelog import db
    r = db.one("SELECT 10000.0 / -ln(avg(exp(-10000.0 / (t + 273.15)))) - 273.15 AS mkt "
               "FROM (VALUES (5.0::float8), (5.0), (5.0)) v(t)")
    assert math.isclose(r["mkt"], 5.0, abs_tol=1e-9)


def test_mkt_is_never_below_the_plain_average():
    from lifelog import db
    for r in db.query("SELECT i.id, s.* FROM items i, LATERAL item_stats(i.id) s"):
        if r["mkt_c"] is not None:
            assert r["mkt_c"] >= r["avg_c"] - 1e-9, r


def test_alert_job_keeps_one_open_alert_per_item_and_kind():
    from lifelog import db, services
    services.check_alerts()
    services.check_alerts()
    dupes = db.query("""SELECT item_id, kind, count(*) FROM alerts WHERE resolved_at IS NULL
                        GROUP BY 1, 2 HAVING count(*) > 1""")
    assert dupes == []


def test_open_alerts_match_current_conditions():
    from lifelog import db, services
    services.check_alerts()
    open_ = {(r["item_id"], r["kind"]) for r in db.query("SELECT item_id, kind FROM alerts WHERE resolved_at IS NULL")}
    now = {(r["item_id"], r["kind"]) for r in db.query("SELECT item_id, kind FROM current_conditions()")}
    assert open_ == now


def test_alert_job_is_scheduled():
    from lifelog import db
    assert db.one("SELECT 1 AS ok FROM timescaledb_information.jobs WHERE proc_name = 'check_alerts'")



def test_daily_rollup_matches_the_5_minute_rollup():
    """readings_1d (built on readings_5m) carries the same budget burn as the item timeline."""
    from lifelog import db
    for item in db.query("SELECT id FROM items ORDER BY id LIMIT 4"):
        daily = db.one("SELECT coalesce(sum(burn), 0) AS b FROM readings_1d WHERE item_id = %s", (item["id"],))["b"]
        five = db.one("SELECT coalesce(sum(burn), 0) AS b FROM item_timeline(%s)", (item["id"],))["b"]
        assert abs(daily - five) < 1e-6, (item, daily, five)


def test_expired_and_in_use_alerts():
    from lifelog import db, services
    services.check_alerts()
    kinds = {r["kind"] for r in db.query("SELECT kind FROM alerts WHERE resolved_at IS NULL")}
    expired = db.one("SELECT count(*) AS n FROM items WHERE expires_on < current_date")["n"]
    assert ("expired" in kinds) == (expired > 0)
