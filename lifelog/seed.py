"""Demo data. Product models are clearly marked '(demo)': they resemble typical label rules but are NOT
copied from any real label -- upload a real label to get a Gemini-extracted model with verbatim quotes."""
import json
import math
import random
from datetime import date, datetime, timedelta, timezone

from . import config, db, engine, weather

rng = random.Random(7)


def _band(label, lo, hi, hours, quote):
    return {"label": label, "min_c": lo, "max_c": hi, "budget_hours": hours, "quote": quote}


def _model(name, form, tmin, tmax, tq, bands, cold_ok=True, freeze=True, above=8.0, visual=(), discard=()):
    return {
        "product_name": name, "form": form,
        "target_min_c": tmin, "target_max_c": tmax, "target_quote": tq,
        "freeze_discard": freeze, "freeze_c": 0.0,
        "freeze_quote": "(demo) Do not freeze. Do not use if it has been frozen." if freeze else "Not stated on label",
        "cold_ok": cold_ok, "bands": bands,
        "above_limit_budget_hours": above, "above_limit_is_assumption": True,
        "above_limit_quote": "Not stated on label (LIFELOG conservative default: 8 h above the highest labeled limit, doubling per 10C)",
        "visual_checks": list(visual), "discard_rules": list(discard), "notes": "Demo model for hackathon use.",
    }


PRODUCTS = {
    "glp1": _model("GLP-1 pen (demo)", "pen", 2, 8, "(demo) Store unused pens in the refrigerator between 2C and 8C.",
                   [_band("Room-temp allowance", 8, 30, 56 * 24,
                          "(demo) After first use the pen can be stored at room temperature up to 30C for 56 days.")],
                   visual=["Liquid should be clear and colorless"], discard=["Discard 56 days after first use"]),
    "insulin": _model("Insulin vial (demo)", "vial", 2, 8, "(demo) Unopened vials: refrigerate at 2C to 8C.",
                      [_band("Room-temp allowance", 8, 30, 28 * 24,
                             "(demo) May be kept unrefrigerated below 30C for up to 28 days.")],
                      visual=["Do not use if cloudy, thickened or contains particles"],
                      discard=["Discard 28 days after opening"]),
    "epi": _model("Epinephrine auto-injector (demo)", "auto-injector", 20, 25,
                  "(demo) Store at 20C to 25C; excursions permitted to 15C-30C.",
                  [_band("Cool excursion", 15, 20, 90 * 24, "(demo) Excursions permitted to 15C-30C."),
                   _band("Warm excursion", 25, 30, 90 * 24, "(demo) Excursions permitted to 15C-30C.")],
                  cold_ok=False, visual=["Solution should be clear; do not use if discolored (pink/brown)"]),
    "biologic": _model("Biologic pen (demo)", "pen", 2, 8, "(demo) Refrigerate at 2C to 8C in the original carton.",
                       [_band("Room-temp allowance", 8, 25, 14 * 24,
                              "(demo) May be stored at room temperature up to 25C for a single period of up to 14 days.")]),
}

NAMES = ["Margaret", "Ahmed", "Lina", "Jamal", "Rosa", "Hassan", "Denise", "Omar", "Grace", "Tyrone",
         "Fatima", "Walter", "Mei", "Carlos"]

REFUGES = [
    ("24h pharmacy (demo) - Michigan Ave", "pharmacy", 42.3049, -83.2105),
    ("Fire station (demo) - Ford Rd", "fire_station", 42.3290, -83.2560),
    ("Cooling center (demo) - Ford Community", "cooling_center", 42.3215, -83.1760),
    ("Hospital pharmacy (demo) - Oakwood", "hospital", 42.2935, -83.2152),
]

ZIPS = [("48126", "Dearborn (east)", 42.3330, -83.1780), ("48124", "Dearborn (west)", 42.2990, -83.2500),
        ("48128", "Dearborn (UM-D)", 42.3200, -83.2650), ("48120", "Dearborn (south-east)", 42.3070, -83.1590),
        ("48228", "Detroit (west)", 42.3560, -83.2160), ("48210", "Detroit (Southwest)", 42.3370, -83.1290)]


