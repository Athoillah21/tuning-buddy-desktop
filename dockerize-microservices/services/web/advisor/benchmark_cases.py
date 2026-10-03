"""
Benchmark cases for the demo database.

Each case targets a different optimizer behaviour: a query that is already optimal,
a sequential scan an index can fix, joins, sorting, a pattern search no index can help,
and the two extension workloads (PostGIS, pgvector). `requires` names the PostgreSQL
extension a case needs, so unsupported cases are skipped rather than failing.
"""

BENCHMARK_CASES = [
    {
        "slug": "simple-pk",
        "title": "Primary key lookup (already optimal)",
        "expectation": "Index scan already; no recommendation should beat it",
        "requires": None,
        "query": "SELECT * FROM orders WHERE id = 1234;",
    },
    {
        "slug": "simple-filter",
        "title": "Equality filter on 1M rows",
        "expectation": "Sequential scan replaced by a b-tree index",
        "requires": None,
        "query": "SELECT * FROM large_orders WHERE customer_email = 'user12345@example.com';",
    },
    {
        "slug": "range-filter",
        "title": "Range filter with two predicates",
        "expectation": "Composite or partial index on amount and order_date",
        "requires": None,
        "query": (
            "SELECT id, customer_email, amount, order_date FROM large_orders "
            "WHERE amount > 900 AND order_date > NOW() - INTERVAL '30 days' "
            "ORDER BY amount DESC LIMIT 50;"
        ),
    },
    {
        "slug": "join-aggregate",
        "title": "Join with grouping and aggregation",
        "expectation": "Indexes on the join and filter columns; hash join retained",
        "requires": None,
        "query": (
            "SELECT c.email, COUNT(o.id) AS order_count, SUM(o.total_amount) AS total_spent "
            "FROM customers c JOIN orders o ON o.customer_id = c.id "
            "WHERE c.country = 'Indonesia' AND o.status = 'delivered' "
            "GROUP BY c.email ORDER BY total_spent DESC LIMIT 20;"
        ),
    },
    {
        "slug": "three-table-join",
        "title": "Three-table join",
        "expectation": "Foreign key indexes on order_items",
        "requires": None,
        "query": (
            "SELECT p.category, COUNT(*) AS items, SUM(oi.quantity * oi.unit_price) AS revenue "
            "FROM order_items oi "
            "JOIN orders o ON o.id = oi.order_id "
            "JOIN products p ON p.id = oi.product_id "
            "WHERE o.status = 'delivered' "
            "GROUP BY p.category ORDER BY revenue DESC;"
        ),
    },
    {
        "slug": "sort-limit",
        "title": "Top-N by date",
        "expectation": "Index on order_date removes the sort",
        "requires": None,
        "query": "SELECT id, customer_email, amount FROM large_orders ORDER BY order_date DESC LIMIT 50;",
    },
    {
        "slug": "like-wildcard",
        "title": "Leading-wildcard pattern search",
        "expectation": "No b-tree index can help; a trigram index or nothing",
        "requires": None,
        "query": "SELECT id, customer_email FROM large_orders WHERE customer_email LIKE '%12345%' LIMIT 20;",
    },
    {
        "slug": "spatial-knn",
        "title": "PostGIS nearest neighbour",
        "expectation": "GiST index on geom turns the scan into an index scan",
        "requires": "postgis",
        "query": (
            "SELECT id, name, category FROM places "
            "ORDER BY geom <-> ST_SetSRID(ST_MakePoint(106.8456, -6.2088), 4326) LIMIT 10;"
        ),
    },
    {
        "slug": "spatial-radius",
        "title": "PostGIS radius search",
        "expectation": "GiST index supporting ST_DWithin",
        "requires": "postgis",
        "query": (
            "SELECT category, COUNT(*) FROM places "
            "WHERE ST_DWithin(geom::geography, "
            "ST_SetSRID(ST_MakePoint(106.8456, -6.2088), 4326)::geography, 3000) "
            "GROUP BY category;"
        ),
    },
    {
        "slug": "vector-knn",
        "title": "pgvector similarity search",
        "expectation": "HNSW or IVFFlat index replaces the exhaustive scan",
        "requires": "vector",
        "query": (
            "SELECT id, title FROM documents "
            "ORDER BY embedding <-> (SELECT embedding FROM documents WHERE id = 1) LIMIT 10;"
        ),
    },
    {
        "slug": "vector-filtered",
        "title": "pgvector search with a filter",
        "expectation": "Combined filter index plus ANN index",
        "requires": "vector",
        "query": (
            "SELECT id, title FROM documents WHERE category = 'manual' "
            "ORDER BY embedding <-> (SELECT embedding FROM documents WHERE id = 1) LIMIT 10;"
        ),
    },

    # --- Non-sargable predicates: the column is wrapped, so a plain index cannot be used ---
    {
        "slug": "function-on-column",
        "title": "Function applied to the filtered column",
        "expectation": "Expression index on lower(customer_email)",
        "requires": None,
        "query": "SELECT id, amount FROM large_orders WHERE lower(customer_email) = 'user12345@example.com';",
    },
    {
        "slug": "date-extract",
        "title": "EXTRACT() on a timestamp filter",
        "expectation": "Rewrite to a sargable date range plus an index on order_date",
        "requires": None,
        "query": (
            "SELECT id, amount FROM large_orders "
            "WHERE EXTRACT(YEAR FROM order_date) = 2026 AND EXTRACT(MONTH FROM order_date) = 3 AND amount > 990;"
        ),
    },
    {
        "slug": "implicit-cast",
        "title": "Casting the column instead of the constant",
        "expectation": "Rewrite id::text = '...' to id = ..., plus an index on id",
        "requires": None,
        "query": "SELECT customer_email, amount FROM large_orders WHERE id::text = '654321';",
    },
    {
        "slug": "prefix-like",
        "title": "Prefix LIKE search",
        "expectation": "B-tree index usable for LIKE 'prefix%' (C collation or text_pattern_ops)",
        "requires": None,
        "query": "SELECT id, customer_email FROM large_orders WHERE customer_email LIKE 'user9999%';",
    },

    # --- Query shape: correct results, poor formulation ---
    {
        "slug": "or-across-columns",
        "title": "OR across two different columns",
        "expectation": "Two indexes combined by a BitmapOr, or a UNION rewrite",
        "requires": None,
        "query": (
            "SELECT id, customer_email, product_name FROM large_orders "
            "WHERE customer_email = 'user777@example.com' OR product_name = 'Product 42';"
        ),
    },
    {
        "slug": "not-in-subquery",
        "title": "NOT IN over a large subquery",
        "expectation": "NOT EXISTS anti-join plus an index on the subquery column",
        "requires": None,
        "query": (
            "SELECT c.id, c.email FROM customers c WHERE c.email NOT IN "
            "(SELECT customer_email FROM large_orders WHERE country = 'Indonesia');"
        ),
    },
    {
        "slug": "correlated-subquery",
        "title": "Correlated subquery in the SELECT list",
        "expectation": "Index on large_orders(customer_email, order_date) or a join/LATERAL rewrite",
        "requires": None,
        "query": (
            "SELECT c.id, c.email, (SELECT MAX(lo.order_date) FROM large_orders lo "
            "WHERE lo.customer_email = c.email) AS last_order FROM customers c WHERE c.city = 'Jakarta';"
        ),
    },
    {
        "slug": "deep-offset",
        "title": "Deep OFFSET pagination",
        "expectation": "Index on order_date; keyset pagination instead of OFFSET",
        "requires": None,
        "query": (
            "SELECT id, customer_email, amount FROM large_orders "
            "ORDER BY order_date DESC OFFSET 200000 LIMIT 20;"
        ),
    },
    {
        "slug": "union-dedupe",
        "title": "UNION of two scans of the same table",
        "expectation": "Single scan with IN, or UNION ALL when duplicates are acceptable",
        "requires": None,
        "query": (
            "SELECT customer_email FROM large_orders WHERE country = 'Japan' AND amount > 995 UNION "
            "SELECT customer_email FROM large_orders WHERE country = 'Brazil' AND amount > 995;"
        ),
    },
    {
        "slug": "self-join",
        "title": "Self-join on an unindexed column",
        "expectation": "Index on customer_email so the join can use it",
        "requires": None,
        "query": (
            "SELECT a.id, a.customer_email, b.amount AS related_amount "
            "FROM large_orders a JOIN large_orders b ON a.customer_email = b.customer_email AND a.id <> b.id "
            "WHERE a.country = 'Indonesia' AND a.amount > 998 LIMIT 100;"
        ),
    },
    {
        "slug": "covering-index",
        "title": "Multi-column filter returning two columns",
        "expectation": "Composite index, ideally covering (INCLUDE) for an index-only scan",
        "requires": None,
        "query": (
            "SELECT customer_email, amount FROM large_orders "
            "WHERE country = 'Indonesia' AND city = 'Jakarta' AND order_status = 'pending';"
        ),
    },
    {
        "slug": "top-n-per-group",
        "title": "Top 3 per group with a window function",
        "expectation": "Index on (country, amount DESC) or a LATERAL rewrite",
        "requires": None,
        "query": (
            "SELECT * FROM (SELECT country, customer_email, amount, "
            "row_number() OVER (PARTITION BY country ORDER BY amount DESC) AS rn FROM large_orders) t "
            "WHERE rn <= 3;"
        ),
    },

    # --- Honesty checks: little or nothing should help ---
    {
        "slug": "low-selectivity",
        "title": "Filter matching 20% of the table",
        "expectation": "Sequential scan is already right; no significant improvement",
        "requires": None,
        "query": "SELECT id, amount FROM large_orders WHERE order_status = 'pending';",
    },
    {
        "slug": "count-distinct",
        "title": "COUNT(DISTINCT) over the whole table",
        "expectation": "Must read every row; at most a modest gain",
        "requires": None,
        "query": "SELECT country, COUNT(DISTINCT customer_email) FROM large_orders GROUP BY country;",
    },

    # --- Search and semi-structured data ---
    {
        "slug": "full-text",
        "title": "Full-text search without a GIN index",
        "expectation": "GIN index on to_tsvector('english', notes)",
        "requires": None,
        "query": (
            "SELECT id, notes FROM large_orders "
            "WHERE to_tsvector('english', notes) @@ to_tsquery('english', 'abcde:*');"
        ),
    },
    {
        "slug": "jsonb-containment",
        "title": "JSONB containment (@>)",
        "expectation": "GIN index on payload (jsonb_path_ops)",
        "requires": None,
        "query": (
            "SELECT id, created_at FROM events "
            "WHERE payload @> '{\"type\": \"refund\", \"device\": \"ios\", \"country\": \"JP\"}';"
        ),
    },
    {
        "slug": "jsonb-field",
        "title": "Filter on a JSONB field",
        "expectation": "Expression index on ((payload->>'user_id'))",
        "requires": None,
        "query": "SELECT id, payload->>'type' AS type FROM events WHERE payload->>'user_id' = '4242';",
    },

    # --- Partitioned tables ---
    {
        "slug": "partition-pruning-blocked",
        "title": "date_trunc() on the partition key",
        "expectation": "Rewrite to a range on recorded_at so PostgreSQL scans one partition, not twelve",
        "requires": None,
        "query": (
            "SELECT sensor_id, AVG(temperature) FROM measurements "
            "WHERE date_trunc('month', recorded_at) = date_trunc('month', NOW() - INTERVAL '2 months') "
            "GROUP BY sensor_id ORDER BY sensor_id LIMIT 20;"
        ),
    },
    {
        "slug": "partitioned-index",
        "title": "Filter on a partitioned table with no index",
        "expectation": "Index on the partitioned parent; fit check explains building it per partition",
        "requires": None,
        "query": "SELECT id, recorded_at, temperature FROM measurements WHERE sensor_id = 1234 AND status = 'error';",
    },

    # --- More extension workloads ---
    {
        "slug": "spatial-bbox",
        "title": "PostGIS points inside a bounding box",
        "expectation": "GiST index on geom",
        "requires": "postgis",
        "query": (
            "SELECT id, name FROM places "
            "WHERE ST_Within(geom, ST_MakeEnvelope(106.80, -6.25, 106.82, -6.23, 4326));"
        ),
    },
    {
        "slug": "spatial-transform",
        "title": "ST_Transform applied to the column",
        "expectation": "Transform the constant instead, or index the expression",
        "requires": "postgis",
        "query": (
            "SELECT id, name FROM places WHERE ST_DWithin(ST_Transform(geom, 3857), "
            "ST_Transform(ST_SetSRID(ST_MakePoint(106.8456, -6.2088), 4326), 3857), 500);"
        ),
    },
    {
        "slug": "vector-cosine",
        "title": "pgvector cosine distance (<=>)",
        "expectation": "HNSW index built with vector_cosine_ops, matching the operator",
        "requires": "vector",
        "query": (
            "SELECT id, title FROM documents "
            "ORDER BY embedding <=> (SELECT embedding FROM documents WHERE id = 42) LIMIT 10;"
        ),
    },
]


