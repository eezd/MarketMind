"""Official Nuxt articles and hydrated JinFlashItem history, without API replay."""

import json
import os
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from marketmind.collection_types import Article, PageResult, SourceError
from marketmind.jin10_session import close_page, has_runtime_session, isolated_browser, load_session, verify_context
from marketmind.proxy_runtime import is_connection_failure, note_transport_failure, request_context

_NEWS = "https://xnews.jin10.com"
_LIVE = "https://www.jin10.com/"
_ZONE = ZoneInfo("Asia/Shanghai")
_NEWS_FIELDS = (
    "id",
    "title",
    "introduction",
    "content",
    "display_datetime",
    "detail_url",
    "web_redirect_url",
    "source",
    "source_url",
    "vip",
    "super_vip",
    "elite_vip",
    "access",
    "ndata",
    "more",
    "reveal_tags",
    "web_thumbs",
    "mobile_thumbs",
    "audio_url",
    "vdata",
    "type",
    "data_type",
)
_FLASH_JS = """() => {
    const rows = new Map();
    for (const element of document.querySelectorAll('[id^="flash"]')) {
        let component = element.__vue__;
        while (component && !component.flash) component = component.$parent;
        if (!component?.flash?.id) continue;
        const f = component.flash;
        rows.set(String(f.id), {id: f.id, time: f.time, type: f.type,
            data: f.data, remark: f.remark, channel: f.channel, important: f.important,
            tags: f.tags, content: component.flashContent, locked: component.needUnLock,
            dom_id: element.id});
    }
    return [...rows.values()];
}"""


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")
        if tag == "img":
            values = dict(attrs)
            image = values.get("src") or values.get("data-src")
            if image and urlsplit(image).scheme in {"https", "http"}:
                self.images.append(image)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _text(value: Any) -> tuple[str, list[str]]:
    parser = _Text()
    if value is not None and not isinstance(value, str):
        raise SourceError("parse_error", "Jin10 article content changed structure")
    parser.feed(value or "")
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip()), parser.images


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=_ZONE)).astimezone(UTC)
    except ValueError:
        return None


def _safe_news(item: dict[str, Any]) -> dict[str, Any]:
    # Never serialize the entire Nuxt store: it can include the logged-in user.
    selected = {key: item[key] for key in _NEWS_FIELDS if key in item}
    author = item.get("author")
    if isinstance(author, dict):
        selected["author_name"] = author.get("nick")
    return selected


def _paid(item: dict[str, Any]) -> bool:
    ndata = item.get("ndata") or {}
    access = item.get("access") or {}
    return bool(
        item.get("vip")
        or item.get("super_vip")
        or item.get("elite_vip")
        or ndata.get("vip_type")
        or access.get("has_products")
    )


def _article(item: dict[str, Any], *, detail: bool = False) -> Article:
    identifier = item.get("id")
    if identifier is None:
        raise SourceError("parse_error", "Jin10 article is missing its source ID")
    # /details/{id} is the observed official router link, not the webapp share URL.
    canonical = f"{_NEWS}/details/{identifier}"
    summary, _ = _text(item.get("introduction"))
    body, images = _text(item.get("content"))
    external = bool(item.get("web_redirect_url"))
    paid = _paid(item)
    status = "external_link" if external else "paywalled" if paid else "complete" if detail and body else "pending"
    if status != "complete":
        body = ""
    tags: list[Any] = list(item.get("reveal_tags") or [])
    tags.extend({"kind": "image", "url": url} for url in dict.fromkeys(images + (item.get("web_thumbs") or [])))
    if detail and status == "pending" and not body and images:
        status = "summary_only" if summary else "unavailable"
        tags.append({"gap": "image_only_content"})
    if item.get("web_redirect_url"):
        tags.append({"kind": "external_link", "url": item["web_redirect_url"]})
    if item.get("audio_url"):
        tags.append({"kind": "audio", "url": item["audio_url"]})
    if item.get("more") and status == "complete":
        status = "pending"
        tags.append({"gap": "source_reports_additional_content"})
    return Article(
        source_item_id=str(identifier),
        canonical_url=canonical,
        original_url=item.get("detail_url") or canonical,
        title=_text(item.get("title"))[0],
        summary=summary or None,
        body_text=body or None,
        body_status=status,
        published_at=_date(item.get("display_datetime")),
        source_time_text=item.get("display_datetime"),
        source_timezone="Asia/Shanghai",
        author=item.get("author_name"),
        source_tags=tags,
    )


