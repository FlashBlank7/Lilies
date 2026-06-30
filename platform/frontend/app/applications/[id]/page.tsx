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
  type Block,
  type Draft,
  type WorkflowNode,
  withFrontendToken,
} from '@/lib/platform'
import { defaultLocale, isLocale, messages, nextLocale, type Locale } from '@/lib/i18n'

type StudioNode = Node<{ title: string; blockType: string; description: string; status?: string }>
type Copy = (typeof messages)[Locale]
type Version = { version: number; content_hash: string; created_at: string; validation_report: Record<string, unknown> }
type Build = { id: string; status: string; error?: string; team_state: { tasks: Array<Record<string, unknown>>; teammates: Record<string, Record<string, unknown>>; repair_cycles: number } }
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
  const [tab, setTab] = useState<'build' | 'edit' | 'test' | 'run'>('build')
  const [requirement, setRequirement] = useState('')
  const [runFields, setRunFields] = useState<RunInputFieldState[]>([])
  const [run, setRun] = useState<Run | null>(null)
  const [runEvents, setRunEvents] = useState<StoredEvent[]>([])
  const [testReport, setTestReport] = useState<Record<string, unknown> | null>(null)
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

  useEffect(() => {
    const stored = globalThis.localStorage?.getItem('foundry.locale')
    if (isLocale(stored)) setLocale(stored)
    setTokenInput(getClientToken())
    refresh().catch(error => setNotice(String(error)))
  }, [refresh])
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
    const result = await api<{ build_id: string }>(`/api/v1/applications/${id}/builds`, {
      method: 'POST', body: JSON.stringify({ requirement, auto_publish: true }),
    })
    history.replaceState(null, '', `?build=${result.build_id}`)
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
    watchRun(run.id)
  }

  const grouped = useMemo(() => groupBlocks(blocks), [blocks])
  const tested = draft?.tested_hash && draft.tested_hash === draft.content_hash
  const activeVersion = versions[0]?.version
  const acceptanceCaseViews = useMemo(() => acceptanceCases(draft, testReport), [draft, testReport])
  const runInputParsed = useMemo(() => parseRunFieldInputs(runFields, t), [runFields, t])
  const runInputPreview = JSON.stringify(runInputParsed.inputs || {}, null, 2)
  const pendingPermission = useMemo(() => latestPendingPermission(runEvents), [runEvents])
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
        <div className="panel-tabs">{(['build', 'edit', 'test', 'run'] as const).map(item => <button className={tab === item ? 'active' : ''} onClick={() => setTab(item)} key={item}>{item === 'build' ? t.buildTab : item === 'edit' ? t.editTab : item === 'test' ? t.testTab : t.runTab}</button>)}</div>
        {tab === 'build' && <div className="panel-body">
          <div className="panel-kicker">{t.builderTeam}</div><h2>{t.continueBuild}</h2>
          <textarea className="requirement-input" value={requirement} onChange={event => setRequirement(event.target.value)} />
          <button className="wide" onClick={startBuild}>{t.startTeam}</button>
          {build && <div className="build-status"><b>{build.status}</b><span>{Object.keys(build.team_state.teammates).length} teammates · {build.team_state.tasks.length} tasks · {build.team_state.repair_cycles} repairs</span>{build.error && <p>{build.error}</p>}</div>}
          <h3>{t.tasksTitle}</h3>
          <div className="test-list">{build?.team_state.tasks.map((task, index) => <pre key={index}>{JSON.stringify(task, null, 2)}</pre>) || <p className="muted">{t.tasksEmpty}</p>}</div>
          <h3>{t.architectureTitle}</h3>
          <div className="architecture-list">{architecture.map(item => <code key={item}>{item}</code>)}</div>
          <div className="event-log">{events.map((event, index) => <div key={index}><span>{event.type}</span><pre>{JSON.stringify(event.data, null, 2)}</pre></div>)}</div>
        </div>}
        {tab === 'edit' && <div className="panel-body">
          <div className="panel-kicker">{t.nodeInspector}</div><h2>{selected?.title || t.selectBrick}</h2>
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
      </aside>
      <section className="canvas-wrap">
        {authRequired && <form className="auth-card studio-auth-card" onSubmit={saveToken}>
          <div><strong>{t.authTitle}</strong><p>{t.authCopy}</p></div>
          <input type="password" value={tokenInput} placeholder={t.authPlaceholder} onChange={event => setTokenInput(event.target.value)} />
          <div className="auth-actions"><button>{t.authSave}</button><button type="button" className="ghost" onClick={() => { clearClientToken(); setTokenInput('') }}>{t.authClear}</button></div>
        </form>}
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} deleteKeyCode={['Backspace', 'Delete']} onInit={instance => { flowRef.current = instance; scheduleFitView(nodes) }} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onNodesDelete={deleted => { void persistDeletedNodes(deleted as StudioNode[]) }} onEdgesDelete={deleted => { void persistDeletedEdges(deleted) }} onNodeClick={(_, node) => chooseNode(node)} onEdgeClick={(_, edge) => chooseEdge(edge)} onPaneClick={() => setSelectedNode(null)} onNodeDragStop={(_, node) => mutation('update_node', { node_id: node.id, changes: { position: node.position } })} fitView fitViewOptions={{ padding: 0.22 }} colorMode="dark">
          <Background color="#283142" gap={24} size={1}/><MiniMap pannable zoomable nodeColor={node => accents[(node.data as { blockType?: string } | undefined)?.blockType || ''] || '#64748b'}/><Controls/>
        </ReactFlow>
        {notice && <button className="toast" onClick={() => setNotice('')}>{notice}</button>}
      </section>
      <aside className="block-panel"><div className="block-heading"><span>{t.bricks}</span><small>{t.available(blocks.length)}</small></div>{Object.entries(grouped).map(([category, items]) => <div className="block-group" key={category}><h4>{items?.[0] ? blockCategory(items[0]) : category}</h4>{items?.map(block => <button onClick={() => addBlock(block)} key={block.type}><i style={{ background: accents[block.type] || '#64748b' }}/><span><b>{blockTitle(block)}</b><small>{blockDescription(block)}</small></span><em>+</em></button>)}</div>)}</aside>
    </div>
  </main>
}
