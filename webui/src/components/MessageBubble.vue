<template>
  <div class="message" :class="role">
    <div class="message-header">
      <span class="role-label">{{ role === 'user' ? 'YOU' : 'AGENT' }}</span>
      <button
        v-if="hasPrefix"
        class="toggle-btn"
        @click="expanded = !expanded"
      >{{ expanded ? '收起' : '展开运行时上下文' }}</button>
    </div>

    <!-- 折叠区：仅 user 消息且后端注入了前缀时展示 -->
    <div
      v-if="hasPrefix && expanded"
      class="message-prefix"
      v-html="renderedPrefix"
    ></div>

    <!-- 主消息体：始终展示。streaming 时在末尾追加打字机光标。 -->
    <div class="message-content" :class="{ streaming }" v-html="renderedContent"></div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { renderMarkdown } from '../markdown'

const props = defineProps<{
  role: 'user' | 'assistant'
  content: string
  prefix?: string
  streaming?: boolean
}>()

// prefix 由后端拆好（按哨兵分隔符），无前缀就是空串。无需任何启发式。
const hasPrefix = computed(() => props.role === 'user' && !!props.prefix && props.prefix.length > 0)

const expanded = ref(false)

const renderedContent = computed(() => renderMarkdown(props.content || ''))
const renderedPrefix = computed(() => renderMarkdown(props.prefix || ''))
</script>

<style scoped>
.toggle-btn {
  background: transparent;
  border: 1px solid var(--pill-border);
  color: var(--body);
  font-size: 12px;
  font-family: inherit;
  font-weight: 400;
  line-height: 16px;
  padding: 2px var(--sp-md);
  border-radius: var(--radius-pill);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.toggle-btn:hover {
  border-color: var(--pill-border-hover);
  background: var(--canvas-soft);
  color: var(--ink);
}

/* 折叠的运行时上下文：recessed 暗面 + 发丝左边线。
   overflow-x: hidden 防止宽 pre/table 撑出容器横向滚动条；
   内部 pre/table 自带 overflow-x: auto，由它们各自横向滚动。 */
.message-prefix {
  font-size: 13px;
  line-height: 1.6;
  padding: var(--sp-sm) var(--sp-md);
  background: var(--canvas);
  border: 1px solid var(--hairline);
  border-left: 2px solid var(--pill-border);
  border-radius: var(--radius-sm);
  color: var(--body-mid);
  margin-bottom: var(--sp-sm);
  max-height: 400px;
  overflow-x: hidden;
  overflow-y: auto;
}
.message-prefix :deep(p) { margin-bottom: 4px; }
.message-prefix :deep(code) {
  background: var(--canvas-mid);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 12px;
}
.message-prefix :deep(pre) {
  background: var(--canvas-mid);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  padding: var(--sp-sm) var(--sp-md);
  overflow-x: auto;
  margin: var(--sp-xs) 0;
}
.message-prefix :deep(pre code) {
  background: none;
  padding: 0;
  font-size: 12px;
  line-height: 18px;
}
.message-prefix :deep(ul),
.message-prefix :deep(ol) {
  padding-left: var(--sp-lg);
  margin-bottom: 4px;
}
.message-prefix :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: var(--sp-xs) 0;
  font-size: 12px;
  display: block;
  overflow-x: auto;
}
.message-prefix :deep(th),
.message-prefix :deep(td) {
  border: 1px solid var(--hairline);
  padding: var(--sp-xs) var(--sp-sm);
  text-align: left;
  vertical-align: top;
}

/* 打字机光标：作为最后一个子元素的内联伪元素，跟在正文末尾闪烁。 */
.message-content.streaming > :last-child::after {
  content: '▋';
  display: inline-block;
  margin-left: 2px;
  color: var(--ink);
  animation: cursor-blink 1s steps(2, start) infinite;
}
@keyframes cursor-blink {
  0%, 50% { opacity: 1; }
  51%, 100% { opacity: 0; }
}
</style>