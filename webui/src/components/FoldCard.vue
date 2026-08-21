<template>
  <div class="fold-marker">
    <div class="fold-header">
      <span class="fold-icon">📦</span>
      <span class="fold-title">
        此段 {{ fold.compressed_count }} 条对话已压缩为摘要提供给 AI · 第 {{ fold.compression_id }} 次压缩
      </span>
    </div>
    <!-- 摘要预览：展示 AI 实际收到的摘要首段，原始消息始终完整显示在上下消息流中。 -->
    <div v-if="fold.summary_excerpt" class="fold-excerpt">
      {{ fold.summary_excerpt }}
    </div>
  </div>
</template>

<script setup lang="ts">
interface FoldInfo {
  compression_id: number
  compressed_count: number
  summary_excerpt: string
  head_end_turn: number
  tail_start_turn: number
}

defineProps<{
  fold: FoldInfo
}>()
</script>

<style scoped>
.fold-marker {
  margin: 8px 0;
  border: 1px dashed var(--border-color, #555);
  border-radius: 8px;
  background: var(--fold-bg, rgba(128, 128, 128, 0.06));
}
.fold-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  user-select: none;
}
.fold-icon {
  font-size: 0.95em;
}
.fold-title {
  flex: 1;
  font-size: 0.85em;
  color: var(--secondary-text, #aaa);
}
.fold-excerpt {
  padding: 0 12px 10px;
  font-size: 0.8em;
  color: var(--tertiary-text, #888);
  white-space: pre-wrap;
  line-height: 1.5;
}
</style>
