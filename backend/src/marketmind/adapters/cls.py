"""Read CLS news through its official frontend; never construct API signatures."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from marketmind.collection_types import Article, PageResult, SourceError
from marketmind.proxy_runtime import (
    browser_continue,
    current_transport,
    guard_request,
    is_connection_failure,
    note_transport_failure,
    throttle,
)

_ENTRIES = {
    "cls_depth": "https://www.cls.cn/depth?id=1000",
    "cls_telegraph": "https://www.cls.cn/telegraph",
}
_MAX_REPLAY = 50
_SECRET_KEYS = {
    "cookie",
    "cookies",
    "authorization",
    "token",
    "accesstoken",
    "refreshtoken",
    "sign",
    "signature",
    "storage",
    "storagestate",
    "userinfo",
}
_NEWS_FIELDS = frozenset(
    {
        "id",
        "title",
        "brief",
        "content",
        "ctime",
        "modified_time",
        "author",
        "source",
        "isFree",
        "external_link",
        "level",
        "article_tag",
        "visibleTags",
        "subjects",
        "subject",
        "category",
        "tags",
        "stocks",
        "stock_list",
        "plate_list",
        "funds",
        "sub_titles",
        "image",
        "images",
        "img",
        "imgs",
        "audio_url",
        "audioUrl",
        "miniMaxAudioUrl",
        "assocArticleUrl",
        "assocVideoTitle",
        "assocVideoUrl",
        "associatedFastFact",
        "assocFastFact",
        "video",
    }
)


class _Content(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.media: list[dict[str, str]] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")
        if tag in {"img", "audio", "video", "source"}:
            source = dict(attrs).get("src")
            if source:
                self.media.append({"kind": tag, "url": source})

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _text(value: Any) -> tuple[str, list[dict[str, str]]]:
    parser = _Content()
    parser.feed(value if isinstance(value, str) else "")
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip()), parser.media


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact(child)
            for key, child in value.items()
            if str(key).lower().replace("_", "").replace("-", "") not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        url = urlsplit(value)
        query = [(key, val) for key, val in parse_qsl(url.query) if key.lower().replace("_", "") not in _SECRET_KEYS]
        return urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), url.fragment))
    return value


def _news_raw(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _redact(value) for key, value in item.items() if key in _NEWS_FIELDS}


def _timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _cursor(value: str | None, collector_code: str) -> dict[str, Any]:
    if value is None:
        return {"collector": collector_code, "page": 0}
    try:
        state = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (
            not isinstance(state, dict)
            or state.get("collector") != collector_code
            or type(state.get("page")) is not int
            or state["page"] < 0
        ):
            raise ValueError
        return state
    except (ValueError, TypeError, UnicodeError):
        raise SourceError("parse_error", "CLS 游标无效") from None


def _endpoint(url: str, collector_code: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname != "www.cls.cn":
        return None
    if collector_code == "cls_depth":
        if parsed.path == "/v3/depth/home/assembled/1000":
            return "initial"
        if parsed.path == "/v3/depth/list/1000":
            return "history"
    else:
        if parsed.path == "/api/cache" and parse_qs(parsed.query).get("name") == ["telegraph"]:
            return "cache_history" if "lastTime" in parse_qs(parsed.query) else "initial"
        if parsed.path == "/v1/roll/get_roll_list":
            return "history"
    return None


def _rows(payload: Any, collector_code: str, kind: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("errno") != 0:
        raise SourceError("parse_error", "CLS 新闻接口返回错误或结构变化")
    data = payload.get("data")
    if collector_code == "cls_depth":
        rows = data.get("depth_list") if kind == "initial" and isinstance(data, dict) else data
    else:
        rows = data.get("roll_data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("id") for row in rows):
        raise SourceError("parse_error", "CLS 新闻列表字段缺失或类型变化")
    return rows


def _pagination(url: str) -> dict[str, int]:
    query = parse_qs(urlsplit(url).query)
    try:
        return {key: int(query[key][0]) for key in ("lastTime", "last_time", "rn", "refresh_type") if key in query}
    except (ValueError, IndexError):
        raise SourceError("parse_error", "CLS 官方分页参数格式变化") from None


async def _read_official_page(
    queue: asyncio.Queue[Any],
    collector_code: str,
    raw_pages: list[dict[str, Any]],
    *,
    action: int,
    last_time: int | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Consume a whole UI action, including the cache's 0–19 row supplement."""
    from playwright.async_api import Error as BrowserError

    initial = action == 0
    deadline = time.monotonic() + 60
    combined: list[dict[str, Any]] = []
    supplement: dict[str, int] | None = None
    while time.monotonic() < deadline:
        try:
            owner, kind, response = await asyncio.wait_for(queue.get(), max(0.1, deadline - time.monotonic()))
        except TimeoutError:
            raise SourceError("response_timeout", "CLS 官方新闻响应或缓存回退未在 60 秒内到达") from None
        if owner != action:
            continue
        params = _pagination(response.url)
        if supplement is not None:
            if kind != "history" or any(params.get(key) != value for key, value in supplement.items()):
                continue
        elif initial:
            if kind not in ({"initial", "history"} if collector_code == "cls_telegraph" else {"initial"}):
                continue
        elif collector_code == "cls_telegraph":
            if kind != "cache_history" or params.get("lastTime") != last_time or params.get("rn") != 20:
                continue
        elif kind != "history" or params.get("last_time") != last_time:
            continue
        if response.status in {401, 403, 429}:
            code = "waiting_login" if response.status == 401 else "access_denied"
            raise SourceError(code, f"CLS 新闻访问受限 (HTTP {response.status})")
        if response.status != 200:
            raise SourceError("access_denied", f"CLS 新闻接口 HTTP {response.status}")
        try:
            payload = await response.json()
        except (ValueError, BrowserError):
            raise SourceError("parse_error", "CLS 新闻响应不是有效 JSON") from None
        rows = _rows(payload, collector_code, kind)
        # Never retain a signed URL or account/ad response subtrees.
        raw_pages.append(
            {"action": action, "kind": kind, "pagination": params, "articles": [_news_raw(row) for row in rows]}
        )
        combined.extend(rows)
        if collector_code == "cls_telegraph":
            if kind == "cache_history" and len(rows) < 20:
                boundary = rows[-1].get("ctime") if rows else last_time
                if _timestamp(boundary) is None:
                    raise SourceError("parse_error", "CLS 缓存补页缺少可确认的时间边界")
                supplement = {"last_time": int(boundary), "rn": 20 - len(rows), "refresh_type": 1}
                continue
            if kind == "initial" and not rows:
                continue  # The initial feed may also use the official fallback.
        if not rows and kind != "history":
            raise SourceError("parse_error", "CLS 空新闻页未提供可确认的历史终止信号")
        return combined, not rows
    raise SourceError("response_timeout", "CLS 新闻响应等待超时")


