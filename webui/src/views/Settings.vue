<template>
  <div class="settings-layout">
    <!-- 左侧导航 -->
    <aside class="settings-nav">
      <div class="settings-nav-header">
        <button class="btn-back" @click="goBack">
          <span class="arrow">←</span> 返回聊天
        </button>
        <h2>设置</h2>
      </div>
      <ul class="settings-nav-list">
        <li
          v-for="g in groups"
          :key="g.id"
          :class="{ active: g.id === currentGroup }"
          @click="onSelectGroup(g.id)"
        >
          <span class="nav-icon">{{ g.icon }}</span>
          <span class="nav-label">{{ g.label }}</span>
        </li>
      </ul>
    </aside>

    <!-- 右侧内容 -->
    <div class="settings-main">
      <header class="settings-header">
        <div class="settings-title">
          <span class="eyebrow">SETTINGS</span>
          <h1>{{ activeMeta?.label || '设置' }}</h1>
        </div>
        <button class="btn" @click="goBack">关闭</button>
      </header>

      <div class="settings-body">
        <div v-if="loading" class="settings-loading">
          <span class="activity-spinner" />
          <span>加载配置中…</span>
        </div>

        <div v-else-if="loadError" class="settings-error">
          <p>{{ loadError }}</p>
          <button class="btn" @click="loadConfig">重试</button>
        </div>

        <div v-else-if="currentGroup === 'ai-models' && fields.length" class="settings-content">
          <!-- 主模型 -->
          <section class="settings-card">
            <div class="card-head">
              <h3>主模型 · LLM</h3>
              <p class="card-desc">Agent 对话使用的核心模型。改后新会话生效。</p>
            </div>
            <div class="card-grid">
              <ConfigField
                v-for="f in fieldsByGroup('llm')"
                :key="f.path"
                :field="f"
                :secret-masked="isMasked(f.path)"
                v-model="form[f.path]"
                @unmask="onUnmask(f.path)"
              />
            </div>
          </section>

          <!-- 视觉模型 -->
          <section class="settings-card">
            <div class="card-head">
              <h3>视觉模型 · VISION</h3>
              <p class="card-desc">主模型不支持图片时,可由辅助视觉模型先识别图片再交给主模型。</p>
            </div>
            <div class="card-grid">
              <ConfigField
                v-for="f in fieldsByGroup('vision')"
                :key="f.path"
                :field="f"
                :secret-masked="isMasked(f.path)"
                v-model="form[f.path]"
                @unmask="onUnmask(f.path)"
              />
            </div>
          </section>

          <!-- 摘要压缩辅助模型 -->
          <section class="settings-card">
            <div class="card-head">
              <h3>摘要压缩辅助模型 · SUMMARY</h3>
              <p class="card-desc">长对话上下文压缩用的辅助模型;留空则复用主模型。</p>
            </div>
            <div class="card-grid">
              <ConfigField
                v-for="f in fieldsByGroup('summary')"
                :key="f.path"
                :field="f"
                :secret-masked="isMasked(f.path)"
                v-model="form[f.path]"
                @unmask="onUnmask(f.path)"
              />
            </div>
          </section>

          <!-- 保存条 -->
          <div class="settings-actions" :class="{ visible: dirty }">
            <span class="dirty-hint" v-if="dirty">有未保存的修改</span>
            <span class="dirty-hint saved" v-else-if="justSaved">已保存</span>
            <div class="action-buttons">
              <button class="btn" :disabled="!dirty || saving" @click="onReset">放弃修改</button>
              <button class="btn btn-primary" :disabled="!dirty || saving" @click="onSave">
                {{ saving ? '保存中…' : '保存' }}
              </button>
            </div>
          </div>
        </div>

        <div v-else class="settings-empty">
          <p>该设置分组暂未实现。</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import ConfigField from '../components/ConfigField.vue'
import { getConfigGroup, updateConfigGroup } from '../api'
import type { ConfigField as ConfigFieldType } from '../api'

const router = useRouter()
const route = useRoute()

