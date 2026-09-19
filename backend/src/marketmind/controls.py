"""持久控制面：鉴权、配置版本、幂等命令，不在API进程运行采集。"""

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text, tuple_

from marketmind.auth import Authenticated, Db, current_session
from marketmind.errors import ApiError
from marketmind.models import ACTIVE_RUN_SQL, AuditEvent, Collector, ControlCommand, CrawlRun, CrawlTask, Source
from marketmind.pagination import decode_cursor, encode_cursor
from marketmind.queries import source_response
from marketmind.schemas import CollectorResponse, RunResponse, SourceResponse

router = APIRouter(tags=["Collection control"], dependencies=[Depends(current_session)])
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
Limit = Annotated[int, Query(ge=1, le=200)]


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_version: int = Field(ge=1)
    enabled: bool | None = None
    proxy_ids: list[UUID] | None = None
    failover_enabled: bool | None = None
    on_proxy_exhausted: Literal["direct", "pause"] | None = None
    request_interval_seconds: float | None = Field(default=None, ge=3, le=3600)


class CollectorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_version: int = Field(ge=1)
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=86400)
    config: dict | None = None


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_type: Literal["realtime", "backfill"] = "realtime"
    start_at: datetime | None = None
    end_at: datetime | None = None
    max_pages: int = Field(default=100, ge=1, le=10000)

    @model_validator(mode="after")
    def range_valid(self):
        if self.run_type == "realtime":
            if self.start_at or self.end_at:
                raise ValueError("Realtime runs cannot have a backfill range")
        elif (
            not self.start_at
            or not self.end_at
            or not self.start_at.tzinfo
            or not self.end_at.tzinfo
            or self.start_at >= self.end_at
        ):
            raise ValueError("Backfill requires an ordered, timezone-aware range")
        return self


def require(db: Db, model, identifier: UUID, *, lock=False):
    statement = select(model).where(model.id == identifier)
    with db.no_autoflush:
        obj = db.scalar(statement.with_for_update() if lock else statement)
    if obj is None:
        raise ApiError(404, "not_found", "Requested object does not exist")
    return obj


def audit(db, user, request, action, target, identifier, details=None):
    db.add(
        AuditEvent(
            admin_id=user.id,
            action=action,
            target_type=target,
            target_id=identifier,
            request_id=request.state.request_id,
            outcome="accepted",
            details=details or {},
        )
    )


def command(db, user, request, key, action, payload, **target):
    route = request.url.path
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # 同幂等键序列化，重放不创建新run，也不重执行已接受动作。
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": f"command:{user.id}:{route}:{key}"}
    )
    old = db.scalar(
        select(ControlCommand).where(
            ControlCommand.admin_id == user.id, ControlCommand.route == route, ControlCommand.idempotency_key == key
        )
    )
    if old:
        if old.request_hash != digest:
            raise ApiError(409, "idempotency_conflict", "Key was already used with a different request")
        return old, False
    obj = ControlCommand(
        id=uuid4(),
        admin_id=user.id,
        action=action,
        route=route,
        idempotency_key=key,
        request_hash=digest,
        status="pending",
        result={"request": payload},
        **target,
    )
    db.add(obj)
    return obj, True


def command_response(obj):
    return {
        "id": obj.id,
        "status": obj.status,
        "action": obj.action,
        "result": obj.result,
        "created_at": obj.created_at,
        "completed_at": obj.completed_at,
    }


@router.get("/sources/{identifier}", response_model=SourceResponse)
def source_detail(identifier: UUID, db: Db):
    return source_response(db, require(db, Source, identifier))


@router.patch("/sources/{identifier}", response_model=SourceResponse)
def patch_source(identifier: UUID, payload: SourcePatch, db: Db, identity: Authenticated, request: Request):
    from marketmind.proxies import validate_source_config

    obj = require(db, Source, identifier, lock=True)
    if obj.config_version != payload.config_version:
        raise ApiError(409, "version_conflict", "Source configuration changed; reload before saving")
    values = payload.model_dump(mode="json", exclude_none=True, exclude={"config_version", "enabled"})
    config = {**obj.permission_config, **values}
    validate_source_config(db, identifier, config)
    obj.permission_config = config
    if payload.enabled is not None:
        obj.enabled = payload.enabled
    obj.config_version += 1
    audit(db, identity[0], request, "source.update", "source", identifier, {"config_version": obj.config_version})
    db.commit()
    return source_response(db, obj)


