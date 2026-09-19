from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

BODY_STATUSES = ("pending", "complete", "summary_only", "paywalled", "external_link", "unavailable")
RUN_STATUSES = (
    "queued",
    "running",
    "waiting_login",
    "paused",
    "succeeded",
    "partial_failed",
    "failed",
    "cancelled",
    "interrupted",
)
ACTIVE_RUN_SQL = "status IN ('queued', 'running', 'waiting_login', 'paused', 'interrupted')"


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Identity:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class Source(Identity, Base):
    __tablename__ = "sources"
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    permission_config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    config_version: Mapped[int] = mapped_column(Integer, server_default="1")
    __table_args__ = (CheckConstraint("config_version > 0", name="config_version"),)


class Collector(Identity, Base):
    __tablename__ = "collectors"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    entry_url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    interval_seconds: Mapped[int] = mapped_column(Integer)
    config_version: Mapped[int] = mapped_column(Integer, server_default="1")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("id", "source_id"),
        CheckConstraint("interval_seconds > 0", name="interval_positive"),
        CheckConstraint("config_version > 0", name="config_version"),
        Index("ix_collectors_source_id", "source_id"),
    )


class CrawlRun(Identity, Base):
    __tablename__ = "crawl_runs"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    collector_id: Mapped[UUID] = mapped_column()
    trigger_type: Mapped[str] = mapped_column(String(20))
    run_type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(24), server_default="queued")
    node_id: Mapped[str | None] = mapped_column(String(100))
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    range_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    range_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    statistics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    coverage_gaps: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    __table_args__ = (
        ForeignKeyConstraint(["collector_id", "source_id"], ["collectors.id", "collectors.source_id"]),
        UniqueConstraint("id", "collector_id", "source_id"),
        CheckConstraint(f"status IN {RUN_STATUSES}", name="status"),
        CheckConstraint("run_type IN ('realtime', 'backfill')", name="run_type"),
        CheckConstraint("trigger_type IN ('scheduled', 'manual', 'recovery')", name="trigger_type"),
        CheckConstraint("range_end_at IS NULL OR range_start_at < range_end_at", name="range_order"),
        CheckConstraint("finished_at IS NULL OR started_at <= finished_at", name="time_order"),
        Index(
            "uq_crawl_runs_active",
            "collector_id",
            "run_type",
            unique=True,
            postgresql_where=text(ACTIVE_RUN_SQL),
        ),
        Index("ix_crawl_runs_created_id", "created_at", "id"),
    )


class CrawlTask(Identity, Base):
    __tablename__ = "crawl_tasks"
    run_id: Mapped[UUID] = mapped_column()
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    collector_id: Mapped[UUID] = mapped_column()
    business_key: Mapped[str] = mapped_column(String(256))
    task_type: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(Text)
    request_params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempt: Mapped[int] = mapped_column(Integer, server_default="0")
    generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempt_history: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "collector_id", "source_id"],
            ["crawl_runs.id", "crawl_runs.collector_id", "crawl_runs.source_id"],
        ),
        UniqueConstraint("run_id", "business_key"),
        CheckConstraint("task_type IN ('list', 'detail')", name="task_type"),
        CheckConstraint(
            "status IN ('pending', 'queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')",
            name="status",
        ),
        CheckConstraint("attempt >= 0 AND generation >= 0", name="attempt_generation"),
        CheckConstraint("(lease_owner IS NULL) = (lease_expires_at IS NULL)", name="lease_pair"),
        CheckConstraint(
            "status != 'running' OR (lease_owner IS NOT NULL AND generation > 0)",
            name="running_lease",
        ),
        CheckConstraint("status != 'succeeded' OR completed_at IS NOT NULL", name="completion"),
        Index("ix_crawl_tasks_recovery", "status", "retry_at", "lease_expires_at"),
    )


class NewsItem(Identity, Base):
    __tablename__ = "news_items"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    source_item_id: Mapped[str | None] = mapped_column(String(256))
    canonical_url: Mapped[str] = mapped_column(Text)
    original_url: Mapped[str] = mapped_column(Text)
    current_revision_id: Mapped[UUID | None] = mapped_column()
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawal_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("source_id", "source_item_id"),
        UniqueConstraint("source_id", "canonical_url", name="uq_news_items_source_id_url"),
        UniqueConstraint("id", "source_id"),
        ForeignKeyConstraint(
            ["current_revision_id", "id"],
            ["news_revisions.id", "news_revisions.news_id"],
            name="fk_news_items_current_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_order"),
        CheckConstraint("withdrawn_at IS NULL OR withdrawal_reason IS NOT NULL", name="withdrawal_evidence"),
        Index("ix_news_items_seen_id", "first_seen_at", "id"),
        Index("ix_news_items_source_seen_id", "source_id", "first_seen_at", "id"),
    )


