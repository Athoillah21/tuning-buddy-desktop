"""
Read-only queries behind the pgAdmin-style object browser: the server, its databases, schemas,
every kind of object in a schema, their details and their DDL.

Table sizes and shapes come from catalog.py, which the analysis shares; this module adds what
only the browser needs. Objects are addressed by schema and name, except functions, which can
be overloaded and are addressed by oid.
"""
from typing import Any, Dict, List, Optional

from .catalog import HIDDEN_SCHEMA_SQL, _one, _rows, list_tables, quote_qualified, table_columns

GROUPS = ('tables', 'views', 'materialized_views', 'functions', 'sequences', 'types')

# The settings a DBA looks at first when a query is slow
KEY_SETTINGS = (
    'shared_buffers', 'effective_cache_size', 'work_mem', 'maintenance_work_mem', 'max_connections',
    'random_page_cost', 'seq_page_cost', 'effective_io_concurrency', 'default_statistics_target',
    'max_parallel_workers_per_gather', 'max_parallel_workers', 'jit', 'enable_partition_pruning',
    'max_wal_size', 'checkpoint_timeout', 'autovacuum', 'autovacuum_vacuum_scale_factor',
    'autovacuum_analyze_scale_factor', 'statement_timeout', 'lock_timeout',
    'idle_in_transaction_session_timeout', 'shared_preload_libraries', 'server_encoding', 'TimeZone',
)


# ---------------------------------------------------------------------------
# Server and databases
# ---------------------------------------------------------------------------

def server_info(cur) -> Dict[str, Any]:
    cur.execute("""
        SELECT version() AS version_full,
               current_setting('server_version') AS version,
               inet_server_addr()::text AS address,
               inet_server_port() AS port,
               pg_postmaster_start_time() AS started_at,
               now() - pg_postmaster_start_time() AS uptime,
               current_user AS user,
               (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser,
               pg_is_in_recovery() AS is_replica,
               (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend') AS sessions,
               current_setting('max_connections')::int AS max_connections
    """)
    info = _one(cur)
    info['uptime'] = str(info['uptime']).split('.')[0] if info.get('uptime') else None

    cur.execute("""
        SELECT name, current_setting(name) AS value, short_desc AS description, context
        FROM pg_settings WHERE name = ANY(%(names)s)
    """, {'names': list(KEY_SETTINGS)})
    order = {name: i for i, name in enumerate(KEY_SETTINGS)}
    info['settings'] = sorted(_rows(cur), key=lambda s: order.get(s['name'], 999))

    cur.execute("""
        SELECT COALESCE(state, backend_type) AS state, count(*) AS sessions
        FROM pg_stat_activity GROUP BY 1 ORDER BY 2 DESC
    """)
    info['activity_summary'] = _rows(cur)

    # The longest-running statements right now; other users' query text needs pg_read_all_stats
    cur.execute("""
        SELECT pid, datname AS database, usename AS user, state, wait_event_type, wait_event,
               EXTRACT(EPOCH FROM (now() - query_start))::numeric(12, 1) AS seconds,
               left(query, 300) AS query
        FROM pg_stat_activity
        WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() AND state <> 'idle'
        ORDER BY query_start NULLS LAST
        LIMIT 20
    """)
    info['active_queries'] = _rows(cur)
    return info


def list_databases(cur) -> List[Dict[str, Any]]:
    cur.execute("""
        SELECT d.datname AS name,
               pg_get_userbyid(d.datdba) AS owner,
               pg_encoding_to_char(d.encoding) AS encoding,
               d.datcollate AS collation,
               CASE WHEN has_database_privilege(d.datname, 'CONNECT')
                    THEN pg_database_size(d.oid) END::bigint AS total_bytes,
               has_database_privilege(d.datname, 'CONNECT') AS can_connect,
               d.datname = current_database() AS is_current
        FROM pg_database d
        WHERE d.datallowconn AND NOT d.datistemplate
        ORDER BY d.datname
    """)
    return _rows(cur)


