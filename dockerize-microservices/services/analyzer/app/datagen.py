"""
Test data generator: fills tables (typically a production schema restored without data) up to
a size the user picks, so slow queries can be reproduced and tuned locally.

Rows are generated on the server with set-based SQL (INSERT ... SELECT ... FROM
generate_series), in batches that are committed one by one until each table has grown by its
target. The values follow the column types and names, respect NOT NULL, UNIQUE, primary and
foreign keys, simple CHECK constraints, enums, domains and partition bounds, and use skewed
distributions for low-cardinality columns so the planner sees realistic statistics.

A job runs in a worker thread and is polled for progress. The analyzer runs one process, so
the job registry can live in memory.
"""
import logging
import math
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2 import sql

from . import config
from .catalog import _one, _rows

logger = logging.getLogger(__name__)

LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1', '[::1]', 'host.docker.internal'}

MIN_BATCH, MAX_BATCH, FIRST_BATCH = 500, 2_000_000, 1_000
# Every row costs at least a 24-byte tuple header and a 4-byte line pointer, so a table cannot hold
# more than target / MIN_ROW_BYTES new rows before it has grown by the target: a hard stop that does
# not depend on the size measurement
MIN_ROW_BYTES = 24
STALL_ROWS = 200_000         # rows inserted with no measured growth at all means the measurement is broken
BATCH_SECONDS = 2.0          # each batch is sized to take about this long
FK_KEY_SAMPLE = 1_000_000    # parent keys sampled for the children to reference
NULL_SHARE = 0.05            # nullable, non-key columns get this share of NULLs

WORDS = ('alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel', 'india', 'juliet',
         'kilo', 'lima', 'mike', 'november', 'oscar', 'papa', 'quebec', 'romeo', 'sierra', 'tango',
         'order', 'invoice', 'customer', 'product', 'shipment', 'payment', 'account', 'report',
         'fast', 'slow', 'blue', 'green', 'red', 'large', 'small', 'new', 'old', 'daily', 'weekly')
FIRST_NAMES = ('James', 'Mary', 'Robert', 'Patricia', 'John', 'Jennifer', 'Michael', 'Linda', 'David',
               'Elizabeth', 'Budi', 'Siti', 'Agus', 'Dewi', 'Wei', 'Yuki', 'Carlos', 'Ana', 'Ahmed', 'Fatima')
LAST_NAMES = ('Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Santoso',
              'Wijaya', 'Tanaka', 'Chen', 'Rodriguez', 'Martinez', 'Hernandez', 'Lopez', 'Wilson', 'Khan')
CITIES = ('Jakarta', 'New York', 'London', 'Tokyo', 'Singapore', 'Sydney', 'Paris', 'Berlin', 'Toronto',
          'Mumbai', 'Sao Paulo', 'Surabaya', 'Bandung', 'Seoul', 'Madrid', 'Chicago', 'Dubai', 'Lagos')
COUNTRIES = ('Indonesia', 'USA', 'UK', 'Japan', 'Singapore', 'Australia', 'France', 'Germany', 'Canada',
             'India', 'Brazil', 'South Korea', 'Spain', 'UAE', 'Nigeria')
STATUSES = ('active', 'pending', 'completed', 'shipped', 'cancelled', 'inactive', 'failed', 'archived')
CATEGORIES = ('electronics', 'clothing', 'books', 'home', 'sports', 'toys', 'food', 'beauty', 'garden', 'auto')

# A "name" column holds a person's name only in tables like these; elsewhere it names a thing
PEOPLE_WORDS = ('customer', 'user', 'employee', 'person', 'people', 'contact', 'member', 'author', 'staff',
                'student', 'patient', 'client', 'account', 'driver', 'agent', 'owner', 'manager')

INTEGER_MAX = {'int2': 32_000, 'int4': 1_000_000, 'int8': 1_000_000}
INTEGER_TYPES = ('int2', 'int4', 'int8')
NUMERIC_TYPES = ('numeric', 'float4', 'float8', 'money')
TEXT_TYPES = ('text', 'varchar', 'bpchar', 'name', 'citext')
TIME_TYPES = ('timestamp', 'timestamptz', 'date')


class DatagenError(Exception):
    pass


# ---------------------------------------------------------------------------
# Small SQL helpers
# ---------------------------------------------------------------------------

def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _text_array(values) -> str:
    return "ARRAY[" + ", ".join(_lit(v) for v in values) + "]::text[]"


def _pick(values, skew: float = 1.0) -> str:
    """One of `values` per row. skew > 1 favours the first values, like real status columns."""
    n = len(values)
    if skew == 1.0:
        index = f"1 + floor(random() * {n})::int"
    else:
        index = f"1 + floor(power(random(), {skew}) * {n})::int"
    return f"({_text_array(values)})[{index}]"


def _hash_index(salt: int, n: int) -> str:
    """
    A deterministic pseudo-random 1..n per row, so one value can be computed twice consistently.
    (Generated SQL always uses mod(), never %: it runs with psycopg2 parameters, where % is a placeholder.)
    """
    return f"(1 + mod(abs(hashint8(g * 7919 + {salt})), {max(n, 1)}))"


def _named(name: str, *words: str) -> bool:
    """Whether a column name contains one of the words: whole tokens for short words, substrings otherwise."""
    tokens = set(re.split(r'[_\W]+', name.lower()))
    return any(word in tokens or (len(word) >= 5 and word in name.lower()) for word in words)


def is_local_host(host: str) -> bool:
    host = (host or '').strip().lower()
    return host in LOCAL_HOSTS or host.startswith('127.')


# ---------------------------------------------------------------------------
# Reading the schema
# ---------------------------------------------------------------------------

