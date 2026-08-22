<template>
  <div class="input-area">
    <div class="input-row">
      <!-- 待发送的附件预览条:独立于输入框,在其上方展示 -->
      <div v-if="pendingAttachments.length > 0" class="att-preview-bar">
        <div
          v-for="(att, idx) in pendingAttachments"
          :key="idx"
          class="att-chip"
          :class="{ 'att-chip--error': att.status === 'error' }"
        >
          <img
            v-if="att.previewUrl"
            :src="att.previewUrl"
            class="att-chip-thumb"
            @click="previewImage(att.previewUrl!)"
          />
          <span v-else class="att-chip-icon">{{ fileIcon(att.name) }}</span>
          <span class="att-chip-name">{{ att.name }}</span>
          <span v-if="att.status === 'uploading'" class="att-chip-status">上传中{{ att.progress !== null && att.progress !== undefined ? ` ${att.progress}%` : '…' }}</span>
          <span v-if="att.status === 'error'" class="att-chip-status att-chip-error-text" :title="att.errorText || ''">失败{{ att.errorText ? `：${att.errorText}` : '' }}</span>
          <button
            v-if="!generating"
            class="att-chip-remove"
            @click="removeAttachment(idx)"
            title="移除"
          >×</button>
        </div>
      </div>

      <div class="chat-input-shell">
        <textarea
          v-model="text"
          ref="taRef"
          :placeholder="generating ? '生成中…（可先输入，回车排队，生成结束自动发送）' : '说点什么（Enter 发送，Shift+Enter 换行）'"
          @keydown="onKey"
          @input="autoResize"
          @paste="onPaste"
          @dragover.prevent
          @drop.prevent="onDrop"
          rows="1"
        />
        <button
          class="btn-attach"
          @click="triggerFileInput"
          :disabled="generating"
          title="添加附件"
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
          </svg>
        </button>
        <button v-if="!generating" class="btn-send" :disabled="!canSend" @click="onSend" title="发送">发送</button>
        <button v-else class="btn-stop" @click="onStop" title="停止生成">停止</button>
        <input
          type="file"
          multiple
          ref="fileInputRef"
          @change="onFileSelect"
          style="display:none"
        />
      </div>
    </div>

    <!-- 生成期间的排队草稿:任务结束后自动发送 -->
    <div v-if="queuedText !== null" class="queue-bar">
      <span class="queue-label">生成结束后自动发送：</span>
      <span class="queue-text">{{ queuedText }}</span>
      <button class="queue-remove" title="取消排队" @click="queuedText = null">×</button>
    </div>

    <!-- 大图预览灯箱 -->
    <div v-if="lightboxUrl" class="lightbox" @click="lightboxUrl = ''">
      <img :src="lightboxUrl" class="lightbox-img" @click.stop />
      <button class="lightbox-close" @click="lightboxUrl = ''">×</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, computed, onMounted, onUnmounted } from 'vue'
import { uploadAttachment, type AttachmentRef } from '../api'

const text = ref('')
const taRef = ref<HTMLTextAreaElement | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const lightboxUrl = ref('')
// 生成期间回车发送的消息先进排队,任务结束后自动发出(不再丢弃或禁输入)。
const queuedText = ref<string | null>(null)

interface PendingAttachment {
  name: string
  status: 'uploading' | 'done' | 'error'
  ref?: AttachmentRef
  previewUrl?: string
  progress?: number | null
  errorText?: string
}

const pendingAttachments = ref<PendingAttachment[]>([])

const props = defineProps<{ generating: boolean }>()
const emit = defineEmits<{
  (e: 'send', text: string, attachments: AttachmentRef[]): void
  (e: 'stop'): void
}>()

const canSend = computed(() => {
  const hasText = text.value.trim().length > 0
  const hasReadyAttachments = pendingAttachments.value.some(a => a.status === 'done')
  return hasText || hasReadyAttachments
})

function triggerFileInput() {
  fileInputRef.value?.click()
}

function onFileSelect(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files) {
    handleFiles(Array.from(input.files))
  }
  input.value = ''
}

function onPaste(e: ClipboardEvent) {
  const files = e.clipboardData?.files
  if (files && files.length > 0) {
    handleFiles(Array.from(files))
  }
}

function onDrop(e: DragEvent) {
  const files = e.dataTransfer?.files
  if (files && files.length > 0) {
    handleFiles(Array.from(files))
  }
}

