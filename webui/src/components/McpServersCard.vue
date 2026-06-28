<template>
  <div class="mcp-content">
    <div v-if="loading" class="mcp-loading">
      <span class="activity-spinner" />
      <span>加载 MCP 配置中…</span>
    </div>

    <div v-else-if="loadError" class="mcp-error">
      <p>{{ loadError }}</p>
      <button class="btn" @click="loadMcp">重试</button>
    </div>

    <div v-else>
      <section
        v-for="(s, idx) in form"
        :key="idx"
        class="settings-card mcp-card"
      >
        <div class="mcp-card-head">
          <input
            class="mcp-name"
            v-model="s.name"
            placeholder="服务器名称(唯一,如 local_tools)"
            @input="touch"
          />
          <select v-model="s.type" @change="onTypeChange(s)">
            <option value="stdio">stdio(本地进程)</option>
            <option value="http">http(远程)</option>
          </select>
          <button type="button" class="btn btn-mini" @click="remove(idx)">删除</button>
        </div>

        <!-- stdio 字段:command / args / env -->
        <template v-if="s.type === 'stdio'">
          <label class="mcp-field">
            <span class="mcp-field-label">command</span>
            <input
              class="mcp-input"
              v-model="s.command"
              placeholder="python / npx / node …"
              @input="touch"
            />
          </label>
          <label class="mcp-field">
            <span class="mcp-field-label">args(每行一个)</span>
            <textarea
              class="mcp-textarea"
              rows="3"
              v-model="s.argsText"
              placeholder="-m&#10;some_mcp_server"
              @input="touch"
            />
          </label>
          <label class="mcp-field">
            <span class="mcp-field-label">env(每行 KEY=VALUE,可选)</span>
            <textarea
              class="mcp-textarea"
              rows="3"
              v-model="s.envText"
              placeholder="SOME_API_KEY=xxx"
              @input="touch"
            />
          </label>
        </template>

        <!-- http 字段:url / headers -->
        <template v-else>
          <label class="mcp-field">
            <span class="mcp-field-label">url</span>
            <input
              class="mcp-input"
              v-model="s.url"
              placeholder="http://localhost:8000/mcp"
              @input="touch"
            />
          </label>
          <label class="mcp-field">
            <span class="mcp-field-label">headers(每行 KEY: VALUE,可选)</span>
            <textarea
              class="mcp-textarea"
              rows="3"
              v-model="s.headersText"
              placeholder="Authorization: Bearer xxx"
              @input="touch"
            />
          </label>
        </template>
      </section>

      <div v-if="!form.length" class="mcp-empty">
        <p>尚未配置任何 MCP 服务器。</p>
      </div>

      <button type="button" class="btn mcp-add" @click="addServer">+ 新增 MCP 服务器</button>

      <p class="card-restart-note">⚠️ MCP 服务器的增删与连接参数改动需重启进程才生效(mcp_manager 在启动期建连)。</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { getMcpServers, putMcpServers } from '../api'

// 本地编辑形态:args/env/headers 用换行分隔的文本编辑,保存时拆回数组/对象。
interface ServerForm {
  name: string
  type: 'stdio' | 'http'
  command: string
  argsText: string
  envText: string
  url: string
  headersText: string
}

const form = ref<ServerForm[]>([])
// 快照:用于 dirty 比对的原始形态(序列化后比较)。
const snapshotJson = ref('')

const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const justSaved = ref(false)

function emptyForm(): ServerForm {
  return {
    name: '',
    type: 'stdio',
    command: '',
    argsText: '',
    envText: '',
    url: '',
    headersText: '',
  }
}

// env dict → "K=V\n" 文本
function envToText(env: Record<string, string> | null): string {
  if (!env) return ''
  return Object.entries(env).map(([k, v]) => `${k}=${v}`).join('\n')
}
// 输入 → 表单(把数组/对象转成可编辑文本)
function serverToForm(s: { name: string; type: 'stdio' | 'http'; command: string | null; args: string[]; url: string | null; env: Record<string, string> | null; headers: Record<string, string> | null }): ServerForm {
  return {
    name: s.name,
    type: s.type,
    command: s.command ?? '',
    argsText: (s.args ?? []).join('\n'),
    envText: envToText(s.env),
    url: s.url ?? '',
    headersText: s.headers ? Object.entries(s.headers).map(([k, v]) => `${k}: ${v}`).join('\n') : '',
  }
}

// 文本行 → 去空去重的字符串数组
function textToLines(text: string): string[] {
  return text.split('\n').map((l) => l.trim()).filter(Boolean)
}
// "K=V" 文本 → env dict(无 = 的行忽略,空 value 允许)
function textToEnv(text: string): Record<string, string> | null {
  const out: Record<string, string> = {}
  for (const line of textToLines(text)) {
    const eq = line.indexOf('=')
    if (eq <= 0) continue
    const k = line.slice(0, eq).trim()
    const v = line.slice(eq + 1)
    if (!k) continue
    out[k] = v
  }
  return Object.keys(out).length ? out : null
}
// "KEY: VALUE" 文本 → headers dict
function textToHeaders(text: string): Record<string, string> | null {
  const out: Record<string, string> = {}
  for (const line of textToLines(text)) {
    const sep = line.indexOf(':')
    if (sep <= 0) continue
    const k = line.slice(0, sep).trim()
    const v = line.slice(sep + 1).trim()
    if (!k) continue
    out[k] = v
  }
  return Object.keys(out).length ? out : null
}

