"""
Parsing of AI responses into recommendation dicts.
"""
import json
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class AIParseError(ValueError):
    """Raised when an AI response is not the JSON we asked for."""
    pass


def shorten_error(error_str: str) -> str:
    """Shorten verbose API errors (like Gemini quota errors)."""
    if len(error_str) <= 150:
        return error_str
    if 'RESOURCE_EXHAUSTED' in error_str or '429' in error_str:
        retry_match = re.search(r'retry in (\d+\.?\d*)s', error_str, re.IGNORECASE)
        retry_info = f", retry in {retry_match.group(1)}s" if retry_match else ""
        return f"429 Quota exceeded{retry_info}"
    return error_str[:150] + "..."


def strip_code_fences(text: str) -> str:
    """Remove markdown code blocks if present."""
    text = (text or "").strip()
    if text.startswith('```'):
        lines = text.split('\n')
        text = '\n'.join(lines[1:-1] if lines[-1].strip().startswith('```') else lines[1:])
    return text.strip()


def _salvage_truncated_array(text: str):
    """
    A reply cut off by the token limit never closes its JSON. Keep the objects that did
    complete instead of throwing away the whole answer - two good recommendations beat none.
    """
    start = text.find('[')
    if start == -1:
        return None

    objects, depth, object_start = [], 0, None
    in_string = escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == chr(92):   # backslash escape inside a JSON string
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            if depth == 0:
                object_start = index
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0 and object_start is not None:
                try:
                    objects.append(json.loads(text[object_start:index + 1]))
                except json.JSONDecodeError:
                    pass
                object_start = None
    return objects or None


def parse_json_payload(text: str) -> Any:
    """Parse JSON from a model response, tolerating code fences and surrounding prose."""
    cleaned = strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Some models wrap the JSON in prose or reasoning text; fall back to the outermost array/object
    starts = [i for i in (cleaned.find('['), cleaned.find('{')) if i != -1]
    if starts:
        start = min(starts)
        end = cleaned.rfind(']' if cleaned[start] == '[' else '}')
        if end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass

    salvaged = _salvage_truncated_array(cleaned)
    if salvaged is not None:
        logger.warning(f"Recovered {len(salvaged)} complete object(s) from a truncated response")
        return salvaged

    logger.error(f"Failed to parse response: {cleaned[:500]}")
    raise AIParseError(f"Failed to parse AI response as JSON: {cleaned[:120]!r}")


def parse_recommendations(response_text: str, original_query: str) -> List[Dict[str, Any]]:
    """Parse AI response into recommendations."""
    recommendations = parse_json_payload(response_text)

    if isinstance(recommendations, dict) and isinstance(recommendations.get('recommendations'), list):
        recommendations = recommendations['recommendations']
    if not isinstance(recommendations, list):
        raise AIParseError("Expected a list of recommendations")

    # Validate and normalize
    validated = []
    for i, rec in enumerate(r for r in recommendations if isinstance(r, dict)):
        if i >= 3:
            break
        validated.append({
            'type': rec.get('type', 'rewrite'),
            'description': rec.get('description', 'No description provided'),
            'optimized_query': rec.get('optimized_query', original_query),
            'suggested_indexes': rec.get('suggested_indexes', []),
            'expected_improvement': rec.get('expected_improvement', 'medium'),
            'explanation': rec.get('explanation', ''),
            'rank': i + 1,
        })

    if not validated:
        raise AIParseError("AI response contained no recommendations")
    return validated


def parse_single_recommendation(response_text: str, original_query: str) -> Dict[str, Any]:
    """Parse AI response for single recommendation (seq scan fix)."""
    recommendation = parse_json_payload(response_text)

    if isinstance(recommendation, list) and recommendation and isinstance(recommendation[0], dict):
        recommendation = recommendation[0]
    if not isinstance(recommendation, dict):
        raise AIParseError("Expected a single recommendation object")

    return {
        'type': recommendation.get('type', 'index'),
        'description': recommendation.get('description', 'No description provided'),
        'optimized_query': recommendation.get('optimized_query', original_query),
        'suggested_indexes': recommendation.get('suggested_indexes', []),
        'expected_improvement': recommendation.get('expected_improvement', 'high'),
        'explanation': recommendation.get('explanation', ''),
        'seq_scan_fix_reason': recommendation.get('seq_scan_fix_reason', ''),
    }
