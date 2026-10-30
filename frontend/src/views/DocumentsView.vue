<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { UploadFile, UploadInstance, UploadRawFile } from 'element-plus'
import { ElMessage } from 'element-plus'
import { Delete, Search, UploadFilled, WarningFilled } from '@element-plus/icons-vue'

import { request, validateUploadFile } from '../api/client'
import type { DocumentItem, DocumentPage, UploadResult } from '../api/types'
import {
  CLEARANCE_LABELS,
  clearanceLabel,
  clearanceTagType,
  formatBytes,
  formatDateTime,
  isProcessing,
  statusLabel,
  statusTagType,
} from '../utils/format'

const MAX_DOCUMENTS = 200
const POLL_INTERVAL_MS = 3000

const route = useRoute()
const router = useRouter()

const documents = ref<DocumentItem[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

const sortOptions = [
  { label: '最新上传', value: 'uploaded_at:desc' },
  { label: '最早上传', value: 'uploaded_at:asc' },
]
const sortValue = ref('uploaded_at:desc')
const searchQuery = ref('')
const levelFilter = ref<'all' | 0 | 1 | 2 | 3>('all')

const hasFilters = computed(() => searchQuery.value.trim().length > 0 || levelFilter.value !== 'all')

/** 固定按密级 0–3 分组，空等级也保留区块。 */
const groups = computed(() =>
  CLEARANCE_LABELS.map((label, level) => ({
    level,
    label,
    items: documents.value.filter((document) => document.clearance_level === level),
  })),
)

/** 选择具体密级时只展示该等级区块；搜索/筛选生效时隐藏空等级，避免整屏占位。 */
const visibleGroups = computed(() => {
  const base = levelFilter.value === 'all'
    ? groups.value
    : groups.value.filter((group) => group.level === levelFilter.value)
  return hasFilters.value ? base.filter((group) => group.items.length > 0) : base
})

const uploadVisible = ref(false)
const uploadRef = ref<UploadInstance>()
const uploadFile = ref<UploadRawFile | null>(null)
const uploadLevel = ref(0)
const uploadBusy = ref(false)
const uploadError = ref('')

const deleteVisible = ref(false)
const deleteTarget = ref<DocumentItem | null>(null)
const deleting = ref(false)

let pollTimer: ReturnType<typeof setInterval> | undefined

const hasProcessing = computed(() => documents.value.some((item) => isProcessing(item.ingestion_status)))

function stopPolling() {
  if (pollTimer !== undefined) {
    clearInterval(pollTimer)
    pollTimer = undefined
  }
}

function ensurePolling() {
  if (hasProcessing.value && pollTimer === undefined) {
    pollTimer = setInterval(() => {
      void loadDocuments(true)
    }, POLL_INTERVAL_MS)
  } else if (!hasProcessing.value) {
    stopPolling()
  }
}

async function loadDocuments(silent = false) {
  if (!silent) {
    loading.value = true
  }
  error.value = ''
  try {
    const [sort, order] = sortValue.value.split(':')
    const query = new URLSearchParams({
      limit: String(MAX_DOCUMENTS),
      offset: '0',
      sort,
      order,
    })
    const keyword = searchQuery.value.trim()
    if (keyword) {
      query.set('q', keyword)
    }
    if (levelFilter.value !== 'all') {
      query.set('clearance_level', String(levelFilter.value))
    }
    const result = await request<DocumentPage>(`/api/v1/admin/documents?${query.toString()}`)
    documents.value = result.items
    total.value = result.total
    ensurePolling()
  } catch (exception) {
    if (!silent) {
      error.value = exception instanceof Error ? exception.message : '文档列表加载失败。'
    }
    stopPolling()
  } finally {
    if (!silent) {
      loading.value = false
    }
  }
}

let filterTimer: ReturnType<typeof setTimeout> | undefined

/** 搜索、密级、排序共用一个防抖加载，保证三者联动。 */
watch([searchQuery, levelFilter, sortValue], () => {
  if (filterTimer !== undefined) {
    clearTimeout(filterTimer)
  }
  filterTimer = setTimeout(() => {
    void loadDocuments()
  }, 250)
})

function clearFilters() {
  searchQuery.value = ''
  levelFilter.value = 'all'
}

watch(
  () => route.query.action,
  (action) => {
    if (action === 'upload') {
      openUploadDialog()
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  stopPolling()
  if (filterTimer !== undefined) {
    clearTimeout(filterTimer)
  }
})

function openUploadDialog() {
  uploadError.value = ''
  uploadVisible.value = true
  void router.replace({ path: '/documents' })
}

function handleFileChange(file: UploadFile) {
  const raw = file.raw
  if (!raw) {
    return
  }
  const message = validateUploadFile(raw)
  uploadError.value = message ?? ''
  uploadFile.value = message ? null : raw
}

function handleFileRemove() {
  uploadFile.value = null
  uploadError.value = ''
}

function handleExceed() {
  ElMessage.warning('一次只能上传一个文件，请先移除已选择的文件。')
}

async function submitUpload() {
  if (uploadBusy.value) {
    return
  }
  if (!uploadFile.value) {
    uploadError.value = '请选择要上传的文件。'
    return
  }
  uploadBusy.value = true
  uploadError.value = ''
  const form = new FormData()
  form.append('file', uploadFile.value)
  form.append('clearance_level', String(uploadLevel.value))
  try {
    const result = await request<UploadResult>('/api/v1/admin/documents', { method: 'POST', body: form })
    if (result.duplicate) {
      ElMessage.info('该文件已在知识库中，已显示原有任务。')
    } else {
      ElMessage.success('上传成功，正在解析入库')
    }
    closeUploadDialog()
    await loadDocuments()
  } catch (exception) {
    uploadError.value = exception instanceof Error ? exception.message : '上传失败，请重试。'
  } finally {
    uploadBusy.value = false
  }
}

function closeUploadDialog() {
  uploadVisible.value = false
  uploadRef.value?.clearFiles()
  uploadFile.value = null
  uploadLevel.value = 0
  uploadError.value = ''
}

function openDeleteDialog(document: DocumentItem) {
  deleteTarget.value = document
  deleteVisible.value = true
}

async function confirmDeleteDocument() {
  if (!deleteTarget.value || deleting.value) {
    return
  }
  deleting.value = true
  try {
    await request<void>(`/api/v1/admin/documents/${deleteTarget.value.id}`, { method: 'DELETE' })
    ElMessage.success('文档已移出知识库')
    deleteVisible.value = false
    await loadDocuments()
  } catch (exception) {
    ElMessage.error(exception instanceof Error ? exception.message : '删除失败，请稍后重试。')
  } finally {
    deleting.value = false
  }
}

void loadDocuments()
</script>

<template>
  <div class="documents">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="searchQuery"
          class="search-input"
          placeholder="搜索文档名称或文件名"
          clearable
          :prefix-icon="Search"
        />
        <el-select v-model="levelFilter" class="filter-select" aria-label="按密级筛选">
          <el-option label="全部密级" value="all" />
          <el-option v-for="(label, level) in CLEARANCE_LABELS" :key="level" :label="label" :value="level" />
        </el-select>
        <el-select v-model="sortValue" class="sort-select" aria-label="排序方式">
          <el-option v-for="option in sortOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
      </div>
      <div class="toolbar-right">
        <span class="total-hint">共 {{ total }} 份文档</span>
        <el-button type="primary" :icon="UploadFilled" @click="openUploadDialog">上传文档</el-button>
      </div>
    </div>

    <el-alert v-if="error" type="error" :title="error" :closable="false" show-icon class="block" />

    <div v-if="!loading && !total" class="empty-wrap">
      <el-empty :description="hasFilters ? '没有符合筛选条件的文档。' : '暂无文档，上传第一份企业资料吧。'">
        <el-button v-if="hasFilters" @click="clearFilters">清除筛选</el-button>
        <el-button v-else type="primary" :icon="UploadFilled" @click="openUploadDialog">上传文档</el-button>
      </el-empty>
    </div>

    <div v-else v-loading="loading" class="level-groups">
      <section v-for="group in visibleGroups" :key="group.level" class="level-section">
        <div class="level-header">
          <span class="level-title">密级 {{ group.level }}</span>
          <span class="level-count">{{ group.items.length }} 份</span>
        </div>
        <div v-if="group.items.length" class="grid">
          <el-card
            v-for="document in group.items"
            :key="document.id"
            shadow="never"
            class="doc-card"
          >
            <div class="doc-card-top">
              <div class="doc-tags">
                <el-tag :type="clearanceTagType(document.clearance_level)" size="small" effect="light">
                  {{ clearanceLabel(document.clearance_level) }}
                </el-tag>
                <el-tag :type="statusTagType(document.ingestion_status)" size="small" effect="light">
                  {{ statusLabel(document.ingestion_status) }}
                </el-tag>
              </div>
              <el-tooltip content="删除文档" placement="top">
                <el-button link class="delete-button" :icon="Delete" aria-label="删除文档" @click="openDeleteDialog(document)" />
              </el-tooltip>
            </div>
            <h3 class="doc-title" :title="document.title">{{ document.title }}</h3>
            <p v-if="document.filename" class="doc-filename">{{ document.filename }}</p>
            <div class="doc-meta">
              <span>{{ formatBytes(document.byte_size) }}</span>
              <span class="dot" aria-hidden="true">·</span>
              <span>{{ formatDateTime(document.uploaded_at) }}</span>
            </div>
            <el-alert
              v-if="document.ingestion_status === 'failed' && document.error_summary"
              type="error"
              :title="document.error_summary"
              :closable="false"
              class="doc-error"
            />
          </el-card>
        </div>
        <p v-else class="level-empty">暂无密级 {{ group.level }} 文档</p>
      </section>
    </div>

    <el-dialog v-model="deleteVisible" title="删除文档" width="460px" :close-on-click-modal="!deleting" :close-on-press-escape="!deleting" :show-close="!deleting">
      <div class="confirm-dialog-body">
        <el-icon class="confirm-dialog-icon" :size="20"><WarningFilled /></el-icon>
        <p class="confirm-dialog-text">
          删除后「{{ deleteTarget?.title ?? '' }}」将停止索引并移出列表，历史任务与操作日志仍会保留。确定删除吗？
        </p>
      </div>
      <template #footer>
        <el-button :disabled="deleting" @click="deleteVisible = false">取消</el-button>
        <el-button type="danger" :loading="deleting" @click="confirmDeleteDocument">删除</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="uploadVisible"
      title="上传文档"
      width="520px"
      :close-on-click-modal="!uploadBusy"
      :close-on-press-escape="!uploadBusy"
      :show-close="!uploadBusy"
      @closed="closeUploadDialog"
    >
      <el-form label-position="top" @submit.prevent>
        <el-form-item label="文档等级">
          <el-select v-model="uploadLevel" class="level-select">
            <el-option v-for="(label, level) in CLEARANCE_LABELS" :key="level" :label="label" :value="level" />
          </el-select>
        </el-form-item>
        <el-form-item label="选择文件">
          <el-upload
            ref="uploadRef"
            drag
            class="uploader"
            :auto-upload="false"
            :limit="1"
            accept=".pdf,.docx,.txt,.md,.markdown"
            :on-change="handleFileChange"
            :on-remove="handleFileRemove"
            :on-exceed="handleExceed"
          >
            <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
            <div class="el-upload__text">拖拽文件到此处，或 <em>点击选择</em></div>
            <template #tip>
              <div class="el-upload__tip">支持 PDF、DOCX、TXT、Markdown，单个文件不超过 20 MB。</div>
            </template>
          </el-upload>
        </el-form-item>
      </el-form>
      <el-alert v-if="uploadError" type="error" :title="uploadError" :closable="false" show-icon class="block" />
      <template #footer>
        <el-button :disabled="uploadBusy" @click="closeUploadDialog">取消</el-button>
        <el-button type="primary" :loading="uploadBusy" :disabled="!uploadFile || Boolean(uploadError)" @click="submitUpload">
          上传并入库
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.documents {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.toolbar-left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.toolbar-right {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  gap: 12px;
}

.search-input {
  width: 260px;
}

.filter-select,
.sort-select {
  width: 140px;
}

.total-hint {
  color: #6b7280;
  font-size: 13px;
}

.block {
  border-radius: 10px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
  min-height: 120px;
}

.doc-card {
  position: relative;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  transition: border-color 140ms ease, box-shadow 140ms ease;
}

.doc-card:hover {
  border-color: #b9d9ff;
  box-shadow: 0 6px 18px rgb(0 123 255 / 8%);
}

.doc-card :deep(.el-card__body) {
  padding: 18px 20px;
}

.doc-card-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}

.doc-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.delete-button {
  color: #9ca3af;
  padding: 2px;
}

.delete-button:hover {
  color: #dc2626;
}

.doc-title {
  margin: 0 0 6px;
  overflow: hidden;
  color: #111827;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow-wrap: anywhere;
}

.doc-filename {
  margin: 0 0 8px;
  overflow: hidden;
  color: #9ca3af;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.doc-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #6b7280;
  font-size: 12px;
  line-height: 24px;
}

.dot {
  color: #d1d5db;
}

.doc-error {
  margin-top: 10px;
}

/* 按密级分成四个区块，空等级也保留占位。 */
.level-groups {
  display: flex;
  flex-direction: column;
  gap: 22px;
}

.level-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.level-header {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.level-title {
  color: #111827;
  font-size: 16px;
  font-weight: 600;
}

.level-count {
  color: #9ca3af;
  font-size: 12px;
}

.level-empty {
  margin: 0;
  padding: 18px;
  border: 1px dashed #e5e7eb;
  border-radius: 12px;
  background: #ffffff;
  color: #9ca3af;
  font-size: 13px;
  text-align: center;
}

.empty-wrap {
  padding: 24px 0;
}

.level-select {
  width: 100%;
}

.uploader {
  width: 100%;
}

@media (max-width: 768px) {
  .toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .toolbar-left,
  .toolbar-right {
    width: 100%;
  }

  .search-input {
    width: 100%;
  }

  .filter-select,
  .sort-select {
    flex: 1;
    min-width: 130px;
  }
}
</style>
