"""
The Databases page (a pgAdmin-style explorer that also manages connections) and the test
data generator.

The page is a lazily loaded object tree on the left (connections, the databases on each
server, schemas, object groups, objects) and a panel on the right that is fetched as a
server-rendered HTML fragment. Connections are added, edited, tested and deleted from that
panel too. The selection lives in the URL query string, so refresh and back/forward work.
Every database read goes through the analyzer service.
"""
import json
import logging
import re
from urllib.parse import urlencode

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .clients import AnalyzerClient, ServiceError, ServiceUnavailable
from .forms import ConnectionForm
from .models import Connection
from .views import _password_expired, connection_form_initial, demo_connection_id

logger = logging.getLogger(__name__)

BACKEND_ERRORS = (ServiceUnavailable, ServiceError)

GROUPS = (
    ('tables', 'Tables', 'table'),
    ('views', 'Views', 'view'),
    ('materialized_views', 'Materialized views', 'materialized_view'),
    ('functions', 'Functions', 'function'),
    ('sequences', 'Sequences', 'sequence'),
    ('types', 'Types', 'type'),
)
GROUP_LABELS = {key: label for key, label, _ in GROUPS}
GROUP_KINDS = {key: kind for key, _, kind in GROUPS}
TABLE_TABS = (('columns', 'Columns'), ('indexes', 'Indexes'), ('partitions', 'Partitions'),
              ('constraints', 'Constraints'), ('activity', 'Statistics'), ('ddl', 'DDL'), ('data', 'Data'))
PANEL_KINDS = {'welcome', 'server', 'database', 'schema', 'group', 'table', 'view', 'materialized_view',
               'function', 'sequence', 'type', 'query', 'connection_add', 'connection_edit'}
JOB_ID_RE = re.compile(r'[0-9a-f]{12}')
SERVER_TABS = (('properties', 'Properties'), ('databases', 'Databases'), ('settings', 'Settings'),
               ('activity', 'Activity'))


# The same rule as the analyzer's datagen.is_local_host, which enforces it again on every write
LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1', '[::1]', 'host.docker.internal')


def is_local_host(host: str) -> bool:
    host = (host or '').strip().lower()
    return host in LOCAL_HOSTS or host.startswith('127.')


class PanelError(Exception):
    """Shown in the panel instead of its content."""

    def __init__(self, message: str, connection=None):
        super().__init__(message)
        self.connection = connection


_SIMPLE_NAME_RE = re.compile(r'[a-z_][a-z0-9_$]*')


def sql_name(*parts: str) -> str:
    """schema.table as it would be typed: quoted only when PostgreSQL needs the quotes."""
    return '.'.join(part if _SIMPLE_NAME_RE.fullmatch(part) else '"' + part.replace('"', '""') + '"'
                    for part in parts)


def explorer_url(**params) -> str:
    query = urlencode({k: v for k, v in params.items() if v not in (None, '')})
    return reverse('advisor:explorer') + (f'?{query}' if query else '')


def _connection_and_params(source, require_db: bool = True):
    """(connection, database, connection params for that database) from request GET/POST data."""
    connection_id = str(source.get('c', ''))
    if not connection_id.isdigit():
        raise PanelError("Choose a connection.")
    connection = Connection.objects.filter(pk=int(connection_id)).first()
    if connection is None:
        raise Http404("Unknown connection")
    if _password_expired(connection):
        raise PanelError(f'The saved password for "{connection.name}" has expired. '
                         f'Re-enter it on the connection page to browse this server.', connection)
    params = connection.get_connection_params()
    database = (source.get('db') or '').strip()
    if database:
        if len(database) > 63:
            raise PanelError("That is not a database name.")
        # Other databases on the same server are opened with the same credentials
        params['database'] = database
    elif require_db:
        database = params['database']
    return connection, params['database'], params


# ---------------------------------------------------------------------------
# Page, tree and panels
# ---------------------------------------------------------------------------

def explorer(request):
    """The Explorer shell. The tree and the panel load from the endpoints below."""
    connections = list(Connection.objects.all())
    state = {key: request.GET.get(key, '') for key in
             ('c', 'db', 'schema', 'kind', 'name', 'oid', 'group', 'tab', 'sql')}
    if not connections and not state['kind']:
        state['kind'] = 'connection_add'  # nothing to browse yet: start with the form
    elif not state['c'] and not state['kind'] and len(connections) == 1:
        state['c'] = str(connections[0].pk)
    return render(request, 'advisor/explorer/index.html', {
        'connections': connections,
        'state_json': json.dumps(state),
    })


