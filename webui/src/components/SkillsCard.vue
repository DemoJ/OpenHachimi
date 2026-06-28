<template>
  <div class="skills-content">
    <!-- 顶部工具条:搜索 + 分类筛选 + 新增 -->
    <div class="skills-toolbar">
      <input
        class="skills-search"
        type="search"
        v-model="query"
        placeholder="搜索技能名或描述…"
      />
      <select class="skills-filter" v-model="sourceFilter">
        <option value="">全部来源</option>
        <option value="user">user</option>
        <option value="external">外部</option>
      </select>
      <button type="button" class="btn btn-primary skills-add-btn" @click="openInstall">
        + 新增技能
      </button>
    </div>

    <div v-if="loading" class="skills-loading">
      <span class="activity-spinner" />
      <span>加载技能中…</span>
    </div>

    <div v-else-if="loadError" class="skills-error">
      <p>{{ loadError }}</p>
      <button class="btn" @click="loadSkills">重试</button>
    </div>

    <div v-else-if="!skills.length" class="skills-empty">
      <p>未发现任何技能。把技能放入 <code>user/skills/</code> 或配置外部技能目录(见"路径与日志"),每个技能是一个含 SKILL.md 的目录;也可点"新增技能"从 URL 或本地路径安装。</p>
    </div>

    <div v-else-if="!filtered.length" class="skills-empty">
      <p>没有匹配的技能。</p>
    </div>

    <div v-else>
      <section
        v-for="s in filtered"
        :key="s.source_path"
        class="settings-card skills-card"
      >
        <div class="skills-card-row">
          <div class="skills-card-main">
            <h3 class="card-title">
              <span class="card-title-text">{{ s.name }}</span>
              <span class="skill-dir-tag" :class="{ external: s.source_dir_key !== 'user' }">{{ dirTagLabel(s.source_dir_key) }}</span>
              <span v-if="s.category" class="skill-cat-tag">{{ s.category }}</span>
            </h3>
            <p class="card-desc">{{ s.description }}</p>
          </div>

          <div class="skills-card-actions">
            <!-- 删除按钮:仅 user 源技能可删(外部目录只读) -->
            <button
              v-if="s.source_dir_key === 'user'"
              type="button"
              class="btn btn-mini skill-del-btn"
              :disabled="saving"
              @click="onDelete(s)"
              title="删除该技能"
            >删除</button>

            <!-- 开关:禁用模型自动调用,放技能名同行最右侧 -->
            <button
              type="button"
              class="skill-toggle-btn"
              :class="{ on: !currentDisabled(s.source_path) }"
              :title="currentDisabled(s.source_path) ? '已禁用模型自动调用 · 点击启用' : '已启用 · 点击禁用模型自动调用'"
              @click="onToggle(s, !currentDisabled(s.source_path))"
            >
              <span class="skill-toggle-track">
                <span class="skill-toggle-thumb" />
              </span>
              <span class="skill-toggle-state">{{ currentDisabled(s.source_path) ? '已禁用' : '已启用' }}</span>
              <span v-if="isDirty(s.source_path)" class="skill-dirty">未保存</span>
            </button>
          </div>
        </div>
      </section>

      <p class="card-restart-note">⚠️ 改后下一次技能索引刷新(按文件 mtime 自动重载)即生效,一般无需重启进程;新增/删除技能即时重新扫描。</p>
    </div>

    <!-- 新增技能弹层:贴 URL/路径安装 -->
    <div v-if="installOpen" class="skills-modal-overlay" @click.self="closeInstall">
      <div class="skills-modal">
        <h3>新增技能</h3>
        <p class="skills-modal-desc">
          支持 GitHub/Git 仓库 URL、zip/tar 下载 URL、本地技能目录路径。安装到 <code>user/skills/</code>,同名会更新覆盖。
        </p>
        <label class="mcp-field">
          <span class="mcp-field-label">来源(URL 或本地目录路径)</span>
          <input
            class="mcp-input"
            v-model="installSource"
            placeholder="https://github.com/owner/skill-repo  或  D:/path/to/skill"
          />
        </label>
        <div class="skills-modal-options">
          <label class="skill-check-row">
            <input type="checkbox" v-model="installAllowHttp" />
            <span>允许 http://(不安全,仅可信内网)</span>
          </label>
        </div>
        <p v-if="installError" class="skills-modal-error">{{ installError }}</p>
        <p v-else-if="installMessage" class="skills-modal-ok">{{ installMessage }}</p>
        <div class="skills-modal-actions">
          <button type="button" class="btn" :disabled="installWorking" @click="closeInstall">关闭</button>
          <button
            type="button"
            class="btn btn-primary"
            :disabled="installWorking || !installSource.trim()"
            @click="onInstall"
          >{{ installWorking ? '安装中…' : '安装' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { getSkills, toggleSkill, installSkill, deleteSkill } from '../api'
import type { SkillItem } from '../api'

const skills = ref<SkillItem[]>([])
// 加载时各技能的原始 disabled 快照(保存基准)。
const snapshot = ref<Record<string, boolean>>({})
// pending:已改但未保存的目标 disabled 值,键为 source_path。
const pending = ref<Record<string, boolean>>({})

const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const justSaved = ref(false)

// 搜索 / 筛选状态(纯前端,不触发保存条)。
const query = ref('')
// 来源筛选:""全部 / "user" / "external"(外部目录的聚合标签)。
const sourceFilter = ref('')

// 新增技能弹层状态。
const installOpen = ref(false)
const installSource = ref('')
const installAllowHttp = ref(false)
const installWorking = ref(false)
const installError = ref('')
const installMessage = ref('')

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  const sf = sourceFilter.value
  return skills.value.filter((s) => {
    if (sf === 'user' && s.source_dir_key !== 'user') return false
    if (sf === 'external' && s.source_dir_key === 'user') return false
    if (!q) return true
    return s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q)
  })
})

