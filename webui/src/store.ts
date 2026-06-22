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
    setGenerating(v: boolean) {
      this.isGenerating = v
    },
  },
})