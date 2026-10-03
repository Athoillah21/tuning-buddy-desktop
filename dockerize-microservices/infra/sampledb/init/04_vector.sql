-- ============================================
-- Vector benchmark data (pgvector)
-- 20k embeddings, deliberately WITHOUT an ANN index so every search scans the table
-- ============================================
CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS documents CASCADE;

CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    title TEXT,
    category VARCHAR(40),
    published_at TIMESTAMP DEFAULT NOW(),
    embedding vector(96)
);

INSERT INTO documents (title, category, published_at, embedding)
SELECT
    'Document ' || i,
    (ARRAY['policy', 'manual', 'report', 'faq'])[1 + (i % 4)],
    NOW() - ((i % 730) || ' days')::INTERVAL,
    (
        -- `i > 0` ties the subquery to the outer row. Uncorrelated, PostgreSQL runs it once
        -- (an InitPlan) and every document gets the same embedding.
        SELECT ('[' || string_agg(round(random()::numeric, 4)::text, ',') || ']')::vector
        FROM generate_series(1, 96)
        WHERE i > 0
    )
FROM generate_series(1, 20000) AS i;

ANALYZE documents;

SELECT 'documents: ' || count(*) || ' rows, no ANN index' AS vector_seed FROM documents;