@require_GET
def api_explorer_tree(request):
    """The children of one tree node, as JSON."""
    get = request.GET
    try:
        if not get.get('c'):
            demo_id = demo_connection_id()
            return JsonResponse({'children': [
                {'label': c.name, 'kind': 'server', 'expandable': True, 'params': {'c': c.pk},
                 'meta': 'bundled' if c.pk == demo_id else c.database}
                for c in Connection.objects.order_by('name')
            ]})
        connection, database, params = _connection_and_params(get, require_db=False)
        client = AnalyzerClient()
        c = connection.pk
        if not get.get('db'):
            databases = client.explorer_databases(params)['databases']
            return JsonResponse({'children': [
                {'label': d['name'], 'kind': 'database', 'expandable': d['can_connect'],
                 'params': {'c': c, 'db': d['name']}, 'bytes': d['total_bytes'],
                 'muted': not d['can_connect']}
                for d in databases
            ]})
        if not get.get('schema'):
            schemas = client.explorer_schemas(params)['schemas']
            return JsonResponse({'children': [
                {'label': s['name'], 'kind': 'schema', 'expandable': True,
                 'params': {'c': c, 'db': database, 'schema': s['name']}, 'meta': f"{s['tables']} tables"}
                for s in schemas
            ]})
        schema = get['schema']
        if not get.get('group'):
            counts = client.explorer_groups(params, schema)['counts']
            return JsonResponse({'children': [
                {'label': label, 'kind': 'group', 'group': key, 'count': counts.get(key, 0),
                 'expandable': bool(counts.get(key)), 'muted': not counts.get(key),
                 'params': {'c': c, 'db': database, 'schema': schema, 'group': key}}
                for key, label, _ in GROUPS
            ]})
        group = get['group']
        if group not in GROUP_KINDS:
            return JsonResponse({'error': 'Unknown group'}, status=400)
        objects = client.explorer_objects(params, schema, group)['objects']
        kind = GROUP_KINDS[group]
        children = []
        for obj in objects:
            node = {'label': obj['name'], 'kind': kind, 'expandable': False,
                    'params': {'c': c, 'db': database, 'schema': schema, 'kind': kind, 'name': obj['name']}}
            if kind == 'function':
                node['label'] = f"{obj['name']}({obj['arguments']})"
                node['params']['oid'] = obj['oid']
            if kind == 'table':
                node['bytes'] = obj.get('total_bytes')
                if obj.get('relkind') == 'p':
                    node['meta'] = f"{obj.get('partition_count') or 0} partitions"
            children.append(node)
        return JsonResponse({'children': children})
    except PanelError as e:
        return JsonResponse({'error': str(e)}, status=400)
    except BACKEND_ERRORS as e:
        return JsonResponse({'error': str(e)}, status=502)


@require_GET
def explorer_panel(request):
    """The right-hand panel for the selected node, as an HTML fragment."""
    get = request.GET
    kind = get.get('kind') or ('database' if get.get('db') and not get.get('schema') else
                               'group' if get.get('group') else
                               'schema' if get.get('schema') else
                               'server' if get.get('c') else 'welcome')
    if kind not in PANEL_KINDS:
        raise Http404("Unknown panel")
    context = {'kind': kind, 'tab': get.get('tab', ''), 'demo_connection_id': demo_connection_id()}
    template = f"advisor/explorer/panels/{'connection_form' if kind.startswith('connection_') else kind}.html"
    try:
        if kind == 'welcome':
            context['connections'] = Connection.objects.order_by('name')
        elif kind == 'connection_add':
            context.update(form=ConnectionForm(), title='Add a connection')
        elif kind == 'connection_edit':
            connection = get_object_or_404(Connection, pk=_int(get.get('c')))
            context.update(connection=connection, title='Edit connection',
                           form=ConnectionForm(instance=connection, initial=connection_form_initial(connection)))
        elif kind == 'server':
            _fill_server_panel(context, get)
        else:
            connection, database, params = _connection_and_params(get, require_db=kind != 'server')
            context.update(connection=connection, database=database, schema=get.get('schema', ''),
                           name=get.get('name', ''))
            _fill_panel(context, kind, params, get)
    except PanelError as e:
        context['error'] = str(e)
        context.setdefault('connection', e.connection)
        template = 'advisor/explorer/panels/error.html'
    except BACKEND_ERRORS as e:
        context['error'] = str(e)
        template = 'advisor/explorer/panels/error.html'
    return HttpResponse(render_to_string(template, context, request=request))


