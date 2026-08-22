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

// 带 status 的错误:调用方可区分 401(令牌无效)/网络异常/服务端 5xx,
// 给出不同的用户提示,而不是一句笼统的"获取初始化数据失败"。
export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    ...(options?.headers as Record<string, string> || {}),
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  let res: Response
  try {
    res = await fetch(path, { ...options, headers })
  } catch {
    // fetch 网络层失败(服务未启动/断网)抛 TypeError,不带 status
    throw new ApiError('无法连接服务器', 0)
  }
  if (res.status === 401) {
    clearToken()
    window.location.hash = '#/login'
    throw new ApiError('未授权', 401)
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new ApiError(body.detail || `请求失败: ${res.status}`, res.status)
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

// 附件引用:用户上传的文件经 /attachments/upload 落盘后返回。
// kind 由 content_type 派生:image/document/audio/video/unknown。
export interface AttachmentRef {
  id: string
  filename: string | null
  content_type: string | null
  size_bytes: number | null
  local_path: string
  source: 'telegram' | 'weixin' | 'http' | 'local'
  kind: 'image' | 'document' | 'audio' | 'video' | 'unknown'
  metadata: Record<string, unknown>
}

// 产物引用:agent 生成的文件(图片/文档等),通过 SSE artifact 事件下发。
// download_url 为 /artifacts/{id}/download,前端需拼接 token query 参数。
export interface ArtifactRef {
  id: string
  filename: string
  content_type: string | null
  size_bytes: number
  local_path: string
  download_url: string | null
  title: string | null
  description: string | null
  metadata: Record<string, unknown>
}

export interface MessageItem {
  role: 'user' | 'assistant'
  content: string
  prefix?: string                       // 仅 user 消息：运行时注入的上下文前缀，可折叠
  timestamp: string | null              // ISO-8601；user=收到时间，assistant=模型回复时间
  // 仅 assistant：本轮请求的 token 用量；旧会话 / 流式中尚未拿到 usage 时为 null
  tokens?: { input: number; output: number; total: number; cache_read?: number } | null
  // 压缩标记条：非空时本条是「压缩标记」而非真实消息，仅提示该段对话
  // 已压缩为摘要提供给 AI；原始消息始终完整返回并正常渲染。
  fold?: {
    compression_id: number
    compressed_count: number
    summary_excerpt: string
    head_end_turn: number
    tail_start_turn: number
  } | null
  // 用户消息携带的附件列表。仅 user 消息有值;无附件时为 null。
  attachments?: AttachmentRef[] | null
  // assistant 消息携带的产物列表(agent 生成的文件)。流式期间通过 SSE artifact 事件收集;
  // 历史回放时由后端从消息 metadata 反序列化注入。
  artifacts?: ArtifactRef[] | null
  // assistant 消息本轮执行过的工具调用摘要(已脱敏展示文本)。历史回放时由后端
  // 从 ToolCallPart 渲染注入;流式期间为空(过程由活动条实时展示)。
  tool_calls?: string[] | null
}

export interface SessionMessagesResponse {
  role: string
  session_id: string
  messages: MessageItem[]
  total?: number
  has_more?: boolean
  next_before_turn?: number | null
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

export function getSessionMessages(session_id: string, role?: string, opts?: { limit?: number; before_turn?: number }) {
  const params: string[] = []
  if (role) params.push(`role=${encodeURIComponent(role)}`)
  if (opts?.limit !== undefined) params.push(`limit=${opts.limit}`)
  if (opts?.before_turn !== undefined) params.push(`before_turn=${opts.before_turn}`)
  const q = params.length ? `?${params.join('&')}` : ''
  return get<SessionMessagesResponse>(`/sessions/${encodeURIComponent(session_id)}/messages${q}`)
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
  // select 选项的显示标签映射(如 {blacklist: "黑名单模式"}),无映射时显示原始值。
  option_labels?: Record<string, string>
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

// ---------------------------------------------------------------- 附件上传/下载
// WebUI 文件收发:/attachments/upload 接收 multipart 文件,返回 AttachmentRef;
// /attachments/download?path=... 按 local_path 下载附件。
// 用 XHR 而非 fetch:fetch 拿不到上传进度回调,大文件只能干等。
export function uploadAttachment(
  file: File,
  onProgress?: (percent: number) => void,
): Promise<AttachmentRef> {
  const token = getToken()
  const form = new FormData()
  form.append('file', file)
  return new Promise<AttachmentRef>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/attachments/upload')
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 100))
      }
    })
    xhr.addEventListener('load', () => {
      if (xhr.status === 401) {
        clearToken()
        window.location.hash = '#/login'
        reject(new ApiError('未授权', 401))
        return
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        let detail = `上传失败: ${xhr.status}`
        try {
          detail = JSON.parse(xhr.responseText)?.detail || detail
        } catch { /* ignore */ }
        reject(new ApiError(detail, xhr.status))
        return
      }
      try {
        resolve(JSON.parse(xhr.responseText) as AttachmentRef)
      } catch (err) {
        reject(new ApiError('上传响应解析失败', xhr.status))
      }
    })
    xhr.addEventListener('error', () => reject(new ApiError('网络错误，上传失败', 0)))
    xhr.send(form)
  })
}

