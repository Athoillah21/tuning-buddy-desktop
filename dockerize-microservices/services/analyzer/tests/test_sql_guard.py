"""
The SQL console must never run more than one statement, and only read-only ones.

Each attack below was tried against a live server: the guard has to see the same
statement boundaries PostgreSQL sees, whatever quoting trick hides a semicolon.
"""
import pytest

from app.sql_guard import GuardError, check_console_sql, split_statements

BACKSLASH = chr(92)


@pytest.mark.parametrize("sql_text", [
    "SELECT 1; DROP TABLE customers",
    "SELECT 1;\nDROP TABLE customers;",
    # E'\'' is a complete string, so everything after it is real SQL
    f"SELECT E'{BACKSLASH}''; COMMIT; SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE; "
    "DROP TABLE customers; --'",
    # With standard_conforming_strings = off, a backslash escapes in plain strings too
    f"SELECT 'a{BACKSLASH}'; COMMIT; DROP TABLE customers; --'",
    # $x$ inside an identifier is not a dollar quote
    "SELECT 1 AS a$x$; COMMIT; DROP TABLE customers; SELECT $x$",
])
def test_several_statements_are_refused(sql_text):
    with pytest.raises(GuardError, match="one statement"):
        check_console_sql(sql_text)


@pytest.mark.parametrize("sql_text", [
    "SELECT ';' AS semicolon",
    "SELECT 'it''s; fine'",
    'SELECT 1 AS "odd;name"',
    "SELECT $q$ ; not a statement ; $q$",
    "SELECT $$;$$",
    "SELECT 1 -- ; DROP TABLE customers",
    "SELECT 1 /* ; DROP TABLE customers; */",
    "SELECT 1 /* a /* nested */ ; still a comment */",
    "SELECT 1;",
    "  SELECT 1 ;;  ",
])
def test_semicolons_inside_quotes_and_comments_are_one_statement(sql_text):
    assert check_console_sql(sql_text)


@pytest.mark.parametrize("sql_text", [
    "DROP TABLE customers",
    "DELETE FROM customers",
    "COPY customers TO PROGRAM 'whoami'",
    "SET default_transaction_read_only = off",
    "DO $$ BEGIN DROP TABLE customers; END $$",
    "CALL cleanup()",
    "/* SELECT */ DELETE FROM customers",
])
def test_only_read_only_keywords_are_allowed(sql_text):
    with pytest.raises(GuardError, match="read-only"):
        check_console_sql(sql_text)


@pytest.mark.parametrize("sql_text", [
    "SELECT * FROM orders",
    "select 1",
    "WITH t AS (SELECT 1) SELECT * FROM t",
    "VALUES (1), (2)",
    "TABLE orders",
    "EXPLAIN SELECT 1",
    "SHOW work_mem",
    "(SELECT 1) UNION (SELECT 2)",
    "-- comment first\nSELECT 1",
])
def test_read_only_statements_pass(sql_text):
    assert check_console_sql(sql_text)


def test_empty_input_is_refused():
    with pytest.raises(GuardError, match="Enter a query"):
        check_console_sql("  -- nothing here\n ; ")


def test_split_keeps_setup_statements_apart():
    assert split_statements("SET work_mem = '64MB'; SELECT 1") == ["SET work_mem = '64MB'", "SELECT 1"]


# ---------------------------------------------------------------------------
# Write mode ("Allow changes" in the query tool)
# ---------------------------------------------------------------------------

def test_write_mode_splits_a_script():
    from app.sql_guard import check_write_script
    assert check_write_script("CREATE TABLE t (i int); INSERT INTO t VALUES (1);") == [
        "CREATE TABLE t (i int)", "INSERT INTO t VALUES (1)"]


@pytest.mark.parametrize("sql_text", [
    "BEGIN; DELETE FROM t",                 # the script is already one transaction
    "DELETE FROM t; COMMIT",
    "VACUUM t; SELECT 1",                   # cannot run inside a transaction with other statements
    "CREATE INDEX CONCURRENTLY i ON t (a); SELECT 1",
    BACKSLASH + "connect shop",             # psql commands from a dump file
    "SELECT 1;\n" + BACKSLASH + "set x 1",
    "COPY t FROM STDIN",
    "   ",
])
def test_write_mode_refuses(sql_text):
    from app.sql_guard import check_write_script
    with pytest.raises(GuardError):
        check_write_script(sql_text)


def test_statements_that_need_autocommit_run_alone():
    from app.sql_guard import check_write_script, needs_autocommit
    assert check_write_script("VACUUM ANALYZE t") == ["VACUUM ANALYZE t"]
    assert needs_autocommit("VACUUM ANALYZE t")
    assert needs_autocommit("create index concurrently i on t (a)")
    assert not needs_autocommit("CREATE INDEX i ON t (a)")
