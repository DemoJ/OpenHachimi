<template>
  <div class="messages-container" ref="containerRef" @scroll="onScroll" aria-live="polite">
    <div class="messages-list">
      <!-- 无限滚动触发哨兵 -->
      <div v-if="store.messagesHasMore" ref="loadMoreSentinel" class="load-more-sentinel" style="text-align: center; padding: 12px; color: var(--body-mid);">
        <span v-if="store.messagesLoading">加载中...</span>
        <span v-else>滚动加载更多</span>
      </div>
      <template v-for="(m, idx) in messages" :key="messageKey(m, idx)">
        <!-- 压缩标记条：该段对话被压缩为摘要提供给 AI 时显示。
              可点击展开被折叠的原始消息。 -->
        <FoldCard
          v-if="m.fold"
          :fold="m.fold"
          :session-id="store.currentSessionId"
          :role="store.currentRole"
        />
        <MessageBubble
          v-else
          :role="m.role"
          :content="m.content"
          :prefix="m.prefix"
          :timestamp="m.timestamp"
          :tokens="m.tokens"
          :streaming="isStreaming(idx)"
          :attachments="m.attachments"
          :artifacts="m.artifacts"
          :tool-calls="m.tool_calls"
        />
      </template>
      <!-- 空状态:脱离消息流,垂直水平居中于容器中央。
           hero 主标题 + 副标题,给出明确的起始指引,避免孤字飘在左上角。 -->
      <div v-if="messages.length === 0 && !generating" class="empty-hero">
        <div class="empty-hero-icon">✦</div>
        <h2 class="empty-hero-title">开始你的第一段对话</h2>
        <p class="empty-hero-sub">在下方输入框输入消息，按 Enter 发送</p>
      </div>

      <!-- 思考中气泡：首个正文 chunk 到达前展示。
           Agent 此刻可能在规划或调工具，有 activity 文案时一并显示。 -->
      <div v-if="showThinking" class="thinking">
        <span class="thinking-dots"><i></i><i></i><i></i></span>
        <span class="thinking-text">{{ activity || 'Agent 正在思考…' }}</span>
      </div>

      <!-- 活动状态条：已经在流式输出正文，但 Agent 中途又调起工具时展示。 -->
      <div v-else-if="showActivity" class="activity-bar">
        <span class="activity-spinner"></span>
        <span class="activity-text">{{ activity }}</span>
      </div>
    </div>

    <!-- 回到底部浮标:用户上翻离开底部时出现(生成期间不再强制拉回底部) -->
    <button v-if="showJumpButton" class="jump-bottom" title="回到底部" @click="jumpToBottom">↓</button>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, computed, onMounted, onBeforeUnmount } from 'vue'
import MessageBubble from './MessageBubble.vue'
import FoldCard from './FoldCard.vue'
import type { MessageItem } from '../api'
import { useChatStore } from '../store'

const props = defineProps<{ messages: MessageItem[] }>()
const store = useChatStore()
const containerRef = ref<HTMLElement | null>(null)

const generating = computed(() => store.isGenerating)
const activity = computed(() => store.activity)
const lastIdx = computed(() => props.messages.length - 1)
const lastIsAssistant = computed(
  () => props.messages.length > 0 && props.messages[props.messages.length - 1].role === 'assistant',
)

// 首 chunk 到达前：还没有 assistant 消息 → 显示思考气泡
const showThinking = computed(() => generating.value && !lastIsAssistant.value)
// 流式中途：已有 assistant 正文，但 Agent 又调工具 → 显示活动状态条
const showActivity = computed(() => generating.value && !!activity.value && lastIsAssistant.value)

// 只有最后一条 assistant 消息、正在生成、且当前不在调工具时显示打字机光标
function isStreaming(idx: number): boolean {
  return (
    generating.value &&
    idx === lastIdx.value &&
    lastIsAssistant.value &&
    !activity.value
  )
}

