<script setup lang="ts">
import type { MarketBar, MarketDashboard, MarketInstrument, SignalDirection } from '../types'
import { computed, onMounted, ref } from 'vue'
import { formatDate } from '../format'
import { usePageRequests } from '../operations'

const page = usePageRequests()
const { error } = page
const dashboard = ref<MarketDashboard | null>(null)
const selectedSymbol = ref('')
const interval = ref<'5m' | '1h' | '1d'>('1h')
const bars = ref<MarketBar[]>([])
const loading = ref(false)
const chartLoading = ref(false)

const directionLabels: Record<SignalDirection | 'insufficient_data', string> = {
  bullish: '偏多',
  bearish: '偏空',
  neutral: '中性',
  insufficient_data: '数据积累中',
}

const selected = computed(() => dashboard.value?.instruments.find(item => item.symbol === selectedSymbol.value) || null)
const completedBars = computed(() => bars.value.filter(item => item.complete))
const chartPoints = computed(() => {
  const values = completedBars.value.map(item => item.close)
  if (values.length < 2)
    return ''
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  return values.map((value, index) => {
    const x = 20 + index / (values.length - 1) * 760
    const y = 235 - (value - min) / range * 205
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
})
const chartRange = computed(() => {
  const values = completedBars.value.map(item => item.close)
  return values.length ? { min: Math.min(...values), max: Math.max(...values) } : null
})

function directionLabel(direction: SignalDirection | 'insufficient_data') {
  return directionLabels[direction]
}

function price(value: number | null, quoteAsset = '') {
  if (value === null)
    return '等待行情'
  const digits = value >= 100 ? 2 : value >= 1 ? 4 : 6
  return `${new Intl.NumberFormat('en-US', { maximumFractionDigits: digits, minimumFractionDigits: 2 }).format(value)} ${quoteAsset}`
}

function percent(value: number | null) {
  return value === null ? '—' : `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

function metric(value: number | null, digits = 2) {
  return value === null ? '积累中' : value.toFixed(digits)
}

async function loadBars() {
  if (!selectedSymbol.value)
    return
  chartLoading.value = true
  try {
    bars.value = await page.get<MarketBar[]>(`/markets/${selectedSymbol.value}/bars?interval=${interval.value}&limit=200`)
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    chartLoading.value = false
  }
}

async function selectInstrument(item: MarketInstrument) {
  selectedSymbol.value = item.symbol
  await loadBars()
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    dashboard.value = await page.get<MarketDashboard>('/markets/overview')
    if (!selectedSymbol.value && dashboard.value.instruments.length)
      selectedSymbol.value = dashboard.value.instruments[0]!.symbol
    await loadBars()
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section :aria-busy="loading">
    <header class="page-heading insight-heading">
      <div>
        <p class="eyebrow">
          LIVE MARKET TAPE
        </p>
        <h1>数字资产与美股行情</h1>
        <p class="muted">
          币安公开行情经固定 SOCKS5 出口采集；价格信号与新闻信号分别计算、透明融合。
        </p>
      </div>
      <div class="market-asof">
        <span>行情截至</span><strong>{{ formatDate(dashboard?.data_as_of) }}</strong>
      </div>
    </header>

    <ElAlert v-if="error" class="mb-5" type="error" :closable="false" :title="error" show-icon role="alert" />
    <ElAlert v-if="dashboard?.stale" class="mb-5" type="warning" :closable="false" title="行情尚未同步或已超过两个同步周期。" show-icon />
    <p v-if="loading && !dashboard" class="loading-line">
      正在读取真实行情…
    </p>

    <template v-if="dashboard">
      <div class="market-tape">
        <button
          v-for="item in dashboard.instruments"
          :key="item.id"
          type="button"
          class="market-ticker"
          :class="[{ active: item.symbol === selectedSymbol }, `is-${item.technical.direction}`]"
          @click="selectInstrument(item)"
        >
          <span>{{ item.symbol }}<small>{{ item.asset_class === 'crypto' ? 'CRYPTO' : item.asset_class.toUpperCase() }}</small></span>
          <strong>{{ price(item.price, item.quote_asset) }}</strong>
          <em :class="item.technical.return_1 !== null && item.technical.return_1 < 0 ? 'negative-text' : 'positive-text'">
            {{ percent(item.technical.return_1) }}
          </em>
          <i v-if="item.stale">延迟</i>
        </button>
      </div>

      <section v-if="selected" class="market-terminal">
        <header class="market-terminal-head">
          <div>
            <p class="eyebrow">
              {{ selected.asset_class.toUpperCase() }} / {{ selected.quote_asset }}
            </p>
            <h2>{{ selected.symbol }}</h2>
            <p>{{ price(selected.price, selected.quote_asset) }}</p>
          </div>
          <div class="interval-tabs" aria-label="K线周期">
            <button v-for="value in ['5m', '1h', '1d'] as const" :key="value" type="button" :class="{ active: interval === value }" @click="interval = value; loadBars()">
              {{ value }}
            </button>
          </div>
        </header>

        <div class="market-terminal-body">
          <div class="price-chart" :aria-busy="chartLoading">
            <div v-if="chartRange" class="chart-scale">
              <span>{{ price(chartRange.max) }}</span><span>{{ price(chartRange.min) }}</span>
            </div>
            <svg v-if="chartPoints" viewBox="0 0 800 260" preserveAspectRatio="none" role="img" :aria-label="`${selected.symbol} ${interval} 收盘价走势`">
              <defs><linearGradient id="market-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#168c76" stop-opacity=".25" /><stop offset="1" stop-color="#168c76" stop-opacity="0" /></linearGradient></defs>
              <polyline :points="`20,250 ${chartPoints} 780,250`" fill="url(#market-area)" stroke="none" />
              <polyline :points="chartPoints" fill="none" stroke="#168c76" stroke-width="2" vector-effect="non-scaling-stroke" />
            </svg>
            <div v-else class="chart-empty">
              <strong>历史数据积累中</strong><span>数字货币将自动回补；美股从行情 worker 启用时开始记录。</span>
            </div>
            <footer>
              <span>{{ completedBars.length }} 根 {{ interval }} K线</span>
              <span>覆盖起点 {{ formatDate(selected.history_start_at) }}</span>
            </footer>
          </div>

          <aside class="technical-board">
            <p class="eyebrow">
              TECHNICAL READOUT
            </p>
            <h3>技术指标</h3>
            <dl>
              <div><dt>EMA 12</dt><dd>{{ metric(selected.technical.ema_12) }}</dd></div>
              <div><dt>EMA 26</dt><dd>{{ metric(selected.technical.ema_26) }}</dd></div>
              <div><dt>RSI 14</dt><dd>{{ metric(selected.technical.rsi_14, 1) }}</dd></div>
              <div><dt>MACD</dt><dd>{{ metric(selected.technical.macd, 4) }}</dd></div>
              <div><dt>ATR 14</dt><dd>{{ metric(selected.technical.atr_14, 4) }}</dd></div>
              <div><dt>波动率 20</dt><dd>{{ percent(selected.technical.volatility_20) }}</dd></div>
            </dl>
          </aside>
        </div>

        <section class="combined-outlook" :class="`is-${selected.outlook.direction}`">
          <div><span>24H 综合方向</span><strong>{{ directionLabel(selected.outlook.direction) }}</strong></div>
          <div><span>综合分数</span><strong>{{ selected.outlook.score === null ? '—' : `${selected.outlook.score > 0 ? '+' : ''}${(selected.outlook.score * 100).toFixed(1)}` }}</strong></div>
          <div><span>信号强度</span><strong>{{ selected.outlook.signal_strength }}</strong></div>
          <p>{{ selected.outlook.rationale }}<small>{{ dashboard.disclaimer }}</small></p>
        </section>
      </section>
    </template>
  </section>
</template>
