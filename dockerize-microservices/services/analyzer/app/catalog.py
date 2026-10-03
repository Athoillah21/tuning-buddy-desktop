"""
Read-only catalog queries: sizes, row estimates, indexes, partitions and activity.

Shared by the analysis (table facts for the AI, the report and the fit checks) and the
database explorer. Every lookup goes through to_regclass(), so a name resolves exactly
the way PostgreSQL resolves it in a query, schema and search_path included.
"""
import datetime
import decimal
from typing import Any, Dict, List, Optional

# Never shown: system schemas and the optimizer's own throwaway test schemas
HIDDEN_SCHEMA_SQL = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp%%' "
    "AND n.nspname NOT LIKE 'temp_test\\_%%'"
)

RELKIND_LABELS = {'r': 'table', 'p': 'partitioned table', 'm': 'materialized view', 'f': 'foreign table'}


def _plain(value: Any) -> Any:
    """Catalog results travel as JSON (to the AI service, the web app, the report)."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _rows(cur) -> List[Dict[str, Any]]:
    names = [col.name for col in cur.description]
    return [{name: _plain(value) for name, value in zip(names, row)} for row in cur.fetchall()]


def _one(cur) -> Optional[Dict[str, Any]]:
    rows = _rows(cur)
    return rows[0] if rows else None


# Sizes, rows and activity of one relation. A partitioned parent stores nothing itself,
# so its figures are the sum over its leaf partitions.
_TABLE_SQL = """
WITH rel AS (
    SELECT c.oid, n.nspname, c.relname, c.relkind, c.relispartition
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.oid = to_regclass(%(name)s)
),
members AS (
    SELECT t.relid AS oid FROM rel, pg_partition_tree(rel.oid) t
    WHERE rel.relkind = 'p' AND t.isleaf
    UNION ALL
    SELECT rel.oid FROM rel WHERE rel.relkind <> 'p'
)
SELECT
    rel.oid, rel.nspname AS schema, rel.relname AS name, rel.relkind, rel.relispartition AS is_partition,
    COALESCE(SUM(pg_total_relation_size(m.oid)), 0)::bigint AS total_bytes,
    COALESCE(SUM(pg_table_size(m.oid)), 0)::bigint AS table_bytes,
    COALESCE(SUM(pg_indexes_size(m.oid)), 0)::bigint AS index_bytes,
    COALESCE(SUM(pg_total_relation_size(NULLIF(c.reltoastrelid, 0))), 0)::bigint AS toast_bytes,
    COALESCE(SUM(GREATEST(c.reltuples, 0)), 0)::bigint AS row_estimate,
    SUM(s.n_live_tup)::bigint AS live_rows,
    SUM(s.n_dead_tup)::bigint AS dead_rows,
    SUM(s.seq_scan)::bigint AS seq_scans,
    SUM(s.idx_scan)::bigint AS index_scans,
    SUM(s.n_tup_ins)::bigint AS rows_inserted,
    SUM(s.n_tup_upd)::bigint AS rows_updated,
    SUM(s.n_tup_del)::bigint AS rows_deleted,
    MAX(GREATEST(s.last_vacuum, s.last_autovacuum)) AS last_vacuum,
    MAX(GREATEST(s.last_analyze, s.last_autoanalyze)) AS last_analyze,
    BOOL_AND(c.reltuples >= 0) AS analyzed