async def _settle_page(page: Any, collector_code: str, rows: list[dict[str, Any]], terminal: bool) -> None:
    # Both entrypoints keep the primary list and its load state in this column.
    scope = page.locator(".f-l.w-894")
    if terminal:
        text = "已加载完" if collector_code == "cls_telegraph" else "已经加载到最后了"
        await scope.get_by_text(text, exact=True).wait_for(state="visible", timeout=15_000)
        return
    # A response event precedes the frontend's promise/React commit. In particular,
    # the cache commit is NOT the completion of a pending get_roll_list supplement.
    visible_rows = [row for row in rows if not row.get("recovery")]
    if visible_rows:
        row = visible_rows[-1]
        href = row.get("external_link") if collector_code == "cls_depth" else None
        href = href or f"/detail/{row['id']}"
        await page.wait_for_function(
            """href => Array.from(document.querySelectorAll('.f-l.w-894 a[href]'))
                .some(link => link.getAttribute('href') === href)""",
            arg=href,
            timeout=15_000,
        )
    await scope.get_by_text("加载中...", exact=True).wait_for(state="hidden", timeout=15_000)
    if collector_code == "cls_depth":
        await scope.get_by_text("加载更多", exact=True).wait_for(state="visible", timeout=15_000)


async def _advance_page(page: Any, collector_code: str, action: int) -> None:
    if collector_code == "cls_depth" or action == 1:
        more = page.locator(".f-l.w-894").get_by_text("加载更多", exact=True)
        await more.wait_for(state="visible", timeout=15_000)
        await more.evaluate("element => element.click()")
    else:
        # The first telegraph click removes the button and enables the official
        # window scroll handler. Dispatch also works if already at the bottom.
        await page.evaluate("""() => {
            window.scrollTo(0, document.documentElement.scrollHeight);
            window.dispatchEvent(new Event('scroll'));
        }""")


