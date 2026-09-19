"""Network boundary regressions; PostgreSQL cases use an isolated rollback-only database."""

import asyncio
import os
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Connection, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from marketmind import network_policy, proxy_runtime
from marketmind.collection_types import SourceError
from marketmind.models import Source
from marketmind.proxy_models import ProxyEndpoint, ProxyLease, ProxySourceHealth, SourceEgressState


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    records = {
        "www.cls.cn": ["8.8.8.8"],
        "proxy.example": ["8.8.4.4"],
    }

    def getaddrinfo(host, port, *args, **kwargs):
        if host not in records:
            raise socket.gaierror(socket.EAI_NONAME, "Unconfigured test hostname")
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in records[host]
        ]

    monkeypatch.setattr(
        network_policy, "socket", SimpleNamespace(getaddrinfo=getaddrinfo, SOCK_STREAM=socket.SOCK_STREAM)
    )
    monkeypatch.delenv("MM_PROXY_ALLOWED_CIDRS", raising=False)
    return records


def test_cls_script_host_is_allowed_without_allowing_arbitrary_subdomains(dns: dict[str, list[str]]) -> None:
    dns["wwwjs.cls.cn"] = ["8.8.8.8"]
    assert (
        asyncio.run(network_policy.validate_target_url("https://wwwjs.cls.cn/_next/static/chunks/main.js", "cls"))
        == "8.8.8.8"
    )
    dns["untrusted.cls.cn"] = ["8.8.8.8"]
    with pytest.raises(SourceError) as error:
        asyncio.run(network_policy.validate_target_url("https://untrusted.cls.cn/main.js", "cls"))
    assert error.value.code == "unsafe_target"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.cls.cn.attacker.example/",
        "https://www.jin10.com/",
        "https://user:secret@www.cls.cn/",
        "http://www.cls.cn/",
        "https://www.cls.cn:8443/",
    ],
    ids=["deceptive-host", "different-source", "credentials", "plaintext", "nonstandard-port"],
)
def test_target_requires_credential_free_official_https(url: str) -> None:
    with pytest.raises(SourceError) as error:
        asyncio.run(network_policy.validate_target_url(url, "cls"))
    assert error.value.code == "unsafe_target"


@pytest.mark.parametrize(
    "addresses",
    [
        ["10.20.30.40"],
        ["169.254.169.254"],
        ["8.8.8.8", "::1"],
    ],
    ids=["private-dns", "metadata-dns", "mixed-public-loopback-dns"],
)
def test_official_target_rejects_any_nonpublic_dns_answer(dns: dict[str, list[str]], addresses: list[str]) -> None:
    assert asyncio.run(network_policy.validate_target_url("https://www.cls.cn/telegraph", "cls")) == "8.8.8.8"
    dns["www.cls.cn"] = addresses
    with pytest.raises(SourceError) as error:
        asyncio.run(network_policy.validate_target_url("https://www.cls.cn/telegraph", "cls"))
    assert error.value.code == "unsafe_target"