_COLUMNS_SQL = """
SELECT a.attnum, a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS type,
       a.attnotnull OR COALESCE(t.typnotnull, false) AS not_null,
       a.attidentity AS identity, a.attgenerated AS generated,
       pg_get_expr(d.adbin, d.adrelid) AS default_expr,
       CASE WHEN t.typtype = 'd' THEN t.typtypmod ELSE a.atttypmod END AS typmod,
       bt.oid AS base_oid, bt.typname AS base, bt.typtype AS base_typtype, bt.typcategory AS category,
       et.oid AS elem_oid, et.typname AS elem, et.typtype AS elem_typtype,
       quote_ident(bn.nspname) || '.' || quote_ident(bt.typname) AS base_qualified,
       CASE WHEN et.oid IS NOT NULL THEN quote_ident(en.nspname) || '.' || quote_ident(et.typname) END AS elem_qualified,
       CASE WHEN t.typtype = 'd' THEN t.oid END AS domain_oid
FROM pg_attribute a
JOIN pg_type t ON t.oid = a.atttypid
JOIN pg_type bt ON bt.oid = CASE WHEN t.typtype = 'd' THEN t.typbasetype ELSE t.oid END
JOIN pg_namespace bn ON bn.oid = bt.typnamespace
LEFT JOIN pg_type et ON et.oid = bt.typelem AND bt.typcategory = 'A'
LEFT JOIN pg_namespace en ON en.oid = et.typnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = %(oid)s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""


def _table_oid(cur, schema: str, table: str) -> Optional[Tuple[int, str]]:
    cur.execute("""
        SELECT c.oid, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %(schema)s AND c.relname = %(table)s AND c.relkind IN ('r', 'p')
    """, {'schema': schema, 'table': table})
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def _enum_labels(cur, type_oid: int) -> List[str]:
    cur.execute("SELECT enumlabel FROM pg_enum WHERE enumtypid = %(oid)s ORDER BY enumsortorder", {'oid': type_oid})
    return [row[0] for row in cur.fetchall()]


def _size_sql(oid: int) -> str:
    """
    Bytes on disk, indexes and TOAST included. A partitioned table stores nothing itself, so its
    size is the sum over its leaf partitions; pg_partition_tree() returns no rows for a plain table.
    """
    oid = int(oid)
    return (f"SELECT CASE WHEN c.relkind = 'p' THEN "
            f"(SELECT COALESCE(SUM(pg_total_relation_size(t.relid)), 0) FROM pg_partition_tree(c.oid) t WHERE t.isleaf) "
            f"ELSE pg_total_relation_size(c.oid) END::bigint FROM pg_class c WHERE c.oid = {oid}")


def inspect_table(cur, schema: str, table: str) -> Optional[Dict[str, Any]]:
    """Everything the generator needs to know about one table."""
    found = _table_oid(cur, schema, table)
    if found is None:
        return None
    oid, relkind = found
    cur.execute(_COLUMNS_SQL, {'oid': oid})
    columns = _rows(cur)
    by_attnum = {c['attnum']: c for c in columns}
    for column in columns:
        column['enum_labels'] = []
        if column['base_typtype'] == 'e':
            column['enum_labels'] = _enum_labels(cur, column['base_oid'])
        elif column['elem_typtype'] == 'e':
            column['enum_labels'] = _enum_labels(cur, column['elem_oid'])
        column['checks'] = []
        if column['domain_oid']:
            cur.execute("SELECT pg_get_constraintdef(oid, true) FROM pg_constraint WHERE contypid = %(oid)s",
                        {'oid': column['domain_oid']})
            # A domain check is written against VALUE; read it as a check on this column
            column['checks'] = [re.sub(r'\bVALUE\b', column['name'], row[0]) for row in cur.fetchall()]

    cur.execute("""
        SELECT c.conname AS name, c.contype AS type, c.conkey AS columns, c.confkey AS ref_columns,
               c.confrelid AS ref_oid, rn.nspname AS ref_schema, rc.relname AS ref_table,
               pg_get_constraintdef(c.oid, true) AS definition
        FROM pg_constraint c
        LEFT JOIN pg_class rc ON rc.oid = c.confrelid
        LEFT JOIN pg_namespace rn ON rn.oid = rc.relnamespace
        WHERE c.conrelid = %(oid)s AND c.contype IN ('p', 'u', 'f', 'c')
    """, {'oid': oid})
    constraints = _rows(cur)

    def names(attnums):
        return [by_attnum[n]['name'] for n in (attnums or []) if n in by_attnum]

    primary_key, uniques, foreign_keys, checks = [], [], [], []
    for con in constraints:
        if con['type'] == 'p':
            primary_key = names(con['columns'])
        elif con['type'] == 'u':
            uniques.append(names(con['columns']))
        elif con['type'] == 'f':
            ref_found = con['ref_oid']
            cur.execute("SELECT attnum, attname FROM pg_attribute WHERE attrelid = %(oid)s AND attnum = ANY(%(nums)s)",
                        {'oid': ref_found, 'nums': list(con['ref_columns'] or [])})
            ref_names = dict(cur.fetchall())
            foreign_keys.append({
                'name': con['name'], 'columns': names(con['columns']),
                'ref_schema': con['ref_schema'], 'ref_table': con['ref_table'],
                'ref_columns': [ref_names[n] for n in con['ref_columns']],
            })
        elif con['type'] == 'c':
            checks.append({'name': con['name'], 'definition': con['definition']})

    # Unique indexes that are not constraints count as unique too
    cur.execute("""
        SELECT i.indkey::int2[] FROM pg_index i
        WHERE i.indrelid = %(oid)s AND i.indisunique AND i.indexprs IS NULL AND i.indpred IS NULL
          AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
    """, {'oid': oid})
    uniques += [names(row[0]) for row in cur.fetchall()]

    partition = None
    if relkind == 'p':
        cur.execute("SELECT partstrat, partattrs::int2[] FROM pg_partitioned_table WHERE partrelid = %(oid)s",
                    {'oid': oid})
        strategy, attrs = cur.fetchone()
        cur.execute("""
            SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
            WHERE i.inhparent = %(oid)s
        """, {'oid': oid})
        bounds = [row[0] for row in cur.fetchall()]
        key_columns = names([a for a in attrs if a != 0])
        partition = {'strategy': {'r': 'range', 'l': 'list', 'h': 'hash'}.get(strategy, strategy),
                     'columns': key_columns, 'bounds': bounds,
                     'has_default': any(b.strip().upper() == 'DEFAULT' for b in bounds),
                     'expression_key': 0 in attrs}

    cur.execute(_size_sql(oid))
    total_bytes = cur.fetchone()[0]
    cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.{})").format(sql.Identifier(schema), sql.Identifier(table)))
    has_rows = cur.fetchone()[0]
    # pg_partition_tree() lists nothing for a plain table, so only use it for partitioned ones
    cur.execute("""
        SELECT CASE WHEN c.relkind = 'p' THEN
                   (SELECT GREATEST(SUM(pc.reltuples), 0) FROM pg_partition_tree(c.oid) t
                    JOIN pg_class pc ON pc.oid = t.relid WHERE t.isleaf)
               ELSE GREATEST(c.reltuples, 0) END::bigint
        FROM pg_class c WHERE c.oid = %(oid)s
    """, {'oid': oid})
    row_estimate = cur.fetchone()[0] or 0

    return {
        'schema': schema, 'name': table, 'oid': oid, 'relkind': relkind,
        'columns': columns, 'primary_key': primary_key, 'uniques': [u for u in uniques if u],
        'foreign_keys': foreign_keys, 'checks': checks, 'partition': partition,
        'total_bytes': total_bytes, 'is_empty': not has_rows, 'row_estimate': row_estimate if has_rows else 0,
    }


# ---------------------------------------------------------------------------
# CHECK constraints we can honour
# ---------------------------------------------------------------------------

# A column as pg_get_constraintdef writes it: plain, or double-quoted ("Qty", "Order ID")
_IDENT = r'(?:"((?:[^"]|"")+)"|([A-Za-z_][A-Za-z0-9_$]*))'
# A cast, as many times as it is applied: ::numeric, ::character varying, ::text[]. It never
# contains AND/OR because the body is split into its AND parts before these patterns run.
_CASTS = r'(?:\s*::\s*(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_ ]*?)(?:\(\d+(?:,\s*\d+)?\))?(?:\[\])?)*'
_NUMBER = r"\(*\s*'?(-?\d+(?:\.\d+)?)'?\s*\)*" + _CASTS
_OPERAND = r"\(*\s*" + _IDENT + r"\s*\)*" + _CASTS
_COMPARE_RE = re.compile(r"^" + _OPERAND + r"\s*(>=|<=|>|<)\s*" + _NUMBER + r"$")
_COMPARE_REVERSED_RE = re.compile(r"^" + _NUMBER + r"\s*(>=|<=|>|<)\s*" + _OPERAND + r"$")
_COLUMN_COMPARE_RE = re.compile(r"^" + _OPERAND + r"\s*(>=|<=|>|<)\s*" + _OPERAND + r"$")
_IN_ARRAY_RE = re.compile(r"^" + _OPERAND + r"\s*=\s*ANY\s*\(+\s*ARRAY\[(.*)\]\s*" + _CASTS + r"\s*\)+$", re.IGNORECASE)
_IN_LIST_RE = re.compile(r"^" + _OPERAND + r"\s+IN\s*\((.*)\)$", re.IGNORECASE)
_NOT_NULL_RE = re.compile(r"^" + _OPERAND + r"\s+IS\s+NOT\s+NULL$", re.IGNORECASE)
_NOT_EMPTY_RE = re.compile(r"^" + _OPERAND + r"\s*<>\s*''" + _CASTS + r"$")
_QUOTED_RE = re.compile(r"'((?:[^']|'')*)'")
_FLIP = {'<': '>', '<=': '>=', '>': '<', '>=': '<='}


def _ident_of(match, first_group: int = 1) -> str:
    quoted, plain = match.group(first_group), match.group(first_group + 1)
    return quoted.replace('""', '"') if quoted is not None else plain


def _strip_outer_parens(text: str) -> str:
    text = text.strip()
    while text.startswith('(') and text.endswith(')') and _balanced(text[1:-1]):
        text = text[1:-1].strip()
    return text


def _balanced(text: str) -> bool:
    depth, quote = 0, None
    for char in text:
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _split_top_level(text: str, keyword: str) -> List[str]:
    """Split on a keyword (AND / OR) outside brackets and quotes."""
    parts, depth, quote, start, i = [], 0, None, 0, 0
    pattern = re.compile(r"\s" + keyword + r"\s", re.IGNORECASE)
    while i < len(text):
        char = text[i]
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif depth == 0:
            match = pattern.match(text, i)
            if match:
                parts.append(text[start:i])
                start = i = match.end()
                continue
        i += 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _list_values(text: str) -> List[str]:
    """The items of an IN (...) or ARRAY[...] list: quoted strings or plain numbers."""
    quoted = [v.replace("''", "'") for v in _QUOTED_RE.findall(text)]
    if quoted:
        return quoted
    return [m.group(1) for m in re.finditer(r"(-?\d+(?:\.\d+)?)", text)]


def parse_checks(table: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Per column: allowed values (IN / = ANY), numeric bounds (with strictness), "never NULL",
    and "greater than another column". Each CHECK is split into its AND parts first, so one
    condition can never swallow the next. Anything else is left to PostgreSQL, and is listed as
    a note: rows that break it stop the table with the check's name.
    """
    column_names = {c['name'] for c in table['columns']}
    rules: Dict[str, Dict[str, Any]] = {}
    definitions = [c['definition'] for c in table['checks']]
    for column in table['columns']:
        definitions += [f"CHECK ({check})" for check in column['checks']]

    def unparsed(definition):
        rules.setdefault('__unparsed__', {}).setdefault('checks', []).append(definition)

    for definition in definitions:
        body = _strip_outer_parens(re.sub(r"^CHECK\s*", "", definition.strip(), flags=re.IGNORECASE))
        body = re.sub(r"\s+NOT VALID$", "", body)
        if len(_split_top_level(body, 'OR')) > 1:
            unparsed(definition)  # alternatives are too loose to turn into a generator
            continue
        understood_all = True
        for part in _split_top_level(body, 'AND'):
            part = _strip_outer_parens(part)
            if not _apply_condition(part, column_names, rules):
                understood_all = False
        if not understood_all:
            unparsed(definition)
    return rules


