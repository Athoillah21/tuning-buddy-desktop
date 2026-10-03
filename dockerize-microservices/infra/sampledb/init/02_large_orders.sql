-- ============================================
-- SLOW QUERY TEST DATA
-- Creates 1 million rows of unoptimized data
-- ============================================

-- Drop existing test table if exists
DROP TABLE IF EXISTS large_orders CASCADE;

-- Create table WITHOUT indexes (intentionally slow)
CREATE TABLE large_orders (
    id SERIAL,                           -- No primary key!
    customer_email VARCHAR(255),
    product_name VARCHAR(255),
    order_status VARCHAR(50),
    order_date TIMESTAMP,
    amount DECIMAL(10,2),
    country VARCHAR(100),
    city VARCHAR(100),
    notes TEXT
);

-- Generate 1 million rows of random data
-- This will take a few minutes
INSERT INTO large_orders (customer_email, product_name, order_status, order_date, amount, country, city, notes)
SELECT
    'user' || (random() * 100000)::int || '@example.com',
    'Product ' || (random() * 500)::int,
    (ARRAY['pending', 'processing', 'shipped', 'delivered', 'cancelled'])[floor(random() * 5 + 1)::int],
    NOW() - (random() * 365)::int * INTERVAL '1 day',
    (random() * 1000)::decimal(10,2),
    (ARRAY['USA', 'UK', 'Germany', 'France', 'Japan', 'Australia', 'Canada', 'Brazil', 'India', 'Indonesia'])[floor(random() * 10 + 1)::int],
    (ARRAY['New York', 'London', 'Berlin', 'Paris', 'Tokyo', 'Sydney', 'Toronto', 'Sao Paulo', 'Mumbai', 'Jakarta'])[floor(random() * 10 + 1)::int],
    'Order notes: ' || md5(random()::text)
FROM generate_series(1, 1000000);

-- Verify row count
SELECT COUNT(*) as total_rows FROM large_orders;

-- ============================================
-- SLOW QUERIES FOR TESTING
-- These should take several seconds without indexes
-- ============================================

-- SLOW QUERY 1: Full table scan with string comparison
-- Expected: Very slow (no index on customer_email)
-- SELECT * FROM large_orders WHERE customer_email = 'user12345@example.com';

-- SLOW QUERY 2: Range query without index
-- Expected: Very slow (sequential scan)
-- SELECT * FROM large_orders WHERE amount > 500 AND order_date > NOW() - INTERVAL '30 days';

-- SLOW QUERY 3: Aggregation without proper indexes
-- Expected: Slow (full scan for grouping)
-- SELECT country, order_status, COUNT(*), SUM(amount) 
-- FROM large_orders 
-- WHERE order_date > NOW() - INTERVAL '90 days'
-- GROUP BY country, order_status;

-- SLOW QUERY 4: LIKE pattern matching (worst case)
-- Expected: Very slow (no index can help with leading wildcard)
-- SELECT * FROM large_orders WHERE customer_email LIKE '%12345%';

-- SLOW QUERY 5: JOIN simulation with self-join
-- Expected: Extremely slow without indexes
-- SELECT a.*, b.amount as related_amount
-- FROM large_orders a, large_orders b
-- WHERE a.customer_email = b.customer_email
-- AND a.id != b.id
-- AND a.country = 'Indonesia'
-- LIMIT 100;

ANALYZE large_orders;

SELECT 'Table created with 1 million rows. Now run the slow queries!' as message;
