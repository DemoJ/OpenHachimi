<template>
  <!-- 记忆管理弹窗:单一弹窗内完成全部操作 —— 列表查看、搜索/角色/层级/类型筛选、
       L1 编辑(弹窗内编辑态)、任意层级软删除(ConfirmDialog 二次确认)。镜像
       RoleEditDialog 的 teleport-to-body + head/body/foot 骨架,body 区放大承载完整管理界面。 -->
  <teleport to="body">
    <div class="mem-dialog-overlay" @click.self="onClose">
      <div class="mem-dialog" role="dialog" aria-modal="true">
        <div class="mem-dialog-head">
          <h3>记忆管理</h3>
          <button type="button" class="mem-dialog-close" @click="onClose" aria-label="关闭">✕</button>
        </div>

        <div class="mem-dialog-body">
          <!-- 统计条:库内各层级条数 + 待向量化数,轻量小标签。 -->
          <div v-if="enabled" class="mem-stats">
            <span class="mem-stat"><span class="mem-stat-label">L1 原子</span><span class="mem-stat-value">{{ stats.atoms ?? 0 }}</span></span>
            <span class="mem-stat-sep">·</span>
            <span class="mem-stat"><span class="mem-stat-label">L2 区块</span><span class="mem-stat-value">{{ stats.blocks ?? 0 }}</span></span>
            <span class="mem-stat-sep">·</span>
            <span class="mem-stat"><span class="mem-stat-label">L3 画像</span><span class="mem-stat-value">{{ stats.profiles ?? 0 }}</span></span>
            <span class="mem-stat-sep">·</span>
            <span class="mem-stat"><span class="mem-stat-label">轮次</span><span class="mem-stat-value">{{ stats.turns ?? 0 }}</span></span>
            <span v-if="(stats.embeddings_pending ?? 0) > 0" class="mem-stat-sep">·</span>
            <span v-if="(stats.embeddings_pending ?? 0) > 0" class="mem-stat mem-stat-warn">
              <span class="mem-stat-label">待向量化</span><span class="mem-stat-value">{{ stats.embeddings_pending }}</span>
            </span>
          </div>

          <!-- 未启用提示:memory.enabled=false 时仅展示提示,不渲染工具栏/列表。 -->
          <div v-if="!enabled" class="mem-empty">
            <p>记忆系统未启用。请在下方「记忆系统」配置中开启总开关后重启进程。</p>
          </div>

          <template v-else>
            <!-- 工具栏:搜索 + 角色/层级/类型筛选 + 刷新。 -->
            <div class="mem-toolbar">
              <input
                class="mem-search"
                v-model="searchText"
                placeholder="搜索记忆内容…"
                @input="onSearchInput"
              />
              <select class="mem-select" v-model="roleFilter" @change="reload">
                <option value="__all__">全部角色</option>
                <option v-for="r in roles" :key="r" :value="r">{{ r }}</option>
              </select>
              <select class="mem-select" v-model="levelFilter" @change="reload">
                <option value="">全部层级</option>
                <option value="L1">L1 原子</option>
                <option value="L2">L2 区块</option>
                <option value="L3">L3 画像</option>
              </select>
              <select class="mem-select" v-model="typeFilter" @change="reload">
                <option value="">全部类型</option>
                <option v-for="t in availableTypes" :key="t" :value="t">{{ t }}</option>
              </select>
              <button type="button" class="btn mem-refresh" @click="reload">刷新</button>
            </div>

            <!-- loading / error -->
            <div v-if="loading" class="mem-loading">
              <span class="activity-spinner" />
              <span>加载记忆中…</span>
            </div>
            <div v-else-if="loadError" class="mem-error">
              <p>{{ loadError }}</p>
              <button class="btn" @click="reload">重试</button>
            </div>

            <!-- 列表 -->
            <div v-else>
              <section v-for="item in items" :key="item.id" class="mem-card" :class="{ editing: editingId === item.id }">
                <!-- 编辑态:textarea 预填内容,保存/取消。L2/L3 不会进入此态(编辑按钮置灰)。 -->
                <template v-if="editingId === item.id">
                  <div class="mem-edit-head">
                    <span class="mem-level-badge" :class="levelClass(item.level)">{{ item.level }}</span>
                    <span class="mem-card-spacer" />
                    <button type="button" class="btn btn-mini" :disabled="saving" @click="cancelEdit">取消</button>
                    <button type="button" class="btn btn-primary btn-mini" :disabled="saving" @click="saveEdit">
                      {{ saving ? '保存中…' : '保存' }}
                    </button>
                  </div>
                  <textarea class="mem-edit-textarea" rows="6" v-model="editContent" :disabled="saving"></textarea>
                  <p v-if="editError" class="mem-edit-error">{{ editError }}</p>
                </template>

                <!-- 查看态 -->
                <template v-else>
                  <div class="mem-card-head">
                    <span class="mem-level-badge" :class="levelClass(item.level)">{{ item.level }}</span>
                    <span class="mem-type">{{ item.memory_type }}</span>
                    <span class="mem-conf">置信度 {{ formatConf(item.confidence) }}</span>
                    <span class="mem-time">{{ formatTime(item.updated_at) }}</span>
                    <span class="mem-card-spacer" />
                    <button
                      type="button"
                      class="btn btn-mini"
                      :disabled="!item.editable || saving"
                      :title="item.editable ? '编辑' : 'L2/L3 为只读'"
                      @click="openEdit(item)"
                    >编辑</button>
                    <button
                      type="button"
                      class="btn btn-mini mem-del-btn"
                      :disabled="saving"
                      @click="askDelete(item)"
                    >删除</button>
                  </div>
                  <p class="mem-content">{{ item.content }}</p>
                  <div v-if="keywordsOf(item).length" class="mem-tags">
                    <span v-for="kw in keywordsOf(item)" :key="kw" class="mem-tag">{{ kw }}</span>
                  </div>
                </template>
              </section>

              <div v-if="!items.length" class="mem-empty">
                <p>{{ searchText || roleFilter !== '__all__' || levelFilter || typeFilter ? '没有匹配的记忆。' : '暂无记忆。与 Agent 对话后会自动捕获并写入长期记忆。' }}</p>
              </div>

              <!-- 加载更多:limit 递增式(后端无 offset 分页),上限 200。
                    当返回条数 < currentLimit 时说明已无更多数据,隐藏按钮。 -->
              <div v-if="items.length && items.length >= currentLimit && currentLimit < 200" class="mem-load-more">
                <button type="button" class="btn mem-load-more-btn" :disabled="loading" @click="loadMore">
                  {{ loading ? '加载中…' : '加载更多' }}
                </button>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 删除确认:复用 ConfirmDialog,弹窗内的二级浮层(teleport 到 body,DOM 顺序在后故居上)。 -->
    <ConfirmDialog
      v-if="pendingDelete"
      title="删除记忆"
      :message="deleteMessage"
      confirm-text="删除"
      :loading="saving"
      @confirm="confirmDelete"
      @cancel="cancelDelete"
    />
  </teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getMemories, updateMemory, deleteMemories } from '../api'
