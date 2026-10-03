"""
Views for the Query Tuning Advisor.
The web service owns connections and history; analysis, AI and PDF work is delegated to the backend services.
"""
import json
import logging
import os
import re
import time
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from django.http import Http404, JsonResponse, HttpResponse
from django.conf import settings
from django.contrib import messages
from django.db.models import ProtectedError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods, require_POST

from .analysis import available_extensions, best_recommendation, gain_percent, run_analysis
from .benchmark_cases import BENCHMARK_CASES, case_by_slug
from .clients import AIServiceClient, AnalyzerClient, ReportClient, ServiceError, ServiceUnavailable
from .middleware import clear_ai_status_cache, get_ai_status
from .models import Connection, QueryHistory, Recommendation, is_local_host
from .forms import AIProviderForm, ConnectionForm, QueryForm
from .plan_utils import table_time

logger = logging.getLogger(__name__)

# The id the analyze page makes up so it can poll the analysis' live progress
PROGRESS_ID_RE = re.compile(r'[0-9a-f]{32}')


def _progress_id(value):
    value = str(value or '')
    return value if PROGRESS_ID_RE.fullmatch(value) else None

BACKEND_ERRORS = (ServiceUnavailable, ServiceError)

# Recommendation fields the report service needs to render a PDF
REPORT_RECOMMENDATION_FIELDS = [
    'id', 'recommendation_type', 'description', 'optimized_query', 'suggested_indexes',
    'tested_execution_time', 'tested_plan', 'improvement_percentage', 'rank',
    'all_indexes_applied', 'final_optimized_query', 'query_was_rewritten',
    'optimization_attempts', 'seq_scan_eliminated',
    'verdict', 'measurement_spread_ms', 'tested_execution_times',
    'result_check', 'result_check_note', 'fit_checks', 'index_sizes',
]


def demo_connection_id():
    """The desktop app's bundled demo database, if it has one (set by the launcher)."""
    value = os.environ.get('DEMO_CONNECTION_ID', '')
    return int(value) if value.isdigit() else None


def _password_expired(connection) -> bool:
    # The app keeps the demo database's password itself, so it is never asked for again
    return connection.pk != demo_connection_id() and connection.is_password_expired()


def _password_expired_redirect(request, connection, action: str):
    """Send the user to re-enter an expired saved password, or None if it is still valid."""
    if not _password_expired(connection):
        return None
    messages.warning(
        request,
        f'The saved password for "{connection.name}" expired after '
        f'{settings.PASSWORD_EXPIRY_HOURS} hour(s). Re-enter it to {action}.',
    )
    return redirect(_databases_url(c=connection.pk, kind='connection_edit'))


def _tables_involved(query_history) -> list:
    """
    The tables of an analysis with their size and shape as recorded at analysis time,
    and the share of the plan's time spent scanning each. The table that took the most
    is the bottleneck, and it is listed first.
    """
    times = table_time(query_history.original_plan)
    tables = []
    for label, info in (query_history.table_stats or {}).items():
        if not isinstance(info, dict):
            continue
        if info.get('error'):
            tables.append({'label': label, 'error': info['error']})
            continue
        # A partitioned table's time is spent in its partitions, which the plan names instead
        relations = {(info.get('name') or label.split('.')[-1]).lower()}
        relations.update((part.get('name') or '').lower() for part in info.get('partitions') or [])
        timed = [times[relation] for relation in relations if relation in times]
        timing = {'ms': sum(t['ms'] for t in timed), 'share': sum(t['share'] for t in timed)} if timed else None
        tables.append({
            'label': label,
            'schema': info.get('schema'),
            'name': info.get('name') or label,
            'kind': info.get('kind'),
            'total_bytes': info.get('total_bytes'),
            'table_bytes': info.get('table_bytes'),
            'index_bytes': info.get('index_bytes'),
            'row_estimate': info.get('row_estimate'),
            'index_count': len(info.get('indexes') or []),
            'is_partitioned': info.get('is_partitioned'),
            'partition_count': info.get('partition_count'),
            'time_share': round(timing['share'] * 100) if timing else None,
            'time_ms': timing['ms'] if timing else 0,
        })
    timed_tables = [table for table in tables if table.get('time_ms')]
    bottleneck = max(timed_tables, key=lambda table: table['time_ms']) if timed_tables else None
    for table in tables:
        table['is_bottleneck'] = table is bottleneck
    tables.sort(key=lambda table: (not table.get('is_bottleneck'), -(table.get('total_bytes') or 0)))
    return tables