def extensions(cur) -> List[Dict[str, Any]]:
    cur.execute("""
        SELECT e.extname AS name, e.extversion AS version, n.nspname AS schema,
               obj_description(e.oid, 'pg_extension') AS description
        FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
        ORDER BY e.extname
    """)
    return _rows(cur)


# ---------------------------------------------------------------------------
# Schemas and the objects in them
# ---------------------------------------------------------------------------

# Composite types that belong to a table (every table has one) are not listed as types
_TYPE_FILTER = """
    t.typtype IN ('e', 'd', 'r', 'm')
    OR (t.typtype = 'c' AND (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c')
"""

# Objects created by an extension (PostGIS alone adds hundreds of functions) are hidden
_NOT_EXTENSION = "NOT EXISTS (SELECT 1 FROM pg_depend dep WHERE dep.objid = {oid} AND dep.deptype = 'e')"


def list_schemas(cur) -> List[Dict[str, Any]]:
    cur.execute(f"""
        SELECT n.nspname AS name, pg_get_userbyid(n.nspowner) AS owner,
               (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid
                  AND c.relkind IN ('r', 'p', 'f') AND NOT c.relispartition) AS tables,
               (SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0) FROM pg_class c
                  WHERE c.relnamespace = n.oid AND c.relkind IN ('r', 'm'))::bigint AS total_bytes
        FROM pg_namespace n
        WHERE {HIDDEN_SCHEMA_SQL}
        ORDER BY n.nspname = 'public' DESC, n.nspname
    """)
    return _rows(cur)


def group_counts(cur, schema: str) -> Dict[str, int]:
    cur.execute(f"""
        SELECT
            (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid
               AND c.relkind IN ('r', 'p', 'f') AND NOT c.relispartition) AS tables,
            (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind = 'v'
               AND {_NOT_EXTENSION.format(oid='c.oid')}) AS views,
            (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind = 'm') AS materialized_views,
            (SELECT count(*) FROM pg_proc p WHERE p.pronamespace = n.oid AND p.prokind IN ('f', 'p')
               AND {_NOT_EXTENSION.format(oid='p.oid')}) AS functions,
            (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind = 'S') AS sequences,
            (SELECT count(*) FROM pg_type t WHERE t.typnamespace = n.oid AND ({_TYPE_FILTER})
               AND {_NOT_EXTENSION.format(oid='t.oid')}) AS types
        FROM pg_namespace n WHERE n.nspname = %(schema)s
    """, {'schema': schema})
    return _one(cur) or {group: 0 for group in GROUPS}


def list_objects(cur, schema: str, group: str) -> List[Dict[str, Any]]:
    if group == 'tables':
        return [t for t in list_tables(cur, schema) if t['relkind'] in ('r', 'p', 'f')]
    if group in ('views', 'materialized_views', 'sequences'):
        relkind = {'views': 'v', 'materialized_views': 'm', 'sequences': 'S'}[group]
        cur.execute(f"""
            SELECT c.relname AS name, pg_get_userbyid(c.relowner) AS owner,
                   pg_total_relation_size(c.oid)::bigint AS total_bytes,
                   obj_description(c.oid, 'pg_class') AS description
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %(schema)s AND c.relkind = %(relkind)s
              AND {_NOT_EXTENSION.format(oid='c.oid')}
            ORDER BY c.relname
        """, {'schema': schema, 'relkind': relkind})
        return _rows(cur)
    if group == 'functions':
        cur.execute(f"""
            SELECT p.oid, p.proname AS name, pg_get_function_identity_arguments(p.oid) AS arguments,
                   CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END AS kind,
                   pg_get_function_result(p.oid) AS returns, l.lanname AS language
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_language l ON l.oid = p.prolang
            WHERE n.nspname = %(schema)s AND p.prokind IN ('f', 'p')
              AND {_NOT_EXTENSION.format(oid='p.oid')}
            ORDER BY p.proname, p.oid
        """, {'schema': schema})
        return _rows(cur)
    if group == 'types':
        cur.execute(f"""
            SELECT t.typname AS name,
                   CASE t.typtype WHEN 'e' THEN 'enum' WHEN 'd' THEN 'domain' WHEN 'r' THEN 'range'
                                  WHEN 'm' THEN 'multirange' ELSE 'composite' END AS kind
            FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = %(schema)s AND ({_TYPE_FILTER})
              AND {_NOT_EXTENSION.format(oid='t.oid')}
            ORDER BY t.typname
        """, {'schema': schema})
        return _rows(cur)
    raise ValueError(f"Unknown group {group}")