import type { MemoryItem } from '../api'
import ConfirmDialog from './ConfirmDialog.vue'

const emit = defineEmits<{ close: [] }>()

// 列表数据
const items = ref<MemoryItem[]>([])
const roles = ref<string[]>([])
const stats = ref<Record<string, number>>({})
const enabled = ref(true)
const loading = ref(false)
const loadError = ref('')

// 筛选项
const searchText = ref('')
const roleFilter = ref('__all__')
const levelFilter = ref('')
const typeFilter = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null

// 当前加载的 limit(递增式分页,上限 200)
const currentLimit = ref(50)

// 从当前结果动态收集 distinct memory_type,填充类型筛选下拉。
const availableTypes = computed(() => {
  const set = new Set<string>()
  for (const it of items.value) set.add(it.memory_type)
  return Array.from(set).sort()
})

// 编辑态:同一时间只编辑一条。editingId 非 null 即处于编辑态。
const editingId = ref<string | null>(null)
const editContent = ref('')
const editError = ref('')

// 删除确认态:pendingDelete 为待删条目;saving 复用于编辑/删除的提交中禁用。
const pendingDelete = ref<MemoryItem | null>(null)
const saving = ref(false)

const deleteMessage = computed(() => {
  const item = pendingDelete.value
  if (!item) return ''
  const preview = item.content.length > 60 ? item.content.slice(0, 60) + '…' : item.content
  return `确定删除该记忆？\n\n「${preview}」\n\n此操作为软删除,记忆将从列表移除且不再被召回。`
})

function levelClass(level: string): string {
  return `mem-level-${level.toLowerCase()}`
}

