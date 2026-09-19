<script setup lang="ts">
import type { Collector, CrawlRun, CrawlTask, Page, RunLog, Source } from '../types'
import { computed, onMounted, reactive, ref } from 'vue'
import CommandLedger from '../components/CommandLedger.vue'
import { formatDate, statusLabel, statusType } from '../format'
import { beijingIso, useOperations, usePageRequests, usePolling, validRange } from '../operations'

const page = usePageRequests()
const { error } = page
const ops = useOperations(page)
const { busy, commands } = ops
const runs = ref<CrawlRun[]>([])
const collectors = ref<Collector[]>([])
const sources = ref<Source[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(false)
const selected = ref<CrawlRun | null>(null)
const detailLoading = ref(false)
const tasks = ref<CrawlTask[]>([])
const logs = ref<RunLog[]>([])
const taskCursor = ref<string | null>(null)
const logCursor = ref<string | null>(null)
const taskStatus = ref('failed')
const sourceFilter = ref('')
const runForm = reactive({ collector_id: '', run_type: 'realtime', start_at: '', end_at: '', max_pages: 100 })
const eligibleCollectors = computed(() => collectors.value.filter(item => !sourceFilter.value || item.source_id === sourceFilter.value))
let listPaged = false
let taskPaged = false
let logPaged = false
let detailGeneration = 0

async function load(more = false) {
  if (loading.value)
    return
  loading.value = true
  try {
    const query = new URLSearchParams({ limit: '50' })
    if (more && nextCursor.value)
      query.set('cursor', nextCursor.value)
    const result = await page.get<Page<CrawlRun>>(`/runs?${query}`)
    runs.value = more ? [...runs.value, ...result.items] : result.items
    nextCursor.value = result.next_cursor
    listPaged = more
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    loading.value = false
  }
}
async function taskPage(more = false) {
  if (!selected.value)
    return
  const id = selected.value.id
  const generation = detailGeneration
  const query = new URLSearchParams({ limit: '50' })
  if (taskStatus.value)
    query.set('status', taskStatus.value)
  if (more && taskCursor.value)
    query.set('cursor', taskCursor.value)
  const result = await page.get<Page<CrawlTask>>(`/runs/${id}/tasks?${query}`)
  if (selected.value?.id !== id || generation !== detailGeneration)
    return
  tasks.value = more ? [...tasks.value, ...result.items] : result.items
  taskCursor.value = result.next_cursor
  taskPaged = more
}
async function logPage(more = false) {
  if (!selected.value)
    return
  const id = selected.value.id
  const generation = detailGeneration
  const query = new URLSearchParams({ limit: '50' })
  if (more && logCursor.value)
    query.set('cursor', logCursor.value)
  const result = await page.get<Page<RunLog>>(`/runs/${id}/logs?${query}`)
  if (selected.value?.id !== id || generation !== detailGeneration)
    return
  logs.value = more ? [...logs.value, ...result.items] : result.items
  logCursor.value = result.next_cursor
  logPaged = more
}
async function refreshDetail() {
  if (!selected.value || detailLoading.value)
    return
  const id = selected.value.id
  const generation = detailGeneration
  detailLoading.value = true
  try {
    const result = await page.get<CrawlRun>(`/runs/${id}`)
    if (selected.value?.id !== id || generation !== detailGeneration)
      return
    selected.value = result
    const index = runs.value.findIndex(run => run.id === id)
    if (index !== -1)
      runs.value[index] = result
    await Promise.all([!taskPaged ? taskPage() : Promise.resolve(), !logPaged ? logPage() : Promise.resolve()])
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    detailLoading.value = false
  }
}
async function openRun(run: CrawlRun) {
  detailGeneration++
  selected.value = run
  tasks.value = []
  logs.value = []
  taskCursor.value = null
  logCursor.value = null
  taskPaged = false
  logPaged = false
  await refreshDetail()
}
async function loadTasks(more = false) {
  if (detailLoading.value)
    return
  detailGeneration++
  detailLoading.value = true
  try {
    await taskPage(more)
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    detailLoading.value = false
  }
}
async function loadLogs(more = false) {
  if (detailLoading.value)
    return
  detailLoading.value = true
  try {
    await logPage(more)
  }
  catch (cause) {
    page.report(cause)
  }
  finally {
    detailLoading.value = false
  }
}
async function createRun() {
  if (!runForm.collector_id) {
    error.value = '请选择采集入口。'
    return
  }
  if (runForm.run_type === 'backfill' && !validRange(runForm.start_at, runForm.end_at)) {
    error.value = '请填写有效的北京时间范围，结束时间必须晚于开始时间。'
    return
  }
  try {
    const payload = { run_type: runForm.run_type, max_pages: runForm.max_pages, ...(runForm.run_type === 'backfill' ? { start_at: beijingIso(runForm.start_at), end_at: beijingIso(runForm.end_at) } : {}) }
    const result = await ops.submit(`/collectors/${runForm.collector_id}/runs`, payload, runForm.run_type === 'backfill' ? '创建范围回补' : '立即实时采集')
    await load()
    if (result.run_id)
      await openRun(await page.get<CrawlRun>(`/runs/${result.run_id}`))
  }
  catch (cause) {
    page.report(cause)
  }
}
async function control(action: 'pause' | 'resume' | 'cancel') {
  if (!selected.value)
    return
  try {
    await ops.submit(`/runs/${selected.value.id}/${action}`, undefined, `${{ pause: '暂停', resume: '恢复', cancel: '取消' }[action]}运行`)
  }
  catch (cause) {
    page.report(cause)
  }
}
async function retry(task: CrawlTask) {
  try {
    await ops.submit(`/tasks/${task.id}/retry`, undefined, '重试失败任务')
  }
  catch (cause) {
    page.report(cause)
  }
}
function closeRun() {
  selected.value = null
  detailGeneration++
}
onMounted(async () => {
  try {
    ;[collectors.value, sources.value] = await Promise.all([page.all<Collector>('/collectors'), page.all<Source>('/sources')])
    await load()
  }
  catch (cause) {
    page.report(cause)
  }
})
usePolling(async () => {
  if (!listPaged)
    await load()
  await refreshDetail()
})
</script>

<template>
  <section :aria-busy="loading">
    <header class="page-heading">
      <div>
        <p class="eyebrow">
          EXECUTION LEDGER
        </p><h1>运行与恢复</h1><p class="muted">
          真实运行账本 · 每 5 秒刷新当前页与运行详情；历史翻页保留位置。
        </p>
      </div><ElButton :loading="loading" @click="load()">
        刷新记录
      </ElButton>
    </header>
    <ElAlert v-if="error" type="error" :closable="false" :title="error" show-icon class="mb-5" role="alert" />
    <CommandLedger :commands="commands" />
    <section class="data-panel mb-5">
      <header class="panel-heading">
        <h2>发起采集</h2><span class="muted">实时与回补独立记账</span>
      </header>
      <form class="filter-bar" @submit.prevent="createRun">
        <div class="filter-field">
          <label for="run-source">来源</label><ElSelect id="run-source" v-model="sourceFilter" aria-label="来源" @change="runForm.collector_id = ''">
            <ElOption label="全部来源" value="" /><ElOption v-for="source in sources" :key="source.id" :value="source.id" :label="source.name" />
          </ElSelect>
        </div>
        <div class="filter-field">
          <label for="run-collector">采集入口</label><ElSelect id="run-collector" v-model="runForm.collector_id" aria-label="采集入口">
            <ElOption v-for="collector in eligibleCollectors" :key="collector.id" :value="collector.id" :label="collector.name" />
          </ElSelect>
        </div>
        <div class="filter-field">
          <label for="run-type">任务类型</label><ElSelect id="run-type" v-model="runForm.run_type" aria-label="任务类型">
            <ElOption label="立即实时采集" value="realtime" /><ElOption label="按范围回补" value="backfill" />
          </ElSelect>
        </div>
        <div v-if="runForm.run_type === 'backfill'" class="filter-field">
          <label for="run-start">开始时间（北京时间 UTC+8）</label><input id="run-start" v-model="runForm.start_at" class="native-input" type="datetime-local" required>
        </div>
        <div v-if="runForm.run_type === 'backfill'" class="filter-field">
          <label for="run-end">结束时间（不含，UTC+8）</label><input id="run-end" v-model="runForm.end_at" class="native-input" type="datetime-local" required>
        </div>
        <div class="filter-field">
          <label for="run-pages">单批最大页数</label><ElInputNumber id="run-pages" v-model="runForm.max_pages" :min="1" :max="10000" aria-label="单批最大页数" />
        </div>
        <ElButton type="primary" native-type="submit" :loading="busy">
          提交运行
        </ElButton>
        <p class="form-hint">
          回补按 [开始时间, 结束时间) 处理；覆盖结果以运行详情为准。提交只表示接受，执行结果见操作回执。
        </p>
      </form>
    </section>
    <section class="data-panel">
      <header class="panel-heading">
        <h2>运行历史</h2><span class="muted">选择运行查看统计、任务与日志</span>
      </header>
      <ElTable :data="runs" class="data-table" empty-text="尚无运行记录">
        <ElTableColumn label="运行 / 采集器" min-width="290">
          <template #default="{ row }">
            <ElButton link type="primary" :disabled="detailLoading" @click="openRun(row)">
              {{ collectors.find(item => item.id === row.collector_id)?.name || row.collector_id }}
            </ElButton><span class="cell-subtext mono">{{ row.id }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="状态" min-width="130">
          <template #default="{ row }">
            <ElTag :type="statusType(row.status)" effect="plain">
              {{ statusLabel(row.status) }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="类型" min-width="100">
          <template #default="{ row }">
            {{ row.run_type === 'backfill' ? '历史回补' : '实时采集' }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="开始 / 结束" min-width="205">
          <template #default="{ row }">
            {{ formatDate(row.started_at) }}<span class="cell-subtext">{{ formatDate(row.finished_at) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="最后心跳" min-width="190">
          <template #default="{ row }">
            {{ formatDate(row.heartbeat_at) }}
          </template>
        </ElTableColumn>
      </ElTable><footer class="list-footer">
        <span>已加载 {{ runs.length }} 条</span><ElButton v-if="nextCursor" :loading="loading" @click="load(true)">
          加载更多
        </ElButton>
      </footer>
    </section>
    <ElDrawer :model-value="Boolean(selected)" title="运行详情" class="run-drawer" size="min(100%, 1000px)" @close="closeRun">
      <template v-if="selected">
        <ElAlert v-if="error" type="error" :title="error" :closable="false" class="mb-5" />
        <CommandLedger :commands="commands" />
        <div class="detail-heading">
          <div>
            <p class="mono">
              {{ selected.id }}
            </p><ElTag :type="statusType(selected.status)">
              {{ statusLabel(selected.status) }}
            </ElTag>
          </div><ElButton :loading="detailLoading" @click="refreshDetail">
            刷新详情
          </ElButton>
        </div>
        <div class="row-actions mt-4">
          <ElButton :disabled="busy || !['queued', 'running', 'waiting_login'].includes(selected.status)" @click="control('pause')">
            暂停
          </ElButton><ElButton type="primary" :disabled="busy || !['paused', 'waiting_login', 'interrupted'].includes(selected.status)" @click="control('resume')">
            恢复
          </ElButton><ElPopconfirm title="取消本次运行？已入库数据保留，未来计划不受影响。" @confirm="control('cancel')">
            <template #reference>
              <ElButton type="danger" plain :disabled="busy || ['succeeded', 'failed', 'cancelled'].includes(selected.status)">
                取消运行
              </ElButton>
            </template>
          </ElPopconfirm>
        </div>
        <dl class="detail-list">
          <dt>最后心跳</dt><dd>{{ formatDate(selected.heartbeat_at) }}</dd><dt>请求范围</dt><dd>{{ formatDate(selected.range_start_at) }} → {{ formatDate(selected.range_end_at) }}（不含结束）</dd>
        </dl>
        <section class="detail-section">
          <h2>统计与进度</h2><dl v-if="Object.keys(selected.statistics || {}).length" class="detail-list">
            <template v-for="(value, key) in selected.statistics" :key="key">
              <dt>{{ key }}</dt><dd>{{ value }}</dd>
            </template>
          </dl><p v-else class="muted">
            尚未产生统计
          </p>
        </section>
        <details class="detail-section">
          <summary>持久化检查点</summary><pre class="json-view">{{ JSON.stringify(selected.checkpoint || {}, null, 2) }}</pre>
        </details>
        <section class="detail-section">
          <h2>覆盖缺口</h2><pre v-if="selected.coverage_gaps?.length" class="json-view">{{ JSON.stringify(selected.coverage_gaps, null, 2) }}</pre><p v-else class="muted">
            尚未记录覆盖缺口；不等于已完成全部范围。
          </p>
        </section>
        <section class="detail-section">
          <div class="panel-heading">
            <h2>任务账本</h2><ElSelect v-model="taskStatus" aria-label="任务状态" class="task-status-filter" :disabled="detailLoading" @change="loadTasks()">
              <ElOption label="失败任务" value="failed" /><ElOption label="全部任务" value="" /><ElOption label="排队中" value="queued" /><ElOption label="运行中" value="running" /><ElOption label="等待登录" value="waiting_login" />
            </ElSelect>
          </div>
          <ElTable :data="tasks" class="data-table" empty-text="没有符合条件的任务">
            <ElTableColumn label="任务" min-width="240">
              <template #default="{ row }">
                <span class="mono">{{ row.id }}</span><span class="cell-subtext">{{ row.task_type }} · 尝试 {{ row.attempt }} · generation {{ row.generation }}</span><span class="cell-subtext">{{ row.error_code || '未记录错误' }} {{ row.error_details?.message || '' }}</span>
              </template>
            </ElTableColumn><ElTableColumn label="状态" min-width="115">
              <template #default="{ row }">
                {{ statusLabel(row.status) }}
              </template>
            </ElTableColumn><ElTableColumn label="操作" width="115">
              <template #default="{ row }">
                <ElButton size="small" :disabled="busy || row.status !== 'failed'" @click="retry(row)">
                  重试失败
                </ElButton>
              </template>
            </ElTableColumn>
          </ElTable><footer class="list-footer">
            <span>{{ tasks.length }} 条</span><ElButton v-if="taskCursor" :loading="detailLoading" @click="loadTasks(true)">
              更多任务
            </ElButton>
          </footer>
        </section>
        <section class="detail-section">
          <h2>脱敏运行日志</h2><ElEmpty v-if="!logs.length" description="暂无日志事件" /><article v-for="log in logs" :key="log.id" class="log-event">
            <span class="mono">{{ formatDate(log.created_at) }}</span><strong>{{ log.action }} · {{ log.outcome }}</strong><p class="muted">
              {{ log.details?.message || log.details?.error_code || '已记录事件' }}
            </p>
          </article><ElButton v-if="logCursor" :loading="detailLoading" @click="loadLogs(true)">
            更多日志
          </ElButton>
        </section>
      </template>
    </ElDrawer>
  </section>
</template>