def test_private_proxy_requires_explicit_permission_for_every_address(
    dns: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    dns["proxy.example"] = ["10.20.0.7", "10.21.0.8"]
    with pytest.raises(ValueError):
        network_policy.validate_proxy_address("proxy.example", 8080)
    monkeypatch.setenv("MM_PROXY_ALLOWED_CIDRS", "10.20.0.0/24")
    with pytest.raises(ValueError):
        network_policy.validate_proxy_address("proxy.example", 8080)
    monkeypatch.setenv("MM_PROXY_ALLOWED_CIDRS", "10.20.0.0/24,10.21.0.0/24")
    assert network_policy.validate_proxy_address("proxy.example", 8080) == "10.20.0.7"


@pytest.mark.parametrize("address", ["169.254.169.254", "100.100.100.200", "fd00:ec2::254"])
def test_metadata_proxy_cannot_be_allowlisted(address: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MM_PROXY_ALLOWED_CIDRS", "0.0.0.0/0,::/0")
    with pytest.raises(ValueError):
        network_policy.validate_proxy_address(address, 8080)


@pytest.mark.parametrize(
    "location",
    ["https://attacker.example/", "https://user:secret@www.cls.cn/", "https://img.cls.cn/"],
    ids=["external-redirect", "credential-redirect", "private-dns-redirect"],
)
def test_redirect_is_rejected_before_followup_request(location: str, dns: dict[str, list[str]]) -> None:
    dns["img.cls.cn"] = ["169.254.169.254"]
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) > 1:
            pytest.fail("Unsafe redirect reached the HTTP transport")
        return httpx.Response(302, headers={"location": location})

    async def request() -> None:
        token = proxy_runtime.request_context.set({"source_code": "cls"})
        try:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond),
                follow_redirects=True,
                event_hooks={"response": [proxy_runtime.http_response_hook]},
            ) as client:
                await client.get("https://www.cls.cn/telegraph")
        finally:
            proxy_runtime.request_context.reset(token)

    with pytest.raises(SourceError) as error:
        asyncio.run(request())
    assert error.value.code == "unsafe_target"
    assert requests == ["https://www.cls.cn/telegraph"]


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> Iterator[Connection]:
    url = os.environ.get("MM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MM_TEST_DATABASE_URL to an isolated migrated PostgreSQL database")
    database = make_url(url).database or ""
    if not database.startswith("marketmind_") or not database.endswith("_verify"):
        pytest.fail("Integration tests require a marketmind_*_verify database")
    engine = create_engine(url)
    try:
        with engine.connect() as conn, conn.begin() as transaction:
            # Real Sessions join this external transaction; their commits cannot persist test writes.
            monkeypatch.setattr(proxy_runtime, "_engine", lambda: conn)
            yield conn
            transaction.rollback()
    finally:
        engine.dispose()


def create_source(connection: Connection, config: dict) -> UUID:
    identifier = uuid4()
    with Session(connection) as db, db.begin():
        db.add(Source(id=identifier, code=identifier.hex, name="Proxy regression", permission_config=config))
    return identifier


def create_proxy(connection: Connection, *, enabled: bool = True) -> UUID:
    identifier = uuid4()
    with Session(connection) as db, db.begin():
        db.add(ProxyEndpoint(id=identifier, name="Proxy regression", host="proxy.example", port=8080, enabled=enabled))
    return identifier


def test_first_selection_persists_usable_source_scoped_proxy_and_lease(connection: Connection) -> None:
    create_proxy(connection)  # Available inventory is not permission to use an unassigned endpoint.
    assigned = create_proxy(connection)
    source_id = create_source(connection, {"proxy_ids": [str(assigned)], "on_proxy_exhausted": "pause"})

    transport = proxy_runtime._select_transport(source_id)

    assert transport["proxy_id"] == assigned
    assert transport["httpx_proxy"] == "http://8.8.4.4:8080"
    with Session(connection) as db:
        state = db.get(SourceEgressState, source_id)
        assert state.proxy_id == assigned
        assert not state.paused
        lease = db.get(ProxyLease, transport["lease_id"])
        assert lease.source_id == source_id and lease.proxy_id == assigned
        assert lease.expires_at > datetime.now(UTC)


def test_unconfigured_source_does_not_borrow_inventory_proxy(connection: Connection) -> None:
    create_proxy(connection)
    source_id = create_source(connection, {})

    transport = proxy_runtime._select_transport(source_id)

    assert transport["httpx_proxy"] is None
    with Session(connection) as db:
        state = db.get(SourceEgressState, source_id)
        assert state.proxy_id is None and state.egress_id == "direct"
        assert db.scalar(select(ProxyLease.id).where(ProxyLease.source_id == source_id)) is None


