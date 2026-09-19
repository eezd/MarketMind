from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from marketmind.auth import Db, current_session
from marketmind.errors import ApiError
from marketmind.market_indicators import technical_snapshot
from marketmind.models import MarketBar, MarketInstrument, MarketQuote, NewsAnalysis, NewsRevision
from marketmind.schemas import (
    CombinedMarketOutlook,
    MarketBarResponse,
    MarketDashboardResponse,
    MarketInstrumentResponse,
    TechnicalSignalResponse,
)

router = APIRouter(prefix="/markets", tags=["Market data"], dependencies=[Depends(current_session)])
Interval = Literal["5m", "1h", "1d"]


def _news_score(db: Db) -> float:
    data_as_of = db.scalar(
        select(func.max(NewsRevision.published_at)).join(NewsAnalysis, NewsAnalysis.revision_id == NewsRevision.id)
    )
    if data_as_of is None:
        return 0.0
    rows = db.execute(
        select(NewsAnalysis.sentiment, NewsAnalysis.importance)
        .join(NewsRevision, NewsRevision.id == NewsAnalysis.revision_id)
        .where(NewsRevision.published_at >= data_as_of - timedelta(hours=24))
        .where(NewsRevision.published_at <= data_as_of)
    ).all()
    total = sum(max(1, importance) for _, importance in rows)
    if not total:
        return 0.0
    positive = sum(max(1, importance) for sentiment, importance in rows if sentiment == "positive")
    negative = sum(max(1, importance) for sentiment, importance in rows if sentiment == "negative")
    return round((positive - negative) / total, 4)


def _combined_outlook(technical_score: float, news_score: float, ready: bool) -> CombinedMarketOutlook:
    if not ready:
        return CombinedMarketOutlook(
            horizon_hours=24,
            direction="insufficient_data",
            score=None,
            signal_strength=0,
            price_weight=0.7,
            news_weight=0.3,
            rationale="价格历史不足 26 根 1 小时 K 线，暂不生成综合方向。",
        )
    score = round(max(-1.0, min(1.0, technical_score * 0.7 + news_score * 0.3)), 4)
    direction = "bullish" if score >= 0.15 else "bearish" if score <= -0.15 else "neutral"
    return CombinedMarketOutlook(
        horizon_hours=24,
        direction=direction,
        score=score,
        signal_strength=round(min(100, abs(score) * 100)),
        price_weight=0.7,
        news_weight=0.3,
        rationale=f"价格技术信号 {technical_score:+.2f} 占 70%，全市场新闻信号 {news_score:+.2f} 占 30%。",
    )


@router.get("/overview", response_model=MarketDashboardResponse)
def market_overview(request: Request, db: Db) -> MarketDashboardResponse:
    now = datetime.now(UTC)
    news_score = _news_score(db)
    rows = db.execute(
        select(MarketInstrument, MarketQuote)
        .outerjoin(MarketQuote, MarketQuote.instrument_id == MarketInstrument.id)
        .where(MarketInstrument.enabled.is_(True))
        .order_by(MarketInstrument.asset_class, MarketInstrument.symbol)
    ).all()
    items: list[MarketInstrumentResponse] = []
    quote_times = []
    stale_after = timedelta(seconds=request.app.state.settings.binance_sync_interval_seconds * 2)
    for instrument, quote in rows:
        bars = list(
            reversed(
                db.scalars(
                    select(MarketBar)
                    .where(
                        MarketBar.instrument_id == instrument.id,
                        MarketBar.interval == "1h",
                        MarketBar.complete.is_(True),
                    )
                    .order_by(MarketBar.open_time.desc())
                    .limit(120)
                ).all()
            )
        )
        technical = technical_snapshot(bars)
        ready = len(bars) >= 26
        if quote is not None:
            quote_times.append(quote.event_at)
        items.append(
            MarketInstrumentResponse(
                id=instrument.id,
                symbol=instrument.symbol,
                asset_class=instrument.asset_class,
                base_asset=instrument.base_asset,
                quote_asset=instrument.quote_asset,
                price=float(quote.last) if quote else None,
                bid=float(quote.bid) if quote and quote.bid is not None else None,
                ask=float(quote.ask) if quote and quote.ask is not None else None,
                quote_at=quote.event_at if quote else None,
                stale=quote is None or now - quote.event_at > stale_after,
                history_start_at=instrument.history_start_at,
                history_ready=ready,
                technical=TechnicalSignalResponse.model_validate(asdict(technical)),
                outlook=_combined_outlook(technical.score, news_score, ready),
            )
        )
    data_as_of = max(quote_times) if quote_times else None
    return MarketDashboardResponse(
        generated_at=now,
        data_as_of=data_as_of,
        stale=data_as_of is None or now - data_as_of > stale_after,
        news_score=news_score,
        instruments=items,
        disclaimer="价格来自币安公开行情；综合方向是技术指标与新闻信号的模型结果，不构成投资建议。",
    )


@router.get("/{symbol}/bars", response_model=list[MarketBarResponse])
def market_bars(
    symbol: str,
    db: Db,
    interval: Interval = "1h",
    limit: Annotated[int, Query(ge=20, le=500)] = 200,
) -> list[MarketBarResponse]:
    normalized = symbol.upper()
    instrument = db.scalar(
        select(MarketInstrument).where(
            MarketInstrument.provider == "binance",
            MarketInstrument.symbol == normalized,
            MarketInstrument.enabled.is_(True),
        )
    )
    if instrument is None:
        raise ApiError(404, "market_instrument_not_found", "Market instrument does not exist")
    rows = list(
        reversed(
            db.scalars(
                select(MarketBar)
                .where(MarketBar.instrument_id == instrument.id, MarketBar.interval == interval)
                .order_by(MarketBar.open_time.desc())
                .limit(limit)
            ).all()
        )
    )
    return [MarketBarResponse.model_validate(row, from_attributes=True) for row in rows]
