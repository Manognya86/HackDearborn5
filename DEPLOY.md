# Deploying LIFELOG

LIFELOG is one FastAPI process talking to one Tiger Cloud (TimescaleDB) service. Everything time-series
(hypertables, continuous aggregates, compression, retention, the alert job, `LISTEN/NOTIFY`) runs inside Tiger;
the web process serves the UI, the API, a Server-Sent Events stream and a few in-memory threads.

**It needs a long-running process.** The SSE broker, the `LISTEN lifelog` thread, the alert re-check thread, the
benchmark warm-up and the live-sensor simulator live in memory. Serverless hosts (Vercel, Netlify functions,
AWS Lambda, Cloud Run scaled to zero) won't work. Run **one** worker: the SSE broker is per process.

---

## 1. Tiger Cloud service

Install the [Tiger CLI](https://github.com/timescale/tiger-cli):
```powershell
irm https://cli.tigerdata.com/install.ps1 | iex          # Windows (PowerShell)
```
```bash
curl -fsSL https://cli.tigerdata.com | sh                  # macOS / Linux
```
Then:
```bash
tiger auth login
tiger service create --name lifelog          # paid plan / trial: 0.5 CPU / 2 GB + time-series add-on
tiger db uri lifelog --with-password         # DIRECT URI with sslmode=require -> DATABASE_URL
tiger db ping lifelog
```
- Use a **standard (paid or trial) service**, not the free shared tier: on the Free plan `tiger service create`
  makes a shared-CPU service with fewer features that turns read-only at its storage limit.
- Use the **direct** URI, never `--pooled`: `LISTEN/NOTIFY` needs a session connection, not a transaction pooler.
- If the service was created in the web console, the CLI doesn't know its password: `tiger db save-password <service>`,
  or reset it with `tiger service update-password <service>` (teammates on the old password are locked out).
- `tiger version` (not `--version`) prints the CLI version.

Extensions used: `timescaledb`, `timescaledb_toolkit` (enabled by default on Tiger Cloud), `postgis`, `vector`.
`scripts/check_setup.py` lists what's installed and available.

## 2. Environment variables

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Direct Tiger URI, `...?sslmode=require`. Secret. |
| `GEMINI_API_KEY` | for Gemini features | https://aistudio.google.com/apikey. Secret. Without it, everything Tiger-powered still works. |
| `GEMINI_MODEL` | no | Default `gemini-3.8-flash`. |
| `GEMINI_TIMEOUT_S` | no | Per-request timeout, default 60 (verdicts have been measured at ~30 s). |
| `GEMINI_FALLBACK_MODELS` | no | Tried in order on 503 / 429 / 404. Default `gemini-3.7-flash,gemini-3.5-flash,gemini-3.1-flash-lite`. |
| `POLICY_START_OFFSET` | no | Refresh window of the `readings_5m` policy, default `2 hours`. |
| `PORT` | host-provided | The container and Procfile bind to it (default 8000). |

Never commit `.env` (it's in `.gitignore` and `.dockerignore`). Never print `DATABASE_URL` or `GEMINI_API_KEY`.

## 3. Create and seed the database (once, from your machine)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env                                      # fill DATABASE_URL and GEMINI_API_KEY
.venv\Scripts\python scripts\check_setup.py
.venv\Scripts\python scripts\setup_db.py                    # demo seed: 14 days, 26 medicines (~20 s)
.venv\Scripts\python scripts\setup_db.py --scale 50         # + 50 x 90 days of 1-minute history (~2 min)
.venv\Scripts\python scripts\verify_tiger.py                # pass/fail report, exit code != 0 on failure
.venv\Scripts\python scripts\benchmark.py                   # Markdown table for slides / Devpost
```
`setup_db.py` is idempotent: every run re-applies the schema and reseeds from scratch. Sizes above `--scale 50`
need `--yes` after you check the printed storage estimate. Seed from your machine, not on every deploy.

## 4. Docker (Docker Desktop)

The image contains the code only; secrets are passed at run time.
```powershell
docker build -t lifelog .
docker run -d --name lifelog --restart unless-stopped --env-file .env -e GEMINI_TIMEOUT_S=60 -p 8000:8000 lifelog
```
Open **http://localhost:8000**. Check it with `curl http://localhost:8000/api/health` (`"ok": true`).

After changing code:
```powershell
docker build -t lifelog . ; docker rm -f lifelog ; docker run -d --name lifelog --restart unless-stopped --env-file .env -e GEMINI_TIMEOUT_S=60 -p 8000:8000 lifelog
```
Useful: `docker logs -f lifelog`, `docker ps --filter name=lifelog` (the `HEALTHCHECK` turns the status
"healthy" once the database answers). `--env-file` takes values literally: no quotes around values in `.env`.

## 5. Render or Railway

- **Build:** `pip install -r requirements.txt` (or use the `Dockerfile`).
- **Start** (also in `Procfile`):
  `uvicorn lifelog.app:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips="*"`
- **Secrets:** `DATABASE_URL`, `GEMINI_API_KEY`; set `GEMINI_TIMEOUT_S=60`.
- **Health check path:** `/api/health`.
- **Instance:** an always-on plan (free web services that sleep drop the SSE stream and the LISTEN thread).
- SSE responses send `X-Accel-Buffering: no` and `Cache-Control: no-cache` so proxies don't buffer the live stream.

## 6. `/api/health`

```json
{"ok": true, "database": true, "timescaledb_version": "2.30.2", "jobs": 6, "job_failures": 0,
 "jobs_failing_now": 0, "listen_thread_alive": true, "listen_connected": true, "gemini": true, ...}
```
`ok` = database answers, no Tiger job's last run failed, LISTEN thread alive. No secrets are returned.

## 7. Known-good backup: fork the service

A fork is an independent copy of the service taken now, with its own background jobs.
```bash
tiger service fork lifelog --name lifelog-judging --no-set-default   # keep 'lifelog' as the CLI default
tiger service list                                                   # wait for READY
tiger db save-password lifelog-judging                               # only if the next command says it's unavailable
tiger db uri lifelog-judging --with-password                         # direct URI of the fork
```
To switch: replace `DATABASE_URL` in `.env` (and on the host), restart the app (or the container), then run
`scripts/verify_tiger.py --no-wait`. Switch back the same way. `--last-snapshot` forks faster;
`--to-timestamp 2026-10-04T14:00:00Z` forks a point in time.

## 8. Before judging

1. Rehearse with **Demo controls → Reset** as often as you like (it returns to the small seed and drops scale data).
2. `scripts/setup_db.py --scale 50`, then **don't press Reset** during judging.
3. `scripts/verify_tiger.py` must end with `ALL CHECKS PASSED`; `scripts/validate_app.py` must end with `0 failed`
   (it uses ~15 Gemini requests: run it before, not right before, judging on a free-tier key).
4. `scripts/benchmark.py`, and paste the table.
5. Fork (section 7).
6. Start the app (or rebuild/restart the container) and open **Under the hood** once: the benchmark warms up in the
   background for ~2 minutes after start-up, then the page opens instantly.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `password authentication failed` | `tiger db ping <service>`; if that fails too, `tiger service update-password`, then rewrite `DATABASE_URL`. |
| `password not available to include in connection string` | `tiger db save-password <service>`. |
| Live updates stop (`listen_connected: false`) | You're on a pooled URI: use the direct one. |
| A Gemini button says "didn't answer within N s" or "rate-limited" | Retry; raise `GEMINI_TIMEOUT_S`. Tiger features are unaffected. |
| "429 free-tier limit of 20 requests per day reached" | The key is on Gemini's free tier (20 requests per model per day; a chatbot question uses 3-5). Enable billing on the key's Google AI Studio project, or add models to `GEMINI_FALLBACK_MODELS`. The chatbot still answers from Tiger data, labeled "without AI". |
| `could not refresh continuous aggregate ... due to a concurrent refresh` | Fixed: refreshes retry while Tiger's policy job holds the window (`db.call_refresh`). |
| Under the hood takes minutes | The benchmark scans every raw reading; it's cached for 10 min after the first run. |
| `pytest` fails `test_daily_rollup_matches_the_5_minute_rollup` | Run tests while the demo is idle: "late upload (no fix)" (press **Repair late windows**) or live sensors / a storm in progress make the 15-minute daily rollup lag on purpose. |
| Docker can't reach the database | `.env` values must not be quoted; check `docker logs lifelog`. |
