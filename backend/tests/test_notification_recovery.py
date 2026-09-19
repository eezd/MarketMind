"""离线通知恢复：仅临时私有文件、受控时钟与 Telegram/数据库替身。"""

import asyncio
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from marketmind import notification_worker as worker


class ProcessInterrupted(BaseException):
    pass


@pytest.fixture
def outage(monkeypatch, tmp_path):
    class Clock(datetime):
        current = datetime(2026, 9, 17, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    calls = []
    results = []

    async def send(config, message):
        calls.append(message)
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline tests must not access the database")

    monkeypatch.setattr(worker, "datetime", Clock)
    monkeypatch.setattr(worker, "send_telegram", send)
    monkeypatch.setattr(worker, "offline_configuration", lambda directory: {"token_ciphertext": "secret-sentinel"})
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(origins={"https://example.invalid"}))
    monkeypatch.setattr(worker, "alert_engine", forbidden)
    monkeypatch.setattr(worker, "persist_alert", forbidden)
    monkeypatch.setattr(worker, "Session", forbidden)
    return SimpleNamespace(directory=tmp_path, clock=Clock, calls=calls, results=results)


def incident(outage):
    return json.loads(worker.private_read(outage.directory / "database-incident.json"))


def poll(outage):
    asyncio.run(worker.handle_database_outage(outage.directory))


@pytest.mark.parametrize("retry_after,delay", [(None, 15), (120, 120)])
def test_transient_failure_waits_until_retry_deadline_and_success_stops(outage, retry_after, delay):
    outage.results.extend(
        [
            worker.SendResult("failed", "telegram_rate_limited", retry_after, True),
            worker.SendResult("sent"),
        ]
    )
    started = outage.clock.current
    poll(outage)
    first = incident(outage)
    assert first["delivery_status"] == "retrying"
    assert datetime.fromisoformat(first["next_attempt_at"]) == started + timedelta(seconds=delay)

    outage.clock.current += timedelta(seconds=delay - 1)
    poll(outage)
    assert len(outage.calls) == 1
    outage.clock.current += timedelta(seconds=1)
    poll(outage)
    outage.clock.current += timedelta(days=1)
    poll(outage)
    assert len(outage.calls) == 2
    assert incident(outage)["delivery_status"] == "sent"
    assert incident(outage)["id"] == first["id"]
    assert outage.calls[0] == outage.calls[1]
    for path in outage.directory.iterdir():
        assert b"secret-sentinel" not in worker.private_read(path)


@pytest.mark.parametrize("writes_before_crash", [1, 2])
def test_durable_creation_or_claim_survives_process_interruption(outage, monkeypatch, writes_before_crash):
    write = worker.private_write
    writes = 0

    def interrupted_write(path, content):
        nonlocal writes
        write(path, content)
        writes += 1
        if writes == writes_before_crash:
            raise ProcessInterrupted()

    monkeypatch.setattr(worker, "private_write", interrupted_write)
    with pytest.raises(ProcessInterrupted):
        poll(outage)
    identifier = incident(outage)["id"]
    assert outage.calls == []
    monkeypatch.setattr(worker, "private_write", write)
    outage.results.append(worker.SendResult("sent"))
    if writes_before_crash == 2:
        poll(outage)
        assert outage.calls == []
        outage.clock.current += timedelta(seconds=worker.LEASE_SECONDS)
    poll(outage)
    assert len(outage.calls) == 1
    assert incident(outage)["id"] == identifier
    assert incident(outage)["delivery_status"] == "sent"
    assert incident(outage)["attempts"] == writes_before_crash


def test_lost_success_commit_is_retried_after_lease(outage, monkeypatch):
    write = worker.private_write

    def lose_success(path, content):
        if json.loads(content).get("delivery_status") == "sent":
            raise ProcessInterrupted()
        write(path, content)

    monkeypatch.setattr(worker, "private_write", lose_success)
    outage.results.extend([worker.SendResult("sent"), worker.SendResult("sent")])
    with pytest.raises(ProcessInterrupted):
        poll(outage)
    monkeypatch.setattr(worker, "private_write", write)
    poll(outage)
    assert len(outage.calls) == 1
    outage.clock.current += timedelta(seconds=worker.LEASE_SECONDS)
    poll(outage)
    # Telegram has no idempotency key: an accepted request without a durable
    # response is deliberately retried, not silently treated as delivered.
    assert len(outage.calls) == 2
    assert incident(outage)["delivery_status"] == "sent"


