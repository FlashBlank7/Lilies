'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  cancelLocalLiliesAssignment,
  createLocalLiliesAssignment,
  deriveLocalLiliesBusinessContext,
  idempotency,
  localLiliesApplicationAssignments,
  localLiliesAssignment,
  localLiliesAssignmentMessages,
  localLiliesErrorMessage,
  localLiliesStatus,
  openLocalLiliesAssignmentEventStream,
  reconnectLocalLilies,
  resumeLocalLiliesAssignment,
  sendLocalLiliesAssignmentMessage,
  PlatformApiError,
  type LocalLiliesAssignment,
  type LocalLiliesAssignmentEvent,
  type LocalLiliesCapabilityContext,
  type LocalLiliesMessage,
  type LocalLiliesStatus,
} from '@/lib/platform'
import type { Locale } from '@/lib/i18n'
import { LocalLiliesOperationAttempt } from '@/lib/local-lilies-operation-attempt'

type Props = {
  applicationId: string
  requirement: string
  locale: Locale
  requestedAssignmentId: string
  capabilityContext?: LocalLiliesCapabilityContext | null
  onApplicationChanged: () => unknown | Promise<unknown>
}

const LOCAL_LILIES_WORKFLOW_DELIVERABLES = [{
  name: 'Editable workflow application',
  description: 'The workflow, acceptance evidence, and visible delivery status in Lilies.',
  media_type: 'application/vnd.lilies.workflow+json',
  required: true,
}]

const unavailableStatus: LocalLiliesStatus = {
  enabled: false,
  default_route: false,
  connections: [],
}

type LocalLiliesSseMessage = { data: string; lastEventId: string }

function parseLocalLiliesSseFrame(frame: string): { eventType: string; message: LocalLiliesSseMessage } | null {
  let eventType = 'message'
  let lastEventId = ''
  const data: string[] = []
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue
    const separator = line.indexOf(':')
    const field = separator < 0 ? line : line.slice(0, separator)
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '')
    if (field === 'event' && value) eventType = value
    else if (field === 'id') lastEventId = value
    else if (field === 'data') data.push(value)
  }
  if (!data.length) return null
  return { eventType, message: { data: data.join('\n'), lastEventId } }
}

function waitForLocalLiliesReconnect(signal: AbortSignal, milliseconds = 750) {
  return new Promise<void>(resolve => {
    if (signal.aborted) {
      resolve()
      return
    }
    const finish = () => {
      window.clearTimeout(timer)
      signal.removeEventListener('abort', finish)
      resolve()
    }
    const timer = window.setTimeout(finish, milliseconds)
    signal.addEventListener('abort', finish, { once: true })
  })
}

function eventProjection(raw: LocalLiliesSseMessage, fallbackType: string): LocalLiliesAssignmentEvent | null {
  try {
    const payload = JSON.parse(raw.data) as Record<string, unknown>
    const data = payload.data && typeof payload.data === 'object' && !Array.isArray(payload.data)
      ? payload.data as Record<string, unknown>
      : payload
    const seqValue = payload.seq ?? payload.daemon_seq ?? raw.lastEventId
    const seq = typeof seqValue === 'number' ? seqValue : Number(seqValue || 0)
    return {
      event_id: String(payload.event_id || payload.id || raw.lastEventId || `${fallbackType}:${seq}`),
      seq: Number.isFinite(seq) ? seq : 0,
      event_type: String(payload.event_type || payload.type || fallbackType),
      data,
      replayed: payload.replayed === true,
      created_at: typeof payload.created_at === 'string' ? payload.created_at : undefined,
    }
  } catch {
    return null
  }
}

