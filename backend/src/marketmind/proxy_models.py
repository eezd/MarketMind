"""Proxy inventory, source-scoped health and durable check jobs."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from marketmind.models import Base, Identity


class ProxyEndpoint(Identity, Base):
    __tablename__ = "proxy_endpoints"
    name: Mapped[str] = mapped_column(String(100))
    scheme: Mapped[str] = mapped_column(String(10), default="http")
    host: Mapped[str] = mapped_column(String(253))
    port: Mapped[int] = mapped_column(Integer)
    credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint("scheme = 'http'", name="scheme"),
        CheckConstraint("port BETWEEN 1 AND 65535", name="port"),
        Index("ix_proxy_endpoints_created_id", "created_at", "id"),
    )


class ProxySourceHealth(Base):
    __tablename__ = "proxy_source_health"
    proxy_id: Mapped[UUID] = mapped_column(ForeignKey("proxy_endpoints.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (
        CheckConstraint("status IN ('unknown', 'healthy', 'cooling_down', 'unavailable', 'disabled')", name="status"),
    )


class SourceEgressState(Base):
    __tablename__ = "source_egress_states"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    config_version: Mapped[int] = mapped_column(Integer)
    proxy_id: Mapped[UUID | None] = mapped_column(ForeignKey("proxy_endpoints.id"))
    egress_id: Mapped[str] = mapped_column(String(64), default="direct")
    reason: Mapped[str] = mapped_column(String(80))
    fallback_direct: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProxyLease(Identity, Base):
    __tablename__ = "proxy_leases"
    proxy_id: Mapped[UUID] = mapped_column(ForeignKey("proxy_endpoints.id", ondelete="CASCADE"))
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_proxy_leases_proxy_expiry", "proxy_id", "expires_at"),)


class ProxyCheck(Identity, Base):
    __tablename__ = "proxy_checks"
    proxy_id: Mapped[UUID] = mapped_column(ForeignKey("proxy_endpoints.id"))
    admin_id: Mapped[UUID] = mapped_column(ForeignKey("admin_users.id"))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("admin_id", "proxy_id", "idempotency_key"),
        CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed')", name="status"),
        Index("ix_proxy_checks_status_created", "status", "created_at"),
    )
