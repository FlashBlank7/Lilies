'use client'

import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowLeft,
  Check,
  CircleAlert,
  Clock3,
  LoaderCircle,
  LockKeyhole,
  MessageSquareMore,
  Play,
  RefreshCw,
  Square,
  Workflow,
} from 'lucide-react'
import { MarkdownResultCard } from '@/lib/markdown'
import { api, isAuthError, saveClientToken, type Draft } from '@/lib/platform'
import styles from './runtime.module.css'
import responsive from './runtime-responsive.module.css'


type ApplicationRecord = {
  id: string
  name: string
  description: string
  requirement: string
  active_version?: number | null
}

type RunRecord = {
  id: string
  status: 'queued' | 'running' | 'paused' | 'succeeded' | 'failed' | 'cancelled'
  outputs: Record<string, unknown>
  error?: string | null
  state: {
    snapshot?: Draft['snapshot']
    waiting_node_id?: string | null
    completed?: string[]
    skipped?: string[]
  }
  created_at?: string
  updated_at?: string
}

type RuntimeDefinition = {
  application_id: string
  source: 'published' | 'draft'
  version?: number | null
  draft_revision?: number | null
  content_hash: string
  snapshot: Draft['snapshot']
}

type StoredEvent = {
  id: number
  type: string
  data: Record<string, unknown>
  created_at: string
}

type RuntimeField = {
  name: string
  label: string
  description: string
  type: string
  required: boolean
  value: string
  checked: boolean
}

type PermissionRequest = {
  request_id: string
  session_id: string
  tool?: string
  node_id?: string
}

type RuntimeStep = {
  id: string
  title: string
  description: string
  nodeIds: string[]
}

type RuntimeStepStatus = 'idle' | 'pending' | 'running' | 'waiting' | 'completed' | 'skipped' | 'failed'

