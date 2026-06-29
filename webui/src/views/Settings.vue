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

        <div v-else-if="currentGroup === 'prompts'" class="settings-content">
          <!-- 提示词编辑页:独立数据形态(整文件多行文本)。PromptsCard 自管加载,
               保存/放弃复用全局悬浮保存条(与其他设置页交互一致)。 -->
          <PromptsCard ref="promptsRef" />

          <!-- 全局保存条(与其它分组共用同一组件类),dirty 时悬浮显示。 -->
          <div class="settings-actions" :class="{ visible: promptsDirty }">
            <span class="dirty-hint" v-if="promptsDirty">有未保存的修改</span>
            <span class="dirty-hint saved" v-else-if="promptsJustSaved">已保存</span>
            <div class="action-buttons">
              <button class="btn" :disabled="!promptsDirty || promptsSaving" @click="onResetPrompts">放弃修改</button>
              <button class="btn btn-primary" :disabled="!promptsDirty || promptsSaving" @click="onSavePrompts">
                {{ promptsSaving ? '保存中…' : '保存' }}
              </button>
            </div>
          </div>
        </div>

        <div v-else-if="currentGroup === 'skills'" class="settings-content">
          <!-- Skills 配置:扫到的技能清单 + 单项开关,写回各 SKILL.md。SkillsCard 自管加载,
               保存/放弃复用全局悬浮保存条(与 prompts 一致)。 -->
          <SkillsCard ref="skillsRef" />

          <div class="settings-actions" :class="{ visible: skillsDirty }">
            <span class="dirty-hint" v-if="skillsDirty">有未保存的修改</span>
            <span class="dirty-hint saved" v-else-if="skillsJustSaved">已保存</span>
            <div class="action-buttons">
              <button class="btn" :disabled="!skillsDirty || skillsSaving" @click="onResetSkills">放弃修改</button>
              <button class="btn btn-primary" :disabled="!skillsDirty || skillsSaving" @click="onSaveSkills">
                {{ skillsSaving ? '保存中…' : '保存' }}
              </button>
            </div>
          </div>
        </div>

        <div v-else-if="currentGroup === 'roles'" class="settings-content">
          <!-- 角色管理:小卡片列表 + 编辑弹窗。RolesCard 自管加载与保存(弹窗内直接落盘,
               无全局 dirty/保存条),与 prompts/skills/mcp 的"顶部保存条"模型不同。 -->
          <RolesCard ref="rolesRef" />
        </div>

        <div v-else-if="currentGroup === 'mcp'" class="settings-content">
          <!-- MCP 配置:动态服务器清单(增删改),整体覆盖写 mcp-servers.json。McpServersCard 自管加载,
               保存/放弃复用全局悬浮保存条(与 prompts 一致)。 -->
          <McpServersCard ref="mcpRef" />

          <div class="settings-actions" :class="{ visible: mcpDirty }">
            <span class="dirty-hint" v-if="mcpDirty">有未保存的修改</span>
            <span class="dirty-hint saved" v-else-if="mcpJustSaved">已保存</span>
            <div class="action-buttons">
              <button class="btn" :disabled="!mcpDirty || mcpSaving" @click="onResetMcp">放弃修改</button>
              <button class="btn btn-primary" :disabled="!mcpDirty || mcpSaving" @click="onSaveMcp">
                {{ mcpSaving ? '保存中…' : '保存' }}
              </button>
            </div>
          </div>
        </div>

        <div v-else-if="fields.length" class="settings-content">
          <!-- 记忆管理入口:仅 memory 分组显示。点击打开单一管理弹窗,弹窗内完成
               全部查看/编辑/删除(弹窗自管加载与保存,不接入本页 dirty/保存条)。 -->
          <section v-if="currentGroup === 'memory'" class="settings-card memory-manage-entry">
            <div class="card-head">
              <h3 class="card-title">记忆管理</h3>
              <p class="card-desc">查看、修改或删除 Agent 已记住的内容(长期记忆 L1/L2/L3)。编辑仅限 L1 原子记忆,删除为软删除。</p>
            </div>
            <button type="button" class="btn btn-primary memory-manage-btn" @click="showMemoryDialog = true">管理记忆</button>
          </section>

          <!-- 按 currentGroup 渲染对应卡片组;卡片元数据见 GROUP_CARDS。 -->
          <section
            v-for="card in activeCards"
            :key="card.key"
            class="settings-card"
            :class="{ 'is-collapsed': card.collapsible && isCollapsed(card.key) }"
          >
            <div class="card-head">
              <h3 class="card-title">
                <button
                  v-if="card.collapsible"
                  type="button"
                  class="card-collapse-btn"
                  :class="{ collapsed: isCollapsed(card.key) }"
                  :aria-expanded="!isCollapsed(card.key)"
                  @click="toggleCard(card.key)"
                >
                  <span class="card-chevron">
                    <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
                      <path d="M4 2.5 L8 6 L4 9.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </span>
                  <span class="card-title-text">{{ card.title }}</span>
                  <span class="card-collapse-label" v-if="isCollapsed(card.key)">展开</span>
                  <span class="card-collapse-label expanded" v-else>收起</span>
                </button>
                <template v-else>{{ card.title }}</template>
                <span v-if="card.advanced" class="card-advanced-tag">高级</span>
              </h3>
              <p class="card-desc">{{ card.desc }}</p>
            </div>
            <div v-show="!card.collapsible || !isCollapsed(card.key)" class="card-grid">
              <ConfigField
                v-for="f in fieldsByGroup(card.key)"
                :key="f.path"
                :field="f"
                :secret-masked="isMasked(f.path)"
                v-model="form[f.path]"
                @unmask="onUnmask(f.path)"
              />
            </div>
            <p v-if="card.restartNote" class="card-restart-note">⚠️ {{ card.restartNote }}</p>
          </section>

          <!-- 保存条(所有分组共用) -->
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

    <!-- 记忆管理弹窗:由「记忆系统」配置页的入口卡片触发。 -->
    <MemoryManageDialog v-if="showMemoryDialog" @close="showMemoryDialog = false" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import ConfigField from '../components/ConfigField.vue'
