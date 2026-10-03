"""
Business logic: health checks and recommendations with priority-based fallback.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, crypto
from .health import CheckResult, failed, run_health_check
from .models import AIProvider, utcnow
from .parsing import AIParseError, parse_recommendations, parse_single_recommendation, shorten_error
from .prompts import OPTIMIZATION_PROMPT, SEQ_SCAN_FIX_PROMPT, SYSTEM_PROMPT, format_table_info
from .providers import PROVIDER_TYPES, BaseProvider, ProviderError, build_adapter

logger = logging.getLogger(__name__)


class AllProvidersFailed(Exception):
    """Raised when no enabled provider could produce a usable response."""
    pass


def make_adapter(provider_type: str, api_key: str, model: str, base_url: Optional[str], *,
                 timeout: float, max_retries: int) -> BaseProvider:
    return build_adapter(provider_type, api_key=api_key, model=model, base_url=base_url,
                         timeout=timeout, max_retries=max_retries)


def adapter_for(provider: AIProvider, *, timeout: float, max_retries: int) -> BaseProvider:
    return make_adapter(provider.provider_type, crypto.decrypt(provider.api_key_encrypted),
                        provider.model, provider.base_url, timeout=timeout, max_retries=max_retries)


def provider_info(provider: AIProvider) -> Dict[str, Any]:
    """Same shape the monolith stored in QueryHistory.ai_provider."""
    return {
        'provider': provider.provider_type,
        'provider_name': provider.name,
        'model': provider.model,
        'color': PROVIDER_TYPES.get(provider.provider_type, {}).get('color', '#666'),
        'provider_id': provider.id,
    }


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def check_config(provider_type: str, api_key: str, model: str, base_url: Optional[str]) -> CheckResult:
    """Health-check a config that has not been saved."""
    try:
        adapter = make_adapter(provider_type, api_key, model, base_url,
                               timeout=config.HEALTH_CHECK_TIMEOUT, max_retries=0)
    except ProviderError as e:
        return CheckResult(False, e.describe(), None, e.kind)
    return run_health_check(adapter)


def _check_saved(provider: AIProvider) -> CheckResult:
    try:
        adapter = adapter_for(provider, timeout=config.HEALTH_CHECK_TIMEOUT, max_retries=0)
    except crypto.CryptoError as e:
        return failed("config", str(e))
    except ProviderError as e:
        return CheckResult(False, e.describe(), None, e.kind)
    return run_health_check(adapter)


def apply_check_result(provider: AIProvider, result: CheckResult) -> None:
    provider.last_check_status = "healthy" if result.healthy else "unhealthy"
    provider.last_check_message = result.message
    provider.last_check_latency_ms = result.latency_ms
    provider.last_checked_at = utcnow()


def check_provider(db: Session, provider: AIProvider) -> CheckResult:
    result = _check_saved(provider)
    apply_check_result(provider, result)
    db.commit()
    return result


def check_all(db: Session) -> List[Tuple[AIProvider, CheckResult]]:
    """Check every enabled provider in parallel."""
    providers = list(db.scalars(
        select(AIProvider).where(AIProvider.enabled.is_(True)).order_by(AIProvider.priority, AIProvider.id)
    ))
    if not providers:
        return []

    with ThreadPoolExecutor(max_workers=min(8, len(providers))) as pool:
        results = list(pool.map(_check_saved, providers))

    for provider, result in zip(providers, results):
        apply_check_result(provider, result)
    db.commit()
    return list(zip(providers, results))


def status_summary(db: Session) -> Dict[str, Any]:
    providers = list(db.scalars(select(AIProvider).order_by(AIProvider.priority, AIProvider.id)))
    enabled = [p for p in providers if p.enabled]
    healthy = [p for p in enabled if p.last_check_status == "healthy"]
    return {
        "ready": bool(healthy),
        "healthy_count": len(healthy),
        "enabled_count": len(enabled),
        "total_count": len(providers),
        "providers": [
            {
                "id": p.id,
                "name": p.name,
                "provider_type": p.provider_type,
                "model": p.model,
                "enabled": p.enabled,
                "status": p.last_check_status,
                "message": p.last_check_message,
                "last_checked_at": p.last_checked_at.isoformat() if p.last_checked_at else None,
            }
            for p in providers
        ],
    }


# ---------------------------------------------------------------------------
# Recommendations with fallback
# ---------------------------------------------------------------------------

def _candidates(db: Session) -> List[AIProvider]:
    enabled = db.scalars(select(AIProvider).where(AIProvider.enabled.is_(True)))
    return sorted(enabled, key=lambda p: (p.last_check_status != "healthy", p.priority, p.id))


def _complete_with_fallback(db: Session, prompt: str, parse: Callable[[str], Any]) -> Tuple[Any, Dict[str, Any]]:
    """
    Try each enabled provider (healthy first, then by priority) until one returns a parseable response.
    API-level failures mark the provider unhealthy so the web app's gate reflects reality.
    """
    candidates = _candidates(db)
    if not candidates:
        raise AllProvidersFailed("No enabled AI provider is configured. Add one in AI Settings.")

    errors = []
    for provider in candidates:
        logger.info(f"Trying AI provider: {provider.name} ({provider.provider_type}/{provider.model})")
        try:
            adapter = adapter_for(provider, timeout=config.REQUEST_TIMEOUT, max_retries=2)
            response_text = adapter.complete(SYSTEM_PROMPT, prompt,
                                             max_tokens=config.RECOMMENDATION_MAX_TOKENS)
        except (ProviderError, crypto.CryptoError) as e:
            message = e.describe() if isinstance(e, ProviderError) else str(e)
            message = shorten_error(message)
            logger.warning(f"Provider {provider.name} failed: {message}")
            errors.append(f"{provider.name}: {message}")
            provider.last_check_status = "unhealthy"
            provider.last_check_message = f"Failed during analysis - {message}"
            provider.last_checked_at = utcnow()
            db.commit()
            continue
        except Exception as e:
            logger.exception(f"Unexpected error from provider {provider.name}")
            errors.append(f"{provider.name}: {shorten_error(str(e))}")
            continue

        try:
            result = parse(response_text)
        except AIParseError as e:
            # A single malformed reply doesn't mean the provider is down; just try the next one
            logger.warning(f"Provider {provider.name} returned unparseable output: {e}")
            errors.append(f"{provider.name}: {e}")
            continue

        if provider.last_check_status != "healthy":
            provider.last_check_status = "healthy"
            provider.last_check_message = "OK - succeeded during analysis"
            provider.last_checked_at = utcnow()
            db.commit()

        logger.info(f"Successfully got response from {provider.name}")
        return result, provider_info(provider)

    raise AllProvidersFailed("All AI providers failed. " + " | ".join(errors))


def get_recommendations(db: Session, query: str, plan: Any, execution_time: float,
                        issues: List[Any], table_info: Optional[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    prompt = OPTIMIZATION_PROMPT.format(
        query=query,
        table_info=format_table_info(table_info) if table_info else "No table information available",
        plan=json.dumps(plan, indent=2)[:4000],
        execution_time=execution_time,
        issues='\n'.join(f"- {issue}" for issue in issues) if issues else "No specific issues detected",
    )
    return _complete_with_fallback(db, prompt, lambda text: parse_recommendations(text, query))


def get_seq_scan_fix(db: Session, query: str, previous_recommendation: Dict[str, Any], tested_plan: Any,
                     current_table_info: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    prompt = SEQ_SCAN_FIX_PROMPT.format(
        query=query,
        current_table_info=format_table_info(current_table_info) if current_table_info else "Table structure not available",
        prev_type=previous_recommendation.get('type', 'unknown'),
        prev_description=previous_recommendation.get('description', ''),
        prev_query=previous_recommendation.get('optimized_query', query),
        prev_indexes=json.dumps(previous_recommendation.get('suggested_indexes', [])),
        plan=json.dumps(tested_plan, indent=2)[:4000],
    )
    return _complete_with_fallback(db, prompt, lambda text: parse_single_recommendation(text, query))
