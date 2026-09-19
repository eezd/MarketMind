<script setup lang="ts">
import type { SummaryArticle } from '../types'
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { formatDate, statusType } from '../format'
import { usePageRequests } from '../operations'

const route = useRoute()
const page = usePageRequests()
const { error } = page
const article = ref<SummaryArticle | null>(null)
const loading = ref(false)

function statusLabel(value: SummaryArticle) {
  if (value.status === 'succeeded')
    return value.provider === 'ai' ? 'AI 生成' : '摘要完成'
  if (value.status === 'fallback')
    return '规则回退摘要'
  return '生成失败'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    article.value = await page.get<SummaryArticle>(`/analysis/summaries/${String(route.params.id)}`)
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
    <header class="page-heading">
      <div>
        <p class="eyebrow">
          RESEARCH NOTE
        </p>
        <h1>摘要详情</h1>
        <p class="muted">
          生成内容、模型状态和原始新闻均保留可追溯关系。
        </p>
      </div>
      <RouterLink to="/summaries">
        <ElButton>返回摘要列表</ElButton>
      </RouterLink>
    </header>

    <ElAlert v-if="error" class="mb-5" type="error" :closable="false" :title="error" show-icon role="alert" />
    <p v-if="loading" class="loading-line">
      正在读取摘要详情…
    </p>

    <article v-if="article" class="research-note">
      <header class="research-note-header">
        <div class="research-note-meta">
          <span>{{ article.category }}</span>
          <ElTag :type="statusType(article.status)" effect="plain">
            {{ statusLabel(article) }}
          </ElTag>
        </div>
        <h2>{{ article.title }}</h2>
        <div class="research-dateline">
          <span>资讯时间 {{ formatDate(article.first_published_at) }} — {{ formatDate(article.last_published_at) }}</span>
          <span>更新 {{ formatDate(article.updated_at) }}</span>
        </div>
      </header>

      <div class="research-note-body">
        <section class="research-summary">
          <p class="eyebrow">
            EXECUTIVE SUMMARY
          </p>
          <p>{{ article.summary }}</p>
        </section>

        <section v-if="article.key_points.length" class="research-points">
          <p class="eyebrow">
            KEY POINTS
          </p>
          <ol>
            <li v-for="point in article.key_points" :key="point">
              {{ point }}
            </li>
          </ol>
        </section>

        <section class="research-provenance">
          <div>
            <p class="eyebrow">
              PROVENANCE
            </p>
            <h3>生成记录</h3>
          </div>
          <dl>
            <div><dt>提供方</dt><dd>{{ article.provider }}</dd></div>
            <div><dt>模型</dt><dd>{{ article.model || '未配置' }}</dd></div>
            <div><dt>状态</dt><dd>{{ statusLabel(article) }}</dd></div>
            <div><dt>异常代码</dt><dd>{{ article.error_code || '无' }}</dd></div>
          </dl>
        </section>

        <section class="research-sources">
          <div>
            <p class="eyebrow">
              SOURCE LEDGER
            </p>
            <h3>原始新闻 · {{ article.news_ids.length }}</h3>
          </div>
          <div class="source-ledger">
            <RouterLink v-for="(newsId, index) in article.news_ids" :key="newsId" :to="`/news/${newsId}`">
              <span>{{ String(index + 1).padStart(2, '0') }}</span>
              <code>{{ newsId }}</code>
              <strong>查看原文归档 →</strong>
            </RouterLink>
          </div>
        </section>
      </div>
    </article>
  </section>
</template>
