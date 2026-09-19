<script setup lang="ts">
import type { NewsDetail, Page, Revision, Source } from '../types'
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { errorMessage } from '../api'
import { bodyStatusLabels, externalUrl, formatDate, statusType } from '../format'
import { usePageRequests } from '../operations'

const route = useRoute()
const page = usePageRequests()
const { error } = page
const news = ref<NewsDetail | null>(null)
const sourceName = ref('')
const revisions = ref<Revision[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(false)
const loadingRevisions = ref(false)
const revisionError = ref('')
let generation = 0

async function load() {
  const currentGeneration = ++generation
  loading.value = true
  error.value = ''
  revisionError.value = ''
  news.value = null
  revisions.value = []
  nextCursor.value = null
  const id = encodeURIComponent(String(route.params.id))
  try {
    const [detail, history, sources] = await Promise.all([
      page.get<NewsDetail>(`/news/${id}`),
      page.get<Page<Revision>>(`/news/${id}/revisions`),
      page.all<Source>('/sources'),
    ])
    if (currentGeneration !== generation)
      return
    news.value = detail
    revisions.value = history.items
    nextCursor.value = history.next_cursor
    sourceName.value = sources.find(source => source.id === detail.source_id)?.name || detail.source_id
  }
  catch (cause) {
    if (currentGeneration === generation)
      page.report(cause)
  }
  finally {
    if (currentGeneration === generation)
      loading.value = false
  }
}

async function loadMoreRevisions() {
  if (!news.value || !nextCursor.value || loadingRevisions.value)
    return
  const currentGeneration = generation
  loadingRevisions.value = true
  revisionError.value = ''
  try {
    const query = new URLSearchParams({ cursor: nextCursor.value })
    const result = await page.get<Page<Revision>>(`/news/${news.value.id}/revisions?${query}`)
    if (currentGeneration === generation) {
      revisions.value = [...revisions.value, ...result.items]
      nextCursor.value = result.next_cursor
    }
  }
  catch (cause) {
    if (currentGeneration === generation && !page.signal.aborted)
      revisionError.value = errorMessage(cause)
  }
  finally {
    loadingRevisions.value = false
  }
}

watch(() => route.params.id, load, { immediate: true })
</script>

<template>
  <section :aria-busy="loading">
    <header class="page-heading">
      <div>
        <RouterLink to="/news" class="text-link">
          ← 返回新闻档案
        </RouterLink><p class="eyebrow mt-5">
          ARTICLE / REVISION HISTORY
        </p><h1>新闻详情</h1>
      </div><ElButton :loading="loading" @click="load">
        刷新详情
      </ElButton>
    </header>
    <ElAlert v-if="error" class="mb-5" type="error" :closable="false" :title="error" show-icon role="alert" />
    <p v-if="loading" class="loading-line" role="status">
      正在读取正文与修订记录…
    </p>
    <template v-else-if="news">
      <article class="data-panel article-panel">
        <div class="article-meta">
          <span>{{ sourceName }}</span><ElTag :type="statusType(news.body_status)" effect="plain">
            {{ bodyStatusLabels[news.body_status] }}
          </ElTag>
        </div>
        <h2 class="article-title">
          {{ news.title || '（来源未提供标题）' }}
        </h2>
        <dl class="article-facts">
          <div><dt>发布时间</dt><dd>{{ formatDate(news.published_at) }}</dd></div><div><dt>首次入库</dt><dd>{{ formatDate(news.first_seen_at) }}</dd></div><div>
            <dt>来源条目 ID</dt><dd class="mono">
              {{ news.source_item_id }}
            </dd>
          </div><div>
            <dt>当前修订 ID</dt><dd class="mono">
              {{ news.current_revision_id || '尚无修订' }}
            </dd>
          </div>
        </dl>
        <a v-if="externalUrl(news.canonical_url)" :href="externalUrl(news.canonical_url)" target="_blank" rel="noopener noreferrer" class="external-link article-source">查看来源原文 ↗<span class="sr-only">（新窗口打开）</span></a>
        <section v-if="news.summary" class="article-summary">
          <h3>来源摘要</h3><p class="plain-text">
            {{ news.summary }}
          </p>
        </section>
        <section class="article-body">
          <h3>原始正文</h3><p v-if="news.body_text" class="plain-text">
            {{ news.body_text }}
          </p><p v-else class="muted">
            未保存正文。当前状态：{{ bodyStatusLabels[news.body_status] }}。摘要不视为完整正文。
          </p>
        </section>
      </article>
      <section class="data-panel mt-6">
        <header class="panel-heading">
          <div>
            <h2>修订记录</h2><p class="muted">
              按观测时间倒序 · 以纯文本查看保存的版本
            </p>
          </div><span class="muted">已加载 {{ revisions.length }} 个版本</span>
        </header>
        <ElAlert v-if="revisionError" class="panel-alert" type="error" :closable="false" :title="revisionError" show-icon role="alert" />
        <ElEmpty v-if="!revisions.length" description="该新闻尚无修订记录。" />
        <div v-else class="revision-list">
          <details v-for="revision in revisions" :key="revision.id" class="revision-item">
            <summary>
              <span><strong>{{ revision.title || '（来源未提供标题）' }}</strong><span class="cell-subtext">观测于 {{ formatDate(revision.observed_at) }}</span></span><ElTag :type="statusType(revision.body_status)" effect="plain">
                {{ bodyStatusLabels[revision.body_status] }}
              </ElTag>
            </summary>
            <div class="revision-content">
              <p class="muted mono">
                修订 ID：{{ revision.id }}
              </p><p class="muted">
                发布时间：{{ formatDate(revision.published_at) }}
              </p><p v-if="revision.body_text" class="plain-text">
                {{ revision.body_text }}
              </p><p v-else class="muted">
                此版本未保存正文，状态为「{{ bodyStatusLabels[revision.body_status] }}」。
              </p>
            </div>
          </details>
        </div>
        <footer v-if="nextCursor" class="list-footer">
          <span>还有更早的修订</span><ElButton :loading="loadingRevisions" @click="loadMoreRevisions">
            加载更多修订
          </ElButton>
        </footer>
      </section>
    </template>
  </section>
</template>
