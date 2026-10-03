import pytest

from app.parsing import AIParseError, parse_json_payload, parse_recommendations, shorten_error


def test_parses_fenced_json():
    assert parse_json_payload('```json\n{"status": "ok"}\n```') == {"status": "ok"}


def test_parses_json_wrapped_in_prose():
    assert parse_json_payload('Here you go:\n[{"type": "index"}]\nHope it helps') == [{"type": "index"}]


def test_rejects_non_json():
    with pytest.raises(AIParseError):
        parse_json_payload("Sure, everything is fine!")


def test_recommendations_are_normalized_and_capped_at_three():
    text = '[{"type": "index"}, {"type": "rewrite"}, {"type": "config"}, {"type": "index"}]'
    recs = parse_recommendations(text, "SELECT 1")
    assert [r["rank"] for r in recs] == [1, 2, 3]
    assert recs[0]["optimized_query"] == "SELECT 1"
    assert recs[0]["suggested_indexes"] == []


def test_shorten_error_summarizes_quota_errors():
    long_error = "429 RESOURCE_EXHAUSTED " + "x" * 200 + " Please retry in 12.5s"
    assert shorten_error(long_error) == "429 Quota exceeded, retry in 12.5s"


def test_salvages_a_reply_cut_off_by_the_token_limit():
    """
    DeepSeek hit max_tokens mid-sentence and the whole (correct) answer was discarded.
    Keep the objects that completed.
    """
    truncated = (
        '[\n'
        '  {"type": "index", "description": "Add an index on customer_email"},\n'
        '  {"type": "rewrite", "description": "Select only the needed columns"},\n'
        '  {"type": "index", "description": "Consider a composite index on (id, customer_id'
    )
    parsed = parse_json_payload(truncated)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0]["description"] == "Add an index on customer_email"


def test_salvage_keeps_strings_containing_braces_and_quotes():
    truncated = (
        '[{"type": "index", "description": "Use json_extract_path_text(col, \\"key\\") {not} a literal"},'
        ' {"type": "rewrite", "descr'
    )
    parsed = parse_json_payload(truncated)
    assert len(parsed) == 1
    assert "{not}" in parsed[0]["description"]
