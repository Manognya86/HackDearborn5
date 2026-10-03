-- 5-minute rollup, created after functions.sql because it calls burn_rate().
-- Real-time mode so fresh data shows up immediately; anything older than the materialization
-- watermark only changes after a refresh (the late-data problem).
--
-- avg_rate = mean of the PER-READING burn rate. Heat damage grows exponentially with temperature,
-- so burn(avg temperature) underestimates whenever a bucket mixes temperatures (Jensen's inequality);
-- a 1-minute spike to 45C inside a 4C bucket burns nothing by average, but does burn per reading.
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_5m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('5 minutes', r.ts) AS bucket,
       r.item_id,
       avg(r.temp_c) AS avg_temp,
       min(r.temp_c) AS min_temp,
       max(r.temp_c) AS max_temp,
       count(*)      AS n,
       avg(least(burn_rate(r.temp_c, p.model), 1e6)) AS avg_rate
FROM readings r
JOIN items i ON i.id = r.item_id
JOIN products p ON p.id = i.product_id
GROUP BY bucket, r.item_id
WITH NO DATA;

-- Daily rollup built ON the 5-minute rollup (hierarchical continuous aggregate): a 30-day exposure
-- calendar without rescanning raw readings. burn = budget used that day (each 5-minute bucket capped at 1).
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_1d
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 day', bucket) AS day,
       item_id,
       avg(avg_temp) AS avg_temp,
       min(min_temp) AS min_temp,
       max(max_temp) AS max_temp,
       sum(least(1.0, avg_rate * 5 / 60.0)) AS burn,
       count(*) AS buckets
FROM readings_5m
GROUP BY day, item_id
WITH NO DATA;
