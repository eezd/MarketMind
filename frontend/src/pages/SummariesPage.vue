<script setup lang="ts">
import type { Page, SummaryArticle } from '../types'
import { onMounted, ref } from 'vue'
import { formatDate, statusType } from '../format'
import { usePageRequests } from '../operations'

const categories = ['宏观', '政策', '公司', '行业', '市场', '综合']
const page = usePageRequests()
const { error } = page
const articles = ref<SummaryArticle[]>([])
const category = ref('')
const nextCursor = ref<string | null>(null)
const loading = ref(false)
const loaded = ref(false)
let activeCategory = ''

function summaryStatus(article: SummaryArticle) {
  if (article.status === 'succeeded')
    return article.provider === 'ai' ? 'AI 摘要' : '摘要完成'
  if (article.status === 'fallback')
    return '规则摘要'
  return '生成失败'
}

async function load(more = false) {
  if (loading.value)
    return
  loading.value = true
  error.value = ''
  if (!more) {
    activeCategory = category.value
    articles.value = []
    nextCursor.value = null
    loaded.value = false
  }
  const query = new URLSearchParams({ limit: '24' })
  if (activeCategory)
    query.set('category', activeCategory)
  if (more && nextCursor.value)
    query.set('cursor', nextCursor.value)
  try {
    const result = await page.get<Page<SummaryArticle>>(`/analysis/summaries?${query}`)
    articles.value = more ? [...articles.value, ...result.items] : result.items
    nextCursor.value = result.next_cursor
    loaded.value = true
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    loading.value = false
  }
}

onMounted(() => load())
</script>

<template>
  <section :aria-busy="loading">
    <header class="page-heading insight-heading">
      <div>
        <p class="eyebrow">
          RESEARCH BRIEFS
        </p>
        <h1>摘要文章</h1>
        <p class="muted">
          查看聚合后的财经资讯摘要、关键要点和每一条原始来源。
        </p>
      </div>
      <form class="summary-filter" @submit.prevent="load()">
        <label for="summary-category">分类</label>
        <ElSelect id="summary-category" v-model="category" aria-label="摘要分类" :disabled="loading">
          <ElOption label="全部分类" value="" />
          <ElOption v-for="item in categories" :key="item" :label="item" :value="item" />
        </ElSelect>
        <ElButton native-type="submit" type="primary" :loading="loading">
          筛选
        </ElButton>
      </form>
    </header>

    <ElAlert v-if="error" class="mb-5" type="error" :closable="false" :title="error" show-icon role="alert" />
    <p v-if="loading && !loaded" class="loading-line">
      正在读取摘要文章…
    </p>

    <template v-if="loaded">
      <div v-if="articles.length" class="summary-grid">
        <RouterLink v-for="article in articles" :key="article.id" :to="`/summaries/${article.id}`" class="summary-card">
          <div class="summary-card-meta">
            <span>{{ article.category }}</span>
            <ElTag :type="statusType(article.status)" effect="plain" size="small">
              {{ summaryStatus(article) }}
            </ElTag>
          </div>
          <h2>{{ article.title }}</h2>
          <p>{{ article.summary }}</p>
          <ul v-if="article.key_points.length">
            <li v-for="point in article.key_points.slice(0, 3)" :key="point">
              {{ point }}
            </li>
          </ul>
          <footer>
            <span>{{ article.news_ids.length }} 条来源</span>
            <time>{{ formatDate(article.last_published_at || article.updated_at) }}</time>
          </footer>
        </RouterLink>
      </div>
      <div v-else class="archive-empty data-panel">
        <span class="empty-mark" aria-hidden="true">R / 0</span>
        <h2>暂无摘要文章</h2>
        <p>当前分类没有已生成摘要，请先确认第二阶段处理结果。</p>
      </div>
      <footer class="summary-list-footer">
        <span>已加载 {{ articles.length }} 篇</span>
        <ElButton v-if="nextCursor" :loading="loading" @click="load(true)">
          加载更多
        </ElButton>
        <span v-else-if="articles.length">已到末尾</span>
      </footer>
    </template>
  </section>
</template>
