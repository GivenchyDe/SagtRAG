<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, Plus, Search, WarningFilled } from '@element-plus/icons-vue'

import { request } from '../api/client'
import type { AdminAccount } from '../api/types'
import { formatDateTime } from '../utils/format'

const admins = ref<AdminAccount[]>([])
const loading = ref(false)
const error = ref('')

const adminSearch = ref('')
const statusFilter = ref<'all' | 'active' | 'disabled'>('all')
const sortValue = ref('desc')

const hasFilters = computed(() => adminSearch.value.trim().length > 0 || statusFilter.value !== 'all')

const sortOptions = [
  { label: '最新添加', value: 'desc' },
  { label: '最早添加', value: 'asc' },
]

const createVisible = ref(false)
const saving = ref(false)
const form = reactive({ username: '', initial_password: '', confirm_password: '' })

const confirmVisible = ref(false)
const confirmAction = ref<'disable' | 'delete'>('disable')
const confirmTarget = ref<AdminAccount | null>(null)
const confirmBusy = ref(false)

const confirmTitle = computed(() => (confirmAction.value === 'delete' ? '删除管理员' : '停用管理员'))
const confirmText = computed(() => {
  const name = confirmTarget.value?.username ?? ''
  return confirmAction.value === 'delete'
    ? `删除后「${name}」将从列表隐藏、无法登录，已签发的登录状态立即失效；操作日志会保留。确定删除吗？`
    : `停用后「${name}」将立即无法登录。确定停用吗？`
})
const confirmButtonText = computed(() => (confirmAction.value === 'delete' ? '删除' : '停用'))
const confirmButtonType = computed(() => (confirmAction.value === 'delete' ? 'danger' : 'warning'))

async function loadAdmins() {
  loading.value = true
  error.value = ''
  try {
    const query = new URLSearchParams({ order: sortValue.value })
    const keyword = adminSearch.value.trim()
    if (keyword) {
      query.set('q', keyword)
    }
    if (statusFilter.value !== 'all') {
      query.set('status', statusFilter.value)
    }
    admins.value = await request<AdminAccount[]>(`/api/v1/admin/admin-users?${query.toString()}`)
  } catch (exception) {
    error.value = exception instanceof Error ? exception.message : '管理员列表加载失败。'
  } finally {
    loading.value = false
  }
}

let filterTimer: ReturnType<typeof setTimeout> | undefined

watch([adminSearch, statusFilter, sortValue], () => {
  if (filterTimer !== undefined) {
    clearTimeout(filterTimer)
  }
  filterTimer = setTimeout(() => {
    void loadAdmins()
  }, 250)
})

onMounted(loadAdmins)

function openCreateDialog() {
  form.username = ''
  form.initial_password = ''
  form.confirm_password = ''
  createVisible.value = true
}

async function createAdmin() {
  if (saving.value) {
    return
  }
  const username = form.username.trim()
  if (!username) {
    ElMessage.warning('请输入管理员账号名。')
    return
  }
  if (form.initial_password.length < 8) {
    ElMessage.warning('初始密码至少需要 8 位。')
    return
  }
  if (form.initial_password !== form.confirm_password) {
    ElMessage.warning('两次输入的密码不一致。')
    return
  }
  saving.value = true
  try {
    await request<{ id: string; username: string }>('/api/v1/admin/admin-users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, initial_password: form.initial_password }),
    })
    ElMessage.success('管理员已创建，首次登录需修改密码')
    createVisible.value = false
    await loadAdmins()
  } catch (exception) {
    ElMessage.error(exception instanceof Error ? exception.message : '创建失败，请重试。')
  } finally {
    saving.value = false
  }
}

