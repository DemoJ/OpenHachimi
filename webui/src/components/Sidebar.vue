<template>
  <aside class="sidebar">
    <div class="sidebar-header">
      <h2>OpenHachimi</h2>
      <button class="btn-new" @click="onNew">+ 新建会话</button>
    </div>

    <div class="sidebar-section">
      <div class="sidebar-section-title">角色 · ROLES</div>
      <ul class="sidebar-list">
        <li
          v-for="r in store.roles"
          :key="r"
          :class="{ active: r === store.currentRole }"
          @click="onSwitchRole(r)"
        >{{ r }}</li>
      </ul>
    </div>

    <div class="sidebar-section">
      <div class="sidebar-section-title">历史会话 · SESSIONS</div>
    </div>
    <div class="sidebar-sessions">
      <div
        v-for="s in store.sessions"
        :key="s.session_id"
        class="session-item"
        :class="{ active: s.session_id === store.currentSessionId }"
        @click="onLoadSession(s.session_id)"
      >
        <div class="preview">{{ s.preview || '(空会话)' }}</div>
        <div class="time">{{ formatTime(s.mtime) }}</div>
      </div>
      <div v-if="store.sessions.length === 0" class="session-item">
        <div class="preview" style="color: var(--body-mid)">暂无历史会话</div>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { useChatStore } from '../store'
import { newSession, switchRole, loadSession, getSessionMessages } from '../api'

const store = useChatStore()
const emit = defineEmits<{ (e: 'role-changed' | 'session-loaded'): void }>()

function formatTime(mtime: number): string {
  const d = new Date(mtime * 1000)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  if (sameDay) {
    return `今天 ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  }
  return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
}

async function onNew() {
  try {
    const res = await newSession(store.currentRole)
    store.setCurrentSession(res.session_id)
    store.setMessages([])
    await store.refreshSessions()
    emit('session-loaded')
  } catch (e) {
    console.error(e)
  }
}

async function onSwitchRole(role: string) {
  if (role === store.currentRole) return
  try {
    const res = await switchRole(role)
    store.currentRole = res.role
    store.setCurrentSession(res.session_id)
    store.setMessages([])
    await store.refreshSessions(res.role)
    emit('role-changed')
  } catch (e) {
    console.error(e)
  }
}

async function onLoadSession(session_id: string) {
  try {
    await loadSession(store.currentRole, session_id)
    store.setCurrentSession(session_id)
    const msgs = await getSessionMessages(session_id, store.currentRole)
    store.setMessages(msgs.messages)
    emit('session-loaded')
  } catch (e) {
    console.error(e)
  }
}
</script>