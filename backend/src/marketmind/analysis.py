"""第二阶段：新闻分类、跨来源聚合和可追溯摘要。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from marketmind.config import Settings
from marketmind.models import NewsAnalysis, NewsCluster, NewsItem, NewsRevision, NewsSummary, Source

CLASSIFIER_VERSION = "rules-v1"
_CATEGORIES = {
    "宏观": ("美联储", "央行", "利率", "通胀", "GDP", "非农", "就业", "财政", "货币政策"),
    "政策": ("政策", "国务院", "监管", "证监会", "央行", "会议", "法规", "发布"),
    "公司": ("公司", "财报", "业绩", "IPO", "融资", "收购", "董事会", "CEO", "涨停"),
    "行业": ("行业", "产业", "供应链", "汽车", "半导体", "能源", "银行", "地产", "AI"),
    "市场": ("股市", "股票", "港股", "A股", "美股", "债券", "期货", "汇率", "原油", "黄金"),
}
_POSITIVE = ("上涨", "增长", "利好", "突破", "回暖", "盈利", "上调", "创新高")
_NEGATIVE = ("下跌", "下降", "利空", "风险", "亏损", "暴跌", "下调", "危机", "制裁")
_TOKEN_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def _text(*values: str | None) -> str:
    return " ".join(value.strip() for value in values if value and value.strip())


def _display_title(revision: NewsRevision) -> str:
    title = (revision.title or "").strip()
    if title:
        return title
    content = _text(revision.summary, revision.body_text)
    first_sentence = re.split(r"[。！？\n]", content, maxsplit=1)[0].strip()
    return (first_sentence or content or "未命名资讯")[:100]


def _classify(title: str, body: str | None) -> tuple[str, list[str], str, int]:
    content = _text(title, body)
    scores = {
        category: sum(content.lower().count(word.lower()) for word in words) for category, words in _CATEGORIES.items()
    }
    category = max(scores, key=scores.get) if max(scores.values(), default=0) else "综合"
    topics = [word for word in _CATEGORIES.get(category, ()) if word.lower() in content.lower()][:5]
    positive = sum(content.count(word) for word in _POSITIVE)
    negative = sum(content.count(word) for word in _NEGATIVE)
    sentiment = "positive" if positive > negative else "negative" if negative > positive else "neutral"
    importance = min(
        5,
        3
        + (1 if any(word in title for word in ("重大", "突发", "央行", "美联储")) else 0)
        + (1 if len(content) > 800 else 0),
    )
    return category, topics, sentiment, importance


def _cluster_key(title: str, category: str) -> str:
    normalized = "".join(_TOKEN_RE.split(title.lower()))
    return hashlib.sha256(f"{category}:{normalized}".encode()).hexdigest()


def _fallback_summary(title: str, rows: list[tuple[NewsItem, NewsRevision]]) -> tuple[str, list[str]]:
    points = []
    for _, revision in rows[:5]:
        text = _text(revision.body_text, revision.summary)
        if text:
            points.append(text[:180])
    summary = f"围绕“{title}”的相关信息已从 {len(rows)} 条来源记录归并。" + (
        f"核心内容：{points[0]}" if points else "当前暂无可用正文，需后续补充。"
    )
    return summary, points[:5]


def _ai_configured(settings: Settings) -> bool:
    return bool(
        settings.ai_base_url
        and settings.ai_model.strip()
        and settings.ai_api_key
        and settings.ai_api_key.get_secret_value().strip()
    )


def _ai_summary(
    settings: Settings, title: str, rows: list[tuple[NewsItem, NewsRevision]]
) -> tuple[str, list[str], str, str]:
    fallback, points = _fallback_summary(title, rows)
    if not _ai_configured(settings):
        return fallback, points, "fallback", "not_configured"
    source_text = "\n".join(
        f"- {revision.title}: {_text(revision.body_text, revision.summary)[:1200]}" for _, revision in rows[:10]
    )
    payload = {
        "model": settings.ai_model,
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是金融资讯编辑。只根据给定材料输出 JSON：summary 为不超过200字的中文摘要，"
                    "key_points 为不超过5条要点。不得补充材料外事实。"
                ),
            },
            {"role": "user", "content": json.dumps({"title": title, "items": source_text}, ensure_ascii=False)},
        ],
    }
    if settings.ai_reasoning_effort != "disabled":
        payload["reasoning_effort"] = settings.ai_reasoning_effort
    try:
        response = httpx.post(
            f"{settings.ai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.ai_api_key.get_secret_value()}"},
            json=payload,
            timeout=settings.ai_timeout_seconds,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        summary = str(parsed["summary"]).strip()
        key_points = [str(item).strip() for item in parsed.get("key_points", [])][:5]
        if not summary:
            raise ValueError("empty_summary")
        return summary, key_points, "ai", settings.ai_model
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return fallback, points, "fallback", "ai_error"


def process_news(
    db: Session,
    settings: Settings,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    query = (
        select(NewsItem, NewsRevision)
        .join(NewsRevision, NewsRevision.id == NewsItem.current_revision_id)
        .join(Source, Source.id == NewsItem.source_id)
        .where(Source.code.in_(("wscn", "cls")))
        .where(NewsItem.withdrawn_at.is_(None))
        .order_by(NewsRevision.published_at.desc().nullslast(), NewsItem.id)
        .limit(limit)
    )
    if start_at is not None:
        query = query.where(NewsRevision.published_at >= start_at)
    if end_at is not None:
        query = query.where(NewsRevision.published_at < end_at)
    rows = db.execute(query).all()
    now = datetime.now(UTC)
    item_ids = [item.id for item, _ in rows]
    analyses = {
        analysis.news_id: analysis
        for analysis in db.scalars(select(NewsAnalysis).where(NewsAnalysis.news_id.in_(item_ids)))
    }
    groups: dict[str, list[tuple[NewsItem, NewsRevision]]] = defaultdict(list)
    group_categories: dict[str, str] = {}
    for item, revision in rows:
        title = _display_title(revision)
        category, topics, sentiment, importance = _classify(title, revision.body_text or revision.summary)
        analysis = analyses.get(item.id)
        if analysis is None:
            analysis = NewsAnalysis(
                news_id=item.id,
                revision_id=revision.id,
                category=category,
                topics=topics,
                sentiment=sentiment,
                importance=importance,
                classifier_version=CLASSIFIER_VERSION,
                processed_at=now,
            )
            db.add(analysis)
        else:
            analysis.revision_id = revision.id
            analysis.category, analysis.topics, analysis.sentiment, analysis.importance = (
                category,
                topics,
                sentiment,
                importance,
            )
            analysis.classifier_version = CLASSIFIER_VERSION
            analysis.processed_at = now
        key = _cluster_key(title, category)
        groups[key].append((item, revision))
        group_categories[key] = category

    clusters = {
        cluster.cluster_key: cluster
        for cluster in db.scalars(select(NewsCluster).where(NewsCluster.cluster_key.in_(groups)))
    }
    new_clusters = [
        NewsCluster(cluster_key=key, category=group_categories[key], title=_display_title(group[0][1]))
        for key, group in groups.items()
        if key not in clusters
    ]
    db.add_all(new_clusters)
    db.flush()
    clusters.update({cluster.cluster_key: cluster for cluster in new_clusters})
    cluster_ids = [cluster.id for cluster in clusters.values()]
    existing_summaries = {
        summary.cluster_id: summary
        for summary in db.scalars(select(NewsSummary).where(NewsSummary.cluster_id.in_(cluster_ids)))
    }

    providers: set[str] = set()
    for key, group in groups.items():
        cluster = clusters[key]
        cluster.category = group_categories[key]
        cluster.title = _display_title(group[0][1])
        cluster.news_ids = [str(item.id) for item, _ in group]
        published = [revision.published_at for _, revision in group if revision.published_at]
        cluster.first_published_at = min(published) if published else None
        cluster.last_published_at = max(published) if published else None
        cluster.updated_at = now
        summary, points, provider, model = _ai_summary(settings, cluster.title, group)
        providers.add(provider)
        result = existing_summaries.get(cluster.id)
        if result is None:
            result = NewsSummary(cluster_id=cluster.id)
            db.add(result)
        result.title, result.summary, result.key_points = cluster.title, summary, points
        result.provider, result.model, result.status, result.error_code = (
            provider,
            model,
            "fallback" if provider == "fallback" else "succeeded",
            model if provider == "fallback" else None,
        )
        result.updated_at = now
    db.commit()
    return {
        "processed": len(rows),
        "clusters": len(groups),
        "created_clusters": len(new_clusters),
        "summaries": len(groups),
        "provider": "ai" if providers == {"ai"} else "fallback",
    }
