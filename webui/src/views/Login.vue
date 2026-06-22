<template>
  <div class="login-page">
    <div class="login-card">
      <h1>OpenHachimi</h1>
      <p>输入 HTTP API Token 以访问 WebUI</p>
      <label for="token">API Token</label>
      <input
        id="token"
        v-model="token"
        type="password"
        placeholder="在 user/config.yaml 中查看 http_api_token"
        @keyup.enter="onLogin"
        autofocus
      />
      <p v-if="error" class="error">{{ error }}</p>
      <button class="btn btn-primary" style="margin-top: 16px; width: 100%;" :disabled="loading" @click="onLogin">
        {{ loading ? '验证中...' : '登录' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { setToken } from '../api'
import { useChatStore } from '../store'

const token = ref('')
const error = ref('')
const loading = ref(false)
const router = useRouter()
const store = useChatStore()

async function onLogin() {
  if (!token.value.trim()) {
    error.value = '请输入 Token'
    return
  }
  loading.value = true
  error.value = ''
  setToken(token.value.trim())
  try {
    await store.loadInit()
    store.setToken(token.value.trim())
    router.push('/chat')
  } catch (e) {
    error.value = (e as Error).message || 'Token 无效或服务器无响应'
  } finally {
    loading.value = false
  }
}
</script>