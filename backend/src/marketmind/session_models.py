"""Durable, private source-session and official-login state."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from marketmind.models import Base, Identity


class SourceSession(Identity, Base):
    __tablename__ = "source_sessions"

    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), unique=True)
    status: Mapped[str] = mapped_column(String(24), server_default="unauthenticated")
    generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    encrypted_state: Mapped[bytes | None] = mapped_column(LargeBinary)
    domains: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    egress_id: Mapped[str | None] = mapped_column(String(100))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("generation >= 0", name="generation"),
        CheckConstraint(
            "status IN ('unauthenticated', 'qr_pending', 'verifying', 'authenticated', 'expired', 'failed', 'revoked')",
            name="status",
        ),
    )


class LoginAttempt(Identity, Base):
    __tablename__ = "login_attempts"

    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    admin_id: Mapped[UUID] = mapped_column(ForeignKey("admin_users.id"))
    route: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), server_default="queued")
    encrypted_qr: Mapped[bytes | None] = mapped_column(LargeBinary)
    domains: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("admin_id", "route", "idempotency_key"),
        CheckConstraint("generation > 0", name="generation"),
        CheckConstraint("expires_at > created_at", name="expiry"),
        CheckConstraint(
            "status IN ('queued', 'qr_pending', 'verifying', 'authenticated', 'expired', "
            "'failed', 'cancelled', 'action_required')",
            name="status",
        ),
        Index("ix_login_attempts_status_created", "status", "created_at"),
    )
