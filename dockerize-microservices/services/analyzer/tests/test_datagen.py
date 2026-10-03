"""The test data generator's planning logic: no database needed."""
from app import datagen


def column(name, base='int4', not_null=False, typmod=-1, **extra):
    return {
        'attnum': extra.pop('attnum', 1), 'name': name, 'type': extra.pop('type', base), 'not_null': not_null,
        'identity': extra.pop('identity', ''), 'generated': '', 'default_expr': extra.pop('default_expr', None),
        'typmod': typmod, 'base_oid': 0, 'base': base, 'base_typtype': 'b', 'category': extra.pop('category', 'N'),
        'elem_oid': None, 'elem': None, 'elem_typtype': None, 'base_qualified': f'pg_catalog.{base}',
        'elem_qualified': None, 'domain_oid': None, 'enum_labels': extra.pop('enum_labels', []), 'checks': [],
    }


def table(name, columns, primary_key=(), uniques=(), foreign_keys=(), checks=(), partition=None):
    return {
        'schema': 'public', 'name': name, 'oid': 1, 'relkind': 'p' if partition else 'r', 'columns': columns,
        'primary_key': list(primary_key), 'uniques': [list(u) for u in uniques], 'foreign_keys': list(foreign_keys),
        'checks': [{'name': f'c{i}', 'definition': d} for i, d in enumerate(checks)], 'partition': partition,
        'total_bytes': 0, 'is_empty': True, 'row_estimate': 0,
    }


def test_checks_become_allowed_values_and_bounds():
    t = table('orders', [column('status', 'varchar', typmod=24), column('qty'), column('price', 'numeric')], checks=[
        "CHECK (((status)::text = ANY ((ARRAY['new'::character varying, 'paid'::character varying])::text[])))",
        "CHECK ((qty >= 1) AND (qty <= 10))",
        "CHECK ((price > (0)::numeric))",
    ])
    rules = datagen.parse_checks(t)
    assert rules['status']['values'] == ['new', 'paid']
    assert (rules['qty']['min'], rules['qty']['max']) == ((1, False), (10, False))
    assert rules['price']['min'] == (0, True)          # strictly greater than 0
    assert '__unparsed__' not in rules


def test_column_comparison_makes_one_column_follow_the_other():
    t = table('trips', [column('start_at', 'timestamp'), column('end_at', 'timestamp')],
              checks=["CHECK ((end_at > start_at))"])
    assert datagen.parse_checks(t)['end_at']['greater_than'] == ('start_at', False)


def test_unknown_checks_are_reported_not_guessed():
    t = table('t', [column('a'), column('b')], checks=["CHECK (((a + b) > 10) OR (a IS NULL))"])
    assert datagen.parse_checks(t)['__unparsed__']['checks']


def test_partition_bounds_are_read():
    partition = {'strategy': 'range', 'columns': ['at'], 'has_default': False, 'expression_key': False, 'bounds': [
        "FOR VALUES FROM ('2026-01-01 00:00:00') TO ('2026-02-01 00:00:00')",
        "FOR VALUES FROM (MINVALUE) TO ('2025-01-01')",
        "DEFAULT",
    ]}
    assert datagen.partition_ranges(partition) == [('2026-01-01 00:00:00', '2026-02-01 00:00:00')]
    listed = {'bounds': ["FOR VALUES IN ('ID', 'SG')", "FOR VALUES IN ('US')"]}
    assert datagen.partition_list_values(listed) == ['ID', 'SG', 'US']


def test_parents_are_loaded_before_children():
    tables = {
        'order_items': table('order_items', [], foreign_keys=[
            {'name': 'f1', 'columns': ['order_id'], 'ref_schema': 'public', 'ref_table': 'orders', 'ref_columns': ['id']},
            {'name': 'f2', 'columns': ['product_id'], 'ref_schema': 'public', 'ref_table': 'products', 'ref_columns': ['id']}]),
        'orders': table('orders', [], foreign_keys=[
            {'name': 'f3', 'columns': ['customer_id'], 'ref_schema': 'public', 'ref_table': 'customers', 'ref_columns': ['id']}]),
        'customers': table('customers', []),
        'products': table('products', []),
    }
    order, cycles = datagen.insertion_order(tables, list(tables))
    assert order.index('customers') < order.index('orders') < order.index('order_items')
    assert order.index('products') < order.index('order_items')
    assert cycles == []


def test_cycles_are_reported():
    fk = lambda to: {'name': 'f' + to, 'columns': ['x'], 'ref_schema': 'public', 'ref_table': to, 'ref_columns': ['id']}
    tables = {'a': table('a', [], foreign_keys=[fk('b')]), 'b': table('b', [], foreign_keys=[fk('a')])}
    assert datagen.insertion_order(tables, ['a', 'b'])[1] == ['a', 'b']


