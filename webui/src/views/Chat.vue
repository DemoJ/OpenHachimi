<template>
  <div class="chat-layout">
    <Sidebar @role-changed="onRoleChanged" @session-loaded="onSessionLoaded" />
    <div class="main-area">
      <header class="header">
        <div class="brand">{{ store.currentRole || '加载中…' }}</div>
        <div style="display: flex; gap: 12px; align-items: center;">
          <span class="model-badge" v-if="store.state">{{ store.state.model }}</span>
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
 * 新会话首条消息时 store.currentSessionId 还是 null，此时从 refreshSessions()
 * 拉到的最新一条会话就是本轮 —— 顺手把它写回 store.currentSessionId，
 * 让 /stop 等按钮也能正确工作。
 */
async function syncMessagesFromServer() {
  let sid = store.currentSessionId
  if (!sid) {
    // refreshSessions 已在 onDone 里调过，这里直接读 store
    const latest = store.sessions[0]
    if (!latest) {
      console.warn('[Chat] syncMessagesFromServer: no session found')
      return
    }
    sid = latest.session_id
    store.setCurrentSession(sid)
    console.info('[Chat] picked up newly-created session', { sid })
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
    }, ctrl.signal)
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
