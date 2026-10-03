"""
PDF Report Generator for Query Optimization Results.

Design: a printed consulting report for DBAs - white page, navy ink, one accent blue,
hairline rules, uppercase micro-labels and a source note under every figure.
"""
import io
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    KeepTogether, HRFlowable, PageBreak
)

MAX_PLAN_LINES = 40
# Recommendation pages already carry the diagram, so their tree is trimmed to keep
# one option to one page
MAX_RECOMMENDATION_PLAN_LINES = 14
MAX_FLOWCHART_NODES = 12
# A node is only called out when it owns this share of the plan's measured time
BOTTLENECK_MIN_SHARE = 0.4

# Flowchart geometry
NODE_HEIGHT = 42
NODE_V_GAP = 26
NODE_MAX_WIDTH = 150


# ----------------------------------------------------------------------
# Palette - navy ink on white, with the report's existing blue as the accent
# ----------------------------------------------------------------------

class Palette:
    INK = colors.HexColor('#0B2545')        # headings, strong text
    BODY = colors.HexColor('#1F2A37')       # body copy
    MUTED = colors.HexColor('#64748B')      # labels, captions
    ACCENT = colors.HexColor('#3B82F6')     # the report's core blue
    ACCENT_DEEP = colors.HexColor('#1D4ED8')  # emphasis, chart bars
    SUCCESS = colors.HexColor('#15803D')    # print-grade green
    DANGER = colors.HexColor('#B42318')     # print-grade red
    WARNING = colors.HexColor('#B54708')    # print-grade amber
    RULE = colors.HexColor('#D7DEE8')       # hairlines
    SOFT = colors.HexColor('#F5F8FC')       # table tint, code background
    WHITE = colors.white


