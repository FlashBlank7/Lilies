'use client'

import '@xyflow/react/dist/style.css'
import Link from 'next/link'
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  addEdge,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
  useEdgesState,
  useNodesState,
} from '@xyflow/react'
import { use, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import {
  api,
  clearClientToken,
  getClientToken,
  idempotency,
  isAuthError,
  saveClientToken,
  type AdaptiveMonitoringStatus,
  type BuilderBenchmarkHistoryRecord,
  type Block,
  type Draft,
  type DraftPatchPreview,
  type GovernedMemoryItem,
  type PlatformPolicyControls,
  type PlatformPolicyControlsUpdate,
  type PlatformPolicyControlsUpdateResponse,
  type PlatformTaskRecord,
  type WorkflowNode,
  withFrontendToken,
} from '@/lib/platform'
import { defaultLocale, isLocale, messages, nextLocale, type Locale } from '@/lib/i18n'

type StudioNode = Node<{ title: string; blockType: string; description: string; status?: string }>
type Copy = (typeof messages)[Locale]
type StudioTab = 'build' | 'edit' | 'test' | 'run' | 'monitor'
type MonitorFilter = 'related' | 'failed' | 'all'
type GovernedMemoryFilter = 'active' | 'revoked' | 'expired' | 'all'
type Version = { version: number; content_hash: string; created_at: string; validation_report: Record<string, unknown> }
type Build = {
  id: string
  status: string
  error?: string
  max_elapsed_seconds?: number | null
  deadline?: { enabled: boolean; max_elapsed_seconds?: number | null }
  team_state: { tasks: Array<Record<string, unknown>>; teammates: Record<string, Record<string, unknown>>; repair_cycles: number }
}
type Run = { id: string; status: string; outputs: Record<string, unknown>; error?: string; state: { waiting_node_id?: string | null } }
type StoredEvent = { id: number; type: string; data: Record<string, unknown> }
type PermissionRequest = { session_id: string; request_id: string; tool?: string; input?: unknown; node_id?: string }
type InputField = { name: string; label?: string; type?: string; required?: boolean; default?: unknown }
type RunInputFieldState = InputField & { value: string; checked?: boolean }
type TestAssertion = { path?: string[]; operator?: string; expected?: unknown }
type AcceptanceResult = {
  test_id?: string
  name?: string
  mandatory?: boolean
  passed?: boolean
  run_id?: string
  assertions?: Array<Record<string, unknown>>
  tool_evidence?: Record<string, unknown>
}
type AcceptanceCaseView = {
  id: string
  name: string
  requirement: string
  mandatory: boolean
  inputs: Record<string, unknown>
  assertions: TestAssertion[]
  requiredNodeTypes: string[]
  requiredToolNodes: string[]
  requiredTools: string[]
  minimumToolCalls: number
  requireCitedToolUrls: boolean
  raw: Record<string, unknown>
  result?: AcceptanceResult
}
type PolicyControlsForm = {
  network_egress_policy: 'full' | 'allowlist' | 'none'
  network_egress_allowlist: string
  cancellation_policy: 'enabled' | 'disabled'
  secret_policy_enabled: boolean
  worker_lease_seconds: string
  reason: string
  limits: Record<string, string>
}
type GovernedMemoryForm = {
  scope_id: string
  actor_id: string
  purpose: string
  reason: string
  content: string
  source_type: string
  source_id: string
  evidence_text: string
  retention_class: 'session' | 'project' | 'user_renewable'
  expires_at: string
}

const policyLimitKeys = [
  'max_active_tasks',
  'max_model_calls_per_task',
  'max_tool_calls_per_task',
  'max_node_executions_per_task',
  'max_model_calls_per_owner',
  'max_tool_calls_per_owner',
  'max_node_executions_per_owner',
]

const accents: Record<string, string> = {
  start: '#8b5cf6', llm: '#3b82f6', claude_agent: '#f97316', tool: '#10b981',
  if_else: '#eab308', question_classifier: '#eab308', end: '#ec4899', answer: '#ec4899',
  human_input: '#ef4444', iteration: '#14b8a6', loop: '#14b8a6', http_request: '#06b6d4',
  schedule_trigger: '#a855f7',
}

function BrickNode({ data, selected }: NodeProps<StudioNode>) {
  const accent = accents[data.blockType] || '#64748b'
  return <div className={`brick-node ${selected ? 'selected' : ''}`} style={{ '--accent': accent } as React.CSSProperties}>
    <Handle type="target" position={Position.Left} />
    <div className="brick-type">{data.blockType.replaceAll('_', ' ')}</div>
    <strong>{data.title}</strong>
    <small>{data.description || '已配置积木'}</small>
    {data.status && <span className={`node-status ${data.status}`}>{data.status}</span>}
    <Handle type="source" position={Position.Right} />
  </div>
}

const nodeTypes = { brick: BrickNode }

function visiblePositions(workflowNodes: WorkflowNode[], workflowEdges: Draft['snapshot']['workflow']['edges']) {
  const depth = new Map(workflowNodes.map(node => [node.id, 0]))
  const incoming = new Map(workflowNodes.map(node => [node.id, 0]))
  const outgoing = new Map(workflowNodes.map(node => [node.id, [] as string[]]))

  workflowEdges.forEach(edge => {
    incoming.set(edge.target, (incoming.get(edge.target) || 0) + 1)
    outgoing.get(edge.source)?.push(edge.target)
  })

  const queue = workflowNodes.filter(node => incoming.get(node.id) === 0).map(node => node.id)
  for (let index = 0; index < queue.length; index += 1) {
    const source = queue[index]
    for (const target of outgoing.get(source) || []) {
      depth.set(target, Math.max(depth.get(target) || 0, (depth.get(source) || 0) + 1))
      incoming.set(target, (incoming.get(target) || 1) - 1)
      if (incoming.get(target) === 0) queue.push(target)
    }
  }

  const rows = new Map<number, number>()
  return new Map(workflowNodes.map(node => {
    if (node.position.x !== 0 || node.position.y !== 0) return [node.id, node.position]
    const column = depth.get(node.id) || 0
    const row = rows.get(column) || 0
    rows.set(column, row + 1)
    return [node.id, { x: 90 + column * 280, y: 110 + row * 130 }]
  }))
}

function validWorkflowEdges(workflowNodes: WorkflowNode[], workflowEdges: Draft['snapshot']['workflow']['edges']) {
  const nodeIds = new Set(workflowNodes.map(node => node.id))
  return workflowEdges.filter(edge => nodeIds.has(edge.source) && nodeIds.has(edge.target))
}

function defaultConfig(type: string): Record<string, unknown> {
  const configs: Record<string, Record<string, unknown>> = {
    start: { inputs: [] },
    schedule_trigger: { timezone: 'Asia/Tokyo', hour: 8, minute: 0, inputs: {} },
    llm: { system: 'You are a helpful assistant.', prompt: { $ref: { node_id: '$inputs', path: ['query'] } } },
    claude_agent: { agent_id: '', task: { $ref: { node_id: '$inputs', path: ['query'] } } },
    tool: { tool_name: 'Read', input: {} },
    if_else: { cases: [{ id: 'true', conditions: [{ value: true, operator: 'equals', expected: true }] }], default_branch: 'else' },
    question_classifier: { input: { $ref: { node_id: '$inputs', path: ['query'] } }, classes: ['class_a', 'class_b'] },
    parameter_extractor: { input: { $ref: { node_id: '$inputs', path: ['query'] } }, fields: [{ name: 'value', type: 'string' }] },
    template_transform: { template: '{{ value }}', variables: { value: '' } },
    variable_assigner: { assignments: {} }, variable_aggregator: { variables: [null], mode: 'first_non_null' },
    http_request: { method: 'GET', url: 'https://example.com', headers: {}, query: {} },
    iteration: { items: [], workflow: { nodes: [], edges: [], viewport: { x: 0, y: 0, zoom: 0.8 } }, output_node_id: '', output_path: [] },
    loop: { workflow: { nodes: [], edges: [], viewport: { x: 0, y: 0, zoom: 0.8 } }, break_condition: { value: false, operator: 'equals', expected: true }, break_value: false, output_node_id: '' },
    human_input: { title: '需要你的输入', fields: [{ name: 'value', label: 'Value', type: 'string' }] },
    end: { outputs: {} }, answer: { answer: '' },
  }
  return configs[type] || {}
}

function workflowRef(nodeId: string, sourcePort = 'output') {
  return { $ref: { node_id: nodeId, path: [sourcePort || 'output'] } }
}

function referencedNodeIds(value: unknown) {
  const ids = new Set<string>()
  const visit = (item: unknown) => {
    if (!item || typeof item !== 'object') return
    if (Array.isArray(item)) {
      item.forEach(visit)
      return
    }
    const record = item as Record<string, unknown>
    const ref = record.$ref as Record<string, unknown> | undefined
    if (ref && typeof ref.node_id === 'string') ids.add(ref.node_id)
    Object.values(record).forEach(visit)
  }
  visit(value)
  return ids
}

function configAfterConnect(node: WorkflowNode, sourceId: string, sourcePort = 'output') {
  if (node.type !== 'variable_aggregator') return node.config
  const current = Array.isArray(node.config.variables) ? [...node.config.variables] : []
  if (referencedNodeIds({ variables: current }).has(sourceId)) return node.config
  const nextRef = workflowRef(sourceId, sourcePort)
  const emptyIndex = current.findIndex(item => item === null || item === undefined || item === '')
  if (emptyIndex >= 0) current[emptyIndex] = nextRef
  else current.push(nextRef)
  return { ...node.config, variables: current }
}

function configAfterDisconnect(node: WorkflowNode, sourceId: string) {
  const stripped = stripRefsToNode(node.config, sourceId)
  return (stripped && typeof stripped === 'object' && !Array.isArray(stripped)) ? stripped as Record<string, unknown> : {}
}

function stripRefsToNode(value: unknown, sourceId: string): unknown {
  if (!value || typeof value !== 'object') return value
  if (Array.isArray(value)) {
    return value.map(item => stripRefsToNode(item, sourceId)).filter(item => item !== undefined)
  }
  const record = value as Record<string, unknown>
  const ref = record.$ref as Record<string, unknown> | undefined
  if (ref && ref.node_id === sourceId) return undefined
  return Object.fromEntries(
    Object.entries(record)
      .map(([key, item]) => [key, stripRefsToNode(item, sourceId)] as const)
      .filter(([, item]) => item !== undefined),
  )
}

function groupBlocks(blocks: Block[]) {
  return blocks.reduce<Record<string, Block[]>>((groups, block) => {
    const category = block.block_kind === 'agent_architecture'
      ? 'agent_architecture'
      : block.block_kind === 'legacy_compatibility'
        ? 'legacy_compatibility'
        : block.category || 'other'
    groups[category] ||= []
    groups[category].push(block)
    return groups
  }, {})
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(item => String(item)) : []
}

function shortTime(value?: string | null) {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString()
}

function localDateTimeInDays(days: number) {
  const date = new Date(Date.now() + days * 24 * 60 * 60 * 1000)
  const offset = date.getTimezoneOffset() * 60 * 1000
  return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}

function governedMemoryExpiresAt(value: string) {
  if (!value.trim()) return undefined
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? undefined : parsed.toISOString()
}

function policyFormFromControls(controls: PlatformPolicyControls): PolicyControlsForm {
  return {
    network_egress_policy: controls.network_egress_policy === 'allowlist' || controls.network_egress_policy === 'none'
      ? controls.network_egress_policy
      : 'full',
    network_egress_allowlist: controls.network_egress_allowlist.join('\n'),
    cancellation_policy: controls.cancellation_policy === 'disabled' ? 'disabled' : 'enabled',
    secret_policy_enabled: controls.secret_policy_enabled,
    worker_lease_seconds: String(controls.worker_lease_seconds || 0),
    reason: '',
    limits: Object.fromEntries(policyLimitKeys.map(key => [key, String(controls.limits[key] ?? 0)])),
  }
}

function numericPolicyValue(value: string) {
  const trimmed = value.trim()
  if (!trimmed) return 0
  const numeric = Number(trimmed)
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : 0
}

function integerPolicyValue(value: string) {
  return Math.floor(numericPolicyValue(value))
}

function taskIsRelated(task: PlatformTaskRecord, applicationId: string, build: Build | null, run: Run | null) {
  return task.owner_id === applicationId
    || task.resource_id === applicationId
    || task.id === build?.id
    || task.resource_id === build?.id
    || task.id === run?.id
    || task.resource_id === run?.id
}

function workflowTests(draft: Draft | null): Record<string, unknown>[] {
  return (draft?.snapshot.tests || []).map(test => asRecord(test))
}

function firstMandatoryInputs(draft: Draft | null): Record<string, unknown> {
  const test = workflowTests(draft).find(item => item.mandatory !== false) || workflowTests(draft)[0]
  return asRecord(test?.inputs)
}

function startInputFields(draft: Draft | null): InputField[] {
  const node = draft?.snapshot.workflow.nodes.find(item => item.type === 'start')
  const inputs = node?.config.inputs
  if (!Array.isArray(inputs)) return []
  return inputs
    .map(item => asRecord(item))
    .filter(item => typeof item.name === 'string' && item.name)
    .map(item => ({
      name: String(item.name),
      label: typeof item.label === 'string' ? item.label : '',
      type: typeof item.type === 'string' ? item.type : 'string',
      required: item.required !== false,
      default: item.default,
    }))
}

function defaultInputValue(field: InputField, testInputs: Record<string, unknown>): unknown {
  if (Object.prototype.hasOwnProperty.call(testInputs, field.name)) return testInputs[field.name]
  if (field.default !== undefined && field.default !== null) return field.default
  if (field.type === 'number') return 0
  if (field.type === 'boolean') return false
  if (field.type === 'object') return {}
  if (field.type === 'array' || field.type === 'file_list') return []
  return ''
}

function stringifyFieldValue(value: unknown, type?: string) {
  if (type === 'object' || type === 'array' || type === 'file_list') {
    return JSON.stringify(value, null, 2)
  }
  if (type === 'boolean') return value ? 'true' : 'false'
  return value === undefined || value === null ? '' : String(value)
}

function buildRunFields(draft: Draft | null, previous: RunInputFieldState[]): RunInputFieldState[] {
  const previousByName = new Map(previous.map(field => [field.name, field]))
  const testInputs = firstMandatoryInputs(draft)
  return startInputFields(draft).map(field => {
    const existing = previousByName.get(field.name)
    const value = existing ? existing.value : stringifyFieldValue(defaultInputValue(field, testInputs), field.type)
    return { ...field, value, checked: field.type === 'boolean' ? value === 'true' : undefined }
  })
}

function parseRunFieldInputs(fields: RunInputFieldState[], t: Copy) {
  const inputs: Record<string, unknown> = {}
  for (const field of fields) {
    const raw = field.type === 'boolean' ? (field.checked ? 'true' : 'false') : field.value
    if (field.required !== false && raw.trim() === '') {
      return { error: t.requiredInput(field.label || field.name), inputs }
    }
    if (raw.trim() === '' && field.required === false) {
      inputs[field.name] = null
      continue
    }
    try {
      if (field.type === 'number') {
        const value = Number(raw)
        if (Number.isNaN(value)) return { error: t.invalidNumber(field.label || field.name), inputs }
        inputs[field.name] = value
      } else if (field.type === 'boolean') {
        inputs[field.name] = field.checked === true
      } else if (field.type === 'object' || field.type === 'array' || field.type === 'file_list') {
        inputs[field.name] = JSON.parse(raw)
      } else {
        inputs[field.name] = raw
      }
    } catch (error) {
      return { error: t.invalidJsonInput(field.label || field.name, String(error)), inputs }
    }
  }
  return { inputs }
}

function acceptanceCases(draft: Draft | null, testReport: Record<string, unknown> | null): AcceptanceCaseView[] {
  const results = Array.isArray(testReport?.tests) ? testReport.tests.map(item => asRecord(item)) : []
  return workflowTests(draft).map((test, index) => {
    const id = String(test.id || `test-${index}`)
    const name = String(test.name || id)
    const result = results.find(item => item.test_id === id || item.name === name) as AcceptanceResult | undefined
    return {
      id,
      name,
      requirement: String(test.requirement || ''),
      mandatory: test.mandatory !== false,
      inputs: asRecord(test.inputs),
      assertions: Array.isArray(test.assertions) ? test.assertions.map(item => asRecord(item) as TestAssertion) : [],
      requiredNodeTypes: asStringArray(test.required_node_types),
      requiredToolNodes: asStringArray(test.required_tool_nodes),
      requiredTools: asStringArray(test.required_tools),
      minimumToolCalls: typeof test.minimum_tool_calls === 'number' ? test.minimum_tool_calls : 0,
      requireCitedToolUrls: test.require_cited_tool_urls === true,
      raw: test,
      result,
    }
  })
}

function fieldInputType(type?: string) {
  if (type === 'number') return 'number'
  return 'text'
}

function latestPendingPermission(events: StoredEvent[]): PermissionRequest | null {
  const resolved = new Set(
    events
      .filter(event => event.type === 'permission.resolved' || event.type === 'node.agent.permission.resolved')
      .map(event => String(event.data.request_id || '')),
  )
  for (const event of [...events].reverse()) {
    if (event.type !== 'permission.requested' && event.type !== 'node.agent.permission.requested') continue
    const requestId = String(event.data.request_id || '')
    const sessionId = String(event.data.session_id || '')
    if (!requestId || !sessionId || resolved.has(requestId)) continue
    return {
      session_id: sessionId,
      request_id: requestId,
      tool: typeof event.data.tool === 'string' ? event.data.tool : undefined,
      input: event.data.input,
      node_id: typeof event.data.node_id === 'string' ? event.data.node_id : undefined,
    }
  }
  return null
}

function visibleRunEvents(events: StoredEvent[]) {
  return events.filter(event =>
    event.type.startsWith('node.')
    || event.type.startsWith('workflow.')
    || event.type.startsWith('permission.')
    || event.type === 'human_input.required',
  )
}

export default function Studio({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const [locale, setLocale] = useState<Locale>(defaultLocale)
  const t = messages[locale]
  const [draft, setDraft] = useState<Draft | null>(null)
  const [blocks, setBlocks] = useState<Block[]>([])
  const [versions, setVersions] = useState<Version[]>([])
  const [nodes, setNodes, onNodesChange] = useNodesState<StudioNode>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [selected, setSelected] = useState<WorkflowNode | null>(null)
  const [selectedEdge, setSelectedEdge] = useState<Edge | null>(null)
  const [configText, setConfigText] = useState('{}')
  const [build, setBuild] = useState<Build | null>(null)
  const [events, setEvents] = useState<Array<{ type: string; data: Record<string, unknown> }>>([])
  const [tab, setTab] = useState<StudioTab>('build')
  const [requirement, setRequirement] = useState('')
  const [buildDeadlineSeconds, setBuildDeadlineSeconds] = useState('')
  const [runFields, setRunFields] = useState<RunInputFieldState[]>([])
  const [run, setRun] = useState<Run | null>(null)
  const [runEvents, setRunEvents] = useState<StoredEvent[]>([])
  const [testReport, setTestReport] = useState<Record<string, unknown> | null>(null)
  const [monitorTasks, setMonitorTasks] = useState<PlatformTaskRecord[]>([])
  const [monitorFilter, setMonitorFilter] = useState<MonitorFilter>('related')
  const [monitorLoading, setMonitorLoading] = useState(false)
  const [monitorError, setMonitorError] = useState('')
  const [policyControls, setPolicyControls] = useState<PlatformPolicyControls | null>(null)
  const [policyControlsLoading, setPolicyControlsLoading] = useState(false)
  const [policyControlsSaving, setPolicyControlsSaving] = useState(false)
  const [policyControlsError, setPolicyControlsError] = useState('')
  const [policyControlsNotice, setPolicyControlsNotice] = useState('')
  const [policyForm, setPolicyForm] = useState<PolicyControlsForm | null>(null)
  const [benchmarkHistory, setBenchmarkHistory] = useState<BuilderBenchmarkHistoryRecord[]>([])
  const [benchmarkHistoryLoading, setBenchmarkHistoryLoading] = useState(false)
  const [benchmarkHistoryError, setBenchmarkHistoryError] = useState('')
  const [adaptiveMonitoring, setAdaptiveMonitoring] = useState<AdaptiveMonitoringStatus | null>(null)
  const [adaptiveMonitoringLoading, setAdaptiveMonitoringLoading] = useState(false)
  const [adaptiveMonitoringError, setAdaptiveMonitoringError] = useState('')
  const [governedMemoryItems, setGovernedMemoryItems] = useState<GovernedMemoryItem[]>([])
  const [governedMemoryAudit, setGovernedMemoryAudit] = useState<StoredEvent[]>([])
  const [governedMemoryFilter, setGovernedMemoryFilter] = useState<GovernedMemoryFilter>('active')
  const [governedMemoryLoading, setGovernedMemoryLoading] = useState(false)
  const [governedMemorySaving, setGovernedMemorySaving] = useState(false)
  const [governedMemoryError, setGovernedMemoryError] = useState('')
  const [governedMemoryNotice, setGovernedMemoryNotice] = useState('')
  const [governedMemoryForm, setGovernedMemoryForm] = useState<GovernedMemoryForm>({
    scope_id: 'project-alpha',
    actor_id: 'studio-operator',
    purpose: 'studio governed memory operator',
    reason: 'operator-managed scoped memory',
    content: '',
    source_type: 'operator_note',
    source_id: 'studio-note',
    evidence_text: '',
    retention_class: 'project',
    expires_at: localDateTimeInDays(30),
  })
  const [patchInstruction, setPatchInstruction] = useState('')
  const [patchPreview, setPatchPreview] = useState<DraftPatchPreview | null>(null)
  const [patchPreviewLoading, setPatchPreviewLoading] = useState(false)
  const [patchApplyLoading, setPatchApplyLoading] = useState(false)
  const [humanValues, setHumanValues] = useState('{}')
  const [notice, setNotice] = useState('')
  const [authRequired, setAuthRequired] = useState(false)
  const [tokenInput, setTokenInput] = useState('')
  const eventSource = useRef<EventSource | null>(null)
  const draftRef = useRef<Draft | null>(null)
  const selectedId = useRef<string | null>(null)
  const selectedEdgeId = useRef<string | null>(null)
  const runFieldsRef = useRef<RunInputFieldState[]>([])
  const flowRef = useRef<ReactFlowInstance<StudioNode, Edge> | null>(null)
  const latestRevision = useRef(0)
  const lastFitSignature = useRef('')
  const buildPoll = useRef<number | null>(null)
  const buildRefreshTimer = useRef<number | null>(null)
  const runPoll = useRef<number | null>(null)

  function setSelectedNode(value: WorkflowNode | null) {
    selectedId.current = value?.id || null
    selectedEdgeId.current = null
    setSelected(value)
    setSelectedEdge(null)
    setConfigText(JSON.stringify(value?.config || {}, null, 2))
  }

  function setSelectedWorkflowEdge(value: Edge | null) {
    selectedEdgeId.current = value?.id || null
    setSelectedEdge(value)
    setEdges(current => current.map(edge => ({
      ...edge,
      selected: edge.id === value?.id,
      style: { ...(edge.style || {}), stroke: edge.id === value?.id ? '#ff8a50' : (edge.label ? '#eab308' : '#465166'), strokeWidth: edge.id === value?.id ? 3 : 1 },
    })))
  }

  function scheduleFitView(renderNodes: StudioNode[]) {
    if (!renderNodes.length) return
    const signature = renderNodes.map(node => node.id).join('|')
    if (signature === lastFitSignature.current) return
    lastFitSignature.current = signature
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        flowRef.current?.fitView({ padding: 0.22, duration: 250 })
      })
    })
  }

  const syncCanvas = useCallback((next: Draft) => {
    if (next.revision < latestRevision.current) return
    latestRevision.current = next.revision
    draftRef.current = next
    setDraft(next)
    setRequirement(next.snapshot.requirement)
    setRunFields(current => {
      const fields = buildRunFields(next, current)
      runFieldsRef.current = fields
      return fields
    })
    const workflowEdges = validWorkflowEdges(next.snapshot.workflow.nodes, next.snapshot.workflow.edges)
    const positions = visiblePositions(next.snapshot.workflow.nodes, workflowEdges)
    const renderNodes: StudioNode[] = next.snapshot.workflow.nodes.map(item => ({
      id: item.id, type: 'brick', position: positions.get(item.id) || item.position,
      data: { title: item.title, blockType: item.type, description: item.description || t.configuredBrick },
    }))
    setNodes(renderNodes)
    setEdges(workflowEdges.map(item => {
      const selected = item.id === selectedEdgeId.current
      return {
        id: item.id, source: item.source, target: item.target, label: item.branch || undefined,
        selected,
        animated: Boolean(item.branch),
        style: { stroke: selected ? '#ff8a50' : (item.branch ? '#eab308' : '#465166'), strokeWidth: selected ? 3 : 1 },
      }
    }))
    if (selectedId.current) {
      const updated = next.snapshot.workflow.nodes.find(item => item.id === selectedId.current) || null
      if (updated) {
        setSelected(updated)
        setConfigText(JSON.stringify(updated.config || {}, null, 2))
      } else {
        selectedId.current = null
        setSelected(null)
        setConfigText('{}')
      }
    }
    if (selectedEdgeId.current) {
      const updated = workflowEdges.find(item => item.id === selectedEdgeId.current)
      if (updated) {
        setSelectedEdge({ id: updated.id, source: updated.source, target: updated.target, label: updated.branch || undefined })
      } else {
        selectedEdgeId.current = null
        setSelectedEdge(null)
      }
    }
    scheduleFitView(renderNodes)
  }, [setEdges, setNodes, t.configuredBrick])

  const refresh = useCallback(async () => {
    try {
      const [next, nextBlocks, nextVersions] = await Promise.all([
        api<Draft>(`/api/v1/applications/${id}/draft`),
        api<Block[]>('/api/v1/blocks'),
        api<Version[]>(`/api/v1/applications/${id}/versions`),
      ])
      syncCanvas(next)
      setBlocks(nextBlocks)
      setVersions(nextVersions)
      setAuthRequired(false)
      return next
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      throw error
    }
  }, [id, syncCanvas])

  const scheduleBuildRefresh = useCallback((delay = 80) => {
    if (buildRefreshTimer.current) window.clearTimeout(buildRefreshTimer.current)
    buildRefreshTimer.current = window.setTimeout(() => {
      buildRefreshTimer.current = null
      void refresh().catch(error => setNotice(String(error)))
    }, delay)
  }, [refresh])

  const refreshMonitorTasks = useCallback(async () => {
    setMonitorLoading(true)
    setMonitorError('')
    try {
      const tasks = await api<PlatformTaskRecord[]>('/api/v1/platform/harness/tasks?limit=100')
      setMonitorTasks(tasks)
      setAuthRequired(false)
      return tasks
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setMonitorError(String(error))
      throw error
    } finally {
      setMonitorLoading(false)
    }
  }, [])

  const refreshPolicyControls = useCallback(async () => {
    setPolicyControlsLoading(true)
    setPolicyControlsError('')
    try {
      const controls = await api<PlatformPolicyControls>('/api/v1/platform/harness/policy-controls')
      setPolicyControls(controls)
      setPolicyForm(policyFormFromControls(controls))
      setAuthRequired(false)
      return controls
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setPolicyControlsError(String(error))
      throw error
    } finally {
      setPolicyControlsLoading(false)
    }
  }, [])

  const savePolicyControls = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!policyForm) return
    setPolicyControlsSaving(true)
    setPolicyControlsError('')
    setPolicyControlsNotice('')
    const limits = Object.fromEntries(policyLimitKeys.map(key => [key, integerPolicyValue(policyForm.limits[key] || '0')]))
    const allowlist = policyForm.network_egress_allowlist
      .split(/[\n,]/)
      .map(item => item.trim())
      .filter(Boolean)
    const body: PlatformPolicyControlsUpdate = {
      network_egress_policy: policyForm.network_egress_policy,
      network_egress_allowlist: allowlist,
      cancellation_policy: policyForm.cancellation_policy,
      secret_policy_enabled: policyForm.secret_policy_enabled,
      worker_lease_seconds: numericPolicyValue(policyForm.worker_lease_seconds),
      limits,
      reason: policyForm.reason.trim(),
    }
    try {
      const result = await api<PlatformPolicyControlsUpdateResponse>('/api/v1/platform/harness/policy-controls', {
        method: 'PATCH',
        body: JSON.stringify(body),
      })
      setPolicyControls(result.after)
      setPolicyForm(policyFormFromControls(result.after))
      setPolicyControlsNotice(`${t.policySaved}: ${result.audit.changed_fields.join(', ')}`)
      setAuthRequired(false)
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setPolicyControlsError(String(error))
    } finally {
      setPolicyControlsSaving(false)
    }
  }, [policyForm, t.policySaved])

  const refreshBenchmarkHistory = useCallback(async () => {
    setBenchmarkHistoryLoading(true)
    setBenchmarkHistoryError('')
    try {
      const records = await api<BuilderBenchmarkHistoryRecord[]>('/api/v1/builder-benchmark/history?limit=50')
      setBenchmarkHistory(records)
      setAuthRequired(false)
      return records
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setBenchmarkHistoryError(String(error))
      throw error
    } finally {
      setBenchmarkHistoryLoading(false)
    }
  }, [])

  const refreshAdaptiveMonitoring = useCallback(async () => {
    setAdaptiveMonitoringLoading(true)
    setAdaptiveMonitoringError('')
    try {
      const status = await api<AdaptiveMonitoringStatus>('/api/v1/templates/adaptive-monitoring')
      setAdaptiveMonitoring(status)
      setAuthRequired(false)
      return status
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setAdaptiveMonitoringError(String(error))
      throw error
    } finally {
      setAdaptiveMonitoringLoading(false)
    }
  }, [])

  const recordAdaptiveMonitoringRefresh = useCallback(async () => {
    setAdaptiveMonitoringLoading(true)
    setAdaptiveMonitoringError('')
    try {
      const status = await api<AdaptiveMonitoringStatus>('/api/v1/templates/adaptive-monitoring/refresh', { method: 'POST' })
      setAdaptiveMonitoring(status)
      setAuthRequired(false)
      return status
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setAdaptiveMonitoringError(String(error))
      throw error
    } finally {
      setAdaptiveMonitoringLoading(false)
    }
  }, [])

  const governedMemoryPermission = useCallback((operations: Array<'create' | 'read' | 'update' | 'revoke' | 'expire'>) => ({
    actor_id: governedMemoryForm.actor_id.trim(),
    owner_id: id,
    scope_id: governedMemoryForm.scope_id.trim(),
    purpose: governedMemoryForm.purpose.trim(),
    allowed_operations: operations,
  }), [governedMemoryForm.actor_id, governedMemoryForm.purpose, governedMemoryForm.scope_id, id])

  const refreshGovernedMemoryAudit = useCallback(async () => {
    const scope = governedMemoryForm.scope_id.trim()
    if (!scope) return []
    const streamId = `governed-memory:${id}:${scope}`
    const events = await api<StoredEvent[]>(`/v1/streams/${encodeURIComponent(streamId)}`)
    setGovernedMemoryAudit(events)
    return events
  }, [governedMemoryForm.scope_id, id])

  const refreshGovernedMemoryItems = useCallback(async () => {
    const scope = governedMemoryForm.scope_id.trim()
    const actor = governedMemoryForm.actor_id.trim()
    const purpose = governedMemoryForm.purpose.trim()
    const reason = governedMemoryForm.reason.trim()
    if (!scope || !actor || !purpose || !reason) {
      setGovernedMemoryError(t.governedMemoryMissingScope)
      return []
    }
    setGovernedMemoryLoading(true)
    setGovernedMemoryError('')
    const query = new URLSearchParams({
      owner_id: id,
      scope_id: scope,
      actor_id: actor,
      purpose,
      reason,
      status_filter: governedMemoryFilter,
      limit: '100',
    })
    try {
      const items = await api<GovernedMemoryItem[]>(`/api/v1/platform/governed-memory?${query.toString()}`)
      setGovernedMemoryItems(items)
      await refreshGovernedMemoryAudit()
      setAuthRequired(false)
      return items
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setGovernedMemoryError(String(error))
      throw error
    } finally {
      setGovernedMemoryLoading(false)
    }
  }, [governedMemoryFilter, governedMemoryForm.actor_id, governedMemoryForm.purpose, governedMemoryForm.reason, governedMemoryForm.scope_id, id, refreshGovernedMemoryAudit, t.governedMemoryMissingScope])

  const createGovernedMemory = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const content = governedMemoryForm.content.trim()
    const reason = governedMemoryForm.reason.trim()
    if (!content || !reason) {
      setGovernedMemoryError(t.governedMemoryMissingContent)
      return
    }
    setGovernedMemorySaving(true)
    setGovernedMemoryError('')
    setGovernedMemoryNotice('')
    try {
      const created = await api<GovernedMemoryItem>('/api/v1/platform/governed-memory', {
        method: 'POST',
        body: JSON.stringify({
          permission: governedMemoryPermission(['create']),
          content,
          source: {
            source_type: governedMemoryForm.source_type.trim(),
            source_id: governedMemoryForm.source_id.trim(),
            evidence_text: governedMemoryForm.evidence_text.trim() || content,
          },
          retention_class: governedMemoryForm.retention_class,
          expires_at: governedMemoryExpiresAt(governedMemoryForm.expires_at),
          reason,
        }),
      })
      setGovernedMemoryNotice(`${t.governedMemoryCreated}: ${created.id}`)
      setGovernedMemoryForm(current => ({ ...current, content: '', evidence_text: '' }))
      await refreshGovernedMemoryItems()
      setAuthRequired(false)
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setGovernedMemoryError(String(error))
    } finally {
      setGovernedMemorySaving(false)
    }
  }, [governedMemoryForm, governedMemoryPermission, refreshGovernedMemoryItems, t.governedMemoryCreated, t.governedMemoryMissingContent])

  const revokeGovernedMemory = useCallback(async (memoryId: string) => {
    const reason = governedMemoryForm.reason.trim()
    if (!reason) {
      setGovernedMemoryError(t.governedMemoryMissingReason)
      return
    }
    setGovernedMemoryLoading(true)
    setGovernedMemoryError('')
    setGovernedMemoryNotice('')
    try {
      const revoked = await api<GovernedMemoryItem>(`/api/v1/platform/governed-memory/${memoryId}/revoke`, {
        method: 'POST',
        body: JSON.stringify({
          permission: governedMemoryPermission(['revoke']),
          reason,
        }),
      })
      setGovernedMemoryNotice(`${t.governedMemoryRevoked}: ${revoked.id}`)
      await refreshGovernedMemoryItems()
      setAuthRequired(false)
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setGovernedMemoryError(String(error))
    } finally {
      setGovernedMemoryLoading(false)
    }
  }, [governedMemoryForm.reason, governedMemoryPermission, refreshGovernedMemoryItems, t.governedMemoryMissingReason, t.governedMemoryRevoked])

  useEffect(() => {
    const stored = globalThis.localStorage?.getItem('foundry.locale')
    if (isLocale(stored)) setLocale(stored)
    setTokenInput(getClientToken())
    refresh().catch(error => setNotice(String(error)))
    refreshMonitorTasks().catch(error => setNotice(String(error)))
    refreshPolicyControls().catch(error => setNotice(String(error)))
    refreshBenchmarkHistory().catch(error => setNotice(String(error)))
    refreshAdaptiveMonitoring().catch(error => setNotice(String(error)))
  }, [refresh, refreshAdaptiveMonitoring, refreshBenchmarkHistory, refreshMonitorTasks, refreshPolicyControls])
  useEffect(() => {
    const buildId = new URLSearchParams(window.location.search).get('build')
    if (buildId) watchBuild(buildId)
    else api<Build[]>(`/api/v1/applications/${id}/builds`).then(items => {
      if (!items[0]) return
      setBuild(items[0])
      if (['queued', 'building'].includes(items[0].status)) watchBuild(items[0].id)
    }).catch(() => undefined)
    return () => {
      eventSource.current?.close()
      if (buildPoll.current) {
        window.clearInterval(buildPoll.current)
        buildPoll.current = null
      }
      if (buildRefreshTimer.current) {
        window.clearTimeout(buildRefreshTimer.current)
        buildRefreshTimer.current = null
      }
      if (runPoll.current) {
        window.clearInterval(runPoll.current)
        runPoll.current = null
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function mutation(op: string, data: Record<string, unknown>) {
    const current = draftRef.current
    if (!current) return null
    try {
      await api(`/api/v1/applications/${id}/draft`, {
        method: 'POST',
        body: JSON.stringify({ expected_revision: current.revision, idempotency_key: idempotency(), op, data }),
      })
      const next = await refresh()
      setNotice(t.savedDraft)
      return next
    } catch (error) {
      setNotice(String(error))
      await refresh()
      return null
    }
  }

  const onConnect = useCallback(async (connection: Connection) => {
    if (!connection.source || !connection.target) return
    const edgeId = idempotency()
    setEdges(current => addEdge({ ...connection, id: edgeId }, current))
    const next = await mutation('add_edge', { edge: {
      id: edgeId, source: connection.source, target: connection.target,
      source_port: 'output', target_port: 'input',
    } })
    const target = next?.snapshot.workflow.nodes.find(item => item.id === connection.target)
    if (target) {
      const config = configAfterConnect(target, connection.source, 'output')
      if (config !== target.config) {
        await mutation('update_node', { node_id: target.id, changes: { config }, merge_config: false })
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  async function addBlock(block: Block) {
    const index = draft?.snapshot.workflow.nodes.length || 0
    await mutation('add_node', { node: {
      id: `${block.type}-${Date.now()}`, type: block.type, block_version: 1, title: blockTitle(block),
      description: blockDescription(block), config: defaultConfig(block.type), position: { x: 120 + index * 55, y: 120 + (index % 4) * 90 },
      retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
    } })
  }

  function chooseNode(node: StudioNode) {
    const value = draft?.snapshot.workflow.nodes.find(item => item.id === node.id) || null
    setSelectedNode(value)
    setTab('edit')
  }

  function chooseEdge(edge: Edge) {
    setSelectedWorkflowEdge(edge)
  }

  async function saveConfig() {
    if (!selected) return
    try {
      const config = JSON.parse(configText)
      const next = await mutation('update_node', { node_id: selected.id, changes: { config }, merge_config: false })
      await reconcileIncomingEdges(selected.id, config, next)
    } catch (error) { setNotice(t.invalidJson(String(error))) }
  }

  async function previewDraftPatch() {
    const instruction = patchInstruction.trim()
    if (!instruction) {
      setNotice(t.patchPreviewEmpty)
      return
    }
    setPatchPreviewLoading(true)
    setPatchPreview(null)
    try {
      const result = await api<DraftPatchPreview>(`/api/v1/applications/${id}/draft/preview-patch`, {
        method: 'POST',
        body: JSON.stringify({ instruction }),
      })
      setPatchPreview(result)
      setNotice(result.supported ? t.patchPreviewReady : t.patchPreviewUnsupported)
      await refreshMonitorTasks().catch(error => setNotice(String(error)))
    } catch (error) {
      setNotice(String(error))
    } finally {
      setPatchPreviewLoading(false)
    }
  }

  async function applyDraftPatch() {
    if (!patchPreview?.supported || !patchPreview.operations.length) return
    setPatchApplyLoading(true)
    try {
      let current = draftRef.current
      for (const operation of patchPreview.operations) {
        await api(`/api/v1/applications/${id}/draft`, {
          method: 'POST',
          body: JSON.stringify({
            expected_revision: current?.revision ?? operation.expected_revision,
            idempotency_key: idempotency(),
            op: operation.op,
            data: operation.data,
          }),
        })
        current = await refresh()
      }
      setPatchPreview(null)
      setPatchInstruction('')
      setNotice(t.patchApplied)
      await refreshMonitorTasks().catch(error => setNotice(String(error)))
    } catch (error) {
      setNotice(String(error))
      await refresh().catch(() => undefined)
    } finally {
      setPatchApplyLoading(false)
    }
  }

  async function reconcileIncomingEdges(nodeId: string, config: Record<string, unknown>, next: Draft | null) {
    const current = next || draftRef.current
    const node = current?.snapshot.workflow.nodes.find(item => item.id === nodeId)
    if (!current || node?.type !== 'variable_aggregator') return
    const desiredSources = referencedNodeIds(config)
    const availableSources = new Set(current.snapshot.workflow.nodes.map(item => item.id))
    const incoming = current.snapshot.workflow.edges.filter(edge => edge.target === nodeId && !edge.branch)
    for (const edge of incoming) {
      if (!desiredSources.has(edge.source)) await mutation('remove_edge', { edge_id: edge.id })
    }
    const refreshed = draftRef.current || current
    const existingSources = new Set(refreshed.snapshot.workflow.edges.filter(edge => edge.target === nodeId && !edge.branch).map(edge => edge.source))
    for (const source of desiredSources) {
      if (source !== nodeId && availableSources.has(source) && !existingSources.has(source)) {
        await mutation('add_edge', { edge: {
          id: idempotency(), source, target: nodeId, source_port: 'output', target_port: 'input',
        } })
      }
    }
  }

  async function deleteSelectedNode() {
    if (!selected) return
    const nodeId = selected.id
    setSelectedNode(null)
    await mutation('remove_node', { node_id: nodeId })
  }

  async function persistDeletedNodes(deleted: StudioNode[]) {
    for (const node of deleted) {
      if (selectedId.current === node.id) setSelectedNode(null)
      await mutation('remove_node', { node_id: node.id })
    }
  }

  async function persistDeletedEdges(deleted: Edge[]) {
    for (const edge of deleted) {
      const before = draftRef.current
      const actual = before?.snapshot.workflow.edges.find(item => item.id === edge.id)
        || before?.snapshot.workflow.edges.find(item => item.source === edge.source && item.target === edge.target)
      if (!actual) {
        setNotice(t.edgeAlreadyRemoved)
        await refresh().catch(() => undefined)
        continue
      }
      if (selectedEdgeId.current === actual.id || selectedEdgeId.current === edge.id) {
        selectedEdgeId.current = null
        setSelectedEdge(null)
      }
      const target = before?.snapshot.workflow.nodes.find(item => item.id === actual.target)
      if (target) {
        const config = configAfterDisconnect(target, actual.source)
        await mutation('update_node', {
          node_id: target.id,
          changes: { config },
          merge_config: false,
        })
      }
      await mutation('remove_edge', { edge_id: actual.id })
    }
  }

  async function startBuild() {
    const trimmedDeadline = buildDeadlineSeconds.trim()
    let maxElapsedSeconds: number | undefined
    if (trimmedDeadline) {
      maxElapsedSeconds = Number(trimmedDeadline)
      if (Number.isNaN(maxElapsedSeconds) || maxElapsedSeconds <= 0) {
        setNotice(t.buildDeadlineInvalid)
        return
      }
    }
    const result = await api<{ build_id: string }>(`/api/v1/applications/${id}/builds`, {
      method: 'POST',
      body: JSON.stringify({
        requirement,
        auto_publish: true,
        ...(maxElapsedSeconds ? { max_elapsed_seconds: maxElapsedSeconds } : {}),
      }),
    })
    history.replaceState(null, '', `?build=${result.build_id}`)
    void refreshMonitorTasks().catch(error => setNotice(String(error)))
    watchBuild(result.build_id)
  }

  function watchBuild(buildId: string) {
    eventSource.current?.close()
    if (buildPoll.current) window.clearInterval(buildPoll.current)
    buildPoll.current = null
    if (buildRefreshTimer.current) window.clearTimeout(buildRefreshTimer.current)
    buildRefreshTimer.current = null
    setTab('build')
    const source = new EventSource(withFrontendToken(`/api/platform/api/v1/builds/${buildId}/events`))
    eventSource.current = source
    source.onerror = () => {
      if (!getClientToken()) setAuthRequired(true)
    }
    const names = ['build.started', 'build.operation', 'build.turn.completed', 'team.teammate.spawned', 'team.teammate.idle', 'tests.completed', 'build.published', 'build.completed', 'build.needs_attention']
    names.forEach(type => source.addEventListener(type, async raw => {
      const event = raw as MessageEvent
      const data = JSON.parse(event.data)
      setEvents(current => [...current.slice(-199), { type, data }])
      if (type === 'build.operation' || type === 'build.turn.completed' || type === 'team.teammate.idle' || type === 'tests.completed' || type === 'build.published') {
        scheduleBuildRefresh()
      }
      if (type === 'build.completed' || type === 'build.needs_attention') {
        source.close()
        const current = await api<Build>(`/api/v1/builds/${buildId}`)
        setBuild(current)
        await refresh()
        await refreshMonitorTasks().catch(error => setNotice(String(error)))
      }
    }))
    buildPoll.current = window.setInterval(() => api<Build>(`/api/v1/builds/${buildId}`).then(value => {
      setBuild(value)
      if (['published', 'ready', 'needs_attention', 'cancelled'].includes(value.status) && buildPoll.current) {
        window.clearInterval(buildPoll.current)
        buildPoll.current = null
      }
    }).catch(error => {
      if (isAuthError(error)) {
        setAuthRequired(true)
        if (buildPoll.current) {
          window.clearInterval(buildPoll.current)
          buildPoll.current = null
        }
        source.close()
      }
    }), 1500)
  }

  async function runTests() {
    setNotice(t.testing)
    const result = await api<{ passed: boolean } & Record<string, unknown>>(`/api/v1/applications/${id}/tests/run`, { method: 'POST' })
    setTestReport(result)
    setNotice(result.passed ? t.testsPassed : t.testsFailed)
    await refresh()
    await refreshMonitorTasks().catch(error => setNotice(String(error)))
  }

  async function publish() {
    const result = await api<{ version: number }>(`/api/v1/applications/${id}/versions`, { method: 'POST' })
    setNotice(t.published(result.version))
    await refresh()
  }

  function updateRunField(name: string, changes: Partial<RunInputFieldState>) {
    setRunFields(current => {
      const next = current.map(field => field.name === name ? { ...field, ...changes } : field)
      runFieldsRef.current = next
      return next
    })
  }

  async function startRun(useDraft = false) {
    const parsed = parseRunFieldInputs(runFieldsRef.current, t)
    if (parsed.error) {
      setNotice(parsed.error)
      return
    }
    const result = await api<{ run_id: string }>(`/api/v1/applications/${id}/runs`, {
      method: 'POST', body: JSON.stringify({ inputs: parsed.inputs, use_draft: useDraft, workspace_path: '.' }),
    })
    setTab('run')
    setRunEvents([])
    setRun({ id: result.run_id, status: 'queued', outputs: {}, state: {} })
    void refreshMonitorTasks().catch(error => setNotice(String(error)))
    watchRun(result.run_id)
  }

  async function resumeRun() {
    if (!run) return
    await api(`/api/v1/runs/${run.id}/resume`, { method: 'POST', body: JSON.stringify({ values: JSON.parse(humanValues) }) })
    setRun({ ...run, status: 'running' })
    watchRun(run.id)
  }

  function watchRun(runId: string) {
    if (runPoll.current) window.clearInterval(runPoll.current)
    const tick = async () => {
      const [current, stream] = await Promise.all([
        api<Run>(`/api/v1/runs/${runId}`),
        api<StoredEvent[]>(`/v1/streams/${runId}`),
      ])
      setRun(current)
      setRunEvents(stream)
      if (['succeeded', 'failed', 'paused', 'cancelled'].includes(current.status) && runPoll.current) {
        window.clearInterval(runPoll.current)
        runPoll.current = null
        void refreshMonitorTasks().catch(error => setNotice(String(error)))
      }
    }
    void tick().catch(error => setNotice(String(error)))
    runPoll.current = window.setInterval(() => {
      void tick().catch(error => setNotice(String(error)))
    }, 1000)
  }

  async function resolvePermission(permission: PermissionRequest, behavior: 'allow' | 'deny') {
    await api(`/v1/sessions/${permission.session_id}/permissions/${permission.request_id}`, {
      method: 'POST',
      body: JSON.stringify({ behavior }),
    })
    setNotice(behavior === 'allow' ? t.permissionApproved : t.permissionDenied)
    if (run) watchRun(run.id)
  }

  async function cancelRun() {
    if (!run) return
    await api(`/api/v1/runs/${run.id}/cancel`, { method: 'POST' })
    setNotice(t.runCancelling)
    void refreshMonitorTasks().catch(error => setNotice(String(error)))
    watchRun(run.id)
  }

  const grouped = useMemo(() => groupBlocks(blocks), [blocks])
  const tested = draft?.tested_hash && draft.tested_hash === draft.content_hash
  const activeVersion = versions[0]?.version
  const acceptanceCaseViews = useMemo(() => acceptanceCases(draft, testReport), [draft, testReport])
  const runInputParsed = useMemo(() => parseRunFieldInputs(runFields, t), [runFields, t])
  const runInputPreview = JSON.stringify(runInputParsed.inputs || {}, null, 2)
  const pendingPermission = useMemo(() => latestPendingPermission(runEvents), [runEvents])
  const relatedMonitorTasks = useMemo(
    () => monitorTasks.filter(task => taskIsRelated(task, id, build, run)),
    [build, id, monitorTasks, run],
  )
  const visibleMonitorTasks = useMemo(() => {
    if (monitorFilter === 'all') return monitorTasks
    if (monitorFilter === 'failed') return monitorTasks.filter(task => task.status === 'failed')
    return relatedMonitorTasks
  }, [monitorFilter, monitorTasks, relatedMonitorTasks])
  const monitorSummary = useMemo(() => ({
    total: monitorTasks.length,
    related: relatedMonitorTasks.length,
    failed: monitorTasks.filter(task => task.status === 'failed').length,
    running: monitorTasks.filter(task => task.status === 'running').length,
  }), [monitorTasks, relatedMonitorTasks.length])
  const canvasStats = useMemo(() => ({
    nodes: draft?.snapshot.workflow.nodes.length || 0,
    edges: validWorkflowEdges(draft?.snapshot.workflow.nodes || [], draft?.snapshot.workflow.edges || []).length,
  }), [draft])
  const readinessCards = [
    {
      label: t.readinessDraft,
      ready: Boolean(draft),
      detail: draft ? `${t.draft} r${draft.revision}` : t.loading,
    },
    {
      label: t.readinessTest,
      ready: Boolean(tested),
      detail: tested ? t.verified : t.unverified,
    },
    {
      label: t.readinessPublish,
      ready: Boolean(activeVersion),
      detail: activeVersion ? t.activeVersion(activeVersion) : t.noPublishedVersion,
    },
    {
      label: t.readinessRun,
      ready: Boolean(draft),
      detail: runInputParsed.error ? runInputParsed.error : t.runDraftButton,
    },
    {
      label: t.readinessMonitor,
      ready: monitorSummary.related > 0 || monitorSummary.failed > 0 || monitorSummary.running > 0,
      detail: `${monitorSummary.related} ${t.monitorRelated} · ${monitorSummary.failed} ${t.monitorFailed}`,
    },
  ]
  const detailSignals = [
    {
      label: t.detailSignalStructure,
      value: t.detailSignalStructureValue(canvasStats.nodes, canvasStats.edges),
      ready: canvasStats.nodes > 0,
    },
    {
      label: t.detailSignalAcceptance,
      value: acceptanceCaseViews.length ? t.acceptanceCases(acceptanceCaseViews.length) : t.noRequirementText,
      ready: acceptanceCaseViews.length > 0,
    },
    {
      label: t.detailSignalRun,
      value: runInputParsed.error || t.runDraftButton,
      ready: !runInputParsed.error,
    },
    {
      label: t.detailSignalMonitor,
      value: `${monitorSummary.related} ${t.monitorRelated} · ${monitorSummary.failed} ${t.monitorFailed}`,
      ready: monitorSummary.related > 0 || monitorSummary.failed > 0 || monitorSummary.running > 0,
    },
  ]
  const nextActionCards: Array<{
    id: string
    label: string
    detail: string
    ready: boolean
    target: StudioTab
  }> = [
    {
      id: 'inspect',
      label: t.nextActionInspect,
      detail: canvasStats.nodes ? t.nextActionInspectReady(canvasStats.nodes, canvasStats.edges) : t.nextActionInspectEmpty,
      ready: canvasStats.nodes > 0,
      target: 'edit',
    },
    {
      id: 'build',
      label: t.nextActionBuild,
      detail: build ? `${build.status} · ${build.team_state.tasks.length} ${t.tasksTitle}` : t.nextActionBuildHelp,
      ready: Boolean(build),
      target: 'build',
    },
    {
      id: 'test',
      label: t.nextActionTest,
      detail: tested ? t.testsPassed : t.nextActionTestHelp,
      ready: Boolean(tested),
      target: 'test',
    },
    {
      id: 'run',
      label: t.nextActionRun,
      detail: runInputParsed.error || t.nextActionRunReady,
      ready: !runInputParsed.error,
      target: 'run',
    },
    {
      id: 'publish',
      label: t.nextActionPublish,
      detail: activeVersion ? t.activeVersion(activeVersion) : tested ? t.nextActionPublishReady : t.nextActionPublishBlocked,
      ready: Boolean(activeVersion),
      target: 'test',
    },
    {
      id: 'monitor',
      label: t.nextActionMonitor,
      detail: monitorSummary.related || monitorSummary.failed ? t.nextActionMonitorReady(monitorSummary.related, monitorSummary.failed) : t.nextActionMonitorHelp,
      ready: monitorSummary.related > 0 || monitorSummary.failed > 0,
      target: 'monitor',
    },
  ]
  function toggleLocale() {
    const value = nextLocale(locale)
    setLocale(value)
    globalThis.localStorage?.setItem('foundry.locale', value)
  }
  function blockTitle(block: Block) {
    return block.editor?.i18n?.[locale]?.title || block.title
  }
  function blockDescription(block: Block) {
    return block.editor?.i18n?.[locale]?.description || block.description
  }
  function blockCategory(block: Block) {
    if (block.block_kind === 'agent_architecture') return locale === 'zh' ? 'Agent 架构积木' : 'Agent Architecture'
    if (block.block_kind === 'legacy_compatibility') return locale === 'zh' ? 'Legacy 兼容层' : 'Legacy Compatibility'
    return block.editor?.i18n?.[locale]?.category || block.category
  }
  function saveToken(event: FormEvent) {
    event.preventDefault()
    saveClientToken(tokenInput)
    setNotice(t.authSaved)
    void refresh().catch(error => setNotice(String(error)))
  }
  const architecture = useMemo(() => {
    const workflow = draft?.snapshot.workflow
    if (!workflow) return []
    return workflow.nodes.map(node => {
      const next = workflow.edges.filter(edge => edge.source === node.id).map(edge => edge.target).join(', ') || t.terminal
      const detail = node.type === 'tool' ? ` · ${(node.config.tool_name as string) || t.unboundTool}` : ''
      return `${node.id}: ${node.type}${detail} → ${next}`
    })
  }, [draft, t.terminal, t.unboundTool])

  return <main className="studio-shell">
    <header className="studio-header">
      <Link href="/" className="back">←</Link>
      <div className="studio-title"><strong>{draft?.snapshot.name || t.loading}</strong><span>{draft?.snapshot.mode === 'chat' ? t.modeChat : t.modeWorkflow} · {t.draft} r{draft?.revision ?? 0}</span></div>
      <div className="header-center"><span className={tested ? 'verified' : 'unverified'}>{tested ? t.verified : t.unverified}</span>{activeVersion && <span>{t.activeVersion(activeVersion)}</span>}</div>
      <div className="header-actions"><button className="lang-toggle" onClick={toggleLocale}>{t.switchLabel}</button><button className="ghost" onClick={() => setTab('run')}>{t.debugDraft}</button><button onClick={publish} disabled={!tested}>{t.publishVersion}</button></div>
    </header>
    <div className="studio-grid">
      <aside className="left-panel">
        <div className="panel-tabs">{(['build', 'edit', 'test', 'run', 'monitor'] as const).map(item => <button className={tab === item ? 'active' : ''} onClick={() => setTab(item)} key={item}>{item === 'build' ? t.buildTab : item === 'edit' ? t.editTab : item === 'test' ? t.testTab : item === 'run' ? t.runTab : t.monitorTab}</button>)}</div>
        {tab === 'build' && <div className="panel-body">
          <div className="panel-kicker">{t.builderTeam}</div><h2>{t.continueBuild}</h2>
          <textarea className="requirement-input" value={requirement} onChange={event => setRequirement(event.target.value)} />
          <label className="run-field">
            <span>{t.buildDeadlineLabel}<em>{t.buildDeadlineHelp}</em></span>
            <input type="number" min="0.001" step="0.1" value={buildDeadlineSeconds} onChange={event => setBuildDeadlineSeconds(event.target.value)} />
          </label>
          <button className="wide" onClick={startBuild}>{t.startTeam}</button>
          {build && <div className="build-status"><b>{build.status}</b><span>{Object.keys(build.team_state.teammates).length} teammates · {build.team_state.tasks.length} tasks · {build.team_state.repair_cycles} repairs</span><span>{build.deadline?.enabled && build.deadline.max_elapsed_seconds ? t.buildDeadlineActive(build.deadline.max_elapsed_seconds) : t.buildDeadlineInactive}</span>{build.error && <p>{build.error}</p>}</div>}
          <h3>{t.tasksTitle}</h3>
          <div className="test-list">{build?.team_state.tasks.map((task, index) => <pre key={index}>{JSON.stringify(task, null, 2)}</pre>) || <p className="muted">{t.tasksEmpty}</p>}</div>
          <section className="bug-triage-panel">
            <div className="bug-triage-head"><strong>{t.bugTriageTitle}</strong><small>{t.bugTriageHelp}</small></div>
            <ul>{t.bugTriageItems.map(item => <li key={item}>{item}</li>)}</ul>
          </section>
          <h3>{t.architectureTitle}</h3>
          <div className="architecture-list">{architecture.map(item => <code key={item}>{item}</code>)}</div>
          <div className="event-log">{events.map((event, index) => <div key={index}><span>{event.type}</span><pre>{JSON.stringify(event.data, null, 2)}</pre></div>)}</div>
        </div>}
        {tab === 'edit' && <div className="panel-body">
          <div className="panel-kicker">{t.nodeInspector}</div><h2>{selected?.title || t.selectBrick}</h2>
          <section className="patch-panel">
            <div className="patch-panel-head"><strong>{t.patchPreviewTitle}</strong><small>{t.patchPreviewHelp}</small></div>
            <textarea className="patch-input" value={patchInstruction} placeholder={t.patchPreviewPlaceholder} onChange={event => setPatchInstruction(event.target.value)} />
            <div className="run-actions"><button className="wide" onClick={previewDraftPatch} disabled={patchPreviewLoading}>{patchPreviewLoading ? t.patchPreviewing : t.patchPreviewButton}</button><button className="wide secondary" onClick={applyDraftPatch} disabled={!patchPreview?.supported || patchPreview.operations.length === 0 || patchApplyLoading}>{patchApplyLoading ? t.patchApplying : t.patchApplyButton}</button></div>
            {patchPreview && <div className={`patch-result ${patchPreview.supported ? 'supported' : 'unsupported'}`}>
              <div><b>{patchPreview.intent.replaceAll('_', ' ')}</b><span>{patchPreview.supported ? t.patchSupported : t.patchUnsupported}</span></div>
              <p>{patchPreview.message}</p>
              <p>{t.patchTaskId}: <code>{patchPreview.task_id}</code></p>
              {patchPreview.warnings.length > 0 && <ul>{patchPreview.warnings.map(item => <li key={item}>{item}</li>)}</ul>}
              {patchPreview.operations.length > 0 && <details open><summary>{t.patchOperations}</summary><pre>{JSON.stringify(patchPreview.operations, null, 2)}</pre></details>}
            </div>}
          </section>
          {selected ? <><label>{t.configLabel}</label><textarea className="json-editor" value={configText} onChange={event => setConfigText(event.target.value)} /><button className="wide" onClick={saveConfig}>{t.saveConfig}</button><button className="danger-link" onClick={deleteSelectedNode}>{t.deleteNode}</button></> : <p className="muted">{selectedEdge ? t.edgeSelectedHint : t.nodeHelp}</p>}
        </div>}
        {tab === 'test' && <div className="panel-body">
          <div className="panel-kicker">{t.deliveryGate}</div><h2>{t.acceptanceCases(acceptanceCaseViews.length)}</h2>
          <p className="muted">{t.acceptanceHelp}</p>
          <button className="wide" onClick={runTests}>{t.runAllTests}</button>
          <div className="acceptance-list">{acceptanceCaseViews.map(test => <section className="acceptance-card" key={test.id}>
            <div className="acceptance-card-head"><div><strong>{test.name}</strong><small>{test.requirement || t.noRequirementText}</small></div><span className={test.result ? (test.result.passed ? 'passed' : 'failed') : 'pending'}>{test.result ? (test.result.passed ? t.passedLabel : t.failedLabel) : t.notRunLabel}</span></div>
            <div className="acceptance-grid">
              <div><h4>{t.businessRequirement}</h4><p>{test.mandatory ? t.mandatoryLabel : t.optionalLabel}</p><pre>{JSON.stringify(test.inputs, null, 2)}</pre></div>
              <div><h4>{t.outputAssertions}</h4>{test.assertions.length ? <ul>{test.assertions.map((assertion, index) => <li key={index}><code>{(assertion.path || ['output']).join('.')}</code> {assertion.operator || 'exists'} {assertion.expected !== undefined ? <code>{JSON.stringify(assertion.expected)}</code> : null}</li>)}</ul> : <p>{t.noAssertions}</p>}</div>
              <div><h4>{t.structureGate}</h4>{test.requiredNodeTypes.length || test.requiredToolNodes.length ? <ul>{test.requiredNodeTypes.length > 0 && <li>{t.requiredBrickTypes}: <code>{test.requiredNodeTypes.join(', ')}</code></li>}{test.requiredToolNodes.length > 0 && <li>{t.requiredToolNodes}: <code>{test.requiredToolNodes.join(', ')}</code></li>}</ul> : <p>{t.noStructureGate}</p>}</div>
              <div><h4>{t.toolEvidence}</h4>{test.requiredTools.length || test.minimumToolCalls || test.requireCitedToolUrls ? <ul>{test.requiredTools.length > 0 && <li>{t.requiredRuntimeTools}: <code>{test.requiredTools.join(', ')}</code></li>}{test.minimumToolCalls > 0 && <li>{t.minToolCalls}: <code>{test.minimumToolCalls}</code></li>}<li>{test.requireCitedToolUrls ? t.citedUrlsRequired : t.citedUrlsNotRequired}</li></ul> : <p>{t.noToolGate}</p>}</div>
            </div>
            {test.result && <div className="acceptance-result"><h4>{t.latestResult}</h4><p>{t.runId}: <code>{test.result.run_id || '-'}</code></p><p>{t.usedTools}: <code>{asStringArray(asRecord(test.result.tool_evidence).used_tools).join(', ') || '-'}</code></p><p>{t.assertionPassCount}: <code>{(test.result.assertions || []).filter(item => item.passed).length}/{(test.result.assertions || []).length}</code></p></div>}
            <details><summary>{t.engineeringDetails}</summary><pre>{JSON.stringify(test.raw, null, 2)}</pre></details>
          </section>)}</div>
          {testReport && <><h3>{t.latestReport}</h3><pre className="trace-log">{JSON.stringify(testReport, null, 2)}</pre></>}
          <h3>{t.versionHistory}</h3>{versions.map(version => <div className="version-row" key={version.version}><span>v{version.version}</span><small>{version.content_hash.slice(0, 9)}</small><button onClick={async () => { await api(`/api/v1/applications/${id}/versions/${version.version}/restore`, { method: 'POST' }); await refresh() }}>{t.loadEdit}</button></div>)}
        </div>}
        {tab === 'run' && <div className="panel-body">
          <div className="panel-kicker">{t.runApplication}</div><h2>{t.runPublished}</h2>
          <p className="muted">{t.runHelp}</p>
          <div className="run-form">{runFields.length ? runFields.map(field => <label className="run-field" key={field.name}><span>{field.label || field.name}<em>{t.fieldType(field.type || 'string')}</em></span>{field.type === 'boolean' ? <input type="checkbox" checked={field.checked || false} onChange={event => updateRunField(field.name, { checked: event.target.checked, value: event.target.checked ? 'true' : 'false' })} /> : field.type === 'object' || field.type === 'array' || field.type === 'file_list' ? <textarea value={field.value} onChange={event => updateRunField(field.name, { value: event.target.value })} /> : <input type={fieldInputType(field.type)} value={field.value} onChange={event => updateRunField(field.name, { value: event.target.value })} />}</label>) : <p className="muted">{t.runInputsEmpty}</p>}</div>
          <label>{t.runInputPreview}</label><pre className="trace-log">{runInputPreview}</pre>
          <div className="run-actions"><button className="wide" onClick={() => startRun(true)}>{t.runDraftButton}</button><button className="wide secondary" onClick={() => startRun(false)} disabled={!activeVersion}>{t.runPublishedButton}</button></div>
          {!activeVersion && <p className="muted">{t.noPublishedVersion}</p>}
          {run && <div className="run-result"><b>{run.status}</b><button className="danger-link" onClick={cancelRun} disabled={['succeeded', 'failed', 'paused', 'cancelled'].includes(run.status)}>{t.cancelRun}</button><pre>{JSON.stringify(run.outputs || run.error, null, 2)}</pre>{run.status === 'paused' && <><label>{t.humanInput}</label><textarea value={humanValues} onChange={event => setHumanValues(event.target.value)} /><button onClick={resumeRun}>{t.resume}</button></>}</div>}
          {pendingPermission && <div className="permission-card"><h3>{t.permissionWaiting}</h3><p>{t.permissionTool}: <code>{pendingPermission.tool || '-'}</code>{pendingPermission.node_id ? <> · <code>{pendingPermission.node_id}</code></> : null}</p><pre>{JSON.stringify(pendingPermission.input || {}, null, 2)}</pre><div className="run-actions"><button className="wide" onClick={() => resolvePermission(pendingPermission, 'allow')}>{t.approvePermission}</button><button className="wide secondary" onClick={() => resolvePermission(pendingPermission, 'deny')}>{t.denyPermission}</button></div></div>}
          {runEvents.length > 0 && <><h3>{t.traceTitle}</h3><pre className="trace-log">{JSON.stringify(visibleRunEvents(runEvents), null, 2)}</pre></>}
        </div>}
        {tab === 'monitor' && <div className="panel-body">
          <div className="panel-kicker">{t.platformHarness}</div><h2>{t.taskMonitor}</h2>
          <p className="muted">{t.monitorHelp}</p>
          <div className="monitor-summary">
            <span><b>{monitorSummary.related}</b>{t.monitorRelated}</span>
            <span><b>{monitorSummary.running}</b>{t.monitorRunning}</span>
            <span><b>{monitorSummary.failed}</b>{t.monitorFailed}</span>
            <span><b>{monitorSummary.total}</b>{t.monitorTotal}</span>
          </div>
          <div className="monitor-toolbar">
            {(['related', 'failed', 'all'] as const).map(filter => <button className={monitorFilter === filter ? 'active' : ''} onClick={() => setMonitorFilter(filter)} key={filter}>{filter === 'related' ? t.monitorFilterRelated : filter === 'failed' ? t.monitorFilterFailed : t.monitorFilterAll}</button>)}
            <button className="refresh" onClick={() => { refreshMonitorTasks().catch(error => setNotice(String(error))); refreshPolicyControls().catch(error => setNotice(String(error))); refreshBenchmarkHistory().catch(error => setNotice(String(error))); refreshAdaptiveMonitoring().catch(error => setNotice(String(error))); refreshGovernedMemoryItems().catch(error => setNotice(String(error))) }} disabled={monitorLoading || policyControlsLoading || benchmarkHistoryLoading || adaptiveMonitoringLoading || governedMemoryLoading}>{monitorLoading || policyControlsLoading || benchmarkHistoryLoading || adaptiveMonitoringLoading || governedMemoryLoading ? t.monitorRefreshing : t.monitorRefresh}</button>
          </div>
          {monitorError && <p className="error-banner">{monitorError}</p>}
          <section className="governed-memory-panel">
            <div className="governed-memory-head">
              <div><strong>{t.governedMemoryTitle}</strong><small>{t.governedMemoryHelp}</small></div>
              <button onClick={() => { refreshGovernedMemoryItems().catch(error => setNotice(String(error))) }} disabled={governedMemoryLoading}>{governedMemoryLoading ? t.monitorRefreshing : t.monitorRefresh}</button>
            </div>
            {governedMemoryError && <p className="error-banner">{governedMemoryError}</p>}
            {governedMemoryNotice && <p className="success-banner">{governedMemoryNotice}</p>}
            <form className="governed-memory-form" onSubmit={createGovernedMemory}>
              <label>
                {t.governedMemoryScope}
                <input value={governedMemoryForm.scope_id} onChange={event => setGovernedMemoryForm({ ...governedMemoryForm, scope_id: event.target.value })} required />
              </label>
              <label>
                {t.governedMemoryActor}
                <input value={governedMemoryForm.actor_id} onChange={event => setGovernedMemoryForm({ ...governedMemoryForm, actor_id: event.target.value })} required />
              </label>
              <label className="policy-wide">
                {t.governedMemoryPurpose}
                <input value={governedMemoryForm.purpose} onChange={event => setGovernedMemoryForm({ ...governedMemoryForm, purpose: event.target.value })} required />
              </label>
              <label className="policy-wide">
                {t.policyReason}
                <input value={governedMemoryForm.reason} onChange={event => setGovernedMemoryForm({ ...governedMemoryForm, reason: event.target.value })} required />
              </label>
              <label className="policy-wide">
                {t.governedMemoryContent}
                <textarea value={governedMemoryForm.content} onChange={event => setGovernedMemoryForm({ ...governedMemoryForm, content: event.target.value })} />
              </label>
              <label>
                {t.governedMemorySourceType}
                <input value={governedMemoryForm.source_type} onChange={event => setGovernedMemoryForm({ ...governedMemoryForm, source_type: event.target.value })} required />
              </label>
              <label>
                {t.governedMemorySourceId}
                <input value={governedMemoryForm.source_id} onChange={event => setGovernedMemoryForm({ ...governedMemoryForm, source_id: event.target.value })} required />
              </label>
              <label>
                {t.governedMemoryRetention}
                <select value={governedMemoryForm.retention_class} onChange={event => setGovernedMemoryForm({ ...governedMemoryForm, retention_class: event.target.value as GovernedMemoryForm['retention_class'] })}>
                  <option value="session">session</option>
                  <option value="project">project</option>
                  <option value="user_renewable">user_renewable</option>
                </select>
              </label>
              <label>
                {t.governedMemoryExpires}
                <input type="datetime-local" value={governedMemoryForm.expires_at} onChange={event => setGovernedMemoryForm({ ...governedMemoryForm, expires_at: event.target.value })} />
              </label>
              <label className="policy-wide">
                {t.governedMemoryEvidence}
                <textarea value={governedMemoryForm.evidence_text} onChange={event => setGovernedMemoryForm({ ...governedMemoryForm, evidence_text: event.target.value })} />
              </label>
              <div className="policy-actions">
                <button type="submit" disabled={governedMemorySaving || !governedMemoryForm.content.trim()}>{governedMemorySaving ? t.policySaving : t.governedMemoryCreate}</button>
              </div>
            </form>
            <div className="governed-memory-toolbar">
              {(['active', 'revoked', 'expired', 'all'] as const).map(filter => <button className={governedMemoryFilter === filter ? 'active' : ''} onClick={() => setGovernedMemoryFilter(filter)} key={filter}>{filter === 'active' ? t.governedMemoryActive : filter === 'revoked' ? t.governedMemoryRevokedStatus : filter === 'expired' ? t.governedMemoryExpired : t.monitorFilterAll}</button>)}
            </div>
            <div className="governed-memory-list">{governedMemoryItems.length ? governedMemoryItems.map(item => <article className={`governed-memory-card ${item.status}`} key={item.id}>
              <div className="governed-memory-card-head">
                <div><strong>{item.source.source_id}</strong><small>{item.id}</small></div>
                <span>{item.status}</span>
              </div>
              <p>{item.status === 'active' ? item.content : t.governedMemoryInactiveContent}</p>
              <div className="monitor-counts"><span>{item.retention_class}</span><span>{t.governedMemoryExpires} <b>{shortTime(item.expires_at)}</b></span><span>{item.source.source_type}</span></div>
              <div className="monitor-times"><span>{t.monitorCreated}: {shortTime(item.created_at)}</span><span>{t.monitorUpdated}: {shortTime(item.updated_at)}</span>{item.revoked_at && <span>{t.governedMemoryRevokedAt}: {shortTime(item.revoked_at)}</span>}</div>
              {item.revoked_reason && <p className="monitor-error">{item.revoked_reason}</p>}
              <details><summary>{t.governedMemoryAuditMetadata}</summary><pre>{JSON.stringify({ source: item.source, owner_id: item.owner_id, scope_id: item.scope_id }, null, 2)}</pre></details>
              {item.status === 'active' && <button className="danger-link" onClick={() => { revokeGovernedMemory(item.id).catch(error => setNotice(String(error))) }} disabled={governedMemoryLoading}>{t.governedMemoryRevoke}</button>}
            </article>) : <p className="muted">{governedMemoryLoading ? t.monitorRefreshing : t.governedMemoryEmpty}</p>}</div>
            <details className="governed-memory-audit" open>
              <summary>{t.governedMemoryAudit}</summary>
              {governedMemoryAudit.length ? <pre>{JSON.stringify(governedMemoryAudit.slice(-12).reverse(), null, 2)}</pre> : <p className="muted">{t.governedMemoryAuditEmpty}</p>}
            </details>
          </section>
          <section className={`adaptive-monitoring ${adaptiveMonitoring?.status || ''}`}>
            <div className="adaptive-monitoring-head">
              <div><strong>{t.adaptiveMonitoringTitle}</strong><small>{t.adaptiveMonitoringHelp}</small></div>
              <button onClick={() => { recordAdaptiveMonitoringRefresh().catch(error => setNotice(String(error))) }} disabled={adaptiveMonitoringLoading}>{adaptiveMonitoringLoading ? t.adaptiveRefreshing : t.adaptiveRefresh}</button>
            </div>
            {adaptiveMonitoringError && <p className="error-banner">{adaptiveMonitoringError}</p>}
            {adaptiveMonitoring ? <>
              <div className="adaptive-status-row">
                <span><b>{adaptiveMonitoring.status === 'healthy' ? t.adaptiveHealthy : adaptiveMonitoring.status === 'attention' ? t.adaptiveAttention : t.adaptiveMissing}</b>{adaptiveMonitoring.version}</span>
                <span><b>{adaptiveMonitoring.critical_alert_count}</b>{t.adaptiveCriticalAlerts}</span>
                <span><b>{adaptiveMonitoring.warning_alert_count}</b>{t.adaptiveWarningAlerts}</span>
                <span><b>{adaptiveMonitoring.override_options_visible ? t.policyEnabled : t.policyDisabled}</b>{t.adaptiveOverrides}</span>
                <span><b>{adaptiveMonitoring.last_refresh ? shortTime(adaptiveMonitoring.last_refresh.refreshed_at) : t.adaptiveNeverRefreshed}</b>{t.adaptiveLastRefresh}</span>
                <span><b>{adaptiveMonitoring.history_count}</b>{t.adaptiveHistory}</span>
              </div>
              <div className="adaptive-overrides">
                <strong>{t.adaptiveOverrides}</strong>
                <div>{adaptiveMonitoring.available_overrides.length ? adaptiveMonitoring.available_overrides.map(option => <code key={option}>{option}</code>) : <span>{t.policyNoAllowlist}</span>}</div>
              </div>
              <h3>{t.adaptiveCases}</h3>
              <div className="adaptive-case-list">{adaptiveMonitoring.cases.map(item => <article className="adaptive-case" key={`${item.family}-${item.mode}`}>
                <div className="adaptive-case-head"><strong>{item.family}</strong><span>{item.mode}</span></div>
                <div className="monitor-counts">
                  <span>{item.build_status}</span>
                  <span>{item.effective_depth}</span>
                  <span>{item.reuse_depth_source}</span>
                  <span>{t.adaptiveBenchmark} <b>{String(item.benchmark_passed)}</b></span>
                  <span>{t.adaptiveTimeout} <b>{String(item.timeout_like)}</b></span>
                </div>
                <small>{t.adaptiveSource}: {item.source}</small>
              </article>)}</div>
              <p className="muted">{adaptiveMonitoring.conclusion}</p>
            </> : <p className="muted">{adaptiveMonitoringLoading ? t.monitorRefreshing : t.adaptiveMonitoringEmpty}</p>}
          </section>
          <section className="policy-controls">
            <div className="policy-controls-head"><strong>{t.policyControlsTitle}</strong><small>{t.policyControlsHelp}</small></div>
            {policyControlsError && <p className="error-banner">{policyControlsError}</p>}
            {policyControls ? <>
              <div className="policy-summary">
                <span><b>{policyControls.network_egress_policy}</b>{t.policyNetwork}</span>
                <span><b>{policyControls.cancellation_policy === 'enabled' ? t.policyEnabled : t.policyDisabled}</b>{t.policyCancellation}</span>
                <span><b>{policyControls.secret_policy_enabled ? t.policyEnabled : t.policyDisabled}</b>{t.policySecrets}</span>
                <span><b>{policyControls.worker_lease_seconds || 0}s</b>{t.policyWorkerLease}</span>
                <span><b>{policyControls.stdio_mcp.allowlist_supported ? t.policyEnabled : t.policyDisabled}</b>stdio allowlist</span>
              </div>
              <div className="policy-allowlist">
                <strong>{t.policyAllowlist}</strong>
                <div>{policyControls.network_egress_allowlist.length ? policyControls.network_egress_allowlist.map(host => <code key={host}>{host}</code>) : <span>{t.policyNoAllowlist}</span>}</div>
              </div>
              {policyControlsNotice && <p className="success-banner">{policyControlsNotice}</p>}
              {policyForm && <form className="policy-edit-form" onSubmit={savePolicyControls}>
                <label>
                  {t.policyNetwork}
                  <select value={policyForm.network_egress_policy} onChange={event => setPolicyForm({ ...policyForm, network_egress_policy: event.target.value as PolicyControlsForm['network_egress_policy'] })}>
                    <option value="full">full</option>
                    <option value="allowlist">allowlist</option>
                    <option value="none">none</option>
                  </select>
                </label>
                <label>
                  {t.policyCancellation}
                  <select value={policyForm.cancellation_policy} onChange={event => setPolicyForm({ ...policyForm, cancellation_policy: event.target.value as PolicyControlsForm['cancellation_policy'] })}>
                    <option value="enabled">{t.policyEnabled}</option>
                    <option value="disabled">{t.policyDisabled}</option>
                  </select>
                </label>
                <label className="policy-toggle">
                  <input type="checkbox" checked={policyForm.secret_policy_enabled} onChange={event => setPolicyForm({ ...policyForm, secret_policy_enabled: event.target.checked })} />
                  {t.policySecrets}
                </label>
                <label>
                  {t.policyWorkerLease}
                  <input type="number" min="0" step="1" value={policyForm.worker_lease_seconds} onChange={event => setPolicyForm({ ...policyForm, worker_lease_seconds: event.target.value })} />
                </label>
                <label className="policy-wide">
                  {t.policyAllowlist}
                  <textarea value={policyForm.network_egress_allowlist} onChange={event => setPolicyForm({ ...policyForm, network_egress_allowlist: event.target.value })} />
                </label>
                <div className="policy-limit-grid">
                  {policyLimitKeys.map(key => <label key={key}>
                    {key.replaceAll('_', ' ')}
                    <input type="number" min="0" step="1" value={policyForm.limits[key] || '0'} onChange={event => setPolicyForm({ ...policyForm, limits: { ...policyForm.limits, [key]: event.target.value } })} />
                  </label>)}
                </div>
                <label className="policy-wide">
                  {t.policyReason}
                  <input value={policyForm.reason} onChange={event => setPolicyForm({ ...policyForm, reason: event.target.value })} required />
                </label>
                <div className="policy-actions">
                  <button type="submit" disabled={policyControlsSaving || !policyForm.reason.trim()}>{policyControlsSaving ? t.policySaving : t.policySave}</button>
                </div>
              </form>}
              <div className="e08-boundary">
                <div className="e08-boundary-head"><strong>{t.e08BoundaryTitle}</strong><span>{policyControls.e08_boundary.current_slice}</span></div>
                <div className="e08-boundary-grid">
                  <article>
                    <b>{t.e08SoftPassmode}</b>
                    <code>{policyControls.e08_boundary.soft_passmode.enforcement}</code>
                    <p>{policyControls.e08_boundary.soft_passmode.statement}</p>
                  </article>
                  <article>
                    <b>{t.e08HardBoundary}</b>
                    <code>{policyControls.e08_boundary.hard_boundary.enforcement}</code>
                    <p>{policyControls.e08_boundary.hard_boundary.statement}</p>
                  </article>
                </div>
                <div className="e08-control-list">{policyControls.e08_boundary.controls.map(control => <span key={control.id}><b>{control.label}</b><code>{control.layer} · {control.status}</code></span>)}</div>
                <small>{t.e08NotFullSidecar}: {String(policyControls.e08_boundary.not_full_sidecar_completion)} · {policyControls.e08_boundary.source}</small>
              </div>
              <h3>{t.policyStdioTitle}</h3>
              <div className="policy-decision-list">{policyControls.stdio_mcp.decisions.map(decision => <article className={`policy-decision ${decision.allowed ? 'allowed' : 'blocked'}`} key={decision.id}>
                <div className="policy-decision-head"><strong>{decision.label}</strong><span>{decision.allowed ? t.policyAllowed : t.policyBlocked}</span></div>
                <div className="policy-fields"><code>{t.policyMode}: {decision.mode}</code><code>platform: {decision.platform_policy}</code><code>agent: {decision.agent_network_policy}</code><code>sandbox: {decision.sandbox_network_policy || '-'}</code></div>
                <p><b>{t.policyReason}</b>{decision.reason}</p>
                <p><b>{t.policyAction}</b>{decision.operator_action}</p>
              </article>)}</div>
            </> : <p className="muted">{policyControlsLoading ? t.monitorRefreshing : t.policyControlsEmpty}</p>}
          </section>
          <section className="benchmark-history">
            <div className="benchmark-history-head"><strong>{t.benchmarkHistoryTitle}</strong><small>{t.benchmarkHistoryHelp}</small></div>
            {benchmarkHistoryError && <p className="error-banner">{benchmarkHistoryError}</p>}
            <div className="benchmark-history-list">{benchmarkHistory.length ? benchmarkHistory.map(record => {
              const score = record.metadata.score
              const passRate = record.metadata.pass_rate
              return <article className="benchmark-history-card" key={record.id}>
                <div><strong>{record.resource_id}</strong><span className={record.status}>{record.status}</span></div>
                <small>{record.owner_id} · {shortTime(record.updated_at)}</small>
                <div className="monitor-counts">{score !== undefined && <span>{t.benchmarkScore} <b>{String(score)}</b></span>}{passRate !== undefined && <span>{t.benchmarkPassRate} <b>{String(passRate)}</b></span>}{Object.entries(record.usage_counts).map(([name, amount]) => <span key={name}>{name.replaceAll('_', ' ')} <b>{amount}</b></span>)}</div>
                {record.error && <p className="monitor-error">{record.error}</p>}
                <details><summary>{t.engineeringDetails}</summary><pre>{JSON.stringify(record.metadata, null, 2)}</pre></details>
              </article>
            }) : <p className="muted">{benchmarkHistoryLoading ? t.monitorRefreshing : t.benchmarkHistoryEmpty}</p>}</div>
          </section>
          <div className="monitor-list">{visibleMonitorTasks.length ? visibleMonitorTasks.map(task => {
            const related = taskIsRelated(task, id, build, run)
            const usageEntries = Object.entries(task.usage_counts)
            const latestUsage = task.usage.slice(-3).reverse()
            return <section className={`monitor-card ${related ? 'related' : ''}`} key={task.id}>
              <div className="monitor-card-head">
                <div><strong>{task.kind.replaceAll('_', ' ')}</strong><small>{task.id}</small></div>
                <span className={task.status}>{task.status}</span>
              </div>
              <div className="monitor-meta"><code>{task.owner_id}</code><code>{task.resource_id}</code></div>
              <div className="monitor-counts">{usageEntries.length ? usageEntries.map(([name, amount]) => <span key={name}>{name.replaceAll('_', ' ')} <b>{amount}</b></span>) : <span>{t.monitorNoUsage}</span>}</div>
              {task.error && <p className="monitor-error">{task.error}</p>}
              <div className="monitor-times"><span>{t.monitorCreated}: {shortTime(task.created_at)}</span><span>{t.monitorUpdated}: {shortTime(task.updated_at)}</span>{task.worker_id && <span>{t.monitorWorker}: {task.worker_id}</span>}{task.lease_expires_at && <span>{t.monitorLeaseExpires}: {shortTime(task.lease_expires_at)} · v{task.lease_version || 0}</span>}</div>
              {latestUsage.length > 0 && <details><summary>{t.monitorLatestUsage}</summary><pre>{JSON.stringify(latestUsage, null, 2)}</pre></details>}
            </section>
          }) : <p className="muted">{t.monitorEmpty}</p>}</div>
        </div>}
      </aside>
      <section className="canvas-wrap">
        {authRequired && <form className="auth-card studio-auth-card" onSubmit={saveToken}>
          <div><strong>{t.authTitle}</strong><p>{t.authCopy}</p></div>
          <input type="password" value={tokenInput} placeholder={t.authPlaceholder} onChange={event => setTokenInput(event.target.value)} />
          <div className="auth-actions"><button>{t.authSave}</button><button type="button" className="ghost" onClick={() => { clearClientToken(); setTokenInput('') }}>{t.authClear}</button></div>
        </form>}
        <div className="canvas-guidance">
          <section className="draft-readiness">
            <div className="draft-readiness-head"><strong>{t.draftReadinessTitle}</strong><small>{t.draftReadinessHelp}</small></div>
            <div className="readiness-grid">{readinessCards.map(card => <article className={card.ready ? 'ready' : 'needs-action'} key={card.label}>
              <span>{card.label}</span>
              <b>{card.ready ? t.readyLabel : t.needsActionLabel}</b>
              <small>{card.detail}</small>
            </article>)}</div>
          </section>
          <section className="canvas-guide" data-detail-guidance="first-run-orientation">
            <div><strong>{t.canvasGuideTitle}</strong><small>{t.canvasStats(canvasStats.nodes, canvasStats.edges)}</small></div>
            <p>{t.canvasGuideCopy}</p>
            <div className="detail-signal-grid">{detailSignals.map(signal => <article className={signal.ready ? 'ready' : 'needs-action'} key={signal.label}>
              <span>{signal.label}</span>
              <b>{signal.value}</b>
            </article>)}</div>
            <ul>{t.canvasGuideSteps.map(item => <li key={item}>{item}</li>)}</ul>
          </section>
          <section className="next-action-checklist" data-detail-guidance="next-action-checklist">
            <div className="next-action-head"><strong>{t.nextActionTitle}</strong><small>{t.nextActionHelp}</small></div>
            <div className="next-action-list">{nextActionCards.map(action => <button
              className={action.ready ? 'ready' : 'needs-action'}
              data-next-action={action.id}
              key={action.id}
              onClick={() => setTab(action.target)}
              type="button"
            >
              <span>{action.label}</span>
              <b>{action.ready ? t.readyLabel : t.needsActionLabel}</b>
              <small>{action.detail}</small>
              <em>{t.nextActionOpen}</em>
            </button>)}</div>
          </section>
        </div>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} deleteKeyCode={['Backspace', 'Delete']} onInit={instance => { flowRef.current = instance; scheduleFitView(nodes) }} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onNodesDelete={deleted => { void persistDeletedNodes(deleted as StudioNode[]) }} onEdgesDelete={deleted => { void persistDeletedEdges(deleted) }} onNodeClick={(_, node) => chooseNode(node)} onEdgeClick={(_, edge) => chooseEdge(edge)} onPaneClick={() => setSelectedNode(null)} onNodeDragStop={(_, node) => mutation('update_node', { node_id: node.id, changes: { position: node.position } })} fitView fitViewOptions={{ padding: 0.22 }} colorMode="dark">
          <Background color="#283142" gap={24} size={1}/><MiniMap pannable zoomable nodeColor={node => accents[(node.data as { blockType?: string } | undefined)?.blockType || ''] || '#64748b'}/><Controls/>
        </ReactFlow>
        {notice && <button className="toast" onClick={() => setNotice('')}>{notice}</button>}
      </section>
      <aside className="block-panel"><div className="block-heading"><span>{t.bricks}</span><small>{t.available(blocks.length)}</small></div>{Object.entries(grouped).map(([category, items]) => <div className="block-group" key={category}><h4>{items?.[0] ? blockCategory(items[0]) : category}</h4>{items?.map(block => <button onClick={() => addBlock(block)} key={block.type}><i style={{ background: accents[block.type] || '#64748b' }}/><span><b>{blockTitle(block)}</b><small>{blockDescription(block)}</small></span><em>+</em></button>)}</div>)}</aside>
    </div>
  </main>
}
