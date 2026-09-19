"""Authenticated notification settings and safe, transactional alert emission."""

import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator
from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from marketmind.alert_models import AlertEvent, NotificationDelivery, TelegramSettings
from marketmind.auth import Authenticated, Db, current_session
from marketmind.config import get_settings
from marketmind.db import make_engine
from marketmind.errors import ApiError
from marketmind.models import AuditEvent
from marketmind.pagination import decode_cursor, encode_cursor

router = APIRouter(tags=["Alerts and notifications"], dependencies=[Depends(current_session)])
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=2048)]

# Never persist or send arbitrary exception text, page content, QR data, or credentials.
# Unknown event kinds remain distinguishable by a hash without exposing caller input.
SUMMARIES = {
    "waiting_login": "登录会话失效，请在管理页重新扫码。",
    "login_expired": "登录会话失效，请在管理页重新扫码。",
    "proxy_exhausted": "来源指定代理已耗尽，请检查出口配置。",
    "proxy_direct_fallback": "代理已耗尽，来源已按配置回退直连。",
    "collection_failed": "来源持续采集失败，请检查运行详情。",
    "parse_failed": "来源页面解析失败，请检查采集器。",
    "rate_limited": "来源持续限速，请检查运行详情。",
    "queue_backlog": "来源采集队列积压，请检查执行器。",
    "coverage_gap": "来源采集覆盖存在缺口，请检查回补范围。",
    "database_unavailable": "数据库依赖不可用，请检查服务。",
    "redis_unavailable": "Redis 依赖不可用，请检查服务。",
    "recovery_exhausted": "来源自动恢复预算已耗尽，请人工检查并恢复。",
    "access_denied": "来源访问被拒绝，请检查登录或站点限制。",
    "circuit_open": "来源持续失败，已触发熔断。",
}


def safe_kind(kind: str) -> str:
    if kind in SUMMARIES or re.fullmatch(r"event_[0-9a-f]{16}", kind):
        return kind
    return "event_" + hashlib.sha256(kind.encode()).hexdigest()[:16]


def safe_summary(kind: str, *, resolved: bool = False) -> str:
    if resolved:
        return "对应故障已恢复。"
    return SUMMARIES.get(kind, "来源运行异常，请在管理页检查状态。")


def notification_cipher() -> Fernet:
    try:
        return Fernet(os.environ["MM_SESSION_KEY"].encode("ascii"))
    except (KeyError, ValueError, UnicodeError):
        raise ApiError(503, "notification_key_unavailable", "通知加密密钥未配置或无效") from None


def decrypt_token(ciphertext: str) -> str:
    try:
        return notification_cipher().decrypt(ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeError):
        raise ApiError(503, "notification_key_unavailable", "通知凭证无法解密，请重新配置") from None


class TelegramPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    enabled: bool | None = None
    chat_id: str | None = None
    token: SecretStr | None = None

    @field_validator("chat_id")
    @classmethod
    def validate_chat(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"-?[0-9]{1,30}|@[A-Za-z][A-Za-z0-9_]{3,98}", value):
            raise ValueError("请输入有效的 Telegram chat_id")
        return value

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", value.get_secret_value()):
            raise ValueError("请输入有效的 Telegram Bot Token")
        return value

    @model_validator(mode="after")
    def nonnull_enabled(self):
        if "enabled" in self.model_fields_set and self.enabled is None:
            raise ValueError("enabled 不能为 null")
        return self


def settings_view(settings: TelegramSettings | None) -> dict:
    return {
        "enabled": bool(settings and settings.enabled),
        "chat_id": settings.chat_id if settings else None,
        "has_token": bool(settings and settings.token_ciphertext),
    }


def initial_delivery_status(settings: TelegramSettings | None) -> str:
    if not settings or not settings.token_ciphertext or not settings.chat_id:
        return "not_configured"
    return "pending" if settings.enabled else "disabled"


def lock_key(db: Session, key: str) -> None:
    identifier = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": identifier})


