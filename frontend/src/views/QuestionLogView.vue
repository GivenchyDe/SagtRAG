<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { request } from '../api/client'
import type { QuestionDetail, QuestionItem, QuestionPage } from '../api/types'
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
  intent_invalid_response: '意图识别返回无效',
}
const diagnosticsLabels: Record<string, string> = {
  dense_count: '向量召回数', fts_count: '全文召回数', min_relevance: '相关度阈值',
  retrieval_ms: '检索耗时（ms）', rerank_ms: '重排耗时（ms）', generation_ms: '生成耗时（ms）',
  rag_total_ms: '问答总耗时（ms）', route_ms: '路由耗时（ms）', send_ms: '发送耗时（ms）',
  generation_model: '生成模型', rerank_model: '重排模型', intent: '意图', predicted_intent: '模型预测意图',
  intent_confidence: '意图置信度', intent_source: '意图来源', intent_rule_id: '意图规则', policy_version: '策略版本',
}
const diagnosticEntries = computed(() => Object.entries(detail.value?.diagnostics ?? {})
  .filter(([key, value]) => key in diagnosticsLabels && value !== null)
  .map(([key, value]) => ({ label: diagnosticsLabels[key], value: String(value) })))
const voiceStateLabels: Record<string, string> = { downloading: '下载中', transcribing: '转写中', transcribed: '转写完成', failed: '转写失败' }
const cards = computed(() => [
  { label: '全部问答', value: stats.value.total }, { label: '排队中', value: stats.value.queued },
  { label: '处理中', value: stats.value.processing }, { label: '已送达', value: stats.value.sent }, { label: '失败', value: stats.value.failed },
])

function outcomeLabel(value: string | null) { return value ? (outcomeLabels[value] ?? value) : '—' }
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
        <el-alert v-if="detail.content_expired" title="正文已超过保留期限，仅显示任务状态。" type="info" :closable="false" />
        <template v-else>
          <h3>{{ detail.msg_type === 'voice' ? '语音转写' : '员工问题' }}</h3>
          <p class="text-block">{{ detail.question || '尚无识别文字' }}</p>
          <div v-if="detail.diagnostics.voice" class="status-line">
            <span>转写状态：{{ voiceStateLabels[detail.diagnostics.voice.state] ?? detail.diagnostics.voice.state }}</span>
            <span v-if="detail.diagnostics.voice.duration_seconds">时长 {{ detail.diagnostics.voice.duration_seconds }} 秒</span>
            <span v-if="detail.diagnostics.voice.asr_ms !== undefined">转写耗时 {{ detail.diagnostics.voice.asr_ms }} ms</span>
          </div>
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
        <p>结果：{{ outcomeLabel(detail.outcome) }} · 错误信息：{{ errorLabel(detail.error_code || detail.diagnostics.error_code || detail.diagnostics.voice?.error_code) }}</p>
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
@media (max-width: 768px) { .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .diagnostic-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .search { width: 100%; } }
</style>
