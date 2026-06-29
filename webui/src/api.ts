const TOKEN_KEY = 'openhachimi_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    ...(options?.headers as Record<string, string> || {}),
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  const res = await fetch(path, { ...options, headers })
  if (res.status === 401) {
    clearToken()
    window.location.hash = '#/login'
    throw new Error('未授权')
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `请求失败: ${res.status}`)
  }
  return res.json()
}

export function get<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'GET' })
}

export function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
}

export function patch<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
}

export function put<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
}

// ---------------------------------------------------------------- 首页
export interface SessionSummary {
  session_id: string
  role: string
  created_at: string | null
  mtime: number
  preview: string
  message_count: number
  channel: string
}

export interface SessionListResponse {
  role: string
  sessions: SessionSummary[]
  // 分页元信息(后端 2026-06 引入)。老服务端不返回这几个字段时,反序列化默认 0。
  total: number
  limit: number | null
  offset: number
}

export interface ChannelListResponse {
  channels: string[]
  default: string
}

export interface StateResponse {
  model: string
  base_url: string | null
  mcp_servers: number
  mcp_errors: string[]
}

export interface RolesResponse {
  roles: string[]
  current_role: string
}

export interface CommandResponse {
  message: string
  role: string
  session_id: string
}

export interface MessageItem {
  role: 'user' | 'assistant'
  content: string
  prefix?: string                       // 仅 user 消息：运行时注入的上下文前缀，可折叠
  timestamp: string | null              // ISO-8601；user=收到时间，assistant=模型回复时间
  // 仅 assistant：本轮请求的 token 用量；旧会话 / 流式中尚未拿到 usage 时为 null
  tokens?: { input: number; output: number; total: number; cache_read?: number } | null
  // 折叠占位条：非空时本条是「折叠条」而非真实消息。点击展开调
  // fetchFoldedMessages 取回被压缩的原始消息。
  fold?: {
    compression_id: number
    dropped_count: number
    summary_excerpt: string
    head_end_turn: number
    tail_start_turn: number
  } | null
}

export interface SessionMessagesResponse {
  role: string
  session_id: string
  messages: MessageItem[]
}

// ---------------------------------------------------------------- 会话
export function fetchState() {
  return get<StateResponse>('/state')
}

export function fetchRoles() {
  return get<RolesResponse>('/roles')
}

export function listSessions(
  role?: string,
  channel?: string,
  opts?: { limit?: number; offset?: number },
) {
  const params: string[] = []
  if (role) params.push(`role=${encodeURIComponent(role)}`)
  if (channel) params.push(`channel=${encodeURIComponent(channel)}`)
  if (opts?.limit !== undefined) params.push(`limit=${opts.limit}`)
  if (opts?.offset !== undefined) params.push(`offset=${opts.offset}`)
  const q = params.length ? `?${params.join('&')}` : ''
  return get<SessionListResponse>(`/sessions${q}`)
}

export function fetchChannels() {
  return get<ChannelListResponse>('/channels')
}

export function loadSession(role: string | null, session_id: string) {
  return post<CommandResponse>('/sessions/load', { role, session_id })
}

export function getSessionMessages(session_id: string, role?: string) {
  const q = role ? `?role=${encodeURIComponent(role)}` : ''
  return get<SessionMessagesResponse>(`/sessions/${encodeURIComponent(session_id)}/messages${q}`)
}

// 展开折叠占位条：取回某次压缩被折叠的原始消息（供前端内联渲染）。
export function fetchFoldedMessages(session_id: string, compression_id: number, role?: string) {
  const params: string[] = []
  if (role) params.push(`role=${encodeURIComponent(role)}`)
  const q = params.length ? `?${params.join('&')}` : ''
  return get<SessionMessagesResponse>(
    `/sessions/${encodeURIComponent(session_id)}/messages/folded/${compression_id}${q}`,
  )
}

export function newSession(role?: string) {
  const q = role ? `?role=${encodeURIComponent(role)}` : ''
  return post<CommandResponse>(`/new${q}`)
}

