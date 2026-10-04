# LIFELOG Home

**We don't tell you whether your medicine is cold right now. We tell you how much safe life it has left, and what to do about it.**

Temperature-sensitive medicines (insulin, GLP-1 pens, EpiPens, biologics) get abused *after* the pharmacy:
hot cars, power outages, mailboxes in August. LIFELOG turns each medicine's own label into a life-budget
model (Gemini), tracks every exposure as time-series in Tiger Data, and tells you what's left.

## Why Tiger
Home sensors don't upload on time. A fridge sensor loses Wi-Fi during the very outage that's warming the insulin,
then dumps six hours of readings at once. A dashboard built on a pre-computed rollup would keep saying "100%, safe"
while the raw data says otherwise. LIFELOG's budget is served from a **real-time continuous aggregate**; `/api/ingest`
compares each batch with the aggregate's **materialization watermark**, and when rows land behind it, refreshes
**only the affected 5-minute and daily windows**. The demo shows it: the late-data screen draws the watermark, the late
rows hidden behind it, and the dashboard-vs-raw gap closing after a targeted refresh. Around that, Tiger runs the
rest inside the database: a hierarchical daily rollup for the 30-day calendar, columnstore compression and retention
policies, a scheduled `check_alerts` job, `LISTEN/NOTIFY` for live updates, plus PostGIS (outage rescue) and pgvector
(similar exposure histories) in the same Postgres.

## Tiger Data by the numbers
`scripts/setup_db.py --scale 50` then `scripts/benchmark.py` on a Tiger Cloud service (4 CPU / 16 GB, us-east-2),
TimescaleDB 2.30.2, PostgreSQL 18.6. Timings are the median of 3 runs after a warm-up, measured from a laptop in
Dearborn, so they include the network round trip.

| Metric | Value |
|---|---|
| Sensor readings (1-minute) | 6,782,041 across 76 medicines, 89 days |
| `readings` hypertable | 91 daily chunks, 144.6 MiB on disk |
| Columnstore (compressed chunks) | 748.7 MiB → 55.9 MiB (**13.4×** smaller) |
| `readings_5m` / `readings_1d` rollups | 1,400,786 / 4,940 rows |

| Query | Continuous aggregate | Raw readings | Speedup |
|---|---:|---:|---:|
| Life budget, every medicine | 4.88 s (1,400,786 rows) | 16.93 s (6,782,041 rows) | **3.5×** |
| 30-day calendar, every medicine | 82.1 ms (1,890 rows) | 16.30 s (2,455,507 rows) | **198.5×** |

The life-budget query is only 3.5× faster because both paths still run the per-bucket budget math (zones, running
sum) over 90 days of 5-minute buckets; the calendar reads the daily rollup directly. Generating the 6.5M rows took 38 s
server-side, refreshing both rollups 38 s, columnstore conversion 7 s. Re-run `scripts/benchmark.py` to refresh these.

## What's in it

