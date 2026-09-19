from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

BodyStatus = Literal["pending", "complete", "summary_only", "paywalled", "external_link", "unavailable"]


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str
    details: list[dict[str, Any]] | dict[str, Any] | None = None


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: str


class IdentityResponse(BaseModel):
    id: UUID
    username: str
    csrf_token: str


class SourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    code: str
    name: str
    enabled: bool
    config_version: int
    permission_config: dict[str, Any]
    actual_egress: str | None = None
    egress_reason: str | None = None
    direct_fallback: bool = False


class CollectorResponse(BaseModel):
    id: UUID
    source_id: UUID
    code: str
    name: str
    entry_url: str
    enabled: bool
    interval_seconds: int
    status: str
    config_version: int
    config: dict[str, Any]
    last_success_at: datetime | None
    next_run_at: datetime | None


class NewsResponse(BaseModel):
    id: UUID
    source_id: UUID
    source_item_id: str | None
    canonical_url: str
    title: str
    body_status: BodyStatus
    published_at: datetime | None
    first_seen_at: datetime


class NewsDetailResponse(NewsResponse):
    body_text: str | None
    summary: str | None
    current_revision_id: UUID


class RevisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str
    body_text: str | None
    body_status: BodyStatus
    published_at: datetime | None
    observed_at: datetime


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    collector_id: UUID
    status: str
    trigger_type: str
    started_at: datetime | None
    finished_at: datetime | None
    source_id: UUID
    run_type: str
    created_at: datetime
    heartbeat_at: datetime | None
    range_start_at: datetime | None
    range_end_at: datetime | None
    checkpoint: dict[str, Any]
    statistics: dict[str, Any]
    coverage_gaps: list[Any]


class AnalysisRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_at: datetime | None = None
    end_at: datetime | None = None
    limit: int = Field(default=500, ge=1, le=5000)


class AnalysisRunResponse(BaseModel):
    processed: int
    clusters: int
    created_clusters: int
    summaries: int
    provider: Literal["ai", "fallback"]


class NewsAnalysisResponse(BaseModel):
    id: UUID
    news_id: UUID
    revision_id: UUID
    category: str
    topics: list[str]
    sentiment: Literal["positive", "negative", "neutral"]
    importance: int
    classifier_version: str
    processed_at: datetime


class SummaryResponse(BaseModel):
    id: UUID
    cluster_id: UUID
    category: str
    title: str
    summary: str
    key_points: list[str]
    news_ids: list[UUID]
    provider: str
    model: str | None
    status: Literal["succeeded", "fallback", "failed"]
    error_code: str | None
    first_published_at: datetime | None
    last_published_at: datetime | None
    updated_at: datetime


class SentimentCounts(BaseModel):
    positive: int
    negative: int
    neutral: int


class CategorySignal(BaseModel):
    category: str
    count: int
    positive: int
    negative: int
    neutral: int
    average_importance: float
    score: float
    direction: Literal["bullish", "bearish", "neutral"]


class TimelineSignal(BaseModel):
    start_at: datetime
    end_at: datetime
    count: int
    score: float
    direction: Literal["bullish", "bearish", "neutral"]


class SignalOutlook(BaseModel):
    horizon_hours: int
    direction: Literal["bullish", "bearish", "neutral"]
    score: float
    signal_strength: int
    rationale: str
    methodology: str


class MarketOverviewResponse(BaseModel):
    generated_at: datetime
    data_as_of: datetime
    window_start: datetime
    window_end: datetime
    hours: int
    stale: bool
    total_news: int
    source_count: int
    sentiments: SentimentCounts
    overall_score: float
    direction: Literal["bullish", "bearish", "neutral"]
    signal_strength: int
    categories: list[CategorySignal]
    timeline: list[TimelineSignal]
    disclaimer: str
    outlook: SignalOutlook


class TechnicalSignalResponse(BaseModel):
    return_1: float | None
    ema_12: float | None
    ema_26: float | None
    rsi_14: float | None
    macd: float | None
    macd_signal: float | None
    atr_14: float | None
    volatility_20: float | None
    score: float
    direction: Literal["bullish", "bearish", "neutral"]
    signal_strength: int


class CombinedMarketOutlook(BaseModel):
    horizon_hours: int
    direction: Literal["bullish", "bearish", "neutral", "insufficient_data"]
    score: float | None
    signal_strength: int
    price_weight: float
    news_weight: float
    rationale: str


class MarketInstrumentResponse(BaseModel):
    id: UUID
    symbol: str
    asset_class: Literal["crypto", "equity", "etf"]
    base_asset: str
    quote_asset: str
    price: float | None
    bid: float | None
    ask: float | None
    quote_at: datetime | None
    stale: bool
    history_start_at: datetime | None
    history_ready: bool
    technical: TechnicalSignalResponse
    outlook: CombinedMarketOutlook


class MarketDashboardResponse(BaseModel):
    generated_at: datetime
    data_as_of: datetime | None
    stale: bool
    news_score: float
    instruments: list[MarketInstrumentResponse]
    disclaimer: str


class MarketBarResponse(BaseModel):
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int | None
    complete: bool


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