def _ts(c, sql, rows):
    with c.cursor() as cur:
        cur.executemany(sql, rows)


def fridge(t: datetime) -> float:
    base = 4.5 + 0.6 * math.sin(t.timestamp() / 1800)
    if rng.random() < 0.01:  # door opening
        base += rng.uniform(2, 5)
    return round(base + rng.gauss(0, 0.25), 2)


def room(t: datetime) -> float:
    h = t.astimezone(timezone(timedelta(hours=-4))).hour
    return round(22 + 1.5 * math.sin((h - 9) / 24 * 2 * math.pi) + rng.gauss(0, 0.3), 2)


# ------------------------------------------------------------------ pattern library
def _curve(kind: str) -> list[float]:
    pts = []
    for i in range(12 * 72):
        h = (i / 12) % 24
        if kind == "fridge":
            v = 4.5 + rng.gauss(0, 0.4)
        elif kind == "door_overnight":
            v = 4.5 if not (i > 12 * 40 and i < 12 * 49) else 14 + rng.gauss(0, 1)
        elif kind == "hot_car":
            v = 4.5 if not (i > 12 * 66 and i < 12 * 69) else 25 + (i - 12 * 66) * 0.6
        elif kind == "outage_summer":
            v = 4.5 if i < 12 * 58 else min(33, 4.5 + (i - 12 * 58) * 0.25)
        elif kind == "outage_winter":
            v = 4.5 if i < 12 * 58 else min(16, 4.5 + (i - 12 * 58) * 0.1)
        elif kind == "freezer":
            v = 4.5 if i < 12 * 70 else -6
        elif kind == "on_body":
            v = 31 if 8 <= h <= 20 else 22
        elif kind == "checked_winter":
            v = 22 if not (i > 12 * 60 and i < 12 * 66) else 3 + rng.gauss(0, 1)
        elif kind == "mailbox":
            v = 22 if not (i > 12 * 60 and i < 12 * 68) else 30 + 10 * math.sin((i - 12 * 60) / (12 * 8) * math.pi)
        elif kind == "room":
            v = 22 + rng.gauss(0, 0.5)
        pts.append(v)
    return pts


PATTERNS = [
    ("fridge", "Stable refrigerator storage", "Continuous 2-8C storage with normal door openings.", "Budget essentially untouched."),
    ("room", "Stable room-temperature storage", "Kept indoors around 22C.", "Consumes room-temp allowance slowly; fine for labeled period."),
    ("door_overnight", "Fridge door left ajar overnight", "~9 hours at 12-16C, then recovered.", "Small, recoverable budget use for most refrigerated products."),
    ("hot_car", "Left in a hot car", "Afternoon in a parked car reaching 40-45C.", "Often exceeds labeled limits; many labels say discard."),
    ("outage_summer", "Summer power outage", "Fridge without power warming to indoor heat-wave temps (~33C).", "Above-limit exposure within hours; move to refrigeration."),
    ("outage_winter", "Winter power outage", "Fridge without power drifting to ~16C.", "Usually within room-temp allowance."),
    ("freezer", "Accidentally frozen", "Placed against the freezer coil / in freezer.", "Labels typically require discarding frozen product."),
    ("on_body", "Carried on body all day", "~31C during waking hours.", "Above many room-temp limits for biologics; burns budget quickly."),
    ("checked_winter", "Checked bag in winter cargo hold", "Several hours near 0-5C.", "Freezing risk for liquids; check label."),
    ("mailbox", "Mail-order delivery left in mailbox", "Afternoon sun in a metal mailbox (30-40C).", "Heat excursion before the patient even receives it."),
]


