"""华尔街见闻 news/live 游客采集；只访问公开正文，保留脱敏新闻原文。"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from marketmind.collection_types import Article, PageResult, SourceError
from marketmind.proxy_runtime import current_transport, http_request_hook, http_response_hook, note_transport_failure

_SECRET_KEYS = {
    "token",
    "accesstoken",
    "refreshtoken",
    "authorization",
    "cookie",
    "setcookie",
    "password",
    "secret",
    "session",
    "sessionid",
    "apikey",
    "signature",
}


async def _get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    try:
        return await client.get(url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError):
        await note_transport_failure()
        raise SourceError("transport_error", "WSCN 连接失败，等待有界出口重试") from None
    except httpx.HTTPError:
        raise SourceError("access_denied", "WSCN 请求失败") from None


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact(child)
            for key, child in value.items()
            if str(key).lower().replace("_", "").replace("-", "") not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, str):

        def clean_url(match: re.Match[str]) -> str:
            url = urlsplit(html.unescape(match.group()))
            query = [
                (key, val)
                for key, val in parse_qsl(url.query)
                if key.lower().replace("_", "").replace("-", "") not in _SECRET_KEYS
            ]
            host = url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((url.scheme, host, url.path, urlencode(query), ""))

        return re.sub(r"https?://[^\s<>\"']+", clean_url, value)
    return value


def _dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC)
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class _Content(HTMLParser):
    def __init__(self, *, detail: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.detail = detail
        self.depth = 0
        self.hidden = 0
        self.parts: list[str] = []
        self.markup: list[str] = []
        self.media: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self.detail and tag == "section" and (self.depth or "articleBody" in (attributes.get("class") or "")):
            self.depth += 1
        if self.detail and not self.depth:
            return
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        self.markup.append(self.get_starttag_text())
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")
        if tag in {"img", "audio", "video", "source", "iframe"} and attributes.get("src"):
            self.media.append({"kind": tag, "url": attributes["src"]})

    def handle_endtag(self, tag: str) -> None:
        if self.detail and not self.depth:
            return
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        self.markup.append(f"</{tag}>")
        if tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")
        if self.detail and tag == "section":
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and (not self.detail or self.depth):
            self.parts.append(data)
            self.markup.append(html.escape(data))

    @property
    def text(self) -> str:
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


def _text(value: Any) -> str:
    parser = _Content()
    parser.feed(str(value or ""))
    return parser.text


def _article(row: dict[str, Any], live: bool = False) -> Article | None:
    item = row.get("resource", row)
    if not isinstance(item, dict):
        return None
    ident = item.get("id")
    uri = item.get("uri") or item.get("url")
    if not ident and uri:
        match = re.search(r"/(?:articles|livenews)/(\d+)", str(uri))
        ident = match.group(1) if match else None
    if not uri and ident:
        uri = f"https://wallstreetcn.com/{'livenews' if live else 'articles'}/{ident}"
    if not uri:
        return None
    paid = bool(
        item.get("is_priced")
        or item.get("is_in_vip_privilege")
        or item.get("vip_type")
        or re.search(r"/(?:member|premium)/articles/", str(uri))
    )
    external = urlsplit(str(uri)).hostname != "wallstreetcn.com"
    content = item.get("content") or item.get("content_text") or item.get("articleBody") or ""
    if item.get("content_more"):
        content = f"{content}\n{item['content_more']}"
    parser = _Content()
    if not paid:
        parser.feed(content)
    body = parser.text or None
    tags: list[Any] = []
    for key in ("source_name", "categories", "tags", "channels", "symbols", "images", "cover_images"):
        if item.get(key):
            tags.append({"field": key, "value": _redact(item[key])})
    tags.extend(_redact(parser.media))
    score = item.get("importance", item.get("score"))
    return Article(
        source_item_id=str(ident) if ident is not None else None,
        canonical_url=str(uri),
        original_url=str(uri),
        title=_text(item.get("title") or item.get("headline")),
        summary=_text(item.get("content_short") or item.get("subtitle")) or None,
        body_text=body,
        body_status="paywalled"
        if paid
        else "external_link"
        if external
        else "complete"
        if live and body
        else "pending",
        published_at=_dt(item.get("display_time") or item.get("datePublished")),
        source_updated_at=_dt(item.get("updated_at") or item.get("update_time")),
        author=_text(
            (item.get("author") or {}).get("display_name")
            if isinstance(item.get("author"), dict)
            else item.get("author")
        )
        or None,
        source_tags=tags,
        importance=int(score) if isinstance(score, (int, float)) else None,
    )


async def fetch_page(
    collector_code: str, cursor: str | None, *, start_at: datetime | None = None, end_at: datetime | None = None
) -> PageResult:
    live = collector_code in {"wscn_live", "live"}
    if collector_code not in {"wscn_news", "wscn_live", "news", "live"}:
        raise SourceError("parse_error", "未知 WSCN 入口")
    low = start_at.replace(tzinfo=UTC) if start_at and not start_at.tzinfo else start_at
    high = end_at.replace(tzinfo=UTC) if end_at and not end_at.tzinfo else end_at
    api_url = (
        "https://api-one-wscn.awtmt.com/apiv1/content/lives"
        if live
        else "https://api.wscn.net/apiv1/content/information-flow"
    )
    params = (
        {
            "channel": "global-channel",
            "client": "pc",
            "cursor": cursor,
            "limit": 20,
            "first_page": "false" if cursor else "true",
            "accept": "live,vip-live",
        }
        if live
        else {"channel": "global", "accept": "article", "limit": 20, "cursor": cursor}
    )
    headers = {"User-Agent": "MarketMind/1.0", "Referer": "https://wallstreetcn.com/"}
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=False,
        headers=headers,
        trust_env=False,
        proxy=current_transport()["httpx_proxy"],
        event_hooks={"request": [http_request_hook], "response": [http_response_hook]},
    ) as client:
        response = await _get(client, api_url, params={k: v for k, v in params.items() if v is not None})
        if response.status_code != 200:
            raise SourceError("access_denied", f"WSCN API HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("parse_error", "WSCN API 返回不是 JSON，无法确认分页覆盖") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("code") != 20000
            or not isinstance(data, dict)
            or not isinstance(data.get("items"), list)
            or "next_cursor" not in data
        ):
            raise SourceError("parse_error", "WSCN API 新闻列表或分页字段缺失")
        rows = data["items"]
        next_cursor = str(data["next_cursor"]) if data["next_cursor"] else None
        if next_cursor == cursor and cursor or rows and not next_cursor:
            raise SourceError("parse_error", "WSCN 分页游标未推进，无法确认覆盖")
        articles: list[Article] = []
        details: list[dict[str, Any]] = []
        timestamps: list[datetime] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SourceError("parse_error", "WSCN 新闻条目格式异常")
            article = _article(row, live)
            if article is None:
                raise SourceError("parse_error", "WSCN 新闻缺少标识或链接")
            stamp = article.published_at
            if stamp is None and (low or high):
                raise SourceError("parse_error", "WSCN 新闻时间缺失，无法确认区间覆盖")
            if stamp:
                timestamps.append(stamp)
                if low and stamp < low or high and stamp >= high:
                    continue
            if not live and article.body_status == "pending":
                detail = await _get(client, article.canonical_url)
                if detail.status_code == 200:
                    parser = _Content(detail=True)
                    parser.feed(detail.text)
                    details.append(
                        {
                            "id": article.source_item_id,
                            "url": article.canonical_url,
                            "body_html": "".join(parser.markup),
                        }
                    )
                    article = article.model_copy(
                        update={
                            "body_text": parser.text or None,
                            "body_status": "complete"
                            if parser.text
                            else "summary_only"
                            if article.summary
                            else "unavailable",
                            "source_tags": article.source_tags + _redact(parser.media),
                        }
                    )
                else:
                    article = article.model_copy(
                        update={"body_text": None, "body_status": "summary_only" if article.summary else "unavailable"}
                    )
            elif live and article.body_status == "pending":
                article = article.model_copy(
                    update={"body_status": "summary_only" if article.summary else "unavailable"}
                )
            articles.append(article)
        below_range = bool(low and timestamps and len(timestamps) == len(rows) and max(timestamps) < low)
        exhausted = not rows and not next_cursor or below_range
        return PageResult(
            articles=articles,
            next_cursor=None if exhausted else next_cursor,
            exhausted=exhausted,
            raw_text=json.dumps(
                _redact({"items": rows, "next_cursor": data["next_cursor"], "details": details}), ensure_ascii=False
            ),
            response_status=response.status_code,
            content_type="application/json",
            fetched_at=datetime.now(UTC),
            parser_version="wscn-http-v4",
        )
