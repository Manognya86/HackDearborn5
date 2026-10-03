-- Bodies reference readings_5m, created afterwards in aggregates.sql.
SET check_function_bodies = off;

-- Life-budget math. Mirrors lifelog/engine.py exactly (tests/test_parity.py checks this).
--
-- Model JSON (extracted from the label by Gemini):
--   target_min_c / target_max_c   labeled storage range: burns 0 budget
--   bands[]: {label,min_c,max_c,budget_hours}   labeled allowances, e.g. "room temp up to 30C for 28 days"
--   freeze_discard, freeze_c      "do not freeze": any reading <= freeze_c consumes the whole budget
--   cold_ok                       below target but above freezing is fine (e.g. fridge at 1.5C)
--   above_limit_budget_hours      hours tolerated just above the highest labeled limit;
--                                 rate doubles every 10C beyond it (Q10 = 2 assumption)

CREATE OR REPLACE FUNCTION burn_rate(t DOUBLE PRECISION, m JSONB)
RETURNS DOUBLE PRECISION LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    b   JSONB;
    lo  DOUBLE PRECISION := (m->>'target_min_c')::float8;
    hi  DOUBLE PRECISION := (m->>'target_max_c')::float8;
    lim DOUBLE PRECISION := (m->>'above_limit_budget_hours')::float8;
BEGIN
    IF t IS NULL THEN RETURN 0; END IF;
    IF coalesce((m->>'freeze_discard')::bool, false) AND t <= (m->>'freeze_c')::float8 THEN
        RETURN 'Infinity';
    END IF;
    IF t >= lo AND t <= hi THEN RETURN 0; END IF;
    FOR b IN SELECT * FROM jsonb_array_elements(coalesce(m->'bands', '[]'::jsonb)) LOOP
        IF t >= (b->>'min_c')::float8 AND t <= (b->>'max_c')::float8 THEN
            RETURN 1.0 / (b->>'budget_hours')::float8;
        END IF;
        hi := greatest(hi, (b->>'max_c')::float8);
        lo := least(lo, (b->>'min_c')::float8);
    END LOOP;
    IF t > hi THEN RETURN power(2.0, (t - hi) / 10.0) / lim; END IF;
    IF coalesce((m->>'cold_ok')::bool, true) THEN RETURN 0; END IF;
    RETURN 1.0 / lim;
END $$;

CREATE OR REPLACE FUNCTION zone_of(avg_t DOUBLE PRECISION, min_t DOUBLE PRECISION, m JSONB)
RETURNS TEXT LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    b  JSONB;
    lo DOUBLE PRECISION := (m->>'target_min_c')::float8;
    hi DOUBLE PRECISION := (m->>'target_max_c')::float8;
BEGIN
    IF coalesce((m->>'freeze_discard')::bool, false) AND min_t <= (m->>'freeze_c')::float8 THEN
        RETURN 'Frozen';
    END IF;
    IF avg_t >= lo AND avg_t <= hi THEN RETURN 'Labeled storage'; END IF;
    FOR b IN SELECT * FROM jsonb_array_elements(coalesce(m->'bands', '[]'::jsonb)) LOOP
        IF avg_t >= (b->>'min_c')::float8 AND avg_t <= (b->>'max_c')::float8 THEN
            RETURN b->>'label';
        END IF;
        hi := greatest(hi, (b->>'max_c')::float8);
        lo := least(lo, (b->>'min_c')::float8);
    END LOOP;
    IF avg_t > hi THEN RETURN 'Above labeled limit'; END IF;
    IF coalesce((m->>'cold_ok')::bool, true) THEN RETURN 'Labeled storage'; END IF;
    RETURN 'Too cold';
END $$;

CREATE OR REPLACE FUNCTION bucket_burn(avg_t DOUBLE PRECISION, min_t DOUBLE PRECISION, m JSONB, hours DOUBLE PRECISION)
RETURNS DOUBLE PRECISION LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN coalesce((m->>'freeze_discard')::bool, false) AND min_t <= (m->>'freeze_c')::float8 THEN 1.0
        ELSE least(1.0, burn_rate(avg_t, m) * hours)
    END
$$;

-- Exact bucket burn: uses the mean per-reading rate when available, else burn(avg temperature).
CREATE OR REPLACE FUNCTION bucket_burn_exact(avg_t DOUBLE PRECISION, min_t DOUBLE PRECISION, avg_rate DOUBLE PRECISION,
                                             m JSONB, hours DOUBLE PRECISION)
