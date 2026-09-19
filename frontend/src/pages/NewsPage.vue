<script setup lang="ts">
import type { BodyStatus, NewsItem, Page, Source } from '../types'
import { onMounted, ref } from 'vue'
import { bodyStatusLabels, formatDate, statusType } from '../format'
import { beijingIso, usePageRequests, validRange } from '../operations'

const page = usePageRequests()
const { error } = page
const sources = ref<Source[]>([])
const news = ref<NewsItem[]>([])
const sourceId = ref('')
const bodyStatus = ref<BodyStatus | ''>('')
const startAt = ref('')
const endAt = ref('')
const nextCursor = ref<string | null>(null)
const loading = ref(false)
const loaded = ref(false)
let activeFilters = new URLSearchParams()

async function load(more = false) {
  if (loading.value)
    return
  if (!more && startAt.value && endAt.value && !validRange(startAt.value, endAt.value)) {
    error.value = '结束时间必须晚于开始时间（北京时间 UTC+8）。'
    return
  }
  loading.value = true
  error.value = ''
  if (!more) {
    activeFilters = new URLSearchParams()
    if (sourceId.value)
      activeFilters.set('source_id', sourceId.value)
    if (bodyStatus.value)
      activeFilters.set('body_status', bodyStatus.value)
    if (startAt.value)
      activeFilters.set('start_at', beijingIso(startAt.value))
    if (endAt.value)
      activeFilters.set('end_at', beijingIso(endAt.value))
    news.value = []
    nextCursor.value = null
    loaded.value = false
  }
  const query = new URLSearchParams(activeFilters)
  query.set('limit', '50')
  if (more && nextCursor.value)
    query.set('cursor', nextCursor.value)
  try {
    if (!more)
      sources.value = await page.all<Source>('/sources')
    const result = await page.get<Page<NewsItem>>(`/news?${query}`)
    news.value = more ? [...news.value, ...result.items] : result.items
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
    <header class="page-heading">
      <div>
        <p class="eyebrow">
          NEWS ARCHIVE
        </p><h1>新闻档案</h1><p class="muted">
          查询已保存的资讯，查看原始正文与不可变修订记录。
        </p>
      </div>
    </header>
    <section class="data-panel">
      <form class="filter-bar" @submit.prevent="load()">
        <div class="filter-field">
          <label for="news-source">资讯来源</label><ElSelect id="news-source" v-model="sourceId" aria-label="资讯来源" :disabled="loading">
            <ElOption label="全部来源" value="" /><ElOption v-for="source in sources" :key="source.id" :label="source.name" :value="source.id" />
          </ElSelect>
        </div>
        <div class="filter-field">
          <label for="news-body-status">正文状态</label><ElSelect id="news-body-status" v-model="bodyStatus" aria-label="正文状态" :disabled="loading">
            <ElOption label="全部状态" value="" /><ElOption v-for="(label, value) in bodyStatusLabels" :key="value" :label="label" :value="value" />
          </ElSelect>
        </div>
        <div class="filter-field">
          <label for="news-start">发布开始时间（北京时间 UTC+8）</label><input id="news-start" v-model="startAt" class="native-input" type="datetime-local" :disabled="loading">
        </div>
        <div class="filter-field">
          <label for="news-end">发布结束时间（不含，UTC+8）</label><input id="news-end" v-model="endAt" class="native-input" type="datetime-local" :disabled="loading">
        </div>
        <ElButton native-type="submit" type="primary" :loading="loading">
          查询新闻
        </ElButton>
      </form>
      <ElAlert v-if="error" class="panel-alert" type="error" :closable="false" :title="error" show-icon role="alert" />
      <p v-if="loading" class="loading-line" role="status">
        正在读取新闻档案…
      </p>
      <template v-if="loaded">
        <ElTable v-if="news.length" :data="news" class="data-table">
          <ElTableColumn label="新闻标题" min-width="320">
            <template #default="{ row }">
              <RouterLink :to="`/news/${row.id}`" class="news-title-link">
                {{ row.title || '（来源未提供标题）' }}
              </RouterLink><span class="cell-subtext mono">{{ row.source_item_id }}</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="来源" min-width="135">
            <template #default="{ row }">
              {{ sources.find(source => source.id === row.source_id)?.name || row.source_id }}
            </template>
          </ElTableColumn>
          <ElTableColumn label="正文状态" width="125">
            <template #default="{ row }">
              <ElTag :type="statusType(row.body_status)" effect="plain">
                {{ bodyStatusLabels[row.body_status as BodyStatus] || row.body_status }}
              </ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn label="发布时间" min-width="185">
            <template #default="{ row }">
              {{ formatDate(row.published_at) }}
            </template>
          </ElTableColumn>
          <ElTableColumn label="首次入库" min-width="185">
            <template #default="{ row }">
              {{ formatDate(row.first_seen_at) }}
            </template>
          </ElTableColumn>
        </ElTable>
        <div v-else class="archive-empty">
          <span class="empty-mark" aria-hidden="true">N / 0</span>
          <h2>暂无符合条件的新闻</h2>
          <p>当前筛选条件下没有已入库记录。<br>请调整来源、发布时间或正文状态后重试。</p>
        </div>
        <footer class="list-footer">
          <span>已加载 {{ news.length }} 条 · 按首次入库时间倒序</span><ElButton v-if="nextCursor" :loading="loading" @click="load(true)">
            加载更多
          </ElButton><span v-else-if="news.length">已到末尾</span>
        </footer>
      </template>
    </section>
  </section>
</template>
