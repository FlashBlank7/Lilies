'use client'

import Link from 'next/link'
import {
  AlertTriangle,
  ArrowLeft,
  Bot,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleStop,
  Clock3,
  Code2,
  FileCheck2,
  KeyRound,
  LoaderCircle,
  MessagesSquare,
  Play,
  RefreshCw,
  RotateCcw,
  Send,
  Server,
  ShieldAlert,
  ShieldCheck,
  TestTube2,
  UserRound,
  Workflow,
  X,
  XCircle,
} from 'lucide-react'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import {
  cancelLocalLiliesAssignment,
  closeStudioCollaborationChannel,
  decideStudioCollaborationReport,
  idempotency,
  isAuthError,
  listStudioCollaborationChannels,
  openStudioCollaborationEventStream,
  resolveLocalLiliesAssignmentPermission,
  saveClientToken,
  studioCollaborationChannel,
  studioCollaborationExport,
  updateStudioCollaborationSettings,
  type CollaborationApprovalMode,
  type CollaborationChannel,
  type CollaborationChannelDetail,
  type CollaborationEvidenceRef,
  type CollaborationExportResponse,
  type CollaborationPermissionRequest,
  type CollaborationReport,
} from '@/lib/platform'
import {
  CollaborationOperationLedger,
  canDecideCollaborationReport,
  channelStateView,
  collaborationMobileView,
  collaborationTimeline,
  derivedStatusView,
  reportStateView,
  type CollaborationMobileView,
  type CollaborationTimelineItem,
} from '@/lib/collaboration-view-model'
import styles from './collaboration.module.css'

type StreamState = 'idle' | 'connecting' | 'live' | 'reconnecting'
type DecisionDraft = {
  reportId: string
  decision: 'reject' | 'needs_more_evidence'
  reason: string
}

const TERMINAL_CHANNELS = new Set(['closed', 'archived'])
const TERMINAL_ASSIGNMENTS = new Set(['cancelled', 'canceled', 'completed', 'succeeded', 'failed', 'closed'])

function displayTime(value: string | null | undefined) {
  if (!value) return '未记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function shortId(value: string | null | undefined, size = 8) {
  if (!value) return '未记录'
  return value.length > size * 2 + 1 ? `${value.slice(0, size)}…${value.slice(-4)}` : value
}

function statusTone(value: string | null | undefined) {
  const normalized = (value || '').toLocaleLowerCase()
  if (/(failed|error|rejected|disconnected|denied|unresolved|missing)/.test(normalized)) return styles.danger
  if (/(verified|passed|success|completed|restored|active|live|current|allowed)/.test(normalized)) return styles.success
  if (/(pending|await|requested|collecting|implementing|running|started|closing)/.test(normalized)) return styles.attention
  return styles.neutral
}

function stringList(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
    : []
}

function humanKey(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, character => character.toLocaleUpperCase())
}

function latestReportForChannel(channel: CollaborationChannel, details: Record<string, CollaborationChannelDetail>) {
  const reports = details[channel.channel_id]?.reports || []
  return [...reports].sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))[0]
}

function reportIcon(report: CollaborationReport) {
  if (report.category === 'platform_defect_suspected') return <AlertTriangle size={16} />
  if (report.category === 'platform_capability_gap') return <Code2 size={16} />
  if (report.category === 'environment_gap') return <Server size={16} />
  return <FileCheck2 size={16} />
}

function timelineIcon(item: CollaborationTimelineItem) {
  if (item.kind === 'tool') return <Workflow size={15} />
  if (item.kind === 'permission') return <ShieldAlert size={15} />
  if (item.kind === 'report') return <AlertTriangle size={15} />
  if (item.kind === 'decision') return <UserRound size={15} />
  if (item.kind === 'developer') return <Code2 size={15} />
  if (item.kind === 'verification') return <FileCheck2 size={15} />
  if (item.kind === 'context') return <RotateCcw size={15} />
  return <Bot size={15} />
}

function EvidenceList({ items, title = '证据引用' }: { items: CollaborationEvidenceRef[]; title?: string }) {
  if (!items.length) return null
  return <div className={styles.evidenceList}>
    <span>{title}</span>
    {items.map(item => <article key={item.evidence_id}>
      <FileCheck2 size={13} />
      <div><strong>{item.label || item.kind}</strong><small>{item.kind} · {item.media_type} · {displayTime(item.captured_at)}</small><code>{shortId(item.digest, 12)}</code></div>
    </article>)}
  </div>
}

function ConfirmDialog({
  title,
  children,
  confirmLabel,
  danger = false,
  busy = false,
  onClose,
  onConfirm,
}: {
  title: string
  children: ReactNode
  confirmLabel: string
  danger?: boolean
  busy?: boolean
  onClose: () => void
  onConfirm: () => void
}) {
  const confirmRef = useRef<HTMLButtonElement>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    confirmRef.current?.focus()
    function escape(event: KeyboardEvent) {
      if (event.key === 'Escape' && !busy) onClose()
    }
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('keydown', escape)
      restoreFocusRef.current?.focus()
    }
  }, [busy, onClose])

  return <div className={styles.dialogBackdrop} role="presentation" onMouseDown={event => {
    if (event.target === event.currentTarget && !busy) onClose()
  }}>
    <section aria-modal="true" className={styles.dialog} role="dialog" aria-labelledby="collaboration-dialog-title">
      <header><ShieldAlert size={20} /><h2 id="collaboration-dialog-title">{title}</h2></header>
      <div className={styles.dialogBody}>{children}</div>
      <footer>
        <button className={styles.secondaryButton} disabled={busy} onClick={onClose} type="button">取消</button>
        <button ref={confirmRef} className={danger ? styles.dangerButton : styles.primaryButton} disabled={busy} onClick={onConfirm} type="button">
          {busy && <LoaderCircle className={styles.spin} size={14} />}{confirmLabel}
        </button>
      </footer>
    </section>
  </div>
}