import PromptsCard from '../components/PromptsCard.vue'
import RolesCard from '../components/RolesCard.vue'
import SkillsCard from '../components/SkillsCard.vue'
import McpServersCard from '../components/McpServersCard.vue'
import MemoryManageDialog from '../components/MemoryManageDialog.vue'
import { getConfigGroup, updateConfigGroup } from '../api'
import type { ConfigField as ConfigFieldType } from '../api'

const router = useRouter()
const route = useRoute()

// 设置分组元信息(左侧导航)。新增分组时在此追加,并扩展 GROUP_CARDS。
const groups = [
  { id: 'ai-models', label: 'AI 模型', icon: '🤖' },
  { id: 'roles', label: '角色管理', icon: '🎭' },
  { id: 'prompts', label: '提示词', icon: '💬' },
  { id: 'skills', label: 'Skills', icon: '🧩' },
  { id: 'mcp', label: 'MCP 服务器', icon: '🔌' },
  { id: 'network', label: '网络与服务', icon: '🌐' },
  { id: 'browser', label: '浏览器自动化', icon: '🖥️' },
  { id: 'memory', label: '记忆系统', icon: '🧠' },
  { id: 'context', label: '上下文压缩', icon: '✂️' },
  { id: 'scheduler', label: '任务调度', icon: '⏰' },
  { id: 'research', label: '联网研究', icon: '🔎' },
  { id: 'paths-logging', label: '路径与日志', icon: '📁' },
] as const

