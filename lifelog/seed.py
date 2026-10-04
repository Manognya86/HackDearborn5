"""Seed data: demo patient, neighbors, 14 days of readings. The medicines and their label-quoted rules live in
lifelog/medicines.py. Upload a label or use "add by name" for any other medicine."""
import json
import math
import random
from datetime import date, datetime, timedelta, timezone

from . import config, db, engine, weather

rng = random.Random(7)
# every medicine's rules, quoted from its FDA label (checked word-for-word by tests/test_labels.py)
from .medicines import PRODUCTS, ROOM_TEMP, SHORT, SOURCES  # noqa: E402

HISTORY_DAYS = 14

# Published heat-tolerance evidence that would replace LIFELOG's 8-hour assumption. Proposed only: nothing changes
# until a pharmacist approves it in the Review view. Derivations use the same doubling-per-10°C rule as the engine.
EVIDENCE = {
    "epi": {
        "proposed": 10752.0,
        "citation": "Grant TA et al., Am J Emerg Med 1994;12(3):319-22 (PMID 8179739); Parish HG et al., "
                    "Ann Allergy Asthma Immunol 2016;117(1):79-87 (PMID 27221065)",
        "url": "https://pubmed.ncbi.nlm.nih.gov/8179739/",
        "finding": "Epinephrine 1:1,000 (the auto-injector concentration) showed no statistically significant loss after "
                   "12 weeks of cycled heating to 70°C for 8 hours a day; a 2016 systematic review found heat degradation "
                   "only with prolonged exposure and none with freezing.",
        "derivation": "12 weeks × 8 h/day = 672 h at 70°C with no significant loss. At 70°C the engine burns 2^((70−30)/10) = 16× "
                      "the rate just above 30°C, so 672 h at 70°C is equivalent to 672 × 16 = 10,752 h just above the label limit. "
                      "Treated as a lower bound (no loss was seen, so the true tolerance is at least this).",
    },
    "insulin": {
        "proposed": 303.0,
        "citation": "Vimalavathini R, Gitanjali B, Indian J Pharmacol 2009, as summarised in a 2023 review of insulin "
                    "storage at high temperatures (PMC10627263)",
        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC10627263/",
        "finding": "Short-acting human insulin lost 18% of potency after 28 days at 37°C (14% at 32°C). The review concludes "
                   "unopened insulin can be kept at up to 37°C for at most two months without clinically relevant loss.",
        "derivation": "18% loss in 672 h at 37°C. LIFELOG treats a 5% potency loss as the end of the budget: 672 × 5/18 = 187 h "
                      "at 37°C, which is 187 × 2^(7/10) = 303 h just above 30°C. Human-insulin data applied to insulin glargine: "
                      "the pharmacist must judge whether that extrapolation is acceptable.",
    },
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
    t_start = t_end - timedelta(days=HISTORY_DAYS)
    with db.conn() as c:
        c.execute("""TRUNCATE alerts, readings, ingest_log, items, products, users, refuges, outages, pattern_library,
                     deliveries, weather_hourly, zips RESTART IDENTITY CASCADE""")
        c.execute("TRUNCATE readings_5m")  # materialized rows survive a raw-table truncate
        c.execute("TRUNCATE readings_1d")
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
        # accounts: "You" is the demo patient; a demo pharmacist reviews rules and evidence (no password: demo button)
        c.execute("UPDATE users SET email = 'demo.patient@lifelog.example' WHERE id = %s", (uid[0],))
        c.execute("""INSERT INTO users (name, is_me, can_host, lat, lon, geom, email, role, credentials)
                     VALUES ('Demo Pharmacist', FALSE, FALSE, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                             'demo.pharmacist@lifelog.example', 'pharmacist', 'PharmD (demo account)')""",
                  (REFUGES[0][2], REFUGES[0][3], REFUGES[0][3], REFUGES[0][2]))
        for key, ev in EVIDENCE.items():
            c.execute("""INSERT INTO evidence (product_id, field, current_value, proposed_value, citation, url, finding, derivation)
                         VALUES (%s, 'above_limit_budget_hours', %s, %s, %s, %s, %s, %s)""",
                      (pid[key], PRODUCTS[key]["above_limit_budget_hours"], ev["proposed"], ev["citation"], ev["url"],
                       ev["finding"], ev["derivation"]))
        for r in REFUGES:
            c.execute("""INSERT INTO refuges (name, kind, lat, lon, geom)
                         VALUES (%s,%s,%s,%s, ST_SetSRID(ST_MakePoint(%s,%s),4326))""", (*r, r[3], r[2]))

        items = []  # (user, product, nickname, profile)
        items += [(uid[0], pid["glp1"], "My Mounjaro pen", "fridge"),
                  (uid[0], pid["insulin"], "Insulin pen: Lantus (spare, kitchen fridge)", "fridge_gap"),
                  (uid[0], pid["epi"], "EpiPen in backpack", "room"),
                  (uid[0], pid["victoza"], "Victoza pen (in use)", "fridge"),
                  (uid[0], pid["humira"], "Humira Pen (fridge door)", "fridge_door"),
                  (uid[0], pid["gvoke"], "Gvoke HypoPen in handbag", "carry")]
        keys = sorted(PRODUCTS)
        for n, u in enumerate(uid[1:]):   # every medicine appears in the neighborhood at least once
            for j in range(2):
                key = keys[(2 * n + j) % len(keys)]
                items.append((u, pid[key], SHORT[key], "room" if key in ROOM_TEMP else "fridge"))
        iid = []
        MINE = 6
        today = t_end.date()
        for k, (u, p, nick, prof) in enumerate(items):
            in_use = next(m["in_use_days"] for key, m in PRODUCTS.items() if pid[key] == p)
            if k < MINE:  # yours: the Lantus is an unopened spare (fridge); the Victoza pen is in use, 3 days left
                opened = t_end - timedelta(days=27) if k == 3 else None
                expires = today + timedelta(days=(120, 180, 270, 150, 200, 330)[k])
                lot = ("D7K2291", "MP4410A", "1FM882", "NV30215", "1162845", "XG2209")[k]
            else:      # neighbors: a realistic mix, including one past its expiry and one past its in-use period
                opened = t_end - timedelta(days=rng.randint(3, 34)) if in_use else None
                expires = today + timedelta(days=-3 if k == 5 else rng.randint(20, 400))
                lot = None
            iid.append((c.execute("""INSERT INTO items (user_id, product_id, nickname, started_at, opened_at, expires_on, lot)
                                     VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                  (u, p, nick, t_start, opened, expires, lot)).fetchone()["id"], prof))

        # real Dearborn outdoor temperature for the history window: the EpiPen you carry follows it in daytime
        from . import forecast_ml
        wx = forecast_ml.outdoor(config.HOME_LAT, config.HOME_LON)
        w_mean = sum(wx.values()) / len(wx) if wx else 0.0
        with c.cursor() as cur, cur.copy("COPY readings (ts, item_id, temp_c, source) FROM STDIN") as cp:
            n = 0
            for k, (item_id, prof) in enumerate(iid):
                step = timedelta(minutes=1 if k < MINE else 2)   # your sensors every minute, neighbors every 2
                for t in engine.daterange(t_start, t_end, step):
                    if prof == "fridge_gap" and t > t_end - timedelta(hours=6):
                        continue  # sensor offline: these readings arrive later via /api/demo/late_upload
                    v = room(t) if prof in ("room", "carry") else fridge(t)
                    if prof == "fridge_door":   # door shelves run warmer and swing when the door opens
                        v += 1.6 + (0.9 if rng.random() < 0.03 else 0.0)
                    if k in (2, 5) and wx and 8 <= t.astimezone(timezone(timedelta(hours=-4))).hour < 20:
                        w = wx.get(t.replace(minute=0, second=0, microsecond=0))
                        if w is not None:   # carried around outside: about 0.35 C per C of outdoor anomaly
                            v += 0.35 * (w - w_mean)
                    ago = (t_end - t).total_seconds() / 86400
                    if k == 0 and 9.0 < ago < 9.125:      # Mounjaro: 3 h in a bag at work, ~27C
                        v = 27 + rng.gauss(0, 0.4)
                    if k == 1 and 5.0 < ago < 5.25:       # Lantus: fridge door left ajar overnight, ~12C
                        v = 12 + rng.gauss(0, 0.6)
                    cp.write_row((t, item_id, round(v, 2), "sensor"))
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
        db.call_refresh(c,"CALL refresh_continuous_aggregate('readings_5m', NULL, time_bucket('5 minutes', now()))")
        db.call_refresh(c,"CALL refresh_continuous_aggregate('readings_1d', NULL, time_bucket('1 day', now()))")
    compressed = compress_history()
    doses = seed_doses(iid[3][0], iid[0][0], t_end)
    check_alerts()
    return {"readings": n, "doses": doses, "items": len(iid), "users": len(uid), "compressed_chunks": compressed, **porch}


def seed_doses(victoza: int, mounjaro: int, t_end: datetime) -> int:
    """Dose history so 'was last Tuesday's dose still good?' has an answer: Victoza daily at 8 am since it was
    opened, Mounjaro weekly. Each dose records the status computed from Tiger Data at that moment."""
    from . import services
    detroit = timezone(timedelta(hours=-4))
    n = 0
    for back in range(HISTORY_DAYS - 1, 0, -1):
        day = (t_end - timedelta(days=back)).astimezone(detroit).replace(hour=8, minute=rng.randint(0, 40), second=0, microsecond=0)
        services.log_dose(victoza, day, None)
        n += 1
        if back % 7 == 2:
            services.log_dose(mounjaro, day.replace(hour=19), "Weekly dose")
            n += 1
    return n


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
                           JOIN outages o ON o.id = %s AND ST_Contains(o.area, u.geom)
                           WHERE NOT u.synthetic""", (out["id"],))
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
        c.execute("""SELECT add_continuous_aggregate_policy('readings_1d', start_offset => INTERVAL '3 days',
                        end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '15 minutes', if_not_exists => TRUE)""")
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
        c.execute((sql / "security.sql").read_text(encoding="utf-8"))
        if stale:
            db.call_refresh(c,"CALL refresh_continuous_aggregate('readings_5m', NULL, time_bucket('5 minutes', now()))")
    add_policy()


def sync_reference_data() -> dict:
    """Non-destructive top-up for an existing database (scripts/migrate.py): add medicines that are missing, refresh
    the label quotes of existing ones, add the evidence proposals and the demo accounts. Readings, items, alerts and
    anything a pharmacist approved are left as they are."""
    added, updated = [], []
    with db.system():
        for key, m in PRODUCTS.items():
            row = db.one("SELECT id, model FROM products WHERE name = %s ORDER BY id LIMIT 1", (m["product_name"],))
            if not row:
                db.execute("""INSERT INTO products (name, model, source, source_label, source_url) VALUES (%s,%s,'fda',%s,%s)""",
                           (m["product_name"], json.dumps(m), SOURCES[key]["label"], SOURCES[key]["url"]))
                added.append(key)
                continue
            new = dict(m)
            if not row["model"].get("above_limit_is_assumption", True):   # keep a pharmacist-approved tolerance
                for f in ("above_limit_budget_hours", "above_limit_is_assumption", "above_limit_quote"):
                    new[f] = row["model"][f]
            if new != row["model"]:
                db.execute("UPDATE products SET model = %s, source_label = %s, source_url = %s WHERE id = %s",
                           (json.dumps(new), SOURCES[key]["label"], SOURCES[key]["url"], row["id"]))
                updated.append(key)
        for key, ev in EVIDENCE.items():
            pid = db.one("SELECT id FROM products WHERE name = %s ORDER BY id LIMIT 1", (PRODUCTS[key]["product_name"],))["id"]
            if not db.one("SELECT 1 AS x FROM evidence WHERE product_id = %s", (pid,)):
                db.execute("""INSERT INTO evidence (product_id, field, current_value, proposed_value, citation, url, finding, derivation)
                              VALUES (%s, 'above_limit_budget_hours', %s, %s, %s, %s, %s, %s)""",
                           (pid, PRODUCTS[key]["above_limit_budget_hours"], ev["proposed"], ev["citation"], ev["url"],
                            ev["finding"], ev["derivation"]))
        db.execute("""UPDATE users SET email = 'demo.patient@lifelog.example'
                      WHERE id = (SELECT id FROM users WHERE is_me ORDER BY id LIMIT 1) AND email IS NULL
                        AND NOT EXISTS (SELECT 1 FROM users WHERE lower(email) = 'demo.patient@lifelog.example')""")
        if not db.one("SELECT 1 AS x FROM users WHERE role = 'pharmacist'"):
            db.execute("""INSERT INTO users (name, is_me, can_host, lat, lon, geom, email, role, credentials)
                          VALUES ('Demo Pharmacist', FALSE, FALSE, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                                  'demo.pharmacist@lifelog.example', 'pharmacist', 'PharmD (demo account)')""",
                       (REFUGES[0][2], REFUGES[0][3], REFUGES[0][3], REFUGES[0][2]))
    return {"medicines_added": added, "label_quotes_updated": updated}