RETURNS DOUBLE PRECISION LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN coalesce((m->>'freeze_discard')::bool, false) AND min_t <= (m->>'freeze_c')::float8 THEN 1.0
        WHEN avg_rate IS NOT NULL THEN least(1.0, avg_rate * hours)
        ELSE least(1.0, burn_rate(avg_t, m) * hours)
    END
$$;

-- Per-bucket timeline with running budget use.
--   p_raw   = true recomputes from raw readings instead of the continuous aggregate (ground truth)
--   p_naive = true uses burn(average temperature) instead of the per-reading rate (accuracy comparison)
DROP FUNCTION IF EXISTS item_timeline(INT, BOOLEAN);
CREATE OR REPLACE FUNCTION item_timeline(p_item INT, p_raw BOOLEAN DEFAULT FALSE, p_naive BOOLEAN DEFAULT FALSE)
RETURNS TABLE (bucket TIMESTAMPTZ, avg_temp DOUBLE PRECISION, min_temp DOUBLE PRECISION,
               max_temp DOUBLE PRECISION, n BIGINT, zone TEXT, burn DOUBLE PRECISION, used DOUBLE PRECISION)
LANGUAGE sql STABLE AS $$
    WITH m AS (
        SELECT p.model, i.started_at FROM items i JOIN products p ON p.id = i.product_id WHERE i.id = p_item
    ),
    b AS (
        SELECT r.bucket, r.avg_temp, r.min_temp, r.max_temp, r.n, r.avg_rate
        FROM readings_5m r WHERE NOT p_raw AND r.item_id = p_item
        UNION ALL
        SELECT time_bucket('5 minutes', x.ts), avg(x.temp_c), min(x.temp_c), max(x.temp_c), count(*),
               avg(least(burn_rate(x.temp_c, m.model), 1e6))
        FROM readings x, m WHERE p_raw AND x.item_id = p_item
        GROUP BY 1
    ),
    c AS (
        SELECT b.*, m.model,
               bucket_burn_exact(b.avg_temp, b.min_temp, CASE WHEN p_naive THEN NULL ELSE b.avg_rate END,
                                 m.model, 5 / 60.0) AS burn
        FROM b, m WHERE b.bucket >= m.started_at
    )
    SELECT c.bucket, c.avg_temp, c.min_temp, c.max_temp, c.n,
           -- a brief spike inside an otherwise in-range bucket is attributed to the spike's zone
           CASE WHEN c.burn > 0 AND burn_rate(c.avg_temp, c.model) = 0
                THEN zone_of(c.max_temp, c.min_temp, c.model)
                ELSE zone_of(c.avg_temp, c.min_temp, c.model) END,
           c.burn,
           least(1.0, sum(c.burn) OVER (ORDER BY c.bucket))
    FROM c
    ORDER BY c.bucket
$$;

-- Collapse the timeline into exposure episodes (gaps-and-islands): what consumed the budget.
CREATE OR REPLACE FUNCTION item_episodes(p_item INT, p_raw BOOLEAN DEFAULT FALSE)
RETURNS TABLE (zone TEXT, started TIMESTAMPTZ, ended TIMESTAMPTZ, minutes DOUBLE PRECISION,
               avg_temp DOUBLE PRECISION, peak_temp DOUBLE PRECISION, low_temp DOUBLE PRECISION,
               burn DOUBLE PRECISION)
LANGUAGE sql STABLE AS $$
    WITH t AS (SELECT * FROM item_timeline(p_item, p_raw)),
    g AS (
        SELECT t.*,
               CASE WHEN t.zone IS DISTINCT FROM lag(t.zone) OVER w
                      OR t.bucket - lag(t.bucket) OVER w > INTERVAL '5 minutes'
                    THEN 1 ELSE 0 END AS brk
        FROM t WINDOW w AS (ORDER BY t.bucket)
    ),
    s AS (SELECT g.*, sum(g.brk) OVER (ORDER BY g.bucket) AS grp FROM g)
    SELECT s.zone, min(s.bucket), max(s.bucket) + INTERVAL '5 minutes', count(*) * 5.0,
           avg(s.avg_temp), max(s.max_temp), min(s.min_temp), sum(s.burn)
    FROM s GROUP BY s.grp, s.zone ORDER BY min(s.bucket)
$$;

-- Mailbox / porch temperature from air temperature: daytime solar gain assumption.
CREATE OR REPLACE FUNCTION mailbox_temp(air DOUBLE PRECISION, ts TIMESTAMPTZ)
RETURNS DOUBLE PRECISION LANGUAGE sql IMMUTABLE AS $$
    SELECT air + CASE WHEN extract(hour FROM ts AT TIME ZONE 'America/Detroit') BETWEEN 10 AND 17
                      THEN 12.0 ELSE 2.0 END
