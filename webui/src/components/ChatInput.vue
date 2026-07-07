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
          <span v-if="att.status === 'uploading'" class="att-chip-status">上传中…</span>
          <span v-if="att.status === 'error'" class="att-chip-status att-chip-error-text">失败</span>
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
          :placeholder="generating ? '生成中…' : '说点什么（Enter 发送，Shift+Enter 换行）'"
          :disabled="generating"
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

    <!-- 大图预览灯箱 -->
    <div v-if="lightboxUrl" class="lightbox" @click="lightboxUrl = ''">
      <img :src="lightboxUrl" class="lightbox-img" @click.stop />
      <button class="lightbox-close" @click="lightboxUrl = ''">×</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, computed } from 'vue'
import { uploadAttachment, type AttachmentRef } from '../api'

const text = ref('')
const taRef = ref<HTMLTextAreaElement | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const lightboxUrl = ref('')

interface PendingAttachment {
  name: string
  status: 'uploading' | 'done' | 'error'
  ref?: AttachmentRef
  previewUrl?: string
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
    }
    if (file.type.startsWith('image/')) {
      pending.previewUrl = URL.createObjectURL(file)
    }
    pendingAttachments.value.push(pending)
    const idx = pendingAttachments.value.length - 1
    uploadAttachment(file)
      .then((ref) => {
        pendingAttachments.value[idx].status = 'done'
        pendingAttachments.value[idx].ref = ref
      })
      .catch((err) => {
        console.warn('[ChatInput] upload failed', file.name, err)
        pendingAttachments.value[idx].status = 'error'
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
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    onSend()
  }
}

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