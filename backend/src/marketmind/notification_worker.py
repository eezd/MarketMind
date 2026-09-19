"""Independent Telegram outbox worker and dependency health probe.

Run with ``python -m marketmind.notification_worker``. Notification HTTP I/O never
runs in a collection process or a database transaction. Delivery is at-least-once:
a crash after Telegram accepts a request may repeat that request after lease expiry.
The local outage file has the same boundary: one worker owns the state directory,
and a response lost before its durable commit cannot be deduplicated by Telegram.
"""

import argparse
import asyncio
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

import httpx
from cryptography.fernet import InvalidToken
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from marketmind.alert_models import AlertEvent, NotificationDelivery
from marketmind.alerts import (
    alert_engine,
    configured_settings,
    decrypt_token,
    emit_alert,
    notification_cipher,
    persist_alert,
    safe_summary,
)
from marketmind.config import get_settings
from marketmind.errors import ApiError
from marketmind.models import Source

MAX_ATTEMPTS = 5
LEASE_SECONDS = 90
HTTP_DEADLINE = 25
SAFE_SOURCE_NAMES = {"jin10": "金十数据", "cls": "财联社", "wscn": "华尔街见闻"}


@dataclass(frozen=True)
class SendResult:
    status: str
    error: str | None = None
    retry_after: int | None = None
    retryable: bool = False


@dataclass(frozen=True)
class ClaimedDelivery:
    id: UUID
    lease_token: UUID
    text: str
    event_status: str


