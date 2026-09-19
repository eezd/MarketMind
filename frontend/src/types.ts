export interface Identity {
  id: string
  username: string
  csrf_token: string
}

export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export interface Source {
  id: string
  code: string
  name: string
  enabled: boolean
  config_version: number
  permission_config: SourceConfig
  actual_egress?: string | null
  egress_reason?: string | null
  direct_fallback?: boolean
}

export interface Collector {
  id: string
  source_id: string
  code: string
  name: string
  entry_url: string
  enabled: boolean
  interval_seconds: number
  status: string
  config_version: number
  config: Record<string, unknown>
  last_success_at?: string | null
  next_run_at?: string | null
}

export type BodyStatus = 'pending' | 'complete' | 'summary_only' | 'paywalled' | 'external_link' | 'unavailable'

export interface NewsItem {
  id: string
  source_id: string
  source_item_id: string
  canonical_url: string
  title: string
  body_status: BodyStatus
  published_at: string | null
  first_seen_at: string
}

export interface NewsDetail extends NewsItem {
  body_text: string | null
  summary: string | null
  current_revision_id: string | null
}

export interface Revision {
  id: string
  title: string
  body_text: string | null
  body_status: BodyStatus
  published_at: string | null
  observed_at: string
}

export interface CrawlRun {
  id: string
  collector_id: string
  status: string
  trigger_type: string
  started_at: string | null
  finished_at: string | null
  run_type: string
  source_id: string
  range_start_at: string | null
  range_end_at: string | null
  heartbeat_at: string | null
  statistics: Record<string, unknown>
  checkpoint: Record<string, unknown>
  coverage_gaps: unknown[]
}

export interface SourceConfig {
  proxy_ids?: string[]
  failover_enabled?: boolean
  on_proxy_exhausted?: 'direct' | 'pause'
  request_interval_seconds?: number
  [key: string]: unknown
}

export interface ProxyEndpoint {
  id: string
  name: string
  scheme: 'http'
  host: string
  port: number
  enabled: boolean
  has_credentials: boolean
  expires_at: string | null
  status?: string
  health_status?: string
  last_success_at?: string | null
  consecutive_failures?: number
  source_health?: { source_id: string, status: string, consecutive_failures: number, last_success_at: string | null, last_checked_at: string | null, cooldown_until: string | null, error_code: string | null }[]
}

export interface Command {
  id: string
  status: string
  result?: {
    message?: string
    error_code?: string
    items?: { source_id: string, status: string, error_code?: string }[]
    [key: string]: unknown
  } | null
}

export interface CrawlTask {
  id: string
  status: string
  task_type?: string
  url?: string
  attempt?: number
  generation: number
  error_details?: Record<string, unknown> | null
  error_code?: string | null
  lease_expires_at?: string | null
}

export interface RunLog {
  id: string
  created_at?: string
  action: string
  outcome: string
  details: Record<string, unknown>
}

export interface LoginAttempt {
  id: string
  status: string
  expires_at: string
  qr_available: boolean
  error_code?: string | null
  message?: string
  instructions?: string
  poll_interval_seconds?: number
}

export interface SourceSession {
  source_id: string
  status: string
  domains: { domain: string, status: string }[]
  updated_at?: string | null
}

export interface AlertEvent {
  id: string
  source_id: string | null
  kind: string
  status: string
  message: string
  created_at: string
  delivery_status: string | null
  delivery_id: string | null
}

export interface TelegramSettings {
  enabled: boolean
  chat_id: string | null
  has_token: boolean
}

export interface NotificationDelivery {
  id: string
  command_id: string | null
  status: string
  attempts: number
  last_error: string | null
  next_attempt_at: string | null
  created_at: string
  completed_at: string | null
  attempt_history: { attempt?: number, at: string, status: string, error?: string, completed_at?: string }[]
}

export type SignalDirection = 'bullish' | 'bearish' | 'neutral'

export interface SummaryArticle {
  id: string
  cluster_id: string
  category: string
  title: string
  summary: string
  key_points: string[]
  news_ids: string[]
  provider: string
  model: string | null
  status: 'succeeded' | 'fallback' | 'failed'
  error_code: string | null
  first_published_at: string | null
  last_published_at: string | null
  updated_at: string
}

export interface CategorySignal {
  category: string
  count: number
  positive: number
  negative: number
  neutral: number
  average_importance: number
  score: number
  direction: SignalDirection
}

export interface TimelineSignal {
  start_at: string
  end_at: string
  count: number
  score: number
  direction: SignalDirection
}

export interface SignalOutlook {
  horizon_hours: number
  direction: SignalDirection
  score: number
  signal_strength: number
  rationale: string
  methodology: string
}

export interface MarketOverview {
  generated_at: string
  data_as_of: string
  window_start: string
  window_end: string
  hours: number
  stale: boolean
  total_news: number
  source_count: number
  sentiments: { positive: number, negative: number, neutral: number }
  overall_score: number
  direction: SignalDirection
  signal_strength: number
  categories: CategorySignal[]
  timeline: TimelineSignal[]
  outlook: SignalOutlook
  disclaimer: string
}

export interface TechnicalSignal {
  return_1: number | null
  ema_12: number | null
  ema_26: number | null
  rsi_14: number | null
  macd: number | null
  macd_signal: number | null
  atr_14: number | null
  volatility_20: number | null
  score: number
  direction: SignalDirection
  signal_strength: number
}

export interface CombinedMarketOutlook {
  horizon_hours: number
  direction: SignalDirection | 'insufficient_data'
  score: number | null
  signal_strength: number
  price_weight: number
  news_weight: number
  rationale: string
}

export interface MarketInstrument {
  id: string
  symbol: string
  asset_class: 'crypto' | 'equity' | 'etf'
  base_asset: string
  quote_asset: string
  price: number | null
  bid: number | null
  ask: number | null
  quote_at: string | null
  stale: boolean
  history_start_at: string | null
  history_ready: boolean
  technical: TechnicalSignal
  outlook: CombinedMarketOutlook
}

export interface MarketDashboard {
  generated_at: string
  data_as_of: string | null
  stale: boolean
  news_score: number
  instruments: MarketInstrument[]
  disclaimer: string
}

export interface MarketBar {
  open_time: string
  close_time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  trades: number | null
  complete: boolean
}