def _article(item: dict[str, Any], *, restricted: bool = False, gap: str | None = None) -> Article:
    ident = str(item["id"])
    if not ident.isdecimal():
        raise SourceError("parse_error", "CLS 新闻 ID 格式变化")
    body, media = _text(item.get("content"))
    summary, _ = _text(item.get("brief"))
    external = item.get("external_link")
    restricted = restricted or item.get("isFree") is False
    status = "external_link" if external else "paywalled" if restricted else "complete" if body else "summary_only"
    tags: list[Any] = []
    for key in ("article_tag", "visibleTags", "subjects", "subject"):
        if item.get(key):
            tags.append({"field": key, "value": _redact(item[key])})
    for key in ("image", "images", "imgs", "audio_url", "miniMaxAudioUrl", "assocVideoUrl"):
        if item.get(key):
            tags.append({"kind": "media", "field": key, "value": _redact(item[key])})
    tags.extend(media)
    if external:
        tags.append({"kind": "external_link", "url": _redact(external)})
    if gap:
        tags.append({"kind": "collection_gap", "reason": gap})
    if item.get("level"):
        tags.append({"field": "level", "value": item["level"]})
    author = item.get("author") or item.get("source")
    if isinstance(author, dict):
        author = author.get("name")
    published = item.get("ctime")
    return Article(
        source_item_id=ident,
        canonical_url=f"https://www.cls.cn/detail/{ident}",
        original_url=f"https://www.cls.cn/detail/{ident}",
        title=str(item.get("title") or ""),
        summary=summary or None,
        body_text=body if status == "complete" else None,
        body_status=status,
        published_at=_timestamp(published),
        source_updated_at=_timestamp(item.get("modified_time")),
        source_time_text=str(published) if published is not None else None,
        source_timezone="UTC" if published is not None else None,
        author=author if isinstance(author, str) else None,
        source_tags=tags,
    )


