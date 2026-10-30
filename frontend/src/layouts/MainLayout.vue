<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Avatar, ChatDotRound, Document, House, Menu as MenuIcon, SwitchButton, Tickets, User } from '@element-plus/icons-vue'

import logoUrl from '../assets/logo.png'
import { authState, changePassword, logout } from '../auth/session'

const route = useRoute()
const router = useRouter()

const menuItems = [
  { path: '/dashboard', title: '首页', icon: House },
  { path: '/documents', title: '企业文档', icon: Document },
  { path: '/employees', title: '员工管理', icon: User },
  { path: '/audit', title: '操作日志', icon: Tickets },
  { path: '/qa', title: '问答记录', icon: ChatDotRound },
  { path: '/admins', title: '管理员账号', icon: Avatar },
]

const drawerVisible = ref(false)
const isNarrow = ref(false)
let mediaQuery: MediaQueryList | null = null

function syncMedia(query: MediaQueryListEvent | MediaQueryList) {
  isNarrow.value = query.matches
  if (!query.matches) {
    drawerVisible.value = false
  }
}

onMounted(() => {
  mediaQuery = window.matchMedia('(max-width: 768px)')
  syncMedia(mediaQuery)
  mediaQuery.addEventListener('change', syncMedia)
})

onBeforeUnmount(() => {
  mediaQuery?.removeEventListener('change', syncMedia)
})

watch(() => route.path, () => {
  drawerVisible.value = false
})

function handleLogout() {
  logout()
  void router.replace('/login')
}

const needsPasswordChange = computed(() => authState.admin?.is_first_login === true)
const passwordVisible = ref(false)
const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const passwordError = ref('')
const passwordBusy = ref(false)

watch(needsPasswordChange, (value) => {
  passwordVisible.value = value
  if (value) {
    currentPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
    passwordError.value = ''
  }
}, { immediate: true })

async function submitPasswordChange() {
  if (passwordBusy.value) {
    return
  }
  passwordError.value = ''
  if (!currentPassword.value) {
    passwordError.value = '请输入当前密码。'
    return
  }
  if (newPassword.value.length < 8) {
    passwordError.value = '新密码至少需要 8 位。'
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    passwordError.value = '两次输入的新密码不一致。'
    return
  }
  if (newPassword.value === currentPassword.value) {
    passwordError.value = '新密码不能与当前密码相同。'
    return
  }
  passwordBusy.value = true
  try {
    await changePassword(currentPassword.value, newPassword.value)
    ElMessage.success('密码已更新')
    passwordVisible.value = false
    currentPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
  } catch (exception) {
    passwordError.value = exception instanceof Error ? exception.message : '密码更新失败，请稍后重试。'
  } finally {
    passwordBusy.value = false
  }
}
</script>

<template>
  <el-container class="layout">
    <el-aside v-if="!isNarrow" width="240px" class="sidebar">
      <div class="brand">
        <img class="brand-logo" :src="logoUrl" alt="SagtRAG" />
        <span class="brand-name">SagtRAG</span>
      </div>
      <el-menu :default-active="route.path" router class="menu">
        <el-menu-item v-for="item in menuItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.title }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-drawer v-else v-model="drawerVisible" direction="ltr" size="240px" :with-header="false" class="drawer">
      <div class="brand">
        <img class="brand-logo" :src="logoUrl" alt="SagtRAG" />
        <span class="brand-name">SagtRAG</span>
      </div>
      <el-menu :default-active="route.path" router class="menu">
        <el-menu-item v-for="item in menuItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.title }}</span>
        </el-menu-item>
      </el-menu>
    </el-drawer>

    <el-container class="content-area">
      <el-header class="topbar" height="64px">
        <el-button v-if="isNarrow" text class="menu-trigger" aria-label="打开菜单" @click="drawerVisible = true">
          <el-icon :size="20"><MenuIcon /></el-icon>
        </el-button>
        <h1 class="page-title">{{ route.meta.title }}</h1>
        <div class="topbar-right">
          <div class="account-chip" :title="authState.admin?.username">
            <el-icon class="account-icon"><User /></el-icon>
            <span class="account-name">{{ authState.admin?.username }}</span>
          </div>
          <el-button plain type="danger" class="logout" @click="handleLogout">
            <el-icon><SwitchButton /></el-icon>
            退出登录
          </el-button>
        </div>
      </el-header>
      <el-main class="page">
        <router-view v-if="!needsPasswordChange" />
        <div v-else class="password-pending">请先完成初始密码修改。</div>
      </el-main>
    </el-container>

    <el-dialog
      v-model="passwordVisible"
      title="修改初始密码"
      width="460px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
    >
      <p class="password-hint">首次登录需要先设置新密码，完成后即可使用管理功能。</p>
      <el-form label-position="top" @submit.prevent="submitPasswordChange">
        <el-form-item label="当前密码">
          <el-input v-model="currentPassword" type="password" show-password placeholder="请输入初始密码" />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="newPassword" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="confirmPassword" type="password" show-password placeholder="再次输入新密码" />
        </el-form-item>
      </el-form>
      <el-alert v-if="passwordError" type="error" :title="passwordError" :closable="false" show-icon />
      <template #footer>
        <el-button type="primary" :loading="passwordBusy" @click="submitPasswordChange">保存并进入</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100vh;
  background: #f3f4f6;
}

.sidebar {
  display: flex;
  flex-direction: column;
  background: #ffffff;
  border-right: 1px solid #e5e7eb;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 64px;
  padding: 0 20px;
  border-bottom: 1px solid #f0f1f3;
}

.brand-logo {
  width: 30px;
  height: 30px;
  object-fit: contain;
}

.brand-name {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -0.3px;
  color: #111827;
}

.menu {
  border-right: none;
  padding: 8px;
  --el-menu-active-color: #007bff;
  --el-menu-hover-bg-color: #f2f7ff;
  --el-menu-item-height: 44px;
}

.menu :deep(.el-menu-item) {
  border-radius: 8px;
  margin-bottom: 4px;
  color: #4b5563;
}

.menu :deep(.el-menu-item.is-active) {
  background: #eaf4ff;
  color: #007bff;
  font-weight: 600;
}

.drawer :deep(.el-drawer__body) {
  padding: 0;
}

.content-area {
  min-width: 0;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 24px;
  background: #ffffff;
  border-bottom: 1px solid #e5e7eb;
}

.page-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #111827;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
  margin-left: auto;
}

.account-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 50vw;
  height: 32px;
  padding: 0 12px;
  border: 1px solid #cce4ff;
  border-radius: 8px;
  background: #eaf4ff;
  color: #0b5cab;
}

.account-icon {
  flex-shrink: 0;
  font-size: 15px;
}

.account-name,
.logout {
  font-size: 14px;
  font-weight: 400;
  line-height: 1;
}

.account-name {
  min-width: 0;
  overflow: hidden;
  font-size: 15px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.logout {
  flex-shrink: 0;
}

.menu-trigger {
  padding: 6px;
}

.page {
  padding: 24px;
}

.password-pending {
  display: grid;
  place-items: center;
  min-height: 50vh;
  color: #9ca3af;
  font-size: 14px;
}

.password-hint {
  margin: 0 0 16px;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

@media (max-width: 768px) {
  .topbar {
    padding: 0 12px;
  }

  .page {
    padding: 16px;
  }

  .account-chip {
    max-width: 120px;
  }

  .account-name {
    font-size: 14px;
  }
}
</style>