def case_by_slug(slug: str):
    for case in BENCHMARK_CASES:
        if case["slug"] == slug:
            return case
    return None


# ---------------------------------------------------------------------------
# What each case teaches: shown when a case is opened on the Test cases page.
# (slug: (why it is slow and what fixes it, the demo tables it reads))
# ---------------------------------------------------------------------------
EXPLANATIONS = {
    "simple-pk": (
        "A lookup by primary key is already an index scan that takes a fraction of a millisecond. "
        "This is the control case: any \"improvement\" the AI claims here is measurement noise, and "
        "Tuning Buddy should say no action is needed.",
        ["orders"]),
    "simple-filter": (
        "There is no index on customer_email, so PostgreSQL reads all 1M rows of large_orders to find "
        "a handful. A plain b-tree index on customer_email turns that into a direct lookup.",
        ["large_orders"]),
    "range-filter": (
        "Two range conditions plus ORDER BY amount with LIMIT: without an index PostgreSQL scans and sorts "
        "everything. An index on (amount) or a partial index for recent orders lets it read the top rows "
        "in order and stop after 50.",
        ["large_orders"]),
    "join-aggregate": (
        "The join filters on customers.country and orders.status and joins on orders.customer_id, none of "
        "them indexed. Indexes on the join key and the filter columns cut the rows fed into the join and "
        "the GROUP BY; a hash join is still the right join method.",
        ["customers", "orders"]),
    "three-table-join": (
        "order_items is joined to orders and products through foreign keys that have no indexes "
        "(PostgreSQL never creates them automatically). Indexing order_items.order_id and product_id, "
        "plus orders.status, shrinks the work of every join.",
        ["order_items", "orders", "products"]),
    "sort-limit": (
        "\"Latest 50 orders\" sorts all 1M rows just to keep 50. An index on order_date DESC returns rows "
        "already in order, so PostgreSQL reads 50 index entries and stops.",
        ["large_orders"]),
    "like-wildcard": (
        "A leading % means no b-tree index can be used: the match can start anywhere, so every row is "
        "checked. Only a trigram index (pg_trgm GIN) helps; a plain index on customer_email does not, and "
        "Tuning Buddy should reject that suggestion.",
        ["large_orders"]),
    "spatial-knn": (
        "\"Nearest 10 places\" computes the distance to all 200k points and sorts them. A GiST index on geom "
        "lets the <-> operator walk the index outward from the point and stop after 10.",
        ["places"]),
    "spatial-radius": (
        "ST_DWithin on geography checks every point. A GiST index on geom::geography (the same expression "
        "the query uses) lets PostgreSQL find only the points inside the 3 km radius.",
        ["places"]),
    "vector-knn": (
        "A similarity search with no ANN index compares the query vector with all 20k embeddings. An HNSW "
        "(or IVFFlat) index with vector_l2_ops, matching the <-> operator, finds the nearest ones from a "
        "small part of the data.",
        ["documents"]),
    "vector-filtered": (
        "Similarity search plus a category filter: the filter alone does not order the rows, and an ANN "
        "index alone may return documents from other categories. An HNSW index plus an index on category "
        "(or a partial index per category) is the usual answer.",
        ["documents"]),
    "function-on-column": (
        "lower(customer_email) hides the column inside a function, so an ordinary index on customer_email "
        "cannot be used. An expression index on lower(customer_email) matches the query exactly.",
        ["large_orders"]),
    "date-extract": (
        "EXTRACT(YEAR/MONTH FROM order_date) runs for every row and cannot use an index on order_date. "
        "Rewriting it as order_date >= '2026-03-01' AND order_date < '2026-04-01' makes it indexable.",
        ["large_orders"]),
    "implicit-cast": (
        "id::text = '654321' casts the column, not the constant, so even an index on id is useless and "
        "every row is converted to text. Comparing id = 654321 directly lets an index on id answer it.",
        ["large_orders"]),
    "prefix-like": (
        "LIKE 'user9999%' only needs rows starting with a prefix, which a b-tree can find, but only with "
        "the C collation or text_pattern_ops. A plain index in a non-C locale is skipped by the planner.",
        ["large_orders"]),
    "or-across-columns": (
        "An OR across two different columns cannot use one index. Separate indexes on customer_email and "
        "product_name let PostgreSQL combine them with a BitmapOr, or the query can be split into a UNION.",
        ["large_orders"]),
    "not-in-subquery": (
        "NOT IN against a large subquery is planned badly (and behaves oddly with NULLs). NOT EXISTS turns "
        "it into an anti-join, and an index on large_orders(customer_email) makes each probe cheap.",
        ["customers", "large_orders"]),
    "correlated-subquery": (
        "The subquery runs once per Jakarta customer, each time scanning large_orders. An index on "
        "(customer_email, order_date) makes each run a single index lookup; a join or LATERAL rewrite "
        "also works.",
        ["customers", "large_orders"]),
    "deep-offset": (
        "OFFSET 200000 still produces and throws away 200,000 sorted rows. An index on order_date avoids "
        "the sort, and keyset pagination (WHERE order_date < last seen) avoids the skipping entirely.",
        ["large_orders"]),
    "union-dedupe": (
        "Two scans of the same table, then a sort to remove duplicates. One scan with country IN ('Japan', "
        "'Brazil') does it in a single pass; UNION ALL is cheaper still if duplicates are acceptable.",
        ["large_orders"]),
    "self-join": (
        "Joining large_orders to itself on customer_email without an index means hashing 1M rows. An index "
        "on customer_email lets each of the few filtered rows find its partners directly.",
        ["large_orders"]),
    "covering-index": (
        "Three equality filters and only two returned columns. A composite index on (country, city, "
        "order_status) INCLUDE (customer_email, amount) can answer it with an index-only scan, without "
        "touching the table.",
        ["large_orders"]),
    "top-n-per-group": (
        "The window function ranks all 1M rows before keeping 3 per country. An index on (country, amount "
        "DESC) or a LATERAL rewrite reads just the top 3 of each country.",
        ["large_orders"]),
    "low-selectivity": (
        "About 20% of rows are pending, so an index would be slower than reading the table sequentially. "
        "This is a control case: the right answer is that no index helps.",
        ["large_orders"]),
    "count-distinct": (
        "COUNT(DISTINCT) must look at every row and de-duplicate per country; no index can skip that work. "
        "Expect at most a modest gain, for example from an index-only scan.",
        ["large_orders"]),
    "full-text": (
        "to_tsvector() is computed for every row at query time. A GIN index on to_tsvector('english', notes) "
        "(the same expression) lets the @@ search use the index.",
        ["large_orders"]),
    "jsonb-containment": (
        "@> on JSONB has to open every payload without an index. A GIN index on payload, with "
        "jsonb_path_ops for containment-only queries, finds matching documents directly.",
        ["events"]),
    "jsonb-field": (
        "payload->>'user_id' extracts a field from every row. An expression index on ((payload->>'user_id')) "
        "matches the query and turns it into a lookup.",
        ["events"]),
    "partition-pruning-blocked": (
        "measurements is split into 12 monthly partitions, but date_trunc() on the partition key stops "
        "PostgreSQL from knowing which month is wanted, so it scans all 12. A plain range on recorded_at "
        "lets it prune down to one partition.",
        ["measurements"]),
    "partitioned-index": (
        "A filter on sensor_id and status across 600k partitioned rows. An index created on the parent is "
        "built on every partition; the fit check explains the cost of building it per partition.",
        ["measurements"]),
    "spatial-bbox": (
        "ST_Within a small box still tests all 200k points without a spatial index. A GiST index on geom lets "
        "PostgreSQL look only at points whose bounding boxes overlap the envelope.",
        ["places"]),
    "spatial-transform": (
        "ST_Transform(geom, ...) reprojects every row, so an index on geom cannot be used. Transform the "
        "constant point instead (or use geography), or index the transformed expression.",
        ["places"]),
    "vector-cosine": (
        "<=> is cosine distance; an HNSW index only helps if it is built with the matching vector_cosine_ops. "
        "An index built for L2 distance (<->) would be ignored by this query.",
        ["documents"]),
}

for _case in BENCHMARK_CASES:
    _case["explanation"], _case["tables"] = EXPLANATIONS.get(_case["slug"], ("", []))