def collector_response(db, obj):
    status = db.scalar(
        select(CrawlRun.status)
        .where(CrawlRun.collector_id == obj.id)
        .order_by(CrawlRun.created_at.desc(), CrawlRun.id.desc())
        .limit(1)
    )
    return CollectorResponse(
        id=obj.id,
        source_id=obj.source_id,
        code=obj.code,
        name=obj.name,
        entry_url=obj.entry_url,
        enabled=obj.enabled,
        interval_seconds=obj.interval_seconds,
        status=status or ("not_run" if obj.enabled else "disabled"),
        config_version=obj.config_version,
        config=obj.config,
        last_success_at=obj.last_success_at,
        next_run_at=obj.next_run_at,
    )


@router.get("/collectors/{identifier}", response_model=CollectorResponse)
def collector_detail(identifier: UUID, db: Db):
    return collector_response(db, require(db, Collector, identifier))


@router.patch("/collectors/{identifier}", response_model=CollectorResponse)
def patch_collector(identifier: UUID, payload: CollectorPatch, db: Db, identity: Authenticated, request: Request):
    obj = require(db, Collector, identifier, lock=True)
    if obj.config_version != payload.config_version:
        raise ApiError(409, "version_conflict", "Collector configuration changed; reload before saving")
    if payload.config is not None:
        if set(payload.config) - {"max_pages"} or not isinstance(payload.config.get("max_pages", 100), int):
            raise ApiError(422, "invalid_config", "Only the max_pages collection budget is configurable")
        if not 1 <= payload.config.get("max_pages", 100) <= 10000:
            raise ApiError(422, "invalid_config", "Invalid page budget")
    for key, value in payload.model_dump(exclude_none=True, exclude={"config_version"}).items():
        setattr(obj, key, value)
    if payload.enabled:
        obj.next_run_at = datetime.now(UTC)
    obj.config_version += 1
    audit(db, identity[0], request, "collector.update", "collector", identifier)
    db.commit()
    return collector_response(db, obj)


@router.post("/collectors/{identifier}/runs", status_code=202)
def create_run(identifier: UUID, payload: RunInput, key: Key, db: Db, identity: Authenticated, request: Request):
    values = payload.model_dump(mode="json")
    cmd, fresh = command(db, identity[0], request, key, "run", values, collector_id=identifier)
    if fresh:
        collector = require(db, Collector, identifier, lock=True)
        source = require(db, Source, collector.source_id)
        active = db.scalar(
            select(CrawlRun.id).where(
                CrawlRun.collector_id == identifier, CrawlRun.run_type == payload.run_type, text(ACTIVE_RUN_SQL)
            )
        )
        if active:
            raise ApiError(409, "active_run", "An active run of this type already exists")
        run_id = uuid4()
        db.add(
            CrawlRun(
                id=run_id,
                collector_id=identifier,
                source_id=source.id,
                trigger_type="manual",
                run_type=payload.run_type,
                status="queued",
                range_start_at=payload.start_at,
                range_end_at=payload.end_at,
                config_snapshot={
                    "collector_code": collector.code,
                    "max_pages": payload.max_pages,
                    "source_config": source.permission_config,
                    "config_version": collector.config_version,
                },
            )
        )
        cmd.result = {"request": values, "run_id": str(run_id)}
        audit(db, identity[0], request, "run.create", "run", run_id, {"collector_id": str(identifier)})
        db.commit()
    return {"command_id": cmd.id, "run_id": (cmd.result or {}).get("run_id")}


@router.post("/runs/{identifier}/pause", status_code=202)
@router.post("/runs/{identifier}/resume", status_code=202)
@router.post("/runs/{identifier}/cancel", status_code=202)
def run_action(identifier: UUID, key: Key, db: Db, identity: Authenticated, request: Request):
    action = request.url.path.rsplit("/", 1)[-1]
    cmd, fresh = command(db, identity[0], request, key, action, {}, run_id=identifier)
    if fresh:
        run = require(db, CrawlRun, identifier)
        allowed = {
            "pause": {"queued", "running", "waiting_login", "interrupted"},
            "resume": {"paused", "waiting_login", "interrupted", "partial_failed"},
            "cancel": {"queued", "running", "waiting_login", "paused", "interrupted", "partial_failed"},
        }
        if run.status not in allowed[action]:
            raise ApiError(409, "invalid_transition", "Run state does not permit this operation")
        audit(db, identity[0], request, f"run.{action}", "run", identifier)
        db.commit()
    return {"command_id": cmd.id}