def healthz(request):
    """Liveness probe for docker compose."""
    return HttpResponse('ok', content_type='text/plain')


def home(request):
    """Home page - shows connections and recent queries."""
    connections = Connection.objects.all()[:6]
    recent_queries = QueryHistory.objects.select_related('connection')[:6]
    demo_id = demo_connection_id()

    # Typical gain: the median over recent completed analyses whose best option was significant
    gains = []
    recent_completed = (QueryHistory.objects.filter(analysis_status='completed')
                        .prefetch_related('recommendations')[:50])
    for history in recent_completed:
        gain = gain_percent(history, best_recommendation(history))
        if gain is not None:
            gains.append(gain)
    gains.sort()
    median_gain = gains[len(gains) // 2] if gains else None

    ai_status = getattr(request, 'ai_status', None) or {}
    healthy = [p for p in ai_status.get('providers', []) if p.get('enabled') and p.get('status') == 'healthy']

    return render(request, 'advisor/home.html', {
        'connections': connections,
        'recent_queries': recent_queries,
        'demo_connection': Connection.objects.filter(pk=demo_id).first() if demo_id else None,
        'test_case_count': len(BENCHMARK_CASES),
        'stats': {
            'connections': Connection.objects.count(),
            'completed': QueryHistory.objects.filter(analysis_status='completed').count(),
            'failed': QueryHistory.objects.filter(analysis_status='failed').count(),
            'median_gain': median_gain,
            'gain_samples': len(gains),
            'ai_provider': healthy[0] if healthy else None,
            'ai_healthy_count': len(healthy),
        },
    })


def _databases_url(**params) -> str:
    """A place on the Databases page (imported here: explorer_views imports this module)."""
    from .explorer_views import explorer_url
    return explorer_url(**params)


def _is_ajax(request) -> bool:
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def _connection_form_response(request, form, connection=None, status=200):
    """The connection form: a panel fragment for the Databases page, or a full page without JS."""
    context = {'form': form, 'connection': connection,
               'title': 'Edit connection' if connection else 'Add a connection'}
    if _is_ajax(request):
        return HttpResponse(render_to_string('advisor/explorer/panels/connection_form.html', context,
                                             request=request), status=status)
    return render(request, 'advisor/connections/form.html', context, status=status)


def connection_list(request):
    """Connections now live on the Databases page."""
    return redirect(_databases_url())


def connection_add(request):
    """Add a connection. The form is shown in the Databases page's panel and posted with fetch."""
    if request.method != 'POST':
        return redirect(_databases_url(kind='connection_add'))
    form = ConnectionForm(request.POST)
    if not form.is_valid():
        return _connection_form_response(request, form, status=400)
    connection = form.save()
    if _is_ajax(request):
        return JsonResponse({'ok': True, 'c': connection.pk})
    messages.success(request, f'Connection "{connection.name}" created.')
    return redirect(_databases_url(c=connection.pk, kind='server'))


def connection_edit(request, pk):
    """Edit a connection; the password must be entered again (it is stored encrypted)."""
    connection = get_object_or_404(Connection, pk=pk)
    if request.method != 'POST':
        return redirect(_databases_url(c=pk, kind='connection_edit'))
    form = ConnectionForm(request.POST, instance=connection)
    if not form.is_valid():
        return _connection_form_response(request, form, connection, status=400)
    form.save()
    if _is_ajax(request):
        return JsonResponse({'ok': True, 'c': connection.pk})
    messages.success(request, f'Connection "{connection.name}" updated.')
    return redirect(_databases_url(c=pk, kind='server'))


def connection_form_initial(connection) -> dict:
    """Decrypted values for the edit form; the password is never sent back to the page."""
    return {'host': connection.get_decrypted_host(), 'username': connection.get_decrypted_username(),
            'password': ''}


def connection_delete(request, pk):
    """Delete a connection, unless saved analyses still reference it (POST; confirmed on the page)."""
    connection = get_object_or_404(Connection, pk=pk)
    if request.method != 'POST':
        return redirect(_databases_url(c=pk, kind='server'))
    query_count = connection.queries.count()
    name = connection.name
    try:
        connection.delete()
    except ProtectedError:
        error = (f'"{name}" still has {query_count} saved analysis record{"s" if query_count != 1 else ""}. '
                 f'Delete those from History first.')
        if _is_ajax(request):
            return JsonResponse({'ok': False, 'error': error}, status=409)
        messages.error(request, error)
        return redirect(_databases_url(c=pk, kind='server'))
    if _is_ajax(request):
        return JsonResponse({'ok': True})
    messages.success(request, f'Connection "{name}" deleted.')
    return redirect(_databases_url())


def connection_test(request, pk):
    """Test a database connection (AJAX endpoint)."""
    connection = get_object_or_404(Connection, pk=pk)

    try:
        return JsonResponse(AnalyzerClient().test_connection(connection.get_connection_params()))
    except BACKEND_ERRORS as e:
        return JsonResponse({
            'success': False,
            'message': str(e),
        })


def analyze_query(request, connection_id):
    """Query analysis page."""
    connection = get_object_or_404(Connection, pk=connection_id)

    expired = _password_expired_redirect(request, connection, 'run this analysis')
    if expired:
        return expired

    # Testing copies the tables the query reads into a temporary schema on the analysed server
    remote_host = '' if is_local_host(connection.get_decrypted_host()) else connection.get_decrypted_host()

    if request.method == 'POST':
        form = QueryForm(request.POST)
        if (form.is_valid() and remote_host and form.cleaned_data.get('test_recommendations')
                and not form.cleaned_data.get('confirm_remote')):
            form.add_error('confirm_remote', f'Confirm that Tuning Buddy may copy these tables on {remote_host}, '
                                             'or turn off testing the recommendations.')
        if form.is_valid():
            query = form.cleaned_data['query']
            test_recommendations = form.cleaned_data.get('test_recommendations', True)

            # Shared with the benchmark command, so both produce identical records
            try:
                query_history = run_analysis(connection, query, test_recommendations,
                                             progress_id=_progress_id(request.POST.get('progress_id')))
            except Exception as e:
                logger.exception("Unexpected error during analysis")
                messages.error(request, f"Unexpected error: {e}")
            else:
                if query_history.analysis_status == 'completed':
                    return redirect('advisor:view_results', query_id=query_history.id)
                messages.error(request, f"Analysis failed: {query_history.error_message}")
    else:
        # ?query= pre-fills the editor (from a test case, or the query tool)
        form = QueryForm(initial={'query': request.GET.get('query', '')})

    return render(request, 'advisor/analyze.html', {
        'form': form,
        'connection': connection,
        'remote_host': remote_host,
    })


def view_results(request, query_id):
    """View optimization results."""
    query_history = get_object_or_404(
        QueryHistory.objects.select_related('connection').prefetch_related('recommendations'),
        pk=query_id
    )

    all_recommendations = list(query_history.recommendations.all().order_by('rank'))
    original_time = query_history.original_execution_time or 0

    # Group by what the measurement actually showed, not by raw timing: an option whose
    # gain sits inside the noise must not be presented as an improvement, and one that
    # returns different rows is not an optimization at all
    significant, noise, slower_recs, untested_recs, different = [], [], [], [], []
    for rec in all_recommendations:
        verdict = rec.verdict or ''
        if rec.result_check == 'different':
            different.append(rec)
        elif rec.tested_execution_time is None:
            untested_recs.append(rec)
        elif verdict in ('within_noise', 'already_fast'):
            noise.append(rec)
        elif verdict == 'slower' or (not verdict and rec.tested_execution_time >= original_time):
            slower_recs.append(rec)
        else:
            significant.append(rec)

    if significant or untested_recs:
        filtered_recommendations = significant + untested_recs + noise + different
        filtered_count = len(slower_recs)
    else:
        filtered_recommendations = [rec for rec in all_recommendations if rec not in different] + different
        filtered_count = 0

    for i, rec in enumerate(filtered_recommendations):
        rec.display_rank = i + 1
        rec.is_slower = rec in slower_recs
        rec.is_noise = rec in noise
        rec.is_different = rec in different
        sizes = {(entry.get('statement') or '').strip(): entry for entry in rec.index_sizes or []}
        rec.index_rows = [
            {'statement': statement, 'size': sizes.get(statement.strip().rstrip(';').strip())}
            for statement in rec.suggested_indexes or []
        ]

    no_action_message = ''
    if all_recommendations and not significant:
        if any(rec.verdict == 'already_fast' for rec in all_recommendations):
            no_action_message = (
                f"No action needed. The query already runs in {original_time:.3f} ms, where the "
                "difference between two runs is cache noise rather than optimization."
            )
        elif noise:
            no_action_message = (
                "No measurable improvement. Every option tested within the variation between "
                "repeated runs, so none of them can be shown to help."
            )

    return render(request, 'advisor/results.html', {
        'query': query_history,
        'recommendations': filtered_recommendations,
        'total_recommendations': len(all_recommendations),
        'filtered_count': filtered_count,
        'no_action_message': no_action_message,
        'all_slower': not significant and not untested_recs and bool(slower_recs),
        'tables': _tables_involved(query_history),
        'has_table_stats': bool(query_history.table_stats),
    })


def query_history(request):
    """View query analysis history."""
    queries = QueryHistory.objects.select_related('connection').all()

    return render(request, 'advisor/history.html', {
        'queries': queries,
    })


def history_delete(request, pk):
    """Delete a history record."""
    query = get_object_or_404(QueryHistory, pk=pk)

    if request.method == 'POST':
        query.delete()
        messages.success(request, 'History record deleted successfully!')
        return redirect('advisor:query_history')

    return render(request, 'advisor/confirm_delete_history.html', {
        'query': query,
    })


def _report_payload(query_history, recommendations) -> dict:
    """Serialize a query analysis for the report service."""
    return {
        'query_history': {
            'id': query_history.id,
            'original_query': query_history.original_query,
            'original_plan': query_history.original_plan,
            'original_execution_time': query_history.original_execution_time,
            'original_execution_times': query_history.original_execution_times or [],
            'ai_provider': query_history.ai_provider,
            'analysis_status': query_history.analysis_status,
            'created_at': query_history.created_at.isoformat(),
            # The web service owns TIME_ZONE, so it formats the timestamp for the report
            'created_at_display': timezone.localtime(query_history.created_at).strftime('%Y-%m-%d %H:%M'),
            'connection': {
                'name': query_history.connection.name,
                'database': query_history.connection.database,
            },
            'table_stats': query_history.table_stats or {},
        },
        'recommendations': [
            {
                **{field: getattr(rec, field) for field in REPORT_RECOMMENDATION_FIELDS},
                'recommendation_type_display': rec.get_recommendation_type_display(),
            }
            for rec in recommendations
        ],
    }


def download_pdf(request, query_id):
    """Generate and download PDF report for query analysis."""
    query_history = get_object_or_404(QueryHistory.objects.select_related('connection'), pk=query_id)
    recommendations = query_history.recommendations.all()

    try:
        pdf = ReportClient().optimization_report(_report_payload(query_history, recommendations))
    except BACKEND_ERRORS as e:
        logger.error(f"Error generating PDF: {e}")
        messages.error(request, f"Failed to generate PDF: {e}")
        return redirect('advisor:view_results', query_id=query_id)

    response = HttpResponse(pdf, content_type='application/pdf')
    filename = f"query_report_{query_id}_{query_history.created_at.strftime('%Y%m%d')}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@require_http_methods(["POST"])
def api_analyze(request):
    """API endpoint for asynchronous query analysis."""
    try:
        data = json.loads(request.body)
        connection_id = data.get('connection_id')
        query = data.get('query')
        test_recommendations = data.get('test_recommendations', True)

        if not connection_id or not query:
            return JsonResponse({
                'success': False,
                'error': 'Missing connection_id or query',
            }, status=400)

        connection = get_object_or_404(Connection, pk=connection_id)

        results = AnalyzerClient().optimize(
            connection.get_connection_params(), query, test_recommendations=test_recommendations,
        )

        return JsonResponse(results)

    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON',
        }, status=400)
    except BACKEND_ERRORS as e:
        return JsonResponse({
            'success': False,
            'error': str(e),
        }, status=502)
    except Http404:
        raise
    except Exception as e:
        logger.exception("API analyze error")
        return JsonResponse({
            'success': False,
            'error': str(e),
        }, status=500)