| Feature | Tiger Data | Gemini |
|---|---|---|
| Label → stability model | `products.model` JSONB | Reads photo/PDF, structured output with verbatim quotes |
| Life budget + "what consumed it" | `readings` hypertable → `readings_5m` continuous aggregate → `item_timeline()` / `item_episodes()` SQL (gaps-and-islands) | |
| Late-arriving sensor data | Watermark detection + targeted `refresh_continuous_aggregate`; raw-vs-aggregate comparison; a strip showing the watermark and where late rows landed | |
| Live trend forecast | `item_trend()`: least-squares slope and R² over the last 30 min of the real-time `readings_5m` aggregate → "↑ 6.4°C/h · leaves its range in ~25 min", "at this trend it lasts …", 2-hour forecast line on the chart | |
| Early-warning alerts | `check_alerts` also opens `warming_trend` (still in range, trend reaches the label max within 1 h) and `freeze_risk` (cooling toward 0°C within 1 h) | |
| Similar exposure histories | `pattern_library` + pgvector cosine search | |
| "Is it safe?" | | Grounded verdict citing label quotes |
| Voice report | Writes `source='voice'` readings | Audio → exposure events |
| Photo check | | Vision vs label's visual warnings |
| Outage rescue | PostGIS: who's inside the outage polygon, nearest refuge with power; each fridge's warming time constant measured from its live trend (6 h assumed until there's enough data); the ranking refreshes as readings arrive | Dispatch plan + SMS (Maps grounding when available) |
| Refill letter | Exposure timeline as evidence | Writes the letter |
| Trip pre-check | | Itinerary → legs; advice (weather from Open-Meteo) |
| Porch Heat Index | `weather_hourly` hypertable × `deliveries` | Pharmacy brief |
| Alerts | `check_alerts` procedure scheduled with `add_job` every minute; opens, updates and resolves alerts | Explains them in Ask LIFELOG |
| Exposure statistics | Mean Kinetic Temperature in SQL; time-weighted average via Toolkit `time_weight()`; budget used in the last hour | |
| Storage | Columnstore policy (segment by item, order by time; 13.4× on compressed chunks at scale), 400-day retention policies | |
| Under the hood page | Hypertable sizes, compression ratio, job runs; rollup vs raw timings (median of 3) for every budget and the 30-day calendar | |
| Live sensors | Streams a reading per medicine every 5 s into the real-time aggregate | |
| Ask LIFELOG | 6 tools query Tiger (medicines most urgent first with status, dates, live trend; details with forecast and what-if; 30-day calendar from `readings_1d`; exposure simulation; outage; alerts), all in explicit units | Function calling; shows the tools it called and the model that answered. If every Gemini model is unavailable it answers straight from the Tiger data, labeled "without AI" |
| Real time | `LISTEN/NOTIFY` triggers on `readings` and `alerts` → Server-Sent Events; alerts re-checked within ~2 s of new data; medicines, forecast, outage ranking and the late-data comparison refresh on each event | |
| Accuracy report | Exact per-reading burn inside the continuous aggregate (joins `items`/`products`), cross-checked against raw SQL and an independent Python recomputation; sensitivity to the 8 h assumption | |
| Data quality | Ingest rejects impossible values and duplicates, flags implausible jumps, logs counts | |
| Precautions | Care checklist from the label + situation (too warm, outage, sensor silent) + 48 h heat/freeze forecast | |
| Exports | CSV exposure log per medicine; print-friendly page | |
| Opened / expiry dates | `items.opened_at`, `expires_on`, `lot`; alert rules for expired, in-use period over, use-by within 3 days | Label extraction includes the in-use period ("discard 28 days after opening") |
| 30-day exposure calendar | `readings_1d`: a hierarchical continuous aggregate built on `readings_5m`, with its own refresh policy | |
| FDA recall check | Lot numbers stored per item | openFDA drug enforcement reports, cached 6 h, matched by brand and lot |
| Caregiver share link | `shares` table; read-only `/s/<token>` page, revocable, QR code | |
| Notifications + offline | Alerts pushed over LISTEN/NOTIFY trigger a system notification | Service worker caches the app shell |

The life-budget math lives in **both** SQL (`sql/functions.sql`) and Python (`lifelog/engine.py`);
`tests/test_parity.py` proves they agree.

### Accuracy
- **Per-reading burn.** The continuous aggregate stores the mean *per-reading* burn rate for each 5-minute bucket.
  Computing the burn from the bucket's average temperature is wrong whenever readings straddle a label limit or spike
  (a 1-minute 25°C spike in a 3°C fridge averages 7.4°C, "in range", but still burns budget).
- **Three-way cross-check.** Live aggregate = SQL recomputation from raw readings = independent Python recomputation
  (shown per medicine under "How accurate is this?").
- **Uncertainty from gaps.** A silent sensor produces a range: budget if nothing happened, and a worst case at room
  temperature. A sensor silent for an hour or more turns the status to "Check on it" rather than "Safe".
- **Sensitivity.** The 8-hour above-limit default matters a lot for labels that don't state it (the hot-car EpiPen is
  0% at 4 h, 43% at 8 h, 81% at 24 h), so the UI shows this table instead of hiding it.

