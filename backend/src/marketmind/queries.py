import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, tuple_

from marketmind.auth import Db, current_session
from marketmind.errors import ApiError
from marketmind.models import Collector, CrawlRun, NewsItem, NewsRevision, Source
from marketmind.pagination import decode_cursor, encode_cursor
from marketmind.proxy_models import SourceEgressState
from marketmind.schemas import (
    BodyStatus,
    CollectorResponse,
    NewsDetailResponse,
    NewsResponse,
    Page,
    RevisionResponse,
    RunResponse,
    SourceResponse,
)

router = APIRouter(tags=["Read-only data"], dependencies=[Depends(current_session)])
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=2048)]


def news_row(item: NewsItem, revision: NewsRevision) -> NewsResponse:
    return NewsResponse(
        id=item.id,
        source_id=item.source_id,
        source_item_id=item.source_item_id,
        canonical_url=item.canonical_url,
        title=revision.title,
        body_status=revision.body_status,
        published_at=revision.published_at,
        first_seen_at=item.first_seen_at,
    )


def source_response(db, item):
    result = SourceResponse.model_validate(item)
    state = db.get(SourceEgressState, item.id)
    if state is not None:
        result.actual_egress = state.egress_id
        result.egress_reason = state.reason
        result.direct_fallback = state.fallback_direct
    return result


@router.get("/sources", response_model=Page[SourceResponse])
def sources(db: Db) -> Page[SourceResponse]:
    items = db.scalars(select(Source).order_by(Source.code)).all()
    return Page(items=[source_response(db, item) for item in items])


@router.get("/collectors", response_model=Page[CollectorResponse])
def collectors(db: Db) -> Page[CollectorResponse]:
    # Correlated subquery reads the last actual run; absence is never reported as success.
    latest_status = (
        select(CrawlRun.status)
        .where(CrawlRun.collector_id == Collector.id)
        .order_by(CrawlRun.created_at.desc(), CrawlRun.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    rows = db.execute(select(Collector, latest_status).order_by(Collector.code)).all()
    return Page(
        items=[
            CollectorResponse(
                id=item.id,
                source_id=item.source_id,
                code=item.code,
                name=item.name,
                entry_url=item.entry_url,
                enabled=item.enabled,
                interval_seconds=item.interval_seconds,
                status=status or ("not_run" if item.enabled else "disabled"),
                config_version=item.config_version,
                config=item.config,
                last_success_at=item.last_success_at,
                next_run_at=item.next_run_at,
            )
            for item, status in rows
        ]
    )


@router.get("/news", response_model=Page[NewsResponse])
def news(
    db: Db,
    limit: Limit = 50,
    cursor: Cursor = None,
    source_id: UUID | None = None,
    body_status: BodyStatus | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> Page[NewsResponse]:
    for boundary in (start_at, end_at):
        if boundary is not None and boundary.tzinfo is None:
            raise ApiError(422, "timezone_required", "Timestamp must include a timezone")
    if start_at and end_at and start_at >= end_at:
        raise ApiError(422, "invalid_range", "Start must precede end")
    scope = json.dumps(
        [
            "news",
            str(source_id) if source_id else None,
            body_status,
            start_at.astimezone(UTC).isoformat() if start_at else None,
            end_at.astimezone(UTC).isoformat() if end_at else None,
        ]
    )
    query = select(NewsItem, NewsRevision).join(
        NewsRevision,
        NewsRevision.id == NewsItem.current_revision_id,
    )
    if source_id is not None:
        query = query.where(NewsItem.source_id == source_id)
    if body_status is not None:
        query = query.where(NewsRevision.body_status == body_status)
    if start_at is not None:
        query = query.where(NewsRevision.published_at >= start_at)
    if end_at is not None:
        query = query.where(NewsRevision.published_at < end_at)
    if cursor is not None:
        timestamp, identifier = decode_cursor(cursor, scope)
        query = query.where(tuple_(NewsItem.first_seen_at, NewsItem.id) < (timestamp, identifier))
    rows = db.execute(
        query.order_by(
            NewsItem.first_seen_at.desc(),
            NewsItem.id.desc(),
        ).limit(limit + 1)
    ).all()
    next_cursor = None
    if len(rows) > limit:
        item, _ = rows[limit - 1]
        next_cursor = encode_cursor(scope, item.first_seen_at, item.id)
    return Page(items=[news_row(item, rev) for item, rev in rows[:limit]], next_cursor=next_cursor)


@router.get("/news/{news_id}", response_model=NewsDetailResponse)
def news_detail(news_id: UUID, db: Db) -> NewsDetailResponse:
    row = db.execute(
        select(NewsItem, NewsRevision)
        .join(NewsRevision, NewsRevision.id == NewsItem.current_revision_id)
        .where(NewsItem.id == news_id)
    ).first()
    if row is None:
        raise ApiError(404, "news_not_found", "News item does not exist")
    item, revision = row
    return NewsDetailResponse(
        **news_row(item, revision).model_dump(),
        body_text=revision.body_text,
        summary=revision.summary,
        current_revision_id=revision.id,
    )


@router.get("/news/{news_id}/revisions", response_model=Page[RevisionResponse])
def revisions(news_id: UUID, db: Db, limit: Limit = 50, cursor: Cursor = None) -> Page[RevisionResponse]:
    if db.get(NewsItem, news_id) is None:
        raise ApiError(404, "news_not_found", "News item does not exist")
    scope = f"revisions:{news_id}"
    query = select(NewsRevision).where(NewsRevision.news_id == news_id)
    if cursor is not None:
        timestamp, identifier = decode_cursor(cursor, scope)
        query = query.where(tuple_(NewsRevision.observed_at, NewsRevision.id) < (timestamp, identifier))
    rows = db.scalars(
        query.order_by(
            NewsRevision.observed_at.desc(),
            NewsRevision.id.desc(),
        ).limit(limit + 1)
    ).all()
    next_cursor = None
    if len(rows) > limit:
        item = rows[limit - 1]
        next_cursor = encode_cursor(scope, item.observed_at, item.id)
    return Page(
        items=[RevisionResponse.model_validate(item) for item in rows[:limit]],
        next_cursor=next_cursor,
    )


@router.get("/runs", response_model=Page[RunResponse])
def runs(db: Db, limit: Limit = 50, cursor: Cursor = None) -> Page[RunResponse]:
    query = select(CrawlRun)
    if cursor is not None:
        timestamp, identifier = decode_cursor(cursor, "runs")
        query = query.where(tuple_(CrawlRun.created_at, CrawlRun.id) < (timestamp, identifier))
    rows = db.scalars(query.order_by(CrawlRun.created_at.desc(), CrawlRun.id.desc()).limit(limit + 1)).all()
    next_cursor = None
    if len(rows) > limit:
        item = rows[limit - 1]
        next_cursor = encode_cursor("runs", item.created_at, item.id)
    return Page(items=[RunResponse.model_validate(item) for item in rows[:limit]], next_cursor=next_cursor)
