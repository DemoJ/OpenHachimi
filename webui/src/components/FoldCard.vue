<template>
  <!-- 压缩标记:放在压缩发生的那一轮之后,只是一条提示,聊天记录本身不受影响。
       展开看到的是 AI 此后实际收到的摘要。 -->
  <div class="fold-marker">
    <div
      class="fold-header"
      role="button"
      tabindex="0"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
      @keydown.enter.prevent="expanded = !expanded"
      @keydown.space.prevent="expanded = !expanded"
    >
      <span class="fold-line"></span>
      <span class="fold-title">
        上下文已压缩（第 {{ fold.compression_id }} 次）· 之前 {{ fold.compressed_count }} 条消息以摘要形式提供给 AI，聊天记录完整保留
      </span>
      <span class="fold-toggle">{{ expanded ? '收起摘要 ▲' : '查看摘要 ▼' }}</span>
      <span class="fold-line"></span>
    </div>
    <div v-if="expanded" class="fold-detail">{{ fold.summary || fold.summary_excerpt }}</div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { MessageItem } from '../api'

defineProps<{ fold: NonNullable<MessageItem['fold']> }>()

const expanded = ref(false)
</script>

<style scoped>
.fold-marker {
  margin: 4px 0;
}
.fold-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 0;
  user-select: none;
  cursor: pointer;
  font-size: 0.75em;
  color: var(--body-mid, #888);
}
.fold-header:hover .fold-title,
.fold-header:hover .fold-toggle { color: var(--ink, #ededed); }
.fold-line {
  flex: 1;
  min-width: 16px;
  border-top: 1px dashed var(--hairline, #2a2c31);
}
.fold-title {
  text-align: center;
}
.fold-toggle {
  white-space: nowrap;
}
.fold-detail {
  margin-top: 6px;
  padding: 10px 12px;
  border: 1px dashed var(--hairline, #2a2c31);
  border-radius: 8px;
  background: var(--canvas-soft, rgba(128, 128, 128, 0.06));
  max-height: 360px;
  overflow-y: auto;
  font-size: 0.8em;
  line-height: 1.6;
  color: var(--body-mid, #aaa);
  white-space: pre-wrap;
  word-break: break-word;
}
@media (max-width: 640px) {
  .fold-header { flex-wrap: wrap; }
  .fold-line { display: none; }
}
</style>