class NewsRevision(Identity, Base):
    __tablename__ = "news_revisions"
    news_id: Mapped[UUID] = mapped_column(ForeignKey("news_items.id"))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_status: Mapped[str] = mapped_column(String(24))
    source_tags: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    importance: Mapped[int | None] = mapped_column(Integer)
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_time_text: Mapped[str | None] = mapped_column(Text)
    source_timezone: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("id", "news_id"),
        CheckConstraint(f"body_status IN {BODY_STATUSES}", name="body_status"),
        CheckConstraint(
            "body_status != 'complete' OR length(trim(body_text)) > 0 AND body_text IS NOT NULL",
            name="complete_body",
        ),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'", name="content_hash"),
        Index("ix_news_revisions_news_observed_id", "news_id", "observed_at", "id"),
        Index("ix_news_revisions_published_at", "published_at"),
    )


class NewsOccurrence(Identity, Base):
    __tablename__ = "news_occurrences"
    news_id: Mapped[UUID] = mapped_column()
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    collector_id: Mapped[UUID] = mapped_column()
    run_id: Mapped[UUID] = mapped_column()
    discovered_url: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(["news_id", "source_id"], ["news_items.id", "news_items.source_id"]),
        ForeignKeyConstraint(
            ["run_id", "collector_id", "source_id"],
            ["crawl_runs.id", "crawl_runs.collector_id", "crawl_runs.source_id"],
        ),
        UniqueConstraint("news_id", "collector_id", "run_id"),
    )


class RawDocument(Identity, Base):
    __tablename__ = "raw_documents"
    revision_id: Mapped[UUID | None] = mapped_column(ForeignKey("news_revisions.id"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("crawl_tasks.id"))
    url: Mapped[str] = mapped_column(Text)
    response_status: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(100))
    content_text: Mapped[str] = mapped_column(Text)
    parser_version: Mapped[str] = mapped_column(String(100))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("response_status BETWEEN 100 AND 599", name="http_status"),
        Index("ix_raw_documents_revision_id", "revision_id"),
        Index("ix_raw_documents_task_id", "task_id"),
    )


class SourceChangeSequence(Base):
    __tablename__ = "source_change_sequences"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    last_change_id: Mapped[int] = mapped_column(BigInteger, server_default="0")
    __table_args__ = (CheckConstraint("last_change_id >= 0", name="nonnegative"),)


class NewsChange(Base):
    __tablename__ = "news_changes"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    change_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
        server_default=FetchedValue(),
    )
    news_id: Mapped[UUID] = mapped_column()
    revision_id: Mapped[UUID] = mapped_column()
    change_type: Mapped[str] = mapped_column(String(20))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(["news_id", "source_id"], ["news_items.id", "news_items.source_id"]),
        ForeignKeyConstraint(["revision_id", "news_id"], ["news_revisions.id", "news_revisions.news_id"]),
        CheckConstraint("change_id > 0", name="positive_change_id"),
        CheckConstraint("change_type IN ('created', 'revised', 'withdrawn')", name="change_type"),
    )


class NewsAnalysis(Identity, Base):
    __tablename__ = "news_analyses"
    news_id: Mapped[UUID] = mapped_column(ForeignKey("news_items.id"))
    revision_id: Mapped[UUID] = mapped_column(ForeignKey("news_revisions.id"))
    category: Mapped[str] = mapped_column(String(40))
    topics: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    sentiment: Mapped[str] = mapped_column(String(16), server_default="neutral")
    importance: Mapped[int] = mapped_column(Integer, server_default="3")
    classifier_version: Mapped[str] = mapped_column(String(40))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("news_id", name="uq_news_analyses_news_id"),
        CheckConstraint("importance BETWEEN 1 AND 5", name="importance_range"),
        CheckConstraint("sentiment IN ('positive', 'negative', 'neutral')", name="sentiment_value"),
        Index("ix_news_analyses_category_processed", "category", "processed_at"),
    )


