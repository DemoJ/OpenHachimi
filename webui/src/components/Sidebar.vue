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
        <div class="preview-row">
          <span class="preview">{{ s.preview || '(空会话)' }}</span>
          <span class="channel-tag" v-if="s.channel">{{ channelLabel(s.channel) }}</span>
        </div>
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

const CHANNEL_LABELS: Record<string, string> = {
  webui: 'WebUI',
  cli: 'CLI',
  telegram: 'TG',
  weixin: '微信',
}

function channelLabel(code: string): string {
  return CHANNEL_LABELS[code] ?? code
}

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

<style scoped>
.preview-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.preview-row .preview {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.channel-tag {
  flex: 0 0 auto;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(120, 120, 160, 0.18);
  color: var(--body-mid, #888);
  letter-spacing: 0.04em;
}
</style>