// 设置分组元信息(左侧导航)。新增分组时在此追加,并扩展下方渲染分支。
const groups = [
  { id: 'ai-models', label: 'AI 模型', icon: '🤖' },
] as const

const activeMeta = computed(() => groups.find((g) => g.id === currentGroup.value))

const currentGroup = ref<string>((route.params.group as string) || 'ai-models')

const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const justSaved = ref(false)

const fields = ref<ConfigFieldType[]>([])
// 原始值快照:保存基准,用于 dirty 比对与"放弃修改"还原。
// secret 字段保存的是脱敏后的值(来自后端),用户不主动改它时永远等于快照 → 不算 dirty。
const snapshot = ref<Record<string, string | number | boolean>>({})
// 当前编辑表单。
const form = ref<Record<string, string | number | boolean>>({})
// 记录哪些 secret 字段当前是脱敏态(未改动);一旦用户点击"修改",移出该集合。
const maskedSecrets = ref<Set<string>>(new Set())

const dirty = computed(() => {
  for (const f of fields.value) {
    if (form.value[f.path] !== snapshot.value[f.path]) return true
  }
  return false
})

function fieldsByGroup(g: string): ConfigFieldType[] {
  return fields.value.filter((f) => f.group === g)
}

function isMasked(path: string): boolean {
  return maskedSecrets.value.has(path)
}

function goBack() {
  if (dirty.value) {
    if (!confirm('有未保存的修改,确定离开吗?')) return
  }
  router.push('/chat')
}

function onSelectGroup(id: string) {
  if (id === currentGroup.value) return
  if (dirty.value) {
    if (!confirm('有未保存的修改,确定切换吗?')) return
  }
  router.push(`/settings/${id}`)
}

function onUnmask(path: string) {
  // 用户要对一个脱敏 secret 字段做改动:清空表单值让它变成"未设置",
  // 同时移出 maskedSecrets,使后续输入成为真正的修改。
  maskedSecrets.value.delete(path)
  form.value[path] = ''
}

// 比较两个值是否相等(区分 bool/number/string,避免 1 === true 误判)。
function valueEquals(a: unknown, b: unknown): boolean {
  if (typeof a !== typeof b) return false
  return a === b
}

async function loadConfig() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await getConfigGroup(currentGroup.value)
    fields.value = res.fields
    // 深拷贝 values 到 snapshot/form,避免引用同一对象导致 dirty 永真。
    snapshot.value = { ...res.values }
    form.value = { ...res.values }
    maskedSecrets.value = new Set(res.masked)
    justSaved.value = false
  } catch (e) {
    loadError.value = (e as Error).message || '加载配置失败'
  } finally {
    loading.value = false
  }
}

