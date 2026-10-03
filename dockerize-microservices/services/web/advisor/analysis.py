"""
Running an analysis and persisting it.

The web view and the benchmark command both go through here, so a benchmark run
produces exactly the same records - and therefore the same reports - as the UI.
"""
import logging

from .clients import AnalyzerClient, ServiceError, ServiceUnavailable
from .middleware import clear_ai_status_cache
from .models import QueryHistory, Recommendation

logger = logging.getLogger(__name__)

BACKEND_ERRORS = (ServiceUnavailable, ServiceError)


def result_error(results: dict) -> str:
    """Analyzer failures carry either 'error' or a list of validation 'errors'."""
    return results.get('error') or '; '.join(results.get('errors') or []) or 'Unknown error'


def run_analysis(connection, query: str, test_recommendations: bool = True,
                 progress_id: str = None) -> QueryHistory:
    """
    Analyze a query and store the outcome.

    Always returns the QueryHistory: check analysis_status for 'completed' or 'failed'
    rather than relying on exceptions.
    """
    query_history = QueryHistory.objects.create(
        connection=connection,
        original_query=query,
        analysis_status='analyzing',
    )

    def fail(message: str) -> QueryHistory:
        query_history.analysis_status = 'failed'
        query_history.error_message = message
        query_history.save()
        # An AI failure may have marked providers unhealthy
        clear_ai_status_cache()
        return query_history

    try:
        results = AnalyzerClient().optimize(
            connection.get_connection_params(), query, test_recommendations=test_recommendations,
            progress_id=progress_id,
        )
    except BACKEND_ERRORS as e:
        return fail(str(e))

    if not results.get('success'):
        return fail(result_error(results))

    query_history.original_plan = results.get('original_plan')
    query_history.original_execution_time = results.get('original_execution_time')
    query_history.original_execution_times = results.get('original_execution_times', [])
    query_history.ai_provider = results.get('ai_provider')
    query_history.table_stats = results.get('table_stats') or {}
    query_history.analysis_status = 'completed'
    query_history.save()

    for rec in results.get('recommendations', []):
        Recommendation.objects.create(
            query_history=query_history,
            recommendation_type=rec.get('type', 'rewrite'),
            description=rec.get('description', ''),
            optimized_query=rec.get('optimized_query', ''),
            suggested_indexes=rec.get('suggested_indexes', []),
            tested_execution_time=rec.get('tested_execution_time'),
            tested_plan=(rec.get('test_result') or {}).get('plan'),
            improvement_percentage=rec.get('improvement_percentage'),
            rank=rec.get('rank', 0),
            gemini_raw_response=rec,
            all_indexes_applied=rec.get('all_indexes_applied', []),
            final_optimized_query=rec.get('final_optimized_query', ''),
            query_was_rewritten=rec.get('query_was_rewritten', False),
            optimization_attempts=rec.get('optimization_attempts', 1),
            seq_scan_eliminated=rec.get('seq_scan_eliminated', False),
            verdict=rec.get('verdict', ''),
            measurement_spread_ms=rec.get('measurement_spread_ms'),
            tested_execution_times=rec.get('tested_execution_times', []),
            result_check=rec.get('result_check') or '',
            result_check_note=rec.get('result_check_note') or '',
            fit_checks=rec.get('fit_checks') or [],
            index_sizes=rec.get('index_sizes') or [],
        )

    return query_history


def available_extensions(connection) -> set:
    """The extensions installed in the target database, so cases that need one can be skipped."""
    import psycopg2

    with psycopg2.connect(**connection.get_connection_params()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT extname FROM pg_extension")
            return {row[0] for row in cur.fetchall()}


def best_recommendation(query_history):
    """The fastest tested recommendation of a completed analysis, or None."""
    return min((rec for rec in query_history.recommendations.all() if rec.tested_execution_time is not None),
               key=lambda rec: rec.tested_execution_time, default=None)


def gain_percent(query_history, best):
    """
    The improvement of `best` over the original, or None. Only a significant result earns a
    percentage: anything else would be presenting cache noise as an improvement.
    """
    original = query_history.original_execution_time or 0
    if best is None or not original or (best.verdict or 'faster') != 'faster':
        return None
    return (original - best.tested_execution_time) / original * 100