async def _goto(page: Page, url: str) -> int:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    if response is None or response.status in {401, 403, 429}:
        raise SourceError("access_denied", "Jin10 denied the official page request")
    if response.status >= 400:
        raise SourceError("parse_error", "Jin10 official page returned an error")
    return response.status


async def _detail(context: BrowserContext, article: Article) -> tuple[Article, dict[str, Any]]:
    page = await context.new_page()
    try:
        await _goto(page, article.canonical_url)
        await page.wait_for_function("window.__NUXT__?.data?.some(x => x.news?.id)")
        item = _safe_news(await page.evaluate("window.__NUXT__.data.find(x => x.news?.id).news"))
        if str(item.get("id")) != article.source_item_id:
            raise SourceError("parse_error", "Jin10 detail identity differs from the list record")
        result = _article(item, detail=True)
        if result.body_status == "pending" and not result.body_text:
            raise SourceError("parse_error", "Jin10 ordinary detail has no complete content")
        return result, item
    finally:
        await close_page(page)


async def _news(
    context: BrowserContext,
    cursor: str | None,
    max_details: int | None,
    start_at: datetime | None,
    end_at: datetime | None,
) -> PageResult:
    try:
        number = int(cursor) if cursor else 1
    except ValueError:
        raise SourceError("parse_error", "Invalid Jin10 article page cursor") from None
    if number < 1:
        raise SourceError("parse_error", "Invalid Jin10 article page cursor")
    page = await context.new_page()
    try:
        status = await _goto(page, f"{_NEWS}/page/{number}")
        await page.wait_for_function("window.__NUXT__?.data?.some(x => Array.isArray(x.list) && 'total' in x)")
        data = await page.evaluate(
            "(() => {const x=window.__NUXT__.data.find(x=>Array.isArray(x.list)&&'total' in x);"
            "return {page:x.page,page_size:x.page_size,total:x.total,list:x.list}})()"
        )
    finally:
        await close_page(page)
    if int(data["page"]) != number or int(data["page_size"]) <= 0:
        raise SourceError("parse_error", "Jin10 pagination did not return the requested page")
    records = [_safe_news(item) for item in data["list"]]
    articles = []
    details = []
    fetched = 0
    for item in records:
        article = _article(item)
        in_range = article.published_at is not None and (
            (start_at is None or article.published_at >= start_at) and (end_at is None or article.published_at < end_at)
        )
        if article.body_status == "pending" and (in_range or (start_at is None and end_at is None)):
            if max_details is None or fetched < max_details:
                article, raw = await _detail(context, article)
                details.append(raw)
                fetched += 1
            else:
                article.source_tags.append({"gap": "detail_limit_reached"})
        articles.append(article)
    exhausted = number * int(data["page_size"]) >= int(data["total"])
    if not records and not exhausted:
        raise SourceError("parse_error", "Jin10 returned an empty article page before its reported total")
    return PageResult(
        articles=articles,
        next_cursor=None if exhausted else str(number + 1),
        next_url=None if exhausted else f"{_NEWS}/page/{number + 1}",
        exhausted=exhausted,
        raw_text=json.dumps(
            {"page": data["page"], "total": data["total"], "list": records, "details": details}, ensure_ascii=False
        ),
        response_status=status,
        content_type="application/json",
        fetched_at=datetime.now(UTC),
        parser_version="jin10-nuxt-v1",
    )


async def _load_more(page: Page, tail_id: str) -> bool:
    button = page.locator("a.load-more")
    if not await button.count():
        if await page.locator(".login-mask").is_visible():
            raise SourceError("waiting_login", "Jin10 history requires a valid authenticated session")
        # Only an explicit official end marker proves exhaustion.
        if await page.get_by_text("没有更多了", exact=True).count():
            return False
        raise SourceError("parse_error", "Jin10 history control is unavailable without an explicit end marker")
    await button.evaluate("element => element.click()")
    try:
        await page.wait_for_function(
            "tail => {const rows=[...document.querySelectorAll('[id^=flash]')].map(e=>{"
            "let v=e.__vue__;while(v&&!v.flash)v=v.$parent;return v?.flash?.id&&String(v.flash.id)"
            "}).filter(Boolean);const index=rows.indexOf(tail);return index>=0&&index<rows.length-1}",
            arg=tail_id,
            timeout=90_000,
        )
    except PlaywrightTimeoutError:
        if await page.locator(".login-mask").is_visible():
            raise SourceError("waiting_login", "Jin10 login expired while loading history") from None
        if await page.get_by_text("没有更多了", exact=True).count():
            return False
        raise SourceError("parse_error", "Jin10 official load-more did not advance history") from None
    return True


