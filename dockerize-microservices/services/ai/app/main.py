"""
Tuning Buddy AI service: provider configuration, health checks and recommendations.
"""
import logging
from contextlib import asynccontextmanager
from typing import List, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .security import ServiceGuard
from . import crypto, service
from .db import Base, engine, get_db
from .models import AIProvider
from .providers import PROVIDER_TYPES
from .schemas import (
    CheckOut,
    ProviderCreate,
    ProviderOut,
    ProviderTest,
    ProviderUpdate,
    RecommendationsIn,
    SeqScanFixIn,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    crypto.validate_key()
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Tuning Buddy AI Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(ServiceGuard)  # who may call it: see security.py


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


# Providers on this computer (or the Docker stack's own network), where http:// never leaves the machine
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal", "ollama"}

KEY_FOR_NEW_ADDRESS = ("Enter the API key again to use this provider with a different address or type: "
                       "a saved key is only ever sent where it was first entered for.")


def _host(base_url: Optional[str]) -> str:
    return (urlsplit(base_url).hostname or "").lower() if base_url else ""


def _validate_config(provider_type: str, base_url: Optional[str], has_api_key: bool) -> None:
    meta = PROVIDER_TYPES[provider_type]
    if meta["base_url_required"] and not base_url:
        raise HTTPException(status_code=422, detail=f"A base URL is required for {meta['label']} providers")
    if meta["api_key_required"] and not has_api_key:
        raise HTTPException(status_code=422, detail=f"An API key is required for {meta['label']}")
    # An API key must not cross the network in plain text (a local Ollama or LM Studio is fine)
    if has_api_key and base_url and urlsplit(base_url).scheme == "http" and _host(base_url) not in LOCAL_HOSTS:
        raise HTTPException(status_code=422, detail="Use https:// for a provider on another computer: with http:// "
                                                    "the API key and your queries would cross the network unencrypted.")


def _to_out(provider: AIProvider) -> ProviderOut:
    try:
        api_key = crypto.decrypt(provider.api_key_encrypted)
    except crypto.CryptoError:
        api_key = ""
    return ProviderOut(
        id=provider.id,
        name=provider.name,
        provider_type=provider.provider_type,
        provider_label=PROVIDER_TYPES.get(provider.provider_type, {}).get("label", provider.provider_type),
        base_url=provider.base_url,
        model=provider.model,
        api_key_masked=crypto.mask(api_key),
        has_api_key=bool(provider.api_key_encrypted),
        priority=provider.priority,
        enabled=provider.enabled,
        last_check_status=provider.last_check_status,
        last_check_message=provider.last_check_message,
        last_check_latency_ms=provider.last_check_latency_ms,
        last_checked_at=provider.last_checked_at,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


def _get_or_404(db: Session, provider_id: int) -> AIProvider:
    provider = db.get(AIProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/provider-types")
def provider_types():
    return PROVIDER_TYPES


# ---------------------------------------------------------------------------
# Provider CRUD
# ---------------------------------------------------------------------------

@app.get("/providers", response_model=List[ProviderOut])
def list_providers(db: Session = Depends(get_db)):
    providers = db.scalars(select(AIProvider).order_by(AIProvider.priority, AIProvider.id))
    return [_to_out(p) for p in providers]


@app.post("/providers", response_model=ProviderOut, status_code=201)
def create_provider(body: ProviderCreate, db: Session = Depends(get_db)):
    api_key = (body.api_key or "").strip()
    base_url = _clean(body.base_url)
    _validate_config(body.provider_type, base_url, bool(api_key))

    provider = AIProvider(
        name=body.name.strip(),
        provider_type=body.provider_type,
        base_url=base_url,
        model=body.model.strip(),
        api_key_encrypted=crypto.encrypt(api_key),
        priority=body.priority,
        enabled=body.enabled,
        last_check_status="unknown",
    )
    db.add(provider)
    db.commit()
    return _to_out(provider)


@app.get("/providers/{provider_id}", response_model=ProviderOut)
def get_provider(provider_id: int, db: Session = Depends(get_db)):
    return _to_out(_get_or_404(db, provider_id))


@app.put("/providers/{provider_id}", response_model=ProviderOut)
def update_provider(provider_id: int, body: ProviderUpdate, db: Session = Depends(get_db)):
    provider = _get_or_404(db, provider_id)
    data = body.model_dump(exclude_unset=True)
    config_changed = False

    # A saved key never follows the provider to another host or vendor without being typed again
    new_type = data.get("provider_type") or provider.provider_type
    new_base_url = _clean(data["base_url"]) if "base_url" in data else provider.base_url
    if (provider.api_key_encrypted and not body.clear_api_key and not (data.get("api_key") or "").strip()
            and (new_type != provider.provider_type or _host(new_base_url) != _host(provider.base_url))):
        raise HTTPException(status_code=422, detail=KEY_FOR_NEW_ADDRESS)

    if data.get("name") is not None:
        provider.name = data["name"].strip()
    if data.get("priority") is not None:
        provider.priority = data["priority"]
    if data.get("enabled") is not None:
        provider.enabled = data["enabled"]

    if data.get("provider_type") and data["provider_type"] != provider.provider_type:
        provider.provider_type = data["provider_type"]
        config_changed = True
    if data.get("model") is not None and data["model"].strip() != provider.model:
        provider.model = data["model"].strip()
        config_changed = True
    if "base_url" in data and _clean(data["base_url"]) != provider.base_url:
        provider.base_url = _clean(data["base_url"])
        config_changed = True
    if body.clear_api_key and provider.api_key_encrypted:
        provider.api_key_encrypted = ""
        config_changed = True
    elif (data.get("api_key") or "").strip():
        provider.api_key_encrypted = crypto.encrypt(data["api_key"].strip())
        config_changed = True

    _validate_config(provider.provider_type, provider.base_url, bool(provider.api_key_encrypted))

    # A changed config hasn't been verified yet
    if config_changed:
        provider.reset_check()

    db.commit()
    return _to_out(provider)


@app.delete("/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: int, db: Session = Depends(get_db)):
    db.delete(_get_or_404(db, provider_id))
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

@app.post("/providers/test", response_model=CheckOut)
def test_provider_config(body: ProviderTest, db: Session = Depends(get_db)):
    """Check a config before it is saved (the form's Test button)."""
    api_key = (body.api_key or "").strip()
    base_url = _clean(body.base_url)
    if not api_key and body.provider_id is not None:
        stored = _get_or_404(db, body.provider_id)
        # The saved key is only tested against the address and type it was saved for
        if body.provider_type != stored.provider_type or _host(base_url) != _host(stored.base_url):
            raise HTTPException(status_code=422, detail=KEY_FOR_NEW_ADDRESS)
        try:
            api_key = crypto.decrypt(stored.api_key_encrypted)
        except crypto.CryptoError as e:
            raise HTTPException(status_code=422, detail=str(e))
    _validate_config(body.provider_type, base_url, bool(api_key))
    result = service.check_config(body.provider_type, api_key, body.model.strip(), base_url)
    return CheckOut(**result.as_dict())


@app.post("/providers/check-all")
def check_all_providers(db: Session = Depends(get_db)):
    results = service.check_all(db)
    return {
        "results": [
            {"provider": _to_out(p), "result": CheckOut(**r.as_dict())}
            for p, r in results
        ],
        "ready": service.status_summary(db)["ready"],
    }


@app.post("/providers/{provider_id}/check")
def check_provider(provider_id: int, db: Session = Depends(get_db)):
    provider = _get_or_404(db, provider_id)
    result = service.check_provider(db, provider)
    return {
        "provider": _to_out(provider),
        "result": CheckOut(**result.as_dict()),
        "ready": service.status_summary(db)["ready"],
    }


@app.get("/status")
def status(db: Session = Depends(get_db)):
    """ready=True when at least one enabled provider passed its latest health check."""
    return service.status_summary(db)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

@app.post("/recommendations")
def recommendations(body: RecommendationsIn, db: Session = Depends(get_db)):
    try:
        recs, info = service.get_recommendations(
            db, body.query, body.plan or {}, body.execution_time, body.issues, body.table_info,
        )
    except service.AllProvidersFailed as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"recommendations": recs, "provider_info": info}


@app.post("/seq-scan-fix")
def seq_scan_fix(body: SeqScanFixIn, db: Session = Depends(get_db)):
    try:
        rec, info = service.get_seq_scan_fix(
            db, body.query, body.previous_recommendation, body.tested_plan or {}, body.current_table_info,
        )
    except service.AllProvidersFailed as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"recommendation": rec, "provider_info": info}