export function switchRole(role: string) {
  return post<CommandResponse>('/role', { role })
}

export function deleteSession(session_id: string, role?: string) {
  const q = role ? `?role=${encodeURIComponent(role)}` : ''
  return request<CommandResponse>(`/sessions/${encodeURIComponent(session_id)}${q}`, {
    method: 'DELETE',
  })
}

export function stop(session_id: string) {
  return post<CommandResponse>('/stop', { session_id })
}

// ---------------------------------------------------------------- 配置(设置页)
// 字段定义与后端 config.py 的 SETTINGS_FIELD_GROUPS 对齐。
export type ConfigFieldKind = 'secret' | 'string' | 'select' | 'bool' | 'int' | 'float' | 'multi'

export interface ConfigField {
  path: string
  kind: ConfigFieldKind
  group: string
  label: string
  description: string
  options?: string[]
  // editable=true 的 select 渲染为可选预设、可填任意值的输入(如浏览器通道允许填绝对路径)。
  editable?: boolean
}

export type ConfigValue = string | number | boolean | string[]

export interface ConfigGroupResponse {
  group: string
  fields: ConfigField[]
  values: Record<string, ConfigValue>
  masked: string[]
}

export interface ConfigUpdateResult {
  group: string
  values: Record<string, ConfigValue>
  masked: string[]
  written: string[]
  skipped: string[]
}

export function getConfigGroup(group: string) {
  return get<ConfigGroupResponse>(`/config/${encodeURIComponent(group)}`)
}

export function updateConfigGroup(group: string, updates: Record<string, ConfigValue>) {
  return patch<ConfigUpdateResult>(`/config/${encodeURIComponent(group)}`, { updates })
}

// ---------------------------------------------------------------- 提示词编辑(设置页)
// 与后端 /prompts 对齐;数据形态为整文件多行文本,不走 ConfigField 字段表。
export interface PromptSpec {
  name: string
  title: string
  description: string
  has_template_vars: boolean
  restart_note: string
  content: string            // 当前生效值(覆盖优先,回退内置),textarea 直接显示
  is_overridden: boolean     // 是否已有用户覆盖文件
}

export interface PromptsResponse {
  prompts: PromptSpec[]
}

export interface PromptUpdateResult {
  name: string
  content: string
  is_overridden: boolean
}

export function getPrompts() {
  return get<PromptsResponse>('/prompts')
}

export function updatePrompt(name: string, content: string) {
  return patch<PromptUpdateResult>('/prompts', { name, content })
}

// ---------------------------------------------------------------- Skills 配置(设置页)
// 数据形态:扫到的技能清单 + 每项开关(disable-model-invocation),写回各 SKILL.md,
// 不走 yaml 字段表。同 /prompts 属"特殊设置分组"。
export interface SkillItem {
  name: string
  description: string
  source_path: string          // SKILL.md 绝对路径,前端唯一 key 与回写标识
  source_dir_key: string       // 所属 skills_dir 标识("user" 或外部目录名)
  disabled: boolean            // 即 SKILL.md frontmatter 的 disable-model-invocation
  category: string | null
}

export interface SkillsResponse {
  skills: SkillItem[]
}

export interface SkillToggleResult {
  source_path: string
  disabled: boolean
}

export function getSkills() {
  return get<SkillsResponse>('/skills')
}

export function toggleSkill(source_path: string, disabled: boolean) {
  return patch<SkillToggleResult>('/skills/toggle', { source_path, disabled })
}

export interface SkillInstallResult {
  message: string
}

export function installSkill(source_path_or_url: string, allow_http = false) {
  return post<SkillInstallResult>('/skills/install', { source_path_or_url, allow_http })
}

export interface SkillDeleteResult {
  source_path: string
  message: string
}

export function deleteSkill(source_path: string) {
  return request<SkillDeleteResult>('/skills', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_path }),
  })
}