# ---------------------------------------------------------------------------
# AI Settings
# ---------------------------------------------------------------------------

def _service_unavailable(request, error):
    return render(request, 'advisor/service_unavailable.html', {'error': str(error)}, status=503)


def _provider_payload(form) -> dict:
    data = form.cleaned_data
    return {
        'name': data['name'],
        'provider_type': data['provider_type'],
        'model': data['model'],
        'base_url': data.get('base_url') or None,
        'api_key': data.get('api_key') or None,
        'priority': data['priority'],
        'enabled': data.get('enabled', False),
    }


def _check_and_report(request, client, provider):
    """Health-check a just-saved provider and flash the outcome."""
    clear_ai_status_cache()
    name = provider['name']
    try:
        result = client.check_provider(provider['id'])['result']
    except BACKEND_ERRORS as e:
        messages.warning(request, f'"{name}" was saved, but the health check could not run: {e}')
        return
    finally:
        clear_ai_status_cache()

    if result['healthy']:
        messages.success(request, f'"{name}" saved and verified. {result["message"]}')
    else:
        messages.error(request, f'"{name}" saved, but the health check failed. {result["message"]}')


def ai_settings(request):
    """List AI providers with their health status."""
    client = AIServiceClient()
    try:
        providers = client.list_providers()
        status = get_ai_status(force_refresh=True)
    except BACKEND_ERRORS as e:
        return _service_unavailable(request, e)

    for provider in providers:
        provider['last_checked_at'] = parse_datetime(provider['last_checked_at']) if provider.get('last_checked_at') else None

    return render(request, 'advisor/ai_settings/list.html', {
        'providers': providers,
        'status': status,
    })