def _apply_condition(part: str, column_names, rules) -> bool:
    """Turn one AND part of a CHECK into a rule. False if it is not a form we understand."""
    match = _IN_ARRAY_RE.match(part) or _IN_LIST_RE.match(part)
    if match:
        column, values = _ident_of(match), _list_values(match.group(3))
        if column in column_names and values:
            rules.setdefault(column, {})['values'] = values
            return True
        return False
    match = _NOT_NULL_RE.match(part)
    if match and _ident_of(match) in column_names:
        rules.setdefault(_ident_of(match), {})['not_null'] = True
        return True
    match = _NOT_EMPTY_RE.match(part)
    if match and _ident_of(match) in column_names:
        return True  # generated text is never empty
    match = _COLUMN_COMPARE_RE.match(part)
    if match:
        left, op, right = _ident_of(match, 1), match.group(3), _ident_of(match, 4)
        if left in column_names and right in column_names:
            # Keep the "bigger" column as the dependent one: a > b, a >= b
            if op in ('<', '<='):
                left, right, op = right, left, _FLIP[op]
            rules.setdefault(left, {})['greater_than'] = (right, op == '>=')
            return True
        return False
    match = _COMPARE_RE.match(part)
    if match:
        column, op, number = _ident_of(match), match.group(3), float(match.group(4))
    else:
        match = _COMPARE_REVERSED_RE.match(part)
        if not match:
            return False
        number, op, column = float(match.group(1)), _FLIP[match.group(2)], _ident_of(match, 3)
    if column not in column_names:
        return False
    rule = rules.setdefault(column, {})
    if op in ('>', '>='):
        bound = (number, op == '>')
        if 'min' not in rule or (bound[0], bound[1]) > (rule['min'][0], rule['min'][1]):
            rule['min'] = bound
    else:
        bound = (number, op == '<')
        if 'max' not in rule or (bound[0], not bound[1]) < (rule['max'][0], not rule['max'][1]):
            rule['max'] = bound
    return True


# ---------------------------------------------------------------------------
# Partition bounds
# ---------------------------------------------------------------------------

_RANGE_BOUND_RE = re.compile(r"FROM \((.*)\) TO \((.*)\)", re.IGNORECASE)
_LIST_BOUND_RE = re.compile(r"IN \((.*)\)", re.IGNORECASE)


def _bound_value(text: str) -> Optional[str]:
    text = text.strip()
    if text.upper() in ('MINVALUE', 'MAXVALUE'):
        return None
    quoted = _QUOTED_RE.fullmatch(text)
    return quoted.group(1).replace("''", "'") if quoted else text


def partition_ranges(partition: Dict[str, Any]) -> List[Tuple[str, str]]:
    ranges = []
    for bound in partition['bounds']:
        match = _RANGE_BOUND_RE.search(bound)
        if not match or ',' in match.group(1):
            continue  # multi-column range keys: the default partition, if any, takes the rows
        low, high = _bound_value(match.group(1)), _bound_value(match.group(2))
        if low is not None and high is not None:
            ranges.append((low, high))
    return ranges


def partition_list_values(partition: Dict[str, Any]) -> List[str]:
    values = []
    for bound in partition['bounds']:
        match = _LIST_BOUND_RE.search(bound)
        if match:
            values += [v for v in (_bound_value(part) for part in match.group(1).split(',')) if v not in (None, 'NULL')]
    return values


# ---------------------------------------------------------------------------
# Value expressions
# ---------------------------------------------------------------------------

def _limit_text(expression: str, typmod: int, base: str) -> str:
    if base in ('varchar', 'bpchar') and typmod and typmod > 4:
        return f"left({expression}, {typmod - 4})"
    return expression


def _numeric_bounds(column: Dict[str, Any], rule: Dict[str, Any]) -> Tuple[float, float, int]:
    base, typmod = column['base'], column['typmod'] or -1
    scale = 0
    high = INTEGER_MAX.get(base, 100_000)
    if base == 'numeric' and typmod > 4:
        precision, scale = ((typmod - 4) >> 16) & 0xFFFF, (typmod - 4) & 0xFFFF
        high = min(high, 10 ** (precision - scale) - 1) if precision > scale else 0.9
    elif base in ('float4', 'float8', 'money'):
        scale = 2
    low = 0 if base not in INTEGER_TYPES else 1
    name = column['name'].lower()
    if _named(name, 'price', 'amount', 'total', 'cost', 'salary', 'balance', 'fee', 'revenue', 'tax'):
        high = min(high, 5000)
        scale = max(scale, 2) if base not in INTEGER_TYPES else 0
    elif _named(name, 'qty', 'quantity', 'count', 'stock', 'units'):
        low, high = 0, min(high, 500)
    elif _named(name, 'age'):
        low, high = 18, min(high, 90)
    elif _named(name, 'year', 'yr'):
        low, high = 1990, min(high, 2030)
    elif _named(name, 'rating', 'score', 'stars'):
        low, high = 1, min(high, 5)
    elif _named(name, 'percent', 'pct', 'ratio'):
        low, high = 0, min(high, 100)
    # A strict bound moves by the smallest step the column can hold: 1 for integers,
    # 10^-scale for numeric (discount < 1 on numeric(4,3) means at most 0.999)
    step = 1 if base in INTEGER_TYPES else 10 ** -max(scale, 2 if base in ('float4', 'float8') else scale)
    if 'min' in rule:
        value, strict = rule['min']
        low = value + step if strict else value
    if 'max' in rule:
        value, strict = rule['max']
        high = value - step if strict else value
    if base in INTEGER_TYPES:
        low, high = math.ceil(low), math.floor(high)
    if high < low:
        high = low
    return low, high, scale


def _number_expr(column: Dict[str, Any], rule: Dict[str, Any]) -> str:
    low, high, scale = _numeric_bounds(column, rule)
    if column['base'] in INTEGER_TYPES:
        return f"({int(low)} + floor(random() * {int(high) - int(low) + 1})::bigint)"
    expression = f"({low} + random() * {high - low})"
    return f"round({expression}::numeric, {scale})" if column['base'] in ('numeric', 'money') else expression


def _time_expr(column: Dict[str, Any], salt: int) -> str:
    name, base = column['name'].lower(), column['base']
    span = "interval '730 days'"
    if _named(name, 'birth', 'dob', 'birthday', 'birthdate'):
        span, offset = "interval '60 years'", "interval '18 years'"
    else:
        offset = "interval '0'"
    if base == 'date':
        return f"(current_date - {offset} - (random() * {span}))::date"
    return f"(now() - {offset} - random() * {span})::{base}"


