<script setup lang="ts">
import type { TrackedCommand } from '../operations'
import { statusLabel, statusType } from '../format'

defineProps<{ commands: TrackedCommand[] }>()
</script>

<template>
  <section v-if="commands.length" class="command-ledger" aria-label="操作执行结果" aria-live="polite">
    <h2>操作回执 <span class="muted">已接受不等于执行成功 · 每 5 秒核对</span></h2>
    <article v-for="command in commands" :key="command.id" class="command-row">
      <div><strong>{{ command.label }}</strong><span class="cell-subtext mono">{{ command.id }}</span></div>
      <ElTag :type="statusType(command.status)" effect="plain">
        {{ statusLabel(command.status) }}
      </ElTag>
      <p v-if="command.poll_error" class="operation-error" role="alert">
        {{ command.poll_error }} · 将自动重试查询
      </p>
      <p v-if="command.result?.message || command.result?.error_code" class="muted">
        {{ command.result.message || command.result.error_code }}
      </p>
      <p v-if="command.last_error" class="operation-error">
        {{ command.last_error }}
      </p>
      <p v-for="(item, index) in command.endpoint === '/proxy-checks' ? command.result?.items || [] : []" :key="index" class="muted">
        <span class="mono">{{ item.source_id }}</span> · {{ statusLabel(item.status) }}<template v-if="item.error_code">
          · {{ item.error_code }}
        </template>
      </p>
    </article>
  </section>
</template>
