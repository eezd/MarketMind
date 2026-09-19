<script setup lang="ts">
import type { Collector, ProxyEndpoint, Source } from '../types'
import { computed, onMounted, reactive, ref } from 'vue'
import CommandLedger from '../components/CommandLedger.vue'
import { externalUrl, formatDate, statusLabel, statusType } from '../format'
import { useOperations, usePageRequests, usePolling } from '../operations'

const page = usePageRequests()
const { error } = page
const ops = useOperations(page)
const { busy, commands } = ops
const sources = ref<Source[]>([])
const collectors = ref<Collector[]>([])
const proxies = ref<ProxyEndpoint[]>([])
const loading = ref(false)
const notice = ref('')
const sourceEdit = ref<Source | null>(null)
const collectorEdit = ref<Collector | null>(null)
const sourceForm = reactive({ enabled: true, request_interval_seconds: 3, proxy_ids: [] as string[], failover_enabled: false, on_proxy_exhausted: 'pause' as 'direct' | 'pause' })
const collectorForm = reactive({ enabled: true, interval_seconds: 60, max_pages: 100 })
const proxyChoice = ref('')
const enabledCount = computed(() => collectors.value.filter(collector => collector.enabled).length)

async function load() {
  if (loading.value)
    return
  loading.value = true
  try {
    const result = await Promise.all([page.all<Source>('/sources'), page.all<Collector>('/collectors'), page.all<ProxyEndpoint>('/proxies')])
    ;[sources.value, collectors.value, proxies.value] = result
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    loading.value = false
  }
}
function editSource(source: Source) {
  sourceEdit.value = source
  Object.assign(sourceForm, { enabled: source.enabled, request_interval_seconds: source.permission_config?.request_interval_seconds ?? 3, proxy_ids: [...(source.permission_config?.proxy_ids ?? [])], failover_enabled: source.permission_config?.failover_enabled ?? false, on_proxy_exhausted: source.permission_config?.on_proxy_exhausted ?? 'pause' })
  proxyChoice.value = ''
}
function moveProxy(index: number, direction: number) {
  const ids = sourceForm.proxy_ids
  const other = index + direction
  if (other >= 0 && other < ids.length)
    [ids[index], ids[other]] = [ids[other]!, ids[index]!]
}
function addProxy() {
  if (proxyChoice.value && !sourceForm.proxy_ids.includes(proxyChoice.value))
    sourceForm.proxy_ids.push(proxyChoice.value)
  proxyChoice.value = ''
}
async function saveSource() {
  if (!sourceEdit.value)
    return
  try {
    await ops.mutate(`/sources/${sourceEdit.value.id}`, { ...sourceForm, config_version: sourceEdit.value.config_version }, 'PATCH')
    sourceEdit.value = null
    notice.value = '来源配置已保存，将在下次安全请求边界应用。'
    await load()
  }
  catch (cause) {
    page.report(cause)
  }
}
function editCollector(collector: Collector) {
  collectorEdit.value = collector
  Object.assign(collectorForm, { enabled: collector.enabled, interval_seconds: collector.interval_seconds, max_pages: typeof collector.config?.max_pages === 'number' ? collector.config.max_pages : 100 })
}
async function saveCollector() {
  if (!collectorEdit.value)
    return
  try {
    await ops.mutate(`/collectors/${collectorEdit.value.id}`, { enabled: collectorForm.enabled, interval_seconds: collectorForm.interval_seconds, config: { max_pages: collectorForm.max_pages }, config_version: collectorEdit.value.config_version }, 'PATCH')
    collectorEdit.value = null
    notice.value = '调度配置已保存。关闭计划不会取消已有运行。'
    await load()
  }
  catch (cause) {
    page.report(cause)
  }
}
async function restart(collector: Collector) {
  try {
    await ops.submit(`/collectors/${collector.id}/restart`, undefined, `重启 ${collector.name}`)
  }
  catch (cause) {
    page.report(cause)
  }
}
onMounted(load)
usePolling(load)
</script>