def _text_expr(column: Dict[str, Any], unique: bool, table_name: str = '') -> str:
    name, typmod, base = column['name'].lower(), column['typmod'] or -1, column['base']
    people = any(word in table_name.lower() for word in PEOPLE_WORDS)
    width = typmod - 4 if base in ('varchar', 'bpchar') and typmod > 4 else None
    if unique:
        if _named(name, 'email', 'mail'):
            expression = "'user' || g || '@example.com'"
        elif width is not None and width < 12:
            expression = "to_hex(g)"
        else:
            expression = f"'{name[:6]}_' || g"
        return _limit_text(expression, typmod, base)

    if _named(name, 'email', 'mail'):
        expression = "'user' || (1 + floor(random() * 100000)::int) || '@example.com'"
    elif name in ('first_name', 'firstname', 'given_name'):
        expression = _pick(FIRST_NAMES)
    elif name in ('last_name', 'lastname', 'surname', 'family_name'):
        expression = _pick(LAST_NAMES)
    elif name in ('full_name', 'fullname', 'customer_name', 'username', 'user_name', 'display_name') or (
            name == 'name' and people):
        expression = f"{_pick(FIRST_NAMES)} || ' ' || {_pick(LAST_NAMES)}"
    elif name in ('name', 'title', 'label') or name.endswith('_name'):
        expression = f"initcap({_pick(WORDS)}) || ' ' || {_pick(WORDS)} || ' ' || (1 + floor(random() * 1000)::int)"
    elif _named(name, 'city', 'town'):
        expression = _pick(CITIES, skew=1.6)
    elif _named(name, 'country', 'nation'):
        expression = _pick(COUNTRIES, skew=1.8)
    elif _named(name, 'status', 'state'):
        expression = _pick(STATUSES, skew=2.2)
    elif _named(name, 'category', 'type', 'kind', 'segment', 'tier', 'level', 'channel', 'group', 'class'):
        expression = _pick(CATEGORIES, skew=1.7)
    elif _named(name, 'phone', 'mobile', 'tel', 'fax'):
        expression = "'+1-555-' || lpad((floor(random() * 10000000))::int::text, 7, '0')"
    elif _named(name, 'url', 'website', 'link', 'uri'):
        expression = "'https://example.com/' || md5(random()::text)"
    elif _named(name, 'code', 'sku', 'ref', 'number', 'no', 'num', 'serial', 'barcode'):
        expression = "upper(left(md5(random()::text), 10))"
    elif _named(name, 'description', 'desc', 'note', 'notes', 'comment', 'body', 'content', 'message', 'text',
                'summary', 'title', 'remark', 'remarks', 'detail', 'details'):
        words = _text_array(WORDS)
        expression = (f"array_to_string(ARRAY(SELECT ({words})[1 + floor(random() * {len(WORDS)})::int] "
                      f"FROM generate_series(1, 4 + mod(g, 12)) WHERE g > 0), ' ')")
    elif _named(name, 'address', 'street', 'addr'):
        expression = f"(1 + floor(random() * 999)::int) || ' ' || {_pick(WORDS)} || ' Street'"
    elif _named(name, 'zip', 'postal', 'postcode', 'zipcode'):
        expression = "lpad((floor(random() * 99999))::int::text, 5, '0')"
    elif _named(name, 'currency', 'ccy'):
        expression = _pick(('USD', 'IDR', 'EUR', 'JPY', 'SGD', 'GBP'), skew=2.0)
    else:
        expression = f"{_pick(WORDS)} || '-' || left(md5(random()::text), 8)"
    return _limit_text(expression, typmod, base)


def _geometry_expr(column: Dict[str, Any]) -> str:
    match = re.search(r"\((\w+)(?:\s*,\s*(\d+))?\)", column['type'])
    shape = (match.group(1) if match else 'point').lower()
    srid = int(match.group(2)) if match and match.group(2) else 4326
    point = f"ST_SetSRID(ST_MakePoint(-180 + random() * 360, -85 + random() * 170), {srid})"
    if srid == 4326:  # a city-sized area reads more like real data than the whole globe
        point = f"ST_SetSRID(ST_MakePoint(106.6 + random() * 0.6, -6.4 + random() * 0.5), {srid})"
    if 'polygon' in shape:
        expression = f"ST_Buffer({point}, 0.001 + random() * 0.01)"
        expression = f"ST_Multi({expression})" if shape.startswith('multi') else expression
    elif 'line' in shape:
        expression = f"ST_MakeLine({point}, ST_Translate({point}, random() * 0.01, random() * 0.01))"
        expression = f"ST_Multi({expression})" if shape.startswith('multi') else expression
    elif shape.startswith('multipoint'):
        expression = f"ST_Multi({point})"
    else:
        expression = point
    if column['base'] == 'geography':
        return f"({expression})::geography"
    return expression


def _vector_expr(column: Dict[str, Any]) -> str:
    dims = column['typmod'] if column['typmod'] and column['typmod'] > 0 else 3
    return (f"('[' || array_to_string(ARRAY(SELECT round(random()::numeric, 4) "
            f"FROM generate_series(1, {dims}) WHERE g > 0), ',') || ']')::{column['base_qualified']}")


def scalar_expr(column: Dict[str, Any], rule: Dict[str, Any], unique: bool, salt: int,
                table_name: str = '') -> Optional[str]:
    """A SQL expression producing one value of the column per row `g`, or None if the type is unknown."""
    base, category = column['base'], column['category']
    if rule.get('values'):
        values = rule['values']
        index = _hash_index(salt, len(values)) if unique else f"1 + floor(random() * {len(values)})::int"
        return f"({_text_array(values)})[{index}]::{column['base_qualified']}"
    if column['enum_labels'] and category != 'A':
        labels = column['enum_labels']
        return f"({_text_array(labels)})[1 + floor(power(random(), 1.5) * {len(labels)})::int]::{column['base_qualified']}"

    if base in INTEGER_TYPES:
        return "g" if unique else _number_expr(column, rule)
    if base in NUMERIC_TYPES:
        return "g" if unique else _number_expr(column, rule)
    if base == 'bool':
        return "(random() < 0.5)"
    if base in TEXT_TYPES:
        return _text_expr(column, unique, table_name)
    if base in TIME_TYPES:
        if unique:
            step = "interval '1 day'" if base == 'date' else "interval '1 second'"
            return f"('2020-01-01'::timestamp + g * {step})::{base}"
        return _time_expr(column, salt)
    if base == 'time':
        return "(random() * interval '24 hours')::time"
    if base == 'timetz':
        return "(random() * interval '24 hours')::time::timetz"
    if base == 'interval':
        return "(random() * interval '30 days')"
    if base == 'uuid':
        return "md5(g::text || random()::text || clock_timestamp()::text)::uuid"
    if base in ('json', 'jsonb'):
        return (f"jsonb_build_object('id', g, 'type', {_pick(CATEGORIES, skew=1.7)}, "
                f"'status', {_pick(STATUSES, skew=2.0)}, 'value', round((random() * 1000)::numeric, 2), "
                f"'tags', jsonb_build_array({_pick(WORDS)}, {_pick(WORDS)}))::{base}")
    if base == 'bytea':
        return "decode(md5(g::text || random()::text), 'hex')"
    if base in ('inet', 'cidr'):
        address = "('10.' || ((g >> 16) & 255) || '.' || ((g >> 8) & 255) || '.' || (g & 255))"
        return f"{address}::inet" if base == 'inet' else f"({address} || '/32')::cidr"
    if base == 'macaddr':
        return "regexp_replace(left(md5(g::text), 12), '(..)(?!$)', '\\1:', 'g')::macaddr"
    if base == 'tsvector':
        words = _text_array(WORDS)
        return (f"to_tsvector('simple', array_to_string(ARRAY(SELECT ({words})[1 + floor(random() * {len(WORDS)})::int] "
                f"FROM generate_series(1, 6) WHERE g > 0), ' '))")
    if base in ('geometry', 'geography'):
        return _geometry_expr(column)
    if base in ('vector', 'halfvec'):
        return _vector_expr(column)
    if base == 'oid':
        return "mod(g, 100000)::oid"
    if base in ('int4range', 'int8range', 'numrange'):
        cast = {'int4range': 'int', 'int8range': 'bigint', 'numrange': 'numeric'}[base]
        return (f"{base}(mod(g, 1000)::{cast}, (mod(g, 1000) + 1 + mod(g * 7, 100))::{cast})")
    if base in ('tsrange', 'tstzrange'):
        start = "(timestamp '2024-01-01' + mod(g, 700) * interval '1 day' + mod(g * 13, 24) * interval '1 hour')"
        kind = 'timestamptz' if base == 'tstzrange' else 'timestamp'
        return f"{base}({start}::{kind}, ({start} + (1 + mod(g * 7, 72)) * interval '1 hour')::{kind})"
    if base == 'daterange':
        return "daterange(date '2024-01-01' + mod(g, 700)::int, date '2024-01-01' + (mod(g, 700) + 1 + mod(g * 7, 30))::int)"
    if base == 'bit':
        bits = column['typmod'] if column['typmod'] and column['typmod'] > 0 else 1
        if bits <= 31:
            return f"mod(abs(hashint8(g * 31 + {salt})), 2147483647)::int::bit({bits})"
        if bits <= 63:
            return f"abs(hashint8(g * 31 + {salt}))::bigint::bit({bits})"
        return None
    if base == 'varbit':
        return f"mod(abs(hashint8(g * 31 + {salt})), 256)::int::bit(8)::varbit"
    if base == 'xml':
        return f"('<item id=\"' || g || '\" status=\"' || {_pick(STATUSES, skew=2.0)} || '\"/>')::xml"
    if base in ('point',):
        return "point(random() * 100, random() * 100)"
    return None


