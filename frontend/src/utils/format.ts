/** 文档密级、摄取状态、大小和时间的统一展示规则。 */

import type { DocumentStatus } from '../api/types'

export const CLEARANCE_LABELS = ['密级 0', '密级 1', '密级 2', '密级 3'] as const

export function clearanceLabel(level: number): string {
  return CLEARANCE_LABELS[level] ?? `密级 ${level}`
}

export function clearanceTagType(level: number): 'info' | 'primary' | 'warning' | 'danger' {
  return (['info', 'primary', 'warning', 'danger'] as const)[level] ?? 'info'
}

export const PROCESSING_STATUSES: readonly DocumentStatus[] = [
  'queued',
  'validating',
  'parsing',
  'chunking',
  'embedding',
  'indexing',
]

const STATUS_LABELS: Record<DocumentStatus, string> = {
  queued: '排队中',
  validating: '校验中',
  parsing: '解析中',
  chunking: '分块中',
  embedding: '向量化中',
  indexing: '索引中',
  completed: '已入库',
  failed: '入库失败',
}

export function statusLabel(status: DocumentStatus): string {
  return STATUS_LABELS[status] ?? status
}

export function statusTagType(status: DocumentStatus): 'success' | 'danger' | 'warning' | 'info' {
  if (status === 'completed') {
    return 'success'
  }
  if (status === 'failed') {
    return 'danger'
  }
  if (status === 'queued') {
    return 'info'
  }
  return 'warning'
}

export function isProcessing(status: DocumentStatus): boolean {
  return PROCESSING_STATUSES.includes(status)
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) {
    return '—'
  }
  if (bytes < 1024) {
    return `${bytes} B`
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return '—'
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}
