import { defineStore } from 'pinia'
import type { SessionSummary, MessageItem, StateResponse } from './api'
import { fetchRoles, listSessions, fetchState, fetchChannels, getSessionMessages } from './api'
import { getToken, clearToken } from './api'

interface ChatStoreState {
  token: string | null
  state: StateResponse | null
  roles: string[]
  currentRole: string
  currentSessionId: string | null
  sessions: SessionSummary[]
  // sidebar 分页:total 由后端返回的总数,limit 是单页大小,sessions.length 当
  // 作下一页 offset。loading 防并发触发(IntersectionObserver 可能在加载完成
  // 之前再次触发)。
  sessionsTotal: number
  sessionsLimit: number
  sessionsLoading: boolean
  // 渠道筛选:WebUI 默认看 webui 渠道自己的会话;切换到 cli/telegram/weixin
  // 时筛选 sidebar 列表,并把发消息时绑定到该渠道。
  channels: string[]
  currentChannel: string
  messages: MessageItem[]
  isGenerating: boolean
  // Agent 当前正在执行的动作文案（来自 SSE 的 temporary 工具调用事件）。
  // 首 chunk 到达前用它驱动"思考中"指示器；流式中调工具时作为底部状态条。
  activity: string | null
}

const SESSIONS_PAGE_SIZE = 50

export const useChatStore = defineStore('chat', {
  state: (): ChatStoreState => ({
    token: getToken(),
    state: null,
    roles: [],
    currentRole: '',
    currentSessionId: null,
    sessions: [],
    sessionsTotal: 0,
    sessionsLimit: SESSIONS_PAGE_SIZE,
    sessionsLoading: false,
    channels: ['webui', 'cli', 'telegram', 'weixin'],
    currentChannel: 'webui',
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
    // Sidebar 用此判定是否还能继续滚加载。
    hasMoreSessions: (state) => state.sessions.length < state.sessionsTotal,
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
      this.sessionsTotal = 0
      this.sessionsLoading = false
      this.channels = ['webui', 'cli', 'telegram', 'weixin']
      this.currentChannel = 'webui'
      this.messages = []
      this.isGenerating = false
      this.activity = null
      clearToken()
    },
    async loadInit(role?: string) {
      try {
        const [s, r, ch] = await Promise.all([fetchState(), fetchRoles(), fetchChannels().catch(() => null)])
        this.state = s
        this.roles = r.roles
        const targetRole = role || r.current_role
        this.currentRole = targetRole
        if (ch && ch.channels.length > 0) {
          this.channels = ch.channels
          this.currentChannel = ch.default || 'webui'
        }
        const sessionsRes = await listSessions(targetRole, this.currentChannel, { limit: this.sessionsLimit, offset: 0 })
        this.sessions = sessionsRes.sessions
        this.sessionsTotal = sessionsRes.total ?? sessionsRes.sessions.length
        return r
      } catch {
        // 不清空 token，让上层决定如何处理。

        throw new Error('获取初始化数据失败')

      }
    },
    async refreshSessions(role?: string) {
      // 重置为第一页:角色 / 渠道 / 新建会话后都走这里。
      const r = role || this.currentRole
      const res = await listSessions(r, this.currentChannel, { limit: this.sessionsLimit, offset: 0 })
      this.sessions = res.sessions
      this.sessionsTotal = res.total ?? res.sessions.length
    },
    async loadMoreSessions() {
      // IntersectionObserver 触发的"加载下一页"。互斥锁防并发,边界保护防越界。
      if (this.sessionsLoading) return
      if (this.sessions.length >= this.sessionsTotal) return
      this.sessionsLoading = true
      try {
        const res = await listSessions(this.currentRole, this.currentChannel, {
          limit: this.sessionsLimit,
          offset: this.sessions.length,
        })
        // 用 session_id 去重防 offset 漂移时偶发的重叠条(Risks #1)。
        const seen = new Set(this.sessions.map((s) => s.session_id))
        for (const s of res.sessions) {
          if (!seen.has(s.session_id)) this.sessions.push(s)
        }
        // 后端 total 是最新真实值,以其为准
        this.sessionsTotal = res.total ?? this.sessionsTotal
      } catch (err) {
        console.warn('[store] failed to load more sessions', err)
      } finally {
        this.sessionsLoading = false
      }
    },
    setCurrentSession(id: string | null) {
      this.currentSessionId = id
    },
    setMessages(msgs: MessageItem[]) {
      this.messages = msgs
    },
    async setCurrentChannel(channel: string) {
      // 切换渠道:重新拉 sidebar 第一页,自动选中 mtime 最新一条并加载消息;
      // 列表为空时把 currentSessionId 置 null,空白页直发会自动新建一条绑该渠道。
      this.currentChannel = channel
      try {
        const res = await listSessions(this.currentRole, channel, { limit: this.sessionsLimit, offset: 0 })
        this.sessions = res.sessions
        this.sessionsTotal = res.total ?? res.sessions.length
        if (res.sessions.length > 0) {
          const top = res.sessions[0]
          this.currentSessionId = top.session_id
          try {
            const msgs = await getSessionMessages(top.session_id, this.currentRole)
            this.messages = msgs.messages
          } catch (err) {
            console.warn('[store] failed to load messages after channel switch', err)
            this.messages = []
          }
        } else {
          this.currentSessionId = null
          this.messages = []
        }
      } catch (err) {
        console.warn('[store] failed to refresh sessions after channel switch', err)
      }
    },
    appendAssistantChunk(text: string) {
      const last = this.messages[this.messages.length - 1]
      if (last && last.role === 'assistant') {
        last.content += text
      } else {
        // 首个 assistant chunk 到达：用客户端本地时间乐观打 timestamp，
        // 流结束后 syncMessagesFromServer 会用后端 ModelResponse.timestamp 覆盖。
        // tokens 此刻还拿不到（usage 只在 ModelResponse 终结时聚合），先留 null。
        this.messages.push({
          role: 'assistant',
          content: text,
          prefix: '',
          timestamp: new Date().toISOString(),
          tokens: null,
        })
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