export function LocalLiliesBuildPanel({ applicationId, requirement, locale, requestedAssignmentId, capabilityContext, onApplicationChanged }: Props) {
  const zh = locale === 'zh'
  const [status, setStatus] = useState<LocalLiliesStatus>(unavailableStatus)
  const [assignment, setAssignment] = useState<LocalLiliesAssignment | null>(null)
  const [events, setEvents] = useState<LocalLiliesAssignmentEvent[]>([])
  const [messages, setMessages] = useState<LocalLiliesMessage[]>([])
  const [messageBefore, setMessageBefore] = useState('')
  const [messageHasMore, setMessageHasMore] = useState(false)
  const [operatorMessage, setOperatorMessage] = useState('')
  const [messageBusy, setMessageBusy] = useState(false)
  const [cursor, setCursor] = useState(0)
  const [connectionId, setConnectionId] = useState('')
  const [reconnectCode, setReconnectCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [streamError, setStreamError] = useState('')
  const [streamState, setStreamState] = useState<'idle' | 'connected' | 'reconnecting'>('idle')
  const streamAbortRef = useRef<AbortController | null>(null)
  const applicationChangedRef = useRef(onApplicationChanged)
  const seenEvents = useRef(new Set<string>())
  const replayBoundary = useRef(0)
  const streamedAssignmentId = useRef('')
  const reconnectAttemptRef = useRef(new LocalLiliesOperationAttempt(idempotency))

  useEffect(() => {
    applicationChangedRef.current = onApplicationChanged
  }, [onApplicationChanged])
  useEffect(() => {
    reconnectAttemptRef.current.reset()
  }, [connectionId])

  const loadStatus = useCallback(async () => {
    try {
      const next = await localLiliesStatus()
      setStatus(next)
      setConnectionId(current => current || next.connections.find(item => item.status === 'connected')?.connection_id || next.connections.map(item => item.connection_id).shift() || '')
      return next
    } catch (cause) {
      setStatus(unavailableStatus)
      setError(String(cause))
      return unavailableStatus
    }
  }, [])

  const loadAssignment = useCallback(async (assignmentId: string) => {
    try {
      const next = await localLiliesAssignment(assignmentId)
      if (next.application_id !== applicationId) {
        setError(`assignment_application_mismatch: expected application_id=${applicationId}, actual application_id=${next.application_id}`)
        return null
      }
      setAssignment(next)
      setConnectionId(next.connection_id)
      setError(localLiliesErrorMessage(next.last_error))
      return next
    } catch (cause) {
      // Keep an already-rendered assignment and all correlation IDs visible.
      setError(String(cause))
      await loadStatus()
      return null
    }
  }, [applicationId, loadStatus])

  const loadMessages = useCallback(async (
    assignmentId: string,
    expectedSessionId: string,
    before = '',
  ) => {
    try {
      const page = await localLiliesAssignmentMessages(
        assignmentId,
        { limit: 20, ...(before ? { before } : {}) },
      )
      if (page.session_id !== expectedSessionId) {
        setError(`assignment_session_mismatch: expected session_id=${expectedSessionId}, actual session_id=${page.session_id}`)
        return
      }
      setMessages(current => {
        const combined = before ? [...page.messages, ...current] : page.messages
        const unique = new Map(combined.map(message => [message.message_id, message]))
        return [...unique.values()]
      })
      setMessageBefore(page.next_before || '')
      setMessageHasMore(page.has_more)
    } catch (cause) {
      setError(String(cause))
    }
  }, [])

  const recoverAssignment = useCallback(async () => {
    if (requestedAssignmentId) return loadAssignment(requestedAssignmentId)
    try {
      const items = await localLiliesApplicationAssignments(applicationId)
      const latest = [...items]
        .reverse()
        .find(item => item.application_id === applicationId)
      if (items.length > 0 && !latest) {
        setError(`assignment_application_mismatch: no assignment belongs to application_id=${applicationId}`)
        return null
      }
      if (!latest) return null
      setAssignment(latest)
      setConnectionId(latest.connection_id)
      setError(localLiliesErrorMessage(latest.last_error))
      return latest
    } catch (cause) {
      setError(String(cause))
      return null
    }
  }, [applicationId, loadAssignment, requestedAssignmentId])

  useEffect(() => {
    void loadStatus()
    void recoverAssignment()
  }, [loadStatus, recoverAssignment])

  useEffect(() => {
    if (!assignment) {
      setMessages([])
      setMessageBefore('')
      setMessageHasMore(false)
      return
    }
    void loadMessages(assignment.assignment_id, assignment.session_id)
  }, [assignment?.assignment_id, assignment?.session_id, loadMessages])

  useEffect(() => {
    streamAbortRef.current?.abort()
    const assignmentId = assignment?.assignment_id
    if (!assignmentId) return
    let afterCursor = cursor
    const assignmentChanged = streamedAssignmentId.current !== assignmentId
    if (assignmentChanged) {
      // Daemon SSE ids are session-local integers. A query/recovery switch must
      // reset both the cursor and dedupe set before subscribing to another
      // assignment or legitimate events from the new session would disappear.
      streamedAssignmentId.current = assignmentId
      seenEvents.current.clear()
      setEvents([])
      setCursor(0)
      afterCursor = 0
    }
    replayBoundary.current = assignmentChanged
      ? assignment.relay_cursor || 0
      : Math.max(assignment.relay_cursor || 0, cursor)
    const controller = new AbortController()
    streamAbortRef.current = controller
    let streamCursor = afterCursor
    const receive = (fallbackType: string, raw: LocalLiliesSseMessage) => {
      const projected = eventProjection(raw, fallbackType)
      if (!projected) return 0
      const key = projected.event_id || `${projected.seq}:${projected.event_type}`
      if (seenEvents.current.has(key)) return projected.seq
      seenEvents.current.add(key)
      const normalized = { ...projected, replayed: projected.replayed || projected.seq <= replayBoundary.current }
      setEvents(current => [...current.slice(-199), normalized])
      setCursor(current => Math.max(current, normalized.seq))
      const updatesAssignmentState = ['assignment.', 'session.', 'turn.', 'permission.', 'bridge.']
        .some(prefix => normalized.event_type.startsWith(prefix))
      if (updatesAssignmentState) {
        void loadAssignment(assignmentId).then(next => {
          if (next && ['completed', 'cancelled'].includes(next.phase)) {
            controller.abort()
            setStreamState('idle')
          }
        })
      }
      if (['tool.completed', 'turn.finished', 'assignment.completed'].includes(normalized.event_type)) {
        void Promise.resolve(applicationChangedRef.current()).catch(cause => setError(String(cause)))
      }
      if (['message.created', 'turn.finished', 'assignment.completed'].includes(normalized.event_type)) {
        void loadMessages(assignmentId, assignment.session_id)
      }
      if (['assignment.completed', 'assignment.cancelled'].includes(normalized.event_type)) {
        controller.abort()
        setStreamState('idle')
      }
      return normalized.seq
    }
    const consumeFrame = (frame: string) => {
      const parsed = parseLocalLiliesSseFrame(frame)
      if (!parsed) return
      streamCursor = Math.max(streamCursor, receive(parsed.eventType, parsed.message))
    }
    const runStream = async () => {
      while (!controller.signal.aborted) {
        try {
          const response = await openLocalLiliesAssignmentEventStream(assignmentId, streamCursor, controller.signal)
          if (!response.body) throw new Error('Local Lilies event stream returned no readable body')
          setStreamState('connected')
          setStreamError('')
          const reader = response.body.getReader()
          const decoder = new TextDecoder()
          let buffer = ''
          while (!controller.signal.aborted) {
            const { done, value } = await reader.read()
            if (done) break
            buffer += decoder.decode(value, { stream: true })
            let boundary = /\r?\n\r?\n/.exec(buffer)
            while (boundary) {
              consumeFrame(buffer.slice(0, boundary.index))
              buffer = buffer.slice(boundary.index + boundary[0].length)
              boundary = /\r?\n\r?\n/.exec(buffer)
            }
          }
          buffer += decoder.decode()
          if (buffer.trim()) consumeFrame(buffer)
          if (controller.signal.aborted) return
          throw new Error('Local Lilies event stream ended; reconnecting from Last-Event-ID')
        } catch (cause) {
          if (controller.signal.aborted) return
          setStreamState('reconnecting')
          setStreamError(`${zh ? '事件流暂时断开；保留当前 ID 和 cursor，正在等待重连。' : 'Event stream disconnected; IDs and cursor are preserved while reconnecting.'} ${String(cause)}`)
          void loadStatus()
          await waitForLocalLiliesReconnect(controller.signal)
        }
      }
    }
    void runStream()
    return () => {
      controller.abort()
      if (streamAbortRef.current === controller) streamAbortRef.current = null
    }
  // cursor is intentionally excluded: streamCursor tracks the latest SSE id
  // within this cancellable fetch loop and is sent as Last-Event-ID on retry.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assignment?.assignment_id, assignment?.session_id, loadAssignment, loadMessages, loadStatus, zh])

  async function startLocalAssignment() {
    if (!connectionId || !status.enabled) return
    setBusy(true)
    setError('')
    try {
      const next = await createLocalLiliesAssignment(applicationId, {
        idempotency_key: idempotency(),
        connection_id: connectionId,
        requirement,
        business_context: deriveLocalLiliesBusinessContext(requirement, capabilityContext, LOCAL_LILIES_WORKFLOW_DELIVERABLES),
        deliverables: LOCAL_LILIES_WORKFLOW_DELIVERABLES,
      })
      seenEvents.current.clear()
      setEvents([])
      setAssignment(next)
      setCursor(0)
      setError(localLiliesErrorMessage(next.last_error))
      const query = new URLSearchParams(window.location.search)
      query.set('assignment', next.assignment_id)
      window.history.replaceState(null, '', `${window.location.pathname}?${query.toString()}`)
    } catch (cause) {
      if (cause instanceof PlatformApiError && cause.assignment_id) {
        const errorApplicationId = cause.application_id || applicationId
        if (errorApplicationId !== applicationId) {
          setError(`assignment_application_mismatch: expected application_id=${applicationId}, actual application_id=${errorApplicationId}`)
          return
        }
        const query = new URLSearchParams(window.location.search)
        query.set('assignment', cause.assignment_id)
        window.history.replaceState(null, '', `${window.location.pathname}?${query.toString()}`)
        const recovered = await loadAssignment(cause.assignment_id)
        if (!recovered) setError(String(cause))
        return
      }
      setError(String(cause))
    } finally {
      setBusy(false)
    }
  }

  async function cancel() {
    if (!assignment) return
    setBusy(true)
    setError('')
    try {
      const next = await cancelLocalLiliesAssignment(assignment.assignment_id)
      streamAbortRef.current?.abort()
      setStreamState('idle')
      setAssignment(next)
      setError(localLiliesErrorMessage(next.last_error))
    } catch (cause) {
      setError(String(cause))
      // Cancellation intent is persisted before daemon cleanup.  Reload the
      // assignment even when the daemon returned 503 so the durable desired
      // state and correlation IDs immediately replace the stale running view.
      await loadAssignment(assignment.assignment_id)
      await loadStatus()
    } finally {
      setBusy(false)
    }
  }

  async function resume() {
    if (!assignment) return
    setBusy(true)
    setError('')
    try {
      const next = await resumeLocalLiliesAssignment(assignment.assignment_id)
      if (next.application_id !== applicationId) {
        setError(`assignment_application_mismatch: expected application_id=${applicationId}, actual application_id=${next.application_id}`)
        return
      }
      setAssignment(next)
      setError(localLiliesErrorMessage(next.last_error))
      await loadStatus()
    } catch (cause) {
      setError(String(cause))
      await loadStatus()
    } finally {
      setBusy(false)
    }
  }

  async function reconnect() {
    if (!connectionId || !reconnectCode.trim()) return
    setBusy(true)
    setError('')
    try {
      const next = await reconnectLocalLilies(
        connectionId,
        reconnectCode.trim(),
        reconnectAttemptRef.current.current(),
      )
      reconnectAttemptRef.current.reset()
      setStatus(next)
      setReconnectCode('')
      const assignmentId = assignment?.assignment_id || requestedAssignmentId
      if (assignmentId) await loadAssignment(assignmentId)
    } catch (cause) {
      setError(String(cause))
    } finally {
      setBusy(false)
    }
  }

  async function sendOperatorMessage() {
    if (!assignment || !operatorMessage.trim()) return
    setMessageBusy(true)
    setError('')
    try {
      await sendLocalLiliesAssignmentMessage(
        assignment.assignment_id,
        operatorMessage.trim(),
      )
      setOperatorMessage('')
      await loadMessages(assignment.assignment_id, assignment.session_id)
      await loadAssignment(assignment.assignment_id)
    } catch (cause) {
      setError(String(cause))
    } finally {
      setMessageBusy(false)
    }
  }

  const connection = status.connections.find(item => item.connection_id === connectionId) || null
  const terminal = Boolean(assignment && ['completed', 'cancelled'].includes(assignment.phase))
  const hasNonterminalAssignment = Boolean(assignment && !terminal)
  const resumable = Boolean(assignment && [assignment.phase, assignment.status, assignment.daemon_status]
    .filter(Boolean)
    .some(value => ['interrupted', 'error', 'unavailable'].includes(String(value).toLowerCase())))
  const visibleError = error
    || localLiliesErrorMessage(assignment?.last_error)
    || localLiliesErrorMessage(connection?.last_error)
  const canSendOperatorMessage = Boolean(
    assignment
    && !terminal
    && ['ready', 'error', 'interrupted', 'waiting_permission', 'waiting_collaboration']
      .includes(String(assignment.daemon_status || '').toLowerCase()),
  )

  return <section className="local-lilies-build" data-local-lilies-build="explicit" data-assignment-status={assignment?.status || 'none'}>
    <header>
      <div><span>{zh ? '显式本地路线' : 'Explicit local route'}</span><h3>Local Lilies</h3></div>
      <b className={connection?.status === 'connected' ? 'connected' : ''}>{connection?.status || (status.enabled ? 'unpaired' : 'disabled')}</b>
    </header>
    <p>{zh ? '本路线把当前应用和完整需求交给本地 Lilies；启动动作不会先改草稿，也不会在 daemon 失败时调用旧 Builder。' : 'This route assigns the current application and complete requirement to local Lilies. The launch action never prebuilds the draft or invokes the legacy Builder after a daemon failure.'}</p>
    <div className="local-lilies-route-controls">
      <label><span>Connection</span><select value={connectionId} disabled={busy || !status.enabled} onChange={event => { reconnectAttemptRef.current.reset(); setConnectionId(event.target.value) }}><option value="">{zh ? '选择已配对 daemon' : 'Choose a paired daemon'}</option>{status.connections.map(item => <option value={item.connection_id} key={item.connection_id}>{item.connection_id} · {item.status}</option>)}</select></label>
      <button type="button" disabled={busy || hasNonterminalAssignment || !status.enabled || connection?.status !== 'connected' || !requirement.trim()} onClick={() => void startLocalAssignment()}>{busy ? (zh ? '处理中…' : 'Working…') : (zh ? '启动新的 Local Lilies assignment' : 'Start a new Local Lilies assignment')}</button>
    </div>
    <div className="local-lilies-identifiers" data-correlation-ids="application,build,assignment,session">
      <div><span>Application ID</span><code>{assignment?.application_id || applicationId}</code></div>
      <div><span>Build ID</span><code>{assignment?.build_id || '—'}</code></div>
      <div><span>Assignment ID</span><code>{assignment?.assignment_id || requestedAssignmentId || '—'}</code></div>
      <div><span>Session ID</span><code>{assignment?.session_id || '—'}</code></div>
    </div>
    {assignment && <div className="local-lilies-assignment-state">
      <span><b>{assignment.status}</b> · {assignment.phase} · daemon {assignment.daemon_status || 'unknown'}</span>
      <span>relay cursor {cursor} · persisted {assignment.relay_cursor} · ack {assignment.ack_cursor} · stream {streamState}</span>
      <button type="button" className="ghost" disabled={busy} onClick={() => void loadAssignment(assignment.assignment_id)}>{zh ? '刷新 assignment' : 'Refresh assignment'}</button>
      {resumable && <button type="button" disabled={busy || connection?.status !== 'connected'} onClick={() => void resume()}>{zh ? '显式恢复 assignment' : 'Resume assignment explicitly'}</button>}
      {!terminal && <button type="button" className="danger" disabled={busy} onClick={() => void cancel()}>{zh ? '取消' : 'Cancel'}</button>}
    </div>}
    {connection && connection.status !== 'connected' && <div className="local-lilies-reconnect">
      <label><span>{zh ? '新的 daemon 一次性配对码' : 'New daemon one-time pairing code'}</span><input autoComplete="off" value={reconnectCode} onChange={event => setReconnectCode(event.target.value)} /></label>
      <button type="button" disabled={busy || !reconnectCode.trim()} onClick={() => void reconnect()}>{zh ? '显式重连' : 'Reconnect explicitly'}</button>
    </div>}
    {visibleError && <p className="error-banner" role="alert">{visibleError}</p>}
    {streamError && <p className="error-banner" role="status">{streamError}</p>}
    <div className="local-lilies-transcript" data-session-transcript={assignment?.session_id || 'none'}>
      <div>
        <strong>{zh ? '任务会话' : 'Task conversation'}</strong>
        <span>{messages.length}</span>
      </div>
      {messageHasMore && assignment && <button type="button" className="ghost" disabled={messageBusy || !messageBefore} onClick={() => void loadMessages(assignment.assignment_id, assignment.session_id, messageBefore)}>{zh ? '加载更早消息' : 'Load older messages'}</button>}
      <div className="local-lilies-message-list" role="log" aria-live="polite">
        {messages.length ? messages.map(message => <article key={message.message_id} data-message-role={message.role}>
          <header><b>{message.role}</b><time dateTime={message.created_at}>{new Date(message.created_at).toLocaleString()}</time></header>
          {message.content.map((block, index) => {
            if (block.type === 'text') return <p key={index}>{block.text}</p>
            if (block.type === 'tool_use') return <code key={index}>{zh ? '调用积木/工具：' : 'Tool: '}{block.name}</code>
            return <code key={index} data-tool-error={block.is_error ? 'true' : 'false'}>{block.is_error ? (zh ? '工具返回错误' : 'Tool returned an error') : (zh ? '工具调用完成' : 'Tool completed')}</code>
          })}
          {message.content_truncated && <small>{zh ? '该条消息已按公开会话上限截断。' : 'This message was truncated at the public transcript limit.'}</small>}
        </article>) : <p>{zh ? '尚无可见消息。私有思维链不会进入这里。' : 'No visible messages yet. Private chain-of-thought is never projected here.'}</p>}
      </div>
      <form onSubmit={event => { event.preventDefault(); void sendOperatorMessage() }}>
        <label><span>{zh ? '向当前 Lilies 会话追加指令' : 'Send a follow-up to this Lilies session'}</span><textarea value={operatorMessage} disabled={!canSendOperatorMessage || messageBusy} maxLength={100000} onChange={event => setOperatorMessage(event.target.value)} placeholder={canSendOperatorMessage ? (zh ? '补充目标、询问当前判断，或要求先整体检查…' : 'Clarify the goal, ask for the current rationale, or request an overall review…') : (zh ? '当前状态只支持查看；等待会话可接收消息。' : 'This state is read-only; wait until the session can accept a message.')} /></label>
        <button type="submit" disabled={!canSendOperatorMessage || messageBusy || !operatorMessage.trim()}>{messageBusy ? (zh ? '发送中…' : 'Sending…') : (zh ? '发送到同一会话' : 'Send to same session')}</button>
      </form>
    </div>
    <div className="local-lilies-event-log" data-event-dedupe="event_id" data-event-cursor={cursor} role="log" aria-live="polite" aria-relevant="additions">
      <div><strong>{zh ? 'Live / replayed events' : 'Live / replayed events'}</strong><span>{events.length}</span></div>
      {events.length ? events.map(event => <article key={event.event_id} data-replayed={event.replayed ? 'true' : 'false'}><span>#{event.seq} · {event.event_type}{event.replayed ? ` · ${zh ? '重放' : 'replayed'}` : ''}</span><pre>{JSON.stringify(event.data, null, 2)}</pre></article>) : <p>{zh ? '尚无事件；重连后从持久 cursor 继续。' : 'No events yet; reconnects resume from the persisted cursor.'}</p>}
    </div>
  </section>
}
