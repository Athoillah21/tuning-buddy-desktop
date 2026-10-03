"""EXPLAIN ANALYZE executes what it measures: only one read-only statement, in a read-only transaction."""
from contextlib import contextmanager

import pytest

from app import sql_guard
from app.db_connector import DBConnector


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders WHERE id = 1",
    "select 1;",
    "  -- a comment\n WITH x AS (SELECT 1) SELECT * FROM x",
    "(SELECT 1) UNION (SELECT 2)",
    "SELECT ';' AS semicolon_in_a_string",
])
def test_plain_queries_are_analyzable(sql):
    assert sql_guard.check_analyzed_query(sql)


@pytest.mark.parametrize("sql, reason", [
    ("SELECT 1; DROP TABLE orders", "one query"),
    ("SELECT 1; COMMIT; DROP TABLE orders; COMMIT", "one query"),
    ("UPDATE orders SET total = 0", "read-only"),
    ("DELETE FROM orders", "read-only"),
    ("INSERT INTO orders VALUES (1)", "read-only"),
    ("DROP TABLE orders", "read-only"),
    ("EXPLAIN ANALYZE DELETE FROM orders", "read-only"),
    ("", "no query"),
])
def test_writes_and_several_statements_are_refused(sql, reason):
    with pytest.raises(sql_guard.GuardError, match=reason):
        sql_guard.check_analyzed_query(sql)


class FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, statement, params=None):
        self.log.append(statement)

    def fetchone(self):
        return [[{"Plan": {}, "Execution Time": 1.5, "Planning Time": 0.5}]]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, log):
        self.log, self.readonly, self.rolled_back = log, None, False

    def set_session(self, readonly=None):
        self.readonly = readonly

    def cursor(self):
        return FakeCursor(self.log)

    def rollback(self):
        self.rolled_back = True


@pytest.fixture
def db(monkeypatch):
    connector = DBConnector.__new__(DBConnector)
    connector.connections = []
    connector.log = []

    @contextmanager
    def get_connection():
        conn = FakeConnection(connector.log)
        connector.connections.append(conn)
        yield conn

    monkeypatch.setattr(connector, "get_connection", get_connection)
    return connector


def test_runs_in_a_read_only_transaction_and_rolls_back(db):
    result = db.execute_explain_analyze("SELECT * FROM orders;")
    assert result["success"] and result["execution_time"] == 1.5
    conn = db.connections[0]
    assert conn.readonly is True and conn.rolled_back
    assert db.log[-1] == "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM orders"


def test_an_ai_rewrite_with_extra_statements_never_reaches_the_database(db):
    result = db.execute_explain_analyze("SELECT 1; COMMIT; DROP TABLE orders")
    assert not result["success"] and result["refused"]
    assert result["error"].startswith("Refused:")
    assert db.connections == []  # not even connected


def test_the_median_stops_at_a_refusal(db):
    result = db.execute_explain_analyze_median("DELETE FROM orders", repeats=3)
    assert result["refused"] and db.connections == []
