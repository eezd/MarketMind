from datetime import UTC, datetime, timedelta

from marketmind.signals import build_market_overview


def test_market_overview_exposes_weighted_news_signal_and_freshness():
    data_as_of = datetime(2026, 9, 19, 12, tzinfo=UTC)
    rows = [
        ("宏观", "positive", 5, data_as_of - timedelta(hours=1), "wscn"),
        ("宏观", "positive", 3, data_as_of - timedelta(hours=2), "cls"),
        ("市场", "negative", 2, data_as_of - timedelta(hours=4), "cls"),
        ("市场", "neutral", 1, data_as_of - timedelta(hours=8), "wscn"),
    ]

    result = build_market_overview(
        rows,
        data_as_of=data_as_of,
        generated_at=data_as_of + timedelta(hours=3),
        hours=24,
    )

    assert result["direction"] == "bullish"
    assert result["overall_score"] == round(6 / 11, 4)
    assert result["stale"] is True
    assert result["source_count"] == 2
    assert result["sentiments"] == {"positive": 2, "negative": 1, "neutral": 1}
    assert sum(point["count"] for point in result["timeline"]) == 4
    assert result["categories"][0]["category"] == "宏观"
    assert result["categories"][0]["average_importance"] == 4
    assert result["outlook"]["horizon_hours"] == 24
    assert result["outlook"]["direction"] == "bullish"
    assert "60%" in result["outlook"]["methodology"]
