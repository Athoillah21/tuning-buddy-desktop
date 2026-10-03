"""
HTTP clients for the backend services (analyzer, ai, report).
"""
from typing import Any, Dict, Optional

import requests
from django.conf import settings


class ServiceUnavailable(Exception):
    """The service could not be reached at all."""
    pass


class ServiceError(Exception):
    """The service answered with an error status."""

    def __init__(self, message: str, status_code: Optional[int] = None, payload: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}  # the JSON error body, for callers that show more than the message


def _error_detail(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300] or f"HTTP {response.status_code}"

    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, list):
        # FastAPI validation errors
        return "; ".join(
            f"{'.'.join(str(part) for part in item.get('loc', [])[1:])}: {item.get('msg')}"
            for item in detail if isinstance(item, dict)
        )
    if detail:
        return str(detail)
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    return str(data)[:300]


class _ServiceClient:
    service_name = "service"
    base_url = ""
    timeout = 30

    def _request(self, method: str, path: str, timeout: Optional[float] = None, **kwargs) -> requests.Response:
        url = f"{self.base_url.rstrip('/')}{path}"
        if settings.INTERNAL_TOKEN:  # the desktop services answer only calls that carry it
            kwargs["headers"] = {**(kwargs.get("headers") or {}), "X-Tuning-Buddy-Token": settings.INTERNAL_TOKEN}
        try:
            response = requests.request(method, url, timeout=timeout or self.timeout, **kwargs)
        except requests.RequestException as e:
            raise ServiceUnavailable(f"The {self.service_name} is unreachable at {self.base_url}: {e}") from e
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            raise ServiceError(_error_detail(response), response.status_code,
                               payload if isinstance(payload, dict) else None)
        return response


class AIServiceClient(_ServiceClient):
    service_name = "AI service"

    def __init__(self):
        self.base_url = settings.AI_SERVICE_URL
        self.timeout = 15

    def status(self) -> Dict[str, Any]:
        return self._request("GET", "/status", timeout=5).json()

    def provider_types(self) -> Dict[str, Any]:
        return self._request("GET", "/provider-types").json()

    def list_providers(self) -> list:
        return self._request("GET", "/providers").json()

    def get_provider(self, provider_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/providers/{provider_id}").json()

    def create_provider(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/providers", json=data).json()

    def update_provider(self, provider_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"/providers/{provider_id}", json=data).json()

    def delete_provider(self, provider_id: int) -> None:
        self._request("DELETE", f"/providers/{provider_id}")

    def check_provider(self, provider_id: int) -> Dict[str, Any]:
        return self._request("POST", f"/providers/{provider_id}/check", timeout=90).json()

    def check_all(self) -> Dict[str, Any]:
        return self._request("POST", "/providers/check-all", timeout=120).json()

    def test_provider(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/providers/test", json=data, timeout=90).json()


class AnalyzerClient(_ServiceClient):
    service_name = "analyzer service"

    def __init__(self):
        self.base_url = settings.ANALYZER_URL
        self.timeout = settings.ANALYZER_TIMEOUT

    def test_connection(self, connection_params: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/connections/test",
                             json={"connection_params": connection_params}, timeout=60).json()

    # Database explorer: everything below runs read-only on the analyzer side
    def explorer_overview(self, connection_params: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/explorer/overview",
                             json={"connection_params": connection_params}, timeout=60).json()

    def explorer_table(self, connection_params: Dict[str, Any], schema: str, table: str) -> Dict[str, Any]:
        return self._request("POST", "/explorer/table", json={
            "connection_params": connection_params, "schema_name": schema, "table": table,
        }, timeout=60).json()

    def explorer_preview(self, connection_params: Dict[str, Any], schema: str, table: str,
                         limit: int = 100) -> Dict[str, Any]:
        return self._request("POST", "/explorer/preview", json={
            "connection_params": connection_params, "schema_name": schema, "table": table, "limit": limit,
        }, timeout=60).json()

    def explorer_query(self, connection_params: Dict[str, Any], sql: str, limit: int = 500,
                       mode: str = "read", confirm_not_production: bool = False) -> Dict[str, Any]:
        return self._request("POST", "/explorer/query", json={
            "connection_params": connection_params, "sql": sql, "limit": limit,
            "mode": mode, "confirm_not_production": confirm_not_production,
        }, timeout=60 if mode == "read" else 900).json()

    # Object browser
    def explorer_server(self, connection_params: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/explorer/server", json={"connection_params": connection_params}, timeout=60).json()

    def explorer_databases(self, connection_params: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/explorer/databases", json={"connection_params": connection_params}, timeout=60).json()

    def explorer_schemas(self, connection_params: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/explorer/schemas", json={"connection_params": connection_params}, timeout=60).json()

    def explorer_groups(self, connection_params: Dict[str, Any], schema: str) -> Dict[str, Any]:
        return self._request("POST", "/explorer/groups", json={
            "connection_params": connection_params, "schema_name": schema}, timeout=60).json()

    def explorer_objects(self, connection_params: Dict[str, Any], schema: str, group: str) -> Dict[str, Any]:
        return self._request("POST", "/explorer/objects", json={
            "connection_params": connection_params, "schema_name": schema, "group": group}, timeout=60).json()

    def explorer_tables(self, connection_params: Dict[str, Any], schema: str) -> Dict[str, Any]:
        return self._request("POST", "/explorer/tables", json={
            "connection_params": connection_params, "schema_name": schema}, timeout=60).json()

    def explorer_object(self, connection_params: Dict[str, Any], kind: str, schema: str = "", name: str = "",
                        oid: Optional[int] = None) -> Dict[str, Any]:
        return self._request("POST", "/explorer/object", json={
            "connection_params": connection_params, "kind": kind, "schema_name": schema, "name": name, "oid": oid,
        }, timeout=60).json()

    # Test data generator
    def datagen_plan(self, connection_params: Dict[str, Any], schema: str, tables=None) -> Dict[str, Any]:
        return self._request("POST", "/datagen/plan", json={
            "connection_params": connection_params, "schema_name": schema, "tables": tables}, timeout=180).json()

    def datagen_start(self, connection_params: Dict[str, Any], schema: str, targets: Dict[str, int],
                      confirm_database: str, confirm_not_production: bool) -> Dict[str, Any]:
        return self._request("POST", "/datagen/start", json={
            "connection_params": connection_params, "schema_name": schema, "targets": targets,
            "confirm_database": confirm_database, "confirm_not_production": confirm_not_production,
        }, timeout=180).json()

    def datagen_job(self, job_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/datagen/jobs/{job_id}", timeout=15).json()

    def datagen_cancel(self, job_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/datagen/jobs/{job_id}/cancel", timeout=15).json()

    def optimize(self, connection_params: Dict[str, Any], query: str, test_recommendations: bool = True,
                 progress_id: Optional[str] = None) -> Dict[str, Any]:
        return self._request("POST", "/optimize", json={
            "connection_params": connection_params,
            "query": query,
            "test_recommendations": test_recommendations,
            "progress_id": progress_id,
        }).json()

    def analyze_progress(self, progress_id: str) -> Dict[str, Any]:
        """What a running analysis is doing right now (the overlay polls this)."""
        return self._request("GET", f"/optimize/progress/{progress_id}", timeout=5).json()


class ReportClient(_ServiceClient):
    service_name = "report service"

    def __init__(self):
        self.base_url = settings.REPORT_URL
        self.timeout = 60

    def optimization_report(self, payload: Dict[str, Any]) -> bytes:
        return self._request("POST", "/reports/optimization", json=payload).content
