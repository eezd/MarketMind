"""binance market instruments quotes bars and sync runs

Revision ID: 20260919_0004
Revises: 20260918_0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260919_0004"
down_revision = "20260918_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_instruments",
        sa.Column("provider", sa.String(length=32), server_default="binance", nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column("base_asset", sa.String(length=32), nullable=False),
        sa.Column("quote_asset", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("history_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("asset_class IN ('crypto', 'equity', 'etf')", name="ck_market_instruments_asset_class"),
        sa.PrimaryKeyConstraint("id", name="pk_market_instruments"),
        sa.UniqueConstraint("provider", "symbol", name="uq_market_instruments_provider_symbol"),
    )
    op.create_index(
        "ix_market_instruments_enabled_symbol",
        "market_instruments",
        ["enabled", "symbol"],
    )
    op.create_table(
        "market_quotes",
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("bid", sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column("ask", sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column("last", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("last > 0", name="ck_market_quotes_positive_last"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["market_instruments.id"],
            name="fk_market_quotes_instrument_id_market_instruments",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_market_quotes"),
        sa.UniqueConstraint("instrument_id", name="uq_market_quotes_instrument_id"),
    )
    op.create_table(
        "market_bars",
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("high", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("low", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("close", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("volume", sa.Numeric(precision=36, scale=12), nullable=False),
        sa.Column("trades", sa.Integer(), nullable=True),
        sa.Column("complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("interval IN ('5m', '1h', '1d')", name="ck_market_bars_interval"),
        sa.CheckConstraint("close_time > open_time", name="ck_market_bars_time_order"),
        sa.CheckConstraint(
            "high >= low AND high >= open AND high >= close",
            name="ck_market_bars_valid_high",
        ),
        sa.CheckConstraint("low <= open AND low <= close", name="ck_market_bars_valid_low"),
        sa.CheckConstraint("volume >= 0", name="ck_market_bars_nonnegative_volume"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["market_instruments.id"],
            name="fk_market_bars_instrument_id_market_instruments",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_market_bars"),
        sa.UniqueConstraint(
            "instrument_id",
            "interval",
            "open_time",
            name="uq_market_bars_instrument_interval_open",
        ),
    )
    op.create_index(
        "ix_market_bars_instrument_interval_time",
        "market_bars",
        ["instrument_id", "interval", "open_time"],
    )
    op.create_table(
        "market_sync_runs",
        sa.Column("provider", sa.String(length=32), server_default="binance", nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "statistics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("mode IN ('backfill', 'stream', 'poll')", name="ck_market_sync_runs_mode"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'interrupted')",
            name="ck_market_sync_runs_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_market_sync_runs"),
    )
    op.create_index("ix_market_sync_runs_started", "market_sync_runs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_market_sync_runs_started", table_name="market_sync_runs")
    op.drop_table("market_sync_runs")
    op.drop_index("ix_market_bars_instrument_interval_time", table_name="market_bars")
    op.drop_table("market_bars")
    op.drop_table("market_quotes")
    op.drop_index("ix_market_instruments_enabled_symbol", table_name="market_instruments")
    op.drop_table("market_instruments")
