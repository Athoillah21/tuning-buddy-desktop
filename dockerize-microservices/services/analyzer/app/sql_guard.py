"""
Gatekeeping for the SQL console and the result check.

The console runs in a read-only transaction, but that alone is not enough: with several
statements in one request, "COMMIT; SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE;
DROP TABLE t" would leave it. So a request must hold exactly one statement, and that
statement must start with a read-only keyword. PostgreSQL's read-only mode then rejects
whatever still slips through (a data-modifying CTE, EXPLAIN ANALYZE DELETE).
"""
import re
from typing import List

READ_ONLY_KEYWORDS = ('SELECT', 'WITH', 'VALUES', 'TABLE', 'EXPLAIN', 'SHOW')

_DOLLAR_TAG_RE = re.compile(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$')
_LEADING_WORD_RE = re.compile(r'[A-Za-z]+')


class GuardError(ValueError):
    """The SQL is not allowed in the console."""
    pass


def _is_identifier_char(char: str) -> bool:
    return char.isalnum() or char in ('_', '$')


def split_statements(sql_text: str, backslash_escapes: bool = False) -> List[str]:
    """
    Split on top-level semicolons, ignoring those inside quotes, dollar quotes and comments.
    Empty statements (";;", trailing ";") are dropped.

    E'...' strings always treat backslash as an escape. Plain '...' strings do so only when
    the server runs with standard_conforming_strings = off, hence `backslash_escapes`.
    """
    statements, current = [], []
    i, length = 0, len(sql_text)

    while i < length:
        char = sql_text[i]
        pair = sql_text[i:i + 2]
        previous = sql_text[i - 1] if i else ''
        before_previous = sql_text[i - 2] if i > 1 else ''

        if pair == '--':
            end = sql_text.find('\n', i)
            end = length if end == -1 else end
            current.append(sql_text[i:end])
            i = end
        elif pair == '/*':
            # Block comments nest in PostgreSQL
            depth, j = 1, i + 2
            while j < length and depth:
                if sql_text[j:j + 2] == '/*':
                    depth, j = depth + 1, j + 2
                elif sql_text[j:j + 2] == '*/':
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            current.append(sql_text[i:j])
            i = j
        elif char in ("'", '"'):
            # A doubled quote inside a string is an escaped quote, not the end
            escape_string = char == "'" and previous in ('E', 'e') and not _is_identifier_char(before_previous)
            backslash = char == "'" and (escape_string or backslash_escapes)
            j = i + 1
            while j < length:
                if backslash and sql_text[j] == '\\':
                    j += 2
                    continue
                if sql_text[j] == char:
                    if sql_text[j + 1:j + 2] == char:
                        j += 2
                        continue
                    break
                j += 1
            current.append(sql_text[i:j + 1])
            i = j + 1
        elif char == '$' and not _is_identifier_char(previous) and _DOLLAR_TAG_RE.match(sql_text, i):
            tag = _DOLLAR_TAG_RE.match(sql_text, i).group(0)
            end = sql_text.find(tag, i + len(tag))
            end = length if end == -1 else end + len(tag)
            current.append(sql_text[i:end])
            i = end
        elif char == ';':
            statements.append(''.join(current))
            current = []
            i += 1
        else:
            current.append(char)
            i += 1

    statements.append(''.join(current))
    return [statement.strip() for statement in statements if _has_code(statement)]


def _strip_comments(statement: str) -> str:
    """The statement with leading comments removed."""
    text = statement.lstrip()
    while text.startswith(('--', '/*')):
        if text.startswith('--'):
            newline = text.find('\n')
            text = '' if newline == -1 else text[newline + 1:]
        else:
            # split_statements already balanced nested comments; find the matching close
            depth, j = 1, 2
            while j < len(text) and depth:
                if text[j:j + 2] == '/*':
                    depth, j = depth + 1, j + 2
                elif text[j:j + 2] == '*/':
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            text = text[j:]
        text = text.lstrip()
    return text


def _has_code(statement: str) -> bool:
    return bool(_strip_comments(statement))


def leading_keyword(statement: str) -> str:
    text = _strip_comments(statement).lstrip('(').lstrip()
    match = _LEADING_WORD_RE.match(text)
    return match.group(0).upper() if match else ''


def check_console_sql(sql_text: str) -> str:
    """The single statement to run, or GuardError explaining why it is refused."""
    statements = split_statements(sql_text or '')
    if not statements:
        raise GuardError("Enter a query to run.")
    # Both string rules must agree on one statement, whichever the server uses
    if len(statements) > 1 or len(split_statements(sql_text, backslash_escapes=True)) > 1:
        raise GuardError("Run one statement at a time. The console does not accept several "
                         "statements separated by semicolons.")
    statement = statements[0]
    keyword = leading_keyword(statement)
    if keyword not in READ_ONLY_KEYWORDS:
        raise GuardError(f"The console is read-only: {keyword or 'this statement'} is not allowed. "
                         f"Use one of {', '.join(READ_ONLY_KEYWORDS)}.")
    return statement


# Analyze runs a query with EXPLAIN ANALYZE, which executes it: the pasted query and every
# rewrite the AI suggests must be one plain read-only statement (EXPLAIN is added by the analyzer)
ANALYZABLE_KEYWORDS = ('SELECT', 'WITH', 'VALUES', 'TABLE')


def check_analyzed_query(sql_text: str) -> str:
    """The single statement to analyze, or GuardError explaining why it is refused."""
    statements = split_statements(sql_text or '')
    if not statements:
        raise GuardError("There is no query to analyze.")
    if len(statements) > 1 or len(split_statements(sql_text, backslash_escapes=True)) > 1:
        raise GuardError("Analyze takes one query at a time, not several separated by semicolons.")
    statement = statements[0]
    keyword = leading_keyword(statement)
    if keyword not in ANALYZABLE_KEYWORDS:
        raise GuardError(f"Analyze only runs read-only queries: {keyword or 'this statement'} is not allowed. "
                         f"Start with {', '.join(ANALYZABLE_KEYWORDS)}.")
    return statement


# ---------------------------------------------------------------------------
# Write mode: the query tool with "Allow changes" on
# ---------------------------------------------------------------------------

# The script runs as one transaction that the service commits or rolls back itself
TRANSACTION_CONTROL = ('BEGIN', 'START', 'COMMIT', 'END', 'ROLLBACK', 'ABORT', 'PREPARE')
# PostgreSQL refuses these inside a transaction block, so they run on their own, autocommitted
_AUTOCOMMIT_RE = re.compile(
    r'^\s*(VACUUM|ALTER\s+SYSTEM|(CREATE|DROP)\s+(DATABASE|TABLESPACE)'
    r'|(CREATE|DROP)\s+(UNIQUE\s+)?INDEX\s+CONCURRENTLY|REINDEX\b.*\bCONCURRENTLY'
    r'|DETACH\s+PARTITION\b.*\bCONCURRENTLY)', re.IGNORECASE | re.DOTALL)
_PSQL_META_RE = re.compile(r'^\s*\\[A-Za-z!]', re.MULTILINE)
_COPY_STDIN_RE = re.compile(r'^\s*COPY\b.*\bFROM\s+STDIN\b', re.IGNORECASE | re.DOTALL)


def needs_autocommit(statement: str) -> bool:
    return bool(_AUTOCOMMIT_RE.match(_strip_comments(statement)))


def check_write_script(sql_text: str) -> List[str]:
    """
    The statements of a script for write mode, or GuardError. They run in one transaction, so
    transaction control is refused, and statements PostgreSQL cannot run in a transaction must
    be run on their own. psql commands and COPY FROM STDIN (as in a plain pg_dump file) need psql.
    """
    if _PSQL_META_RE.search(sql_text or ''):
        raise GuardError("psql commands such as \\connect are not SQL. Restore dump files with psql or "
                         "pg_restore, or remove those lines.")
    statements = [s for s in split_statements(sql_text or '') if _has_code(s)]
    if not statements:
        raise GuardError("Enter a statement to run.")
    for statement in statements:
        keyword = leading_keyword(statement)
        if keyword in TRANSACTION_CONTROL:
            raise GuardError(f"{keyword} is not needed: the whole script runs as one transaction, "
                             "committed when every statement succeeds and rolled back otherwise.")
        if _COPY_STDIN_RE.match(_strip_comments(statement)):
            raise GuardError("COPY ... FROM STDIN needs psql. Use INSERT statements, or load the file with psql.")
        if len(statements) > 1 and needs_autocommit(statement):
            raise GuardError(f"{keyword} cannot run inside a transaction. Run it on its own.")
    return statements
