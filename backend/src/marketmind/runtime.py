"""One supervised executor per source; PostgreSQL is the authoritative work ledger."""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from marketmind.config import get_settings
from marketmind.models import AuditEvent, Collector, ControlCommand, CrawlRun, CrawlTask, Source
from marketmind.runtime_models import SourceRuntime

ACTIVE = ("queued", "running", "paused", "waiting_login", "interrupted")
LEASE_SECONDS = 90


def now():
    return datetime.now(UTC)


def database():
    engine = create_async_engine(
        get_settings().database_url.get_secret_value(),
        pool_size=3,
        max_overflow=0,
        connect_args={"connect_timeout": 10, "options": "-c statement_timeout=15000 -c lock_timeout=5000"},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def runtime_row(db, source_id):
    await db.scalar(select(Source).where(Source.id == source_id).with_for_update())
    row = await db.scalar(select(SourceRuntime).where(SourceRuntime.source_id == source_id).with_for_update())
    if row is None:
        row = SourceRuntime(source_id=source_id)
        db.add(row)
        await db.flush()
    return row


async def create_run(db, collector, source, *, run_type, trigger_type, start=None, end=None, max_pages=1):
    existing = await db.scalar(
        select(CrawlRun).where(
            CrawlRun.collector_id == collector.id,
            CrawlRun.run_type == run_type,
            CrawlRun.status.in_(ACTIVE),
        )
    )
    if existing:
        return existing
    run = CrawlRun(
        source_id=source.id,
        collector_id=collector.id,
        run_type=run_type,
        trigger_type=trigger_type,
        range_start_at=start,
        range_end_at=end,
        status="queued",
        config_snapshot={
            "collector_code": collector.code,
            "max_pages": max_pages,
            "source_config": source.permission_config,
            "collector_config": collector.config,
        },
    )
    db.add(run)
    await db.flush()
    return run


@asynccontextmanager
async def source_lock(engine, source_id, *, worker=False):
    # A worker has its own lock: an orphan must finish before a replacement can access the site.
    connection = await engine.connect()
    key = f"marketmind:{'worker' if worker else 'executor'}:{source_id}"
    try:
        acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key": key})
        await connection.commit()
        if not acquired:
            raise RuntimeError("Source already has an active execution instance")
        yield connection
    finally:
        if not connection.closed:
            try:
                # A failed heartbeat leaves an invalid transaction. Roll it back
                # before unlocking; closing the session releases the advisory lock
                # when PostgreSQL itself is unavailable.
                await connection.rollback()
                await connection.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": key})
                await connection.commit()
            except SQLAlchemyError:
                await connection.invalidate()
        await connection.close()


async def fence_tasks(db, run, *, cancelled=False):
    tasks = (
        await db.scalars(
            select(CrawlTask)
            .where(
                CrawlTask.run_id == run.id,
                CrawlTask.status.notin_(("succeeded", "cancelled")),
            )
            .with_for_update()
        )
    ).all()
    run.checkpoint = {**run.checkpoint, "generation": int(run.checkpoint.get("generation", 0)) + 1}
    for task in tasks:
        task.generation += 1
        task.lease_owner = None
        task.lease_expires_at = None
        task.status = "cancelled" if cancelled else "pending"
        task.retry_at = None


async def resume_run(db, run):
    """Resume only recoverable work, under the caller's source lock."""
    if run.status in ("succeeded", "cancelled"):
        raise ValueError("completed_run")
    competing = await db.scalar(
        select(CrawlRun.id)
        .where(
            CrawlRun.collector_id == run.collector_id,
            CrawlRun.run_type == run.run_type,
            CrawlRun.id != run.id,
            CrawlRun.status.in_(ACTIVE),
        )
        .limit(1)
    )
    if competing:
        raise ValueError("active_run_conflict")
    unfinished = await db.scalar(
        select(CrawlTask.id)
        .where(
            CrawlTask.run_id == run.id,
            CrawlTask.status.notin_(("succeeded", "cancelled")),
        )
        .limit(1)
    )
    if unfinished is None:
        completed = await db.scalar(select(CrawlTask.id).where(CrawlTask.run_id == run.id).limit(1))
        if completed is not None:
            # A non-advancing/missing cursor cannot be made resumable by changing a status.
            raise ValueError("no_resumable_task")
    await fence_tasks(db, run)
    run.checkpoint = {**run.checkpoint, "batch_start_page": int(run.checkpoint.get("page", 0))}
    run.status = "queued"
    run.finished_at = None


async def apply_command(db, runtime, command, source_id):
    request = (command.result or {}).get("request", {})
    target_task = await db.get(CrawlTask, command.task_id) if command.task_id else None
    if command.task_id and target_task is None:
        raise ValueError("missing_task")
    if command.action == "retry" and target_task and target_task.status in ("succeeded", "cancelled"):
        raise ValueError("completed_task")
    requested_run_id = command.run_id or (target_task.run_id if target_task else None)
    if command.action == "run":
        if not command.result or not command.result.get("run_id"):
            raise ValueError("missing_run")
        requested_run_id = UUID(command.result["run_id"])
    target_run = await db.get(CrawlRun, requested_run_id) if requested_run_id else None
    if requested_run_id and (target_run is None or target_run.source_id != source_id):
        raise ValueError("missing_run")
    baseline = (command.result or {}).get("baseline", {})
    if command.status == "running" and command.action == "restart":
        runs = (
            await db.scalars(
                select(CrawlRun)
                .where(
                    CrawlRun.source_id == source_id,
                    CrawlRun.id.in_([UUID(value) for value in baseline]),
                )
                .with_for_update()
            )
        ).all()
        progressed = any(int(run.checkpoint.get("page", 0)) > baseline.get(str(run.id), -1) for run in runs)
        if not progressed:
            if not runs or any(
                run.status in ("failed", "partial_failed", "cancelled", "waiting_login", "paused") for run in runs
            ):
                raise ValueError("restart_without_progress")
            return False
    else:
        runs = (
            [target_run]
            if target_run
            else (
                await db.scalars(
                    select(CrawlRun)
                    .where(
                        CrawlRun.collector_id == command.collector_id,
                        CrawlRun.status.in_(ACTIVE),
                    )
                    .with_for_update()
                )
            ).all()
        )
        if command.action == "restart" and not runs:
            collector = await db.get(Collector, command.collector_id)
            if collector is None or collector.source_id != source_id:
                raise ValueError("missing_collector")
            source = await db.get(Source, source_id)
            runs = [await create_run(db, collector, source, run_type="realtime", trigger_type="manual")]
        if not runs:
            raise ValueError("missing_run")
        for run in runs:
            if command.action == "pause":
                if run.status not in ACTIVE:
                    raise ValueError("terminal_run")
                await fence_tasks(db, run)
                run.status = "paused"
            elif command.action == "cancel":
                if run.status == "succeeded":
                    raise ValueError("completed_run")
                await fence_tasks(db, run, cancelled=True)
                run.status = "cancelled"
                run.finished_at = now()
            elif command.action in ("resume", "restart", "retry"):
                await resume_run(db, run)
                if target_task is not None:
                    target_task.request_params = {**target_task.request_params, "retry_base": target_task.attempt}
            elif command.action == "run":
                if run.status == "interrupted":
                    run.status = "queued"
            else:
                raise ValueError("unsupported_action")
        reset_transport = False
        if command.action in ("resume", "restart", "retry"):
            if runtime.status != "running":
                runtime.status = "idle"
                reset_transport = True
            runtime.pause_reason = None
            runtime.restart_history = []
            runtime.restart_pending = False
            runtime.retry_at = None
            runtime.consecutive_failures = 0
        if command.action == "restart":
            command.status = "running"
            command.result = {
                "request": request,
                "baseline": {str(run.id): int(run.checkpoint.get("page", 0)) for run in runs},
            }
            return reset_transport
    command.status = "succeeded"
    command.completed_at = now()
    command.result = {**(command.result or {}), "request": request, "applied": True}
    for run in runs:
        db.add(
            AuditEvent(
                admin_id=command.admin_id,
                action=f"run.{command.action}",
                target_type="run",
                target_id=run.id,
                outcome="succeeded",
                details={"command_id": str(command.id)},
            )
        )
    return reset_transport if command.action != "restart" else False


async def commands(sessions, source_id):
    """Each command owns a savepoint: an invalid recovery cannot poison supervision."""
    reset_transport = False
    async with sessions() as db, db.begin():
        runtime = await runtime_row(db, source_id)
        collector_ids = select(Collector.id).where(Collector.source_id == source_id)
        run_ids = select(CrawlRun.id).where(CrawlRun.source_id == source_id)
        task_ids = select(CrawlTask.id).where(CrawlTask.source_id == source_id)
        pending = (
            await db.scalars(
                select(ControlCommand)
                .where(
                    ControlCommand.status.in_(("pending", "running")),
                    or_(
                        ControlCommand.collector_id.in_(collector_ids),
                        ControlCommand.run_id.in_(run_ids),
                        ControlCommand.task_id.in_(task_ids),
                    ),
                )
                .order_by(ControlCommand.created_at)
                .with_for_update()
            )
        ).all()
        for command in pending:
            original_result = dict(command.result or {})
            try:
                async with db.begin_nested():
                    reset = await apply_command(db, runtime, command, source_id)
                    await db.flush()
                reset_transport = reset_transport or reset
            except (IntegrityError, ValueError) as exc:
                await db.refresh(command)
                await db.refresh(runtime)
                command.status = "failed"
                command.completed_at = now()
                reason = "state_conflict" if isinstance(exc, IntegrityError) else str(exc)
                allowed = {
                    "completed_run",
                    "active_run_conflict",
                    "no_resumable_task",
                    "missing_task",
                    "completed_task",
                    "missing_run",
                    "restart_without_progress",
                    "missing_collector",
                    "terminal_run",
                    "unsupported_action",
                    "state_conflict",
                }
                command.result = {**original_result, "error": reason if reason in allowed else "invalid_command"}
                db.add(
                    AuditEvent(
                        admin_id=command.admin_id,
                        action=f"run.{command.action}",
                        target_type="run" if command.run_id else "command",
                        target_id=command.run_id or command.id,
                        outcome="failed",
                        details={"command_id": str(command.id), "reason": command.result["error"]},
                    )
                )
    if reset_transport:
        from marketmind.proxy_runtime import reset_source_transport

        await reset_source_transport(source_id)


async def claim_batch(sessions, source_id, owner, node_id, run_id=None):
    async with sessions() as db, db.begin():
        runtime = await runtime_row(db, source_id)
        if runtime.status in ("paused", "waiting_login") or (runtime.retry_at and runtime.retry_at > now()):
            return None
        source = await db.get(Source, source_id)
        candidates = (
            select(CrawlRun)
            .join(Collector, Collector.id == CrawlRun.collector_id)
            .where(
                CrawlRun.source_id == source_id,
                CrawlRun.status.in_(("queued", "running", "interrupted")),
            )
        )
        if run_id:
            candidates = candidates.where(CrawlRun.id == run_id)
        else:
            if not source.enabled:
                return None
            candidates = candidates.where(Collector.enabled.is_(True))
        runs = (await db.scalars(candidates.order_by(CrawlRun.created_at).with_for_update(of=CrawlRun))).all()
        # Alternate lanes, then serve the least recently active run within each lane.
        # Per-run heartbeats survive lane switches and executor restarts; last_run_id does not.
        preferred = "backfill" if runtime.last_run_type == "realtime" else "realtime"
        runs.sort(
            key=lambda run: (
                run.run_type != preferred,
                run.heartbeat_at or datetime.min.replace(tzinfo=UTC),
                run.created_at,
                run.id,
            )
        )
        for run in runs:
            blocked = await db.scalar(
                select(CrawlTask.id)
                .where(
                    CrawlTask.run_id == run.id,
                    CrawlTask.status == "running",
                    CrawlTask.lease_expires_at > now(),
                )
                .limit(1)
            )
            if blocked:
                continue
            task = await db.scalar(
                select(CrawlTask)
                .where(
                    CrawlTask.run_id == run.id,
                    or_(
                        CrawlTask.status.in_(("pending", "queued")),
                        (CrawlTask.status == "retry_wait") & (CrawlTask.retry_at <= now()),
                        (CrawlTask.status == "running") & (CrawlTask.lease_expires_at <= now()),
                    ),
                )
                .order_by(CrawlTask.created_at)
                .with_for_update()
                .limit(1)
            )
            if task is None:
                any_task = await db.scalar(select(CrawlTask.id).where(CrawlTask.run_id == run.id).limit(1))
                if any_task:
                    continue
                collector = await db.get(Collector, run.collector_id)
                task = CrawlTask(
                    run_id=run.id,
                    source_id=source_id,
                    collector_id=collector.id,
                    business_key=f"page:{int(run.checkpoint.get('page', 0))}",
                    task_type="list",
                    url=collector.entry_url,
                    request_params={"cursor": run.checkpoint.get("cursor"), "page": int(run.checkpoint.get("page", 0))},
                )
                db.add(task)
                await db.flush()
            runtime.generation += 1
            runtime.owner = owner
            runtime.node_id = node_id
            if runtime.restart_pending:
                runtime.restart_history = [
                    stamp
                    for stamp in runtime.restart_history
                    if datetime.fromisoformat(stamp) > now() - timedelta(minutes=30)
                ] + [now().isoformat()]
                runtime.restart_pending = False
            runtime.status = "running"
            runtime.heartbeat_at = now()
            runtime.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)
            runtime.last_run_id = run.id
            runtime.last_run_type = run.run_type
            run.status = "running"
            run.started_at = run.started_at or now()
            run.heartbeat_at = now()
            run.node_id = node_id
            run.checkpoint = {**run.checkpoint, "generation": int(run.checkpoint.get("generation", 0)) + 1}
            task.status = "queued"
            task.lease_owner = None
            task.lease_expires_at = None
            db.add(
                AuditEvent(
                    action="run.batch_started",
                    target_type="run",
                    target_id=run.id,
                    outcome="running",
                    details={"source_generation": runtime.generation, "run_generation": run.checkpoint["generation"]},
                )
            )
            return run.id, runtime.generation, run.checkpoint["generation"]
    return None