# ------------------------------------------------------------------ main seed
def run(with_weather: bool = True) -> dict:
    t_end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    t_start = t_end - timedelta(hours=72)
    with db.conn() as c:
        c.execute("""TRUNCATE alerts, readings, ingest_log, items, products, users, refuges, outages, pattern_library,
                     deliveries, weather_hourly, zips RESTART IDENTITY CASCADE""")
        c.execute("TRUNCATE readings_5m")  # materialized rows survive a raw-table truncate
        pid = {}
        for key, m in PRODUCTS.items():
            pid[key] = c.execute("INSERT INTO products (name, model, source) VALUES (%s,%s,'demo') RETURNING id",
                                 (m["product_name"], json.dumps(m))).fetchone()["id"]

        users = [("You", True, False, config.HOME_LAT, config.HOME_LON)]
        for i, n in enumerate(NAMES):
            users.append((n, False, i in (3, 9, 12), 42.295 + rng.random() * 0.045, -83.27 + rng.random() * 0.11))
        uid = []
        for u in users:
            uid.append(c.execute("""INSERT INTO users (name, is_me, can_host, lat, lon, geom)
                                    VALUES (%s,%s,%s,%s,%s, ST_SetSRID(ST_MakePoint(%s,%s),4326)) RETURNING id""",
                                 (*u, u[4], u[3])).fetchone()["id"])
        for r in REFUGES:
            c.execute("""INSERT INTO refuges (name, kind, lat, lon, geom)
                         VALUES (%s,%s,%s,%s, ST_SetSRID(ST_MakePoint(%s,%s),4326))""", (*r, r[3], r[2]))

        items = []  # (user, product, nickname, profile)
        items += [(uid[0], pid["glp1"], "My GLP-1 pen", "fridge"),
                  (uid[0], pid["insulin"], "Insulin vial (kitchen fridge)", "fridge_gap"),
                  (uid[0], pid["epi"], "EpiPen in backpack", "room")]
        for u in uid[1:]:
            for _ in range(rng.choice([1, 1, 2])):
                key = rng.choice(["insulin", "insulin", "glp1", "biologic", "epi"])
                items.append((u, pid[key], PRODUCTS[key]["product_name"].replace(" (demo)", ""),
                              "room" if key == "epi" else "fridge"))
        iid = []
        for u, p, nick, prof in items:
            iid.append((c.execute("INSERT INTO items (user_id, product_id, nickname, started_at) VALUES (%s,%s,%s,%s) RETURNING id",
                                  (u, p, nick, t_start)).fetchone()["id"], prof))

        with c.cursor() as cur, cur.copy("COPY readings (ts, item_id, temp_c, source) FROM STDIN") as cp:
            n = 0
            for item_id, prof in iid:
                for t in engine.daterange(t_start, t_end, timedelta(minutes=1)):
                    if prof == "fridge_gap" and t > t_end - timedelta(hours=6):
                        continue  # sensor offline: these readings arrive later via /api/demo/late_upload
                    cp.write_row((t, item_id, room(t) if prof == "room" else fridge(t), "sensor"))
                    n += 1

        for kind, name, desc, outcome in PATTERNS:
            fp = engine.vector_literal(engine.fingerprint(_curve(kind)))
            c.execute("INSERT INTO pattern_library (name, description, outcome, fingerprint) VALUES (%s,%s,%s,%s::vector)",
                      (name, desc, outcome, fp))

        for z in ZIPS:
            c.execute("INSERT INTO zips VALUES (%s,%s,%s,%s)", z)

    porch = seed_porch(with_weather)
    with db.conn(autocommit=True) as c:
        # leave the in-progress bucket to real-time aggregation
        c.execute("CALL refresh_continuous_aggregate('readings_5m', NULL, time_bucket('5 minutes', now()))")
    compressed = compress_history()
    check_alerts()
    return {"readings": n, "items": len(iid), "users": len(uid), "compressed_chunks": compressed, **porch}


