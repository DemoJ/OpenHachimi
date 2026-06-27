<template>
  <div class="prompts-content">
    <div v-if="loading" class="prompts-loading">
      <span class="activity-spinner" />
      <span>加载提示词中…</span>
    </div>

    <div v-else-if="loadError" class="prompts-error">
      <p>{{ loadError }}</p>
      <button class="btn" @click="loadPrompts">重试</button>
    </div>

    <div v-else>
      <section
        v-for="p in prompts"
        :key="p.name"
        class="settings-card prompts-card"
      >
        <div class="card-head">
          <h3 class="card-title">
            <span class="card-title-text">{{ p.title }}</span>
            <span class="prompt-status" :class="{ overridden: isOverriddenNow(p.name) }">
              {{ isOverriddenNow(p.name) ? '已自定义' : '使用内置默认' }}
            </span>
            <span v-if="p.has_template_vars" class="prompt-tpl-tag">含占位符</span>
          </h3>
          <p class="card-desc">{{ p.description }}</p>
        </div>

        <textarea
          class="prompt-textarea"
          rows="10"
          :value="form[p.name] ?? ''"
          :placeholder="p.has_template_vars ? '编辑时保留 {{ }} 占位符' : ''"
          @input="onInput(p.name, ($event.target as HTMLTextAreaElement).value)"
        />

        <div class="prompt-actions">
          <button type="button" class="btn btn-mini" @click="onResetOne(p.name)">
            恢复内置
          </button>
          <span v-if="isDirty(p.name)" class="prompt-dirty">未保存</span>
        </div>

        <p v-if="p.has_template_vars" class="prompt-tpl-hint">
          ⚠️ 含 <code v-pre>{{ 变量名 }}</code> 占位符,编辑时请保留。
        </p>
        <p v-if="p.restart_note" class="card-restart-note">⚠️ {{ p.restart_note }}</p>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { getPrompts, updatePrompt } from '../api'
import type { PromptSpec } from '../api'

const prompts = ref<PromptSpec[]>([])
// 快照:加载时的原始 content(用作 dirty 基准)与原始 is_overridden(用于"恢复内置"标记)。
const snapshot = ref<Record<string, { content: string; overridden: boolean }>>({})
// 当前编辑表单:每个 prompt 的 textarea 内容。
const form = ref<Record<string, string>>({})
// 用户点了"恢复内置"的 prompt 集合:这些 textarea 内容被清空,提交时传空串触发后端删除。
const restoredBuiltins = ref<Set<string>>(new Set())

const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const justSaved = ref(false)

function isOverriddenNow(name: string): boolean {
  // 当前展示态:若用户点了"恢复内置"(清空了 textarea),显示为"将回退内置";
  // 否则按快照里的原始 is_overridden。
  if (restoredBuiltins.value.has(name)) return false
  return snapshot.value[name]?.overridden ?? false
}

function isDirty(name: string): boolean {
  if (restoredBuiltins.value.has(name)) return true
  const orig = snapshot.value[name]?.content ?? ''
  return (form.value[name] ?? '') !== orig
}

const anyDirty = computed(() => prompts.value.some((p) => isDirty(p.name)))

async function loadPrompts() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await getPrompts()
    prompts.value = res.prompts
    const snap: typeof snapshot.value = {}
    const f: typeof form.value = {}
    for (const p of res.prompts) {
      snap[p.name] = { content: p.content, overridden: p.is_overridden }
      f[p.name] = p.content
    }
    snapshot.value = snap
    form.value = f
    restoredBuiltins.value = new Set()
    justSaved.value = false
  } catch (e) {
    loadError.value = (e as Error).message || '加载提示词失败'
  } finally {
    loading.value = false
  }
}

function onInput(name: string, v: string) {
  form.value[name] = v
  // 用户重新输入内容,退出"恢复内置"标记态。
  restoredBuiltins.value.delete(name)
}

function onResetOne(name: string) {
  // 恢复内置:清空 textarea,提交时传空串让后端删覆盖文件。
  // 不立即把内置内容填回(避免 dirty 误判);展示态标"将回退内置"。
  form.value[name] = ''
  restoredBuiltins.value.add(name)
}

