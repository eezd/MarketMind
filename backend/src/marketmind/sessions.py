"""Authenticated control plane; browser work belongs only to session_worker."""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from marketmind.auth import Authenticated, Db
from marketmind.errors import ApiError
from marketmind.jin10_session import ORIGINS, _cipher
from marketmind.models import AuditEvent, CrawlRun, CrawlTask, Source
from marketmind.runtime_models import SourceRuntime
from marketmind.session_models import LoginAttempt, SourceSession

router = APIRouter(tags=["Source sessions"])
ACTIVE_ATTEMPTS = ("queued", "qr_pending", "verifying")
LOGIN_LIFETIME = timedelta(minutes=5)
VALIDATION_MAX_AGE = timedelta(minutes=10)
DOMAINS = tuple(origin.split("/")[2] for origin in ORIGINS)


def _source(db: Session, source_id: UUID, *, lock: bool = False) -> Source:
    statement = select(Source).where(Source.id == source_id)
    if lock:
        statement = statement.with_for_update()
    source = db.scalar(statement)
    if source is None:
        raise ApiError(404, "source_not_found", "来源不存在")
    if source.code != "jin10":
        raise ApiError(422, "session_not_supported", "此来源不使用扫码会话")
    return source


def locked_state(db: Session, source_id: UUID) -> SourceSession:
    # All source-session mutations share this lock, including worker publication.
    _source(db, source_id, lock=True)
    state = db.scalar(select(SourceSession).where(SourceSession.source_id == source_id))
    if state is None:
        state = SourceSession(
            source_id=source_id,
            status="unauthenticated",
            generation=0,
            domains={},
            updated_at=datetime.now(UTC),
        )
        db.add(state)
        db.flush()
    return state


def audit(
    db: Session,
    action: str,
    target_id: UUID,
    *,
    admin_id: UUID | None = None,
    outcome: str = "succeeded",
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditEvent(
            admin_id=admin_id,
            action=action,
            target_type="source_session",
            target_id=target_id,
            outcome=outcome,
            details=details or {},
        )
    )


def pause_source_runs(db: Session, source_id: UUID) -> None:
    runtime = db.scalar(select(SourceRuntime).where(SourceRuntime.source_id == source_id).with_for_update())
    if runtime is not None:
        runtime.generation += 1
        runtime.lease_expires_at = None
        if runtime.status != "paused":
            runtime.status = "waiting_login"
            runtime.pause_reason = "waiting_login"
    run_ids = list(
        db.scalars(
            select(CrawlRun.id)
            .where(
                CrawlRun.source_id == source_id,
                CrawlRun.status.in_(("queued", "running", "interrupted", "waiting_login")),
            )
            .with_for_update()
        )
    )
    if not run_ids:
        return
    db.execute(update(CrawlRun).where(CrawlRun.id.in_(run_ids)).values(status="waiting_login"))
    db.execute(
        update(CrawlTask)
        .where(
            CrawlTask.run_id.in_(run_ids),
            CrawlTask.status == "running",
        )
        .values(
            status="pending",
            generation=CrawlTask.generation + 1,
            lease_owner=None,
            lease_expires_at=None,
            error_code="waiting_login",
            error_details={"message": "来源会话需要重新验证"},
        )
    )


def expire_attempts(db: Session, state: SourceSession, now: datetime) -> None:
    expired = list(
        db.scalars(
            select(LoginAttempt).where(
                LoginAttempt.source_id == state.source_id,
                LoginAttempt.status.in_(ACTIVE_ATTEMPTS),
                LoginAttempt.expires_at <= now,
            )
        )
    )
    for attempt in expired:
        attempt.status = "expired"
        attempt.encrypted_qr = None
        attempt.completed_at = now
        attempt.updated_at = now
        attempt.error_code = "qr_expired"
        if attempt.generation == state.generation:
            state.generation += 1
        audit(db, "session.login_expired", state.source_id, details={"attempt_id": str(attempt.id)})
    if expired and state.status == "qr_pending":
        state.status = "expired"
        state.updated_at = now


