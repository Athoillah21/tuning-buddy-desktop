"""
Index statements come from an AI provider, so they must never be able to touch
the database being analyzed.
"""
import pytest

from app.db_connector import qualify_index_statement

SCHEMA = "temp_test_abc123"


def target_of(statement: str) -> str:
    rewritten, _ = qualify_index_statement(statement, SCHEMA)
    return rewritten


@pytest.mark.parametrize("statement", [
    "CREATE INDEX idx_orders_customer ON orders (customer_id)",
    "create index idx_orders_customer on orders(customer_id)",
    "CREATE INDEX idx ON openidm.genericobjects (objecttypes_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx ON orders (customer_id)",
    'CREATE INDEX idx ON "Orders" (customer_id)',
    "CREATE INDEX idx ON ONLY orders (customer_id)",
    "CREATE INDEX idx ON orders ((json_extract_path_text(fullobject, 'startTime')))",
    "CREATE INDEX idx_orders_customer ON orders (customer_id);",
])
def test_rewrites_target_into_the_temp_schema(statement):
    # Keyword case and an ONLY modifier are preserved, so match on the qualified target
    assert f'"{SCHEMA}"."' in target_of(statement)


def test_unqualified_table_is_confined_to_temp_schema():
    # The bug this guards: unqualified names used to be left alone and hit the real table
    rewritten, table = qualify_index_statement("CREATE INDEX idx ON orders (customer_id)", SCHEMA)
    assert rewritten == f'CREATE INDEX idx ON "{SCHEMA}"."orders" (customer_id)'
    assert table == "orders"


def test_schema_qualified_table_is_redirected():
    rewritten, table = qualify_index_statement("CREATE INDEX idx ON openidm.genericobjects (id)", SCHEMA)
    assert "openidm" not in rewritten
    assert table == "genericobjects"


def test_expression_index_body_is_preserved():
    statement = "CREATE INDEX idx ON orders ((json_extract_path_text(fullobject, 'startTime')))"
    assert statement[statement.index("("):] in target_of(statement)


@pytest.mark.parametrize("statement", [
    "ALTER TABLE orders ADD COLUMN x int",
    "DROP TABLE customers",
    "CREATE INDEX idx ON orders (customer_id); DROP TABLE customers",
    "CREATE TABLE evil (id int)",
    "",
    "   ",
    None,
])
def test_refuses_anything_that_is_not_a_single_create_index(statement):
    with pytest.raises(ValueError):
        qualify_index_statement(statement, SCHEMA)
