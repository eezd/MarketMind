"""CLS pagination regressions using an offline official-flow browser fixture."""

import asyncio
import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from marketmind.adapters import cls
from marketmind.collection_types import SourceError

# All browser documents/API calls are fulfilled locally; no site traffic or session.
_DOCUMENT = """<!doctype html><html><body>
<div class="f-l w-894"><div id="items"></div><div id="status"></div></div>
<script>
const config = CONFIG;
let rows = [], manual = true, busy = false, ended = false;
const items = document.getElementById('items'), status = document.getElementById('status');
const query = params => new URLSearchParams(params).toString();
async function api(path, params = {}) {
    const result = await fetch(path + '?' + query(params));
    return (await result.json()).data;
}
function commit() {
    items.replaceChildren(...rows.map(row => {
        const link = document.createElement('a');
        link.href = '/detail/' + row.id;
        link.textContent = row.content;
        return link;
    }));
    status.textContent = ended ? (config.depth ? '已经加载到最后了' : '已加载完') : '';
    if (!ended && (manual || config.depth)) {
        status.textContent = '加载更多';
        status.onclick = load;
    } else status.onclick = null;
    busy = false;
}
async function load() {
    if (busy || ended) return;
    busy = true;
    manual = false;
    status.textContent = '加载中...';
    const tail = () => rows.length ? rows[rows.length - 1].ctime : 0;
    if (config.depth) {
        const next = await api('/v3/depth/list/1000', {last_time: tail(), rn: 20});
        ended = next.length === 0;
        rows.push(...next.filter(row => !rows.some(existing => existing.id === row.id)));
    } else {
        const cache = (await api('/api/cache', {name: 'telegraph', rn: 20, lastTime: tail()})).roll_data;
        rows.push(...cache);
        if (cache.length < 20) {
            const next = (await api('/v1/roll/get_roll_list', {
                last_time: tail(), rn: 20 - cache.length, refresh_type: 1
            })).roll_data;
            rows.push(...next);
            ended = next.length === 0;
        }
    }
    // The network response arrives before the visible load-state update.
    setTimeout(commit, 30);
}
window.addEventListener('scroll', () => { if (!manual) load(); });
(async () => {
    const data = await api(config.depth ? '/v3/depth/home/assembled/1000' : '/api/cache',
        config.depth ? {} : {name: 'telegraph'});
    rows = config.depth ? data.depth_list : data.roll_data;
    setTimeout(commit, 30);
})();
</script></body></html>"""


def row(ident):
    return {"id": ident, "ctime": ident, "content": f"news {ident}"}


def cursor(code, ident, page=1):
    state = {"collector": code, "page": page, "last_id": str(ident)}
    return base64.urlsafe_b64encode(json.dumps(state).encode()).decode().rstrip("=")


def offline_site(monkeypatch, *, depth=False, cache_size=0, terminal=False, initial_id=100):
    async def allow(*args):
        pass

    async def fulfill(route):
        request = route.request
        if request.resource_type == "document":
            await route.fulfill(
                content_type="text/html; charset=utf-8",
                body=_DOCUMENT.replace("CONFIG", json.dumps({"depth": depth})),
            )
            return
        parsed = urlsplit(request.url)
        params = parse_qs(parsed.query)
        if parsed.path == "/v3/depth/home/assembled/1000":
            data = {"depth_list": [row(initial_id)]}
        elif parsed.path == "/v3/depth/list/1000":
            data = [] if terminal else [row(int(params["last_time"][0]) - 1)]
        elif parsed.path == "/api/cache" and "lastTime" not in params:
            data = {"roll_data": [row(initial_id)]}
        elif parsed.path == "/api/cache":
            tail = int(params["lastTime"][0])
            data = {"roll_data": [row(tail - offset - 1) for offset in range(cache_size)]}
        elif parsed.path == "/v1/roll/get_roll_list":
            tail = int(params["last_time"][0])
            count = 0 if terminal else int(params["rn"][0])
            data = {"roll_data": [row(tail - offset - 1) for offset in range(count)]}
        else:
            raise AssertionError(f"Unexpected offline endpoint: {parsed.path}")
        await route.fulfill(content_type="application/json", body=json.dumps({"errno": 0, "data": data}))

    monkeypatch.setattr(cls, "guard_request", allow)
    monkeypatch.setattr(cls, "throttle", allow)
    monkeypatch.setattr(cls, "browser_continue", fulfill)
    monkeypatch.setattr(cls, "current_transport", lambda: {"playwright_proxy": None, "source_id": "offline-cls"})


@pytest.mark.parametrize("cache_size", [0, 19, 20])
def test_telegraph_combines_cache_and_supplement_before_next_scroll(monkeypatch, cache_size):
    offline_site(monkeypatch, cache_size=cache_size)
    result = asyncio.run(cls.fetch_page("cls_telegraph", cursor("cls_telegraph", 80, page=2)))
    assert [article.source_item_id for article in result.articles] == [str(ident) for ident in range(79, 59, -1)]
    assert not result.exhausted
    assert cls._cursor(result.next_cursor, "cls_telegraph")["last_id"] == "60"


