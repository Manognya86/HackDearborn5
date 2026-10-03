# LIFELOG Home

**We don't tell you whether your medicine is cold right now. We tell you how much safe life it has left, and what to do about it.**

Temperature-sensitive medicines (insulin, GLP-1 pens, EpiPens, biologics) get abused *after* the pharmacy:
hot cars, power outages, mailboxes in August. LIFELOG turns each medicine's own label into a life-budget
model (Gemini), tracks every exposure as time-series in Tiger Data, and tells you what's left.

## What's in it

| Feature | Tiger Data | Gemini |
|---|---|---|
| Label → stability model | `products.model` JSONB | Reads photo/PDF, structured output with verbatim quotes |
| Life budget + "what consumed it" | `readings` hypertable → `readings_5m` continuous aggregate → `item_timeline()` / `item_episodes()` SQL (gaps-and-islands) | |
| Late-arriving sensor data | Watermark detection + targeted `refresh_continuous_aggregate`; raw-vs-aggregate comparison | |
| Similar exposure histories | `pattern_library` + pgvector cosine search | |
| "Is it safe?" | | Grounded verdict citing label quotes |
| Voice report | Writes `source='voice'` readings | Audio → exposure events |
| Photo check | | Vision vs label's visual warnings |
| Outage rescue | PostGIS: who's inside the outage polygon, nearest refuge with power | Dispatch plan + SMS (Maps grounding when available) |
| Refill letter | Exposure timeline as evidence | Writes the letter |
| Trip pre-check | | Itinerary → legs; advice (weather from Open-Meteo) |
| Porch Heat Index | `weather_hourly` hypertable × `deliveries` | Pharmacy brief |
| Alerts | `check_alerts` procedure scheduled with `add_job` every minute; opens, updates and resolves alerts | Explains them in Ask LIFELOG |
| Exposure statistics | Mean Kinetic Temperature in SQL; time-weighted average via Toolkit `time_weight()` | |
| Storage | Compression policy (segment by item, 11× on old chunks), retention policies | |
| Under the hood page | Hypertable sizes, compression ratio, job runs, aggregate vs raw timing | |
| Live sensors | Streams a reading per medicine every 5 s into the real-time aggregate | |
| Ask LIFELOG | Tools query Tiger | Function calling over 5 tools, shows which ones it called |

The life-budget math lives in **both** SQL (`sql/functions.sql`) and Python (`lifelog/engine.py`);
`tests/test_parity.py` proves they agree.

### Model (and its honest assumptions)
- Inside the labeled storage range: no budget used.
- Inside a labeled allowance (e.g. "room temp up to 30°C for 28 days"): `1 / budget_hours` per hour.
- Above the highest labeled limit: `2^((T − limit)/10) / above_limit_budget_hours` (label value if given, else **8 h, flagged as an assumption**).
- At or below freezing when the label says "do not freeze": budget gone.
- Outage projection: a closed fridge warms toward indoor temperature with a 6 h time constant.
- Mailbox temperature = air + 12°C (10am–6pm), +2°C otherwise.
- Mean Kinetic Temperature uses ΔH = 83.144 kJ/mol (USP <1079>) over 5-minute buckets.
- The live simulator speeds up outage warming so the demo moves within minutes.

Demo product models are marked "(demo)" and are **not** copied from real labels. Upload a real label to get real quotes.
This is a decision-support prototype, not medical advice.

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env        # fill GEMINI_API_KEY and DATABASE_URL
.venv/Scripts/python scripts/check_setup.py
.venv/Scripts/python scripts/setup_db.py
.venv/Scripts/uvicorn lifelog.app:app --port 8000
```
Open http://localhost:8000.

### Database options
- **Tiger Cloud (for judging):** `tiger auth login`, `tiger service create --name lifelog`, then `tiger db uri --with-password` → `DATABASE_URL`. Needs the `timescaledb`, `vector` and `postgis` extensions (`check_setup.py` lists what's available).
- **Local:** `docker run -d --name lifelog-db -p 55432:5432 -e POSTGRES_PASSWORD=lifelog timescale/timescaledb-ha:pg17`, then `DATABASE_URL=postgresql://postgres:lifelog@localhost:55432/postgres`.

### Tests
```bash
.venv/Scripts/python -m pytest -q
```

## 5-minute demo script
1. **Demo controls → Reset.** My medicines: three items at 100%. The insulin shows *sensor silent*.
2. **Add medicine:** upload a real label photo. Gemini builds the model with quotes. Track it.
3. **Demo controls → EpiPen: hot car.** Open the EpiPen: budget drops to ~45%, "Above labeled limit, peak 48°C". The what-if table shows that moving it indoors saves it. Click **Is it safe to use?**
4. **Late data (the Tiger moment):** *Insulin: late upload (no fix)*. Dashboard 100% vs raw truth ~91%, because the late rows sit behind the continuous-aggregate watermark. Click **Repair late windows**: both agree. (Normal path: `/api/ingest` detects late rows and refreshes just those buckets.)
5. **Summer storm outage → Outage rescue.** Ranked list: your hot-car EpiPen fails first (~3 h) vs 12 h to restore. Click **Gemini dispatch plan**.
6. **Refill letter** on the insulin. Then **Porch heat** → **Gemini pharmacy brief**.
7. **Alerts** tab: the hot-car and outage alerts were opened by a job running inside Tiger; the sensor-silent alert resolved itself when the late data arrived.
8. **Ask LIFELOG:** "Which of my medicines is in the worst shape, and why?" Gemini calls the tools and shows which.
9. **Under the hood:** compression ratio, scheduled jobs, aggregate vs raw timing. Optionally **Start live sensors** and watch the dashboard move.

## API
`GET /api/items`, `GET /api/items/{id}`, `GET /api/items/{id}/compare`, `POST /api/items/{id}/advice|visual|letter|trip|repair`,
`POST /api/products/extract`, `POST /api/products`, `POST /api/ingest`, `POST /api/voice`,
`GET /api/rescue`, `POST /api/rescue/plan`, `GET /api/porch`, `POST /api/porch/brief`, `GET /api/alerts`, `POST /api/alerts/check`,
`POST /api/alerts/{id}/ack`, `GET /api/tiger`, `POST /api/sim/{start|stop|status}`, `POST /api/ask`,
`POST /api/demo/{reset|hot_car|late_upload|storm|clear_storm}`.

`POST /api/ingest` body:
```json
{"item_id": 2, "source": "sensor", "auto_refresh": true,
 "readings": [{"ts": "2026-10-03T14:05:00Z", "temp_c": 9.4}]}
```
