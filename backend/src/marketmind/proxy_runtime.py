"""Sticky source egress, shared request budgets and durable proxy-check worker."""

import asyncio
import base64
import json
import random
import secrets
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import InvalidToken
from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from marketmind.collection_types import SourceError
from marketmind.config import get_settings
from marketmind.db import make_engine
from marketmind.models import Source
from marketmind.network_policy import CHECK_TARGETS, validate_proxy_address, validate_target_url
from marketmind.proxies import cipher
from marketmind.proxy_models import ProxyCheck, ProxyEndpoint, ProxyLease, ProxySourceHealth, SourceEgressState

request_context: ContextVar[dict[str, Any] | None] = ContextVar("source_request_context", default=None)
guard_request = validate_target_url

# No reservations: a waiter rechecks the newest source-wide backoff before admission.
_RATE_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local due = math.max(tonumber(redis.call('GET', KEYS[1]) or 0), tonumber(redis.call('GET', KEYS[2]) or 0))
if due > now then return due - now end
redis.call('SET', KEYS[1], now + tonumber(ARGV[1]), 'PX', tonumber(ARGV[1]) + 1000)
return 0
"""
_BACKOFF_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local until_at = math.max(tonumber(redis.call('GET', KEYS[1]) or 0), now + tonumber(ARGV[1]))
redis.call('SET', KEYS[1], until_at, 'PX', until_at - now + 1000)
return until_at
"""
_RESPONSE_SCRIPT = """
local failures = tonumber(redis.call('GET', KEYS[1]) or 0)
if ARGV[1] == 'failure' then
    failures = redis.call('INCR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], 3600)
    if failures >= 3 then redis.call('SET', KEYS[2], 1) end
    return failures
end
redis.call('DEL', KEYS[1])
-- Retain recovery intent until the DB write succeeds, including pre-upgrade counters.
if failures >= 3 then redis.call('SET', KEYS[2], 1) end
return redis.call('EXISTS', KEYS[2])
"""


@lru_cache
def _engine():
    return make_engine(get_settings())


@lru_cache
def _redis():
    return Redis.from_url(get_settings().redis_url.get_secret_value(), socket_connect_timeout=3, socket_timeout=5)


def current_transport() -> dict[str, Any]:
    context = request_context.get()
    if context is None:
        raise SourceError("transport_unconfigured", "请求必须绑定明确的来源出口")
    if context.get("lease_lost"):
        raise SourceError("transport_unconfigured", "出口租约无法续期，已停止请求")
    return context


async def browser_continue(route: Any) -> None:
    """Follow redirects ourselves: Chromium routes alone do not intercept redirect hops."""
    from urllib.parse import urljoin, urlsplit

    context = current_transport()
    url, method = route.request.url, route.request.method
    headers = dict(route.request.headers)
    for hop in range(6):
        await guard_request(url, context["source_code"])
        if hop:
            await throttle(context["source_id"])
        try:
            response = await route.fetch(url=url, method=method, headers=headers, max_redirects=0, timeout=30_000)
        except Exception as exc:
            if is_connection_failure(exc):
                await note_transport_failure()
            await route.abort()
            raise SourceError(
                "transport_error" if is_connection_failure(exc) else "access_denied", "浏览器网络请求失败"
            ) from None
        try:
            await note_response(response.status, response.headers)
            if response.status not in (301, 302, 303, 307, 308):
                await route.fulfill(response=response)
                return
            location = response.headers.get("location")
            if not location:
                await route.abort()
                return
            next_url = urljoin(url, location)
            await guard_request(next_url, context["source_code"])
            if urlsplit(next_url).netloc != urlsplit(url).netloc:
                headers = {
                    key: value
                    for key, value in headers.items()
                    if key.lower() not in {"authorization", "cookie", "host"}
                }
            if response.status == 303 or response.status in (301, 302) and method == "POST":
                method = "GET"
                headers = {
                    key: value
                    for key, value in headers.items()
                    if key.lower() not in {"content-length", "content-type"}
                }
            url = next_url
        finally:
            await response.dispose()
    await route.abort()
    raise SourceError("unsafe_target", "官方请求重定向次数超限")


def is_connection_failure(error: Exception) -> bool:
    return any(
        code in str(error)
        for code in (
            "net::ERR_PROXY_CONNECTION_FAILED",
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            "net::ERR_CONNECTION_REFUSED",
            "net::ERR_CONNECTION_RESET",
            "net::ERR_CONNECTION_CLOSED",
            "net::ERR_NAME_NOT_RESOLVED",
            "net::ERR_CONNECTION_TIMED_OUT",
        )
    )


