from types import SimpleNamespace

from marketmind.analysis import _ai_summary, _classify, _cluster_key, _display_title
from marketmind.config import Settings


def settings(**overrides):
    values = {
        "database_url": "postgresql+psycopg://user:password@localhost/marketmind_test",
        "redis_url": "redis://localhost/0",
        "cookie_secure": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_financial_classification_is_deterministic():
    category, topics, sentiment, importance = _classify(
        "美联储上调利率，黄金下跌",
        "通胀风险上升，市场预期政策继续收紧。",
    )

    assert category == "宏观"
    assert topics == ["美联储", "利率", "通胀"]
    assert sentiment == "negative"
    assert importance == 4
    assert _cluster_key(" 美联储：上调利率 ", category) == _cluster_key("美联储上调利率", category)


def test_flash_without_title_uses_body_first_sentence():
    revision = SimpleNamespace(title="", summary=None, body_text="美国公布最新就业数据。市场等待后续指引。")

    title = _display_title(revision)

    assert title == "美国公布最新就业数据"
    assert _cluster_key(title, "宏观") != _cluster_key("", "宏观")


def test_missing_ai_configuration_is_explicit_fallback():
    rows = [
        (
            SimpleNamespace(),
            SimpleNamespace(title="原油价格上涨", body_text="供应收紧推动国际原油价格上涨。", summary=None),
        )
    ]

    summary, points, provider, model = _ai_summary(settings(), "原油价格上涨", rows)

    assert "1 条来源记录" in summary
    assert points == ["供应收紧推动国际原油价格上涨。"]
    assert provider == "fallback"
    assert model == "not_configured"


def test_ai_reasoning_effort_is_optional_for_compatible_providers(monkeypatch):
    payloads = []

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": '{"summary":"摘要","key_points":[]}'}}]}

    def fake_post(*_args, **kwargs):
        payloads.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("marketmind.analysis.httpx.post", fake_post)
    rows = [
        (
            SimpleNamespace(),
            SimpleNamespace(title="市场动态", body_text="市场保持稳定。", summary=None),
        )
    ]

    _ai_summary(
        settings(
            ai_base_url="https://ai.example.com/v1", ai_api_key="secret", ai_model="model", ai_reasoning_effort="high"
        ),
        "市场动态",
        rows,
    )
    _ai_summary(
        settings(
            ai_base_url="https://ai.example.com/v1",
            ai_api_key="secret",
            ai_model="model",
            ai_reasoning_effort="disabled",
        ),
        "市场动态",
        rows,
    )

    assert payloads[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in payloads[1]
