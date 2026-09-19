from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

_DIRECTION_THRESHOLD = 0.12
_BUCKET_COUNT = 6


def _direction(score: float) -> str:
    if score >= _DIRECTION_THRESHOLD:
        return "bullish"
    if score <= -_DIRECTION_THRESHOLD:
        return "bearish"
    return "neutral"


def _score(positive_weight: int, negative_weight: int, total_weight: int) -> float:
    if total_weight == 0:
        return 0.0
    return round((positive_weight - negative_weight) / total_weight, 4)


def build_market_overview(
    rows: list[tuple[str, str, int, datetime, str]],
    *,
    data_as_of: datetime,
    generated_at: datetime,
    hours: int,
) -> dict[str, Any]:
    """Aggregate classified news into an explicit news-sentiment signal."""
    window_start = data_as_of - timedelta(hours=hours)
    bucket_width = timedelta(hours=hours / _BUCKET_COUNT)
    categories: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "count": 0,
            "positive": 0,
            "negative": 0,
            "neutral": 0,
            "weight": 0,
            "positive_weight": 0,
            "negative_weight": 0,
        }
    )
    buckets = [
        {
            "start_at": window_start + bucket_width * index,
            "end_at": window_start + bucket_width * (index + 1),
            "count": 0,
            "weight": 0,
            "positive_weight": 0,
            "negative_weight": 0,
        }
        for index in range(_BUCKET_COUNT)
    ]
    sentiments = {"positive": 0, "negative": 0, "neutral": 0}
    sources: set[str] = set()
    total_weight = positive_weight = negative_weight = 0

    for category, sentiment, importance, published_at, source in rows:
        weight = max(1, importance)
        sentiments[sentiment] += 1
        sources.add(source)
        total_weight += weight
        positive_weight += weight if sentiment == "positive" else 0
        negative_weight += weight if sentiment == "negative" else 0

        group = categories[category]
        group["count"] += 1
        group[sentiment] += 1
        group["weight"] += weight
        group["positive_weight"] += weight if sentiment == "positive" else 0
        group["negative_weight"] += weight if sentiment == "negative" else 0

        offset = (published_at - window_start).total_seconds() / bucket_width.total_seconds()
        index = min(_BUCKET_COUNT - 1, max(0, int(offset)))
        bucket = buckets[index]
        bucket["count"] += 1
        bucket["weight"] += weight
        bucket["positive_weight"] += weight if sentiment == "positive" else 0
        bucket["negative_weight"] += weight if sentiment == "negative" else 0

    overall_score = _score(positive_weight, negative_weight, total_weight)
    category_signals = []
    for category, group in categories.items():
        score = _score(group["positive_weight"], group["negative_weight"], group["weight"])
        category_signals.append(
            {
                "category": category,
                "count": group["count"],
                "positive": group["positive"],
                "negative": group["negative"],
                "neutral": group["neutral"],
                "average_importance": round(group["weight"] / group["count"], 2),
                "score": score,
                "direction": _direction(score),
            }
        )
    category_signals.sort(key=lambda item: (-item["count"], item["category"]))

    timeline = []
    for bucket in buckets:
        score = _score(bucket["positive_weight"], bucket["negative_weight"], bucket["weight"])
        timeline.append(
            {
                "start_at": bucket["start_at"],
                "end_at": bucket["end_at"],
                "count": bucket["count"],
                "score": score,
                "direction": _direction(score),
            }
        )

    volume_strength = min(len(rows), 120) / 120 * 25
    signal_strength = round(min(100, abs(overall_score) * 75 + volume_strength)) if rows else 0
    recent_positive = sum(bucket["positive_weight"] for bucket in buckets[-2:])
    recent_negative = sum(bucket["negative_weight"] for bucket in buckets[-2:])
    recent_weight = sum(bucket["weight"] for bucket in buckets[-2:])
    recent_score = _score(recent_positive, recent_negative, recent_weight)
    outlook_score = round(max(-1.0, min(1.0, overall_score * 0.6 + recent_score * 0.4)), 4)
    outlook_direction = _direction(outlook_score)
    outlook_strength = round(min(100, abs(outlook_score) * 75 + volume_strength)) if rows else 0
    direction_text = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}
    outlook = {
        "horizon_hours": 24,
        "direction": outlook_direction,
        "score": outlook_score,
        "signal_strength": outlook_strength,
        "rationale": (
            f"窗口整体信号为{direction_text[_direction(overall_score)]}（{overall_score:+.2f}），"
            f"最近两个时间段信号为{direction_text[_direction(recent_score)]}（{recent_score:+.2f}）。"
        ),
        "methodology": "按新闻重要性加权，将全窗口信号占 60%、最近两个时间段动量占 40% 合成。",
    }
    return {
        "generated_at": generated_at,
        "data_as_of": data_as_of,
        "window_start": window_start,
        "window_end": data_as_of,
        "hours": hours,
        "stale": generated_at - data_as_of > timedelta(hours=2),
        "total_news": len(rows),
        "source_count": len(sources),
        "sentiments": sentiments,
        "overall_score": overall_score,
        "direction": _direction(overall_score),
        "signal_strength": signal_strength,
        "categories": category_signals,
        "timeline": timeline,
        "outlook": outlook,
        "disclaimer": "该结果仅反映已采集新闻的情绪与重要性，不代表实时行情价格，也不构成投资建议。",
    }