@pytest.mark.parametrize("code", ["cls_depth", "cls_telegraph"])
def test_terminal_dom_commit_does_not_claim_uncovered_time_range(monkeypatch, code):
    offline_site(monkeypatch, depth=code == "cls_depth", terminal=True)
    result = asyncio.run(
        cls.fetch_page(code, cursor(code, 100), start_at=datetime.fromtimestamp(50, UTC), max_details=0)
    )
    assert result.coverage_gap["reason"] == "cls_history_unavailable"
    assert not result.exhausted
    assert result.next_cursor is None
    # Without a requested lower bound, the same official terminal state is valid.
    result = asyncio.run(cls.fetch_page(code, cursor(code, 100), max_details=0))
    assert result.exhausted
    assert result.next_cursor is None
    assert result.articles == []


def test_partial_cache_is_retained_when_supplement_reaches_terminal(monkeypatch):
    offline_site(monkeypatch, cache_size=19, terminal=True)
    result = asyncio.run(cls.fetch_page("cls_telegraph", cursor("cls_telegraph", 100)))
    assert [article.source_item_id for article in result.articles] == [str(ident) for ident in range(99, 80, -1)]
    assert result.exhausted
    assert result.next_cursor is None


def test_previous_action_and_wrong_supplement_cannot_supply_current_page():
    async def scenario():
        queue = asyncio.Queue()

        def response(path, rows):
            async def payload():
                return {"errno": 0, "data": {"roll_data": rows}}

            return SimpleNamespace(url=f"https://www.cls.cn{path}", status=200, json=payload)

        queue.put_nowait((1, "cache_history", response("/api/cache?lastTime=100&rn=20", [row(99)])))
        queue.put_nowait((2, "cache_history", response("/api/cache?lastTime=80&rn=20", [row(79)])))
        queue.put_nowait(
            (1, "history", response("/v1/roll/get_roll_list?last_time=99&rn=19&refresh_type=1", [row(98)]))
        )
        queue.put_nowait(
            (2, "history", response("/v1/roll/get_roll_list?last_time=78&rn=19&refresh_type=1", [row(77)]))
        )
        queue.put_nowait(
            (2, "history", response("/v1/roll/get_roll_list?last_time=79&rn=19&refresh_type=1", [row(78)]))
        )
        rows, terminal = await cls._read_official_page(queue, "cls_telegraph", [], action=2, last_time=80)
        assert [item["id"] for item in rows] == [79, 78]
        assert not terminal

    asyncio.run(scenario())


def test_partial_terminal_page_can_complete_a_reached_lower_bound(monkeypatch):
    offline_site(monkeypatch, cache_size=19, terminal=True)
    result = asyncio.run(
        cls.fetch_page("cls_telegraph", cursor("cls_telegraph", 100), start_at=datetime.fromtimestamp(81, UTC))
    )
    assert [article.source_item_id for article in result.articles] == [str(ident) for ident in range(99, 80, -1)]
    assert result.exhausted
    assert result.next_cursor is None


@pytest.mark.parametrize("anchor", [1020, 9999])
def test_replay_boundary_retains_last_page_without_unusable_successor(monkeypatch, anchor):
    offline_site(monkeypatch, initial_id=2000)
    result = asyncio.run(
        cls.fetch_page(
            "cls_telegraph", cursor("cls_telegraph", anchor, page=50), start_at=datetime.fromtimestamp(0, UTC)
        )
    )
    assert [article.source_item_id for article in result.articles] == [str(ident) for ident in range(1019, 999, -1)]
    assert result.next_cursor is None
    assert not result.exhausted
    assert result.coverage_gap["reason"] == ("cls_replay_limit" if anchor == 1020 else "cls_cursor_unreachable")
    assert result.coverage_gap["replay_page"] == 50
    assert json.loads(result.raw_text)["pages"][-1]["articles"][-1]["id"] == 1000


def test_legacy_over_limit_cursor_reports_coverage_without_browser(monkeypatch):
    def no_transport():
        pytest.fail("An over-limit cursor must not open a browser or request transport")

    monkeypatch.setattr(cls, "current_transport", no_transport)
    with pytest.raises(SourceError) as error:
        asyncio.run(cls.fetch_page("cls_telegraph", cursor("cls_telegraph", 1000, page=51)))
    assert error.value.code == "cls_replay_limit"


def test_unreachable_anchor_at_history_end_is_not_complete(monkeypatch):
    offline_site(monkeypatch, terminal=True)
    result = asyncio.run(cls.fetch_page("cls_telegraph", cursor("cls_telegraph", 9999)))
    assert result.coverage_gap["reason"] == "cls_cursor_unreachable"
    assert not result.exhausted
    assert result.next_cursor is None


def test_partial_terminal_page_retains_data_for_uncovered_lower_bound(monkeypatch):
    offline_site(monkeypatch, cache_size=19, terminal=True)
    result = asyncio.run(
        cls.fetch_page("cls_telegraph", cursor("cls_telegraph", 100), start_at=datetime.fromtimestamp(50, UTC))
    )
    assert [article.source_item_id for article in result.articles] == [str(ident) for ident in range(99, 80, -1)]
    assert result.coverage_gap["reason"] == "cls_history_unavailable"
    assert not result.exhausted
    assert result.next_cursor is None


def test_official_response_timeout_is_retryable(monkeypatch):
    async def timeout(awaitable, _seconds):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(cls.asyncio, "wait_for", timeout)
    with pytest.raises(SourceError) as error:
        asyncio.run(cls._read_official_page(asyncio.Queue(), "cls_telegraph", [], action=0, last_time=None))

    assert error.value.code == "response_timeout"


def test_structurally_invalid_cursor_remains_parse_error():
    with pytest.raises(SourceError) as error:
        asyncio.run(cls.fetch_page("cls_telegraph", cursor("cls_depth", 100, page=51)))
    assert error.value.code == "parse_error"
