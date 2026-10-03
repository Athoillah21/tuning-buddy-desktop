"""
A rewrite only counts if it returns what the original returned.
"""
from app.optimizer import match_index_sizes
from app.result_check import DIFFERENT, SAME, UNCHECKED, check_result, split_setup

ORIGINAL = "SELECT id, customer_email FROM large_orders WHERE customer_email LIKE '%12345%'"


def _fingerprints(table):
    calls = []

    def fingerprint(sql_text):
        calls.append(sql_text)
        return table[sql_text]
    return fingerprint, calls


def test_wrong_rewrite_is_caught():
    """The benchmark's LIKE '%12345%' -> LIKE '12345%' returned 0 rows instead of 10."""
    rewritten = ORIGINAL.replace("'%12345%'", "'12345%'")
    fingerprint, _ = _fingerprints({ORIGINAL: (10, 'aaa', None), rewritten: (0, 'bbb', None)})
    result = check_result(ORIGINAL, rewritten, fingerprint)
    assert result['status'] == DIFFERENT
    assert '0 row(s) instead of 10' in result['note']


def test_equivalent_rewrite_passes():
    rewritten = "SELECT id, customer_email FROM large_orders WHERE customer_email >= 'x'"
    fingerprint, _ = _fingerprints({ORIGINAL: (372, 'same', None), rewritten: (372, 'same', None)})
    assert check_result(ORIGINAL, rewritten, fingerprint)['status'] == SAME


def test_two_empty_results_prove_nothing():
    """A rewrite added an arbitrary date window; on data where the original finds nothing, it 'matched'."""
    original = "SELECT id FROM measurements WHERE sensor_id = 1234 AND status = 'error'"
    rewritten = original + " AND recorded_at >= '2026-01-01' AND recorded_at < '2026-06-01'"
    fingerprint, _ = _fingerprints({original: (0, 'empty', None), rewritten: (0, 'empty', None)})
    result = check_result(original, rewritten, fingerprint)
    assert result['status'] == UNCHECKED
    assert 'no rows' in result['note']


def test_same_count_but_other_values_is_different():
    rewritten = "SELECT id FROM large_orders WHERE customer_email LIKE '%12345%'"
    fingerprint, _ = _fingerprints({ORIGINAL: (10, 'aaa', None), rewritten: (10, 'bbb', None)})
    result = check_result(ORIGINAL, rewritten, fingerprint)
    assert result['status'] == DIFFERENT
    assert 'different values or columns' in result['note']


def test_index_only_recommendation_needs_no_run():
    fingerprint, calls = _fingerprints({})
    result = check_result(ORIGINAL, "  " + ORIGINAL.upper() + " ;", fingerprint)
    assert result['status'] == SAME
    assert calls == []


def test_limit_without_order_by_with_equal_counts_cannot_be_compared():
    original = "SELECT id FROM large_orders WHERE customer_email LIKE '%1%' LIMIT 20"
    rewritten = "SELECT id FROM large_orders WHERE customer_email LIKE '%1%' AND id > 0 LIMIT 20"
    fingerprint, _ = _fingerprints({original: (20, 'a', None), rewritten: (20, 'b', None)})
    assert check_result(original, rewritten, fingerprint)['status'] == UNCHECKED


def test_limit_without_order_by_still_catches_a_different_count():
    """The benchmark's actual bug: LIKE '%12345%' LIMIT 20 -> LIKE '12345%' LIMIT 20, 10 rows -> 0."""
    original = ORIGINAL + " LIMIT 20"
    rewritten = original.replace("'%12345%'", "'12345%'")
    fingerprint, _ = _fingerprints({original: (10, 'a', None), rewritten: (0, 'b', None)})
    result = check_result(original, rewritten, fingerprint)
    assert result['status'] == DIFFERENT
    assert '0 row(s) instead of 10' in result['note']


def test_sorted_limit_warns_about_ties():
    original = "SELECT id FROM large_orders ORDER BY order_date DESC LIMIT 50"
    rewritten = "SELECT id FROM large_orders ORDER BY order_date DESC, id LIMIT 50"
    fingerprint, _ = _fingerprints({original: (50, 'a', None), rewritten: (50, 'b', None)})
    result = check_result(original, rewritten, fingerprint)
    assert result['status'] == DIFFERENT
    assert 'tied' in result['note']


def test_errors_leave_the_result_unchecked():
    rewritten = "SELECT broken"
    fingerprint, _ = _fingerprints({ORIGINAL: (10, 'a', None), rewritten: (None, None, 'syntax error')})
    result = check_result(ORIGINAL, rewritten, fingerprint)
    assert result['status'] == UNCHECKED
    assert 'syntax error' in result['note']


def test_setup_statements_are_split_from_the_query():
    assert split_setup("SET work_mem = '256MB'; SELECT 1") == (["SET work_mem = '256MB'"], 'SELECT 1')


def test_index_sizes_are_matched_by_name_or_shape():
    built = [
        {'name': 'idx_named', 'method': 'btree', 'columns': ['customer_email'], 'bytes': 100,
         'table': 'large_orders', 'table_bytes': 1000},
        {'name': 'large_orders_lower_idx', 'method': 'btree', 'columns': ['lower(customer_email::text)'],
         'bytes': 200, 'table': 'large_orders', 'table_bytes': 1000},
    ]
    sizes = match_index_sizes([
        "CREATE INDEX idx_named ON large_orders (customer_email)",
        "CREATE INDEX ON large_orders (lower(customer_email))",
    ], built)
    assert [entry['bytes'] for entry in sizes] == [100, 200]
