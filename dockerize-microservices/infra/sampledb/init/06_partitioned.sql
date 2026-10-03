-- ============================================
-- Partitioned benchmark data
-- 12 monthly range partitions, ~600k readings, deliberately WITHOUT indexes
-- ============================================
DROP TABLE IF EXISTS measurements CASCADE;

CREATE TABLE measurements (
    id BIGSERIAL,
    sensor_id INTEGER NOT NULL,
    recorded_at TIMESTAMP NOT NULL,
    temperature NUMERIC(5, 2),
    humidity NUMERIC(5, 2),
    status VARCHAR(20)
) PARTITION BY RANGE (recorded_at);

-- One partition per month, covering the 12 months up to the start of next month
DO $$
DECLARE
    month_start DATE := date_trunc('month', NOW())::date - INTERVAL '11 months';
BEGIN
    FOR i IN 0..11 LOOP
        EXECUTE format(
            'CREATE TABLE measurements_%s PARTITION OF measurements FOR VALUES FROM (%L) TO (%L)',
            to_char(month_start + (i || ' months')::INTERVAL, 'YYYY_MM'),
            month_start + (i || ' months')::INTERVAL,
            month_start + ((i + 1) || ' months')::INTERVAL
        );
    END LOOP;
END $$;

INSERT INTO measurements (sensor_id, recorded_at, temperature, humidity, status)
SELECT
    1 + (i % 2000),
    date_trunc('month', NOW()) - INTERVAL '11 months'
        + (random() * (date_trunc('month', NOW()) + INTERVAL '1 month'
                       - (date_trunc('month', NOW()) - INTERVAL '11 months'))),
    round((15 + random() * 20)::numeric, 2),
    round((30 + random() * 60)::numeric, 2),
    (ARRAY['ok', 'ok', 'ok', 'warning', 'error'])[1 + (i % 5)]
FROM generate_series(1, 600000) AS i;

ANALYZE measurements;

SELECT 'measurements: ' || count(*) || ' rows in '
       || (SELECT count(*) FROM pg_inherits WHERE inhparent = 'measurements'::regclass)
       || ' partitions, no indexes' AS partitioned_seed
FROM measurements;
