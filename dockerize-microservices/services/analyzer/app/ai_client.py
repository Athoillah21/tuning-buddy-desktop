"""
HTTP client for the AI service. Exposes the same methods the optimizer used on the old in-process AIClient.
"""
import os
from typing import Any, Dict, List, Tuple

import httpx

from . import config


class AIClientError(Exception):
    """Raised when the AI service is unreachable or no provider could answer."""
    pass


class AIServiceClient:

    def __init__(self, base_url: str = None, timeout: float = None):
        self.base_url = (base_url or config.AI_SERVICE_URL).rstrip("/")
        self.timeout = timeout or config.AI_REQUEST_TIMEOUT

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        # The desktop app's services answer only calls that carry this run's key (security.py)
        token = os.environ.get("TB_INTERNAL_TOKEN", "")
        headers = {"X-Tuning-Buddy-Token": token} if token else {}
        try:
            response = httpx.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout, headers=headers)
        except httpx.HTTPError as e:
            raise AIClientError(f"AI service unreachable at {self.base_url}: {e}") from e

        if response.status_code == 401:
            raise AIClientError("The analyzer was refused by Tuning Buddy's AI service: its internal key did not "
                                "match. Close Tuning Buddy completely and open it again. If it keeps happening, "
                                "reinstall Tuning Buddy.")
        if response.status_code != 200:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise AIClientError(str(detail))
        return response.json()

    def get_optimization_recommendations(
        self,
        query: str,
        plan: Dict[str, Any],
        execution_time: float,
        issues: List[Any],
        table_info: Dict[str, Any] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        data = self._post("/recommendations", {
            "query": query,
            "plan": plan,
            "execution_time": execution_time,
            "issues": issues,
            "table_info": table_info,
        })
        return data["recommendations"], data["provider_info"]

    def get_seq_scan_fix(
        self,
        query: str,
        previous_recommendation: Dict[str, Any],
        tested_plan: Dict[str, Any],
        current_table_info: Dict[str, Any] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        data = self._post("/seq-scan-fix", {
            "query": query,
            "previous_recommendation": previous_recommendation,
            "tested_plan": tested_plan,
            "current_table_info": current_table_info,
        })
        return data["recommendation"], data["provider_info"]
