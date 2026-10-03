"""
Tuning Buddy analyzer service: connects to user databases, runs EXPLAIN ANALYZE
and drives the optimize/test loop.
"""
import datetime
import decimal
import logging
import time
from typing import Dict, List, Literal, Optional

import psycopg2
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg2 import sql
from pydantic import BaseModel, Field

from .security import ServiceGuard
from . import browse, catalog, config, datagen, progress, sql_guard
from .db_connector import DBConnector
from .optimizer import OptimizationError, QueryOptimizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Tuning Buddy Analyzer Service", version="1.0.0")
app.add_middleware(ServiceGuard)  # who may call it: see security.py


class ConnectionParams(BaseModel):
    host: str
    port: int = 5432
    database: str
    user: str
    password: str = ""
    sslmode: str = "prefer"
    connect_timeout: Optional[int] = None


class TestConnectionIn(BaseModel):
    connection_params: ConnectionParams


class OptimizeIn(BaseModel):
    connection_params: ConnectionParams
    query: str = Field(min_length=1)
    test_recommendations: bool = True
    progress_id: Optional[str] = None  # the web page polls /optimize/progress/{id} while this runs


def _params(connection: ConnectionParams) -> dict:
    params = connection.model_dump()
    if params["connect_timeout"] is None:
        params["connect_timeout"] = config.DB_CONNECTION_TIMEOUT
    return params


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/connections/test")
def test_connection(body: TestConnectionIn):
    try:
        success, message = DBConnector(_params(body.connection_params)).test_connection()
    except Exception as e:
        success, message = False, str(e)
    return {"success": success, "message": message}


# ---------------------------------------------------------------------------
# Database explorer (read-only)
# ---------------------------------------------------------------------------

class ExplorerIn(BaseModel):
    connection_params: ConnectionParams


class ExplorerTablesIn(ExplorerIn):
    schema_name: Optional[str] = None


class ExplorerTableIn(ExplorerIn):
    schema_name: str
    table: str


class ExplorerPreviewIn(ExplorerTableIn):
    limit: int = Field(default=100, ge=1, le=config.EXPLORER_PREVIEW_MAX_ROWS)


class ExplorerQueryIn(ExplorerIn):
    sql: str
    limit: int = Field(default=500, ge=1, le=config.EXPLORER_CONSOLE_MAX_ROWS)
    # "write" runs a whole script in one transaction; the web app confirms it with the user first
    mode: Literal["read", "write"] = "read"
    confirm_not_production: bool = False


class ExplorerSchemaIn(ExplorerIn):
    schema_name: str


class ExplorerObjectsIn(ExplorerSchemaIn):
    group: Literal["tables", "views", "materialized_views", "functions", "sequences", "types"]


class ExplorerObjectIn(ExplorerIn):
    kind: Literal["view", "materialized_view", "function", "sequence", "type", "table_ddl"]
    schema_name: str = ""
    name: str = ""
    oid: Optional[int] = None


class DatagenPlanIn(ExplorerSchemaIn):
    tables: Optional[List[str]] = None


class DatagenStartIn(ExplorerSchemaIn):
    targets: Dict[str, int]  # table -> bytes to add
    confirm_database: str
    confirm_not_production: bool = False


