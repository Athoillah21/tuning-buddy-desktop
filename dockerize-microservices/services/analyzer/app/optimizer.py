"""
Optimizer service that orchestrates the query optimization process.
Tests recommendations using temporary tables and compares execution times.
"""
import re
import uuid
import logging
from typing import List, Dict, Any

from . import config
from .ai_client import AIServiceClient, AIClientError
from .db_connector import DBConnector
from .fit_checks import fit_checks, normalize_column, parse_index
from .progress import NoProgress
from .query_analyzer import QueryValidator, ExecutionPlanAnalyzer
from .result_check import check_result, normalize_sql

logger = logging.getLogger(__name__)

# References to the throwaway test schema, e.g. temp_test_556038a6.orders or "temp_test_x"."orders"
TEMP_SCHEMA_REF_RE = re.compile(
    r'"?(temp_test_[0-9a-f]{8})"?\s*\.\s*"?([A-Za-z_][\w$]*)"?', re.IGNORECASE)


def _human_bytes(value) -> str:
    value = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == 'B' else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _human_ms(value) -> str:
    value = float(value or 0)
    return f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.1f} ms" if value >= 10 else f"{value:.2f} ms"


def _human_rows(value) -> str:
    value = float(value or 0)
    for limit, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if value >= limit:
            return f"{value / limit:.1f}{suffix} rows"
    return f"{value:.0f} rows"


def _result_line(index: int, rec_type, verdict, improvement, result_check) -> str:
    """One option's outcome, as the overlay shows it: "#1 index: 97% faster ✓"."""
    name = f"#{index} {rec_type or 'option'}"
    if result_check == 'different':
        return f"{name}: returns different rows ✕"
    if verdict == 'faster' and improvement is not None:
        return f"{name}: {improvement:.0f}% faster ✓"
    if verdict == 'already_fast':
        return f"{name}: already fast, nothing to gain"
    if verdict == 'within_noise':
        return f"{name}: no measurable change"
    if verdict == 'slower':
        return f"{name}: slower ✕"
    return f"{name}: could not be tested"


def _index_name(statement: str) -> str:
    match = re.search(r'INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?("?[\w.]+"?)', statement, re.IGNORECASE)
    return match.group(1) if match else 'the suggested index'


def restore_user_tables(value: Any, tables: List[str]) -> Any:
    """
    Recommendations are applied by the user to their own database, so they must never
    mention the temporary schema the optimizer tests in. The AI sees the temp tables while
    iterating, so any reference it echoes back is rewritten to the real table here.
    """
    mapping = {table.split('.')[-1].lower(): table for table in tables}

    def rewrite(text: str) -> str:
        return TEMP_SCHEMA_REF_RE.sub(
            lambda match: mapping.get(match.group(2).lower(), match.group(2)), text)

    if isinstance(value, str):
        return rewrite(value)
    if isinstance(value, list):
        return [restore_user_tables(item, tables) for item in value]
    if isinstance(value, dict):
        return {key: restore_user_tables(item, tables) for key, item in value.items()}
    return value


