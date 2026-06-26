<template>
  <div class="messages-container" ref="containerRef">
    <div class="messages-list">
      <MessageBubble
        v-for="(m, idx) in messages"
        :key="idx"
        :role="m.role"
        :content="m.content"
        :prefix="m.prefix"
        :timestamp="m.timestamp"
        :tokens="m.tokens"
        :streaming="isStreaming(idx)"
      />
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
import { ref, watch, nextTick, computed } from 'vue'
import MessageBubble from './MessageBubble.vue'
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

watch(
  scrollTrigger,
  async () => {
    await nextTick()
    if (containerRef.value) {
      containerRef.value.scrollTop = containerRef.value.scrollHeight
    }
  },
  { flush: 'post' },
)
</script>
