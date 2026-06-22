import { defineStore } from 'pinia'
import type { SessionSummary, MessageItem, StateResponse } from './api'
import { fetchRoles, listSessions, fetchState } from './api'
import { getToken, clearToken } from './api'

interface ChatStoreState {
  token: string | null
  state: StateResponse | null
  roles: string[]
  currentRole: string
  currentSessionId: string | null
  sessions: SessionSummary[]
  messages: MessageItem[]
  isGenerating: boolean
  // Agent 当前正在执行的动作文案（来自 SSE 的 temporary 工具调用事件）。
  // 首 chunk 到达前用它驱动"思考中"指示器；流式中调工具时作为底部状态条。
  activity: string | null
}

export const useChatStore = defineStore('chat', {
  state: (): ChatStoreState => ({
    token: getToken(),
    state: null,
    roles: [],
    currentRole: '',
    currentSessionId: null,
    sessions: [],
    messages: [],
    isGenerating: false,
    activity: null,
  }),
  getters: {
    authenticated: (state) => !!state.token,
    sessionPreview: (state) => {
      const map: Record<string, string> = {}
      for (const s of state.sessions) {
        map[s.session_id] = s.preview
      }
      return map
    },
  },
  actions: {
    setToken(token: string) {
      this.token = token
    },
    logout() {
      this.token = null
      this.state = null
      this.roles = []
      this.currentRole = ''
      this.currentSessionId = null
      this.sessions = []
      this.messages = []
      this.isGenerating = false
      this.activity = null
      clearToken()
    },
    async loadInit(role?: string) {
      try {
        const [s, r] = await Promise.all([fetchState(), fetchRoles()])
        this.state = s
        this.roles = r.roles
        const targetRole = role || r.current_role
        this.currentRole = targetRole
        const sessionsRes = await listSessions(targetRole)
        this.sessions = sessionsRes.sessions
        return r
      } catch {
        this.logout()
        throw new Error('获取初始化数据失败')

      }
    },
    async refreshSessions(role?: string) {
      const r = role || this.currentRole
      const res = await listSessions(r)
      this.sessions = res.sessions
    },
    setCurrentSession(id: string) {
      this.currentSessionId = id
    },
    setMessages(msgs: MessageItem[]) {
      this.messages = msgs
    },
    appendAssistantChunk(text: string) {
      const last = this.messages[this.messages.length - 1]
      if (last && last.role === 'assistant') {
        last.content += text
      } else {
        this.messages.push({ role: 'assistant', content: text, prefix: '', timestamp: null })
      }
    },
    setActivity(text: string | null) {
      this.activity = text
    },
    setGenerating(v: boolean) {
      this.isGenerating = v
      // 流结束时清掉活动状态，避免上一轮的工具文案残留到下次"思考中"指示器
      if (!v) this.activity = null
    },
  },
})