def _array_expr(column: Dict[str, Any], rule: Dict[str, Any], salt: int) -> Optional[str]:
    element = {
        'name': column['name'], 'type': column['elem'], 'base': column['elem'], 'typmod': -1,
        'category': None, 'enum_labels': column['enum_labels'], 'base_qualified': column['elem_qualified'],
    }
    if column['enum_labels']:
        pick = f"({_text_array(column['enum_labels'])})[1 + floor(random() * {len(column['enum_labels'])})::int]"
        return f"ARRAY[{pick}, {pick}]::{column['elem_qualified']}[]"
    single = scalar_expr(element, {}, False, salt)
    if single is None:
        return None
    return f"ARRAY[{single}, {single}]::{column['elem_qualified']}[]"


# ---------------------------------------------------------------------------
# Planning one table
# ---------------------------------------------------------------------------

def _serial_default(column: Dict[str, Any]) -> bool:
    return bool(column['identity']) or 'nextval(' in (column['default_expr'] or '')


def plan_table(table: Dict[str, Any]) -> Dict[str, Any]:
    """
    How each column will be filled. Returns the table dict extended with `column_plans`
    (name, how, kind: 'default' | 'expr' | 'fk' | 'self' | 'null' | 'partition'),
    `problems` (blocking) and `notes`.
    """
    rules = parse_checks(table)
    problems, notes = [], []
    unparsed = rules.pop('__unparsed__', {}).get('checks', [])
    for definition in unparsed:
        notes.append(f"Check not understood, rows that break it stop the load: {definition}")

    unique_columns = set()
    for columns in [table['primary_key']] + table['uniques']:
        if len(columns) == 1:
            unique_columns.add(columns[0])
        elif columns:
            # A multi-column key is unique if any one of its columns is; pick one that we can make unique
            candidates = [c for c in columns if not any(c in fk['columns'] for fk in table['foreign_keys'])]
            unique_columns.add((candidates or columns)[0])

    fk_by_column = {}
    for fk in table['foreign_keys']:
        for position, column in enumerate(fk['columns']):
            fk_by_column[column] = (fk, position)

    # Unique keys made only of foreign-key columns: those foreign keys are enumerated, not sampled
    sequential_fks = []
    for columns in [table['primary_key']] + table['uniques']:
        if columns and all(c in fk_by_column for c in columns):
            for c in columns:
                name = fk_by_column[c][0]['name']
                if name not in sequential_fks:
                    sequential_fks.append(name)

    partition = table['partition']
    plans = []
    for salt, column in enumerate(table['columns'], start=1):
        name = column['name']
        rule = rules.get(name, {})
        entry = {'name': name, 'type': column['type'], 'kind': 'expr', 'expr': None, 'how': ''}

        if column['identity'] == 'a' or column['generated'] == 's':
            entry.update(kind='default', how='generated by the database')
        elif name in fk_by_column:
            fk, position = fk_by_column[name]
            is_self = (fk['ref_schema'], fk['ref_table']) == (table['schema'], table['name'])
            if is_self:
                ref_column = fk['ref_columns'][position]
                ref_plan = next((p for p in plans if p['name'] == ref_column), None)
                counts_up = ref_plan and ref_plan['kind'] == 'expr' and ref_plan['expr'] == 'g'
                if counts_up and table['is_empty'] and len(fk['columns']) == 1:
                    # A tree, like managers and employees: each row points to an earlier one. The
                    # rows of one INSERT are checked together, so the parent may be in the same batch.
                    if column['not_null']:
                        expression = "CASE WHEN g = 1 THEN 1 ELSE 1 + floor(random() * (g - 1))::bigint END"
                        how = "an earlier row of this table (a tree; the first row is its own root)"
                    else:
                        expression = "CASE WHEN g = 1 OR random() < 0.2 THEN NULL ELSE 1 + floor(random() * (g - 1))::bigint END"
                        how = "an earlier row of this table (a tree; about 20% are top level)"
                    entry.update(kind='expr', expr=expression, how=how)
                elif not column['not_null']:
                    entry.update(kind='null', how=f"NULL (refers to this table: {fk['name']})")
                elif counts_up:
                    entry.update(kind='expr', expr='g', how="refers to its own row")
                else:
                    problems.append(f"{name} must refer to a row of this same table ({fk['name']}), "
                                    "which cannot be generated. Make it nullable or drop the constraint for the load.")
                    entry.update(kind='null', how='cannot be generated')
            else:
                entry.update(kind='fk', fk=fk['name'], position=position,
                             how=f"random existing {fk['ref_table']}.{fk['ref_columns'][position]}")
        elif _serial_default(column) and name not in rule:
            entry.update(kind='default', how='sequence / identity')
        elif partition and name in partition['columns'] and partition['strategy'] in ('range', 'list') and not unique_columns & {name}:
            entry.update(kind='partition', how=f"spread over the {partition['strategy']} partitions")
        else:
            unique = name in unique_columns
            if 'greater_than' in rule:
                other, inclusive = rule['greater_than']
                entry.update(kind='after', after=other, inclusive=inclusive,
                             how=f"after {other} (check constraint)")
            else:
                expression = (_array_expr(column, rule, salt) if column['category'] == 'A'
                              else scalar_expr(column, rule, unique, salt, table['name']))
                if expression is None:
                    if column['not_null'] and column['default_expr']:
                        entry.update(kind='default', how='column default (type not supported)')
                    elif not column['not_null']:
                        entry.update(kind='null', how=f"NULL (type {column['type']} not supported)")
                    else:
                        problems.append(f"Cannot generate values of type {column['type']} for NOT NULL column {name}.")
                        entry.update(kind='null', how='cannot be generated')
                else:
                    if (not column['not_null'] and not rule.get('not_null') and not unique
                            and name not in table['primary_key']):
                        expression = f"CASE WHEN random() < {NULL_SHARE} THEN NULL ELSE {expression} END"
                    entry.update(expr=expression, how=_describe(column, rule, unique))
        plans.append(entry)

    if partition and partition['expression_key']:
        notes.append("The partition key is an expression; rows go wherever it sends them.")
    if partition and partition['strategy'] == 'range' and not partition_ranges(partition) and not partition['has_default']:
        problems.append("The partition bounds could not be read, and there is no default partition.")
    if partition and not table['partition']['bounds']:
        problems.append("The table is partitioned but has no partitions yet. Create them first.")

    table = dict(table)
    table.update(column_plans=plans, problems=problems, notes=notes, sequential_fks=sequential_fks)
    return table