$$;

-- Exposure statistics. Mean Kinetic Temperature (USP <1079>) with dH/R = 83.144 kJ/mol / R = 10000 K,
-- over equal-length 5-minute buckets: the single temperature that would cause the same total degradation.
CREATE OR REPLACE FUNCTION item_stats(p_item INT)
RETURNS TABLE (mkt_c DOUBLE PRECISION, avg_c DOUBLE PRECISION, min_c DOUBLE PRECISION,
               max_c DOUBLE PRECISION, hours DOUBLE PRECISION)
LANGUAGE sql STABLE AS $$
    SELECT 10000.0 / -ln(avg(exp(-10000.0 / (t.avg_temp + 273.15)))) - 273.15,
           avg(t.avg_temp), min(t.min_temp), max(t.max_temp), count(*) * 5 / 60.0
    FROM item_timeline(p_item) t
$$;

-- Live temperature trend: least-squares slope over the item's last p_minutes of 5-minute buckets, read from
-- the real-time continuous aggregate (so it includes readings that arrived seconds ago). Feeds the trend
-- forecast (lifelog/engine.py trend_forecast), the early-warning alerts below and the outage warming model.
CREATE OR REPLACE FUNCTION item_trend(p_item INT, p_minutes INT DEFAULT 30)
RETURNS TABLE (slope_c_per_h DOUBLE PRECISION, r2 DOUBLE PRECISION, n BIGINT,
               last_temp DOUBLE PRECISION, last_bucket TIMESTAMPTZ)
LANGUAGE sql STABLE AS $$
    WITH w AS (
        SELECT r.bucket, r.avg_temp FROM readings_5m r
        WHERE r.item_id = p_item
          AND r.bucket > (SELECT max(x.bucket) FROM readings_5m x WHERE x.item_id = p_item) - make_interval(mins => p_minutes)
    )
    SELECT regr_slope(w.avg_temp, extract(epoch FROM w.bucket) / 3600.0),
           regr_r2(w.avg_temp, extract(epoch FROM w.bucket) / 3600.0),
           count(*),
           (SELECT y.avg_temp FROM w y ORDER BY y.bucket DESC LIMIT 1),
           max(w.bucket)
    FROM w
$$;