def ai_provider_add(request):
    """Add an AI provider, then health-check it."""
    client = AIServiceClient()
    try:
        provider_types = client.provider_types()
    except BACKEND_ERRORS as e:
        return _service_unavailable(request, e)

    if request.method == 'POST':
        form = AIProviderForm(request.POST)
        if form.is_valid():
            try:
                provider = client.create_provider(_provider_payload(form))
            except ServiceUnavailable as e:
                return _service_unavailable(request, e)
            except ServiceError as e:
                form.add_error(None, str(e))
            else:
                _check_and_report(request, client, provider)
                return redirect('advisor:ai_settings')
    else:
        form = AIProviderForm()

    return render(request, 'advisor/ai_settings/form.html', {
        'form': form,
        'title': 'Add AI Provider',
        'provider_types': provider_types,
    })


def ai_provider_edit(request, pk):
    """Edit an AI provider. Changing its connection settings triggers a new health check."""
    client = AIServiceClient()
    try:
        provider = client.get_provider(pk)
        provider_types = client.provider_types()
    except ServiceError as e:
        if e.status_code == 404:
            raise Http404("AI provider not found")
        return _service_unavailable(request, e)
    except ServiceUnavailable as e:
        return _service_unavailable(request, e)

    if request.method == 'POST':
        form = AIProviderForm(request.POST, has_api_key=provider['has_api_key'])
        if form.is_valid():
            payload = _provider_payload(form)
            if not payload['api_key']:
                payload.pop('api_key')  # Blank keeps the saved key
            payload['clear_api_key'] = form.cleaned_data.get('clear_api_key', False)
            try:
                updated = client.update_provider(pk, payload)
            except ServiceUnavailable as e:
                return _service_unavailable(request, e)
            except ServiceError as e:
                form.add_error(None, str(e))
            else:
                if updated['last_check_status'] == 'unknown':
                    _check_and_report(request, client, updated)
                else:
                    clear_ai_status_cache()
                    messages.success(request, f'"{updated["name"]}" updated.')
                return redirect('advisor:ai_settings')
    else:
        form = AIProviderForm(has_api_key=provider['has_api_key'], initial={
            'name': provider['name'],
            'provider_type': provider['provider_type'],
            'base_url': provider['base_url'] or '',
            'model': provider['model'],
            'priority': provider['priority'],
            'enabled': provider['enabled'],
        })

    return render(request, 'advisor/ai_settings/form.html', {
        'form': form,
        'title': 'Edit AI Provider',
        'provider': provider,
        'provider_types': provider_types,
    })


