<template>
  <div class="fold-marker">
    <div
      class="fold-header"
      role="button"
      :tabindex="sessionId ? 0 : -1"
      :aria-expanded="expanded"
      @click="toggle"
      @keydown.enter.prevent="toggle"
      @keydown.space.prevent="toggle"
    >
      <span class="fold-icon">📦</span>
      <span class="fold-title">
        此段 {{ fold.compressed_count }} 条对话已压缩为摘要提供给 AI · 第 {{ fold.compression_id }} 次压缩
      </span>
      <span v-if="sessionId" class="fold-toggle">{{ expanded ? '收起 ▲' : '展开查看 ▼' }}</span>
    </div>
    <!-- 摘要预览：展示 AI 实际收到的摘要首段。 -->
    <div v-if="fold.summary_excerpt && !expanded" class="fold-excerpt">
      {{ fold.summary_excerpt }}
    </div>
    <!-- 展开的原始消息:按需从后端 /messages/folded/{compression_id} 拉取。 -->
    <div v-if="expanded" class="fold-detail">
      <div v-if="loading" class="fold-status">加载中…</div>
      <div v-else-if="errorText" class="fold-status fold-error">{{ errorText }}</div>
      <template v-else>
        <div v-for="(m, i) in foldedMessages" :key="i" class="fold-message" :class="`fold-message--${m.role}`">
          <span class="fold-message-role">{{ m.role === 'user' ? '我' : 'AI' }}</span>
          <span class="fold-message-content">{{ m.content }}</span>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { getFoldedMessages, type MessageItem } from '../api'

interface FoldInfo {
  compression_id: number
  compressed_count: number
  summary_excerpt: string
  head_end_turn: number
  tail_start_turn: number
}

const props = defineProps<{
  fold: FoldInfo
  sessionId: string | null
  role: string
}>()

const expanded = ref(false)
const loading = ref(false)
const errorText = ref('')
const foldedMessages = ref<MessageItem[]>([])

async function toggle() {
  expanded.value = !expanded.value
  if (expanded.value && !foldedMessages.value.length && props.sessionId) {
    loading.value = true
    errorText.value = ''
    try {
      const res = await getFoldedMessages(props.sessionId, props.fold.compression_id)
      foldedMessages.value = res.messages
    } catch (err) {
      errorText.value = `加载被折叠的消息失败：${err instanceof Error ? err.message : String(err)}`
    } finally {
      loading.value = false
    }
  }
}
</script>

<style scoped>
.fold-marker {
  margin: 8px 0;
  border: 1px dashed var(--hairline, #212327);
  border-radius: 8px;
  background: var(--canvas-soft, rgba(128, 128, 128, 0.06));
}
.fold-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  user-select: none;
  cursor: pointer;
}
.fold-header:hover .fold-title { color: var(--ink, #ededed); }
.fold-icon {
  font-size: 0.95em;
}
.fold-title {
  flex: 1;
  font-size: 0.85em;
  color: var(--body-mid, #aaa);
}
.fold-toggle {
  font-size: 0.75em;
  color: var(--body-mid, #888);
  white-space: nowrap;
}
.fold-excerpt {
  padding: 0 12px 10px;
  font-size: 0.8em;
  color: var(--body-mid, #888);
  white-space: pre-wrap;
  line-height: 1.5;
}
.fold-detail {
  border-top: 1px solid var(--hairline, #212327);
  padding: 8px 12px 10px;
  max-height: 320px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.fold-status { font-size: 0.8em; color: var(--body-mid, #888); }
.fold-error { color: #e5484d; }
.fold-message {
  display: flex;
  gap: 8px;
  font-size: 0.8em;
  line-height: 1.5;
}
.fold-message-role {
  flex: none;
  color: var(--body-mid, #888);
  font-size: 0.9em;
}
.fold-message-content {
  color: var(--body-mid, #aaa);
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