FROM rel
LEFT JOIN members m ON true
LEFT JOIN pg_class c ON c.oid = m.oid
LEFT JOIN pg_stat_all_tables s ON s.relid = m.oid
GROUP BY rel.oid, rel.nspname, rel.relname, rel.relkind, rel.relispartition
"""

# Key columns come from pg_get_indexdef(oid, n) so expressions ("lower(email)") read like SQL.
# The size and scan count of an index on a partitioned table are summed over its partitions.
_INDEXES_SQL = """
SELECT
    ic.relname AS name,
    pg_get_indexdef(i.indexrelid) AS definition,
    am.amname AS method,
    ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true) FROM generate_series(1, i.indnkeyatts) k) AS columns,
    pg_get_expr(i.indpred, i.indrelid) AS predicate,
    i.indisunique AS is_unique, i.indisprimary AS is_primary, i.indisvalid AS is_valid,
    CASE WHEN ic.relkind = 'I' THEN
        (SELECT COALESCE(SUM(pg_relation_size(t.relid)), 0) FROM pg_partition_tree(i.indexrelid) t WHERE t.isleaf)
    ELSE pg_relation_size(i.indexrelid) END::bigint AS bytes,
    CASE WHEN ic.relkind = 'I' THEN
        (SELECT SUM(s2.idx_scan) FROM pg_partition_tree(i.indexrelid) t
         JOIN pg_stat_all_indexes s2 ON s2.indexrelid = t.relid WHERE t.isleaf)
    ELSE s.idx_scan END::bigint AS scans
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_am am ON am.oid = ic.relam
LEFT JOIN pg_stat_all_indexes s ON s.indexrelid = i.indexrelid
WHERE i.indrelid = %(oid)s
ORDER BY i.indisprimary DESC, ic.relname
"""

_PARTITIONS_SQL = """
SELECT
    n.nspname AS schema, c.relname AS name, c.relkind,
    pg_get_expr(c.relpartbound, c.oid) AS bounds,
    CASE WHEN c.relkind = 'p' THEN
        (SELECT COALESCE(SUM(pg_total_relation_size(t.relid)), 0) FROM pg_partition_tree(c.oid) t WHERE t.isleaf)
    ELSE pg_total_relation_size(c.oid) END::bigint AS total_bytes,
    GREATEST(c.reltuples, 0)::bigint AS row_estimate
FROM pg_inherits inh
JOIN pg_class c ON c.oid = inh.inhrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE inh.inhparent = %(oid)s
ORDER BY pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT', c.relname
"""


def table_stats(cur, name: str, include_partitions: bool = True) -> Optional[Dict[str, Any]]:
    """Everything about a table's size and shape, or None if the name does not resolve."""
    cur.execute(_TABLE_SQL, {'name': name})
    stats = _one(cur)
    if stats is None:
        return None
    oid = stats.pop('oid')
    stats['kind'] = RELKIND_LABELS.get(stats['relkind'], stats['relkind'])
    stats['is_partitioned'] = stats['relkind'] == 'p'
    stats['qualified_name'] = f"{stats['schema']}.{stats['name']}"

    cur.execute(_INDEXES_SQL, {'oid': oid})
    stats['indexes'] = _rows(cur)

    stats['partition_key'] = None
    stats['partitions'] = []
    if stats['is_partitioned']:
        cur.execute("SELECT pg_get_partkeydef(%(oid)s)", {'oid': oid})
        stats['partition_key'] = cur.fetchone()[0]
        if include_partitions:
            cur.execute(_PARTITIONS_SQL, {'oid': oid})
            stats['partitions'] = _rows(cur)
    stats['partition_count'] = len(stats['partitions']) if include_partitions else None

    stats['parent'] = None
    if stats['is_partition']:
        cur.execute("""
            SELECT pn.nspname AS schema, pc.relname AS name
            FROM pg_inherits inh
            JOIN pg_class pc ON pc.oid = inh.inhparent
            JOIN pg_namespace pn ON pn.oid = pc.relnamespace
            WHERE inh.inhrelid = %(oid)s
        """, {'oid': oid})
        stats['parent'] = _one(cur)
    return stats


def table_columns(cur, name: str) -> List[Dict[str, Any]]:
    cur.execute("""
        SELECT a.attname AS name,
               format_type(a.atttypid, a.atttypmod) AS type,
               NOT a.attnotnull AS nullable,
               pg_get_expr(d.adbin, d.adrelid) AS default
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = to_regclass(%(name)s) AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
    """, {'name': name})
    return _rows(cur)


def table_constraints(cur, name: str) -> List[Dict[str, Any]]:
    cur.execute("""
        SELECT conname AS name,
               CASE contype WHEN 'p' THEN 'primary key' WHEN 'f' THEN 'foreign key'
                            WHEN 'u' THEN 'unique' WHEN 'c' THEN 'check'
                            WHEN 'x' THEN 'exclusion' ELSE contype::text END AS type,
               pg_get_constraintdef(oid, true) AS definition
        FROM pg_constraint
        WHERE conrelid = to_regclass(%(name)s)
        ORDER BY contype, conname
    """, {'name': name})
    return _rows(cur)