def ai_provider_delete(request, pk):
    """Delete an AI provider."""
    client = AIServiceClient()
    try:
        provider = client.get_provider(pk)
    except ServiceError as e:
        if e.status_code == 404:
            raise Http404("AI provider not found")
        return _service_unavailable(request, e)
    except ServiceUnavailable as e:
        return _service_unavailable(request, e)

    if request.method == 'POST':
        try:
            client.delete_provider(pk)
        except BACKEND_ERRORS as e:
            return _service_unavailable(request, e)
        clear_ai_status_cache()
        messages.success(request, f'"{provider["name"]}" deleted.')
        return redirect('advisor:ai_settings')

    return render(request, 'advisor/ai_settings/confirm_delete.html', {
        'provider': provider,
    })


@require_POST
def ai_provider_check(request, pk):
    """Run the health check for one saved provider (AJAX)."""
    try:
        body = AIServiceClient().check_provider(pk)
    except BACKEND_ERRORS as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=502)
    finally:
        clear_ai_status_cache()
    return JsonResponse({'success': True, **body})


@require_POST
def ai_check_all(request):
    """Run the health check for every enabled provider (AJAX)."""
    try:
        body = AIServiceClient().check_all()
    except BACKEND_ERRORS as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=502)
    finally:
        clear_ai_status_cache()
    return JsonResponse({'success': True, **body})


