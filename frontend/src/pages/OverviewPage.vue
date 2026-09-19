<script setup lang="ts">
import type { MarketDashboard, MarketOverview, Page, SignalDirection, SummaryArticle } from '../types'
import { computed, onMounted, ref } from 'vue'
import { formatDate } from '../format'
import { usePageRequests } from '../operations'

const page = usePageRequests()
const { error } = page
const overview = ref<MarketOverview | null>(null)
const latest = ref<SummaryArticle[]>([])
const market = ref<MarketDashboard | null>(null)
const hours = ref(24)
const loading = ref(false)
const loaded = ref(false)

const directionCopy: Record<SignalDirection, { label: string, note: string }> = {
  bullish: { label: '偏多', note: '正向资讯权重占优' },
  bearish: { label: '偏空', note: '负向资讯权重占优' },
  neutral: { label: '中性', note: '多空资讯暂未形成明显差值' },
}

const totalSentiment = computed(() => {
  if (!overview.value)
    return 0
  const { positive, negative, neutral } = overview.value.sentiments
  return positive + negative + neutral
})

function share(value: number) {
  return totalSentiment.value ? Math.round(value / totalSentiment.value * 100) : 0
}

function signed(value: number) {
  return `${value > 0 ? '+' : ''}${(value * 100).toFixed(1)}`
}

function directionLabel(value: SignalDirection) {
  return directionCopy[value].label
}

function timelineHeight(score: number) {
  return `${Math.max(8, Math.round(Math.abs(score) * 72))}px`
}
function marketPrice(value: number | null) {
  if (value === null)
    return '等待行情'
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: value >= 100 ? 2 : 4 }).format(value)
}

