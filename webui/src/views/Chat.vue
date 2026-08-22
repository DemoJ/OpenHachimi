<template>
  <div class="chat-layout" :class="{ 'sidebar-open': sidebarOpen }">
    <Sidebar @role-changed="onRoleChanged" @session-loaded="onSessionLoaded" />
    <div v-if="sidebarOpen" class="sidebar-overlay" @click="sidebarOpen = false"></div>
    <div class="main-area">
      <header class="header">
        <button class="btn menu-btn" title="会话列表" @click="sidebarOpen = !sidebarOpen">☰</button>
        <div class="brand">{{ store.currentRole || '加载中…' }}</div>
        <div style="display: flex; gap: 12px; align-items: center;">
          <label class="channel-picker">
            <span class="channel-picker-label">渠道</span>
            <select :value="store.currentChannel" @change="onChannelChange" :disabled="store.isGenerating">
              <option v-for="c in store.channels" :key="c" :value="c">{{ channelLabel(c) }}</option>
            </select>
          </label>
          <button class="btn" @click="onLogout">退出</button>
        </div>
      </header>
      <MessageList :messages="store.visibleMessages" />
      <!-- clarify_user 追问的预设选项:点击即发送选项文本,比手打回复省事 -->
      <div v-if="store.clarification && !store.isGenerating" class="clarify-bar">
        <span class="clarify-question">{{ store.clarification.question }}</span>
        <div class="clarify-choices">
          <button
            v-for="(choice, i) in store.clarification.choices"
            :key="i"
            class="btn clarify-choice"
            @click="onClarifyChoice(choice)"
          >{{ choice }}</button>
        </div>
      </div>
      <ChatInput :generating="store.isGenerating" @send="onSend" @stop="onStop" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import Sidebar from '../components/Sidebar.vue'
import MessageList from '../components/MessageList.vue'
import ChatInput from '../components/ChatInput.vue'
import { useChatStore, type LiveTurn } from '../store'
import { chatStream } from '../sse'
import { post, getToken, getSessionMessages, type AttachmentRef } from '../api'

const router = useRouter()
const store = useChatStore()
// 移动端抽屉开关(<768px 时侧栏收进抽屉,顶栏 ☰ 呼出)。
const sidebarOpen = ref(false)

/**
 * 流结束后从后端拉一次完整历史，把后端权威的 timestamp / tokens / prefix
 * 回填到本地乐观渲染的消息上，让"展开运行时上下文"按钮、回复时间与 token 计数
 * 都无需用户刷新页面就能出现。
 *
 * 正常完成的轮次在 onDone 里已经 completeTurn 物化进 store.messages，
 * 所以这里对齐的是 store.messages 的尾部（本轮消息）；按 role 从后往前匹配。
 * 若流结束时会话已被切走，这里同步的只是当前会话，历史信息同样无害。
 */
async function syncMessagesFromServer() {
  const sid = store.currentSessionId
  if (!sid) {
    console.warn('[Chat] syncMessagesFromServer: no currentSessionId after stream done')
    return
  }

  try {
    const res = await getSessionMessages(sid, store.currentRole)
    // 按顺序对齐本地与远端消息（按 role 匹配），只回填 metadata 字段，
    // 不替换 content —— 避免覆盖 SSE 期间流式追加的 assistant 文本，也避免触发
    // MessageList 的滚动闪烁。因为远端分页只返回最新一页，所以从后往前遍历对齐。
    const local = store.messages
    const remote = res.messages
    let li = local.length - 1
    let filledPrefix = 0
    let filledTime = 0
    let filledTokens = 0
    for (let ri = remote.length - 1; ri >= 0; ri--) {
      const rm = remote[ri]
      while (li >= 0 && local[li].role !== rm.role) li -= 1
      if (li < 0) break
      const lm = local[li]
      if (lm.role === 'user' && rm.role === 'user' && rm.prefix && !lm.prefix) {
        lm.prefix = rm.prefix
        filledPrefix += 1
      }
      // 用后端 ISO timestamp 覆盖本地的乐观时间戳（来源更权威，与持久化历史一致）
      if (rm.timestamp) {
        lm.timestamp = rm.timestamp
        filledTime += 1
      }
      // tokens 仅 assistant 有；流式期间没办法实时拿，靠这次回填补齐
      if (rm.role === 'assistant' && rm.tokens) {
        lm.tokens = rm.tokens
        filledTokens += 1
      }
      li -= 1
    }
    console.info('[Chat] message meta sync done', {
      sid,
      filledPrefix,
      filledTime,
      filledTokens,
    })
  } catch (err) {
    console.warn('[Chat] message meta sync failed', err)
  }
}

