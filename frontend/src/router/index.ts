/** 后台路由：登录页独立，其余页面共享 MainLayout。 */

import { createRouter, createWebHistory } from 'vue-router'

import { onUnauthorized } from '../api/client'
import { authState, hasToken, loadProfile, logout } from '../auth/session'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { public: true, title: '登录' },
    },
    {
      path: '/',
      component: () => import('../layouts/MainLayout.vue'),
      children: [
        { path: '', redirect: '/dashboard' },
        { path: 'dashboard', name: 'dashboard', component: () => import('../views/DashboardView.vue'), meta: { title: '首页' } },
        { path: 'documents', name: 'documents', component: () => import('../views/DocumentsView.vue'), meta: { title: '企业文档' } },
        { path: 'employees', name: 'employees', component: () => import('../views/EmployeesView.vue'), meta: { title: '员工管理' } },
        { path: 'admins', name: 'admins', component: () => import('../views/AdminsView.vue'), meta: { title: '管理员账号' } },
        { path: 'audit', name: 'audit', component: () => import('../views/AuditLogView.vue'), meta: { title: '操作日志' } },
        { path: 'qa', name: 'qa', component: () => import('../views/QuestionLogView.vue'), meta: { title: '问答记录' } },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

onUnauthorized(() => {
  logout()
  if (router.currentRoute.value.path !== '/login') {
    void router.replace('/login')
  }
})

router.beforeEach(async (to) => {
  if (!hasToken()) {
    if (to.meta.public) {
      return true
    }
    return { path: '/login', query: to.fullPath === '/login' ? undefined : { redirect: to.fullPath } }
  }
  if (!authState.admin) {
    try {
      await loadProfile()
    } catch {
      logout()
      return { path: '/login' }
    }
  }
  if (authState.admin?.is_first_login) {
    // 允许进入后台骨架，由 MainLayout 弹出强制改密框并暂缓页面数据加载。
    return to.path === '/login' ? { path: '/dashboard' } : true
  }
  if (to.path === '/login') {
    return { path: '/dashboard' }
  }
  return true
})

router.afterEach((to) => {
  const title = typeof to.meta.title === 'string' ? to.meta.title : ''
  document.title = title ? `${title} · SagtRAG 管理后台` : 'SagtRAG 管理后台'
})

export default router
