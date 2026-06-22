<template>
  <div class="messages-container" ref="containerRef">
    <div class="messages-list">
      <MessageBubble
        v-for="(m, idx) in messages"
        :key="idx"
        :role="m.role"
        :content="m.content"
        :prefix="m.prefix"
      />
      <div v-if="messages.length === 0" style="text-align: center; color: var(--text-muted); margin-top: 40px;">
        开始你的第一段对话吧
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import MessageBubble from './MessageBubble.vue'
import type { MessageItem } from '../api'

const props = defineProps<{ messages: MessageItem[] }>()
const containerRef = ref<HTMLElement | null>(null)

watch(
  () => props.messages.map((m) => m.content).join('|'),
  async () => {
    await nextTick()
    if (containerRef.value) {
      containerRef.value.scrollTop = containerRef.value.scrollHeight
    }
  },
  { flush: 'post' },
)
</script>