def _cell(value):
    """A result value as something JSON can carry and a table cell can show."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        text = '\\x' + bytes(value).hex()
    elif isinstance(value, (dict, list)):
        return value
    elif isinstance(value, decimal.Decimal):
        return str(value)
    elif isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    else:
        text = str(value)
    limit = config.EXPLORER_MAX_CELL_CHARS
    return text if len(text) <= limit else text[:limit] + '…'


def _error_message(e: Exception, timeout_seconds: int = config.EXPLORER_TIMEOUT) -> str:
    if isinstance(e, psycopg2.errors.QueryCanceled):
        return f"Stopped after {timeout_seconds}s (statement timeout)."
    if isinstance(e, psycopg2.errors.ReadOnlySqlTransaction):
        # PostgreSQL names the outer statement ("cannot execute SELECT"), which is misleading
        # for a data-modifying CTE such as WITH d AS (DELETE ...) SELECT ...
        return "The console is read-only: this statement tries to change data, so it was not run."
    return (getattr(e, 'pgerror', None) or str(e)).strip()


def _explorer_error(e: Exception):
    return JSONResponse(status_code=400, content={"success": False, "error": _error_message(e)})


def _explorer(body: ExplorerIn):
    return DBConnector(_params(body.connection_params)).readonly_cursor(config.EXPLORER_TIMEOUT * 1000)


@app.post("/explorer/overview")
def explorer_overview(body: ExplorerIn):
    try:
        with _explorer(body) as cur:
            return {"success": True, **catalog.database_overview(cur)}
    except Exception as e:
        return _explorer_error(e)


@app.post("/explorer/tables")
def explorer_tables(body: ExplorerTablesIn):
    try:
        with _explorer(body) as cur:
            return {"success": True, "tables": catalog.list_tables(cur, body.schema_name)}
    except Exception as e:
        return _explorer_error(e)


@app.post("/explorer/table")
def explorer_table(body: ExplorerTableIn):
    try:
        with _explorer(body) as cur:
            detail = catalog.table_detail(cur, body.schema_name, body.table)
    except Exception as e:
        return _explorer_error(e)
    if detail is None:
        return JSONResponse(status_code=404, content={"success": False,
                                                      "error": f"Table {body.schema_name}.{body.table} not found"})
    return {"success": True, **detail}


@app.post("/explorer/preview")
def explorer_preview(body: ExplorerPreviewIn):
    """The first rows of a table. Spatial columns are shown as EWKT instead of hex WKB."""
    name = catalog.quote_qualified(body.schema_name, body.table)
    try:
        with _explorer(body) as cur:
            columns = catalog.table_columns(cur, name)
            if not columns:
                return JSONResponse(status_code=404, content={"success": False, "error": f"Table {name} not found"})
            select = []
            for column in columns:
                identifier = sql.Identifier(column['name'])
                if column['type'].startswith(('geometry', 'geography')):
                    select.append(sql.SQL("ST_AsEWKT({0}) AS {0}").format(identifier))
                else:
                    select.append(identifier)
            cur.execute(sql.SQL("SELECT {} FROM {}.{} LIMIT %s").format(
                sql.SQL(', ').join(select), sql.Identifier(body.schema_name), sql.Identifier(body.table)),
                (body.limit,))
            rows = [[_cell(value) for value in row] for row in cur.fetchall()]
    except Exception as e:
        return _explorer_error(e)
    return {"success": True, "columns": [{"name": c['name'], "type": c['type']} for c in columns], "rows": rows}


def _result_set(cur, limit: int) -> dict:
    if cur.description is None:
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}
    fetched = cur.fetchmany(limit + 1)
    rows = [[_cell(value) for value in row] for row in fetched[:limit]]
    return {"columns": [{"name": column.name} for column in cur.description], "rows": rows,
            "row_count": len(rows), "truncated": len(fetched) > limit}


def _explorer_write(body: ExplorerQueryIn):
    """
    A script run as one transaction: committed when every statement succeeds, rolled back at
    the first error. Statements PostgreSQL cannot run in a transaction run alone, autocommitted.
    """
    if not datagen.is_local_host(body.connection_params.host) and not body.confirm_not_production:
        return JSONResponse(status_code=400, content={
            "success": False, "error": "Confirm that this is not a production database before changing it."})
    try:
        statements = sql_guard.check_write_script(body.sql)
    except sql_guard.GuardError as e:
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})

    started = time.perf_counter()
    results, index = [], 0
    try:
        with DBConnector(_params(body.connection_params)).get_connection() as conn:
            autocommit = len(statements) == 1 and sql_guard.needs_autocommit(statements[0])
            conn.autocommit = autocommit
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = %s", (config.QUERY_TOOL_WRITE_TIMEOUT * 1000,))
                for index, statement in enumerate(statements):
                    statement_started = time.perf_counter()
                    cur.execute(statement)
                    results.append({
                        "statement": statement.strip()[:300],
                        "command": cur.statusmessage,
                        "affected": cur.rowcount if cur.description is None else None,
                        "duration_ms": round((time.perf_counter() - statement_started) * 1000, 2),
                        **_result_set(cur, body.limit),
                    })
            if not autocommit:
                conn.commit()
    except Exception as e:
        return JSONResponse(status_code=400, content={
            "success": False, "rolled_back": True, "statement_index": index, "statement_count": len(statements),
            "error": _error_message(e, config.QUERY_TOOL_WRITE_TIMEOUT), "results": results,
        })
    return {"success": True, "mode": "write", "committed": True, "results": results,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2)}


@app.post("/explorer/query")
def explorer_query(body: ExplorerQueryIn):
    """The SQL console: read-only by default (one statement, read-only keywords and transaction)."""
    if body.mode == "write":
        return _explorer_write(body)
    try:
        statement = sql_guard.check_console_sql(body.sql)
    except sql_guard.GuardError as e:
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})

    started = time.perf_counter()
    try:
        with _explorer(body) as cur:
            cur.execute(statement)
            result = _result_set(cur, body.limit)
    except Exception as e:
        return _explorer_error(e)
    return {"success": True, "mode": "read", **result,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2)}


# ---------------------------------------------------------------------------
# Object browser (pgAdmin-style tree and panels)
# ---------------------------------------------------------------------------

@app.post("/explorer/server")
def explorer_server(body: ExplorerIn):
    try:
        with _explorer(body) as cur:
            return {"success": True, **browse.server_info(cur), "databases": browse.list_databases(cur)}
    except Exception as e:
        return _explorer_error(e)


@app.post("/explorer/databases")
def explorer_databases(body: ExplorerIn):
    try:
        with _explorer(body) as cur:
            return {"success": True, "databases": browse.list_databases(cur)}
    except Exception as e:
        return _explorer_error(e)


@app.post("/explorer/schemas")
def explorer_schemas(body: ExplorerIn):
    try:
        with _explorer(body) as cur:
            return {"success": True, "schemas": browse.list_schemas(cur), "extensions": browse.extensions(cur)}
    except Exception as e:
        return _explorer_error(e)


@app.post("/explorer/groups")
def explorer_groups(body: ExplorerSchemaIn):
    try:
        with _explorer(body) as cur:
            return {"success": True, "counts": browse.group_counts(cur, body.schema_name)}
    except Exception as e:
        return _explorer_error(e)


@app.post("/explorer/objects")
def explorer_objects(body: ExplorerObjectsIn):
    try:
        with _explorer(body) as cur:
            return {"success": True, "objects": browse.list_objects(cur, body.schema_name, body.group)}
    except Exception as e:
        return _explorer_error(e)


@app.post("/explorer/object")
def explorer_object(body: ExplorerObjectIn):
    """Details of a view, materialized view, function, sequence or type, or a table's DDL."""
    try:
        with _explorer(body) as cur:
            if body.kind in ("view", "materialized_view"):
                detail = browse.view_detail(cur, body.schema_name, body.name)
            elif body.kind == "function":
                detail = browse.function_detail(cur, body.oid) if body.oid else None
            elif body.kind == "sequence":
                detail = browse.sequence_detail(cur, body.schema_name, body.name)
            elif body.kind == "type":
                detail = browse.type_detail(cur, body.schema_name, body.name)
            else:
                ddl = browse.table_ddl(cur, body.schema_name, body.name)
                detail = {"ddl": ddl} if ddl is not None else None
    except Exception as e:
        return _explorer_error(e)
    if detail is None:
        return JSONResponse(status_code=404, content={"success": False, "error": f"{body.name or body.oid} not found"})
    return {"success": True, **detail}


