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
  developer_mode: boolean
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

export type QuestionIntent = 'chitchat' | 'knowledge_query' | 'out_of_scope'

export type QuestionTiming = {
  end_to_end_ms: number | null
  queue_wait_ms: number | null
  processing_ms: number | null
  route_ms: number | null
  route_reused: boolean
  historical_route_ms: number | null
  voice_ms: number | null
  asr_ms: number | null
  embedding_ms: number | null
  dense_ms: number | null
  fts_ms: number | null
  rrf_ms: number | null
  retrieval_ms: number | null
  rerank_ms: number | null
  generation_ms: number | null
  generation_first_content_ms: number | null
  rag_total_ms: number | null
  send_ms: number | null
}

export type QuestionAttempt = {
  number: number | null
  kind: string
  state: string
  started_at: string | null
  finished_at?: string | null
  duration_ms?: number | null
  outcome?: string | null
  error_code?: string | null
}

export type QuestionSendAttempt = QuestionAttempt & { duration_ms: number | null; state: string }

export type QuestionPolicySnapshot = {
  policy_version?: string | null
  config_sha256?: string
  config?: Record<string, unknown>
  source_whitelist?: Record<string, unknown>
}

export type QuestionItem = {
  id: string; employee_name: string; msg_type: 'text' | 'voice'; question: string | null
  state: string; send_state: string; outcome: string | null; error_code: string | null
  received_at: string; first_claimed_at: string | null; completed_at: string | null
  attempt_count: number; send_attempt_count: number
  progress_state?: string; progress_attempt_count?: number; progress_sent_at?: string | null
  manual_recovery_required?: boolean
  content_expired: boolean
  intent: QuestionIntent | null; predicted_intent: QuestionIntent | null
  intent_confidence: number | null; intent_source: string | null; route_ms: number | null
  diagnostic_code: string | null; timing: QuestionTiming
}

export type QuestionPage = {
  items: QuestionItem[]; total: number; limit: number; offset: number
  stats: { total: number; queued: number; processing: number; sent: number; failed: number }
}

export type QuestionDetail = QuestionItem & {
  answer: { text: string; outcome: string; plain_text?: string | null; sources: Array<{ chunk_id: string; title: string; ordinal: number; quote: string }>
    claim_citations?: Array<{ claim_index: number; chunk_id: string; document_id: string; source_number: number; quote: string }> } | null
  diagnostics: Record<string, unknown> & { voice?: { state: string; duration_seconds?: number; asr_ms?: number; voice_ms?: number; download_ms?: number; convert_ms?: number; error_code?: string } }
  timing: QuestionTiming
  attempts: QuestionAttempt[]
  send_attempts: QuestionSendAttempt[]
  policy: QuestionPolicySnapshot | null
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
