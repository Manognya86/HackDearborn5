"""SQL burn_rate / zone_of must match engine.py. Needs DATABASE_URL with schema applied."""
import json

import pytest

from lifelog import config, engine
from lifelog.seed import PRODUCTS

pytestmark = pytest.mark.skipif(not config.DATABASE_URL, reason="no DATABASE_URL")

TEMPS = [float(x) for x in [-5, 0, 0.5, 1.9, 2, 5, 8, 8.1, 15, 19.9, 22, 25, 25.1, 29.9, 30, 30.5, 35, 45, 60]]


@pytest.mark.parametrize("key", list(PRODUCTS))
def test_sql_matches_python(key):
    from lifelog import db
    m = PRODUCTS[key]
    rows = db.query("SELECT t, burn_rate(t, %s::jsonb) AS r, zone_of(t, t, %s::jsonb) AS z FROM unnest(%s::float8[]) t",
                    (json.dumps(m), json.dumps(m), TEMPS))
    for row in rows:
        py = engine.burn_rate(row["t"], m)
        assert (py == row["r"]) or abs(py - row["r"]) < 1e-12, (key, row["t"], py, row["r"])
        assert engine.zone_of(row["t"], row["t"], m) == row["z"], (key, row["t"])
