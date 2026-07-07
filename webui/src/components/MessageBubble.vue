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

    <!-- 附件区：图片缩略图 + 文件下载链接 -->
    <div v-if="hasAttachments" class="message-attachments">
      <!-- 图片网格 -->
      <div v-if="imageAttachments.length" class="att-image-grid">
        <div
          v-for="att in imageAttachments"
          :key="att.id"
          class="att-image-item"
          @click="previewImage(att)"
        >
          <img
            :src="attachmentDownloadUrl(att.local_path)"
            :alt="att.filename || ''"
            loading="lazy"
          />
        </div>
      </div>
      <!-- 非图片文件列表 -->
      <div v-if="fileAttachments.length" class="att-file-list">
        <a
          v-for="att in fileAttachments"
          :key="att.id"
          class="att-file-item"
          :href="attachmentDownloadUrl(att.local_path)"
          :download="att.filename || ''"
          target="_blank"
          rel="noopener"
        >
          <span class="att-file-icon">{{ fileIcon(att) }}</span>
          <span class="att-file-name">{{ att.filename || '未知文件' }}</span>
          <span v-if="att.size_bytes" class="att-file-size">{{ formatSize(att.size_bytes) }}</span>
          <span class="att-file-dl">下载</span>
        </a>
      </div>
    </div>

    <!-- 产物区:agent 生成的文件(图片缩略图 + 文件下载链接) -->
    <div v-if="hasArtifacts" class="message-attachments">
      <div v-if="artifactImages.length" class="att-image-grid">
        <div
          v-for="art in artifactImages"
          :key="art.id"
          class="att-image-item"
          @click="previewArtifactImage(art)"
        >
          <img
            :src="artifactDownloadUrl(art.download_url || '')"
            :alt="art.filename"
            loading="lazy"
          />
        </div>
      </div>
      <div v-if="artifactFiles.length" class="att-file-list">
        <a
          v-for="art in artifactFiles"
          :key="art.id"
          class="att-file-item"
          :href="artifactDownloadUrl(art.download_url || '')"
          :download="art.filename"
          target="_blank"
          rel="noopener"
        >
          <span class="att-file-icon">{{ artifactFileIcon(art) }}</span>
          <span class="att-file-name">{{ art.filename }}</span>
          <span v-if="art.size_bytes" class="att-file-size">{{ formatSize(art.size_bytes) }}</span>
          <span class="att-file-dl">下载</span>
        </a>
      </div>
    </div>

    <!-- 大图预览灯箱 -->
    <div v-if="lightboxSrc" class="lightbox" @click="closeLightbox">
      <img :src="lightboxSrc" class="lightbox-img" @click.stop />
      <button class="lightbox-close" @click="closeLightbox">×</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { renderMarkdown } from '../markdown'
import { attachmentDownloadUrl, artifactDownloadUrl, isArtifactImage, type AttachmentRef, type ArtifactRef } from '../api'

const props = defineProps<{
  role: 'user' | 'assistant'
  content: string
  prefix?: string
  streaming?: boolean
  timestamp?: string | null
  tokens?: { input: number; output: number; total: number; cache_read?: number } | null
  attachments?: AttachmentRef[] | null
  artifacts?: ArtifactRef[] | null
}>()

// prefix 由后端拆好（按哨兵分隔符），无前缀就是空串。无需任何启发式。
const hasPrefix = computed(() => props.role === 'user' && !!props.prefix && props.prefix.length > 0)

const expanded = ref(false)

const renderedContent = computed(() => renderMarkdown(props.content || ''))
const renderedPrefix = computed(() => renderMarkdown(props.prefix || ''))

// ---- 附件 ----
const hasAttachments = computed(() => !!props.attachments && props.attachments.length > 0)
const imageAttachments = computed(() =>
  (props.attachments || []).filter(a => a.kind === 'image'),
)
const fileAttachments = computed(() =>
  (props.attachments || []).filter(a => a.kind !== 'image'),
)

// ---- 产物(agent 生成的文件) ----
const hasArtifacts = computed(() => !!props.artifacts && props.artifacts.length > 0)
const artifactImages = computed(() =>
  (props.artifacts || []).filter(a => isArtifactImage(a)),
)
const artifactFiles = computed(() =>
  (props.artifacts || []).filter(a => !isArtifactImage(a)),
)

const lightboxSrc = ref('')
function previewImage(att: AttachmentRef) {
  lightboxSrc.value = attachmentDownloadUrl(att.local_path)
}
function closeLightbox() {
  lightboxSrc.value = ''
}

function previewArtifactImage(art: ArtifactRef) {
  lightboxSrc.value = artifactDownloadUrl(art.download_url || '')
}

function artifactFileIcon(art: ArtifactRef): string {
  const name = art.filename || ''
  const ext = name.split('.').pop()?.toLowerCase() || ''
  if ((art.content_type || '').startsWith('audio/')) return '🎵'
  if ((art.content_type || '').startsWith('video/')) return '🎬'
  if (ext === 'pdf') return '📄'
  if (['zip', 'rar', '7z', 'tar', 'gz'].includes(ext)) return '📦'
  if (['doc', 'docx'].includes(ext)) return '📝'
  if (['xls', 'xlsx', 'csv'].includes(ext)) return '📊'
  if (['py', 'js', 'ts', 'json', 'yaml', 'yml', 'xml', 'html', 'css', 'go', 'rs', 'java', 'c', 'cpp', 'sh'].includes(ext)) return '⚙️'
  return '📎'
}

function fileIcon(att: AttachmentRef): string {
  const name = att.filename || ''
  const ext = name.split('.').pop()?.toLowerCase() || ''
  if (att.kind === 'audio') return '🎵'
  if (att.kind === 'video') return '🎬'
  if (ext === 'pdf') return '📄'
  if (['zip', 'rar', '7z', 'tar', 'gz'].includes(ext)) return '📦'
  if (['doc', 'docx'].includes(ext)) return '📝'
  if (['xls', 'xlsx', 'csv'].includes(ext)) return '📊'
  if (['py', 'js', 'ts', 'json', 'yaml', 'yml', 'xml', 'html', 'css', 'go', 'rs', 'java', 'c', 'cpp', 'sh'].includes(ext)) return '⚙️'
  return '📎'
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

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

/* 附件区 */
.message-attachments {
  margin-top: var(--sp-sm);
}
.att-image-grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-sm);
}
.att-image-item {
  width: 120px;
  height: 120px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  cursor: pointer;
  border: 1px solid var(--hairline);
  background: var(--canvas);
  transition: border-color 0.15s, transform 0.15s;
}
.att-image-item:hover {
  border-color: var(--pill-border-hover);
  transform: scale(1.02);
}
.att-image-item img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.att-file-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-xs);
}
.att-file-item {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  padding: var(--sp-sm) var(--sp-md);
  background: var(--canvas);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  color: var(--body);
  font-size: 14px;
  text-decoration: none;
  transition: border-color 0.15s, background 0.15s;
}
.att-file-item:hover {
  border-color: var(--pill-border-hover);
  background: var(--canvas-soft);
  color: var(--ink);
  text-decoration: none;
}
.att-file-icon {
  font-size: 18px;
  line-height: 1;
  flex-shrink: 0;
}
.att-file-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.att-file-size {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: var(--body-mid);
  flex-shrink: 0;
}
.att-file-dl {
  font-size: 12px;
  color: var(--body-mid);
  flex-shrink: 0;
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
