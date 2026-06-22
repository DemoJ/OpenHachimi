<template>
  <div class="chat-layout">
    <Sidebar @role-changed="onRoleChanged" @session-loaded="onSessionLoaded" />
    <div class="main-area">
      <header class="header">
        <div class="brand">{{ store.currentRole || '加载中…' }}</div>
        <div style="display: flex; gap: 12px; align-items: center;">
          <span class="model-badge" v-if="store.state">🤖 {{ store.state.model }}</span>
          <button class="btn" style="background: transparent; color: var(--text-secondary)" @click="onLogout">退出</button>
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
 * 流结束后从后端拉一次完整历史，把运行时上下文 prefix 回填到本地乐观渲染的
 * user 消息上，让"展开运行时上下文"按钮出现，无需用户刷新页面。
 *
 * 新会话首条消息时 store.currentSessionId 还是 null，此时从 refreshSessions()
 * 拉到的最新一条会话就是本轮 —— 顺手把它写回 store.currentSessionId，
 * 让 /stop 等按钮也能正确工作。
 */
async function syncPrefixesFromServer() {
  let sid = store.currentSessionId
  if (!sid) {
    // refreshSessions 已在 onDone 里调过，这里直接读 store
    const latest = store.sessions[0]
    if (!latest) {
      console.warn('[Chat] syncPrefixesFromServer: no session found')
      return
    }
    sid = latest.session_id
    store.setCurrentSession(sid)
    console.info('[Chat] picked up newly-created session', { sid })
  }

  try {
    const res = await getSessionMessages(sid, store.currentRole)
    // 只把 prefix 回填到现有 user 消息（按顺序对齐），不替换整条消息体 ——
    // 避免覆盖 SSE 期间流式追加的 assistant 内容，也避免触发 MessageList 的滚动闪烁。
    const local = store.messages
    const remote = res.messages
    let li = 0
    let filled = 0
    for (const rm of remote) {
      while (li < local.length && local[li].role !== rm.role) li += 1
      if (li >= local.length) break
      if (local[li].role === 'user' && rm.role === 'user' && rm.prefix && !local[li].prefix) {
        local[li].prefix = rm.prefix
        filled += 1
      }
      li += 1
    }
    console.info('[Chat] prefix sync done', { sid, filled })
  } catch (err) {
    console.warn('[Chat] prefix sync failed', err)
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

  // 用户实时输入的消息没有运行时前缀，prefix 留空
  store.messages.push({ role: 'user', content: text, prefix: '', timestamp: null })
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
        // 刷新会话列表 + 回填本轮的运行时上下文 prefix。
        // 用户实时输入的消息是乐观渲染（prefix=''），但后端在 turn.run_turn 里会注入
        // 时间/记忆/技能/TaskFrame 等 volatile 上下文。流结束后拉一次完整历史，
        // 把这些 prefix 回填到本轮 user 消息上，"展开运行时上下文"按钮才会出现。
        store.refreshSessions().then(() => syncPrefixesFromServer()).catch((err) => {
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