# ---------------------------------------------------------------------------
# Object details
# ---------------------------------------------------------------------------

def view_detail(cur, schema: str, name: str) -> Optional[Dict[str, Any]]:
    qualified = quote_qualified(schema, name)
    cur.execute("""
        SELECT c.relname AS name, n.nspname AS schema, c.relkind,
               pg_get_userbyid(c.relowner) AS owner,
               pg_get_viewdef(c.oid, true) AS definition,
               pg_total_relation_size(c.oid)::bigint AS total_bytes,
               c.relispopulated AS is_populated,
               obj_description(c.oid, 'pg_class') AS description
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.oid = to_regclass(%(name)s) AND c.relkind IN ('v', 'm')
    """, {'name': qualified})
    detail = _one(cur)
    if detail is None:
        return None
    detail['kind'] = 'materialized view' if detail['relkind'] == 'm' else 'view'
    detail['columns'] = table_columns(cur, qualified)
    detail['ddl'] = view_ddl(detail)
    if detail['relkind'] == 'm':
        detail['ddl'] += _index_ddl(cur, qualified)
    return detail


def view_ddl(detail: Dict[str, Any]) -> str:
    name = quote_qualified(detail['schema'], detail['name'])
    definition = (detail.get('definition') or '').rstrip().rstrip(';')
    if detail['relkind'] == 'm':
        return f"CREATE MATERIALIZED VIEW {name} AS\n{definition}\nWITH DATA;\n"
    return f"CREATE OR REPLACE VIEW {name} AS\n{definition};\n"


def function_detail(cur, oid: int) -> Optional[Dict[str, Any]]:
    cur.execute("""
        SELECT p.oid, p.proname AS name, n.nspname AS schema,
               CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END AS kind,
               pg_get_function_arguments(p.oid) AS arguments,
               pg_get_function_result(p.oid) AS returns,
               l.lanname AS language,
               CASE p.provolatile WHEN 'i' THEN 'immutable' WHEN 's' THEN 'stable' ELSE 'volatile' END AS volatility,
               p.prosecdef AS security_definer, p.proisstrict AS is_strict,
               CASE p.proparallel WHEN 's' THEN 'safe' WHEN 'r' THEN 'restricted' ELSE 'unsafe' END AS parallel,
               p.procost AS cost, p.prorows AS rows,
               pg_get_userbyid(p.proowner) AS owner,
               obj_description(p.oid, 'pg_proc') AS description,
               pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_language l ON l.oid = p.prolang
        WHERE p.oid = %(oid)s AND p.prokind IN ('f', 'p')
    """, {'oid': oid})
    return _one(cur)


