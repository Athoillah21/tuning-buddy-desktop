"""
Common interface and error type for AI provider adapters.
"""
from typing import Optional

# Human-readable explanation for each error kind, shown in the AI Settings page
KIND_MESSAGES = {
    "auth": "Invalid API key or no access to this model",
    "not_found": "Model or endpoint not found - check the model name and base URL",
    "rate_limit": "Quota exceeded or rate limited",
    "connection": "Could not reach the API host - check the base URL and network",
    "timeout": "Timed out waiting for the API",
    "bad_request": "The API rejected the request",
    "server": "The provider returned a server error",
    "refusal": "The model declined the request",
    "invalid_output": "The model did not return valid JSON",
    "config": "Provider configuration problem",
    "error": "Unexpected error",
}


class ProviderError(Exception):
    """An API-level failure from a provider, classified by kind."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind if kind in KIND_MESSAGES else "error"

    def describe(self) -> str:
        detail = str(self)
        summary = KIND_MESSAGES[self.kind]
        if len(detail) > 200:
            detail = detail[:200] + "..."
        return f"{summary}: {detail}" if detail else summary


def with_root_cause(error: BaseException, fallback: str) -> str:
    """
    The SDKs report every network failure as a bare "Connection error."; append the
    underlying cause (DNS, TLS, proxy, refused) so the user can tell them apart.
    """
    message = str(error) or fallback
    root = error
    while root.__cause__ is not None or root.__context__ is not None:
        root = root.__cause__ or root.__context__
    if root is not error and str(root) and str(root) not in message:
        message = f"{message} ({type(root).__name__}: {root})"
    return message


def kind_for_status(status: Optional[int]) -> str:
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limit"
    if status in (400, 422):
        return "bad_request"
    if status is not None and status >= 500:
        return "server"
    return "error"


class BaseProvider:
    """Adapters turn (system, prompt) into the model's text reply or raise ProviderError."""

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None,
                 timeout: float = 300.0, max_retries: int = 2):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries

    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        raise NotImplementedError
