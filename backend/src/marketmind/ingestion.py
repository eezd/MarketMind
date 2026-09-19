"""短事务入库：来源内串行身份对齐，新闻/修订/变更/任务完成一起提交。"""

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from marketmind.collection_types import PageResult
from marketmind.models import (
    CrawlRun,
    CrawlTask,
    NewsChange,
    NewsItem,
    NewsOccurrence,
    NewsRevision,
    RawDocument,
    Source,
)


async def persist_page(
    db: AsyncSession,
    task_id: UUID,
    generation: int,
    page: PageResult,
    *,
    source_generation: int | None = None,
    run_generation: int | None = None,
) -> dict[str, int]:
    counts = {"created": 0, "revised": 0, "duplicate": 0, "access_limited": 0}
    async with db.begin():
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        identity = await db.get(CrawlTask, task_id)
        if identity is None:
            raise ValueError("Task no longer exists")
        await db.scalar(select(Source).where(Source.id == identity.source_id).with_for_update())
        if source_generation is not None:
            from marketmind.runtime_models import SourceRuntime

            runtime = await db.scalar(
                select(SourceRuntime).where(SourceRuntime.source_id == identity.source_id).with_for_update()
            )
            if (
                runtime is None
                or runtime.generation != source_generation
                or runtime.status != "running"
                or runtime.lease_expires_at is None
                or runtime.lease_expires_at <= datetime.now(UTC)
            ):
                raise ValueError("Source lease is no longer current")
        run = await db.scalar(select(CrawlRun).where(CrawlRun.id == identity.run_id).with_for_update())
        if run is None or run.status != "running":
            raise ValueError("Run is no longer running")
        if run_generation is not None and run.checkpoint.get("generation") != run_generation:
            raise ValueError("Run generation is no longer current")
        task = await db.scalar(
            select(CrawlTask).where(CrawlTask.id == task_id).with_for_update().execution_options(populate_existing=True)
        )
        now = datetime.now(UTC)
        if task is None or task.status != "running" or task.generation != generation:
            raise ValueError("Task lease is no longer current")
        if task.lease_expires_at is None or task.lease_expires_at <= now:
            raise ValueError("Task lease expired")
        # 相同来源的身份对齐共用短事务锁，不跨来源加锁，不在锁内发网络请求。
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:source, 0))"), {"source": str(task.source_id)}
        )
        for article in page.articles:
            query = select(NewsItem).where(NewsItem.source_id == task.source_id)
            by_id = (
                await db.scalar(query.where(NewsItem.source_item_id == article.source_item_id))
                if article.source_item_id
                else None
            )
            by_url = await db.scalar(query.where(NewsItem.canonical_url == article.canonical_url))
            if by_id is not None and by_url is not None and by_id.id != by_url.id:
                raise ValueError("Source identity conflicts with an existing canonical URL")
            item = by_id or by_url
            created = item is None
            if item is None:
                item = NewsItem(
                    source_id=task.source_id,
                    source_item_id=article.source_item_id,
                    canonical_url=article.canonical_url,
                    original_url=article.original_url,
                    first_seen_at=page.fetched_at,
                    last_seen_at=page.fetched_at,
                )
                db.add(item)
                await db.flush()
            else:
                if item.source_item_id is None and article.source_item_id:
                    item.source_item_id = article.source_item_id
                item.last_seen_at = max(item.last_seen_at, page.fetched_at)
            previous = await db.get(NewsRevision, item.current_revision_id) if item.current_revision_id else None
            values = article.model_dump(mode="json", exclude={"source_item_id", "canonical_url", "original_url"})
            import json

            digest = hashlib.sha256(
                json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
            if previous and previous.body_status == "complete" and article.body_status != "complete":
                counts["access_limited"] += 1
                revision = previous
            elif previous and previous.content_hash == digest:
                counts["duplicate"] += 1
                revision = previous
            else:
                revision = NewsRevision(
                    news_id=item.id,
                    content_hash=digest,
                    observed_at=page.fetched_at,
                    **article.model_dump(exclude={"source_item_id", "canonical_url", "original_url"}),
                )
                db.add(revision)
                await db.flush()
                item.current_revision_id = revision.id
                change_type = "created" if created else "revised"
                db.add(
                    NewsChange(
                        source_id=task.source_id, news_id=item.id, revision_id=revision.id, change_type=change_type
                    )
                )
                counts[change_type] += 1
            occurrence = await db.scalar(
                select(NewsOccurrence).where(
                    NewsOccurrence.news_id == item.id,
                    NewsOccurrence.collector_id == task.collector_id,
                    NewsOccurrence.run_id == task.run_id,
                )
            )
            if occurrence is None:
                db.add(
                    NewsOccurrence(
                        news_id=item.id,
                        source_id=task.source_id,
                        collector_id=task.collector_id,
                        run_id=task.run_id,
                        discovered_url=article.original_url,
                        observed_at=page.fetched_at,
                    )
                )
        db.add(
            RawDocument(
                task_id=task.id,
                url=task.url,
                response_status=page.response_status,
                content_type=page.content_type,
                content_text=page.raw_text,
                parser_version=page.parser_version,
                fetched_at=page.fetched_at,
            )
        )
        task.status = "succeeded"
        task.completed_at = now
        task.lease_owner = None
        task.lease_expires_at = None
        task.error_code = None
        task.error_details = None
        task.retry_at = None
        # Discovery, progress and completion share the news transaction: Redis is reconstructible.
        number = int(task.request_params.get("page", 0)) + 1
        previous_cursor = task.request_params.get("cursor")
        checkpoint = {**run.checkpoint, "cursor": page.next_cursor, "page": number}
        run.checkpoint = checkpoint
        run.statistics = {key: int(run.statistics.get(key, 0)) + value for key, value in counts.items()}
        run.heartbeat_at = now
        finished = page.exhausted
        if page.coverage_gap is not None:
            run.status = "partial_failed"
            run.coverage_gaps = [*run.coverage_gaps, {**page.coverage_gap, "page": number}]
            finished = True
        if not finished and (not page.next_cursor or page.next_cursor == previous_cursor):
            run.status = "partial_failed"
            run.coverage_gaps = [*run.coverage_gaps, {"reason": "pagination_unavailable", "page": number}]
            finished = True
        if not finished and run.run_type == "realtime" and number >= int(run.config_snapshot.get("max_pages", 1)):
            run.coverage_gaps = [*run.coverage_gaps, {"reason": "page_limit", "cursor": page.next_cursor}]
            finished = True
        if finished:
            if run.status == "running":
                run.status = "succeeded"
            run.finished_at = now
            if run.status == "succeeded":
                from marketmind.models import Collector

                collector = await db.get(Collector, task.collector_id)
                collector.last_success_at = now
        else:
            db.add(
                CrawlTask(
                    run_id=run.id,
                    source_id=task.source_id,
                    collector_id=task.collector_id,
                    business_key=f"page:{number}",
                    task_type="list",
                    url=page.next_url or task.url,
                    request_params={"cursor": page.next_cursor, "page": number},
                    status="pending",
                )
            )
            if run.run_type == "backfill" and number - int(checkpoint.get("batch_start_page", 0)) >= int(
                run.config_snapshot.get("max_pages", 100)
            ):
                # Preserve the discovered next page, but require an explicit new batch budget.
                run.status = "paused"
                run.coverage_gaps = [
                    *run.coverage_gaps,
                    {
                        "reason": "batch_page_limit",
                        "page": number,
                        "cursor": page.next_cursor,
                    },
                ]
    return counts
