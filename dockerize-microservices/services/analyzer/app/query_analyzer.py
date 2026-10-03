"""
Query analyzer service.
Parses SQL queries, extracts table information, and identifies potential issues.
"""
import re
from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


# Dangerous SQL patterns that could harm the database
DANGEROUS_PATTERNS = [
    (r'\bDROP\s+', 'DROP statements are not allowed'),
    (r'\bTRUNCATE\s+', 'TRUNCATE statements are not allowed'),
    (r'\bDELETE\s+FROM\s+\w+\s*(?:;|$)', 'DELETE without WHERE clause is not allowed'),
    (r'\bUPDATE\s+\w+\s+SET\s+.*(?:;|$)(?!\s*WHERE)', 'UPDATE without WHERE clause is dangerous'),
    (r'\bALTER\s+', 'ALTER statements are not allowed'),
    (r'\bCREATE\s+(?!INDEX)', 'CREATE statements (except INDEX) are not allowed'),
    (r'\bGRANT\s+', 'GRANT statements are not allowed'),
    (r'\bREVOKE\s+', 'REVOKE statements are not allowed'),
]

# Patterns that indicate potential performance issues
PERFORMANCE_ISSUE_PATTERNS = [
    (r'SELECT\s+\*', 'Selecting all columns (SELECT *) can be inefficient'),
    (r'(?<!NOT\s)LIKE\s+[\'"]%', 'Leading wildcard in LIKE prevents index usage'),
    (r'(?i)\bOR\b', 'OR conditions may prevent index optimization'),
    (r'(?i)\bIN\s*\([^)]{100,}\)', 'Large IN clause might be slow'),
]


class QueryValidator:
    """Validates SQL queries for safety and basic optimization issues."""
    
    @staticmethod
    def validate(query: str) -> Dict[str, Any]:
        """
        Validate a query for safety and potential issues.
        
        Returns:
            Dict with 'is_valid', 'errors', and 'warnings'
        """
        errors = []
        warnings = []
        
        # Check for dangerous patterns
        for pattern, message in DANGEROUS_PATTERNS:
            if re.search(pattern, query, re.IGNORECASE):
                errors.append(message)
        
        # Check for performance issue patterns
        for pattern, message in PERFORMANCE_ISSUE_PATTERNS:
            if re.search(pattern, query, re.IGNORECASE):
                warnings.append(message)
        
        # Basic syntax check - must be a SELECT query for analysis
        cleaned = query.strip().upper()
        if not cleaned.startswith('SELECT') and not cleaned.startswith('WITH'):
            errors.append('Only SELECT queries and CTEs are supported for analysis')
        
        return {
            'is_valid': len(errors) == 0,
            'errors': errors,
            'warnings': warnings,
        }
    
    @staticmethod
    def extract_tables(query: str, plan: Optional[Dict[str, Any]] = None) -> List[str]:
        """
        Extract table names from a SQL query.
        This is a simplified parser - may not catch all cases.

        The regex also matches words that follow FROM without being tables
        (EXTRACT(YEAR FROM order_date), CTE names). Given the query's EXPLAIN plan,
        only names the plan actually scans are kept, spelled as the query wrote them.
        """
        tables = set()
        
        # Match tables after FROM
        from_pattern = r'\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)'
        tables.update(re.findall(from_pattern, query, re.IGNORECASE))
        
        # Match tables after JOIN
        join_pattern = r'\bJOIN\s+([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)'
        tables.update(re.findall(join_pattern, query, re.IGNORECASE))

        if plan is not None:
            scanned = ExecutionPlanAnalyzer.relation_names(plan)
            tables = {table for table in tables if table.split('.')[-1].lower() in scanned}

        return list(tables)