@pytest.mark.parametrize("interrupted_last", [False, True])
def test_retry_budget_survives_restarts_and_interrupted_final_attempt(outage, interrupted_last):
    for attempt in range(1, worker.MAX_ATTEMPTS + 1):
        if interrupted_last and attempt == worker.MAX_ATTEMPTS:
            outage.results.append(ProcessInterrupted())
            with pytest.raises(ProcessInterrupted):
                poll(outage)
        else:
            outage.results.append(worker.SendResult("failed", "telegram_network_error", retryable=True))
            poll(outage)
        state = incident(outage)
        assert state["attempts"] == attempt
        if attempt < worker.MAX_ATTEMPTS:
            due = datetime.fromisoformat(state["next_attempt_at"])
            assert due == outage.clock.current + timedelta(seconds=15 * 2 ** (attempt - 1))
            outage.clock.current = due
    outage.clock.current += timedelta(days=1)
    poll(outage)
    poll(outage)
    assert len(outage.calls) == worker.MAX_ATTEMPTS
    assert incident(outage)["delivery_status"] == "failed"


def test_nonretryable_failure_is_terminal(outage):
    outage.results.append(worker.SendResult("failed", "telegram_access_denied"))
    poll(outage)
    outage.clock.current += timedelta(days=1)
    poll(outage)
    assert len(outage.calls) == 1
    assert incident(outage)["last_error"] == "telegram_access_denied"


@pytest.mark.parametrize("legacy_status,attempts", [("not_attempted", 1), ("failed", 2), ("sent", 1)])
def test_legacy_incidents_recover_without_repeating_known_success(outage, legacy_status, attempts):
    identifier = str(uuid4())
    worker.private_write(
        outage.directory / "database-incident.json",
        json.dumps(
            {
                "id": identifier,
                "observed_at": outage.clock.current.isoformat(),
                "delivery_status": legacy_status,
            }
        ).encode(),
    )
    if legacy_status != "sent":
        outage.results.append(worker.SendResult("sent"))
    poll(outage)
    assert len(outage.calls) == (0 if legacy_status == "sent" else 1)
    state = worker._read_incident(outage.directory / "database-incident.json")
    assert state["id"] == identifier
    assert state["delivery_status"] == "sent"
    assert state["attempts"] == attempts


@pytest.mark.parametrize(
    "result",
    [
        worker.SendResult("sent"),
        worker.SendResult("failed", "telegram_rate_limited", 120, True),
        worker.SendResult("failed", "telegram_access_denied"),
    ],
)
def test_reconcile_preserves_delivery_outcome_and_is_idempotent_after_partial_failure(outage, monkeypatch, result):
    outage.results.append(result)
    poll(outage)
    state = incident(outage)
    delivery = SimpleNamespace(status="pending", attempts=0, attempt_history=[])
    alerts = {}
    fail_recovery = True

    def persist(source, kind, *, resolved=False, event_id=None, observed_at=None):
        nonlocal fail_recovery
        if resolved and fail_recovery:
            fail_recovery = False
            raise SQLAlchemyError("controlled reconciliation interruption")
        alerts.setdefault(event_id, resolved)
        return event_id

    class Session:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def begin(self):
            return nullcontext()

        def scalar(self, statement):
            return delivery

    monkeypatch.setattr(worker, "alert_engine", lambda: None)
    monkeypatch.setattr(worker, "Session", Session)
    monkeypatch.setattr(worker, "persist_alert", persist)
    with pytest.raises(SQLAlchemyError):
        worker.reconcile_database_outage(outage.directory)
    assert (outage.directory / "database-incident.json").exists()
    assert delivery.status == state["delivery_status"]
    assert delivery.attempts == 1
    assert delivery.next_attempt_at == datetime.fromisoformat(state["next_attempt_at"])
    history = list(delivery.attempt_history)
    if result.retryable:
        outage.clock.current = datetime.fromisoformat(state["next_attempt_at"])
        outage.results.append(worker.SendResult("sent"))
        poll(outage)
    worker.reconcile_database_outage(outage.directory)
    if result.retryable:
        assert delivery.status == "sent"
        assert delivery.attempts == 2
        assert [item["attempt"] for item in delivery.attempt_history] == [1, 2]
        history = list(delivery.attempt_history)
    worker.reconcile_database_outage(outage.directory)
    assert delivery.attempt_history == history
    assert alerts[UUID(state["id"])] is False
    assert list(alerts.values()).count(True) == 1
    assert not (outage.directory / "database-incident.json").exists()
    assert len(outage.calls) == (2 if result.retryable else 1)