def configured_settings(db: Session) -> TelegramSettings | None:
    """Bootstrap explicitly enabled environment secrets once; DB edits always win."""
    settings = db.scalar(select(TelegramSettings))
    if settings is not None or os.environ.get("MM_TELEGRAM_ENABLED", "").lower() != "true":
        return settings
    token = os.environ.get("MM_TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("MM_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    lock_key(db, "telegram_settings")
    settings = db.scalar(select(TelegramSettings))
    if settings is not None:
        return settings
    try:
        payload = TelegramPatch(enabled=True, token=token, chat_id=chat_id)
    except ValueError:
        raise ApiError(503, "notification_environment_invalid", "通知环境配置无效，请检查秘密配置") from None
    ciphertext = notification_cipher().encrypt(payload.token.get_secret_value().encode("ascii")).decode("ascii")
    settings = TelegramSettings(id=uuid4(), enabled=True, chat_id=chat_id, token_ciphertext=ciphertext)
    db.add(settings)
    db.add(
        AuditEvent(
            action="notification.bootstrap",
            target_type="telegram_settings",
            target_id=settings.id,
            outcome="succeeded",
            details={"enabled": True, "has_token": True, "origin": "environment"},
        )
    )
    db.flush()
    return settings


@router.get("/notification-settings/telegram")
def get_telegram_settings(db: Db) -> dict:
    result = settings_view(configured_settings(db))
    db.commit()
    return result


@router.patch("/notification-settings/telegram")
def patch_telegram_settings(payload: TelegramPatch, request: Request, db: Db, auth: Authenticated) -> dict:
    lock_key(db, "telegram_settings")
    settings = configured_settings(db)
    if settings is None:
        settings = TelegramSettings(id=uuid4(), enabled=False)
        db.add(settings)
    before = settings_view(settings)
    if "token" in payload.model_fields_set:
        settings.token_ciphertext = (
            notification_cipher().encrypt(payload.token.get_secret_value().encode("ascii")).decode("ascii")
            if payload.token is not None
            else None
        )
    if "chat_id" in payload.model_fields_set:
        settings.chat_id = payload.chat_id
    if "enabled" in payload.model_fields_set:
        settings.enabled = payload.enabled
    if settings.enabled and (not settings.token_ciphertext or not settings.chat_id):
        raise ApiError(409, "notification_not_configured", "启用通知前请配置 Token 和 chat_id")
    if settings.enabled:
        decrypt_token(settings.token_ciphertext)
    settings.updated_at = datetime.now(UTC)
    after = settings_view(settings)
    db.add(
        AuditEvent(
            admin_id=auth[0].id,
            action="notification.settings",
            target_type="telegram_settings",
            target_id=settings.id,
            outcome="succeeded",
            request_id=getattr(request.state, "request_id", None),
            details={
                "before": {"enabled": before["enabled"], "has_token": before["has_token"]},
                "after": {"enabled": after["enabled"], "has_token": after["has_token"]},
                "changed_fields": sorted(payload.model_fields_set),
            },
        )
    )
    db.commit()
    return after


@router.post("/notification-settings/telegram/test", status_code=202)
def test_telegram(
    request: Request,
    db: Db,
    auth: Authenticated,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
) -> dict:
    lock_key(db, f"notification_test:{auth[0].id}:{idempotency_key}")
    previous = db.scalar(
        select(NotificationDelivery).where(
            NotificationDelivery.admin_id == auth[0].id,
            NotificationDelivery.idempotency_key == idempotency_key,
        )
    )
    if previous is not None:
        return {"command_id": previous.id}
    settings = configured_settings(db)
    if initial_delivery_status(settings) == "not_configured":
        raise ApiError(409, "notification_not_configured", "请先配置 Telegram Token 和 chat_id")
    # An explicit administrator test may run while routine notifications are disabled.
    decrypt_token(settings.token_ciphertext)
    delivery = NotificationDelivery(
        id=uuid4(),
        admin_id=auth[0].id,
        idempotency_key=idempotency_key,
        event_status="test",
        status="pending",
    )
    db.add(delivery)
    db.add(
        AuditEvent(
            admin_id=auth[0].id,
            action="notification.test",
            target_type="notification_delivery",
            target_id=delivery.id,
            outcome="queued",
            request_id=getattr(request.state, "request_id", None),
        )
    )
    db.commit()
    return {"command_id": delivery.id}


@router.get("/notification-deliveries/{delivery_id}")
def get_delivery(delivery_id: UUID, db: Db) -> dict:
    delivery = db.get(NotificationDelivery, delivery_id)
    if delivery is None:
        raise ApiError(404, "notification_delivery_not_found", "通知发送记录不存在")
    return {
        "id": delivery.id,
        "command_id": delivery.id,
        "alert_id": delivery.alert_id,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "attempt_history": delivery.attempt_history,
        "last_error": delivery.last_error,
        "next_attempt_at": delivery.next_attempt_at if delivery.status in {"pending", "retrying"} else None,
        "created_at": delivery.created_at,
        "completed_at": delivery.completed_at,
    }


@router.get("/alerts")
def list_alerts(
    db: Db,
    limit: Limit = 50,
    cursor: Cursor = None,
    source_id: UUID | None = None,
    status: Literal["active", "resolved"] | None = None,
) -> dict:
    scope = json.dumps(["alerts", str(source_id) if source_id else None, status])
    query = select(AlertEvent, NotificationDelivery.status, NotificationDelivery.id).outerjoin(
        NotificationDelivery,
        NotificationDelivery.alert_id == AlertEvent.id,
    )
    if source_id is not None:
        query = query.where(AlertEvent.source_id == source_id)
    if status is not None:
        query = query.where(AlertEvent.status == status)
    if cursor is not None:
        timestamp, identifier = decode_cursor(cursor, scope)
        query = query.where(tuple_(AlertEvent.created_at, AlertEvent.id) < (timestamp, identifier))
    rows = db.execute(query.order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc()).limit(limit + 1)).all()
    next_cursor = None
    if len(rows) > limit:
        final = rows[limit - 1][0]
        next_cursor = encode_cursor(scope, final.created_at, final.id)
    return {
        "items": [
            {
                "id": item.id,
                "source_id": item.source_id,
                "kind": item.kind,
                "status": item.status,
                "message": item.message,
                "created_at": item.created_at,
                "delivery_status": delivery_status or "not_configured",
                "occurrences": item.occurrences,
                "delivery_id": delivery_id,
                "last_seen_at": item.last_seen_at,
                "resolved_at": item.resolved_at,
                "related_alert_id": item.related_alert_id,
            }
            for item, delivery_status, delivery_id in rows[:limit]
        ],
        "next_cursor": next_cursor,
    }