def escape_markup(value: Any) -> str:
    """
    ReportLab parses paragraph text as markup, so anything that reaches it must be escaped.
    Descriptions come from an AI provider and regularly contain '<' and '&'.
    """
    return (
        str(value if value is not None else "")
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


def code_markup(value: Any) -> str:
    """Escape text and keep its line breaks and indentation inside a code block."""
    return escape_markup(value).replace('\n', '<br/>').replace('  ', '&nbsp;&nbsp;')


def format_ms(value: Any) -> str:
    return f"{value:,.2f} ms" if isinstance(value, (int, float)) else "N/A"


def format_number(value: Any) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "N/A"


def format_bytes(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "N/A"
    size = float(value)
    for unit in ('bytes', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f"{size:.0f} {unit}" if unit == 'bytes' else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_rows(value: Any) -> str:
    """Planner estimates: 1,000,000 -> '1.0M', no false precision."""
    if not isinstance(value, (int, float)):
        return "N/A"
    for limit, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return f"{value:.0f}"


def table_time_shares(plan_data) -> Dict[str, float]:
    """
    Share of the plan's measured time spent in each table's scan nodes, keyed by the
    lower-cased relation name. Self time = a node's total time minus its children's.
    """
    _, root = _plan_and_root(plan_data)
    if not root:
        return {}
    per_table: Dict[str, float] = {}
    plan_total = [0.0]

    def walk(node: Dict[str, Any]) -> Optional[float]:
        children = [child for child in node.get('Plans') or [] if isinstance(child, dict)]
        child_time = sum(walk(child) or 0 for child in children)
        total = _node_total_time(node)
        if total is None:
            return None
        self_time = max(total - child_time, 0)
        plan_total[0] += self_time
        relation = node.get('Relation Name')
        if relation:
            per_table[relation.lower()] = per_table.get(relation.lower(), 0) + self_time
        return total

    walk(root)
    if not plan_total[0]:
        return {}
    return {table: ms / plan_total[0] for table, ms in per_table.items()}


def _relations_of(label: str, info: Dict[str, Any]) -> set:
    """A table's own name plus its partitions': the plan names partitions, not their parent."""
    relations = {(info.get('name') or label.split('.')[-1]).lower()}
    relations.update((part.get('name') or '').lower() for part in info.get('partitions') or [])
    return relations


def table_rows(table_stats: Dict[str, Any], plan_data) -> List[Dict[str, Any]]:
    """The recorded tables with their time share, bottleneck (most scan time) first."""
    shares = table_time_shares(plan_data)
    rows = []
    for label, info in (table_stats or {}).items():
        if not isinstance(info, dict) or info.get('error'):
            continue
        timed = [shares[relation] for relation in _relations_of(label, info) if relation in shares]
        rows.append({**info, 'label': info.get('name') or label, 'share': sum(timed) if timed else None,
                     'is_bottleneck': False})
    timed_rows = [row for row in rows if row['share']]
    if timed_rows:
        max(timed_rows, key=lambda row: row['share'])['is_bottleneck'] = True
    rows.sort(key=lambda row: (not row['is_bottleneck'], -(row.get('total_bytes') or 0)))
    return rows


def collapse_partitions(relations: List[str], table_stats: Dict[str, Any]) -> List[str]:
    """['m_2026_01', 'm_2026_02', 'orders'] -> ['measurements (2 of 12 partitions)', 'orders']."""
    parent_of, sizes = {}, {}
    for label, info in (table_stats or {}).items():
        if isinstance(info, dict) and info.get('partitions'):
            name = info.get('name') or label
            sizes[name] = len(info['partitions'])
            for part in info['partitions']:
                parent_of[(part.get('name') or '').lower()] = name
    described, counts = [], {}
    for relation in relations:
        parent = parent_of.get(relation.lower())
        if parent is None:
            described.append(relation)
        else:
            if parent not in counts:
                described.append(parent)
            counts[parent] = counts.get(parent, 0) + 1
    return [f"{name} ({counts[name]} of {sizes[name]} partitions)" if name in counts else name
            for name in described]


def _plan_and_root(plan_data):
    """EXPLAIN output arrives either as the plan wrapper, a list of them, or a bare node."""
    if isinstance(plan_data, list) and plan_data:
        plan_data = plan_data[0]
    if not isinstance(plan_data, dict):
        return None, None
    root = plan_data.get('Plan', plan_data)
    return plan_data, root if isinstance(root, dict) else {}


def plan_metrics(plan_data) -> Dict[str, Any]:
    """Pull the headline numbers out of an execution plan."""
    plan, root = _plan_and_root(plan_data)
    if plan is None:
        return {}
    return {
        'execution_time': plan.get('Execution Time'),
        'planning_time': plan.get('Planning Time'),
        'total_cost': root.get('Total Cost'),
        'actual_rows': root.get('Actual Rows', root.get('Plan Rows')),
        'node_type': root.get('Node Type'),
    }


def format_plan_tree(plan_data, max_lines: int = MAX_PLAN_LINES) -> str:
    """Render an execution plan as an indented node tree."""
    _, root = _plan_and_root(plan_data)
    if not root:
        return "Execution plan data not available."

    lines: List[str] = []
    truncated = False

    def walk(node: Dict[str, Any], depth: int):
        nonlocal truncated
        if not isinstance(node, dict):
            return
        if len(lines) >= max_lines:
            truncated = True
            return

        parts = [node.get('Node Type', 'Unknown')]
        relation = node.get('Relation Name')
        index_name = node.get('Index Name')
        if relation:
            parts.append(f"on {relation}")
        if index_name:
            parts.append(f"using {index_name}")

        details = []
        rows = node.get('Actual Rows')
        estimated = node.get('Plan Rows')
        if rows is not None:
            detail = f"rows={format_number(rows)}"
            if estimated is not None:
                detail += f" (est {format_number(estimated)})"
            details.append(detail)
        if node.get('Total Cost') is not None:
            details.append(f"cost={node['Total Cost']:,.1f}")
        if node.get('Actual Total Time') is not None:
            details.append(f"time={node['Actual Total Time']:,.2f}ms")
        loops = node.get('Actual Loops')
        if isinstance(loops, (int, float)) and loops > 1:
            details.append(f"loops={format_number(loops)}")

        line = "  " * depth + "-> " + " ".join(parts)
        if details:
            line += "  (" + ", ".join(details) + ")"
        lines.append(line)

        condition = (node.get('Index Cond') or node.get('Filter')
                     or node.get('Hash Cond') or node.get('Recheck Cond'))
        if condition and len(lines) < max_lines:
            lines.append("  " * depth + "     " + str(condition)[:110])

        for child in node.get('Plans', []) or []:
            walk(child, depth + 1)

    walk(root, 0)
    if truncated:
        lines.append("... (plan truncated; the full plan is stored with the analysis)")
    return "\n".join(lines)


SIGNIFICANT = 'faster'


def best_recommendation(recommendations: List, original_time: Optional[float]):
    """
    The fastest option that beat the original by more than the measurement noise.

    An option whose verdict is 'within_noise' or 'already_fast' is not a win, however
    flattering its percentage: recommending it would tell the reader to add indexes
    that buy nothing. Rows recorded before verdicts existed fall back to the old rule.
    """
    candidates = [
        rec for rec in recommendations
        if getattr(rec, 'tested_execution_time', None) is not None
        and original_time
        and rec.tested_execution_time < original_time
        and (getattr(rec, 'verdict', '') or SIGNIFICANT) == SIGNIFICANT
        # A rewrite that answers differently is not an optimization, however fast
        and getattr(rec, 'result_check', '') != 'different'
    ]
    if not candidates:
        return None
    # A result proven to match beats one that could not be compared
    verified = [rec for rec in candidates if getattr(rec, 'result_check', '') != 'unchecked']
    return min(verified or candidates, key=lambda rec: rec.tested_execution_time)


def verdict_sentence(rec, original_time: Optional[float]) -> str:
    """Plain wording for what the measurement actually showed."""
    verdict = getattr(rec, 'verdict', '') or ''
    tested = getattr(rec, 'tested_execution_time', None)
    spread = getattr(rec, 'measurement_spread_ms', None)

    if tested is None:
        return "Not tested"
    if verdict == 'already_fast':
        return ("The query is already fast enough that the difference is cache noise, "
                "not optimization")
    if verdict == 'within_noise':
        margin = f" (repeats varied by {spread:.2f} ms)" if isinstance(spread, (int, float)) else ""
        return f"No measurable difference{margin}"
    if original_time:
        change = ((original_time - tested) / original_time) * 100
        if verdict == 'slower' or change < 0:
            return f"{abs(change):.1f}% slower than the current query"
        return f"{change:.1f}% faster than the current query"
    return "Measured, no baseline to compare against"


# ----------------------------------------------------------------------
# Plan flowchart
# ----------------------------------------------------------------------

SEQ_SCAN_TYPES = ('Seq Scan', 'Parallel Seq Scan')
INDEX_SCAN_TYPES = ('Index Scan', 'Index Only Scan', 'Bitmap Index Scan', 'Bitmap Heap Scan')
JOIN_TYPES = ('Nested Loop', 'Hash Join', 'Merge Join', 'Hash', 'Aggregate', 'GroupAggregate',
              'HashAggregate', 'Sort', 'Gather', 'Gather Merge', 'Limit', 'Materialize')


def _node_total_time(node: Dict[str, Any]) -> Optional[float]:
    """Actual Total Time is per loop, so multiply it out."""
    total = node.get('Actual Total Time')
    if not isinstance(total, (int, float)):
        return None
    loops = node.get('Actual Loops')
    return total * (loops if isinstance(loops, (int, float)) and loops > 0 else 1)


def collect_plan_nodes(plan_data, max_nodes: int = MAX_FLOWCHART_NODES):
    """
    Flatten a plan into positioned entries for drawing: depth, column
    (leaves get their own column, parents sit above the middle of theirs) and self time.
    """
    _, root = _plan_and_root(plan_data)
    if not root:
        return [], 0, False

    entries: List[Dict[str, Any]] = []
    state = {'leaves': 0, 'count': 0, 'truncated': False}

    def walk(node: Dict[str, Any], depth: int):
        if state['count'] >= max_nodes:
            state['truncated'] = True
            return None
        state['count'] += 1

        children = [child for child in (node.get('Plans') or []) if isinstance(child, dict)]
        child_entries = [entry for entry in (walk(child, depth + 1) for child in children) if entry]

        if child_entries:
            column = sum(child['column'] for child in child_entries) / len(child_entries)
        else:
            column = state['leaves']
            state['leaves'] += 1

        total_time = _node_total_time(node)
        child_time = sum(child['total_time'] or 0 for child in child_entries)
        self_time = None if total_time is None else max(total_time - child_time, 0)

        entry = {
            'node': node,
            'depth': depth,
            'column': column,
            'children': child_entries,
            'total_time': total_time,
            'self_time': self_time,
        }
        entries.append(entry)
        return entry

    walk(root, 0)
    return entries, max(state['leaves'], 1), state['truncated']


def find_bottleneck(entries: List[Dict[str, Any]], min_share: float = BOTTLENECK_MIN_SHARE):
    """
    The node worth calling out: it must own at least `min_share` of the measured time.
    On a well-optimized plan nothing dominates, so nothing is marked.
    """
    timed = [entry for entry in entries if entry['self_time'] is not None]
    if not timed:
        return None
    total = sum(entry['self_time'] for entry in timed)
    if not total:
        return None
    candidate = max(timed, key=lambda entry: entry['self_time'])
    if candidate['self_time'] / total < min_share:
        return None
    return candidate


def _node_accent(node: Dict[str, Any]) -> colors.Color:
    node_type = node.get('Node Type', '')
    if node_type in SEQ_SCAN_TYPES:
        return Palette.DANGER
    if node_type in INDEX_SCAN_TYPES:
        return Palette.SUCCESS
    if node_type in JOIN_TYPES:
        return Palette.ACCENT
    return Palette.MUTED


def _truncate(text: str, width: float, font_size: float) -> str:
    """Rough character budget for a box of this width."""
    max_chars = max(int(width / (font_size * 0.52)), 4)
    text = str(text)
    return text if len(text) <= max_chars else text[:max_chars - 1] + "."


def create_plan_flowchart(plan_data, width: float, max_nodes: int = MAX_FLOWCHART_NODES,
                          highlight_bottleneck: bool = True) -> Optional[Drawing]:
    """
    Draw the execution plan as a flowchart: one box per node, arrows pointing the way
    data flows (children run first). The dominant node is called out only when
    `highlight_bottleneck` is set and it really does dominate the runtime.
    """
    entries, columns, truncated = collect_plan_nodes(plan_data, max_nodes)
    if not entries:
        return None

    depths = max(entry['depth'] for entry in entries) + 1
    max_depth = depths - 1
    legend_height = 16
    top_margin = legend_height + 18  # legend, plus room for a callout label above the first row
    height = depths * NODE_HEIGHT + (depths - 1) * NODE_V_GAP + top_margin + 6
    drawing = Drawing(width, height)

    column_width = width / columns
    box_width = min(column_width - 12, NODE_MAX_WIDTH)

    # Only a dominant sequential scan earns the word "bottleneck". Any other dominant node
    # is simply the costliest step, so it gets a neutral label instead of an alarm.
    dominant = find_bottleneck(entries) if highlight_bottleneck else None
    bottleneck = dominant if dominant and dominant['node'].get('Node Type') in SEQ_SCAN_TYPES else None
    hotspot = dominant if dominant is not None and bottleneck is None else None

    def box_position(entry):
        # Read top to bottom: the scans that run first sit at the top, the final step at the bottom
        row = max_depth - entry['depth']
        center_x = (entry['column'] + 0.5) * column_width
        y = height - top_margin - (row + 1) * NODE_HEIGHT - row * NODE_V_GAP
        return center_x - box_width / 2, y, center_x

    # Connectors first so the boxes paint over the line ends
    for entry in entries:
        _, parent_y, parent_center = box_position(entry)
        parent_top = parent_y + NODE_HEIGHT
        for child in entry['children']:
            _, child_y, child_center = box_position(child)
            mid_y = child_y - NODE_V_GAP / 2
            for line in (
                Line(child_center, child_y, child_center, mid_y),
                Line(child_center, mid_y, parent_center, mid_y),
                Line(parent_center, mid_y, parent_center, parent_top),
            ):
                line.strokeColor = Palette.RULE
                line.strokeWidth = 0.9
                drawing.add(line)
            # Arrow head pointing down into the step that consumes these rows
            drawing.add(Polygon([parent_center - 3, parent_top + 5, parent_center + 3, parent_top + 5,
                                 parent_center, parent_top],
                                fillColor=Palette.RULE, strokeColor=Palette.RULE))

    for entry in entries:
        node = entry['node']
        x, y, center_x = box_position(entry)
        accent = _node_accent(node)
        is_bottleneck = bottleneck is not None and entry is bottleneck
        is_hotspot = hotspot is not None and entry is hotspot

        # White card, hairline border, a coloured rule along the top edge carries the meaning
        drawing.add(Rect(x, y, box_width, NODE_HEIGHT, rx=2, ry=2,
                         fillColor=Palette.WHITE, strokeColor=Palette.RULE, strokeWidth=0.8))
        drawing.add(Rect(x, y + NODE_HEIGHT - 2.5, box_width, 2.5,
                         fillColor=accent, strokeColor=accent, strokeWidth=0))
        if is_bottleneck:
            drawing.add(Rect(x, y, box_width, NODE_HEIGHT, rx=2, ry=2,
                             fillColor=None, strokeColor=Palette.DANGER, strokeWidth=1.6))

        node_type = node.get('Node Type', 'Node')
        drawing.add(String(center_x, y + NODE_HEIGHT - 14, _truncate(node_type, box_width - 10, 7.5),
                           fontName='Helvetica-Bold', fontSize=7.5, fillColor=Palette.INK,
                           textAnchor='middle'))

        subject = node.get('Relation Name') or node.get('Index Name') or ''
        if subject:
            drawing.add(String(center_x, y + NODE_HEIGHT - 24, _truncate(subject, box_width - 10, 6.5),
                               fontName='Helvetica', fontSize=6.5, fillColor=Palette.MUTED,
                               textAnchor='middle'))

        stats = []
        if node.get('Actual Rows') is not None:
            stats.append(f"{format_number(node['Actual Rows'])} rows")
        if entry['total_time'] is not None:
            stats.append(f"{entry['total_time']:,.2f} ms")
        if stats:
            drawing.add(String(center_x, y + 7, _truncate(" | ".join(stats), box_width - 10, 6.5),
                               fontName='Helvetica', fontSize=6.5,
                               fillColor=Palette.DANGER if is_bottleneck else Palette.MUTED,
                               textAnchor='middle'))

        if is_bottleneck:
            drawing.add(String(x + box_width, y + NODE_HEIGHT + 4, "BOTTLENECK",
                               fontName='Helvetica-Bold', fontSize=5.8, fillColor=Palette.DANGER,
                               textAnchor='end'))
        elif is_hotspot:
            drawing.add(String(x + box_width, y + NODE_HEIGHT + 4, "HIGHEST COST",
                               fontName='Helvetica', fontSize=5.8, fillColor=Palette.MUTED,
                               textAnchor='end'))

    # Legend: only the categories actually present
    present = []
    for label, color in (("Sequential scan", Palette.DANGER), ("Index scan", Palette.SUCCESS),
                         ("Join / aggregate", Palette.ACCENT), ("Other", Palette.MUTED)):
        if any(_node_accent(entry['node']) is color for entry in entries):
            present.append((label, color))

    legend_y = height - 9
    legend_x = 0
    for label, color in present:
        drawing.add(Rect(legend_x, legend_y - 4, 8, 3, fillColor=color, strokeColor=color, strokeWidth=0))
        drawing.add(String(legend_x + 12, legend_y - 4, label.upper(), fontName='Helvetica',
                           fontSize=5.8, fillColor=Palette.MUTED))
        legend_x += 26 + len(label) * 3.2
    if truncated:
        drawing.add(String(width, legend_y - 4, f"SHOWING FIRST {max_nodes} NODES",
                           fontName='Helvetica', fontSize=5.8, fillColor=Palette.MUTED, textAnchor='end'))

    return drawing


class NumberedCanvas(pdfcanvas.Canvas):
    """Adds the running head and 'Page X of Y' footer used throughout the report."""

    def __init__(self, *args, **kwargs):
        self.footer_text = kwargs.pop('footer_text', "Tuning Buddy")
        self.running_head = kwargs.pop('running_head', "")
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_furniture(total_pages)
            super().showPage()
        super().save()

    def _draw_furniture(self, total_pages: int):
        width, height = A4
        page = self.getPageNumber()
        self.saveState()

        # Running head on continuation pages only
        if page > 1 and self.running_head:
            self.setFont('Helvetica', 7.5)
            self.setFillColor(Palette.MUTED)
            self.drawString(2 * cm, height - 1.25 * cm, self.running_head.upper())
            self.setStrokeColor(Palette.RULE)
            self.setLineWidth(0.5)
            self.line(2 * cm, height - 1.45 * cm, width - 2 * cm, height - 1.45 * cm)

        self.setStrokeColor(Palette.RULE)
        self.setLineWidth(0.5)
        self.line(2 * cm, 1.5 * cm, width - 2 * cm, 1.5 * cm)
        self.setFont('Helvetica', 7.5)
        self.setFillColor(Palette.MUTED)
        self.drawString(2 * cm, 1.05 * cm, self.footer_text)
        self.drawRightString(width - 2 * cm, 1.05 * cm, f"{page} / {total_pages}")
        self.restoreState()


class PDFReportGenerator:
    """Generates PDF reports for query optimization results."""

    # Palette kept as class attributes so callers and tests can reach them
    INK = Palette.INK
    BODY = Palette.BODY
    MUTED = Palette.MUTED
    ACCENT = Palette.ACCENT
    ACCENT_DEEP = Palette.ACCENT_DEEP
    SUCCESS = Palette.SUCCESS
    DANGER = Palette.DANGER
    WARNING = Palette.WARNING
    RULE = Palette.RULE
    SOFT = Palette.SOFT

    SEQ_SCAN_TYPES = list(SEQ_SCAN_TYPES)
    INDEX_SCAN_TYPES = list(INDEX_SCAN_TYPES)

    @staticmethod
    def _extract_scan_types(plan_data) -> dict:
        """Extract scan types from an execution plan."""
        result = {
            'has_seq_scan': False,
            'has_index_scan': False,
            'scan_nodes': [],
            'seq_scan_tables': [],
            'index_scan_tables': []
        }

        def traverse_plan(node):
            if isinstance(node, dict):
                node_type = node.get('Node Type', '')
                table_name = node.get('Relation Name', node.get('Alias', ''))

                if node_type in PDFReportGenerator.SEQ_SCAN_TYPES:
                    result['has_seq_scan'] = True
                    result['scan_nodes'].append(f"{node_type}: {table_name}")
                    if table_name:
                        result['seq_scan_tables'].append(table_name)
                elif node_type in PDFReportGenerator.INDEX_SCAN_TYPES:
                    result['has_index_scan'] = True
                    index_name = node.get('Index Name', '')
                    result['scan_nodes'].append(f"{node_type}: {table_name} ({index_name})")
                    if table_name:
                        result['index_scan_tables'].append(table_name)

                for child in node.get('Plans', []):
                    traverse_plan(child)
                if 'Plan' in node:
                    traverse_plan(node['Plan'])

        if isinstance(plan_data, list) and plan_data:
            traverse_plan(plan_data[0])
        elif isinstance(plan_data, dict):
            traverse_plan(plan_data)

        return result

    def __init__(self):
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()
        self.width, self.height = A4
        self.content_width = self.width - 4*cm

    def _setup_custom_styles(self):
        """Typography: one family, a tight scale, generous leading."""
        add = self.styles.add

        add(ParagraphStyle(name='Eyebrow', fontName='Helvetica-Bold', fontSize=7.5, leading=10,
                           textColor=Palette.ACCENT_DEEP, spaceAfter=4, alignment=TA_LEFT))
        add(ParagraphStyle(name='ReportTitle', fontName='Helvetica-Bold', fontSize=21, leading=25,
                           textColor=Palette.INK, spaceAfter=6, alignment=TA_LEFT))
        add(ParagraphStyle(name='Subtitle', fontName='Helvetica', fontSize=9.5, leading=13,
                           textColor=Palette.MUTED, spaceAfter=10, alignment=TA_LEFT))
        add(ParagraphStyle(name='SectionHeader', fontName='Helvetica-Bold', fontSize=12.5, leading=16,
                           textColor=Palette.INK, spaceBefore=2, spaceAfter=2))
        add(ParagraphStyle(name='SubHeader', fontName='Helvetica-Bold', fontSize=8, leading=11,
                           textColor=Palette.MUTED, spaceBefore=8, spaceAfter=4))
        add(ParagraphStyle(name='CustomBody', fontName='Helvetica', fontSize=9.5, leading=14,
                           textColor=Palette.BODY))
        add(ParagraphStyle(name='Lead', fontName='Helvetica', fontSize=10.5, leading=15,
                           textColor=Palette.INK))
        add(ParagraphStyle(name='Small', fontName='Helvetica', fontSize=8, leading=11,
                           textColor=Palette.MUTED))
        add(ParagraphStyle(name='Caption', fontName='Helvetica', fontSize=7, leading=10,
                           textColor=Palette.MUTED, spaceBefore=4))
        add(ParagraphStyle(name='MetricLabel', fontName='Helvetica-Bold', fontSize=7, leading=9,
                           textColor=Palette.MUTED))
        add(ParagraphStyle(name='MetricValue', fontName='Helvetica-Bold', fontSize=16, leading=19,
                           textColor=Palette.INK))
        add(ParagraphStyle(name='CodeParagraph', fontName='Courier', fontSize=8.5, leading=11.5,
                           textColor=Palette.INK, wordWrap='CJK'))
        add(ParagraphStyle(name='CodeSmall', fontName='Courier', fontSize=7.2, leading=9.5,
                           textColor=Palette.INK, wordWrap='CJK'))

    # ------------------------------------------------------------------
    # Building blocks
    # ------------------------------------------------------------------

    def _section(self, title: str, eyebrow: str = "") -> List:
        """A section heading with its hairline rule - the report's main rhythm."""
        elements = []
        if eyebrow:
            elements.append(Paragraph(escape_markup(eyebrow).upper(), self.styles['Eyebrow']))
        elements.append(Paragraph(escape_markup(title), self.styles['SectionHeader']))
        elements.append(HRFlowable(width="100%", thickness=0.6, color=Palette.RULE,
                                   spaceBefore=3, spaceAfter=8))
        return elements

    def _source_note(self, text: str) -> Paragraph:
        return Paragraph(f"Source: {escape_markup(text)}", self.styles['Caption'])

    def _code_block(self, text: str, small: bool = False) -> Table:
        """Light code panel with an accent rule down the left edge."""
        style = 'CodeSmall' if small else 'CodeParagraph'
        size = 7.2 if small else 8.5
        paragraph = Paragraph(
            f"<font face='Courier' size='{size}'>{code_markup(text)}</font>", self.styles[style])
        table = Table([[paragraph]], colWidths=[self.content_width])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), Palette.SOFT),
            ('LINEBEFORE', (0, 0), (0, -1), 2, Palette.ACCENT),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        return table

    def _callout(self, rows: List, accent: colors.Color) -> Table:
        """A white callout with a heavy accent rule on the left."""
        panel = Table([[row] for row in rows], colWidths=[self.content_width])
        panel.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), Palette.WHITE),
            ('LINEBEFORE', (0, 0), (0, -1), 3, accent),
            ('LINEBELOW', (0, -1), (-1, -1), 0.6, Palette.RULE),
            ('LINEABOVE', (0, 0), (-1, 0), 0.6, Palette.RULE),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        return panel

    def _data_table(self, rows: List[List[str]], widths: List[float], numeric_from: int = 1) -> Table:
        """Consulting-style table: no grid, hairlines only, numerals right-aligned."""
        table = Table(rows, colWidths=widths)
        table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('TEXTCOLOR', (0, 0), (-1, 0), Palette.INK),
            ('BACKGROUND', (0, 0), (-1, 0), Palette.SOFT),
            ('FONTNAME', (0, 1), (0, -1), 'Helvetica-Bold'),
            ('TEXTCOLOR', (0, 1), (0, -1), Palette.MUTED),
            ('TEXTCOLOR', (1, 1), (-1, -1), Palette.BODY),
            ('ALIGN', (numeric_from, 0), (-1, -1), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LINEBELOW', (0, 0), (-1, -2), 0.4, Palette.RULE),
            ('LINEBELOW', (0, -1), (-1, -1), 0.8, Palette.RULE),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        return table

    def _analysis_timestamp(self, query_history) -> str:
        """When the analysis ran, in the app's timezone - not when the PDF was printed."""
        display = getattr(query_history, 'created_at_display', None)
        if display:
            return str(display)
        created_at = getattr(query_history, 'created_at', None)
        if isinstance(created_at, datetime):
            return created_at.strftime('%Y-%m-%d %H:%M')
        if created_at:
            try:
                return datetime.fromisoformat(str(created_at)).strftime('%Y-%m-%d %H:%M')
            except ValueError:
                return str(created_at)
        return "unknown"

    # ------------------------------------------------------------------
    # Document
    # ------------------------------------------------------------------

    def generate_report(self, query_history, recommendations: List) -> io.BytesIO:
        """Generate a PDF report for query analysis results."""
        recommendations = list(recommendations or [])
        buffer = io.BytesIO()
        report_id = getattr(query_history, 'id', None)
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=2*cm,
            leftMargin=2*cm,
            topMargin=2*cm,
            bottomMargin=2.2*cm,
            title=f"Query Optimization Report{f' #{report_id}' if report_id else ''}",
            author="Tuning Buddy",
        )

        orig_plan = query_history.original_plan
        original_time = query_history.original_execution_time
        scan_info = self._extract_scan_types(orig_plan) if orig_plan else {
            'has_seq_scan': False, 'has_index_scan': False, 'scan_nodes': [],
            'seq_scan_tables': [], 'index_scan_tables': [],
        }
        winner = best_recommendation(recommendations, original_time)

        # Page 1: what was run, what to do about it, and the numbers
        story = []
        story.extend(self._create_header(query_history))
        story.extend(self._create_metrics_section(query_history, recommendations))
        story.extend(self._create_query_section(query_history))
        story.extend(self._create_action_section(winner, original_time, recommendations))
        story.extend(self._create_chart_section(original_time, recommendations))

        # Page 2: the evidence
        table_stats = getattr(query_history, 'table_stats', None) or {}
        if orig_plan:
            story.append(PageBreak())
            story.extend(self._create_comparison_table(query_history, winner))
            story.extend(self._create_execution_plan_section(orig_plan, table_stats))
        story.extend(self._create_tables_section(table_stats, orig_plan))

        # One page per recommendation
        story.extend(self._create_recommendations_section(recommendations, original_time, scan_info))
        story.extend(self._create_footer(query_history))

        connection_name = getattr(getattr(query_history, 'connection', None), 'name', 'database')
        timestamp = self._analysis_timestamp(query_history)
        doc.build(
            story,
            canvasmaker=lambda *args, **kwargs: NumberedCanvas(
                *args,
                footer_text=f"Tuning Buddy  |  {connection_name}  |  {timestamp}",
                running_head=f"Query Optimization Report  |  {connection_name}",
                **kwargs,
            ),
        )
        buffer.seek(0)
        return buffer

    def _create_header(self, query_history) -> List:
        """Masthead: eyebrow, title, subtitle, accent rule."""
        elements = []
        elements.append(Paragraph("POSTGRESQL PERFORMANCE ANALYSIS", self.styles['Eyebrow']))
        elements.append(Paragraph("Query Optimization Report", self.styles['ReportTitle']))
        subtitle = (f"{escape_markup(query_history.connection.name)}&nbsp;&nbsp;|&nbsp;&nbsp;"
                    f"Analyzed {escape_markup(self._analysis_timestamp(query_history))}")
        elements.append(Paragraph(subtitle, self.styles['Subtitle']))
        elements.append(HRFlowable(width="100%", thickness=2.5, color=Palette.ACCENT_DEEP, spaceAfter=16))
        return elements

    def _create_metrics_section(self, query_history, recommendations) -> List:
        """Headline numbers, separated by hairlines rather than boxed in."""
        exec_time = query_history.original_execution_time or 0

        best_improvement = 0
        best_time = None
        for rec in recommendations:
            tested = getattr(rec, 'tested_execution_time', None)
            if tested is None or getattr(rec, 'result_check', '') == 'different':
                continue
            if best_time is None or tested < best_time:
                best_time = tested
            # Only a significant result may contribute a headline percentage
            if exec_time and (getattr(rec, 'verdict', '') or SIGNIFICANT) == SIGNIFICANT:
                improvement = ((exec_time - tested) / exec_time) * 100
                if improvement > best_improvement:
                    best_improvement = improvement

        cells = [
            ("ORIGINAL RUNTIME", format_ms(exec_time) if exec_time else "N/A", Palette.INK),
            ("BEST TESTED RUNTIME", format_ms(best_time) if best_time is not None else "N/A", Palette.ACCENT_DEEP),
            ("IMPROVEMENT", f"{best_improvement:.1f}%" if best_improvement > 0 else "none",
             Palette.SUCCESS if best_improvement > 0 else Palette.MUTED),
            ("RECOMMENDATIONS", str(len(recommendations)), Palette.INK),
        ]

        labels = [Paragraph(label, self.styles['MetricLabel']) for label, _, _ in cells]
        values = [Paragraph(escape_markup(value), self.styles['MetricValue']) for _, value, _ in cells]

        column_width = self.content_width / 4
        table = Table([labels, values], colWidths=[column_width] * 4)
        # Colour the figures through the table style: it takes Color objects directly,
        # so there is no colour string to get wrong.
        style = [
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LINEBEFORE', (1, 0), (-1, -1), 0.5, Palette.RULE),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('TOPPADDING', (0, 0), (-1, 0), 2),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 2),
            ('BOTTOMPADDING', (0, 1), (-1, 1), 4),
        ]
        for column, (_, _, color) in enumerate(cells):
            style.append(('TEXTCOLOR', (column, 1), (column, 1), color))
        table.setStyle(TableStyle(style))
        return [table, Spacer(1, 6),
                HRFlowable(width="100%", thickness=0.6, color=Palette.RULE, spaceAfter=14)]

    def _create_query_section(self, query_history) -> List:
        elements = self._section("Query under review", "The workload")
        elements.append(self._code_block(query_history.original_query.strip()))
        elements.append(Spacer(1, 14))
        return elements

    def _create_action_section(self, winner, original_time: Optional[float],
                               recommendations: List = None) -> List:
        """The one thing a reader needs: what to run, and what it buys."""
        elements = self._section("Recommended action", "What to do")
        recommendations = recommendations or []

        if winner is None:
            verdicts = {getattr(rec, 'verdict', '') for rec in recommendations}
            if 'already_fast' in verdicts:
                body = (f"<b>No action needed.</b> The query already runs in "
                        f"{format_ms(original_time)}. At this scale the difference between two "
                        "runs is cache and scheduling noise, so no index can be shown to help.")
            elif verdicts and verdicts <= {'within_noise', 'unknown', ''}:
                body = ("<b>No measurable improvement.</b> Every option tested within the "
                        "variation between repeated runs, so none of them can be said to help.")
            else:
                body = ("<b>No option beat the current query.</b> Review the options that follow "
                        "before applying anything: on small tables a sequential scan is frequently "
                        "already the optimal plan.")
            elements.append(self._callout([Paragraph(body, self.styles['CustomBody'])], Palette.WARNING))
            elements.append(Spacer(1, 14))
            return elements

        tested = winner.tested_execution_time
        improvement = ((original_time - tested) / original_time) * 100 if original_time else 0
        headline = (f"<b>Apply the indexes below.</b> Measured at {format_ms(tested)} against "
                    f"{format_ms(original_time)} for the current query, a <b>{improvement:.1f}% reduction</b> "
                    "in execution time.")

        rows = [Paragraph(headline, self.styles['Lead'])]
        indexes = getattr(winner, 'all_indexes_applied', None) or getattr(winner, 'suggested_indexes', None) or []
        for statement in indexes:
            rows.append(self._code_block(statement, small=True))
        if not indexes:
            final_query = getattr(winner, 'final_optimized_query', None) or getattr(winner, 'optimized_query', None)
            if final_query:
                rows.append(Paragraph("APPLY THE REWRITTEN QUERY", self.styles['MetricLabel']))
                rows.append(self._code_block(final_query, small=True))

        # What to know about this database before running it (lock time, duplicates, partitions)
        warnings = [check for check in getattr(winner, 'fit_checks', None) or [] if check.get('level') == 'warn']
        if warnings:
            rows.append(Paragraph("BEFORE YOU APPLY", self.styles['MetricLabel']))
            for check in warnings:
                rows.append(Paragraph(f"&#8226; {escape_markup(check.get('message'))}", self.styles['Small']))

        elements.append(self._callout(rows, Palette.SUCCESS))
        elements.append(Spacer(1, 14))
        return elements

    def _create_chart_section(self, original_time, recommendations) -> List:
        chart = self._create_timing_chart(original_time, recommendations)
        if chart is None:
            return []
        elements = self._section("Execution time by option", "The numbers")
        elements.append(chart)
        elements.append(self._source_note(
            "EXPLAIN (ANALYZE) on the original query and on each tested recommendation. Lower is better."))
        return elements

    def _create_timing_chart(self, original_time, recommendations) -> Optional[Drawing]:
        tested = [(f"Option {getattr(rec, 'rank', i) or i}", rec.tested_execution_time)
                  for i, rec in enumerate(recommendations, 1)
                  if getattr(rec, 'tested_execution_time', None) is not None]
        if not tested or not original_time:
            return None

        labels = ["Current"] + [name for name, _ in tested]
        values = [original_time] + [value for _, value in tested]

        row_height = 19
        drawing = Drawing(self.content_width, 26 + row_height * len(values))
        chart = HorizontalBarChart()
        chart.x = 58
        chart.y = 12
        chart.height = row_height * len(values)
        chart.width = self.content_width - 120
        chart.data = [values]
        chart.categoryAxis.categoryNames = labels
        chart.categoryAxis.labels.fontSize = 7.5
        chart.categoryAxis.labels.fontName = 'Helvetica'
        chart.categoryAxis.labels.fillColor = Palette.MUTED
        chart.categoryAxis.strokeColor = Palette.RULE
        chart.valueAxis.valueMin = 0
        chart.valueAxis.labels.fontSize = 6.5
        chart.valueAxis.labels.fillColor = Palette.MUTED
        chart.valueAxis.strokeColor = Palette.RULE
        chart.valueAxis.gridStrokeColor = Palette.RULE
        chart.valueAxis.gridStrokeWidth = 0.4
        chart.valueAxis.visibleGrid = True
        chart.barLabels.fontSize = 6.8
        chart.barLabels.fontName = 'Helvetica-Bold'
        chart.barLabels.fillColor = Palette.INK
        chart.barLabelFormat = '%0.2f ms'
        chart.barLabels.dx = 16
        chart.barSpacing = 3
        chart.bars.strokeWidth = 0

        chart.bars[(0, 0)].fillColor = Palette.MUTED
        for index, value in enumerate(values[1:], start=1):
            chart.bars[(0, index)].fillColor = Palette.ACCENT_DEEP if value < original_time else Palette.DANGER

        drawing.add(chart)
        return drawing

    def _create_comparison_table(self, query_history, winner) -> List:
        original_plan = query_history.original_plan
        elements = self._section("Current plan versus recommended plan", "The evidence")

        before = plan_metrics(original_plan)
        before_scans = self._extract_scan_types(original_plan)
        original_time = query_history.original_execution_time or before.get('execution_time')

        if winner is not None:
            after = plan_metrics(getattr(winner, 'tested_plan', None))
            after_scans = self._extract_scan_types(getattr(winner, 'tested_plan', None) or {})
            after_time = winner.tested_execution_time
        else:
            after, after_scans, after_time = {}, {}, None

        def scan_label(scans):
            if not scans:
                return "N/A"
            if scans.get('has_seq_scan'):
                return "Sequential scan"
            if scans.get('has_index_scan'):
                return "Index scan"
            return "No scan nodes"

        def change(before_value, after_value):
            if not isinstance(before_value, (int, float)) or not isinstance(after_value, (int, float)):
                return "-"
            if before_value == 0:
                return "-"
            delta = ((before_value - after_value) / before_value) * 100
            return f"{'-' if delta > 0 else '+'}{abs(delta):.1f}%"

        rows = [
            ['Measure', 'Current', 'Recommended', 'Change'],
            ['Execution time', format_ms(original_time), format_ms(after_time), change(original_time, after_time)],
            ['Access method', scan_label(before_scans), scan_label(after_scans), ''],
            ['Planner cost', format_number(before.get('total_cost')), format_number(after.get('total_cost')),
             change(before.get('total_cost'), after.get('total_cost'))],
            ['Rows returned', format_number(before.get('actual_rows')), format_number(after.get('actual_rows')), ''],
        ]

        label_width = self.content_width * 0.28
        value_width = (self.content_width - label_width) / 3
        elements.append(self._data_table(rows, [label_width] + [value_width] * 3))
        elements.append(self._source_note("EXPLAIN (ANALYZE) measurements on a full-data temporary copy."))
        elements.append(Spacer(1, 14))
        return elements

    def _create_plan_diagram(self, plan_data, caption: str, highlight_bottleneck: bool = True) -> List:
        diagram = create_plan_flowchart(plan_data, self.content_width,
                                        highlight_bottleneck=highlight_bottleneck)
        if diagram is None:
            return []
        elements = [diagram]
        if caption:
            elements.append(self._source_note(caption))
        elements.append(Spacer(1, 10))
        return elements

    def _create_tables_section(self, table_stats: Dict[str, Any], plan_data) -> List:
        """How big each table is, and which one the time goes into."""
        rows = table_rows(table_stats, plan_data)
        if not rows:
            return []
        elements = [Spacer(1, 14)]
        elements.extend(self._section("Tables involved", "Where the time goes"))

        header = ['Table', 'Rows (est.)', 'Data', 'Indexes', 'Total', 'Share of time']
        data = [header]
        bottleneck_row = None
        for row in rows:
            # A paragraph, not a plain string, so a long name wraps inside its column
            name = f"<b>{escape_markup(row['label'])}</b>"
            if row.get('is_partitioned'):
                name += f"<br/>partitioned, {row.get('partition_count') or '?'} partitions"
            if row['is_bottleneck']:
                name += f"<br/><font color='#{Palette.DANGER.hexval()[2:]}'><b>BOTTLENECK</b></font>"
                bottleneck_row = len(data)
            index_count = len(row.get('indexes') or [])
            data.append([
                Paragraph(name, self.styles['Small']),
                format_rows(row.get('row_estimate')),
                format_bytes(row.get('table_bytes')),
                f"{format_bytes(row.get('index_bytes'))} ({index_count})",
                format_bytes(row.get('total_bytes')),
                f"{row['share'] * 100:.0f}%" if row.get('share') is not None else "-",
            ])

        width = self.content_width
        table = self._data_table(data, [width * 0.34, width * 0.12, width * 0.13, width * 0.15,
                                        width * 0.13, width * 0.13])
        if bottleneck_row is not None:
            table.setStyle(TableStyle([
                ('FONTNAME', (1, bottleneck_row), (-1, bottleneck_row), 'Helvetica-Bold'),
            ]))
        elements.append(table)
        elements.append(self._source_note(
            "PostgreSQL catalog (pg_class, pg_stat_user_tables) when the query was analyzed; row counts "
            "are planner estimates. Share of time is the self time of each table's scan nodes in the "
            "original plan."))
        return elements

    def _create_execution_plan_section(self, plan_data, table_stats: Dict[str, Any] = None) -> List:
        elements = self._section("How the query executes today", "Diagnosis")

        metrics = plan_metrics(plan_data)
        if not metrics:
            elements.append(Paragraph("Execution plan data not available.", self.styles['CustomBody']))
            return elements

        summary = (f"Planning {format_ms(metrics.get('planning_time'))} &nbsp;&nbsp;|&nbsp;&nbsp; "
                   f"Execution {format_ms(metrics.get('execution_time'))} &nbsp;&nbsp;|&nbsp;&nbsp; "
                   f"Planner cost {format_number(metrics.get('total_cost'))}")
        elements.append(Paragraph(summary, self.styles['Small']))
        elements.append(Spacer(1, 8))

        elements.extend(self._create_plan_diagram(
            plan_data,
            "Execution plan, read top to bottom: the scans run first and each step feeds the one "
            "below it, ending with the rows returned to the client."))

        scan_info = self._extract_scan_types(plan_data)
        if scan_info['has_seq_scan']:
            seq_tables = ', '.join(collapse_partitions(scan_info['seq_scan_tables'], table_stats)) \
                or 'the scanned tables'
            finding = (f"<b>Sequential scan on {escape_markup(seq_tables)}.</b> Every row is read to satisfy "
                       "the filter, so runtime grows with the table. An index on the filtered columns "
                       "allows PostgreSQL to seek directly to the matching rows.")
            # Name the size of what is being read: 5 ms on 160 MB is a different story to 5 ms on 50 kB
            bottleneck = next((row for row in table_rows(table_stats or {}, plan_data) if row['is_bottleneck']), None)
            if bottleneck and bottleneck.get('share') is not None:
                finding += (f" <b>{escape_markup(bottleneck['label'])}</b> holds "
                            f"{format_bytes(bottleneck.get('total_bytes'))} (about "
                            f"{format_rows(bottleneck.get('row_estimate'))} rows) and takes "
                            f"{bottleneck['share'] * 100:.0f}% of the measured time.")
            accent = Palette.DANGER
        elif scan_info['has_index_scan']:
            finding = ("<b>The query already uses index scans.</b> Remaining gains will come from the "
                       "index definition or the shape of the query rather than from adding a first index.")
            accent = Palette.SUCCESS
        else:
            finding = "No scan operations were detected in this plan."
            accent = Palette.MUTED

        elements.append(self._callout([Paragraph(finding, self.styles['CustomBody'])], accent))
        elements.append(Spacer(1, 12))

        elements.append(Paragraph("PLAN DETAIL", self.styles['SubHeader']))
        elements.append(self._code_block(format_plan_tree(plan_data), small=True))
        return elements

    def _create_recommendations_section(self, recommendations, original_time, scan_info=None) -> List:
        elements = [PageBreak()]

        if not recommendations:
            elements.extend(self._section("Recommendations", "Options"))
            elements.append(Paragraph("No recommendations were generated for this query.",
                                      self.styles['CustomBody']))
            return elements

        total = len(recommendations)
        for i, rec in enumerate(recommendations, 1):
            if i > 1:
                elements.append(PageBreak())
            elements.extend(self._create_recommendation_card(rec, i, total, original_time, scan_info))

        return elements

    def _create_recommendation_card(self, rec, index: int, total: int, original_time: float,
                                    scan_info=None) -> List:
        """One recommendation per page: verdict, numbers, indexes, query, plan."""
        tested_time = rec.tested_execution_time
        verdict_code = getattr(rec, 'verdict', '') or ''
        verdict = verdict_sentence(rec, original_time)
        is_faster = tested_time is not None and original_time is not None and tested_time < original_time

        if verdict_code in ('within_noise', 'already_fast', 'unknown') or tested_time is None:
            verdict_color = Palette.MUTED
            is_faster = False
        elif is_faster and verdict_code != 'slower':
            verdict_color = Palette.SUCCESS
        else:
            verdict_color = Palette.DANGER

        rec_type = rec.get_recommendation_type_display() if hasattr(rec, 'get_recommendation_type_display') \
            else rec.recommendation_type

        elements = self._section(f"Option {index}: {escape_markup(rec_type)}",
                                 f"Recommendation {index} of {total}")

        # Description comes from the AI provider, so it must be escaped before it is parsed as markup
        elements.append(Paragraph(escape_markup(rec.description), self.styles['Lead']))
        elements.append(Spacer(1, 10))

        # Correctness before speed: did the rewrite return what the original returned?
        result_check = getattr(rec, 'result_check', '') or ''
        note = escape_markup(getattr(rec, 'result_check_note', '') or '')
        if result_check == 'different':
            elements.append(self._callout([Paragraph(
                f"<b>Different results.</b> {note} Do not apply this rewrite as it stands.",
                self.styles['CustomBody'])], Palette.DANGER))
            elements.append(Spacer(1, 10))
        elif result_check == 'same' and getattr(rec, 'query_was_rewritten', False):
            elements.append(self._callout([Paragraph(f"<b>Same results.</b> {note}", self.styles['CustomBody'])],
                                          Palette.SUCCESS))
            elements.append(Spacer(1, 10))
        elif result_check == 'unchecked':
            elements.append(self._callout([Paragraph(f"<b>Results not compared.</b> {note}",
                                                     self.styles['CustomBody'])], Palette.MUTED))
            elements.append(Spacer(1, 10))

        # Key numbers for this option
        attempts = getattr(rec, 'optimization_attempts', None) or 1
        seq_scan_note = "-"
        tested_plan = getattr(rec, 'tested_plan', None)
        if scan_info and scan_info.get('has_seq_scan') and tested_plan:
            tested_scans = self._extract_scan_types(tested_plan)
            seq_scan_note = "Eliminated" if not tested_scans['has_seq_scan'] else "Still present"

        repeats = getattr(rec, 'tested_execution_times', None) or []
        spread = getattr(rec, 'measurement_spread_ms', None)
        if repeats:
            measurement = f"median of {len(repeats)} runs"
            if isinstance(spread, (int, float)):
                measurement += f", spread {spread:.2f} ms"
        else:
            measurement = "single run"

        summary_rows = [
            ['Measure', 'Value'],
            ['Execution time', format_ms(tested_time) if tested_time else "not tested"],
            ['Versus current query', verdict],
            ['Measurement', measurement],
            ['Sequential scan', seq_scan_note],
            ['Tuning iterations', str(attempts)],
        ]
        elements.append(self._data_table(summary_rows,
                                         [self.content_width * 0.35, self.content_width * 0.65]))
        elements.append(Spacer(1, 12))

        all_indexes = getattr(rec, 'all_indexes_applied', None) or rec.suggested_indexes
        sizes = {(entry.get('statement') or '').strip(): entry
                 for entry in getattr(rec, 'index_sizes', None) or [] if isinstance(entry, dict)}
        if all_indexes:
            label = "INDEXES APPLIED DURING TESTING" if attempts > 1 else "INDEXES TO APPLY"
            elements.append(Paragraph(label, self.styles['SubHeader']))
            for statement in all_indexes:
                elements.append(self._code_block(statement, small=True))
                size = sizes.get(statement.strip().rstrip(';').strip())
                if size and isinstance(size.get('bytes'), (int, float)):
                    table_bytes = size.get('table_bytes')
                    share = (f", {size['bytes'] * 100 / table_bytes:.0f}% of the table's {format_bytes(table_bytes)}"
                             if isinstance(table_bytes, (int, float)) and table_bytes else "")
                    elements.append(Paragraph(f"Measured size on a full copy: {format_bytes(size['bytes'])}{share}",
                                              self.styles['Caption']))
                elements.append(Spacer(1, 4))
            elements.append(Spacer(1, 6))

        fit_checks = [check for check in getattr(rec, 'fit_checks', None) or [] if isinstance(check, dict)]
        if fit_checks:
            elements.append(Paragraph("FIT WITH THIS DATABASE", self.styles['SubHeader']))
            markers = {'ok': ('&#10003;', Palette.SUCCESS), 'warn': ('!', Palette.WARNING),
                       'info': ('i', Palette.ACCENT_DEEP)}
            for check in fit_checks:
                marker, color = markers.get(check.get('level'), ('i', Palette.MUTED))
                elements.append(Paragraph(
                    f"<font color='#{color.hexval()[2:]}'><b>{marker}</b></font>&nbsp;&nbsp;"
                    f"{escape_markup(check.get('message'))}", self.styles['Small']))
            elements.append(Spacer(1, 10))

        # Optimized query - always shown, with a note when it is unchanged
        final_query = getattr(rec, 'final_optimized_query', None) or rec.optimized_query
        query_was_rewritten = getattr(rec, 'query_was_rewritten', False)
        if final_query and final_query.strip():
            elements.append(Paragraph(
                "OPTIMIZED QUERY (REWRITTEN)" if query_was_rewritten
                else "OPTIMIZED QUERY (UNCHANGED - INDEXES ONLY)", self.styles['SubHeader']))
            elements.append(self._code_block(final_query.strip(), small=True))
            elements.append(Spacer(1, 10))

        # Optimized plan: diagram plus the node tree
        if tested_plan:
            elements.append(Paragraph("PLAN AFTER THIS CHANGE", self.styles['SubHeader']))
            still_slow = seq_scan_note == "Still present"
            elements.extend(self._create_plan_diagram(
                tested_plan,
                "Measured plan after applying this option." if not still_slow else
                "Measured plan after applying this option; a sequential scan remains.",
                highlight_bottleneck=still_slow,
            ))
            elements.append(self._code_block(
                format_plan_tree(tested_plan, MAX_RECOMMENDATION_PLAN_LINES), small=True))

        return elements

    def _create_footer(self, query_history) -> List:
        elements = [Spacer(1, 16),
                    HRFlowable(width="100%", thickness=0.6, color=Palette.RULE, spaceAfter=8)]

        methodology = ("<b>Method.</b> Each recommendation was applied to a temporary schema containing a "
                       "full copy of the affected tables and measured with EXPLAIN (ANALYZE). The temporary "
                       "schema is dropped afterwards and the analyzed database is never modified, so timings "
                       "reflect production data volumes on a colder cache.")
        elements.append(Paragraph(methodology, self.styles['Small']))
        elements.append(Spacer(1, 8))

        if query_history.ai_provider:
            provider = query_history.ai_provider.get('provider_name', 'AI')
            model = query_history.ai_provider.get('model', 'unknown')
            ai_text = f"Recommendations generated by {escape_markup(provider)} ({escape_markup(model)})"
        else:
            ai_text = "Recommendations generated by AI"

        elements.append(Paragraph(f"{ai_text}  |  Tuning Buddy", self.styles['Caption']))
        return elements


def generate_optimization_report(query_history, recommendations) -> io.BytesIO:
    """Convenience function to generate PDF report."""
    generator = PDFReportGenerator()
    return generator.generate_report(query_history, recommendations)
