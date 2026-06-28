<template>
  <!-- 角色绑定编辑弹窗:skills 或 mcp 二选一(kind 区分),只显示可选项名称(不显示描述)。
       模式:使用全部 / 个性化绑定(勾选)。保存时整体覆盖写 /roles-config,只改本角色的该项绑定。 -->
  <teleport to="body">
    <div class="role-dialog-overlay" @click.self="onClose">
      <div class="role-dialog role-binding-dialog" role="dialog" aria-modal="true">
        <div class="role-dialog-head">
          <h3>{{ kind === 'skills' ? 'Skills 绑定' : 'MCP 服务器绑定' }} · {{ role.name }}</h3>
          <button type="button" class="role-dialog-close" @click="onClose" aria-label="关闭">✕</button>
        </div>

        <div v-if="saveError" class="role-dialog-error">
          <p>{{ saveError }}</p>
        </div>

        <div class="role-dialog-body">
          <div class="role-mode-row">
            <label class="role-radio"><input type="radio" value="all" v-model="mode" /><span>使用全部</span></label>
            <label class="role-radio"><input type="radio" value="selected" v-model="mode" /><span>个性化绑定</span></label>
          </div>

          <div v-if="mode === 'selected'" class="role-multi">
            <label v-for="o in options" :key="o.name" class="role-check">
              <input
                type="checkbox"
                :checked="selected.includes(o.name)"
                @change="toggle(o.name)"
              />
              <span class="role-check-name">{{ o.name }}</span>
            </label>
            <p v-if="!options.length" class="role-multi-empty">{{ kind === 'skills' ? '系统中尚无可用 skill。' : '系统中尚无配置的 MCP 服务器。' }}</p>
            <p v-else-if="!selected.length" class="role-multi-empty">⚠️ 未勾选任何项,该角色将无法使用{{ kind === 'skills' ? '任何 skill' : '任何 MCP' }}。</p>
          </div>
        </div>

        <div class="role-dialog-foot">
          <button type="button" class="btn" :disabled="saving" @click="onClose">取消</button>
          <button type="button" class="btn btn-primary" :disabled="saving" @click="onSave">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </teleport>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { putRolesConfig } from '../api'
import type { RoleBindingItem, RoleOption } from '../api'

const props = defineProps<{
  role: RoleBindingItem
  kind: 'skills' | 'mcp'
  // 可勾选清单(只取 name 渲染)。
  options: RoleOption[]
  // 完整列表快照(只读):整体覆盖写时基于此替换当前角色。
  allRoles: RoleBindingItem[]
}>()

const emit = defineEmits<{
  close: []
  saved: []
}>()

// 初值:取该角色当前对应绑定的 mode + selected。
const mode = ref<'all' | 'selected'>(
  props.kind === 'skills' ? props.role.skills_mode : props.role.mcp_mode,
)
const selected = ref<string[]>(
  [...(props.kind === 'skills' ? props.role.selected_skills : props.role.selected_mcp_servers)],
)

const saving = ref(false)
const saveError = ref('')

function toggle(name: string) {
  const i = selected.value.indexOf(name)
  if (i >= 0) selected.value.splice(i, 1)
  else selected.value.push(name)
}

function onClose() {
  if (saving.value) return
  emit('close')
}

async function onSave() {
  if (saving.value) return
  saveError.value = ''
  saving.value = true
  try {
    // 整体覆盖写:克隆完整列表,只更新目标角色的该项绑定,其余字段不变。
    const next = props.allRoles.map((r) => ({ ...r }))
    const idx = next.findIndex((r) => r.name === props.role.name)
    if (idx < 0) {
      saveError.value = '角色不存在,可能已被删除,请刷新后重试'
      saving.value = false
      return
    }
    const cur = next[idx]
    if (props.kind === 'skills') {
      cur.skills_mode = mode.value
      cur.selected_skills = [...selected.value]
    } else {
      cur.mcp_mode = mode.value
      cur.selected_mcp_servers = [...selected.value]
    }
    await putRolesConfig(next)
    emit('saved')
  } catch (e) {
    saveError.value = (e as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
/* 遮罩 + 卡片壳:复用与 RoleEditDialog 一致的设计语言。 */
.role-dialog-overlay {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-xl);
}
.role-dialog {
  width: 460px;
  max-width: 92vw;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  background: var(--canvas-soft);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
}
.role-dialog-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-lg) var(--sp-xl);
  border-bottom: 1px solid var(--hairline);
  flex-shrink: 0;
}
.role-dialog-head h3 {
  font-size: 16px;
  font-weight: 400;
  line-height: 22px;
  letter-spacing: -0.2px;
  color: var(--ink);
}
.role-dialog-close {
  background: transparent;
  border: none;
  color: var(--body-mid);
  font-size: 16px;
  cursor: pointer;
  padding: var(--sp-xs) var(--sp-sm);
  border-radius: var(--radius-sm);
  transition: color 0.15s, background 0.15s;
}
.role-dialog-close:hover { color: var(--ink); background: var(--canvas); }

.role-dialog-error {
  margin: var(--sp-md) var(--sp-xl) 0;
  padding: var(--sp-sm) var(--sp-md);
  background: rgba(255, 122, 23, 0.06);
  border-left: 2px solid var(--accent);
  border-radius: 4px;
}
.role-dialog-error p { color: var(--accent-soft); font-size: 13px; line-height: 18px; }

.role-dialog-body {
  padding: var(--sp-lg) var(--sp-xl);
  overflow-y: auto;
  flex: 1;
}

.role-dialog-foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-sm);
  padding: var(--sp-lg) var(--sp-xl);
  border-top: 1px solid var(--hairline);
  flex-shrink: 0;
}

.role-mode-row {
  display: flex;
  align-items: center;
  gap: var(--sp-md);
  flex-wrap: wrap;
  margin-bottom: var(--sp-sm);
}
.role-radio {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-xs);
  font-size: 13px;
  color: var(--body);
  cursor: pointer;
  user-select: none;
}
.role-radio input {
  width: 15px;
  height: 15px;
  cursor: pointer;
  accent-color: var(--ink);
}
.role-multi {
  display: flex;
  flex-direction: column;
  gap: var(--sp-xs);
  margin-top: var(--sp-xs);
  padding-left: var(--sp-xs);
}
.role-check {
  display: flex;
  align-items: center;
  gap: var(--sp-xs);
  font-size: 13px;
  color: var(--body);
  cursor: pointer;
  user-select: none;
}
.role-check input {
  width: 15px;
  height: 15px;
  cursor: pointer;
  accent-color: var(--ink);
  flex: 0 0 auto;
}
.role-check-name {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  flex: 0 0 auto;
}
.role-multi-empty {
  font-size: 12px;
  color: var(--body-mid);
  line-height: 16px;
  margin: 0;
}

@media (max-width: 768px) {
  .role-dialog { width: 100%; }
}
</style>
