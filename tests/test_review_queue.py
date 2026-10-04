"""The pharmacist review queue shows the care checklist even for a medicine nobody uses yet
(e.g. one added to an existing database by scripts/migrate.py). Needs DATABASE_URL with the schema applied."""
import json

import pytest

from lifelog import config

pytestmark = pytest.mark.skipif(not config.DATABASE_URL, reason="no DATABASE_URL")


def test_unused_medicine_still_has_a_checklist():
    from lifelog import db, services
    from lifelog.seed import PRODUCTS
    m = {**PRODUCTS["insulin"], "product_name": "Review-queue test pen"}
    with db.system():
        pid = db.one("""INSERT INTO products (name, model, source) VALUES ('Review-queue test pen', %s, 'demo')
                        RETURNING id""", (json.dumps(m),))["id"]
        try:
            row = next(p for p in services.review_queue() if p["id"] == pid)
            assert row["checklist"], "empty checklist for a medicine without items"
            assert any("fridge" in c for c in row["checklist"])
        finally:
            db.execute("DELETE FROM products WHERE id = %s", (pid,))
