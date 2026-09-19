"""Authenticated proxy management; credentials are write-only."""

import json
import os
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from cryptography.fernet import Fernet
from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from marketmind.auth import Authenticated, Db
from marketmind.errors import ApiError
from marketmind.models import AuditEvent, Source
from marketmind.network_policy import validate_proxy_address
from marketmind.pagination import decode_cursor, encode_cursor
from marketmind.proxy_models import ProxyCheck, ProxyEndpoint, ProxyLease, ProxySourceHealth, SourceEgressState

router = APIRouter(tags=["Proxies"])


class ProxyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=100)
    scheme: Literal["http"] = "http"
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    username: SecretStr | None = None
    password: SecretStr | None = None
    enabled: bool = True
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("有效期必须包含时区")
        return value


class ProxyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    scheme: Literal["http"] | None = None
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: SecretStr | None = None
    password: SecretStr | None = None
    enabled: bool | None = None
    expires_at: datetime | None = None


class ProxyImport(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    items: list[dict[str, Any]] = Field(min_length=1, max_length=200)


def cipher() -> Fernet:
    try:
        return Fernet(os.environ["MM_SESSION_KEY"].encode())
    except (KeyError, ValueError):
        raise ApiError(503, "session_key_unavailable", "代理凭证加密密钥未配置或无效") from None


def _credentials(payload: ProxyInput) -> str | None:
    if payload.username is None and payload.password is None:
        return None
    if payload.username is None or payload.password is None:
        raise ApiError(422, "invalid_credentials", "代理用户名和密码必须同时提供")
    content = {"username": payload.username.get_secret_value(), "password": payload.password.get_secret_value()}
    return cipher().encrypt(json.dumps(content).encode()).decode()


def _audit(db: Session, admin_id: UUID, action: str, proxy_id: UUID) -> None:
    db.add(AuditEvent(admin_id=admin_id, action=action, target_type="proxy", target_id=proxy_id, outcome="success"))


def _get(db: Session, proxy_id: UUID, *, lock: bool = False) -> ProxyEndpoint:
    stmt = select(ProxyEndpoint).where(ProxyEndpoint.id == proxy_id)
    row = db.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise ApiError(404, "proxy_not_found", "代理不存在")
    return row


def _serialize(db: Session, proxy: ProxyEndpoint) -> dict[str, Any]:
    health = db.scalars(select(ProxySourceHealth).where(ProxySourceHealth.proxy_id == proxy.id)).all()
    return {
        "id": proxy.id,
        "name": proxy.name,
        "scheme": proxy.scheme,
        "host": proxy.host,
        "port": proxy.port,
        "enabled": proxy.enabled,
        "expires_at": proxy.expires_at,
        "has_credentials": bool(proxy.credentials_encrypted),
        "created_at": proxy.created_at,
        "updated_at": proxy.updated_at,
        "status": "disabled" if not proxy.enabled else "unknown",
        "source_health": [
            {
                "source_id": h.source_id,
                "status": h.status,
                "consecutive_failures": h.consecutive_failures,
                "last_success_at": h.last_success_at,
                "last_checked_at": h.last_checked_at,
                "cooldown_until": h.cooldown_until,
                "error_code": h.error_code,
            }
            for h in health
        ],
    }


def _create(db: Session, payload: ProxyInput, admin_id: UUID) -> ProxyEndpoint:
    try:
        validate_proxy_address(payload.host, payload.port)
    except ValueError:
        raise ApiError(422, "unsafe_proxy", "代理地址解析失败或不在允许的网络范围内") from None
    proxy = ProxyEndpoint(
        **payload.model_dump(exclude={"username", "password"}), credentials_encrypted=_credentials(payload)
    )
    db.add(proxy)
    db.flush()
    _audit(db, admin_id, "proxy.create", proxy.id)
    return proxy


def validate_source_config(db: Session, source_id: UUID, config: dict[str, Any]) -> None:
    del source_id
    ids = config.get("proxy_ids", [])
    try:
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            raise ValueError
        parsed = [UUID(value) for value in ids]
        if len(set(parsed)) != len(parsed) or len(parsed) > 200:
            raise ValueError
        if type(config.get("failover_enabled", False)) is not bool:
            raise ValueError
        if config.get("on_proxy_exhausted", "pause") not in {"pause", "direct"}:
            raise ValueError
        if parsed and "on_proxy_exhausted" not in config:
            raise ValueError
        interval = config.get("request_interval_seconds", 3)
        if type(interval) not in (int, float) or not 3 <= interval <= 86400:
            raise ValueError
    except (ValueError, TypeError):
        raise ApiError(422, "invalid_source_transport", "来源代理顺序、耗尽策略或请求间隔无效") from None
    # Endpoint row locks serialize binding against deletion.
    existing = list(db.scalars(select(ProxyEndpoint.id).where(ProxyEndpoint.id.in_(parsed)).with_for_update()))
    if set(existing) != set(parsed):
        raise ApiError(422, "proxy_not_found", "来源配置包含不存在的代理")


@router.get("/proxies")
def list_proxies(db: Db, auth: Authenticated, cursor: str | None = None, limit: int = Query(50, ge=1, le=200)):
    stmt = select(ProxyEndpoint)
    if cursor:
        stamp, ident = decode_cursor(cursor, "proxies")
        stmt = stmt.where(tuple_(ProxyEndpoint.created_at, ProxyEndpoint.id) < tuple_(stamp, ident))
    rows = list(db.scalars(stmt.order_by(ProxyEndpoint.created_at.desc(), ProxyEndpoint.id.desc()).limit(limit + 1)))
    return {
        "items": [_serialize(db, row) for row in rows[:limit]],
        "next_cursor": encode_cursor("proxies", rows[limit - 1].created_at, rows[limit - 1].id)
        if len(rows) > limit
        else None,
    }


@router.post("/proxies", status_code=201)
def create_proxy(payload: ProxyInput, db: Db, auth: Authenticated):
    row = _create(db, payload, auth[0].id)
    db.commit()
    return _serialize(db, row)


@router.post("/proxies/import")
def import_proxies(payload: ProxyImport, db: Db, auth: Authenticated):
    results = []
    for index, item in enumerate(payload.items):
        try:
            with db.begin_nested():
                row = _create(db, ProxyInput.model_validate(item), auth[0].id)
                result = {"index": index, "id": str(row.id), "status": "created"}
            results.append(result)
        except (ValidationError, ApiError, IntegrityError):
            results.append({"index": index, "status": "error", "message": "代理字段、地址或凭证配置无效"})
    db.commit()
    return {"items": results}


@router.patch("/proxies/{proxy_id}")
def update_proxy(proxy_id: UUID, payload: ProxyPatch, db: Db, auth: Authenticated):
    row = _get(db, proxy_id, lock=True)
    fields = payload.model_fields_set
    base = {key: getattr(row, key) for key in ("name", "scheme", "host", "port", "enabled", "expires_at")}
    base.update(payload.model_dump(exclude_unset=True, exclude={"username", "password"}))
    try:
        normalized = ProxyInput.model_validate(base)
        validate_proxy_address(normalized.host, normalized.port)
    except (ValidationError, ValueError):
        raise ApiError(422, "invalid_proxy", "代理字段、地址或有效期无效") from None
    if fields & {"username", "password"}:
        if not {"username", "password"} <= fields:
            raise ApiError(422, "invalid_credentials", "用户名和密码必须同时更新或清除")
        normalized.username, normalized.password = payload.username, payload.password
        row.credentials_encrypted = _credentials(normalized)
    for key, value in normalized.model_dump(exclude={"username", "password"}).items():
        setattr(row, key, value)
    for health in db.scalars(select(ProxySourceHealth).where(ProxySourceHealth.proxy_id == proxy_id)):
        health.status, health.cooldown_until, health.consecutive_failures = "unknown", None, 0
    _audit(db, auth[0].id, "proxy.update", row.id)
    db.commit()
    return _serialize(db, row)


@router.delete("/proxies/{proxy_id}", status_code=204)
def delete_proxy(proxy_id: UUID, db: Db, auth: Authenticated):
    row = _get(db, proxy_id, lock=True)
    referenced = db.scalar(select(Source.id).where(Source.permission_config["proxy_ids"].contains([str(proxy_id)])))
    active = db.scalar(
        select(ProxyLease.id).where(ProxyLease.proxy_id == proxy_id, ProxyLease.expires_at > datetime.now(UTC))
    )
    checking = db.scalar(
        select(ProxyCheck.id).where(ProxyCheck.proxy_id == proxy_id, ProxyCheck.status.in_(("pending", "running")))
    )
    if referenced or active or checking:
        raise ApiError(409, "proxy_in_use", "代理仍被来源或活动检查引用，不能删除")
    for state in db.scalars(select(SourceEgressState).where(SourceEgressState.proxy_id == proxy_id)):
        db.delete(state)
    # Completed check audit remains in AuditEvent; remove its endpoint-dependent rows.
    for check in db.scalars(select(ProxyCheck).where(ProxyCheck.proxy_id == proxy_id)):
        db.delete(check)
    _audit(db, auth[0].id, "proxy.delete", proxy_id)
    db.delete(row)
    db.commit()


@router.post("/proxies/{proxy_id}/check", status_code=202)
def check_proxy(
    proxy_id: UUID,
    db: Db,
    auth: Authenticated,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
):
    _get(db, proxy_id, lock=True)
    prior = db.scalar(
        select(ProxyCheck).where(
            ProxyCheck.proxy_id == proxy_id,
            ProxyCheck.admin_id == auth[0].id,
            ProxyCheck.idempotency_key == idempotency_key,
        )
    )
    if prior is not None:
        return {"command_id": prior.id}
    row = ProxyCheck(proxy_id=proxy_id, admin_id=auth[0].id, idempotency_key=idempotency_key)
    db.add(row)
    db.flush()
    _audit(db, auth[0].id, "proxy.check", proxy_id)
    db.commit()
    return {"command_id": row.id}


@router.get("/proxy-checks/{check_id}")
def get_check(check_id: UUID, db: Db, auth: Authenticated):
    row = db.get(ProxyCheck, check_id)
    if row is None:
        raise ApiError(404, "proxy_check_not_found", "代理检查不存在")
    return {
        "id": row.id,
        "proxy_id": row.proxy_id,
        "status": row.status,
        "result": row.result,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
    }
