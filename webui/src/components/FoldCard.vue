<template>
  <div class="fold-card">
    <div class="fold-card-header" @click="toggle">
      <span class="fold-icon">📦</span>
      <span class="fold-title">
        已折叠 {{ fold.dropped_count }} 条对话 · 第 {{ fold.compression_id }} 次压缩
      </span>
      <span class="fold-toggle">{{ expanded ? '收起' : '展开' }}</span>
    </div>

    <!-- 摘要预览：折叠态下展示摘要首段，帮助用户判断是否值得展开。 -->
    <div v-if="!expanded && fold.summary_excerpt" class="fold-excerpt">
      {{ fold.summary_excerpt }}
    </div>

    <!-- 展开区：调 fetchFoldedMessages 取回被折叠的原始消息，内联渲染为 MessageBubble 列表。 -->
    <div v-if="expanded" class="fold-expanded">
      <div v-if="loading" class="fold-loading">加载中…</div>
      <div v-else-if="error" class="fold-error">加载失败：{{ error }}</div>
      <template v-else>
        <MessageBubble
          v-for="(m, i) in foldedMessages"
          :key="i"
          :role="m.role"
          :content="m.content"
          :prefix="m.prefix"
          :timestamp="m.timestamp"
          :tokens="m.tokens"
        />
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import MessageBubble from './MessageBubble.vue'
import { fetchFoldedMessages } from '../api'
import type { MessageItem } from '../api'

interface FoldInfo {
  compression_id: number
  dropped_count: number
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
const error = ref('')
const foldedMessages = ref<MessageItem[]>([])

async function toggle() {
  if (!expanded.value) {
    expanded.value = true
    // 首次展开才拉取，避免重复请求
    if (foldedMessages.value.length === 0 && !error.value) {
      await load()
    }
  } else {
    expanded.value = false
  }
}

async function load() {
  if (!props.sessionId) {
    error.value = '缺少 session_id'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const res = await fetchFoldedMessages(
      props.sessionId,
      props.fold.compression_id,
      props.role || undefined,
    )
    foldedMessages.value = res.messages ?? []
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.fold-card {
  margin: 8px 0;
  border: 1px dashed var(--border-color, #555);
  border-radius: 8px;
  background: var(--fold-bg, rgba(128, 128, 128, 0.06));
  overflow: hidden;
}
.fold-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
}
.fold-card-header:hover {
  background: var(--fold-hover, rgba(128, 128, 128, 0.1));
}
.fold-icon {
  font-size: 0.95em;
}
.fold-title {
  flex: 1;
  font-size: 0.85em;
  color: var(--secondary-text, #aaa);
}
.fold-toggle {
  font-size: 0.8em;
  color: var(--accent, #4aa);
}
.fold-excerpt {
  padding: 0 12px 10px;
  font-size: 0.8em;
  color: var(--tertiary-text, #888);
  white-space: pre-wrap;
  line-height: 1.5;
}
.fold-expanded {
  padding: 4px 8px 8px;
  border-top: 1px solid var(--border-color, #444);
}
.fold-loading,
.fold-error {
  padding: 12px;
  font-size: 0.85em;
  color: var(--tertiary-text, #888);
}
.fold-error {
  color: var(--error, #c66);
}
</style>
