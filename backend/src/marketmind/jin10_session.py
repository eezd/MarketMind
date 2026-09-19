"""Isolated official QR login and encrypted Playwright session storage."""

import argparse
import asyncio
import json
import os
import stat
import tempfile
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from playwright.async_api import BrowserContext, Page, Route, async_playwright
from playwright.async_api import Error as PlaywrightError

from marketmind.collection_types import SourceError

ORIGINS = ("https://www.jin10.com/", "https://xnews.jin10.com/")


def _cipher() -> Fernet:
    try:
        return Fernet(os.environ["MM_SESSION_KEY"].encode("ascii"))
    except (KeyError, ValueError, UnicodeError):
        raise SourceError("waiting_login", "MM_SESSION_KEY must contain a valid Fernet key") from None


def _session_file(path: str | None = None) -> Path:
    value = path or os.environ.get("MM_JIN10_SESSION_FILE")
    if not value:
        raise SourceError("waiting_login", "MM_JIN10_SESSION_FILE is not configured")
    return Path(value).expanduser()


@lru_cache(maxsize=1)
def _runtime_engine():
    from marketmind.config import get_settings
    from marketmind.db import make_engine

    return make_engine(get_settings())


def _transport() -> dict[str, Any] | None:
    from marketmind.proxy_runtime import request_context

    return request_context.get()


def _validate_storage(state: Any) -> dict[str, Any]:
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("cookies"), list)
        or not isinstance(state.get("origins"), list)
    ):
        raise SourceError("waiting_login", "Jin10 session storage has an invalid structure")
    return state


def has_runtime_session() -> bool:
    """Public news may stay anonymous only when no source credential exists.

    Empty control-plane rows and unsuccessful first-time login attempts are not
    sessions. Previously stored, invalid, or revoked credentials must instead
    pass the strict loader; they never silently downgrade to anonymous access.
    Only credential-presence flags are queried, not their encrypted contents.
    """
    transport = _transport()
    if transport is None:
        return False
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from marketmind.session_models import SourceSession

    with Session(_runtime_engine()) as db:
        saved = db.execute(
            select(
                SourceSession.status,
                SourceSession.encrypted_state.is_not(None),
                SourceSession.validated_at.is_not(None),
                SourceSession.revoked_at.is_not(None),
            ).where(SourceSession.source_id == transport["source_id"])
        ).one_or_none()
    return saved is not None and (saved[0] in {"authenticated", "verifying", "revoked"} or any(saved[1:]))


def load_session(path: str | None = None) -> dict[str, Any]:
    """Use validated durable credentials in workers, private encrypted files in CLI."""
    transport = _transport()
    if transport is not None:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from marketmind.session_models import SourceSession

        with Session(_runtime_engine()) as db:
            saved = db.scalar(select(SourceSession).where(SourceSession.source_id == transport["source_id"]))
            if (
                saved is None
                or saved.status != "authenticated"
                or saved.encrypted_state is None
                or saved.validated_at is None
                or saved.validated_at < datetime.now(UTC) - timedelta(minutes=10)
            ):
                raise SourceError("waiting_login", "Jin10 session requires current two-domain verification")
            try:
                return _validate_storage(json.loads(_cipher().decrypt(saved.encrypted_state)))
            except (InvalidToken, ValueError, UnicodeError):
                raise SourceError("waiting_login", "Jin10 encrypted session is unavailable or invalid") from None
    target = _session_file(path)
    try:
        with target.open("rb") as stream:
            mode = os.fstat(stream.fileno())
            if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o600:
                raise SourceError("waiting_login", "Jin10 session file must have mode 0600")
            state = json.loads(_cipher().decrypt(stream.read()))
    except (OSError, InvalidToken, ValueError, UnicodeError):
        raise SourceError("waiting_login", "Jin10 encrypted session is unavailable or invalid") from None
    return _validate_storage(state)


def save_session(state: dict[str, Any], path: str | None = None) -> None:
    """Atomically persist ciphertext; plaintext storage never touches disk."""
    target = _session_file(path)
    encrypted = _cipher().encrypt(json.dumps(state, separators=(",", ":")).encode())
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".jin10-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