// 滚动触发器:此前用 join(全部 content) 触发,长会话每个 chunk 都要
// O(总字符) 重新拼接。改为"消息条数 + 尾条内容长度 + 状态"的轻量签名,
// 行为等价(任何新内容都会改变签名),计算量 O(1)。
const scrollTrigger = computed(() => {
  const last = props.messages[props.messages.length - 1]
  return `${props.messages.length}:${last ? last.content.length : 0}:${last?.role ?? ''}:${
    generating.value ? 'g' : ''
  }:${activity.value || ''}`
})

// 稳定的消息 key:用索引会在向上翻页 prepend 后让 Vue 复用错位的组件实例
// (例如"展开上下文"的 expanded 状态跳到别的消息上)。timestamp+role+长度
// 组合在会话内基本唯一,且 prepend 不改变既有消息的 key。
function messageKey(m: MessageItem, idx: number): string {
  return `${m.timestamp ?? 't'}:${m.role}:${m.content.length}:${idx}`
}

const loadMoreSentinel = ref<HTMLElement | null>(null)
let observer: IntersectionObserver | null = null
let isPrepending = false

function attachObserver() {
  if (observer || !loadMoreSentinel.value) return
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        loadOlderMessages()
      }
    },
    {
      root: containerRef.value ?? null,
      rootMargin: '100px',
      threshold: 0,
    },
  )
  observer.observe(loadMoreSentinel.value)
}

function detachObserver() {
  observer?.disconnect()
  observer = null
}

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

async function loadOlderMessages() {
  if (store.messagesLoading || !store.messagesHasMore) return
  
  if (!containerRef.value) return
  const el = containerRef.value
  const oldScrollHeight = el.scrollHeight
  const oldScrollTop = el.scrollTop
  
  isPrepending = true
  
  await store.loadOlderMessages()
  
  await nextTick()
  const newScrollHeight = el.scrollHeight
  el.scrollTop = oldScrollTop + (newScrollHeight - oldScrollHeight)
  
  // 等待一下避免 watch 里的滚动逻辑干扰
  setTimeout(() => {
    isPrepending = false
  }, 100)
}

let wasAtBottom = false
const showJumpButton = ref(false)

function onScroll() {
  const el = containerRef.value
  if (!el) return
  wasAtBottom = Math.abs(el.scrollHeight - el.scrollTop - el.clientHeight) < 100
  showJumpButton.value = !wasAtBottom
}

function jumpToBottom() {
  const el = containerRef.value
  if (!el) return
  el.scrollTop = el.scrollHeight
  wasAtBottom = true
  showJumpButton.value = false
}

// 切换会话/角色后滚到底:此前依赖 wasAtBottom 判断,用户在旧会话处于上翻
// 位置时新会话会停在中部。currentSessionId 只在切换时变化(流式 chunk 不会触发)。
watch(
  () => store.currentSessionId,
  () => {
    nextTick(() => jumpToBottom())
  },
)

watch(
  scrollTrigger,
  () => {
    if (!containerRef.value) return
    onScroll()
  },
  { flush: 'pre' }
)

watch(
  scrollTrigger,
  async () => {
    if (isPrepending) return
    await nextTick()
    const el = containerRef.value
    if (!el) return
    // 只在用户本来就在底部附近时跟随滚动。生成期间强制拉回底部会
    // 打断用户回看长回复的上文(wasAtBottom 由最近的 scroll/trigger 更新)。
    if (wasAtBottom) {
      el.scrollTop = el.scrollHeight
    }
  },
  { flush: 'post' },
)
</script>

<style scoped>
.jump-bottom {
  position: absolute;
  right: 20px;
  bottom: 16px;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: 1px solid var(--pill-border, rgba(255, 255, 255, 0.14));
  background: var(--canvas-soft, #1a1c20);
  color: var(--ink, #ededed);
  cursor: pointer;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 5;
}
.jump-bottom:hover { border-color: var(--pill-border-hover, rgba(255, 255, 255, 0.3)); }
</style>