export function attachmentDownloadUrl(localPath: string): string {
  const token = getToken()
  const base = `/attachments/download?path=${encodeURIComponent(localPath)}`
  // <img>/<a> 标签无法设 Authorization header,通过 query 参数传 token。
  // 后端中间件对 GET 下载端点支持 ?token=... 鉴权。
  return token ? `${base}&token=${encodeURIComponent(token)}` : base
}

// 产物下载 URL:artifact.download_url 为 /artifacts/{id}/download,
// 拼接 token query 参数供 <img>/<a> 标签直接访问。
export function artifactDownloadUrl(downloadUrl: string): string {
  const token = getToken()
  if (!token) return downloadUrl
  const sep = downloadUrl.includes('?') ? '&' : '?'
  return `${downloadUrl}${sep}token=${encodeURIComponent(token)}`
}

// 判断产物是否为图片(用于决定渲染缩略图还是文件链接)。
export function isArtifactImage(artifact: ArtifactRef): boolean {
  return (artifact.content_type || '').startsWith('image/')
}

// ---------------------------------------------------------------- 定时任务管理
// 与后端 /schedules CRUD 对齐,供"任务"页使用。
export interface ScheduleTask {
  id: string
  name: string
  prompt: string
  schedule_type: 'once' | 'interval' | 'cron'
  schedule_expr: string
  status: string                      // enabled / paused / deleted
  next_run_at: string | null
  last_run_at: string | null
  last_status: string | null
  last_error: string | null
  role: string | null
  session_id: string | null
  delivery_mode: string | null
  running?: boolean
}

export interface ScheduleCreateRequest {
  name: string
  prompt: string
  schedule_type: 'once' | 'interval' | 'cron'
  schedule_expr: string
  timezone?: string | null
  role?: string | null
  delivery_mode?: string
  paused?: boolean
}

export function listSchedules(includeDeleted = false) {
  return get<ScheduleTask[]>(`/schedules?include_deleted=${includeDeleted}`)
}

export function createSchedule(payload: ScheduleCreateRequest) {
  return post<ScheduleTask>('/schedules', payload)
}

export function pauseSchedule(id: string) {
  return post<ScheduleTask>(`/schedules/${encodeURIComponent(id)}/pause`)
}

export function resumeSchedule(id: string) {
  return post<ScheduleTask>(`/schedules/${encodeURIComponent(id)}/resume`)
}

export function removeSchedule(id: string) {
  return request<{ ok?: boolean; message?: string }>(`/schedules/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })
}

// 被压缩折叠段的原始消息:FoldCard 点击展开时按需拉取。
export function getFoldedMessages(sessionId: string, compressionId: number) {
  return get<SessionMessagesResponse>(
    `/sessions/${encodeURIComponent(sessionId)}/messages/folded/${compressionId}`,
  )
}