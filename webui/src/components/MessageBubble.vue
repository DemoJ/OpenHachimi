<template>
  <div class="message" :class="role">
    <div class="message-header">
      <span class="role-label">{{ role === 'user' ? 'YOU' : 'AGENT' }}</span>
      <span class="message-meta">
        <span v-if="formattedTime" class="meta-time" :title="rawTimeTitle">{{ formattedTime }}</span>
        <span v-if="tokenLabel" class="meta-tokens" :title="tokenTitle">{{ tokenLabel }}</span>
        <button
          v-if="hasPrefix"
          class="toggle-btn"
          @click="expanded = !expanded"
        >{{ expanded ? '收起' : '展开运行时上下文' }}</button>
      </span>
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
  timestamp?: string | null
  tokens?: { input: number; output: number; total: number; cache_read?: number } | null
}>()

// prefix 由后端拆好（按哨兵分隔符），无前缀就是空串。无需任何启发式。
const hasPrefix = computed(() => props.role === 'user' && !!props.prefix && props.prefix.length > 0)

const expanded = ref(false)

const renderedContent = computed(() => renderMarkdown(props.content || ''))
const renderedPrefix = computed(() => renderMarkdown(props.prefix || ''))

// ---- 时间展示 ----
// 显示策略：当天的消息只显示 HH:mm，跨日的加上 MM-DD。title 给出完整本地时间。
function pad(n: number): string {
  return n < 10 ? '0' + n : String(n)
}
const parsedTime = computed<Date | null>(() => {
  if (!props.timestamp) return null
  const d = new Date(props.timestamp)
  return isNaN(d.getTime()) ? null : d
})
const formattedTime = computed<string>(() => {
  const d = parsedTime.value
  if (!d) return ''
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return sameDay ? hm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`
})
const rawTimeTitle = computed<string>(() => {
  const d = parsedTime.value
  return d ? d.toLocaleString() : ''
})

// ---- token 展示 ----
// 例：↑1.2k ↓318（总 1.5k）。input/output 都为 0 时不展示。
// 缓存命中 cache_read 不显示在 chip 上(避免拥挤),但放进 title。
function fmtTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0'
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k'
  return String(n)
}
const tokenLabel = computed<string>(() => {
  if (props.role !== 'assistant') return ''
  const t = props.tokens
  if (!t) return ''
  if (!t.input && !t.output) return ''
  return `↑${fmtTokens(t.input)} ↓${fmtTokens(t.output)}`
})
const tokenTitle = computed<string>(() => {
  const t = props.tokens
  if (!t) return ''
  const parts = [`输入 ${t.input}`, `输出 ${t.output}`, `合计 ${t.total} tokens`]
  if (typeof t.cache_read === 'number' && t.cache_read > 0) {
    parts.push(`缓存命中 ${t.cache_read} tokens`)
  }
  return parts.join(' · ')
})
</script>

<style scoped>
.message-meta {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-sm);
}
.meta-time,
.meta-tokens {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  line-height: 16px;
  color: var(--body-mid);
  letter-spacing: 0.4px;
}
.meta-tokens {
  padding: 1px 6px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-pill);
  white-space: nowrap;
}

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