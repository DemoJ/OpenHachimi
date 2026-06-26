<template>
  <div class="chat-layout">
    <Sidebar @role-changed="onRoleChanged" @session-loaded="onSessionLoaded" />
    <div class="main-area">
      <header class="header">
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
      <MessageList :messages="store.messages" />
      <ChatInput :generating="store.isGenerating" @send="onSend" @stop="onStop" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import Sidebar from '../components/Sidebar.vue'
import MessageList from '../components/MessageList.vue'
import ChatInput from '../components/ChatInput.vue'
import { useChatStore } from '../store'
import { chatStream } from '../sse'
import { post, getToken, getSessionMessages } from '../api'

const router = useRouter()
const store = useChatStore()
// 当前正在进行的 SSE 流。切换会话/角色/卸载组件前都要 abort，
// 否则后端会看到"上一次的流还在跑"，而前端组件状态已经被换掉了。
let abortCtrl: AbortController | null = null

function abortCurrentStream(reason: string) {
  if (abortCtrl && !abortCtrl.signal.aborted) {
    console.info('[Chat] aborting in-flight stream', { reason })
    abortCtrl.abort()
  }
  abortCtrl = null
}

/**
 * 流结束后从后端拉一次完整历史，把后端权威的 timestamp / tokens / prefix
 * 回填到本地乐观渲染的消息上，让"展开运行时上下文"按钮、回复时间与 token 计数
 * 都无需用户刷新页面就能出现。
 *
 * 新会话首条消息时 store.currentSessionId 来自 SSE 首个 type=session 事件的
 * onSession 回调；如果回调未触发（理论上不该发生），这里只记日志并返回，
 * 不再回退到 sessions[0] —— 那是渠道隔离前的兜底，会跨渠道挑错会话。
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
    // MessageList 的滚动闪烁。
    const local = store.messages
    const remote = res.messages
    let li = 0
    let filledPrefix = 0
    let filledTime = 0
    let filledTokens = 0
    for (const rm of remote) {
      while (li < local.length && local[li].role !== rm.role) li += 1
      if (li >= local.length) break
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
      li += 1
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

onBeforeUnmount(() => {
  abortCurrentStream('component-unmount')
})

function onRoleChanged() {
  // 切角色后 Sidebar 已经重置了 messages / currentSessionId，
  // 这里必须把旧流断掉，否则旧流的 chunk 会污染新会话视图。
  abortCurrentStream('role-changed')
  store.setGenerating(false)
}

function onSessionLoaded() {
  abortCurrentStream('session-loaded')
  store.setGenerating(false)
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
  abortCurrentStream('channel-changed')
  store.setGenerating(false)
  await store.setCurrentChannel(channel)
}

async function onSend(text: string) {
  // 防御：理论上 ChatInput 已经在 generating=true 时禁用了发送，
  // 这里再兜底一次，防止异常路径下产生并发流。
  if (store.isGenerating) {
    console.warn('[Chat] onSend ignored: still generating')
    return
  }

  // 用户实时输入的消息没有运行时前缀，prefix 留空；
  // timestamp 本地乐观打一次，流结束后 syncMessagesFromServer 会用后端权威值覆盖。
  store.messages.push({
    role: 'user',
    content: text,
    prefix: '',
    timestamp: new Date().toISOString(),
    tokens: null,
  })
  store.setGenerating(true)
  abortCtrl = new AbortController()
  const ctrl = abortCtrl
  console.info('[Chat] send', { chars: text.length, role: store.currentRole })

  try {
    await chatStream(text, store.currentRole, {
      onChunk(t, temporary) {
        if (temporary) {
          // 临时事件是工具调用提示（如"🖥️ 执行命令：npm test"），
          // 不计入消息正文，但用它驱动"思考中/活动中"指示器，
          // 让 Agent 在首句产出前的规划与工具调用对用户可见。
          store.setActivity(t)
          return
        }
        // 收到首个正文 chunk 后清掉活动状态条，
        // 让打字机光标接管"生成中"的视觉反馈。
        if (store.activity) store.setActivity(null)
        store.appendAssistantChunk(t)
      },
      onSession(sid) {
        // 后端首事件:把空白页直发自动新建的 session_id 回填到 store,
        // 后续 /stop、syncMessagesFromServer 都能直接拿到正确 id。
        if (!store.currentSessionId) {
          console.info('[Chat] session bound from stream', { sid })
          store.setCurrentSession(sid)
        }
      },
      onDone() {
        console.info('[Chat] stream done')
        store.setGenerating(false)
        // 刷新会话列表 + 回填本轮 prefix / timestamp / tokens。
        // 用户实时输入的消息是乐观渲染（prefix=''、本地 timestamp），但后端 turn.run_turn
        // 会注入 system_context、并把权威 timestamp + ModelResponse.usage 持久化。
        // 流结束后拉一次完整历史，把这些 metadata 回填到本轮消息上：
        // - user：补 prefix（运行时上下文）+ 校正 timestamp
        // - assistant：补 timestamp + token 用量
        store.refreshSessions().then(() => syncMessagesFromServer()).catch((err) => {
          console.warn('[Chat] post-stream refresh failed', err)
        })
      },
      onError(err) {
        const msg = err instanceof Error ? err.message : String(err)
        // user-abort（停止按钮 / 切换会话 / 组件卸载）不应展示成错误
        const isAbort = msg.toLowerCase().includes('abort') || ctrl.signal.aborted
        if (!isAbort) {
          console.error('[Chat] stream error', msg)
          store.appendAssistantChunk(`\n\n**[错误]** ${msg}`)
        } else {
          console.info('[Chat] stream aborted by user/route')
        }
        store.setGenerating(false)
      },
    }, ctrl.signal, { sessionId: store.currentSessionId, channel: store.currentChannel })
  } catch (err) {
    console.warn('[Chat] chatStream threw', err)
    store.setGenerating(false)
  } finally {
    // 仅当当前 controller 还是这次启动的那个时清空（避免 onSend 已经被下次调用覆盖）
    if (abortCtrl === ctrl) abortCtrl = null
  }
}

async function onStop() {
  console.info('[Chat] stop requested')
  abortCurrentStream('user-stop')
  if (store.currentSessionId) {
    try {
      await post('/stop', { session_id: store.currentSessionId })
    } catch (err) {
      console.warn('[Chat] /stop request failed', err)
    }
  }
  store.setGenerating(false)
}

function onLogout() {
  abortCurrentStream('logout')
  store.logout()
  router.replace('/login')
}
</script>

<style scoped>
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