def state_directory() -> Path:
    path = Path(os.environ.get("MM_NOTIFICATION_STATE_DIR", ".private/notifications"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("notification_state_directory_not_private")
    return path


def private_read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("notification_state_file_not_private")
        data = file.read(65537)
        if len(data) > 65536:
            raise RuntimeError("notification_state_file_too_large")
        return data


def private_write(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".notification-")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def durable_log(directory: Path, event: str, *, status: str | None = None, identifier: str | None = None) -> None:
    """Only controlled codes enter the journal; daily archives are never deleted."""
    now = datetime.now(UTC)
    record = {"at": now.isoformat(), "event": event, "status": status, "id": identifier}
    path = directory / f"notification-{now:%Y-%m-%d}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("notification_log_not_private")
        file.write((json.dumps(record, separators=(",", ":")) + "\n").encode())
        file.flush()
        os.fsync(file.fileno())


def read_configuration() -> dict:
    with Session(alert_engine()) as db, db.begin():
        settings = configured_settings(db)
        return {
            "enabled": bool(settings and settings.enabled),
            "chat_id": settings.chat_id if settings else None,
            "token_ciphertext": settings.token_ciphertext if settings else None,
        }


def cache_configuration(directory: Path, config: dict) -> None:
    path = directory / "telegram-config.enc"
    try:
        cipher = notification_cipher()
    except ApiError:
        # Do not leave stale credentials available after configuration is cleared.
        path.unlink(missing_ok=True)
        if config["enabled"]:
            durable_log(directory, "configuration_unavailable", status="encryption_key_unavailable")
        return
    private_write(path, cipher.encrypt(json.dumps(config).encode()))


def offline_configuration(directory: Path) -> dict | None:
    path = directory / "telegram-config.enc"
    if path.exists():
        try:
            config = json.loads(notification_cipher().decrypt(private_read(path)))
            if config.get("enabled") and config.get("chat_id") and config.get("token_ciphertext"):
                return config
            return None
        except (ApiError, InvalidToken, ValueError, OSError, RuntimeError):
            durable_log(directory, "offline_configuration_unavailable", status="invalid_encrypted_cache")
            return None
    # Cold-start outage notification is explicitly opted in; never invent a Bot.
    if os.environ.get("MM_TELEGRAM_ENABLED", "").lower() != "true":
        return None
    token = os.environ.get("MM_TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("MM_TELEGRAM_CHAT_ID")
    if not token or not chat:
        return None
    try:
        from marketmind.alerts import TelegramPatch

        TelegramPatch(enabled=True, token=token, chat_id=chat)
        ciphertext = notification_cipher().encrypt(token.encode("ascii")).decode("ascii")
    except (ApiError, ValueError, UnicodeError):
        durable_log(directory, "offline_configuration_unavailable", status="invalid_environment_configuration")
        return None
    return {"enabled": True, "chat_id": chat, "token_ciphertext": ciphertext}


async def send_telegram(config: dict, message: str) -> SendResult:
    if not config.get("chat_id") or not config.get("token_ciphertext"):
        return SendResult("not_configured", "not_configured")
    try:
        token = decrypt_token(config["token_ciphertext"])
    except ApiError:
        return SendResult("failed", "encryption_key_unavailable")
    proxy = os.environ.get("MM_TELEGRAM_PROXY_URL") or None
    if proxy is not None:
        try:
            parsed = urlsplit(proxy)
            if (
                parsed.scheme not in {"http", "https", "socks5", "socks5h"}
                or not parsed.hostname
                or parsed.port is None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                return SendResult("failed", "telegram_proxy_invalid")
        except ValueError:
            return SendResult("failed", "telegram_proxy_invalid")
    # Avoid HTTP client log records containing a URL with the Bot Token in its path.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        async with asyncio.timeout(HTTP_DEADLINE):
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                proxy=proxy,
            ) as client:
                async with client.stream(
                    "POST",
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": config["chat_id"], "text": message, "disable_web_page_preview": True},
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            return SendResult("failed", "telegram_response_too_large")
                    try:
                        payload = json.loads(body)
                    except (ValueError, UnicodeError):
                        payload = {}
                    if not isinstance(payload, dict):
                        payload = {}
                    if response.status_code == 200 and payload.get("ok") is True:
                        return SendResult("sent")
                    if response.status_code == 429 or payload.get("error_code") == 429:
                        parameters = payload.get("parameters")
                        delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
                        delay = min(max(delay, 1), 3600) if type(delay) is int else None
                        return SendResult("failed", "telegram_rate_limited", delay, True)
                    if response.status_code >= 500:
                        return SendResult("failed", "telegram_unavailable", retryable=True)
                    if response.status_code in {401, 403}:
                        return SendResult("failed", "telegram_access_denied")
                    return SendResult("failed", "telegram_rejected")
    except (httpx.HTTPError, TimeoutError):
        # Never stringify exceptions: HTTP exceptions include credential-bearing URLs.
        return SendResult("failed", "telegram_network_error", retryable=True)
    except ImportError:
        return SendResult("failed", "telegram_proxy_dependency_unavailable")
    except ValueError:
        return SendResult("failed", "telegram_proxy_invalid")


def claim_delivery() -> ClaimedDelivery | None:
    now = datetime.now(UTC)
    with Session(alert_engine()) as db, db.begin():
        item = db.scalar(
            select(NotificationDelivery)
            .where(
                or_(
                    and_(
                        NotificationDelivery.status.in_(["pending", "retrying"]),
                        NotificationDelivery.next_attempt_at <= now,
                    ),
                    and_(NotificationDelivery.status == "sending", NotificationDelivery.lease_expires_at <= now),
                )
            )
            .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            return None
        if item.status == "sending":
            history = list(item.attempt_history)
            if history:
                history[-1] = {**history[-1], "status": "unknown", "error": "delivery_lease_expired"}
            item.attempt_history = history
        if item.attempts >= MAX_ATTEMPTS:
            item.status = "failed"
            item.last_error = "delivery_lease_expired"
            item.completed_at = now
            item.lease_token = None
            item.lease_expires_at = None
            return None
        item.status = "sending"
        item.attempts += 1
        item.lease_token = uuid4()
        item.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        item.attempt_history = [
            *item.attempt_history,
            {
                "attempt": item.attempts,
                "at": now.isoformat(),
                "status": "sending",
            },
        ]
        if item.event_status == "test":
            message = f"MarketMind 管理员测试通知\n时间：{now.isoformat()}\n此通知由管理员显式请求。"
        else:
            event = db.get(AlertEvent, item.alert_id)
            source_row = db.get(Source, event.source_id) if event.source_id else None
            source = SAFE_SOURCE_NAMES.get(source_row.code, str(event.source_id)) if source_row else "系统依赖"
            status = "已恢复" if item.event_status == "resolved" else "故障"
            message = (
                f"MarketMind 告警\n来源：{source}\n状态：{status}\n时间：{event.created_at.isoformat()}\n"
                f"摘要：{safe_summary(event.kind, resolved=item.event_status == 'resolved')}"
            )
        # Only the prevalidated deployment origin may become a management link.
        origins = sorted(get_settings().origins)
        if origins:
            message += f"\n管理页：{origins[0]}/"
        return ClaimedDelivery(item.id, item.lease_token, message, item.event_status)


def _retry_delay(attempts: int, retry_after: int | None = None) -> int:
    return max(min(15 * 2 ** (attempts - 1), 900), retry_after or 0)


def finish_delivery(claim: ClaimedDelivery, result: SendResult) -> None:
    now = datetime.now(UTC)
    with Session(alert_engine()) as db, db.begin():
        item = db.scalar(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.id == claim.id,
                NotificationDelivery.lease_token == claim.lease_token,
                NotificationDelivery.status == "sending",
            )
            .with_for_update()
        )
        if item is None:
            return
        retry = result.retryable and item.attempts < MAX_ATTEMPTS
        item.status = "retrying" if retry else result.status
        item.last_error = result.error
        if retry:
            delay = _retry_delay(item.attempts, result.retry_after)
            item.next_attempt_at = now + timedelta(seconds=delay)
        else:
            item.completed_at = now
        history = list(item.attempt_history)
        history[-1] = {**history[-1], "status": result.status, "error": result.error, "completed_at": now.isoformat()}
        item.attempt_history = history
        item.lease_token = None
        item.lease_expires_at = None


def _read_incident(path: Path) -> dict:
    incident = json.loads(private_read(path))
    if "attempts" not in incident:
        # Old files did not distinguish retryable failures or record attempts.
        # A failed first request is therefore eligible for bounded recovery.
        status = incident["delivery_status"]
        incident["attempts"] = 0 if status == "not_attempted" else 1
        incident["delivery_status"] = {"not_attempted": "pending", "failed": "retrying"}.get(status, status)
        incident["next_attempt_at"] = incident["observed_at"]
        incident["last_error"] = None
        incident["completed_at"] = incident["observed_at"] if status in {"sent", "disabled", "not_configured"} else None
        incident["attempt_history"] = (
            [] if status == "not_attempted" else [{"attempt": 1, "at": incident["observed_at"], "status": status}]
        )
    return incident


async def handle_database_outage(directory: Path) -> None:
    incident_path = directory / "database-incident.json"
    now = datetime.now(UTC)
    if incident_path.exists():
        incident = _read_incident(incident_path)
    else:
        incident = {
            "id": str(uuid4()),
            "observed_at": now.isoformat(),
            "delivery_status": "pending",
            "attempts": 0,
            "next_attempt_at": now.isoformat(),
            "last_error": None,
            "completed_at": None,
            "attempt_history": [],
        }
        private_write(incident_path, json.dumps(incident).encode())
        durable_log(directory, "database_unavailable", identifier=incident["id"])
    if incident["delivery_status"] in {"sent", "failed", "disabled", "not_configured"}:
        return
    if datetime.fromisoformat(incident["next_attempt_at"]) > now:
        return
    if incident["delivery_status"] == "sending":
        incident["attempt_history"][-1].update(status="unknown", error="delivery_lease_expired")
    if incident["attempts"] >= MAX_ATTEMPTS:
        incident.update(delivery_status="failed", last_error="delivery_lease_expired", completed_at=now.isoformat())
        private_write(incident_path, json.dumps(incident).encode())
        return
    config = offline_configuration(directory)
    # Claim durably before HTTP. An interrupted claim becomes retryable only after
    # the same lease used by the database outbox; it still consumes an attempt.
    incident["attempts"] += 1
    incident.update(delivery_status="sending", next_attempt_at=(now + timedelta(seconds=LEASE_SECONDS)).isoformat())
    incident["attempt_history"].append({"attempt": incident["attempts"], "at": now.isoformat(), "status": "sending"})
    private_write(incident_path, json.dumps(incident).encode())
    if config is None:
        result = SendResult("not_configured", "not_configured")
    else:
        origins = sorted(get_settings().origins)
        message = (
            f"MarketMind 系统告警\n来源：系统依赖\n状态：故障\n时间：{incident['observed_at']}\n"
            "摘要：数据库依赖不可用。"
        )
        if origins:
            message += f"\n管理页：{origins[0]}/"
        result = await send_telegram(config, message)
    now = datetime.now(UTC)
    retry = result.retryable and incident["attempts"] < MAX_ATTEMPTS
    incident.update(
        delivery_status="retrying" if retry else result.status,
        last_error=result.error,
        completed_at=None if retry else now.isoformat(),
    )
    if retry:
        incident["next_attempt_at"] = (
            now + timedelta(seconds=_retry_delay(incident["attempts"], result.retry_after))
        ).isoformat()
    incident["attempt_history"][-1].update(status=result.status, error=result.error, completed_at=now.isoformat())
    private_write(incident_path, json.dumps(incident).encode())
    durable_log(
        directory, "database_outage_notification", status=result.error or result.status, identifier=incident["id"]
    )


def reconcile_database_outage(directory: Path) -> None:
    path = directory / "database-incident.json"
    if not path.exists():
        return
    incident = _read_incident(path)
    identifier = UUID(incident["id"])
    event_id = persist_alert(
        None,
        "database_unavailable",
        event_id=identifier,
        observed_at=datetime.fromisoformat(incident["observed_at"]),
    )
    with Session(alert_engine()) as db, db.begin():
        delivery = db.scalar(
            select(NotificationDelivery).where(NotificationDelivery.alert_id == event_id).with_for_update()
        )
        if delivery and delivery.status not in {"sent", "sending"}:
            imported = any(item.get("incident_id") == incident["id"] for item in delivery.attempt_history)
            # Do not reset work performed by an outbox worker after an earlier
            # partial reconciliation. A known local success can stop retries.
            if (
                delivery.attempts == 0
                or incident["delivery_status"] == "sent"
                or (imported and incident["attempts"] > delivery.attempts)
            ):
                status = incident["delivery_status"]
                if status == "sending":
                    status = "retrying" if incident["attempts"] < MAX_ATTEMPTS else "failed"
                    incident["last_error"] = "delivery_lease_expired"
                    incident["attempt_history"][-1].update(status="unknown", error="delivery_lease_expired")
                delivery.status = status
                delivery.attempts = max(incident["attempts"], delivery.attempts)
                delivery.last_error = incident["last_error"]
                delivery.next_attempt_at = datetime.fromisoformat(incident["next_attempt_at"])
                completed_at = incident["completed_at"]
                delivery.completed_at = (
                    (datetime.fromisoformat(completed_at) if completed_at else datetime.now(UTC))
                    if status in {"sent", "failed", "disabled", "not_configured"}
                    else None
                )
                delivery.attempt_history = [
                    *(item for item in delivery.attempt_history if item.get("incident_id") != incident["id"]),
                    *(
                        {**item, "channel": "offline_health_probe", "incident_id": incident["id"]}
                        for item in incident["attempt_history"]
                    ),
                ]
    persist_alert(None, "database_unavailable", resolved=True, event_id=uuid5(identifier, "recovered"))
    durable_log(directory, "database_recovered", identifier=str(identifier))
    # The immutable journal and database retain the incident; only coordination state is removed.
    path.unlink()


async def check_redis(redis: Redis, directory: Path) -> None:
    try:
        await redis.ping()
    except (RedisError, OSError, TimeoutError):
        await emit_alert(None, "redis_unavailable", "")
        durable_log(directory, "redis_health", status="unavailable")
    else:
        await emit_alert(None, "redis_unavailable", "", resolved=True)


async def run_worker(*, once: bool = False, health_only: bool = False, poll_seconds: float = 2) -> None:
    directory = state_directory()
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url.get_secret_value(), socket_timeout=3, socket_connect_timeout=3)
    cached_configuration = None
    next_health = 0.0
    try:
        while True:
            try:
                config = await asyncio.to_thread(read_configuration)
                if config != cached_configuration:
                    await asyncio.to_thread(cache_configuration, directory, config)
                    cached_configuration = config
                await asyncio.to_thread(reconcile_database_outage, directory)
                now = asyncio.get_running_loop().time()
                if now >= next_health:
                    await check_redis(redis, directory)
                    next_health = now + 30
                claim = None if health_only else await asyncio.to_thread(claim_delivery)
                if claim is not None:
                    if not config["enabled"] and claim.event_status != "test":
                        result = SendResult("disabled", "disabled")
                    else:
                        result = await send_telegram(config, claim.text)
                    await asyncio.to_thread(finish_delivery, claim, result)
                    await asyncio.to_thread(
                        durable_log,
                        directory,
                        "notification_attempt",
                        status=result.error or result.status,
                        identifier=str(claim.id),
                    )
            except SQLAlchemyError:
                await handle_database_outage(directory)
                if once:
                    raise RuntimeError("notification_database_unavailable") from None
                await asyncio.sleep(10)
                continue
            except (ApiError, OSError, ValueError, RuntimeError):
                # Neither stringify secrets nor recursively emit a notification-failure alert.
                durable_log(directory, "notification_cycle_failed", status="worker_configuration_or_state_error")
                if once:
                    raise RuntimeError("notification_worker_cycle_failed") from None
                await asyncio.sleep(10)
                continue
            if once:
                return
            await asyncio.sleep(0 if claim else poll_seconds)
    finally:
        await redis.aclose()
        alert_engine().dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="MarketMind Telegram outbox and dependency health worker")
    parser.add_argument("--once", action="store_true", help="Run one notification/health cycle")
    parser.add_argument("--health-only", action="store_true", help="Only probe dependencies and reconcile outages")
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args()
    if not 0.1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.1 and 60")
    try:
        asyncio.run(run_worker(once=args.once, health_only=args.health_only, poll_seconds=args.poll_seconds))
    except KeyboardInterrupt:
        return
    except Exception:
        # CLI stderr must never expose connection strings, tokens, or response bodies.
        parser.exit(1, "notification_worker_failed: check the persistent redacted notification journal\n")


if __name__ == "__main__":
    main()
