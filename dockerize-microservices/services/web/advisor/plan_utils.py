"""
Reading an EXPLAIN (ANALYZE) plan for the results page.

Mirrors the report service's plan walk (report/app/pdf_generator.py: collect_plan_nodes,
find_bottleneck): a node's self time is its total time minus its children's, and time
is attributed to a table through the scan nodes that read it.
"""
from typing import Any, Dict, Optional


def _root(plan_data) -> Optional[Dict[str, Any]]:
    if isinstance(plan_data, list) and plan_data:
        plan_data = plan_data[0]
    if not isinstance(plan_data, dict):
        return None
    root = plan_data.get('Plan', plan_data)
    return root if isinstance(root, dict) else None


def _total_time(node: Dict[str, Any]) -> Optional[float]:
    """Actual Total Time is per loop, so multiply it out."""
    total = node.get('Actual Total Time')
    if not isinstance(total, (int, float)):
        return None
    loops = node.get('Actual Loops')
    return total * (loops if isinstance(loops, (int, float)) and loops > 0 else 1)


def table_time(plan_data) -> Dict[str, Dict[str, float]]:
    """
    {table: {'ms': time spent scanning it, 'share': fraction of the plan's time}}.
    Tables are keyed by lower-cased relation name, as the plan reports them.
    """
    root = _root(plan_data)
    if root is None:
        return {}
    per_table: Dict[str, float] = {}
    plan_total = {'ms': 0.0}

    def walk(node: Dict[str, Any]) -> Optional[float]:
        children = [child for child in node.get('Plans') or [] if isinstance(child, dict)]
        child_time = sum(walk(child) or 0 for child in children)
        total = _total_time(node)
        if total is None:
            return None
        self_time = max(total - child_time, 0)
        plan_total['ms'] += self_time
        relation = node.get('Relation Name')
        if relation:
            per_table[relation.lower()] = per_table.get(relation.lower(), 0) + self_time
        return total

    walk(root)
    if not plan_total['ms']:
        return {}
    return {table: {'ms': ms, 'share': ms / plan_total['ms']} for table, ms in per_table.items()}


def bottleneck_table(plan_data) -> Optional[str]:
    """The table whose scans take the most time, or None when the plan has no timings."""
    times = table_time(plan_data)
    if not times:
        return None
    return max(times.items(), key=lambda item: item[1]['ms'])[0]
