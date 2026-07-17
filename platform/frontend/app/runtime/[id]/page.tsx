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
import { ScheduleOperationsPanel } from '@/app/schedule-operations-panel'
import styles from './runtime.module.css'
import responsive from './runtime-responsive.module.css'
import connectorStyles from './connector-runtime.module.css'


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

class RuntimeInputError extends Error {
  constructor(readonly fieldName: string, message: string) {
    super(message)
    this.name = 'RuntimeInputError'
  }
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
  'variable_assigner',
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
  connector_action: { id: 'operate', title: '读取或预演客户系统操作', description: '在测试租户边界内校验请求并返回可追踪回执。' },
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

function connectorStatusLabel(value: unknown) {
  const status = text(value, 'unknown')
  if (status === 'dry_run') return '仅预演（未写入）'
  if (status === 'succeeded') return '已完成'
  if (status === 'failed') return '未完成'
  if (status === 'compensated') return '已撤销'
  if (status === 'executing') return '处理中'
  return '状态待确认'
}

function connectorSideEffectLabel(value: unknown) {
  const state = text(value, 'unknown')
  if (state === 'none') return '未产生'
  if (state === 'applied') return '已写入'
  if (state === 'compensated') return '已撤销'
  return '状态待确认'
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
  const connectorWorkflow = Boolean(snapshot?.workflow.nodes.some(node => node.type === 'connector_action'))
  const internalConnectorInputs = new Set([
    'tenant_id',
    'actor_id',
    'actor_roles',
    'connector_profile_id',
    'connector_authorization_id',
    'connector_idempotency_key',
    'write_mode',
  ])
  return rawInputs.filter(value => {
    const name = text(asRecord(value).name)
    return !connectorWorkflow || !internalConnectorInputs.has(name)
  }).map((value, index) => {
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

function connectorReceipt(run: RunRecord | null): Record<string, unknown> | null {
  const visit = (value: unknown): Record<string, unknown> | null => {
    const item = asRecord(value)
    if (typeof item.execution_id === 'string' && typeof item.side_effect_state === 'string') return item
    for (const child of Object.values(item)) {
      const found = visit(child)
      if (found) return found
    }
    return null
  }
  for (const value of Object.values(run?.outputs || {}).reverse()) {
    const found = visit(value)
    if (found) return found
  }
  return null
}

function parseInputs(fields: RuntimeField[]) {
  const values: Record<string, unknown> = {}
  for (const field of fields) {
    const raw = field.type === 'boolean' ? field.checked : field.value.trim()
    if (field.required && (raw === '' || raw === undefined)) {
      throw new RuntimeInputError(field.name, `请填写“${field.label}”后再启动。`)
    }
    if (raw === '' || raw === undefined) continue
    if (field.type === 'number') {
      const number = Number(raw)
      if (!Number.isFinite(number)) throw new RuntimeInputError(field.name, `“${field.label}”需要填写数字。`)
      values[field.name] = number
    } else if (field.type === 'object' || field.type === 'array' || field.type === 'file_list') {
      try {
        values[field.name] = JSON.parse(String(raw))
      } catch {
        throw new RuntimeInputError(field.name, `“${field.label}”的内容格式无法识别，请检查括号和引号。`)
      }
    } else {
      values[field.name] = raw
    }
  }
  return values
}

const RESULT_FIELD_LABELS: Record<string, string> = {
  answer: '回答',
  result: '结果',
  summary: '摘要',
  output: '输出',
  content: '内容',
  classification: '分类结果',
  urgency_level: '紧急程度',
  urgency: '紧急程度',
  urgent_level: '紧急程度',
  emergency_level: '紧急程度',
  priority: '紧急程度',
  priority_level: '紧急程度',
  severity: '紧急程度',
  severity_level: '紧急程度',
  category: '问题类型',
  complaint_type: '问题类型',
  issue_category: '问题类型',
  issue_kind: '问题类型',
  issue_type: '问题类型',
  problem_type: '问题类型',
  problem_category: '问题类型',
  issue_detail: '问题详情',
  reply: '回复建议',
  response: '回复建议',
  reply_suggestion: '回复建议',
  suggested_reply: '回复建议',
  recommended_reply: '回复建议',
  customer_reply: '回复建议',
  reason: '说明',
  reasoning: '判断依据',
  rationale: '判断依据',
  justification: '判断依据',
  analysis: '判断依据',
  urgency_reason: '紧急程度说明',
  reply_rationale: '回复理由',
  next_step: '下一步',
  next_steps: '下一步',
  next_action: '下一步',
  recommended_action: '下一步',
  recommended_next_step: '下一步',
  follow_up: '下一步',
  follow_up_action: '下一步',
  trace_log: '处理轨迹',
  step_log: '处理步骤',
  process_log: '处理步骤',
  execution_log: '处理步骤',
  trace: '处理步骤',
  steps: '处理步骤',
  execution_steps: '处理步骤',
  step: '步骤',
  input: '输入',
  input_summary: '输入摘要',
  output_summary: '步骤结果',
  description: '步骤说明',
  action: '处理内容',
  status: '状态',
}

const RESULT_WRAPPER_FIELDS = new Set(['answer', 'text', 'result', 'summary', 'output', 'content'])
const RESULT_METADATA_FIELDS = new Set([
  'usage',
  'state',
  'branch',
  'iterations',
  'session_id',
  'tool_calls',
  'tool_use_blocks',
  'node',
  'node_id',
  'stop_reason',
  'cancelled',
  'degraded',
  'fallback_used',
])

function normalizedResultFieldKey(key: string) {
  return key
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .replace(/[\s-]+/g, '_')
    .toLocaleLowerCase()
}

function resultFieldLabel(key: string) {
  const normalized = normalizedResultFieldKey(key)
  const knownLabel = RESULT_FIELD_LABELS[normalized]
  if (knownLabel) return knownLabel
  if (/(?:urgency|urgent|emergency|priority|severity)/.test(normalized)) return '紧急程度'
  if (/(?:issue|problem|complaint).*(?:type|category|kind)|^(?:category|type)$/.test(normalized)) return '问题类型'
  if (/(?:reply|response)/.test(normalized)) return '回复建议'
  if (/(?:reason|rationale|justification|explanation)/.test(normalized)) return '判断依据'
  if (/(?:next|follow_up|recommended).*(?:step|action)/.test(normalized)) return '下一步'
  if (/(?:step|process|execution).*(?:log|trace)|^trace$/.test(normalized)) return '处理步骤'
  return key.replaceAll('_', ' ').replaceAll('-', ' ')
}

function escapeJsonStringControlCharacters(value: string) {
  let result = ''
  let inString = false
  let escaped = false
  for (const character of value) {
    if (!inString) {
      result += character
      if (character === '"') inString = true
      continue
    }
    if (escaped) {
      result += character
      escaped = false
      continue
    }
    if (character === '\\') {
      result += character
      escaped = true
      continue
    }
    if (character === '"') {
      result += character
      inString = false
      continue
    }
    if (character === '\n') result += '\\n'
    else if (character === '\r') result += '\\r'
    else if (character === '\t') result += '\\t'
    else if (character.charCodeAt(0) < 0x20) result += `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`
    else result += character
  }
  return result
}

function serializedStructure(value: string) {
  const trimmed = value.trim()
  if (!(trimmed.startsWith('{') && trimmed.endsWith('}')) && !(trimmed.startsWith('[') && trimmed.endsWith(']'))) return null
  try {
    const parsed: unknown = JSON.parse(trimmed)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    try {
      const parsed: unknown = JSON.parse(escapeJsonStringControlCharacters(trimmed))
      return parsed && typeof parsed === 'object' ? parsed : null
    } catch {
      return null
    }
  }
}

function resultText(value: unknown, depth = 0): string {
  if (typeof value === 'string') {
    const structured = serializedStructure(value)
    return structured ? resultText(structured, depth) : value
  }
  if (Array.isArray(value)) return value.map(item => resultText(item, depth + 1)).filter(Boolean).join('\n\n')
  const record = asRecord(value)
  const entries = Object.entries(record)
  const structuredEntry = entries.find(([key]) => normalizedResultFieldKey(key) === 'structured')
  if (structuredEntry) {
    const structured = resultText(structuredEntry[1], depth)
    if (structured) return structured
  }
  const businessEntries = entries.filter(([key]) => !RESULT_METADATA_FIELDS.has(normalizedResultFieldKey(key)))
  const visibleEntries = businessEntries.length ? businessEntries : entries
  if (visibleEntries.length === 1 && RESULT_WRAPPER_FIELDS.has(normalizedResultFieldKey(visibleEntries[0][0]))) {
    return resultText(visibleEntries[0][1], depth)
  }
  if (visibleEntries.length) {
    const heading = '#'.repeat(Math.min(depth + 2, 4))
    return visibleEntries
      .map(([key, item]) => `${heading} ${resultFieldLabel(key)}\n\n${resultText(item, depth + 1) || String(item)}`)
      .join('\n\n')
  }
  return value === undefined || value === null ? '' : String(value)
}

function runResultMarkdown(run: RunRecord | null) {
  if (!run) return ''
  if (run.status === 'failed') return ''
  return resultText(run.outputs || {})
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

function resultAvailabilityLabel(run: RunRecord | null, resultMarkdown: string) {
  if (resultMarkdown) return '已生成'
  if (run?.status === 'failed') return '未生成'
  if (run?.status === 'cancelled') return '已停止'
  return '等待中'
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

function customerErrorMessage(error: unknown) {
  if (error instanceof Error) return error.message
  return String(error).replace(/^(?:Error|RuntimeInputError):\s*/, '')
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
  const [invalidFieldName, setInvalidFieldName] = useState('')
  const [authNeeded, setAuthNeeded] = useState(false)
  const [accessKey, setAccessKey] = useState('')
  const [resumeValue, setResumeValue] = useState('')
  const pollRef = useRef<number | null>(null)
  const loadGenerationRef = useRef(0)
  const startRunLockedRef = useRef(false)
  const runtimeInputRefs = useRef(new Map<string, HTMLInputElement | HTMLTextAreaElement>())

  const load = useCallback(async () => {
    const generation = loadGenerationRef.current + 1
    loadGenerationRef.current = generation
    setLoading(true)
    setError('')
    setInvalidFieldName('')
    try {
      const [nextApplication, nextDefinition, recentRuns] = await Promise.all([
        api<ApplicationRecord>(`/api/v1/applications/${id}`),
        api<RuntimeDefinition>(`/api/v1/applications/${id}/runtime-definition`),
        api<RunRecord[]>(`/api/v1/applications/${id}/runs?limit=1`),
      ])
      const latestRun = recentRuns[0] || null
      const nextEvents = latestRun ? await api<StoredEvent[]>(`/v1/streams/${latestRun.id}`) : []
      if (generation !== loadGenerationRef.current) return
      setApplication(nextApplication)
      setDefinition(nextDefinition)
      setFields(runtimeFields(nextDefinition.snapshot))
      setRun(latestRun)
      setEvents(nextEvents)
      setAuthNeeded(false)
    } catch (caught) {
      if (generation !== loadGenerationRef.current) return
      if (isAuthError(caught)) setAuthNeeded(true)
      else setError(customerErrorMessage(caught))
    } finally {
      if (generation === loadGenerationRef.current) setLoading(false)
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
    void refreshRun(runId).catch(caught => setError(customerErrorMessage(caught)))
    pollRef.current = window.setInterval(() => {
      void refreshRun(runId).catch(caught => setError(customerErrorMessage(caught)))
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
  const purposeDescription = displaySnapshot?.capability_build_contract?.business_goal
    || displaySnapshot?.description
    || application?.description
    || displaySnapshot?.requirement
  const scheduledWorkflow = Boolean(
    displaySnapshot?.workflow.nodes.some(node => node.type === 'schedule_trigger'),
  )
  const connectorWorkflow = Boolean(
    displaySnapshot?.workflow.nodes.some(node => node.type === 'connector_action'),
  )

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
  const boundedConnectorReceipt = useMemo(() => connectorReceipt(run), [run])
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

  function updateRuntimeField(name: string, changes: Partial<Pick<RuntimeField, 'value' | 'checked'>>) {
    setFields(current => current.map(item => item.name === name ? { ...item, ...changes } : item))
    if (invalidFieldName === name) {
      setInvalidFieldName('')
      setError('')
    }
  }

  async function startRun() {
    if (startRunLockedRef.current) return
    startRunLockedRef.current = true
    setStarting(true)
    setError('')
    setInvalidFieldName('')
    try {
      const inputs = parseInputs(fields)
      const result = connectorWorkflow
        ? await api<{ run_id: string }>(`/api/v1/applications/${id}/connector-test-runs`, {
          method: 'POST',
          body: JSON.stringify({
            request: asRecord(inputs.request),
            use_draft: definition?.source !== 'published',
          }),
        })
        : await api<{ run_id: string }>(`/api/v1/applications/${id}/runs`, {
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
      if (caught instanceof RuntimeInputError) {
        setInvalidFieldName(caught.fieldName)
        window.requestAnimationFrame(() => {
          const input = runtimeInputRefs.current.get(caught.fieldName)
          input?.focus()
          input?.scrollIntoView({ behavior: 'smooth', block: 'center' })
        })
      }
      setError(customerErrorMessage(caught))
    } finally {
      startRunLockedRef.current = false
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
      setError(customerErrorMessage(caught))
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
      setError(customerErrorMessage(caught))
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
      setError(customerErrorMessage(caught))
    } finally {
      setActionPending('')
    }
  }

  function connect() {
    saveClientToken(accessKey)
    void load()
  }

  const running = run?.status === 'queued' || run?.status === 'running'

  const runtimeReady = !loading && !authNeeded && Boolean(application && displaySnapshot)

  return <main
    className={`${styles.shell} ${responsive.shell}`}
    data-customer-runtime="true"
    data-runtime-loading={loading ? 'true' : 'false'}
    data-runtime-ready={runtimeReady ? 'true' : 'false'}
    data-runtime-run-id={run?.id || ''}
  >
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
          <div className={responsive.introContent}><span>工作流用途</span><h1>{displaySnapshot?.name || application?.name}</h1><p data-runtime-purpose="true">{purposeDescription}</p>{connectorWorkflow && <b className={connectorStyles.boundary} data-customer-connector-view="bounded">受控测试租户 · 仅预演</b>}</div>
          <div className={styles.runState} data-run-status={run?.status || 'ready'}><i />{runStatusLabel(run?.status)}</div>
        </div>

        {scheduledWorkflow ? <ScheduleOperationsPanel
          applicationId={id}
          audience="customer"
          hasSchedule
          locale="zh"
          onAuthRequired={() => setAuthNeeded(true)}
        /> : <>
        {run?.status === 'failed' && <div className={styles.recovery}><CircleAlert size={18} /><div><strong>建议这样处理</strong><p>{recoveryMessage(run)}</p></div><button data-customer-runtime-action="retry" disabled={starting || running} onClick={() => void startRun()}><RefreshCw className={starting ? styles.spin : undefined} size={15} />{starting ? '正在重新运行' : '重新运行'}</button></div>}
        <section className={styles.inputSection} aria-labelledby="runtime-input-title">
          <div className={styles.sectionHeading}><div><span>01</span><div><h2 id="runtime-input-title">开始这次运行</h2><p>{connectorWorkflow ? '提交业务请求后，将在受控测试租户中完成读取与写回预演。' : fields.length ? '填写你希望这次工作流处理的内容。' : '这个工作流不需要额外输入，可以直接启动。'}</p></div></div>{run && <button className={styles.iconButton} onClick={() => void refreshRun(run.id)} aria-label="刷新运行状态" title="刷新运行状态"><RefreshCw size={16} /></button>}</div>
          {fields.length > 0 && <div className={styles.formGrid}>{fields.map(field => <label className={styles.field} data-runtime-input={field.name} data-runtime-invalid={invalidFieldName === field.name ? 'true' : 'false'} key={field.name}>
            <span>{field.label}{field.required && <b>必填</b>}</span>
            {field.description && <small>{field.description}</small>}
            {field.type === 'boolean' ? <input
              ref={element => { if (element) runtimeInputRefs.current.set(field.name, element); else runtimeInputRefs.current.delete(field.name) }}
              type="checkbox"
              checked={field.checked}
              aria-invalid={invalidFieldName === field.name ? 'true' : undefined}
              aria-describedby={invalidFieldName === field.name ? 'runtime-input-error' : undefined}
              onChange={event => updateRuntimeField(field.name, { checked: event.target.checked })}
            /> : field.type === 'object' || field.type === 'array' || field.type === 'file_list' ? <textarea
              ref={element => { if (element) runtimeInputRefs.current.set(field.name, element); else runtimeInputRefs.current.delete(field.name) }}
              value={field.value}
              aria-invalid={invalidFieldName === field.name ? 'true' : undefined}
              aria-describedby={invalidFieldName === field.name ? 'runtime-input-error' : undefined}
              onChange={event => updateRuntimeField(field.name, { value: event.target.value })}
            /> : <input
              ref={element => { if (element) runtimeInputRefs.current.set(field.name, element); else runtimeInputRefs.current.delete(field.name) }}
              type={field.type === 'number' ? 'number' : 'text'}
              value={field.value}
              aria-invalid={invalidFieldName === field.name ? 'true' : undefined}
              aria-describedby={invalidFieldName === field.name ? 'runtime-input-error' : undefined}
              onChange={event => updateRuntimeField(field.name, { value: event.target.value })}
            />}
          </label>)}</div>}
          {error && <div className={styles.inlineError} id="runtime-input-error" role="alert"><CircleAlert size={17} /><div><strong>现在还不能继续</strong><span>{error}</span></div></div>}
          {run?.status !== 'failed' && <div className={styles.primaryActions}><button className={styles.primaryButton} data-customer-runtime-action="start" onClick={() => void startRun()} disabled={starting || running}><Play size={17} fill="currentColor" />{starting ? '正在启动' : running ? '正在运行' : '启动工作流'}</button>{running && <button className={styles.stopButton} disabled={actionPending === 'cancel'} onClick={() => void cancelRun()}><Square size={15} fill="currentColor" />{actionPending === 'cancel' ? '正在停止' : '停止'}</button>}</div>}
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
          {boundedConnectorReceipt && <dl className={connectorStyles.receipt} data-customer-connector-receipt="redacted"><div><dt>执行状态</dt><dd data-connector-receipt-status={text(boundedConnectorReceipt.status, 'unknown')}>{connectorStatusLabel(boundedConnectorReceipt.status)}</dd></div><div><dt>副作用</dt><dd data-connector-side-effect={text(boundedConnectorReceipt.side_effect_state, 'unknown')}>{connectorSideEffectLabel(boundedConnectorReceipt.side_effect_state)}</dd></div><div><dt>外部引用</dt><dd>{text(boundedConnectorReceipt.external_reference, '尚未生成')}</dd></div><div><dt>下一步</dt><dd>{boundedConnectorReceipt.status === 'dry_run' ? '等待维护人员核对并授权' : boundedConnectorReceipt.compensation_execution_id ? '补偿已记录' : '查看最终结果'}</dd></div></dl>}
          {resultMarkdown && !connectorWorkflow ? <MarkdownResultCard source={resultMarkdown} emptyLabel="暂无结果" title="工作流结果" description="已按可读格式整理" openLabel="展开阅读" closeLabel="关闭" dataSurface="customer-runtime-result" /> : !boundedConnectorReceipt && <div className={styles.emptyResult}><Workflow size={24} /><span>{running ? '正在生成结果' : run?.status === 'failed' ? '这次运行没有生成可用结果' : '尚无运行结果'}</span></div>}
        </section>
        </>}
      </section>

      <aside className={styles.summaryColumn}>
        <div className={styles.summaryHeader}><span>本次运行</span><strong>{run ? run.id.slice(0, 8) : '尚未开始'}</strong></div>
        <dl><div><dt>状态</dt><dd>{runStatusLabel(run?.status)}</dd></div><div><dt>步骤</dt><dd>{steps.length}</dd></div><div><dt>已完成</dt><dd>{completedCount}</dd></div><div><dt>结果</dt><dd>{resultAvailabilityLabel(run, resultMarkdown)}</dd></div></dl>
        <p>需要修改处理方式时，请联系工作流维护人员。</p>
      </aside>
    </div>}
  </main>
}