@pytest.mark.parametrize("failover", [False, True], ids=["failover-disabled", "failover-enabled"])
def test_unavailable_primary_only_advances_when_failover_enabled(connection: Connection, failover: bool) -> None:
    primary, secondary = create_proxy(connection), create_proxy(connection)
    source_id = create_source(
        connection,
        {"proxy_ids": [str(primary), str(secondary)], "failover_enabled": failover, "on_proxy_exhausted": "pause"},
    )
    proxy_runtime._select_transport(source_id)
    with Session(connection) as db, db.begin():
        db.add(ProxySourceHealth(proxy_id=primary, source_id=source_id, status="unavailable"))

    transport = proxy_runtime._select_transport(source_id)

    with Session(connection) as db:
        state = db.get(SourceEgressState, source_id)
        if failover:
            assert transport["proxy_id"] == secondary
            assert state.proxy_id == secondary and not state.paused
        else:
            assert transport["paused"] and transport["httpx_proxy"] is None
            assert state.paused and state.proxy_id is None and state.egress_id == "paused"


def test_exhausted_source_stays_paused_even_when_unassigned_proxy_is_healthy(connection: Connection) -> None:
    assigned = create_proxy(connection, enabled=False)
    create_proxy(connection)
    source_id = create_source(
        connection,
        {"proxy_ids": [str(assigned)], "failover_enabled": True, "on_proxy_exhausted": "pause"},
    )

    first = proxy_runtime._select_transport(source_id)
    assert first["paused"] and first["httpx_proxy"] is None
    with Session(connection) as db, db.begin():
        db.get(ProxyEndpoint, assigned).enabled = True

    second = proxy_runtime._select_transport(source_id)
    assert second["paused"] and second["httpx_proxy"] is None
    with Session(connection) as db:
        state = db.get(SourceEgressState, source_id)
        assert state.paused and state.egress_id == "paused"
        assert db.scalar(select(ProxyLease.id).where(ProxyLease.source_id == source_id)) is None