function formToJson(): string {
  return JSON.stringify(form.value)
}

const anyDirty = computed(() => formToJson() !== snapshotJson.value)

// touch:用户编辑了任意字段就触发响应式更新(v-model 已改 form,这里仅占位保持与 prompts 风格一致)。
function touch() {}

async function loadMcp() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await getMcpServers()
    form.value = res.servers.map(serverToForm)
    snapshotJson.value = formToJson()
    justSaved.value = false
  } catch (e) {
    loadError.value = (e as Error).message || '加载 MCP 配置失败'
  } finally {
    loading.value = false
  }
}

function addServer() {
  form.value.push(emptyForm())
}

function remove(idx: number) {
  form.value.splice(idx, 1)
}

// 切换 type 时清空对方字段,避免残留无效数据。
function onTypeChange(s: ServerForm) {
  if (s.type === 'http') {
    s.command = ''
    s.argsText = ''
    s.envText = ''
  } else {
    s.url = ''
    s.headersText = ''
  }
}

async function save() {
  if (!anyDirty.value || saving.value) return
  loadError.value = ''
  // 前端校验:name 非空且唯一、stdio 必有 command、http 必有 url。
  const seen = new Set<string>()
  for (const s of form.value) {
    const name = s.name.trim()
    if (!name) {
      loadError.value = '存在空名称的 MCP 服务器'
      return
    }
    if (seen.has(name)) {
      loadError.value = `MCP 服务器名称重复: ${name}`
      return
    }
    seen.add(name)
    if (s.type === 'stdio' && !s.command.trim()) {
      loadError.value = `stdio 服务器 ${name} 缺少 command`
      return
    }
    if (s.type === 'http' && !s.url.trim()) {
      loadError.value = `http 服务器 ${name} 缺少 url`
      return
    }
  }

  saving.value = true
  try {
    const items = form.value.map((s) => ({
      name: s.name.trim(),
      type: s.type,
      command: s.type === 'stdio' ? s.command.trim() : null,
      args: s.type === 'stdio' ? textToLines(s.argsText) : [],
      url: s.type === 'http' ? s.url.trim() : null,
      env: s.type === 'stdio' ? textToEnv(s.envText) : null,
      headers: s.type === 'http' ? textToHeaders(s.headersText) : null,
    }))
    const res = await putMcpServers(items)
    // 用后端返回的最新态重建表单 + 快照,dirty 自动复位。
    form.value = res.servers.map(serverToForm)
    snapshotJson.value = formToJson()
    justSaved.value = true
    setTimeout(() => { justSaved.value = false }, 2500)
  } catch (e) {
    loadError.value = (e as Error).message || '保存失败'
  } finally {
    saving.value = false
  }
}

function reset() {
  // 放弃修改:从快照还原表单。
  form.value = JSON.parse(snapshotJson.value) as ServerForm[]
  justSaved.value = false
}

// 暴露给父组件:让 Settings.vue 全局保存条复用,与其他设置页交互一致。
defineExpose({
  dirty: anyDirty,
  saving,
  justSaved,
  save,
  reset,
})

loadMcp()
</script>

<style scoped>
.mcp-content {
  max-width: 820px;
  margin: 0 auto;
  padding-bottom: 80px;
}
.mcp-loading,
.mcp-error {
  display: flex;
  align-items: center;
  gap: var(--sp-md);
  color: var(--body-mid);
  font-size: 14px;
}
.mcp-error { flex-direction: column; align-items: flex-start; gap: var(--sp-md); }
.mcp-empty {
  color: var(--body-mid);
  font-size: 14px;
  margin-bottom: var(--sp-lg);
}

.mcp-card {
  background: var(--canvas-card);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  padding: var(--sp-xl);
  margin-bottom: var(--sp-xl);
}
.mcp-card-head {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  margin-bottom: var(--sp-lg);
  flex-wrap: wrap;
}
.mcp-name {
  flex: 1 1 220px;
  min-width: 0;
  padding: var(--sp-xs) var(--sp-md);
  background: var(--canvas-soft);
  border: 1px solid var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--ink);
  font-size: 14px;
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  outline: none;
  box-sizing: border-box;
  transition: border-color 0.15s;
}
.mcp-name:focus { border-color: var(--pill-border-hover); }
.mcp-card-head select {
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
.mcp-card-head select:focus { border-color: var(--pill-border-hover); }
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

.mcp-field {
  display: block;
  margin-bottom: var(--sp-md);
}
.mcp-field-label {
  display: block;
  font-size: 12px;
  letter-spacing: 0.3px;
  color: var(--body-mid);
  margin-bottom: var(--sp-xs);
  font-family: 'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}
.mcp-input,
.mcp-textarea {
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
  resize: vertical;
}
.mcp-input:focus,
.mcp-textarea:focus { border-color: var(--pill-border-hover); }

.mcp-add {
  padding: var(--sp-sm) var(--sp-lg);
  background: transparent;
  border: 1px dashed var(--canvas-mid);
  border-radius: var(--radius-sm);
  color: var(--body);
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.mcp-add:hover {
  border-color: var(--pill-border-hover);
  color: var(--ink);
}

.card-restart-note {
  margin-top: var(--sp-xl);
  font-size: 12px;
  line-height: 18px;
  color: var(--body-mid);
}
</style>