function handleFiles(files: File[]) {
  for (const file of files) {
      const pending: PendingAttachment = {
        name: file.name,
        status: 'uploading',
        progress: null,
      }
      if (file.type.startsWith('image/')) {
        pending.previewUrl = URL.createObjectURL(file)
      }
      pendingAttachments.value.push(pending)
      const idx = pendingAttachments.value.length - 1
      uploadAttachment(file, (percent) => {
        pendingAttachments.value[idx].progress = percent
      })
        .then((ref) => {
          pendingAttachments.value[idx].status = 'done'
          pendingAttachments.value[idx].ref = ref
        })
        .catch((err) => {
          console.warn('[ChatInput] upload failed', file.name, err)
          pendingAttachments.value[idx].status = 'error'
          pendingAttachments.value[idx].errorText = err instanceof Error ? err.message : String(err)
        })
    }
  }

function removeAttachment(idx: number) {
  const att = pendingAttachments.value[idx]
  if (att.previewUrl) URL.revokeObjectURL(att.previewUrl)
  pendingAttachments.value.splice(idx, 1)
}

function previewImage(url: string) {
  lightboxUrl.value = url
}

function fileIcon(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  if (['mp3', 'm4a', 'ogg', 'wav', 'flac'].includes(ext)) return '🎵'
  if (['mp4', 'mov', 'avi', 'mkv'].includes(ext)) return '🎬'
  if (['pdf'].includes(ext)) return '📄'
  if (['zip', 'rar', '7z', 'tar', 'gz'].includes(ext)) return '📦'
  if (['doc', 'docx'].includes(ext)) return '📝'
  if (['xls', 'xlsx', 'csv'].includes(ext)) return '📊'
  if (['py', 'js', 'ts', 'json', 'yaml', 'yml', 'xml', 'html', 'css', 'go', 'rs', 'java', 'c', 'cpp', 'sh'].includes(ext)) return '⚙️'
  return '📎'
}

function onSend() {
  if (props.generating) return
  if (!canSend.value) return
  const v = text.value.trim()
  const ready = pendingAttachments.value
    .filter(a => a.status === 'done' && a.ref)
    .map(a => a.ref!) as AttachmentRef[]
  emit('send', v, ready)
  text.value = ''
  // 清理预览 URL
  for (const att of pendingAttachments.value) {
    if (att.previewUrl) URL.revokeObjectURL(att.previewUrl)
  }
  pendingAttachments.value = []
}

function onStop() {
  emit('stop')
}

function onKey(e: KeyboardEvent) {
  if (e.key !== 'Enter' || e.shiftKey) return
  // 中文输入法:候选词确认的 Enter 不能触发发送(isComposing 在部分浏览器
  // 上为 false,需要额外看 keyCode 229 这个历史兼容值)。
  if (e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  if (props.generating) {
    // 生成中:消息进排队区,任务结束后自动发送,不打断也不丢弃。
    if (canSend.value) {
      queuedText.value = text.value.trim()
      text.value = ''
    }
    return
  }
  onSend()
}

// 生成结束:自动发出排队中的草稿。
watch(() => props.generating, (generating) => {
  if (!generating && queuedText.value !== null) {
    const queued = queuedText.value
    queuedText.value = null
    text.value = queued
    nextTick(() => onSend())
  }
})

// 灯箱支持 Esc 关闭(此前只能点遮罩/×,键盘用户无法退出)。
function onWindowKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && lightboxUrl.value) lightboxUrl.value = ''
}
onMounted(() => window.addEventListener('keydown', onWindowKeydown))
onUnmounted(() => window.removeEventListener('keydown', onWindowKeydown))

// 自动撑高：随内容调整高度，最高 160px（与 CSS max-height 对应），超出后内部滚动
function autoResize() {
  const ta = taRef.value
  if (!ta) return
  ta.style.height = 'auto'
  ta.style.height = `${ta.scrollHeight}px`
}

watch(text, () => {
  // 清空发送后回缩到单行
  nextTick(autoResize)
})
</script>
<style scoped>
.queue-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-sm, 8px);
  padding: 6px var(--sp-lg, 16px);
  border-top: 1px solid var(--hairline, #212327);
  font-size: 12px;
  color: var(--body-mid, #7d8187);
}
.queue-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ink, #ededed);
}
.queue-remove {
  background: none;
  border: none;
  color: var(--body-mid, #7d8187);
  cursor: pointer;
  font-size: 14px;
  padding: 0 4px;
}
.queue-remove:hover { color: var(--ink, #ededed); }
</style>
