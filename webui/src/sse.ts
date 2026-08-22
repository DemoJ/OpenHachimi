import { fetchEventSource, EventStreamContentType } from '@microsoft/fetch-event-source'
import { getToken, clearToken, type AttachmentRef, type ArtifactRef } from './api'

export interface SSEPayload {
  text?: string
  type?: string
  temporary?: boolean
  tool_name?: string
  tool_icon?: string
  done?: boolean
  error?: string
  // type === "session" 时携带:后端把空白页直发场景下自动新建的 session_id 在
  // 首事件回传给前端,让 store.currentSessionId 立即同步,避免落到 sessions[0] 兜底。
  // 命令(/new、/role 等)改变会话/角色时也会发 session 事件,携带最新指向。
  session_id?: string
  role?: string
  channel?: string
  auto_created?: boolean
  // type === "artifact" 时携带:agent 生成的产物文件(AgentRef 的 JSON)。
  artifact?: ArtifactRef
  // type === "clarification" 时携带:clarify_user 追问的预设选项,
  // 供渲染可点选项条(点击即发送选项文本)。
  choices?: string[]
}

export interface SSECallbacks {
  onChunk: (text: string, temporary: boolean) => void
  onDone: () => void
  onError: (err: string | Error) => void
  onSession?: (sessionId: string, channel?: string, autoCreated?: boolean, role?: string) => void
  onArtifact?: (artifact: ArtifactRef) => void
  onClarification?: (question: string, choices: string[]) => void
}

/**
 * `@microsoft/fetch-event-source` 的回调默认行为坑很多，必须显式接管：
 *
 * - 默认 `onclose` 实现是 `throw new RetriableError()`，会触发库内部自动重连，
 *   表现为：用户发一条消息，后端却收到 N 次同样的 POST /chat/stream。本函数显式
 *   提供 `onclose`，**不抛错**，而是把它视作"流正常结束"调用 `onDone()`。
 * - 默认 `onopen` 只校验 content-type，4xx 会抛 FatalError，但 401 等鉴权失败
 *   没有用户感知。这里在 401 时清 token 并跳 /login。
 * - 默认 `openWhenHidden: false` 会在 tab 切到后台时主动断流；webui 跑在嵌入
 *   webview / 后台 tab 时会被误杀，这里强制为 true。
 */
class FatalSSEError extends Error {}

export async function chatStream(
  message: string,
  role: string | null,
  callbacks: SSECallbacks,
  signal?: AbortSignal,
  options?: { sessionId?: string | null; channel?: string; attachments?: AttachmentRef[] },
): Promise<void> {
  const token = getToken()
  const startedAt = performance.now()
  let chunkCount = 0
  let doneEmitted = false

  // 内部去重：onDone 只调用一次（既可能来自 server 的 {done:true}，
  // 也可能来自 onclose），避免上层组件状态被反复复位。
  const fireDone = () => {
    if (doneEmitted) return
    doneEmitted = true
    callbacks.onDone()
  }

  const sessionId = options?.sessionId ?? null
  const channel = options?.channel ?? 'webui'
  const attachments = options?.attachments ?? []

  console.info('[SSE] opening stream', { role, channel, sessionId, messageChars: message.length, attachmentCount: attachments.length })

  try {
    await fetchEventSource('/chat/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ message, role, session_id: sessionId, channel, attachments }),
      signal,
      // 防止 tab 切后台时库自动 close 连接（默认 false）
      openWhenHidden: true,

      async onopen(res) {
        const openMs = Math.round(performance.now() - startedAt)
        const contentType = res.headers.get('content-type') || ''
        console.info('[SSE] onopen', { status: res.status, contentType, openMs })

        if (res.status === 401) {
          console.warn('[SSE] unauthorized, redirecting to /login')
          clearToken()
          window.location.hash = '#/login'
          // 抛 FatalSSEError 阻止库内部重连
          throw new FatalSSEError('未授权')
        }

        if (!res.ok) {
          let detail = ''
          try {
            const body = await res.clone().json()
            detail = body?.detail || ''
          } catch {
            /* ignore */
          }
          throw new FatalSSEError(`SSE 打开失败: ${res.status} ${detail}`.trim())
        }

        if (!contentType.includes(EventStreamContentType)) {
          throw new FatalSSEError(`SSE 响应 content-type 异常: ${contentType || '(空)'}`)
        }
      },

      onmessage(event) {
        try {
          const data: SSEPayload = JSON.parse(event.data)
          if (data.done) {
            console.debug('[SSE] server signaled done', { chunkCount })
            fireDone()
            return
          }
          if (data.error) {
            console.warn('[SSE] server reported error', data.error)
            callbacks.onError(data.error)
            return
          }
          if (data.type === 'session' && data.session_id) {
            // 后端首事件:把空白页直发自动新建的 session_id 同步给前端 store;
            // 命令执行后的 session 事件携带变更后的 session/role 指向。
            console.info('[SSE] session bound', { sid: data.session_id, channel: data.channel, autoCreated: data.auto_created, role: data.role })
            callbacks.onSession?.(data.session_id, data.channel, data.auto_created, data.role)
            return
          }
          if (data.type === 'clarification' && data.choices?.length) {
            // clarify_user 追问带预设选项:渲染成可点选项条。
            console.info('[SSE] clarification received', { choices: data.choices })
            callbacks.onClarification?.(data.text || '', data.choices)
            return
          }
          if (data.type === 'artifact' && data.artifact) {
            // agent 生成的产物文件:图片/文档等,前端需渲染缩略图/下载链接。
            console.info('[SSE] artifact received', { id: data.artifact.id, filename: data.artifact.filename })
            callbacks.onArtifact?.(data.artifact)
            return
          }
          if (data.text !== undefined) {
            // notice(步数暂停/手动中断等系统提示)与普通 text 一样并入
            // assistant 气泡正文——它们是面向用户的重要提示。
            chunkCount += 1
            callbacks.onChunk(data.text, !!data.temporary)
          }
        } catch (err) {
          console.warn('[SSE] failed to parse event', err)
        }
      },

      onclose() {
        // 后端 SSE 生成器结束（无异常）时走到这里。
        // 库的默认行为是 `throw new RetriableError()` 触发自动重连——
        // 我们必须显式覆盖，把它当成"流结束"处理，否则会出现重连风暴。
        const totalMs = Math.round(performance.now() - startedAt)
        console.info('[SSE] onclose', { chunkCount, totalMs, doneEmitted })
        fireDone()
      },

      onerror(err) {
        const totalMs = Math.round(performance.now() - startedAt)
        // FatalSSEError（来自 onopen）是我们主动抛的，不打 warn 级别污染日志
        if (err instanceof FatalSSEError) {
          console.info('[SSE] onerror (fatal, no retry)', err.message, { totalMs })
        } else {
          console.warn('[SSE] onerror', err, { totalMs, chunkCount })
        }
        callbacks.onError(err instanceof Error ? err : new Error(String(err)))
        // 抛错阻止库自动重试（默认在 error 路径下也会重连）
        throw err
      },
    })
  } catch (err) {
    // 仅做日志兜底；onerror 已经把错误派发给 callbacks
    if (!(err instanceof FatalSSEError)) {
      console.debug('[SSE] fetchEventSource threw', err)
    }
  } finally {
    // 兜底：如果某条路径漏掉了 onDone（理论上不该发生），这里保证组件状态能复位
    fireDone()
  }
}
