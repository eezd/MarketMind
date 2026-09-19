"""Persistent source supervision; independent of individual collection runs."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from marketmind.models import Base, Identity


class SourceRuntime(Identity, Base):
    __tablename__ = "source_runtimes"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), unique=True)
    status: Mapped[str] = mapped_column(String(24), server_default="idle")
    generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    owner: Mapped[str | None] = mapped_column(String(100))
    node_id: Mapped[str | None] = mapped_column(String(100))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restart_history: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    restart_pending: Mapped[bool] = mapped_column(server_default=text("false"))
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(Text)
    last_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("crawl_runs.id"))
    last_run_type: Mapped[str | None] = mapped_column(String(20))
    recovery_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