async def _live(
    context: BrowserContext, cursor: str | None, max_history_pages: int, max_details: int | None
) -> PageResult:
    page = await context.new_page()
    try:
        async with page.expect_response(
            lambda response: (
                urlsplit(response.url).hostname == "uc-api.jin10.com" and urlsplit(response.url).path == "/userinfo"
            ),
            timeout=120_000,
        ) as identity_response:
            status = await _goto(page, _LIVE)
        identity = await (await identity_response.value).json()
        if identity.get("status") != 200 or not (identity.get("data") or {}).get("id"):
            raise SourceError("waiting_login", "Jin10 flash page did not confirm an authenticated identity")
        # 列表先于身份响应渲染；等待 Vue 将登录结果应用到当前页，不能把初始化遮罩误判为会话失效。
        await page.locator(".login-mask").wait_for(state="hidden", timeout=30_000)
        await page.wait_for_function("""() => {
            let component = document.querySelector('[id^="flash"]')?.__vue__;
            while (component && component.$options.name !== 'JinFlash') component = component.$parent;
            return component?.flashs?.length > 0;
        }""")
        await page.wait_for_function("[...document.querySelectorAll('[id^=flash]')].some(e => e.__vue__)")
        rows = await page.evaluate(_FLASH_JS)
        if not rows:
            raise SourceError("parse_error", "Jin10 flash components did not expose source data")
        exhausted = False
        if cursor:
            # Persist the observed tail ID. Replay official history until that exact
            # anchor is reached, so new live arrivals cannot shift numeric pages.
            found = False
            for _ in range(max_history_pages):
                ids = {str(row["id"]) for row in rows}
                if cursor in ids:
                    found = True
                    position = next(index for index, row in enumerate(rows) if str(row["id"]) == cursor)
                    older = rows[position + 1 :]
                    if older:
                        rows = older
                        break
                    exhausted = not await _load_more(page, str(rows[-1]["id"]))
                    updated = await page.evaluate(_FLASH_JS)
                    anchor = next(index for index, row in enumerate(updated) if str(row["id"]) == cursor)
                    rows = updated[anchor + 1 :]
                    break
                if not await _load_more(page, str(rows[-1]["id"])):
                    raise SourceError("parse_error", "Jin10 history ended before the saved cursor was found")
                rows = await page.evaluate(_FLASH_JS)
            if not found:
                raise SourceError("parse_error", "Jin10 history replay limit reached before the saved cursor")
        articles = []
        details = []
        fetched = 0
        for row in rows:
            data = row["data"]
            if not isinstance(data, dict):
                raise SourceError("parse_error", "Jin10 flash data changed structure")
            content, images = _text(row.get("content") or data.get("content"))
            title = _text(data.get("title") or data.get("vip_title"))[0]
            tags: list[Any] = list(row.get("tags") or [])
            tags.append({"channels": row.get("channel", []), "type": row.get("type")})
            if data.get("pic"):
                images.append(data["pic"])
            tags.extend({"kind": "image", "url": url} for url in dict.fromkeys(images))
            if row.get("remark"):
                tags.append({"remarks": row["remark"]})
            paid = bool(
                row.get("locked")
                or data.get("lock")
                or data.get("vip_level")
                or data.get("exclusive_to")
                or data.get("tag") == "VIP"
            )
            body_status = "paywalled" if paid else "complete" if content else "pending"
            link = data.get("link")
            if link and not paid:
                parsed = urlsplit(link)
                if parsed.hostname == "xnews.jin10.com" and parsed.path.startswith("/details/"):
                    if max_details is None or fetched < max_details:
                        linked_id = parsed.path.removeprefix("/details/").strip("/")
                        linked, raw = await _detail(
                            context,
                            Article(
                                source_item_id=linked_id, canonical_url=link, original_url=link, body_status="pending"
                            ),
                        )
                        details.append(raw)
                        fetched += 1
                        body_status = linked.body_status
                        if linked.body_text:
                            content = "\n\n".join(part for part in (content, linked.body_text) if part)
                        tags.extend(linked.source_tags)
                    else:
                        body_status = "pending"
                        tags.append({"gap": "detail_limit_reached"})
                else:
                    body_status = "external_link"
                tags.append({"kind": "linked_article", "url": link})
            if not content and not paid and not images:
                raise SourceError(
                    "parse_error", "Jin10 flash has no source content; offscreen DOM text is not a substitute"
                )
            # Fragment is the actual observed DOM identity, not an invented API URL.
            canonical = f"{_LIVE}#{row['dom_id']}"
            articles.append(
                Article(
                    source_item_id=str(row["id"]),
                    canonical_url=canonical,
                    original_url=canonical,
                    title=title,
                    summary=_text(data.get("content"))[0] or None,
                    body_text=None if paid else content or None,
                    body_status=body_status,
                    published_at=_date(row.get("time")),
                    source_time_text=row.get("time"),
                    source_timezone="Asia/Shanghai",
                    source_tags=tags,
                    importance=int(row.get("important") or 0),
                )
            )
        return PageResult(
            articles=articles,
            next_cursor=str(rows[-1]["id"]) if rows and not exhausted else None,
            next_url=None if exhausted else _LIVE,
            exhausted=exhausted,
            raw_text=json.dumps({"flashes": rows, "details": details}, ensure_ascii=False),
            response_status=status,
            content_type="application/json",
            fetched_at=datetime.now(UTC),
            parser_version="jin10-flash-v1",
        )
    finally:
        await close_page(page)