// 各分组下的卡片定义:key=字段 group(后端字段表里的 group 字段),用于 fieldsByGroup 过滤。
// restartNote 非空时在卡片底部提示"改后需重启"。
// collapsible=true 的卡片可折叠,defaultCollapsed=true 表示初始收起(recall 高级调参默认折叠)。
// advanced=true 时标题旁显示"高级"标签,用于标注高级调参组。
const GROUP_CARDS: Record<string, {
  key: string
  title: string
  desc: string
  restartNote?: string
  collapsible?: boolean
  defaultCollapsed?: boolean
  advanced?: boolean
}[]> = {
  'ai-models': [
    { key: 'llm', title: '主模型 · LLM', desc: 'Agent 对话使用的核心模型。改后新会话生效。' },
    { key: 'vision', title: '视觉模型 · VISION', desc: '主模型不支持图片时,可由辅助视觉模型先识别图片再交给主模型。' },
    { key: 'summary', title: '摘要压缩辅助模型 · SUMMARY', desc: '长对话上下文压缩用的辅助模型;留空则复用主模型。' },
  ],
  'network': [
    { key: 'http', title: 'HTTP 服务', desc: 'WebUI / API 的监听地址、端口与访问令牌。改后需重启进程。', restartNote: '改了监听地址/端口/Token 需重启进程,否则不生效;改 Token 后前端需用新 Token 重新登录。' },
    { key: 'telegram', title: 'Telegram', desc: 'Telegram Bot 接入。改后需重启(bot 在启动期建连)。', restartNote: '改了 Bot Token / 代理需重启进程,bot 才会重新建连。' },
    { key: 'behavior', title: '消息行为', desc: '工具调用展示、流式心跳、附件上限等即时生效项。' },
  ],
  'browser': [
    { key: 'instance', title: '浏览器实例', desc: '浏览器启动方式、无头模式、UA、窗口与超时。基本热生效——下次启动浏览器实例时生效,当前会话无需重启进程。' },
  ],
  'memory': [
    { key: 'memory-general', title: '总开关', desc: '记忆系统的启用开关与数据库位置。改后建议重启进程。', restartNote: '改了 总开关 / db_path / Embedding 配置后建议重启进程,后台捕获与向量化客户端才会重新初始化。' },
    { key: 'memory-embedding', title: 'Embedding 向量化', desc: '向量化模型、密钥与维度。改后建议重启,改维度/模型须重建记忆库。' },
    { key: 'memory-recall', title: '召回检索 · 高级调参,非必要勿改', desc: 'BM25/向量/RRF/重排各阶段候选数与终筛阈值。影响召回质量与上下文预算,默认值已平衡效果与成本。', collapsible: true, defaultCollapsed: true, advanced: true },
    { key: 'memory-capture', title: '记忆捕获', desc: '从对话提取记忆的开关、异步模式与阈值。基本热生效——下次捕获生效。' },
    { key: 'memory-privacy', title: '隐私', desc: 'PII 脱敏、机密记忆与原始轮次保留期。基本热生效——下次捕获/清理生效。' },
  ],
  'context': [
    { key: 'context-advanced', title: '上下文压缩 · 高级调参,调好了就别动', desc: '长会话压缩阈值与保留策略。误调可能导致对话爆窗口或被过度压缩,默认值已平衡。新会话生效。', collapsible: true, defaultCollapsed: true, advanced: true, restartNote: '⚠️ 这是"调好了就别动"的参数:阈值调高更晚压缩、更易爆上下文窗口;调低则更早压缩、可能过度压缩而丢失上下文。非必要勿改。' },
  ],
  'scheduler': [
    { key: 'scheduler-main', title: '调度主参数', desc: '任务调度的启用、数据库、轮询与并发。改后需重启进程才生效。', restartNote: '调度器在启动期初始化 DB 与轮询循环,改这里需重启进程。' },
    { key: 'scheduler-delivery', title: '投递', desc: '任务结果投递的默认模式与失败回落。改后需重启进程。' },
    { key: 'scheduler-security', title: '安全', desc: '定时任务执行中的工具权限策略。改后需重启进程。' },
  ],
  'research': [
    { key: 'research-main', title: '联网研究', desc: '搜索后端、API Key 与结果策略。brave/tavily 需对应 Key,勾选后端时记得填写其 Key。', restartNote: '后端选择与对应 API Key 联动:启用 brave/tavily 前请先填好对应 Key,否则该后端会报错。' },
  ],
  'paths-logging': [
    { key: 'paths', title: '路径', desc: '角色/记忆/外部技能/附件目录。改错会导致服务找不到资源,改后需重启进程。', restartNote: '路径改错可能导致服务找不到角色/记忆库/技能/附件,且需重启进程才重新加载;请谨慎修改。' },
    { key: 'logging', title: '日志', desc: '日志级别、目录与控制台输出。改后需重启进程。', restartNote: '日志配置改后需重启进程才彻底切换。' },
  ],
}

const activeMeta = computed(() => groups.find((g) => g.id === currentGroup.value))
const activeCards = computed(() => GROUP_CARDS[currentGroup.value] || [])

const currentGroup = ref<string>((route.params.group as string) || 'ai-models')

// 记忆管理弹窗:在「记忆系统」配置页顶部放入口卡片,点击打开单一弹窗完成全部
// 查看/编辑/删除操作(弹窗自管一切,不触碰本页 dirty/保存条逻辑)。
const showMemoryDialog = ref(false)

const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const justSaved = ref(false)

