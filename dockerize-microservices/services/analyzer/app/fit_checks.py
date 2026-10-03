"""
Does a recommendation fit the database it is meant for?

A tested speed-up is only half the answer. These checks compare the suggested indexes
with what the table already has, and look at the table itself: its size, partitioning,
write load and statistics. Each finding is {level: ok|info|warn, code, message}.
"""
import re
from typing import Any, Dict, List, Optional

LARGE_TABLE_BYTES = 100 * 1024 * 1024
LARGE_TABLE_ROWS = 1_000_000
BIG_INDEX_SHARE = 50          # % of the table's size
DEAD_ROW_SHARE = 20           # % of all rows
DEAD_ROW_MIN = 10_000
MANY_INDEXES = 3

_CREATE_INDEX_RE = re.compile(
    r'^\s*CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?'
    r'(?:(?P<name>"[^"]+"|[A-Za-z_][\w$]*)\s+)?'
    r'ON\s+(?:ONLY\s+)?(?:(?P<schema>"[^"]+"|[A-Za-z_][\w$]*)\s*\.\s*)?(?P<table>"[^"]+"|[A-Za-z_][\w$]*)\s*'
    r'(?:USING\s+(?P<method>\w+)\s*)?\(',
    re.IGNORECASE,
)
_ORDERING_RE = re.compile(r'\s+(asc|desc|nulls\s+first|nulls\s+last)\b', re.IGNORECASE)


def _closing_paren(text: str, start: int) -> int:
    """Index of the parenthesis closing the one at `start`, skipping quoted text."""
    depth, i, quote = 0, start, None
    while i < len(text):
        char = text[i]
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text)


def _split_top_level(text: str) -> List[str]:
    parts, depth, current, quote = [], 0, [], None
    for char in text:
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(''.join(current))
            current = []
            continue
        current.append(char)
    parts.append(''.join(current))
    return [part.strip() for part in parts if part.strip()]


def normalize_column(expression: str) -> str:
    """
    Compare index keys by meaning, not spelling: "lower(Email)" and "lower(email)",
    "(payload ->> 'user_id'::text)" and "(payload->>'user_id')", "order_date DESC" and
    "order_date" all compare equal. The operator class (gin_trgm_ops) is kept - it matters.
    """
    text = _ORDERING_RE.sub('', expression.strip())
    text = text.replace('::text', '').replace('"', '')
    text = re.sub(r'\s+', '', text.lower())
    while text.startswith('(') and _closing_paren(text, 0) == len(text) - 1:
        text = text[1:-1]
    return text


def parse_index(statement: str) -> Optional[Dict[str, Any]]:
    """Name, table, method, key columns and predicate of a CREATE INDEX, or None."""
    match = _CREATE_INDEX_RE.match(statement or '')
    if not match:
        return None
    open_paren = match.end() - 1
    close_paren = _closing_paren(statement, open_paren)
    columns = _split_top_level(statement[open_paren + 1:close_paren])
    rest = statement[close_paren + 1:]
    where = re.search(r'\bWHERE\b(?P<predicate>.*)$', rest, re.IGNORECASE | re.DOTALL)
    return {
        'statement': statement.strip().rstrip(';'),
        'name': (match.group('name') or '').strip('"') or None,
        'table': match.group('table').strip('"'),
        'method': (match.group('method') or 'btree').lower(),
        'unique': bool(match.group('unique')),
        'columns': [normalize_column(column) for column in columns],
        'predicate': normalize_column(where.group('predicate').rstrip(';')) if where else None,
    }


def _key(index: Dict[str, Any]) -> tuple:
    return (index['method'], tuple(index['columns']), index.get('predicate'))


def _starts_with(longer: List[str], shorter: List[str]) -> bool:
    return len(shorter) < len(longer) and longer[:len(shorter)] == shorter