onMounted(async () => {
  if (!getToken()) {
    router.replace('/login')
    return
  }
  try {
    await store.loadInit()
  } catch {
    router.replace('/login')
  }
})

// 注意:组件卸载/切会话/切角色时不再 abort 进行中的 SSE —— 轮次状态挂在
// store 的 liveTurns(按 session 缓冲),切回来由 visibleMessages 还原完整
// 回复过程。真正需要断流的只有登出(store.logout 会 abort 所有轮次)。

function onRoleChanged() {
  // 切角色:Sidebar 已重置 currentSessionId/messages。旧角色会话的流式轮次
  // 继续在后台缓冲(轮次对象独立于视图,不会污染新视图),完成时正常落库。
}

function onSessionLoaded() {
  // 切换/新建会话:旧轮次挂在它自己的 session key 下继续流式,
  // 切回来时 visibleMessages = 落库历史 + 该轮次缓冲,回复过程完整可见。
  // 移动端:选中会话后收起抽屉。
  sidebarOpen.value = false
}

const CHANNEL_LABELS: Record<string, string> = {
  webui: 'WebUI',
  cli: 'CLI',
  telegram: 'Telegram',
  weixin: '微信',
}

function channelLabel(code: string): string {
  return CHANNEL_LABELS[code] ?? code
}

async function onChannelChange(e: Event) {
  const target = e.target as HTMLSelectElement
  const channel = target.value
  if (channel === store.currentChannel) return
  // 切渠道不中断旧渠道的流式轮次,它们继续后台缓冲。
  await store.setCurrentChannel(channel)
}

async function onSend(text: string, attachments: AttachmentRef[]) {
  // 防御：理论上 ChatInput 已经在 generating=true 时排队而非发送，
  // 这里再兜底一次，防止异常路径下产生并发流。
  if (store.isGenerating) {
    console.warn('[Chat] onSend ignored: still generating')
    return
  }
  store.clearClarification()
  const turn = store.startTurn(text, attachments)
  if (!turn) {
    console.warn('[Chat] onSend ignored: startTurn rejected')
    return
  }
  const ctrl = turn.abort!
  // 发送时刻的快照:空白页直发时 sessionId 为 null,由 SSE 首事件回填真实 id。
  const sessionId = store.currentSessionId
  const role = store.currentRole
  const channel = store.currentChannel
  console.info('[Chat] send', { chars: text.length, role, attachments: attachments.length })

  try {
    await chatStream(text, role, {
      onChunk(t, temporary) {
        if (temporary) {
          // 临时事件是工具调用提示（如"🖥️ 执行命令：npm test"），
          // 不计入消息正文，但用它驱动"思考中/活动中"指示器，
          // 让 Agent 在首句产出前的规划与工具调用对用户可见。
          // 工具调用同时意味着上一段正文已结束(一个 ModelResponse),
          // 封口当前气泡,让下一段正文开新气泡,与后端落库划分一致。
          store.sealTurn(turn)
          store.setTurnActivity(turn, t)
          return
        }
        // 收到首个正文 chunk 后清掉活动状态条，
        // 让打字机光标接管"生成中"的视觉反馈。
        if (turn.activity) store.setTurnActivity(turn, null)
        store.appendTurnChunk(turn, t)
      },
      onSession(sid, _channel, _autoCreated, role) {
        // 后端首事件:空白页直发自动新建的 session_id;命令(/new、/role)执行后
        // 的 session 事件携带变更后的指向。轮次缓冲迁移到真实 key;用户还停在
        // 空白页(或仍在本轮所属会话)时同步选中,已切走则不打扰。
        store.bindTurnSession(turn, sid)
        const stillHere = sessionId === null ? !store.currentSessionId : store.currentSessionId === sessionId
        if (stillHere && store.currentSessionId !== sid) {
          console.info('[Chat] session bound from stream', { sid })
          store.setCurrentSession(sid)
        }
        // 新会话不等 AI 回复完才进侧栏:SSE 首事件一到就乐观插入,
        // 生成期间也能随时切走再切回来。
        store.ensureSessionVisible(sid, text, turn.role)
        // /role 等命令改变了角色:同步 currentRole 并刷新会话列表
        if (role && role !== store.currentRole) {
          console.info('[Chat] role changed via command', { role })
          store.currentRole = role
          store.refreshSessions(role).catch((err) => console.warn('[Chat] refresh sessions failed', err))
        }
      },
      onClarification(question, choices) {
        console.info('[Chat] clarification received', { choices })
        store.setClarification(question, choices)
      },
      onArtifact(artifact) {
        // agent 生成的产物文件:附加到当前轮次的 assistant 消息上供前端渲染。
        console.info('[Chat] artifact received', { id: artifact.id, filename: artifact.filename })
        store.appendTurnArtifact(turn, artifact)
      },
      onDone() {
        console.info('[Chat] stream done')
        store.finishTurn(turn)
        // done 事件在后端 _persist_turn 之后发出,本轮消息已全部落库:
        // 正在查看 → 乐观消息物化进 messages(视图无跳变),随后回填 meta;
        // 已切走 → 丢弃缓冲,切回时 loadMessages 拉权威历史。
        // aborted 时不物化:中断轮次的内容没落库,保留缓冲给用户看。
        if (!ctrl.signal.aborted) store.completeTurn(turn)
        store.refreshSessions().then(() => syncMessagesFromServer()).catch((err) => {
          console.warn('[Chat] post-stream refresh failed', err)
        })
      },
      onError(err) {
        const msg = err instanceof Error ? err.message : String(err)
        // user-abort（停止按钮 / 删除会话 / 登出）不应展示成错误
        const isAbort = msg.toLowerCase().includes('abort') || ctrl.signal.aborted
        if (!isAbort) {
          console.error('[Chat] stream error', msg)
          store.finishTurn(turn, msg)
        } else {
          console.info('[Chat] stream aborted by user/route')
          // 中断的轮次保留已缓冲的部分回复(未落库,切走时由 store 清扫)
          store.finishTurn(turn)
        }
      },
    }, ctrl.signal, { sessionId, channel, attachments })
  } catch (err) {
    console.warn('[Chat] chatStream threw', err)
    store.finishTurn(turn)
  }
}