// 当前展示态:有 pending 用 pending,否则用 snapshot。
function currentDisabled(path: string): boolean {
  if (path in pending.value) return pending.value[path]
  return snapshot.value[path] ?? false
}

// 来源目录标签文案:user 之外的外部目录同名 "skills" 信息量为零,显示「外部」更直观;
// 其他外部目录名原样保留,以便多个不同外部目录时可区分。
function dirTagLabel(key: string): string {
  if (key === 'user') return 'user'
  return key === 'skills' ? '外部' : key
}

function isDirty(path: string): boolean {
  if (!(path in pending.value)) return false
  return pending.value[path] !== (snapshot.value[path] ?? false)
}

const anyDirty = computed(() => skills.value.some((s) => isDirty(s.source_path)))

async function loadSkills() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await getSkills()
    skills.value = res.skills
    const snap: Record<string, boolean> = {}
    for (const s of res.skills) snap[s.source_path] = s.disabled
    snapshot.value = snap
    pending.value = {}
    justSaved.value = false
  } catch (e) {
    loadError.value = (e as Error).message || '加载技能失败'
  } finally {
    loading.value = false
  }
}

function onToggle(s: SkillItem, checked: boolean) {
  // 与 snapshot 相同则视为未改动:从 pending 移除,避免 dirty 误判。
  if (checked === (snapshot.value[s.source_path] ?? false)) {
    delete pending.value[s.source_path]
  } else {
    pending.value[s.source_path] = checked
  }
}

