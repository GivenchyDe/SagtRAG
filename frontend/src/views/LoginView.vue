<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import logoUrl from '../assets/logo.png'
import { login } from '../auth/session'

const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const showPassword = ref(false)
const error = ref('')
const busy = ref(false)

function redirectTarget(): string {
  const target = route.query.redirect
  return typeof target === 'string' && target.startsWith('/') ? target : '/dashboard'
}

async function submitLogin() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await login(username.value, password.value)
    password.value = ''
    await router.replace(redirectTarget())
  } catch (exception) {
    error.value = exception instanceof Error ? exception.message : '登录失败，请稍后重试。'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <div class="auth-content">
      <section class="auth-card" aria-labelledby="auth-title">
        <header class="auth-heading">
          <img class="auth-logo" :src="logoUrl" alt="SagtRAG" width="48" height="48" />
          <h1 id="auth-title" class="auth-brand">SagtRAG</h1>
          <p class="auth-description">登录工作区，管理企业知识库。</p>
        </header>

        <p v-if="error" id="auth-error" class="auth-error" role="alert">{{ error }}</p>

        <form class="auth-form" :aria-describedby="error ? 'auth-error' : undefined" @submit.prevent="submitLogin">
          <div class="auth-field">
            <label for="login-username">管理员账号</label>
            <div class="auth-input-wrap">
              <svg class="auth-field-icon" viewBox="0 0 24 24" fill="none" stroke="#6B7280" stroke-width="1.6" aria-hidden="true"><circle cx="12" cy="8" r="3.5" /><path d="M5 20v-2a7 7 0 0 1 14 0v2" stroke-linecap="round" /></svg>
              <input id="login-username" v-model="username" name="username" autocomplete="username" placeholder="请输入管理员账号" required :disabled="busy" spellcheck="false" autocapitalize="none" />
            </div>
          </div>
          <div class="auth-field">
            <label for="login-password">密码</label>
            <div class="auth-input-wrap">
              <svg class="auth-field-icon" viewBox="0 0 24 24" fill="none" stroke="#6B7280" stroke-width="1.6" aria-hidden="true"><rect x="5" y="10" width="14" height="10" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" /><path d="M12 14v2" stroke-linecap="round" /></svg>
              <input id="login-password" v-model="password" name="password" :type="showPassword ? 'text' : 'password'" autocomplete="current-password" placeholder="请输入密码" required :disabled="busy" class="auth-password" />
              <button type="button" class="auth-visibility" :aria-label="showPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showPassword" :disabled="busy" @click="showPassword = !showPassword">
                <svg viewBox="0 0 24 24" fill="none" stroke="#6B7280" stroke-width="1.6" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" /><circle cx="12" cy="12" r="3" /><path v-if="showPassword" d="m3 3 18 18" stroke-linecap="round" /></svg>
              </button>
            </div>
          </div>
          <button class="auth-submit" type="submit" :disabled="busy" :aria-busy="busy">
            <span>{{ busy ? '正在登录…' : '登录' }}</span>
            <svg v-if="!busy" viewBox="0 0 24 24" fill="none" stroke="#FFFFFF" stroke-width="1.8" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6" stroke-linecap="round" stroke-linejoin="round" /></svg>
          </button>
        </form>
      </section>
      <footer class="auth-footer">SagtRAG <span aria-hidden="true">·</span> 企业知识库管理平台</footer>
    </div>
  </main>
</template>
