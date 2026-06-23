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

export const useChatStore = defineStore('chat', {
  state: (): ChatStoreState => ({
    token: getToken(),
    state: null,
    roles: [],
    currentRole: '',
    currentSessionId: null,
    sessions: [],
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
        const sessionsRes = await listSessions(targetRole, this.currentChannel)
        this.sessions = sessionsRes.sessions
        return r
      } catch {
        // 不清空 token，让上层决定如何处理。

        throw new Error('获取初始化数据失败')

      }
    },
    async refreshSessions(role?: string) {
      const r = role || this.currentRole
      const res = await listSessions(r, this.currentChannel)
      this.sessions = res.sessions
    },
    setCurrentSession(id: string | null) {
      this.currentSessionId = id
    },
    setMessages(msgs: MessageItem[]) {
      this.messages = msgs
    },
    async setCurrentChannel(channel: string) {
      // 切换渠道:重新拉 sidebar 列表,自动选中 mtime 最新一条并加载消息;
      // 列表为空时把 currentSessionId 置 null,空白页直发会自动新建一条绑该渠道。
      this.currentChannel = channel
      try {
        const res = await listSessions(this.currentRole, channel)
        this.sessions = res.sessions
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