async function setStatus(admin: AdminAccount, status: 'active' | 'disabled'): Promise<boolean> {
  const actionText = status === 'active' ? '启用' : '停用'
  try {
    await request<void>(`/api/v1/admin/admin-users/${admin.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    })
    ElMessage.success(`已${actionText}该管理员`)
    await loadAdmins()
    return true
  } catch (exception) {
    ElMessage.error(exception instanceof Error ? exception.message : `${actionText}失败，请重试。`)
    return false
  }
}

async function toggleStatus(admin: AdminAccount) {
  if (admin.status === 'active') {
    openConfirm('disable', admin)
    return
  }
  await setStatus(admin, 'active')
}

function openConfirm(action: 'disable' | 'delete', admin: AdminAccount) {
  confirmAction.value = action
  confirmTarget.value = admin
  confirmVisible.value = true
}

async function runConfirm() {
  if (!confirmTarget.value || confirmBusy.value) {
    return
  }
  const target = confirmTarget.value
  confirmBusy.value = true
  try {
    if (confirmAction.value === 'delete') {
      await request<void>(`/api/v1/admin/admin-users/${target.id}`, { method: 'DELETE' })
      ElMessage.success('管理员已删除')
      await loadAdmins()
    } else if (!(await setStatus(target, 'disabled'))) {
      return
    }
    confirmVisible.value = false
  } catch (exception) {
    ElMessage.error(exception instanceof Error ? exception.message : '操作失败，请重试。')
  } finally {
    confirmBusy.value = false
  }
}
</script>

<template>
  <div class="admins">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="adminSearch"
          class="search-input"
          placeholder="搜索管理员账号"
          clearable
          :prefix-icon="Search"
        />
        <el-select v-model="statusFilter" class="filter-select" aria-label="按状态筛选">
          <el-option label="全部状态" value="all" />
          <el-option label="启用" value="active" />
          <el-option label="停用" value="disabled" />
        </el-select>
        <el-select v-model="sortValue" class="sort-select" aria-label="排序方式">
          <el-option v-for="option in sortOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
      </div>
      <div class="toolbar-right">
        <span class="total-hint">共 {{ admins.length }} 位管理员</span>
        <el-button type="primary" :icon="Plus" @click="openCreateDialog">新增管理员</el-button>
      </div>
    </div>

    <el-alert v-if="error" type="error" :title="error" :closable="false" show-icon class="block" />

    <el-card shadow="never" class="table-card">
      <el-table
        v-loading="loading"
        :data="admins"
        row-key="id"
        :empty-text="hasFilters ? '没有符合条件的管理员' : '暂无管理员'"
      >
        <el-table-column label="账号名" min-width="180">
          <template #default="{ row }">
            <span class="username">{{ row.username }}</span>
            <el-tag v-if="row.is_current" size="small" class="current-tag">当前账号</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small" effect="light">
              {{ row.status === 'active' ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="首次改密" width="120">
          <template #default="{ row }">
            <el-tag :type="row.must_change_password ? 'warning' : 'info'" size="small" effect="plain">
              {{ row.must_change_password ? '待修改' : '已完成' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="180">
          <template #default="{ row }"><span class="meta-time">{{ formatDateTime(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-tooltip :disabled="!row.is_current" content="不能停用当前登录账号" placement="top">
                <span>
                  <el-button
                    link
                    type="primary"
                    :disabled="row.is_current"
                    @click="toggleStatus(row)"
                  >
                    {{ row.status === 'active' ? '停用' : '启用' }}
                  </el-button>
                </span>
              </el-tooltip>
              <el-tooltip :disabled="!row.is_current" content="不能删除当前登录账号" placement="top">
                <span>
                  <el-button link type="danger" :icon="Delete" :disabled="row.is_current" @click="openConfirm('delete', row)">
                    删除
                  </el-button>
                </span>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="createVisible" title="新增管理员" width="440px" :close-on-click-modal="!saving">
      <el-form label-position="top" @submit.prevent="createAdmin">
        <el-form-item label="账号名">
          <el-input v-model="form.username" maxlength="128" placeholder="字母、数字或 _ . @ -" />
        </el-form-item>
        <el-form-item label="初始密码">
          <el-input v-model="form.initial_password" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
        <el-form-item label="确认密码">
          <el-input v-model="form.confirm_password" type="password" show-password placeholder="再次输入初始密码" />
        </el-form-item>
      </el-form>
      <p class="dialog-hint">新账号与现有管理员权限相同，登录后必须先修改密码。</p>
      <template #footer>
        <el-button :disabled="saving" @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="createAdmin">创建</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="confirmVisible" :title="confirmTitle" width="460px" :close-on-click-modal="!confirmBusy" :close-on-press-escape="!confirmBusy" :show-close="!confirmBusy">
      <div class="confirm-dialog-body">
        <el-icon class="confirm-dialog-icon" :size="20"><WarningFilled /></el-icon>
        <p class="confirm-dialog-text">{{ confirmText }}</p>
      </div>
      <template #footer>
        <el-button :disabled="confirmBusy" @click="confirmVisible = false">取消</el-button>
        <el-button :type="confirmButtonType" :loading="confirmBusy" @click="runConfirm">{{ confirmButtonText }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.admins {
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
  width: 240px;
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

.table-card {
  border-radius: 12px;
  border: 1px solid #e5e7eb;
}

.username {
  color: #111827;
  font-weight: 500;
}

.row-actions {
  display: flex;
  align-items: center;
  gap: 20px;
}

.row-actions :deep(.el-button) {
  margin-left: 0;
  padding: 0;
}

.current-tag {
  margin-left: 8px;
}

.dialog-hint {
  margin: 4px 0 0;
  color: #9ca3af;
  font-size: 12px;
  line-height: 1.6;
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