class SourceThrottle:
    """Serialize document and data request starts across both Jin10 domains."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.transport = _transport()
        self.failure: SourceError | None = None
        self.routing: dict[asyncio.Task, Page] = {}
        self.closing: set[Page] = set()

    async def drain(self, page: Page) -> None:
        self.closing.add(page)
        while pending := [task for task, owner in self.routing.items() if owner == page]:
            await asyncio.gather(*pending)

    async def tracked_route(self, route: Route) -> None:
        task = asyncio.current_task()
        page = route.request.frame.page
        self.routing[task] = page
        try:
            if page in self.closing:
                with suppress(PlaywrightError):
                    await route.abort()
                return
            await self.route(route)
        finally:
            self.routing.pop(task, None)

    async def route(self, route: Route) -> None:
        try:
            await self._dispatch(route)
            return
        except SourceError as exc:
            self.failure = exc
        except PlaywrightError:
            if not route.request.frame.page.is_closed():
                self.failure = SourceError("access_denied", "Jin10 browser request failed")
        with suppress(PlaywrightError):
            await route.abort()

    async def _continue(self, route: Route) -> None:
        if self.transport is None:
            await route.continue_()
        else:
            from marketmind.proxy_runtime import browser_continue

            await browser_continue(route)

    async def _dispatch(self, route: Route) -> None:
        request = route.request
        if self.transport is not None:
            from marketmind.proxy_runtime import guard_request

            try:
                await guard_request(request.url, self.transport["source_code"])
            except SourceError:
                await route.abort()
                return
        host = urlsplit(request.url).hostname or ""
        official = host == "jin10.com" or host.endswith(".jin10.com")
        if request.is_navigation_request() and not official and request.url != "about:blank":
            await route.abort()
            return
        if request.resource_type in {"document", "xhr", "fetch"}:
            if not official:
                await route.abort()
                return
            # Official pages also poll ads, courses, quotes and telemetry. Those
            # unrelated calls would consume the news budget and time out the
            # site's own flash request before it can leave the queue.
            if (
                request.resource_type in {"xhr", "fetch"}
                and host
                not in {
                    "flash-api.jin10.com",
                    "uc-api.jin10.com",
                    "xnews.jin10.com",
                }
                and urlsplit(request.url).path != "/classify"
            ):
                await route.abort()
                return
            if self.transport is not None:
                from marketmind.proxy_runtime import throttle

                await throttle(self.transport["source_id"])
                await self._continue(route)
            else:
                async with self._lock:
                    await asyncio.sleep(max(0.0, 3.0 - (time.monotonic() - self._last)))
                    self._last = time.monotonic()
                    await route.continue_()
        else:
            await self._continue(route)


async def close_page(page: Page) -> None:
    # 先结束本页在途请求，再关闭页面，避免正常释放被误记为出口或认证失败。
    await page.context._marketmind_gate.drain(page)
    await page.close()


@asynccontextmanager
async def isolated_browser(state: dict[str, Any] | None = None, *, headed: bool = False):
    """Always launch a fresh browser; never attach to an existing user session."""
    async with async_playwright() as playwright:
        executable = os.environ.get("MM_BROWSER_EXECUTABLE")
        transport = _transport()
        browser = await playwright.chromium.launch(
            headless=not headed,
            executable_path=executable,
            proxy=transport["playwright_proxy"] if transport is not None else None,
        )
        context = None
        try:
            context = await browser.new_context(storage_state=state, service_workers="block")
            gate = SourceThrottle()
            context._marketmind_gate = gate
            await context.route("**/*", gate.tracked_route)
            context.set_default_timeout(90_000)
            yield context
            for page in context.pages:
                await close_page(page)
            if gate.failure is not None:
                raise gate.failure
        finally:
            try:
                if context is not None:
                    for page in context.pages:
                        await close_page(page)
            finally:
                await browser.close()


def _authenticated(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("status") == 200
        and isinstance(payload.get("data"), dict)
        and bool(payload["data"].get("id"))
    )


async def domain_status(page: Page, origin: str) -> str:
    """Verify each domain using its own official business identity response."""
    try:
        async with page.expect_response(
            lambda response: (
                urlsplit(response.url).hostname == "uc-api.jin10.com" and urlsplit(response.url).path == "/userinfo"
            ),
            timeout=120_000,
        ) as pending:
            await page.goto(origin, wait_until="domcontentloaded", timeout=120_000)
        response = await pending.value
        if response.status != 200:
            return "failed"
        return "authenticated" if _authenticated(await response.json()) else "unauthenticated"
    except Exception:
        return "failed"


async def verify_domain(page: Page, origin: str) -> None:
    status = await domain_status(page, origin)
    if status != "authenticated":
        raise SourceError("waiting_login", "Jin10 official userinfo did not verify this domain")


async def verify_domains(context: BrowserContext) -> dict[str, str]:
    result = {}
    for origin in ORIGINS:
        page = await context.new_page()
        try:
            result[urlsplit(origin).hostname] = await domain_status(page, origin)
        finally:
            await close_page(page)
    return result


async def verify_context(context: BrowserContext) -> None:
    for origin in ORIGINS:
        page = await context.new_page()
        try:
            await verify_domain(page, origin)
        finally:
            await close_page(page)


async def verify_session(path: str | None = None) -> None:
    async with isolated_browser(load_session(path)) as context:
        await verify_context(context)


async def official_qr(context: BrowserContext) -> tuple[Page, bytes]:
    """Capture the actual official QR region; never expose its URL or token."""
    page = await context.new_page()
    await page.goto(ORIGINS[0], wait_until="domcontentloaded", timeout=120_000)
    await page.locator(".unlogin").click()
    image = page.locator("img.scan-login-qrcode")
    await image.wait_for(state="visible")
    await page.wait_for_function("document.querySelector('img.scan-login-qrcode')?.naturalWidth > 0")
    return page, await image.screenshot(type="png")


async def wait_for_confirmation(page: Page, timeout: float) -> None:
    try:
        async with page.expect_response(
            lambda response: (
                urlsplit(response.url).hostname == "uc-api.jin10.com" and urlsplit(response.url).path == "/userinfo"
            ),
            timeout=timeout * 1000,
        ) as pending:
            await page.locator(".unlogin").wait_for(state="hidden", timeout=timeout * 1000)
        response = await pending.value
        if response.status != 200 or not _authenticated(await response.json()):
            raise SourceError("waiting_login", "Phone confirmation did not establish a Jin10 identity")
    except SourceError:
        raise
    except Exception:
        raise SourceError("waiting_login", "QR login was not confirmed; request a fresh QR") from None


async def login(path: str | None, qr_path: str, *, headed: bool, timeout: int) -> None:
    _cipher()
    _session_file(path)
    qr = Path(qr_path).expanduser()
    qr.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The QR is a temporary login capability; keep it private and delete on exit.
    descriptor = os.open(qr, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    try:
        async with isolated_browser(headed=headed) as context:
            page, image = await official_qr(context)
            qr.write_bytes(image)
            print(f"Scan the official QR with the Jin10 app: {qr}", flush=True)
            await wait_for_confirmation(page, timeout)
            await verify_context(context)
            state = await context.storage_state()
        # Verify in a fresh browser before replacing any existing successful file.
        async with isolated_browser(state, headed=headed) as context:
            await verify_context(context)
        save_session(state, path)
        print("Encrypted session saved; fresh-browser userinfo verified on both Jin10 domains.")
    finally:
        qr.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Private Jin10 official QR session helper")
    parser.add_argument("action", choices=("login", "verify"))
    parser.add_argument("--session-file")
    parser.add_argument("--qr", default="/tmp/marketmind-jin10-qr.png")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    try:
        if args.action == "login":
            asyncio.run(login(args.session_file, args.qr, headed=args.headed, timeout=args.timeout))
        else:
            asyncio.run(verify_session(args.session_file))
            print("Fresh-browser userinfo verified on both Jin10 domains.")
    except SourceError as exc:
        parser.exit(2, f"{exc.code}: {exc}\n")
    except Exception:
        parser.exit(2, "access_denied: Jin10 browser operation failed; no session details logged\n")


if __name__ == "__main__":
    main()