async function save() {
  if (!anyDirty.value || saving.value) return
  saving.value = true
  loadError.value = ''
  try {
    for (const s of skills.value) {
      if (!isDirty(s.source_path)) continue
      const target = pending.value[s.source_path]
      await toggleSkill(s.source_path, target)
      snapshot.value[s.source_path] = target
    }
    pending.value = {}
    justSaved.value = true
    setTimeout(() => { justSaved.value = false }, 2500)
  } catch (e) {
    loadError.value = (e as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}

function reset() {
  // 放弃修改:清空 pending,展示态回到 snapshot。
  pending.value = {}
  justSaved.value = false
}

// ---- 新增技能 ----
function openInstall() {
  installSource.value = ''
  installAllowHttp.value = false
  installError.value = ''
  installMessage.value = ''
  installOpen.value = true
}
function closeInstall() {
  if (installWorking.value) return
  installOpen.value = false
}
async function onInstall() {
  if (!installSource.value.trim() || installWorking.value) return
  installWorking.value = true
  installError.value = ''
  installMessage.value = ''
  try {
    const res = await installSkill(installSource.value.trim(), installAllowHttp.value)
    installMessage.value = res.message
    // 安装成功后重新拉取清单,反映新增/更新。
    await loadSkills()
  } catch (e) {
    installError.value = (e as Error).message || '安装失败'
  } finally {
    installWorking.value = false
  }
}

// ---- 删除技能 ----
async function onDelete(s: SkillItem) {
  if (saving.value) return
  if (!confirm(`确定删除技能「${s.name}」?将删除整个目录,不可恢复。`)) return
  loadError.value = ''
  try {
    await deleteSkill(s.source_path)
    await loadSkills()
  } catch (e) {
    loadError.value = (e as Error).message || '删除失败'
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

loadSkills()
</script>

<style scoped>
.skills-content {
  max-width: 820px;
  margin: 0 auto;
  padding-bottom: 80px;
}

/* 顶部工具条 */
.skills-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  margin-bottom: var(--sp-xl);
  flex-wrap: wrap;
}
.skills-search {
  flex: 1 1 220px;
  min-width: 160px;
  padding: var(--sp-xs) var(--sp-md);
  background: var(--canvas-soft);
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: 13px;
  font-family: inherit;
  outline: none;
  box-sizing: border-box;
  transition: border-color 0.15s;
}
.skills-search:focus { border-color: var(--pill-border-hover); }
.skills-filter {
  flex: 0 0 auto;
  padding: var(--sp-xs) var(--sp-md);
  background: var(--canvas-soft);
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: 13px;
  font-family: inherit;
  outline: none;
  cursor: pointer;
  transition: border-color 0.15s;
}
.skills-filter:focus { border-color: var(--pill-border-hover); }
.skills-add-btn {
  flex: 0 0 auto;
  padding: var(--sp-xs) var(--sp-md);
  font-size: 13px;
}

.skills-loading,
.skills-error {
  display: flex;
  align-items: center;
  gap: var(--sp-md);
  color: var(--body-mid);
  font-size: 14px;
}
.skills-error { flex-direction: column; align-items: flex-start; gap: var(--sp-md); }
.skills-empty {
  color: var(--body-mid);
  font-size: 14px;
  line-height: 20px;
}
.skills-empty code {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}

.skills-card {
  background: var(--canvas-card);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  padding: var(--sp-xl);
  margin-bottom: var(--sp-xl);
}
.skills-card-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-lg);
}
.skills-card-main {
  flex: 1 1 auto;
  min-width: 0;
}
.skills-card-actions {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  align-self: flex-start;
}
.card-title {
  font-size: 16px;
  font-weight: 400;
  color: var(--ink);
  margin: 0 0 var(--sp-xs) 0;
  display: flex;
  align-items: baseline;
  gap: var(--sp-sm);
  flex-wrap: wrap;
}
.card-title-text { font-weight: inherit; }
.skill-dir-tag,
.skill-cat-tag {
  font-size: 11px;
  letter-spacing: 0.3px;
  padding: 1px var(--sp-sm);
  border: 1px solid var(--pill-border);
  border-radius: var(--radius-pill);
  background: var(--canvas-soft);
  color: var(--body-mid);
}
.skill-dir-tag.external {
  border-style: dashed;
}
.card-desc {
  font-size: 13px;
  line-height: 18px;
  color: var(--body-mid);
}

/* 删除按钮 */
.btn-mini {
  padding: var(--sp-xs) var(--sp-md);
  background: transparent;
  border: 1px solid var(--pill-border);
  border-radius: var(--radius-pill);
  color: var(--body-mid);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, color 0.15s;
}
.btn-mini:hover:not(:disabled) {
  border-color: var(--pill-border-hover);
  color: var(--ink);
  background: var(--canvas-soft);
}
.btn-mini:disabled { opacity: 0.5; cursor: not-allowed; }

/* Toggle 开关按钮(技能名同行最右侧) */
.skill-toggle-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-sm);
  background: transparent;
  border: none;
  font: inherit;
  color: var(--body-mid);
  cursor: pointer;
  padding: 2px 0;
  transition: color 0.15s;
}
.skill-toggle-btn:hover { color: var(--ink); }
.skill-toggle-track {
  display: inline-flex;
  align-items: center;
  width: 32px;
  height: 18px;
  border-radius: var(--radius-pill);
  background: var(--canvas-mid);
  border: 1px solid var(--pill-border);
  transition: background 0.18s, border-color 0.18s;
  flex: 0 0 auto;
}
.skill-toggle-thumb {
  display: block;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--canvas);
  border: 1px solid var(--pill-border);
  margin-left: 2px;
  transition: transform 0.18s ease;
}
.skill-toggle-btn.on .skill-toggle-track {
  background: var(--accent-soft);
  border-color: var(--pill-border-hover);
}
.skill-toggle-btn.on .skill-toggle-thumb {
  transform: translateX(13px);
}
.skill-toggle-state {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  letter-spacing: 0.4px;
}
.skill-dirty {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  letter-spacing: 0.5px;
  color: var(--body-mid);
}

