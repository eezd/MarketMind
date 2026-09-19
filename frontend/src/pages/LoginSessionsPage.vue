<script setup lang="ts">
import type { LoginAttempt, Source, SourceSession } from '../types'
import { computed, onMounted, onScopeDispose, ref } from 'vue'
import { formatDate, statusLabel, statusType } from '../format'
import { useOperations, usePageRequests, usePolling } from '../operations'

const page = usePageRequests()
const { error } = page
const ops = useOperations(page)
const { busy } = ops
const sources = ref<Source[]>([])
const sourceId = ref('')
const session = ref<SourceSession | null>(null)
const attempt = ref<LoginAttempt | null>(null)
const qrUrl = ref('')
const loading = ref(false)
const now = ref(Date.now())
const notice = ref('')
let qrAttempt = ''
let nextPollAt = 0
let polling = false
const remaining = computed(() => attempt.value ? Math.max(0, Math.ceil((Date.parse(attempt.value.expires_at) - now.value) / 1000)) : 0)
const active = computed(() => Boolean(attempt.value && ['queued', 'qr_pending', 'verifying'].includes(attempt.value.status) && remaining.value > 0))
function clearQr() {
  if (qrUrl.value)
    URL.revokeObjectURL(qrUrl.value)
  qrUrl.value = ''
  qrAttempt = ''
}
async function loadSession() {
  if (!sourceId.value)
    return
  const id = sourceId.value
  const result = await page.get<SourceSession>(`/sources/${id}/session`)
  if (id === sourceId.value)
    session.value = result
}
async function updateQr() {
  const current = attempt.value
  if (!current || !current.qr_available || !active.value) {
    clearQr()
    return
  }
  if (qrAttempt === current.id)
    return
  const blob = await page.get<Blob>(`/login-attempts/${current.id}/qr`, { cache: 'no-store', headers: { Accept: 'image/*' } })
  if (attempt.value?.id !== current.id || !active.value || page.signal.aborted)
    return
  clearQr()
  qrUrl.value = URL.createObjectURL(blob)
  qrAttempt = current.id
}
async function changeSource() {
  attempt.value = null
  session.value = null
  clearQr()
  loading.value = true
  try {
    await loadSession()
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    loading.value = false
  }
}
async function create() {
  if (!sourceId.value)
    return
  try {
    attempt.value = await ops.mutate<LoginAttempt>(`/sources/${sourceId.value}/login-attempts`)
    notice.value = '登录请求已接受；仅分域验证通过后才视为登录成功。'
    now.value = Date.now()
    await updateQr()
    await loadSession()
  }
  catch (cause) {
    page.report(cause)
  }
}
async function action(name: 'refresh' | 'cancel') {
  if (!attempt.value)
    return
  try {
    attempt.value = await ops.mutate<LoginAttempt>(`/login-attempts/${attempt.value.id}/${name}`)
    clearQr()
    now.value = Date.now()
    notice.value = name === 'refresh' ? '刷新请求已接受，旧二维码已失效。' : '取消请求已接受，正在核对状态。'
    await updateQr()
    await loadSession()
  }
  catch (cause) {
    page.report(cause)
  }
}
async function revoke() {
  if (!sourceId.value)
    return
  try {
    await ops.mutate(`/sources/${sourceId.value}/session`, undefined, 'DELETE')
    attempt.value = null
    clearQr()
    notice.value = '本地会话已撤销；不代表已退出第三方网站的全部设备。'
    await loadSession()
  }
  catch (cause) {
    page.report(cause)
  }
}
async function refresh() {
  if (loading.value)
    return
  loading.value = true
  try {
    await loadSession()
    if (attempt.value) {
      const id = attempt.value.id
      const result = await page.get<LoginAttempt>(`/login-attempts/${id}`)
      if (attempt.value?.id === id)
        attempt.value = result
      await updateQr()
    }
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    loading.value = false
  }
}
onMounted(async () => {
  try {
    sources.value = (await page.all<Source>('/sources')).filter(source => source.code === 'jin10')
    sourceId.value = sources.value[0]?.id ?? ''
    await changeSource()
  }
  catch (cause) {
    page.report(cause)
  }
})
usePolling(async () => {
  now.value = Date.now()
  if (!remaining.value)
    clearQr()
  if (polling || now.value < nextPollAt)
    return
  polling = true
  try {
    if (attempt.value && !['authenticated', 'failed', 'cancelled', 'expired', 'action_required'].includes(attempt.value.status))
      await refresh()
    else
      await loadSession()
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    polling = false
    nextPollAt = Date.now() + Math.max(5, attempt.value?.poll_interval_seconds ?? 5) * 1000
  }
}, 1000)
onScopeDispose(clearQr)
</script>