class _RateRedis:
    """In-memory Redis boundary; each eval runs atomically without yielding."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, key, **kwargs):
        return self.locks.setdefault(key, asyncio.Lock())

    async def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key, seconds):
        return key in self.values

    async def delete(self, key):
        return self.values.pop(key, None) is not None

    async def eval(self, script, number_of_keys, *args):
        if script == proxy_runtime._BACKOFF_SCRIPT:
            key, delay = args
            self.values[key] = max(self.values.get(key, 0), delay)
            return self.values[key]
        if script == proxy_runtime._RESPONSE_SCRIPT:
            failures_key, alert_key, outcome = args
            failures = self.values.get(failures_key, 0)
            if outcome == "failure":
                failures += 1
                self.values[failures_key] = failures
                if failures >= 3:
                    self.values[alert_key] = 1
                return failures
            self.values.pop(failures_key, None)
            if failures >= 3:
                self.values[alert_key] = 1
            return int(alert_key in self.values)
        raise AssertionError("Unexpected Redis script")


@pytest.fixture
def rate_recovery(monkeypatch: pytest.MonkeyPatch):
    redis = _RateRedis()
    source_id = uuid4()
    events: list[tuple[UUID, bool]] = []

    async def emit(source, kind, message, *, resolved=False):
        assert kind == "rate_limited"
        events.append((source, resolved))

    monkeypatch.setattr(proxy_runtime, "_redis", lambda: redis)
    monkeypatch.setattr(proxy_runtime, "_emit", emit)
    monkeypatch.setattr(proxy_runtime, "_record_health", lambda *args: None)
    monkeypatch.setattr(proxy_runtime.random, "uniform", lambda *args: 0)
    token = proxy_runtime.request_context.set({"source_id": source_id})
    try:
        yield SimpleNamespace(redis=redis, source_id=source_id, events=events, emit=emit)
    finally:
        proxy_runtime.request_context.reset(token)


def test_success_between_failures_prevents_sustained_rate_alert(rate_recovery) -> None:
    async def exercise():
        for _ in range(6):
            await proxy_runtime.note_response(500, {})
            await proxy_runtime.note_response(200, {})

    asyncio.run(exercise())
    assert rate_recovery.events == []
    # Every failure starts a fresh streak, including after health was already sampled.
    assert rate_recovery.redis.values[f"mm:rate:{{{rate_recovery.source_id}}}:backoff"] == 30_000


def test_rate_recovery_closes_episode_and_next_failure_opens_another(rate_recovery) -> None:
    async def exercise():
        await proxy_runtime.note_response(200, {})
        for status in (429, 500, 503, 200, 200, 500, 429, 503):
            await proxy_runtime.note_response(status, {})

    asyncio.run(exercise())
    assert rate_recovery.events == [
        (rate_recovery.source_id, False),
        (rate_recovery.source_id, True),
        (rate_recovery.source_id, False),
    ]


def test_rate_recovery_preserves_retry_after_and_future_backoff(rate_recovery) -> None:
    async def exercise():
        await proxy_runtime.note_response(429, {"retry-after": "7200"})
        await proxy_runtime.note_response(200, {})
        await proxy_runtime.note_response(500, {})

    asyncio.run(exercise())
    assert rate_recovery.redis.values[f"mm:rate:{{{rate_recovery.source_id}}}:backoff"] == 7_200_000
    assert rate_recovery.events == []


def test_only_untainted_success_for_same_source_resolves_rate_alert(rate_recovery) -> None:
    async def exercise():
        for _ in range(3):
            await proxy_runtime.note_response(500, {})
        for status in (302, 403):
            await proxy_runtime.note_response(status, {})
        context = proxy_runtime.current_transport()
        context["failed"] = True
        await proxy_runtime.note_response(200, {})
        token = proxy_runtime.request_context.set({"source_id": uuid4()})
        try:
            await proxy_runtime.note_response(200, {})
        finally:
            proxy_runtime.request_context.reset(token)
        assert rate_recovery.events == [(rate_recovery.source_id, False)]
        context.pop("failed")
        await proxy_runtime.note_response(204, {})

    asyncio.run(exercise())
    assert rate_recovery.events == [(rate_recovery.source_id, False), (rate_recovery.source_id, True)]


def test_rate_alert_recovery_survives_counter_expiry_and_failed_commit(rate_recovery, monkeypatch) -> None:
    async def exercise():
        for _ in range(3):
            await proxy_runtime.note_response(500, {})
        # Simulate Redis expiring the idle failure streak, not the open episode.
        rate_recovery.redis.values.pop(f"mm:rate:{{{rate_recovery.source_id}}}:failures")

        async def unavailable(*args, **kwargs):
            raise RuntimeError("Database temporarily unavailable")

        monkeypatch.setattr(proxy_runtime, "_emit", unavailable)
        with pytest.raises(SourceError) as error:
            await proxy_runtime.note_response(200, {})
        assert error.value.code == "rate_limit_unavailable"
        monkeypatch.setattr(proxy_runtime, "_emit", rate_recovery.emit)
        await proxy_runtime.note_response(200, {})
        await proxy_runtime.note_response(200, {})

    asyncio.run(exercise())
    assert rate_recovery.events == [(rate_recovery.source_id, False), (rate_recovery.source_id, True)]


def test_overlapping_success_waits_for_rate_alert_commit(rate_recovery, monkeypatch) -> None:
    async def exercise():
        opening = asyncio.Event()
        release = asyncio.Event()
        success_started = asyncio.Event()

        async def delayed_emit(source, kind, message, *, resolved=False):
            if not resolved:
                opening.set()
                await release.wait()
            await rate_recovery.emit(source, kind, message, resolved=resolved)

        async def success():
            success_started.set()
            await proxy_runtime.note_response(200, {})

        monkeypatch.setattr(proxy_runtime, "_emit", delayed_emit)
        await proxy_runtime.note_response(500, {})
        await proxy_runtime.note_response(500, {})
        failure = asyncio.create_task(proxy_runtime.note_response(500, {}))
        await opening.wait()
        recovery = asyncio.create_task(success())
        await success_started.wait()
        release.set()
        await asyncio.gather(failure, recovery)

    asyncio.run(exercise())
    assert rate_recovery.events == [(rate_recovery.source_id, False), (rate_recovery.source_id, True)]
