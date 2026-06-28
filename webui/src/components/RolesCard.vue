<template>
  <div class="roles-content">
    <div class="roles-toolbar">
      <button type="button" class="btn btn-primary roles-add" @click="openCreate">+ 新增角色</button>
    </div>

    <div v-if="loading" class="roles-loading">
      <span class="activity-spinner" />
      <span>加载角色配置中…</span>
    </div>

    <div v-else-if="loadError" class="roles-error">
      <p>{{ loadError }}</p>
      <button class="btn" @click="loadRoles">重试</button>
    </div>

    <div v-else>
      <!-- 小卡片列表:默认角色置顶,其余保持后端返回顺序。每张显示角色名/默认标记/编辑/删除
           + 提示词首行预览 + skills/mcp 绑定摘要。详情(提示词全文、绑定勾选)进编辑弹窗操作。 -->
      <section
        v-for="r in sortedForm"
        :key="r.name"
        class="role-card"
      >
        <div class="role-card-head">
          <span class="role-name-text">{{ r.name }}</span>
          <span v-if="r.name === defaultRole" class="role-default-tag">默认角色</span>
          <span class="role-card-spacer" />
          <button type="button" class="btn btn-mini" @click="openEdit(r)">编辑</button>
          <button type="button" class="btn btn-mini role-del-btn" @click="remove(r)">删除</button>
        </div>

        <p class="role-prompt-preview" :title="r.prompt">{{ r.prompt || '(空提示词)' }}</p>

        <div class="role-summary">
          <span class="role-summary-item">
            <span class="role-summary-label">Skills</span>
            <span class="role-summary-value">{{ summarySkills(r) }}</span>
            <button type="button" class="role-summary-edit" @click="openBinding(r, 'skills')">编辑</button>
          </span>
          <span class="role-summary-sep">·</span>
          <span class="role-summary-item">
            <span class="role-summary-label">MCP</span>
            <span class="role-summary-value">{{ summaryMcp(r) }}</span>
            <button type="button" class="role-summary-edit" @click="openBinding(r, 'mcp')">编辑</button>
          </span>
        </div>
      </section>

      <div v-if="!form.length" class="roles-empty">
        <p>尚未配置任何角色,点"+ 新增角色"创建。</p>
      </div>

      <p class="card-restart-note">⚠️ 角色提示词与 skills/MCP 绑定改后,下一次 agent 依赖刷新(按文件 mtime 自动重载)即生效,无需重启进程;新增/删除角色即时重新扫描。</p>
    </div>

    <!-- 三个独立弹窗:名称+提示词 / skills 绑定 / mcp 绑定,各自独立显隐、各自保存。 -->
    <RoleEditDialog
      v-if="dialogRole !== undefined"
      :role="dialogRole"
      :all-roles="form"
      @close="closeDialog"
      @saved="onSaved"
    />
    <RoleBindingDialog
      v-if="bindingState"
      :role="bindingState.role"
      :kind="bindingState.kind"
      :options="bindingState.kind === 'skills' ? availableSkills : availableMcp"
      :all-roles="form"
      @close="closeBinding"
      @saved="onSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { getRolesConfig, putRolesConfig } from '../api'
import type { RoleBindingItem, RoleOption } from '../api'
import RoleEditDialog from './RoleEditDialog.vue'
import RoleBindingDialog from './RoleBindingDialog.vue'

// 列表数据源:后端 RoleBindingItem 原样存放,不再有本地编辑态/快照(每次改动即时落盘)。
const form = ref<RoleBindingItem[]>([])
const availableSkills = ref<RoleOption[]>([])
const availableMcp = ref<RoleOption[]>([])
const defaultRole = ref('default')

// 渲染顺序:默认角色置顶,其余保持后端返回顺序(用稳定排序,避免非默认角色相对次序被打乱)。
const sortedForm = computed(() => {
  const def = form.value.find((r) => r.name === defaultRole.value)
  if (!def) return form.value
  return [def, ...form.value.filter((r) => r.name !== defaultRole.value)]
})

const loading = ref(false)
const loadError = ref('')

// 弹窗状态(三弹窗独立):名称/提示词弹窗 + skills 绑定弹窗 + mcp 绑定弹窗。
// 名称/提示词弹窗:dialogRole=null 表示新增空白弹窗;RoleBindingItem 表示编辑该角色;undefined 表示关闭。
const dialogRole = ref<RoleBindingItem | null | undefined>(undefined)
// 绑定弹窗:{role, kind} 表示打开指定角色的 skills/mcp 绑定弹窗;null 表示关闭。
const bindingState = ref<{ role: RoleBindingItem; kind: 'skills' | 'mcp' } | null>(null)

// 绑定摘要文案:全部 / 个性化(已选数)。
function summarySkills(r: RoleBindingItem): string {
  return r.skills_mode === 'all' ? '全部' : `个性化(${r.selected_skills.length})`
}
function summaryMcp(r: RoleBindingItem): string {
  return r.mcp_mode === 'all' ? '全部' : `个性化(${r.selected_mcp_servers.length})`
}