async def fetch_page(
    collector_code: str,
    cursor: str | None,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    session_path: str | None = None,
    max_details: int | None = None,
) -> PageResult:
    """Collect one actual page; resume by bounded official clicks and scrolling.

    Default: fetch every ordinary depth detail. A caller-supplied max_details
    leaves explicit collection_gap metadata on skipped items. Reopening may
    replay up to 50 history pages; cursor anchors detect a moving front page.
    """
    if collector_code not in _ENTRIES:
        raise SourceError("parse_error", "不支持的 CLS 入口")
    if max_details is not None and (type(max_details) is not int or max_details < 0):
        raise SourceError("parse_error", "max_details 必须是非负整数")
    state = _cursor(cursor, collector_code)
    if state["page"] > _MAX_REPLAY:
        # Older workers could persist page 51; no request or raw document exists.
        raise SourceError("cls_replay_limit", "CLS 已达到 50 页有限回补上限，历史覆盖存在缺口")
    try:
        from playwright.async_api import Error as BrowserError
        from playwright.async_api import TimeoutError as BrowserTimeout
        from playwright.async_api import async_playwright
    except ImportError:
        raise SourceError("access_denied", "CLS 采集需要 Playwright 和 Chromium 运行时") from None

    queue: asyncio.Queue[Any] = asyncio.Queue()
    raw_pages: list[dict[str, Any]] = []
    raw_details: list[dict[str, Any]] = []
    articles: list[Article] = []
    target_rows: list[dict[str, Any]] = []
    exhausted = False
    coverage_gap = None
    reached_page = 0
    action = 0
    request_actions: dict[Any, int] = {}
    listed_ids: set[str] = set()
    last_time: int | None = None

    async with async_playwright() as runtime:
        browser = None
        context = None
        routing: dict[asyncio.Task, Any] = {}

        async def drain_routes(target=None) -> None:
            while pending := [task for task, owner in routing.items() if target is None or owner == target]:
                await asyncio.gather(*pending)

        try:
            browser = await runtime.chromium.launch(headless=True, proxy=current_transport()["playwright_proxy"])
            context = await browser.new_context(storage_state=session_path, service_workers="block")
            page = await context.new_page()

            async def tracked_route(route: Any) -> None:
                task = asyncio.current_task()
                routing[task] = route.request.frame.page
                try:
                    await route_request(route)
                finally:
                    routing.pop(task, None)

            async def route_request(route: Any) -> None:
                request = route.request
                parsed = urlsplit(request.url)
                if request.frame.page != page and request.resource_type != "document":
                    await route.abort()
                    return
                try:
                    await guard_request(request.url, "cls")
                except SourceError:
                    await route.abort()
                    return
                dynamic = request.resource_type in {"document", "xhr", "fetch"}
                if dynamic:
                    allowed = parsed.hostname == "www.cls.cn" and (
                        request.resource_type == "document"
                        or (request.frame.page == page and _endpoint(request.url, collector_code) is not None)
                    )
                    if not allowed:
                        await route.abort()
                        return
                    await throttle(current_transport()["source_id"])
                    await browser_continue(route)
                elif request.resource_type in {"image", "media", "font"} or not (
                    parsed.hostname == "cls.cn" or (parsed.hostname or "").endswith(".cls.cn")
                ):
                    await route.abort()
                else:
                    await browser_continue(route)

            await context.route("**/*", tracked_route)

            def track_request(request: Any) -> None:
                if _endpoint(request.url, collector_code):
                    request_actions[request] = action

            def capture(response: Any) -> None:
                kind = _endpoint(response.url, collector_code)
                owner = request_actions.pop(response.request, None)
                if kind and owner is not None:
                    queue.put_nowait((owner, kind, response))

            page.on("request", track_request)
            page.on("requestfailed", lambda request: request_actions.pop(request, None))
            page.on("response", capture)

            async def read_page() -> tuple[list[dict[str, Any]], bool]:
                nonlocal last_time
                rows, terminal = await _read_official_page(
                    queue, collector_code, raw_pages, action=action, last_time=last_time
                )
                await _settle_page(page, collector_code, rows, terminal)
                for row in rows:
                    published = _timestamp(row.get("ctime"))
                    # Depth appends only unseen IDs; its next official request
                    # uses the accumulated list tail, not a repeated recommendation.
                    if collector_code == "cls_telegraph" or str(row["id"]) not in listed_ids:
                        if published is None:
                            raise SourceError("parse_error", "CLS 新闻时间缺失，无法确认官方分页边界")
                        last_time = int(published.timestamp())
                    listed_ids.add(str(row["id"]))
                return rows, terminal

            response = await page.goto(_ENTRIES[collector_code], wait_until="domcontentloaded", timeout=60_000)
            if response is None or response.status >= 400:
                raise SourceError("access_denied", "CLS 入口页面访问失败")
            target_rows, exhausted = await read_page()
            anchor = state.get("last_id")
            anchor_found = anchor is None
            wanted_page = state["page"]
            start_position = 0
            for index in range(_MAX_REPLAY + 1):
                reached_page = index
                if anchor:
                    if anchor_found:
                        break
                    for position, row in enumerate(target_rows):
                        if str(row["id"]) == anchor:
                            anchor_found = True
                            start_position = position + 1
                            break
                    if anchor_found and start_position < len(target_rows):
                        break
                elif index >= wanted_page:
                    break
                if exhausted:
                    break
                if index == _MAX_REPLAY:
                    break
                action += 1
                await _advance_page(page, collector_code, action)
                start_position = 0
                target_rows, exhausted = await read_page()
            if (anchor and not anchor_found) or (not anchor and reached_page < wanted_page):
                coverage_gap = {
                    "reason": "cls_cursor_unreachable",
                    "replay_page": reached_page,
                    "cursor": cursor,
                }

            unique: dict[str, tuple[dict[str, Any], datetime | None]] = {}
            all_before_start = bool(target_rows) and start_at is not None
            for position, listed in enumerate(target_rows):
                published = _timestamp(listed.get("ctime"))
                if published is None and (start_at is not None or end_at is not None):
                    raise SourceError("parse_error", "CLS 新闻时间缺失，无法确认请求时间范围的覆盖")
                if published is None or start_at is None or published >= start_at:
                    all_before_start = False
                if position >= start_position:
                    unique[str(listed["id"])] = (listed, published)
            exhausted = exhausted or all_before_start
            if coverage_gap is None:
                if exhausted and start_at is not None and (last_time is None or last_time > start_at.timestamp()):
                    coverage_gap = {"reason": "cls_history_unavailable", "replay_page": reached_page}
                elif not exhausted and reached_page == _MAX_REPLAY:
                    coverage_gap = {"reason": "cls_replay_limit", "replay_page": reached_page, "cursor": cursor}
            if coverage_gap is not None:
                exhausted = False
            detail_count = 0
            for ident, (listed, published) in unique.items():
                if not ident.isdecimal():
                    raise SourceError("parse_error", "CLS 新闻 ID 格式变化")
                if published is not None and (
                    (start_at is not None and published < start_at) or (end_at is not None and published >= end_at)
                ):
                    continue
                item = dict(listed)
                restricted = item.get("isFree") is False
                # Permission is taken from the visible card, never inferred from type IDs.
                links = page.locator(f'a[href="/detail/{ident}"], a[href="https://www.cls.cn/detail/{ident}"]')
                if await links.count():
                    restricted = restricted or await links.first.evaluate("""element => {
                        let node = element;
                        for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
                            const links = Array.from(node.querySelectorAll('a[href*="/detail/"]'));
                            const ids = new Set(links.map(a => a.pathname));
                            if (ids.size > 1) break;
                            if (/解锁直达|专享/.test(node.innerText || '')) return true;
                        }
                        return false;
                    }""")
                gap = None
                need_detail = collector_code == "cls_depth" or not item.get("content")
                if need_detail and not restricted and not item.get("external_link"):
                    if max_details is not None and detail_count >= max_details:
                        gap = "max_details_reached"
                    else:
                        detail_count += 1
                        detail_page = await context.new_page()
                        await detail_page.route("**/*", tracked_route)
                        try:
                            detail_response = await detail_page.goto(
                                f"https://www.cls.cn/detail/{ident}", wait_until="domcontentloaded", timeout=60_000
                            )
                            if detail_response is None or detail_response.status >= 400:
                                raise SourceError("access_denied", "CLS 普通详情页面访问失败")
                            embedded = await detail_page.locator("script#__NEXT_DATA__").text_content(timeout=15_000)
                            try:
                                detail = json.loads(embedded or "")["props"]["pageProps"]["articleDetail"]
                            except (ValueError, KeyError, TypeError):
                                raise SourceError("parse_error", "CLS 详情 articleDetail 字段缺失或结构变化") from None
                            if not isinstance(detail, dict) or str(detail.get("id")) != ident:
                                raise SourceError("parse_error", "CLS 详情 ID 与列表不一致")
                            raw_details.append(_news_raw(detail))
                            item.update(detail)
                            restricted = detail.get("isFree") is False
                            if not restricted and not _text(detail.get("content"))[0]:
                                gap = "public_detail_content_missing"
                        finally:
                            await drain_routes(detail_page)
                            await detail_page.close()
                article = _article(item, restricted=restricted, gap=gap)
                articles.append(article)
        except BrowserTimeout:
            raise SourceError("parse_error", "CLS 页面或正文未在限定时间内可用") from None
        except BrowserError as exc:
            if is_connection_failure(exc):
                await note_transport_failure()
                raise SourceError("transport_error", "CLS 连接失败，等待有界出口重试") from None
            raise SourceError("access_denied", "CLS 浏览器启动或页面访问失败") from None
        finally:
            if context is not None:
                await drain_routes()
                await context.close()
            if browser is not None:
                await browser.close()

    next_cursor = None
    if not exhausted and coverage_gap is None:
        state = {"collector": collector_code, "page": reached_page + 1, "last_id": str(target_rows[-1]["id"])}
        next_cursor = base64.urlsafe_b64encode(json.dumps(state).encode()).decode().rstrip("=")
    return PageResult(
        articles=articles,
        next_url=_ENTRIES[collector_code] if next_cursor else None,
        next_cursor=next_cursor,
        exhausted=exhausted,
        coverage_gap=coverage_gap,
        raw_text=json.dumps({"pages": raw_pages, "details": raw_details}, ensure_ascii=False),
        response_status=200,
        content_type="application/json",
        fetched_at=datetime.now(UTC),
        parser_version="cls-official-browser-v3",
    )
