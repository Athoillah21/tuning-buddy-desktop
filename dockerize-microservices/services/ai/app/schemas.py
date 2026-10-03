"""
Request/response schemas for the AI service API.
"""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

ProviderType = Literal["gemini", "openai_compatible", "anthropic"]


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider_type: ProviderType
    model: str = Field(min_length=1, max_length=200)
    base_url: Optional[str] = Field(default=None, max_length=500)
    api_key: Optional[str] = None
    priority: int = Field(default=100, ge=0)
    enabled: bool = True


class ProviderUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    provider_type: Optional[ProviderType] = None
    model: Optional[str] = Field(default=None, min_length=1, max_length=200)
    base_url: Optional[str] = Field(default=None, max_length=500)
    # Blank/omitted keeps the stored key; set clear_api_key to remove it
    api_key: Optional[str] = None
    clear_api_key: bool = False
    priority: Optional[int] = Field(default=None, ge=0)
    enabled: Optional[bool] = None


class ProviderTest(BaseModel):
    """Unsaved provider config to health-check. provider_id lets an edit form reuse the stored key."""
    provider_type: ProviderType
    model: str = Field(min_length=1, max_length=200)
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    provider_id: Optional[int] = None


class ProviderOut(BaseModel):
    id: int
    name: str
    provider_type: str
    provider_label: str
    base_url: Optional[str]
    model: str
    api_key_masked: str
    has_api_key: bool
    priority: int
    enabled: bool
    last_check_status: str
    last_check_message: Optional[str]
    last_check_latency_ms: Optional[float]
    last_checked_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class CheckOut(BaseModel):
    healthy: bool
    message: str
    latency_ms: Optional[float] = None
    kind: Optional[str] = None


class RecommendationsIn(BaseModel):
    query: str
    plan: Any = None
    execution_time: float = 0
    issues: list[Any] = []
    table_info: Optional[dict[str, Any]] = None


class SeqScanFixIn(BaseModel):
    query: str
    previous_recommendation: dict[str, Any]
    tested_plan: Any = None
    current_table_info: Optional[dict[str, Any]] = None
