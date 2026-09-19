"""受认证的第二阶段处理与摘要查询接口。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, tuple_

from marketmind.analysis import process_news
from marketmind.auth import Db, current_session
from marketmind.errors import ApiError
from marketmind.models import NewsAnalysis, NewsCluster, NewsItem, NewsRevision, NewsSummary, Source
from marketmind.pagination import decode_cursor, encode_cursor
from marketmind.schemas import (
    AnalysisRunInput,
    AnalysisRunResponse,
    MarketOverviewResponse,
    NewsAnalysisResponse,
    Page,
    SummaryResponse,
)
from marketmind.signals import build_market_overview

router = APIRouter(prefix="/analysis", tags=["News analysis"], dependencies=[Depends(current_session)])
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=2048)]


def _validate_range(start_at: datetime | None, end_at: datetime | None) -> None:
    for boundary in (start_at, end_at):
        if boundary is not None and boundary.tzinfo is None:
            raise ApiError(422, "timezone_required", "Timestamp must include a timezone")
    if start_at and end_at and start_at >= end_at:
        raise ApiError(422, "invalid_range", "Start must precede end")


def _summary_row(summary: NewsSummary, cluster: NewsCluster) -> SummaryResponse:
    return SummaryResponse(
        id=summary.id,
        cluster_id=cluster.id,
        category=cluster.category,
        title=summary.title,
        summary=summary.summary,
        key_points=[str(item) for item in summary.key_points],
        news_ids=[UUID(str(item)) for item in cluster.news_ids],
        provider=summary.provider,
        model=summary.model,
        status=summary.status,
        error_code=summary.error_code,
        first_published_at=cluster.first_published_at,
        last_published_at=cluster.last_published_at,
        updated_at=summary.updated_at,
    )


@router.get("/overview", response_model=MarketOverviewResponse)
def overview(db: Db, hours: Annotated[int, Query(ge=6, le=168)] = 24) -> MarketOverviewResponse:
    data_as_of = db.scalar(
        select(func.max(NewsRevision.published_at)).join(
            NewsAnalysis,
            NewsAnalysis.revision_id == NewsRevision.id,
        )
    )
    if data_as_of is None:
        raise ApiError(404, "analysis_not_found", "No analyzed news is available")
    window_start = data_as_of - timedelta(hours=hours)
    rows = db.execute(
        select(
            NewsAnalysis.category,
            NewsAnalysis.sentiment,
            NewsAnalysis.importance,
            NewsRevision.published_at,
            Source.code,
        )
        .join(NewsRevision, NewsRevision.id == NewsAnalysis.revision_id)
        .join(NewsItem, NewsItem.id == NewsAnalysis.news_id)
        .join(Source, Source.id == NewsItem.source_id)
        .where(NewsRevision.published_at >= window_start)
        .where(NewsRevision.published_at <= data_as_of)
    ).all()
    generated_at = datetime.now(UTC)
    result = build_market_overview(
        [
            (category, sentiment, importance, published_at, source)
            for category, sentiment, importance, published_at, source in rows
        ],
        data_as_of=data_as_of,
        generated_at=generated_at,
        hours=hours,
    )
    return MarketOverviewResponse.model_validate(result)


@router.post("/process", response_model=AnalysisRunResponse)
def process(payload: AnalysisRunInput, request: Request, db: Db) -> AnalysisRunResponse:
    _validate_range(payload.start_at, payload.end_at)
    result = process_news(
        db,
        request.app.state.settings,
        start_at=payload.start_at,
        end_at=payload.end_at,
        limit=payload.limit,
    )
    return AnalysisRunResponse.model_validate(result)


@router.get("/news/{news_id}", response_model=NewsAnalysisResponse)
def news_analysis(news_id: UUID, db: Db) -> NewsAnalysisResponse:
    result = db.scalar(select(NewsAnalysis).where(NewsAnalysis.news_id == news_id))
    if result is None:
        raise ApiError(404, "analysis_not_found", "News item has not been analyzed")
    return NewsAnalysisResponse.model_validate(result, from_attributes=True)


@router.get("/summaries", response_model=Page[SummaryResponse])
def summaries(db: Db, limit: Limit = 50, cursor: Cursor = None, category: str | None = None) -> Page[SummaryResponse]:
    scope = f"analysis-summaries:{category or ''}"
    query = select(NewsSummary, NewsCluster).join(NewsCluster, NewsCluster.id == NewsSummary.cluster_id)
    if category:
        query = query.where(NewsCluster.category == category)
    if cursor:
        timestamp, identifier = decode_cursor(cursor, scope)
        query = query.where(tuple_(NewsSummary.updated_at, NewsSummary.id) < (timestamp, identifier))
    rows = db.execute(query.order_by(NewsSummary.updated_at.desc(), NewsSummary.id.desc()).limit(limit + 1)).all()
    next_cursor = None
    if len(rows) > limit:
        summary, _ = rows[limit - 1]
        next_cursor = encode_cursor(scope, summary.updated_at.astimezone(UTC), summary.id)
    return Page(items=[_summary_row(summary, cluster) for summary, cluster in rows[:limit]], next_cursor=next_cursor)


@router.get("/summaries/{summary_id}", response_model=SummaryResponse)
def summary_detail(summary_id: UUID, db: Db) -> SummaryResponse:
    row = db.execute(
        select(NewsSummary, NewsCluster)
        .join(NewsCluster, NewsCluster.id == NewsSummary.cluster_id)
        .where(NewsSummary.id == summary_id)
    ).first()
    if row is None:
        raise ApiError(404, "summary_not_found", "Summary does not exist")
    return _summary_row(*row)
