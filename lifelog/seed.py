"""Seed data. Product storage rules are taken from each medicine's FDA prescribing information
(Section 16, "How Supplied / Storage and Handling"), retrieved through openFDA's drug-label API.
Quotes are verbatim; where a label is silent LIFELOG says so and flags its own assumption.
Upload a label or use "add by name" for any other medicine."""
import json
import math
import random
from datetime import date, datetime, timedelta, timezone

from . import config, db, engine, weather

rng = random.Random(7)
ASSUME = "Not stated on label (LIFELOG conservative default: 8 h above the highest labeled limit, doubling per 10°C)"


def _band(label, lo, hi, hours, quote):
    return {"label": label, "min_c": lo, "max_c": hi, "budget_hours": hours, "quote": quote}


def _model(name, form, tmin, tmax, tq, bands, *, freeze_quote, cold_ok=True, freeze=True, above=8.0,
           above_quote=ASSUME, above_assumed=True, visual=(), discard=(), notes=""):
    return {
        "product_name": name, "form": form,
        "target_min_c": tmin, "target_max_c": tmax, "target_quote": tq,
        "freeze_discard": freeze, "freeze_c": 0.0, "freeze_quote": freeze_quote,
        "cold_ok": cold_ok, "bands": bands,
        "above_limit_budget_hours": above, "above_limit_is_assumption": above_assumed, "above_limit_quote": above_quote,
        "visual_checks": list(visual), "discard_rules": list(discard), "notes": notes,
    }


def _src(brand, set_id, effective):
    return {"label": f"{brand} prescribing information, FDA label version {effective[:4]}-{effective[4:6]}-{effective[6:]}",
            "url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"}