class ExecutionPlanAnalyzer:
    """Analyzes PostgreSQL EXPLAIN ANALYZE output."""
    
    @staticmethod
    def analyze_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze an execution plan and identify issues.
        
        Args:
            plan: EXPLAIN ANALYZE output in JSON format
            
        Returns:
            Dict with analysis results
        """
        issues = []
        suggestions = []
        stats = {
            'total_cost': 0,
            'seq_scans': [],
            'index_scans': [],
            'nested_loops': 0,
            'hash_joins': 0,
            'merge_joins': 0,
        }
        
        def traverse_plan(node: Dict[str, Any], depth: int = 0):
            if not isinstance(node, dict):
                return
                
            node_type = node.get('Node Type', '')
            
            # Track sequential scans
            if node_type == 'Seq Scan':
                relation = node.get('Relation Name', 'unknown')
                rows = node.get('Actual Rows', 0)
                stats['seq_scans'].append({
                    'table': relation,
                    'rows': rows,
                    'cost': node.get('Total Cost', 0),
                })
                if rows > 1000:
                    issues.append(f"Sequential scan on '{relation}' returning {rows} rows")
                    suggestions.append(f"Consider adding an index on '{relation}' for the filtered columns")
            
            # Track index scans
            elif 'Index' in node_type:
                relation = node.get('Relation Name', 'unknown')
                index = node.get('Index Name', 'unknown')
                stats['index_scans'].append({
                    'table': relation,
                    'index': index,
                })
            
            # Track join types
            elif node_type == 'Nested Loop':
                stats['nested_loops'] += 1
                if depth == 0:
                    issues.append("Top-level Nested Loop join may be slow for large datasets")
            elif node_type == 'Hash Join':
                stats['hash_joins'] += 1
            elif node_type == 'Merge Join':
                stats['merge_joins'] += 1
            
            # Track sort operations without index
            elif node_type == 'Sort':
                sort_key = node.get('Sort Key', [])
                if node.get('Sort Method', '').startswith('external'):
                    issues.append(f"External sort on disk for keys: {sort_key}")
                    suggestions.append("Consider adding an index to avoid disk-based sorting")
            
            # Recursively process child nodes
            plans = node.get('Plans', [])
            for child in plans:
                traverse_plan(child, depth + 1)
        
        # Start analysis from the Plan node
        plan_node = plan.get('Plan', plan)
        stats['total_cost'] = plan_node.get('Total Cost', 0)
        traverse_plan(plan_node)
        
        # Add general suggestions based on stats
        if stats['seq_scans'] and not stats['index_scans']:
            suggestions.append("No indexes are being used - consider adding relevant indexes")
        
        if stats['nested_loops'] > 2:
            suggestions.append("Multiple nested loops detected - query might benefit from restructuring")
        
        return {
            'issues': issues,
            'suggestions': suggestions,
            'stats': stats,
            'execution_time': plan.get('Execution Time', 0),
            'planning_time': plan.get('Planning Time', 0),
        }
    
    @staticmethod
    def has_seq_scan(plan: Dict[str, Any]) -> bool:
        """
        Check if an execution plan contains any sequential scans.
        
        Args:
            plan: EXPLAIN ANALYZE output in JSON format
            
        Returns:
            True if any sequential scan is present, False otherwise
        """
        def traverse_for_seq_scan(node: Dict[str, Any]) -> bool:
            if not isinstance(node, dict):
                return False
            
            node_type = node.get('Node Type', '')
            if node_type == 'Seq Scan':
                return True
            
            # Check child nodes
            for child in node.get('Plans', []):
                if traverse_for_seq_scan(child):
                    return True
            
            return False
        
        plan_node = plan.get('Plan', plan)
        return traverse_for_seq_scan(plan_node)

    @staticmethod
    def relation_names(plan: Dict[str, Any]) -> set:
        """Lower-cased names of every table the plan reads."""
        names = set()

        def traverse(node: Dict[str, Any]) -> None:
            if not isinstance(node, dict):
                return
            if node.get('Relation Name'):
                names.add(node['Relation Name'].lower())
            for child in node.get('Plans', []):
                traverse(child)

        traverse(plan.get('Plan', plan))
        return names
    