def _existing(table_stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The table's current indexes, normalized like parsed statements."""
    indexes = []
    for index in table_stats.get('indexes') or []:
        indexes.append({
            'name': index.get('name'),
            'method': (index.get('method') or 'btree').lower(),
            'columns': [normalize_column(column) for column in index.get('columns') or []],
            'predicate': normalize_column(index['predicate']) if index.get('predicate') else None,
            'is_unique': index.get('is_unique') or index.get('is_primary'),
            'bytes': index.get('bytes'),
        })
    return indexes


def format_bytes(value: Optional[float]) -> str:
    if value is None:
        return 'unknown size'
    size = float(value)
    for unit in ('B', 'kB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f"{size:.0f} {unit}" if unit == 'B' else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_rows(value: Optional[float]) -> str:
    if value is None:
        return '?'
    value = float(value)
    for limit, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return f"{value:.0f}"


def _find_stats(table: str, table_stats: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    wanted = table.lower()
    for name, stats in (table_stats or {}).items():
        if not isinstance(stats, dict) or stats.get('error'):
            continue
        if name.split('.')[-1].lower() == wanted or (stats.get('name') or '').lower() == wanted:
            return stats
    return None


def _check(level: str, code: str, message: str) -> Dict[str, str]:
    return {'level': level, 'code': code, 'message': message}


def _table_checks(table: str, stats: Dict[str, Any], building_index: bool) -> List[Dict[str, str]]:
    checks = []
    size = format_bytes(stats.get('total_bytes'))
    rows = format_rows(stats.get('row_estimate'))

    if stats.get('analyzed') is False:
        checks.append(_check('warn', 'stats_stale',
            f"{table} has never been analyzed, so the planner is guessing its row counts. "
            f"Run ANALYZE {table} first: it can fix the plan without any new index."))
    live, dead = stats.get('live_rows') or 0, stats.get('dead_rows') or 0
    if dead >= DEAD_ROW_MIN and dead * 100 / max(live + dead, 1) > DEAD_ROW_SHARE:
        checks.append(_check('warn', 'stats_stale',
            f"{dead * 100 / (live + dead):.0f}% of {table}'s rows are dead. "
            f"Run VACUUM (ANALYZE) {table} before judging the plan."))

    if not building_index:
        return checks

    if stats.get('is_partitioned'):
        checks.append(_check('warn', 'partitioned_table',
            f"{table} is partitioned ({stats.get('partition_count') or 'several'} partitions, {size}): "
            "an index on the parent is built on every partition, and CREATE INDEX CONCURRENTLY is not "
            "allowed on it. To avoid blocking writes, create it ON ONLY the parent, build each partition's "
            "index CONCURRENTLY, then ALTER INDEX ... ATTACH PARTITION."))
    elif (stats.get('total_bytes') or 0) >= LARGE_TABLE_BYTES or (stats.get('row_estimate') or 0) >= LARGE_TABLE_ROWS:
        checks.append(_check('warn', 'large_table_lock',
            f"{table} holds {size} (≈{rows} rows). A plain CREATE INDEX blocks writes to it while it builds; "
            "in production use CREATE INDEX CONCURRENTLY."))

    writes = sum(stats.get(key) or 0 for key in ('rows_inserted', 'rows_updated', 'rows_deleted'))
    reads = (stats.get('seq_scans') or 0) + (stats.get('index_scans') or 0)
    index_count = len(stats.get('indexes') or [])
    if index_count >= MANY_INDEXES and writes > reads:
        checks.append(_check('info', 'write_heavy',
            f"{table} already has {index_count} indexes and sees more row writes ({writes:,}) than scans "
            f"({reads:,}). Every extra index slows its INSERT, UPDATE and DELETE."))
    return checks


def fit_checks(rec: Dict[str, Any], table_stats: Dict[str, Dict[str, Any]],
               index_sizes: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, str]]:
    statements = rec.get('all_indexes_applied') or rec.get('suggested_indexes') or []
    parsed = [index for index in (parse_index(s) for s in statements) if index]
    sizes = {entry.get('statement'): entry for entry in index_sizes or []}
    checks: List[Dict[str, str]] = []

    # Redundancy inside the recommendation itself (iterations can pile up near-copies)
    seen: Dict[tuple, Dict[str, Any]] = {}
    redundant_statements = set()
    for index in parsed:
        key = (index['table'].lower(),) + _key(index)
        if key in seen:
            redundant_statements.add(index['statement'])
            checks.append(_check('warn', 'duplicate_in_recommendation',
                f"{index['name'] or 'An index'} repeats {seen[key]['name'] or 'another index'} in this "
                f"recommendation ({index['method']} on {', '.join(index['columns'])}). Create only one."))
        else:
            seen[key] = index
    for index in parsed:
        for other in parsed:
            if (other is not index and index['statement'] not in redundant_statements
                    and other['table'].lower() == index['table'].lower()
                    and index['method'] == other['method'] == 'btree'
                    and not index['predicate'] and not other['predicate']
                    and _starts_with(other['columns'], index['columns'])):
                redundant_statements.add(index['statement'])
                checks.append(_check('warn', 'duplicate_in_recommendation',
                    f"{index['name'] or 'An index'} ({', '.join(index['columns'])}) is redundant next to "
                    f"{other['name'] or 'another index'} ({', '.join(other['columns'])}) from the same "
                    "recommendation. Create only the wider one."))
                break

    checked_tables = set()
    for index in parsed:
        if index['statement'] in redundant_statements:
            continue
        stats = _find_stats(index['table'], table_stats)
        label = index['name'] or f"the index on {index['table']}"
        columns = ', '.join(index['columns'])

        if stats is not None:
            clash = False
            for existing in _existing(stats):
                if _key(existing) == _key(index):
                    clash = True
                    checks.append(_check('warn', 'duplicate_index',
                        f"{index['table']} already has {existing['name']} with the same {index['method']} "
                        f"columns ({columns}). {label} would be an exact duplicate."))
                elif (existing['method'] == index['method'] == 'btree' and not existing['predicate']
                        and not index['predicate'] and _starts_with(existing['columns'], index['columns'])):
                    clash = True
                    checks.append(_check('warn', 'covered_by_existing',
                        f"{existing['name']} ({', '.join(existing['columns'])}) already starts with "
                        f"{columns}, so it can serve the same lookups as {label}."))
                elif (existing['method'] == index['method'] == 'btree' and not existing['predicate']
                        and not index['predicate'] and not existing['is_unique']
                        and _starts_with(index['columns'], existing['columns'])):
                    checks.append(_check('info', 'makes_existing_redundant',
                        f"Once {label} exists, {existing['name']} ({', '.join(existing['columns'])}) becomes "
                        f"redundant; dropping it would save {format_bytes(existing['bytes'])}."))
            if not clash:
                checks.append(_check('ok', 'no_overlap',
                    f"No existing index on {index['table']} covers {columns}."))

            if index['table'].lower() not in checked_tables:
                checked_tables.add(index['table'].lower())
                checks.extend(_table_checks(index['table'], stats, building_index=True))

        size = sizes.get(index['statement'])
        if size and size.get('bytes') is not None:
            table_bytes = size.get('table_bytes') or 0
            share = size['bytes'] * 100 / table_bytes if table_bytes else None
            share_text = f", {share:.0f}% of the table's {format_bytes(table_bytes)}" if share is not None else ''
            level = 'warn' if share is not None and share > BIG_INDEX_SHARE else 'info'
            checks.append(_check(level, 'index_size',
                f"{label} measured {format_bytes(size['bytes'])} on a full copy of {index['table']}{share_text}."))

    # Rewrites and settings still depend on the tables' statistics
    if not parsed:
        for name, stats in (table_stats or {}).items():
            if isinstance(stats, dict) and not stats.get('error'):
                checks.extend(_table_checks(name, stats, building_index=False))
    return checks
