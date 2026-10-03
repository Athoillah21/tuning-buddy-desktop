"""
Whether a recommendation fits the database as it is: existing indexes, size, partitions.
"""
from app.fit_checks import fit_checks, normalize_column, parse_index


def _stats(**overrides):
    stats = {
        'name': 'large_orders', 'total_bytes': 10 * 1024 * 1024, 'row_estimate': 50_000,
        'is_partitioned': False, 'analyzed': True, 'live_rows': 50_000, 'dead_rows': 0,
        'rows_inserted': 0, 'rows_updated': 0, 'rows_deleted': 0, 'seq_scans': 10, 'index_scans': 10,
        'indexes': [],
    }
    stats.update(overrides)
    return {'large_orders': stats}


def _index(name, columns, method='btree', **extra):
    return {'name': name, 'method': method, 'columns': columns, 'predicate': None,
            'is_unique': False, 'is_primary': False, 'bytes': 2_000_000, **extra}


def _codes(checks):
    return [check['code'] for check in checks]


def test_parse_index_reads_name_table_method_and_columns():
    parsed = parse_index("CREATE INDEX idx_trgm ON public.large_orders USING gin (customer_email gin_trgm_ops);")
    assert parsed['name'] == 'idx_trgm'
    assert parsed['table'] == 'large_orders'
    assert parsed['method'] == 'gin'
    assert parsed['columns'] == ['customer_emailgin_trgm_ops']


def test_parse_index_handles_expressions_include_and_where():
    parsed = parse_index("CREATE INDEX ON events ((payload->>'user_id'), lower(kind)) INCLUDE (id) WHERE id > 5")
    assert parsed['name'] is None
    assert parsed['columns'] == ["payload->>'user_id'", 'lower(kind)']
    assert parsed['predicate'] == 'id>5'


def test_normalize_matches_catalog_spelling():
    # pg_get_indexdef(oid, k, true) spelling versus what the AI writes
    assert normalize_column("(payload ->> 'user_id'::text)") == normalize_column("(payload->>'user_id')")
    assert normalize_column('order_date DESC') == normalize_column('order_date')


def test_exact_duplicate_of_an_existing_index_is_flagged():
    rec = {'all_indexes_applied': ["CREATE INDEX idx_new ON large_orders (customer_email)"]}
    checks = fit_checks(rec, _stats(indexes=[_index('idx_old', ['customer_email'])]))
    assert 'duplicate_index' in _codes(checks)
    assert 'no_overlap' not in _codes(checks)


def test_index_covered_by_a_wider_existing_one():
    rec = {'all_indexes_applied': ["CREATE INDEX idx_new ON large_orders (country)"]}
    checks = fit_checks(rec, _stats(indexes=[_index('idx_country_city', ['country', 'city'])]))
    assert 'covered_by_existing' in _codes(checks)


def test_wider_index_makes_an_existing_one_redundant():
    rec = {'all_indexes_applied': ["CREATE INDEX idx_new ON large_orders (country, city)"]}
    checks = fit_checks(rec, _stats(indexes=[_index('idx_country', ['country'])]))
    assert 'makes_existing_redundant' in _codes(checks)
    assert 'no_overlap' in _codes(checks)


def test_unique_indexes_are_never_called_redundant():
    rec = {'all_indexes_applied': ["CREATE INDEX idx_new ON large_orders (id, amount)"]}
    checks = fit_checks(rec, _stats(indexes=[_index('large_orders_pkey', ['id'], is_primary=True)]))
    assert 'makes_existing_redundant' not in _codes(checks)


def test_different_method_is_not_a_duplicate():
    rec = {'all_indexes_applied': ["CREATE INDEX idx_trgm ON large_orders USING gin (customer_email gin_trgm_ops)"]}
    checks = fit_checks(rec, _stats(indexes=[_index('idx_email', ['customer_email'])]))
    assert 'duplicate_index' not in _codes(checks)
    assert 'no_overlap' in _codes(checks)


def test_duplicates_inside_one_recommendation():
    """The low-selectivity benchmark case applied (status, id) and (status, id, amount)."""
    rec = {'all_indexes_applied': [
        "CREATE INDEX a ON large_orders (order_status, id)",
        "CREATE INDEX b ON large_orders (order_status, id, amount)",
        "CREATE INDEX c ON large_orders (order_status, id, amount)",
    ]}
    checks = fit_checks(rec, _stats())
    assert _codes(checks).count('duplicate_in_recommendation') == 2


def test_partitioned_table_gets_partition_advice_instead_of_lock_advice():
    rec = {'all_indexes_applied': ["CREATE INDEX idx ON large_orders (recorded_at)"]}
    checks = fit_checks(rec, _stats(is_partitioned=True, partition_count=12,
                                    total_bytes=500 * 1024 * 1024, row_estimate=5_000_000))
    assert 'partitioned_table' in _codes(checks)
    assert 'large_table_lock' not in _codes(checks)


def test_large_table_advises_concurrently():
    rec = {'all_indexes_applied': ["CREATE INDEX idx ON large_orders (customer_email)"]}
    checks = fit_checks(rec, _stats(total_bytes=160 * 1024 * 1024, row_estimate=1_000_000))
    assert 'large_table_lock' in _codes(checks)
    assert any('CONCURRENTLY' in check['message'] for check in checks)


def test_write_heavy_table_with_many_indexes():
    rec = {'all_indexes_applied': ["CREATE INDEX idx ON large_orders (amount)"]}
    existing = [_index(f'idx_{n}', [f'col{n}']) for n in range(3)]
    checks = fit_checks(rec, _stats(indexes=existing, rows_inserted=90_000, rows_updated=5_000,
                                    seq_scans=100, index_scans=200))
    assert 'write_heavy' in _codes(checks)


def test_measured_index_size_is_reported_with_its_share():
    statement = "CREATE INDEX idx ON large_orders (notes)"
    rec = {'all_indexes_applied': [statement]}
    sizes = [{'statement': statement, 'bytes': 90 * 1024 * 1024, 'table_bytes': 150 * 1024 * 1024}]
    checks = fit_checks(rec, _stats(), sizes)
    size_check = next(check for check in checks if check['code'] == 'index_size')
    assert size_check['level'] == 'warn'   # 60% of the table
    assert '90.0 MB' in size_check['message'] and '60%' in size_check['message']


def test_never_analyzed_table_is_flagged_even_for_rewrites():
    rec = {'all_indexes_applied': [], 'optimized_query': 'SELECT 1'}
    checks = fit_checks(rec, _stats(analyzed=False))
    assert _codes(checks) == ['stats_stale']


def test_many_dead_rows_are_flagged():
    rec = {'all_indexes_applied': []}
    checks = fit_checks(rec, _stats(live_rows=60_000, dead_rows=40_000))
    assert 'stats_stale' in _codes(checks)


def test_missing_table_stats_do_not_crash():
    rec = {'all_indexes_applied': ["CREATE INDEX idx ON other_table (x)"]}
    assert fit_checks(rec, {}) == []
    assert fit_checks(rec, {'other_table': {'error': 'not found'}}) == []
