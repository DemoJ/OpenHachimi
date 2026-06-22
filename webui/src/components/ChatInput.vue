<template>
  <div class="input-area">
    <div class="input-row">
      <textarea
        v-model="text"
        ref="taRef"
        :placeholder="generating ? '生成中…' : '说点什么（Enter 发送，Shift+Enter 换行）'"
        :disabled="generating"
        @keydown="onKey"
        rows="1"
      />
      <button v-if="!generating" class="btn-send" :disabled="!text.trim()" @click="onSend">发送</button>
      <button v-else class="btn-stop" @click="onStop">停止</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

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
</script>