"""phase 2 news analysis and summaries

Revision ID: 20260918_0003
Revises: 20260916_0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260918_0003"
down_revision = "20260916_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "news_analyses",
        sa.Column("news_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column(
            "topics", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("sentiment", sa.String(length=16), server_default="neutral", nullable=False),
        sa.Column("importance", sa.Integer(), server_default="3", nullable=False),
        sa.Column("classifier_version", sa.String(length=40), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("importance BETWEEN 1 AND 5", name="ck_news_analyses_importance_range"),
        sa.CheckConstraint("sentiment IN ('positive', 'negative', 'neutral')", name="ck_news_analyses_sentiment_value"),
        sa.ForeignKeyConstraint(["news_id"], ["news_items.id"], name="fk_news_analyses_news_id_news_items"),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["news_revisions.id"], name="fk_news_analyses_revision_id_news_revisions"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_news_analyses"),
        sa.UniqueConstraint("news_id", name="uq_news_analyses_news_id"),
    )
    op.create_index("ix_news_analyses_category_processed", "news_analyses", ["category", "processed_at"])
    op.create_table(
        "news_clusters",
        sa.Column("cluster_key", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "news_ids", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("first_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_news_clusters"),
        sa.UniqueConstraint("cluster_key", name="uq_news_clusters_cluster_key"),
    )
    op.create_index("ix_news_clusters_category_updated", "news_clusters", ["category", "updated_at"])
    op.create_table(
        "news_summaries",
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "key_points", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="succeeded", nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("status IN ('succeeded', 'fallback', 'failed')", name="ck_news_summaries_status"),
        sa.ForeignKeyConstraint(
            ["cluster_id"], ["news_clusters.id"], name="fk_news_summaries_cluster_id_news_clusters"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_news_summaries"),
        sa.UniqueConstraint("cluster_id", name="uq_news_summaries_cluster_id"),
    )


def downgrade() -> None:
    op.drop_table("news_summaries")
    op.drop_index("ix_news_clusters_category_updated", table_name="news_clusters")
    op.drop_table("news_clusters")
    op.drop_index("ix_news_analyses_category_processed", table_name="news_analyses")
    op.drop_table("news_analyses")
