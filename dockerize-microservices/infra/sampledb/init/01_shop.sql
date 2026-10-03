-- ============================================
-- Tuning Buddy Test Data - FAST VERSION
-- Creates ~5K rows total, runs in seconds
-- ============================================

-- Clean up if exists
DROP TABLE IF EXISTS order_items CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

-- ============================================
-- Create Tables (NO indexes - intentionally slow)
-- ============================================

CREATE TABLE customers (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255),
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    city VARCHAR(100),
    country VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'active'
);

CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255),
    category VARCHAR(100),
    price DECIMAL(10, 2),
    stock_quantity INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(id),
    order_date TIMESTAMP DEFAULT NOW(),
    status VARCHAR(50),
    total_amount DECIMAL(12, 2),
    shipping_city VARCHAR(100),
    shipping_country VARCHAR(100)
);

CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id),
    product_id INTEGER REFERENCES products(id),
    quantity INTEGER,
    unit_price DECIMAL(10, 2)
);

-- ============================================
-- Insert Sample Data (~5K rows, fast!)
-- ============================================

-- 1000 customers
INSERT INTO customers (email, first_name, last_name, city, country, status)
SELECT 
    'user' || i || '@example.com',
    'FirstName' || (i % 50),
    'LastName' || (i % 100),
    (ARRAY['New York', 'Los Angeles', 'Chicago', 'Houston', 'Jakarta', 'Singapore', 'Tokyo', 'London', 'Paris', 'Berlin'])[1 + (i % 10)],
    (ARRAY['USA', 'Indonesia', 'Singapore', 'Japan', 'UK', 'France', 'Germany'])[1 + (i % 7)],
    (ARRAY['active', 'inactive', 'pending'])[1 + (i % 3)]
FROM generate_series(1, 1000) AS i;

-- 200 products
INSERT INTO products (name, category, price, stock_quantity)
SELECT 
    'Product ' || i,
    (ARRAY['Electronics', 'Clothing', 'Books', 'Home', 'Sports', 'Toys', 'Food', 'Beauty'])[1 + (i % 8)],
    (random() * 500 + 10)::DECIMAL(10, 2),
    (random() * 500)::INTEGER
FROM generate_series(1, 200) AS i;

-- 2000 orders
INSERT INTO orders (customer_id, order_date, status, total_amount, shipping_city, shipping_country)
SELECT 
    1 + (i % 1000),
    NOW() - ((i % 365) || ' days')::INTERVAL,
    (ARRAY['pending', 'processing', 'shipped', 'delivered', 'cancelled'])[1 + (i % 5)],
    (random() * 500 + 20)::DECIMAL(12, 2),
    (ARRAY['New York', 'Los Angeles', 'Jakarta', 'Singapore', 'Tokyo', 'London'])[1 + (i % 6)],
    (ARRAY['USA', 'Indonesia', 'Singapore', 'Japan', 'UK'])[1 + (i % 5)]
FROM generate_series(1, 2000) AS i;

-- 5000 order items
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT 
    1 + (i % 2000),
    1 + (i % 200),
    1 + (i % 5),
    (random() * 100 + 10)::DECIMAL(10, 2)
FROM generate_series(1, 5000) AS i;

-- ============================================
-- Verify Data
-- ============================================
SELECT 'customers' AS table_name, COUNT(*) AS rows FROM customers
UNION ALL SELECT 'products', COUNT(*) FROM products
UNION ALL SELECT 'orders', COUNT(*) FROM orders
UNION ALL SELECT 'order_items', COUNT(*) FROM order_items;

-- ============================================
-- TEST QUERIES (Copy these to Tuning Buddy)
-- ============================================

-- [TEST QUERY 1] - Email lookup without index
-- SELECT o.id, o.order_date, o.status, o.total_amount
-- FROM orders o JOIN customers c ON c.id = o.customer_id
-- WHERE c.email = 'user500@example.com';

-- [TEST QUERY 2] - JOIN with filter on unindexed column  
-- SELECT c.email, p.name, oi.quantity, o.order_date
-- FROM order_items oi
-- JOIN orders o ON o.id = oi.order_id
-- JOIN customers c ON c.id = o.customer_id
-- JOIN products p ON p.id = oi.product_id
-- WHERE o.status = 'delivered' AND c.country = 'Indonesia';

-- [TEST QUERY 3] - Wildcard search
-- SELECT * FROM products WHERE name LIKE '%Product 5%';

-- Give the planner statistics; without these the join cases start from guesses
ANALYZE customers;
ANALYZE products;
ANALYZE orders;
ANALYZE order_items;