# ---------------------------------------------------------------------------
# Test data generator
# ---------------------------------------------------------------------------

@app.post("/datagen/plan")
def datagen_plan(body: DatagenPlanIn):
    try:
        return {"success": True, **datagen.plan(_params(body.connection_params), body.schema_name, body.tables)}
    except Exception as e:
        logger.exception("Data generator plan failed")
        return _explorer_error(e)


@app.post("/datagen/start")
def datagen_start(body: DatagenStartIn):
    params = _params(body.connection_params)
    # Checked here too, not only in the page: nothing is written without both confirmations
    if body.confirm_database.strip() != params["database"]:
        return JSONResponse(status_code=400, content={
            "success": False, "error": "Type the database name exactly to confirm."})
    if not datagen.is_local_host(params["host"]) and not body.confirm_not_production:
        return JSONResponse(status_code=400, content={
            "success": False, "error": "Confirm that this is not a production database."})
    try:
        job_id = datagen.start_job(params, body.schema_name, body.targets)
    except datagen.DatagenError as e:
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})
    except Exception as e:
        logger.exception("Could not start data generation")
        return _explorer_error(e)
    return {"success": True, "job_id": job_id}


@app.get("/datagen/jobs/{job_id}")
def datagen_job(job_id: str):
    job = datagen.get_job(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"success": False, "error": "Unknown job"})
    return {"success": True, **job}


@app.post("/datagen/jobs/{job_id}/cancel")
def datagen_cancel(job_id: str):
    if not datagen.cancel_job(job_id):
        return JSONResponse(status_code=404, content={"success": False, "error": "Unknown job"})
    return {"success": True}


@app.post("/optimize")
def optimize(body: OptimizeIn):
    reporter = progress.reporter(body.progress_id)
    try:
        optimizer = QueryOptimizer(_params(body.connection_params), progress=reporter)
        result = optimizer.optimize(body.query, test_recommendations=body.test_recommendations)
    except OptimizationError as e:
        reporter.finish(False, str(e))
        return {"success": False, "stage": "optimization", "error": str(e)}
    except Exception as e:
        logger.exception("Unexpected error during optimization")
        reporter.finish(False, str(e))
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})
    error = result.get('error') or '; '.join(result.get('errors') or [])
    reporter.finish(bool(result.get('success')), error or None)
    return result


@app.get("/optimize/progress/{progress_id}")
def optimize_progress(progress_id: str):
    """What a running analysis is doing right now, for the web app's overlay."""
    state = progress.snapshot(progress_id) if progress.PROGRESS_ID_RE.fullmatch(progress_id) else None
    if state is None:
        return JSONResponse(status_code=404, content={"success": False, "error": "Unknown analysis"})
    return {"success": True, **state}