async def throttle(source_id: UUID) -> None:
    context = current_transport()
    if context["source_id"] != source_id:
        raise SourceError("transport_unconfigured", "请求来源与出口不匹配")
    interval = max(3.0, float(context.get("interval_seconds", 3)))
    redis = _redis()
    try:
        while True:
            wait = await redis.eval(
                _RATE_SCRIPT,
                2,
                f"mm:rate:{{{source_id}}}:next",
                f"mm:rate:{{{source_id}}}:backoff",
                int(interval * 1000),
            )
            if not wait:
                return
            await asyncio.sleep(min(wait / 1000, 30))
    except asyncio.CancelledError:
        raise
    except Exception:
        raise SourceError("rate_limit_unavailable", "共享限速不可用，已停止发起请求") from None


async def _emit(source_id: UUID, kind: str, message: str, *, resolved: bool = False) -> None:
    from marketmind.alerts import emit_alert

    await emit_alert(source_id, kind, message, resolved=resolved)


async def note_response(status: int, headers: Any) -> None:
    context = current_transport()
    failed_response = status == 429 or status >= 500
    # A transport-tainted context cannot establish recovery. Health sampling is
    # once per context, but source recovery must observe every eligible response.
    recovered_response = 200 <= status < 300 and not context.get("failed")
    if not failed_response and not recovered_response:
        return
    redis = _redis()
    source_id = context["source_id"]
    prefix = f"mm:rate:{{{source_id}}}"
    try:
        # Serialize Redis transitions AND alert commits across workers, so an
        # overlapping success cannot resolve before the failure alert is opened.
        async with redis.lock(f"{prefix}:response-lock", timeout=60, blocking_timeout=5):
            state = await redis.eval(
                _RESPONSE_SCRIPT,
                2,
                f"{prefix}:failures",
                f"{prefix}:alert-active",
                "failure" if failed_response else "success",
            )
            if failed_response:
                delay = min(600, 30 * 2 ** min(state - 1, 5)) + random.uniform(0, 3)
                value = headers.get("retry-after")
                if value:
                    try:
                        delay = max(delay, float(value))
                    except (TypeError, ValueError):
                        try:
                            delay = max(delay, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
                        except (TypeError, ValueError, OverflowError):
                            pass
                # Recovery resets the streak, never a still-valid Retry-After.
                await redis.eval(_BACKOFF_SCRIPT, 1, f"{prefix}:backoff", int(delay * 1000))
                if state >= 3:
                    await _emit(source_id, "rate_limited", "来源持续限速或服务不可用，所有出口共享退避")
            elif state:
                await _emit(source_id, "rate_limited", "来源请求已恢复", resolved=True)
                await redis.delete(f"{prefix}:alert-active")
    except Exception:
        raise SourceError("rate_limit_unavailable", "无法记录来源退避或恢复，已停止请求") from None
    if recovered_response and not context.get("health_recorded"):
        await asyncio.to_thread(_record_health, context, True, None)
        context["health_recorded"] = True


def _record_health(context: dict[str, Any], healthy: bool, error: str | None) -> None:
    proxy_id = context.get("proxy_id")
    if proxy_id is None:
        return
    with Session(_engine()) as db, db.begin():
        db.scalar(select(ProxyEndpoint).where(ProxyEndpoint.id == proxy_id).with_for_update())
        row = db.get(ProxySourceHealth, (proxy_id, context["source_id"]))
        if row is None:
            row = ProxySourceHealth(proxy_id=proxy_id, source_id=context["source_id"], consecutive_failures=0)
            db.add(row)
        row.last_checked_at = datetime.now(UTC)
        row.error_code = error
        if healthy:
            row.status, row.consecutive_failures, row.cooldown_until = "healthy", 0, None
            row.last_success_at = datetime.now(UTC)
        else:
            row.consecutive_failures += 1
            row.status = "cooling_down"
            row.cooldown_until = datetime.now(UTC) + timedelta(
                seconds=min(600, 30 * 2 ** min(row.consecutive_failures - 1, 5))
            )


async def note_transport_failure() -> None:
    context = current_transport()
    if not context.get("failed"):
        context["failed"] = True
        await asyncio.to_thread(_record_health, context, False, "connection_failed")


def _proxy_options(proxy: ProxyEndpoint) -> tuple[str, dict[str, str]]:
    address = validate_proxy_address(proxy.host, proxy.port)
    host = f"[{address}]" if ":" in address else address
    endpoint = f"http://{host}:{proxy.port}"
    browser = {"server": endpoint}
    if proxy.credentials_encrypted:
        try:
            credentials = json.loads(cipher().decrypt(proxy.credentials_encrypted.encode()))
            username, password = credentials["username"], credentials["password"]
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise SourceError("proxy_credentials_unavailable", "代理凭证无法解密") from None
        browser.update(username=username, password=password)
        endpoint = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{proxy.port}"
    return endpoint, browser


def _select_transport(source_id: UUID) -> dict[str, Any]:
    with Session(_engine()) as db, db.begin():
        source = db.scalar(select(Source).where(Source.id == source_id).with_for_update())
        if source is None:
            raise SourceError("transport_unconfigured", "来源不存在")
        config = source.permission_config
        ids = [UUID(value) for value in config.get("proxy_ids", [])]
        state = db.get(SourceEgressState, source_id)
        previous = state.egress_id if state else None
        reset = state is None or state.config_version != source.config_version
        if state is None:
            state = SourceEgressState(source_id=source_id, config_version=source.config_version, reason="initializing")
            db.add(state)
        if reset:
            state.proxy_id, state.egress_id = None, "direct"
            state.fallback_direct, state.paused = False, False
            state.config_version = source.config_version
        context = {
            "source_id": source_id,
            "source_code": source.code,
            "interval_seconds": max(3, config.get("request_interval_seconds", 3)),
            "httpx_proxy": None,
            "playwright_proxy": None,
            "egress_id": "direct",
            "proxy_id": None,
        }
        if state.paused:
            context["paused"] = True
            return context
        proxy = None
        now = datetime.now(UTC)
        if ids and not state.fallback_direct:
            candidates = ids
            if state.proxy_id in ids and not reset:
                candidates = ids[ids.index(state.proxy_id) :]
            if not config.get("failover_enabled", False):
                candidates = candidates[:1]
            for identifier in candidates:
                candidate = db.get(ProxyEndpoint, identifier)
                health = db.get(ProxySourceHealth, (identifier, source_id))
                if candidate is None or not candidate.enabled or candidate.expires_at and candidate.expires_at <= now:
                    continue
                if health and (health.status == "unavailable" or health.cooldown_until and health.cooldown_until > now):
                    continue
                proxy = candidate
                break
            if proxy is None:
                state.paused = config.get("on_proxy_exhausted", "pause") == "pause"
                state.fallback_direct = not state.paused
                state.reason = "proxy_exhausted_pause" if state.paused else "proxy_exhausted_direct"
                state.proxy_id, state.egress_id = None, "paused" if state.paused else "direct"
                context.update(paused=state.paused, exhausted=True)
        if proxy:
            try:
                context["httpx_proxy"], context["playwright_proxy"] = _proxy_options(proxy)
            except ValueError:
                raise SourceError("unsafe_proxy", "代理 DNS 或网络白名单校验失败") from None
            context["proxy_id"], context["egress_id"] = proxy.id, str(proxy.id)
            state.proxy_id, state.egress_id = proxy.id, str(proxy.id)
            state.reason = (
                "configuration_applied"
                if reset
                else "connection_failover"
                if previous != state.egress_id
                else state.reason
            )
            lease_id = uuid4()
            db.add(
                ProxyLease(id=lease_id, proxy_id=proxy.id, source_id=source_id, expires_at=now + timedelta(seconds=120))
            )
            context["lease_id"] = lease_id
        elif not ids:
            state.proxy_id, state.egress_id, state.reason = None, "direct", "default_direct"
        context["egress_changed"] = previous is not None and previous != state.egress_id
        return context


async def reset_source_transport(source_id: UUID) -> None:
    def reset():
        with Session(_engine()) as db, db.begin():
            db.scalar(select(Source).where(Source.id == source_id).with_for_update())
            state = db.get(SourceEgressState, source_id)
            if state:
                db.delete(state)

    await asyncio.to_thread(reset)


def _update_lease(lease_id: UUID, *, release: bool = False) -> None:
    with Session(_engine()) as db, db.begin():
        lease = db.get(ProxyLease, lease_id)
        if lease:
            if release:
                db.delete(lease)
            else:
                lease.expires_at = datetime.now(UTC) + timedelta(seconds=120)


async def _lease_heartbeat(context: dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(_update_lease, context["lease_id"])
        except Exception:
            context["lease_lost"] = True
            return


@asynccontextmanager
async def _browser_proxy(context: dict[str, Any]):
    """A local authenticated CONNECT bridge pins actual sockets, not only URL checks.

    TLS remains end-to-end between Chromium and the official hostname. With an
    upstream proxy, CONNECT names the already validated target IP, never a new
    DNS name, and an upstream failure cannot silently fall back to direct.
    """
    if context["source_code"] not in {"cls", "jin10"}:
        yield
        return
    upstream = context["playwright_proxy"]
    password = secrets.token_urlsafe(32)
    authorization = "Basic " + base64.b64encode(f"marketmind:{password}".encode()).decode()
    tasks: set[asyncio.Task] = set()

    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def tunnel(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        remote = None
        token = request_context.set(context)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = raw.decode("latin1").split("\r\n")
            method, authority, version = lines[0].split(" ")
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            supplied = next(
                (value.strip() for key, value in headers.items() if key.lower() == "proxy-authorization"), ""
            )
            if not secrets.compare_digest(supplied, authorization):
                writer.write(
                    b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="MarketMind"\r\n'
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise SourceError("unsafe_target", "出口只允许官方 HTTPS CONNECT")
            target = urlsplit(f"https://{authority}")
            if target.path or target.query or target.fragment or target.port != 443:
                raise SourceError("unsafe_target", "CONNECT 目标格式无效")
            address = await guard_request(f"https://{authority}/", context["source_code"])
            pinned = f"[{address}]" if ":" in address else address
            try:
                if upstream is None:
                    remote_reader, remote = await asyncio.wait_for(asyncio.open_connection(address, 443), 10)
                else:
                    proxy = urlsplit(upstream["server"])
                    remote_reader, remote = await asyncio.wait_for(
                        asyncio.open_connection(proxy.hostname, proxy.port, limit=16384), 10
                    )
                    authentication = ""
                    if "username" in upstream:
                        credentials = base64.b64encode(
                            f"{upstream['username']}:{upstream['password']}".encode()
                        ).decode()
                        authentication = f"Proxy-Authorization: Basic {credentials}\r\n"
                    remote.write(
                        f"CONNECT {pinned}:443 HTTP/1.1\r\nHost: {pinned}:443\r\n{authentication}\r\n".encode()
                    )
                    await remote.drain()
                    response = await asyncio.wait_for(remote_reader.readuntil(b"\r\n\r\n"), 10)
                    if response.split(b"\r\n", 1)[0].split(b" ")[1:2] != [b"200"]:
                        raise OSError("Upstream CONNECT rejected")
            except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                await note_transport_failure()
                raise SourceError("transport_error", "受控出口连接失败") from None
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            pumps = [asyncio.create_task(pipe(reader, remote)), asyncio.create_task(pipe(remote_reader, writer))]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pump in pumps:
                    pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
        except Exception:
            # No endpoint, credential, URL or raw transport exception is logged.
            with suppress(OSError):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
        finally:
            request_context.reset(token)
            if remote:
                remote.close()
            writer.close()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(tunnel(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=16384)
    port = server.sockets[0].getsockname()[1]
    context["playwright_proxy"] = {"server": f"http://127.0.0.1:{port}", "username": "marketmind", "password": password}
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        active = list(tasks)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        context["playwright_proxy"] = upstream


@asynccontextmanager
async def source_transport(source_id: UUID):
    context = await asyncio.to_thread(_select_transport, source_id)
    if context.get("exhausted"):
        await _emit(
            source_id,
            "proxy_exhausted" if context.get("paused") else "proxy_direct_fallback",
            "指定代理全部不可用，已暂停来源" if context.get("paused") else "指定代理全部不可用，已按配置回退直连",
        )
    if context.get("paused"):
        raise SourceError("proxy_exhausted", "来源出口已暂停，需管理员显式恢复")
    token = request_context.set(context)
    heartbeat = asyncio.create_task(_lease_heartbeat(context)) if context.get("lease_id") else None
    try:
        async with _browser_proxy(context):
            yield context
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError):
        await note_transport_failure()
        raise SourceError("transport_error", "代理或目标连接失败，等待有界重试") from None
    finally:
        request_context.reset(token)
        if heartbeat:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await asyncio.to_thread(_update_lease, context["lease_id"], release=True)


async def http_request_hook(request: httpx.Request) -> None:
    context = current_transport()
    address = await guard_request(str(request.url), context["source_code"])
    request.extensions["marketmind_original_url"] = str(request.url)
    if context["httpx_proxy"] is None:
        # Preserve Host and TLS identity while preventing a second DNS resolution.
        request.extensions["sni_hostname"] = request.url.host
        request.url = request.url.copy_with(host=address)
    await throttle(context["source_id"])


async def http_response_hook(response: httpx.Response) -> None:
    await note_response(response.status_code, response.headers)
    if response.is_redirect:
        location = response.headers.get("location")
        if location:
            origin = httpx.URL(response.request.extensions.get("marketmind_original_url", str(response.url)))
            await guard_request(str(origin.join(location)), current_transport()["source_code"])


def _claim_check() -> UUID | None:
    now = datetime.now(UTC)
    with Session(_engine()) as db, db.begin():
        row = db.scalar(
            select(ProxyCheck)
            .where(
                or_(ProxyCheck.status == "pending", (ProxyCheck.status == "running") & (ProxyCheck.lease_until < now))
            )
            .order_by(ProxyCheck.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.status, row.lease_until = "running", now + timedelta(minutes=4)
        return row.id


def _check_details(check_id: UUID):
    with Session(_engine()) as db:
        job = db.get(ProxyCheck, check_id)
        proxy = db.get(ProxyEndpoint, job.proxy_id)
        sources = [
            (source.id, source.code, source.permission_config.get("request_interval_seconds", 3))
            for source in db.scalars(select(Source))
            if source.code in CHECK_TARGETS
        ]
        if not proxy.enabled or proxy.expires_at and proxy.expires_at <= datetime.now(UTC):
            return proxy.id, None, sources
        return proxy.id, _proxy_options(proxy), sources


def _finish_check(check_id: UUID, result: dict[str, Any], succeeded: bool) -> None:
    with Session(_engine()) as db, db.begin():
        row = db.get(ProxyCheck, check_id)
        row.status = "succeeded" if succeeded else "failed"
        row.result, row.completed_at, row.lease_until = result, datetime.now(UTC), None


async def run_check(check_id: UUID) -> None:
    results: list[dict[str, Any]] = []
    try:
        proxy_id, options, sources = await asyncio.wait_for(asyncio.to_thread(_check_details, check_id), timeout=15)
        if options is None:
            await asyncio.to_thread(
                _finish_check, check_id, {"status": "unknown", "message": "代理已禁用或过期，未发起探测"}, False
            )
            return
        for source_id, code, interval in sources:
            context = {
                "source_id": source_id,
                "source_code": code,
                "proxy_id": proxy_id,
                "egress_id": str(proxy_id),
                "httpx_proxy": options[0],
                "playwright_proxy": options[1],
                "interval_seconds": interval,
            }
            token = request_context.set(context)
            status = "unknown"
            try:
                async with (
                    asyncio.timeout(45),
                    httpx.AsyncClient(
                        proxy=options[0],
                        timeout=httpx.Timeout(15, connect=5),
                        trust_env=False,
                        follow_redirects=False,
                        event_hooks={"request": [http_request_hook], "response": [http_response_hook]},
                    ) as client,
                ):
                    async with client.stream(
                        "GET", CHECK_TARGETS[code], headers={"User-Agent": "MarketMind/1.0"}
                    ) as response:
                        # Receiving actual controlled HTTPS headers proves this source path only.
                        status = "healthy" if 200 <= response.status_code < 300 else "unavailable"
                        if status != "healthy":
                            await asyncio.to_thread(_record_check_restriction, context, response.status_code)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError):
                await note_transport_failure()
                status = "cooling_down"
            except (httpx.HTTPError, SourceError, TimeoutError):
                status = "unknown"
                await asyncio.to_thread(_record_check_restriction, context, None)
            finally:
                request_context.reset(token)
            results.append({"source_id": str(source_id), "status": status})
        await asyncio.to_thread(_finish_check, check_id, {"items": results}, True)
    except Exception:
        await asyncio.to_thread(
            _finish_check, check_id, {"status": "unknown", "message": "受控代理检查未完成，未判定健康"}, False
        )


def _record_check_restriction(context: dict[str, Any], status: int | None) -> None:
    with Session(_engine()) as db, db.begin():
        db.scalar(select(ProxyEndpoint).where(ProxyEndpoint.id == context["proxy_id"]).with_for_update())
        row = db.get(ProxySourceHealth, (context["proxy_id"], context["source_id"]))
        if row is None:
            row = ProxySourceHealth(proxy_id=context["proxy_id"], source_id=context["source_id"])
            db.add(row)
        row.last_checked_at, row.error_code = datetime.now(UTC), f"http_{status}" if status else "check_incomplete"
        # Authorization and server errors never rotate egress to bypass site restrictions.
        row.status = "unknown"


async def worker() -> None:
    try:
        while True:
            check_id = await asyncio.to_thread(_claim_check)
            if check_id is None:
                await asyncio.sleep(2)
            else:
                await run_check(check_id)
    finally:
        await _redis().aclose()
        _engine().dispose()


if __name__ == "__main__":
    asyncio.run(worker())