// 放弃所有改动:还原快照。暴露给父组件(Settings.vue 全局保存条调用)。
function reset() {
  const f: typeof form.value = {}
  for (const p of prompts.value) f[p.name] = snapshot.value[p.name]?.content ?? ''
  form.value = f
  restoredBuiltins.value = new Set()
  justSaved.value = false
}

// 保存所有改动。暴露给父组件。返回是否实际保存了(供父组件判断是否需刷状态)。
async function save() {
  if (!anyDirty.value || saving.value) return
  saving.value = true
  loadError.value = ''
  try {
    for (const p of prompts.value) {
      if (!isDirty(p.name)) continue
      // 恢复内置态或纯空白 → 传空串(后端删覆盖);非空 → 写覆盖。
      const val = restoredBuiltins.value.has(p.name) ? '' : (form.value[p.name] ?? '')
      const res = await updatePrompt(p.name, val)
      // 用后端返回的最新生效值刷新快照与表单。
      snapshot.value[p.name] = { content: res.content, overridden: res.is_overridden }
      form.value[p.name] = res.content
    }
    restoredBuiltins.value = new Set()
    justSaved.value = true
    setTimeout(() => { justSaved.value = false }, 2500)
  } catch (e) {
    loadError.value = (e as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}

// 暴露给父组件:让 Settings.vue 全局保存条复用,与其他设置页交互一致。
defineExpose({
  dirty: anyDirty,
  saving,
  justSaved,
  save,
  reset,
})

loadPrompts()
</script>

<style scoped>
.prompts-content {
  max-width: 820px;
  margin: 0 auto;
  padding-bottom: 80px;
}
.prompts-loading,
.prompts-error {
  display: flex;
  align-items: center;
  gap: var(--sp-md);
  color: var(--body-mid);
  font-size: 14px;
}
.prompts-error { flex-direction: column; align-items: flex-start; gap: var(--sp-md); }

.prompts-card {
  background: var(--canvas-card);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  padding: var(--sp-xl);
  margin-bottom: var(--sp-xl);
}
.card-head { margin-bottom: var(--sp-lg); }
.card-head h3 {
  font-size: 16px;
  font-weight: 400;
  color: var(--ink);
  margin-bottom: var(--sp-xs);
  display: flex;
  align-items: baseline;
  gap: var(--sp-sm);
  flex-wrap: wrap;
}
.card-title-text { font-weight: inherit; }
.prompt-status {
  font-size: 11px;
  letter-spacing: 0.3px;
  padding: 1px var(--sp-sm);
  border: 1px solid var(--pill-border);
  border-radius: var(--radius-pill);
  background: var(--canvas-soft);
  color: var(--body-mid);
}
.prompt-status.overridden {
  color: var(--accent-soft);
  border-color: var(--pill-border-hover);
}
.prompt-tpl-tag {
  font-size: 11px;
  letter-spacing: 0.4px;
  padding: 1px var(--sp-sm);
  border: 1px solid var(--pill-border);
  border-radius: var(--radius-pill);
  background: var(--canvas-soft);
  color: var(--body-mid);
}
.card-desc {
  font-size: 13px;
  line-height: 18px;
  color: var(--body-mid);
}
.prompt-textarea {
  width: 100%;
  min-height: 200px;
  resize: vertical;
  padding: var(--sp-sm) var(--sp-md);
  background: var(--canvas-soft);
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: 13px;
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  line-height: 20px;
  outline: none;
  transition: border-color 0.15s;
  box-sizing: border-box;
}
.prompt-textarea:focus { border-color: var(--pill-border-hover); }
.prompt-textarea::placeholder { color: var(--body-mid); }

.prompt-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-md);
  margin-top: var(--sp-sm);
}
.btn-mini {
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
.btn-mini:hover {
  border-color: var(--pill-border-hover);
  color: var(--ink);
  background: var(--canvas-soft);
}
.prompt-dirty {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  letter-spacing: 0.6px;
  color: var(--body-mid);
}
.prompt-tpl-hint {
  margin-top: var(--sp-sm);
  font-size: 12px;
  line-height: 16px;
  color: var(--body-mid);
}
.prompt-tpl-hint code {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}
.card-restart-note {
  margin-top: var(--sp-md);
  padding-top: var(--sp-md);
  border-top: 1px dashed var(--hairline);
  font-size: 12px;
  line-height: 18px;
  color: var(--body-mid);
}
</style>
