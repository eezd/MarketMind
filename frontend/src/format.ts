import type { BodyStatus } from './types'

export const bodyStatusLabels: Record<BodyStatus, string> = {
  pending: '等待正文',
  complete: '正文完整',
  summary_only: '仅摘要',
  paywalled: '付费内容',
  external_link: '外部链接',
  unavailable: '正文不可得',
}

const statusLabels: Record<string, string> = {
  disabled: '未启用',
  queued: '排队中',
  running: '运行中',
  waiting_login: '等待登录',
  paused: '已暂停',
  succeeded: '已完成',
  partial_failed: '部分失败',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
  accepted: '已接受 · 等待执行',
  pending: '等待处理',
  completed: '已完成',
  rejected: '已拒绝',
  healthy: '健康',
  unknown: '状态未知',
  cooling_down: '冷却中',
  unavailable: '不可用',
  authenticated: '已验证登录',
  unauthenticated: '未登录',
  qr_pending: '等待扫码',
  expired: '已过期',
  action_required: '需要官方页面交互',
  active: '未恢复',
  resolved: '已恢复',
  sent: '已送达',
  retrying: '等待重试',
  verifying: '正在验证授权',
  revoked: '已撤销',
  sending: '正在投递',
  not_configured: '未配置通知',
}

export function statusLabel(status: string) {
  return statusLabels[status] || status
}

export function statusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (['complete', 'succeeded', 'completed', 'authenticated', 'healthy', 'resolved', 'sent'].includes(status))
    return 'success'
  if (['failed', 'partial_failed', 'interrupted', 'unavailable', 'rejected'].includes(status))
    return 'danger'
  if (['running', 'waiting_login', 'queued', 'accepted', 'pending', 'qr_pending', 'verifying', 'sending', 'retrying', 'active', 'action_required', 'cooling_down'].includes(status))
    return 'warning'
  return 'info'
}

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  dateStyle: 'medium',
  timeStyle: 'medium',
  hour12: false,
  timeZone: 'Asia/Shanghai',
})

export function formatDate(value: string | null | undefined) {
  return value && Number.isFinite(Date.parse(value)) ? dateFormatter.format(new Date(value)) : '未记录'
}

export function externalUrl(value: string): string | undefined {
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined
  }
  catch {
    return undefined
  }
}