def _attempt_response(attempt: LoginAttempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "source_id": attempt.source_id,
        "status": attempt.status,
        "expires_at": attempt.expires_at,
        "qr_available": attempt.status == "qr_pending" and attempt.encrypted_qr is not None,
        "domains": [{"domain": domain, "status": attempt.domains.get(domain, "unauthenticated")} for domain in DOMAINS],
        "error_code": attempt.error_code,
    }


def _get_attempt(db: Session, attempt_id: UUID) -> tuple[SourceSession, LoginAttempt]:
    source_id = db.scalar(select(LoginAttempt.source_id).where(LoginAttempt.id == attempt_id))
    if source_id is None:
        raise ApiError(404, "login_attempt_not_found", "登录尝试不存在")
    state = locked_state(db, source_id)
    attempt = db.get(LoginAttempt, attempt_id)
    expire_attempts(db, state, datetime.now(UTC))
    return state, attempt


def _new_attempt(
    db: Session, state: SourceSession, request: Request, admin_id: UUID, payload: dict[str, Any] | None
) -> LoginAttempt:
    key = request.headers.get("idempotency-key", "")
    if not key or len(key) > 200:
        raise ApiError(422, "idempotency_key_required", "需提供不超过200字符的 Idempotency-Key")
    request_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    existing = db.scalar(
        select(LoginAttempt).where(
            LoginAttempt.admin_id == admin_id,
            LoginAttempt.route == request.url.path,
            LoginAttempt.idempotency_key == key,
        )
    )
    if existing:
        if existing.request_hash != request_hash:
            raise ApiError(409, "idempotency_conflict", "此幂等键已用于不同请求")
        expire_attempts(db, state, datetime.now(UTC))
        return existing
    if payload:
        raise ApiError(422, "unexpected_login_fields", "扫码登录不接受额外参数")
    # Fail before accepting work when encryption is not configured.
    try:
        _cipher()
    except Exception:
        raise ApiError(503, "session_key_unavailable", "会话加密配置不可用") from None
    now = datetime.now(UTC)
    expire_attempts(db, state, now)
    for previous in db.scalars(
        select(LoginAttempt).where(
            LoginAttempt.source_id == state.source_id,
            LoginAttempt.status.in_(ACTIVE_ATTEMPTS),
        )
    ):
        previous.status = "cancelled"
        previous.encrypted_qr = None
        previous.completed_at = now
        previous.updated_at = now
        previous.error_code = "superseded"
    state.generation += 1
    state.updated_at = now
    if state.status != "authenticated":
        state.status = "qr_pending"
    attempt = LoginAttempt(
        source_id=state.source_id,
        admin_id=admin_id,
        route=request.url.path,
        idempotency_key=key,
        request_hash=request_hash,
        generation=state.generation,
        status="queued",
        domains={},
        created_at=now,
        updated_at=now,
        expires_at=now + LOGIN_LIFETIME,
    )
    db.add(attempt)
    db.flush()
    audit(db, "session.login_requested", state.source_id, admin_id=admin_id, details={"attempt_id": str(attempt.id)})
    return attempt


@router.get("/sources/{source_id}/session")
def get_session(source_id: UUID, db: Db, auth: Authenticated, response: Response) -> dict[str, Any]:
    _source(db, source_id)
    response.headers["Cache-Control"] = "no-store"
    state = db.scalar(select(SourceSession).where(SourceSession.source_id == source_id))
    status = state.status if state else "unauthenticated"
    domains = state.domains if state else {}
    if (
        state
        and state.status == "authenticated"
        and (state.validated_at is None or state.validated_at < datetime.now(UTC) - VALIDATION_MAX_AGE)
    ):
        status = "verifying"
        domains = {domain: "verifying" for domain in DOMAINS}
    return {
        "source_id": source_id,
        "status": status,
        "domains": [{"domain": domain, "status": domains.get(domain, "unauthenticated")} for domain in DOMAINS],
        "updated_at": state.updated_at if state else None,
    }


