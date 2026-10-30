<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Delete, Edit, Link, Plus, Search, WarningFilled } from '@element-plus/icons-vue'

import { request } from '../api/client'
import type { Employee } from '../api/types'
import { CLEARANCE_LABELS, clearanceLabel, clearanceTagType, formatDateTime } from '../utils/format'

const route = useRoute()
const router = useRouter()

const employees = ref<Employee[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

const employeeSearch = ref('')
const levelFilter = ref<'all' | 0 | 1 | 2 | 3>('all')
const sortValue = ref('desc')

const hasFilters = computed(() => employeeSearch.value.trim().length > 0 || levelFilter.value !== 'all')

const sortOptions = [
  { label: '最新添加', value: 'desc' },
  { label: '最早添加', value: 'asc' },
]

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingId = ref('')
const saving = ref(false)
const form = reactive({ display_name: '', clearance_level: 0, wecom_userid: '' })

const bindVisible = ref(false)
const bindTarget = ref<Employee | null>(null)
const bindValue = ref('')
const binding = ref(false)
const unbinding = ref(false)

const deleteVisible = ref(false)
const deleteTarget = ref<Employee | null>(null)
const deleting = ref(false)

async function loadEmployees() {
  loading.value = true
  error.value = ''
  try {
    const query = new URLSearchParams({ limit: '200', offset: '0', order: sortValue.value })
    const keyword = employeeSearch.value.trim()
    if (keyword) {
      query.set('q', keyword)
    }
    if (levelFilter.value !== 'all') {
      query.set('clearance_level', String(levelFilter.value))
    }
    const result = await request<{ items: Employee[]; total: number }>(`/api/v1/admin/users?${query.toString()}`)
    employees.value = result.items
    total.value = result.total
  } catch (exception) {
    error.value = exception instanceof Error ? exception.message : '员工列表加载失败。'
  } finally {
    loading.value = false
  }
}

let filterTimer: ReturnType<typeof setTimeout> | undefined

watch([employeeSearch, levelFilter, sortValue], () => {
  if (filterTimer !== undefined) {
    clearTimeout(filterTimer)
  }
  filterTimer = setTimeout(() => {
    void loadEmployees()
  }, 250)
})

onMounted(loadEmployees)

watch(
  () => route.query.action,
  (action) => {
    if (action === 'create') {
      openCreateDialog()
    }
  },
  { immediate: true },
)

function openCreateDialog() {
  dialogMode.value = 'create'
  editingId.value = ''
  form.display_name = ''
  form.clearance_level = 0
  form.wecom_userid = ''
  dialogVisible.value = true
  void router.replace({ path: '/employees' })
}

function openEditDialog(employee: Employee) {
  dialogMode.value = 'edit'
  editingId.value = employee.id
  form.display_name = employee.display_name
  form.clearance_level = employee.clearance_level
  form.wecom_userid = employee.wecom_userid ?? ''
  dialogVisible.value = true
}

function openBindDialog(employee: Employee) {
  bindTarget.value = employee
  bindValue.value = employee.wecom_userid ?? ''
  bindVisible.value = true
}

async function saveEmployee() {
  if (saving.value) {
    return
  }
  const displayName = form.display_name.trim()
  if (!displayName) {
    ElMessage.warning('请输入员工姓名。')
    return
  }
  saving.value = true
  try {
    const payload = {
      display_name: displayName,
      clearance_level: form.clearance_level,
      wecom_userid: form.wecom_userid.trim() || null,
    }
    if (dialogMode.value === 'create') {
      await request<Employee>('/api/v1/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      ElMessage.success('员工已创建')
    } else {
      await request<Employee>(`/api/v1/admin/users/${editingId.value}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      ElMessage.success('员工资料已更新')
    }
    dialogVisible.value = false
    await loadEmployees()
  } catch (exception) {
    ElMessage.error(exception instanceof Error ? exception.message : '保存失败，请重试。')
  } finally {
    saving.value = false
  }
}

async function bindWecom() {
  if (!bindTarget.value || binding.value) {
    return
  }
  const userid = bindValue.value.trim()
  if (!userid) {
    ElMessage.warning('请输入企微 userid。')
    return
  }
  binding.value = true
  try {
    await request<void>(`/api/v1/admin/users/${bindTarget.value.id}/binding`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wecom_userid: userid }),
    })
    ElMessage.success('企微身份已绑定')
    bindVisible.value = false
    await loadEmployees()
  } catch (exception) {
    ElMessage.error(exception instanceof Error ? exception.message : '绑定失败，请重试。')
  } finally {
    binding.value = false
  }
}

function openDeleteDialog(employee: Employee) {
  deleteTarget.value = employee
  deleteVisible.value = true
}

async function confirmDeleteEmployee() {
  if (!deleteTarget.value || deleting.value) {
    return
  }
  deleting.value = true
  try {
    await request<void>(`/api/v1/admin/users/${deleteTarget.value.id}`, { method: 'DELETE' })
    ElMessage.success('员工已删除')
    deleteVisible.value = false
    await loadEmployees()
  } catch (exception) {
    ElMessage.error(exception instanceof Error ? exception.message : '删除失败，请重试。')
  } finally {
    deleting.value = false
  }
}

async function unbindFromDialog() {
  if (!bindTarget.value || unbinding.value) {
    return
  }
  unbinding.value = true
  try {
    await request<void>(`/api/v1/admin/users/${bindTarget.value.id}/binding`, { method: 'DELETE' })
    ElMessage.success('已解除企微绑定')
    bindVisible.value = false
    await loadEmployees()
  } catch (exception) {
    ElMessage.error(exception instanceof Error ? exception.message : '解除绑定失败，请重试。')
  } finally {
    unbinding.value = false
  }
}
</script>

<template>
  <div class="employees">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="employeeSearch"
          class="search-input"
          placeholder="搜索姓名或企微 userid"
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
        <span class="total-hint">共 {{ total }} 位员工</span>
        <el-button type="primary" :icon="Plus" @click="openCreateDialog">新增员工</el-button>
      </div>
    </div>

    <el-alert v-if="error" type="error" :title="error" :closable="false" show-icon class="block" />

    <el-card shadow="never" class="table-card">
      <el-table
        v-loading="loading"
        :data="employees"
        row-key="id"
        :empty-text="hasFilters ? '没有符合条件的员工' : '暂无员工'"
      >
        <el-table-column prop="display_name" label="姓名" min-width="140" />
        <el-table-column label="企微 userid" min-width="180">
          <template #default="{ row }">
            <span v-if="row.wecom_userid" class="mono">{{ row.wecom_userid }}</span>
            <span v-else class="muted">未绑定</span>
          </template>
        </el-table-column>
        <el-table-column label="员工密级" width="120">
          <template #default="{ row }">
            <el-tag :type="clearanceTagType(row.clearance_level)" size="small" effect="light">
              {{ clearanceLabel(row.clearance_level) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="绑定状态" width="120">
          <template #default="{ row }">
            <el-tag :type="row.is_bound ? 'success' : 'info'" size="small" effect="light">
              {{ row.is_bound ? '已绑定' : '未绑定' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }"><span class="meta-time">{{ formatDateTime(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="260" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button link type="primary" :icon="Edit" @click="openEditDialog(row)">编辑</el-button>
              <el-button link type="primary" :icon="Link" @click="openBindDialog(row)">绑定企微 ID</el-button>
              <el-button link type="danger" :icon="Delete" @click="openDeleteDialog(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新增员工' : '编辑员工'"
      width="460px"
      :close-on-click-modal="!saving"
    >
      <el-form label-position="top" @submit.prevent="saveEmployee">
        <el-form-item label="姓名">
          <el-input v-model="form.display_name" maxlength="200" placeholder="请输入员工姓名" />
        </el-form-item>
        <el-form-item label="员工密级">
          <el-select v-model="form.clearance_level" class="full-width">
            <el-option v-for="(label, level) in CLEARANCE_LABELS" :key="level" :label="label" :value="level" />
          </el-select>
        </el-form-item>
        <el-form-item label="企微 userid（可选）">
          <el-input v-model="form.wecom_userid" maxlength="128" placeholder="填写企业微信通讯录 userid" />
          <p class="field-hint">留空表示暂不绑定；绑定后该企微用户可进入问答服务。</p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="saving" @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveEmployee">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="bindVisible" title="绑定企微 ID" width="420px" :close-on-click-modal="!binding">
      <el-form label-position="top" @submit.prevent="bindWecom">
        <el-form-item :label="`员工：${bindTarget?.display_name ?? ''}`">
          <el-input v-model="bindValue" maxlength="128" placeholder="请输入企微 userid" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button v-if="bindTarget?.is_bound" link type="danger" :loading="unbinding" @click="unbindFromDialog">
            解除当前绑定
          </el-button>
          <div class="dialog-actions">
            <el-button :disabled="binding" @click="bindVisible = false">取消</el-button>
            <el-button type="primary" :loading="binding" @click="bindWecom">绑定</el-button>
          </div>
        </div>
      </template>
    </el-dialog>
    <el-dialog v-model="deleteVisible" title="删除员工" width="460px" :close-on-click-modal="!deleting" :close-on-press-escape="!deleting" :show-close="!deleting">
      <div class="confirm-dialog-body">
        <el-icon class="confirm-dialog-icon" :size="20"><WarningFilled /></el-icon>
        <p class="confirm-dialog-text">
          删除后「{{ deleteTarget?.display_name ?? '' }}」将从员工列表移除；但历史记录与操作日志仍会保留。确定删除吗？
        </p>
      </div>
      <template #footer>
        <el-button :disabled="deleting" @click="deleteVisible = false">取消</el-button>
        <el-button type="danger" :loading="deleting" @click="confirmDeleteEmployee">删除</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.employees {
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

.row-actions {
  display: flex;
  align-items: center;
  gap: 16px;
}

.row-actions :deep(.el-button) {
  margin-left: 0;
  padding: 0;
}

.dialog-footer {
  display: flex;
  align-items: center;
  gap: 12px;
}

.dialog-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto;
}

.dialog-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.block {
  border-radius: 10px;
}

.table-card {
  border-radius: 12px;
  border: 1px solid #e5e7eb;
}

.mono {
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 13px;
}

.muted {
  color: #9ca3af;
}

.full-width {
  width: 100%;
}

.field-hint {
  margin: 6px 0 0;
  color: #9ca3af;
  font-size: 12px;
  line-height: 1.5;
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
