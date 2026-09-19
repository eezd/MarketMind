"""Create or resume a durable run and execute a finite Scrapy-Redis batch."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from marketmind.models import Collector, CrawlRun, Source
from marketmind.runtime import create_run, database, execute_source, resume_run, runtime_row


async def execute(code, max_pages, start, end, resume_id=None):
    engine, sessions = database()
    try:
        async with sessions() as db, db.begin():
            collector = await db.scalar(select(Collector).where(Collector.code == code))
            if collector is None:
                raise ValueError("Unknown collector")
            source = await db.scalar(select(Source).where(Source.id == collector.source_id).with_for_update())
            runtime = await runtime_row(db, source.id)
            if resume_id:
                run = await db.scalar(select(CrawlRun).where(CrawlRun.id == resume_id).with_for_update())
                if run is None or run.collector_id != collector.id:
                    raise ValueError("Run does not belong to collector")
                await resume_run(db, run)
                runtime.status = "idle"
                runtime.pause_reason = None
                runtime.restart_history = []
                runtime.restart_pending = False
                runtime.retry_at = None
            else:
                run = await create_run(
                    db,
                    collector,
                    source,
                    run_type="backfill" if start else "realtime",
                    trigger_type="manual",
                    start=start,
                    end=end,
                    max_pages=max_pages,
                )
            run_id = run.id
            source_code = source.code
        # Each child owns a real Redis-scheduled request. Exit at the requested finite page budget;
        # unfinished backfills retain their pending next-page task for the long-lived executor.
        for _ in range(max_pages):
            await execute_source(source_code, run_id=run_id, once=True)
            async with sessions() as db:
                run = await db.get(CrawlRun, run_id)
                if run.status != "running":
                    break
        async with sessions() as db:
            run = await db.get(CrawlRun, run_id)
            print(
                json.dumps(
                    {
                        "run_id": str(run_id),
                        "status": run.status,
                        "statistics": run.statistics,
                        "checkpoint": run.checkpoint,
                        "coverage_gaps": run.coverage_gaps,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        await engine.dispose()


def aware_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Use an ISO8601 timestamp with timezone")
    return parsed.astimezone(UTC)


def main():
    parser = argparse.ArgumentParser(description="创建/恢复run并执行有限Scrapy批次")
    parser.add_argument(
        "collector", choices=("wscn_news", "wscn_live", "cls_depth", "cls_telegraph", "jin10_news", "jin10_live")
    )
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--start", type=aware_time)
    parser.add_argument("--end", type=aware_time)
    parser.add_argument("--resume-run", type=UUID)
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 10000:
        parser.error("max-pages must be between 1 and 10000")
    if bool(args.start) != bool(args.end) or (args.start and args.start >= args.end):
        parser.error("start and end must both be present and start must precede end")
    asyncio.run(execute(args.collector, args.max_pages, args.start, args.end, args.resume_run))


if __name__ == "__main__":
    main()
