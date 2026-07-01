<template>
  <div class="messages-container" ref="containerRef">
    <div class="messages-list">
      <!-- 无限滚动触发哨兵 -->
      <div v-if="store.messagesHasMore" ref="loadMoreSentinel" class="load-more-sentinel" style="text-align: center; padding: 12px; color: var(--body-mid);">
        <span v-if="store.messagesLoading">加载中...</span>
        <span v-else>滚动加载更多</span>
      </div>
      <template v-for="(m, idx) in messages" :key="idx">
        <!-- 折叠占位条：压缩过的中间段不直接渲染原始消息，而是显示一张可展开卡片。
             点击展开调 fetchFoldedMessages 取回被折叠的原始消息内联渲染。 -->
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

// 滚动触发器：消息内容、生成态、活动文案任一变化都滚到底
const scrollTrigger = computed(
  () =>
    props.messages.map((m) => m.content).join('|') +
    '|' +
    (generating.value ? 'g' : '') +
    '|' +
    (activity.value || ''),
)

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
watch(
  scrollTrigger,
  () => {
    if (!containerRef.value) return
    const el = containerRef.value
    // 距离底部 100px 以内认为是在底部附近
    wasAtBottom = Math.abs(el.scrollHeight - el.scrollTop - el.clientHeight) < 100
  },
  { flush: 'pre' }
)

watch(
  scrollTrigger,
  async () => {
    if (isPrepending) return
    await nextTick()
    if (containerRef.value) {
      if (generating.value || wasAtBottom) {
        containerRef.value.scrollTop = containerRef.value.scrollHeight
      }
    }
  },
  { flush: 'post' },
)
</script>