def table_detail(cur, schema: str, table: str) -> Optional[Dict[str, Any]]:
    name = quote_qualified(schema, table)
    stats = table_stats(cur, name)
    if stats is None:
        return None
    stats['columns'] = table_columns(cur, name)
    stats['constraints'] = table_constraints(cur, name)
    return stats


def list_tables(cur, schema: Optional[str] = None) -> List[Dict[str, Any]]:
    """Top-level tables (partitions are counted under their parent), largest first."""
    cur.execute(f"""
        WITH tables AS (
            SELECT c.oid, n.nspname, c.relname, c.relkind
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'm', 'f') AND NOT c.relispartition
              AND {HIDDEN_SCHEMA_SQL}
              AND (%(schema)s::text IS NULL OR n.nspname = %(schema)s)
        ),
        members AS (
            SELECT t.oid AS table_oid, pt.relid AS oid
            FROM tables t, pg_partition_tree(t.oid) pt WHERE t.relkind = 'p' AND pt.isleaf
            UNION ALL
            SELECT t.oid, t.oid FROM tables t WHERE t.relkind <> 'p'
        )
        SELECT
            t.nspname AS schema, t.relname AS name, t.relkind,
            COALESCE(SUM(pg_total_relation_size(m.oid)), 0)::bigint AS total_bytes,
            COALESCE(SUM(pg_table_size(m.oid)), 0)::bigint AS table_bytes,
            COALESCE(SUM(pg_indexes_size(m.oid)), 0)::bigint AS index_bytes,
            COALESCE(SUM(GREATEST(c.reltuples, 0)), 0)::bigint AS row_estimate,
            SUM(s.n_dead_tup)::bigint AS dead_rows,
            SUM(s.seq_scan)::bigint AS seq_scans,
            SUM(s.idx_scan)::bigint AS index_scans,
            MAX(GREATEST(s.last_analyze, s.last_autoanalyze)) AS last_analyze,
            (SELECT count(*) FROM pg_index i WHERE i.indrelid = t.oid) AS index_count,
            (SELECT count(*) FROM pg_inherits inh WHERE inh.inhparent = t.oid) AS partition_count
        FROM tables t
        LEFT JOIN members m ON m.table_oid = t.oid
        LEFT JOIN pg_class c ON c.oid = m.oid
        LEFT JOIN pg_stat_all_tables s ON s.relid = m.oid
        GROUP BY t.oid, t.nspname, t.relname, t.relkind
        ORDER BY total_bytes DESC, t.nspname, t.relname
    """, {'schema': schema})
    tables = _rows(cur)
    for table in tables:
        table['kind'] = RELKIND_LABELS.get(table['relkind'], table['relkind'])
    return tables


def database_overview(cur) -> Dict[str, Any]:
    cur.execute("""
        SELECT current_database() AS database,
               current_setting('server_version') AS version,
               pg_database_size(current_database())::bigint AS total_bytes,
               current_user AS user,
               (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser
    """)
    overview = _one(cur)
    cur.execute("SELECT extname AS name, extversion AS version FROM pg_extension ORDER BY extname")
    overview['extensions'] = _rows(cur)

    tables = list_tables(cur)
    schemas: Dict[str, Dict[str, Any]] = {}
    for table in tables:
        entry = schemas.setdefault(table['schema'], {'name': table['schema'], 'tables': 0, 'total_bytes': 0})
        entry['tables'] += 1
        entry['total_bytes'] += table['total_bytes'] or 0
    overview['schemas'] = sorted(schemas.values(), key=lambda s: -s['total_bytes'])
    overview['tables'] = tables
    return overview


def quote_qualified(schema: str, table: str) -> str:
    """"schema"."table" for to_regclass, safe for any identifier."""
    def quote(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'
    return f"{quote(schema)}.{quote(table)}"