def _describe(column: Dict[str, Any], rule: Dict[str, Any], unique: bool) -> str:
    if rule.get('values'):
        return f"one of {', '.join(rule['values'][:5])}{'…' if len(rule['values']) > 5 else ''} (check)"
    if column['enum_labels']:
        return f"enum value ({len(column['enum_labels'])} labels, skewed)"
    if unique:
        return "unique, sequential"
    if column['base'] in INTEGER_TYPES + NUMERIC_TYPES:
        low, high, _ = _numeric_bounds(column, rule)
        return f"random {low:g} to {high:g}"
    if column['base'] in TIME_TYPES:
        return "random, last 2 years"
    if column['base'] in TEXT_TYPES:
        return "text by column name"
    return f"random {column['base']}"


# ---------------------------------------------------------------------------
# Building the INSERT
# ---------------------------------------------------------------------------

def _partition_expr(table: Dict[str, Any], column: Dict[str, Any], salt: int) -> str:
    partition, cast = table['partition'], column['base_qualified']
    if partition['strategy'] == 'list':
        values = partition_list_values(partition)
        if not values:
            return f"NULL::{cast}"
        return f"({_text_array(values)})[{_hash_index(salt, len(values))}]::{cast}"
    ranges = partition_ranges(partition)
    if not ranges:
        return scalar_expr(column, {}, False, salt) or f"NULL::{cast}"
    lows = f"({_text_array([r[0] for r in ranges])})"
    highs = f"({_text_array([r[1] for r in ranges])})"
    k = _hash_index(salt, len(ranges))
    if column['base'] in INTEGER_TYPES:
        return (f"({lows}[{k}]::bigint + floor(random() * ({highs}[{k}]::bigint - {lows}[{k}]::bigint))::bigint)::{cast}")
    if column['base'] == 'date':
        return f"({lows}[{k}]::date + floor(random() * ({highs}[{k}]::date - {lows}[{k}]::date))::int)"
    if column['base'] in ('timestamp', 'timestamptz'):
        return (f"({lows}[{k}]::{column['base']} + random() * "
                f"({highs}[{k}]::{column['base']} - {lows}[{k}]::{column['base']}))")
    if column['base'] == 'numeric':
        return f"({lows}[{k}]::numeric + random() * ({highs}[{k}]::numeric - {lows}[{k}]::numeric))"
    return f"{lows}[{k}]::{cast}"


def _after_expr(column: Dict[str, Any], other_alias: str, inclusive: bool) -> str:
    if column['base'] == 'date':
        return f"({other_alias} + {0 if inclusive else 1} + floor(random() * 30)::int)"
    if column['base'] in ('timestamp', 'timestamptz'):
        return f"({other_alias} + interval '1 minute' + random() * interval '30 days')"
    return f"({other_alias} + {0 if inclusive else 1} + floor(random() * 100))"


def build_insert(table: Dict[str, Any], fk_keys: Dict[str, Dict[str, Any]], estimate: bool = False):
    """
    (INSERT statement taking %(first)s and %(last)s, column names) for the planned table.
    fk_keys maps a foreign key's name to its key table {'table': temp name, 'count': n} or None.
    In estimate mode foreign keys get placeholder numbers, so no parent data is needed.
    """
    columns = {c['name']: c for c in table['columns']}
    inner, outer, joins, targets = [], [], [], []
    aliases = {}
    divisor = 1
    for index, plan in enumerate(table['column_plans']):
        if plan['kind'] == 'default':
            continue
        column = columns[plan['name']]
        alias = f"c{index}"
        aliases[plan['name']] = alias
        targets.append(plan['name'])
        cast = column['type']
        if plan['kind'] == 'null':
            inner.append(f"NULL::{cast} AS {alias}")
        elif plan['kind'] == 'expr':
            inner.append(f"({plan['expr']})::{cast} AS {alias}")
        elif plan['kind'] == 'partition':
            inner.append(f"({_partition_expr(table, column, index + 101)})::{cast} AS {alias}")
        elif plan['kind'] == 'fk':
            keys = fk_keys.get(plan['fk'])
            if estimate:
                # Placeholder values, only to measure the row size
                inner.append(f"({1 + index} + mod(g, 1000))::{cast} AS {alias}"
                             if column['base'] in INTEGER_TYPES + NUMERIC_TYPES else f"NULL::{cast} AS {alias}")
            elif not keys:
                # The parent has no rows (checked earlier: only allowed when the column is nullable)
                inner.append(f"NULL::{cast} AS {alias}")
            else:
                join_alias = f"k_{keys['table']}"
                if join_alias not in {j[0] for j in joins}:
                    if plan['fk'] in table.get('sequential_fks', []):
                        # Mixed radix over the parents' keys: distinct combinations, in order
                        rn = f"(1 + mod((g - 1) / {divisor}, {keys['count']}))"
                        divisor *= keys['count']
                    else:
                        rn = _hash_index(1000 + len(joins), keys['count'])
                    joins.append((join_alias, f"LEFT JOIN {keys['table']} {join_alias} ON {join_alias}.rn = {rn}"))
                value = f"{join_alias}.k{plan['position']}"
                if not column['not_null'] and plan['fk'] not in table.get('sequential_fks', []):
                    value = f"CASE WHEN random() < {NULL_SHARE} THEN NULL ELSE {value} END"
                inner.append(f"({value})::{cast} AS {alias}")
        elif plan['kind'] == 'after':
            inner.append(f"NULL::{cast} AS {alias}")  # filled by the outer SELECT

    for plan in table['column_plans']:
        if plan['name'] not in aliases:
            continue
        alias = aliases[plan['name']]
        if plan['kind'] == 'after' and plan['after'] in aliases:
            outer.append(f"({_after_expr(columns[plan['name']], aliases[plan['after']], plan['inclusive'])})"
                         f"::{columns[plan['name']]['type']}")
        elif plan['kind'] == 'after':
            outer.append(f"NULL::{columns[plan['name']]['type']}")
        else:
            outer.append(alias)

    target = f"{_ident(table['schema'])}.{_ident(table['name'])}"
    column_list = ", ".join(_ident(name) for name in targets)
    join_sql = "\n".join(j[1] for j in joins)
    inner_sql = ",\n       ".join(inner) if inner else "1 AS dummy"
    select = (f"SELECT {', '.join(outer) if outer else ''}\n"
              f"FROM (SELECT {inner_sql}\n      FROM generate_series(%(first)s::bigint, %(last)s::bigint) AS g\n"
              f"      {join_sql}) AS s")
    return target, column_list, select, targets


def _insert_sql(table, fk_keys) -> Tuple[str, str]:
    target, column_list, select, targets = build_insert(table, fk_keys)
    if not targets:
        return f"INSERT INTO {target} DEFAULT VALUES", 'default'
    return f"INSERT INTO {target} ({column_list})\n{select}", 'select'


# ---------------------------------------------------------------------------
# Whole-schema plan
# ---------------------------------------------------------------------------

def insertion_order(tables: Dict[str, Dict[str, Any]], selected: List[str]) -> Tuple[List[str], List[str]]:
    """Selected tables with every parent before its children, and the tables caught in FK cycles."""
    remaining = {name: {fk['ref_table'] for fk in tables[name]['foreign_keys']
                        if fk['ref_schema'] == tables[name]['schema'] and fk['ref_table'] in selected
                        and fk['ref_table'] != name}
                 for name in selected}
    order = []
    while remaining:
        ready = sorted(name for name, parents in remaining.items() if not parents)
        if not ready:
            break
        for name in ready:
            order.append(name)
            del remaining[name]
        for parents in remaining.values():
            parents.difference_update(ready)
    return order + sorted(remaining), sorted(remaining)


def production_signals(cur, host: str) -> Dict[str, Any]:
    cur.execute("""
        SELECT current_database() AS database,
               pg_database_size(current_database())::bigint AS database_bytes,
               pg_is_in_recovery() AS is_replica,
               (SELECT count(*) FROM pg_stat_activity
                 WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()) AS other_sessions,
               (SELECT count(*) FROM pg_stat_activity
                 WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() AND state = 'active') AS active_sessions
    """)
    signals = _one(cur)
    try:
        cur.execute("SELECT count(*) FROM pg_stat_replication")
        signals['replication_clients'] = cur.fetchone()[0]
    except psycopg2.Error:
        cur.connection.rollback()
        signals['replication_clients'] = None
    signals['host'] = host
    signals['is_local'] = is_local_host(host)
    return signals