// ---------------------------------------------------------------- MCP 配置(设置页)
// 数据形态:user/mcp-servers.json 的动态服务器清单,type=stdio/http 字段不同,
// 整体覆盖写。同 /prompts 属"特殊设置分组"。
export interface MCPServerItem {
  name: string
  type: 'stdio' | 'http'
  command: string | null
  args: string[]
  url: string | null
  env: Record<string, string> | null
  headers: Record<string, string> | null
}

export interface McpServersResponse {
  servers: MCPServerItem[]
}

export function getMcpServers() {
  return get<McpServersResponse>('/mcp')
}

export function putMcpServers(servers: MCPServerItem[]) {
  return put<McpServersResponse>('/mcp', { servers })
}

// ---------------------------------------------------------------- 角色管理(设置页)
// 数据形态:角色提示词(user/roles/*.md)+ 角色级 skills/MCP 绑定(user/roles-config.json)
// 合并返回。整覆盖写时后端同步维护角色 .md 文件(增删改)与 roles-config.json。
export interface RoleOption {
  name: string
  description: string
}

export interface RoleBindingItem {
  name: string
  prompt: string
  skills_mode: 'all' | 'selected'
  selected_skills: string[]
  mcp_mode: 'all' | 'selected'
  selected_mcp_servers: string[]
}

export interface RolesConfigResponse {
  roles: RoleBindingItem[]
  available_skills: RoleOption[]
  available_mcp_servers: RoleOption[]
  default_role: string
}

export function getRolesConfig() {
  return get<RolesConfigResponse>('/roles-config')
}

export function putRolesConfig(roles: RoleBindingItem[]) {
  return put<RolesConfigResponse>('/roles-config', { roles })
}

// ---------------------------------------------------------------- 记忆管理(设置页)
// 数据形态:长期记忆库(SQLite)L1/L2/L3 的列表与增删改。后端 GET /memory 一次返回
// 列表 + 角色清单 + 库统计;编辑仅 L1;删除为软删除。属"特殊设置分组",不走 yaml 字段表。
export interface MemoryItem {
  id: string
  level: 'L1' | 'L2' | 'L3'
  content: string
  memory_type: string
  confidence: number
  updated_at: string
  score: number
  editable: boolean           // L1→true,L2/L3→false(后端按 level 派生)
  metadata: Record<string, unknown>
}

export interface MemoryListResponse {
  items: MemoryItem[]
  total: number               // 本页条数(后端无 offset 分页,非全量总数)
  role: string                // 回显查询 role("__all__" 或具体角色名)
  roles: string[]             // 可选角色清单,筛选下拉用
  default_role: string
  enabled: boolean            // config.memory.enabled
  stats: Record<string, number>
}

export interface MemoryUpdateResult {
  updated: boolean
  id: string
  embedding_status: string
}

export interface MemoryDeleteResult {
  deleted: number
  ids: string[]
}

export function getMemories(opts?: {
  role?: string
  q?: string
  memory_type?: string
  level?: string
  limit?: number
  include_archived?: boolean
}) {
  const params: string[] = []
  if (opts?.role) params.push(`role=${encodeURIComponent(opts.role)}`)
  if (opts?.q) params.push(`q=${encodeURIComponent(opts.q)}`)
  if (opts?.memory_type) params.push(`memory_type=${encodeURIComponent(opts.memory_type)}`)
  if (opts?.level) params.push(`level=${encodeURIComponent(opts.level)}`)
  if (opts?.limit !== undefined) params.push(`limit=${opts.limit}`)
  if (opts?.include_archived) params.push(`include_archived=true`)
  const q = params.length ? `?${params.join('&')}` : ''
  return get<MemoryListResponse>(`/memory${q}`)
}

export function updateMemory(id: string, content: string) {
  return patch<MemoryUpdateResult>(`/memory/${encodeURIComponent(id)}`, { content })
}

export function deleteMemories(ids: string[]) {
  // DELETE 带 body:与 deleteSkill 同款写法(request + 手动 JSON.stringify)。
  return request<MemoryDeleteResult>('/memory', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  })
}