async function loadRoles() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await getRolesConfig()
    form.value = res.roles
    availableSkills.value = res.available_skills
    availableMcp.value = res.available_mcp_servers
    defaultRole.value = res.default_role
  } catch (e) {
    loadError.value = (e as Error).message || '加载角色配置失败'
  } finally {
    loading.value = false
  }
}

function openCreate() {
  dialogRole.value = null
}

function openEdit(r: RoleBindingItem) {
  dialogRole.value = r
}

function closeDialog() {
  dialogRole.value = undefined
}

function openBinding(r: RoleBindingItem, kind: 'skills' | 'mcp') {
  bindingState.value = { role: r, kind }
}

function closeBinding() {
  bindingState.value = null
}

// 任一弹窗保存成功后:关闭所有弹窗 + 回读刷新列表(拿到后端最新态)。
async function onSaved() {
  dialogRole.value = undefined
  bindingState.value = null
  await loadRoles()
}

// 删除:确认后从列表移除该条,整体覆盖写提交。
async function remove(r: RoleBindingItem) {
  if (r.name === defaultRole.value && r.name) {
    if (!confirm(`「${r.name}」是默认角色,删除后需在 config.yaml 重新指定 default_role。确定删除?`)) return
  } else if (!confirm(`确定删除角色「${r.name}」?将删除其 .md 文件与绑定配置,不可恢复。`)) {
    return
  }
  const next = form.value.filter((x) => x.name !== r.name)
  try {
    const res = await putRolesConfig(next)
    form.value = res.roles
    availableSkills.value = res.available_skills
    availableMcp.value = res.available_mcp_servers
    defaultRole.value = res.default_role
  } catch (e) {
    loadError.value = (e as Error).message || '删除失败'
  }
}

// 暴露给父组件:供外部需要时刷新(如切回该分组)。不再有 dirty/save/reset(弹窗自管保存)。
defineExpose({
  loadRoles,
})

loadRoles()
</script>

<style scoped>
.roles-content {
  max-width: 820px;
  margin: 0 auto;
  padding-bottom: 80px;
}
.roles-toolbar {
  margin-bottom: var(--sp-lg);
  display: flex;
}
.roles-loading,
.roles-error {
  display: flex;
  align-items: center;
  gap: var(--sp-md);
  color: var(--body-mid);
  font-size: 14px;
}
.roles-error { flex-direction: column; align-items: flex-start; gap: var(--sp-md); }
.roles-empty {
  color: var(--body-mid);
  font-size: 14px;
  margin-bottom: var(--sp-lg);
}

/* 小卡片:更紧凑的内边距与行距,单角色不再占满一屏。 */
.role-card {
  background: var(--canvas-card);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  padding: var(--sp-md) var(--sp-lg);
  margin-bottom: var(--sp-sm);
}
.role-card-head {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  flex-wrap: wrap;
  margin-bottom: var(--sp-xs);
}
.role-name-text {
  font-size: 15px;
  font-weight: 500;
  color: var(--ink);
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}
.role-default-tag {
  flex: 0 0 auto;
  font-size: 11px;
  letter-spacing: 0.3px;
  padding: 1px var(--sp-sm);
  border: 1px solid var(--pill-border);
  border-radius: var(--radius-pill);
  background: var(--canvas-soft);
  color: var(--body-mid);
}
.role-card-spacer { flex: 1; }
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
.role-del-btn:hover {
  border-color: rgba(255, 122, 23, 0.45);
  color: var(--accent);
  background: rgba(255, 122, 23, 0.08);
}

/* 提示词首行预览:单行截断。 */
.role-prompt-preview {
  color: var(--body-mid);
  font-size: 13px;
  line-height: 18px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-bottom: var(--sp-xs);
}

/* 绑定摘要:mono 小标签。 */
.role-summary {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  flex-wrap: wrap;
  font-size: 12px;
}
.role-summary-item {
  display: inline-flex;
  align-items: baseline;
  gap: var(--sp-xs);
}
.role-summary-label {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: 0.3px;
  color: var(--body-mid);
}
.role-summary-value { color: var(--body); }
.role-summary-sep { color: var(--body-mid); }
/* 绑定行内的"编辑"文字按钮:无边框、underlIne-on-hover,与摘要同基线。 */
.role-summary-edit {
  background: none;
  border: none;
  padding: 0 0 0 var(--sp-xs);
  color: var(--body-mid);
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
  text-decoration-color: transparent;
  transition: color 0.15s, text-decoration-color 0.15s;
}
.role-summary-edit:hover {
  color: var(--ink);
  text-decoration-color: var(--pill-border-hover);
}

.roles-add {
  padding: var(--sp-xs) var(--sp-lg);
}

.card-restart-note {
  margin-top: var(--sp-xl);
  font-size: 12px;
  line-height: 18px;
  color: var(--body-mid);
}
</style>
