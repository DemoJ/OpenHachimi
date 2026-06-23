<template>
  <div class="login-page">
    <div class="login-card">
      <h1>OpenHachimi</h1>
      <p>请使用访问令牌（http_api_token）登录</p>
      <input
        id="token"
        v-model="token"
        type="password"
        placeholder="请输入访问令牌"
        @keyup.enter="onLogin"
        autofocus
      />
      <p class="hint-mono">user/config.yaml → http_api_token</p>
      <p v-if="error" class="error">{{ error }}</p>
      <button class="btn btn-primary" :disabled="loading" @click="onLogin">
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
    error.value = '请输入访问令牌（HTTP API Token）'
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
    error.value = (e as Error).message || '访问令牌（HTTP API Token）无效或服务器无响应'
  } finally {
    loading.value = false
  }
}
</script>