// 提示词页子组件引用:通过 defineExpose 暴露 dirty/save/reset,让全局保存条复用。
const promptsRef = ref<InstanceType<typeof PromptsCard> | null>(null)
// Skills / MCP 子组件引用:同 PromptsCard,各自 defineExpose 暴露 dirty/saving/justSaved/save/reset。
const skillsRef = ref<InstanceType<typeof SkillsCard> | null>(null)
const mcpRef = ref<InstanceType<typeof McpServersCard> | null>(null)
const rolesRef = ref<InstanceType<typeof RolesCard> | null>(null)
// 代理各子组件的暴露态;非对应分组时各值为 false/undefined,保存条自然不显示。
// 注:Vue 对 setup 暴露的 ref/computed 在父组件通过 instance ref 访问时自动解包,故直接读值。
const promptsDirty = computed(() => !!promptsRef.value?.dirty)
const promptsSaving = computed(() => !!promptsRef.value?.saving)
const promptsJustSaved = computed(() => !!promptsRef.value?.justSaved)
const skillsDirty = computed(() => !!skillsRef.value?.dirty)
const skillsSaving = computed(() => !!skillsRef.value?.saving)
const skillsJustSaved = computed(() => !!skillsRef.value?.justSaved)
const mcpDirty = computed(() => !!mcpRef.value?.dirty)
const mcpSaving = computed(() => !!mcpRef.value?.saving)
const mcpJustSaved = computed(() => !!mcpRef.value?.justSaved)
// 注:roles 不在此列——它走"弹窗内直接保存"模型,无全局 dirty,故不接入顶部保存条。

async function onSavePrompts() {
  await promptsRef.value?.save()
}
function onResetPrompts() {
  promptsRef.value?.reset()
}
async function onSaveSkills() {
  await skillsRef.value?.save()
}
function onResetSkills() {
  skillsRef.value?.reset()
}
async function onSaveMcp() {
  await mcpRef.value?.save()
}
function onResetMcp() {
  mcpRef.value?.reset()
}

const fields = ref<ConfigFieldType[]>([])
// 原始值快照:保存基准,用于 dirty 比对与"放弃修改"还原。
// secret 字段保存的是脱敏后的值(来自后端),用户不主动改它时永远等于快照 → 不算 dirty。
const snapshot = ref<Record<string, string | number | boolean | string[]>>({})
// 当前编辑表单。
const form = ref<Record<string, string | number | boolean | string[]>>({})
// 记录哪些 secret 字段当前是脱敏态(未改动);一旦用户点击"修改",移出该集合。
const maskedSecrets = ref<Set<string>>(new Set())
// 折叠态:collapsedKeys 存放当前处于收起态的卡片 key。
// 初始按当前分组各卡片 defaultCollapsed 推导;切换分组时在 loadConfig 内重建。
const collapsedCards = ref<Set<string>>(new Set())