### Model (and its honest assumptions)
- Inside the labeled storage range: no budget used.
- Inside a labeled allowance (e.g. "room temp up to 30°C for 28 days"): `1 / budget_hours` per hour.
- Above the highest labeled limit: `2^((T − limit)/10) / above_limit_budget_hours` (label value if given, else **8 h, flagged as an assumption**).
- At or below freezing when the label says "do not freeze": budget gone.
- Outage projection: a closed fridge warms toward indoor temperature (Newton cooling). The time constant is
  **measured** from the fridge's live trend, τ = (indoor − T) / slope, clamped to 0.25–48 h; until there are 20 minutes
  of clean data it's an assumed 6 h. The rescue table says which one it used.
- Mailbox temperature = air + 12°C (10am–6pm), +2°C otherwise.
- Mean Kinetic Temperature uses ΔH = 83.144 kJ/mol (USP <1079>) over 5-minute buckets.
- The live simulator speeds up outage warming so the demo moves within minutes.

### Live forecast (recomputed on every reading)
1. **Trend from Tiger.** `item_trend(item, 30)` runs `regr_slope` / `regr_r2` over the item's last 30 minutes of
   5-minute buckets in the **real-time** continuous aggregate, so a reading that arrived a second ago is already in it.
2. **Is it a trend?** Only if there are ≥ 4 buckets, |slope| ≥ 0.5°C/h and R² ≥ 0.6, and the sensor isn't silent.
   Otherwise the forecast is "steady" and equals the constant-temperature answer exactly (tested).
3. **Temperature path.** Warming toward a known ambient (the outage's indoor temperature, or ~22°C room for a fridge
   medicine) follows Newton cooling with τ implied by the slope. With no known ambient (a warming EpiPen in a car),
   the trend is extrapolated for 1 hour and then held, rather than projected to impossible temperatures.
4. **Budget along the path.** The same `step_burn` as everything else, minute by minute for 72 h, then the final
   temperature's rate: minutes until it leaves its labeled range, minutes until freezing, hours until the budget runs out.
5. **Where it shows.** Medicine cards and detail (trend, "at this trend it lasts"), a dotted 2-hour forecast on the
   chart, the `warming_trend` / `freeze_risk` alerts (evaluated inside Tiger by `check_alerts`), and the outage ranking.
`tests/test_forecast.py` checks the closed-form crossing time, the steady case, freezing, the measured τ and a live
warming ramp through `/api/ingest` that must raise the early warning.

### Where every screen gets its data
| Screen | Endpoint | From Tiger | Live? |
|---|---|---|---|
| My medicines (list, detail, chart) | `/api/items`, `/api/items/{id}` | `readings_5m` (real-time) via `item_timeline()`, `item_episodes()`, `item_trend()`, `item_stats()`, Toolkit `time_weight()`, pgvector `pattern_library`, `alerts` | Yes: refreshes on every `readings` NOTIFY |
| 30-day calendar | `/api/items/{id}/history` | `readings_1d` (hierarchical continuous aggregate) | On open |
| How accurate is this? | `/api/items/{id}/accuracy` | aggregate vs raw `readings` vs Python | On open |
| Alerts (+ badge, notifications) | `/api/alerts`, `/api/stream` | `alerts`, written by the `check_alerts` job; `NOTIFY` on change | Yes |
| Outage rescue | `/api/rescue` | PostGIS `outages` × `users` × `refuges`, budgets and live trends from `readings_5m` | Yes |
| Porch heat | `/api/porch` | `weather_hourly` hypertable (Aug 2026 Open-Meteo archive) × `deliveries` | Historical analysis by design |
| Under the hood | `/api/tiger` | `timescaledb_information.*`, columnstore stats, benchmark (cached 10 min) | On open / Refresh |
| Demo controls → late data | `/api/items/{id}/compare` | aggregate vs raw, `cagg_watermark`, `ingest_log` | Yes |
| Caregiver page | `/s/{token}` → `/api/share/{token}` | `shares` + the same item summaries | On open |
| Not from Tiger | | label text (openFDA / Gemini), recalls (openFDA), 48 h home weather (Open-Meteo, cached 1 h), Gemini answers | |

This is a decision-support prototype, not medical advice.

## What makes LIFELOG different

**For patients and caregivers**
- **Works without AI.** Every Gemini feature has a rule-based fallback built from your data and the label's own words
  (safe-to-use verdict, refill letter, outage dispatch, pharmacy brief, add-by-name from the FDA label, typed reports).
  Answers say when no AI was used. Only photo checks, voice recordings and free-text trip plans need Gemini.