@require_POST
def ai_provider_test(request):
    """Health-check the unsaved values in the provider form (AJAX)."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'healthy': False, 'message': 'Invalid JSON'}, status=400)

    payload = {
        'provider_type': data.get('provider_type'),
        'model': (data.get('model') or '').strip(),
        'base_url': data.get('base_url') or None,
        'api_key': data.get('api_key') or None,
        'provider_id': data.get('provider_id') or None,
    }
    try:
        return JsonResponse(AIServiceClient().test_provider(payload))
    except ServiceError as e:
        return JsonResponse({'healthy': False, 'message': str(e)})
    except ServiceUnavailable as e:
        return JsonResponse({'healthy': False, 'message': str(e)}, status=502)


# ---------------------------------------------------------------------------
# Test cases: the benchmark suite (manage.py benchmark), run from the browser
# ---------------------------------------------------------------------------

def test_cases(request):
    """
    The benchmark cases with a button to run them. The page runs one case per request, in
    order, so progress shows as it goes and closing the page stops after the current case.
    """
    connections = list(Connection.objects.all())
    demo_id = demo_connection_id()
    selected = None
    requested = request.GET.get('connection', '')
    if requested.isdigit():
        selected = next((c for c in connections if c.pk == int(requested)), None)
    if selected is None:
        selected = next((c for c in connections if c.pk == demo_id), None) or (connections[0] if connections else None)

    saved = _saved_case_results(selected) if selected else {}
    cases = [dict(case, result=saved.get(case['slug'])) for case in BENCHMARK_CASES]
    return render(request, 'advisor/test_cases.html', {
        'cases': cases,
        'saved_count': len(saved),
        'connections': connections,
        'selected': selected,
        'demo_connection_id': demo_id,
        'password_expired': bool(selected) and _password_expired(selected),
    })


@require_POST
def api_test_case(request):
    """Run one benchmark case through the normal analysis pipeline (AJAX; can take minutes)."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'error': 'Invalid JSON'}, status=400)

    case = case_by_slug(str(data.get('case') or ''))
    if case is None:
        return JsonResponse({'status': 'error', 'error': 'Unknown test case'}, status=404)
    connection = Connection.objects.filter(pk=data.get('connection_id')).first()
    if connection is None:
        return JsonResponse({'status': 'error', 'error': 'Unknown connection'}, status=404)
    if _password_expired(connection):
        return JsonResponse({'status': 'error', 'error': 'The saved password has expired. Re-enter it on the '
                                                         'connection page, then run the cases again.'}, status=403)

    if case['requires']:
        try:
            installed = available_extensions(connection)
        except Exception as e:
            return JsonResponse({'status': 'error', 'error': f'Could not reach the database: {e}'})
        if case['requires'] not in installed:
            return JsonResponse({'status': 'skipped', 'error': f"Needs the {case['requires']} extension"})

    started = time.monotonic()
    try:
        history = run_analysis(connection, case['query'], test_recommendations=True,
                               progress_id=_progress_id(data.get('progress_id')))
    except Exception as e:
        logger.exception("Test case %s failed", case['slug'])
        return JsonResponse({'status': 'failed', 'error': str(e), 'elapsed': time.monotonic() - started})

    return JsonResponse({**_case_result(history), 'elapsed': time.monotonic() - started})