async def supervise(sessions, source_id, batch, owner):
    run_id, source_generation, run_generation = batch
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "marketmind.scrapy_worker",
        "--run-id",
        str(run_id),
        "--source-generation",
        str(source_generation),
        "--run-generation",
        str(run_generation),
    )
    started = asyncio.get_running_loop().time()
    try:
        while process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
                break
            except TimeoutError:
                pass
            await commands(sessions, source_id)
            async with sessions() as db, db.begin():
                runtime = await runtime_row(db, source_id)
                run = await db.get(CrawlRun, run_id)
                if (
                    runtime.owner != owner
                    or runtime.generation != source_generation
                    or run.status not in ("running", "succeeded", "partial_failed", "paused")
                    or run.checkpoint.get("generation") != run_generation
                ):
                    process.terminate()
                    break
                runtime.heartbeat_at = now()
                runtime.lease_expires_at = now() + timedelta(seconds=LEASE_SECONDS)
            if asyncio.get_running_loop().time() - started > 660:
                process.terminate()
                break
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()
        return process.returncode
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()


async def record_exit(sessions, source_id, batch, returncode):
    from marketmind.alerts import emit_alert

    alarm = None
    async with sessions() as db, db.begin():
        runtime = await runtime_row(db, source_id)
        run = await db.get(CrawlRun, batch[0])
        if runtime.generation != batch[1]:
            return
        if returncode != 0 and run.status == "running" and run.checkpoint.get("generation") == batch[2]:
            await fence_tasks(db, run)
            run.status = "interrupted"
            runtime.restart_pending = True
            runtime.consecutive_failures += 1
            runtime.retry_at = now() + timedelta(seconds=min(60, 5 * 2 ** min(runtime.consecutive_failures, 4)))
            alarm = "collection_failed"
        if runtime.status == "running":
            runtime.status = "idle"
        runtime.heartbeat_at = now()
        runtime.lease_expires_at = None
        db.add(
            AuditEvent(
                action="run.batch_exited",
                target_type="run",
                target_id=run.id,
                outcome="succeeded" if returncode == 0 else "failed",
                details={"exit_code": returncode, "status": run.status},
            )
        )
    if alarm:
        await emit_alert(source_id, alarm, "来源执行进程异常退出")


