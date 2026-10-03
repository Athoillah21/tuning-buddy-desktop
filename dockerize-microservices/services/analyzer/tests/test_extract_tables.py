"""
Tables to clone into the test schema: the regex over-matches, the EXPLAIN plan decides.
"""
from app.query_analyzer import QueryValidator


def _plan(*relations):
    return {"Plan": {"Node Type": "Append", "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": name} for name in relations
    ]}}


def test_extract_from_is_not_a_table():
    """EXTRACT(YEAR FROM order_date) made the optimizer try to clone a table named order_date."""
    query = ("SELECT id FROM large_orders "
             "WHERE EXTRACT(YEAR FROM order_date) = 2026 AND EXTRACT(MONTH FROM order_date) = 3;")
    assert "order_date" in QueryValidator.extract_tables(query)  # the regex alone is fooled
    assert QueryValidator.extract_tables(query, _plan("large_orders")) == ["large_orders"]


def test_cte_names_are_dropped():
    query = "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent JOIN customers c ON true;"
    tables = QueryValidator.extract_tables(query, {"Plan": {"Node Type": "Nested Loop", "Plans": [
        {"Node Type": "CTE Scan", "CTE Name": "recent"},
        {"Node Type": "Seq Scan", "Relation Name": "orders"},
        {"Node Type": "Seq Scan", "Relation Name": "customers"},
    ]}})
    assert sorted(tables) == ["customers", "orders"]


def test_schema_qualified_names_keep_the_query_spelling():
    query = "SELECT * FROM openidm.genericobjects g JOIN public.users u ON u.id = g.owner;"
    tables = QueryValidator.extract_tables(query, _plan("genericobjects", "users"))
    assert sorted(tables) == ["openidm.genericobjects", "public.users"]


def test_without_a_plan_the_regex_result_is_unchanged():
    assert QueryValidator.extract_tables("SELECT * FROM orders;") == ["orders"]