function keywordsOf(item: MemoryItem): string[] {
  const kw = (item.metadata as Record<string, unknown>).keywords
  return Array.isArray(kw) ? kw.filter((k): k is string => typeof k === 'string') : []
}

function formatConf(c: number): string {
  return Number(c).toFixed(2)
}

function formatTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const hh = d.getHours().toString().padStart(2, '0')
  const mm = d.getMinutes().toString().padStart(2, '0')
  if (sameDay) return `今天 ${hh}:${mm}`
  return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`
}

// 搜索框防抖:300ms 后触发刷新;清空时立刻回到 list 模式。
function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => reload(), 300)
}

async function reload() {
  if (loading.value) return
  loading.value = true
  loadError.value = ''
  try {
    const res = await getMemories({
      role: roleFilter.value === '__all__' ? undefined : roleFilter.value,
      q: searchText.value.trim() || undefined,
      memory_type: typeFilter.value || undefined,
      level: levelFilter.value || undefined,
      limit: currentLimit.value,
    })
    items.value = res.items
    roles.value = res.roles
    stats.value = res.stats || {}
    enabled.value = res.enabled
  } catch (e) {
    loadError.value = (e as Error).message || '加载记忆失败'
  } finally {
    loading.value = false
  }
}

function loadMore() {
  // 递增 limit 重新拉取(后端 list_memories 无 offset,只能放大窗口)。
  currentLimit.value = Math.min(200, currentLimit.value + 50)
  reload()
}

function onClose() {
  if (saving.value) return
  emit('close')
}

function openEdit(item: MemoryItem) {
  if (!item.editable) return
  editingId.value = item.id
  editContent.value = item.content
  editError.value = ''
}

function cancelEdit() {
  if (saving.value) return
  editingId.value = null
  editContent.value = ''
  editError.value = ''
}

async function saveEdit() {
  const id = editingId.value
  if (!id || saving.value) return
  const content = editContent.value.trim()
  if (!content) {
    editError.value = '内容不能为空'
    return
  }
  saving.value = true
  editError.value = ''
  try {
    await updateMemory(id, content)
    editingId.value = null
    editContent.value = ''
    await reload()
  } catch (e) {
    editError.value = (e as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}

function askDelete(item: MemoryItem) {
  if (saving.value) return
  pendingDelete.value = item
}

function cancelDelete() {
  if (saving.value) return
  pendingDelete.value = null
}

async function confirmDelete() {
  const item = pendingDelete.value
  if (!item || saving.value) return
  saving.value = true
  try {
    await deleteMemories([item.id])
    pendingDelete.value = null
    await reload()
  } catch (e) {
    // 删除失败:保留确认弹窗,把错误塞进 message 让用户看到并可重试/取消。
    pendingDelete.value = { ...item, content: `删除失败:${(e as Error).message || '未知错误'}\n\n原内容:${item.content}` }
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  reload()
})
</script>

<style scoped>
/* 遮罩 + 弹窗卡片:镜像 RoleEditDialog,但 body 区放大到近全屏承载管理界面。 */
.mem-dialog-overlay {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-xl);
}
.mem-dialog {
  width: 960px;
  max-width: 94vw;
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  background: var(--canvas-soft);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
}
.mem-dialog-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-lg) var(--sp-xl);
  border-bottom: 1px solid var(--hairline);
  flex-shrink: 0;
}
.mem-dialog-head h3 {
  font-size: 18px;
  font-weight: 400;
  line-height: 24px;
  letter-spacing: -0.2px;
  color: var(--ink);
}
.mem-dialog-close {
  background: transparent;
  border: none;
  color: var(--body-mid);
  font-size: 16px;
  cursor: pointer;
  padding: var(--sp-xs) var(--sp-sm);
  border-radius: var(--radius-sm);
  transition: color 0.15s, background 0.15s;
}
.mem-dialog-close:hover { color: var(--ink); background: var(--canvas); }

.mem-dialog-body {
  padding: var(--sp-lg) var(--sp-xl);
  overflow-y: auto;
  flex: 1;
}

/* 统计条 */
.mem-stats {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  flex-wrap: wrap;
  font-size: 12px;
  margin-bottom: var(--sp-md);
  padding-bottom: var(--sp-md);
  border-bottom: 1px dashed var(--hairline);
}
.mem-stat {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
}
.mem-stat-label {
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: 0.3px;
  color: var(--body-mid);
}
.mem-stat-value {
  color: var(--ink);
  font-weight: 500;
}
.mem-stat-sep { color: var(--body-mid); }
.mem-stat-warn .mem-stat-value { color: var(--accent); }

/* 工具栏 */
.mem-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  flex-wrap: wrap;
  margin-bottom: var(--sp-md);
}
.mem-search {
  flex: 1;
  min-width: 200px;
  padding: var(--sp-xs) var(--sp-md);
  background: var(--canvas);
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: 13px;
  font-family: inherit;
  outline: none;
  box-sizing: border-box;
  transition: border-color 0.15s;
}
.mem-search:focus { border-color: var(--pill-border-hover); }
.mem-select {
  padding: var(--sp-xs) var(--sp-md);
  background: var(--canvas);
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: 13px;
  font-family: inherit;
  outline: none;
  cursor: pointer;
  transition: border-color 0.15s;
}
.mem-select:focus { border-color: var(--pill-border-hover); }
.mem-refresh { padding: var(--sp-xs) var(--sp-md); }

/* loading / error / empty */
.mem-loading,
.mem-error {
  display: flex;
  align-items: center;
  gap: var(--sp-md);
  color: var(--body-mid);
  font-size: 14px;
  padding: var(--sp-xl) 0;
}
.mem-error { flex-direction: column; align-items: flex-start; gap: var(--sp-md); }
.mem-empty {
  color: var(--body-mid);
  font-size: 14px;
  padding: var(--sp-xl) 0;
  text-align: center;
}

/* 记忆卡片 */
.mem-card {
  background: var(--canvas-card);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  padding: var(--sp-md) var(--sp-lg);
  margin-bottom: var(--sp-sm);
}
.mem-card.editing {
  border-color: var(--pill-border-hover);
}
.mem-card-head {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  flex-wrap: wrap;
  margin-bottom: var(--sp-xs);
}
.mem-card-spacer { flex: 1; }

/* 层级徽章:L1/L2/L3 不同色 */
.mem-level-badge {
  flex: 0 0 auto;
  font-size: 11px;
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: 0.4px;
  padding: 1px var(--sp-sm);
  border-radius: var(--radius-pill);
  border: 1px solid var(--pill-border);
  background: var(--canvas-soft);
  color: var(--body-mid);
}
.mem-level-l1 { color: var(--ink); border-color: var(--pill-border-hover); }
.mem-level-l2 { color: var(--accent); border-color: rgba(255, 122, 23, 0.35); }
.mem-level-l3 { color: var(--body-mid); }

.mem-type {
  font-size: 12px;
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: 0.3px;
  color: var(--body);
}
.mem-conf,
.mem-time {
  font-size: 12px;
  color: var(--body-mid);
}

/* mini 按钮:迁移自 RolesCard */
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
.btn-mini:hover:not(:disabled) {
  border-color: var(--pill-border-hover);
  color: var(--ink);
  background: var(--canvas-soft);
}
.btn-mini:disabled { opacity: 0.4; cursor: not-allowed; }
.mem-del-btn:hover:not(:disabled) {
  border-color: rgba(255, 122, 23, 0.45);
  color: var(--accent);
  background: rgba(255, 122, 23, 0.08);
}

/* 内容正文 */
.mem-content {
  font-size: 13px;
  line-height: 20px;
  color: var(--body);
  white-space: pre-wrap;
  word-break: break-word;
}

/* 标签 */
.mem-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: var(--sp-xs);
}
.mem-tag {
  font-size: 11px;
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  padding: 1px var(--sp-sm);
  border-radius: var(--radius-pill);
  background: var(--canvas-soft);
  border: 1px solid var(--hairline);
  color: var(--body-mid);
}

/* 编辑态 */
.mem-edit-head {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  margin-bottom: var(--sp-xs);
}
.mem-edit-textarea {
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
.mem-edit-textarea:focus { border-color: var(--pill-border-hover); }
.mem-edit-error {
  margin-top: var(--sp-xs);
  font-size: 12px;
  color: var(--accent-soft);
}

/* 加载更多 */
.mem-load-more {
  text-align: center;
  padding: var(--sp-md) 0;
}
.mem-load-more-btn { padding: var(--sp-xs) var(--sp-lg); }

@media (max-width: 768px) {
  .mem-dialog { width: 100%; }
  .mem-search { min-width: 100%; }
}
</style>