async def fetch_page(
    collector_code: str,
    cursor: str | None,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    session_path: str | None = None,
    max_details: int | None = None,
    max_history_pages: int = 100,
) -> PageResult:
    """Fetch one official page and apply the half-open publication interval.

    Explicit detail budgets retain pending bodies with gap tags.
    History replay limits never imply exhaustion.
    """
    if collector_code not in {"jin10_news", "jin10_live"}:
        raise SourceError("parse_error", "Unsupported Jin10 collector")
    if max_history_pages < 1 or (max_details is not None and max_details < 0):
        raise SourceError("parse_error", "Jin10 request limits must be nonnegative and history limit positive")
    state = None
    use_session = collector_code == "jin10_live" or session_path is not None or has_runtime_session()
    if (
        not use_session
        and request_context.get() is None
        and (default_session := os.environ.get("MM_JIN10_SESSION_FILE"))
    ):
        try:
            Path(default_session).expanduser().lstat()
        except FileNotFoundError:
            pass  # A missing default session must not block public articles.
        except OSError:
            use_session = True  # Let the session loader reject inaccessible storage.
        else:
            use_session = True
    if use_session:
        state = load_session(session_path)
    try:
        async with isolated_browser(state) as context:
            if state is not None:
                await verify_context(context)
            if collector_code == "jin10_news":
                result = await _news(context, cursor, max_details, start_at, end_at)
            else:
                result = await _live(context, cursor, max_history_pages, max_details)
            if (start_at is not None or end_at is not None) and any(
                article.published_at is None for article in result.articles
            ):
                raise SourceError("parse_error", "Jin10 publication time is missing inside a bounded collection")
            if (
                start_at is not None
                and result.articles
                and all(article.published_at < start_at for article in result.articles)
            ):
                result.exhausted = True
            if result.exhausted:
                result.next_cursor = None
                result.next_url = None
            result.articles = [
                article
                for article in result.articles
                if article.published_at is None
                or (
                    (start_at is None or article.published_at >= start_at)
                    and (end_at is None or article.published_at < end_at)
                )
            ]
            return result
    except SourceError:
        raise
    except PlaywrightTimeoutError:
        raise SourceError(
            "parse_error", "Jin10 official page did not expose the expected data before timeout"
        ) from None
    except Exception as exc:
        if is_connection_failure(exc):
            await note_transport_failure()
            raise SourceError("transport_error", "Jin10 连接失败，等待有界出口重试") from None
        raise SourceError("access_denied", "Jin10 browser collection failed; session details were not logged") from None