- **Verifiable exposure receipts.** One tap freezes a medicine's exposure record and stores its SHA-256 fingerprint.
  A pharmacist or insurer scans the QR code (`/r/<code>`) and sees the record, plus whether the stored readings still
  match. Late or edited data for that period is detected. Refill letters include the receipt.
- **Real sensors, no app.** "Connect sensor" pairs a Bluetooth thermometer straight from the browser (Web Bluetooth;
  Health Thermometer or Environmental Sensing service) and streams readings into Tiger Data.

**For developers**
- **Device keys + `POST /v1/readings`** (`Authorization: Bearer llg_…`): any thermometer, smart fridge or cooler bag
  sends temperatures; LIFELOG does the life-budget math, late-data repair and alerts in Tiger Data.
- **Signed alert webhooks**: every alert is POSTed within seconds with `X-Lifelog-Signature: sha256=HMAC(secret, body)`.
- **An open format for medicine storage rules**: `GET /api/schema/stability-model` (JSON Schema), `GET /api/products`,
  and the full API at `/docs`.

## Safety switches
- `GEMINI_DISABLED=1`: no Gemini request ever leaves the server (for testing without spending quota).
- **Reset & seed data** only works on a local database; on a shared/cloud database it is refused unless
  `ALLOW_DEMO_RESET=1`. The button also asks for confirmation.

