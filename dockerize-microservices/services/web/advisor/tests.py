import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .analysis import run_analysis
from .clients import ServiceError, ServiceUnavailable
from .forms import AIProviderForm
from .models import Connection, QueryHistory, Recommendation
from .plan_utils import bottleneck_table, table_time
from .views import _report_payload

STATUS_READY = {"ready": True, "healthy_count": 1, "enabled_count": 1, "total_count": 1, "providers": []}
STATUS_LOCKED = {"ready": False, "healthy_count": 0, "enabled_count": 1, "total_count": 1, "providers": []}


@mock.patch("advisor.middleware.AIServiceClient")
class AIReadyMiddlewareTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_redirects_to_ai_settings_when_no_provider_is_verified(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_LOCKED
        response = self.client.get(reverse("advisor:home"))
        self.assertRedirects(response, reverse("advisor:ai_settings"), fetch_redirect_response=False)

    def test_allows_the_app_when_ready(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY
        response = self.client.get(reverse("advisor:home"))
        self.assertEqual(response.status_code, 200)

    def test_healthz_is_exempt(self, client_cls):
        client_cls.return_value.status.side_effect = ServiceUnavailable("down")
        response = self.client.get(reverse("advisor:healthz"))
        self.assertEqual(response.status_code, 200)

    def test_ai_settings_is_exempt_while_locked(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_LOCKED
        with mock.patch("advisor.views.AIServiceClient") as views_client_cls:
            views_client_cls.return_value.list_providers.return_value = []
            response = self.client.get(reverse("advisor:ai_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Locked.")

    def test_shows_503_when_ai_service_is_down(self, client_cls):
        client_cls.return_value.status.side_effect = ServiceUnavailable("AI service is unreachable")
        response = self.client.get(reverse("advisor:home"))
        self.assertEqual(response.status_code, 503)
        self.assertTemplateUsed(response, "advisor/service_unavailable.html")

    def test_api_gets_json_when_locked(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_LOCKED
        response = self.client.post(reverse("advisor:api_analyze"), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["success"])

    def test_unlocks_immediately_after_a_provider_passes(self, client_cls):
        client_cls.return_value.status.side_effect = [STATUS_LOCKED, STATUS_READY, STATUS_READY]
        self.assertEqual(self.client.get(reverse("advisor:home")).status_code, 302)
        self.assertEqual(self.client.get(reverse("advisor:home")).status_code, 200)


class AIProviderFormTests(TestCase):

    def _data(self, **overrides):
        data = {"name": "Primary", "provider_type": "anthropic", "model": "claude-opus-5",
                "api_key": "sk-test", "priority": 1, "enabled": "on"}
        data.update(overrides)
        return data

    def test_api_key_required_for_new_anthropic_provider(self):
        form = AIProviderForm(self._data(api_key=""))
        self.assertFalse(form.is_valid())
        self.assertIn("api_key", form.errors)

    def test_saved_key_is_kept_when_editing(self):
        form = AIProviderForm(self._data(api_key=""), has_api_key=True)
        self.assertTrue(form.is_valid(), form.errors)

    def test_base_url_required_for_openai_compatible(self):
        form = AIProviderForm(self._data(provider_type="openai_compatible", api_key="", base_url=""))
        self.assertFalse(form.is_valid())
        self.assertIn("base_url", form.errors)


# ---------------------------------------------------------------------------
# Database explorer, table facts, fit checks and the result comparison
# ---------------------------------------------------------------------------

TABLE_STATS = {
    "large_orders": {
        "schema": "public", "name": "large_orders", "kind": "table", "relkind": "r",
        "total_bytes": 165_000_000, "table_bytes": 160_000_000, "index_bytes": 5_000_000,
        "row_estimate": 1_000_000, "is_partitioned": False, "partition_count": 0,
        "indexes": [{"name": "large_orders_pkey"}],
    },
    "customers": {
        "schema": "public", "name": "customers", "kind": "table", "relkind": "r",
        "total_bytes": 200_000, "table_bytes": 150_000, "index_bytes": 50_000,
        "row_estimate": 1000, "is_partitioned": False, "partition_count": 0, "indexes": [],
    },
}

# Hash Join over two scans: large_orders owns 90 of the 100 ms
PLAN = {"Plan": {
    "Node Type": "Hash Join", "Actual Total Time": 100.0, "Actual Loops": 1,
    "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "large_orders", "Actual Total Time": 90.0, "Actual Loops": 1},
        {"Node Type": "Seq Scan", "Relation Name": "customers", "Actual Total Time": 5.0, "Actual Loops": 1},
    ],
}}

OVERVIEW = {
    "success": True, "database": "shop", "version": "16.11", "total_bytes": 320_000_000,
    "user": "demo", "is_superuser": False, "extensions": [{"name": "postgis", "version": "3.6.2"}],
    "schemas": [{"name": "public", "tables": 1, "total_bytes": 165_000_000}],
    "tables": [{"schema": "public", "name": "large_orders", "relkind": "r", "kind": "table",
                "total_bytes": 165_000_000, "table_bytes": 160_000_000, "index_bytes": 5_000_000,
                "row_estimate": 1_000_000, "seq_scans": 12, "index_scans": 0, "last_analyze": None,
                "index_count": 1, "partition_count": 0}],
}

TABLE_DETAIL = {
    "success": True, "schema": "public", "name": "measurements", "kind": "partitioned table",
    "relkind": "p", "is_partitioned": True, "is_partition": False, "parent": None,
    "partition_key": "RANGE (recorded_at)", "total_bytes": 46_000_000, "table_bytes": 46_000_000,
    "index_bytes": 0, "toast_bytes": 0, "row_estimate": 600_000, "analyzed": True,
    "columns": [{"name": "recorded_at", "type": "timestamp without time zone", "nullable": False, "default": None}],
    "indexes": [{"name": "idx_status", "method": "btree", "columns": ["status"], "predicate": None,
                 "bytes": 4_000_000, "scans": 0, "is_primary": False, "is_unique": False, "is_valid": True,
                 "definition": "CREATE INDEX idx_status ON public.measurements USING btree (status)"}],
    "partitions": [{"schema": "public", "name": "measurements_2026_09", "relkind": "r",
                    "bounds": "FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')",
                    "total_bytes": 4_000_000, "row_estimate": 50_000}],
    "partition_count": 1, "constraints": [],
}


@mock.patch("advisor.middleware.AIServiceClient")
class ExplorerViewTests(TestCase):

    def setUp(self):
        cache.clear()
        self.connection = Connection.objects.create(
            name="Test DB", host="localhost", port=5433, database="shop", username="demo", password="demo")

    def _ready(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY

    def _panel(self, **params):
        return self.client.get(reverse("advisor:explorer_panel"), {"c": self.connection.pk, "db": "shop", **params})

    def test_old_urls_open_the_matching_place(self, client_cls):
        self._ready(client_cls)
        response = self.client.get(reverse("advisor:explorer_table", args=[self.connection.pk, "public", "orders"]))
        self.assertEqual(response.status_code, 302)
        for part in ("c=%d" % self.connection.pk, "db=shop", "schema=public", "kind=table", "name=orders"):
            self.assertIn(part, response["Location"])
        console = self.client.get(reverse("advisor:explorer_console", args=[self.connection.pk]) + "?sql=SELECT+1")
        self.assertIn("kind=query", console["Location"])
        self.assertIn("sql=SELECT+1", console["Location"])

    def test_page_selects_the_only_connection(self, client_cls):
        self._ready(client_cls)
        response = self.client.get(reverse("advisor:explorer"))
        self.assertContains(response, '\\u0022c\\u0022: \\u0022%d\\u0022' % self.connection.pk)

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_database_panel_shows_tables_with_sizes(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_overview.return_value = OVERVIEW
        response = self._panel(kind="database")
        self.assertContains(response, "large_orders")
        self.assertContains(response, "157.4\xa0MB")   # 165,000,000 bytes, filesizeformat
        self.assertContains(response, "1.0M")
        self.assertContains(response, "postgis 3.6.2")

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_panel_reports_backend_errors(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_overview.side_effect = ServiceError("password authentication failed", 400)
        self.assertContains(self._panel(kind="database"), "password authentication failed")

    def test_expired_password_is_explained_in_the_panel(self, client_cls):
        self._ready(client_cls)
        Connection.objects.filter(pk=self.connection.pk).update(
            password_updated_at=timezone.now() - timedelta(hours=5))
        response = self._panel(kind="database")
        self.assertContains(response, "has expired")
        self.assertContains(response, "kind=connection_edit")

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_table_panel_shows_partitions_and_unused_indexes(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_table.return_value = json.loads(json.dumps(TABLE_DETAIL))
        partitions = self._panel(schema="public", kind="table", name="measurements", tab="partitions")
        self.assertContains(partitions, "measurements_2026_09")
        self.assertContains(partitions, "RANGE (recorded_at)")
        indexes = self._panel(schema="public", kind="table", name="measurements", tab="indexes")
        self.assertContains(indexes, "unused")
        analyzer_cls.return_value.explorer_preview.assert_not_called()

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_ddl_tab_loads_the_script(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_table.return_value = json.loads(json.dumps(TABLE_DETAIL))
        analyzer_cls.return_value.explorer_object.return_value = {"success": True, "ddl": "CREATE TABLE x ();"}
        response = self._panel(schema="public", kind="table", name="measurements", tab="ddl")
        self.assertContains(response, "CREATE TABLE x ();")
        self.assertEqual(analyzer_cls.return_value.explorer_object.call_args[0][1], "table_ddl")

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_data_tab_loads_the_preview(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_table.return_value = json.loads(json.dumps(TABLE_DETAIL))
        analyzer_cls.return_value.explorer_preview.return_value = {
            "success": True, "columns": [{"name": "status", "type": "varchar"}], "rows": [["ok"], [None]]}
        response = self._panel(schema="public", kind="table", name="measurements", tab="data")
        self.assertContains(response, "NULL")
        analyzer_cls.return_value.explorer_preview.assert_called_once()

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_missing_table_is_explained(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_table.side_effect = ServiceError("Table public.nope not found", 404)
        self.assertContains(self._panel(schema="public", kind="table", name="nope"), "was not found")

    def test_unknown_panel_is_a_404(self, client_cls):
        self._ready(client_cls)
        self.assertEqual(self._panel(kind="nonsense").status_code, 404)

    def test_query_panel_is_prefilled_and_knows_the_host_is_local(self, client_cls):
        self._ready(client_cls)
        response = self._panel(kind="query", sql="SELECT 1")
        self.assertContains(response, "SELECT 1")
        self.assertContains(response, 'data-local="1"')

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_tree_lists_databases_then_groups(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_databases.return_value = {"databases": [
            {"name": "shop", "can_connect": True, "total_bytes": 1}, {"name": "secret", "can_connect": False, "total_bytes": None}]}
        tree = self.client.get(reverse("advisor:api_explorer_tree"), {"c": self.connection.pk}).json()["children"]
        self.assertEqual([n["label"] for n in tree], ["shop", "secret"])
        self.assertFalse(tree[1]["expandable"])
        analyzer_cls.return_value.explorer_groups.return_value = {"counts": {"tables": 3, "views": 0}}
        groups = self.client.get(reverse("advisor:api_explorer_tree"),
                                 {"c": self.connection.pk, "db": "shop", "schema": "public"}).json()["children"]
        self.assertEqual(groups[0]["count"], 3)
        self.assertTrue(groups[0]["expandable"])
        self.assertFalse(groups[1]["expandable"])

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_other_databases_use_the_same_credentials(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_overview.return_value = OVERVIEW
        self._panel(kind="database", db="analytics")
        params = analyzer_cls.return_value.explorer_overview.call_args[0][0]
        self.assertEqual(params["database"], "analytics")
        self.assertEqual(params["user"], "demo")

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_query_api_passes_results_through(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_query.return_value = {
            "success": True, "columns": [{"name": "?column?"}], "rows": [[1]], "row_count": 1,
            "truncated": False, "duration_ms": 1.2}
        response = self.client.post(reverse("advisor:api_explorer_query", args=[self.connection.pk]),
                                    data=json.dumps({"sql": "SELECT 1", "limit": 5000}),
                                    content_type="application/json")
        self.assertEqual(response.json()["rows"], [[1]])
        # The limit is clamped before it reaches the analyzer, and the default mode is read-only
        call = analyzer_cls.return_value.explorer_query.call_args
        self.assertEqual(call[0][2], 1000)
        self.assertEqual(call[1]["mode"], "read")

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_query_api_write_mode_reports_the_rollback(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_query.side_effect = ServiceError(
            "relation t does not exist", 400, {"rolled_back": True, "statement_index": 1, "statement_count": 2})
        response = self.client.post(reverse("advisor:api_explorer_query", args=[self.connection.pk]),
                                    data=json.dumps({"sql": "CREATE TABLE a(); INSERT INTO t VALUES (1)", "mode": "write"}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["rolled_back"])
        self.assertEqual(response.json()["statement_index"], 1)
        self.assertEqual(analyzer_cls.return_value.explorer_query.call_args[1]["mode"], "write")

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_query_api_returns_refusals_as_errors(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_query.side_effect = ServiceError("The console is read-only", 400)
        response = self.client.post(reverse("advisor:api_explorer_query", args=[self.connection.pk]),
                                    data=json.dumps({"sql": "DROP TABLE x"}), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("read-only", response.json()["error"])

    def test_query_api_refuses_an_expired_password(self, client_cls):
        self._ready(client_cls)
        Connection.objects.filter(pk=self.connection.pk).update(
            password_updated_at=timezone.now() - timedelta(hours=5))
        response = self.client.post(reverse("advisor:api_explorer_query", args=[self.connection.pk]),
                                    data=json.dumps({"sql": "SELECT 1"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_datagen_start_passes_the_confirmations(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.datagen_start.return_value = {"success": True, "job_id": "abc123abc123"}
        response = self.client.post(reverse("advisor:api_datagen_start"), data=json.dumps({
            "c": self.connection.pk, "db": "shop", "schema": "public", "targets": {"orders": 1048576},
            "confirm_database": "shop", "confirm_not_production": False}), content_type="application/json")
        self.assertEqual(response.json()["job_id"], "abc123abc123")
        kwargs = analyzer_cls.return_value.datagen_start.call_args[1]
        self.assertEqual(kwargs["confirm_database"], "shop")
        self.assertEqual(analyzer_cls.return_value.datagen_start.call_args[0][2], {"orders": 1048576})

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_a_job_the_analyzer_forgot_is_a_404(self, analyzer_cls, client_cls):
        """After a restart the job is gone: the page must hear 404 and stop waiting, not 502."""
        self._ready(client_cls)
        analyzer_cls.return_value.datagen_job.side_effect = ServiceError("Unknown job", 404)
        response = self.client.get(reverse("advisor:api_datagen_job", args=["abcdefabcdef"]))
        self.assertEqual(response.status_code, 404)

    def test_datagen_job_ids_are_checked(self, client_cls):
        self._ready(client_cls)
        response = self.client.get(reverse("advisor:api_datagen_job", args=["not-a-job"]))
        self.assertEqual(response.status_code, 404)


@mock.patch("advisor.middleware.AIServiceClient")
class ResultsFitTests(TestCase):

    def setUp(self):
        cache.clear()
        self.connection = Connection.objects.create(
            name="Test DB", host="localhost", port=5433, database="shop", username="demo", password="demo")

    def _history(self, **fields):
        defaults = dict(connection=self.connection, original_query="SELECT 1", original_plan=PLAN,
                        original_execution_time=100.0, analysis_status="completed", table_stats=TABLE_STATS)
        defaults.update(fields)
        return QueryHistory.objects.create(**defaults)

    def test_tables_involved_shows_sizes_and_the_bottleneck(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY
        history = self._history()
        response = self.client.get(reverse("advisor:view_results", args=[history.pk]))
        self.assertContains(response, "Tables Involved")
        self.assertContains(response, "Bottleneck")
        self.assertContains(response, "90%")   # 90 of the plan's 100 ms of self time
        tables = response.context["tables"]
        self.assertEqual(tables[0]["name"], "large_orders")
        self.assertTrue(tables[0]["is_bottleneck"])

    def test_old_analyses_say_sizes_were_not_recorded(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY
        history = self._history(table_stats={})
        response = self.client.get(reverse("advisor:view_results", args=[history.pk]))
        self.assertContains(response, "not recorded for analyses made before version 1.1")

    def test_a_rewrite_with_different_results_is_never_best(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY
        history = self._history()
        wrong = Recommendation.objects.create(
            query_history=history, recommendation_type="rewrite", description="Drop the leading wildcard",
            optimized_query="SELECT 2", tested_execution_time=0.1, verdict="faster", rank=1,
            query_was_rewritten=True, result_check="different",
            result_check_note="Returns 0 row(s) instead of 10.")
        right = Recommendation.objects.create(
            query_history=history, recommendation_type="index", description="Add an index",
            tested_execution_time=5.0, verdict="faster", rank=2, result_check="same",
            fit_checks=[{"level": "warn", "code": "large_table_lock", "message": "Use CREATE INDEX CONCURRENTLY."}])
        response = self.client.get(reverse("advisor:view_results", args=[history.pk]))
        recommendations = response.context["recommendations"]
        self.assertEqual([rec.pk for rec in recommendations], [right.pk, wrong.pk])
        self.assertContains(response, "Different results")
        self.assertContains(response, "Returns 0 row(s) instead of 10.")
        self.assertContains(response, "Use CREATE INDEX CONCURRENTLY.")

    def test_measured_index_size_is_shown(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY
        history = self._history()
        statement = "CREATE INDEX idx_email ON large_orders (customer_email);"
        Recommendation.objects.create(
            query_history=history, recommendation_type="index", description="Index the email",
            suggested_indexes=[statement], tested_execution_time=1.0, verdict="faster", rank=1,
            index_sizes=[{"statement": statement.rstrip(";"), "bytes": 32 * 1024 * 1024,
                          "table_bytes": 128 * 1024 * 1024}])
        response = self.client.get(reverse("advisor:view_results", args=[history.pk]))
        self.assertContains(response, "Measured size: 32.0\xa0MB (25% of the table")

    def test_report_payload_carries_the_new_fields(self, client_cls):
        history = self._history()
        rec = Recommendation.objects.create(
            query_history=history, recommendation_type="index", description="x", rank=1,
            result_check="same", result_check_note="n", fit_checks=[{"level": "ok"}], index_sizes=[{"bytes": 1}])
        payload = _report_payload(history, [rec])
        self.assertEqual(payload["query_history"]["table_stats"], TABLE_STATS)
        recommendation = payload["recommendations"][0]
        self.assertEqual(recommendation["result_check"], "same")
        self.assertEqual(recommendation["fit_checks"], [{"level": "ok"}])
        self.assertEqual(recommendation["index_sizes"], [{"bytes": 1}])


class RunAnalysisStoresFitTests(TestCase):

    @mock.patch("advisor.analysis.AnalyzerClient")
    def test_new_fields_are_saved(self, analyzer_cls):
        connection = Connection.objects.create(
            name="Test DB", host="localhost", port=5433, database="shop", username="demo", password="demo")
        analyzer_cls.return_value.optimize.return_value = {
            "success": True, "original_plan": PLAN, "original_execution_time": 100.0,
            "table_stats": TABLE_STATS,
            "recommendations": [{"type": "index", "description": "x", "rank": 1,
                                 "result_check": "same", "result_check_note": "Same SQL",
                                 "fit_checks": [{"level": "ok", "code": "no_overlap", "message": "m"}],
                                 "index_sizes": [{"statement": "s", "bytes": 10}]}],
        }
        history = run_analysis(connection, "SELECT 1")
        history.refresh_from_db()
        self.assertEqual(history.table_stats["large_orders"]["total_bytes"], 165_000_000)
        rec = history.recommendations.get()
        self.assertEqual(rec.result_check, "same")
        self.assertEqual(rec.fit_checks[0]["code"], "no_overlap")
        self.assertEqual(rec.index_sizes[0]["bytes"], 10)


class PlanUtilsTests(TestCase):

    def test_time_is_attributed_to_tables_by_self_time(self):
        times = table_time(PLAN)
        self.assertAlmostEqual(times["large_orders"]["ms"], 90.0)
        self.assertAlmostEqual(times["large_orders"]["share"], 90 / 100)
        self.assertEqual(bottleneck_table(PLAN), "large_orders")

    def test_loops_multiply_the_time(self):
        plan = {"Plan": {"Node Type": "Nested Loop", "Actual Total Time": 50.0, "Actual Loops": 1, "Plans": [
            {"Node Type": "Index Scan", "Relation Name": "orders", "Actual Total Time": 0.1, "Actual Loops": 400},
        ]}}
        self.assertAlmostEqual(table_time(plan)["orders"]["ms"], 40.0)

    def test_plans_without_timings_have_no_bottleneck(self):
        self.assertIsNone(bottleneck_table({"Plan": {"Node Type": "Seq Scan", "Relation Name": "t"}}))
        self.assertEqual(table_time(None), {})

class PartitionedTablesInvolvedTests(TestCase):
    """The plan names a partitioned table's partitions; their time belongs to the parent."""

    def test_partition_time_is_credited_to_the_parent(self):
        connection = Connection.objects.create(
            name="Test DB", host="localhost", port=5433, database="shop", username="demo", password="demo")
        stats = {"measurements": {"schema": "public", "name": "measurements", "is_partitioned": True,
                                  "partition_count": 2, "total_bytes": 46_000_000, "indexes": [],
                                  "partitions": [{"name": "m_2026_01"}, {"name": "m_2026_02"}]},
                 "sensors": {"schema": "public", "name": "sensors", "total_bytes": 10_000, "indexes": []}}
        plan = {"Plan": {"Node Type": "Hash Join", "Actual Total Time": 100.0, "Actual Loops": 1, "Plans": [
            {"Node Type": "Append", "Actual Total Time": 80.0, "Actual Loops": 1, "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "m_2026_01", "Actual Total Time": 40.0, "Actual Loops": 1},
                {"Node Type": "Seq Scan", "Relation Name": "m_2026_02", "Actual Total Time": 38.0, "Actual Loops": 1},
            ]},
            {"Node Type": "Seq Scan", "Relation Name": "sensors", "Actual Total Time": 10.0, "Actual Loops": 1},
        ]}}
        history = QueryHistory.objects.create(connection=connection, original_query="SELECT 1", original_plan=plan,
                                              analysis_status="completed", table_stats=stats)
        from .views import _tables_involved
        tables = _tables_involved(history)
        self.assertEqual(tables[0]["name"], "measurements")
        self.assertTrue(tables[0]["is_bottleneck"])
        self.assertEqual(tables[0]["time_share"], 78)
        self.assertFalse(tables[1]["is_bottleneck"])

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
CONNECTION_FORM = {"name": "Local", "host": "localhost", "port": 5432, "database": "postgres",
                   "username": "postgres", "password": "secret", "ssl_mode": "prefer"}


@mock.patch("advisor.middleware.AIServiceClient")
class ConnectionManagementTests(TestCase):
    """Connections are managed inside the Databases page."""

    def setUp(self):
        cache.clear()
        self.connection = Connection.objects.create(
            name="Test DB", host="db.internal", port=5433, database="shop", username="demo", password="demo")

    def _ready(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY

    def test_old_pages_open_the_databases_page(self, client_cls):
        self._ready(client_cls)
        self.assertEqual(self.client.get(reverse("advisor:connection_list"))["Location"], reverse("advisor:explorer"))
        self.assertIn("kind=connection_add", self.client.get(reverse("advisor:connection_add"))["Location"])
        edit = self.client.get(reverse("advisor:connection_edit", args=[self.connection.pk]))["Location"]
        self.assertIn("kind=connection_edit", edit)
        self.assertIn("c=%d" % self.connection.pk, edit)

    def test_add_from_the_panel(self, client_cls):
        self._ready(client_cls)
        response = self.client.post(reverse("advisor:connection_add"), CONNECTION_FORM, **AJAX)
        created = Connection.objects.get(name="Local")
        self.assertEqual(response.json(), {"ok": True, "c": created.pk})
        self.assertEqual(created.get_decrypted_password(), "secret")

    def test_invalid_form_comes_back_as_the_panel_with_errors(self, client_cls):
        self._ready(client_cls)
        response = self.client.post(reverse("advisor:connection_add"), dict(CONNECTION_FORM, name=""), **AJAX)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "data-panel-form", status_code=400)
        self.assertContains(response, "form-error", status_code=400)
        self.assertNotContains(response, "<html", status_code=400)

    def test_edit_from_the_panel(self, client_cls):
        self._ready(client_cls)
        response = self.client.post(reverse("advisor:connection_edit", args=[self.connection.pk]),
                                    dict(CONNECTION_FORM, name="Renamed"), **AJAX)
        self.assertTrue(response.json()["ok"])
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.name, "Renamed")

    def test_edit_panel_shows_decrypted_values_but_never_the_password(self, client_cls):
        self._ready(client_cls)
        response = self.client.get(reverse("advisor:explorer_panel"),
                                   {"c": self.connection.pk, "kind": "connection_edit"})
        self.assertContains(response, 'value="db.internal"')
        self.assertNotContains(response, 'value="demo" type="password"')
        self.assertNotContains(response, "gAAAAA")

    def test_delete_refuses_a_connection_with_analyses(self, client_cls):
        self._ready(client_cls)
        QueryHistory.objects.create(connection=self.connection, original_query="SELECT 1", analysis_status="completed")
        response = self.client.post(reverse("advisor:connection_delete", args=[self.connection.pk]), **AJAX)
        self.assertEqual(response.status_code, 409)
        self.assertIn("History", response.json()["error"])
        self.assertTrue(Connection.objects.filter(pk=self.connection.pk).exists())

    def test_delete_from_the_panel(self, client_cls):
        self._ready(client_cls)
        response = self.client.post(reverse("advisor:connection_delete", args=[self.connection.pk]), **AJAX)
        self.assertEqual(response.json(), {"ok": True})
        self.assertFalse(Connection.objects.filter(pk=self.connection.pk).exists())

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_unreachable_server_still_offers_edit_and_delete(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.explorer_server.side_effect = ServiceError("could not connect to server", 400)
        response = self.client.get(reverse("advisor:explorer_panel"), {"c": self.connection.pk, "kind": "server"})
        self.assertContains(response, "could not connect to server")
        self.assertContains(response, "kind=connection_edit")
        self.assertContains(response, reverse("advisor:connection_delete", args=[self.connection.pk]))
        self.assertContains(response, "db.internal:5433")

    @mock.patch("advisor.explorer_views.AnalyzerClient")
    def test_expired_password_does_not_call_the_server(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        Connection.objects.filter(pk=self.connection.pk).update(
            password_updated_at=timezone.now() - timedelta(hours=5))
        response = self.client.get(reverse("advisor:explorer_panel"), {"c": self.connection.pk, "kind": "server"})
        self.assertContains(response, "has expired")
        analyzer_cls.return_value.explorer_server.assert_not_called()

    def test_no_connections_opens_the_add_form(self, client_cls):
        self._ready(client_cls)
        Connection.objects.all().delete()
        response = self.client.get(reverse("advisor:explorer"))
        self.assertContains(response, "connection_add")


@mock.patch("advisor.middleware.AIServiceClient")
class LiveProgressTests(TestCase):

    def setUp(self):
        cache.clear()
        self.connection = Connection.objects.create(
            name="Test DB", host="localhost", port=5433, database="shop", username="demo", password="demo")

    def _ready(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY

    @mock.patch("advisor.views.run_analysis")
    def test_analyze_passes_the_progress_id_through(self, run_analysis, client_cls):
        self._ready(client_cls)
        run_analysis.return_value = mock.Mock(analysis_status="failed", error_message="x")
        self.client.post(reverse("advisor:analyze_query", args=[self.connection.pk]),
                         {"query": "SELECT 1", "test_recommendations": "on", "progress_id": "f" * 32})
        self.assertEqual(run_analysis.call_args[1]["progress_id"], "f" * 32)

    @mock.patch("advisor.views.run_analysis")
    def test_a_malformed_progress_id_is_dropped(self, run_analysis, client_cls):
        self._ready(client_cls)
        run_analysis.return_value = mock.Mock(analysis_status="failed", error_message="x")
        self.client.post(reverse("advisor:analyze_query", args=[self.connection.pk]),
                         {"query": "SELECT 1", "progress_id": "../../x"})
        self.assertIsNone(run_analysis.call_args[1]["progress_id"])

    @mock.patch("advisor.views.AnalyzerClient")
    def test_progress_proxy(self, analyzer_cls, client_cls):
        self._ready(client_cls)
        analyzer_cls.return_value.analyze_progress.return_value = {"success": True, "phase": "ai"}
        response = self.client.get(reverse("advisor:api_analyze_progress", args=["a" * 32]))
        self.assertEqual(response.json()["phase"], "ai")
        self.assertEqual(self.client.get(reverse("advisor:api_analyze_progress", args=["nope"])).status_code, 404)

    def test_analyze_page_can_be_prefilled(self, client_cls):
        self._ready(client_cls)
        response = self.client.get(reverse("advisor:analyze_query", args=[self.connection.pk]),
                                   {"query": "SELECT 42"})
        self.assertContains(response, "SELECT 42")

    def test_test_cases_show_their_explanation(self, client_cls):
        self._ready(client_cls)
        response = self.client.get(reverse("advisor:test_cases"))
        self.assertContains(response, "pg_trgm GIN")   # like-wildcard's explanation
        self.assertContains(response, "Open in Query tool")


@mock.patch("advisor.middleware.AIServiceClient")
class SavedTestCaseResultsTests(TestCase):
    """Test case results come back from History when the page is opened again."""

    def setUp(self):
        cache.clear()
        from .benchmark_cases import case_by_slug
        self.case = case_by_slug("simple-filter")
        self.connection = Connection.objects.create(
            name="Demo", host="localhost", port=55432, database="shop", username="demo", password="demo")

    def _ready(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY

    def _run(self, connection, status="completed", original=200.0, tested=5.0, verdict="faster", error=None):
        history = QueryHistory.objects.create(connection=connection, original_query=self.case["query"],
                                              analysis_status=status, original_execution_time=original,
                                              error_message=error)
        if status == "completed":
            Recommendation.objects.create(query_history=history, recommendation_type="index", description="x",
                                          tested_execution_time=tested, verdict=verdict, rank=1)
        return history

    def _page(self):
        return self.client.get(reverse("advisor:test_cases"), {"connection": self.connection.pk})

    def test_a_finished_case_shows_its_saved_result(self, client_cls):
        self._ready(client_cls)
        history = self._run(self.connection)
        response = self._page()
        self.assertContains(response, "200.0 ms")
        self.assertContains(response, "5.000 ms")
        self.assertContains(response, "+97.5%")
        self.assertContains(response, reverse("advisor:view_results", args=[history.id]))
        self.assertContains(response, "Last results: 1 of")

    def test_the_newest_run_wins(self, client_cls):
        self._ready(client_cls)
        self._run(self.connection, original=200.0, tested=5.0)
        newer = self._run(self.connection, original=300.0, tested=150.0)
        response = self._page()
        self.assertContains(response, "300.0 ms")
        self.assertNotContains(response, "200.0 ms")
        self.assertContains(response, reverse("advisor:view_results", args=[newer.id]))

    def test_a_failed_run_shows_its_error(self, client_cls):
        self._ready(client_cls)
        self._run(self.connection, status="failed", error="All AI providers failed")
        response = self._page()
        self.assertContains(response, "All AI providers failed")
        self.assertContains(response, "is-failed")

    def test_other_connections_do_not_leak_in(self, client_cls):
        self._ready(client_cls)
        other = Connection.objects.create(name="Other", host="h", port=5432, database="db", username="u", password="p")
        self._run(other)
        response = self._page()
        self.assertNotContains(response, "200.0 ms")
        self.assertContains(response, "not run")

    def test_verdicts_read_as_words(self, client_cls):
        self._ready(client_cls)
        self._run(self.connection, original=2.0, tested=1.9, verdict="within_noise")
        self.assertContains(self._page(), "within noise")


# ---------------------------------------------------------------------- desktop hardening

from django.test import override_settings  # noqa: E402

from .clients import AIServiceClient  # noqa: E402
from .desktop_access import COOKIE, _cookie_value  # noqa: E402


@mock.patch("advisor.middleware.AIServiceClient")
@override_settings(DESKTOP_ACCESS_TOKEN="run-key-123")
class DesktopAccessTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_pages_refused_without_the_window_cookie(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY
        for path in ("/", "/history/", "/explorer/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403, path)
            self.assertContains(response, "own window", status_code=403)

    def test_health_and_static_stay_open(self, client_cls):
        self.assertEqual(self.client.get("/healthz/").status_code, 200)

    def test_wrong_key_refused(self, client_cls):
        self.assertEqual(self.client.get("/desktop/open/?key=nope").status_code, 403)
        self.assertEqual(self.client.get("/desktop/open/").status_code, 403)

    def test_window_key_opens_the_app_and_leaves_the_address(self, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY
        response = self.client.get("/desktop/open/?key=run-key-123")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        cookie = response.cookies[COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(self.client.get("/history/").status_code, 200)

    def test_cookie_from_an_earlier_run_is_useless(self, client_cls):
        self.client.cookies[COOKIE] = _cookie_value("last-runs-key")
        self.assertEqual(self.client.get("/history/").status_code, 403)

    def test_forged_cookie_refused(self, client_cls):
        self.client.cookies[COOKIE] = "anything"
        self.assertEqual(self.client.get("/history/").status_code, 403)


@mock.patch("advisor.middleware.AIServiceClient")
class DesktopAccessOffTests(TestCase):

    def test_nothing_changes_without_the_token(self, client_cls):  # the Docker stack
        client_cls.return_value.status.return_value = STATUS_READY
        cache.clear()
        self.assertEqual(self.client.get("/history/").status_code, 200)


class InternalTokenTests(TestCase):

    def _call(self):
        with mock.patch("advisor.clients.requests.request") as request:
            request.return_value.status_code = 200
            request.return_value.json.return_value = {"ready": True}
            AIServiceClient().status()
            return request.call_args.kwargs.get("headers") or {}

    @override_settings(INTERNAL_TOKEN="svc-token")
    def test_calls_carry_the_service_token(self):
        self.assertEqual(self._call().get("X-Tuning-Buddy-Token"), "svc-token")

    @override_settings(INTERNAL_TOKEN="")
    def test_no_header_without_it(self):
        self.assertNotIn("X-Tuning-Buddy-Token", self._call())


# ---------------------------------------------------------------------- medium fixes

from .forms import ConnectionForm, QueryForm  # noqa: E402


class AnalyzeFormReadOnlyTests(TestCase):

    def test_select_and_with_are_fine(self):
        for sql in ("SELECT 1", "  -- note\nWITH x AS (SELECT 1) SELECT * FROM x", "(SELECT 1)", "/* c */ select 2"):
            self.assertTrue(QueryForm({"query": sql}).is_valid(), sql)

    def test_writes_are_explained_not_run(self):
        for sql in ("UPDATE orders SET total = 0", "DELETE FROM orders", "DROP TABLE x", "-- hi\nINSERT INTO t VALUES (1)"):
            form = QueryForm({"query": sql})
            self.assertFalse(form.is_valid(), sql)
            self.assertIn("read-only", form.errors["query"][0])


class RemoteSslTests(TestCase):

    def _form(self, host, ssl_mode, allow=False):
        data = {"name": "x", "host": host, "port": 5432, "database": "d", "username": "u",
                "password": "p", "ssl_mode": ssl_mode}
        if allow:
            data["allow_unencrypted"] = "on"
        return ConnectionForm(data)

    def test_remote_server_needs_encryption(self):
        for mode in ("disable", "allow", "prefer"):
            form = self._form("db.example.com", mode)
            self.assertFalse(form.is_valid(), mode)
            self.assertIn("Require", form.errors["ssl_mode"][0])

    def test_remote_server_with_require_or_verify_is_fine(self):
        for mode in ("require", "verify-ca", "verify-full"):
            self.assertTrue(self._form("db.example.com", mode).is_valid(), mode)

    def test_unencrypted_remote_only_when_ticked(self):
        self.assertTrue(self._form("10.0.0.5", "prefer", allow=True).is_valid())

    def test_this_computer_needs_nothing(self):
        for host in ("localhost", "127.0.0.1", "::1", "[::1]", "host.docker.internal"):
            self.assertTrue(self._form(host, "prefer").is_valid(), host)

    def test_panel_flag(self):
        remote = Connection.objects.create(name="r", host="db.example.com", port=5432, database="d",
                                           username="u", password="p", ssl_mode="prefer")
        local = Connection.objects.create(name="l", host="localhost", port=5432, database="d",
                                          username="u", password="p", ssl_mode="disable")
        secure = Connection.objects.create(name="s", host="db.example.com", port=5432, database="d",
                                           username="u", password="p", ssl_mode="verify-full")
        self.assertTrue(remote.may_be_unencrypted)
        self.assertFalse(local.may_be_unencrypted)
        self.assertFalse(secure.may_be_unencrypted)


# ---------------------------------------------------------------------- low fixes

from django.core.exceptions import ImproperlyConfigured  # noqa: E402

from .models import EncryptedFieldMixin  # noqa: E402


class EncryptionFailsClosedTests(TestCase):

    @override_settings(ENCRYPTION_KEY="")
    def test_no_key_means_nothing_is_stored(self):
        with self.assertRaises(ImproperlyConfigured):
            Connection.objects.create(name="x", host="h", port=5432, database="d", username="u", password="p")
        self.assertEqual(Connection.objects.count(), 0)

    @override_settings(ENCRYPTION_KEY="not-a-key")
    def test_bad_key_means_nothing_is_stored(self):
        with self.assertRaises(ImproperlyConfigured):
            EncryptedFieldMixin.encrypt("secret")

    def test_a_password_that_looks_like_a_token_is_still_encrypted(self):
        conn = Connection.objects.create(name="x", host="h", port=5432, database="d", username="u",
                                         password="gAAAAA-my-real-password")
        conn.refresh_from_db()
        self.assertNotEqual(conn.password, "gAAAAA-my-real-password")
        self.assertEqual(conn.get_decrypted_password(), "gAAAAA-my-real-password")

    def test_saving_again_does_not_encrypt_twice(self):
        conn = Connection.objects.create(name="x", host="h", port=5432, database="d", username="u", password="p")
        conn.save()
        conn.refresh_from_db()
        self.assertEqual(conn.get_decrypted_password(), "p")


@mock.patch("advisor.middleware.AIServiceClient")
class RemoteAnalyzeConfirmationTests(TestCase):

    def setUp(self):
        cache.clear()

    def _connection(self, host):
        return Connection.objects.create(name="c", host=host, port=5432, database="d", username="u",
                                         password="p", ssl_mode="require")

    @mock.patch("advisor.views.run_analysis")
    def test_remote_testing_needs_confirmation(self, run, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY
        conn = self._connection("db.example.com")
        response = self.client.post(reverse("advisor:analyze_query", args=[conn.pk]),
                                    {"query": "SELECT 1", "test_recommendations": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirm that Tuning Buddy may copy these tables on db.example.com")
        run.assert_not_called()

    @mock.patch("advisor.views.run_analysis")
    def test_confirmed_or_untested_or_local_runs(self, run, client_cls):
        client_cls.return_value.status.return_value = STATUS_READY
        run.return_value = mock.Mock(analysis_status="failed", error_message="stop here", id=1)
        remote, local = self._connection("db.example.com"), self._connection("localhost")
        for conn, data in ((remote, {"test_recommendations": "on", "confirm_remote": "on"}),
                           (remote, {}),
                           (local, {"test_recommendations": "on"})):
            run.reset_mock()
            self.client.post(reverse("advisor:analyze_query", args=[conn.pk]), {"query": "SELECT 1", **data})
            run.assert_called_once()
