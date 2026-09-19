"""Durable alert episodes and independently retried notification deliveries."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from marketmind.models import Base, Identity


class TelegramSettings(Identity, Base):
    __tablename__ = "telegram_settings"
    singleton: Mapped[bool] = mapped_column(Boolean, unique=True, server_default=text("true"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    chat_id: Mapped[str | None] = mapped_column(String(100))
    token_ciphertext: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("singleton", name="single_settings"),)


class AlertEvent(Identity, Base):
    __tablename__ = "alert_events"
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"))
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(String(300))
    related_alert_id: Mapped[UUID | None] = mapped_column(ForeignKey("alert_events.id"))
    occurrences: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('active', 'resolved')", name="status"),
        CheckConstraint("occurrences > 0", name="positive_occurrences"),
        Index("ix_alert_events_created_id", "created_at", "id"),
        Index(
            "uq_alert_events_source_active",
            "source_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'active' AND source_id IS NOT NULL"),
        ),
        Index(
            "uq_alert_events_system_active",
            "kind",
            unique=True,
            postgresql_where=text("status = 'active' AND source_id IS NULL"),
        ),
    )


class NotificationDelivery(Identity, Base):
    __tablename__ = "notification_deliveries"
    alert_id: Mapped[UUID | None] = mapped_column(ForeignKey("alert_events.id"), unique=True)
    admin_id: Mapped[UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    event_status: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    attempt_history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sending', 'retrying', 'sent', 'failed', 'disabled', 'not_configured')",
            name="status",
        ),
        CheckConstraint("event_status IN ('active', 'resolved', 'test')", name="event_status"),
        CheckConstraint("attempts >= 0 AND attempts <= 5", name="bounded_attempts"),
        Index("ix_notification_deliveries_due", "status", "next_attempt_at"),
        Index("uq_notification_test_key", "admin_id", "idempotency_key", unique=True),
    )