def sequence_detail(cur, schema: str, name: str) -> Optional[Dict[str, Any]]:
    cur.execute("""
        SELECT s.schemaname AS schema, s.sequencename AS name, s.sequenceowner AS owner,
               format_type(s.data_type, NULL) AS data_type, s.start_value, s.min_value, s.max_value,
               s.increment_by, s.cycle, s.cache_size, s.last_value,
               (SELECT quote_ident(tn.nspname) || '.' || quote_ident(tc.relname) || '.' || quote_ident(a.attname)
                FROM pg_depend d
                JOIN pg_class tc ON tc.oid = d.refobjid
                JOIN pg_namespace tn ON tn.oid = tc.relnamespace
                JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
                WHERE d.objid = to_regclass(%(name)s) AND d.deptype IN ('a', 'i') LIMIT 1) AS owned_by
        FROM pg_sequences s
        WHERE s.schemaname = %(schema)s AND s.sequencename = %(sequence)s
    """, {'name': quote_qualified(schema, name), 'schema': schema, 'sequence': name})
    detail = _one(cur)
    if detail is None:
        return None
    ddl = (f"CREATE SEQUENCE {quote_qualified(schema, name)}\n"
           f"    AS {detail['data_type']}\n"
           f"    INCREMENT BY {detail['increment_by']}\n"
           f"    MINVALUE {detail['min_value']}\n"
           f"    MAXVALUE {detail['max_value']}\n"
           f"    START WITH {detail['start_value']}\n"
           f"    CACHE {detail['cache_size']}"
           f"{chr(10) + '    CYCLE' if detail['cycle'] else ''};\n")
    if detail['owned_by']:
        ddl += f"\nALTER SEQUENCE {quote_qualified(schema, name)} OWNED BY {detail['owned_by']};\n"
    detail['ddl'] = ddl
    return detail


def type_detail(cur, schema: str, name: str) -> Optional[Dict[str, Any]]:
    cur.execute("""
        SELECT t.oid, t.typname AS name, n.nspname AS schema, t.typtype, t.typrelid,
               pg_get_userbyid(t.typowner) AS owner,
               format_type(t.typbasetype, t.typtypmod) AS base_type, t.typnotnull AS not_null,
               t.typdefault AS default_value,
               obj_description(t.oid, 'pg_type') AS description
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = %(schema)s AND t.typname = %(name)s
    """, {'schema': schema, 'name': name})
    detail = _one(cur)
    if detail is None:
        return None
    oid, typtype = detail.pop('oid'), detail['typtype']
    qualified = quote_qualified(schema, name)
    detail['labels'], detail['attributes'], detail['checks'] = [], [], []

    if typtype == 'e':
        detail['kind'] = 'enum'
        cur.execute("SELECT enumlabel FROM pg_enum WHERE enumtypid = %(oid)s ORDER BY enumsortorder", {'oid': oid})
        detail['labels'] = [row[0] for row in cur.fetchall()]
        labels = ', '.join("'" + label.replace("'", "''") + "'" for label in detail['labels'])
        detail['ddl'] = f"CREATE TYPE {qualified} AS ENUM ({labels});\n"
    elif typtype == 'd':
        detail['kind'] = 'domain'
        cur.execute("""
            SELECT conname AS name, pg_get_constraintdef(oid, true) AS definition
            FROM pg_constraint WHERE contypid = %(oid)s ORDER BY conname
        """, {'oid': oid})
        detail['checks'] = _rows(cur)
        ddl = f"CREATE DOMAIN {qualified} AS {detail['base_type']}"
        if detail['default_value']:
            ddl += f"\n    DEFAULT {detail['default_value']}"
        if detail['not_null']:
            ddl += "\n    NOT NULL"
        for check in detail['checks']:
            ddl += f"\n    CONSTRAINT {check['name']} {check['definition']}"
        detail['ddl'] = ddl + ";\n"
    elif typtype == 'c':
        detail['kind'] = 'composite'
        cur.execute("""
            SELECT attname AS name, format_type(atttypid, atttypmod) AS type
            FROM pg_attribute WHERE attrelid = %(relid)s AND attnum > 0 AND NOT attisdropped ORDER BY attnum
        """, {'relid': detail['typrelid']})
        detail['attributes'] = _rows(cur)
        fields = ',\n'.join(f"    {a['name']} {a['type']}" for a in detail['attributes'])
        detail['ddl'] = f"CREATE TYPE {qualified} AS (\n{fields}\n);\n"
    else:
        detail['kind'] = 'range' if typtype == 'r' else 'multirange'
        cur.execute("SELECT format_type(rngsubtype, NULL) FROM pg_range WHERE rngtypid = %(oid)s", {'oid': oid})
        row = cur.fetchone()
        detail['base_type'] = row[0] if row else None
        detail['ddl'] = (f"CREATE TYPE {qualified} AS RANGE (SUBTYPE = {detail['base_type']});\n"
                         if typtype == 'r' and detail['base_type'] else '')
    detail.pop('typrelid', None)
    return detail