def match_index_sizes(statements: List[str], built: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Pair each CREATE INDEX that was applied with the index PostgreSQL built for it: by name
    when the statement names one, otherwise by table, method and key columns.
    """
    matched = []
    for statement in statements:
        parsed = parse_index(statement)
        if not parsed:
            continue
        for index in built:
            same_name = parsed['name'] and index['name'].lower() == parsed['name'].lower()
            same_shape = (
                index['table'].lower() == parsed['table'].lower()
                and index['method'] == parsed['method']
                and [normalize_column(c) for c in index['columns']] == parsed['columns']
            )
            if same_name or (not parsed['name'] and same_shape):
                matched.append({'statement': parsed['statement'], 'name': index['name'],
                                'bytes': index['bytes'], 'table': index['table'],
                                'table_bytes': index['table_bytes']})
                break
    return matched


def median_of(values: List[float]) -> float:
    """Median of the repeated measurements; one slow cold run must not decide a verdict."""
    ordered = sorted(value for value in values if isinstance(value, (int, float)))
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def spread_of(values: List[float]) -> float:
    """How much the repeats disagreed - the smallest difference worth believing."""
    numbers = [value for value in values if isinstance(value, (int, float))]
    if len(numbers) < 2:
        return 0.0
    return max(numbers) - min(numbers)


def classify_result(original_times: List[float], tested_times: List[float],
                    noise_floor_ms: float = None) -> Dict[str, Any]:
    """
    Decide whether a measured difference means anything.

    Two honesty rules:
      * a query already below the noise floor cannot be meaningfully improved
      * a difference smaller than the spread between repeats is not a difference -
        unless the two sets of repeats do not overlap at all. A single cold-cache run
        (74, 74, 172ms) must not hide a result that is faster on every repeat (0.1ms).
    """
    floor = config.NOISE_FLOOR_MS if noise_floor_ms is None else noise_floor_ms
    original = median_of(original_times)
    tested = median_of(tested_times)
    spread = max(spread_of(original_times), spread_of(tested_times))

    if not original_times or not tested_times or original <= 0:
        verdict = 'unknown'
        improvement = 0.0
    else:
        improvement = ((original - tested) / original) * 100
        if original < floor:
            verdict = 'already_fast'
        elif max(tested_times) < min(original_times):
            verdict = 'faster'
        elif min(tested_times) > max(original_times):
            verdict = 'slower'
        elif abs(original - tested) <= spread:
            verdict = 'within_noise'
        elif tested < original:
            verdict = 'faster'
        else:
            verdict = 'slower'

    return {
        'verdict': verdict,
        'improvement_percentage': round(improvement, 2),
        'median_original_ms': original,
        'median_tested_ms': tested,
        'measurement_spread_ms': round(spread, 3),
    }


class OptimizationError(Exception):
    """Raised when optimization process fails."""
    pass


class QueryOptimizer:
    """
    Orchestrates the full query optimization workflow:
    1. Analyze original query
    2. Get recommendations from the AI service
    3. Test recommendations using temp tables
    4. Compare and rank results
    """

    def __init__(self, connection_params: Dict[str, Any], progress=None):
        """
        Initialize the optimizer.

        Args:
            connection_params: Database connection parameters
            progress: a progress.Progress reporter for the live overlay (optional)
        """
        self.db = DBConnector(connection_params)
        self.ai = AIServiceClient()
        self.temp_schema = None
        self.progress = progress or NoProgress()
    
    def query_tables(self, query: str, plan: Dict[str, Any]) -> List[str]:
        """
        The tables a query reads, spelled as the query spells them. The regex's candidates
        are confirmed by the plan, which names every table it scans - except a partitioned
        table, whose plan only names its partitions, so those are confirmed by the catalog.
        """
        confirmed = QueryValidator.extract_tables(query, plan)
        for table in QueryValidator.extract_tables(query):
            if table not in confirmed and self.db.relation_kind(table) == 'p':
                confirmed.append(table)
        return confirmed

    def analyze_query(self, query: str) -> Dict[str, Any]:
        """
        Full analysis of a query including validation, execution, and plan analysis.
        
        Args:
            query: SQL query to analyze
            
        Returns:
            Dict with analysis results
        """
        # Step 1: Validate query
        self.progress.phase('baseline', 'Checking the query')
        validation = QueryValidator.validate(query)
        if not validation['is_valid']:
            return {
                'success': False,
                'stage': 'validation',
                'errors': validation['errors'],
            }
        
        # Step 2: Execute EXPLAIN ANALYZE, repeated so the baseline is a median not a guess
        result = self.db.execute_explain_analyze_median(
            query,
            repeats=config.MEASUREMENT_REPEATS,
            timeout_ms=config.QUERY_EXECUTION_TIMEOUT * 1000,
            on_run=lambda run, total: self.progress.now(
                f"Running your query with EXPLAIN ANALYZE, run {run} of {total}"),
        )
        
        if not result['success']:
            return {
                'success': False,
                'stage': 'execution',
                'error': result.get('error', 'Unknown error'),
            }
        
        # Step 3: Analyze the execution plan
        plan = result['plan']
        analysis = ExecutionPlanAnalyzer.analyze_plan(plan)
        
        times = result.get('execution_times') or [result['execution_time']]
        self.progress.fact('baseline_ms', round(result['execution_time'], 3))
        self.progress.fact('baseline_spread_ms', round(max(times) - min(times), 3))
        self.progress.log(f"Your query takes {_human_ms(result['execution_time'])} "
                          f"(median of {len(times)} run{'' if len(times) == 1 else 's'})")

        # Step 4: Extract table info
        tables = self.query_tables(query, plan)
        table_info = {}
        for table in tables:
            self.progress.now(f"Reading the size, indexes and statistics of {table}")
            table_info[table] = self.db.get_table_info(table)
            info = table_info[table]
            if isinstance(info, dict) and not info.get('error') and info.get('total_bytes') is not None:
                self.progress.log(f"{table}: {_human_bytes(info['total_bytes'])} · "
                                  f"{_human_rows(info.get('row_estimate'))} · {len(info.get('indexes') or [])} indexes")
        self.progress.fact('tables', [
            {'name': name, 'bytes': info.get('total_bytes'), 'rows': info.get('row_estimate')}
            for name, info in table_info.items() if isinstance(info, dict) and not info.get('error')
        ])
        
        return {
            'success': True,
            'query': query,
            'plan': plan,
            'execution_time': result['execution_time'],
            'execution_times': result.get('execution_times', [result['execution_time']]),
            'planning_time': result['planning_time'],
            'analysis': analysis,
            'table_info': table_info,
            'warnings': validation.get('warnings', []),
        }
    
    def get_recommendations(self, analysis_result: Dict[str, Any]) -> tuple:
        """
        Get optimization recommendations from AI.
        
        Args:
            analysis_result: Result from analyze_query()
            
        Returns:
            Tuple of (recommendations, provider_info)
        """
        if not analysis_result.get('success'):
            raise OptimizationError("Cannot get recommendations for failed analysis")
        
        try:
            recommendations, provider_info = self.ai.get_optimization_recommendations(
                query=analysis_result['query'],
                plan=analysis_result['plan'],
                execution_time=analysis_result['execution_time'],
                issues=analysis_result['analysis'].get('issues', []),
                table_info=analysis_result.get('table_info'),
            )
            return recommendations, provider_info
        except AIClientError as e:
            raise OptimizationError(f"Failed to get recommendations: {e}")
    
    def optimize(self, query: str, test_recommendations: bool = True, max_seq_scan_attempts: int = 5) -> Dict[str, Any]:
        """
        Full optimization workflow with iterative seq scan elimination.
        
        Args:
            query: SQL query to optimize
            test_recommendations: Whether to test recommendations with temp tables
            max_seq_scan_attempts: Maximum attempts to eliminate seq scans per recommendation
            
        Returns:
            Complete optimization results
        """
        if test_recommendations:
            # Copies an earlier, crashed analysis may have left on this server
            self.db.drop_stale_temp_schemas()
        # Step 1: Analyze original query
        analysis = self.analyze_query(query)
        if not analysis['success']:
            return analysis
        
        # Check if original query has seq scan (we need to eliminate it)
        original_has_seq_scan = ExecutionPlanAnalyzer.has_seq_scan(analysis['plan'])

        # Below the noise floor there is nothing to chase: measure each option once
        # instead of spending five AI rounds on a fraction of a millisecond
        baseline_already_fast = analysis['execution_time'] < config.NOISE_FLOOR_MS
        if baseline_already_fast:
            logger.info(f"Baseline is {analysis['execution_time']:.3f} ms, below the "
                        f"{config.NOISE_FLOOR_MS} ms noise floor - testing once, no iteration")
            self.progress.log(f"Already fast (under {config.NOISE_FLOOR_MS:g} ms): each idea is tested once")
            max_seq_scan_attempts = 1
        
        # Step 2: Get recommendations from AI
        self.progress.phase('ai', 'Sending the plan and table facts to your AI provider')
        self.progress.now('Waiting for the AI to suggest optimizations')
        try:
            recommendations, provider_info = self.get_recommendations(analysis)
        except OptimizationError as e:
            return {
                'success': False,
                'stage': 'recommendations',
                'error': str(e),
                'analysis': analysis,
            }
        
        provider = provider_info or {}
        self.progress.fact('ai_provider', {'name': provider.get('provider_name'), 'model': provider.get('model')})
        self.progress.fact('ideas', len(recommendations))
        self.progress.log(f"{provider.get('provider_name') or 'The AI'} suggested {len(recommendations)} "
                          f"option{'' if len(recommendations) == 1 else 's'}")
        for index, rec in enumerate(recommendations, 1):
            self.progress.rec(index, type=rec.get('type', 'idea'), status='waiting',
                              description=(rec.get('description') or '')[:140])

        # Step 3: Test recommendations (if enabled)
        if test_recommendations:
            self.progress.phase('testing', 'Testing each option on a full copy of the tables')
            tables = list(analysis.get('table_info', {}).keys())
            total_recs = len(recommendations)

            # The original's fingerprint is needed once, not once per recommendation
            fingerprints = {}

            def fingerprint(sql_text: str):
                key = normalize_sql(sql_text)
                if key not in fingerprints:
                    fingerprints[key] = self.db.result_fingerprint(
                        sql_text, timeout_ms=config.QUERY_EXECUTION_TIMEOUT * 1000)
                return fingerprints[key]
            
            for rec_index, rec in enumerate(recommendations, 1):
                rec_type = rec.get('type', 'unknown')
                logger.info(f"=== Testing Recommendation {rec_index}/{total_recs}: {rec_type.upper()} ===")
                
                current_rec = rec
                attempt = 0
                optimization_history = []
                schema_name = None
                all_indexes_created = []  # Track ALL indexes across iterations
                final_optimized_query = rec.get('optimized_query', query)  # Track final query
                test_result = {'success': False, 'plan': None, 'error': 'Recommendation could not be tested'}
                
                try:
                    while attempt < max_seq_scan_attempts:
                        attempt += 1
                        logger.info(f"Recommendation {rec_index}/{total_recs}: Attempt {attempt}/{max_seq_scan_attempts}")
                        self.progress.rec(rec_index, status='testing', attempt=attempt, max_attempts=max_seq_scan_attempts)
                        
                        # For first attempt, create new schema; for subsequent, reuse
                        if schema_name is None:
                            schema_name = f"temp_test_{uuid.uuid4().hex[:8]}"
                            if not self.db.create_temp_schema(schema_name):
                                break
                            # Clone tables - handle schema-qualified names like openidm.genericobjects
                            for table in tables:
                                if '.' in table:
                                    source_schema, table_name = table.split('.', 1)
                                else:
                                    source_schema, table_name = None, table
                                info = analysis.get('table_info', {}).get(table) or {}
                                size = f" ({_human_bytes(info['total_bytes'])})" if info.get('total_bytes') else ''
                                self.progress.now(f"#{rec_index}: copying {table}{size} into a test schema")
                                self.db.clone_table_to_schema(table_name, schema_name, source_schema=source_schema)
                        
                        # Create suggested indexes (accumulates over iterations)
                        for index_stmt in current_rec.get('suggested_indexes', []):
                            if index_stmt not in all_indexes_created:  # Avoid duplicates
                                self.progress.now(f"#{rec_index}: building index {_index_name(index_stmt)} on the copy")
                                if self.db.create_index_on_temp(schema_name, index_stmt, source_tables=tables):
                                    all_indexes_created.append(index_stmt)
                                else:
                                    logger.warning(f"Failed to create index: {index_stmt}")
                        
                        # Track the current optimized query
                        final_optimized_query = current_rec.get('optimized_query', final_optimized_query)
                        
                        # Modify query to use temp schema
                        test_query = current_rec.get('optimized_query', query)
                        for table in tables:
                            # Handle schema-qualified names: replace "schema.table" with "temp_schema.table"
                            if '.' in table:
                                original_schema, table_name = table.split('.', 1)
                                # Replace schema-qualified reference
                                test_query = test_query.replace(
                                    f"{original_schema}.{table_name}", f"{schema_name}.{table_name}"
                                )
                            else:
                                table_name = table
                                # Replace non-qualified references
                                test_query = test_query.replace(
                                    f" {table_name} ", f" {schema_name}.{table_name} "
                                ).replace(
                                    f" {table_name}\n", f" {schema_name}.{table_name}\n"
                                ).replace(
                                    f" {table_name};", f" {schema_name}.{table_name};"
                                )
                        
                        # Execute EXPLAIN ANALYZE, repeated and reduced to its median
                        result = self.db.execute_explain_analyze_median(
                            test_query, repeats=config.MEASUREMENT_REPEATS,
                            on_run=lambda run, total: self.progress.now(
                                f"#{rec_index}: measuring the new version, run {run} of {total}"))
                        test_result = {
                            'success': result['success'],
                            'execution_time': result.get('execution_time', 0),
                            'execution_times': result.get('execution_times', []),
                            'planning_time': result.get('planning_time', 0),
                            'plan': result.get('plan'),
                            'error': result.get('error'),
                        }
                        
                        if not test_result['success']:
                            break
                        
                        tested_plan = test_result.get('plan', {})
                        still_has_seq_scan = ExecutionPlanAnalyzer.has_seq_scan(tested_plan)
                        
                        # Classify the difference instead of trusting a raw percentage
                        tested_time = test_result.get('execution_time', 0)
                        classification = classify_result(
                            analysis.get('execution_times', [analysis['execution_time']]),
                            test_result.get('execution_times') or [tested_time],
                        )
                        improvement = classification['improvement_percentage']
                        self.progress.rec(rec_index, improvement=round(improvement, 1),
                                          verdict=classification['verdict'])
                        
                        # Check if we need to iterate:
                        # 1. Still has seq scan (if original had one)
                        # 2. Improvement is less than 50%
                        needs_seq_scan_fix = original_has_seq_scan and still_has_seq_scan
                        needs_more_improvement = improvement < 50
                        
                        if (needs_seq_scan_fix or needs_more_improvement) and attempt < max_seq_scan_attempts:
                            reason = []
                            if needs_seq_scan_fix:
                                reason.append("still has seq scan")
                            if needs_more_improvement:
                                reason.append(f"only {improvement:.1f}% improvement (need 50%+)")
                            
                            logger.info(f"Recommendation needs improvement: {', '.join(reason)}, attempt {attempt}/{max_seq_scan_attempts}")
                            
                            # Give the AI the current structure under the user's own table names,
                            # so it never learns (and never suggests) the temp schema
                            current_table_info = {}
                            for table in tables:
                                table_name = table.split('.')[-1]
                                info = self.db.get_schema_table_info(schema_name, table_name)
                                current_table_info[table_name] = restore_user_tables(info, tables)
                            
                            # Record this attempt
                            optimization_history.append({
                                'attempt': attempt,
                                'recommendation': {
                                    'type': current_rec.get('type'),
                                    'description': current_rec.get('description'),
                                    'suggested_indexes': current_rec.get('suggested_indexes', []),
                                },
                                'still_has_seq_scan': still_has_seq_scan,
                                'improvement_percentage': round(improvement, 2),
                                'reason': reason,
                            })
                            
                            # Ask AI for a fix with current table structure
                            self.progress.now(f"#{rec_index}: {' and '.join(reason)}; asking the AI for a better "
                                              f"version (attempt {attempt + 1} of {max_seq_scan_attempts})")
                            try:
                                new_rec, _ = self.ai.get_seq_scan_fix(
                                    query=query,
                                    previous_recommendation=current_rec,
                                    tested_plan=tested_plan,
                                    current_table_info=current_table_info,
                                )
                                # Update current_rec with the fix
                                current_rec = {**current_rec, **new_rec}
                                current_rec['seq_scan_fix_attempt'] = attempt
                            except Exception as e:
                                logger.warning(f"Failed to get seq scan fix: {e}")
                                break
                        else:
                            # Goals met: no seq scan (or wasn't issue) AND 50%+ improvement, or max attempts
                            if improvement >= 50 and not needs_seq_scan_fix:
                                logger.info(f"✓ Recommendation {rec_index}/{total_recs}: Goals MET at attempt {attempt} - {improvement:.1f}% improvement")
                            else:
                                logger.info(f"Recommendation {rec_index}/{total_recs}: Max attempts reached at attempt {attempt}")
                            break
                finally:
                    # The clones are full copies, so their indexes' sizes are what the real
                    # indexes will weigh; measure them before the schema goes
                    index_sizes = []
                    if schema_name:
                        index_sizes = match_index_sizes(all_indexes_created, self.db.temp_index_sizes(schema_name))
                        self.db.drop_temp_schema(schema_name)
                
                # Store final results in the recommendation
                rec.update(current_rec)
                rec['test_result'] = test_result
                rec['optimization_attempts'] = attempt
                rec['optimization_history'] = optimization_history
                rec['seq_scan_eliminated'] = original_has_seq_scan and not ExecutionPlanAnalyzer.has_seq_scan(test_result.get('plan') or {})
                
                # Store accumulated results for the report
                rec['all_indexes_applied'] = all_indexes_created  # All indexes that were successfully created
                rec['index_sizes'] = index_sizes
                rec['final_optimized_query'] = final_optimized_query  # The final query after all iterations
                rec['original_query'] = query  # Keep original for comparison

                # Everything here is shown to the user and meant to be run against their database,
                # so rewrite any leftover temp schema reference to the real table
                sanitized = restore_user_tables(dict(rec), tables)
                rec.clear()
                rec.update(sanitized)

                rec['query_was_rewritten'] = rec['final_optimized_query'] != query  # Flag if query changed
                
                if test_result['success']:
                    classification = classify_result(
                        analysis.get('execution_times', [analysis['execution_time']]),
                        test_result.get('execution_times') or [test_result['execution_time']],
                    )
                    rec['improvement_percentage'] = classification['improvement_percentage']
                    rec['tested_execution_time'] = test_result['execution_time']
                    rec['tested_execution_times'] = test_result.get('execution_times', [])
                    rec['verdict'] = classification['verdict']
                    rec['measurement_spread_ms'] = classification['measurement_spread_ms']

                # A faster query that answers differently is not an optimization
                if rec['final_optimized_query'] != query:
                    self.progress.now(f"#{rec_index}: checking the rewrite returns the same rows as the original")
                check = check_result(query, rec['final_optimized_query'], fingerprint)
                rec['result_check'] = check['status']
                rec['result_check_note'] = check['note']
                self.progress.rec(rec_index, status='done', improvement=rec.get('improvement_percentage'),
                                  verdict=rec.get('verdict'), result_check=check['status'])
                self.progress.log(_result_line(rec_index, rec.get('type'), rec.get('verdict'),
                                               rec.get('improvement_percentage'), check['status']))

        # Step 4: Does each option fit the database as it is (existing indexes, size, partitions)?
        self.progress.phase('ranking', 'Checking how each option fits this database, then ranking them')
        for rec in recommendations:
            rec['fit_checks'] = fit_checks(rec, analysis.get('table_info', {}), rec.get('index_sizes'))

        # Step 5: Rank by significance first - an option whose gain is inside the
        # measurement noise must never outrank one that genuinely helped
        recommendations.sort(
            key=lambda r: (
                r.get('result_check') != 'different',  # Priority 1: never a rewrite that answers differently
                r.get('verdict') == 'faster',          # Priority 2: a real improvement
                r.get('result_check') == 'same',       # Priority 3: verified over merely plausible
                r.get('seq_scan_eliminated', False),   # Priority 4: eliminated seq scan
                r.get('improvement_percentage', 0),    # Priority 5: size of the gain
            ),
            reverse=True
        )
        for i, rec in enumerate(recommendations):
            rec['rank'] = i + 1
        
        return {
            'success': True,
            'original_query': query,
            'original_execution_time': analysis['execution_time'],
            'original_execution_times': analysis.get('execution_times', []),
            'baseline_below_noise_floor': baseline_already_fast,
            'original_plan': analysis['plan'],
            'original_has_seq_scan': original_has_seq_scan,
            'analysis': analysis['analysis'],
            'recommendations': recommendations,
            'tables_analyzed': list(analysis.get('table_info', {}).keys()),
            'table_stats': analysis.get('table_info', {}),
            'ai_provider': provider_info,
        }
