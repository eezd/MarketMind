<script setup lang="ts">
import type { Page, ProxyEndpoint } from '../types'
import { onMounted, reactive, ref } from 'vue'
import CommandLedger from '../components/CommandLedger.vue'
import { formatDate, statusLabel, statusType } from '../format'
import { useOperations, usePageRequests } from '../operations'

const page = usePageRequests()
const { error } = page
const ops = useOperations(page)
const { busy, commands } = ops
const proxies = ref<ProxyEndpoint[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(false)
const dialog = ref(false)
const editId = ref<string | null>(null)
const hasCredentials = ref(false)
const importDialog = ref(false)
const importText = ref('')
const importNotice = ref('')
const notice = ref('')
const form = reactive({ name: '', scheme: 'http' as const, host: '', port: 8080, username: '', password: '', enabled: true, expires_at: '' })

async function load(more = false) {
  if (loading.value)
    return
  loading.value = true
  try {
    const query = new URLSearchParams({ limit: '50' })
    if (more && nextCursor.value)
      query.set('cursor', nextCursor.value)
    const result = await page.get<Page<ProxyEndpoint>>(`/proxies?${query}`)
    proxies.value = more ? [...proxies.value, ...result.items] : result.items
    nextCursor.value = result.next_cursor
    error.value = ''
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    loading.value = false
  }
}
function open(proxy?: ProxyEndpoint) {
  editId.value = proxy?.id ?? null
  hasCredentials.value = proxy?.has_credentials ?? false
  Object.assign(form, { name: proxy?.name ?? '', scheme: 'http', host: proxy?.host ?? '', port: proxy?.port ?? 8080, username: '', password: '', enabled: proxy?.enabled ?? true, expires_at: proxy?.expires_at ?? '' })
  dialog.value = true
}
function clearSecrets() {
  form.username = ''
  form.password = ''
}
async function save() {
  if (!form.name.trim() || !form.host.trim()) {
    error.value = '请填写代理名称与主机。'
    return
  }
  if (form.expires_at && !Number.isFinite(Date.parse(form.expires_at))) {
    error.value = '到期时间必须为含时区的 ISO 8601 时间。'
    return
  }
  if (form.expires_at && !/(?:Z|[+-]\d{2}:\d{2})$/i.test(form.expires_at)) {
    error.value = '到期时间必须包含时区，例如 +08:00 或 Z。'
    return
  }
  try {
    const payload = { name: form.name.trim(), scheme: form.scheme, host: form.host.trim(), port: form.port, enabled: form.enabled, expires_at: form.expires_at ? new Date(form.expires_at).toISOString() : null, ...(form.username || form.password ? { username: form.username, password: form.password } : {}) }
    await ops.mutate(editId.value ? `/proxies/${editId.value}` : '/proxies', payload, editId.value ? 'PATCH' : 'POST')
    clearSecrets()
    dialog.value = false
    notice.value = '代理配置已保存；加入库存不会改变任何来源的出口。'
    await load()
  }
  catch (cause) {
    page.report(cause)
  }
}
async function toggle(proxy: ProxyEndpoint) {
  try {
    await ops.mutate(`/proxies/${proxy.id}`, { enabled: !proxy.enabled }, 'PATCH')
    await load()
  }
  catch (cause) {
    page.report(cause)
  }
}
async function remove(proxy: ProxyEndpoint) {
  try {
    await ops.mutate(`/proxies/${proxy.id}`, undefined, 'DELETE')
    notice.value = '代理配置已删除，历史审计保留。'
    await load()
  }
  catch (cause) {
    page.report(cause)
  }
}
async function check(proxy: ProxyEndpoint) {
  try {
    await ops.submit(`/proxies/${proxy.id}/check`, undefined, `检查 ${proxy.name}`, '/proxy-checks')
  }
  catch (cause) {
    page.report(cause)
  }
}
async function checkAll() {
  const enabled = proxies.value.filter(proxy => proxy.enabled)
  if (!enabled.length) {
    notice.value = '当前列表没有已启用的代理。'
    return
  }
  let submitted = 0
  for (const proxy of enabled) {
    try {
      await ops.submit(`/proxies/${proxy.id}/check`, undefined, `检查 ${proxy.name}`, '/proxy-checks')
      submitted += 1
    }
    catch (cause) {
      page.report(cause)
      break
    }
  }
  notice.value = submitted === enabled.length
    ? `已提交 ${submitted} 个代理的健康检查；执行结果将在操作回执中更新。`
    : `已提交 ${submitted}/${enabled.length} 个代理的健康检查；其余未提交，请处理错误后重试。`
}
function parseProxyLine(rawLine: string, lineNumber: number) {
  const [endpoint, username, ...passwordParts] = rawLine.split('@')
  const separator = endpoint.lastIndexOf(':')
  if (separator <= 0 || separator === endpoint.length - 1)
    throw new Error(`第 ${lineNumber} 行必须使用 host:port 格式。`)

  const host = endpoint.slice(0, separator).trim()
  const portText = endpoint.slice(separator + 1).trim()
  const port = Number(portText)
  if (!host || !/^\d+$/.test(portText) || !Number.isInteger(port) || port < 1 || port > 65535)
    throw new Error(`第 ${lineNumber} 行的主机或端口无效。`)
  if (username !== undefined && (!username || !passwordParts.length || !passwordParts.join('@')))
    throw new Error(`第 ${lineNumber} 行必须同时提供用户名和密码。`)

  const endpointName = `${host}:${port}`
  return {
    name: endpointName.length <= 100 ? endpointName : `proxy-${lineNumber}`,
    scheme: 'http',
    host,
    port,
    enabled: true,
    ...(username === undefined ? {} : { username, password: passwordParts.join('@') }),
  }
}
async function importProxies() {
  const lines = importText.value
    .split(/\r?\n/)
    .map((text, index) => ({ text: text.trim(), lineNumber: index + 1 }))
    .filter(line => line.text)
  if (!lines.length) {
    error.value = '请提供至少一行代理。'
    return
  }
  if (lines.length > 200) {
    error.value = '单次最多导入 200 个代理。'
    return
  }

  let items
  try {
    items = lines.map(line => parseProxyLine(line.text, line.lineNumber))
  }
  catch (cause) {
    error.value = cause instanceof Error ? cause.message : '代理格式无效。'
    return
  }
  try {
    const result = await ops.mutate<{ items: { index: number, status: 'created' | 'error', id?: string, message?: string }[] }>('/proxies/import', { items })
    importText.value = ''
    importNotice.value = result.items.map(item => `第 ${lines[item.index]?.lineNumber ?? item.index + 1} 行：${item.status === 'created' ? '已导入' : `失败 · ${item.message || '请检查输入'}`}`).join('；')
    await load()
  }
  catch (cause) {
    page.report(cause)
  }
}
onMounted(() => load())
</script>

<template>
  <section :aria-busy="loading">
    <header class="page-heading">
      <div>
        <p class="eyebrow">
          EGRESS INVENTORY
        </p><h1>代理库存</h1><p class="muted">
          手工维护可信 HTTP 代理；HTTPS 目标使用 CONNECT，始终验证证书。
        </p>
      </div><div class="row-actions">
        <ElButton @click="importDialog = true">
          批量导入
        </ElButton><ElButton type="primary" @click="open()">
          添加代理
        </ElButton>
      </div>
    </header>
    <ElAlert v-if="error" :title="error" type="error" :closable="false" class="mb-5" role="alert" show-icon />
    <ElAlert v-if="notice" :title="notice" type="success" class="mb-5" @close="notice = ''" />
    <CommandLedger :commands="commands" />
    <section class="data-panel">
      <header class="panel-heading">
        <div>
          <h2>出口资源</h2><p class="muted">
            连通成功不等于目标站点可用。配置来源顺序后才会使用。
          </p>
        </div><div class="row-actions">
          <ElButton :loading="loading" @click="load()">
            刷新列表
          </ElButton><ElButton type="primary" :loading="busy" :disabled="!proxies.some(proxy => proxy.enabled)" @click="checkAll">
            检查当前列表
          </ElButton>
        </div>
      </header>
      <ElTable :data="proxies" class="data-table" empty-text="暂无代理；来源默认直连">
        <ElTableColumn label="代理" min-width="210">
          <template #default="{ row }">
            <strong>{{ row.name }}</strong><span class="cell-subtext mono">{{ row.scheme }}://{{ row.host }}:{{ row.port }}</span><span class="cell-subtext">{{ row.has_credentials ? '已保存认证（不回显）' : '未配置认证' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="库存状态" min-width="125">
          <template #default="{ row }">
            <ElTag :type="statusType(row.status)" effect="plain">
              {{ row.enabled ? '已启用' : '已禁用' }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="到期时间" min-width="190">
          <template #default="{ row }">
            {{ row.expires_at ? formatDate(row.expires_at) : '未设置' }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="来源健康 / 最近检查" min-width="265">
          <template #default="{ row }">
            <span v-if="!row.source_health?.length" class="muted">尚未检查</span><div v-for="health in row.source_health" :key="health.source_id" class="cell-subtext">
              <span class="mono">{{ health.source_id }}</span><br>{{ statusLabel(health.status) }} · 连续失败 {{ health.consecutive_failures }}<br>最近检查 {{ formatDate(health.last_checked_at) }}<br>最近成功 {{ formatDate(health.last_success_at) }}<template v-if="health.error_code">
                <br>异常 {{ health.error_code }}
              </template>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" min-width="295">
          <template #default="{ row }">
            <div class="row-actions">
              <ElButton size="small" :disabled="busy" @click="open(row)">
                编辑
              </ElButton><ElButton size="small" :disabled="busy" @click="toggle(row)">
                {{ row.enabled ? '禁用' : '启用' }}
              </ElButton><ElButton size="small" :disabled="busy || !row.enabled" @click="check(row)">
                健康检查
              </ElButton><ElPopconfirm title="仅无活动引用时可删除。确认删除此代理？" @confirm="remove(row)">
                <template #reference>
                  <ElButton size="small" type="danger" plain :disabled="busy">
                    删除
                  </ElButton>
                </template>
              </ElPopconfirm>
            </div>
          </template>
        </ElTableColumn>
      </ElTable><footer class="list-footer">
        <RouterLink to="/sources" class="text-link">
          配置来源出口 →
        </RouterLink><span>{{ proxies.length }} 条</span><ElButton v-if="nextCursor" :loading="loading" @click="load(true)">
          加载更多
        </ElButton>
      </footer>
    </section>
    <ElDialog v-model="dialog" :title="editId ? '编辑代理' : '添加 HTTP 代理'" class="operation-dialog" @closed="clearSecrets">
      <ElAlert v-if="error" :title="error" type="error" :closable="false" class="mb-5" />
      <form class="operation-form" @submit.prevent="save">
        <label>名称<ElInput v-model="form.name" required maxlength="120" aria-label="代理名称" /></label><label>主机（不含协议或路径）<ElInput v-model="form.host" required aria-label="代理主机" /></label><label>端口<ElInputNumber v-model="form.port" :min="1" :max="65535" aria-label="代理端口" /></label><label>启用 <ElSwitch v-model="form.enabled" aria-label="启用代理" /></label><label>认证用户名（只写）<ElInput v-model="form.username" type="password" autocomplete="off" aria-label="代理认证用户名" /></label><label>认证密码（只写）<ElInput v-model="form.password" type="password" autocomplete="new-password" aria-label="代理认证密码" /></label><p class="muted">
          {{ hasCredentials ? '已保存认证。留空保留原凭证；填写时将替换。' : '不使用认证时留空。凭证只用于请求，不会回显。' }}
        </p><label>到期时间（ISO 8601，含时区；留空不限）<ElInput v-model="form.expires_at" aria-label="代理到期时间" placeholder="YYYY-MM-DDTHH:mm:ss+08:00" /></label><ElButton native-type="submit" type="primary" :loading="busy">
          保存代理
        </ElButton>
      </form>
    </ElDialog>
    <ElDialog v-model="importDialog" title="批量导入代理" class="operation-dialog" @closed="importText = ''; importNotice = ''">
      <ElAlert v-if="error" :title="error" type="error" :closable="false" class="mb-5" />
      <ElAlert v-if="importNotice" :title="importNotice" type="info" :closable="false" class="mb-5" />
      <form class="operation-form" @submit.prevent="importProxies">
        <p class="muted">
          每行一个 HTTP 代理，最多 200 行，空行自动忽略。无认证使用 host:port；需要认证使用 host:port@username@password。名称按地址自动生成，默认启用且不限有效期；密码中的后续 @ 会原样保留。提交后立即清空输入，凭证不会回显。
        </p><label>代理列表<ElInput v-model="importText" type="textarea" :rows="10" autocomplete="off" aria-label="逐行批量代理" placeholder="203.0.113.10:8080@username@password&#10;proxy.example.com:3128" /></label><ElButton native-type="submit" type="primary" :loading="busy" :disabled="!importText.trim()">
          导入并查看逐条结果
        </ElButton>
      </form>
    </ElDialog>
  </section>
</template>
