/** 后台接口的数据类型，与后端响应保持一致。 */

export type AdminProfile = {
  id: string
  username: string
  tenant_id: string
  role: string
  is_first_login: boolean
}

export type DocumentStatus =
  | 'queued'
  | 'validating'
  | 'parsing'
  | 'chunking'
  | 'embedding'
  | 'indexing'
  | 'completed'
  | 'failed'

export type DocumentItem = {
  id: string
  title: string
  filename: string | null
  clearance_level: number
  byte_size: number | null
  uploaded_at: string
  updated_at: string
  ingestion_status: DocumentStatus
  error_summary: string | null
  job_id: string | null
}

export type DocumentPage = {
  items: DocumentItem[]
  total: number
  limit: number
  offset: number
}

export type RecentDocument = {
  id: string
  title: string
  clearance_level: number
  ingestion_status: DocumentStatus
  updated_at: string
}

export type DashboardData = {
  document_total: number
  employee_total: number
  recent_documents: RecentDocument[]
  failed_ingestion_count: number
}

export type Employee = {
  id: string
  display_name: string
  wecom_userid: string | null
  clearance_level: number
  status: string
  is_bound: boolean
  created_at: string
}

export type AdminAccount = {
  id: string
  username: string
  status: string
  must_change_password: boolean
  created_at: string
  is_current: boolean
}

export type AuditEvent = {
  id: string
  admin_user_id: string | null
  action: string
  details: Record<string, unknown>
  occurred_at: string
}

export type AuditCategory = 'all' | 'login' | 'admin' | 'user' | 'document' | 'qa'

export type QuestionItem = {
  id: string; employee_name: string; msg_type: 'text' | 'voice'; question: string | null
  state: string; send_state: string; outcome: string | null; error_code: string | null
  received_at: string; completed_at: string | null; attempt_count: number; send_attempt_count: number
  content_expired: boolean
}

export type QuestionPage = {
  items: QuestionItem[]; total: number; limit: number; offset: number
  stats: { total: number; queued: number; processing: number; sent: number; failed: number }
}

export type QuestionDetail = QuestionItem & {
  answer: { text: string; outcome: string; sources: Array<{ chunk_id: string; title: string; ordinal: number; quote: string }> } | null
  diagnostics: Record<string, unknown> & { voice?: { state: string; duration_seconds?: number; asr_ms?: number; error_code?: string } }
  candidates: Array<{ chunk_id: string; title: string; ordinal: number; dense_rank: number | null; fts_rank: number | null
    rrf_rank: number; rrf_score: number; rerank_score: number | null; selected: boolean; cited: boolean }>
}

export type AuditPage = {
  total: number
  limit: number
  offset: number
  items: AuditEvent[]
}

export type UploadResult = {
  document_id: string
  job_id: string
  status: DocumentStatus
  duplicate: boolean
}
