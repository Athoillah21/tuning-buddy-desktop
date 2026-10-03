-- ============================================
-- Spatial benchmark data (PostGIS)
-- 200k points around Jakarta, deliberately WITHOUT a spatial index
-- ============================================
CREATE EXTENSION IF NOT EXISTS postgis;

DROP TABLE IF EXISTS places CASCADE;

CREATE TABLE places (
    id SERIAL PRIMARY KEY,
    name TEXT,
    category VARCHAR(40),
    rating NUMERIC(2, 1),
    geom geometry(Point, 4326)
);

INSERT INTO places (name, category, rating, geom)
SELECT
    'Place ' || i,
    (ARRAY['cafe', 'shop', 'park', 'office', 'school'])[1 + (i % 5)],
    round((random() * 4 + 1)::numeric, 1),
    ST_SetSRID(ST_MakePoint(106.6 + random() * 0.6, -6.4 + random() * 0.5), 4326)
FROM generate_series(1, 200000) AS i;

-- Statistics only; no index, so the benchmark starts from a sequential scan
ANALYZE places;

SELECT 'places: ' || count(*) || ' rows, no spatial index' AS spatial_seed FROM places;