@router.post("/sources/{source_id}/login-attempts", status_code=202)
def create_attempt(
    source_id: UUID,
    request: Request,
    db: Db,
    auth: Authenticated,
    response: Response,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    state = locked_state(db, source_id)
    attempt = _new_attempt(db, state, request, auth[0].id, payload)
    result = _attempt_response(attempt)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/login-attempts/{attempt_id}")
def get_attempt(attempt_id: UUID, db: Db, auth: Authenticated, response: Response) -> dict[str, Any]:
    _, attempt = _get_attempt(db, attempt_id)
    result = _attempt_response(attempt)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/login-attempts/{attempt_id}/qr")
def get_qr(attempt_id: UUID, db: Db, auth: Authenticated) -> Response:
    state, attempt = _get_attempt(db, attempt_id)
    if attempt.status != "qr_pending" or attempt.generation != state.generation or not attempt.encrypted_qr:
        db.commit()
        raise ApiError(409, "qr_unavailable", "二维码尚未生成或已失效")
    try:
        image = _cipher().decrypt(attempt.encrypted_qr)
    except Exception:
        raise ApiError(503, "qr_unavailable", "二维码暂不可用，请重新创建登录尝试") from None
    db.commit()
    return Response(
        image,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
    )


@router.post("/login-attempts/{attempt_id}/refresh", status_code=202)
def refresh_attempt(
    attempt_id: UUID,
    request: Request,
    db: Db,
    auth: Authenticated,
    response: Response,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    state, _ = _get_attempt(db, attempt_id)
    attempt = _new_attempt(db, state, request, auth[0].id, payload)
    result = _attempt_response(attempt)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/login-attempts/{attempt_id}/cancel")
def cancel_attempt(attempt_id: UUID, db: Db, auth: Authenticated, response: Response) -> dict[str, Any]:
    state, attempt = _get_attempt(db, attempt_id)
    if attempt.status in ACTIVE_ATTEMPTS:
        now = datetime.now(UTC)
        attempt.status = "cancelled"
        attempt.encrypted_qr = None
        attempt.completed_at = now
        attempt.updated_at = now
        if attempt.generation == state.generation:
            state.generation += 1
            if state.status == "qr_pending":
                state.status = "unauthenticated"
            state.updated_at = now
        audit(
            db, "session.login_cancelled", state.source_id, admin_id=auth[0].id, details={"attempt_id": str(attempt.id)}
        )
    result = _attempt_response(attempt)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.delete("/sources/{source_id}/session", status_code=204)
def revoke_session(source_id: UUID, db: Db, auth: Authenticated) -> Response:
    state = locked_state(db, source_id)
    now = datetime.now(UTC)
    state.generation += 1
    state.status = "revoked"
    state.encrypted_state = None
    state.validated_at = None
    state.revoked_at = now
    state.updated_at = now
    state.egress_id = None
    state.domains = {domain: "unauthenticated" for domain in DOMAINS}
    for attempt in db.scalars(
        select(LoginAttempt).where(
            LoginAttempt.source_id == source_id,
            LoginAttempt.status.in_(ACTIVE_ATTEMPTS),
        )
    ):
        attempt.status = "cancelled"
        attempt.encrypted_qr = None
        attempt.completed_at = now
        attempt.updated_at = now
        attempt.error_code = "session_revoked"
    pause_source_runs(db, source_id)
    # The configured legacy local copy is also a credential, not an audit record.
    configured_path = os.environ.get("MM_JIN10_SESSION_FILE")
    if configured_path:
        try:
            Path(configured_path).expanduser().unlink(missing_ok=True)
        except OSError:
            raise ApiError(503, "session_revoke_failed", "无法删除本地会话文件，请检查私有目录权限") from None
    audit(db, "session.revoked", source_id, admin_id=auth[0].id)
    db.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