def _int(value) -> int:
    value = str(value or '')
    if not value.isdigit():
        raise Http404("Unknown connection")
    return int(value)


def _fill_server_panel(context, get):
    """
    A connection's home: its settings and actions always show, so a server that cannot be
    reached (or a password that expired) can be fixed or removed from right here.
    """
    connection = get_object_or_404(Connection, pk=_int(get.get('c')))
    context.update(connection=connection, database='', schema='', tabs=SERVER_TABS,
                   tab=context['tab'] if context['tab'] in dict(SERVER_TABS) else 'properties',
                   host=connection.get_decrypted_host(), username=connection.get_decrypted_username(),
                   password_expired=_password_expired(connection))
    if connection.pk == demo_connection_id():
        # Random since 1.3.6 and kept by the app; shown on request for psql, pgAdmin and other tools
        context['demo_password'] = connection.get_decrypted_password()
    if context['password_expired']:
        context['server_error'] = 'The saved password has expired. Edit the connection to enter it again.'
        return
    try:
        context['server'] = AnalyzerClient().explorer_server(connection.get_connection_params())
    except BACKEND_ERRORS as e:
        context['server_error'] = str(e)


def _fill_panel(context, kind, params, get):
    client = AnalyzerClient()
    schema, name = get.get('schema', ''), get.get('name', '')

    if kind == 'database':
        context['overview'] = client.explorer_overview(params)
    elif kind == 'schema':
        context['tables'] = client.explorer_tables(params, schema)['tables']
        context['counts'] = client.explorer_groups(params, schema)['counts']
        context['groups'] = GROUPS
    elif kind == 'group':
        group = get.get('group', '')
        if group not in GROUP_KINDS:
            raise PanelError("Unknown group")
        context.update(group=group, group_label=GROUP_LABELS[group], object_kind=GROUP_KINDS[group],
                       objects=client.explorer_objects(params, schema, group)['objects'])
    elif kind == 'table':
        try:
            detail = client.explorer_table(params, schema, name)
        except ServiceError as e:
            if e.status_code == 404:
                raise PanelError(f"{schema}.{name} was not found.")
            raise
        for index in detail.get('indexes', []):
            index['unused'] = not index.get('scans') and not index.get('is_primary') and not index.get('is_unique')
        tabs = [t for t in TABLE_TABS if t[0] != 'partitions' or detail.get('is_partitioned')]
        tab = context['tab'] if context['tab'] in dict(tabs) else 'columns'
        context.update(detail=detail, tabs=tabs, tab=tab, select_sql=f"SELECT * FROM {sql_name(schema, name)} LIMIT 100;")
        if tab == 'ddl':
            context['ddl'] = client.explorer_object(params, 'table_ddl', schema, name)['ddl']
        elif tab == 'data':
            try:
                context['preview'] = client.explorer_preview(params, schema, name, limit=100)
            except BACKEND_ERRORS as e:
                context['preview_error'] = str(e)
    elif kind in ('view', 'materialized_view'):
        context['detail'] = client.explorer_object(params, kind, schema, name)
        tabs = (('definition', 'Definition'), ('columns', 'Columns'), ('ddl', 'DDL'), ('data', 'Data'))
        tab = context['tab'] if context['tab'] in dict(tabs) else 'definition'
        context.update(tabs=tabs, tab=tab, select_sql=f"SELECT * FROM {sql_name(schema, name)} LIMIT 100;")
        if tab == 'data':
            try:
                context['preview'] = client.explorer_preview(params, schema, name, limit=100)
            except BACKEND_ERRORS as e:
                context['preview_error'] = str(e)
    elif kind == 'function':
        oid = get.get('oid', '')
        if not oid.isdigit():
            raise PanelError("Choose a function in the tree.")
        context['detail'] = client.explorer_object(params, 'function', oid=int(oid))
    elif kind in ('sequence', 'type'):
        context['detail'] = client.explorer_object(params, kind, schema, name)
    elif kind == 'query':
        context.update(initial_sql=get.get('sql', ''), host=params['host'], is_local=is_local_host(params['host']))


