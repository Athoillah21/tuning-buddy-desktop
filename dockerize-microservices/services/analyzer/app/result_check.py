"""
Does a rewritten query still return what the original returned?

Speed means nothing if the answer changed: the AI once turned LIKE '%12345%' into
LIKE '12345%' (10 rows became 0) and it was ranked as the best option. Both queries are
run for a fingerprint - the row count plus an order-insensitive hash of every row - and
compared. Index-only recommendations keep the original SQL and need no run at all.
"""
import re
from typing import Callable, Dict, Optional, Tuple

from .sql_guard import split_statements

SAME, DIFFERENT, UNCHECKED = 'same', 'different', 'unchecked'

# Fingerprint = (row count, hash) or an error message
Fingerprint = Tuple[Optional[int], Optional[str], Optional[str]]

_LIMIT_RE = re.compile(r'\b(limit|fetch\s+first|fetch\s+next)\b', re.IGNORECASE)
_ORDER_BY_RE = re.compile(r'\border\s+by\b', re.IGNORECASE)


def normalize_sql(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '').strip().rstrip(';')).strip().lower()


def split_setup(query: str) -> Tuple[list, str]:
    """Leading SET statements (config recommendations) and the query they apply to."""
    statements = split_statements(query or '')
    if not statements:
        return [], ''
    return statements[:-1], statements[-1]


def is_nondeterministic(query: str) -> bool:
    """LIMIT without ORDER BY: PostgreSQL may return any qualifying rows."""
    return bool(_LIMIT_RE.search(query or '')) and not _ORDER_BY_RE.search(query or '')


def check_result(original: str, rewritten: str,
                 fingerprint: Callable[[str], Fingerprint],
                 original_fingerprint: Optional[Fingerprint] = None) -> Dict[str, Optional[str]]:
    """
    {'status': same|different|unchecked, 'note': ...}. `fingerprint(query)` runs a query and
    returns (rows, hash, error); pass `original_fingerprint` to reuse the original's.
    """
    if not rewritten or normalize_sql(rewritten) == normalize_sql(original):
        return {'status': SAME, 'note': 'Same SQL as the original; only indexes or settings change.'}

    original_fp = original_fingerprint or fingerprint(original)
    if original_fp[2]:
        return {'status': UNCHECKED, 'note': f'The original could not be re-run for comparison: {original_fp[2]}'}
    rewritten_fp = fingerprint(rewritten)
    if rewritten_fp[2]:
        return {'status': UNCHECKED, 'note': f'The rewritten query could not be run for comparison: {rewritten_fp[2]}'}

    original_rows, rewritten_rows = original_fp[0], rewritten_fp[0]
    # Two empty results agree about nothing: a rewrite that adds an arbitrary filter also
    # returns no rows on data where the original finds none
    if original_rows == 0 and rewritten_rows == 0:
        return {'status': UNCHECKED, 'note': 'The original returns no rows on this data, so matching results '
                                             'prove nothing; check the rewrite by reading it.'}
    if original_fp[:2] == rewritten_fp[:2]:
        return {'status': SAME, 'note': f'Returns the same {original_rows:,} row(s) as the original.'}

    # LIMIT without ORDER BY may pick any matching rows, so different rows prove nothing -
    # but a different count does: under the same LIMIT one side ran out of matches
    if is_nondeterministic(original) and original_rows == rewritten_rows:
        return {'status': UNCHECKED, 'note': 'The original uses LIMIT without ORDER BY, so PostgreSQL may '
                                             'return any matching rows; the rows returned cannot be compared.'}

    if original_rows != rewritten_rows:
        note = f'Returns {rewritten_rows:,} row(s) instead of {original_rows:,}.'
    else:
        note = f'Returns {rewritten_rows:,} row(s) like the original, but with different values or columns.'
    if _LIMIT_RE.search(original) and _ORDER_BY_RE.search(original):
        note += (' The query sorts and then limits, so rows tied on the sort key may legitimately differ; '
                 'check before applying.')
    return {'status': DIFFERENT, 'note': note}
