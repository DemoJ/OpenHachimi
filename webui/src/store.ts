import { defineStore } from 'pinia'
import { markRaw } from 'vue'
import type { SessionSummary, MessageItem, StateResponse, AttachmentRef, ArtifactRef } from './api'
import { fetchRoles, listSessions, fetchState, fetchChannels, getSessionMessages, deleteSession } from './api'
import { getToken, clearToken, ApiError } from './api'

// 空白页直发场景,SSE session 事件回来之前轮次还没有真实 session_id,
// 用这个哨兵 key 暂存在 liveTurns 里,绑定后再迁移到真实 key。
export const PENDING_TURN_KEY = '__pending__'

// 进行中的一轮流式对话。挂在 store 而不是组件里:用户切换会话/角色时组件状态
// 被换掉,但 SSE 连接和本轮消息继续在后台缓冲到这里;切回该会话时用
// visibleMessages 合并还原完整的回复过程(含打字机效果),不丢任何 chunk。
export interface LiveTurn {
  // 绑定后的会话 id;空白页直发在 SSE session 事件到达前为空串
  sessionId: string
  role: string
  generating: boolean
  // 工具调用后当前 assistant 气泡是否已封口(下一段正文开新气泡,
  // 与后端按 ModelResponse 落库的消息划分保持一致)
  sealed: boolean
  activity: string | null
  messages: MessageItem[]
  abort: AbortController | null
}

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
  // 当前会话的已落库历史(来自 /sessions/{id}/messages)。
  // 不含进行中轮次——那些在 liveTurns 里,由 visibleMessages getter 合并出视图。
  messages: MessageItem[]
  // 按 session 缓冲的进行中/未清理轮次。key 是 session_id(或 PENDING_TURN_KEY)。
  liveTurns: Record<string, LiveTurn>
  // 消息分页
  messagesTotal: number
  messagesHasMore: boolean
  messagesNextBeforeTurn: number | null
  messagesLoading: boolean
  // clarify_user 的待选追问(带 choices 时非空):驱动输入框上方的可点选项条。
  // 用户点选或自由输入发送后清除;新轮次开始时也清除。
  clarification: { question: string; choices: string[] } | null
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
    liveTurns: {},
    messagesTotal: 0,
    messagesHasMore: false,
    messagesNextBeforeTurn: null,
    messagesLoading: false,
    clarification: null,
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
    // 当前视图对应的轮次(进行中或刚结束还没清理的)。
    // 空白页(currentSessionId 为 null)对应 PENDING_TURN_KEY 下的待绑定轮次。
    currentLiveTurn(state): LiveTurn | null {
      const key = state.currentSessionId ?? PENDING_TURN_KEY
      return state.liveTurns[key] ?? null
    },
    // 是否正在生成 —— 只看"当前会话"。其他会话的后台轮次不阻塞当前输入框,
    // 用户可以切到别的会话并行对话(后端有 per-session 锁,互不干扰)。
    isGenerating(): boolean {
      return !!this.currentLiveTurn?.generating
    },
    // Agent 当前正在执行的动作文案(来自 SSE 的 temporary 工具调用事件)。
    // 首 chunk 到达前驱动"思考中"指示器;流式中调工具时作为底部状态条。
    activity(): string | null {
      return this.currentLiveTurn?.activity ?? null
    },
    // 视图消息 = 当前会话已落库历史 + 该会话进行中轮次的乐观消息。
    // 进行中的轮次还没落库,服务端历史里没有,必须本地合并才能看到完整过程。
    visibleMessages(state): MessageItem[] {
      const key = state.currentSessionId ?? PENDING_TURN_KEY
      const turn = state.liveTurns[key]
      return turn ? [...state.messages, ...turn.messages] : state.messages
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
      this.sessionsTotal = 0
      this.sessionsLoading = false
      this.channels = ['webui', 'cli', 'telegram', 'weixin']
      this.currentChannel = 'webui'
      this.messages = []
      // 登出:断开所有后台流式连接并清空轮次缓冲
      for (const turn of Object.values(this.liveTurns)) turn.abort?.abort()
      this.liveTurns = {}
      this.messagesTotal = 0
      this.messagesHasMore = false
      this.messagesNextBeforeTurn = null
      this.messagesLoading = false
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
      } catch (err) {
        // 不清空 token，让上层决定如何处理。错误分型提示,方便用户排查
        // (token 错 / 服务没启动 / 其他服务端错误)。
        if (err instanceof ApiError && err.status === 401) {
          throw new Error('访问令牌无效，请检查 user/config.yaml 的 app.http_api_token')
        }
        if (err instanceof ApiError && err.status === 0) {
          throw new Error('无法连接服务器，请确认后台服务已启动')
        }
        throw new Error(`初始化失败：${err instanceof Error ? err.message : String(err)}`)
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
    async deleteSession(session_id: string) {
      // 删除指定会话。调用方(Sidebar)负责:该会话还在生成时先 /stop 后端任务,
      // 以及 confirm 二次确认。这里只管落库 + 本地态同步。
      // 该会话若有本地轮次(含后台流式),一并中断并丢弃缓冲。
      const doomed = this.liveTurns[session_id]
      if (doomed) {
        doomed.abort?.abort()
        delete this.liveTurns[session_id]
      }
      await deleteSession(session_id, this.currentRole)
      // 本地先剔除该条,避免等接口刷新的视觉闪烁。
      this.sessions = this.sessions.filter((s) => s.session_id !== session_id)
      this.sessionsTotal = Math.max(0, this.sessionsTotal - 1)
      // 删除的是当前会话 → 进入空白页(不预创建,发消息时自动 /new)。
      if (session_id === this.currentSessionId) {
        this.currentSessionId = null
        this.messages = []
      }
      // 重拉第一页,补齐因分页漂移缺失的后续条目。
      await this.refreshSessions()
    },
    setCurrentSession(id: string | null) {
      this.currentSessionId = id
      // 切走时清扫:已结束且不属于新视图的轮次就地丢弃。
      // 错误/中断轮次的内容没落库,保留到用户看过为止(下次切走再清)。
      const keep = id ?? PENDING_TURN_KEY
      for (const key of Object.keys(this.liveTurns)) {
        if (key !== keep && !this.liveTurns[key].generating) delete this.liveTurns[key]
      }
    },
    setMessages(msgs: MessageItem[]) {
      this.messages = msgs
    },
    async loadMessages(session_id: string, role?: string) {
      this.messagesLoading = true
      try {
        const msgs = await getSessionMessages(session_id, role || this.currentRole)
        this.messages = msgs.messages
        this.messagesTotal = msgs.total ?? 0
        this.messagesHasMore = msgs.has_more ?? false
        this.messagesNextBeforeTurn = msgs.next_before_turn ?? null
      } catch (err) {
        console.warn('[store] failed to load messages', err)
        this.messages = []
      } finally {
        this.messagesLoading = false
      }
    },
    async loadOlderMessages(onBeforePrepend?: () => void, onAfterPrepend?: () => void) {
      if (this.messagesLoading || !this.messagesHasMore || !this.currentSessionId) return
      this.messagesLoading = true
      try {
        const msgs = await getSessionMessages(this.currentSessionId, this.currentRole, { before_turn: this.messagesNextBeforeTurn ?? undefined })
        if (onBeforePrepend) onBeforePrepend()
        this.messages.unshift(...msgs.messages)
        if (onAfterPrepend) onAfterPrepend()
        this.messagesTotal = msgs.total ?? this.messagesTotal
        this.messagesHasMore = msgs.has_more ?? false
        this.messagesNextBeforeTurn = msgs.next_before_turn ?? null
      } catch (err) {
        console.warn('[store] failed to load older messages', err)
      } finally {
        this.messagesLoading = false
      }
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
          this.setCurrentSession(top.session_id)
          try {
            await this.loadMessages(top.session_id, this.currentRole)
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
    // ── 流式轮次生命周期(SSE 回调一律操作传入的 turn 对象,而不是"当前视图",
    //    这样用户切走后流式照常写入该轮次的缓冲,切回来由 visibleMessages 还原) ──

    _keyOfTurn(turn: LiveTurn): string | null {
      for (const [key, t] of Object.entries(this.liveTurns)) {
        if (t === turn) return key
      }
      return null
    },
    // 开启新一轮:推入乐观 user 消息,返回轮次对象(含 AbortController)。
    // 当前会话已有进行中轮次时返回 null(并发防线,理论上 ChatInput 已禁用)。
    startTurn(text: string, attachments: AttachmentRef[]): LiveTurn | null {
      const key = this.currentSessionId ?? PENDING_TURN_KEY
      const existing = this.liveTurns[key]
      if (existing?.generating) return null
      // 新一轮开始,旧的追问选项作废
      this.clarification = null
      if (existing) {
        // 上一轮已结束(错误/中断留下的缓冲)但用户还没切走:先物化再开新一轮,
        // 否则覆盖 liveTurns[key] 会把那轮的正文直接丢掉。
        if (key === (this.currentSessionId ?? PENDING_TURN_KEY)) {
          this.messages.push(...existing.messages)
        }
        delete this.liveTurns[key]
      }
      const turn: LiveTurn = {
        sessionId: this.currentSessionId ?? '',
        role: this.currentRole,
        generating: true,
        sealed: false,
        activity: null,
        messages: [
          {
            role: 'user',
            content: text,
            prefix: '',
            // 客户端本地时间乐观打点,流结束后 syncMessagesFromServer
            // 用后端权威 timestamp 覆盖。
            timestamp: new Date().toISOString(),
            tokens: null,
            attachments: attachments.length > 0 ? attachments : null,
          },
        ],
        // markRaw:AbortController 这类带内部槽的原生对象不能被深度响应式包裹,
        // 否则经由 proxy 调 abort()/读 signal 会触发 Illegal invocation。
        abort: markRaw(new AbortController()),
      }
      this.liveTurns[key] = turn
      // 必须返回 store 里的 reactive 代理而不是上面的原始对象:闭包持有原始
      // 引用直接变更(push chunk 等)不会经过 proxy,流式内容不会触发视图更新。
      return this.liveTurns[key]
    },
    appendTurnChunk(turn: LiveTurn, text: string) {
      const last = turn.messages[turn.messages.length - 1]
      if (last && last.role === 'assistant' && !turn.sealed) {
        last.content += text
      } else {
        turn.sealed = false
        // 首个 assistant chunk(或工具调用后的新段落)到达:开新气泡。
        // timestamp 乐观打点,tokens 要等 ModelResponse 聚合,先留 null,
        // 流结束后 syncMessagesFromServer 回填。
        turn.messages.push({
          role: 'assistant',
          content: text,
          prefix: '',
          timestamp: new Date().toISOString(),
          tokens: null,
          artifacts: [],
        })
      }
    },
    // 把轮次当前流式 assistant 气泡"封口":agent 中途调用工具时,工具前后的
    // 正文属于不同的 ModelResponse,后端落库为多条独立 assistant 消息。
    // 不封口的话多段正文会无分隔地拼在一个气泡里,markdown 跨段解析错乱,
    // 且刷新后与持久化历史的气泡划分不一致。
    sealTurn(turn: LiveTurn) {
      const last = turn.messages[turn.messages.length - 1]
      if (last && last.role === 'assistant' && last.content) {
        turn.sealed = true
      }
    },
    setTurnActivity(turn: LiveTurn, text: string | null) {
      turn.activity = text
    },
    // clarify_user 追问到达(带预设选项):记录供输入框上方渲染可点选项条。
    setClarification(question: string, choices: string[]) {
      this.clarification = { question, choices }
    },
    clearClarification() {
      this.clarification = null
    },
    appendTurnArtifact(turn: LiveTurn, artifact: ArtifactRef) {
      // 把 agent 生成的产物附加到轮次最后一条 assistant 消息上。
      // 若还没有 assistant 消息(理论上不该发生),创建一条空消息。
      const last = turn.messages[turn.messages.length - 1]
      if (last && last.role === 'assistant') {
        if (!last.artifacts) last.artifacts = []
        if (!last.artifacts.some(a => a.id === artifact.id)) {
          last.artifacts.push(artifact)
        }
      } else {
        turn.messages.push({
          role: 'assistant',
          content: '',
          prefix: '',
          timestamp: new Date().toISOString(),
          tokens: null,
          artifacts: [artifact],
        })
      }
    },
    // SSE session 事件到达:把待绑定轮次从哨兵 key 迁到真实 session key。
    bindTurnSession(turn: LiveTurn, sessionId: string) {
      if (turn.sessionId === sessionId) return
      const key = this._keyOfTurn(turn)
      turn.sessionId = sessionId
      if (key === null || key === sessionId) return
      this.liveTurns[sessionId] = turn
      delete this.liveTurns[key]
    },
    // 流结束(正常/错误/中断):终止 generating 态。errorMessage 非空时追加错误气泡。
    // 错误/中断轮次的内容没落库,保留在缓冲里给用户看,切走时由
    // setCurrentSession 清扫;正常结束走 completeTurn。
    finishTurn(turn: LiveTurn, errorMessage?: string) {
      turn.generating = false
      turn.activity = null
      turn.sealed = false
      if (errorMessage) {
        // 后端在客户端断开时会把任务转后台继续执行并落库(见 http.py 的
        // detach 逻辑),提示用户稍后回来能看到完整结果,而不是以为全丢了。
        this.appendTurnChunk(
          turn,
          `\n\n**[连接中断]** ${errorMessage}\n\n任务可能仍在后台执行。稍等片刻后刷新页面或切回本会话，可查看已生成的完整结果。`,
        )
      }
    },
    // 正常完成:此时后端已把本轮全部消息落库(done 事件在 _persist_turn 之后)。
    // 正在查看该会话 → 把乐观消息物化进 messages(视图不变,后续 meta 回填);
    // 已切走 → 直接丢弃缓冲,切回时 loadMessages 拉到的就是权威历史。
    completeTurn(turn: LiveTurn) {
      const key = this._keyOfTurn(turn)
      if (key === null) return
      if (key === (this.currentSessionId ?? PENDING_TURN_KEY)) {
        this.messages.push(...turn.messages)
      }
      delete this.liveTurns[key]
    },
    // 供 Sidebar 删除会话等场景判断:指定会话是否还有进行中的后台轮次。
    isSessionGenerating(sessionId: string): boolean {
      return !!this.liveTurns[sessionId]?.generating
    },
  },
})