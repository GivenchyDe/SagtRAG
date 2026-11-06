<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { request } from '../api/client'
import type { QuestionDetail, QuestionItem, QuestionPage, QuestionTiming } from '../api/types'
import { formatDateTime } from '../utils/format'

const items = ref<QuestionItem[]>([])
const stats = ref({ total: 0, queued: 0, processing: 0, sent: 0, failed: 0 })
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const state = ref('')
const sendState = ref('')
const messageType = ref('')
const intentFilter = ref('')
const automatic = ref(true)
const loading = ref(false)
const error = ref('')
const detailVisible = ref(false)
const detailLoading = ref(false)
const detailError = ref('')
const detail = ref<QuestionDetail | null>(null)
const selectedId = ref('')
let requestVersion = 0
let detailVersion = 0
let filterTimer: ReturnType<typeof setTimeout> | undefined
let pollTimer: ReturnType<typeof setInterval> | undefined

const stateLabels: Record<string, string> = { received: '待处理', queued: '排队中', processing: '处理中', completed: '处理结束', failed: '失败' }
const sendLabels: Record<string, string> = { pending: '待发送', sending: '发送中', retry: '等待重试', sent: '已送达', unknown: '结果未知', cancelled: '已取消', failed: '发送失败' }
const intentLabels: Record<string, string> = { chitchat: '闲聊', knowledge_query: '知识库问答', out_of_scope: '超范围' }
const outcomeLabels: Record<string, string> = {
  answered: '已回答', no_answer: '资料不足', low_relevance: '相关度不足', invalid_citation: '引用校验未通过',
  service_error: '问答服务异常', permission_changed: '权限或资料已变化', identity_revoked: '身份已失效', answer_too_long: '需缩小问题范围',
  chitchat: '闲聊回复', out_of_scope: '超范围引导',
  voice_media_expired: '语音已过期', voice_invalid_audio: '音频格式无效', voice_no_speech: '未识别到清晰文字',
  voice_too_long: '语音超时长', voice_too_large: '语音文件过大', voice_download_error: '语音下载失败', voice_asr_error: '语音转写失败',
}
const errorLabels: Record<string, string> = {
  ...outcomeLabels, embedding_provider_error: '向量服务异常', rerank_provider_error: '重排服务异常', generation_provider_error: '生成服务异常',
  identity_revoked: '员工身份已失效', wecom_recipient_invalid: '收件人无效', wecom_send_unknown: '企微响应丢失，需核对送达情况',
  send_interrupted_unknown: '发送中断，需核对送达情况', send_attempt_limit: '已达发送重试上限', rag_processing_error: '任务处理异常',
  chitchat_generation_error: '闲聊生成失败', intent_timeout: '意图识别超时', intent_http_error: '意图识别服务异常',
  intent_invalid_response: '意图识别返回无效', delivery_unknown: '发送结果未知',
}
const diagnosticLabels: Record<string, string> = {
  answered: '回答通过校验', model_refused: '模型明确拒答', empty_claims: '模型未给出可校验结论',
  schema_invalid: '结构或 schema 不符', field_type_invalid: '字段类型不符', chunk_id_invalid: '引用 UUID 无效',
  source_not_whitelisted: '引用不在候选白名单', quote_too_short: '摘录过短', quote_mismatch: '摘录与原文不匹配',
  quote_overbroad: '摘录包含未使用的额外数字事实',
  claim_numeric_conflict: '结论与引文数字冲突', claim_unit_conflict: '结论与引文单位冲突',
  claim_negation_conflict: '结论与引文否定冲突', claim_subject_conflict: '结论与引文主体冲突',
  claim_subject_value_conflict: '结论把数值绑定到错误主体',
  claim_scope_conflict: '结论与引文范围冲突', claim_condition_conflict: '结论遗漏引文条件或例外',
  invalid_control_combination: 'refused 与 claims 组合非法', json_invalid: '模型输出不是唯一完整 JSON',
  generation_incomplete: '模型流未完整结束', generation_budget_exhausted: '生成/修复预算已耗尽',
  answered_partial: '局部保留独立有效结论',
  chitchat_output_blocked: '闲聊输出含企业事实/秘密，已整段替换',
  creation_unsupported: '新写/创作不支持', secret_request: '索要系统提示或密钥',
  personal_status_unavailable: '个人实时状态未接入', system_action: '实际操作请求',
  realtime_unavailable: '实时外部信息不可用',
  conversation_clarify: '会话信息不足，已固定澄清并保存待补字段',
  processing_deadline_exceeded: '处理期限耗尽，已停止后续外部调用',
  answer_too_long: '答案超过发送预算', no_candidates: '没有可检索候选', below_relevance_threshold: '低于重排相关度阈值',
  empty_question: '空问题', permission_changed: '处理期间权限或资料变化', identity_revoked: '员工身份已失效',
  chitchat: '闲聊回复', out_of_scope: '超范围引导', processing_exception: '处理异常',
  chitchat_generation_error: '闲聊生成失败', wecom_send_unknown: '企微响应丢失，需核对送达情况',
  send_interrupted_unknown: '发送中断，需核对送达情况', send_attempt_limit: '已达发送重试上限',
  delivery_unknown: '发送结果未知', rag_processing_error: '任务处理异常',
  ...outcomeLabels,
}
const attemptStateLabels: Record<string, string> = {
  processing: '处理中', completed: '已完成', failed: '失败', incomplete: '中断（已重试）',
}
const attemptKindLabels: Record<string, string> = { processing: '生成处理', send_retry: '复用答案重发' }
const timingDefinitions: Array<{ key: keyof QuestionTiming; label: string; scope: string; optional?: boolean }> = [
  { key: 'end_to_end_ms', label: '收到→终态', scope: '含排队、处理与发送；完成/失败/未知均记录到终态，不含企微投递到回调' },
  { key: 'queue_wait_ms', label: '首次领取前等待', scope: '收到→首次领取；后续重试不重置' },
  { key: 'processing_ms', label: '本次处理', scope: '当前/最近一次处理尝试，含语音与分类' },
  { key: 'route_ms', label: '本次分类/路由', scope: '本次实际执行的规则或分类模型；复用检查点时为空' },
  { key: 'historical_route_ms', label: '首次分类（历史）', scope: '仅分类被复用时保留首次耗时，不代表本次重新分类', optional: true },
  { key: 'voice_ms', label: '语音全阶段', scope: '下载+转码+ASR；仅语音消息' },
  { key: 'asr_ms', label: 'ASR 调用', scope: '仅语音转写请求本身' },
  { key: 'retrieval_ms', label: '检索合计', scope: '查询向量+Dense+FTS+RRF' },
  { key: 'embedding_ms', label: '查询向量', scope: '检索内的 Embedding 调用' },
  { key: 'dense_ms', label: 'Dense 检索', scope: '检索内向量距离查询' },
  { key: 'fts_ms', label: '全文检索', scope: '检索内 PostgreSQL FTS' },
  { key: 'rrf_ms', label: 'RRF 融合', scope: '检索内候选融合' },
  { key: 'rerank_ms', label: '重排', scope: '授权候选的重排调用' },
  { key: 'generation_ms', label: '生成完成', scope: '生成开始→流结束；不含校验和发送' },
  { key: 'generation_first_content_ms', label: '首个内容', scope: '生成开始→首个 content；不代表用户已经看到' },
  { key: 'rag_total_ms', label: 'RAG 总耗时', scope: 'answer_question 内部；不含排队、ASR、分类和发送' },
  { key: 'send_ms', label: '发送（最近一次）', scope: '企微发送接口调用；重试历史见发送尝试' },
]
const diagnosticsLabels: Record<string, string> = {
  dense_count: '向量召回数', fts_count: '全文召回数', min_relevance: '相关度阈值',
  retrieval_ms: '检索耗时（ms）', rerank_ms: '重排耗时（ms）', generation_ms: '生成耗时（ms）',
  rag_total_ms: '问答总耗时（ms）', route_ms: '路由耗时（ms）', send_ms: '发送耗时（ms）',
  generation_model: '生成模型', rerank_model: '重排模型', intent: '意图', predicted_intent: '模型预测意图',
  intent_confidence: '意图置信度', intent_source: '意图来源', intent_rule_id: '意图规则', policy_version: '策略版本',
  diagnostic_code: '诊断码', processing_ms: '本次处理耗时（ms）', queue_wait_ms: '首次领取前等待（ms）',
  end_to_end_ms: '收到至完成（ms）', embedding_ms: '查询向量耗时（ms）', dense_ms: 'Dense 耗时（ms）',
  fts_ms: '全文检索耗时（ms）', rrf_ms: '融合耗时（ms）', generation_first_content_ms: '首个内容耗时（ms）',
  route_reused: '分类已复用检查点', historical_route_ms: '首次分类耗时（ms）',
  generation_calls: '生成调用次数', repair_attempted: '已触发修复', repair_succeeded: '修复后通过',
  initial_error_code: '初次校验错误', final_error_code: '最终错误', claims_dropped: '局部删除结论数',
  validation_state: '最终校验状态',
}
const diagnosticEntries = computed(() => Object.entries(detail.value?.diagnostics ?? {})
  .filter(([key, value]) => key in diagnosticsLabels && value !== null)
  .map(([key, value]) => ({ label: diagnosticsLabels[key], value: String(value) })))