@lru_cache(maxsize=1)
def alert_engine():
    return make_engine(get_settings())


def persist_alert(
    source_id: UUID | None,
    kind: str,
    *,
    resolved: bool = False,
    event_id: UUID | None = None,
    observed_at: datetime | None = None,
) -> UUID | None:
    """Commit outside the collection transaction; never perform network I/O here."""
    kind = safe_kind(kind)
    now = observed_at or datetime.now(UTC)
    with Session(alert_engine()) as db, db.begin():
        db.execute(text("SET LOCAL statement_timeout = '3000ms'"))
        db.execute(text("SET LOCAL lock_timeout = '1000ms'"))
        lock_key(db, f"alert:{source_id}:{kind}")
        if event_id is not None and db.get(AlertEvent, event_id) is not None:
            return event_id
        active = db.scalar(
            select(AlertEvent)
            .where(
                AlertEvent.source_id == source_id,
                AlertEvent.kind == kind,
                AlertEvent.status == "active",
            )
            .with_for_update()
        )
        if active is not None and not resolved:
            active.last_seen_at = now
            active.occurrences += 1
            return active.id
        if resolved and active is None:
            return None
        if active is not None:
            active.status = "resolved"
            active.resolved_at = now
            db.flush()
        event = AlertEvent(
            id=event_id or uuid4(),
            source_id=source_id,
            kind=kind,
            status="resolved" if resolved else "active",
            message=safe_summary(kind, resolved=resolved),
            related_alert_id=active.id if active else None,
            created_at=now,
            last_seen_at=now,
            resolved_at=now if resolved else None,
        )
        db.add(event)
        db.flush()
        try:
            settings = configured_settings(db)
        except ApiError as exc:
            # Bad notification secrets must not erase the durable source incident.
            delivery_status = "failed"
            delivery_error = exc.code
        else:
            delivery_status = initial_delivery_status(settings)
            delivery_error = delivery_status if delivery_status != "pending" else None
        db.add(
            NotificationDelivery(
                alert_id=event.id,
                event_status=event.status,
                status=delivery_status,
                completed_at=now if delivery_status != "pending" else None,
                last_error=delivery_error,
            )
        )
        return event.id


async def emit_alert(source_id: UUID | None, kind: str, message: str, resolved: bool = False) -> UUID | None:
    """The caller's raw message is deliberately excluded from storage and delivery."""
    return await asyncio.to_thread(persist_alert, source_id, kind, resolved=resolved)