async def restart_budget(sessions, source_id):
    from marketmind.alerts import emit_alert

    exhausted = False
    async with sessions() as db, db.begin():
        runtime = await runtime_row(db, source_id)
        if runtime.status == "paused":
            return False
        if not runtime.restart_pending:
            return True
        if runtime.retry_at and runtime.retry_at > now():
            return False
        history = [
            stamp for stamp in runtime.restart_history if datetime.fromisoformat(stamp) > now() - timedelta(minutes=30)
        ]
        if len(history) >= 3:
            runtime.status = "paused"
            runtime.pause_reason = "recovery_exhausted"
            exhausted = True
    if exhausted:
        await emit_alert(source_id, "recovery_exhausted", "来源自动恢复预算已用尽，请人工恢复")
    return not exhausted


async def execute_source(source_code, *, run_id=None, once=False):
    engine, sessions = database()
    owner = str(uuid4())
    node_id = os.environ.get("MM_NODE_ID", socket.gethostname())
    try:
        async with sessions() as db:
            source = await db.scalar(select(Source).where(Source.code == source_code))
            if source is None:
                raise ValueError("Unknown source")
        async with source_lock(engine, source.id) as lock_connection:
            # Never bypass an orphan worker's live lease after supervisor/container restart.
            async with sessions() as db, db.begin():
                runtime = await runtime_row(db, source.id)
                if runtime.owner and runtime.lease_expires_at and runtime.lease_expires_at > now():
                    delay = (runtime.lease_expires_at - now()).total_seconds()
                else:
                    delay = 0
            await asyncio.sleep(delay)
            async with sessions() as db, db.begin():
                runtime = await runtime_row(db, source.id)
                if runtime.status == "running":
                    runtime.status = "idle"
                    runtime.restart_pending = True
                runtime.owner = owner
            while True:
                # Losing the source lock stops supervision before another process can take over.
                await lock_connection.execute(text("SELECT 1"))
                await lock_connection.commit()
                await commands(sessions, source.id)
                try:
                    async with source_lock(engine, source.id, worker=True):
                        pass
                except RuntimeError:
                    if once:
                        return
                    await asyncio.sleep(2)
                    continue
                if await restart_budget(sessions, source.id):
                    batch = await claim_batch(sessions, source.id, owner, node_id, run_id)
                    if batch:
                        try:
                            returncode = await supervise(sessions, source.id, batch, owner)
                        except OSError:
                            returncode = 1
                        await record_exit(sessions, source.id, batch, returncode)
                        await commands(sessions, source.id)
                        if once:
                            return
                        continue
                if once:
                    return
                await asyncio.sleep(2)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="来源独立执行器（每页让行，持久化恢复预算）")
    parser.add_argument("source", choices=("wscn", "cls", "jin10"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(execute_source(args.source, once=args.once))


if __name__ == "__main__":
    main()