def _check_parents_in_other_schemas(cur, schema: str, planned: Dict[str, Dict[str, Any]]) -> None:
    """
    A table that must refer to an empty table in another schema cannot be filled: say so in the
    plan, where the page shows it, instead of failing once the job has started. (Parents in the
    same schema are handled on the page, which can offer to tick them.)
    """
    empty = {}
    for table in planned.values():
        for fk in table['foreign_keys']:
            if fk['ref_schema'] == schema:
                continue
            key = (fk['ref_schema'], fk['ref_table'])
            if key not in empty:
                cur.execute(sql.SQL("SELECT NOT EXISTS (SELECT 1 FROM {}.{})").format(
                    sql.Identifier(fk['ref_schema']), sql.Identifier(fk['ref_table'])))
                empty[key] = cur.fetchone()[0]
            nullable = all(not next(c for c in table['columns'] if c['name'] == col)['not_null']
                           for col in fk['columns'])
            if empty[key] and not nullable:
                table['problems'].append(
                    f"Refers to {fk['ref_schema']}.{fk['ref_table']}, which is empty. Fill that table first "
                    f"(Generate data in the {fk['ref_schema']} schema).")


def plan(conn_params: Dict[str, Any], schema: str, tables: Optional[List[str]] = None) -> Dict[str, Any]:
    """Every table in the schema (or the named ones), how each would be filled, and the safety signals."""
    conn = psycopg2.connect(**conn_params)
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = %s", (config.EXPLORER_TIMEOUT * 4000,))
            # Tables that belong to an extension (PostGIS's spatial_ref_sys) are not user data
            cur.execute("""
                SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %(schema)s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
                  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')
                ORDER BY c.relname
            """, {'schema': schema})
            names = [row[0] for row in cur.fetchall()]
            if tables:
                names = [name for name in names if name in set(tables)]
            planned = {}
            for name in names:
                table = inspect_table(cur, schema, name)
                if table is not None:
                    planned[name] = plan_table(table)
            _check_parents_in_other_schemas(cur, schema, planned)
            for table in planned.values():
                table['bytes_per_row'] = _estimate_row_bytes(cur, table)
            signals = production_signals(cur, conn_params.get('host', ''))
        conn.rollback()
    finally:
        conn.close()

    result = []
    for name, table in planned.items():
        parents = sorted({fk['ref_table'] for fk in table['foreign_keys']
                          if fk['ref_schema'] == schema and fk['ref_table'] != name})
        outside = sorted({f"{fk['ref_schema']}.{fk['ref_table']}" for fk in table['foreign_keys']
                          if fk['ref_schema'] != schema})
        result.append({
            'name': name,
            'kind': 'partitioned table' if table['relkind'] == 'p' else 'table',
            'is_empty': table['is_empty'], 'row_estimate': table['row_estimate'],
            'total_bytes': table['total_bytes'], 'bytes_per_row': table['bytes_per_row'],
            'parents': parents, 'outside_parents': outside,
            'nullable_parents': sorted({fk['ref_table'] for fk in table['foreign_keys']
                                        if all(not next(c for c in table['columns'] if c['name'] == col)['not_null']
                                               for col in fk['columns'])}),
            'partition': table['partition'] and {'strategy': table['partition']['strategy'],
                                                 'count': len(table['partition']['bounds'])},
            'columns': [{'name': p['name'], 'type': p['type'], 'how': p['how']} for p in table['column_plans']],
            'problems': table['problems'], 'notes': table['notes'],
        })
    order, cycles = insertion_order(planned, list(planned))
    return {'tables': result, 'order': order, 'cycles': cycles, 'signals': signals}


def _estimate_row_bytes(cur, table: Dict[str, Any]) -> Optional[int]:
    """Average on-disk bytes per row: generated sample rows, plus tuple and index overhead."""
    if table['problems']:
        return None
    _, _, select, targets = build_insert(table, {}, estimate=True)
    if not targets:
        return 32
    try:
        cur.execute("SAVEPOINT tb_estimate")
        cur.execute(f"SELECT avg(pg_column_size(s.*))::int FROM ({select}) s", {'first': 1, 'last': 500})
        row_bytes = cur.fetchone()[0] or 0
        cur.execute("RELEASE SAVEPOINT tb_estimate")
    except psycopg2.Error as e:
        cur.execute("ROLLBACK TO SAVEPOINT tb_estimate")
        logger.info("Could not estimate %s: %s", table['name'], e)
        return None
    cur.execute("SELECT count(*) FROM pg_index WHERE indrelid = %(oid)s", {'oid': table['oid']})
    indexes = cur.fetchone()[0]
    # 24-byte tuple header and 4-byte line pointer, ~10% free space, ~24 bytes per row per index
    return int((row_bytes + 28) * 1.1 + indexes * 24)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
MAX_JOBS_KEPT = 20


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        snapshot = {k: v for k, v in job.items() if not k.startswith('_')}
        snapshot['tables'] = [dict(t) for t in job['tables']]
    done = sum(min(max(t['bytes'] - t['start_bytes'], 0), t['target_bytes']) for t in snapshot['tables']
               if t['status'] != 'skipped')
    total = sum(t['target_bytes'] for t in snapshot['tables'] if t['status'] != 'skipped') or 1
    snapshot['done_bytes'], snapshot['target_bytes'] = done, total
    snapshot['progress'] = round(min(done / total, 1.0) * 100, 1)
    end = snapshot.get('finished_at') or time.time()
    snapshot['elapsed'] = round(end - snapshot['started_at'], 1)
    rate = done / snapshot['elapsed'] if snapshot['elapsed'] > 0 else 0
    snapshot['rate_bytes'] = int(rate)
    snapshot['eta'] = round((total - done) / rate, 0) if rate > 0 and snapshot['status'] == 'running' else None
    return snapshot


