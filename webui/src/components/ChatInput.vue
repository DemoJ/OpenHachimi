<template>
  <div class="input-area">
    <div class="input-row">
      <div class="chat-input-shell">
        <textarea
          v-model="text"
          ref="taRef"
          :placeholder="generating ? '生成中…' : '说点什么（Enter 发送，Shift+Enter 换行）'"
          :disabled="generating"
          @keydown="onKey"
          @input="autoResize"
          rows="1"
        />
        <button v-if="!generating" class="btn-send" :disabled="!text.trim()" @click="onSend" title="发送">发送</button>
        <button v-else class="btn-stop" @click="onStop" title="停止生成">停止</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'

const text = ref('')
const taRef = ref<HTMLTextAreaElement | null>(null)

const props = defineProps<{ generating: boolean }>()
const emit = defineEmits<{
  (e: 'send', text: string): void
  (e: 'stop'): void
}>()

function onSend() {
  const v = text.value.trim()
  if (!v || props.generating) return
  emit('send', v)
  text.value = ''
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