def test_column_names_match_whole_words():
    assert datagen._named('order_no', 'no')
    assert not datagen._named('notes', 'no')
    assert datagen._named('customer_email', 'email')
    assert datagen._named('shipping_country', 'country')


def test_serial_keys_use_their_default_and_unique_columns_count_up():
    t = datagen.plan_table(table('customers', [
        column('id', not_null=True, default_expr="nextval('customers_id_seq'::regclass)"),
        column('email', 'varchar', not_null=True, typmod=259, attnum=2),
        column('city', 'text', attnum=3),
    ], primary_key=['id'], uniques=[['email']]))
    plans = {p['name']: p for p in t['column_plans']}
    assert plans['id']['kind'] == 'default'
    assert "'user' || g || '@example.com'" in plans['email']['expr']
    assert 'NULL' in plans['city']['expr']  # nullable columns get a share of NULLs
    assert t['problems'] == []


def test_a_key_made_of_foreign_keys_walks_the_parents_in_order():
    fks = [{'name': 'f_order', 'columns': ['order_id'], 'ref_schema': 'public', 'ref_table': 'orders', 'ref_columns': ['id']},
           {'name': 'f_product', 'columns': ['product_id'], 'ref_schema': 'public', 'ref_table': 'products', 'ref_columns': ['id']}]
    t = datagen.plan_table(table('order_items', [column('order_id', not_null=True), column('product_id', not_null=True, attnum=2)],
                                 primary_key=['order_id', 'product_id'], foreign_keys=fks))
    assert t['sequential_fks'] == ['f_order', 'f_product']
    _, _, select, _ = datagen.build_insert(t, {'f_order': {'table': 'tb_keys_0', 'count': 50},
                                               'f_product': {'table': 'tb_keys_1', 'count': 7}})
    # Mixed radix: distinct (order, product) pairs for the first 50 * 7 rows
    assert '(1 + mod((g - 1) / 1, 50))' in select
    assert '(1 + mod((g - 1) / 50, 7))' in select


def test_not_null_column_of_an_unknown_type_blocks_the_table():
    t = datagen.plan_table(table('t', [column('shape', 'box', not_null=True)]))
    assert t['problems']


def test_local_hosts():
    assert datagen.is_local_host('localhost') and datagen.is_local_host('127.0.0.1')
    assert datagen.is_local_host('host.docker.internal')
    assert not datagen.is_local_host('db.example.com')


def test_generated_sql_has_no_bare_percent_signs():
    """It runs with psycopg2 parameters, where % starts a placeholder."""
    t = datagen.plan_table(table('t', [column('a'), column('note', 'text', attnum=2), column('o', 'oid', attnum=3)]))
    _, _, select, _ = datagen.build_insert(t, {}, estimate=True)
    assert '%' not in select.replace('%(first)s', '').replace('%(last)s', '')


def test_size_of_a_plain_table_does_not_use_the_partition_tree_alone():
    """pg_partition_tree() returns no rows for a plain table; its size must come from the table itself."""
    query = datagen._size_sql(42)
    assert "ELSE pg_total_relation_size(c.oid)" in query
    assert "WHERE c.oid = 42" in query


class _FakeCursor:
    """Inserts always succeed, but the measured size never changes (the bug that filled a disk)."""

    def __init__(self, sizes):
        self.sizes, self.rowcount, self._last = sizes, 0, None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        self._last = statement
        if params and 'first' in params:
            self.rowcount = params['last'] - params['first'] + 1

    def fetchone(self):
        return (self.sizes,)


class _FakeConn:
    def __init__(self, size):
        self.size = size

    def cursor(self):
        return _FakeCursor(self.size)

    def commit(self):
        pass


def test_a_size_that_never_grows_cannot_run_away(monkeypatch):
    monkeypatch.setattr(datagen, '_build_key_tables', lambda conn, table: {})
    t = datagen.plan_table(table('t', [column('a')]))
    state = {'name': 't', 'status': 'pending', 'rows': 0, 'bytes': 0, 'start_bytes': 0,
             'target_bytes': 1024 * 1024, 'rate_bytes': 0, 'error': None}
    job = {'_cancel': False, 'tables': [state]}
    try:
        datagen._load_table(_FakeConn(8192), job, state, t)
    except datagen.DatagenError:
        pass
    # Whichever guard fires first, the rows stay within what 1 MB could possibly hold
    assert state['rows'] <= 1024 * 1024 // datagen.MIN_ROW_BYTES + datagen.MIN_BATCH


def test_name_is_a_person_only_in_tables_of_people():
    person = datagen.scalar_expr(column('name', 'text'), {}, False, 1, 'customers')
    thing = datagen.scalar_expr(column('name', 'text'), {}, False, 1, 'products')
    assert 'James' in person
    assert 'James' not in thing and 'initcap' in thing


