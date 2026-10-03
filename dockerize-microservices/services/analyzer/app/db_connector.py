"""
Database connection manager for connecting to user's PostgreSQL databases.
Handles connection pooling and secure credential retrieval.
"""
import psycopg2
import re
from psycopg2 import sql, errors
from contextlib import contextmanager
from typing import Optional, Dict, Any, Tuple
import logging
import time

from . import catalog, result_check, sql_guard

logger = logging.getLogger(__name__)

# Comment on every test schema the optimizer creates; drop_stale_temp_schemas only removes those
TEMP_SCHEMA_MARKER = 'Tuning Buddy test copy: safe to drop'

def _marker_time(comment: str) -> float:
    """When a marked test schema was made; unreadable markers count as new, so they are kept."""
    try:
        return float(comment.rsplit(" ", 1)[1])
    except (AttributeError, IndexError, ValueError):
        return float("inf")


# Only CREATE INDEX may be applied while testing recommendations
CREATE_INDEX_RE = re.compile(r'^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b', re.IGNORECASE)
# The table the index is built on: "ON [ONLY] [schema.]table"
ON_TARGET_RE = re.compile(
    r'(?P<prefix>\bON\s+(?:ONLY\s+)?)(?:(?P<schema>"[^"]+"|[A-Za-z_][\w$]*)\s*\.\s*)?(?P<table>"[^"]+"|[A-Za-z_][\w$]*)',
    re.IGNORECASE,
)


def qualify_index_statement(index_statement: str, schema_name: str) -> Tuple[str, str]:
    """
    Rewrite a CREATE INDEX statement so it targets the temp schema, and refuse anything
    that cannot be rewritten. Recommendations come from an AI provider, so the database
    being analyzed must never be modified by them.

    Returns:
        Tuple of (rewritten statement, unqualified table name)

    Raises:
        ValueError: the statement is not a single CREATE INDEX, or its target cannot be
            confined to the temp schema.
    """
    statement = (index_statement or '').strip().rstrip(';').strip()
    if not statement:
        raise ValueError("empty index statement")
    if ';' in statement:
        raise ValueError("only a single CREATE INDEX statement is allowed")
    if not CREATE_INDEX_RE.match(statement):
        raise ValueError("only CREATE INDEX statements may be applied")

    match = ON_TARGET_RE.search(statement)
    if not match:
        raise ValueError("could not find the target table")

    table_name = match.group('table').strip('"')
    qualified = f'{match.group("prefix")}"{schema_name}"."{table_name}"'
    statement = statement[:match.start()] + qualified + statement[match.end():]

    # Safety net: never run a statement that still points somewhere else
    check = ON_TARGET_RE.search(statement)
    if not check or (check.group('schema') or '').strip('"') != schema_name:
        raise ValueError("statement does not target the temp schema")

    return statement, table_name


class DatabaseConnectionError(Exception):
    """Raised when database connection fails."""
    pass


class QueryExecutionError(Exception):
    """Raised when query execution fails."""
    pass


