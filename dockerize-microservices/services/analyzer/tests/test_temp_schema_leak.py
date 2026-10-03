"""
The optimizer tests recommendations inside a throwaway schema. Nothing the user
is told to run may mention that schema - the DDL has to apply to their real tables.
"""
from app.optimizer import restore_user_tables

TABLES = ["customers", "orders"]
SCHEMA_TABLES = ["openidm.genericobjects"]


def test_rewrites_temp_schema_back_to_the_real_table():
    statement = "CREATE INDEX idx_orders_status ON temp_test_556038a6.orders (status, customer_id);"
    assert restore_user_tables(statement, TABLES) == (
        "CREATE INDEX idx_orders_status ON orders (status, customer_id);")


def test_restores_the_original_schema_qualified_name():
    statement = "CREATE INDEX idx ON temp_test_aaaaaaaa.genericobjects (objecttypes_id)"
    assert restore_user_tables(statement, SCHEMA_TABLES) == (
        "CREATE INDEX idx ON openidm.genericobjects (objecttypes_id)")


def test_handles_quoted_identifiers():
    statement = 'CREATE INDEX idx ON "temp_test_556038a6"."orders" (status)'
    assert restore_user_tables(statement, TABLES) == "CREATE INDEX idx ON orders (status)"


def test_rewrites_queries_as_well_as_ddl():
    query = "SELECT * FROM temp_test_556038a6.orders o JOIN temp_test_556038a6.customers c ON c.id = o.customer_id"
    assert restore_user_tables(query, TABLES) == (
        "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id")


def test_walks_nested_recommendation_structures():
    recommendation = {
        "description": "Index temp_test_556038a6.orders on status",
        "suggested_indexes": ["CREATE INDEX a ON temp_test_556038a6.orders (status)"],
        "optimization_history": [
            {"recommendation": {"suggested_indexes": ["CREATE INDEX b ON temp_test_556038a6.customers (country)"]}},
        ],
        "tested_execution_time": 1.25,
        "seq_scan_eliminated": True,
    }
    cleaned = restore_user_tables(recommendation, TABLES)
    assert cleaned["description"] == "Index orders on status"
    assert cleaned["suggested_indexes"] == ["CREATE INDEX a ON orders (status)"]
    assert cleaned["optimization_history"][0]["recommendation"]["suggested_indexes"] == [
        "CREATE INDEX b ON customers (country)"]
    # Non-text values pass through untouched
    assert cleaned["tested_execution_time"] == 1.25
    assert cleaned["seq_scan_eliminated"] is True


def test_leaves_ordinary_statements_alone():
    statement = "CREATE INDEX idx_orders_status ON orders (status)"
    assert restore_user_tables(statement, TABLES) == statement


def test_unknown_table_keeps_its_bare_name():
    """A table we never cloned still loses the temp schema prefix."""
    statement = "CREATE INDEX idx ON temp_test_556038a6.unexpected (col)"
    assert restore_user_tables(statement, TABLES) == "CREATE INDEX idx ON unexpected (col)"