def seed_porch(with_weather: bool) -> dict:
    """August 2026 hourly weather per ZIP (Open-Meteo archive, synthetic fallback) + mail-order deliveries."""
    start, end = date(2026, 8, 1), date(2026, 8, 31)
    src = "synthetic"
    rows = []
    for z, _, lat, lon in ZIPS:
        temps = {}
        if with_weather:
            try:
                temps = weather.hourly(lat, lon, start, end, archive=True)
                src = "open-meteo archive"
            except Exception:
                temps = {}
        if not temps:
            t = datetime(2026, 8, 1, tzinfo=timezone.utc)
            while t.date() <= end:
                local_h = (t.hour - 4) % 24
                temps[t] = 24 + 6 * math.sin((local_h - 9) / 24 * 2 * math.pi) + rng.gauss(0, 1.5) + (2 if z in ("48126", "48210") else 0)
                t += timedelta(hours=1)
        rows += [(ts, z, v) for ts, v in temps.items()]
    with db.conn() as c, c.cursor() as cur:
        with cur.copy("COPY weather_hourly (ts, zip, temp_c) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        carriers = ["Carrier A", "Carrier B", "Carrier C"]
        pharmacies = ["MailRx (demo)", "ScriptShip (demo)"]
        dels = []
        for _ in range(600):
            z = rng.choice(ZIPS)[0]
            day = datetime(2026, 8, rng.randint(1, 30), tzinfo=timezone.utc)
            carrier = rng.choice(carriers)
            hour_local = rng.choice([10, 11, 12, 13, 14, 15, 16]) if carrier != "Carrier C" else rng.choice([9, 10, 18, 19])
            delivered = day + timedelta(hours=hour_local + 4, minutes=rng.randint(0, 59))
            wait = rng.choice([0.5, 1, 2, 3, 4, 6, 8]) * (1.6 if z in ("48126", "48210") else 1)
            dels.append((z, carrier, rng.choice(pharmacies), rng.choice(["insulin", "GLP-1", "biologic"]),
                         delivered, delivered + timedelta(hours=wait)))
        cur.executemany("""INSERT INTO deliveries (zip, carrier, pharmacy, product_kind, delivered_at, picked_up_at)
                           VALUES (%s,%s,%s,%s,%s,%s)""", dels)
    return {"weather_rows": len(rows), "weather_source": src, "deliveries": 600}


# ------------------------------------------------------------------ demo scenarios
def me_item(nickname_like: str) -> int:
    return db.one("""SELECT i.id FROM items i JOIN users u ON u.id = i.user_id
                     WHERE u.is_me AND i.nickname ILIKE %s ORDER BY i.id LIMIT 1""", (f"%{nickname_like}%",))["id"]


def scenario_hot_car(item_id: int, hours: float = 2.0) -> dict:
    """Overwrite the last `hours` with a hot-car ramp (25C -> 47C)."""
    t_end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    t0 = t_end - timedelta(hours=hours)
    db.execute("DELETE FROM readings WHERE item_id = %s AND ts >= %s", (item_id, t0))
    pts = []
    total = int(hours * 60)
    for k, t in enumerate(engine.daterange(t0, t_end, timedelta(minutes=1))):
        pts.append({"ts": t, "temp_c": round(25 + 22 * min(1, k / (total * 0.6)) + rng.gauss(0, 0.5), 2)})
    from . import services
    res = services.ingest(item_id, pts, source="sensor", auto_refresh=False)
    db.refresh_readings(t0 - timedelta(minutes=5), t_end)
    return res


def late_upload_points(item_id: int) -> list[dict]:
    """The 6 hours the insulin sensor buffered while offline: fridge failed and warmed to ~24C."""
    t_end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    last = db.one("SELECT max(ts) AS t FROM readings WHERE item_id = %s", (item_id,))["t"]
    start = (last or t_end - timedelta(hours=6)) + timedelta(minutes=1)
    pts = []
    for k, t in enumerate(engine.daterange(start, t_end, timedelta(minutes=1))):
        temp = min(31.5, 4.5 + k * 0.12) if k < 240 else 31.5 - (k - 240) * 0.2
        pts.append({"ts": t, "temp_c": round(max(5.0, temp) + rng.gauss(0, 0.3), 2)})
    return pts


def scenario_storm(hours_to_restore: float = 12, indoor_temp_c: float = 32.0) -> dict:
    """Summer storm: outage polygon over central Dearborn; affected fridges start warming."""
    db.execute("UPDATE outages SET active = FALSE")
    lat, lon = config.HOME_LAT, config.HOME_LON
    poly = (f"POLYGON(({lon-0.045} {lat-0.022},{lon+0.035} {lat-0.026},{lon+0.05} {lat+0.012},"
            f"{lon+0.01} {lat+0.03},{lon-0.04} {lat+0.02},{lon-0.045} {lat-0.022}))")
    out = db.one("""INSERT INTO outages (name, area, est_restore_at, indoor_temp_c, source)
                    VALUES ('Summer storm outage (demo)', ST_GeomFromText(%s, 4326), now() + %s * interval '1 hour', %s, 'demo')
                    RETURNING id""", (poly, hours_to_restore, indoor_temp_c))
    affected = db.query("""SELECT i.id, p.model FROM items i JOIN users u ON u.id = i.user_id
                           JOIN products p ON p.id = i.product_id
                           JOIN outages o ON o.id = %s AND ST_Contains(o.area, u.geom)""", (out["id"],))
    t_end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    t0 = t_end - timedelta(minutes=90)
    for a in affected:
        last = db.one("SELECT temp_c FROM readings WHERE item_id = %s ORDER BY ts DESC LIMIT 1", (a["id"],))
        if last and last["temp_c"] > indoor_temp_c:
            continue  # already hotter than the house (e.g. the hot-car demo): keep its history
        db.execute("DELETE FROM readings WHERE item_id = %s AND ts >= %s", (a["id"], t0))
        start_t = a["model"]["target_max_c"] - 3 if a["model"]["target_max_c"] < 15 else 23
        rate = 0.05 * rng.uniform(0.7, 1.4) if start_t < 15 else 0.12
        pts = [{"ts": t, "temp_c": round(min(indoor_temp_c, start_t + k * rate), 2)}
               for k, t in enumerate(engine.daterange(t0, t_end, timedelta(minutes=1)))]
        from . import services
        services.ingest(a["id"], pts, source="sensor", auto_refresh=False)
    db.refresh_readings(t0 - timedelta(minutes=5), t_end)
    return {"outage_id": out["id"], "affected_items": len(affected)}


def scenario_clear_storm() -> dict:
    db.execute("UPDATE outages SET active = FALSE")
    return {"ok": True}


def add_policy() -> None:
    """Background jobs Tiger runs for us: aggregate refresh, compression, retention, alert checks."""
    with db.conn(autocommit=True) as c:
        c.execute(f"""SELECT add_continuous_aggregate_policy('readings_5m',
                        start_offset => INTERVAL '{config.POLICY_START_OFFSET}',
                        end_offset => INTERVAL '5 minutes',
                        schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE)""")
        enabled = c.execute("""SELECT compression_enabled FROM timescaledb_information.hypertables
                               WHERE hypertable_name = 'readings'""").fetchone()
        if enabled and not enabled["compression_enabled"]:
            c.execute("""ALTER TABLE readings SET (timescaledb.compress,
                            timescaledb.compress_segmentby = 'item_id',
                            timescaledb.compress_orderby = 'ts DESC')""")
        c.execute("SELECT add_compression_policy('readings', INTERVAL '2 days', if_not_exists => TRUE)")
        c.execute("SELECT add_retention_policy('readings', INTERVAL '400 days', if_not_exists => TRUE)")
        c.execute("SELECT add_retention_policy('weather_hourly', INTERVAL '400 days', if_not_exists => TRUE)")
        if not c.execute("SELECT 1 FROM timescaledb_information.jobs WHERE proc_name = 'check_alerts'").fetchone():
            c.execute("SELECT add_job('check_alerts', INTERVAL '1 minute')")


def check_alerts() -> None:
    with db.conn(autocommit=True) as c:
        c.execute("CALL check_alerts()")


def compress_history() -> int:
    """Compress every chunk older than a day now, instead of waiting for the policy."""
    with db.conn(autocommit=True) as c:
        rows = c.execute("""SELECT compress_chunk(ch, if_not_compressed => TRUE) AS ch
                            FROM show_chunks('readings', older_than => now() - INTERVAL '1 day') ch""").fetchall()
    return len(rows)


def apply_schema() -> None:
    with db.conn(autocommit=True) as c:
        c.execute((config.ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))
        c.execute((config.ROOT / "sql" / "functions.sql").read_text(encoding="utf-8"))
    add_policy()
