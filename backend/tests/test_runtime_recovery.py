"""运行恢复回归：仅使用显式隔离 PostgreSQL，所有事务最终回滚。"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from marketmind.collection_types import Article, PageResult, SourceError
from marketmind.ingestion import persist_page
from marketmind.models import AdminUser, Collector, ControlCommand, CrawlRun, CrawlTask, NewsItem, RawDocument, Source
from marketmind.runtime import claim_batch, commands, now, record_exit, source_lock
from marketmind.runtime_models import SourceRuntime


@pytest.fixture
def database_url():
    url = os.environ.get("MM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MM_TEST_DATABASE_URL to an isolated migrated PostgreSQL database")
    parsed = make_url(url)
    database = parsed.database or ""
    if not database.startswith("marketmind_") or not database.endswith("_verify"):
        pytest.fail("Integration tests require a marketmind_*_verify database")
    return parsed.set(drivername="postgresql+psycopg")


@asynccontextmanager
async def isolated_runtime(database_url):
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection, connection.begin() as transaction:
            sessions = async_sessionmaker(connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
            async with sessions() as db, db.begin():
                source = Source(code=uuid4().hex, name="Recovery regression", enabled=True)
                db.add(source)
                await db.flush()
                collector = Collector(
                    source_id=source.id,
                    code=uuid4().hex,
                    name="Recovery regression",
                    entry_url="https://example.invalid/news",
                    enabled=True,
                    interval_seconds=60,
                )
                db.add(collector)
                # An already-running source can resume a paused run without resetting transport.
                db.add(SourceRuntime(source_id=source.id, status="running"))
                admin = await db.scalar(select(AdminUser))
                if admin is None:
                    admin = AdminUser(username=uuid4().hex, password_hash="unused-regression-password")
                    db.add(admin)
                await db.flush()
            try:
                yield sessions, collector, admin.id
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def new_run(collector, *, run_type="realtime", status="queued", max_pages=100):
    return CrawlRun(
        source_id=collector.source_id,
        collector_id=collector.id,
        trigger_type="manual",
        run_type=run_type,
        status=status,
        config_snapshot={"max_pages": max_pages},
    )


def new_command(run, admin_id, action, *, created_at=None):
    return ControlCommand(
        run_id=run.id,
        admin_id=admin_id,
        action=action,
        route=f"/runs/{run.id}/{action}",
        idempotency_key=uuid4().hex,
        request_hash="0" * 64,
        created_at=created_at or now(),
    )


async def finish_claimed_page(sessions, batch, page=None):
    run_id, source_generation, run_generation = batch
    async with sessions() as db, db.begin():
        task = (
            await db.scalars(select(CrawlTask).where(CrawlTask.run_id == run_id, CrawlTask.status == "queued"))
        ).one()
        task.status = "running"
        task.generation += 1
        task.lease_owner = "regression-worker"
        task.lease_expires_at = now() + timedelta(minutes=1)
        next_page = int(task.request_params["page"]) + 1
        cursor = f"cursor-{next_page}"
    async with sessions() as db:
        await persist_page(
            db,
            task.id,
            task.generation,
            page
            or PageResult(
                articles=[],
                next_cursor=cursor,
                raw_text="[]",
                response_status=200,
                content_type="application/json",
                fetched_at=now(),
                parser_version="regression",
            ),
            source_generation=source_generation,
            run_generation=run_generation,
        )


def test_conflicting_recovery_fails_without_stopping_later_commands(database_url):
    async def scenario():
        async with isolated_runtime(database_url) as (sessions, collector, admin_id):
            async with sessions() as db, db.begin():
                old = new_run(collector, status="partial_failed")
                active = new_run(collector)
                db.add_all([old, active])
                await db.flush()
                failed_task = CrawlTask(
                    run_id=old.id,
                    source_id=collector.source_id,
                    collector_id=collector.id,
                    business_key="page:0",
                    task_type="list",
                    url=collector.entry_url,
                    status="failed",
                )
                resume = new_command(old, admin_id, "resume")
                pause = new_command(active, admin_id, "pause", created_at=resume.created_at + timedelta(seconds=1))
                db.add_all([failed_task, resume, pause])
            await commands(sessions, collector.source_id)
            async with sessions() as db:
                rejected = await db.get(ControlCommand, resume.id)
                assert rejected.status == "failed"
                assert rejected.result["error"] == "active_run_conflict"
                assert (await db.get(CrawlRun, old.id)).status == "partial_failed"
                assert (await db.get(CrawlTask, failed_task.id)).status == "failed"
                assert (await db.get(ControlCommand, pause.id)).status == "succeeded"
                assert (await db.get(CrawlRun, active.id)).status == "paused"

    asyncio.run(scenario())


def test_resume_rejects_run_with_only_completed_tasks(database_url):
    async def scenario():
        async with isolated_runtime(database_url) as (sessions, collector, admin_id):
            async with sessions() as db, db.begin():
                run = new_run(collector, status="partial_failed")
                db.add(run)
                await db.flush()
                db.add(
                    CrawlTask(
                        run_id=run.id,
                        source_id=collector.source_id,
                        collector_id=collector.id,
                        business_key="page:0",
                        task_type="list",
                        url=collector.entry_url,
                        status="succeeded",
                        completed_at=now(),
                    )
                )
                resume = new_command(run, admin_id, "resume")
                db.add(resume)
            await commands(sessions, collector.source_id)
            async with sessions() as db:
                rejected = await db.get(ControlCommand, resume.id)
                assert rejected.status == "failed"
                assert rejected.result["error"] == "no_resumable_task"
                assert (await db.get(CrawlRun, run.id)).status == "partial_failed"
            assert await claim_batch(sessions, collector.source_id, "regression-worker", "regression-node") is None

    asyncio.run(scenario())


def test_backfill_keeps_successor_and_grants_fresh_budget_on_resume(database_url):
    async def scenario():
        async with isolated_runtime(database_url) as (sessions, collector, admin_id):
            async with sessions() as db, db.begin():
                run = new_run(collector, run_type="backfill", max_pages=2)
                db.add(run)
            for batch_number in range(2):
                for page_in_batch in range(2):
                    batch = await claim_batch(sessions, collector.source_id, "regression-worker", "regression-node")
                    assert batch is not None
                    assert batch[0] == run.id
                    await finish_claimed_page(sessions, batch)
                    async with sessions() as db:
                        current = await db.get(CrawlRun, run.id)
                        assert current.status == ("running" if page_in_batch == 0 else "paused")
                async with sessions() as db:
                    pending = (
                        await db.scalars(
                            select(CrawlTask).where(CrawlTask.run_id == run.id, CrawlTask.status == "pending")
                        )
                    ).one()
                    next_page = (batch_number + 1) * 2
                    assert pending.request_params == {"page": next_page, "cursor": f"cursor-{next_page}"}
                assert await claim_batch(sessions, collector.source_id, "regression-worker", "regression-node") is None
                if batch_number == 0:
                    async with sessions() as db, db.begin():
                        resume = new_command(run, admin_id, "resume")
                        db.add(resume)
                    await commands(sessions, collector.source_id)
                    async with sessions() as db:
                        assert (await db.get(ControlCommand, resume.id)).status == "succeeded"

    asyncio.run(scenario())


@pytest.mark.parametrize("duplicated_lane", ["realtime", "backfill"])
def test_three_continuously_ready_runs_make_progress_across_lane_switches(database_url, duplicated_lane):
    async def scenario():
        async with isolated_runtime(database_url) as (sessions, collector, _):
            async with sessions() as db, db.begin():
                other_collector = Collector(
                    source_id=collector.source_id,
                    code=uuid4().hex,
                    name="Second ready collector",
                    entry_url="https://example.invalid/other",
                    enabled=True,
                    interval_seconds=60,
                )
                db.add(other_collector)
                await db.flush()
                realtime = new_run(collector)
                backfill = new_run(collector, run_type="backfill")
                other = new_run(other_collector, run_type=duplicated_lane)
                for offset, run in enumerate((realtime, backfill, other)):
                    run.created_at = now() - timedelta(minutes=3 - offset)
                db.add_all([realtime, backfill, other])
            claimed = []
            for iteration in range(8):
                batch = await claim_batch(sessions, collector.source_id, f"worker-{iteration}", "regression-node")
                assert batch is not None
                claimed.append(batch[0])
                await finish_claimed_page(sessions, batch)
                await record_exit(sessions, collector.source_id, batch, 0)
            lanes = {run.id: run.run_type for run in (realtime, backfill, other)}
            assert [lanes[run_id] for run_id in claimed] == ["realtime", "backfill"] * 4
            async with sessions() as db:
                for run in (realtime, backfill, other):
                    completed = (
                        await db.scalars(
                            select(CrawlTask.business_key).where(
                                CrawlTask.run_id == run.id, CrawlTask.status == "succeeded"
                            )
                        )
                    ).all()
                    expected_pages = 2 if run.run_type == duplicated_lane else 4
                    assert set(completed) == {f"page:{page}" for page in range(expected_pages)}

    asyncio.run(scenario())


def test_successful_retry_clears_current_error_state(database_url):
    async def scenario():
        async with isolated_runtime(database_url) as (sessions, collector, _):
            async with sessions() as db, db.begin():
                run = new_run(collector)
                db.add(run)
            batch = await claim_batch(sessions, collector.source_id, "regression-worker", "regression-node", run.id)
            async with sessions() as db, db.begin():
                task = (
                    await db.scalars(select(CrawlTask).where(CrawlTask.run_id == run.id, CrawlTask.status == "queued"))
                ).one()
                task.error_code = "response_timeout"
                task.error_details = {"message": "previous attempt failed"}
                task.retry_at = now() - timedelta(seconds=1)
            await finish_claimed_page(sessions, batch)
            async with sessions() as db:
                task = (
                    await db.scalars(
                        select(CrawlTask).where(CrawlTask.run_id == run.id, CrawlTask.status == "succeeded")
                    )
                ).one()
                assert task.status == "succeeded"
                assert task.error_code is None
                assert task.error_details is None
                assert task.retry_at is None

    asyncio.run(scenario())


def test_coverage_boundary_commits_articles_without_blocking_realtime(database_url):
    async def scenario():
        async with isolated_runtime(database_url) as (sessions, collector, _):
            async with sessions() as db, db.begin():
                backfill = new_run(collector, run_type="backfill")
                realtime = new_run(collector)
                db.add_all([backfill, realtime])
            batch = await claim_batch(
                sessions, collector.source_id, "regression-worker", "regression-node", backfill.id
            )
            await finish_claimed_page(
                sessions,
                batch,
                PageResult(
                    articles=[
                        Article(
                            source_item_id="boundary-item",
                            canonical_url="https://example.invalid/detail/boundary",
                            original_url="https://example.invalid/detail/boundary",
                            body_text="Retained boundary page article",
                            body_status="complete",
                        )
                    ],
                    coverage_gap={"reason": "cls_replay_limit", "replay_page": 50},
                    raw_text='{"articles":[{"id":"boundary-item"}]}',
                    response_status=200,
                    content_type="application/json",
                    fetched_at=now(),
                    parser_version="regression",
                ),
            )
            await record_exit(sessions, collector.source_id, batch, 0)
            async with sessions() as db:
                run = await db.get(CrawlRun, backfill.id)
                assert run.status == "partial_failed"
                assert run.finished_at is not None
                assert run.coverage_gaps == [{"reason": "cls_replay_limit", "replay_page": 50, "page": 1}]
                item = (await db.scalars(select(NewsItem).where(NewsItem.source_id == collector.source_id))).one()
                assert item.source_item_id == "boundary-item"
                documents = (
                    await db.scalars(select(RawDocument).join(CrawlTask).where(CrawlTask.run_id == backfill.id))
                ).all()
                assert [document.content_text for document in documents] == ['{"articles":[{"id":"boundary-item"}]}']
                tasks = (await db.scalars(select(CrawlTask).where(CrawlTask.run_id == backfill.id))).all()
                assert [task.status for task in tasks] == ["succeeded"]
                assert (await db.get(Collector, collector.id)).last_success_at is None
            next_batch = await claim_batch(sessions, collector.source_id, "regression-worker", "regression-node")
            assert next_batch[0] == realtime.id
            await finish_claimed_page(sessions, next_batch)
            async with sessions() as db:
                assert (await db.get(CrawlRun, realtime.id)).checkpoint["page"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["cls_replay_limit", "parse_error"])
def test_legacy_boundary_ends_only_run_but_structure_failure_protects_source(database_url, monkeypatch, code):
    from marketmind import alerts
    from marketmind.scrapy_worker import LedgerSpider

    async def no_alert(*args, **kwargs):
        pass

    monkeypatch.setattr(alerts, "emit_alert", no_alert)

    async def scenario():
        async with isolated_runtime(database_url) as (sessions, collector, _):
            async with sessions() as db, db.begin():
                backfill = new_run(collector, run_type="backfill")
                realtime = new_run(collector)
                db.add_all([backfill, realtime])
            batch = await claim_batch(
                sessions, collector.source_id, "regression-worker", "regression-node", backfill.id
            )
            async with sessions() as db, db.begin():
                task = (await db.scalars(select(CrawlTask).where(CrawlTask.run_id == backfill.id))).one()
                task.status = "running"
                task.generation += 1
                task.lease_owner = "regression-worker"
                task.lease_expires_at = now() + timedelta(minutes=1)
                task.request_params = {"page": 50, "cursor": "legacy-boundary-cursor"}
            spider = SimpleNamespace(
                sessions=sessions,
                source_id=collector.source_id,
                run_id=backfill.id,
                source_generation=batch[1],
                run_generation=batch[2],
                handled_failure=False,
            )
            failure = SimpleNamespace(
                check=lambda *args: False,
                value=SourceError(code, "Regression failure"),
                request=SimpleNamespace(meta={"task_id": str(task.id), "task_generation": task.generation}),
            )
            await LedgerSpider.failed(spider, failure)
            await record_exit(sessions, collector.source_id, batch, 0)
            async with sessions() as db:
                run = await db.get(CrawlRun, backfill.id)
                runtime = await db.scalar(select(SourceRuntime).where(SourceRuntime.source_id == collector.source_id))
                assert run.status == ("partial_failed" if code == "cls_replay_limit" else "paused")
                assert runtime.status == ("idle" if code == "cls_replay_limit" else "paused")
                documents = (
                    await db.scalars(select(RawDocument).join(CrawlTask).where(CrawlTask.run_id == backfill.id))
                ).all()
                assert documents == []
                if code == "cls_replay_limit":
                    assert run.coverage_gaps == [{"reason": code, "cursor": "legacy-boundary-cursor"}]
                    assert (await db.get(CrawlTask, task.id)).status == "failed"
            next_batch = await claim_batch(sessions, collector.source_id, "regression-worker", "regression-node")
            if code == "cls_replay_limit":
                assert next_batch[0] == realtime.id
                await finish_claimed_page(sessions, next_batch)
            else:
                assert next_batch is None

    asyncio.run(scenario())


def test_source_lock_cleanup_preserves_original_failure_when_database_is_unavailable():
    class Connection:
        closed = False
        invalidated = False
        closed_finally = False

        async def scalar(self, *args, **kwargs):
            return True

        async def commit(self):
            pass

        async def rollback(self):
            raise SQLAlchemyError("database unavailable")

        async def invalidate(self):
            self.invalidated = True

        async def close(self):
            self.closed_finally = True

    connection = Connection()
    engine = SimpleNamespace(connect=lambda: asyncio.sleep(0, result=connection))

    async def scenario():
        with pytest.raises(RuntimeError, match="primary worker failure"):
            async with source_lock(engine, uuid4()):
                raise RuntimeError("primary worker failure")
        assert connection.invalidated
        assert connection.closed_finally

    asyncio.run(scenario())