<template>
  <section :aria-busy="loading">
    <header class="page-heading">
      <div>
        <p class="eyebrow">
          PROTECTED ACCESS
        </p><h1>金十登录会话</h1><p class="muted">
          扫码与采集共享来源出口；分别验证域名，不以扫码完成代替授权验证。
        </p>
      </div><ElButton :loading="loading" @click="refresh">
        刷新状态
      </ElButton>
    </header>
    <ElAlert v-if="error" :title="error" type="error" :closable="false" show-icon class="mb-5" role="alert" />
    <ElAlert v-if="notice" :title="notice" type="info" class="mb-5" @close="notice = ''" />
    <ElEmpty v-if="!sources.length && !loading" description="尚未登记金十来源" />
    <div v-else class="session-grid">
      <section class="data-panel">
        <header class="panel-heading">
          <h2>分域会话</h2><ElTag v-if="session" :type="statusType(session.status)" effect="plain">
            {{ statusLabel(session.status) }}
          </ElTag>
        </header><div class="panel-body">
          <label class="field-label" for="session-source">登录来源</label><ElSelect id="session-source" v-model="sourceId" aria-label="登录来源" :disabled="busy || active" @change="changeSource">
            <ElOption v-for="source in sources" :key="source.id" :value="source.id" :label="source.name" />
          </ElSelect>
          <dl class="detail-list">
            <template v-for="domain in session?.domains || []" :key="domain.domain">
              <dt>{{ domain.domain }}</dt><dd>
                <ElTag :type="statusType(domain.status)" effect="plain">
                  {{ statusLabel(domain.status) }}
                </ElTag>
              </dd>
            </template>
          </dl><p v-if="!session?.domains.length" class="muted">
            尚无分域验证结果。
          </p><p class="muted">
            最近更新：{{ formatDate(session?.updated_at) }}
          </p>
          <ElPopconfirm title="撤销保存的会话并暂停依赖任务？不退出第三方全部设备。" @confirm="revoke">
            <template #reference>
              <ElButton type="danger" plain :disabled="busy || !session">
                撤销本地会话
              </ElButton>
            </template>
          </ElPopconfirm>
        </div>
      </section>
      <section class="data-panel">
        <header class="panel-heading">
          <h2>官方二维码</h2><ElTag v-if="attempt" :type="statusType(attempt.status)">
            {{ statusLabel(attempt.status) }}
          </ElTag>
        </header><div class="panel-body">
          <div class="qr-stage">
            <img v-if="qrUrl && active" :src="qrUrl" class="login-qr" alt="金十官方登录二维码，仅当前登录尝试有效" referrerpolicy="no-referrer"><div v-else>
              <p class="eyebrow">
                AUTHENTICATED CHANNEL
              </p><h3>{{ attempt ? remaining ? statusLabel(attempt.status) : '二维码已到期' : '由管理员发起登录' }}</h3><p class="muted">
                {{ attempt?.status === 'action_required' ? '官方流程需要额外交互，请根据下方说明处理；不会绕过验证。' : '仅显示后台获取的官方二维码，不生成模拟内容。' }}
              </p>
            </div>
          </div>
          <p v-if="attempt?.instructions || attempt?.message" class="muted">
            {{ attempt.instructions || attempt.message }}
          </p>
          <ElAlert v-if="attempt?.error_code" :title="`登录状态说明：${attempt.error_code}`" type="warning" :closable="false" class="mb-5" />
          <p v-if="attempt" class="muted">
            到期：{{ formatDate(attempt.expires_at) }}<br>剩余 {{ remaining }} 秒 · {{ attempt.id }}
          </p>
          <div class="row-actions">
            <ElButton type="primary" :loading="busy" :disabled="!sourceId || active" @click="create">
              开始登录
            </ElButton><ElButton v-if="attempt" :disabled="busy || attempt.status === 'authenticated'" @click="action('refresh')">
              刷新二维码
            </ElButton><ElButton v-if="attempt" :disabled="busy || !active" @click="action('cancel')">
              取消登录
            </ElButton>
          </div>
          <p class="muted mt-4">
            二维码经同源鉴权、禁止缓存；到期后立即隐藏。后台验证失败或需要额外交互时会保留真实状态，凭证不会返回浏览器。
          </p>
        </div>
      </section>
    </div>
  </section>
</template>
