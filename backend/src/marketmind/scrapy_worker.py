"""Finite Scrapy/Redis page worker using the official-protocol adapter download handler.

The queued HTTP(S) request identifies a real source entry. Its download handler performs
that entry's HTTP or browser protocol (including required detail requests), rather than
inventing a data URL or feeding pre-fetched items into Scrapy. PostgreSQL owns ACKs.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
from datetime import timedelta
from uuid import UUID

import scrapy
from scrapy import signals
from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import IgnoreRequest
from scrapy.http import TextResponse
from scrapy.utils.defer import deferred_from_coro
from sqlalchemy import select

from marketmind.collection_types import SourceError
from marketmind.ingestion import persist_page
from marketmind.models import Collector, CrawlRun, CrawlTask, Source
from marketmind.runtime import LEASE_SECONDS, database, now, runtime_row, source_lock


class OfficialProtocolHandler:
    """A Scrapy download handler, not an alternate scheduler or an in-memory fake queue."""

    def __init__(self, crawler):
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def download_request(self, request, spider=None):
        return deferred_from_coro(self.download(request, spider or self.crawler.spider))

    async def download(self, request, spider):
        from marketmind.proxy_runtime import source_transport

        if (
            request.meta.get("source_generation") != spider.source_generation
            or request.meta.get("run_generation") != spider.run_generation
        ):
            raise IgnoreRequest("Stale Redis request")
        task_id = UUID(request.meta["task_id"])
        async with spider.sessions() as db, db.begin():
            runtime = await runtime_row(db, spider.source_id)
            run = await db.scalar(select(CrawlRun).where(CrawlRun.id == spider.run_id).with_for_update())
            task = await db.scalar(select(CrawlTask).where(CrawlTask.id == task_id).with_for_update())
            if (
                runtime.generation != spider.source_generation
                or runtime.status != "running"
                or runtime.lease_expires_at is None
                or runtime.lease_expires_at <= now()
                or run.status != "running"
                or run.checkpoint.get("generation") != spider.run_generation
                or task is None
                or task.status not in ("pending", "queued", "retry_wait")
            ):
                raise IgnoreRequest("Fenced ledger request")
            if task.retry_at and task.retry_at > now():
                raise IgnoreRequest("Retry not due")
            task.generation += 1
            task.attempt += 1
            task.status = "running"
            task.lease_owner = f"worker:{spider.source_generation}"
            task.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)
            task.attempt_history = [
                *task.attempt_history,
                {"attempt": task.attempt, "generation": task.generation, "started_at": now().isoformat()},
            ]
            request.meta["task_generation"] = task.generation
            cursor = task.request_params.get("cursor")
            start, end = run.range_start_at, run.range_end_at
            code = run.config_snapshot["collector_code"]
        heartbeat = asyncio.create_task(spider.heartbeat(task_id, request.meta["task_generation"]))
        try:
            adapter = importlib.import_module(f"marketmind.adapters.{spider.source_code}")
            # Context spans ALL page/detail/browser traffic, retaining a sticky source egress.
            async with source_transport(spider.source_id):
                page = await asyncio.wait_for(adapter.fetch_page(code, cursor, start_at=start, end_at=end), 600)
            request.meta["page_result"] = page
            return TextResponse(
                url=request.url,
                status=page.response_status,
                body=page.raw_text.encode(),
                encoding="utf-8",
                request=request,
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass


class LedgerPipeline:
    async def process_item(self, item, spider):
        async with spider.sessions() as db:
            await persist_page(
                db,
                item["task_id"],
                item["generation"],
                item["page"],
                source_generation=spider.source_generation,
                run_generation=spider.run_generation,
            )
        spider.committed = True
        from marketmind.alerts import emit_alert

        await emit_alert(spider.source_id, "collection_failed", "来源采集已恢复", resolved=True)
        await emit_alert(spider.source_id, "recovery_exhausted", "来源采集已恢复", resolved=True)
        return item


class LedgerSpider(scrapy.Spider):
    name = "marketmind"

    def __init__(self, run_id, source_id, source_code, source_generation, run_generation, **kwargs):
        super().__init__(**kwargs)
        self.run_id, self.source_id = UUID(run_id), UUID(source_id)
        self.source_code = source_code
        self.source_generation, self.run_generation = int(source_generation), int(run_generation)
        self.engine_db, self.sessions = database()
        self.committed = False
        self.handled_failure = False
        self.lock = None

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.closed_async, signal=signals.spider_closed)
        return spider

    async def start(self):
        self.lock = source_lock(self.engine_db, self.source_id, worker=True)
        await self.lock.__aenter__()
        async with self.sessions() as db:
            tasks = (
                await db.scalars(
                    select(CrawlTask)
                    .where(
                        CrawlTask.run_id == self.run_id,
                        CrawlTask.status.in_(("pending", "queued", "retry_wait")),
                    )
                    .order_by(CrawlTask.created_at)
                    .limit(1)
                )
            ).all()
        for task in tasks:
            # dont_filter is intentional: the ledger, not Redis's lossy seen-set, defines completion.
            yield scrapy.Request(
                task.url,
                callback=self.parse,
                errback=self.failed,
                dont_filter=True,
                meta={
                    "task_id": str(task.id),
                    "source_generation": self.source_generation,
                    "run_generation": self.run_generation,
                    "download_timeout": 620,
                },
            )

    async def heartbeat(self, task_id, generation):
        while True:
            await asyncio.sleep(15)
            async with self.sessions() as db, db.begin():
                runtime = await runtime_row(db, self.source_id)
                run = await db.scalar(select(CrawlRun).where(CrawlRun.id == self.run_id).with_for_update())
                task = await db.scalar(select(CrawlTask).where(CrawlTask.id == task_id).with_for_update())
                if (
                    runtime.generation != self.source_generation
                    or runtime.lease_expires_at is None
                    or runtime.lease_expires_at <= now()
                    or run.status != "running"
                    or run.checkpoint.get("generation") != self.run_generation
                    or task.status != "running"
                    or task.generation != generation
                ):
                    return
                task.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)
                run.heartbeat_at = now()

    async def parse(self, response):
        yield {
            "task_id": UUID(response.meta["task_id"]),
            "generation": response.meta["task_generation"],
            "page": response.meta["page_result"],
        }

    async def failed(self, failure):
        if failure.check(IgnoreRequest):
            return
        code = failure.value.code if isinstance(failure.value, SourceError) else "request_failed"
        from marketmind.alerts import emit_alert

        async with self.sessions() as db, db.begin():
            runtime = await runtime_row(db, self.source_id)
            run = await db.scalar(select(CrawlRun).where(CrawlRun.id == self.run_id).with_for_update())
            task = await db.scalar(
                select(CrawlTask).where(CrawlTask.id == UUID(failure.request.meta["task_id"])).with_for_update()
            )
            if (
                runtime.generation != self.source_generation
                or run.status != "running"
                or run.checkpoint.get("generation") != self.run_generation
                or task.status != "running"
                or task.generation != failure.request.meta.get("task_generation")
            ):
                return
            task.lease_owner = None
            task.lease_expires_at = None
            task.error_code = code
            task.error_details = {"message": "采集失败，请检查来源访问状态"}
            if code == "cls_replay_limit":
                task.status = "failed"
                task.completed_at = now()
                run.status = "partial_failed"
                run.finished_at = now()
                run.coverage_gaps = [*run.coverage_gaps, {"reason": code, "cursor": task.request_params.get("cursor")}]
            elif code == "waiting_login":
                task.status = "pending"
                run.status = "waiting_login"
                runtime.status = "waiting_login"
            elif code in (
                "proxy_exhausted",
                "parse_error",
                "structure_changed",
                "challenge",
                "access_denied",
                "unsafe_target",
            ):
                task.status = "pending"
                run.status = "paused"
                runtime.status = "paused"
                runtime.pause_reason = code
            elif task.attempt - int(task.request_params.get("retry_base", 0)) < 4:
                task.status = "retry_wait"
                task.retry_at = now() + timedelta(seconds=min(300, 10 * 2 ** min(task.attempt, 5)))
                run.status = "queued"
            else:
                task.status = "failed"
                run.status = "partial_failed"
                run.finished_at = now()
                run.coverage_gaps = [*run.coverage_gaps, {"reason": code, "cursor": task.request_params.get("cursor")}]
            self.handled_failure = True
        kind = code if code in ("waiting_login", "proxy_exhausted") else "collection_failed"
        if code == "cls_replay_limit":
            kind = "coverage_gap"
        elif code == "rate_limit_unavailable":
            kind = "redis_unavailable"
        elif code in ("parse_error", "structure_changed"):
            kind = "parse_failed"
        await emit_alert(self.source_id, kind, "来源采集需要重试或人工处理")

    async def closed_async(self, spider, reason):
        if self.lock:
            await self.lock.__aexit__(None, None, None)
        await self.engine_db.dispose()


async def identity(run_id):
    engine, sessions = database()
    try:
        async with sessions() as db:
            run = await db.get(CrawlRun, run_id)
            if run is None:
                raise ValueError("Unknown run")
            source = await db.get(Source, run.source_id)
            collector = await db.get(Collector, run.collector_id)
            if source.code not in ("wscn", "cls", "jin10"):
                raise ValueError("Unsupported source")
            return source.id, source.code, collector.id, collector.code
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Scrapy-Redis finite page worker (executor-owned lease required)")
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--source-generation", type=int, required=True)
    parser.add_argument("--run-generation", type=int, required=True)
    args = parser.parse_args()
    source_id, source_code, collector_id, collector_code = asyncio.run(identity(args.run_id))
    from marketmind.config import get_settings

    namespace = f"marketmind:{source_id}:{collector_id}:{args.run_id}"
    process = CrawlerProcess(
        settings={
            "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
            "SCHEDULER": "scrapy_redis.scheduler.Scheduler",
            "DUPEFILTER_CLASS": "scrapy_redis.dupefilter.RFPDupeFilter",
            "SCHEDULER_QUEUE_KEY": namespace + ":requests",
            "SCHEDULER_DUPEFILTER_KEY": namespace + ":seen",
            "SCHEDULER_PERSIST": True,
            "REDIS_URL": get_settings().redis_url.get_secret_value(),
            "REDIS_PARAMS": {"socket_connect_timeout": 5, "socket_timeout": 5},
            "DOWNLOAD_HANDLERS": {
                "http": "marketmind.scrapy_worker.OfficialProtocolHandler",
                "https": "marketmind.scrapy_worker.OfficialProtocolHandler",
            },
            "ITEM_PIPELINES": {"marketmind.scrapy_worker.LedgerPipeline": 100},
            "CONCURRENT_REQUESTS": 1,
            "CONCURRENT_ITEMS": 1,
            "AUTOTHROTTLE_ENABLED": True,
            "DOWNLOAD_DELAY": 3,
            "RETRY_ENABLED": False,
            "REDIRECT_ENABLED": False,
            "HTTPERROR_ALLOW_ALL": True,
            "ROBOTSTXT_OBEY": False,
            "LOG_ENABLED": False,
            "TELNETCONSOLE_ENABLED": False,
        }
    )
    crawler = process.create_crawler(type(f"{collector_code}Spider", (LedgerSpider,), {"name": collector_code}))
    process.crawl(
        crawler,
        run_id=str(args.run_id),
        source_id=str(source_id),
        source_code=source_code,
        source_generation=args.source_generation,
        run_generation=args.run_generation,
    )
    process.start()
    spider = crawler.spider
    if spider is None or not (spider.committed or spider.handled_failure):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
