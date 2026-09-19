"""P1 durable schema and disabled source/collector catalog.

This revision is deliberately frozen, independent of live ORM model definitions.
"""

from alembic import op

revision = "20260914_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE sources (
 id UUID PRIMARY KEY, code VARCHAR(32) NOT NULL CONSTRAINT uq_sources_code UNIQUE, name VARCHAR(100) NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT false, permission_config JSONB NOT NULL DEFAULT '{}',
 config_version INTEGER NOT NULL DEFAULT 1,
 CONSTRAINT ck_sources_config_version CHECK (config_version > 0)
);
CREATE TABLE collectors (
 id UUID PRIMARY KEY, source_id UUID NOT NULL REFERENCES sources(id),
 code VARCHAR(40) NOT NULL CONSTRAINT uq_collectors_code UNIQUE, name VARCHAR(100) NOT NULL, entry_url TEXT NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT false, interval_seconds INTEGER NOT NULL,
 config_version INTEGER NOT NULL DEFAULT 1, config JSONB NOT NULL DEFAULT '{}',
 last_success_at TIMESTAMPTZ, next_run_at TIMESTAMPTZ,
 CONSTRAINT uq_collectors_id UNIQUE (id, source_id),
 CONSTRAINT ck_collectors_interval_positive CHECK (interval_seconds > 0),
 CONSTRAINT ck_collectors_config_version CHECK (config_version > 0)
);
CREATE INDEX ix_collectors_source_id ON collectors(source_id);
CREATE TABLE crawl_runs (
 id UUID PRIMARY KEY, source_id UUID NOT NULL REFERENCES sources(id), collector_id UUID NOT NULL,
 trigger_type VARCHAR(20) NOT NULL, run_type VARCHAR(20) NOT NULL,
 status VARCHAR(24) NOT NULL DEFAULT 'queued', node_id VARCHAR(100), config_snapshot JSONB NOT NULL,
 range_start_at TIMESTAMPTZ, range_end_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
 heartbeat_at TIMESTAMPTZ, checkpoint JSONB NOT NULL DEFAULT '{}',
 statistics JSONB NOT NULL DEFAULT '{}', coverage_gaps JSONB NOT NULL DEFAULT '[]',
 CONSTRAINT fk_crawl_runs_collector_id_collectors FOREIGN KEY (collector_id, source_id)
 REFERENCES collectors(id, source_id),
 CONSTRAINT uq_crawl_runs_id UNIQUE (id, collector_id, source_id),
 CONSTRAINT ck_crawl_runs_status CHECK (status IN ('queued','running','waiting_login','paused',
 'succeeded','partial_failed','failed','cancelled','interrupted')),
 CONSTRAINT ck_crawl_runs_run_type CHECK (run_type IN ('realtime','backfill')),
 CONSTRAINT ck_crawl_runs_trigger_type CHECK (trigger_type IN ('scheduled','manual','recovery')),
 CONSTRAINT ck_crawl_runs_range_order CHECK (range_end_at IS NULL OR range_start_at < range_end_at),
 CONSTRAINT ck_crawl_runs_time_order CHECK (finished_at IS NULL OR started_at <= finished_at)
);
CREATE UNIQUE INDEX uq_crawl_runs_active ON crawl_runs(collector_id, run_type)
 WHERE status IN ('queued','running','waiting_login','paused','interrupted');