class NewsCluster(Identity, Base):
    __tablename__ = "news_clusters"
    cluster_key: Mapped[str] = mapped_column(String(64), unique=True)
    category: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(Text)
    news_ids: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_news_clusters_category_updated", "category", "updated_at"),)


class NewsSummary(Identity, Base):
    __tablename__ = "news_summaries"
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("news_clusters.id"))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    key_points: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(24), server_default="succeeded")
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("cluster_id", name="uq_news_summaries_cluster_id"),
        CheckConstraint("status IN ('succeeded', 'fallback', 'failed')", name="summary_status"),
    )


class MarketInstrument(Identity, Base):
    __tablename__ = "market_instruments"
    provider: Mapped[str] = mapped_column(String(32), server_default="binance")
    symbol: Mapped[str] = mapped_column(String(32))
    asset_class: Mapped[str] = mapped_column(String(16))
    base_asset: Mapped[str] = mapped_column(String(32))
    quote_asset: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    history_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("provider", "symbol", name="uq_market_instruments_provider_symbol"),
        CheckConstraint("asset_class IN ('crypto', 'equity', 'etf')", name="asset_class"),
        Index("ix_market_instruments_enabled_symbol", "enabled", "symbol"),
    )


class MarketQuote(Identity, Base):
    __tablename__ = "market_quotes"
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("market_instruments.id"), unique=True)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    last: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("last > 0", name="positive_last"),)


class MarketBar(Identity, Base):
    __tablename__ = "market_bars"
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("market_instruments.id"))
    interval: Mapped[str] = mapped_column(String(8))
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    high: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    low: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    close: Mapped[Decimal] = mapped_column(Numeric(30, 12))
    volume: Mapped[Decimal] = mapped_column(Numeric(36, 12))
    trades: Mapped[int | None] = mapped_column(Integer)
    complete: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("instrument_id", "interval", "open_time", name="uq_market_bars_instrument_interval_open"),
        CheckConstraint("interval IN ('5m', '1h', '1d')", name="interval"),
        CheckConstraint("close_time > open_time", name="time_order"),
        CheckConstraint("high >= low AND high >= open AND high >= close", name="valid_high"),
        CheckConstraint("low <= open AND low <= close", name="valid_low"),
        CheckConstraint("volume >= 0", name="nonnegative_volume"),
        Index("ix_market_bars_instrument_interval_time", "instrument_id", "interval", "open_time"),
    )


class MarketSyncRun(Identity, Base):
    __tablename__ = "market_sync_runs"
    provider: Mapped[str] = mapped_column(String(32), server_default="binance")
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), server_default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    statistics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (
        CheckConstraint("mode IN ('backfill', 'stream', 'poll')", name="mode"),
        CheckConstraint("status IN ('running', 'succeeded', 'failed', 'interrupted')", name="status"),
        Index("ix_market_sync_runs_started", "started_at"),
    )


class AdminUser(Identity, Base):
    __tablename__ = "admin_users"
    singleton: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), unique=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("singleton", name="single_admin"),)


class AdminSession(Identity, Base):
    __tablename__ = "admin_sessions"
    admin_id: Mapped[UUID] = mapped_column(ForeignKey("admin_users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="expiry"),
        Index("ix_admin_sessions_admin_id", "admin_id"),
    )


class ControlCommand(Identity, Base):
    __tablename__ = "control_commands"
    admin_id: Mapped[UUID] = mapped_column(ForeignKey("admin_users.id"))
    collector_id: Mapped[UUID | None] = mapped_column(ForeignKey("collectors.id"))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("crawl_runs.id"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("crawl_tasks.id"))
    action: Mapped[str] = mapped_column(String(24))
    route: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("admin_id", "route", "idempotency_key"),
        CheckConstraint("action IN ('run', 'restart', 'pause', 'resume', 'cancel', 'retry')", name="action"),
        CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed')", name="status"),
        CheckConstraint("num_nonnulls(collector_id, run_id, task_id) = 1", name="one_target"),
        Index("ix_control_commands_status_created", "status", "created_at"),
    )


class AuditEvent(Identity, Base):
    __tablename__ = "audit_events"
    admin_id: Mapped[UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    action: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[UUID | None] = mapped_column()
    request_id: Mapped[str | None] = mapped_column(String(36))
    outcome: Mapped[str] = mapped_column(String(20))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_audit_events_created_id", "created_at", "id"),)
