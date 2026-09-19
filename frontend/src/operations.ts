import type { Ref } from 'vue'
import type { Command, Page } from './types'
import { onScopeDispose, ref } from 'vue'
import { ApiError, errorMessage, request } from './api'

export interface PageRequests {
  get: <T>(path: string, options?: RequestInit) => Promise<T>
  all: <T>(path: string) => Promise<T[]>
  error: Ref<string>
  report: (cause: unknown) => void
  signal: AbortSignal
}

export function usePageRequests() {
  const controller = new AbortController()
  const error = ref('')
  onScopeDispose(() => controller.abort())
  const get = <T>(path: string, options: RequestInit = {}) => request<T>(path, { ...options, signal: controller.signal })
  function report(cause: unknown) {
    if (!controller.signal.aborted)
      error.value = errorMessage(cause)
  }
  async function all<T>(path: string) {
    const items: T[] = []
    let cursor: string | null = null
    do {
      const query = new URLSearchParams({ limit: '200' })
      if (cursor)
        query.set('cursor', cursor)
      const page: Page<T> = await get(`${path}?${query}`)
      items.push(...page.items)
      cursor = page.next_cursor
    } while (cursor && !controller.signal.aborted)
    return items
  }
  return { get, all, error, report, signal: controller.signal }
}

export function usePolling(action: () => Promise<void>, interval = 5000) {
  let stopped = false
  let timer: number | undefined
  async function tick() {
    try {
      await action()
    }
    finally {
      if (!stopped)
        timer = window.setTimeout(tick, interval)
    }
  }
  timer = window.setTimeout(tick, interval)
  onScopeDispose(() => {
    stopped = true
    clearTimeout(timer)
  })
}

export interface TrackedCommand extends Command {
  label: string
  endpoint: string
  poll_error?: string
  last_error?: string | null
}

const terminal: Record<string, true> = { succeeded: true, failed: true, cancelled: true, rejected: true, expired: true, completed: true, sent: true, disabled: true, not_configured: true }

function operationKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0F) | 0x40
  bytes[8] = (bytes[8]! & 0x3F) | 0x80
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function useOperations(page: PageRequests) {
  const busy = ref(false)
  const commands = ref<TrackedCommand[]>([])
  const pendingKeys = new Map<string, string>()
  async function mutate<T>(path: string, body?: unknown, method = 'POST'): Promise<T> {
    const serialized = body === undefined ? undefined : JSON.stringify(body)
    const operation = `${method}:${path}:${serialized ?? ''}`
    const key = pendingKeys.get(operation) || operationKey()
    pendingKeys.set(operation, key)
    busy.value = true
    page.error.value = ''
    try {
      const result = await page.get<T>(path, {
        method,
        body: serialized,
        headers: { 'Idempotency-Key': key },
      })
      pendingKeys.delete(operation)
      return result
    }
    catch (cause) {
      // Uncertain transport/server failures retain the operation key for explicit retries.
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500)
        pendingKeys.delete(operation)
      throw cause
    }
    finally {
      busy.value = false
    }
  }
  function track(id: string, label: string, endpoint = '/commands') {
    if (!commands.value.some(command => command.id === id))
      commands.value.unshift({ id, label, endpoint, status: 'accepted' })
  }
  async function poll() {
    await Promise.all(commands.value.filter(command => !terminal[command.status]).map(async (command) => {
      try {
        const result = await page.get<Command>(`${command.endpoint}/${command.id}`)
        Object.assign(command, result, { poll_error: '' })
      }
      catch (cause) {
        if (!page.signal.aborted)
          command.poll_error = errorMessage(cause)
      }
    }))
  }
  async function submit(path: string, body: unknown, label: string, endpoint = '/commands') {
    const accepted = await mutate<{ command_id: string, run_id?: string }>(path, body)
    track(accepted.command_id, label, endpoint)
    return accepted
  }
  usePolling(poll)
  return { busy, commands, mutate, submit, poll }
}

export function beijingIso(value: string) {
  return new Date(`${value}:00+08:00`).toISOString()
}

export function validRange(start: string, end: string) {
  return Boolean(start && end && Number.isFinite(Date.parse(`${start}:00+08:00`)) && Date.parse(`${start}:00+08:00`) < Date.parse(`${end}:00+08:00`))
}