const dirty = computed(() => {
  // 特殊分组分支:用各子组件暴露的 dirty;其余分组:比对 config 字段快照。
  // roles 走弹窗内直接保存、无全局 dirty,故不在此分支——落到下方字段快照比对时,
  // fields 为空、snapshot 为空,dirty 自然为 false(切组/返回不会被未保存态拦截)。
  if (currentGroup.value === 'prompts') return promptsDirty.value
  if (currentGroup.value === 'skills') return skillsDirty.value
  if (currentGroup.value === 'mcp') return mcpDirty.value
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

// 卡片是否处于收起态。
function isCollapsed(cardKey: string): boolean {
  return collapsedCards.value.has(cardKey)
}

// 切换卡片折叠;首次点击展开后即便切组再回来,按 defaultCollapsed 重置(见 resetCollapsed)。
function toggleCard(cardKey: string) {
  const next = new Set(collapsedCards.value)
  if (next.has(cardKey)) next.delete(cardKey)
  else next.add(cardKey)
  collapsedCards.value = next
}

// 切换分组时按卡片的 defaultCollapsed 重建折叠态。
function resetCollapsed() {
  const next = new Set<string>()
  for (const card of activeCards.value) {
    if (card.collapsible && card.defaultCollapsed) next.add(card.key)
  }
  collapsedCards.value = next
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

// 比较两个值是否相等(区分 bool/number/string/array,避免 1 === true 误判与数组引用误判)。
function valueEquals(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
    return a.every((v, i) => v === b[i])
  }
  if (typeof a !== typeof b) return false
  return a === b
}

async function loadConfig() {
  // 特殊分组分支走各自子组件自管加载,这里跳过避免空 getConfigGroup 调用。
  // roles 同属特殊分组(数据走 /roles-config,不在 yaml 字段表),漏判会 fallthrough
  // 到 getConfigGroup('roles') → 后端 /config/{group} 查不到该组返回 404。
  if (currentGroup.value === 'prompts' || currentGroup.value === 'skills' || currentGroup.value === 'mcp' || currentGroup.value === 'roles') {
    fields.value = []
    snapshot.value = {}
    form.value = {}
    maskedSecrets.value = new Set()
    justSaved.value = false
    resetCollapsed()
    return
  }
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
    // 切换分组后按卡片默认折叠态重建(如 recall 高级调参默认收起)。
    resetCollapsed()
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
    const updates: Record<string, string | number | boolean | string[]> = {}
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
.settings-nav-list li:hover { background: var(--row-hover); color: var(--ink); }
.settings-nav-list li.active {
  background: var(--canvas-soft);
  color: var(--ink);
  border-left-color: var(--ink);
}
/* hover 选中项:保持 active 实色背景,避免 hover 淡背景覆盖选中态(与会话列表一致)。 */
.settings-nav-list li.active:hover {
  background: var(--canvas-soft);
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
  transition: border-color 0.15s, background 0.15s;
}
/* 收起态:卡片描边淡化、底色微弱化,与展开态产生视觉层次差,暗示"还有内容可展开"。 */
.settings-card.is-collapsed {
  border-style: dashed;
  background: transparent;
}
.card-head { margin-bottom: var(--sp-lg); }
.settings-card.is-collapsed .card-head { margin-bottom: 0; }
.card-head h3 {
  font-size: 16px;
  font-weight: 400;
  color: var(--ink);
  margin-bottom: var(--sp-xs);
  display: flex;
  align-items: baseline;
  gap: var(--sp-sm);
}
.card-title {
  /* h3 自身已是 flex 容器;让标题与"高级"标签同行。 */
  margin: 0;
}
/* 折叠按钮:整行可点击,但箭头做成带边框的方块,hover 时整按钮高亮,
   让"可展开/收起"的交互足够清晰。 */
.card-collapse-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-sm);
  padding: 2px 0;
  background: transparent;
  border: none;
  font: inherit;
  color: var(--body);
  cursor: pointer;
  text-align: left;
  transition: color 0.15s;
}
.card-chevron {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex: 0 0 auto;
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  background: var(--canvas-soft);
  color: var(--body-mid);
  transition: color 0.15s, background 0.15s, border-color 0.15s;
}
.card-collapse-btn:hover { color: var(--ink); }
.card-collapse-btn:hover .card-chevron {
  border-color: var(--pill-border-hover);
  background: var(--canvas);
  color: var(--ink);
}
/* 箭头方块内的 svg 随展开旋转 */
.card-chevron svg {
  transition: transform 0.18s ease;
  transform: rotate(0deg);
}
.card-collapse-btn:not(.collapsed) .card-chevron svg {
  transform: rotate(90deg);
}
.card-title-text { font-weight: inherit; }
/* "展开/收起"小标签:进一步明示当前态与可交互性 */
.card-collapse-label {
  flex: 0 0 auto;
  padding: 1px var(--sp-sm);
  font-size: 11px;
  letter-spacing: 0.3px;
  color: var(--body-mid);
  border: 1px solid var(--pill-border);
  border-radius: var(--radius-pill);
  background: var(--canvas-soft);
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.card-collapse-btn:hover .card-collapse-label { color: var(--ink); border-color: var(--pill-border-hover); }
.card-collapse-label.expanded { color: var(--body-mid); }
.card-advanced-tag {
  flex: 0 0 auto;
  padding: 1px var(--sp-sm);
  font-size: 11px;
  letter-spacing: 0.4px;
  color: var(--body-mid);
  border: 1px solid var(--pill-border);
  border-radius: var(--radius-pill);
  background: var(--canvas-soft);
}
.card-desc {
  font-size: 13px;
  line-height: 18px;
  color: var(--body-mid);
}
/* 卡片底部"改后需重启"提示 */
.card-restart-note {
  margin-top: var(--sp-md);
  padding-top: var(--sp-md);
  border-top: 1px dashed var(--hairline);
  font-size: 12px;
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

/* 记忆管理入口卡片:寄生在 memory 配置分组顶部,按钮置于卡片内。 */
.memory-manage-entry {
  display: flex;
  flex-direction: column;
  gap: var(--sp-md);
}
.memory-manage-btn {
  align-self: flex-start;
  padding: var(--sp-xs) var(--sp-lg);
}
</style>
