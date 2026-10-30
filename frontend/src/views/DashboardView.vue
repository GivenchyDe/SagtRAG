<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, CirclePlus, Document, UploadFilled, User, Warning } from '@element-plus/icons-vue'

import { request } from '../api/client'
import type { DashboardData } from '../api/types'
import { clearanceLabel, formatDateTime, statusLabel, statusTagType } from '../utils/format'

const router = useRouter()
const data = ref<DashboardData | null>(null)
const loading = ref(false)
const error = ref('')
const failureAlertVisible = ref(true)

async function loadDashboard() {
  loading.value = true
  error.value = ''
  try {
    data.value = await request<DashboardData>('/api/v1/admin/dashboard')
  } catch (exception) {
    error.value = exception instanceof Error ? exception.message : '首页数据加载失败。'
  } finally {
    loading.value = false
  }
}

onMounted(loadDashboard)
</script>

<template>
  <div v-loading="loading" class="dashboard">
    <el-alert v-if="error" type="error" :title="error" :closable="false" show-icon class="block" />

    <el-alert
      v-if="failureAlertVisible && (data?.failed_ingestion_count ?? 0) > 0"
      type="warning"
      :closable="true"
      show-icon
      class="block failure-alert"
      @close="failureAlertVisible = false"
    >
      <template #title>
        有 {{ data?.failed_ingestion_count }} 份文档入库失败
        <el-button link type="primary" @click="router.push('/documents')">前往企业文档处理</el-button>
      </template>
    </el-alert>

    <section class="overview-grid">
      <el-card shadow="never" class="overview-card">
        <div class="stat-icon documents">
          <el-icon :size="22"><Document /></el-icon>
        </div>
        <div class="stat-body">
          <p class="stat-label">文档总数</p>
          <p class="stat-value">{{ data?.document_total ?? '—' }}</p>
        </div>
      </el-card>
      <el-card shadow="never" class="overview-card">
        <div class="stat-icon employees">
          <el-icon :size="22"><User /></el-icon>
        </div>
        <div class="stat-body">
          <p class="stat-label">员工总数</p>
          <p class="stat-value">{{ data?.employee_total ?? '—' }}</p>
        </div>
      </el-card>
      <el-card shadow="never" class="overview-card quick-card" @click="router.push({ path: '/documents', query: { action: 'upload' } })">
        <div class="quick-icon">
          <el-icon :size="20"><UploadFilled /></el-icon>
        </div>
        <div class="quick-text">
          <p class="quick-title">上传文档</p>
          <p class="quick-desc">上传后自动解析并入库到总知识库</p>
        </div>
        <el-icon class="quick-arrow"><ArrowRight /></el-icon>
      </el-card>
      <el-card shadow="never" class="overview-card quick-card" @click="router.push({ path: '/employees', query: { action: 'create' } })">
        <div class="quick-icon">
          <el-icon :size="20"><CirclePlus /></el-icon>
        </div>
        <div class="quick-text">
          <p class="quick-title">新增员工</p>
          <p class="quick-desc">登记员工并绑定企业微信身份</p>
        </div>
        <el-icon class="quick-arrow"><ArrowRight /></el-icon>
      </el-card>
    </section>

    <el-card shadow="never" class="recent-card">
      <template #header>
        <div class="recent-header">
          <span class="recent-header-title">最近更新的文档</span>
          <el-button link type="primary" @click="router.push('/documents')">查看全部</el-button>
        </div>
      </template>
      <p v-if="!data?.recent_documents.length" class="empty">
        <el-icon><Warning /></el-icon>
        暂无文档，先上传一份资料吧。
      </p>
      <ul v-else class="recent-list">
        <li
          v-for="document in data.recent_documents"
          :key="document.id"
          class="recent-item"
          @click="router.push('/documents')"
        >
          <span class="recent-title" :title="document.title">{{ document.title }}</span>
          <el-tag class="recent-status" :type="statusTagType(document.ingestion_status)" size="small" effect="light">
            {{ statusLabel(document.ingestion_status) }}
          </el-tag>
          <el-tag class="recent-level" size="small" effect="plain">{{ clearanceLabel(document.clearance_level) }}</el-tag>
          <span class="recent-time meta-time">{{ formatDateTime(document.updated_at) }}</span>
        </li>
      </ul>
    </el-card>
  </div>
</template>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.block {
  border-radius: 10px;
}

/* 首页四个入口共用同一网格：等宽、等高，避免两行卡片视觉不齐。 */
.overview-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  grid-auto-rows: 1fr;
  gap: 16px;
}

.overview-card {
  height: 100%;
  border-radius: 12px;
  border: 1px solid #e5e7eb;
  box-shadow: 0 6px 18px rgb(17 24 39 / 4%);
}

.overview-card :deep(.el-card__body) {
  display: flex;
  align-items: center;
  gap: 16px;
  height: 100%;
  padding: 22px 24px;
}

.stat-icon,
.quick-icon {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 48px;
  height: 48px;
  border-radius: 12px;
}

.stat-icon.documents,
.quick-icon {
  color: #007bff;
  background: #eaf4ff;
}

.stat-icon.employees {
  color: #0f9d76;
  background: #e8f7f1;
}

.stat-body,
.quick-text {
  min-width: 0;
}

.stat-label {
  margin: 0 0 6px;
  color: #6b7280;
  font-size: 14px;
}

.stat-value {
  margin: 0;
  color: #111827;
  font-size: 30px;
  font-weight: 700;
  line-height: 1.1;
}

.quick-card {
  cursor: pointer;
  transition: border-color 140ms ease, box-shadow 140ms ease;
}

.quick-card:hover {
  border-color: #b9d9ff;
  box-shadow: 0 6px 18px rgb(0 123 255 / 8%);
}

.quick-title {
  margin: 0 0 4px;
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}

.quick-desc {
  margin: 0;
  color: #6b7280;
  font-size: 13px;
}

.quick-arrow {
  margin-left: auto;
  flex-shrink: 0;
  color: #9ca3af;
}

.recent-card {
  border-radius: 12px;
  border: 1px solid #e5e7eb;
}

/* 标题栏内直接用与数据行相同的网格，列名跟在标题后面而不是另起一行。 */
.recent-card :deep(.el-card__header) {
  padding: 14px 20px;
}

.recent-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 120px 120px 120px;
  align-items: center;
  column-gap: 24px;
}

.recent-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 4px;
}

.recent-header-title {
  color: #111827;
  font-size: 15px;
  font-weight: 600;
}

.recent-card :deep(.el-card__body) {
  padding: 2px 20px 6px;
}

.recent-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.recent-item {
  padding: 8px 4px;
  border-top: 1px solid #f1f2f4;
  cursor: pointer;
}

.recent-list > .recent-item:first-child {
  border-top: none;
}

.recent-item:hover .recent-title {
  color: #007bff;
}

.recent-title {
  overflow: hidden;
  color: #1f2937;
  font-size: 14px;
  line-height: 24px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-status,
.recent-level,
.recent-time {
  justify-self: center;
}

.recent-time {
  white-space: nowrap;
}

.empty {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0;
  color: #6b7280;
  font-size: 14px;
}

@media (max-width: 768px) {
  .overview-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .recent-card :deep(.el-card__body) {
    padding-top: 6px;
  }

  .recent-item {
    grid-template-columns: minmax(0, 1fr) auto;
    row-gap: 6px;
    padding: 10px 0;
  }

  .recent-status,
  .recent-time {
    justify-self: end;
  }

  .recent-level {
    justify-self: start;
  }
}
</style>
