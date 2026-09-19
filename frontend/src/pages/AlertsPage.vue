<script setup lang="ts">
import type { AlertEvent, NotificationDelivery, Page, Source, TelegramSettings } from '../types'
import { onMounted, reactive, ref } from 'vue'
import CommandLedger from '../components/CommandLedger.vue'
import { formatDate, statusLabel, statusType } from '../format'
import { useOperations, usePageRequests, usePolling } from '../operations'

const page = usePageRequests()
const { error } = page
const ops = useOperations(page)
const { busy, commands } = ops
const alerts = ref<AlertEvent[]>([])
const sources = ref<Source[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(false)
const settings = ref<TelegramSettings | null>(null)
const form = reactive({ enabled: false, chat_id: '', token: '' })
const notice = ref('')
const version = ref<{ version: string, commit_sha: string } | null>(null)
const delivery = ref<NotificationDelivery | null>(null)
const deliveryLoading = ref(false)
let deliveryGeneration = 0
let paged = false
function closeDelivery() {
  deliveryGeneration++
  delivery.value = null
}

async function load(more = false) {
  if (loading.value)
    return
  loading.value = true
  try {
    const query = new URLSearchParams({ limit: '50' })
    if (more && nextCursor.value)
      query.set('cursor', nextCursor.value)
    const result = await page.get<Page<AlertEvent>>(`/alerts?${query}`)
    alerts.value = more ? [...alerts.value, ...result.items] : result.items
    nextCursor.value = result.next_cursor
    paged = more
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    loading.value = false
  }
}
async function loadSettings() {
  try {
    settings.value = await page.get<TelegramSettings>('/notification-settings/telegram')
    Object.assign(form, { enabled: settings.value.enabled, chat_id: settings.value.chat_id || '', token: '' })
  }
  catch (cause) {
    page.report(cause)
  }
}
async function save() {
  try {
    await ops.mutate('/notification-settings/telegram', { enabled: form.enabled, chat_id: form.chat_id.trim(), ...(form.token ? { token: form.token } : {}) }, 'PATCH')
    form.token = ''
    notice.value = 'Telegram 配置已保存。保存不会发送测试通知。'
    await loadSettings()
  }
  catch (cause) {
    page.report(cause)
  }
}
async function test() {
  try {
    await ops.submit('/notification-settings/telegram/test', undefined, 'Telegram 显式测试', '/notification-deliveries')
  }
  catch (cause) {
    page.report(cause)
  }
}
async function inspectDelivery(id: string) {
  if (deliveryLoading.value)
    return
  deliveryLoading.value = true
  const generation = ++deliveryGeneration
  try {
    const result = await page.get<NotificationDelivery>(`/notification-deliveries/${id}`)
    if (generation === deliveryGeneration)
      delivery.value = result
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    deliveryLoading.value = false
  }
}
onMounted(async () => {
  await Promise.all([
    load(),
    loadSettings(),
    (async () => {
      try {
        ;[sources.value, version.value] = await Promise.all([page.all<Source>('/sources'), page.get<{ version: string, commit_sha: string }>('/version')])
      }
      catch (cause) {
        page.report(cause)
      }
    })(),
  ])
})
usePolling(async () => {
  if (!paged)
    await load()
  if (delivery.value && ['pending', 'sending', 'retrying'].includes(delivery.value.status))
    await inspectDelivery(delivery.value.id)
})
</script>

<template>
  <section :aria-busy="loading">
    <header class="page-heading">
      <div>
        <p class="eyebrow">
          INCIDENT DESK
        </p><h1>告警与通知</h1><p class="muted">
          来源故障与恢复留痕；通知投递独立运行，不阻塞采集事务。
        </p>
      </div><ElButton :loading="loading" @click="load()">
        刷新告警
      </ElButton>
    </header>
    <ElAlert v-if="error" :title="error" type="error" :closable="false" show-icon class="mb-5" role="alert" />
    <ElAlert v-if="notice" :title="notice" type="success" class="mb-5" @close="notice = ''" />
    <CommandLedger :commands="commands" />
    <section class="data-panel mb-5">
      <header class="panel-heading">
        <h2>来源告警</h2><span class="muted">当前页每 5 秒刷新</span>
      </header>
      <ElTable :data="alerts" class="data-table" empty-text="尚无告警记录">
        <ElTableColumn label="事件 / 摘要" min-width="290">
          <template #default="{ row }">
            <strong>{{ row.kind }}</strong><p class="cell-subtext">
              {{ row.message }}
            </p>
          </template>
        </ElTableColumn>
        <ElTableColumn label="来源" min-width="130">
          <template #default="{ row }">
            {{ row.source_id ? sources.find(source => source.id === row.source_id)?.name || row.source_id : '系统' }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="事件状态" min-width="110">
          <template #default="{ row }">
            <ElTag :type="statusType(row.status)" effect="plain">
              {{ statusLabel(row.status) }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="通知投递" min-width="150">
          <template #default="{ row }">
            <ElTag :type="statusType(row.delivery_status || 'unknown')" effect="plain">
              {{ row.delivery_status ? statusLabel(row.delivery_status) : '未安排投递' }}
            </ElTag><ElButton v-if="row.delivery_id" link type="primary" :disabled="deliveryLoading" @click="inspectDelivery(row.delivery_id)">
              查看投递详情
            </ElButton>
          </template>
        </ElTableColumn>
        <ElTableColumn label="发生时间" min-width="190">
          <template #default="{ row }">
            {{ formatDate(row.created_at) }}
          </template>
        </ElTableColumn>
      </ElTable><footer class="list-footer">
        <span>已加载 {{ alerts.length }} 条</span><ElButton v-if="nextCursor" :loading="loading" @click="load(true)">
          加载更多
        </ElButton>
      </footer>
    </section>
    <div class="session-grid">
      <section class="data-panel">
        <header class="panel-heading">
          <h2>Telegram 通知</h2><ElButton text :disabled="busy" @click="loadSettings">
            重新读取配置
          </ElButton>
        </header><div class="panel-body">
          <form v-if="settings" class="operation-form" @submit.prevent="save">
            <label>启用通知 <ElSwitch v-model="form.enabled" aria-label="启用 Telegram 通知" /></label><label>目标 Chat ID<ElInput v-model="form.chat_id" aria-label="Telegram Chat ID" autocomplete="off" /></label><label>Bot Token（只写）<ElInput v-model="form.token" type="password" autocomplete="new-password" aria-label="Telegram Bot Token" /></label><p class="muted">
              {{ settings.has_token ? '已保存 Token。留空保留原值，填写后替换。' : '尚未配置 Token。' }} 永不回读 Token，不支持 Bot 远程控制命令。
            </p><div class="row-actions">
              <ElButton native-type="submit" type="primary" :loading="busy">
                保存配置
              </ElButton><ElPopconfirm title="使用已保存配置发送一条真实测试通知？" @confirm="test">
                <template #reference>
                  <ElButton :disabled="busy || !settings.has_token || !settings.chat_id">
                    显式发送测试
                  </ElButton>
                </template>
              </ElPopconfirm>
            </div><p class="muted">
              测试只使用已保存配置；请先保存更改。投递结果见操作回执，接受请求不代表消息已送达。
            </p>
          </form>
          <p v-else class="muted">
            配置未加载；请使用“重新读取配置”重试。
          </p>
        </div>
      </section>
      <section class="data-panel">
        <header class="panel-heading">
          <h2>部署身份</h2><span class="muted">版本与提交</span>
        </header><div class="panel-body">
          <dl v-if="version" class="detail-list">
            <dt>日期版本</dt><dd class="mono">
              {{ version.version }}
            </dd><dt>Commit SHA</dt><dd class="mono">
              {{ version.commit_sha }}
            </dd>
          </dl><p v-else class="muted">
            尚未取得版本信息，请刷新页面重试。
          </p><p class="muted">
            排查问题时请同时提供版本、提交与错误中的请求编号。不要粘贴 Bot Token、代理认证或会话资料。
          </p>
        </div>
      </section>
    </div>
    <ElDialog :model-value="Boolean(delivery)" title="通知投递详情" class="operation-dialog" @close="closeDelivery">
      <template v-if="delivery">
        <ElTag :type="statusType(delivery.status)">
          {{ statusLabel(delivery.status) }}
        </ElTag>
        <dl class="detail-list">
          <dt>投递 ID</dt><dd class="mono">
            {{ delivery.id }}
          </dd><dt>已尝试次数</dt><dd>{{ delivery.attempts }}</dd><dt>最后错误</dt><dd>{{ delivery.last_error || '未记录错误' }}</dd><dt>下次重试</dt><dd>{{ formatDate(delivery.next_attempt_at) }}</dd><dt>完成时间</dt><dd>{{ formatDate(delivery.completed_at) }}</dd>
        </dl>
        <h3>投递尝试历史</h3>
        <article v-for="(attempt, index) in delivery.attempt_history" :key="index" class="log-event">
          <strong>{{ statusLabel(attempt.status) }} · {{ formatDate(attempt.at) }}</strong><span>{{ attempt.error || '未记录错误' }}</span>
        </article>
        <ElButton :loading="deliveryLoading" @click="inspectDelivery(delivery.id)">
          刷新投递结果
        </ElButton>
      </template>
    </ElDialog>
  </section>
</template>