function PermissionCard({
  assignmentId,
  permission,
  busy,
  resolved,
  onResolve,
}: {
  assignmentId: string
  permission: CollaborationPermissionRequest
  busy: boolean
  resolved: boolean
  onResolve: (permission: CollaborationPermissionRequest, behavior: 'allow' | 'deny') => void
}) {
  return <section className={styles.permissionCard} data-permission-channel="runtime">
    <header><ShieldAlert size={17} /><div><span>{resolved ? '运行权限已处理' : '运行权限（与能力审批分开）'}</span><strong>{permission.tool_name}</strong></div></header>
    <p>{resolved ? '后续权限结果已覆盖这条历史请求，刷新页面也不会再次授权。' : '莉莉丝需要执行一次受控工具调用。允许只对这一次、这份输入摘要生效；能否修改代码仍取决于任务显式授予的 workspace 与 scope，这个按钮不会自动扩大权限。'}</p>
    <dl>
      {Object.entries(permission.redacted_input).slice(0, 10).map(([key, value]) => <div key={key}>
        <dt>{humanKey(key)}</dt>
        <dd>{typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ? String(value) : '结构化内容（已脱敏）'}</dd>
      </div>)}
      <div><dt>输入摘要</dt><dd><code>{shortId(permission.input_digest, 12)}</code></dd></div>
      <div><dt>Assignment</dt><dd><code>{shortId(assignmentId)}</code></dd></div>
    </dl>
    {resolved ? <div className={styles.permissionResolved}><CheckCircle2 size={14} />这次请求已处理；等待莉莉丝继续同步。</div> : <footer>
      <button className={styles.secondaryButton} disabled={busy} onClick={() => onResolve(permission, 'deny')} type="button"><XCircle size={14} />拒绝</button>
      <button className={styles.permissionButton} disabled={busy} onClick={() => onResolve(permission, 'allow')} type="button"><Check size={14} />允许一次</button>
    </footer>}
  </section>
}

