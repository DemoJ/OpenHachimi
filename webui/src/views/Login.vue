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
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { getToken, setToken } from '../api'
import { useChatStore } from '../store'

const token = ref('')
const error = ref('')
const loading = ref(false)
const router = useRouter()
const store = useChatStore()

// 预填 localStorage 中已有的 token（上次登录或被 Chat.vue 因临时错误踢回时保留下来的）
// —— 让用户不必反复手动粘贴；首次访问没有时输入框照样为空，不影响体验。
// 如果有旧 token，自动尝试登录一次，成功则直接进入 Chat，失败则留在登录页让用户手动重试。
onMounted(async () => {
  const saved = getToken()
  if (saved) {
    token.value = saved
    loading.value = true
    try {
      await store.loadInit()
      store.setToken(saved)
      router.push('/chat')
      return
    } catch (e) {
      // 自动登录失败——可能是服务器重启后 token 变了，也可能是临时网络问题。
      // 不清空 token，让用户看到错误后可以手动重试或修改。
      error.value = (e as Error).message || '自动登录失败，请检查令牌或服务器状态'
    } finally {
      loading.value = false
    }
  }
})

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