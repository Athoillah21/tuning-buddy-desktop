"""
Prompt templates for query optimization (moved from the monolith's gemini_client.py).
"""
from typing import Any, Dict

SYSTEM_PROMPT = "You are a PostgreSQL performance optimization expert. Always respond with valid JSON only."

HEALTH_CHECK_PROMPT = 'This is a connectivity check. Reply with only this JSON object and nothing else: {"status": "ok"}'

# Prompt template for query optimization
OPTIMIZATION_PROMPT = """You are a PostgreSQL performance expert. Analyze the following SQL query and its execution plan, then provide exactly 3 different optimization recommendations.

## Original Query:
```sql
{query}
```

## Current Table Structure:
{table_info}

## Current Execution Plan:
```json
{plan}
```

## Execution Time: {execution_time}ms

## Current Issues Detected:
{issues}

---

Based on the table structure above (including EXISTING indexes), provide exactly 3 optimization recommendations with different approaches. Each recommendation should be practical and testable.

IMPORTANT: Review the existing indexes before suggesting new ones. Only suggest indexes that don't already exist.

CRITICAL INDEX RULES (MUST FOLLOW):
1. **ABSOLUTELY NEVER** use INCLUDE clause - it causes "index row size exceeds maximum" errors
2. **ONLY CREATE INDEX statements** in suggested_indexes - NO ALTER TABLE, NO CREATE TABLE
3. Do NOT include large columns (JSON, JSONB, TEXT, BYTEA, or any column with large data) in indexes
4. Use expression indexes for extracting values from JSON: CREATE INDEX ... ON table((json_extract_path_text(col, 'key')))
5. If you need covering index, use composite index on extracted values only, NOT INCLUDE

WRONG (will fail):
- CREATE INDEX ... INCLUDE (fullobject) -- INCLUDE causes row size error
- CREATE INDEX ... INCLUDE (any_text_column) -- INCLUDE with large data fails
- ALTER TABLE ... -- Not allowed in suggested_indexes

CORRECT:
- CREATE INDEX ... ON table(objecttypes_id, (json_extract_path_text(fullobject, 'startTime')))

Return your response as a valid JSON array with exactly 3 objects. Each object must have:
- "type": one of "index", "rewrite", or "config"
- "description": what this optimization does and why it helps, at most 2 sentences
- "optimized_query": the rewritten query (can be same as original if only index changes)
- "suggested_indexes": array of CREATE INDEX statements ONLY (no ALTER TABLE, no INCLUDE clause)
- "expected_improvement": "high", "medium", or "low"
- "explanation": the technical reason it helps, at most 2 sentences


KEEP EVERY TEXT FIELD SHORT: at most 2 sentences and under 300 characters each.
A long reply is cut off by the token limit and becomes unusable JSON.

IMPORTANT: Return ONLY the JSON array, no additional text or markdown formatting.
"""

# Follow-up prompt for fixing seq scans and improving performance
SEQ_SCAN_FIX_PROMPT = """You are a PostgreSQL performance expert. The previous optimization recommendation did NOT meet our goals. We need:
1. Eliminate Sequential Scans (use Index Scans instead)
2. Achieve at least 50% performance improvement

## Original Query:
```sql
{query}
```

## Current Table Structure (with indexes created so far):
{current_table_info}

## Previous Failed Recommendation:
- Type: {prev_type}
- Description: {prev_description}
- Optimized Query: {prev_query}
- Suggested Indexes: {prev_indexes}

## Execution Plan (still needs improvement):
```json
{plan}
```

---

The previous recommendation did NOT achieve our goals. Review the CURRENT TABLE STRUCTURE above to see what indexes already exist.

Provide a BETTER recommendation that will:
1. Eliminate Sequential Scans (force Index Scan usage)
2. Achieve 50%+ performance improvement

Consider:
1. The index might need different columns or column order
2. The query might need restructuring (avoid functions on indexed columns, fix data types)
3. PostgreSQL might need hints via query restructuring (LIMIT, subqueries, CTEs)
4. Statistics might be stale (suggest ANALYZE on the table)
5. The existing indexes might not match the WHERE clause columns exactly

CRITICAL INDEX RULES (MUST FOLLOW):
1. **ABSOLUTELY NEVER** use INCLUDE clause - it causes "index row size exceeds maximum" errors
2. **ONLY CREATE INDEX statements** in suggested_indexes - NO ALTER TABLE, NO CREATE TABLE
3. Do NOT include large columns (JSON, JSONB, TEXT, BYTEA) in indexes - they will fail
4. Use expression indexes: CREATE INDEX ... ON table((json_extract_path_text(col, 'key')))

WRONG (will fail):
- INCLUDE (fullobject) or INCLUDE (any_column) -- All INCLUDE clauses fail
- ALTER TABLE ... -- Not allowed

CORRECT:
- CREATE INDEX idx ON table(objecttypes_id, (json_extract_path_text(fullobject, 'startTime')))

Return your response as a valid JSON object with:
- "type": one of "index", "rewrite", or "config"
- "description": what this NEW optimization does, at most 2 sentences
- "optimized_query": the rewritten query
- "suggested_indexes": array of CREATE INDEX statements ONLY (no ALTER TABLE, no INCLUDE clause)
- "expected_improvement": "high", "medium", or "low"
- "explanation": why this approach eliminates the Sequential Scan, at most 2 sentences
- "seq_scan_fix_reason": explain what was wrong with the previous approach


KEEP EVERY TEXT FIELD SHORT: at most 2 sentences and under 300 characters each.
A long reply is cut off by the token limit and becomes unusable JSON.

IMPORTANT: Return ONLY the JSON object, no additional text or markdown formatting.
"""


def format_table_info(table_info: Dict[str, Any]) -> str:
    """Format table info dictionary into a readable string for AI prompts."""
    if not table_info:
        return "No table information available"

    lines = []
    for table_name, info in table_info.items():
        if isinstance(info, dict) and 'error' not in info:
            lines.append(f"\n### Table: {table_name}")

            # Row count
            row_count = info.get('row_count') or 0
            lines.append(f"- Approximate rows: {row_count:,}")

            # Columns
            columns = info.get('columns', [])
            if columns:
                lines.append("- Columns:")
                for col in columns:
                    nullable = "NULL" if col.get('nullable') == 'YES' else "NOT NULL"
                    lines.append(f"  - {col['name']} ({col['type']}, {nullable})")

            # Existing indexes
            indexes = info.get('indexes', [])
            if indexes:
                lines.append("- Existing Indexes:")
                for idx in indexes:
                    lines.append(f"  - {idx['name']}: {idx['definition']}")
            else:
                lines.append("- Existing Indexes: None")

    return '\n'.join(lines) if lines else "No table information available"