<template>
  <section :aria-busy="loading">
    <header class="page-heading">
      <div>
        <p class="eyebrow">
          SOURCE REGISTRY
        </p><h1>来源与采集器</h1><p class="muted">
          共享请求预算与稳定出口；计划开关和运行健康分别管理。
        </p>
      </div><ElButton :loading="loading" @click="load">
        刷新状态
      </ElButton>
    </header>
    <ElAlert v-if="error" class="mb-5" type="error" :closable="false" :title="error" show-icon role="alert" />
    <ElAlert v-if="notice" class="mb-5" type="success" :title="notice" @close="notice = ''" />
    <CommandLedger :commands="commands" />
    <div class="registry-summary">
      <div><span>已登记来源</span><strong>{{ sources.length }}<small>个</small></strong></div><div><span>采集入口</span><strong>{{ collectors.length }}<small>个</small></strong></div><div><span>计划启用</span><strong>{{ enabledCount }}<small>/ {{ collectors.length }}</small></strong></div><p>每 5 秒刷新<br><span>关闭计划不会取消在途运行</span></p>
    </div>
    <ElEmpty v-if="!loading && !sources.length" description="尚未登记来源" />
    <div class="source-grid">
      <article v-for="source in sources" :key="source.id" class="source-card">
        <div class="source-card-heading">
          <span class="source-code">{{ source.code }} / v{{ source.config_version }}</span><ElTag :type="source.enabled ? 'success' : 'info'" effect="plain">
            {{ source.enabled ? '来源启用' : '来源关闭' }}
          </ElTag>
        </div>
        <h2>{{ source.name }}</h2>
        <dl class="detail-list">
          <dt>实际出口</dt><dd>{{ source.actual_egress || '尚未记录' }}</dd><dt>切换原因</dt><dd>{{ source.egress_reason || '未记录切换' }}</dd><dt>直连回退</dt><dd>{{ source.direct_fallback === undefined ? '未记录' : source.direct_fallback ? '已回退直连' : '未回退' }}</dd><dt>共享间隔</dt><dd>{{ source.permission_config?.request_interval_seconds ?? 3 }} 秒 / 请求</dd><dt>故障切换</dt><dd>{{ source.permission_config?.failover_enabled ? '开启' : '关闭' }}</dd><dt>代理耗尽</dt><dd>{{ source.permission_config?.on_proxy_exhausted === 'direct' ? '显式回退直连' : '暂停来源' }}</dd>
        </dl>
        <p class="muted">
          代理顺序：{{ (source.permission_config?.proxy_ids || []).map(id => proxies.find(proxy => proxy.id === id)?.name || id).join(' → ') || '未绑定 · 直连' }}
        </p>
        <ElButton @click="editSource(source)">
          配置来源与出口
        </ElButton>
      </article>
    </div>
    <section class="data-panel">
      <header class="panel-heading">
        <h2>采集入口明细</h2><RouterLink to="/runs" class="text-link">
          运行与范围回补 →
        </RouterLink>
      </header>
      <ElTable :data="collectors" class="data-table" empty-text="暂无采集入口">
        <ElTableColumn label="采集器 / 入口" min-width="210">
          <template #default="{ row }">
            <strong>{{ row.name }}</strong><a v-if="externalUrl(row.entry_url)" :href="externalUrl(row.entry_url)" class="cell-subtext external-link" target="_blank" rel="noopener noreferrer">{{ row.code }} ↗</a><span class="cell-subtext">配置版本 {{ row.config_version }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="计划" min-width="140">
          <template #default="{ row }">
            {{ row.enabled ? '已开启' : '已关闭' }} · {{ row.interval_seconds }} 秒
          </template>
        </ElTableColumn>
        <ElTableColumn label="运行健康" min-width="110">
          <template #default="{ row }">
            <ElTag :type="statusType(row.status)" effect="plain">
              {{ statusLabel(row.status) }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="最近成功 / 下次计划" min-width="205">
          <template #default="{ row }">
            {{ formatDate(row.last_success_at) }}<span class="cell-subtext">{{ formatDate(row.next_run_at) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" min-width="205">
          <template #default="{ row }">
            <div class="row-actions">
              <ElButton size="small" @click="editCollector(row)">
                配置计划
              </ElButton><ElPopconfirm title="受控重启保留数据和未完成任务，确认继续？" @confirm="restart(row)">
                <template #reference>
                  <ElButton size="small" :disabled="busy">
                    重启
                  </ElButton>
                </template>
              </ElPopconfirm>
            </div>
          </template>
        </ElTableColumn>
      </ElTable>
    </section>
    <ElDialog :model-value="Boolean(sourceEdit)" title="来源与共享出口" class="operation-dialog" @close="sourceEdit = null">
      <ElAlert v-if="error" type="error" :title="error" :closable="false" class="mb-5" />
      <form class="operation-form" @submit.prevent="saveSource">
        <label>来源启用 <ElSwitch v-model="sourceForm.enabled" aria-label="来源启用" /></label>
        <label>共享请求间隔（3–3600 秒）<ElInputNumber v-model="sourceForm.request_interval_seconds" :min="3" :max="3600" aria-label="共享请求间隔" /></label>
        <label>添加代理<ElSelect v-model="proxyChoice" aria-label="添加代理" filterable><ElOption v-for="proxy in proxies.filter(p => !sourceForm.proxy_ids.includes(p.id))" :key="proxy.id" :label="`${proxy.name}${proxy.enabled ? '' : '（已禁用）'}`" :value="proxy.id" /></ElSelect></label><ElButton :disabled="!proxyChoice" @click="addProxy">
          加入队尾
        </ElButton>
        <ol class="proxy-order">
          <li v-for="(id, index) in sourceForm.proxy_ids" :key="id">
            <span>{{ proxies.find(proxy => proxy.id === id)?.name || id }}</span><div class="row-actions">
              <ElButton size="small" :disabled="index === 0" :aria-label="`上移第 ${index + 1} 个代理`" @click="moveProxy(index, -1)">
                上移
              </ElButton><ElButton size="small" :disabled="index === sourceForm.proxy_ids.length - 1" :aria-label="`下移第 ${index + 1} 个代理`" @click="moveProxy(index, 1)">
                下移
              </ElButton><ElButton size="small" type="danger" plain @click="sourceForm.proxy_ids.splice(index, 1)">
                移除
              </ElButton>
            </div>
          </li>
        </ol>
        <p class="muted">
          无绑定时默认直连。仅使用上述顺序，不会自动选择库存中其他代理。
        </p>
        <label>故障切换 <ElSwitch v-model="sourceForm.failover_enabled" aria-label="故障切换" /></label>
        <label>代理耗尽策略<ElSelect v-model="sourceForm.on_proxy_exhausted" aria-label="代理耗尽策略"><ElOption label="暂停此来源，等待人工处理" value="pause" /><ElOption label="允许回退服务器直连并告警" value="direct" /></ElSelect></label>
        <p class="muted">
          保存版本 {{ sourceEdit?.config_version }}；配置冲突时关闭此窗口并刷新后重新编辑。
        </p>
        <ElButton native-type="submit" type="primary" :loading="busy">
          保存来源配置
        </ElButton>
      </form>
    </ElDialog>
    <ElDialog :model-value="Boolean(collectorEdit)" title="采集计划" class="operation-dialog" @close="collectorEdit = null">
      <ElAlert v-if="error" type="error" :title="error" :closable="false" class="mb-5" />
      <form class="operation-form" @submit.prevent="saveCollector">
        <label>计划启用 <ElSwitch v-model="collectorForm.enabled" aria-label="计划启用" /></label><label>调度间隔（60–86400 秒）<ElInputNumber v-model="collectorForm.interval_seconds" :min="60" :max="86400" aria-label="调度间隔" /></label><label>单批最大页数<ElInputNumber v-model="collectorForm.max_pages" :min="1" :max="10000" aria-label="计划单批最大页数" /></label><p class="muted">
          关闭后不再创建新运行，已有运行继续。版本 {{ collectorEdit?.config_version }}。
        </p><ElButton native-type="submit" type="primary" :loading="busy">
          保存计划
        </ElButton>
      </form>
    </ElDialog>
  </section>
</template>