const timingEntries = computed(() => timingDefinitions
  .filter((item) => !item.optional || detail.value?.timing?.[item.key] != null)
  .map((item) => ({ ...item, value: formatMs(detail.value?.timing?.[item.key]) })))
const policyEntries = computed(() => {
  const policy = detail.value?.policy
  if (!policy) return []
  const entries: Array<{ label: string; value: string }> = []
  if (policy.policy_version) entries.push({ label: '策略版本', value: policy.policy_version })
  if (policy.config_sha256) entries.push({ label: '配置摘要指纹', value: policy.config_sha256.slice(0, 16) })
  for (const [key, value] of Object.entries(policy.config ?? {})) {
    entries.push({ label: key, value: String(value) })
  }
  const whitelist = policy.source_whitelist
  if (whitelist) {
    const parts = Object.entries(whitelist).map(([key, value]) => `${key}=${String(value)}`)
    entries.push({ label: '来源白名单摘要', value: parts.join(' · ') })
  }
  return entries
})
const voiceStateLabels: Record<string, string> = { downloading: '下载中', converting: '转码中', transcribing: '转写中', transcribed: '转写完成', failed: '转写失败' }
const cards = computed(() => [
  { label: '全部问答', value: stats.value.total }, { label: '排队中', value: stats.value.queued },
  { label: '处理中', value: stats.value.processing }, { label: '已送达', value: stats.value.sent }, { label: '失败', value: stats.value.failed },
])