def _case_result(history) -> dict:
    """
    One test case's outcome, from its analysis in History. The live run returns it, and the
    page rebuilds every row from it on load, so results survive leaving the page.
    """
    result = {
        'status': history.analysis_status,
        'history_id': history.id,
        'results_url': reverse('advisor:view_results', args=[history.id]),
        'ran_at': timezone.localtime(history.created_at).strftime('%b %d, %H:%M'),
    }
    if history.analysis_status != 'completed':
        result['error'] = history.error_message or 'The analysis did not finish.'
        return result
    best = best_recommendation(history)
    result.update({
        'original_ms': history.original_execution_time,
        'best_ms': best.tested_execution_time if best else None,
        'verdict': (best.verdict or 'faster') if best else '',
        'gain': gain_percent(history, best),
    })
    result['verdict_label'] = result['verdict'].replace('_', ' ') or 'not tested'
    return result


def _saved_case_results(connection) -> dict:
    """The newest analysis of each test case's query on this connection, as {slug: result}."""
    slug_by_query = {case['query']: case['slug'] for case in BENCHMARK_CASES}
    results = {}
    histories = (QueryHistory.objects.filter(connection=connection, original_query__in=list(slug_by_query))
                 .order_by('-created_at', '-id').prefetch_related('recommendations'))
    for history in histories:
        slug = slug_by_query[history.original_query]
        if slug not in results:
            results[slug] = _case_result(history)
    return results


def api_analyze_progress(request, progress_id):
    """Live progress of a running analysis, for the overlay (AJAX, polled)."""
    if not PROGRESS_ID_RE.fullmatch(progress_id):
        raise Http404("Unknown analysis")
    try:
        return JsonResponse(AnalyzerClient().analyze_progress(progress_id))
    except ServiceError as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=e.status_code or 502)
    except ServiceUnavailable as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=503)