ROOM = "Room-temp allowance"
PRODUCTS = {
    "insulin": _model(
        "Lantus SoloStar pen (insulin glargine)", "prefilled pen", 2, 8,
        "Store unused LANTUS in a refrigerator between 36°F and 46°F (2°C and 8°C).",
        [_band(ROOM, 8, 30, 28 * 24, "Storage table, 3 mL SoloStar prefilled pen: Room Temperature (up to 86°F [30°C]): 28 days.")],
        freeze_quote="Do not freeze. Discard LANTUS if it has been frozen.",
        discard=["Protect LANTUS from direct heat and light.", "In-use pens: room temperature only (do not refrigerate), 28 days."]),
    "novolog": _model(
        "NovoLog FlexPen (insulin aspart)", "prefilled pen", 2, 8,
        "Store unused NOVOLOG in a refrigerator between 2°C to 8°C (36°F to 46°F).",
        [_band(ROOM, 8, 30, 28 * 24, "Storage table, 3 mL FlexPen: Room Temperature (up to 30°C [86°F]): 28 days.")],
        freeze_quote="Do not freeze NOVOLOG and do not use NOVOLOG if it has been frozen.",
        discard=["Do not expose NOVOLOG to excessive heat or light.",
                 "In an insulin pump: change it after exposure to temperatures that exceed 37°C (98.6°F)."]),
    "tresiba": _model(
        "Tresiba FlexTouch (insulin degludec)", "prefilled pen", 2, 8,
        "Store unused TRESIBA in a refrigerator (36°F to 46°F [2°C to 8°C]).",
        [_band(ROOM, 8, 30, 56 * 24, "Storage table, FlexTouch: Room Temperature (up to 86°F [30°C]): 56 days (8 weeks).")],
        freeze_quote="Do not freeze. Do not use TRESIBA if it has been frozen.",
        discard=["Do not store in the freezer or directly adjacent to the refrigerator cooling element."]),
    "glp1": _model(
        "Mounjaro single-dose pen (tirzepatide)", "single-dose pen", 2, 8,
        "Store MOUNJARO single-dose pen and single-dose vial in a refrigerator at 2°C to 8°C (36°F to 46°F).",
        [_band(ROOM, 8, 30, 21 * 24, "If needed, each single-dose pen or single-dose vial can be stored unrefrigerated at "
                                    "temperatures not to exceed 30°C (86°F) for up to a total of 21 days.")],
        freeze_quote="Do not freeze MOUNJARO. Do not use MOUNJARO if frozen.",
        discard=["Discard the single-dose pen or single-dose vial after a total of 21 days at room temperature.",
                 "Protect MOUNJARO from heat and light."]),
    "trulicity": _model(
        "Trulicity pen (dulaglutide)", "single-dose pen", 2, 8,
        "Store TRULICITY in the refrigerator at 36°F to 46°F (2°C to 8°C).",
        [_band(ROOM, 8, 30, 14 * 24, "If needed, each single-dose pen can be kept at room temperature, not to exceed "
                                    "86°F (30°C) for a total of 14 days.")],
        freeze_quote="Do not freeze TRULICITY. Do not use TRULICITY if it has been frozen.",
        discard=["Protect TRULICITY from light."]),
    "victoza": _model(
        "Victoza pen, in use (liraglutide)", "multi-dose pen", 2, 8,
        "Prior to first use, VICTOZA should be stored in a refrigerator between 36°F to 46°F (2°C to 8°C).",
        [_band(ROOM, 8, 30, 30 * 24, "After first use of the VICTOZA pen, the pen can be stored for 30 days at controlled "
                                    "room temperature 59°F to 86°F (15°C to 30°C) or in a refrigerator 36°F to 46°F (2°C to 8°C).")],
        freeze_quote="Do not freeze VICTOZA and do not use VICTOZA if it has been frozen.",
        discard=["Protect VICTOZA from excessive heat and sunlight.",
                 "The 30 days after first use apply even in the fridge; LIFELOG's budget only tracks heat."],
        notes="The label gives 15–30°C; LIFELOG applies the same allowance to 8–15°C."),
    "biologic": _model(
        "Enbrel SureClick (etanercept)", "autoinjector", 2, 8,
        "Enbrel should be refrigerated at 36°F to 46°F (2°C to 8°C) in the original carton to protect from light or physical damage.",
        [_band(ROOM, 8, 25, 30 * 24, "storage of individual single-dose prefilled syringes, SureClick autoinjectors, single-dose vials, "
                                    "or Enbrel Mini cartridges at room temperature at 68°F to 77°F (20°C to 25°C) for a maximum "
                                    "single period of 30 days is permissible, with protection from light and sources of heat.")],
        freeze_quote="DO NOT FREEZE.", above_quote="Do not store Enbrel in extreme heat or cold. (No time limit given: "
                                                    "LIFELOG assumes 8 h above 25°C, doubling per 10°C.)",
        discard=["Once stored at room temperature, it should not be placed back into the refrigerator.",
                 "If not used within 30 days at room temperature, discard it.", "DO NOT SHAKE."],
        notes="The label gives 20–25°C; LIFELOG applies the same allowance to 8–20°C."),
    "epi": _model(
        "EpiPen auto-injector (epinephrine)", "auto-injector", 20, 25,
        "Store at 20°C to 25°C (68°F to 77°F); excursions permitted to 15°C to 30°C (59°F to 86°F) [See USP Controlled Room Temperature].",
        [_band("Cool excursion", 15, 20, 90 * 24, "excursions permitted to 15°C to 30°C (59°F to 86°F)"),
         _band("Warm excursion", 25, 30, 90 * 24, "excursions permitted to 15°C to 30°C (59°F to 86°F)")],
        freeze_quote="Not stated on label (it says: Do not refrigerate.)", freeze=False, cold_ok=False,
        visual=["Before using, check to make sure the solution in the auto-injector is clear and colorless.",
                "Replace the auto-injector if the solution is discolored (pinkish or brown color), cloudy, or contains particles."],
        discard=["Protect from light: store in the carrier tube provided.", "Do not refrigerate."],
        notes="The label permits excursions without a time limit; LIFELOG assumes 90 days of cumulative excursion."),
    "xalatan": _model(
        "Xalatan eye drops, opened (latanoprost)", "eye drop bottle", 2, 8,
        "Store unopened bottle(s) under refrigeration at 2°C to 8°C (36°F to 46°F).",
        [_band(ROOM, 8, 25, 6 * 7 * 24, "Once a bottle is opened for use, it may be stored at room temperature up to 25°C (77°F) for 6 weeks."),
         _band("Shipping allowance", 25, 40, 8 * 24, "During shipment to the patient, the bottle may be maintained at temperatures "
                                                    "up to 40°C (104°F) for a period not exceeding 8 days.")],
        freeze_quote="Not stated on label", freeze=False, discard=["Protect from light."]),
}
SOURCES = {
    "insulin": _src("LANTUS", "d5e07a0c-7e14-4756-9152-9fea485d654a", "20250602"),
    "novolog": _src("NOVOLOG", "3a1e73a2-3009-40d0-876c-b4cb2be56fc5", "20230228"),
    "tresiba": _src("TRESIBA", "456c5e87-3dfd-46fa-8ac0-c6128d4c97c6", "20220701"),
    "glp1": _src("MOUNJARO", "d2d7da5d-ad07-4228-955f-cf7e355c8cc0", "20260827"),
    "trulicity": _src("TRULICITY", "463050bd-2b1c-40f5-b3c3-0a04bb433309", "20260616"),
    "victoza": _src("VICTOZA", "5a9ef4ea-c76a-4d34-a604-27c5b505f5a4", "20251014"),
    "biologic": _src("ENBREL", "a002b40c-097d-47a5-957f-7a7b1807af7f", "20260511"),
    "epi": _src("EpiPen", "311af6d9-20c0-4b87-b236-4185bfa49988", "20230829"),
    "xalatan": _src("XALATAN", "a98595b3-9f47-48e0-b18d-550a2095f264", "20230515"),
}
SHORT = {"insulin": "Lantus pen", "novolog": "NovoLog FlexPen", "tresiba": "Tresiba pen", "glp1": "Mounjaro pen",
         "trulicity": "Trulicity pen", "victoza": "Victoza pen", "biologic": "Enbrel SureClick", "epi": "EpiPen",
         "xalatan": "Xalatan eye drops"}

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
            pid[key] = c.execute("""INSERT INTO products (name, model, source, source_label, source_url)
                                    VALUES (%s,%s,'fda',%s,%s) RETURNING id""",
                                 (m["product_name"], json.dumps(m), SOURCES[key]["label"], SOURCES[key]["url"])).fetchone()["id"]

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
        items += [(uid[0], pid["glp1"], "My Mounjaro pen", "fridge"),
                  (uid[0], pid["insulin"], "Insulin pen: Lantus (kitchen fridge)", "fridge_gap"),
                  (uid[0], pid["epi"], "EpiPen in backpack", "room")]
        for u in uid[1:]:
            for _ in range(rng.choice([1, 1, 2])):
                key = rng.choice(["insulin", "novolog", "tresiba", "glp1", "trulicity", "victoza", "biologic", "epi", "xalatan"])
                items.append((u, pid[key], SHORT[key], "room" if key == "epi" else "fridge"))
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
        # Columnstore (hypercore): the current API since TimescaleDB 2.18; add_compression_policy is deprecated.
        enabled = c.execute("""SELECT compression_enabled FROM timescaledb_information.hypertables
                               WHERE hypertable_name = 'readings'""").fetchone()
        try:
            if enabled and not enabled["compression_enabled"]:
                c.execute("""ALTER TABLE readings SET (timescaledb.enable_columnstore = true,
                                timescaledb.segmentby = 'item_id', timescaledb.orderby = 'ts DESC')""")
            c.execute("CALL add_columnstore_policy('readings', after => INTERVAL '2 days', if_not_exists => true)")
        except Exception:  # TimescaleDB < 2.18
            if enabled and not enabled["compression_enabled"]:
                c.execute("""ALTER TABLE readings SET (timescaledb.compress,
                                timescaledb.compress_segmentby = 'item_id', timescaledb.compress_orderby = 'ts DESC')""")
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
        chunks = [r["ch"] for r in c.execute("""SELECT ch::text AS ch FROM show_chunks('readings',
                                                 older_than => now() - INTERVAL '1 day') ch""").fetchall()]
        for ch in chunks:
            try:
                c.execute("CALL convert_to_columnstore(%s::regclass, if_not_columnstore => true)", (ch,))
            except Exception:  # TimescaleDB < 2.18
                c.execute("SELECT compress_chunk(%s::regclass, if_not_compressed => TRUE)", (ch,))
    return len(chunks)


def apply_schema() -> None:
    sql = config.ROOT / "sql"
    with db.conn(autocommit=True) as c:
        # migrate: older readings_5m without the per-reading burn rate is rebuilt
        stale = c.execute("""SELECT 1 FROM information_schema.views v WHERE v.table_name = 'readings_5m'
                             AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                             WHERE table_name = 'readings_5m' AND column_name = 'avg_rate')""").fetchone()
        if stale:
            c.execute("DROP MATERIALIZED VIEW readings_5m CASCADE")
        c.execute((sql / "schema.sql").read_text(encoding="utf-8"))
        c.execute((sql / "functions.sql").read_text(encoding="utf-8"))
        c.execute((sql / "aggregates.sql").read_text(encoding="utf-8"))
        c.execute((sql / "realtime.sql").read_text(encoding="utf-8"))
        if stale:
            c.execute("CALL refresh_continuous_aggregate('readings_5m', NULL, time_bucket('5 minutes', now()))")
    add_policy()