def test_two_conditions_with_casts_are_both_kept():
    """The bug: '::numeric AND discount ' was read as one cast, losing the upper bound."""
    t = table('lines', [column('discount', 'numeric', typmod=(4 << 16 | 3) + 4)],
              checks=["CHECK (discount >= 0::numeric AND discount < 1::numeric)"])
    rules = datagen.parse_checks(t)
    assert rules['discount'] == {'min': (0, False), 'max': (1, True)}
    low, high, scale = datagen._numeric_bounds(t['columns'][0], rules['discount'])
    assert (low, high, scale) == (0, 0.999, 3)


def test_quoted_column_names_are_understood():
    t = table('lines', [column('Qty', 'int2'), column('Order Status', 'text', attnum=2)], checks=[
        'CHECK ("Qty" > 0)',
        """CHECK (("Order Status")::text = ANY ((ARRAY['open'::character varying, 'closed'::character varying])::text[]))""",
    ])
    rules = datagen.parse_checks(t)
    assert rules['Qty']['min'] == (0, True)
    assert rules['Order Status']['values'] == ['open', 'closed']
    assert '__unparsed__' not in rules
    low, high, _ = datagen._numeric_bounds(t['columns'][0], rules['Qty'])
    assert low == 1                                  # an integer strictly above 0


def test_more_check_shapes():
    t = table('t', [column('a'), column('b', 'numeric'), column('code', 'text', attnum=3), column('n', attnum=4)], checks=[
        "CHECK (10 >= a)",                            # the number on the left
        "CHECK ((b > (0)::numeric) AND (b <= (100)::numeric))",
        "CHECK (code IS NOT NULL AND code <> ''::text)",
        "CHECK (n = ANY (ARRAY[1, 2, 3]))",
    ])
    rules = datagen.parse_checks(t)
    assert rules['a']['max'] == (10, False)
    assert rules['b'] == {'min': (0, True), 'max': (100, False)}
    assert rules['code'] == {'not_null': True}
    assert rules['n']['values'] == ['1', '2', '3']
    assert '__unparsed__' not in rules


def test_a_check_saying_not_null_turns_off_the_nulls():
    t = datagen.plan_table(table('t', [column('code', 'text')], checks=["CHECK (code IS NOT NULL)"]))
    assert 'NULL' not in t['column_plans'][0]['expr']


def test_a_nullable_foreign_key_to_an_empty_parent_is_null_not_a_made_up_number():
    fk = {'name': 'f', 'columns': ['customer_id'], 'ref_schema': 'app', 'ref_table': 'customers', 'ref_columns': ['id']}
    t = datagen.plan_table(table('orders', [column('customer_id')], foreign_keys=[fk]))
    _, _, select, _ = datagen.build_insert(t, {'f': None})
    assert 'NULL::int4 AS c0' in select
    assert 'mod(g, 1000)' not in select


class _EmptyCursor:
    def __init__(self, empty):
        self.empty = empty

    def execute(self, statement, params=None):
        pass

    def fetchone(self):
        return (self.empty,)


def test_an_empty_parent_in_another_schema_blocks_the_table():
    fk = {'name': 'f', 'columns': ['customer_id'], 'ref_schema': 'app', 'ref_table': 'customers', 'ref_columns': ['id']}
    planned = {'lines': datagen.plan_table(table('lines', [column('customer_id', not_null=True)], foreign_keys=[fk]))}
    datagen._check_parents_in_other_schemas(_EmptyCursor(True), 'sales', planned)
    assert any('app.customers' in p for p in planned['lines']['problems'])
    planned = {'lines': datagen.plan_table(table('lines', [column('customer_id', not_null=True)], foreign_keys=[fk]))}
    datagen._check_parents_in_other_schemas(_EmptyCursor(False), 'sales', planned)
    assert planned['lines']['problems'] == []


def test_a_self_reference_builds_a_tree_in_an_empty_table():
    fk = {'name': 'f', 'columns': ['parent_id'], 'ref_schema': 'public', 'ref_table': 'nodes', 'ref_columns': ['id']}
    t = datagen.plan_table(table('nodes', [column('id', not_null=True), column('parent_id', attnum=2)],
                                 primary_key=['id'], foreign_keys=[fk]))
    parent = t['column_plans'][1]
    assert parent['kind'] == 'expr' and 'g - 1' in parent['expr'] and 'NULL' in parent['expr']
    filled = table('nodes', [column('id', not_null=True), column('parent_id', attnum=2)], primary_key=['id'],
                   foreign_keys=[fk])
    filled['is_empty'] = False
    assert datagen.plan_table(filled)['column_plans'][1]['kind'] == 'null'   # ids unknown: stay safe


def test_range_bit_and_xml_types_get_values():
    for base in ('int4range', 'numrange', 'tsrange', 'tstzrange', 'daterange', 'varbit', 'xml'):
        assert datagen.scalar_expr(column('c', base), {}, False, 1), base
    assert 'bit(4)' in datagen.scalar_expr(column('c', 'bit', typmod=4), {}, False, 1)
