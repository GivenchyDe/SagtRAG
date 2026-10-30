<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'

import { request } from '../api/client'
import type { AdminAccount, AuditCategory, AuditEvent, AuditPage } from '../api/types'
import { formatDateTime } from '../utils/format'

const PAGE_SIZE = 20

const events = ref<AuditEvent[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const error = ref('')
const category = ref<AuditCategory>('all')
const admins = ref<AdminAccount[]>([])
const searchQuery = ref('')

const hasFilters = computed(() => searchQuery.value.trim().length > 0 || category.value !== 'all')

const CATEGORY_OPTIONS: Array<{ label: string; value: AuditCategory }> = [
  { label: '全部操作', value: 'all' },
  { label: '登录', value: 'login' },
  { label: '管理员账号', value: 'admin' },
  { label: '员工', value: 'user' },
  { label: '文档', value: 'document' },
  { label: '问答', value: 'qa' },
]

const ACTION_LABELS: Record<string, string> = {
  'admin.login_succeeded': '登录成功',
  'admin.login_failed': '登录失败',
  'admin.created': '创建管理员',
  'admin.deleted': '删除管理员',
  'admin.status_updated': '启用/停用管理员',
  'admin.password_changed': '修改密码',
  'admin.bootstrap_created': '初始化管理员',
  'document.uploaded': '上传文档',
  'document.deleted': '删除文档',
  'user.created': '新增员工',
  'user.updated': '编辑员工',
  'user.wecom_bound': '绑定企微',
  'user.wecom_unbound': '解除企微绑定',
  'user.deleted': '删除员工',
  'qa.detail_viewed': '查看问答详情',
  'knowledge_base.created': '创建知识库',
}

function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action
}

function adminName(adminUserId: string | null): string {
  if (!adminUserId) {
    return '系统'
  }
  const matched = admins.value.find((item) => item.id === adminUserId)
  return matched?.username ?? '已删除管理员'
}

function targetLabel(event: AuditEvent): string {
  const details = event.details ?? {}
  if (typeof details.message_id === 'string') {
    return `问答 ${details.message_id.replaceAll('-', '').slice(0, 12)}`
  }
  const username = details.username
  if (typeof username === 'string' && username) {
    return username
  }
  const displayName = details.display_name
  if (typeof displayName === 'string' && displayName) {
    return displayName
  }
  const title = details.title
  if (typeof title === 'string' && title) {
    return title
  }
  const userId = details.user_id
  if (typeof userId === 'string') {
    return `员工 ${userId.slice(0, 8)}`
  }
  const documentId = details.document_id
  if (typeof documentId === 'string') {
    return `文档 ${documentId.slice(0, 8)}`
  }
  const adminId = details.admin_user_id
  if (typeof adminId === 'string') {
    const matched = admins.value.find((item) => item.id === adminId)
    return `管理员 ${matched?.username ?? adminId.slice(0, 8)}`
  }
  return '—'
}

async function loadAdmins() {
  try {
    admins.value = await request<AdminAccount[]>('/api/v1/admin/admin-users')
  } catch {
    // 操作人名称只用于展示，获取失败时回退为账号 ID。
  }
}

async function loadEvents() {
  loading.value = true
  error.value = ''
  try {
    const query = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String((page.value - 1) * PAGE_SIZE),
    })
    if (category.value !== 'all') {
      query.set('category', category.value)
    }
    const keyword = searchQuery.value.trim()
    if (keyword) {
      query.set('q', keyword)
    }
    const result = await request<AuditPage>(`/api/v1/admin/audit-events?${query.toString()}`)
    events.value = result.items
    total.value = result.total
  } catch (exception) {
    error.value = exception instanceof Error ? exception.message : '操作日志加载失败。'
  } finally {
    loading.value = false
  }
}

let filterTimer: ReturnType<typeof setTimeout> | undefined

watch([searchQuery, category], () => {
  page.value = 1
  if (filterTimer !== undefined) {
    clearTimeout(filterTimer)
  }
  filterTimer = setTimeout(() => {
    void loadEvents()
  }, 250)
})

onMounted(async () => {
  await loadAdmins()
  await loadEvents()
})

function handlePageChange(next: number) {
  page.value = next
  void loadEvents()
}
</script>

<template>
  <div class="audit-page">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="searchQuery"
          class="search-input"
          placeholder="搜索操作、操作人或对象"
          clearable
          :prefix-icon="Search"
        />
        <el-select v-model="category" class="category-select" aria-label="操作类别">
          <el-option v-for="option in CATEGORY_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <span class="total-hint">共 {{ total }} 条</span>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadEvents">刷新</el-button>
    </div>

    <el-alert v-if="error" type="error" :title="error" :closable="false" show-icon class="block" />

    <el-card shadow="never" class="table-card">
      <el-table
        v-loading="loading"
        :data="events"
        row-key="id"
        class="audit-table"
        :empty-text="hasFilters ? '没有符合条件的操作日志' : '暂无操作日志'"
      >
        <el-table-column label="序号" width="90" align="left">
          <template #default="{ $index }">{{ total - ((page - 1) * PAGE_SIZE + $index) }}</template>
        </el-table-column>
        <el-table-column label="时间" width="280">
          <template #default="{ row }"><span class="meta-time">{{ formatDateTime(row.occurred_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="270">
          <template #default="{ row }">{{ actionLabel(row.action) }}</template>
        </el-table-column>
        <el-table-column label="操作人" width="260">
          <template #default="{ row }">{{ adminName(row.admin_user_id) }}</template>
        </el-table-column>
        <el-table-column label="对象" min-width="180">
          <template #default="{ row }">{{ targetLabel(row) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <div v-if="total > PAGE_SIZE" class="pagination">
      <el-pagination
        :current-page="page"
        :page-size="PAGE_SIZE"
        :total="total"
        layout="prev, pager, next, total"
        background
        @current-change="handlePageChange"
      />
    </div>
  </div>
</template>

<style scoped>
.audit-page {
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

.search-input {
  width: 280px;
}

.category-select {
  width: 150px;
}

.total-hint {
  color: #6b7280;
  font-size: 13px;
}

.block {
  border-radius: 10px;
}

.table-card {
  border-radius: 12px;
  border: 1px solid #e5e7eb;
}

.pagination {
  display: flex;
  justify-content: flex-end;
}

@media (max-width: 768px) {
  .toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .pagination {
    justify-content: center;
  }
}
</style>