async function onStop() {
  const turn: LiveTurn | null = store.currentLiveTurn
  console.info('[Chat] stop requested', { sid: turn?.sessionId })
  if (!turn) return
  turn.abort?.abort()
  const sid = turn.sessionId || store.currentSessionId
  if (sid) {
    try {
      await post('/stop', { session_id: sid })
    } catch (err) {
      // 停止请求失败要告知用户:本地 SSE 已断,但后端任务可能仍在跑。
      console.warn('[Chat] /stop request failed', err)
      store.appendTurnChunk(turn, '\n\n*停止请求发送失败，后端任务可能仍在执行；可稍后重试或刷新查看。*')
    }
  }
  // abort 触发的 onError 会 finishTurn;这里兜底再标一次(幂等)。
  store.finishTurn(turn)
}

function onClarifyChoice(choice: string) {
  console.info('[Chat] clarify choice selected', { choice })
  onSend(choice, [])
}

function onLogout() {
  store.logout()
  router.replace('/login')
}
</script>

<style scoped>
/* clarify 选项条:追问 + 可点选项,出现在输入框上方 */
.clarify-bar {
  padding: var(--sp-sm) var(--sp-lg);
  border-top: 1px solid var(--hairline);
  display: flex;
  flex-direction: column;
  gap: var(--sp-sm);
}
.clarify-question {
  font-size: 12px;
  color: var(--body-mid);
  max-height: 60px;
  overflow: hidden;
}
.clarify-choices {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-sm);
}
.clarify-choice {
  font-size: 13px;
  cursor: pointer;
}
.channel-picker {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-sm);
}
/* 标签用 mono 大写 eyebrow 风格,融入全局设计语言 */
.channel-picker-label {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  font-weight: 400;
  line-height: 16px;
  letter-spacing: 1.2px;
  text-transform: uppercase;
  color: var(--body-mid);
}
/* select 与 .btn 描边胶囊一致:canvas-soft 填充 + 半透明白边,深色画布上清晰可见 */
.channel-picker select {
  background: var(--canvas-soft);
  border: 1px solid var(--pill-border);
  border-radius: var(--radius-pill);
  padding: var(--sp-xs) var(--sp-lg) var(--sp-xs) var(--sp-md);
  color: var(--ink);
  font-size: 13px;
  font-family: inherit;
  font-weight: 400;
  line-height: 20px;
  cursor: pointer;
  appearance: none;
  -webkit-appearance: none;
  /* 自定义下拉箭头(用 --body-mid 颜色,避免系统默认黑箭头在深色背景上看不见) */
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M3 5l3 3 3-3' stroke='%237d8187' stroke-width='1.5' fill='none' stroke-linecap='round'/></svg>");
  background-repeat: no-repeat;
  background-position: right 10px center;
  padding-right: 30px;
  transition: border-color 0.15s, background 0.15s;
}
.channel-picker select:hover { border-color: var(--pill-border-hover); }
.channel-picker select:focus { outline: none; border-color: var(--pill-border-hover); }
.channel-picker select:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
/* 下拉弹层:深色背景 + 白字 + 发丝边。仅 WebKit/Blink 生效,Firefox 用系统原生 */
.channel-picker select option {
  background: var(--canvas-soft);
  color: var(--ink);
}
</style>
