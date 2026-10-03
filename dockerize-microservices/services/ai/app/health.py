"""
Health check: a provider is "good" when it answers a tiny prompt with the JSON we asked for.
That covers the API key, model access, network reachability and JSON compliance,
which is everything the optimizer depends on.
"""
import logging
import time
from dataclasses import asdict, dataclass
from typing import Optional

from .parsing import AIParseError, parse_json_payload
from .prompts import HEALTH_CHECK_PROMPT, SYSTEM_PROMPT
from .providers import KIND_MESSAGES, BaseProvider, ProviderError

logger = logging.getLogger(__name__)

# Enough headroom for models that emit a little reasoning before the JSON
HEALTH_CHECK_MAX_TOKENS = 256


@dataclass
class CheckResult:
    healthy: bool
    message: str
    latency_ms: Optional[float] = None
    kind: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def failed(kind: str, detail: str = "", latency_ms: Optional[float] = None) -> CheckResult:
    summary = KIND_MESSAGES.get(kind, KIND_MESSAGES["error"])
    return CheckResult(False, f"{summary}: {detail}" if detail else summary, latency_ms, kind)


def run_health_check(adapter: BaseProvider) -> CheckResult:
    start = time.perf_counter()
    try:
        text = adapter.complete(SYSTEM_PROMPT, HEALTH_CHECK_PROMPT, max_tokens=HEALTH_CHECK_MAX_TOKENS)
    except ProviderError as e:
        return CheckResult(False, e.describe(), None, e.kind)
    except Exception as e:
        logger.exception("Unexpected error during health check")
        return failed("error", str(e)[:200])

    latency_ms = round((time.perf_counter() - start) * 1000, 1)

    try:
        payload = parse_json_payload(text)
    except AIParseError:
        return failed("invalid_output", f"got {text[:120]!r}", latency_ms)

    if not isinstance(payload, dict) or str(payload.get("status", "")).lower() != "ok":
        return failed("invalid_output", f"got {text[:120]!r}", latency_ms)

    return CheckResult(True, f"OK - responded with valid JSON in {latency_ms:.0f} ms", latency_ms)