## Medicines and where their rules come from
Every seeded product quotes Section 16 ("How Supplied / Storage and Handling") of its FDA prescribing information,
retrieved through [openFDA](https://open.fda.gov/apis/drug/label/). Each links to its DailyMed page in the app.

| Medicine | Labeled storage | Allowance outside it (verbatim on label) |
|---|---|---|
| Lantus SoloStar (insulin glargine) | 2–8°C | up to 30°C, 28 days |
| NovoLog FlexPen (insulin aspart) | 2–8°C | up to 30°C, 28 days |
| Tresiba FlexTouch (insulin degludec) | 2–8°C | up to 30°C, 56 days |
| Mounjaro single-dose pen (tirzepatide) | 2–8°C | up to 30°C, 21 days total |
| Trulicity pen (dulaglutide) | 2–8°C | up to 30°C, 14 days total |
| Victoza pen, in use (liraglutide) | 2–8°C | 15–30°C, 30 days |
| Enbrel SureClick (etanercept) | 2–8°C | 20–25°C, one period of 30 days |
| EpiPen (epinephrine) | 20–25°C | excursions 15–30°C (no time limit given) |
| Xalatan, opened (latanoprost) | 2–8°C | up to 25°C for 6 weeks; up to 40°C for 8 days |

Calendar limits apply on top of the life budget: the printed expiration date and the label's in-use period after
opening (Lantus and NovoLog 28 days, Tresiba 56, Victoza 30, Xalatan 6 weeks). Either one turns the status to
"Do not use" no matter how cold the medicine was kept.

Where a label is silent (time tolerated above its highest limit, EpiPen excursion length, temperatures between the
fridge range and a stated room range) LIFELOG says so in the app and uses a flagged assumption.
Not seeded: injectable Ozempic and Humira, because openFDA only returned repackager labels without the storage text;
use "Add it by name" or a label photo.

### Add any medicine by name
`POST /api/products/lookup {"name": "Trulicity"}`:
1. **openFDA first** — the manufacturer's label, storage section verbatim (repackager labels without storage text are skipped).
2. **Gemini fallback** — Google Search grounding + URL Context find the official prescribing information; the app shows
   every source URL and the exact label text it read, so the person can confirm it matches their medicine
   (e.g. "Ozempic" on openFDA is now also an oral tablet).
3. Gemini structured output turns the text into the life-budget model.

### Languages
Gemini answers (verdicts, photo checks, Ask LIFELOG, trip advice, outage texts) can be in English, Arabic, Spanish or
Bengali — Dearborn has large Arabic-speaking and Bengali-speaking communities. Label quotes, numbers and medicine
names stay as written; refill letters stay in English for the pharmacist with a summary in the chosen language.

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
- **Tiger Cloud (for judging):** install the [Tiger CLI](https://github.com/timescale/tiger-cli)
  (Windows: `irm https://cli.tigerdata.com/install.ps1 | iex`; macOS/Linux: `curl -fsSL https://cli.tigerdata.com | sh`), then:
  ```bash
  tiger auth login
  tiger service create --name lifelog          # paid plan / trial: 0.5 CPU / 2 GB + time-series add-on
  tiger db uri lifelog --with-password         # direct URI with sslmode=require -> DATABASE_URL in .env
  tiger db ping lifelog
  ```
  Use a **standard (paid or trial) service**, not the free shared tier: on the Free plan `tiger service create`
  makes a shared-CPU service with fewer features that turns read-only at its storage limit.
  If the service was created in the web console, the CLI doesn't know its password yet: run
  `tiger db save-password <service>` (or `tiger service update-password <service>` to reset it) before `tiger db uri --with-password`.
  Use the **direct** URI, not `--pooled`: `LISTEN/NOTIFY` needs a session connection.
  Needs the `timescaledb`, `vector` and `postgis` extensions (`check_setup.py` lists what's available).
- **Local:** `docker run -d --name lifelog-db -p 55432:5432 -e POSTGRES_PASSWORD=lifelog timescale/timescaledb-ha:pg17`, then `DATABASE_URL=postgresql://postgres:lifelog@localhost:55432/postgres`.

### Scale mode (benchmark data)
```bash
.venv/Scripts/python scripts/setup_db.py --scale 50     # demo seed + 50 synthetic medicines x 90 days of 1-minute readings
.venv/Scripts/python scripts/benchmark.py               # Markdown table: sizes, compression, rollup vs raw timings
```
History is generated **server-side** (`INSERT ... SELECT` over `generate_series`, one transaction per day), then both
continuous aggregates are refreshed over the full range and old chunks are converted to the columnstore. The script prints
a storage estimate first; sizes above 50 need `--yes`. Synthetic users (`users.synthetic`) count everywhere Tiger is
measured but are left out of alerts, the outage demo and the live simulator.

**Demo controls → Reset returns to the small seed** (14 days, 26 medicines) and drops scale data. Rehearse with Reset,
then run `setup_db.py --scale 50` right before judging and don't press Reset during it.

### Phones and other devices
Responsive layout: sidebar on desktop, bottom tab bar on phones, safe-area insets for notched phones, light and dark
themes, installable to the home screen (web app manifest), print stylesheet for a pharmacist summary.

### Checked against Tiger Data's docs
- **Columnstore (hypercore)** is the current compression API: `enable_columnstore` + `segmentby`/`orderby`,
  `add_columnstore_policy`, `convert_to_columnstore`. `add_compression_policy` is deprecated since TimescaleDB 2.18;
  LIFELOG uses the new API and falls back to the old one on older versions.
- **Continuous aggregates with joins**: one hypertable with several regular tables is supported from TimescaleDB 2.16
  (INNER/LEFT/LATERAL joins, equality conditions). Changes to the joined regular tables (`items`, `products`) are not
  tracked — a product's rules are written once; if they were edited, refresh that item's readings.
- **Real-time aggregation** has been off by default since 2.13; `readings_5m` turns it on explicitly.
- **Extensions on Tiger Cloud**: `timescaledb_toolkit` is enabled by default; `postgis` and `vector` (pgvector) are
  available and enabled by `sql/schema.sql`.

### Real time on Tiger Cloud
`LISTEN/NOTIFY` needs a direct (not transaction-pooled) connection: use the default `tiger db uri`, not `--pooled`.

### Deploy
Docker (Docker Desktop), Render / Railway, environment variables, the Tiger fork backup and the pre-judging checklist
are in **[DEPLOY.md](DEPLOY.md)**. Quick local container:
```bash
docker build -t lifelog .
docker run -d --name lifelog --restart unless-stopped --env-file .env -e GEMINI_TIMEOUT_S=60 -p 8000:8000 lifelog
```
then open http://localhost:8000.

### Tests and validation
```bash
.venv/Scripts/python -m pytest -q                 # 53 unit + integration tests (engine, SQL parity, forecast, Gemini fallback, chatbot tools)
.venv/Scripts/python scripts/verify_tiger.py      # Tiger objects, jobs, late-data path, LISTEN/NOTIFY
.venv/Scripts/python scripts/validate_app.py      # every endpoint end to end against the running app (add --demo for the scenarios)
```
Run them while the demo is idle (no live sensors, storm or unrepaired late upload).

### Gemini models and limits
Every Gemini call goes through `gem.generate()`: a per-request timeout (`GEMINI_TIMEOUT_S`, default 60), one retry on
503/500/504, then the next model in `GEMINI_FALLBACK_MODELS` (default `gemini-3.7-flash,gemini-3.5-flash,gemini-3.1-flash-lite`)
on 503 / 429 / 404. When all fail, the button shows Google's reason (e.g. "429 free-tier limit of 20 requests per day
reached, resets in ~21 min") and the Tiger-powered features keep working. **A free-tier key allows only 20 requests per
model per day, and one chatbot question uses 3 to 5** (one per tool round): enable billing on the key's Google AI Studio
project before judging.

## 5-minute demo script
1. **Demo controls → Reset.** My medicines: four medicines near 100%. The Lantus spare shows *sensor silent*; the Victoza pen must be used within 3 days of its in-use period. (If scale data is loaded for judging, skip Reset: it returns to the small seed.)
2. **Add medicine:** upload a real label photo. Gemini builds the model with quotes. Track it.
3. **Demo controls → EpiPen: hot car.** Open the EpiPen: budget drops to ~45%, "Above labeled limit, peak 48°C". The what-if table shows that moving it indoors saves it. Click **Is it safe to use?**
4. **Late data (the Tiger moment):** *Insulin: late upload (no fix)*. Dashboard ~97–99% vs raw truth ~90%, because the late rows sit behind the continuous-aggregate watermark (the strip shows them in red behind the watermark line). Click **Repair late windows**: the rows turn green and both numbers agree. (Normal path: `/api/ingest` detects late rows and refreshes just those buckets.)
5. **Start live sensors, then Summer storm outage → Outage rescue.** Fridges show a live warming trend (↑ °C/h) and the ranking uses each fridge's measured warming rate. Ranked list: your hot-car EpiPen fails first (~3 h) vs 12 h to restore. Click **Gemini dispatch plan**.
6. **Refill letter** on the insulin. Then **Porch heat** → **Gemini pharmacy brief**.
7. **Alerts** tab: the hot-car and outage alerts were opened by a job running inside Tiger; the sensor-silent alert resolved itself when the late data arrived.
8. **Ask LIFELOG:** "Which of my medicines is in the worst shape, and why?" Gemini calls the tools and shows which.
9. **Under the hood:** compression ratio, scheduled jobs, rollup vs raw timings (the 30-day calendar is ~200× faster from `readings_1d` at scale). Optionally **Start live sensors** and watch the dashboard move.

## API
`GET /api/health`, `GET /api/items`, `GET|PATCH /api/items/{id}` (PATCH: `opened_at`, `expires_on`, `lot`),
`GET /api/items/{id}/compare|history|recalls|accuracy|export.csv`, `POST /api/items/{id}/advice|visual|letter|trip|repair`,
`POST /api/products/extract|lookup`, `POST /api/products`, `POST /api/ingest`, `POST /api/voice`, `GET /api/stream` (SSE),
`GET /api/rescue`, `POST /api/rescue/plan`, `GET /api/porch`, `POST /api/porch/brief`, `GET /api/alerts`, `POST /api/alerts/check`,
`POST /api/alerts/{id}/ack`, `POST /api/share`, `GET /api/shares`, `GET|DELETE /api/share/{token}`, `GET /s/{token}` (caregiver page),
`GET /api/tiger[?fresh=1]`, `POST /api/sim/{start|stop|status}`, `POST /api/ask`,
`POST /api/demo/{reset|hot_car|late_upload|storm|clear_storm}`.

`POST /api/ingest` body:
```json
{"item_id": 2, "source": "sensor", "auto_refresh": true,
 "readings": [{"ts": "2026-10-03T14:05:00Z", "temp_c": 9.4}]}
```
