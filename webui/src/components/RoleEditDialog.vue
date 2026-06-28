<template>
  <!-- 角色名称/提示词编辑弹窗:只编辑名称与提示词,绑定(skills/mcp)由各自独立的绑定弹窗管理。
       保存时整体覆盖写 /roles-config,保留该角色原有绑定不变。 -->
  <teleport to="body">
    <div class="role-dialog-overlay" @click.self="onClose">
      <div class="role-dialog" role="dialog" aria-modal="true">
        <div class="role-dialog-head">
          <h3>{{ isCreate ? '新增角色' : '编辑角色' }}</h3>
          <button type="button" class="role-dialog-close" @click="onClose" aria-label="关闭">✕</button>
        </div>

        <div v-if="saveError" class="role-dialog-error">
          <p>{{ saveError }}</p>
        </div>

        <div class="role-dialog-body">
          <label class="role-field">
            <span class="role-field-label">角色名称(唯一,如 coder)</span>
            <input
              class="role-name"
              v-model="form.name"
              placeholder="coder / writer / researcher …"
            />
          </label>

          <label class="role-field">
            <span class="role-field-label">提示词(user/roles/&lt;name&gt;.md)</span>
            <textarea
              class="role-textarea"
              rows="10"
              v-model="form.prompt"
              placeholder="你是 … 的助手,具备 …"
            />
          </label>
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
import type { RoleBindingItem } from '../api'

// 本地编辑形态:仅名称 + 提示词(绑定不在本弹窗编辑)。
interface RoleForm {
  name: string
  prompt: string
}

const props = defineProps<{
  // 编辑时为该角色的当前态;新增时为 null(用空表单)。
  role: RoleBindingItem | null
  // 完整列表快照(只读):后端是整体覆盖写,单条编辑需在此基础上替换/追加后整体提交。
  allRoles: RoleBindingItem[]
}>()

const emit = defineEmits<{
  close: []
  saved: []
}>()

const isCreate = props.role === null

// 初始化表单:编辑预填当前角色,新增空白。
const form = ref<RoleForm>(
  props.role
    ? { name: props.role.name, prompt: props.role.prompt }
    : { name: '', prompt: '' },
)

const saving = ref(false)
const saveError = ref('')

function onClose() {
  if (saving.value) return
  emit('close')
}

async function onSave() {
  if (saving.value) return
  saveError.value = ''
  const name = form.value.name.trim()
  if (!name) {
    saveError.value = '角色名称不能为空'
    return
  }
  if (!form.value.prompt.trim()) {
    saveError.value = '角色提示词不能为空'
    return
  }
  // 重名校验:排除自身(编辑场景下自身原名允许保留)。
  const others = props.allRoles
    .filter((r) => r.name !== props.role?.name)
    .map((r) => r.name)
  if (others.includes(name)) {
    saveError.value = `角色名称重复: ${name}`
    return
  }

  saving.value = true
  try {
    // 整体覆盖写:在完整列表上替换当前编辑条(编辑)或追加(新增),整体 PUT。
    // 仅更新名称 + 提示词,绑定字段沿用原值(新增角色默认 all/空)。
    const next = props.allRoles.map((r) => ({ ...r }))
    const edited: RoleBindingItem = {
      name,
      prompt: form.value.prompt,
      skills_mode: props.role?.skills_mode ?? 'all',
      selected_skills: props.role ? [...props.role.selected_skills] : [],
      mcp_mode: props.role?.mcp_mode ?? 'all',
      selected_mcp_servers: props.role ? [...props.role.selected_mcp_servers] : [],
    }
    const idx = props.role ? next.findIndex((r) => r.name === props.role!.name) : -1
    if (idx >= 0) next[idx] = edited
    else next.push(edited)
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
/* 遮罩:半透明黑盖住全屏,点击空白关闭。 */
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
/* 弹窗卡片:canvas-soft + 发丝边 + 8px 圆角,无阴影(遵循设计规范)。 */
.role-dialog {
  width: 600px;
  max-width: 92vw;
  max-height: 86vh;
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
  font-size: 18px;
  font-weight: 400;
  line-height: 24px;
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

/* 字段样式:迁移自旧 RolesCard,保持输入控件视觉一致。 */
.role-field {
  display: block;
  margin-bottom: var(--sp-lg);
}
.role-field-label {
  display: block;
  font-size: 12px;
  letter-spacing: 0.3px;
  color: var(--body-mid);
  margin-bottom: var(--sp-xs);
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}
.role-name {
  width: 100%;
  padding: var(--sp-xs) var(--sp-md);
  background: var(--canvas);
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: 14px;
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  outline: none;
  box-sizing: border-box;
  transition: border-color 0.15s;
}
.role-name:focus { border-color: var(--pill-border-hover); }
.role-textarea {
  width: 100%;
  padding: var(--sp-xs) var(--sp-md);
  background: var(--canvas);
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: 13px;
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  line-height: 20px;
  outline: none;
  box-sizing: border-box;
  transition: border-color 0.15s;
  resize: vertical;
}
.role-textarea:focus { border-color: var(--pill-border-hover); }

@media (max-width: 768px) {
  .role-dialog { width: 100%; }
}
</style>