async function load() {
  if (loading.value)
    return
  loading.value = true
  error.value = ''
  try {
    const [signal, summaries, marketDashboard] = await Promise.all([
      page.get<MarketOverview>(`/analysis/overview?hours=${hours.value}`),
      page.get<Page<SummaryArticle>>('/analysis/summaries?limit=6'),
      page.get<MarketDashboard>('/markets/overview'),
    ])
    overview.value = signal
    latest.value = summaries.items
    market.value = marketDashboard
    loaded.value = true
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
  <section class="insight-overview" :aria-busy="loading">
    <header class="page-heading insight-heading">
      <div>
        <p class="eyebrow">
          MARKET INTELLIGENCE
        </p>
        <h1>研判总览</h1>
        <p class="muted">
          从已采集财经资讯中提取方向、强度与关键驱动，不将新闻信号冒充实时价格。
        </p>
      </div>
      <label class="window-control">
        <span>观察窗口</span>
        <ElSelect v-model="hours" aria-label="观察窗口" :disabled="loading" @change="load">
          <ElOption label="最近 24 小时" :value="24" />
          <ElOption label="最近 48 小时" :value="48" />
          <ElOption label="最近 72 小时" :value="72" />
          <ElOption label="最近 7 天" :value="168" />
        </ElSelect>
      </label>
    </header>

    <ElAlert v-if="error" class="mb-5" type="error" :closable="false" :title="error" show-icon role="alert" />
    <p v-if="loading && !loaded" class="loading-line">
      正在汇总资讯信号…
    </p>

    <template v-if="overview">
      <ElAlert
        v-if="overview.stale"
        class="signal-stale-alert"
        type="warning"
        :closable="false"
        title="采集数据距离当前时间已超过 2 小时；下方结果不是实时行情。"
        show-icon
      />

      <section class="signal-hero" :class="`is-${overview.direction}`">
        <div class="signal-primary">
          <p class="signal-kicker">
            NEWS-BASED SIGNAL / {{ overview.hours }}H
          </p>
          <div class="signal-verdict">
            <span class="signal-orbit" aria-hidden="true"><i /></span>
            <div>
              <strong>{{ directionCopy[overview.direction].label }}</strong>
              <span>{{ directionCopy[overview.direction].note }}</span>
            </div>
          </div>
          <p class="signal-disclaimer">
            {{ overview.disclaimer }}
          </p>
        </div>
        <div class="signal-stat">
          <span>加权信号</span>
          <strong>{{ signed(overview.overall_score) }}</strong>
          <small>范围 −100 至 +100</small>
        </div>
        <div class="signal-stat">
          <span>信号强度</span>
          <strong>{{ overview.signal_strength }}</strong>
          <small>资讯差值与样本量综合</small>
        </div>
        <div class="signal-stat">
          <span>样本</span>
          <strong>{{ overview.total_news }}</strong>
          <small>{{ overview.source_count }} 个数据来源</small>
        </div>
      </section>

      <div class="signal-meta">
        <span>数据截至 {{ formatDate(overview.data_as_of) }}</span>
        <span>窗口起点 {{ formatDate(overview.window_start) }}</span>
        <span>页面生成 {{ formatDate(overview.generated_at) }}</span>
      </div>

      <div v-if="market" class="overview-market-strip">
        <RouterLink v-for="item in market.instruments" :key="item.id" to="/markets">
          <span>{{ item.symbol }} <small>{{ item.stale ? '延迟' : 'LIVE' }}</small></span>
          <strong>{{ marketPrice(item.price) }}</strong>
          <em :class="item.technical.return_1 !== null && item.technical.return_1 < 0 ? 'negative-text' : 'positive-text'">
            {{ item.technical.return_1 === null ? '—' : `${item.technical.return_1 >= 0 ? '+' : ''}${(item.technical.return_1 * 100).toFixed(2)}%` }}
          </em>
        </RouterLink>
      </div>

      <section class="insight-grid">
        <article class="insight-panel sentiment-panel">
          <header class="insight-panel-heading">
            <div>
              <p class="eyebrow">
                SENTIMENT MIX
              </p><h2>情绪构成</h2>
            </div>
            <span>{{ totalSentiment }} 条</span>
          </header>
          <div class="sentiment-band" aria-label="正面、负面与中性资讯比例">
            <i class="positive" :style="{ width: `${share(overview.sentiments.positive)}%` }" />
            <i class="neutral" :style="{ width: `${share(overview.sentiments.neutral)}%` }" />
            <i class="negative" :style="{ width: `${share(overview.sentiments.negative)}%` }" />
          </div>
          <dl class="sentiment-list">
            <div><dt><i class="dot positive" />正面</dt><dd>{{ overview.sentiments.positive }}<small>{{ share(overview.sentiments.positive) }}%</small></dd></div>
            <div><dt><i class="dot neutral" />中性</dt><dd>{{ overview.sentiments.neutral }}<small>{{ share(overview.sentiments.neutral) }}%</small></dd></div>
            <div><dt><i class="dot negative" />负面</dt><dd>{{ overview.sentiments.negative }}<small>{{ share(overview.sentiments.negative) }}%</small></dd></div>
          </dl>
        </article>

        <article class="insight-panel timeline-panel">
          <header class="insight-panel-heading">
            <div>
              <p class="eyebrow">
                SIGNAL PULSE
              </p><h2>窗口内脉冲</h2>
            </div>
            <span>加权情绪</span>
          </header>
          <div class="signal-chart" role="img" aria-label="观察窗口内六个时间段的资讯信号">
            <div class="chart-zero" />
            <div v-for="point in overview.timeline" :key="point.start_at" class="chart-column">
              <div class="chart-slot">
                <i
                  :class="point.score > 0 ? 'positive' : point.score < 0 ? 'negative' : 'neutral'"
                  :style="{ height: timelineHeight(point.score) }"
                  :title="`${formatDate(point.start_at)}，${point.count} 条，信号 ${signed(point.score)}`"
                />
              </div>
              <span>{{ point.count }}</span>
            </div>
          </div>
          <p class="chart-note">
            柱上/下表示偏多/偏空，数字为该时间段样本量。
          </p>
        </article>
      </section>

      <section class="insight-panel category-panel">
        <header class="insight-panel-heading">
          <div>
            <p class="eyebrow">
              SECTOR READOUT
            </p><h2>分类信号</h2>
          </div>
          <span>按资讯量排序</span>
        </header>
        <div class="category-grid">
          <article v-for="item in overview.categories" :key="item.category" class="category-signal" :class="`is-${item.direction}`">
            <div class="category-topline">
              <span>{{ item.category }}</span><strong>{{ directionLabel(item.direction) }}</strong>
            </div>
            <div class="category-score">
              {{ signed(item.score) }}
            </div>
            <div class="category-meter">
              <i :style="{ width: `${Math.min(100, Math.abs(item.score) * 100)}%` }" />
            </div>
            <p>{{ item.count }} 条资讯 · 重要性均值 {{ item.average_importance.toFixed(1) }}</p>
          </article>
        </div>
      </section>

      <section class="insight-panel outlook-panel" :class="`is-${overview.outlook.direction}`">
        <header class="insight-panel-heading">
          <div>
            <p class="eyebrow">
              FORWARD BIAS
            </p><h2>未来 {{ overview.outlook.horizon_hours }} 小时资讯偏向</h2>
          </div>
          <span>模型推演 · 非价格预测</span>
        </header>
        <div class="outlook-body">
          <div class="outlook-verdict">
            <span>方向</span>
            <strong>{{ directionLabel(overview.outlook.direction) }}</strong>
          </div>
          <div class="outlook-verdict">
            <span>合成信号</span>
            <strong>{{ signed(overview.outlook.score) }}</strong>
          </div>
          <div class="outlook-verdict">
            <span>信号强度</span>
            <strong>{{ overview.outlook.signal_strength }}</strong>
          </div>
          <div class="outlook-explanation">
            <p>{{ overview.outlook.rationale }}</p>
            <small>{{ overview.outlook.methodology }} 仅用于资讯研判，不构成投资建议。</small>
          </div>
        </div>
      </section>

      <section class="insight-panel latest-panel">
        <header class="insight-panel-heading">
          <div>
            <p class="eyebrow">
              LATEST BRIEFS
            </p><h2>最新摘要</h2>
          </div>
          <RouterLink class="text-link" to="/summaries">
            查看全部 →
          </RouterLink>
        </header>
        <div class="brief-list">
          <RouterLink v-for="article in latest" :key="article.id" :to="`/summaries/${article.id}`" class="brief-row">
            <span class="brief-category">{{ article.category }}</span>
            <div><h3>{{ article.title }}</h3><p>{{ article.summary }}</p></div>
            <time>{{ formatDate(article.last_published_at || article.updated_at) }}</time>
          </RouterLink>
        </div>
      </section>
    </template>
  </section>
</template>