-- Every alert condition that holds right now, one row per (item, kind).
CREATE OR REPLACE FUNCTION current_conditions()
RETURNS TABLE (item_id INT, kind TEXT, severity TEXT, message TEXT)
LANGUAGE sql STABLE AS $$
    WITH last AS (
        SELECT i.id, i.nickname, i.opened_at, i.expires_on, p.model, u.geom, t.bucket, t.avg_temp, t.zone, t.used,
               tr.slope_c_per_h AS slope, tr.r2, tr.n AS trend_n, tr.last_temp
        FROM items i
        JOIN products p ON p.id = i.product_id
        JOIN users u ON u.id = i.user_id
        CROSS JOIN LATERAL (SELECT * FROM item_timeline(i.id) x ORDER BY x.bucket DESC LIMIT 1) t
        CROSS JOIN LATERAL item_trend(i.id) tr
        WHERE NOT u.synthetic   -- scale-mode history is for benchmarks, not people to alert
    )
    SELECT id, 'frozen', 'critical',
           nickname || ' froze (' || round(avg_temp::numeric, 1) || '°C). The label says not to use frozen product.'
    FROM last WHERE zone = 'Frozen'
    UNION ALL
    SELECT id, 'above_limit', 'critical',
           nickname || ' is at ' || round(avg_temp::numeric, 1) || '°C, above its labeled limit: using '
           || round((burn_rate(avg_temp, model) * 100)::numeric, 1) || '% of its life budget per hour.'
    FROM last WHERE zone = 'Above labeled limit'
    UNION ALL
    SELECT id, 'budget_exhausted', 'critical', nickname || ' has used its entire life budget.'
    FROM last WHERE used >= 1
    UNION ALL
    SELECT id, 'budget_low', 'warning',
           nickname || ' has ' || round(((1 - used) * 100)::numeric, 0) || '% of its life budget left.'
    FROM last WHERE used >= 0.5 AND used < 1
    UNION ALL
    SELECT id, 'sensor_silent', 'warning',
           nickname || ' sensor has been silent since '
           || to_char(bucket AT TIME ZONE 'America/Detroit', 'FMHH12:MI AM') || '. Exposure is unknown.'
    FROM last WHERE bucket < now() - INTERVAL '30 minutes'
    UNION ALL
    SELECT l.id, 'outage', 'warning',
           l.nickname || ' is inside ' || o.name || '. Power expected back around '
           || to_char(o.est_restore_at AT TIME ZONE 'America/Detroit', 'FMHH12:MI AM') || '.'
    FROM last l JOIN outages o ON o.active AND ST_Contains(o.area, l.geom)
    UNION ALL
    SELECT id, 'expired', 'critical',
           nickname || ' passed its expiration date (' || to_char(expires_on, 'FMMon FMDD, YYYY') || ').'
    FROM last WHERE expires_on IS NOT NULL AND expires_on < current_date
    UNION ALL
    SELECT id, 'in_use_over', 'critical',
           nickname || ' was opened ' || (current_date - opened_at::date) || ' days ago; the label allows '
           || round((model->>'in_use_days')::numeric) || ' days after opening.'
    FROM last WHERE opened_at IS NOT NULL AND coalesce((model->>'in_use_days')::numeric, 0) > 0
      AND opened_at + make_interval(days => round((model->>'in_use_days')::numeric)::int) < now()
    UNION ALL
    SELECT id, 'in_use_ending', 'warning',
           nickname || ' must be used by '
           || to_char(opened_at + make_interval(days => round((model->>'in_use_days')::numeric)::int), 'FMMon FMDD')
           || ' (' || round((model->>'in_use_days')::numeric) || ' days after opening).'
    FROM last WHERE opened_at IS NOT NULL AND coalesce((model->>'in_use_days')::numeric, 0) > 0
      AND opened_at + make_interval(days => round((model->>'in_use_days')::numeric)::int)
          BETWEEN now() AND now() + INTERVAL '3 days'
    UNION ALL
    -- early warning: still inside its range, but a steady warming trend reaches the label's max within an hour
    SELECT id, 'warming_trend', 'warning',
           nickname || ' is warming ' || round(slope::numeric, 1) || '°C per hour (now ' || round(last_temp::numeric, 1)
           || '°C). At this rate it leaves its labeled range (' || (model->>'target_max_c') || '°C) in about '
           || greatest(1, ceil(((model->>'target_max_c')::float8 - last_temp) / slope * 60))::int || ' min.'
    FROM last WHERE zone = 'Labeled storage' AND bucket >= now() - INTERVAL '15 minutes'
      AND trend_n >= 4 AND slope >= 1 AND coalesce(r2, 0) >= 0.6
      AND last_temp <= (model->>'target_max_c')::float8
      AND ((model->>'target_max_c')::float8 - last_temp) / slope <= 1
    UNION ALL
    -- early warning: a "do not freeze" medicine cooling toward freezing within an hour
    SELECT id, 'freeze_risk', 'warning',
           nickname || ' is cooling ' || round((-slope)::numeric, 1) || '°C per hour (now ' || round(last_temp::numeric, 1)
           || '°C). At this rate it reaches freezing in about '
           || greatest(1, ceil((last_temp - (model->>'freeze_c')::float8) / -slope * 60))::int || ' min.'
    FROM last WHERE zone <> 'Frozen' AND coalesce((model->>'freeze_discard')::bool, false)
      AND bucket >= now() - INTERVAL '15 minutes'
      AND trend_n >= 4 AND slope <= -1 AND coalesce(r2, 0) >= 0.6
      AND last_temp > (model->>'freeze_c')::float8
      AND (last_temp - (model->>'freeze_c')::float8) / -slope <= 1
$$;

-- Scheduled by add_job every minute: opens new alerts, refreshes messages, resolves cleared ones.
CREATE OR REPLACE PROCEDURE check_alerts(job_id INT DEFAULT 0, config JSONB DEFAULT NULL)
LANGUAGE plpgsql AS $$
BEGIN
    DROP TABLE IF EXISTS _cond;
    CREATE TEMP TABLE _cond ON COMMIT DROP AS SELECT * FROM current_conditions();
    UPDATE alerts a SET resolved_at = now()
    WHERE a.resolved_at IS NULL
      AND NOT EXISTS (SELECT 1 FROM _cond c WHERE c.item_id = a.item_id AND c.kind = a.kind);
    INSERT INTO alerts (item_id, kind, severity, message)
    SELECT c.item_id, c.kind, c.severity, c.message FROM _cond c
    ON CONFLICT (item_id, kind) WHERE resolved_at IS NULL
    DO UPDATE SET message = EXCLUDED.message, severity = EXCLUDED.severity;
END $$;
