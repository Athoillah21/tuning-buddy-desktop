"""
Database models for the AI service.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AIProvider(Base):
    """
    A configured AI provider. Providers are tried in priority order (lowest first),
    healthy ones before unhealthy ones.
    """
    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    provider_type: Mapped[str] = mapped_column(String(30))
    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    model: Mapped[str] = mapped_column(String(200))
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Result of the most recent health check (or runtime failure)
    last_check_status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_check_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_check_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    def reset_check(self) -> None:
        self.last_check_status = "unknown"
        self.last_check_message = None
        self.last_check_latency_ms = None
        self.last_checked_at = None