def cancel_job(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return False
        job['_cancel'] = True
        return True


def running_job_for(host: str, port: int, database: str) -> Optional[str]:
    with _jobs_lock:
        for job_id, job in _jobs.items():
            if job['status'] == 'running' and job['_target'] == (host, port, database):
                return job_id
    return None


def start_job(conn_params: Dict[str, Any], schema: str, targets: Dict[str, int]) -> str:
    """Validate the plan for the chosen tables and start loading them in a worker thread."""
    key = (conn_params.get('host'), conn_params.get('port'), conn_params.get('database'))
    if running_job_for(*key):
        raise DatagenError("Data is already being generated in this database. Wait for it to finish or cancel it.")
    targets = {name: int(size) for name, size in targets.items() if int(size) > 0}
    if not targets:
        raise DatagenError("Choose at least one table and a size.")
    limit = config.DATAGEN_MAX_BYTES
    if any(size > limit for size in targets.values()):
        raise DatagenError(f"The largest size allowed per table is {limit // (1024 ** 3)} GB.")

    overview = plan(conn_params, schema)
    if overview['signals']['is_replica']:
        raise DatagenError("This server is a read-only replica.")
    tables = {t['name']: t for t in overview['tables']}
    missing = [name for name in targets if name not in tables]
    if missing:
        raise DatagenError(f"Not found in {schema}: {', '.join(missing)}")
    problems = []
    for name in targets:
        table = tables[name]
        problems += [f"{name}: {p}" for p in table['problems']]
        for parent in table['parents']:
            if parent not in targets and tables.get(parent, {}).get('is_empty') and parent not in table['nullable_parents']:
                problems.append(f"{name} refers to {parent}, which is empty. Tick {parent} too.")
    cycle = [name for name in overview['cycles'] if name in targets]
    if cycle:
        problems.append(f"These tables refer to each other in a cycle: {', '.join(cycle)}.")
    if problems:
        raise DatagenError(" ".join(problems))

    order = [name for name in overview['order'] if name in targets]
    job_id = uuid.uuid4().hex[:12]
    job = {
        'job_id': job_id, 'status': 'running', 'phase': 'preparing', 'schema': schema,
        'database': conn_params.get('database'), 'started_at': time.time(), 'finished_at': None, 'error': None,
        'tables': [{'name': name, 'status': 'pending', 'rows': 0, 'bytes': tables[name]['total_bytes'],
                    'start_bytes': tables[name]['total_bytes'], 'target_bytes': targets[name],
                    'rate_bytes': 0, 'error': None} for name in order],
        '_cancel': False, '_target': key,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        for old in [k for k, v in _jobs.items() if v['status'] != 'running'][:-MAX_JOBS_KEPT]:
            _jobs.pop(old, None)
    threading.Thread(target=_run_job, args=(job, conn_params, schema), name=f"datagen-{job_id}", daemon=True).start()
    return job_id


def _update(target: Dict[str, Any], **changes) -> None:
    """Change a job or one of its table entries while a status request may be reading it."""
    with _jobs_lock:
        target.update(changes)


def _run_job(job: Dict[str, Any], conn_params: Dict[str, Any], schema: str) -> None:
    try:
        conn = psycopg2.connect(**conn_params)
    except psycopg2.Error as e:
        _update(job, status='failed', error=str(e).strip(), finished_at=time.time())
        return
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
            cur.execute("SET synchronous_commit = off")  # safe: a crash only loses the last batches
            conn.commit()
        _update(job, phase='loading')
        failed = set()
        for state in job['tables']:
            if job['_cancel']:
                _update(state, status='skipped', error='Cancelled')
                continue
            with conn.cursor() as cur:
                table = inspect_table(cur, schema, state['name'])
                conn.commit()
            if table is None:
                _update(state, status='failed', error='Table not found')
                failed.add(state['name'])
                continue
            table = plan_table(table)
            blocked = [fk['ref_table'] for fk in table['foreign_keys'] if fk['ref_table'] in failed]
            if blocked:
                _update(state, status='skipped', error=f"{', '.join(blocked)} could not be loaded")
                failed.add(state['name'])
                continue
            try:
                _load_table(conn, job, state, table)
            except psycopg2.errors.DiskFull as e:
                conn.rollback()
                message = "The database server's disk is full. Everything loaded before this batch is kept."
                logger.error("Data generation stopped, disk full: %s", e)
                _update(state, status='failed', error=message)
                for later in job['tables']:
                    if later['status'] == 'pending':
                        _update(later, status='skipped', error='Stopped: disk full')
                _update(job, error=message)
                break
            except Exception as e:
                conn.rollback()
                message = getattr(e, 'pgerror', None) or str(e)
                logger.warning("Data generation for %s failed: %s", state['name'], message)
                _update(state, status='failed', error=message.strip())
                if not state['rows']:
                    failed.add(state['name'])

        _update(job, phase='analyzing')
        for state in job['tables']:
            if state['rows'] and state['status'] in ('done', 'cancelled', 'failed'):
                _update(state, analyzing=True)
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("ANALYZE {}.{}").format(sql.Identifier(schema), sql.Identifier(state['name'])))
                conn.commit()
                _update(state, analyzing=False, analyzed=True)
        _update(job, status='cancelled' if job['_cancel'] else 'done', phase='done', finished_at=time.time())
    except Exception as e:
        logger.exception("Data generation job failed")
        _update(job, status='failed', phase='done', error=str(e).strip(), finished_at=time.time())
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _build_key_tables(conn, table: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """For each foreign key, a temp table of up to FK_KEY_SAMPLE parent keys numbered 1..n."""
    keys = {}
    with conn.cursor() as cur:
        for index, fk in enumerate(table['foreign_keys']):
            if (fk['ref_schema'], fk['ref_table']) == (table['schema'], table['name']):
                continue
            name = f"tb_keys_{index}"
            columns = sql.SQL(', ').join(
                sql.SQL("{} AS {}").format(sql.Identifier(ref), sql.Identifier(f"k{position}"))
                for position, ref in enumerate(fk['ref_columns']))
            not_null = sql.SQL(' AND ').join(sql.SQL("{} IS NOT NULL").format(sql.Identifier(ref))
                                             for ref in fk['ref_columns'])
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(name)))
            cur.execute(sql.SQL(
                "CREATE TEMP TABLE {name} AS SELECT row_number() OVER () AS rn, s.* "
                "FROM (SELECT {columns} FROM {schema}.{table} WHERE {not_null} LIMIT %(limit)s) s"
            ).format(name=sql.Identifier(name), columns=columns, schema=sql.Identifier(fk['ref_schema']),
                     table=sql.Identifier(fk['ref_table']), not_null=not_null), {'limit': FK_KEY_SAMPLE})
            cur.execute(sql.SQL("CREATE INDEX ON {} (rn)").format(sql.Identifier(name)))
            cur.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(name)))
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name)))
            count = cur.fetchone()[0]
            keys[fk['name']] = {'table': name, 'count': count} if count else None
    conn.commit()
    return keys


def _load_table(conn, job: Dict[str, Any], state: Dict[str, Any], table: Dict[str, Any]) -> None:
    fk_keys = _build_key_tables(conn, table)
    for fk in table['foreign_keys']:
        is_self = (fk['ref_schema'], fk['ref_table']) == (table['schema'], table['name'])
        nullable = all(not next(c for c in table['columns'] if c['name'] == col)['not_null'] for col in fk['columns'])
        if not is_self and fk_keys.get(fk['name']) is None and not nullable:
            raise DatagenError(f"{fk['ref_table']} has no rows to refer to ({fk['name']}).")

    with conn.cursor() as cur:
        statement, mode = _insert_sql(table, fk_keys)
        # Continue numbering after existing rows, so unique columns never collide with them
        offset = 0
        for plan_entry in table['column_plans']:
            if plan_entry['kind'] == 'expr' and plan_entry['expr'] == 'g':
                column = next(c for c in table['columns'] if c['name'] == plan_entry['name'])
                if column['base'] in INTEGER_TYPES + NUMERIC_TYPES:
                    cur.execute(sql.SQL("SELECT COALESCE(max({})::bigint, 0) FROM {}.{}").format(
                        sql.Identifier(column['name']), sql.Identifier(table['schema']), sql.Identifier(table['name'])))
                    offset = max(offset, cur.fetchone()[0])
        if offset == 0 and not table['is_empty']:
            cur.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(table['schema']), sql.Identifier(table['name'])))
            offset = cur.fetchone()[0]
        conn.commit()

    size_sql = _size_sql(table['oid'])
    with conn.cursor() as cur:
        cur.execute(size_sql)
        start_bytes = cur.fetchone()[0]
        conn.commit()
    _update(state, status='loading', start_bytes=start_bytes, bytes=start_bytes)
    max_rows = state['target_bytes'] // MIN_ROW_BYTES + MIN_BATCH
    batch, next_g, rows = FIRST_BATCH, offset + 1, 0
    started = time.monotonic()
    while True:
        if job['_cancel']:
            _update(state, status='cancelled')
            return
        batch_started = time.monotonic()
        with conn.cursor() as cur:
            if mode == 'default':
                for _ in range(min(batch, 10_000)):
                    cur.execute(statement)
                inserted = min(batch, 10_000)
            else:
                cur.execute(statement, {'first': next_g, 'last': next_g + batch - 1})
                inserted = cur.rowcount
            conn.commit()
            cur.execute(size_sql)
            size = cur.fetchone()[0]
            conn.commit()
        rows += inserted
        next_g += batch
        grown = size - start_bytes
        elapsed = max(time.monotonic() - started, 0.001)
        _update(state, rows=rows, bytes=size, rate_bytes=int(max(grown, 0) / elapsed))
        if grown >= state['target_bytes'] or inserted == 0:
            break
        if rows >= max_rows:
            logger.warning("%s: stopped at the row ceiling (%d rows) before the size target", table['name'], rows)
            break
        if grown <= 0 and rows >= STALL_ROWS:
            raise DatagenError(f"Stopped: {rows:,} rows were added but the table's size did not change, "
                               "so the target could not be measured.")
        took = max(time.monotonic() - batch_started, 0.01)
        batch = int(min(MAX_BATCH, max(MIN_BATCH, batch * min(BATCH_SECONDS / took, 4.0))))
        batch = min(batch, max(MIN_BATCH, max_rows - rows))
        # Don't overshoot the target by much: stop growing once the end is in sight
        per_row = grown / rows if rows else None
        if per_row:
            batch = max(MIN_BATCH, min(batch, int((state['target_bytes'] - grown) / per_row * 1.05) + 1))
    _update(state, status='done')
