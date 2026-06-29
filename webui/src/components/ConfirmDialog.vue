<template>
  <!-- 通用确认弹窗:用于删除等不可逆操作的二次确认。
       视觉范式与 RoleEditDialog 一致 —— teleport 到 body、半透明黑遮罩、
       canvas-soft 卡片 + 发丝边 + 8px 圆角、无阴影、head/body/foot 三段。
       确认按钮走 sunset 橙描边胶囊(与 .btn-stop 同语义:停止/错误/破坏性)。 -->
  <teleport to="body">
    <div class="confirm-overlay" @click.self="onCancel">
      <div class="confirm-dialog" role="dialog" aria-modal="true">
        <div class="confirm-head">
          <h3>{{ title }}</h3>
          <button type="button" class="confirm-close" @click="onCancel" aria-label="关闭">✕</button>
        </div>

        <div class="confirm-body">
          <p v-if="message">{{ message }}</p>
          <slot />
        </div>

        <div class="confirm-foot">
          <button type="button" class="btn" :disabled="loading" @click="onCancel">
            {{ cancelText }}
          </button>
          <button
            type="button"
            class="btn confirm-btn-danger"
            :disabled="loading"
            @click="onConfirm"
          >
            {{ loading ? '处理中…' : confirmText }}
          </button>
        </div>
      </div>
    </div>
  </teleport>
</template>

<script setup lang="ts">
// 通用二次确认弹窗。父组件用 v-if 控制显隐,确认/取消通过 emit 通知;
// loading 期间禁用两枚按钮并阻止遮罩/关闭按钮关闭,避免删一半被中断。
const props = withDefaults(
  defineProps<{
    title?: string
    message?: string
    confirmText?: string
    cancelText?: string
    loading?: boolean
  }>(),
  {
    title: '确认操作',
    message: '',
    confirmText: '确认',
    cancelText: '取消',
    loading: false,
  },
)

const emit = defineEmits<{
  confirm: []
  cancel: []
}>()

function onCancel() {
  if (props.loading) return
  emit('cancel')
}

function onConfirm() {
  if (props.loading) return
  emit('confirm')
}
</script>

<style scoped>
/* 遮罩:半透明黑盖住全屏,点击空白关闭。 */
.confirm-overlay {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-xl);
}
/* 弹窗卡片:canvas-soft + 发丝边 + 8px 圆角,无阴影(遵循设计规范)。 */
.confirm-dialog {
  width: 440px;
  max-width: 92vw;
  display: flex;
  flex-direction: column;
  background: var(--canvas-soft);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
}
.confirm-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-lg) var(--sp-xl);
  border-bottom: 1px solid var(--hairline);
  flex-shrink: 0;
}
.confirm-head h3 {
  font-size: 18px;
  font-weight: 400;
  line-height: 24px;
  letter-spacing: -0.2px;
  color: var(--ink);
}
.confirm-close {
  background: transparent;
  border: none;
  color: var(--body-mid);
  font-size: 16px;
  cursor: pointer;
  padding: var(--sp-xs) var(--sp-sm);
  border-radius: var(--radius-sm);
  transition: color 0.15s, background 0.15s;
}
.confirm-close:hover { color: var(--ink); background: var(--canvas); }

.confirm-body {
  padding: var(--sp-lg) var(--sp-xl);
}
.confirm-body p {
  font-size: 14px;
  line-height: 22px;
  color: var(--body);
}

.confirm-foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-sm);
  padding: var(--sp-lg) var(--sp-xl);
  border-top: 1px solid var(--hairline);
  flex-shrink: 0;
}

/* 确认(破坏性)按钮:sunset 橙描边胶囊,与 .btn-stop 同语义。
   透明底 + 半透明橙边 + 橙字,hover 转半透明橙填充。 */
.confirm-btn-danger {
  border: 1px solid rgba(255, 122, 23, 0.45);
  background: transparent;
  color: var(--accent);
  transition: background 0.15s, border-color 0.15s;
}
.confirm-btn-danger:hover:not(:disabled) {
  background: rgba(255, 122, 23, 0.08);
  border-color: var(--accent);
}
.confirm-btn-danger:disabled { opacity: 0.5; cursor: not-allowed; }

@media (max-width: 768px) {
  .confirm-dialog { width: 100%; }
}
</style>