@router.post("/collectors/{identifier}/restart", status_code=202)
def restart(identifier: UUID, key: Key, db: Db, identity: Authenticated, request: Request):
    cmd, fresh = command(db, identity[0], request, key, "restart", {}, collector_id=identifier)
    if fresh:
        require(db, Collector, identifier)
        audit(db, identity[0], request, "collector.restart", "collector", identifier)
        db.commit()
    return {"command_id": cmd.id}


@router.post("/tasks/{identifier}/retry", status_code=202)
def retry(identifier: UUID, key: Key, db: Db, identity: Authenticated, request: Request):
    cmd, fresh = command(db, identity[0], request, key, "retry", {}, task_id=identifier)
    if fresh:
        task = require(db, CrawlTask, identifier)
        if task.status != "failed":
            raise ApiError(409, "invalid_transition", "Only failed tasks can be retried")
        audit(db, identity[0], request, "task.retry", "task", identifier)
        db.commit()
    return {"command_id": cmd.id}


@router.get("/commands/{identifier}")
def get_command(identifier: UUID, db: Db):
    return command_response(require(db, ControlCommand, identifier))


@router.get("/runs/{identifier}", response_model=RunResponse)
def run_detail(identifier: UUID, db: Db):
    return require(db, CrawlRun, identifier)


def page_rows(db, query, model, scope, limit, cursor, fields):
    if cursor:
        timestamp, identifier = decode_cursor(cursor, scope)
        query = query.where(tuple_(model.created_at, model.id) < (timestamp, identifier))
    rows = db.scalars(query.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)).all()
    next_cursor = encode_cursor(scope, rows[limit - 1].created_at, rows[limit - 1].id) if len(rows) > limit else None
    return {
        "items": [{field: getattr(row, field) for field in fields} for row in rows[:limit]],
        "next_cursor": next_cursor,
    }


@router.get("/runs/{identifier}/tasks")
def run_tasks(identifier: UUID, db: Db, limit: Limit = 50, cursor: str | None = None, status: str | None = None):
    require(db, CrawlRun, identifier)
    query = select(CrawlTask).where(CrawlTask.run_id == identifier)
    if status:
        query = query.where(CrawlTask.status == status)
    return page_rows(
        db,
        query,
        CrawlTask,
        f"tasks:{identifier}:{status}",
        limit,
        cursor,
        (
            "id",
            "status",
            "attempt",
            "generation",
            "task_type",
            "url",
            "error_code",
            "error_details",
            "created_at",
            "completed_at",
            "lease_expires_at",
        ),
    )


@router.get("/audit-events")
def audits(db: Db, limit: Limit = 50, cursor: str | None = None):
    return page_rows(
        db,
        select(AuditEvent),
        AuditEvent,
        "audit",
        limit,
        cursor,
        ("id", "created_at", "action", "target_type", "target_id", "outcome", "details"),
    )


@router.get("/runs/{identifier}/logs")
def run_logs(identifier: UUID, db: Db, limit: Limit = 50, cursor: str | None = None):
    require(db, CrawlRun, identifier)
    return page_rows(
        db,
        select(AuditEvent).where(AuditEvent.target_id == identifier),
        AuditEvent,
        f"run-logs:{identifier}",
        limit,
        cursor,
        ("id", "created_at", "action", "outcome", "details"),
    )


@router.get("/metrics")
def metrics(db: Db):
    return {
        "runs": dict(db.execute(select(CrawlRun.status, func.count()).group_by(CrawlRun.status)).all()),
        "tasks": dict(db.execute(select(CrawlTask.status, func.count()).group_by(CrawlTask.status)).all()),
        "observed_at": datetime.now(UTC),
    }


@router.get("/version")
def version():
    return {
        "version": os.environ.get("MM_VERSION", "2026.09.16.1"),
        "commit_sha": os.environ.get("MM_COMMIT_SHA", "development"),
    }
