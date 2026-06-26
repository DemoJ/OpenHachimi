<template>
  <div class="cfg-field" :class="{ 'is-secret': field.kind === 'secret' }">
    <label class="cfg-label">
      <span class="cfg-label-text">{{ field.label }}</span>
      <span class="cfg-path" v-if="showPath">{{ field.path }}</span>
    </label>

    <!-- secret:脱敏态显示掩码 + 修改按钮;编辑态显示 password 输入框 -->
    <div v-if="field.kind === 'secret'" class="cfg-control secret-control">
      <template v-if="secretMasked">
        <input
          class="cfg-input cfg-input-secret"
          type="text"
          :value="typeof modelValue === 'string' ? modelValue : ''"
          readonly
        />
        <button type="button" class="cfg-btn-mini" @click="$emit('unmask')">修改</button>
      </template>
      <template v-else>
        <input
          class="cfg-input"
          type="password"
          :value="typeof modelValue === 'string' ? modelValue : ''"
          :placeholder="placeholderText"
          @input="onInput(($event.target as HTMLInputElement).value)"
        />
        <button
          v-if="typeof modelValue === 'string' && modelValue.length > 0"
          type="button"
          class="cfg-btn-mini"
          @click="onInput('')"
        >清除</button>
      </template>
    </div>

    <!-- bool:开关 -->
    <div v-else-if="field.kind === 'bool'" class="cfg-control">
      <button
        type="button"
        class="cfg-toggle"
        :class="{ on: !!modelValue }"
        role="switch"
        :aria-checked="!!modelValue"
        @click="onInput(!modelValue)"
      >
        <span class="cfg-toggle-knob" />
      </button>
      <span class="cfg-toggle-state">{{ modelValue ? '开启' : '关闭' }}</span>
    </div>

    <!-- select:下拉 -->
    <div v-else-if="field.kind === 'select'" class="cfg-control">
      <select
        class="cfg-input cfg-select"
        :value="String(modelValue ?? '')"
        @change="onInput(($event.target as HTMLSelectElement).value)"
      >
        <option v-for="opt in field.options" :key="opt" :value="opt">{{ opt }}</option>
      </select>
    </div>

    <!-- int:数字输入 -->
    <div v-else-if="field.kind === 'int'" class="cfg-control">
      <input
        class="cfg-input"
        type="number"
        inputmode="numeric"
        :value="modelValue"
        :placeholder="placeholderText"
        @input="onInput(parseInt(($event.target as HTMLInputElement).value, 10) || 0)"
      />
    </div>

    <!-- string:文本输入 -->
    <div v-else class="cfg-control">
      <input
        class="cfg-input"
        type="text"
        :value="typeof modelValue === 'string' ? modelValue : ''"
        :placeholder="placeholderText"
        @input="onInput(($event.target as HTMLInputElement).value)"
      />
    </div>

    <p class="cfg-desc" v-if="field.description">{{ field.description }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ConfigField } from '../api'

const props = defineProps<{
  field: ConfigField
  modelValue: string | number | boolean
  secretMasked?: boolean
  showPath?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | number | boolean): void
  (e: 'unmask'): void
}>()

const placeholderText = computed(() => {
  // 留空复用主模型的字段给一个提示;其余给"留空"。
  if (props.field.description && props.field.description.includes('留空')) {
    return '留空使用默认'
  }
  return ''
})

function onInput(v: string | number | boolean) {
  emit('update:modelValue', v)
}
</script>

<style scoped>
.cfg-field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-xs);
}
.cfg-label {
  display: flex;
  align-items: baseline;
  gap: var(--sp-sm);
}
.cfg-label-text {
  font-size: 13px;
  color: var(--ink);
  line-height: 18px;
}
.cfg-path {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10px;
  letter-spacing: 0.4px;
  color: var(--body-mid);
  opacity: 0.7;
}

.cfg-control {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
}
.cfg-input {
  flex: 1;
  min-width: 0;
  padding: var(--sp-sm) var(--sp-md);
  background: var(--canvas-soft);
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: 14px;
  font-family: inherit;
  font-weight: 400;
  outline: none;
  transition: border-color 0.15s;
}
.cfg-input:focus { border-color: var(--pill-border-hover); }
.cfg-input::placeholder { color: var(--body-mid); }
.cfg-input-secret {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: 1px;
  color: var(--body-mid);
}
.cfg-select {
  cursor: pointer;
  appearance: none;
  -webkit-appearance: none;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M3 5l3 3 3-3' stroke='%237d8187' stroke-width='1.5' fill='none' stroke-linecap='round'/></svg>");
  background-repeat: no-repeat;
  background-position: right 12px center;
  padding-right: 32px;
}

.cfg-btn-mini {
  flex: 0 0 auto;
  padding: var(--sp-xs) var(--sp-md);
  background: transparent;
  border: 1px solid var(--pill-border);
  border-radius: var(--radius-pill);
  color: var(--body);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, color 0.15s;
}
.cfg-btn-mini:hover {
  border-color: var(--pill-border-hover);
  color: var(--ink);
  background: var(--canvas-soft);
}

/* 开关 */
.cfg-toggle {
  position: relative;
  width: 40px;
  height: 22px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--canvas-mid);
  background: var(--canvas-soft);
  cursor: pointer;
  flex: 0 0 auto;
  transition: background 0.15s, border-color 0.15s;
  padding: 0;
}
.cfg-toggle.on {
  background: var(--ink);
  border-color: var(--ink);
}
.cfg-toggle-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--ink);
  transition: transform 0.15s, background 0.15s;
}
.cfg-toggle.on .cfg-toggle-knob {
  transform: translateX(18px);
  background: var(--on-primary);
}
.cfg-toggle-state {
  font-size: 13px;
  color: var(--body-mid);
}

.cfg-desc {
  font-size: 12px;
  line-height: 16px;
  color: var(--body-mid);
  margin-top: 2px;
}
</style>
