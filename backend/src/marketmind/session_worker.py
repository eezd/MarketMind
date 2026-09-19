"""Independent official-login browser worker (python -m marketmind.session_worker)."""

import asyncio
import json
import logging
import os
import signal
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from marketmind.collection_types import SourceError
from marketmind.config import get_settings
from marketmind.db import make_engine
from marketmind.jin10_session import (
    _cipher,
    _validate_storage,
    isolated_browser,
    load_session,
    official_qr,
    verify_domains,
    wait_for_confirmation,
)
from marketmind.models import CrawlRun, Source
from marketmind.proxy_runtime import source_transport
from marketmind.session_models import LoginAttempt
from marketmind.sessions import ACTIVE_ATTEMPTS, DOMAINS, audit, expire_attempts, locked_state, pause_source_runs

logger = logging.getLogger("marketmind.session_worker")
RECHECK_INTERVAL = timedelta(minutes=5)
WORKER_LOCK = 764136207441


class SessionWorker:
    def __init__(self, engine):
        self.engine = engine
        self.stop = asyncio.Event()
        self.jobs: dict[UUID, asyncio.Task] = {}

    def bootstrap(self) -> None:
        """A process restart never trusts an authenticated flag or lost browser."""
        with Session(self.engine) as db:
            source_ids = list(db.scalars(select(Source.id).where(Source.code == "jin10")))
        for source_id in source_ids:
            with Session(self.engine) as db:
                state = locked_state(db, source_id)
                now = datetime.now(UTC)
                expire_attempts(db, state, now)
                for attempt in db.scalars(
                    select(LoginAttempt).where(
                        LoginAttempt.source_id == source_id,
                        LoginAttempt.status.in_(("qr_pending", "verifying")),
                    )
                ):
                    attempt.status = "failed"
                    attempt.encrypted_qr = None
                    attempt.error_code = "worker_restarted"
                    attempt.completed_at = now
                    attempt.updated_at = now
                    if attempt.generation == state.generation:
                        state.generation += 1
                    audit(
                        db,
                        "session.login_interrupted",
                        source_id,
                        outcome="failed",
                        details={"attempt_id": str(attempt.id)},
                    )
                db.execute(
                    update(LoginAttempt)
                    .where(
                        LoginAttempt.source_id == source_id,
                        LoginAttempt.status.not_in(ACTIVE_ATTEMPTS),
                    )
                    .values(encrypted_qr=None)
                )
                # Import the existing private encrypted CLI file once. Do not
                # rewrite it, and never resurrect a revoked or replaced session.
                if state.encrypted_state is None and state.generation == 0 and state.revoked_at is None:
                    configured_path = os.environ.get("MM_JIN10_SESSION_FILE")
                    if configured_path:
                        try:
                            imported = load_session(configured_path)
                            state.encrypted_state = _cipher().encrypt(
                                json.dumps(imported, separators=(",", ":")).encode()
                            )
                            audit(db, "session.local_imported", source_id)
                        except SourceError:
                            audit(db, "session.local_import_unavailable", source_id, outcome="failed")
                if state.encrypted_state is not None:
                    state.status = "verifying"
                    state.validated_at = None
                    state.domains = {domain: "verifying" for domain in DOMAINS}
                elif state.status != "revoked":
                    state.status = "unauthenticated"
                    state.domains = {domain: "unauthenticated" for domain in DOMAINS}
                state.updated_at = now
                if state.encrypted_state is not None or state.revoked_at is not None:
                    pause_source_runs(db, source_id)
                db.commit()

    def _current(self, source_id: UUID, generation: int, attempt_id: UUID | None = None) -> bool:
        with Session(self.engine) as db:
            state = locked_state(db, source_id)
            expire_attempts(db, state, datetime.now(UTC))
            current = state.generation == generation and state.status != "revoked"
            if attempt_id is not None:
                attempt = db.get(LoginAttempt, attempt_id)
                current = state.generation == generation and attempt is not None and attempt.status in ACTIVE_ATTEMPTS
            db.commit()
            return current

    async def _guard(self, source_id: UUID, generation: int, operation, attempt_id: UUID | None = None) -> None:
        job = asyncio.create_task(operation)
        try:
            while not job.done():
                await asyncio.wait({job}, timeout=1)
                if not self._current(source_id, generation, attempt_id):
                    job.cancel()
                    with suppress(asyncio.CancelledError):
                        await job
                    return
            await job
        finally:
            if not job.done():
                job.cancel()
                with suppress(asyncio.CancelledError):
                    await job

    def _publish_qr(self, source_id: UUID, attempt_id: UUID, generation: int, image: bytes) -> bool:
        encrypted = _cipher().encrypt(image)
        with Session(self.engine) as db:
            state = locked_state(db, source_id)
            now = datetime.now(UTC)
            expire_attempts(db, state, now)
            attempt = db.get(LoginAttempt, attempt_id)
            if state.generation != generation or attempt is None or attempt.status not in ACTIVE_ATTEMPTS:
                db.commit()
                return False
            attempt.encrypted_qr = encrypted
            attempt.status = "qr_pending"
            # This is our conservative validity window, never an assertion that
            # the official site cannot expire its QR sooner.
            attempt.expires_at = min(attempt.expires_at, now + timedelta(minutes=3))
            attempt.updated_at = now
            db.commit()
            return True

    def _confirming(self, source_id: UUID, attempt_id: UUID, generation: int) -> bool:
        with Session(self.engine) as db:
            state = locked_state(db, source_id)
            now = datetime.now(UTC)
            expire_attempts(db, state, now)
            attempt = db.get(LoginAttempt, attempt_id)
            if state.generation != generation or attempt is None or attempt.status != "qr_pending":
                db.commit()
                return False
            attempt.status = "verifying"
            attempt.encrypted_qr = None
            attempt.updated_at = now
            # The QR has been consumed. Permit bounded two-browser/domain
            # verification without presenting the consumed QR as still valid.
            attempt.expires_at = now + timedelta(minutes=5)
            db.commit()
            return True

    def _finish_attempt(
        self,
        source_id: UUID,
        attempt_id: UUID,
        generation: int,
        *,
        status: str,
        error_code: str | None,
        domains: dict[str, str] | None = None,
    ) -> None:
        with Session(self.engine) as db:
            state = locked_state(db, source_id)
            now = datetime.now(UTC)
            expire_attempts(db, state, now)
            attempt = db.get(LoginAttempt, attempt_id)
            if state.generation != generation or attempt is None or attempt.status not in ACTIVE_ATTEMPTS:
                db.commit()
                return
            attempt.status = status
            attempt.error_code = error_code
            attempt.encrypted_qr = None
            attempt.domains = domains or {}
            attempt.updated_at = now
            attempt.completed_at = now
            if state.status != "authenticated":
                state.status = "expired" if status == "expired" else "failed"
                state.domains = domains or {domain: "unauthenticated" for domain in DOMAINS}
                state.updated_at = now
            audit(
                db,
                "session.login_completed",
                source_id,
                outcome="failed",
                details={"attempt_id": str(attempt_id), "status": status, "error_code": error_code},
            )
            db.commit()

    async def _notify(self, source_id: UUID, *, resolved: bool) -> None:
        # Notification persistence is an independent transaction, never part of
        # publishing credentials or acknowledging the login attempt.
        try:
            from marketmind.alerts import emit_alert

            await emit_alert(
                source_id,
                "waiting_login",
                "金十会话已验证恢复" if resolved else "金十会话验证失败，需要管理员扫码",
                resolved=resolved,
            )
        except Exception:
            logger.warning("Source-session alert could not be persisted")

    def _publish_session(
        self,
        source_id: UUID,
        generation: int,
        state_data: dict,
        domains: dict[str, str],
        egress_id: str,
        attempt_id: UUID | None = None,
    ) -> bool:
        encrypted = _cipher().encrypt(json.dumps(state_data, separators=(",", ":")).encode())
        with Session(self.engine) as db:
            state = locked_state(db, source_id)
            now = datetime.now(UTC)
            expire_attempts(db, state, now)
            if state.generation != generation:
                db.commit()
                return False
            if attempt_id is not None:
                attempt = db.get(LoginAttempt, attempt_id)
                if attempt is None or attempt.status != "verifying":
                    db.commit()
                    return False
                attempt.status = "authenticated"
                attempt.error_code = None
                attempt.encrypted_qr = None
                attempt.domains = domains
                attempt.updated_at = now
                attempt.completed_at = now
            elif state.status == "revoked":
                return False
            state.encrypted_state = encrypted
            state.status = "authenticated"
            state.domains = domains
            state.egress_id = egress_id
            state.validated_at = now
            state.revoked_at = None
            state.updated_at = now
            from marketmind.runtime_models import SourceRuntime

            runtime = db.scalar(select(SourceRuntime).where(SourceRuntime.source_id == source_id).with_for_update())
            if runtime is not None and runtime.status == "waiting_login":
                runtime.status = "idle"
                runtime.pause_reason = None
                runtime.retry_at = None
            # Manual pauses remain manual: only the auth-specific waiting state
            # can recover as a consequence of a successful official identity.
            db.execute(
                update(CrawlRun)
                .where(
                    CrawlRun.source_id == source_id,
                    CrawlRun.status == "waiting_login",
                )
                .values(status="queued")
            )
            audit(db, "session.verified", source_id, details={"domains": domains, "generation": generation})
            db.commit()
            return True

    async def _login(self, source_id: UUID, attempt_id: UUID, generation: int) -> None:
        try:
            async with source_transport(source_id) as transport:
                async with isolated_browser() as context:
                    page, image = await official_qr(context)
                    if not self._publish_qr(source_id, attempt_id, generation, image):
                        return
                    await wait_for_confirmation(page, 180)
                    if not self._confirming(source_id, attempt_id, generation):
                        return
                    domains = await verify_domains(context)
                    if any(status != "authenticated" for status in domains.values()):
                        self._finish_attempt(
                            source_id,
                            attempt_id,
                            generation,
                            status="failed",
                            error_code="domain_verification_failed",
                            domains=domains,
                        )
                        await self._notify(source_id, resolved=False)
                        return
                    saved = await context.storage_state()
                # The same official flow as CLI: a genuinely new browser must
                # authenticate from storage_state before anything is published.
                async with isolated_browser(saved) as context:
                    domains = await verify_domains(context)
                    if any(status != "authenticated" for status in domains.values()):
                        self._finish_attempt(
                            source_id,
                            attempt_id,
                            generation,
                            status="failed",
                            error_code="fresh_browser_verification_failed",
                            domains=domains,
                        )
                        await self._notify(source_id, resolved=False)
                        return
                    saved = await context.storage_state()
                if self._publish_session(source_id, generation, saved, domains, transport["egress_id"], attempt_id):
                    await self._notify(source_id, resolved=True)
        except SourceError as exc:
            # Never persist exception messages, URLs or browser diagnostics.
            code = "confirmation_required" if exc.code == "waiting_login" else "login_transport_unavailable"
            self._finish_attempt(
                source_id,
                attempt_id,
                generation,
                status="action_required" if exc.code == "waiting_login" else "failed",
                error_code=code,
            )
            await self._notify(source_id, resolved=False)
        except Exception:
            self._finish_attempt(
                source_id, attempt_id, generation, status="failed", error_code="official_login_unavailable"
            )
            await self._notify(source_id, resolved=False)

    async def _verify(self, source_id: UUID, generation: int, ciphertext: bytes) -> None:
        domains = {domain: "failed" for domain in DOMAINS}
        try:
            saved = _validate_storage(json.loads(_cipher().decrypt(ciphertext)))
            async with source_transport(source_id) as transport:
                async with isolated_browser(saved) as context:
                    domains = await verify_domains(context)
                    if all(status == "authenticated" for status in domains.values()):
                        saved = await context.storage_state()
                if all(status == "authenticated" for status in domains.values()):
                    if self._publish_session(source_id, generation, saved, domains, transport["egress_id"]):
                        await self._notify(source_id, resolved=True)
                    return
        except Exception:
            # Keep audit information deliberately smaller than browser errors.
            pass
        with Session(self.engine) as db:
            state = locked_state(db, source_id)
            if state.generation != generation or state.status == "revoked":
                return
            state.status = "expired" if "unauthenticated" in domains.values() else "failed"
            state.domains = domains
            state.validated_at = None
            state.updated_at = datetime.now(UTC)
            pause_source_runs(db, source_id)
            audit(db, "session.verification_failed", source_id, outcome="failed", details={"domains": domains})
            db.commit()
        await self._notify(source_id, resolved=False)

    def _schedule(self) -> None:
        with Session(self.engine) as db:
            source_ids = list(db.scalars(select(Source.id).where(Source.code == "jin10")))
        for source_id in source_ids:
            if source_id in self.jobs:
                continue
            with Session(self.engine) as db:
                state = locked_state(db, source_id)
                now = datetime.now(UTC)
                expire_attempts(db, state, now)
                attempt = db.scalar(
                    select(LoginAttempt)
                    .where(
                        LoginAttempt.source_id == source_id,
                        LoginAttempt.status == "queued",
                    )
                    .order_by(LoginAttempt.created_at, LoginAttempt.id)
                )
                if attempt is not None and attempt.generation == state.generation:
                    generation, attempt_id = state.generation, attempt.id
                    db.commit()
                    self.jobs[source_id] = asyncio.create_task(
                        self._guard(
                            source_id,
                            generation,
                            self._login(source_id, attempt_id, generation),
                            attempt_id,
                        )
                    )
                elif (
                    state.encrypted_state is not None
                    and state.status != "revoked"
                    and (
                        state.status == "verifying"
                        or (state.validated_at or state.updated_at) <= now - RECHECK_INTERVAL
                    )
                ):
                    generation, ciphertext = state.generation, state.encrypted_state
                    db.commit()
                    self.jobs[source_id] = asyncio.create_task(
                        self._guard(
                            source_id,
                            generation,
                            self._verify(source_id, generation, ciphertext),
                        )
                    )
                else:
                    db.commit()

    def _discard_contexts(self) -> None:
        for source_id in self.jobs:
            with Session(self.engine) as db:
                state = locked_state(db, source_id)
                now = datetime.now(UTC)
                for attempt in db.scalars(
                    select(LoginAttempt).where(
                        LoginAttempt.source_id == source_id,
                        LoginAttempt.status.in_(("qr_pending", "verifying")),
                    )
                ):
                    attempt.status = "failed"
                    attempt.error_code = "worker_stopped"
                    attempt.encrypted_qr = None
                    attempt.completed_at = now
                    attempt.updated_at = now
                    if state.generation == attempt.generation:
                        state.generation += 1
                    audit(
                        db,
                        "session.login_interrupted",
                        source_id,
                        outcome="failed",
                        details={"attempt_id": str(attempt.id)},
                    )
                if state.status == "qr_pending":
                    state.status = "failed"
                    state.updated_at = now
                db.commit()

    async def run(self) -> None:
        # A dedicated PostgreSQL connection owns this singleton lease. A second
        # worker must not reset contexts belonging to the live first worker.
        with self.engine.connect() as ownership:
            acquired = ownership.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})
            ownership.commit()
            if not acquired:
                raise RuntimeError("Another source-session worker is active")
            try:
                self.bootstrap()
                logger.info("Source-session worker ready")
                while not self.stop.is_set():
                    ownership.execute(text("SELECT 1"))
                    ownership.commit()
                    for source_id, job in list(self.jobs.items()):
                        if job.done():
                            try:
                                job.result()
                            except asyncio.CancelledError:
                                pass
                            except Exception:
                                logger.warning("Source-session operation failed; credentials were not logged")
                            del self.jobs[source_id]
                    self._schedule()
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self.stop.wait(), timeout=1)
            finally:
                for job in self.jobs.values():
                    job.cancel()
                await asyncio.gather(*self.jobs.values(), return_exceptions=True)
                with suppress(Exception):
                    self._discard_contexts()
                with suppress(Exception):
                    ownership.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})
                    ownership.commit()


async def serve() -> None:
    engine = make_engine(get_settings())
    worker = SessionWorker(engine)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, worker.stop.set)
    try:
        await worker.run()
    finally:
        engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.error("Source-session worker stopped; check database, private storage and browser configuration")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