# ---------------------------------------------------------------------------
# Table DDL
# ---------------------------------------------------------------------------

def _index_ddl(cur, qualified: str) -> str:
    """CREATE INDEX statements for the indexes that no constraint already creates."""
    cur.execute("""
        SELECT pg_get_indexdef(i.indexrelid) || ';'
        FROM pg_index i
        WHERE i.indrelid = to_regclass(%(name)s)
          AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
        ORDER BY i.indexrelid
    """, {'name': qualified})
    statements = [row[0] for row in cur.fetchall()]
    return ('\n' + '\n'.join(statements) + '\n') if statements else ''


def table_ddl(cur, schema: str, table: str) -> Optional[str]:
    """
    A CREATE TABLE script built from the catalog, close to what pg_dump writes: columns with
    defaults, identity and generated expressions, constraints, the partition key, partitions,
    extra indexes, ownership and comments.
    """
    qualified = quote_qualified(schema, table)
    cur.execute("""
        SELECT c.oid, c.relkind, c.relpersistence, c.relispartition,
               quote_ident(pg_get_userbyid(c.relowner)) AS owner,
               obj_description(c.oid, 'pg_class') AS description,
               CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END AS partition_key,
               CASE WHEN c.relispartition THEN pg_get_expr(c.relpartbound, c.oid) END AS bounds,
               (SELECT quote_ident(pn.nspname) || '.' || quote_ident(pc.relname)
                FROM pg_inherits inh JOIN pg_class pc ON pc.oid = inh.inhparent
                JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                WHERE inh.inhrelid = c.oid LIMIT 1) AS parent
        FROM pg_class c WHERE c.oid = to_regclass(%(name)s) AND c.relkind IN ('r', 'p', 'f')
    """, {'name': qualified})
    rel = _one(cur)
    if rel is None:
        return None
    oid = rel['oid']

    cur.execute("""
        SELECT quote_ident(a.attname) AS name, format_type(a.atttypid, a.atttypmod) AS type,
               a.attnotnull AS not_null, a.attidentity AS identity, a.attgenerated AS generated,
               pg_get_expr(d.adbin, d.adrelid) AS default_expr,
               CASE WHEN a.attcollation <> t.typcollation AND a.attcollation <> 0
                    THEN (SELECT quote_ident(collname) FROM pg_collation WHERE oid = a.attcollation) END AS collation,
               col_description(a.attrelid, a.attnum) AS description,
               a.attislocal AS is_local
        FROM pg_attribute a
        JOIN pg_type t ON t.oid = a.atttypid
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
    """, {'oid': oid})
    columns = _rows(cur)

    cur.execute("""
        SELECT quote_ident(conname) AS name, pg_get_constraintdef(oid, true) AS definition, conislocal
        FROM pg_constraint
        WHERE conrelid = %(oid)s AND contype IN ('p', 'u', 'f', 'c', 'x')
        ORDER BY CASE contype WHEN 'p' THEN 0 WHEN 'u' THEN 1 WHEN 'c' THEN 2 WHEN 'x' THEN 3 ELSE 4 END, conname
    """, {'oid': oid})
    constraints = _rows(cur)

    # Sequences behind serial columns (owned with deptype 'a'; identity columns are 'i' and need
    # nothing): the table's DEFAULT nextval(...) only works if the script creates them first
    sequences = []
    if not rel['relispartition']:
        cur.execute("""
            SELECT quote_ident(sn.nspname) || '.' || quote_ident(sc.relname) AS name,
                   quote_ident(a.attname) AS column_name, format_type(sq.seqtypid, NULL) AS data_type,
                   sq.seqstart, sq.seqincrement, sq.seqmin, sq.seqmax, sq.seqcache, sq.seqcycle
            FROM pg_depend d
            JOIN pg_class sc ON sc.oid = d.objid AND sc.relkind = 'S'
            JOIN pg_namespace sn ON sn.oid = sc.relnamespace
            JOIN pg_sequence sq ON sq.seqrelid = sc.oid
            JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
            WHERE d.classid = 'pg_class'::regclass AND d.refobjid = %(oid)s AND d.deptype = 'a'
            ORDER BY a.attnum
        """, {'oid': oid})
        sequences = _rows(cur)

    kind = 'UNLOGGED TABLE' if rel['relpersistence'] == 'u' else 'TABLE'
    if rel['relkind'] == 'f':
        kind = 'FOREIGN TABLE'

    if rel['relispartition'] and rel['parent']:
        # A partition takes its columns from the parent; only its own constraints are listed
        own = [f"    CONSTRAINT {c['name']} {c['definition']}" for c in constraints if c['conislocal']]
        body = " (\n" + ",\n".join(own) + "\n)" if own else ''
        ddl = f"CREATE {kind} {qualified} PARTITION OF {rel['parent']}{body}\n{rel['bounds']};\n"
    else:
        lines = []
        for column in columns:
            line = f"    {column['name']} {column['type']}"
            if column['collation']:
                line += f" COLLATE {column['collation']}"
            if column['identity']:
                line += f" GENERATED {'ALWAYS' if column['identity'] == 'a' else 'BY DEFAULT'} AS IDENTITY"
            elif column['generated'] == 's':
                line += f" GENERATED ALWAYS AS ({column['default_expr']}) STORED"
            elif column['default_expr'] is not None:
                line += f" DEFAULT {column['default_expr']}"
            if column['not_null']:
                line += " NOT NULL"
            lines.append(line)
        lines += [f"    CONSTRAINT {c['name']} {c['definition']}" for c in constraints]
        ddl = f"CREATE {kind} {qualified} (\n" + ",\n".join(lines) + "\n)"
        if rel['partition_key']:
            ddl += f" PARTITION BY {rel['partition_key']}"
        ddl += ";\n"

    if sequences:
        created = "\n".join(
            f"CREATE SEQUENCE {seq['name']} AS {seq['data_type']} INCREMENT BY {seq['seqincrement']} "
            f"MINVALUE {seq['seqmin']} MAXVALUE {seq['seqmax']} START WITH {seq['seqstart']} "
            f"CACHE {seq['seqcache']}{' CYCLE' if seq['seqcycle'] else ''};"
            for seq in sequences)
        owned = "\n".join(f"ALTER SEQUENCE {seq['name']} OWNED BY {qualified}.{seq['column_name']};"
                           for seq in sequences)
        ddl = created + "\n\n" + ddl + "\n" + owned + "\n"

    ddl += _index_ddl(cur, qualified)

    if rel['relkind'] == 'p':
        cur.execute("""
            SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS name,
                   pg_get_expr(c.relpartbound, c.oid) AS bounds
            FROM pg_inherits inh JOIN pg_class c ON c.oid = inh.inhrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE inh.inhparent = %(oid)s
            ORDER BY pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT', c.relname
        """, {'oid': oid})
        partitions = _rows(cur)
        if partitions:
            ddl += "\n" + "\n".join(f"CREATE TABLE {p['name']} PARTITION OF {qualified} {p['bounds']};"
                                    for p in partitions) + "\n"

    ddl += f"\nALTER TABLE {qualified} OWNER TO {rel['owner']};\n"
    comments = []
    if rel['description']:
        comments.append(f"COMMENT ON TABLE {qualified} IS {_literal(rel['description'])};")
    for column in columns:
        if column['description']:
            comments.append(f"COMMENT ON COLUMN {qualified}.{column['name']} IS {_literal(column['description'])};")
    if comments:
        ddl += "\n" + "\n".join(comments) + "\n"
    return ddl


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"
