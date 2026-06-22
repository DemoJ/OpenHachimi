<template>
  <div class="message" :class="role">
    <div class="message-header">
      <span>{{ role === 'user' ? '你' : 'Agent' }}</span>
      <button
        v-if="hasPrefix"
        class="toggle-btn"
        @click="expanded = !expanded"
      >{{ expanded ? '收起完整内容' : '展开运行时上下文' }}</button>
    </div>

    <!-- 折叠区：仅 user 消息且后端注入了前缀时展示 -->
    <div
      v-if="hasPrefix && expanded"
      class="message-prefix"
      v-html="renderedPrefix"
    ></div>

    <!-- 主消息体：始终展示 -->
    <div class="message-content" v-html="renderedContent"></div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { renderMarkdown } from '../markdown'

const props = defineProps<{
  role: 'user' | 'assistant'
  content: string
  prefix?: string
}>()

// prefix 由后端拆好（按哨兵分隔符），无前缀就是空串。无需任何启发式。
const hasPrefix = computed(() => props.role === 'user' && !!props.prefix && props.prefix.length > 0)

const expanded = ref(false)

const renderedContent = computed(() => renderMarkdown(props.content || ''))
const renderedPrefix = computed(() => renderMarkdown(props.prefix || ''))
</script>

<style scoped>
.message-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.toggle-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  cursor: pointer;
  font-weight: normal;
  transition: all 0.15s;
}
.toggle-btn:hover {
  color: var(--accent);
  border-color: var(--accent-dim);
}

.message-prefix {
  font-size: 13px;
  line-height: 1.6;
  padding: 10px 14px;
  background: rgba(0, 0, 0, 0.2);
  border-left: 3px solid var(--accent-dim);
  border-radius: 4px;
  color: var(--text-secondary);
  margin-bottom: 6px;
  max-height: 400px;
  overflow-y: auto;
}
.message-prefix :deep(p) { margin-bottom: 4px; }
.message-prefix :deep(code) {
  background: rgba(0, 0, 0, 0.3);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 12px;
}
</style>