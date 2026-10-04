-- LIFELOG Home schema. Idempotent: safe to re-run.
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- people & places
CREATE TABLE IF NOT EXISTS users (
    id        SERIAL PRIMARY KEY,
    name      TEXT NOT NULL,
    is_me     BOOLEAN NOT NULL DEFAULT FALSE,
    can_host  BOOLEAN NOT NULL DEFAULT FALSE,   -- neighbor with generator/fridge who opted in
    lat       DOUBLE PRECISION NOT NULL,
    lon       DOUBLE PRECISION NOT NULL,
    geom      geometry(Point, 4326) NOT NULL
);

-- scale-mode users (scripts/setup_db.py --scale N): history for benchmarks, kept out of the outage demo
ALTER TABLE users ADD COLUMN IF NOT EXISTS synthetic BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS refuges (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    lat        DOUBLE PRECISION NOT NULL,
    lon        DOUBLE PRECISION NOT NULL,
    geom       geometry(Point, 4326) NOT NULL,
    has_fridge BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS refuges_geom ON refuges USING gist (geom);
CREATE INDEX IF NOT EXISTS users_geom ON users USING gist (geom);

-- ---------------------------------------------------------------- medicines
-- model = stability model extracted by Gemini from the label (see lifelog/models.py)
CREATE TABLE IF NOT EXISTS products (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    model      JSONB NOT NULL,
    source     TEXT NOT NULL DEFAULT 'demo',   -- 'demo' | 'gemini'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE products ADD COLUMN IF NOT EXISTS source_label TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS source_url TEXT;

CREATE TABLE IF NOT EXISTS items (
    id         SERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_id INT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    nickname   TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now()   -- life budget starts counting here
);

ALTER TABLE items ADD COLUMN IF NOT EXISTS opened_at  TIMESTAMPTZ;  -- first use / opening
ALTER TABLE items ADD COLUMN IF NOT EXISTS expires_on DATE;         -- printed expiration date
ALTER TABLE items ADD COLUMN IF NOT EXISTS lot        TEXT;         -- lot number, matched against FDA recalls

-- Read-only caregiver links
CREATE TABLE IF NOT EXISTS shares (
    token      TEXT PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked    BOOLEAN NOT NULL DEFAULT FALSE
);

-- Verifiable exposure receipts: a fingerprint (SHA-256) of an item's exposure record up to a moment in time.
CREATE TABLE IF NOT EXISTS receipts (
    code       TEXT PRIMARY KEY,
    item_id    INT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    as_of      TIMESTAMPTZ NOT NULL,
    payload    JSONB NOT NULL,
    digest     TEXT NOT NULL
);

-- Developer platform: per-device ingest keys and signed alert webhooks
CREATE TABLE IF NOT EXISTS devices (
    token      TEXT PRIMARY KEY,
    item_id    INT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    label      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ,
    readings   BIGINT NOT NULL DEFAULT 0,
    revoked    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS webhooks (
    id          SERIAL PRIMARY KEY,
    url         TEXT NOT NULL,
    secret      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    last_status TEXT,
    last_at     TIMESTAMPTZ
);

-- ---------------------------------------------------------------- telemetry
CREATE TABLE IF NOT EXISTS readings (
    ts          TIMESTAMPTZ NOT NULL,
    item_id     INT NOT NULL,
    temp_c      DOUBLE PRECISION NOT NULL,
    humidity    DOUBLE PRECISION,
    source      TEXT NOT NULL DEFAULT 'sensor',  -- sensor | voice | weather | manual
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
SELECT create_hypertable('readings', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS readings_item_ts ON readings (item_id, ts DESC);


CREATE TABLE IF NOT EXISTS ingest_log (
    id          SERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    item_id     INT NOT NULL,
    source      TEXT NOT NULL,
    n           INT NOT NULL,
    min_ts      TIMESTAMPTZ NOT NULL,
    max_ts      TIMESTAMPTZ NOT NULL,
    late        BOOLEAN NOT NULL,
    refreshed   BOOLEAN NOT NULL
);
ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS rejected INT NOT NULL DEFAULT 0;
ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS flagged INT NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------- exposure pattern library
CREATE TABLE IF NOT EXISTS pattern_library (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    fingerprint vector(12) NOT NULL
);

-- ---------------------------------------------------------------- outages
CREATE TABLE IF NOT EXISTS outages (
    id             SERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    area           geometry(Polygon, 4326) NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    est_restore_at TIMESTAMPTZ NOT NULL,
    indoor_temp_c  DOUBLE PRECISION NOT NULL,      -- expected indoor temp without power
    source         TEXT NOT NULL DEFAULT 'demo',
    active         BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS outages_area ON outages USING gist (area);

-- ---------------------------------------------------------------- porch heat index
CREATE TABLE IF NOT EXISTS zips (
    zip  TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    lat  DOUBLE PRECISION NOT NULL,
    lon  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_hourly (
    ts     TIMESTAMPTZ NOT NULL,
    zip    TEXT NOT NULL,
    temp_c DOUBLE PRECISION NOT NULL
);
SELECT create_hypertable('weather_hourly', by_range('ts', INTERVAL '7 days'), if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS weather_hourly_zip_ts ON weather_hourly (zip, ts);

CREATE TABLE IF NOT EXISTS deliveries (
    id           SERIAL PRIMARY KEY,
    zip          TEXT NOT NULL REFERENCES zips(zip),
    carrier      TEXT NOT NULL,
    pharmacy     TEXT NOT NULL,
    product_kind TEXT NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL,
    picked_up_at TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------- hyperfunctions (optional)
DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'timescaledb_toolkit not available: time-weighted averages disabled';
END $$;

-- ---------------------------------------------------------------- alerts (written by the check_alerts job)
CREATE TABLE IF NOT EXISTS alerts (
    id          SERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    item_id     INT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,      -- frozen | above_limit | budget_exhausted | budget_low | sensor_silent | outage
                                    -- | expired | in_use_over | in_use_ending | warming_trend | freeze_risk
    severity    TEXT NOT NULL,      -- critical | warning
    message     TEXT NOT NULL,
    resolved_at TIMESTAMPTZ,
    acked       BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX IF NOT EXISTS alerts_open ON alerts (item_id, kind) WHERE resolved_at IS NULL;
