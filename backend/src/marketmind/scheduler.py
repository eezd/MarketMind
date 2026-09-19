"""Persist due runs only; collection never executes in the scheduler process."""

import argparse
import asyncio
import random
from datetime import timedelta

from sqlalchemy import select

from marketmind.models import Collector, CrawlRun, Source
from marketmind.runtime import ACTIVE, create_run, database, now, runtime_row


async def schedule_once(sessions):
    stamp = now()
    # A row lock plus the active-run unique index serializes competing schedulers.
    async with sessions() as db:
        source_ids = list(await db.scalars(select(Source.id).where(Source.enabled.is_(True))))
    for source_id in source_ids:
        async with sessions() as db, db.begin():
            source = await db.scalar(select(Source).where(Source.id == source_id).with_for_update(skip_locked=True))
            if source is None or not source.enabled:
                continue
            runtime = await runtime_row(db, source_id)
            if runtime.status in ("paused", "waiting_login"):
                continue
            collectors = (
                await db.scalars(
                    select(Collector)
                    .where(
                        Collector.source_id == source_id,
                        Collector.enabled.is_(True),
                    )
                    .with_for_update()
                )
            ).all()
            for collector in collectors:
                if collector.next_run_at and collector.next_run_at > stamp:
                    continue
                # A real-time run has its own 30-minute overlap and does not share the backfill cursor.
                await create_run(
                    db,
                    collector,
                    source,
                    run_type="realtime",
                    trigger_type="scheduled",
                    start=stamp - timedelta(minutes=30),
                    end=stamp,
                    max_pages=int(collector.config.get("max_pages", 1)),
                )
                collector.next_run_at = stamp + timedelta(
                    seconds=max(3, collector.interval_seconds * random.uniform(0.8, 1.2))
                )
                backfill = await db.scalar(
                    select(CrawlRun).where(
                        CrawlRun.collector_id == collector.id,
                        CrawlRun.run_type == "backfill",
                        CrawlRun.status.in_(ACTIVE),
                    )
                )
                last_review = await db.scalar(
                    select(CrawlRun.created_at)
                    .where(
                        CrawlRun.collector_id == collector.id,
                        CrawlRun.run_type == "backfill",
                        CrawlRun.trigger_type == "recovery",
                    )
                    .order_by(CrawlRun.created_at.desc())
                    .limit(1)
                )
                offline = collector.last_success_at is None or collector.last_success_at < stamp - timedelta(minutes=10)
                review_due = last_review is None or last_review < stamp - timedelta(hours=24)
                if backfill is None and (
                    review_due
                    or (
                        offline
                        and collector.last_success_at is not None
                        and (last_review is None or last_review < collector.last_success_at)
                    )
                ):
                    start = stamp - timedelta(hours=48 if offline else 24)
                    recovery = await create_run(
                        db,
                        collector,
                        source,
                        run_type="backfill",
                        trigger_type="recovery",
                        start=start,
                        end=stamp,
                        max_pages=100,
                    )
                    if collector.last_success_at and collector.last_success_at < start:
                        recovery.coverage_gaps = [
                            {
                                "reason": "older_than_automatic_window",
                                "start_at": collector.last_success_at.isoformat(),
                                "end_at": start.isoformat(),
                            }
                        ]
            runtime.recovery_scheduled_at = stamp


async def serve(once=False):
    engine, sessions = database()
    try:
        while True:
            try:
                await schedule_once(sessions)
            except Exception:
                # No DSN, SQL parameters or adapter responses in process logs.
                print("调度暂不可用，将在下个周期重试", flush=True)
                if once:
                    raise
            if once:
                return
            await asyncio.sleep(5)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="仅持久化到期任务的独立调度器")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(serve(args.once))


if __name__ == "__main__":
    main()