const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'paused', 'cancelled'])
const HIDDEN_STEP_TYPES = new Set([
  'start',
  'schedule_trigger',
  'end',
  'event_recorder',
  'checkpoint_resume',
  'cancellation_point',
])
const STEP_PHASES: Record<string, { id: string; title: string; description: string }> = {
  context_assembler: { id: 'prepare', title: '准备任务信息', description: '整理你的输入和完成任务所需的背景信息。' },
  workspace_context_injector: { id: 'prepare', title: '准备任务信息', description: '整理你的输入和完成任务所需的背景信息。' },
  skill_loader: { id: 'prepare', title: '准备任务信息', description: '整理你的输入和完成任务所需的背景信息。' },
  capability_registry: { id: 'prepare', title: '准备任务信息', description: '整理你的输入和完成任务所需的背景信息。' },
  conversation_memory: { id: 'prepare', title: '准备任务信息', description: '整理你的输入和完成任务所需的背景信息。' },
  context_compactor: { id: 'prepare', title: '准备任务信息', description: '整理你的输入和完成任务所需的背景信息。' },
  model_turn: { id: 'process', title: '理解并处理请求', description: '分析需求，逐步形成可交付的结果。' },
  llm: { id: 'process', title: '理解并处理请求', description: '分析需求，逐步形成可交付的结果。' },
  loop: { id: 'process', title: '理解并处理请求', description: '分析需求，逐步形成可交付的结果。' },
  stop_continue_controller: { id: 'process', title: '理解并处理请求', description: '分析需求，逐步形成可交付的结果。' },
  retry_error_classifier: { id: 'process', title: '理解并处理请求', description: '分析需求，逐步形成可交付的结果。' },
  round_limit: { id: 'process', title: '理解并处理请求', description: '分析需求，逐步形成可交付的结果。' },
  mcp_gateway: { id: 'operate', title: '获取信息或执行操作', description: '在允许的范围内调用完成任务所需的服务。' },
  tool_call_router: { id: 'operate', title: '获取信息或执行操作', description: '在允许的范围内调用完成任务所需的服务。' },
  tool_executor: { id: 'operate', title: '获取信息或执行操作', description: '在允许的范围内调用完成任务所需的服务。' },
  tool_result_normalizer: { id: 'operate', title: '获取信息或执行操作', description: '在允许的范围内调用完成任务所需的服务。' },
  tool: { id: 'operate', title: '获取信息或执行操作', description: '在允许的范围内调用完成任务所需的服务。' },
  http_request: { id: 'operate', title: '获取信息或执行操作', description: '在允许的范围内调用完成任务所需的服务。' },
  permission_gate: { id: 'safety', title: '确认安全边界', description: '需要时等待你的批准，再继续受保护的操作。' },
  sandbox_boundary: { id: 'safety', title: '确认安全边界', description: '需要时等待你的批准，再继续受保护的操作。' },
  budget_gate: { id: 'safety', title: '确认安全边界', description: '需要时等待你的批准，再继续受保护的操作。' },
  dependency_gate: { id: 'safety', title: '确认安全边界', description: '需要时等待你的批准，再继续受保护的操作。' },
  subagent_spawn: { id: 'collaborate', title: '协同完成子任务', description: '把可并行的部分交给协作执行单元并汇总进展。' },
  task_dispatcher: { id: 'collaborate', title: '协同完成子任务', description: '把可并行的部分交给协作执行单元并汇总进展。' },
  mailbox_wait_wake: { id: 'collaborate', title: '协同完成子任务', description: '把可并行的部分交给协作执行单元并汇总进展。' },
  template_transform: { id: 'result', title: '整理结果', description: '把处理结果整理成便于阅读和继续使用的形式。' },
  answer: { id: 'result', title: '整理结果', description: '把处理结果整理成便于阅读和继续使用的形式。' },
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function text(value: unknown, fallback = '') {
  return typeof value === 'string' ? value : fallback
}

function inputValue(value: unknown, type: string) {
  if (type === 'object' || type === 'array' || type === 'file_list') {
    return value === undefined || value === null ? '' : JSON.stringify(value, null, 2)
  }
  return value === undefined || value === null ? '' : String(value)
}

function runtimeFields(snapshot: Draft['snapshot'] | null): RuntimeField[] {
  const trigger = snapshot?.workflow.nodes.find(node => node.type === 'start')
    || snapshot?.workflow.nodes.find(node => node.type === 'schedule_trigger')
  if (!trigger) return []
  const config = asRecord(trigger.config)
  const settings = asRecord(config.settings)
  const rawInputs = Array.isArray(settings.inputs) ? settings.inputs : Array.isArray(config.inputs) ? config.inputs : []
  return rawInputs.map((value, index) => {
    const field = asRecord(value)
    const name = text(field.name, `input_${index + 1}`)
    const type = text(field.type, 'string')
    const defaultValue = field.default
    return {
      name,
      label: text(field.label, text(field.title, name.replaceAll('_', ' '))),
      description: text(field.description),
      type,
      required: field.required !== false,
      value: inputValue(defaultValue, type),
      checked: Boolean(defaultValue),
    }
  })
}

function parseInputs(fields: RuntimeField[]) {
  const values: Record<string, unknown> = {}
  for (const field of fields) {
    const raw = field.type === 'boolean' ? field.checked : field.value.trim()
    if (field.required && (raw === '' || raw === undefined)) {
      throw new Error(`请填写“${field.label}”后再启动。`)
    }
    if (raw === '' || raw === undefined) continue
    if (field.type === 'number') {
      const number = Number(raw)
      if (!Number.isFinite(number)) throw new Error(`“${field.label}”需要填写数字。`)
      values[field.name] = number
    } else if (field.type === 'object' || field.type === 'array' || field.type === 'file_list') {
      try {
        values[field.name] = JSON.parse(String(raw))
      } catch {
        throw new Error(`“${field.label}”的内容格式无法识别，请检查括号和引号。`)
      }
    } else {
      values[field.name] = raw
    }
  }
  return values
}

function resultText(value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(item => resultText(item)).filter(Boolean).join('\n\n')
  const record = asRecord(value)
  for (const key of ['answer', 'text', 'result', 'summary', 'output', 'content']) {
    if (record[key] !== undefined) {
      const resolved = resultText(record[key])
      if (resolved) return resolved
    }
  }
  if (Object.keys(record).length) {
    return Object.entries(record)
      .map(([key, item]) => `### ${key.replaceAll('_', ' ')}\n\n${resultText(item) || String(item)}`)
      .join('\n\n')
  }
  return value === undefined || value === null ? '' : String(value)
}

function runResultMarkdown(run: RunRecord | null) {
  if (!run) return ''
  const values = Object.values(run.outputs || {})
  for (const value of values.reverse()) {
    const rendered = resultText(value)
    if (rendered) return rendered
  }
  return run.error ? `## 运行未完成\n\n${run.error}` : ''
}

function eventNodeMatches(event: StoredEvent, nodeId: string) {
  const eventNode = text(event.data.node_id)
  return eventNode === nodeId || eventNode.endsWith(`.${nodeId}`) || eventNode.endsWith(`/${nodeId}`)
}

function nodeStatus(nodeId: string, run: RunRecord | null, events: StoredEvent[]): RuntimeStepStatus {
  if (!run) return 'idle'
  if (run.state.completed?.includes(nodeId)) return 'completed'
  if (run.state.skipped?.includes(nodeId)) return 'skipped'
  if (run.state.waiting_node_id === nodeId) return 'waiting'
  const related = events.filter(event => eventNodeMatches(event, nodeId))
  if (related.some(event => event.type.includes('failed'))) return 'failed'
  if (related.some(event => event.type.includes('completed'))) return 'completed'
  if (related.some(event => event.type.includes('started'))) return 'running'
  return run.status === 'succeeded' ? 'completed' : 'pending'
}

function stepStatus(step: RuntimeStep, run: RunRecord | null, events: StoredEvent[]) {
  const statuses = step.nodeIds.map(nodeId => nodeStatus(nodeId, run, events))
  if (!statuses.length) return run?.status === 'succeeded' ? 'completed' : run ? 'pending' : 'idle'
  for (const status of ['failed', 'waiting', 'running'] as const) {
    if (statuses.includes(status)) return status
  }
  if (statuses.every(status => status === 'completed' || status === 'skipped')) {
    return statuses.every(status => status === 'skipped') ? 'skipped' : 'completed'
  }
  return run ? 'pending' : 'idle'
}

function customerSteps(snapshot: Draft['snapshot'] | null): RuntimeStep[] {
  const result: RuntimeStep[] = []
  const phaseIndex = new Map<string, number>()
  for (const node of snapshot?.workflow.nodes || []) {
    if (HIDDEN_STEP_TYPES.has(node.type)) continue
    const phase = STEP_PHASES[node.type]
    if (!phase) {
      result.push({
        id: `node:${node.id}`,
        title: node.title,
        description: node.description || `完成“${node.title}”并把结果交给下一步。`,
        nodeIds: [node.id],
      })
      continue
    }
    const existingIndex = phaseIndex.get(phase.id)
    if (existingIndex !== undefined) {
      result[existingIndex].nodeIds.push(node.id)
      continue
    }
    phaseIndex.set(phase.id, result.length)
    result.push({ ...phase, nodeIds: [node.id] })
  }
  if (!result.length && snapshot) {
    result.push({
      id: 'workflow',
      title: '处理你的请求',
      description: snapshot.description || snapshot.requirement,
      nodeIds: [],
    })
  }
  return result
}

function runStatusLabel(status?: RunRecord['status']) {
  if (status === 'queued') return '等待启动'
  if (status === 'running') return '正在运行'
  if (status === 'paused') return '等待回答'
  if (status === 'succeeded') return '已完成'
  if (status === 'failed') return '需要处理'
  if (status === 'cancelled') return '已停止'
  return '可以启动'
}

function recoveryMessage(run: RunRecord | null) {
  if (!run?.error) return ''
  const value = run.error.toLowerCase()
  if (value.includes('permission') || value.includes('denied')) return '这一步需要额外授权。确认允许的操作后重新启动即可。'
  if (value.includes('timeout') || value.includes('network')) return '外部服务暂时没有响应。稍后重试；原输入仍可继续使用。'
  if (value.includes('missing required input')) return '有必填内容尚未提供。补充上方输入后重新启动。'
  if (value.includes('interrupted')) return '运行被服务重启中断。重新启动会创建一条新的运行记录。'
  return '请检查上方输入后重试。问题仍然存在时，请联系工作流维护人员处理。'
}

export default function CustomerRuntimePage() {
  const params = useParams<{ id: string }>()
  const id = String(params.id)
  const [application, setApplication] = useState<ApplicationRecord | null>(null)
  const [definition, setDefinition] = useState<RuntimeDefinition | null>(null)
  const [fields, setFields] = useState<RuntimeField[]>([])
  const [run, setRun] = useState<RunRecord | null>(null)
  const [events, setEvents] = useState<StoredEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)
  const [actionPending, setActionPending] = useState('')
  const [error, setError] = useState('')
  const [authNeeded, setAuthNeeded] = useState(false)
  const [accessKey, setAccessKey] = useState('')
  const [resumeValue, setResumeValue] = useState('')
  const pollRef = useRef<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [nextApplication, nextDefinition, recentRuns] = await Promise.all([
        api<ApplicationRecord>(`/api/v1/applications/${id}`),
        api<RuntimeDefinition>(`/api/v1/applications/${id}/runtime-definition`),
        api<RunRecord[]>(`/api/v1/applications/${id}/runs?limit=1`),
      ])
      setApplication(nextApplication)
      setDefinition(nextDefinition)
      setFields(runtimeFields(nextDefinition.snapshot))
      const latestRun = recentRuns[0] || null
      setRun(latestRun)
      setEvents(latestRun ? await api<StoredEvent[]>(`/v1/streams/${latestRun.id}`) : [])
      setAuthNeeded(false)
    } catch (caught) {
      if (isAuthError(caught)) setAuthNeeded(true)
      else setError(String(caught))
    } finally {
      setLoading(false)
    }
  }, [id])

  const refreshRun = useCallback(async (runId: string) => {
    const [nextRun, nextEvents] = await Promise.all([
      api<RunRecord>(`/api/v1/runs/${runId}`),
      api<StoredEvent[]>(`/v1/streams/${runId}`),
    ])
    setRun(nextRun)
    setEvents(nextEvents)
    if (TERMINAL_STATUSES.has(nextRun.status) && pollRef.current) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  const watchRun = useCallback((runId: string) => {
    if (pollRef.current) window.clearInterval(pollRef.current)
    void refreshRun(runId).catch(caught => setError(String(caught)))
    pollRef.current = window.setInterval(() => {
      void refreshRun(runId).catch(caught => setError(String(caught)))
    }, 1200)
  }, [refreshRun])

  useEffect(() => {
    void load()
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current)
    }
  }, [load])

  useEffect(() => {
    if (!run || TERMINAL_STATUSES.has(run.status) || pollRef.current) return
    watchRun(run.id)
  }, [run, watchRun])

  const displaySnapshot = run?.state.snapshot || definition?.snapshot || null

  const steps = useMemo(() => {
    return customerSteps(displaySnapshot).map((step, index) => ({
      ...step,
      index: index + 1,
      status: stepStatus(step, run, events),
    }))
  }, [displaySnapshot, events, run])

  const currentStep = steps.find(item => ['running', 'waiting', 'failed'].includes(item.status))
  const completedCount = steps.filter(item => ['completed', 'skipped'].includes(item.status)).length
  const resultMarkdown = useMemo(() => runResultMarkdown(run), [run])
  const pendingPermission = useMemo<PermissionRequest | null>(() => {
    for (const event of [...events].reverse()) {
      if (event.type !== 'permission.requested') continue
      const requestId = text(event.data.request_id)
      const sessionId = text(event.data.session_id)
      if (!requestId || !sessionId) continue
      const resolved = events.some(candidate => candidate.id > event.id && candidate.type === 'permission.resolved' && candidate.data.request_id === requestId)
      if (!resolved) return { request_id: requestId, session_id: sessionId, tool: text(event.data.tool), node_id: text(event.data.node_id) }
    }
    return null
  }, [events])

  async function startRun() {
    setStarting(true)
    setError('')
    try {
      const inputs = parseInputs(fields)
      const result = await api<{ run_id: string }>(`/api/v1/applications/${id}/runs`, {
        method: 'POST',
        body: JSON.stringify({
          inputs,
          use_draft: definition?.source !== 'published',
          workspace_path: '.',
        }),
      })
      const nextRun: RunRecord = { id: result.run_id, status: 'queued', outputs: {}, state: {} }
      setRun(nextRun)
      setEvents([])
      watchRun(result.run_id)
    } catch (caught) {
      setError(String(caught).replace(/^Error:\s*/, ''))
    } finally {
      setStarting(false)
    }
  }

  async function cancelRun() {
    if (!run || !window.confirm('确定停止这次运行吗？已经完成的步骤会保留在记录中。')) return
    setActionPending('cancel')
    setError('')
    try {
      await api(`/api/v1/runs/${run.id}/cancel`, { method: 'POST' })
      watchRun(run.id)
    } catch (caught) {
      setError(String(caught).replace(/^Error:\s*/, ''))
    } finally {
      setActionPending('')
    }
  }

  async function resolvePermission(behavior: 'allow' | 'deny') {
    if (!pendingPermission) return
    setActionPending(`permission-${behavior}`)
    setError('')
    try {
      await api(`/v1/sessions/${pendingPermission.session_id}/permissions/${pendingPermission.request_id}`, {
        method: 'POST',
        body: JSON.stringify({ behavior }),
      })
      if (run) watchRun(run.id)
    } catch (caught) {
      setError(String(caught).replace(/^Error:\s*/, ''))
    } finally {
      setActionPending('')
    }
  }

  async function resumeRun() {
    if (!run) return
    let values: Record<string, unknown>
    try {
      const parsed = JSON.parse(resumeValue)
      values = asRecord(parsed)
      if (!Object.keys(values).length) values = { answer: resumeValue }
    } catch {
      values = { answer: resumeValue }
    }
    setActionPending('resume')
    setError('')
    try {
      await api(`/api/v1/runs/${run.id}/resume`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      watchRun(run.id)
    } catch (caught) {
      setError(String(caught).replace(/^Error:\s*/, ''))
    } finally {
      setActionPending('')
    }
  }

  function connect() {
    saveClientToken(accessKey)
    void load()
  }

  const running = run?.status === 'queued' || run?.status === 'running'

  return <main className={`${styles.shell} ${responsive.shell}`} data-customer-runtime="true">
    <header className={styles.header}>
      <Link className={styles.iconLink} href="/" aria-label="返回应用列表" title="返回应用列表"><ArrowLeft size={18} /></Link>
      <div className={styles.identity}>
        <span>Customer Runtime</span>
        <strong>{application?.name || '工作流运行'}</strong>
      </div>
    </header>

    {loading ? <section className={styles.centerState}><LoaderCircle className={styles.spin} size={24} /><strong>正在准备工作流</strong></section> : authNeeded ? <section className={styles.centerState}>
      <LockKeyhole size={26} />
      <strong>需要访问密钥</strong>
      <p>输入团队提供的访问密钥后继续。</p>
      <div className={styles.authRow}><input type="password" value={accessKey} onChange={event => setAccessKey(event.target.value)} placeholder="访问密钥" /><button onClick={connect} disabled={!accessKey.trim()}><Check size={16} />连接</button></div>
    </section> : <div className={`${styles.layout} ${responsive.layout}`}>
      <section className={styles.mainColumn}>
        <div className={styles.introBand}>
          <div className={styles.introIcon}><Workflow size={24} /></div>
          <div className={responsive.introContent}><span>工作流用途</span><h1>{displaySnapshot?.name || application?.name}</h1><p>{displaySnapshot?.description || application?.description || displaySnapshot?.requirement}</p></div>
          <div className={styles.runState} data-run-status={run?.status || 'ready'}><i />{runStatusLabel(run?.status)}</div>
        </div>

        <section className={styles.inputSection} aria-labelledby="runtime-input-title">
          <div className={styles.sectionHeading}><div><span>01</span><div><h2 id="runtime-input-title">开始这次运行</h2><p>{fields.length ? '填写你希望这次工作流处理的内容。' : '这个工作流不需要额外输入，可以直接启动。'}</p></div></div>{run && <button className={styles.iconButton} onClick={() => void refreshRun(run.id)} aria-label="刷新运行状态" title="刷新运行状态"><RefreshCw size={16} /></button>}</div>
          {fields.length > 0 && <div className={styles.formGrid}>{fields.map(field => <label className={styles.field} key={field.name}>
            <span>{field.label}{field.required && <b>必填</b>}</span>
            {field.description && <small>{field.description}</small>}
            {field.type === 'boolean' ? <input type="checkbox" checked={field.checked} onChange={event => setFields(current => current.map(item => item.name === field.name ? { ...item, checked: event.target.checked } : item))} /> : field.type === 'object' || field.type === 'array' || field.type === 'file_list' ? <textarea value={field.value} onChange={event => setFields(current => current.map(item => item.name === field.name ? { ...item, value: event.target.value } : item))} /> : <input type={field.type === 'number' ? 'number' : 'text'} value={field.value} onChange={event => setFields(current => current.map(item => item.name === field.name ? { ...item, value: event.target.value } : item))} />}
          </label>)}</div>}
          {error && <div className={styles.inlineError} role="alert"><CircleAlert size={17} /><div><strong>现在还不能继续</strong><span>{error}</span></div></div>}
          <div className={styles.primaryActions}><button className={styles.primaryButton} onClick={() => void startRun()} disabled={starting || running}><Play size={17} fill="currentColor" />{starting ? '正在启动' : running ? '正在运行' : '启动工作流'}</button>{running && <button className={styles.stopButton} disabled={actionPending === 'cancel'} onClick={() => void cancelRun()}><Square size={15} fill="currentColor" />{actionPending === 'cancel' ? '正在停止' : '停止'}</button>}</div>
        </section>

        <section className={styles.progressSection} aria-labelledby="runtime-progress-title">
          <div className={styles.sectionHeading}><div><span>02</span><div><h2 id="runtime-progress-title">处理进度</h2><p>{currentStep ? `当前：${currentStep.title}` : run?.status === 'succeeded' ? '所有步骤已完成。' : '启动后会在这里看到实时进度。'}</p></div></div><strong>{completedCount}/{steps.length}</strong></div>
          <div className={styles.progressTrack}><i style={{ width: `${steps.length ? (completedCount / steps.length) * 100 : 0}%` }} /></div>
          <ol className={styles.stepList}>{steps.map(item => <li className={styles[item.status] || ''} data-step-status={item.status} key={item.id}>
            <span>{item.status === 'completed' || item.status === 'skipped' ? <Check size={14} /> : item.status === 'running' ? <LoaderCircle className={styles.spin} size={14} /> : item.status === 'waiting' ? <MessageSquareMore size={14} /> : item.status === 'failed' ? <CircleAlert size={14} /> : item.index}</span>
            <div><strong>{item.title}</strong><small>{item.description}</small></div>
            <b>{item.status === 'completed' ? '完成' : item.status === 'skipped' ? '无需执行' : item.status === 'running' ? '处理中' : item.status === 'waiting' ? '等待回答' : item.status === 'failed' ? '未完成' : item.status === 'pending' ? '待处理' : '尚未启动'}</b>
          </li>)}</ol>
        </section>

        {pendingPermission && <section className={styles.approvalSection}>
          <LockKeyhole size={21} />
          <div><h2>需要你的批准</h2><p>工作流准备执行一项受保护操作{pendingPermission.tool ? `：${pendingPermission.tool}` : ''}。只有批准后才会继续。</p></div>
          <div><button disabled={actionPending.startsWith('permission-')} onClick={() => void resolvePermission('allow')}><Check size={16} />{actionPending === 'permission-allow' ? '正在提交' : '批准并继续'}</button><button className={styles.secondaryButton} disabled={actionPending.startsWith('permission-')} onClick={() => void resolvePermission('deny')}><Square size={14} />{actionPending === 'permission-deny' ? '正在提交' : '拒绝'}</button></div>
        </section>}

        {run?.status === 'paused' && <section className={styles.approvalSection}>
          <MessageSquareMore size={21} />
          <div><h2>工作流需要补充信息</h2><p>回答后将从当前步骤继续，不会从头重来。</p><textarea value={resumeValue} onChange={event => setResumeValue(event.target.value)} placeholder="输入补充说明" /></div>
          <button onClick={() => void resumeRun()} disabled={!resumeValue.trim() || actionPending === 'resume'}><Play size={16} />{actionPending === 'resume' ? '正在继续' : '继续运行'}</button>
        </section>}

        <section className={styles.resultSection} aria-labelledby="runtime-result-title">
          <div className={styles.sectionHeading}><div><span>03</span><div><h2 id="runtime-result-title">本次结果</h2><p>{run?.status === 'succeeded' ? '结果已经整理完成。' : run?.status === 'failed' ? '这次运行没有完成，下面给出了恢复建议。' : '工作流完成后，结果会显示在这里。'}</p></div></div>{run?.updated_at && <time><Clock3 size={14} />{new Date(run.updated_at).toLocaleTimeString()}</time>}</div>
          {resultMarkdown ? <MarkdownResultCard source={resultMarkdown} emptyLabel="暂无结果" title="工作流结果" description="已按可读格式整理" openLabel="展开阅读" closeLabel="关闭" dataSurface="customer-runtime-result" /> : <div className={styles.emptyResult}><Workflow size={24} /><span>{running ? '正在生成结果' : '尚无运行结果'}</span></div>}
          {run?.status === 'failed' && <div className={styles.recovery}><CircleAlert size={18} /><div><strong>建议这样处理</strong><p>{recoveryMessage(run)}</p></div><button onClick={() => void startRun()}><RefreshCw size={15} />重新运行</button></div>}
        </section>
      </section>

      <aside className={styles.summaryColumn}>
        <div className={styles.summaryHeader}><span>本次运行</span><strong>{run ? run.id.slice(0, 8) : '尚未开始'}</strong></div>
        <dl><div><dt>状态</dt><dd>{runStatusLabel(run?.status)}</dd></div><div><dt>步骤</dt><dd>{steps.length}</dd></div><div><dt>已完成</dt><dd>{completedCount}</dd></div><div><dt>结果</dt><dd>{resultMarkdown ? '已生成' : '等待中'}</dd></div></dl>
        <p>需要修改处理方式时，请联系工作流维护人员。</p>
      </aside>
    </div>}
  </main>
}