export function CollaborationWorkspace() {
  const [channels, setChannels] = useState<CollaborationChannel[]>([])
  const [detailCache, setDetailCache] = useState<Record<string, CollaborationChannelDetail>>({})
  const [selectedChannelId, setSelectedChannelId] = useState('')
  const [detail, setDetail] = useState<CollaborationChannelDetail | null>(null)
  const [exported, setExported] = useState<CollaborationExportResponse | null>(null)
  const [selectedTimelineId, setSelectedTimelineId] = useState('')
  const [selectedReportId, setSelectedReportId] = useState('')
  const [mobileView, setMobileView] = useState<CollaborationMobileView>('tasks')
  const [channelFilter, setChannelFilter] = useState<'open' | 'all'>('open')
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [authNeeded, setAuthNeeded] = useState(false)
  const [accessKey, setAccessKey] = useState('')
  const [streamState, setStreamState] = useState<StreamState>('idle')
  const [decisionDraft, setDecisionDraft] = useState<DecisionDraft | null>(null)
  const [dialog, setDialog] = useState<'auto' | 'stop' | null>(null)
  const [resolvedPermissionIds, setResolvedPermissionIds] = useState<Set<string>>(() => new Set())
  const [pendingOperations, setPendingOperations] = useState<Set<string>>(() => new Set())
  const ledgerRef = useRef(new CollaborationOperationLedger(idempotency))
  const inFlightRef = useRef(new Set<string>())
  const loadSequenceRef = useRef(0)

  const readLocation = useCallback(() => {
    const query = new URLSearchParams(window.location.search)
    const channelId = query.get('channel') || ''
    const nextView = collaborationMobileView(query.get('view'), Boolean(channelId))
    setSelectedChannelId(channelId)
    setSelectedTimelineId(query.get('event') || '')
    setSelectedReportId(query.get('report') || '')
    setMobileView(nextView)
  }, [])

  const writeLocation = useCallback((
    next: {
      channelId?: string
      view?: CollaborationMobileView
      eventId?: string
      reportId?: string
    },
    replace = false,
  ) => {
    const query = new URLSearchParams(window.location.search)
    const channelId = next.channelId === undefined ? query.get('channel') || '' : next.channelId
    const view = next.view === undefined
      ? collaborationMobileView(query.get('view'), Boolean(channelId))
      : next.view
    const eventId = next.eventId === undefined ? query.get('event') || '' : next.eventId
    const reportId = next.reportId === undefined ? query.get('report') || '' : next.reportId
    if (channelId) query.set('channel', channelId)
    else query.delete('channel')
    if (view !== 'tasks') query.set('view', view)
    else query.delete('view')
    if (eventId) query.set('event', eventId)
    else query.delete('event')
    if (reportId) query.set('report', reportId)
    else query.delete('report')
    const href = `${window.location.pathname}${query.size ? `?${query.toString()}` : ''}`
    window.history[replace ? 'replaceState' : 'pushState']({}, '', href)
  }, [])

  const handleError = useCallback((caught: unknown) => {
    if (isAuthError(caught)) setAuthNeeded(true)
    else setError(String(caught))
  }, [])

  const loadChannels = useCallback(async (preferredChannelId?: string) => {
    try {
      const response = await listStudioCollaborationChannels()
      const nextChannels = [...response.channels].sort((left, right) => {
        const leftOpen = TERMINAL_CHANNELS.has(left.status) ? 1 : 0
        const rightOpen = TERMINAL_CHANNELS.has(right.status) ? 1 : 0
        return leftOpen - rightOpen || Date.parse(right.created_at) - Date.parse(left.created_at)
      })
      setChannels(nextChannels)
      setAuthNeeded(false)
      const locationQuery = new URLSearchParams(window.location.search)
      const locationChannelId = locationQuery.get('channel') || ''
      const requested = preferredChannelId || locationChannelId
      const available = nextChannels.some(channel => channel.channel_id === requested)
      const fallback = nextChannels.find(channel => !TERMINAL_CHANNELS.has(channel.status))?.channel_id || nextChannels[0]?.channel_id || ''
      const nextSelected = available ? requested : fallback
      if (nextSelected) {
        setSelectedChannelId(nextSelected)
        if (nextSelected !== locationChannelId) {
          const nextView = collaborationMobileView(locationQuery.get('view'), true)
          setMobileView(nextView)
          writeLocation({ channelId: nextSelected, view: nextView, eventId: '', reportId: '' }, true)
        }
      }
    } catch (caught) {
      handleError(caught)
    } finally {
      setLoading(false)
    }
  }, [handleError, writeLocation])

  const loadDetail = useCallback(async (channelId: string, quiet = false) => {
    if (!channelId) {
      setDetail(null)
      setExported(null)
      return
    }
    const sequence = ++loadSequenceRef.current
    if (!quiet) setDetailLoading(true)
    try {
      const [nextDetail, nextExport] = await Promise.all([
        studioCollaborationChannel(channelId),
        studioCollaborationExport(channelId),
      ])
      if (sequence !== loadSequenceRef.current) return
      setDetail(nextDetail)
      setExported(nextExport)
      setDetailCache(current => ({ ...current, [channelId]: nextDetail }))
      setAuthNeeded(false)
      setError('')
    } catch (caught) {
      if (sequence === loadSequenceRef.current) handleError(caught)
    } finally {
      if (sequence === loadSequenceRef.current && !quiet) setDetailLoading(false)
    }
  }, [handleError])

  useEffect(() => {
    readLocation()
    void loadChannels()
    const onPopState = () => readLocation()
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [loadChannels, readLocation])

  useEffect(() => {
    if (selectedChannelId) void loadDetail(selectedChannelId)
  }, [loadDetail, selectedChannelId])

  useEffect(() => {
    if (!selectedChannelId || TERMINAL_CHANNELS.has(detail?.channel.status || '')) {
      setStreamState('idle')
      return
    }
    const controller = new AbortController()
    let cursor = 0
    let refreshTimer: number | null = null

    const scheduleRefresh = () => {
      if (refreshTimer !== null) return
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null
        void Promise.all([loadDetail(selectedChannelId, true), loadChannels(selectedChannelId)])
      }, 250)
    }

    const consume = async () => {
      let firstAttempt = true
      while (!controller.signal.aborted) {
        setStreamState(firstAttempt ? 'connecting' : 'reconnecting')
        try {
          const response = await openStudioCollaborationEventStream(selectedChannelId, cursor, controller.signal)
          if (!response.body) throw new Error('协作事件流不可用')
          setStreamState('live')
          const reader = response.body.getReader()
          const decoder = new TextDecoder()
          let buffer = ''
          while (!controller.signal.aborted) {
            const chunk = await reader.read()
            if (chunk.done) break
            buffer += decoder.decode(chunk.value, { stream: true }).replaceAll('\r\n', '\n')
            let boundary = buffer.indexOf('\n\n')
            while (boundary >= 0) {
              const frame = buffer.slice(0, boundary)
              buffer = buffer.slice(boundary + 2)
              const idLine = frame.split('\n').find(line => line.startsWith('id:'))
              const parsed = Number(idLine?.slice(3).trim())
              if (Number.isFinite(parsed) && parsed > cursor) cursor = parsed
              if (frame.split('\n').some(line => line.startsWith('data:'))) scheduleRefresh()
              boundary = buffer.indexOf('\n\n')
            }
          }
        } catch (caught) {
          if (controller.signal.aborted) return
          if (isAuthError(caught)) {
            handleError(caught)
            return
          }
        }
        firstAttempt = false
        await new Promise(resolve => window.setTimeout(resolve, 1_500))
      }
    }
    void consume()
    return () => {
      controller.abort()
      if (refreshTimer !== null) window.clearTimeout(refreshTimer)
    }
  }, [detail?.channel.status, handleError, loadChannels, loadDetail, selectedChannelId])

  const timeline = useMemo(() => detail ? collaborationTimeline(detail) : [], [detail])
  const selectedTimeline = useMemo(
    () => timeline.find(item => item.id === selectedTimelineId) || timeline[timeline.length - 1] || null,
    [selectedTimelineId, timeline],
  )
  const selectedReport = useMemo(() => {
    if (!detail) return null
    const reportId = selectedReportId || selectedTimeline?.reportId
    return detail.reports.find(report => report.report_id === reportId)
      || [...detail.reports].sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))[0]
      || null
  }, [detail, selectedReportId, selectedTimeline?.reportId])
  const assignment = detail?.context?.assignment || null
  const channel = detail?.channel || channels.find(item => item.channel_id === selectedChannelId) || null
  const channelView = detail?.derived
    ? derivedStatusView(detail.derived)
    : channel
      ? channelStateView(channel)
      : null
  const filteredChannels = useMemo(
    () => channelFilter === 'all' ? channels : channels.filter(item => !TERMINAL_CHANNELS.has(item.status)),
    [channelFilter, channels],
  )
  const streamLabel = streamState === 'live' ? '实时同步' : streamState === 'idle' ? '只读' : streamState === 'connecting' ? '连接事件流' : '正在重连'

  const runOperation = useCallback(async (
    signature: string,
    operation: (key: string) => Promise<unknown>,
    successMessage: string,
  ) => {
    if (inFlightRef.current.has(signature)) return false
    inFlightRef.current.add(signature)
    setPendingOperations(current => new Set(current).add(signature))
    setError('')
    setNotice('')
    const key = ledgerRef.current.keyFor(signature)
    try {
      await operation(key)
      ledgerRef.current.complete(signature)
      setNotice(successMessage)
    } catch (caught) {
      handleError(caught)
      inFlightRef.current.delete(signature)
      setPendingOperations(current => {
        const next = new Set(current)
        next.delete(signature)
        return next
      })
      return false
    }
    inFlightRef.current.delete(signature)
    setPendingOperations(current => {
      const next = new Set(current)
      next.delete(signature)
      return next
    })
    const refreshes = await Promise.allSettled([
      loadChannels(selectedChannelId),
      loadDetail(selectedChannelId, true),
    ])
    const refreshFailure = refreshes.find(result => result.status === 'rejected')
    if (refreshFailure?.status === 'rejected') handleError(refreshFailure.reason)
    return true
  }, [handleError, loadChannels, loadDetail, selectedChannelId])

  const decide = useCallback(async (
    report: CollaborationReport,
    decision: 'approve' | 'reject' | 'needs_more_evidence',
    reason?: string,
  ) => {
    const signature = `report:${report.report_id}:${report.revision}:${decision}`
    const completed = await runOperation(signature, key => decideStudioCollaborationReport(report.report_id, {
      idempotency_key: key,
      expected_report_revision: report.revision,
      decision,
      ...(reason?.trim() ? { reason: reason.trim() } : {}),
    }), decision === 'approve' ? '报告已批准并发送给 Codex。' : decision === 'reject' ? '报告已拒绝。' : '已要求莉莉丝补充证据。')
    if (completed) setDecisionDraft(null)
  }, [runOperation])

  const changeApprovalMode = useCallback(async (mode: CollaborationApprovalMode) => {
    if (!channel) return
    const signature = `mode:${channel.channel_id}:${channel.revision}:${mode}`
    const completed = await runOperation(signature, key => updateStudioCollaborationSettings(channel.channel_id, {
      idempotency_key: key,
      expected_channel_revision: channel.revision,
      approval_mode: mode,
      confirmed: mode === 'auto_forward',
    }), mode === 'auto_forward' ? '自动转发已开启；后续合格报告会直接发给 Codex。' : '已切回每次人工审批。')
    if (completed) setDialog(null)
  }, [channel, runOperation])

  const stopTask = useCallback(async () => {
    if (!channel) return
    const assignmentId = assignment?.assignment_id || channel.assignment_id
    const assignmentStatus = assignment?.status || ''
    const stopSignature = `stop-assignment:${assignmentId}`
    const closeSignature = `close-channel:${channel.channel_id}:${channel.revision}`
    if (!TERMINAL_ASSIGNMENTS.has(assignmentStatus.toLocaleLowerCase())) {
      if (inFlightRef.current.has(stopSignature)) return
      inFlightRef.current.add(stopSignature)
      setPendingOperations(current => new Set(current).add(stopSignature))
      try {
        const key = ledgerRef.current.keyFor(stopSignature)
        await cancelLocalLiliesAssignment(assignmentId, {
          idempotency_key: key,
          reason: '用户从协作监控台明确停止任务。',
        })
        ledgerRef.current.complete(stopSignature)
      } catch (caught) {
        handleError(caught)
        inFlightRef.current.delete(stopSignature)
        setPendingOperations(current => {
          const next = new Set(current)
          next.delete(stopSignature)
          return next
        })
        return
      }
      inFlightRef.current.delete(stopSignature)
      setPendingOperations(current => {
        const next = new Set(current)
        next.delete(stopSignature)
        return next
      })
    }
    const completed = await runOperation(closeSignature, key => closeStudioCollaborationChannel(channel.channel_id, {
      idempotency_key: key,
      expected_channel_revision: channel.revision,
      reason: '用户从协作监控台明确停止任务并关闭通道。',
    }), 'Assignment 已取消，协作通道已关闭。')
    if (completed) setDialog(null)
  }, [assignment, channel, handleError, runOperation])

  const resolvePermission = useCallback(async (
    permission: CollaborationPermissionRequest,
    behavior: 'allow' | 'deny',
  ) => {
    if (!assignment) return
    const signature = `permission:${assignment.assignment_id}:${permission.request_id}:${permission.input_digest}:${behavior}`
    const completed = await runOperation(signature, key => resolveLocalLiliesAssignmentPermission(
      assignment.assignment_id,
      permission.request_id,
      {
        idempotency_key: key,
        behavior,
        expected_input_digest: permission.input_digest,
        ...(behavior === 'deny' ? { message: '用户拒绝本次工具调用。' } : {}),
      },
    ), behavior === 'allow' ? '已允许这一次工具调用。' : '已拒绝这一次工具调用。')
    if (completed) setResolvedPermissionIds(current => new Set(current).add(permission.request_id))
  }, [assignment, runOperation])

  function selectChannel(nextChannelId: string) {
    setSelectedChannelId(nextChannelId)
    setSelectedTimelineId('')
    setSelectedReportId('')
    setMobileView('timeline')
    writeLocation({ channelId: nextChannelId, view: 'timeline', eventId: '', reportId: '' })
  }

  function selectTimeline(item: CollaborationTimelineItem) {
    setSelectedTimelineId(item.id)
    setSelectedReportId(item.reportId || '')
    setMobileView('detail')
    writeLocation({ view: 'detail', eventId: item.id, reportId: item.reportId || '' })
  }

  function selectReport(report: CollaborationReport) {
    setSelectedReportId(report.report_id)
    setMobileView('detail')
    writeLocation({ view: 'detail', reportId: report.report_id })
  }

  function navigateMobile(view: CollaborationMobileView) {
    setMobileView(view)
    writeLocation({ view })
  }

  function connect() {
    saveClientToken(accessKey)
    setAuthNeeded(false)
    setLoading(true)
    void loadChannels(selectedChannelId)
  }

  if (authNeeded) {
    return <main className={styles.shell} data-collaboration-workspace="auth">
      <section className={styles.authState}>
        <KeyRound size={28} />
        <h1>需要平台访问密钥</h1>
        <p>这个页面只接受用户级平台凭证，不会把开发者凭证存进浏览器。</p>
        <label><span>平台访问密钥</span><input autoFocus type="password" value={accessKey} onChange={event => setAccessKey(event.target.value)} /></label>
        <button className={styles.primaryButton} disabled={!accessKey.trim()} onClick={connect} type="button"><Check size={15} />连接</button>
      </section>
    </main>
  }

  return <main className={styles.shell} data-collaboration-workspace="true" data-mobile-view={mobileView}>
    <header className={styles.topbar}>
      <div className={styles.topbarTitle}>
        <Link href="/" aria-label="返回应用列表"><ArrowLeft size={17} /></Link>
        <MessagesSquare size={19} />
        <div><span>Developer Studio</span><h1>莉莉丝协作监控台</h1></div>
      </div>
      <div className={styles.topbarSignals}>
        <span className={statusTone(streamState)}><i />{streamLabel}</span>
        {assignment && <span className={statusTone(assignment.daemon_status)} title={`连接：${assignment.connection_status}`}><Bot size={13} />daemon {assignment.daemon_status || '未报告'}</span>}
        {channel && <span className={statusTone(channel.approval_mode)}><ShieldCheck size={13} />{channel.approval_mode === 'manual' ? '逐次审批' : '自动转发'}</span>}
        {assignment?.deadline_at && <span><Clock3 size={13} />截止 {displayTime(assignment.deadline_at)}</span>}
        {assignment?.max_budget_usd && <span>预算 ${assignment.max_budget_usd.toFixed(2)}</span>}
      </div>
      <div className={styles.topbarActions}>
        <button aria-label="刷新协作数据" className={styles.iconButton} disabled={loading || detailLoading} onClick={() => void Promise.all([loadChannels(selectedChannelId), loadDetail(selectedChannelId)])} title="刷新协作数据" type="button"><RefreshCw className={loading || detailLoading ? styles.spin : ''} size={16} /></button>
        {channel && !TERMINAL_CHANNELS.has(channel.status) && <>
          {channel.approval_mode === 'manual'
            ? <button className={styles.secondaryButton} onClick={() => setDialog('auto')} type="button">开启自动转发</button>
            : <button className={styles.secondaryButton} disabled={pendingOperations.has(`mode:${channel.channel_id}:${channel.revision}:manual`)} onClick={() => void changeApprovalMode('manual')} type="button">改为逐次审批</button>}
          <button className={styles.stopButton} onClick={() => setDialog('stop')} type="button"><CircleStop size={15} />停止任务</button>
        </>}
      </div>
    </header>

    {(error || notice) && <section className={`${styles.feedback} ${error ? styles.feedbackError : styles.feedbackNotice}`} role={error ? 'alert' : 'status'}>
      {error ? <AlertTriangle size={16} /> : <CheckCircle2 size={16} />}
      <span>{error || notice}</span>
      <button aria-label="关闭提示" onClick={() => { setError(''); setNotice('') }} type="button"><X size={14} /></button>
    </section>}

    <section className={styles.mobileTrail} aria-label="移动端协作层级">
      <button aria-current={mobileView === 'tasks' ? 'step' : undefined} onClick={() => navigateMobile('tasks')} type="button">任务</button>
      <ChevronRight size={13} />
      <button aria-current={mobileView === 'timeline' ? 'step' : undefined} disabled={!selectedChannelId} onClick={() => navigateMobile('timeline')} type="button">时间线</button>
      <ChevronRight size={13} />
      <button aria-current={mobileView === 'detail' ? 'step' : undefined} disabled={!selectedChannelId} onClick={() => navigateMobile('detail')} type="button">详情</button>
    </section>

    <section className={styles.workspace}>
      <aside className={`${styles.pane} ${styles.taskPane}`} data-pane="tasks">
        <header className={styles.paneHeader}>
          <div><span>任务与通道</span><strong>{filteredChannels.length} 条</strong></div>
          <div className={styles.segmented} role="group" aria-label="任务筛选">
            <button aria-pressed={channelFilter === 'open'} onClick={() => setChannelFilter('open')} type="button">进行中</button>
            <button aria-pressed={channelFilter === 'all'} onClick={() => setChannelFilter('all')} type="button">全部</button>
          </div>
        </header>
        <div className={styles.taskList}>
          {loading && !channels.length && <div className={styles.loadingState}><LoaderCircle className={styles.spin} size={20} />加载协作任务</div>}
          {!loading && !filteredChannels.length && <div className={styles.emptyState}><MessagesSquare size={24} /><strong>暂无协作任务</strong><p>从应用的 Engineer Studio 把任务派给莉莉丝后，会在这里出现。</p><Link href="/">返回应用列表</Link></div>}
          {filteredChannels.map(item => {
            const latest = latestReportForChannel(item, detailCache)
            const cachedDetail = detailCache[item.channel_id]
            const state = cachedDetail?.derived
              ? derivedStatusView(cachedDetail.derived)
              : channelStateView(item)
            const unreadCount = cachedDetail?.derived?.unread_count ?? item.unread_count ?? 0
            return <button aria-current={selectedChannelId === item.channel_id ? 'true' : undefined} className={styles.taskCard} key={item.channel_id} onClick={() => selectChannel(item.channel_id)} type="button">
              <div className={styles.taskCardTop}><span className={styles[state.tone]}><i />{state.label}</span>{unreadCount > 0 && <b aria-label={`${unreadCount} 条未读`}>{unreadCount}</b>}</div>
              <strong>{item.task_id}</strong>
              <small>任务修订 r{item.task_revision} · 会话 {shortId(item.lilies_session_id)} · {item.application_ids.length} 个应用</small>
              {cachedDetail?.derived ? <p>{cachedDetail.derived.next_action.label}</p> : latest ? <p>{latest.summary}</p> : <p>{state.nextAction}</p>}
              <footer><code>{shortId(item.channel_id)}</code><time>{displayTime(item.created_at)}</time></footer>
            </button>
          })}
        </div>
      </aside>

      <section className={`${styles.pane} ${styles.timelinePane}`} data-pane="timeline">
        <header className={styles.paneHeader}>
          <button className={styles.mobileBack} onClick={() => navigateMobile('tasks')} type="button"><ChevronLeft size={15} />任务</button>
          <div><span>公开执行时间线</span><strong>{timeline.length} 个事件</strong></div>
          {channel && <code>{shortId(channel.channel_id)}</code>}
        </header>
        {channelView && <section className={`${styles.stateSummary} ${styles[channelView.tone]}`} data-channel-state={channel?.status}>
          <div><span>{channelView.label}</span><strong>{channelView.what}</strong></div>
          <dl><div><dt>当前负责</dt><dd>{channelView.owner}</dd></div><div><dt>为什么在等待</dt><dd>{channelView.why}</dd></div><div><dt>下一步</dt><dd>{channelView.nextAction}</dd></div></dl>
        </section>}
        <div className={styles.timelineList}>
          {detailLoading && !detail && <div className={styles.loadingState}><LoaderCircle className={styles.spin} size={20} />加载公开事件</div>}
          {!detailLoading && detail && !timeline.length && <div className={styles.emptyState}><Clock3 size={24} /><strong>等待第一个公开事件</strong><p>私有推理不会显示；公开消息、工具、报告与验证会按时间出现。</p></div>}
          {timeline.map(item => <button aria-current={selectedTimeline?.id === item.id ? 'true' : undefined} className={styles.timelineCard} data-event-kind={item.kind} key={item.id} onClick={() => selectTimeline(item)} type="button">
            <div className={styles.timelineRail}><span>{timelineIcon(item)}</span><i /></div>
            <div className={styles.timelineContent}>
              <header><strong>{item.title}</strong><time>{displayTime(item.occurredAt)}</time></header>
              <p>{item.summary || '状态已更新。'}</p>
              <footer><span className={statusTone(item.status)}>{item.status || item.kind}</span><span>{item.actor || 'platform'}</span>{item.durationMs !== undefined && <span>{item.durationMs < 1_000 ? `${Math.round(item.durationMs)} ms` : `${(item.durationMs / 1_000).toFixed(1)} s`}</span>}{item.evidenceRefs.length > 0 && <span>{item.evidenceRefs.length} 份证据</span>}</footer>
            </div>
          </button>)}
        </div>
      </section>

      <aside className={`${styles.pane} ${styles.detailPane}`} data-pane="detail">
        <header className={styles.paneHeader}>
          <button className={styles.mobileBack} onClick={() => navigateMobile('timeline')} type="button"><ChevronLeft size={15} />时间线</button>
          <div><span>状态、证据与下一步</span><strong>语义详情</strong></div>
          {selectedTimeline && <span className={statusTone(selectedTimeline.status)}>{selectedTimeline.status || selectedTimeline.kind}</span>}
        </header>
        <div className={styles.detailScroll}>
          {assignment && <section className={styles.detailSection} data-assignment-context="semantic">
            <header><Bot size={16} /><div><span>正式 Assignment</span><strong>{assignment.phase} · {assignment.status}</strong></div></header>
            <p className={styles.requirement}>{assignment.requirement}</p>
            <dl className={styles.factGrid}>
              <div><dt>任务</dt><dd>{assignment.task_id} · r{assignment.task_revision}</dd></div>
              <div><dt>莉莉丝会话</dt><dd><code>{shortId(assignment.session_id)}</code></dd></div>
              <div><dt>连接</dt><dd>{assignment.connection_status}</dd></div>
              <div><dt>合同摘要</dt><dd><code>{shortId(assignment.contract_digest, 12)}</code></dd></div>
              <div><dt>公开任务摘要</dt><dd><code>{shortId(assignment.task_package?.public_summary_digest, 12)}</code></dd></div>
              <div><dt>更新时间</dt><dd>{displayTime(assignment.updated_at)}</dd></div>
              <div><dt>回合上限</dt><dd>{assignment.max_turns ?? '未设'}</dd></div>
              <div><dt>工具调用上限</dt><dd>{assignment.max_tool_calls ?? '未设'}</dd></div>
            </dl>
            {Object.entries(assignment.business_context).map(([key, value]) => {
              const values = stringList(value)
              if (!values.length && typeof value !== 'string') return null
              return <div className={styles.semanticList} key={key}><span>{humanKey(key)}</span>{values.length ? <ul>{values.map((item, index) => <li key={`${key}-${index}`}>{item}</li>)}</ul> : <p>{String(value)}</p>}</div>
            })}
            {assignment.deliverables.length > 0 && <div className={styles.deliverableList}><span>正式交付物</span>{assignment.deliverables.map(deliverable => <article key={`${deliverable.name}:${deliverable.media_type}`}><FileCheck2 size={13} /><div><strong>{deliverable.name}{deliverable.required ? '（必需）' : ''}</strong><p>{deliverable.description}</p><small>{deliverable.media_type}</small></div></article>)}</div>}
            {assignment.compaction && <section className={styles.compactionSummary}><RotateCcw size={14} /><div><strong>上下文压缩摘要</strong><span>覆盖到 Assignment 事件 #{assignment.compaction.summary_through_event_seq}</span><p>{assignment.compaction.summary}</p></div></section>}
            {assignment.allowed_actions.length > 0 && <div className={styles.chips}><span>允许动作</span>{assignment.allowed_actions.map(action => <b key={action}>{action}</b>)}</div>}
          </section>}

          {selectedTimeline && <section className={styles.detailSection} data-selected-event={selectedTimeline.kind}>
            <header>{timelineIcon(selectedTimeline)}<div><span>{selectedTimeline.actor || 'platform'} · {displayTime(selectedTimeline.occurredAt)}</span><strong>{selectedTimeline.title}</strong></div></header>
            <p>{selectedTimeline.summary || '状态已更新。'}</p>
            <dl className={styles.factGrid}>
              <div><dt>发生了什么</dt><dd>{selectedTimeline.kind === 'tool' ? '莉莉丝执行了一次公开工具步骤。' : selectedTimeline.title}</dd></div>
              <div><dt>当前状态</dt><dd>{selectedTimeline.status || '已记录'}</dd></div>
              <div><dt>谁在负责</dt><dd>{selectedTimeline.actor || '平台'}</dd></div>
              <div><dt>证据</dt><dd>{selectedTimeline.evidenceRefs.length ? `${selectedTimeline.evidenceRefs.length} 份引用` : '此事件未附独立证据'}</dd></div>
            </dl>
            {selectedTimeline.redactedInput && selectedTimeline.redactedInput.length > 0 && <div className={styles.semanticList}><span>脱敏调用输入</span><dl>{selectedTimeline.redactedInput.map(item => <div key={item.label}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl></div>}
          </section>}

          {selectedTimeline?.permissionRequest && assignment && <PermissionCard
            assignmentId={assignment.assignment_id}
            busy={pendingOperations.has(`permission:${assignment.assignment_id}:${selectedTimeline.permissionRequest.request_id}:${selectedTimeline.permissionRequest.input_digest}:allow`) || pendingOperations.has(`permission:${assignment.assignment_id}:${selectedTimeline.permissionRequest.request_id}:${selectedTimeline.permissionRequest.input_digest}:deny`)}
            onResolve={(permission, behavior) => void resolvePermission(permission, behavior)}
            permission={selectedTimeline.permissionRequest}
            resolved={resolvedPermissionIds.has(selectedTimeline.permissionRequest.request_id) || Boolean(selectedTimeline.permissionResolved)}
          />}

          {detail?.reports.length ? <section className={styles.detailSection}>
            <header><AlertTriangle size={16} /><div><span>阻塞与升级报告</span><strong>{detail.reports.length} 份报告</strong></div></header>
            <div className={styles.reportTabs}>{detail.reports.map(report => {
              const view = reportStateView(report)
              return <button aria-pressed={selectedReport?.report_id === report.report_id} key={report.report_id} onClick={() => selectReport(report)} type="button">{reportIcon(report)}<span>{report.summary}</span><b className={styles[view.tone]}>{view.label}</b></button>
            })}</div>
          </section> : null}

          {selectedReport && (() => {
            const view = reportStateView(selectedReport)
            const decisionBusy = ['approve', 'reject', 'needs_more_evidence'].some(decision => pendingOperations.has(`report:${selectedReport.report_id}:${selectedReport.revision}:${decision}`))
            const leases = [
              ...(detail?.active_leases || []),
              ...(exported?.export.developer_leases || []),
            ].filter((item, index, all) => item.report_id === selectedReport.report_id
              && all.findIndex(candidate => candidate.lease_id === item.lease_id) === index)
            const responses = exported?.export.developer_responses?.filter(item => item.report_id === selectedReport.report_id) || []
            return <section className={`${styles.reportDetail} ${styles[view.tone]}`} data-report-status={selectedReport.status}>
              <header>{reportIcon(selectedReport)}<div><span>{selectedReport.category} · {selectedReport.phase}</span><h2>{selectedReport.summary}</h2></div><b>{view.label}</b></header>
              <div className={styles.causality}>
                <article><span>发生了什么</span><p>{view.what}</p></article>
                <article><span>谁在负责</span><p>{view.owner}</p></article>
                <article><span>为什么</span><p>{view.why}</p></article>
                <article><span>下一步</span><p>{view.nextAction}</p></article>
              </div>
              <dl className={styles.reportFacts}>
                <div><dt>原始目标</dt><dd>{selectedReport.original_goal}</dd></div>
                {selectedReport.expected && <div><dt>预期</dt><dd>{selectedReport.expected}</dd></div>}
                {selectedReport.actual && <div><dt>实际</dt><dd>{selectedReport.actual}</dd></div>}
                {selectedReport.missing_contract && <div><dt>缺失能力</dt><dd>{selectedReport.missing_contract}</dd></div>}
                <div><dt>阻塞范围</dt><dd>{selectedReport.blocking_scope}</dd></div>
                <div><dt>绕行损失</dt><dd>{selectedReport.workaround_loss}</dd></div>
                <div><dt>请求结果</dt><dd>{selectedReport.requested_outcome}</dd></div>
              </dl>
              {selectedReport.attempted_routes.length > 0 && <div className={styles.routeList}><span>已经尝试</span>{selectedReport.attempted_routes.map(route => <article key={route.attempt_id}><strong>{route.action || route.route}</strong><p>{route.result}</p></article>)}</div>}
              <div className={styles.evidenceLine}><FileCheck2 size={14} /><span>{selectedReport.evidence_refs.length} 份证据 · {selectedReport.manuals_checked.length} 份手册引用 · 置信度 {Math.round(selectedReport.confidence * 100)}%</span></div>
              <EvidenceList items={selectedReport.evidence_refs} />
              {selectedReport.manuals_checked.length > 0 && <div className={styles.manualList}><span>核对过的手册</span>{selectedReport.manuals_checked.map(manual => <article key={manual.manual_id}><strong>{manual.title}</strong><small>{[manual.version, manual.section].filter(Boolean).join(' · ') || manual.manual_id}</small>{manual.digest && <code>{shortId(manual.digest, 12)}</code>}</article>)}</div>}
              {canDecideCollaborationReport(selectedReport) && <div className={styles.approvalPanel} data-approval-channel="capability">
                <p><ShieldCheck size={15} /><span>能力审批只决定是否把平台报告交给 Codex；它不会授予莉莉丝运行工具的权限。</span></p>
                {decisionDraft?.reportId === selectedReport.report_id ? <form onSubmit={event => {
                  event.preventDefault()
                  if (!decisionDraft.reason.trim()) return
                  void decide(selectedReport, decisionDraft.decision, decisionDraft.reason)
                }}>
                  <label><span>{decisionDraft.decision === 'reject' ? '拒绝理由' : '需要补充什么证据'}</span><textarea autoFocus value={decisionDraft.reason} onChange={event => setDecisionDraft(current => current ? { ...current, reason: event.target.value } : null)} /></label>
                  <div><button className={styles.secondaryButton} disabled={decisionBusy} onClick={() => setDecisionDraft(null)} type="button">取消</button><button className={decisionDraft.decision === 'reject' ? styles.dangerButton : styles.primaryButton} disabled={decisionBusy || !decisionDraft.reason.trim()} type="submit">确认</button></div>
                </form> : <div className={styles.approvalActions}>
                  <button className={styles.secondaryButton} disabled={decisionBusy} onClick={() => setDecisionDraft({ reportId: selectedReport.report_id, decision: 'reject', reason: '' })} type="button"><XCircle size={14} />拒绝</button>
                  <button className={styles.secondaryButton} disabled={decisionBusy} onClick={() => setDecisionDraft({ reportId: selectedReport.report_id, decision: 'needs_more_evidence', reason: '' })} type="button"><FileCheck2 size={14} />更多证据</button>
                  <button className={styles.primaryButton} disabled={decisionBusy} onClick={() => void decide(selectedReport, 'approve')} type="button"><Send size={14} />批准并发送</button>
                </div>}
              </div>}
              {leases.map(lease => <div className={styles.developerEvidence} key={lease.lease_id}><Code2 size={15} /><div><strong>Codex 处理租约 · {lease.status}</strong><span>负责人 {lease.owner_id} · 到期 {displayTime(lease.expires_at)}</span></div></div>)}
              {responses.map(response => <div className={styles.developerResponse} key={response.response_id}>
                <header><Code2 size={15} /><strong>Codex 结果：{response.outcome}</strong>{response.commit_sha && <code>{shortId(response.commit_sha, 10)}</code>}</header>
                {response.generic_capability_changes.map(change => <p key={change}>{change}</p>)}
                <span>{response.tests_run.length} 项测试 · {response.browser_or_live_evidence.length} 份实时证据 · {response.known_limits.length} 项已知限制</span>
                <div className={styles.testEvidence}>{response.tests_run.map(test => <article key={test.test_id}><TestTube2 size={13} /><div><strong>{test.command}</strong><p>{test.summary}</p><small className={test.exit_code === 0 ? styles.success : styles.danger}>退出码 {test.exit_code}</small></div></article>)}</div>
                <EvidenceList items={response.browser_or_live_evidence} title="浏览器或实时证据" />
                {response.known_limits.length > 0 && <div className={styles.semanticList}><span>已知限制</span><ul>{response.known_limits.map(limit => <li key={limit}>{limit}</li>)}</ul></div>}
                {response.reprobe_steps.length > 0 && <div className={styles.reprobeSteps}><span>莉莉丝复验步骤</span><ol>{response.reprobe_steps.map(step => <li key={step.order}><b>{step.order}</b><div><strong>{step.action}</strong><p>预期：{step.expected}</p></div></li>)}</ol></div>}
              </div>)}
            </section>
          })()}

          {(detail?.claims ?? exported?.export.claims ?? []).map(claim => {
            const claimVerifications = exported?.export.verifications?.filter(item => item.claim_id === claim.claim_id) || []
            return <section className={styles.claimCard} data-claim-status={claim.status} key={claim.claim_id}>
              <header><FileCheck2 size={16} /><div><span>冻结交付声明</span><strong>{claim.status}</strong></div><b className={statusTone(claim.status)}>r{claim.claim_revision}</b></header>
              <p>莉莉丝已把应用草稿 r{claim.draft_revision} 的内容、测试、业务运行和交付物冻结为独立验证对象。</p>
              <dl className={styles.factGrid}>
                <div><dt>应用</dt><dd><Link href={`/applications/${claim.application_id}`}>{shortId(claim.application_id)}</Link></dd></div>
                <div><dt>内容摘要</dt><dd><code>{shortId(claim.content_hash, 12)}</code></dd></div>
                <div><dt>测试运行</dt><dd>{claim.test_run_ids.length}</dd></div>
                <div><dt>业务运行</dt><dd>{claim.business_run_ids.length}</dd></div>
                <div><dt>已解决报告</dt><dd>{claim.resolved_report_ids.length}</dd></div>
                <div><dt>验证结果</dt><dd>{claimVerifications.length || '等待中'}</dd></div>
              </dl>
              <EvidenceList items={[...claim.artifact_refs, ...claim.host_receipt_refs]} title="交付物与宿主回执" />
              {claim.remaining_limits.length > 0 && <div className={styles.semanticList}><span>剩余边界</span><ul>{claim.remaining_limits.map(limit => <li key={limit}>{limit}</li>)}</ul></div>}
              {claim.invalidation_reason && <p className={styles.runError}>失效原因：{claim.invalidation_reason}</p>}
              {claimVerifications.map(verification => <article className={`${styles.verificationCard} ${statusTone(verification.verdict)}`} key={verification.verification_id}>
                <header><ShieldCheck size={15} /><div><strong>{verification.verdict === 'independently_verified' ? '独立验证通过' : '独立验证失败'}</strong><span>验证者 {verification.verifier_id} · {displayTime(verification.created_at)}</span></div></header>
                <code>Oracle {shortId(verification.oracle_digest, 12)}</code>
                {verification.differences.map(difference => <dl key={difference.check_id}><div><dt>检查</dt><dd>{difference.check_id}</dd></div><div><dt>预期</dt><dd>{difference.expected}</dd></div><div><dt>实际</dt><dd>{difference.actual}</dd></div></dl>)}
                <EvidenceList items={verification.evidence_refs} title="独立验证证据" />
              </article>)}
            </section>
          })}

          {detail?.context?.applications.map(application => <section className={styles.applicationCard} key={application.application_id}>
            <header><Workflow size={16} /><div><span>可编辑交付物</span><strong>{application.name}</strong></div><Link href={`/applications/${application.application_id}`}>打开工作流</Link></header>
            {application.description && <p>{application.description}</p>}
            <dl className={styles.factGrid}>
              <div><dt>草稿</dt><dd>r{application.draft.revision}</dd></div>
              <div><dt>证据</dt><dd className={statusTone(application.draft.evidence_state)}>{application.draft.evidence_state}</dd></div>
              <div><dt>结构</dt><dd>{application.draft.node_count} 节点 · {application.draft.edge_count} 连线</dd></div>
              <div><dt>测试</dt><dd>{application.draft.tests_passed} 通过 · {application.draft.tests_failed} 失败</dd></div>
            </dl>
            {application.runs.length > 0 && <div className={styles.runList}><span>最近运行</span>{application.runs.map(run => <details key={run.run_id}><summary><Play size={13} /><strong>{shortId(run.run_id)}</strong><b className={statusTone(run.status)}>{run.status}</b><time>{displayTime(run.updated_at || run.created_at)}</time></summary>{run.error && <p className={styles.runError}>{run.error}</p>}{run.trace.length ? <ol>{run.trace.map(event => <li key={`${run.run_id}-${event.seq}`}><span>{event.title || event.event_type}</span><b className={statusTone(event.status)}>{event.status}</b></li>)}</ol> : <p>此运行没有公开 trace 事件。</p>}</details>)}</div>}
          </section>)}

          {exported && <section className={styles.exportSummary}>
            <FileCheck2 size={16} />
            <div><strong>因果证据导出已校验</strong><span>{exported.counters.messages} 条消息 · {exported.counters.reports} 份报告 · {exported.counters.claims} 项声明</span><code>{shortId(exported.digest, 14)}</code></div>
          </section>}
        </div>
      </aside>
    </section>

    {dialog === 'auto' && <ConfirmDialog busy={Boolean(channel && pendingOperations.has(`mode:${channel.channel_id}:${channel.revision}:auto_forward`))} confirmLabel="确认开启自动转发" onClose={() => setDialog(null)} onConfirm={() => void changeApprovalMode('auto_forward')} title="开启自动转发？">
      <p>开启后，后续满足合同和证据条件的平台能力/缺陷报告会直接发给隔离的 Codex 开发通道，不再逐次等待你点击批准。</p>
      <ul><li>运行权限仍会单独询问“允许一次/拒绝”。</li><li>Codex 只接收批准范围内的报告，不会进入 Customer Runtime。</li><li>你可以随时切回逐次审批。</li></ul>
    </ConfirmDialog>}

    {dialog === 'stop' && <ConfirmDialog busy={Boolean(channel && (pendingOperations.has(`stop-assignment:${assignment?.assignment_id || channel.assignment_id}`) || pendingOperations.has(`close-channel:${channel.channel_id}:${channel.revision}`)))} confirmLabel="停止并关闭通道" danger onClose={() => setDialog(null)} onConfirm={() => void stopTask()} title="停止当前莉莉丝任务？">
      <p>这会先取消 Assignment，再关闭协作通道。已经写入的草稿、报告和证据会保留，但莉莉丝不会继续执行剩余步骤。</p>
    </ConfirmDialog>}
  </main>
}
