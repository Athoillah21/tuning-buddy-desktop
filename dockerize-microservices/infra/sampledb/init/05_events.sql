-- ============================================
-- Semi-structured benchmark data (JSONB)
-- 300k events, deliberately WITHOUT a GIN or expression index
-- ============================================
DROP TABLE IF EXISTS events CASCADE;

CREATE TABLE events (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP,
    payload JSONB
);

INSERT INTO events (created_at, payload)
SELECT
    NOW() - ((i % 180) || ' days')::INTERVAL - ((i % 1440) || ' minutes')::INTERVAL,
    jsonb_build_object(
        'type', (ARRAY['view', 'click', 'cart', 'purchase', 'refund'])[1 + (i % 5)],
        'device', (ARRAY['ios', 'android', 'web', 'desktop'])[1 + (i % 4)],
        'user_id', 1 + (i % 50000),
        'country', (ARRAY['ID', 'US', 'JP', 'DE', 'BR', 'IN'])[1 + (i % 6)],
        'amount', round((random() * 300)::numeric, 2),
        'tags', (ARRAY['["promo", "new"]', '["returning"]', '["vip", "promo"]', '["organic"]'])[1 + (i % 4)]::jsonb
    )
FROM generate_series(1, 300000) AS i;

ANALYZE events;

SELECT 'events: ' || count(*) || ' rows, no JSONB index' AS events_seed FROM events;