function outcomeLabel(value: string | null) { return value ? (outcomeLabels[value] ?? value) : '—' }
function formatMs(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '未记录'
  return `${value} ms`
}
function diagnosticCodeLabel(value: unknown): string {
  if (typeof value !== 'string' || !value) return '未记录'
  return diagnosticLabels[value] ?? value
}
function errorLabel(value: unknown) {
  if (typeof value !== 'string' || !value) return '—'
  return errorLabels[value] ?? (value.startsWith('wecom_') ? `企微接口异常（${value}）` : value)
}

async function loadMessages(silent = false) {
  const version = ++requestVersion
  if (!silent) loading.value = true
  try {
    const params = new URLSearchParams({ limit: '20', offset: String((page.value - 1) * 20) })
    if (keyword.value.trim()) params.set('q', keyword.value.trim())
    if (state.value) params.set('state', state.value)
    if (sendState.value) params.set('send_state', sendState.value)
    if (messageType.value) params.set('msg_type', messageType.value)
    if (intentFilter.value) params.set('intent', intentFilter.value)
    const result = await request<QuestionPage>(`/api/v1/admin/qa-messages?${params}`)
    if (version !== requestVersion) return
    items.value = result.items
    stats.value = result.stats
    total.value = result.total
    error.value = ''
  } catch (exception) {
    if (version === requestVersion) error.value = exception instanceof Error ? exception.message : '问答记录加载失败。'
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

async function recoverUnknown() {
  if (!detail.value) return
  if (!window.confirm('仅确认企微会话没有收到最终答案后再恢复；系统不会自动重发。是否继续？')) return
  try {
    await request(`/api/v1/admin/qa-messages/${detail.value.id}/recover`, { method: 'POST' })
    await showDetail(detail.value.id)
    await loadMessages(true)
  } catch (exception) {
    detailError.value = exception instanceof Error ? exception.message : '人工恢复失败。'
  }
}

async function showDetail(id: string) {
  const version = ++detailVersion
  selectedId.value = id
  detailVisible.value = true
  detailLoading.value = true
  detailError.value = ''
  detail.value = null
  try {
    const result = await request<QuestionDetail>(`/api/v1/admin/qa-messages/${id}`)
    if (version === detailVersion) detail.value = result
  } catch (exception) {
    if (version === detailVersion) detailError.value = exception instanceof Error ? exception.message : '详情加载失败。'
  } finally {
    if (version === detailVersion) detailLoading.value = false
  }
}

watch([keyword, state, sendState, messageType, intentFilter], () => {
  page.value = 1
  clearTimeout(filterTimer)
  filterTimer = setTimeout(() => void loadMessages(), 250)
})
onMounted(() => {
  void loadMessages()
  pollTimer = setInterval(() => {
    if (automatic.value && !document.hidden && !loading.value) void loadMessages(true)
  }, 5000)
})
onBeforeUnmount(() => {
  clearTimeout(filterTimer)
  clearInterval(pollTimer)
  requestVersion++
  detailVersion++
})
</script>

<template>
  <div class="qa-page">
    <div class="stats-grid">
      <el-card v-for="card in cards" :key="card.label" shadow="never" class="stat-card">
        <div class="stat-label">{{ card.label }}</div><div class="stat-value">{{ card.value }}</div>
      </el-card>
    </div>
    <div class="qa-toolbar">
      <div class="qa-filters">
        <el-input v-model="keyword" class="search" placeholder="搜索员工、问题或问答编号" :prefix-icon="Search" clearable />
        <el-select v-model="messageType" placeholder="全部类型" clearable class="filter">
          <el-option label="文字" value="text" /><el-option label="语音" value="voice" />
        </el-select>
        <el-select v-model="intentFilter" placeholder="全部意图" clearable class="filter">
          <el-option v-for="(label, value) in intentLabels" :key="value" :label="label" :value="value" />
        </el-select>
        <el-select v-model="state" placeholder="全部处理状态" clearable class="filter">
          <el-option v-for="(label, value) in stateLabels" :key="value" :label="label" :value="value" />
        </el-select>
        <el-select v-model="sendState" placeholder="全部发送状态" clearable class="filter">
          <el-option v-for="(label, value) in sendLabels" :key="value" :label="label" :value="value" />
        </el-select>
      </div>
      <div class="refresh-controls">
        <el-checkbox v-model="automatic">自动刷新</el-checkbox>
        <el-button :icon="Refresh" :loading="loading" @click="loadMessages()">刷新</el-button>
      </div>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-card shadow="never" class="table-card">
      <el-table v-loading="loading" :data="items" row-key="id" empty-text="暂无符合条件的问答记录">
        <el-table-column label="时间" width="175"><template #default="{ row }"><span class="meta-time">{{ formatDateTime(row.received_at) }}</span></template></el-table-column>
        <el-table-column prop="employee_name" label="员工" width="110" />
        <el-table-column label="类型" width="75"><template #default="{ row }">{{ row.msg_type === 'voice' ? '语音' : '文字' }}</template></el-table-column>
        <el-table-column label="意图" width="110"><template #default="{ row }">{{ row.intent ? (intentLabels[row.intent] ?? row.intent) : '未记录' }}</template></el-table-column>
        <el-table-column label="问题" min-width="250" show-overflow-tooltip><template #default="{ row }">{{ row.content_expired ? '正文已到期清除' : (row.question || '等待语音转写') }}</template></el-table-column>
        <el-table-column label="处理状态" width="110"><template #default="{ row }"><el-tag :type="row.state === 'failed' ? 'danger' : 'info'">{{ stateLabels[row.state] ?? row.state }}</el-tag></template></el-table-column>
        <el-table-column label="回答结果" width="145"><template #default="{ row }">{{ outcomeLabel(row.outcome) }}</template></el-table-column>
        <el-table-column label="收到→终态" width="110"><template #default="{ row }">{{ formatMs(row.timing?.end_to_end_ms) }}</template></el-table-column>
        <el-table-column label="发送状态" width="110"><template #default="{ row }"><el-tag :type="row.send_state === 'sent' ? 'success' : row.send_state === 'unknown' ? 'warning' : 'info'">{{ sendLabels[row.send_state] ?? row.send_state }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="80" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="showDetail(row.id)">详情</el-button></template></el-table-column>
      </el-table>
    </el-card>
    <el-pagination v-if="total > 20" v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next, total" background @current-change="loadMessages()" />

    <el-dialog v-model="detailVisible" title="问答详情" width="min(1040px, 94vw)" top="5vh">
      <el-skeleton v-if="detailLoading" :rows="6" animated />
      <el-alert v-else-if="detailError" :title="detailError" type="error" :closable="false" />
      <div v-else-if="detail" class="detail-content">
        <div class="detail-heading">
          <span>{{ detail.employee_name }} · {{ detail.msg_type === 'voice' ? '语音问答' : '文字问答' }}</span>
          <span class="meta-time">{{ formatDateTime(detail.received_at) }}</span>
        </div>
        <div class="status-line">
          <span>问答编号：{{ detail.id.replaceAll('-', '').slice(0, 12) }}</span>
          <span>
            意图：{{ detail.intent ? (intentLabels[detail.intent] ?? detail.intent) : '未记录' }}
            <template v-if="detail.intent_confidence != null">（{{ (detail.intent_confidence * 100).toFixed(0) }}%）</template>
            <template v-if="detail.intent_source"> · {{ detail.intent_source }}</template>
          </span>
          <span>{{ stateLabels[detail.state] }} / {{ sendLabels[detail.send_state] }}</span>
          <span>处理 {{ detail.attempt_count }} 次，发送 {{ detail.send_attempt_count }} 次</span>
        </div>
        <el-alert v-if="detail.send_state === 'unknown'" title="发送结果未知，请先核对企微会话，避免重复发送。" type="warning" :closable="false" show-icon />
        <el-button v-if="detail.manual_recovery_required" type="warning" plain size="small" @click="recoverUnknown">人工确认后重新排队</el-button>
        <el-alert v-if="detail.progress_state === 'sent'" title="已发送过处理中提示；该提示不是最终答案。" type="info" :closable="false" show-icon />
        <el-alert v-if="detail.progress_state === 'unknown'" title="处理中提示可能已发送，系统不会自动重发。" type="warning" :closable="false" show-icon />
        <el-alert v-if="detail.content_expired" title="正文已超过保留期限，仅显示任务状态。" type="info" :closable="false" />
        <template v-else>
          <h3>{{ detail.msg_type === 'voice' ? '语音转写' : '员工问题' }}</h3>
          <p class="text-block">{{ detail.question || '尚无识别文字' }}</p>
          <div v-if="detail.diagnostics.voice" class="status-line">
            <span>转写状态：{{ voiceStateLabels[detail.diagnostics.voice.state] ?? detail.diagnostics.voice.state }}</span>
            <span v-if="detail.diagnostics.voice.duration_seconds">时长 {{ detail.diagnostics.voice.duration_seconds }} 秒</span>
            <span v-if="detail.diagnostics.voice.asr_ms !== undefined">转写耗时 {{ detail.diagnostics.voice.asr_ms }} ms</span>
          </div>
          <h3>耗时（各指标统计范围不同）</h3>
          <div class="timing-grid">
            <div v-for="entry in timingEntries" :key="entry.key">
              <span>{{ entry.label }}</span><strong>{{ entry.value }}</strong><small>{{ entry.scope }}</small>
            </div>
          </div>
          <template v-if="detail.attempts?.length">
            <h3>处理尝试</h3>
            <el-table :data="detail.attempts" size="small" empty-text="没有处理尝试记录">
              <el-table-column label="次数" width="60"><template #default="{ row }">{{ row.number ?? '—' }}</template></el-table-column>
              <el-table-column label="类型" width="110"><template #default="{ row }">{{ attemptKindLabels[row.kind] ?? row.kind }}</template></el-table-column>
              <el-table-column label="开始" width="165"><template #default="{ row }">{{ formatDateTime(row.started_at) }}</template></el-table-column>
              <el-table-column label="用时" width="95"><template #default="{ row }">{{ formatMs(row.duration_ms) }}</template></el-table-column>
              <el-table-column label="状态" width="110"><template #default="{ row }">{{ attemptStateLabels[row.state] ?? row.state }}</template></el-table-column>
              <el-table-column label="结果/错误" min-width="150"><template #default="{ row }">{{ row.error_code || row.outcome || '—' }}</template></el-table-column>
            </el-table>
          </template>
          <template v-if="detail.send_attempts?.length">
            <h3>发送尝试</h3>
            <el-table :data="detail.send_attempts" size="small" empty-text="没有发送尝试记录">
              <el-table-column label="次数" width="60"><template #default="{ row }">{{ row.number ?? '—' }}</template></el-table-column>
              <el-table-column label="开始" width="165"><template #default="{ row }">{{ formatDateTime(row.started_at) }}</template></el-table-column>
              <el-table-column label="用时" width="95"><template #default="{ row }">{{ formatMs(row.duration_ms) }}</template></el-table-column>
              <el-table-column label="状态" width="110"><template #default="{ row }">{{ sendLabels[row.state] ?? row.state }}</template></el-table-column>
              <el-table-column label="错误" min-width="150"><template #default="{ row }">{{ row.error_code || '—' }}</template></el-table-column>
            </el-table>
          </template>
          <template v-if="policyEntries.length">
            <h3>运行策略与白名单摘要</h3>
            <div class="diagnostic-grid">
              <div v-for="entry in policyEntries" :key="entry.label"><span>{{ entry.label }}</span><strong>{{ entry.value }}</strong></div>
            </div>
          </template>
          <h3>回答与来源</h3>
          <p class="text-block">{{ detail.answer?.text || '尚未生成答案' }}</p>
          <div v-for="(source, index) in detail.answer?.sources ?? []" :key="source.chunk_id" class="source">
            <strong>[{{ index + 1 }}] {{ source.title }} · 片段 {{ source.ordinal + 1 }}</strong>
            <p>{{ source.quote }}</p>
          </div>
          <h3>检索诊断</h3>
          <div class="diagnostic-grid"><div v-for="entry in diagnosticEntries" :key="entry.label"><span>{{ entry.label }}</span><strong>{{ entry.value }}</strong></div></div>
          <el-table :data="detail.candidates" empty-text="本次没有可展示的检索候选" size="small">
            <el-table-column prop="title" label="文档" min-width="160" show-overflow-tooltip />
            <el-table-column label="片段" width="65"><template #default="{ row }">{{ row.ordinal + 1 }}</template></el-table-column>
            <el-table-column prop="dense_rank" label="向量序" width="75" />
            <el-table-column prop="fts_rank" label="全文序" width="75" />
            <el-table-column prop="rrf_rank" label="融合序" width="75" />
            <el-table-column label="重排分数" width="100"><template #default="{ row }">{{ row.rerank_score?.toFixed(3) ?? '—' }}</template></el-table-column>
            <el-table-column label="采用情况" width="100"><template #default="{ row }">{{ row.cited ? '已引用' : row.selected ? '进入上下文' : '未采用' }}</template></el-table-column>
          </el-table>
        </template>
        <p>结果：{{ outcomeLabel(detail.outcome) }} · 诊断：{{ diagnosticCodeLabel(detail.diagnostic_code || detail.diagnostics.diagnostic_code) }} · 错误信息：{{ errorLabel(detail.error_code || detail.diagnostics.error_code || detail.diagnostics.voice?.error_code) }}</p>
      </div>
      <template #footer><el-button v-if="selectedId" :disabled="detailLoading" @click="showDetail(selectedId)">刷新详情</el-button><el-button type="primary" @click="detailVisible = false">关闭</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.qa-page, .detail-content { display: flex; flex-direction: column; gap: 16px; }
.stats-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 16px; }
.stat-card {
  border-radius: 12px;
  border: 1px solid #e5e7eb;
  box-shadow: 0 6px 18px rgb(17 24 39 / 4%);
}
.stat-card :deep(.el-card__body) { padding: 20px 24px; }
.stat-label { color: #6b7280; font-size: 13px; }
.stat-value { margin-top: 8px; font-size: 28px; font-weight: 600; }
.qa-toolbar, .qa-filters, .refresh-controls, .detail-heading, .status-line { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.qa-toolbar, .detail-heading { justify-content: space-between; }
.search { width: 270px; }
.filter { width: 150px; }
.table-card { border-radius: 12px; }
.status-line { font-size: 12px; color: #6b7280; }
h3 { font-size: 14px; margin: 8px 0 0; }
.text-block { margin: 0; padding: 14px; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.8; }
.source { border-left: 3px solid #dbeafe; padding: 8px 14px; font-size: 13px; }
.source p { margin: 8px 0 0; white-space: pre-wrap; }
.diagnostic-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.diagnostic-grid div { display: flex; flex-direction: column; gap: 5px; font-size: 12px; }
.diagnostic-grid span { color: #6b7280; }
.diagnostic-grid strong { font-weight: 500; overflow-wrap: anywhere; }
.timing-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.timing-grid div { display: flex; flex-direction: column; gap: 4px; padding: 10px 12px; border: 1px solid #eef2f7; border-radius: 8px; font-size: 12px; }
.timing-grid span { color: #6b7280; }
.timing-grid strong { font-weight: 600; font-variant-numeric: tabular-nums; }
.timing-grid small { color: #9ca3af; line-height: 1.5; }
@media (max-width: 768px) { .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .diagnostic-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .timing-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .search { width: 100%; } }
</style>