class DBConnector:
    """
    Manages connections to user's PostgreSQL databases.
    Uses context manager pattern for safe connection handling.
    """
    
    def __init__(self, connection_params: Dict[str, Any]):
        """
        Initialize with connection parameters.
        
        Args:
            connection_params: Dict with host, port, database, user, password, sslmode
        """
        self.connection_params = connection_params
        self._conn = None
    
    @contextmanager
    def get_connection(self):
        """
        Context manager for database connections.
        Ensures connections are properly closed.
        """
        # Only connecting is translated into DatabaseConnectionError. Errors raised while the
        # connection is in use (a statement timeout is an OperationalError too) propagate
        # unchanged, so callers can tell "could not connect" from "the query failed"
        try:
            conn = psycopg2.connect(**self.connection_params)
        except psycopg2.OperationalError as e:
            error_msg = str(e)
            if 'timeout' in error_msg.lower():
                raise DatabaseConnectionError(f"Connection timeout: {error_msg}")
            elif 'authentication' in error_msg.lower():
                raise DatabaseConnectionError(f"Authentication failed: Invalid username or password")
            elif 'could not connect' in error_msg.lower():
                raise DatabaseConnectionError(f"Could not connect to host: {error_msg}")
            else:
                raise DatabaseConnectionError(f"Connection error: {error_msg}")
        except Exception as e:
            raise DatabaseConnectionError(f"Unexpected error: {str(e)}")

        try:
            yield conn
        finally:
            conn.close()

    def test_connection(self) -> Tuple[bool, str]:
        """
        Test if the connection can be established.
        
        Returns:
            Tuple of (success: bool, message: str)
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version();")
                    version = cur.fetchone()[0]
                    return True, f"Connected successfully!\nPostgreSQL: {version}"
        except DatabaseConnectionError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Test failed: {str(e)}"
    
    def execute_explain_analyze(self, query: str, timeout_ms: int = 300000) -> Dict[str, Any]:
        """
        Execute EXPLAIN ANALYZE on a query and return the plan.
        
        Args:
            query: SQL query to analyze
            timeout_ms: Statement timeout in milliseconds
            
        Returns:
            Dict containing execution plan and metrics
        """
        # EXPLAIN ANALYZE executes the query, and it may be an AI's rewrite: one read-only
        # statement only, in a read-only transaction that is always rolled back. PostgreSQL then
        # also refuses what a keyword check can't see (data-modifying CTEs, nextval(), ...).
        try:
            statement = sql_guard.check_analyzed_query(query)
        except sql_guard.GuardError as e:
            return {'success': False, 'refused': True, 'error': f'Refused: {e}'}
        try:
            with self.get_connection() as conn:
                conn.set_session(readonly=True)
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = %s", (int(timeout_ms),))

                    # Run EXPLAIN ANALYZE with JSON output
                    cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}")

                    result = cur.fetchone()[0]
                    conn.rollback()  # nothing the query did is kept

                    # Extract key metrics
                    plan = result[0] if result else {}
                    execution_time = plan.get('Execution Time', 0)
                    planning_time = plan.get('Planning Time', 0)
                    
                    return {
                        'success': True,
                        'plan': plan,
                        'execution_time': execution_time,
                        'planning_time': planning_time,
                        'total_time': execution_time + planning_time,
                    }
                    
        except errors.QueryCanceled:
            return {
                'success': False,
                'error': f'Query exceeded timeout of {timeout_ms}ms',
            }
        except psycopg2.Error as e:
            return {
                'success': False,
                'error': f'Query execution error: {str(e)}',
            }
        except Exception as e:
            return {
                'success': False,
                'error': f'Unexpected error: {str(e)}',
            }
    
    def execute_explain_analyze_median(self, query: str, repeats: int = 3,
                                       timeout_ms: int = 300000, on_run=None) -> Dict[str, Any]:
        """
        Run EXPLAIN ANALYZE several times and keep the median run.

        A single execution is dominated by cache state, which is how a sub-millisecond
        query can appear to swing by 80%. The median run's plan is returned, along with
        every timing so callers can see the spread.
        """
        runs = []
        total = max(int(repeats), 1)
        for run in range(1, total + 1):
            if on_run:
                on_run(run, total)
            result = self.execute_explain_analyze(query, timeout_ms=timeout_ms)
            if not result['success']:
                return result
            runs.append(result)

        runs.sort(key=lambda run: run['execution_time'])
        median = dict(runs[len(runs) // 2])
        times = [run['execution_time'] for run in runs]
        median['execution_times'] = times
        median['execution_time_spread'] = max(times) - min(times)
        return median

    @contextmanager
    def readonly_cursor(self, timeout_ms: int):
        """A cursor in a read-only transaction: nothing run through it can change the database."""
        with self.get_connection() as conn:
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = %s", (int(timeout_ms),))
                yield cur

    def get_table_info(self, table_name: str) -> Dict[str, Any]:
        """
        Columns, indexes, sizes, row estimate, partitioning and activity of a table.

        Resolved like the query resolves it (search_path, optional schema), so a table of the
        same name in another schema - including the optimizer's temp_test_* copies - never
        leaks in. The first three keys are what the AI prompt has always received.
        """
        try:
            with self.readonly_cursor(timeout_ms=30000) as cur:
                stats = catalog.table_stats(cur, table_name)
                if stats is None:
                    return {'error': f'Table {table_name} not found'}
                columns = catalog.table_columns(cur, table_name)
            return {
                'columns': [{'name': c['name'], 'type': c['type'], 'nullable': 'YES' if c['nullable'] else 'NO'}
                            for c in columns],
                'row_count': stats['row_estimate'],
                **stats,
            }
        except Exception as e:
            logger.error(f"Error getting table info: {e}")
            return {'error': str(e)}

    def relation_kind(self, name: str) -> Optional[str]:
        """pg_class.relkind of a name as the database resolves it ('r', 'p', 'v', ...), or None."""
        try:
            with self.readonly_cursor(timeout_ms=10000) as cur:
                cur.execute("SELECT relkind FROM pg_class WHERE oid = to_regclass(%s)", (name,))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.warning(f"Could not resolve {name}: {e}")
            return None

    def result_fingerprint(self, query: str, timeout_ms: int) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        """
        (row count, order-insensitive hash of every row, error) for a SELECT, run read-only.
        Rows are compared as text, so bigint 125 and numeric 125 count as the same value.
        """
        setup, body = result_check.split_setup(query)
        try:
            with self.readonly_cursor(timeout_ms=timeout_ms) as cur:
                for statement in setup:
                    cur.execute(statement)
                cur.execute(
                    "SELECT count(*), md5(COALESCE(string_agg(md5(t::text), '' ORDER BY md5(t::text)), '')) "
                    f"FROM ({body}) AS t"
                )
                rows, digest = cur.fetchone()
                return rows, digest, None
        except errors.QueryCanceled:
            return None, None, f'timed out after {timeout_ms / 1000:.0f}s'
        except Exception as e:
            return None, None, str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__

    def temp_index_sizes(self, schema_name: str) -> list:
        """Every index built in a test schema, with its measured size and its table's size."""
        try:
            with self.readonly_cursor(timeout_ms=30000) as cur:
                # An index on a partitioned table weighs what its partitions' indexes weigh;
                # those per-partition children are folded into it rather than listed
                cur.execute("""
                    SELECT ic.relname AS name, am.amname AS method,
                           CASE WHEN ic.relkind = 'I' THEN
                               (SELECT COALESCE(SUM(pg_relation_size(p.relid)), 0)
                                FROM pg_partition_tree(ic.oid) p WHERE p.isleaf)
                           ELSE pg_relation_size(ic.oid) END::bigint AS bytes,
                           t.relname AS table,
                           CASE WHEN t.relkind = 'p' THEN
                               (SELECT COALESCE(SUM(pg_table_size(p.relid)), 0)
                                FROM pg_partition_tree(t.oid) p WHERE p.isleaf)
                           ELSE pg_table_size(t.oid) END::bigint AS table_bytes,
                           ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
                                 FROM generate_series(1, i.indnkeyatts) k) AS columns
                    FROM pg_index i
                    JOIN pg_class ic ON ic.oid = i.indexrelid
                    JOIN pg_am am ON am.oid = ic.relam
                    JOIN pg_class t ON t.oid = i.indrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    WHERE n.nspname = %s AND NOT ic.relispartition
                """, (schema_name,))
                names = [col.name for col in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.warning(f"Could not measure test indexes in {schema_name}: {e}")
            return []
    
    def get_schema_table_info(self, schema_name: str, table_name: str) -> Dict[str, Any]:
        """
        Get information about a table in a specific schema including columns, indexes, and row count.
        Useful for getting temp table structure.
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # Get column info for specific schema
                    cur.execute("""
                        SELECT column_name, data_type, is_nullable
                        FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                        ORDER BY ordinal_position;
                    """, (schema_name, table_name))
                    columns = cur.fetchall()
                    
                    # Get index info for specific schema
                    cur.execute("""
                        SELECT indexname, indexdef
                        FROM pg_indexes
                        WHERE schemaname = %s AND tablename = %s;
                    """, (schema_name, table_name))
                    indexes = cur.fetchall()
                    
                    # Get approximate row count
                    cur.execute("""
                        SELECT reltuples::bigint
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = %s AND c.relname = %s;
                    """, (schema_name, table_name))
                    row_count = cur.fetchone()
                    
                    return {
                        'table': f"{schema_name}.{table_name}",
                        'columns': [{'name': c[0], 'type': c[1], 'nullable': c[2]} for c in columns],
                        'indexes': [{'name': i[0], 'definition': i[1]} for i in indexes],
                        'row_count': row_count[0] if row_count else 0,
                    }
        except Exception as e:
            logger.error(f"Error getting schema table info: {e}")
            return {'error': str(e)}
    
    def create_temp_schema(self, schema_name: str) -> bool:
        """Create a temporary schema for testing optimizations."""
        try:
            with self.get_connection() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(schema_name)
                    ))
                    # The marker that lets a later run clean it up if this one never gets to
                    cur.execute(sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
                        sql.Identifier(schema_name), sql.Literal(f"{TEMP_SCHEMA_MARKER} {int(time.time())}")
                    ))
                    return True
        except Exception as e:
            logger.error(f"Error creating temp schema: {e}")
            return False

    def drop_stale_temp_schemas(self, max_age_hours: float = 1) -> int:
        """
        Remove test schemas a crashed or killed analysis left behind. Only schemas this service
        made are touched: named temp_test_*, carrying the marker comment with the time they were
        made, and older than max_age_hours (an analysis still running is far younger).
        """
        try:
            with self.get_connection() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT nspname, obj_description(oid, 'pg_namespace') FROM pg_namespace "
                        "WHERE starts_with(nspname, 'temp_test_') "
                        "AND starts_with(obj_description(oid, 'pg_namespace'), %s)",
                        (TEMP_SCHEMA_MARKER + " ",))
                    cutoff = time.time() - float(max_age_hours) * 3600
                    stale = [name for name, comment in cur.fetchall() if _marker_time(comment) < cutoff]
                    for name in stale:
                        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))
                        logger.info("Removed %s, left behind by an earlier analysis", name)
                    return len(stale)
        except Exception as e:
            logger.warning(f"Could not check for leftover test schemas: {e}")
            return 0

    def drop_temp_schema(self, schema_name: str) -> bool:
        """Drop a temporary schema and all its contents."""
        try:
            with self.get_connection() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema_name)
                    ))
                    return True
        except Exception as e:
            logger.error(f"Error dropping temp schema: {e}")
            return False
    
    def clone_table_to_schema(self, source_table: str, target_schema: str, source_schema: str = None, limit: int = None) -> bool:
        """
        Clone a table's structure and data to a temp schema.
        Uses LIKE ... INCLUDING STORAGE to preserve TOAST settings for large columns.
        
        Args:
            source_table: Name of the table to clone (without schema prefix)
            target_schema: Target schema name
            source_schema: Source schema name (e.g., 'openidm'). If None, assumes public.
            limit: Optional row limit. If None, copies ALL data for realistic testing.
        """
        try:
            with self.get_connection() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    # Build source table reference
                    if source_schema:
                        source_ref = f'"{source_schema}"."{source_table}"'
                    else:
                        source_ref = f'"{source_table}"'
                    
                    target_ref = f'"{target_schema}"."{source_table}"'

                    # Step 1: Create table structure with storage settings
                    # Use INCLUDING DEFAULTS INCLUDING STORAGE (not ALL to avoid generated columns)
                    cur.execute(f"""
                        CREATE TABLE {target_ref} (
                            LIKE {source_ref} INCLUDING DEFAULTS INCLUDING STORAGE
                        ){self._partition_clause(cur, source_ref)}
                    """)
                    # A partitioned table is copied as one: same key, same partition bounds, so
                    # partition pruning behaves in the test exactly as it does in the database
                    self._clone_partitions(cur, source_ref, target_schema, target_ref)
                    
                    # Step 2: Get non-generated columns for INSERT
                    cur.execute("""
                        SELECT column_name 
                        FROM information_schema.columns 
                        WHERE table_schema = %s 
                          AND table_name = %s 
                          AND is_generated = 'NEVER'
                        ORDER BY ordinal_position
                    """, (source_schema or 'public', source_table))
                    columns = [row[0] for row in cur.fetchall()]
                    
                    if columns:
                        cols_str = ', '.join(f'"{c}"' for c in columns)
                        
                        # Step 3: Insert data (with optional limit)
                        if limit:
                            cur.execute(f"""
                                INSERT INTO {target_ref} ({cols_str})
                                SELECT {cols_str} FROM {source_ref} LIMIT %s
                            """, (limit,))
                        else:
                            cur.execute(f"""
                                INSERT INTO {target_ref} ({cols_str})
                                SELECT {cols_str} FROM {source_ref}
                            """)

                    # A fresh clone has no planner statistics, which makes the planner
                    # fall back to guesses and produce plans far worse than the original.
                    cur.execute(sql.SQL("ANALYZE {}.{}").format(
                        sql.Identifier(target_schema), sql.Identifier(source_table)
                    ))

                    return True
        except Exception as e:
            logger.error(f"Error cloning table: {e}")
            return False
    
    @staticmethod
    def _partition_clause(cur, source_ref: str) -> str:
        """' PARTITION BY RANGE (...)' for a partitioned table, '' for anything else."""
        cur.execute("SELECT CASE WHEN relkind = 'p' THEN pg_get_partkeydef(oid) END "
                    "FROM pg_class WHERE oid = to_regclass(%s)", (source_ref,))
        row = cur.fetchone()
        return f" PARTITION BY {row[0]}" if row and row[0] else ""

    def _clone_partitions(self, cur, source_ref: str, target_schema: str, target_ref: str) -> None:
        """Recreate each partition (recursively for sub-partitions) under the cloned parent."""
        cur.execute("""
            SELECT c.relname, pg_get_expr(c.relpartbound, c.oid), format('%%I.%%I', n.nspname, c.relname)
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE i.inhparent = to_regclass(%s)
        """, (source_ref,))
        for name, bounds, child_ref in cur.fetchall():
            child_target = sql.SQL("{}.{}").format(sql.Identifier(target_schema), sql.Identifier(name)).as_string(cur)
            partition_clause = self._partition_clause(cur, child_ref)
            cur.execute(f"CREATE TABLE {child_target} PARTITION OF {target_ref} {bounds}{partition_clause}")
            if partition_clause:
                self._clone_partitions(cur, child_ref, target_schema, child_target)

    def create_index_on_temp(self, schema_name: str, index_statement: str, source_tables: list = None) -> bool:
        """
        Create an index on a temp schema table.
        The statement is rewritten to target the temp schema, and refused if that is not
        possible, so an AI recommendation can never touch the database being analyzed.

        Args:
            schema_name: Temp schema name
            index_statement: CREATE INDEX statement (may have original schema like openidm.tablename)
            source_tables: Unused; kept so existing callers keep working
        """
        try:
            statement, table_name = qualify_index_statement(index_statement, schema_name)
        except ValueError as e:
            logger.error(f"Refused index statement ({e}): {index_statement[:200]}")
            return False

        try:
            with self.get_connection() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    logger.info(f"Creating index on temp table: {statement[:200]}")
                    cur.execute(statement)
                    # Refresh statistics so the tested plan can actually use the new index
                    cur.execute(sql.SQL("ANALYZE {}.{}").format(
                        sql.Identifier(schema_name), sql.Identifier(table_name)
                    ))
                    return True
        except Exception as e:
            logger.error(f"Error creating index: {e}")
            logger.error(f"Statement: {statement[:200]}")
            return False