.card-restart-note {
  margin-top: var(--sp-md);
  font-size: 12px;
  line-height: 18px;
  color: var(--body-mid);
}

/* 新增技能弹层 */
.skills-modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.skills-modal {
  width: min(520px, 92vw);
  max-height: 86vh;
  overflow-y: auto;
  background: var(--canvas-card);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  padding: var(--sp-xl);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}
.skills-modal h3 {
  font-size: 16px;
  font-weight: 400;
  color: var(--ink);
  margin: 0 0 var(--sp-sm) 0;
}
.skills-modal-desc {
  font-size: 13px;
  line-height: 18px;
  color: var(--body-mid);
  margin-bottom: var(--sp-lg);
}
.skills-modal-desc code {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}
.skills-modal-options {
  margin: var(--sp-md) 0;
}
.skill-check-row {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-sm);
  font-size: 13px;
  color: var(--body);
  cursor: pointer;
  user-select: none;
}
.skill-check-row input {
  width: 15px;
  height: 15px;
  cursor: pointer;
  accent-color: var(--ink);
}
.skills-modal-error {
  font-size: 12px;
  line-height: 16px;
  color: #c0392b;
  margin: var(--sp-sm) 0;
  word-break: break-word;
}
.skills-modal-ok {
  font-size: 12px;
  line-height: 16px;
  color: var(--accent-soft);
  margin: var(--sp-sm) 0;
  word-break: break-word;
}
.skills-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-sm);
  margin-top: var(--sp-md);
}

/* 复用 McpServersCard 的输入框风格(此处仅为视觉一致,无依赖关系) */
.mcp-field { display: block; margin-bottom: var(--sp-md); }
.mcp-field-label {
  display: block;
  font-size: 12px;
  letter-spacing: 0.3px;
  color: var(--body-mid);
  margin-bottom: var(--sp-xs);
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}
.mcp-input {
  width: 100%;
  padding: var(--sp-xs) var(--sp-md);
  background: var(--canvas-soft);
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: 13px;
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  line-height: 20px;
  outline: none;
  box-sizing: border-box;
  transition: border-color 0.15s;
}
.mcp-input:focus { border-color: var(--pill-border-hover); }
</style>