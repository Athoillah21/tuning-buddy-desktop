"""Leftover test schemas: only Tuning Buddy's own, and only old ones, are removed (needs the :5433 test DB)."""
import time

import psycopg2
import pytest

from app.db_connector import TEMP_SCHEMA_MARKER, DBConnector

PARAMS = {"host": "127.0.0.1", "port": 5433, "user": "demo", "password": "demo", "dbname": "shop"}


@pytest.fixture
def cur():
    try:
        conn = psycopg2.connect(connect_timeout=3, **PARAMS)
    except psycopg2.OperationalError:
        pytest.skip("the local test PostgreSQL on :5433 is not running")
    conn.autocommit = True
    cursor = conn.cursor()
    yield cursor
    cursor.execute("DROP SCHEMA IF EXISTS temp_test_oldmark, temp_test_newmark, temp_test_nomark, "
                   "temp_test_other CASCADE")
    conn.close()


def _schema(cur, name, comment):
    cur.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
    cur.execute(f"CREATE SCHEMA {name}")
    cur.execute(f"CREATE TABLE {name}.t (a int)")
    if comment is not None:
        cur.execute(f"COMMENT ON SCHEMA {name} IS %s", (comment,))


def _exists(cur, name):
    cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (name,))
    return cur.fetchone() is not None


def test_only_old_marked_schemas_go(cur):
    _schema(cur, "temp_test_oldmark", f"{TEMP_SCHEMA_MARKER} {int(time.time()) - 3 * 3600}")
    _schema(cur, "temp_test_newmark", f"{TEMP_SCHEMA_MARKER} {int(time.time())}")
    _schema(cur, "temp_test_nomark", None)                          # someone else's: never touched
    _schema(cur, "temp_test_other", "a user's own comment")

    removed = DBConnector(PARAMS).drop_stale_temp_schemas(max_age_hours=1)

    assert removed == 1
    assert not _exists(cur, "temp_test_oldmark")
    assert _exists(cur, "temp_test_newmark") and _exists(cur, "temp_test_nomark") and _exists(cur, "temp_test_other")


def test_new_test_schemas_carry_the_marker(cur):
    db = DBConnector(PARAMS)
    assert db.create_temp_schema("temp_test_newmark")
    cur.execute("SELECT obj_description('temp_test_newmark'::regnamespace, 'pg_namespace')")
    comment = cur.fetchone()[0]
    assert comment.startswith(TEMP_SCHEMA_MARKER + " ") and abs(float(comment.split()[-1]) - time.time()) < 60