async function onSave() {
  if (!dirty.value || saving.value) return
  saving.value = true
  loadError.value = ''
  try {
    // 只提交发生变化的字段;secret 脱敏态的值(等于快照)自然不会被包含。
    const updates: Record<string, string | number | boolean> = {}
    for (const f of fields.value) {
      if (!valueEquals(form.value[f.path], snapshot.value[f.path])) {
        updates[f.path] = form.value[f.path]
      }
    }
    const res = await updateConfigGroup(currentGroup.value, updates)
    // 用后端返回的最新值(已脱敏)刷新快照与表单,dirty 自动复位。
    snapshot.value = { ...res.values }
    form.value = { ...res.values }
    maskedSecrets.value = new Set(res.masked)
    justSaved.value = true
    setTimeout(() => { justSaved.value = false }, 2500)
  } catch (e) {
    loadError.value = (e as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}

function onReset() {
  // 放弃修改:用快照还原表单,secret 恢复脱敏态。
  form.value = { ...snapshot.value }
  // 重新根据 snapshot 推导 maskedSecrets:凡是值形如掩码的 secret 视为脱敏态。
  maskedSecrets.value = new Set(
    fields.value
      .filter((f) => f.kind === 'secret')
      .map((f) => f.path)
      .filter((p) => isMaskLike(String(snapshot.value[p] ?? ''))),
  )
  justSaved.value = false
}

function isMaskLike(s: string): boolean {
  return s.includes('••••')
}

// 路由参数变化时切换分组并重新加载。
watch(
  () => route.params.group,
  (g) => {
    const next = (g as string) || 'ai-models'
    if (next !== currentGroup.value) {
      currentGroup.value = next
      loadConfig()
    }
  },
)

// 进入页面即加载当前分组。
loadConfig()
</script>

<style scoped>
.settings-layout {
  display: flex;
  height: 100%;
}

/* 左侧导航 */
.settings-nav {
  width: var(--sidebar-width);
  background: var(--canvas-sink);
  border-right: 1px solid var(--hairline);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.settings-nav-header {
  padding: var(--sp-xl);
  border-bottom: 1px solid var(--hairline);
}
.settings-nav-header h2 {
  font-size: 20px;
  font-weight: 400;
  line-height: 28px;
  letter-spacing: -0.2px;
  margin-top: var(--sp-md);
}
.btn-back {
  background: transparent;
  border: none;
  color: var(--body-mid);
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
  padding: 0;
  transition: color 0.15s;
}
.btn-back:hover { color: var(--ink); }
.btn-back .arrow { display: inline-block; margin-right: 4px; }

.settings-nav-list {
  list-style: none;
  padding: var(--sp-xs) var(--sp-md);
}
.settings-nav-list li {
  display: flex;
  align-items: center;
  gap: var(--sp-md);
  padding: var(--sp-sm) var(--sp-md);
  border-radius: var(--radius-sm);
  border-left: 2px solid transparent;
  cursor: pointer;
  color: var(--body);
  font-size: 14px;
  line-height: 20px;
  transition: background 0.15s, color 0.15s;
}
.settings-nav-list li:hover { background: var(--canvas-soft); color: var(--ink); }
.settings-nav-list li.active {
  background: var(--canvas-soft);
  color: var(--ink);
  border-left-color: var(--ink);
}
.nav-icon { font-size: 16px; }

/* 右侧主体 */
.settings-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.settings-header {
  height: var(--header-height);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--sp-xl);
  border-bottom: 1px solid var(--hairline);
  flex-shrink: 0;
}
.settings-title h1 {
  font-size: 20px;
  font-weight: 400;
  line-height: 28px;
  letter-spacing: -0.2px;
}
.settings-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-2xl) var(--sp-xl);
}
.settings-content {
  max-width: 820px;
  margin: 0 auto;
  padding-bottom: 80px;
}

/* 卡片 */
.settings-card {
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
}
.card-desc {
  font-size: 13px;
  line-height: 18px;
  color: var(--body-mid);
}

/* 字段网格 */
.card-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--sp-lg) var(--sp-xl);
}
@media (max-width: 900px) {
  .card-grid { grid-template-columns: 1fr; }
}

/* loading / error / empty */
.settings-loading,
.settings-error,
.settings-empty {
  max-width: 820px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  gap: var(--sp-md);
  color: var(--body-mid);
  font-size: 14px;
}
.settings-error { flex-direction: column; align-items: flex-start; gap: var(--sp-md); }

/* 保存条 */
.settings-actions {
  position: fixed;
  bottom: var(--sp-xl);
  left: 50%;
  transform: translateX(-50%) translateY(20px);
  display: flex;
  align-items: center;
  gap: var(--sp-lg);
  padding: var(--sp-sm) var(--sp-md);
  background: var(--canvas-soft);
  border: 1px solid var(--pill-border);
  border-radius: var(--radius-pill);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s, transform 0.2s;
  z-index: 10;
}
.settings-actions.visible {
  opacity: 1;
  pointer-events: auto;
  transform: translateX(-50%) translateY(0);
}
.dirty-hint {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  letter-spacing: 0.6px;
  color: var(--body-mid);
  padding-left: var(--sp-sm);
}
.dirty-hint.saved { color: var(--accent-soft); }
.action-buttons { display: flex; gap: var(--sp-sm); }
</style>