CREATE INDEX ix_crawl_runs_created_id ON crawl_runs(created_at, id);
CREATE TABLE crawl_tasks (
 id UUID PRIMARY KEY, run_id UUID NOT NULL, source_id UUID NOT NULL REFERENCES sources(id),
 collector_id UUID NOT NULL, business_key VARCHAR(256) NOT NULL,
 task_type VARCHAR(20) NOT NULL, url TEXT NOT NULL, request_params JSONB NOT NULL DEFAULT '{}',
 status VARCHAR(20) NOT NULL DEFAULT 'pending', attempt INTEGER NOT NULL DEFAULT 0,
 generation BIGINT NOT NULL DEFAULT 0, lease_owner VARCHAR(100), lease_expires_at TIMESTAMPTZ,
 retry_at TIMESTAMPTZ, error_code VARCHAR(80), error_details JSONB,
 attempt_history JSONB NOT NULL DEFAULT '[]', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 completed_at TIMESTAMPTZ,
 CONSTRAINT fk_crawl_tasks_run_id_crawl_runs FOREIGN KEY (run_id, collector_id, source_id)
 REFERENCES crawl_runs(id, collector_id, source_id),
 CONSTRAINT uq_crawl_tasks_run_id UNIQUE (run_id, business_key),
 CONSTRAINT ck_crawl_tasks_task_type CHECK (task_type IN ('list','detail')),
 CONSTRAINT ck_crawl_tasks_status CHECK (status IN ('pending','queued','running','retry_wait',
 'succeeded','failed','cancelled')),
 CONSTRAINT ck_crawl_tasks_attempt_generation CHECK (attempt >= 0 AND generation >= 0),
 CONSTRAINT ck_crawl_tasks_lease_pair CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
 CONSTRAINT ck_crawl_tasks_running_lease CHECK
 (status != 'running' OR (lease_owner IS NOT NULL AND generation > 0)),
 CONSTRAINT ck_crawl_tasks_completion CHECK (status != 'succeeded' OR completed_at IS NOT NULL)
);
CREATE INDEX ix_crawl_tasks_recovery ON crawl_tasks(status, retry_at, lease_expires_at);
CREATE TABLE news_items (
 id UUID PRIMARY KEY, source_id UUID NOT NULL REFERENCES sources(id), source_item_id VARCHAR(256),
 canonical_url TEXT NOT NULL, original_url TEXT NOT NULL, current_revision_id UUID,
 first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 withdrawn_at TIMESTAMPTZ, withdrawal_reason TEXT,
 CONSTRAINT uq_news_items_source_id UNIQUE (source_id, source_item_id),
 CONSTRAINT uq_news_items_source_id_url UNIQUE (source_id, canonical_url),
 CONSTRAINT uq_news_items_id UNIQUE (id, source_id),
 CONSTRAINT ck_news_items_seen_order CHECK (last_seen_at >= first_seen_at),
 CONSTRAINT ck_news_items_withdrawal_evidence CHECK
 (withdrawn_at IS NULL OR withdrawal_reason IS NOT NULL)
);
CREATE INDEX ix_news_items_seen_id ON news_items(first_seen_at, id);
CREATE INDEX ix_news_items_source_seen_id ON news_items(source_id, first_seen_at, id);
CREATE TABLE news_revisions (
 id UUID PRIMARY KEY, news_id UUID NOT NULL REFERENCES news_items(id), title TEXT NOT NULL,
 summary TEXT, body_text TEXT, body_status VARCHAR(24) NOT NULL, source_tags JSONB NOT NULL DEFAULT '[]',
 importance INTEGER, author TEXT, published_at TIMESTAMPTZ, source_updated_at TIMESTAMPTZ,
 source_time_text TEXT, source_timezone VARCHAR(64), content_hash VARCHAR(64) NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT uq_news_revisions_id UNIQUE (id, news_id),
 CONSTRAINT ck_news_revisions_body_status CHECK
 (body_status IN ('pending','complete','summary_only','paywalled','external_link','unavailable')),
 CONSTRAINT ck_news_revisions_complete_body CHECK
 (body_status != 'complete' OR length(trim(body_text)) > 0 AND body_text IS NOT NULL),
 CONSTRAINT ck_news_revisions_content_hash CHECK (content_hash ~ '^[a-f0-9]{64}$')
);
CREATE INDEX ix_news_revisions_news_observed_id ON news_revisions(news_id, observed_at, id);
CREATE INDEX ix_news_revisions_published_at ON news_revisions(published_at);
ALTER TABLE news_items ADD CONSTRAINT fk_news_items_current_revision
 FOREIGN KEY (current_revision_id, id) REFERENCES news_revisions(id, news_id)
 DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE news_occurrences (
 id UUID PRIMARY KEY, news_id UUID NOT NULL, source_id UUID NOT NULL REFERENCES sources(id),
 collector_id UUID NOT NULL, run_id UUID NOT NULL, discovered_url TEXT NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT fk_news_occurrences_news_id_news_items FOREIGN KEY (news_id, source_id)
 REFERENCES news_items(id, source_id),
 CONSTRAINT fk_news_occurrences_run_id_crawl_runs FOREIGN KEY (run_id, collector_id, source_id)
 REFERENCES crawl_runs(id, collector_id, source_id),
 CONSTRAINT uq_news_occurrences_news_id UNIQUE (news_id, collector_id, run_id)
);
CREATE TABLE raw_documents (
 id UUID PRIMARY KEY, revision_id UUID REFERENCES news_revisions(id),
 task_id UUID NOT NULL REFERENCES crawl_tasks(id), url TEXT NOT NULL,
 response_status INTEGER NOT NULL, content_type VARCHAR(100) NOT NULL, content_text TEXT NOT NULL,
 parser_version VARCHAR(100) NOT NULL, fetched_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT ck_raw_documents_http_status CHECK (response_status BETWEEN 100 AND 599)
);
CREATE INDEX ix_raw_documents_revision_id ON raw_documents(revision_id);
CREATE INDEX ix_raw_documents_task_id ON raw_documents(task_id);
CREATE TABLE source_change_sequences (
 source_id UUID PRIMARY KEY REFERENCES sources(id), last_change_id BIGINT NOT NULL DEFAULT 0,
 CONSTRAINT ck_source_change_sequences_nonnegative CHECK (last_change_id >= 0)
);
CREATE TABLE news_changes (
 source_id UUID NOT NULL REFERENCES sources(id), change_id BIGINT NOT NULL,
 news_id UUID NOT NULL, revision_id UUID NOT NULL, change_type VARCHAR(20) NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (source_id, change_id),
 CONSTRAINT fk_news_changes_news_id_news_items FOREIGN KEY (news_id, source_id)
 REFERENCES news_items(id, source_id),
 CONSTRAINT fk_news_changes_revision_id_news_revisions FOREIGN KEY (revision_id, news_id)
 REFERENCES news_revisions(id, news_id),
 CONSTRAINT ck_news_changes_positive_change_id CHECK (change_id > 0),
 CONSTRAINT ck_news_changes_change_type CHECK (change_type IN ('created','revised','withdrawn'))
);
CREATE TABLE admin_users (
 id UUID PRIMARY KEY, singleton BOOLEAN NOT NULL DEFAULT true CONSTRAINT uq_admin_users_singleton UNIQUE,
 username VARCHAR(100) NOT NULL CONSTRAINT uq_admin_users_username UNIQUE, password_hash TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT ck_admin_users_single_admin CHECK (singleton)
);
CREATE TABLE admin_sessions (
 id UUID PRIMARY KEY, admin_id UUID NOT NULL REFERENCES admin_users(id),
 token_hash VARCHAR(64) NOT NULL CONSTRAINT uq_admin_sessions_token_hash UNIQUE, csrf_token VARCHAR(64) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL, revoked_at TIMESTAMPTZ,
 CONSTRAINT ck_admin_sessions_expiry CHECK (expires_at > created_at)
);
CREATE INDEX ix_admin_sessions_admin_id ON admin_sessions(admin_id);
CREATE TABLE control_commands (
 id UUID PRIMARY KEY, admin_id UUID NOT NULL REFERENCES admin_users(id),
 collector_id UUID REFERENCES collectors(id), run_id UUID REFERENCES crawl_runs(id),
 task_id UUID REFERENCES crawl_tasks(id), action VARCHAR(24) NOT NULL, route VARCHAR(200) NOT NULL,
 idempotency_key VARCHAR(200) NOT NULL, request_hash VARCHAR(64) NOT NULL,
 status VARCHAR(20) NOT NULL DEFAULT 'pending', result JSONB,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ,
 CONSTRAINT uq_control_commands_admin_id UNIQUE (admin_id, route, idempotency_key),
 CONSTRAINT ck_control_commands_action CHECK (action IN ('run','restart','pause','resume','cancel','retry')),
 CONSTRAINT ck_control_commands_status CHECK (status IN ('pending','running','succeeded','failed')),
 CONSTRAINT ck_control_commands_one_target CHECK (num_nonnulls(collector_id, run_id, task_id) = 1)
);
CREATE INDEX ix_control_commands_status_created ON control_commands(status, created_at);
CREATE TABLE audit_events (
 id UUID PRIMARY KEY, admin_id UUID REFERENCES admin_users(id), action VARCHAR(80) NOT NULL,
 target_type VARCHAR(40) NOT NULL, target_id UUID, request_id VARCHAR(36), outcome VARCHAR(20) NOT NULL,
 details JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_events_created_id ON audit_events(created_at, id);

CREATE FUNCTION marketmind_preserve_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'MarketMind history is immutable' USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER news_revisions_immutable BEFORE UPDATE OR DELETE ON news_revisions
 FOR EACH ROW EXECUTE FUNCTION marketmind_preserve_history();
CREATE TRIGGER raw_documents_immutable BEFORE UPDATE OR DELETE ON raw_documents
 FOR EACH ROW EXECUTE FUNCTION marketmind_preserve_history();
CREATE TRIGGER news_changes_immutable BEFORE UPDATE OR DELETE ON news_changes
 FOR EACH ROW EXECUTE FUNCTION marketmind_preserve_history();
CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
 FOR EACH ROW EXECUTE FUNCTION marketmind_preserve_history();

CREATE FUNCTION marketmind_allocate_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.change_id IS NOT NULL THEN
  RAISE EXCEPTION 'change_id is assigned by the source ledger' USING ERRCODE = '23514';
 END IF;
 INSERT INTO source_change_sequences(source_id) VALUES (NEW.source_id) ON CONFLICT DO NOTHING;
 -- This row lock is held until commit; unrelated sources acquire unrelated locks.
 UPDATE source_change_sequences SET last_change_id = last_change_id + 1
 WHERE source_id = NEW.source_id RETURNING last_change_id INTO NEW.change_id;
 RETURN NEW;
END;
$$;
CREATE TRIGGER news_changes_allocate BEFORE INSERT ON news_changes
 FOR EACH ROW EXECUTE FUNCTION marketmind_allocate_change();

CREATE FUNCTION marketmind_require_current_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS (SELECT 1 FROM news_items WHERE id = NEW.id AND current_revision_id IS NULL) THEN
  RAISE EXCEPTION 'News requires a current revision at commit' USING ERRCODE = '23514';
 END IF;
 RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER news_items_require_revision AFTER INSERT OR UPDATE ON news_items
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION marketmind_require_current_revision();

CREATE FUNCTION marketmind_protect_complete_body() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.current_revision_id IS DISTINCT FROM NEW.current_revision_id
 AND EXISTS (SELECT 1 FROM news_revisions WHERE id = OLD.current_revision_id AND body_status = 'complete')
 AND NOT EXISTS (SELECT 1 FROM news_revisions WHERE id = NEW.current_revision_id AND body_status = 'complete') THEN
  RAISE EXCEPTION 'Complete body cannot be replaced with incomplete content' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER news_items_protect_complete BEFORE UPDATE OF current_revision_id ON news_items
 FOR EACH ROW EXECUTE FUNCTION marketmind_protect_complete_body();

INSERT INTO sources (id,code,name) VALUES
 ('00000000-0000-0000-0000-000000000001','wscn','华尔街见闻'),
 ('00000000-0000-0000-0000-000000000002','cls','财联社'),
 ('00000000-0000-0000-0000-000000000003','jin10','金十数据');
INSERT INTO source_change_sequences(source_id) SELECT id FROM sources;
INSERT INTO collectors(id,source_id,code,name,entry_url,interval_seconds) VALUES
 ('10000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000001',
 'wscn_news','见闻全球新闻','https://wallstreetcn.com/news/global',300),
 ('10000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000001',
 'wscn_live','见闻全球快讯','https://wallstreetcn.com/live/global',60),
 ('10000000-0000-0000-0000-000000000003','00000000-0000-0000-0000-000000000002',
 'cls_depth','财联社深度','https://www.cls.cn/depth?id=1000',300),
 ('10000000-0000-0000-0000-000000000004','00000000-0000-0000-0000-000000000002',
 'cls_telegraph','财联社电报','https://www.cls.cn/telegraph',60),
 ('10000000-0000-0000-0000-000000000005','00000000-0000-0000-0000-000000000003',
 'jin10_live','金十快讯','https://www.jin10.com/',60),
 ('10000000-0000-0000-0000-000000000006','00000000-0000-0000-0000-000000000003',
 'jin10_news','金十资讯','https://xnews.jin10.com/',300);
""")


def downgrade() -> None:
    raise RuntimeError("P1 migration retains business history; automatic destructive downgrade refused")
