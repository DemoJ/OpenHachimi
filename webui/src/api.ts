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

export function newSession(role?: string) {
  const q = role ? `?role=${encodeURIComponent(role)}` : ''
  return post<CommandResponse>(`/new${q}`)
}

export function switchRole(role: string) {
  return post<CommandResponse>('/role', { role })
}

// ---------------------------------------------------------------- 配置(设置页)
// 字段定义与后端 config.py 的 SETTINGS_FIELD_GROUPS 对齐。
export type ConfigFieldKind = 'secret' | 'string' | 'select' | 'bool' | 'int'

export interface ConfigField {
  path: string
  kind: ConfigFieldKind
  group: string
  label: string
  description: string
  options?: string[]
}

export interface ConfigGroupResponse {
  group: string
  fields: ConfigField[]
  values: Record<string, string | number | boolean>
  masked: string[]
}

export interface ConfigUpdateResult {
  group: string
  values: Record<string, string | number | boolean>
  masked: string[]
  written: string[]
  skipped: string[]
}

export function getConfigGroup(group: string) {
  return get<ConfigGroupResponse>(`/config/${encodeURIComponent(group)}`)
}

export function updateConfigGroup(group: string, updates: Record<string, string | number | boolean>) {
  return patch<ConfigUpdateResult>(`/config/${encodeURIComponent(group)}`, { updates })
}