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

-- Per-bucket timeline with running budget use. p_raw = true bypasses the continuous
-- aggregate and recomputes from raw readings (the "ground truth" for the late-data demo).
CREATE OR REPLACE FUNCTION item_timeline(p_item INT, p_raw BOOLEAN DEFAULT FALSE)
RETURNS TABLE (bucket TIMESTAMPTZ, avg_temp DOUBLE PRECISION, min_temp DOUBLE PRECISION,
               max_temp DOUBLE PRECISION, n BIGINT, zone TEXT, burn DOUBLE PRECISION, used DOUBLE PRECISION)
LANGUAGE sql STABLE AS $$
    WITH m AS (
        SELECT p.model, i.started_at FROM items i JOIN products p ON p.id = i.product_id WHERE i.id = p_item
    ),
    b AS (
        SELECT r.bucket, r.avg_temp, r.min_temp, r.max_temp, r.n
        FROM readings_5m r WHERE NOT p_raw AND r.item_id = p_item
        UNION ALL
        SELECT time_bucket('5 minutes', x.ts), avg(x.temp_c), min(x.temp_c), max(x.temp_c), count(*)
        FROM readings x WHERE p_raw AND x.item_id = p_item
        GROUP BY 1
    )
    SELECT b.bucket, b.avg_temp, b.min_temp, b.max_temp, b.n,
           zone_of(b.avg_temp, b.min_temp, m.model),
           bucket_burn(b.avg_temp, b.min_temp, m.model, 5 / 60.0),
           least(1.0, sum(bucket_burn(b.avg_temp, b.min_temp, m.model, 5 / 60.0)) OVER (ORDER BY b.bucket))
    FROM b, m
    WHERE b.bucket >= m.started_at
    ORDER BY b.bucket
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
