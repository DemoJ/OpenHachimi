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
    <div class="sidebar-sessions" ref="sessionsContainer">
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
      <!--
        无限滚动触发哨兵:IntersectionObserver 监听到此 div 进入视口就调
        store.loadMoreSessions。只在 hasMoreSessions 为真时渲染,避免到底后
        仍永久占位触发(总数 == sessions.length 时该元素 v-if 直接消失,
        observer 自然失去目标)。loading 中显示占位文案,避免视觉闪烁。
      -->
      <div
        v-if="store.hasMoreSessions"
        ref="loadMoreSentinel"
        class="session-item load-more"
      >
        <div class="preview" style="color: var(--body-mid)">
          {{ store.sessionsLoading ? '加载中…' : '滚动加载更多' }}
        </div>
      </div>
    </div>

    <!-- 侧栏底部:居中设置入口。纯文字风格,弱化到几乎融入侧栏,
         默认低对比,仅 hover 时有轻微反馈,不喧宾夺主。 -->
    <div class="sidebar-footer">
      <button class="btn-settings" @click="onSettings">设置</button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useChatStore } from '../store'
import { newSession, switchRole, loadSession, getSessionMessages } from '../api'

const store = useChatStore()
const router = useRouter()
const emit = defineEmits<{ (e: 'role-changed' | 'session-loaded'): void }>()

function onSettings() {
  router.push('/settings/ai-models')
}

// 无限滚动:监听 sessionsContainer 滚动区内 loadMoreSentinel 进入视口,
// 触发 store.loadMoreSessions()。观察器只挂一次;sentinel 的出现/消失
// 跟 hasMoreSessions getter 联动,所以到达末尾后不会再触发。
const sessionsContainer = ref<HTMLElement | null>(null)
const loadMoreSentinel = ref<HTMLElement | null>(null)
let observer: IntersectionObserver | null = null

function attachObserver() {
  if (observer || !loadMoreSentinel.value) return
  observer = new IntersectionObserver(
    (entries) => {
      // 不能依赖 entries[0] —— 浏览器可能给多帧;只要任一帧 isIntersecting 就加载。
      if (entries.some((e) => e.isIntersecting)) {
        store.loadMoreSessions()
      }
    },
    {
      // root=null 默认视口;改用 sessionsContainer 作 root,在 sidebar 内部
      // 滚动场景下也能可靠触发(sidebar 自带 overflow-y:auto)。
      root: sessionsContainer.value ?? null,
      rootMargin: '64px',  // sentinel 进入视口前 64px 就预取下一页,体感无感
      threshold: 0,
    },
  )
  observer.observe(loadMoreSentinel.value)
}

function detachObserver() {
  observer?.disconnect()
  observer = null
}

// sentinel 是 v-if 控制的,首次出现 / hasMore 翻转(到底 → 又有新会话)时
// DOM ref 会变,需要重新挂载 observer。watch ref 比 nextTick 调度更精准。
watch(loadMoreSentinel, (el) => {
  detachObserver()
  if (el) attachObserver()
})

onMounted(() => {
  attachObserver()
})

onBeforeUnmount(() => {
  detachObserver()
})

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
.session-item.load-more {
  cursor: default;
  text-align: center;
  font-size: 12px;
  padding: 8px;
  opacity: 0.7;
}
.session-item.load-more:hover {
  background: transparent;
}

/* ── 侧栏底部:居中设置入口 ──
   轻量"幽灵按钮":极淡描边胶囊(比主交互 pill-border 更淡一档),默认低对比
   文字,hover 时描边+文字一起提亮并轻微填充。有实体形态但不抢主操作视觉。
   不放发丝顶线——侧栏内部靠留白分区,唯一结构分隔是右侧脊柱。 */
.sidebar-footer {
  flex-shrink: 0;
  padding: var(--sp-lg) var(--sp-xl) var(--sp-xl);
}
.btn-settings {
  display: block;
  width: 100%;
  background: transparent;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-pill);
  color: var(--body-mid);
  cursor: pointer;
  font-size: 13px;
  font-family: inherit;
  font-weight: 400;
  line-height: 20px;
  letter-spacing: 0.4px;
  text-align: center;
  padding: var(--sp-xs) 0;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.btn-settings:hover {
  color: var(--ink);
  border-color: var(--pill-border);
  background: var(--canvas-soft);
}
</style>