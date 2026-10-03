"""
The report renders text produced by an AI provider, so it must survive markup,
missing fields and empty result sets. The plan diagram must only cry "bottleneck"
when there genuinely is one.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pdf_generator import (
    best_recommendation,
    collect_plan_nodes,
    create_plan_flowchart,
    escape_markup,
    find_bottleneck,
    format_plan_tree,
    plan_metrics,
    verdict_sentence,
)

ORIGINAL_PLAN = {
    "Plan": {
        "Node Type": "Seq Scan",
        "Relation Name": "large_orders",
        "Total Cost": 24518.0,
        "Actual Rows": 11,
        "Plan Rows": 5,
        "Actual Total Time": 160.4,
        "Actual Loops": 1,
        "Filter": "(customer_email = 'user12345@example.com'::text)",
    },
    "Execution Time": 161.06,
    "Planning Time": 0.12,
}

TESTED_PLAN = {
    "Plan": {
        "Node Type": "Index Scan",
        "Relation Name": "large_orders",
        "Index Name": "idx_large_orders_customer_email",
        "Total Cost": 8.45,
        "Actual Rows": 11,
        "Actual Total Time": 0.09,
    },
    "Execution Time": 0.157,
}

JOIN_PLAN = {
    "Plan": {
        "Node Type": "Aggregate",
        "Actual Total Time": 42.0,
        "Actual Rows": 20,
        "Plans": [{
            "Node Type": "Hash Join",
            "Actual Total Time": 40.0,
            "Actual Rows": 500,
            "Hash Cond": "(o.customer_id = c.id)",
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "orders",
                 "Actual Total Time": 30.0, "Actual Rows": 2000},
                {"Node Type": "Index Scan", "Relation Name": "customers",
                 "Index Name": "customers_pkey", "Actual Total Time": 2.0, "Actual Rows": 140},
            ],
        }],
    },
    "Execution Time": 42.5,
}

# Work spread evenly across three index scans - nothing dominates
BALANCED_PLAN = {
    "Plan": {
        "Node Type": "Append",
        "Actual Total Time": 3.5,
        "Plans": [
            {"Node Type": "Index Scan", "Relation Name": "a", "Actual Total Time": 1.0},
            {"Node Type": "Index Scan", "Relation Name": "b", "Actual Total Time": 1.0},
            {"Node Type": "Index Scan", "Relation Name": "c", "Actual Total Time": 1.0},
        ],
    },
    "Execution Time": 3.6,
}


def payload(**overrides):
    body = {
        "query_history": {
            "id": 3,
            "original_query": "SELECT * FROM large_orders WHERE customer_email = 'user12345@example.com';",
            "original_plan": ORIGINAL_PLAN,
            "original_execution_time": 161.06,
            "ai_provider": {"provider_name": "Deepseek", "model": "deepseek-chat"},
            "created_at": "2026-09-22T12:58:00+00:00",
            "created_at_display": "2026-09-22 19:58",
            "connection": {"name": "Demo shop", "database": "shop"},
        },
        "recommendations": [{
            "id": 1,
            "recommendation_type": "index",
            "recommendation_type_display": "Add Index",
            "description": "Add an index on customer_email",
            "optimized_query": "SELECT * FROM large_orders WHERE customer_email = 'x';",
            "suggested_indexes": ["CREATE INDEX idx ON large_orders (customer_email)"],
            "all_indexes_applied": ["CREATE INDEX idx ON large_orders (customer_email)"],
            "tested_execution_time": 0.157,
            "tested_plan": TESTED_PLAN,
            "rank": 1,
        }],
    }
    body.update(overrides)
    return body


def labels_of(drawing) -> list:
    return [shape.text for shape in drawing.contents if hasattr(shape, 'text')]


@pytest.fixture
def client():
    return TestClient(app)


def render(client, body) -> bytes:
    response = client.post("/reports/optimization", json=body)
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF")
    return response.content


def test_renders_a_pdf(client):
    assert len(render(client, payload())) > 2000


def test_every_inline_font_colour_is_a_valid_spec():
    """
    A '<font color=...>' tag built from a Color object silently produced '0b2545'
    instead of '#0b2545' and broke every render. Parse what the generator emits.
    """
    from reportlab.platypus import Paragraph

    from app.pdf_generator import PDFReportGenerator, Palette

    generator = PDFReportGenerator()
    for color in (Palette.INK, Palette.ACCENT_DEEP, Palette.SUCCESS, Palette.DANGER, Palette.MUTED):
        # '#rrggbb' is the form ReportLab accepts; the bare hex body is not
        Paragraph(f"<font color='#{color.hexval()[2:]}'>x</font>", generator.styles['CustomBody'])
        with pytest.raises(ValueError):
            Paragraph(f"<font color='{color.hexval()[2:]}'>x</font>",
                      generator.styles['CustomBody']).wrap(100, 100)


def test_survives_markup_in_ai_written_text(client):
    """A description with < and & used to break paragraph parsing."""
    body = payload()
    body["recommendations"][0]["description"] = (
        "Use WHERE amount < 500 & status <> 'cancelled' -- see <https://example.com>"
    )
    assert render(client, body)


def test_handles_no_recommendations(client):
    assert render(client, payload(recommendations=[]))


def test_handles_untested_recommendation(client):
    body = payload()
    body["recommendations"][0].pop("tested_execution_time")
    body["recommendations"][0].pop("tested_plan")
    assert render(client, body)


def test_handles_missing_plan_and_optional_fields(client):
    body = payload()
    body["query_history"]["original_plan"] = None
    body["query_history"]["original_execution_time"] = None
    body["query_history"]["ai_provider"] = None
    body["query_history"].pop("created_at_display")
    assert render(client, body)


def test_renders_multi_node_join_plan(client):
    body = payload()
    body["query_history"]["original_plan"] = JOIN_PLAN
    assert render(client, body)


def test_escape_markup_neutralizes_tags():
    assert escape_markup("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert escape_markup(None) == ""


def test_plan_tree_is_readable():
    tree = format_plan_tree(ORIGINAL_PLAN)
    assert "Seq Scan on large_orders" in tree
    assert "cost=24,518.0" in tree
    assert "time=160.40ms" in tree
    assert "customer_email" in tree  # the filter condition is shown


def test_plan_tree_handles_missing_plan():
    assert "not available" in format_plan_tree(None)


def test_plan_metrics_reads_headline_numbers():
    metrics = plan_metrics(ORIGINAL_PLAN)
    assert metrics["execution_time"] == 161.06
    assert metrics["total_cost"] == 24518.0
    assert metrics["node_type"] == "Seq Scan"


# ----------------------------------------------------------------------
# Plan flowchart
# ----------------------------------------------------------------------

def test_flowchart_collects_every_node_with_depth_and_columns():
    entries, columns, truncated = collect_plan_nodes(JOIN_PLAN)
    assert len(entries) == 4
    assert columns == 2  # two leaf scans sit side by side
    assert truncated is False
    depths = {entry['node']['Node Type']: entry['depth'] for entry in entries}
    assert depths == {"Aggregate": 0, "Hash Join": 1, "Seq Scan": 2, "Index Scan": 2}


def test_flowchart_self_time_excludes_children():
    entries, _, _ = collect_plan_nodes(JOIN_PLAN)
    by_type = {entry['node']['Node Type']: entry for entry in entries}
    # Hash Join reports 40ms total but its children account for 32ms of that
    assert by_type["Hash Join"]['self_time'] == pytest.approx(8.0)
    assert by_type["Seq Scan"]['self_time'] == pytest.approx(30.0)


def test_find_bottleneck_picks_the_dominant_node():
    entries, _, _ = collect_plan_nodes(JOIN_PLAN)
    assert find_bottleneck(entries)['node']['Node Type'] == "Seq Scan"


def test_find_bottleneck_ignores_evenly_spread_work():
    """Nothing dominates a balanced plan, so nothing should be singled out."""
    entries, _, _ = collect_plan_nodes(BALANCED_PLAN)
    assert find_bottleneck(entries) is None


def test_flowchart_marks_a_dominant_seq_scan_as_bottleneck():
    assert "BOTTLENECK" in labels_of(create_plan_flowchart(JOIN_PLAN, 450))


def test_optimized_index_plan_is_never_called_a_bottleneck():
    """An index scan that simply is the costliest step is not a bottleneck."""
    labels = labels_of(create_plan_flowchart(TESTED_PLAN, 450))
    assert "BOTTLENECK" not in labels
    assert "HIGHEST COST" in labels


def test_flowchart_can_suppress_the_callout_entirely():
    labels = labels_of(create_plan_flowchart(JOIN_PLAN, 450, highlight_bottleneck=False))
    assert "BOTTLENECK" not in labels
    assert "HIGHEST COST" not in labels


def test_flowchart_truncates_very_deep_plans():
    node = {"Node Type": "Limit", "Actual Total Time": 1.0}
    deepest = node
    for _ in range(20):
        child = {"Node Type": "Nested Loop", "Actual Total Time": 1.0}
        deepest["Plans"] = [child]
        deepest = child
    entries, _, truncated = collect_plan_nodes({"Plan": node}, max_nodes=5)
    assert len(entries) <= 5
    assert truncated is True


def test_flowchart_reads_top_down_from_the_scans():
    """Scans run first, so they belong at the top, feeding the steps below them."""
    drawing = create_plan_flowchart(JOIN_PLAN, 450)
    tops = {shape.text: shape.y for shape in drawing.contents if hasattr(shape, 'text')}
    assert tops["Seq Scan"] > tops["Hash Join"] > tops["Aggregate"]
    assert tops["Index Scan"] > tops["Hash Join"]


def test_flowchart_drawing_is_produced():
    drawing = create_plan_flowchart(JOIN_PLAN, 450)
    assert drawing is not None
    assert drawing.width == 450
    assert len(drawing.contents) > 4  # boxes, labels, connectors and legend


def test_flowchart_returns_none_without_a_plan():
    assert create_plan_flowchart(None, 450) is None


class FakeRec:
    def __init__(self, tested_execution_time, verdict='faster', spread=None):
        self.tested_execution_time = tested_execution_time
        self.verdict = verdict
        self.measurement_spread_ms = spread
        self.tested_execution_times = []


def test_best_recommendation_picks_the_fastest_that_beats_the_original():
    recs = [FakeRec(90.0), FakeRec(5.0), FakeRec(None), FakeRec(150.0)]
    assert best_recommendation(recs, 100.0).tested_execution_time == 5.0


def test_best_recommendation_returns_none_when_nothing_is_faster():
    assert best_recommendation([FakeRec(150.0), FakeRec(None)], 100.0) is None


def test_best_recommendation_ignores_wins_inside_the_noise():
    """A faster number that is not a significant result must not become the action."""
    noisy = FakeRec(38.0, verdict='within_noise')
    assert best_recommendation([noisy], 40.0) is None


def test_best_recommendation_ignores_already_fast_queries():
    """The 0.2ms primary key lookup that used to report '+81% faster'."""
    trivial = FakeRec(0.056, verdict='already_fast')
    assert best_recommendation([trivial], 0.185) is None


def test_best_recommendation_still_accepts_legacy_rows_without_a_verdict():
    assert best_recommendation([FakeRec(5.0, verdict='')], 100.0).tested_execution_time == 5.0


def test_verdict_sentence_is_honest_about_noise():
    assert "cache noise" in verdict_sentence(FakeRec(0.05, verdict='already_fast'), 0.2)
    assert "No measurable difference" in verdict_sentence(
        FakeRec(38.0, verdict='within_noise', spread=6.0), 40.0)
    assert "faster" in verdict_sentence(FakeRec(1.0, verdict='faster'), 100.0)
    assert "slower" in verdict_sentence(FakeRec(200.0, verdict='slower'), 100.0)
    assert verdict_sentence(FakeRec(None), 100.0) == "Not tested"


def test_report_says_no_action_needed_for_an_already_fast_query(client):
    body = payload()
    body["query_history"]["original_execution_time"] = 0.185
    body["recommendations"][0]["tested_execution_time"] = 0.056
    body["recommendations"][0]["verdict"] = "already_fast"
    assert render(client, body)


def test_report_handles_every_option_being_noise(client):
    body = payload()
    body["recommendations"][0]["verdict"] = "within_noise"
    body["recommendations"][0]["measurement_spread_ms"] = 6.0
    body["recommendations"][0]["tested_execution_times"] = [38.0, 41.0, 37.0]
    assert render(client, body)


# ---------------------------------------------------------------------------
# Table sizes, the bottleneck table, fit checks and the result comparison
# ---------------------------------------------------------------------------

TABLE_STATS = {
    "large_orders": {"schema": "public", "name": "large_orders", "total_bytes": 165_000_000,
                     "table_bytes": 160_000_000, "index_bytes": 5_000_000, "row_estimate": 1_000_000,
                     "is_partitioned": False, "indexes": [{"name": "large_orders_pkey"}]},
    "customers": {"schema": "public", "name": "customers", "total_bytes": 200_000, "table_bytes": 150_000,
                  "index_bytes": 50_000, "row_estimate": 1000, "is_partitioned": False, "indexes": []},
}


def test_time_share_follows_the_scan_nodes():
    from app.pdf_generator import table_time_shares
    shares = table_time_shares(JOIN_PLAN)
    # Aggregate 2 + Hash Join 8 + orders 30 + customers 2 = 42 ms of self time
    assert shares["orders"] == pytest.approx(30 / 42)
    assert shares["customers"] == pytest.approx(2 / 42)


def test_table_rows_put_the_bottleneck_first():
    from app.pdf_generator import table_rows
    rows = table_rows(TABLE_STATS, ORIGINAL_PLAN)
    assert rows[0]["label"] == "large_orders"
    assert rows[0]["is_bottleneck"]
    assert rows[0]["share"] == pytest.approx(1.0)
    assert not rows[1]["is_bottleneck"]


def test_table_rows_skip_errors_and_old_payloads():
    from app.pdf_generator import table_rows
    assert table_rows({}, ORIGINAL_PLAN) == []
    assert table_rows({"gone": {"error": "not found"}}, ORIGINAL_PLAN) == []


def test_size_formatting():
    from app.pdf_generator import format_bytes, format_rows
    assert format_bytes(165_000_000) == "157.4 MB"
    assert format_bytes(512) == "512 bytes"
    assert format_rows(1_000_000) == "1.0M"
    assert format_rows(950) == "950"


def test_best_recommendation_never_picks_a_rewrite_with_different_results():
    wrong, right = FakeRec(0.1), FakeRec(5.0)
    wrong.result_check, right.result_check = "different", "same"
    assert best_recommendation([wrong, right], 160.0) is right
    assert best_recommendation([wrong], 160.0) is None


def test_tables_section_names_size_and_share():
    from app.pdf_generator import PDFReportGenerator
    elements = PDFReportGenerator()._create_tables_section(TABLE_STATS, ORIGINAL_PLAN)
    table = next(element for element in elements if element.__class__.__name__ == "Table")
    cells = [getattr(cell, 'text', cell) for row in table._cellvalues for cell in row]
    name_cell = table._cellvalues[1][0].text
    assert "large_orders" in name_cell and "BOTTLENECK" in name_cell
    assert "157.4 MB" in cells
    assert "100%" in cells


def test_report_with_table_stats_fit_checks_and_result_check(client):
    statement = "CREATE INDEX idx ON large_orders (customer_email)"
    body = payload()
    body["query_history"]["table_stats"] = TABLE_STATS
    body["recommendations"][0].update({
        "result_check": "same", "result_check_note": "Same SQL as the original.",
        "index_sizes": [{"statement": statement, "bytes": 32 * 1024 * 1024, "table_bytes": 160_000_000}],
        "fit_checks": [
            {"level": "warn", "code": "large_table_lock", "message": "Use CREATE INDEX CONCURRENTLY <now>."},
            {"level": "ok", "code": "no_overlap", "message": "No existing index covers customer_email."},
        ],
    })
    body["recommendations"].append({
        "id": 2, "recommendation_type": "rewrite", "recommendation_type_display": "Query Rewrite",
        "description": "Drop the wildcard", "optimized_query": "SELECT 1", "final_optimized_query": "SELECT 1",
        "query_was_rewritten": True, "tested_execution_time": 0.01, "verdict": "faster", "rank": 2,
        "result_check": "different", "result_check_note": "Returns 0 row(s) instead of 10.",
    })
    assert len(render(client, body)) > 2000

PARTITIONED_STATS = {"measurements": {"name": "measurements", "is_partitioned": True, "partition_count": 2,
                                      "total_bytes": 46_000_000, "row_estimate": 600_000, "indexes": [],
                                      "partitions": [{"name": "m_2026_01"}, {"name": "m_2026_02"}]}}
PARTITIONED_PLAN = {"Plan": {"Node Type": "Append", "Actual Total Time": 80.0, "Actual Loops": 1, "Plans": [
    {"Node Type": "Seq Scan", "Relation Name": "m_2026_01", "Actual Total Time": 40.0, "Actual Loops": 1},
    {"Node Type": "Seq Scan", "Relation Name": "m_2026_02", "Actual Total Time": 38.0, "Actual Loops": 1},
]}}


def test_partition_time_belongs_to_the_parent_table():
    from app.pdf_generator import table_rows
    rows = table_rows(PARTITIONED_STATS, PARTITIONED_PLAN)
    assert rows[0]["is_bottleneck"]
    assert rows[0]["share"] == pytest.approx(78 / 80)


def test_partitions_are_collapsed_into_their_parent():
    from app.pdf_generator import collapse_partitions
    assert collapse_partitions(["m_2026_01", "m_2026_02", "orders"], PARTITIONED_STATS) == \
        ["measurements (2 of 2 partitions)", "orders"]
    assert collapse_partitions(["orders"], {}) == ["orders"]


def test_best_recommendation_prefers_a_verified_result():
    unverified, verified = FakeRec(0.25), FakeRec(0.40)
    unverified.result_check, verified.result_check = "unchecked", "same"
    assert best_recommendation([unverified, verified], 85.0) is verified
    assert best_recommendation([unverified], 85.0) is unverified