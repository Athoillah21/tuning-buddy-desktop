"""
Run the benchmark suite through the real analysis pipeline.

Every case produces the same QueryHistory records the web UI creates, so each run is
viewable and downloadable at /results/<id>/ afterwards.

    docker compose exec web python manage.py benchmark --list
    docker compose exec web python manage.py benchmark --connection 1
    docker compose exec web python manage.py benchmark --cases spatial-knn,vector-knn
"""
import time

from django.core.management.base import BaseCommand, CommandError

from advisor.analysis import available_extensions, best_recommendation, gain_percent, run_analysis
from advisor.benchmark_cases import BENCHMARK_CASES, case_by_slug
from advisor.models import Connection


class Command(BaseCommand):
    help = "Run benchmark queries (relational, join, spatial, vector) through the analyzer"

    def add_arguments(self, parser):
        parser.add_argument('--connection', type=int, default=1,
                            help="Connection id to analyze against (default: 1)")
        parser.add_argument('--cases', type=str, default='',
                            help="Comma-separated case slugs (default: all supported)")
        parser.add_argument('--list', action='store_true', help="List the cases and exit")
        parser.add_argument('--no-test', action='store_true',
                            help="Skip temp-schema testing (recommendations only, much faster)")

    def handle(self, *args, **options):
        if options['list']:
            for case in BENCHMARK_CASES:
                requires = f" [requires {case['requires']}]" if case['requires'] else ""
                self.stdout.write(f"{case['slug']:18} {case['title']}{requires}")
            return

        connection = self._get_connection(options['connection'])
        available = self._available_extensions(connection)
        self.stdout.write(f"Database: {connection.name} | extensions: "
                          f"{', '.join(sorted(available)) or 'none beyond core'}\n")

        cases = self._select_cases(options['cases'])
        results = []

        for case in cases:
            if case['requires'] and case['requires'] not in available:
                self.stdout.write(self.style.WARNING(
                    f"skip  {case['slug']:18} needs the {case['requires']} extension"))
                results.append({'case': case, 'skipped': True})
                continue

            self.stdout.write(f"run   {case['slug']:18} {case['title']} ...", ending='')
            self.stdout.flush()
            started = time.time()
            history = run_analysis(connection, case['query'],
                                   test_recommendations=not options['no_test'])
            elapsed = time.time() - started

            if history.analysis_status != 'completed':
                self.stdout.write(self.style.ERROR(f" failed in {elapsed:.0f}s: {history.error_message}"))
                results.append({'case': case, 'history': history, 'elapsed': elapsed})
                continue

            best = best_recommendation(history)
            self.stdout.write(self.style.SUCCESS(f" done in {elapsed:.0f}s -> /results/{history.id}/"))
            results.append({'case': case, 'history': history, 'best': best, 'elapsed': elapsed})

        self._summary(results)

    # ------------------------------------------------------------------

    def _get_connection(self, connection_id: int) -> Connection:
        try:
            return Connection.objects.get(pk=connection_id)
        except Connection.DoesNotExist:
            available = ", ".join(f"{c.id}={c.name}" for c in Connection.objects.all()) or "none"
            raise CommandError(f"Connection {connection_id} not found. Available: {available}")

    def _available_extensions(self, connection) -> set:
        """Ask the target database which extensions it has, so cases can be skipped cleanly."""
        try:
            return available_extensions(connection)
        except Exception as e:
            raise CommandError(f"Could not reach the database: {e}")

    def _select_cases(self, slugs: str):
        if not slugs:
            return BENCHMARK_CASES
        selected = []
        for slug in (part.strip() for part in slugs.split(',') if part.strip()):
            case = case_by_slug(slug)
            if case is None:
                raise CommandError(f"Unknown case '{slug}'. Use --list to see them all.")
            selected.append(case)
        return selected

    def _summary(self, results):
        self.stdout.write("\n" + "=" * 104)
        self.stdout.write(f"{'CASE':18} {'STATUS':8} {'ORIGINAL':>12} {'BEST':>12} {'GAIN':>9} "
                          f"{'VERDICT':>14} {'REPORT':>15}")
        self.stdout.write("-" * 104)

        for entry in results:
            slug = entry['case']['slug']
            if entry.get('skipped'):
                self.stdout.write(f"{slug:18} {'skipped':8}")
                continue

            history = entry['history']
            if history.analysis_status != 'completed':
                self.stdout.write(f"{slug:18} {'failed':8} {history.error_message[:60]}")
                continue

            best = entry.get('best')
            original = history.original_execution_time or 0
            verdict = (getattr(best, 'verdict', '') or '-') if best is not None else '-'
            if best is not None and original:
                best_text = f"{best.tested_execution_time:.3f} ms"
                gain = gain_percent(history, best)
                gain_text = f"{gain:+.1f}%" if gain is not None else "n/a"
            else:
                gain_text, best_text = "-", "not tested"

            self.stdout.write(
                f"{slug:18} {'ok':8} {original:>9.3f} ms {best_text:>12} {gain_text:>9} "
                f"{verdict:>14} {'/results/' + str(history.id) + '/':>15}")

        self.stdout.write("=" * 104)
        self.stdout.write("Open http://localhost:8000/history/ to read or download any of these reports.")
