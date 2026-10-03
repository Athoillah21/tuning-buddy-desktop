"""
Tuning Buddy report service: renders optimization results as PDF.
"""
import logging
from types import SimpleNamespace
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .security import ServiceGuard
from .pdf_generator import generate_optimization_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Tuning Buddy Report Service", version="1.0.0")
app.add_middleware(ServiceGuard)  # who may call it: see security.py

QUERY_HISTORY_DEFAULTS = {
    "id": None,
    "original_query": "",
    "original_plan": None,
    "original_execution_time": None,
    "original_execution_times": [],
    "ai_provider": None,
    "created_at": None,
    # Preformatted by the web service, which knows the configured timezone
    "created_at_display": None,
    # Size and shape of each table in the query (empty for analyses before 1.1)
    "table_stats": {},
}

RECOMMENDATION_DEFAULTS = {
    "recommendation_type": "rewrite",
    "description": "",
    "optimized_query": "",
    "suggested_indexes": [],
    "tested_execution_time": None,
    "tested_plan": None,
    "improvement_percentage": None,
    "rank": 0,
    "all_indexes_applied": [],
    "final_optimized_query": "",
    "query_was_rewritten": False,
    "optimization_attempts": 1,
    "seq_scan_eliminated": False,
    "verdict": "",
    "measurement_spread_ms": None,
    "tested_execution_times": [],
    "result_check": "",
    "result_check_note": "",
    "fit_checks": [],
    "index_sizes": [],
}


class OptimizationReportIn(BaseModel):
    query_history: Dict[str, Any]
    recommendations: List[Dict[str, Any]] = []


def _query_history(data: Dict[str, Any]) -> SimpleNamespace:
    """The generator was written against Django models, so give it attribute access."""
    query_history = SimpleNamespace(**{**QUERY_HISTORY_DEFAULTS, **data})
    query_history.connection = SimpleNamespace(**(data.get("connection") or {"name": "Unknown"}))
    return query_history


def _recommendation(data: Dict[str, Any]) -> SimpleNamespace:
    recommendation = SimpleNamespace(**{**RECOMMENDATION_DEFAULTS, **data})
    display = data.get("recommendation_type_display") or recommendation.recommendation_type
    recommendation.get_recommendation_type_display = lambda: display
    return recommendation


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/reports/optimization")
def optimization_report(body: OptimizationReportIn):
    try:
        buffer = generate_optimization_report(
            _query_history(body.query_history),
            [_recommendation(r) for r in body.recommendations],
        )
    except Exception as e:
        logger.exception("Error generating PDF")
        raise HTTPException(status_code=500, detail=f"Failed to generate PDF: {e}")
    return Response(content=buffer.getvalue(), media_type="application/pdf")