# ---------------------------------------------------------------------------
# The old Explorer URLs now open the matching place in the new one
# ---------------------------------------------------------------------------

def explorer_pick(request):
    return redirect(explorer_url())


def explorer_overview(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    return redirect(explorer_url(c=pk, db=connection.database, kind='database'))


def explorer_table(request, pk, schema, table):
    connection = get_object_or_404(Connection, pk=pk)
    return redirect(explorer_url(c=pk, db=connection.database, schema=schema, kind='table', name=table,
                                 tab=request.GET.get('tab')))


def explorer_console(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    return redirect(explorer_url(c=pk, db=connection.database, kind='query', sql=request.GET.get('sql')))


# ---------------------------------------------------------------------------
# Query tool
# ---------------------------------------------------------------------------

@require_POST
def api_explorer_query(request, pk):
    """Run SQL from the query tool (AJAX). Read-only unless the user turned on "Allow changes"."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    try:
        connection, database, params = _connection_and_params({'c': pk, 'db': data.get('db')})
    except PanelError as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=403)
    try:
        limit = min(max(int(data.get('limit') or 500), 1), 1000)
    except (TypeError, ValueError):
        limit = 500
    mode = 'write' if data.get('mode') == 'write' else 'read'

    try:
        return JsonResponse(AnalyzerClient().explorer_query(
            params, data.get('sql') or '', limit, mode=mode,
            confirm_not_production=bool(data.get('confirm_not_production'))))
    except ServiceError as e:
        payload = {'success': False, 'error': str(e)}
        for key in ('rolled_back', 'statement_index', 'statement_count', 'results'):
            if key in e.payload:
                payload[key] = e.payload[key]
        return JsonResponse(payload, status=400)
    except ServiceUnavailable as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=503)


# ---------------------------------------------------------------------------
# Test data generator
# ---------------------------------------------------------------------------

def datagen_page(request):
    """Choose tables and how much data to add to each; the page then shows the job's progress."""
    try:
        connection, database, params = _connection_and_params(request.GET)
    except PanelError as e:
        return render(request, 'advisor/explorer/generate.html', {'error': str(e)})
    schema = request.GET.get('schema') or 'public'
    return render(request, 'advisor/explorer/generate.html', {
        'connection': connection, 'database': database, 'schema': schema,
        'host': params['host'], 'is_local': is_local_host(params['host']),
        'preselect_json': json.dumps([t for t in request.GET.get('tables', '').split(',') if t]),
        'back_url': explorer_url(c=connection.pk, db=database, schema=schema, kind='schema'),
    })


def _json_body(request):
    try:
        return json.loads(request.body)
    except json.JSONDecodeError:
        return None


@require_POST
def api_datagen_plan(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    try:
        connection, database, params = _connection_and_params(data)
        return JsonResponse(AnalyzerClient().datagen_plan(params, data.get('schema') or 'public'))
    except PanelError as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=403)
    except BACKEND_ERRORS as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=502)


@require_POST
def api_datagen_start(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    try:
        targets = {str(name): int(size) for name, size in (data.get('targets') or {}).items()}
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Sizes must be whole numbers of bytes'}, status=400)
    try:
        connection, database, params = _connection_and_params(data)
        return JsonResponse(AnalyzerClient().datagen_start(
            params, data.get('schema') or 'public', targets,
            confirm_database=str(data.get('confirm_database') or ''),
            confirm_not_production=bool(data.get('confirm_not_production'))))
    except PanelError as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=403)
    except BACKEND_ERRORS as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@require_GET
def api_datagen_job(request, job_id):
    if not JOB_ID_RE.fullmatch(job_id):
        raise Http404("Unknown job")
    try:
        return JsonResponse(AnalyzerClient().datagen_job(job_id))
    except ServiceError as e:
        # 404: the job is gone (the app was restarted); the page stops waiting for it
        return JsonResponse({'success': False, 'error': str(e)}, status=e.status_code or 502)
    except ServiceUnavailable as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=502)


@require_POST
def api_datagen_cancel(request, job_id):
    if not JOB_ID_RE.fullmatch(job_id):
        raise Http404("Unknown job")
    try:
        return JsonResponse(AnalyzerClient().datagen_cancel(job_id))
    except BACKEND_ERRORS as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=502)
