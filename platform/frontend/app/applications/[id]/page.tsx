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
  SelectionMode,
  addEdge,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
  useEdgesState,
  useNodesState,
} from '@xyflow/react'
import { use, useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type MouseEvent } from 'react'
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
import { MarkdownResultCard } from '@/lib/markdown'
import { classifyRuntimeStatus, runtimeCommit, runtimeVersion, type RuntimeHealth } from '@/lib/runtime-status'

type StudioNode = Node<{ title: string; blockType: string; description: string; status?: string }>
type Copy = (typeof messages)[Locale]
const STUDIO_TABS = ['build', 'edit', 'test', 'run', 'monitor'] as const
type StudioTab = typeof STUDIO_TABS[number]
type MonitorFilter = 'related' | 'failed' | 'all'
type GovernedMemoryFilter = 'active' | 'revoked' | 'expired' | 'all'
type RunMode = 'unknown' | 'draft' | 'published'
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

function isStudioTab(value: string | null): value is StudioTab {
  return Boolean(value && STUDIO_TABS.includes(value as StudioTab))
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
const CANVAS_LAYOUT_ORIGIN = { x: 90, y: 110 }
const CANVAS_LAYOUT_COLUMN_WIDTH = 300
const CANVAS_LAYOUT_ROW_HEIGHT = 150
const CANVAS_PAN_STEP = 80

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

function arrangedCanvasPositions(workflowNodes: WorkflowNode[], workflowEdges: Draft['snapshot']['workflow']['edges']) {
  const depth = new Map(workflowNodes.map(node => [node.id, 0]))
  const incoming = new Map(workflowNodes.map(node => [node.id, 0]))
  const outgoing = new Map(workflowNodes.map(node => [node.id, [] as string[]]))

  workflowEdges.forEach(edge => {
    incoming.set(edge.target, (incoming.get(edge.target) || 0) + 1)
    outgoing.get(edge.source)?.push(edge.target)
  })

  const queue = workflowNodes.filter(node => incoming.get(node.id) === 0).map(node => node.id)
  const visited = new Set<string>()
  for (let index = 0; index < queue.length; index += 1) {
    const source = queue[index]
    visited.add(source)
    for (const target of outgoing.get(source) || []) {
      depth.set(target, Math.max(depth.get(target) || 0, (depth.get(source) || 0) + 1))
      incoming.set(target, (incoming.get(target) || 1) - 1)
      if (incoming.get(target) === 0) queue.push(target)
    }
  }

  const maxResolvedDepth = Math.max(0, ...Array.from(depth.values()))
  workflowNodes.filter(node => !visited.has(node.id)).forEach((node, index) => {
    depth.set(node.id, maxResolvedDepth + 1 + Math.floor(index / 4))
  })

  const rows = new Map<number, number>()
  return new Map(workflowNodes.map(node => {
    const column = depth.get(node.id) || 0
    const row = rows.get(column) || 0
    rows.set(column, row + 1)
    return [node.id, {
      x: CANVAS_LAYOUT_ORIGIN.x + column * CANVAS_LAYOUT_COLUMN_WIDTH,
      y: CANVAS_LAYOUT_ORIGIN.y + row * CANVAS_LAYOUT_ROW_HEIGHT,
    }]
  }))
}

function validWorkflowEdges(workflowNodes: WorkflowNode[], workflowEdges: Draft['snapshot']['workflow']['edges']) {
  const nodeIds = new Set(workflowNodes.map(node => node.id))
  return workflowEdges.filter(edge => nodeIds.has(edge.source) && nodeIds.has(edge.target))
}

function safeText(value: unknown, fallback = '') {
  return typeof value === 'string' && value.trim() ? value : fallback
}

function safeWorkflowNodeType(node: Partial<WorkflowNode> | null | undefined) {
  return safeText(node?.type, 'unknown')
}

function safeConfigKeys(value: unknown) {
  return value && typeof value === 'object' && !Array.isArray(value) ? Object.keys(value) : []
}

function canvasKeyboardPanDelta(key: string, modifiers: { shiftKey?: boolean; altKey?: boolean } = {}) {
  const step = modifiers.shiftKey ? CANVAS_PAN_STEP * 2 : modifiers.altKey ? CANVAS_PAN_STEP / 2 : CANVAS_PAN_STEP
  switch (key.toLowerCase()) {
    case 'w': return { x: 0, y: step }
    case 'a': return { x: step, y: 0 }
    case 's': return { x: 0, y: -step }
    case 'd': return { x: -step, y: 0 }
    default: return null
  }
}

function shouldIgnoreCanvasKeyboardTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest('button, a, input, textarea, select, [contenteditable="true"], [role="textbox"]'))
}

function panCanvasViewport(instance: ReactFlowInstance<StudioNode, Edge> | null, delta: { x: number; y: number }) {
  if (!instance) return false
  const viewport = instance.getViewport()
  void instance.setViewport({ ...viewport, x: viewport.x + delta.x, y: viewport.y + delta.y }, { duration: 110 })
  return true
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

const RUN_MODE_STORAGE_PREFIX = 'lilies.tryRunMode:'

function isRunMode(value: unknown): value is RunMode {
  return value === 'draft' || value === 'published' || value === 'unknown'
}

function runModeStorageKey(applicationId: string) {
  return `${RUN_MODE_STORAGE_PREFIX}${applicationId}`
}

function readStoredRunMode(applicationId: string): RunMode {
  if (typeof window === 'undefined') return 'unknown'
  try {
    const value = window.localStorage.getItem(runModeStorageKey(applicationId))
    return value === 'draft' || value === 'published' ? value : 'unknown'
  } catch {
    return 'unknown'
  }
}

function persistRunMode(applicationId: string, mode: RunMode) {
  if (typeof window === 'undefined' || !isRunMode(mode) || mode === 'unknown') return
  try {
    window.localStorage.setItem(runModeStorageKey(applicationId), mode)
  } catch {
    // localStorage may be disabled; visible mode still works for the current session.
  }
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

function compactSampleValue(value: unknown) {
  const raw = typeof value === 'string' ? value : JSON.stringify(value)
  const text = raw === undefined || raw === '' ? '""' : raw
  return text.length > 62 ? `${text.slice(0, 59)}...` : text
}

function markdownFence(value: string, language = '') {
  return `\`\`\`${language}\n${value.replace(/```/g, '``\\`')}\n\`\`\``
}

function markdownInlineScalar(value: unknown) {
  if (value === null) return '`null`'
  if (value === undefined) return '`undefined`'
  const text = String(value)
  return text.includes('\n') ? markdownFence(text) : text
}

function markdownValue(value: unknown): string {
  if (typeof value === 'string') return value.trim() || '""'
  if (typeof value === 'number' || typeof value === 'boolean' || value === null || value === undefined) return markdownInlineScalar(value)
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]'
    if (value.every(item => item === null || ['string', 'number', 'boolean'].includes(typeof item))) {
      return value.map(item => `- ${String(item).replace(/\n/g, ' ')}`).join('\n')
    }
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>)
    if (entries.length && entries.every(([, item]) => item === null || ['string', 'number', 'boolean'].includes(typeof item))) {
      return entries.map(([key, item]) => `- **${key}:** ${String(item).replace(/\n/g, ' ')}`).join('\n')
    }
  }
  return markdownFence(JSON.stringify(value, null, 2), 'json')
}

function workflowRunResultMarkdown(run: Run | null, t: Copy) {
  if (!run) return ''
  const outputs = Object.entries(run.outputs || {})
  if (outputs.length) {
    return outputs.map(([key, value]) => {
      const body = markdownValue(value)
      return outputs.length === 1 ? body : `## ${key}\n\n${body}`
    }).join('\n\n---\n\n')
  }
  if (run.error) return `## ${t.tryResultErrorMarkdownTitle}\n\n${markdownValue(run.error)}`
  return ''
}

function sampleSourceKind(field: InputField, testInputs: Record<string, unknown>) {
  if (Object.prototype.hasOwnProperty.call(testInputs, field.name)) return 'acceptance_sample'
  if (field.default !== undefined && field.default !== null) return 'field_default'
  return 'generated_default'
}

function valueKind(value: unknown) {
  if (Array.isArray(value)) return 'array'
  if (value === null) return 'null'
  return typeof value
}

function isActiveRunStatus(status?: string | null) {
  return status === 'queued' || status === 'running'
}

function formatRunStatusCheckedAt(date = new Date()) {
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
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

function isJapaneseLearningWorkflowText(value: string) {
  const text = value.toLocaleLowerCase()
  return /(日语|日本語|日本人|口语|口語|動画|视频|评论|コメント|japanese|spoken|expression|video|comment)/i.test(text)
    && /(学生|学习|學習|learner|student|summary|总结|表达|表現)/i.test(text)
}

function detailBuildRequirementReadiness(requirement: string, t: Copy) {
  const text = requirement.trim()
  const normalized = text.toLocaleLowerCase()
  const signals = [
    { id: 'audience', label: t.requirementSignalAudience, detail: t.requirementSignalAudienceHint, ready: /(客户|用户|负责人|顾问|运营|审阅|学生|学习者|customer|user|owner|operator|consultant|reviewer|learner|student)/i.test(normalized) },
    { id: 'outcome', label: t.requirementSignalOutcome, detail: t.requirementSignalOutcomeHint, ready: /(输出|生成|给出|判断|分类|摘要|清单|result|output|generate|classify|summary|checklist)/i.test(normalized) },
    { id: 'acceptance', label: t.requirementSignalAcceptance, detail: t.requirementSignalAcceptanceHint, ready: /(验收|测试|必须|覆盖|acceptance|test|must|cover|verify)/i.test(normalized) },
    { id: 'detail', label: t.requirementSignalDetail, detail: t.requirementSignalDetailHint, ready: text.length >= 80 },
  ]
  const readyCount = signals.filter(signal => signal.ready).length
  return { signals, readyCount, total: signals.length, ready: readyCount >= 3 }
}

function detailBuildActionState(requirement: string, readinessReady: boolean, build: Build | null, buildIntentConfirmed: boolean, t: Copy) {
  if (build && ['queued', 'building'].includes(build.status)) return { id: 'busy', tone: 'busy', title: t.detailBuildActionBusyTitle, detail: t.detailBuildActionBusyDetail }
  if (requirement.trim().length < 10) return { id: 'add_detail', tone: 'attention', title: t.detailBuildActionAddDetailTitle, detail: t.detailBuildActionAddDetailDetail }
  if (buildIntentConfirmed) return { id: 'confirm_team', tone: 'warning', title: t.detailBuildActionConfirmTitle, detail: t.detailBuildActionConfirmDetail }
  if (readinessReady) return { id: 'arm_team', tone: 'ready', title: t.detailBuildActionArmTitle, detail: t.detailBuildActionArmDetail }
  return { id: 'improve_requirement', tone: 'attention', title: t.detailBuildActionImproveTitle, detail: t.detailBuildActionImproveDetail }
}

function recommendedDetailBuildAction(actionId: string, t: Copy) {
  if (actionId === 'busy') return { target: 'wait', tone: 'busy', label: t.detailBuildRecommendedBusyLabel, detail: t.detailBuildRecommendedBusyDetail, disabled: true }
  if (actionId === 'arm_team' || actionId === 'confirm_team') return { target: 'guarded_build_button', tone: actionId === 'confirm_team' ? 'warning' : 'ready', label: t.detailBuildRecommendedGuardLabel, detail: t.detailBuildRecommendedGuardDetail, disabled: false }
  if (actionId === 'improve_requirement') return { target: 'requirement_focus', tone: 'attention', label: t.detailBuildRecommendedImproveLabel, detail: t.detailBuildRecommendedImproveDetail, disabled: false }
  return { target: 'requirement_focus', tone: 'attention', label: t.detailBuildRecommendedAddDetailLabel, detail: t.detailBuildRecommendedAddDetailDetail, disabled: false }
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
  const [safeDraftLanding, setSafeDraftLanding] = useState(false)
  const [requirement, setRequirement] = useState('')
  const [buildDeadlineSeconds, setBuildDeadlineSeconds] = useState('')
  const [buildIntentConfirmed, setBuildIntentConfirmed] = useState(false)
  const [runFields, setRunFields] = useState<RunInputFieldState[]>([])
  const [run, setRun] = useState<Run | null>(null)
  const [runStatusCheckedAt, setRunStatusCheckedAt] = useState('')
  const [cancelConfirmRunId, setCancelConfirmRunId] = useState<string | null>(null)
  const [cancelRequestedRunId, setCancelRequestedRunId] = useState<string | null>(null)
  const [lastRunMode, setLastRunMode] = useState<RunMode>('unknown')
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
  const [workflowEditReferenceIds, setWorkflowEditReferenceIds] = useState<string[]>([])
  const [patchPreview, setPatchPreview] = useState<DraftPatchPreview | null>(null)
  const [patchPreviewLoading, setPatchPreviewLoading] = useState(false)
  const [patchApplyLoading, setPatchApplyLoading] = useState(false)
  const [canvasArranging, setCanvasArranging] = useState(false)
  const [humanValues, setHumanValues] = useState('{}')
  const [notice, setNotice] = useState('')
  const [tryInputRecoveryReady, setTryInputRecoveryReady] = useState(false)
  const [authRequired, setAuthRequired] = useState(false)
  const [tokenInput, setTokenInput] = useState('')
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth | null>(null)
  const [runtimeUnavailable, setRuntimeUnavailable] = useState(false)
  const eventSource = useRef<EventSource | null>(null)
  const draftRef = useRef<Draft | null>(null)
  const selectedId = useRef<string | null>(null)
  const selectedEdgeId = useRef<string | null>(null)
  const runFieldsRef = useRef<RunInputFieldState[]>([])
  const flowRef = useRef<ReactFlowInstance<StudioNode, Edge> | null>(null)
  const canvasWrapRef = useRef<HTMLElement>(null)
  const detailBuildRequirementRef = useRef<HTMLTextAreaElement>(null)
  const detailBuildStartButtonRef = useRef<HTMLButtonElement>(null)
  const runInputFormRef = useRef<HTMLDivElement>(null)
  const runInputPreviewRef = useRef<HTMLPreElement>(null)
  const tryInputErrorRef = useRef<HTMLElement>(null)
  const tryInputErrorSeenRef = useRef(false)
  const runControlsRef = useRef<HTMLDivElement>(null)
  const runResultRef = useRef<HTMLDivElement>(null)
  const runPermissionRef = useRef<HTMLDivElement>(null)
  const runHumanInputRef = useRef<HTMLTextAreaElement>(null)
  const runTraceRef = useRef<HTMLElement>(null)
  const latestRevision = useRef(0)
  const lastFitSignature = useRef('')
  const buildPoll = useRef<number | null>(null)
  const buildRefreshTimer = useRef<number | null>(null)
  const runPoll = useRef<number | null>(null)
  const setStudioTab = useCallback((next: StudioTab, options: { replace?: boolean } = {}) => {
    setTab(next)
    if (typeof window === 'undefined') return
    const query = new URLSearchParams(window.location.search)
    if (query.get('tab') === next) return
    query.set('tab', next)
    const nextUrl = `${window.location.pathname}?${query.toString()}`
    if (options.replace) window.history.replaceState(null, '', nextUrl)
    else window.history.pushState(null, '', nextUrl)
  }, [])
  const syncStudioTabFromLocation = useCallback(() => {
    if (typeof window === 'undefined') return
    const query = new URLSearchParams(window.location.search)
    const requestedTab = query.get('tab')
    if (isStudioTab(requestedTab)) setTab(requestedTab)
    setSafeDraftLanding(query.get('safeDraft') === '1')
  }, [])

  function dismissSafeDraftLanding() {
    setSafeDraftLanding(false)
    if (typeof window === 'undefined') return
    const query = new URLSearchParams(window.location.search)
    query.delete('safeDraft')
    const nextQuery = query.toString()
    window.history.replaceState(null, '', `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ''}`)
  }

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
    refreshRuntimeStatus()
    refresh().catch(error => setNotice(String(error)))
    refreshMonitorTasks().catch(error => setNotice(String(error)))
    refreshPolicyControls().catch(error => setNotice(String(error)))
    refreshBenchmarkHistory().catch(error => setNotice(String(error)))
    refreshAdaptiveMonitoring().catch(error => setNotice(String(error)))
  }, [refresh, refreshAdaptiveMonitoring, refreshBenchmarkHistory, refreshMonitorTasks, refreshPolicyControls])
  useEffect(() => {
    window.addEventListener('popstate', syncStudioTabFromLocation)
    return () => window.removeEventListener('popstate', syncStudioTabFromLocation)
  }, [syncStudioTabFromLocation])
  useEffect(() => {
    setLastRunMode(readStoredRunMode(id))
  }, [id])
  useEffect(() => {
    if (!run || !isActiveRunStatus(run.status) || cancelConfirmRunId !== run.id) setCancelConfirmRunId(null)
  }, [cancelConfirmRunId, run])
  useEffect(() => {
    if (!run || cancelRequestedRunId !== run.id || (!isActiveRunStatus(run.status) && run.status !== 'cancelled')) setCancelRequestedRunId(null)
  }, [cancelRequestedRunId, run])
  useEffect(() => {
    syncStudioTabFromLocation()
    const query = new URLSearchParams(window.location.search)
    const buildId = query.get('build')
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

  async function arrangeCanvasNodes() {
    const current = draftRef.current
    const workflowNodes = current?.snapshot.workflow.nodes || []
    if (!current || !workflowNodes.length) {
      setNotice(t.canvasArrangeEmpty)
      return
    }
    const workflowEdges = validWorkflowEdges(workflowNodes, current.snapshot.workflow.edges)
    const positions = arrangedCanvasPositions(workflowNodes, workflowEdges)
    const changedNodes = workflowNodes.filter(node => {
      const position = positions.get(node.id)
      return position && (position.x !== node.position.x || position.y !== node.position.y)
    })
    setNodes(renderNodes => renderNodes.map(node => ({ ...node, position: positions.get(node.id) || node.position })))
    canvasWrapRef.current?.focus({ preventScroll: true })
    window.setTimeout(() => flowRef.current?.fitView({ padding: 0.24, duration: 260 }), 30)
    if (!changedNodes.length) {
      setNotice(t.canvasArrangeDone)
      return
    }
    setCanvasArranging(true)
    try {
      let expectedRevision = current.revision
      for (const node of changedNodes) {
        const position = positions.get(node.id)
        if (!position) continue
        const next = await api<Draft>(`/api/v1/applications/${id}/draft`, {
          method: 'POST',
          body: JSON.stringify({
            expected_revision: expectedRevision,
            idempotency_key: idempotency(),
            op: 'update_node',
            data: { node_id: node.id, changes: { position } },
          }),
        })
        expectedRevision = next.revision
        draftRef.current = next
      }
      await refresh()
      setNotice(t.canvasArrangeDone)
      window.setTimeout(() => flowRef.current?.fitView({ padding: 0.24, duration: 260 }), 40)
    } catch (error) {
      setNotice(String(error))
      await refresh().catch(() => undefined)
    } finally {
      setCanvasArranging(false)
    }
  }

  function handleCanvasKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || shouldIgnoreCanvasKeyboardTarget(event.target)) return
    const delta = canvasKeyboardPanDelta(event.key, { shiftKey: event.shiftKey, altKey: event.altKey })
    if (!delta) return
    event.preventDefault()
    event.stopPropagation()
    panCanvasViewport(flowRef.current, delta)
  }

  function focusCanvasForKeyboard(event: MouseEvent<HTMLElement>) {
    const target = event.target
    if (shouldIgnoreCanvasKeyboardTarget(target)) return
    canvasWrapRef.current?.focus({ preventScroll: true })
  }

  function chooseNode(node: StudioNode) {
    const value = draft?.snapshot.workflow.nodes.find(item => item.id === node.id) || null
    setSelectedNode(value)
    setStudioTab('edit')
  }

  function chooseEdge(edge: Edge) {
    setSelectedWorkflowEdge(edge)
  }

  function addWorkflowEditReference(nodeId: string) {
    setWorkflowEditReferenceIds(current => current.includes(nodeId) ? current : [...current, nodeId])
  }

  function removeWorkflowEditReference(nodeId: string) {
    setWorkflowEditReferenceIds(current => current.filter(item => item !== nodeId))
  }

  function setWorkflowEditReferencesFromSelection(selectedNodes: StudioNode[]) {
    const ids = selectedNodes.map(node => node.id)
    if (ids.length) setWorkflowEditReferenceIds(ids)
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
        body: JSON.stringify({ instruction, reference_node_ids: workflowEditReferenceIds }),
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
    if (!buildIntentConfirmed) {
      setBuildIntentConfirmed(true)
      setNotice(t.buildIntentDetailConfirm)
      return
    }
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
    setBuildIntentConfirmed(false)
    void refreshMonitorTasks().catch(error => setNotice(String(error)))
    watchBuild(result.build_id)
  }

  function watchBuild(buildId: string) {
    eventSource.current?.close()
    if (buildPoll.current) window.clearInterval(buildPoll.current)
    buildPoll.current = null
    if (buildRefreshTimer.current) window.clearTimeout(buildRefreshTimer.current)
    buildRefreshTimer.current = null
    setStudioTab('build', { replace: true })
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

  function applySampleRunInputs() {
    const testInputs = firstMandatoryInputs(draft)
    const next = runFieldsRef.current.map(field => {
      const value = stringifyFieldValue(defaultInputValue(field, testInputs), field.type)
      return { ...field, value, checked: field.type === 'boolean' ? value === 'true' : undefined }
    })
    runFieldsRef.current = next
    setRunFields(next)
    setNotice(t.runSampleApplied)
  }

  async function startRun(useDraft = false) {
    if (isActiveRunStatus(run?.status)) {
      setNotice(t.tryRunActiveGuardNotice)
      return
    }
    const parsed = parseRunFieldInputs(runFieldsRef.current, t)
    if (parsed.error) {
      setNotice(t.tryInputErrorGuardNotice)
      tryInputErrorRef.current?.focus()
      return
    }
    const result = await api<{ run_id: string }>(`/api/v1/applications/${id}/runs`, {
      method: 'POST', body: JSON.stringify({ inputs: parsed.inputs, use_draft: useDraft, workspace_path: '.' }),
    })
    const mode: RunMode = useDraft ? 'draft' : 'published'
    setLastRunMode(mode)
    persistRunMode(id, mode)
    tryInputErrorSeenRef.current = false
    setTryInputRecoveryReady(false)
    setStudioTab('run')
    setRunEvents([])
    setRun({ id: result.run_id, status: 'queued', outputs: {}, state: {} })
    setRunStatusCheckedAt(formatRunStatusCheckedAt())
    setCancelConfirmRunId(null)
    setCancelRequestedRunId(null)
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
      setRunStatusCheckedAt(formatRunStatusCheckedAt())
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
    if (cancelConfirmRunId !== run.id) {
      setCancelConfirmRunId(run.id)
      setNotice(t.tryCancelConfirmNotice)
      return
    }
    await api(`/api/v1/runs/${run.id}/cancel`, { method: 'POST' })
    setCancelConfirmRunId(null)
    setCancelRequestedRunId(run.id)
    setRunStatusCheckedAt(formatRunStatusCheckedAt())
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
  const tryInputErrorVisible = Boolean(runInputParsed.error)
  const tryInputErrorBlockingRun = Boolean(runInputParsed.error)
  const tryInputRecoveryReadyVisible = tryInputRecoveryReady && !tryInputErrorBlockingRun && runFields.length > 0
  const runRecoveryHint = runInputParsed.error
    ? t.runMissingInputHelp
    : run?.status === 'failed'
      ? t.runFailureHelp
      : run?.status === 'paused'
        ? t.runPausedHelp
        : run?.status === 'succeeded'
          ? t.runSucceededHelp
          : t.runReadyHint
  const japaneseLearningWorkflow = useMemo(() => {
    const snapshot = draft?.snapshot
    if (!snapshot) return false
    const nodeText = snapshot.workflow.nodes.flatMap(node => [
      node.id,
      node.type,
      node.title,
      node.description,
      JSON.stringify(node.config),
    ])
    return isJapaneseLearningWorkflowText([
      snapshot.name,
      snapshot.description,
      snapshot.requirement,
      ...nodeText,
    ].join(' '))
  }, [draft])
  const runReadinessItems = [
    {
      label: t.tryReadinessDraft,
      ready: Boolean(draft),
      detail: draft ? `${t.draft} r${draft.revision}` : t.loading,
    },
    {
      label: t.tryReadinessInputs,
      ready: !runInputParsed.error,
      detail: runInputParsed.error || (runFields.length ? t.runReadyHint : t.runInputsEmpty),
    },
    {
      label: t.tryReadinessPublished,
      ready: Boolean(activeVersion),
      detail: activeVersion ? t.activeVersion(activeVersion) : t.noPublishedVersion,
    },
    {
      label: t.tryReadinessLastRun,
      ready: run?.status === 'succeeded',
      detail: run ? run.status : t.notRunLabel,
    },
  ]

  useEffect(() => {
    if (runInputParsed.error) {
      tryInputErrorSeenRef.current = true
      if (tryInputRecoveryReady) setTryInputRecoveryReady(false)
      return
    }
    if (tryInputErrorSeenRef.current && runFields.length > 0 && !tryInputRecoveryReady) {
      setTryInputRecoveryReady(true)
    }
  }, [runInputParsed.error, runFields.length, tryInputRecoveryReady])
  const trySampleSummaryItems = useMemo(() => {
    const testInputs = firstMandatoryInputs(draft)
    return runFields.map(field => {
      const value = defaultInputValue(field, testInputs)
      const source = sampleSourceKind(field, testInputs)
      const label = japaneseLearningWorkflow && /^(topic|query|customer_request)$/i.test(field.name)
        ? t.japaneseLearningTopicInputLabel
        : field.label || field.name
      return {
        name: field.name,
        label,
        type: field.type || 'string',
        required: field.required !== false,
        preview: compactSampleValue(value),
        source,
        sourceLabel: t.trySampleSource(source),
      }
    })
  }, [draft, japaneseLearningWorkflow, runFields, t])
  const trySampleRequiredCount = trySampleSummaryItems.filter(item => item.required).length
  const trySampleAcceptanceCount = trySampleSummaryItems.filter(item => item.source === 'acceptance_sample').length
  const trySampleVisibleItems = trySampleSummaryItems.slice(0, 3)
  const trySampleHiddenCount = Math.max(0, trySampleSummaryItems.length - trySampleVisibleItems.length)
  const trySampleNextAction = runFields.length === 0
    ? { id: 'no_inputs', label: t.trySampleNextNoInputs, detail: t.trySampleNextNoInputsDetail }
    : runInputParsed.error
      ? { id: 'fill_sample', label: t.trySampleNextFillSample, detail: t.trySampleNextFillSampleDetail }
      : { id: 'run_draft', label: t.trySampleNextRunDraft, detail: t.trySampleNextRunDraftDetail }
  const pendingPermission = useMemo(() => latestPendingPermission(runEvents), [runEvents])
  const visibleTraceEventsForRun = useMemo(() => visibleRunEvents(runEvents), [runEvents])
  const traceFailureCount = visibleTraceEventsForRun.filter(event => event.type.includes('failed') || event.type.includes('error') || String(event.data.status || '') === 'failed' || Boolean(event.data.error)).length
  const tracePermissionCount = visibleTraceEventsForRun.filter(event => event.type.includes('permission')).length
  const traceWorkflowCount = visibleTraceEventsForRun.filter(event => event.type.startsWith('workflow.')).length
  const traceNodeCount = visibleTraceEventsForRun.filter(event => event.type.startsWith('node.') && !event.type.includes('permission')).length
  const traceSummaryItems = [
    { label: t.traceEvents, ready: visibleTraceEventsForRun.length > 0, detail: String(visibleTraceEventsForRun.length) },
    { label: t.traceWorkflowEvents, ready: traceWorkflowCount > 0, detail: String(traceWorkflowCount) },
    { label: t.traceNodeEvents, ready: traceNodeCount > 0, detail: String(traceNodeCount) },
    { label: t.tracePermissionEvents, ready: tracePermissionCount === 0 && visibleTraceEventsForRun.length > 0, detail: String(tracePermissionCount) },
    { label: t.traceFailureEvents, ready: traceFailureCount === 0 && visibleTraceEventsForRun.length > 0, detail: String(traceFailureCount) },
  ]
  const traceGuidance = pendingPermission
    ? t.traceGuidancePermission
    : traceFailureCount > 0
      ? t.traceGuidanceFailure
      : visibleTraceEventsForRun.length > 0
        ? t.traceGuidanceReady
        : t.traceGuidanceEmpty
  const tryResultOutputCount = Object.keys(run?.outputs || {}).length
  const tryResultErrorPresent = Boolean(run?.error) || run?.status === 'failed'
  const tryResultOutcomeItems = [
    { label: t.tryResultStatus, ready: run?.status === 'succeeded', detail: run?.status || t.notRunLabel },
    { label: t.tryResultOutputs, ready: tryResultOutputCount > 0, detail: String(tryResultOutputCount) },
    { label: t.tryResultErrors, ready: !tryResultErrorPresent, detail: tryResultErrorPresent ? t.tryResultErrorPresent : t.tryResultErrorAbsent },
    { label: t.tryResultTrace, ready: visibleTraceEventsForRun.length > 0, detail: String(visibleTraceEventsForRun.length) },
  ]
  const tryResultNextAction = !run
    ? { id: 'not_run', target: 'run_controls', label: t.tryResultNextNotRun, detail: t.tryResultNextNotRunDetail }
    : pendingPermission
      ? { id: 'paused_permission', target: 'permission_card', label: t.tryResultNextPermission, detail: t.tryResultNextPermissionDetail }
      : run.status === 'paused'
        ? { id: 'paused_human_input', target: 'human_input_card', label: t.tryResultNextHumanInput, detail: t.tryResultNextHumanInputDetail }
        : run.status === 'failed'
          ? { id: 'failed_trace_retry', target: 'trace_panel', label: t.tryResultNextFailure, detail: t.tryResultNextFailureDetail }
          : run.status === 'succeeded'
            ? { id: 'succeeded_review', target: 'acceptance_tab', label: t.tryResultNextSucceeded, detail: t.tryResultNextSucceededDetail }
            : run.status === 'cancelled'
              ? { id: 'cancelled_retry', target: 'run_inputs', label: t.tryResultNextCancelled, detail: t.tryResultNextCancelledDetail }
              : { id: 'running_wait', target: 'result_panel', label: t.tryResultNextRunning, detail: t.tryResultNextRunningDetail }
  const tryResultOutputPreviewItems = useMemo(() => Object.entries(run?.outputs || {}).slice(0, 3).map(([key, value]) => ({
    key,
    kind: valueKind(value),
    preview: compactSampleValue(value),
  })), [run])
  const tryResultHiddenOutputCount = Math.max(0, tryResultOutputCount - tryResultOutputPreviewItems.length)
  const tryResultErrorPreview = run?.error ? compactSampleValue(run.error) : run?.status === 'failed' ? t.tryResultUnknownError : ''
  const tryResultMarkdownSource = useMemo(() => workflowRunResultMarkdown(run, t), [run, t])
  const tryResultRawPayload = run ? JSON.stringify(tryResultOutputCount ? run.outputs : run.error || {}, null, 2) : ''
  const tryRunModeTitle = lastRunMode === 'draft' ? t.tryRunModeDraft : lastRunMode === 'published' ? t.tryRunModePublished : t.tryRunModeUnknown
  const tryRunModeDetail = lastRunMode === 'draft' ? t.tryRunModeDraftDetail : lastRunMode === 'published' ? t.tryRunModePublishedDetail : t.tryRunModeUnknownDetail
  const tryRunActive = isActiveRunStatus(run?.status)
  const tryRunActiveStatus = run?.status || 'none'
  const tryCancelConfirmVisible = Boolean(run && cancelConfirmRunId === run.id && tryRunActive)
  const tryCancelProgressState = run && cancelRequestedRunId === run.id ? tryRunActive ? 'requested' : run.status === 'cancelled' ? 'completed' : 'none' : 'none'
  const tryCancelProgressVisible = tryCancelProgressState !== 'none'
  const tryRunStatusRecencyVisible = Boolean(run && runStatusCheckedAt)
  const customerRunOverviewItems = [
    { label: t.customerRunMetricInputs, value: String(runFields.length), detail: runFields.length ? t.customerRunInputsReady(runFields.length) : t.runInputsEmpty },
    { label: t.customerRunMetricSteps, value: String(draft?.snapshot.workflow.nodes.length || 0), detail: t.customerRunStepsReady(draft?.snapshot.workflow.nodes.length || 0) },
    { label: t.customerRunMetricMode, value: tryRunModeTitle, detail: tryRunModeDetail },
    { label: t.customerRunMetricStatus, value: run?.status || t.notRunLabel, detail: runStatusCheckedAt ? `${t.tryStatusRecencyLabel}: ${runStatusCheckedAt}` : t.customerRunNoStatusYet },
  ]
  const customerStepProgressItems = useMemo(() => {
    const workflow = draft?.snapshot.workflow
    if (!workflow) return []
    const statusForNode = (nodeId: string) => {
      const related = visibleTraceEventsForRun.filter(event => {
        const eventNodeId = String(event.data.node_id || '')
        return eventNodeId === nodeId || event.type.startsWith(`node.${nodeId}.`)
      })
      if (related.some(event => event.type.includes('failed') || event.type.includes('error') || Boolean(event.data.error))) return 'blocked'
      if (pendingPermission?.node_id === nodeId || run?.state.waiting_node_id === nodeId) return 'waiting'
      if (related.some(event => event.type === 'node.completed' || event.type.includes('.completed') || event.type === 'node.skipped' || event.type === 'node.degraded')) return 'completed'
      if (related.some(event => event.type === 'node.started' || event.type.includes('.started'))) return 'running'
      return run ? 'not_started' : 'idle'
    }
    return workflow.nodes.map((node, index) => {
      const status = statusForNode(node.id)
      const next = workflow.edges.filter(edge => edge.source === node.id).map(edge => edge.target)
      const scenarioStep = japaneseLearningWorkflow
        ? t.japaneseLearningProgressSteps[Math.min(index, t.japaneseLearningProgressSteps.length - 1)]
        : null
      return {
        id: node.id,
        title: scenarioStep?.title || safeText(node.title, node.id),
        index: index + 1,
        type: safeWorkflowNodeType(node).replaceAll('_', ' '),
        status,
        detail: scenarioStep?.detail || (next.length ? t.customerStepFlowsTo(next.join(', ')) : t.terminal),
      }
    })
  }, [draft, japaneseLearningWorkflow, pendingPermission?.node_id, run, t, visibleTraceEventsForRun])
  const customerCurrentStep = customerStepProgressItems.find(item => item.status === 'running' || item.status === 'waiting' || item.status === 'blocked')
    || customerStepProgressItems.find(item => item.status === 'not_started')
    || customerStepProgressItems.at(-1)
  const customerDataFlowItems = [
    { label: t.customerDataFlowInput, ready: !runInputParsed.error, detail: runInputParsed.error || (runFields.length ? t.customerRunInputsReady(runFields.length) : t.runInputsEmpty) },
    { label: t.customerDataFlowProgress, ready: visibleTraceEventsForRun.length > 0, detail: t.customerTraceEventCount(visibleTraceEventsForRun.length) },
    { label: t.customerDataFlowCurrentStep, ready: Boolean(customerCurrentStep && customerCurrentStep.status !== 'idle'), detail: customerCurrentStep ? `${customerCurrentStep.index}. ${customerCurrentStep.title} · ${t.customerStepStatus(customerCurrentStep.status)}` : t.customerNoWorkflowSteps },
    { label: t.customerDataFlowResult, ready: tryResultOutputCount > 0, detail: tryResultOutputCount > 0 ? t.customerOutputReady(tryResultOutputCount) : tryResultErrorPreview || t.tryResultPreviewEmpty },
  ]
  const customerResultState = run?.status === 'succeeded' && tryResultOutputCount > 0
    ? 'ready'
    : tryResultErrorPreview
      ? 'error'
      : run
        ? run.status
        : 'empty'
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
  const monitorReadabilityItems = [
    { label: t.monitorGuidanceRelated, ready: monitorSummary.related > 0, detail: t.monitorGuidanceRelatedDetail(monitorSummary.related) },
    { label: t.monitorGuidanceRunning, ready: monitorSummary.running === 0, detail: t.monitorGuidanceRunningDetail(monitorSummary.running) },
    { label: t.monitorGuidanceFailed, ready: monitorSummary.failed === 0, detail: t.monitorGuidanceFailedDetail(monitorSummary.failed) },
    { label: t.monitorGuidanceTotal, ready: monitorSummary.total > 0, detail: t.monitorGuidanceTotalDetail(monitorSummary.total) },
  ]
  const monitorGuidance = monitorSummary.failed > 0
    ? t.monitorGuidanceFailure
    : monitorSummary.running > 0
      ? t.monitorGuidanceRunningState
      : monitorSummary.related > 0
        ? t.monitorGuidanceHealthy
        : t.monitorGuidanceEmpty
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
  const acceptancePassedCount = acceptanceCaseViews.filter(test => test.result?.passed).length
  const acceptanceFailedCount = acceptanceCaseViews.filter(test => test.result && !test.result.passed).length
  const acceptanceReadinessItems = [
    { label: t.acceptanceReadinessCases, ready: acceptanceCaseViews.length > 0, detail: t.acceptanceCases(acceptanceCaseViews.length) },
    { label: t.acceptanceReadinessPassed, ready: acceptancePassedCount > 0 && acceptanceFailedCount === 0, detail: `${acceptancePassedCount}/${acceptanceCaseViews.length}` },
    { label: t.acceptanceReadinessFailures, ready: acceptanceFailedCount === 0, detail: String(acceptanceFailedCount) },
    { label: t.acceptanceReadinessPublish, ready: Boolean(tested), detail: tested ? t.nextActionPublishReady : t.nextActionPublishBlocked },
  ]
  const publishGuidance = activeVersion
    ? t.publishGuidancePublished(activeVersion)
    : tested
      ? t.publishGuidanceReady
      : t.publishGuidanceBlocked
  const runtimeStatus = classifyRuntimeStatus(runtimeHealth, { authRequired, unavailable: runtimeUnavailable })
  const runtimeStatusText = runtimeStatus === 'connected'
    ? t.runtimeStatusConnected(runtimeVersion(runtimeHealth))
    : runtimeStatus === 'auth_required'
      ? t.runtimeStatusAuthRequired
      : runtimeStatus === 'stale'
        ? t.runtimeStatusStale(runtimeVersion(runtimeHealth))
        : runtimeStatus === 'unavailable'
          ? t.runtimeStatusUnavailable
          : t.runtimeStatusChecking
  const runtimeStatusDetail = runtimeStatus === 'connected'
    ? t.runtimeStatusDetailConnected(runtimeCommit(runtimeHealth))
    : runtimeStatus === 'auth_required'
      ? t.runtimeStatusDetailAuthRequired
      : runtimeStatus === 'stale'
        ? t.runtimeStatusDetailStale
        : runtimeStatus === 'unavailable'
          ? t.runtimeStatusDetailUnavailable
          : t.runtimeStatusDetailChecking
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
  function runDetailBuildRecommendedAction() {
    if (detailBuildRecommendedAction.disabled) return
    if (detailBuildRecommendedAction.target === 'requirement_focus') {
      detailBuildRequirementRef.current?.focus()
      return
    }
    if (detailBuildRecommendedAction.target === 'guarded_build_button') {
      detailBuildStartButtonRef.current?.focus()
      setNotice(t.detailBuildRecommendedGuardDetail)
    }
  }

  function focusTryResultRecoveryTarget(target: string) {
    if (target === 'acceptance_tab') {
      setStudioTab('test')
      setNotice(t.tryResultFocusNotice(target))
      return
    }
    setStudioTab('run')
    window.setTimeout(() => {
      const element = target === 'permission_card'
        ? runPermissionRef.current
        : target === 'human_input_card'
          ? runHumanInputRef.current
          : target === 'trace_panel'
            ? runTraceRef.current
            : target === 'run_inputs'
              ? runInputFormRef.current
              : target === 'run_controls'
                ? runControlsRef.current
                : runResultRef.current
      const fallback = runResultRef.current || runControlsRef.current || runInputFormRef.current
      const focusTarget = element || fallback
      focusTarget?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      focusTarget?.focus({ preventScroll: true })
      setNotice(t.tryResultFocusNotice(target))
    }, 40)
  }

  function refreshRuntimeStatus() {
    return api<RuntimeHealth>('/health').then(health => {
      setRuntimeHealth(health)
      setRuntimeUnavailable(false)
    }).catch(() => {
      setRuntimeHealth(null)
      setRuntimeUnavailable(true)
    })
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
  const workflowEditReferenceNodes = useMemo(() => {
    const workflow = draft?.snapshot.workflow
    if (!workflow) return []
    const byId = new Map(workflow.nodes.map(node => [node.id, node]))
    return workflowEditReferenceIds.map(nodeId => byId.get(nodeId)).filter(Boolean) as WorkflowNode[]
  }, [draft, workflowEditReferenceIds])
  const workflowStepSummaryItems = useMemo(() => {
    const workflow = draft?.snapshot.workflow
    if (!workflow) return []
    return workflow.nodes.map((node, index) => {
      const next = workflow.edges.filter(edge => edge.source === node.id).map(edge => edge.target)
      const type = safeWorkflowNodeType(node)
      return {
        id: node.id,
        title: `${index + 1}. ${safeText(node.title, node.id)}`,
        detail: `${type.replaceAll('_', ' ')}${next.length ? ` -> ${next.join(', ')}` : ` -> ${t.terminal}`}`,
      }
    })
  }, [draft, t.terminal])
  const selectedConfigKeys = safeConfigKeys(selected?.config)
  const selectedNodeSummary = selected ? [
    { label: t.nodeInspectorRole, value: safeWorkflowNodeType(selected), detail: safeText(selected.description, t.nodeInspectorNoDescription) },
    { label: t.nodeInspectorConfig, value: t.nodeConfigSummary(selectedConfigKeys.length), detail: selectedConfigKeys.length ? selectedConfigKeys.slice(0, 4).join(', ') : t.nodeInspectorNoConfig },
    { label: t.nodeInspectorSafeNext, value: t.nodeInspectorSafeNextValue, detail: t.nodeInspectorSafeNextDetail },
  ] : []
  const detailBuildReadiness = detailBuildRequirementReadiness(requirement, t)
  const detailBuildAction = detailBuildActionState(requirement, detailBuildReadiness.ready, build, buildIntentConfirmed, t)
  const detailBuildRecommendedAction = recommendedDetailBuildAction(detailBuildAction.id, t)

  return <main className="studio-shell">
    <header className="studio-header">
      <Link href="/" className="back">←</Link>
      <div className="studio-title"><strong>{draft?.snapshot.name || t.loading}</strong><span>{draft?.snapshot.mode === 'chat' ? t.modeChat : t.modeWorkflow} · {t.draft} r{draft?.revision ?? 0}</span></div>
      <div className="header-center"><span className={tested ? 'verified' : 'unverified'}>{tested ? t.verified : t.unverified}</span>{activeVersion && <span>{t.activeVersion(activeVersion)}</span>}<span className={`runtime-chip ${runtimeStatus}`} data-runtime-status={runtimeStatus} title={runtimeStatusDetail}>{runtimeStatusText}</span></div>
      <div className="header-actions"><button className="lang-toggle" onClick={toggleLocale}>{t.switchLabel}</button><button className="ghost" onClick={() => setStudioTab('run')} type="button">{t.debugDraft}</button><button onClick={publish} disabled={!tested}>{t.publishVersion}</button></div>
    </header>
    <div className="studio-grid">
      <aside className="left-panel">
        <div className="panel-tabs" data-detail-tab-url-state="synced">{STUDIO_TABS.map(item => <button aria-pressed={tab === item} className={tab === item ? 'active' : ''} onClick={() => setStudioTab(item)} key={item} type="button">{item === 'build' ? t.buildTab : item === 'edit' ? t.editTab : item === 'test' ? t.testTab : item === 'run' ? t.runTab : t.monitorTab}</button>)}</div>
        {tab === 'build' && <div className="panel-body">
          <div className="panel-kicker">{t.builderTeam}</div><h2>{t.continueBuild}</h2>
          <textarea ref={detailBuildRequirementRef} className="requirement-input" value={requirement} onChange={event => { setRequirement(event.target.value); setBuildIntentConfirmed(false) }} />
          <section className={`requirement-readiness detail-build-readiness ${detailBuildReadiness.ready ? 'ready' : 'needs-detail'}`} data-detail-build-readiness="summary">
            <div className="requirement-readiness-head"><strong>{t.requirementReadinessTitle}</strong><span>{t.requirementReadinessScore(detailBuildReadiness.readyCount, detailBuildReadiness.total)}</span></div>
            <p>{detailBuildReadiness.ready ? t.requirementReadinessReady : t.requirementReadinessNeedsDetail}</p>
            <div className="requirement-readiness-list">{detailBuildReadiness.signals.map(signal => <article className={signal.ready ? 'ready' : ''} key={signal.id}>
              <b>{signal.label}</b>
              <small>{signal.detail}</small>
            </article>)}</div>
          </section>
          <section className={`create-action-explainer detail-build-action-explainer ${detailBuildAction.tone}`} data-detail-build-action-state={detailBuildAction.id}>
            <strong>{detailBuildAction.title}</strong>
            <span>{detailBuildAction.detail}</span>
          </section>
          <section className={`recommended-create-action detail-build-recommended-action ${detailBuildRecommendedAction.tone}`} data-detail-build-recommended-action={detailBuildAction.id} data-detail-build-recommended-target={detailBuildRecommendedAction.target}>
            <div><strong>{t.detailBuildRecommendedTitle}</strong><span>{detailBuildRecommendedAction.detail}</span></div>
            <button type="button" disabled={detailBuildRecommendedAction.disabled} onClick={runDetailBuildRecommendedAction}>{detailBuildRecommendedAction.label}</button>
          </section>
          <label className="run-field">
            <span>{t.buildDeadlineLabel}<em>{t.buildDeadlineHelp}</em></span>
            <input type="number" min="0.001" step="0.1" value={buildDeadlineSeconds} onChange={event => { setBuildDeadlineSeconds(event.target.value); setBuildIntentConfirmed(false) }} />
          </label>
          <section className={`build-intent-guard ${buildIntentConfirmed ? 'armed' : ''}`} data-build-intent={buildIntentConfirmed ? 'confirmed' : 'needs-confirmation'}>
            <strong>{t.buildIntentGuardTitle}</strong>
            <span>{buildIntentConfirmed ? t.buildIntentGuardArmed : t.buildIntentGuardSafe}</span>
          </section>
          <button ref={detailBuildStartButtonRef} className={`wide build-action ${buildIntentConfirmed ? 'armed' : ''}`} data-build-action="detail-start-builder-team" data-build-intent={buildIntentConfirmed ? 'confirmed' : 'needs-confirmation'} onClick={startBuild}>{buildIntentConfirmed ? t.startTeamConfirm : t.startTeam}</button>
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
          <div className="panel-kicker">{t.workflowEditKicker}</div><h2>{t.patchPreviewTitle}</h2>
          <section className="workflow-readable-summary" data-workflow-readable-summary="natural-language">
            <div className="workflow-readable-head"><strong>{t.workflowReadableTitle}</strong><small>{t.workflowReadableHelp}</small></div>
            <p><b>{t.workflowReadablePurpose}</b>{draft?.snapshot.requirement || draft?.snapshot.description || t.fallbackDescription}</p>
            <div className="workflow-readable-steps">{workflowStepSummaryItems.length ? workflowStepSummaryItems.map(item => <article key={item.id}><strong>{item.title}</strong><small>{item.detail}</small></article>) : <p className="muted">{t.nodeInspectorNoConfig}</p>}</div>
          </section>
          <section className="workflow-edit-dialog" data-workflow-edit-dialog="whole-workflow" data-workflow-edit-reference-count={workflowEditReferenceIds.length}>
            <div className="patch-panel-head"><strong>{t.patchPreviewTitle}</strong><small>{t.patchPreviewHelp}</small></div>
            <div className="workflow-edit-references" data-workflow-edit-references={workflowEditReferenceIds.length ? 'present' : 'empty'}>
              <div><strong>{t.workflowEditReferenceTitle}</strong><small>{t.workflowEditReferenceHelp}</small></div>
              {workflowEditReferenceNodes.length ? <div className="workflow-edit-reference-list">{workflowEditReferenceNodes.map(node => <button type="button" key={node.id} data-workflow-edit-reference-node={node.id} onClick={() => removeWorkflowEditReference(node.id)}>{safeText(node.title, node.id)}<span>{safeWorkflowNodeType(node)}</span></button>)}</div> : <p className="muted">{t.workflowEditReferenceEmpty}</p>}
              {workflowEditReferenceIds.length > 0 && <button type="button" className="ghost" data-workflow-edit-reference-action="clear" onClick={() => setWorkflowEditReferenceIds([])}>{t.workflowEditReferenceClear}</button>}
            </div>
            <textarea className="patch-input" data-workflow-edit-input="instruction" value={patchInstruction} placeholder={t.patchPreviewPlaceholder} onChange={event => setPatchInstruction(event.target.value)} />
            <div className="run-actions"><button className="wide" onClick={previewDraftPatch} disabled={patchPreviewLoading}>{patchPreviewLoading ? t.patchPreviewing : t.patchPreviewButton}</button><button className="wide secondary" onClick={applyDraftPatch} disabled={!patchPreview?.supported || patchPreview.operations.length === 0 || patchApplyLoading}>{patchApplyLoading ? t.patchApplying : t.patchApplyButton}</button></div>
            {patchPreview && <div className={`patch-result ${patchPreview.supported ? 'supported' : 'unsupported'}`}>
              <div><b>{patchPreview.intent.replaceAll('_', ' ')}</b><span>{patchPreview.supported ? t.patchSupported : t.patchUnsupported}</span></div>
              <p>{patchPreview.message}</p>
              <p>{t.patchTaskId}: <code>{patchPreview.task_id}</code></p>
              {patchPreview.warnings.length > 0 && <ul>{patchPreview.warnings.map(item => <li key={item}>{item}</li>)}</ul>}
              {patchPreview.operations.length > 0 && <details open><summary>{t.patchOperations}</summary><pre>{JSON.stringify(patchPreview.operations, null, 2)}</pre></details>}
            </div>}
          </section>
          <h3>{t.nodeInspector}</h3>
          <section className="node-inspector-guide" data-node-inspector={selected ? 'selection-summary' : selectedEdge ? 'edge-summary' : 'empty-selection'}>
            <div className="node-inspector-guide-head"><strong>{selected ? t.nodeInspectorSummaryTitle : selectedEdge ? t.nodeInspectorEdgeTitle : t.nodeInspectorNoSelectionTitle}</strong><small>{selected ? t.nodeInspectorSummaryHelp : selectedEdge ? t.nodeInspectorEdgeHelp : t.nodeInspectorNoSelectionHelp}</small></div>
            {selected && <><div className="node-summary-grid">{selectedNodeSummary.map(item => <article key={item.label}><span>{item.label}</span><b>{item.value}</b><small>{item.detail}</small></article>)}</div><button type="button" className="ghost" data-workflow-edit-reference-action="add-selected" onClick={() => addWorkflowEditReference(selected.id)}>{t.workflowEditReferenceAddSelected}</button></>}
            {selectedEdge && <div className="edge-summary"><code>{selectedEdge.source} → {selectedEdge.target}</code>{selectedEdge.label && <span>{selectedEdge.label}</span>}</div>}
          </section>
          {selected ? <><section className="safe-edit-guide" data-node-inspector="safe-edit-guide"><strong>{t.nodeInspectorSafeEditTitle}</strong><span>{t.nodeInspectorSafeEditHelp}</span></section><label>{t.configLabel}</label><textarea className="json-editor" value={configText} onChange={event => setConfigText(event.target.value)} /><button className="wide" onClick={saveConfig}>{t.saveConfig}</button><button className="danger-link" onClick={deleteSelectedNode}>{t.deleteNode}</button></> : <p className="muted">{selectedEdge ? t.edgeSelectedHint : t.nodeHelp}</p>}
        </div>}
        {tab === 'test' && <div className="panel-body">
          <div className="panel-kicker">{t.deliveryGate}</div><h2>{t.acceptanceCases(acceptanceCaseViews.length)}</h2>
          <p className="muted">{t.acceptanceHelp}</p>
          <section className="acceptance-readiness-panel" data-acceptance-guidance="readiness-summary">
            <div className="acceptance-readiness-head"><strong>{t.acceptanceReadinessTitle}</strong><small>{t.acceptanceReadinessHelp}</small></div>
            <div className="acceptance-readiness-list">{acceptanceReadinessItems.map(item => <article className={item.ready ? 'ready' : ''} key={item.label}><span>{item.label}</span><b>{item.ready ? t.tryReady : t.tryNeedsAttention}</b><small>{item.detail}</small></article>)}</div>
            <p className="publish-guidance" data-acceptance-guidance="publish-next-action">{publishGuidance}</p>
          </section>
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
          <div className="panel-kicker">{t.customerRunKicker}</div><h2>{t.customerRunTitle}</h2>
          <section className="customer-run-overview" data-customer-run-interface="overview">
            <div className="customer-run-head"><strong>{draft?.snapshot.name || t.runApplication}</strong><small>{t.customerRunHelp}</small></div>
            <p><b>{t.workflowReadablePurpose}</b>{draft?.snapshot.requirement || draft?.snapshot.description || t.fallbackDescription}</p>
            <div className="customer-run-metrics">{customerRunOverviewItems.map(item => <article key={item.label}><span>{item.label}</span><b>{item.value}</b><small>{item.detail}</small></article>)}</div>
            <div className="customer-run-step-preview">{customerStepProgressItems.length ? customerStepProgressItems.slice(0, 5).map(item => <article key={item.id}><span>{item.index}</span><div><strong>{item.title}</strong><small>{item.type} · {item.detail}</small></div></article>) : <p className="muted">{t.customerNoWorkflowSteps}</p>}</div>
          </section>
          {japaneseLearningWorkflow && <section className="scenario-run-guidance" data-customer-scenario="japanese-learning">
            <div><strong>{t.japaneseLearningScenarioTitle}</strong><small>{t.japaneseLearningScenarioHelp}</small></div>
            <span>{t.japaneseLearningTopicInputLabel}: <b>{t.japaneseLearningSampleTopic}</b></span>
            <small className="scenario-fixture-note"><b>{t.japaneseLearningControlledFixtureTitle}</b>{t.japaneseLearningControlledFixtureHelp}</small>
          </section>}
          <section className="try-readiness-panel" data-try-guidance="run-readiness">
            <div className="try-readiness-head"><strong>{t.tryReadinessTitle}</strong><small>{t.tryReadinessHelp}</small></div>
            <div className="try-readiness-list">{runReadinessItems.map(item => <article className={item.ready ? 'ready' : ''} key={item.label}><span>{item.label}</span><b>{item.ready ? t.tryReady : t.tryNeedsAttention}</b><small>{item.detail}</small></article>)}</div>
            <p className="run-recovery-hint" data-try-guidance={runInputParsed.error ? 'missing-input' : 'run-recovery'}>{runRecoveryHint}</p>
          </section>
          <section className="customer-start-panel" data-customer-run-interface="start-controls" ref={runControlsRef} tabIndex={-1} data-try-input-error-action-guard={tryInputErrorBlockingRun ? 'blocked' : 'ready'} data-try-input-recovery-ready={tryInputRecoveryReadyVisible ? 'restored' : 'inactive'}>
            <div className="customer-start-head"><strong>{t.customerStartTitle}</strong><small>{runFields.length ? t.customerStartHelp : t.customerStartNoInputHelp}</small></div>
            {japaneseLearningWorkflow && <p className="scenario-topic-hint" data-japanese-learning-topic-input="expected"><b>{t.japaneseLearningTopicInputLabel}</b><span>{t.japaneseLearningTopicInputHelp}</span></p>}
            <section className="try-sample-next-action" data-try-sample-next-action={trySampleNextAction.id}><span>{t.trySampleNextAction}</span><strong>{trySampleNextAction.label}</strong><small>{trySampleNextAction.detail}</small></section>
          <section className="try-sample-summary" data-try-sample-input="summary">
            <div className="try-sample-head"><strong>{t.trySampleSummaryTitle}</strong><small>{runFields.length ? t.trySampleSummaryHelp : t.runInputsEmpty}</small></div>
            <div className="try-sample-metrics"><span><b>{trySampleSummaryItems.length}</b>{t.trySampleFields}</span><span><b>{trySampleRequiredCount}</b>{t.trySampleRequired}</span><span><b>{trySampleAcceptanceCount}</b>{t.trySampleAcceptance}</span></div>
            {trySampleVisibleItems.length ? <div className="try-sample-list">{trySampleVisibleItems.map(item => <article key={item.name}><div><strong>{item.label}</strong><small>{t.fieldType(item.type)} · {item.required ? t.trySampleRequiredField : t.trySampleOptionalField}</small></div><code>{item.preview}</code><span>{item.sourceLabel}</span></article>)}</div> : <p className="muted">{t.runInputsEmpty}</p>}
            {trySampleHiddenCount > 0 && <small className="try-sample-more">{t.trySampleMoreFields(trySampleHiddenCount)}</small>}
          </section>
          <div className="run-actions compact"><button className="wide secondary" data-try-sample-action="fill-sample" onClick={applySampleRunInputs} disabled={!runFields.length}>{t.fillRunSample}</button></div>
          <div className="run-form" ref={runInputFormRef} tabIndex={-1}>{runFields.length ? runFields.map(field => {
            const displayLabel = japaneseLearningWorkflow && /^(topic|query|customer_request)$/i.test(field.name)
              ? t.japaneseLearningTopicInputLabel
              : field.label || field.name
            return <label className="run-field" key={field.name}><span>{displayLabel}<em>{t.fieldType(field.type || 'string')}</em></span>{field.type === 'boolean' ? <input type="checkbox" checked={field.checked || false} onChange={event => updateRunField(field.name, { checked: event.target.checked, value: event.target.checked ? 'true' : 'false' })} /> : field.type === 'object' || field.type === 'array' || field.type === 'file_list' ? <textarea value={field.value} onChange={event => updateRunField(field.name, { value: event.target.value })} /> : <input type={fieldInputType(field.type)} value={field.value} onChange={event => updateRunField(field.name, { value: event.target.value })} />}</label>
          }) : <p className="muted">{t.runInputsEmpty}</p>}</div>
          <details className="customer-raw-details" data-customer-run-interface="raw-payload"><summary>{t.runInputPreview}</summary><pre className="trace-log" data-try-input-preview="payload" ref={runInputPreviewRef} tabIndex={-1}>{runInputPreview}</pre></details>
          {tryInputErrorVisible && <section className="try-input-error" data-try-input-error="inline" data-try-input-error-source="parser" ref={tryInputErrorRef} tabIndex={-1}><strong>{t.tryInputErrorTitle}</strong><small>{runInputParsed.error}</small><span>{t.tryInputErrorDetail}</span><button type="button" data-try-input-error-action="focus-form" onClick={() => runInputFormRef.current?.focus()}>{t.tryInputErrorFocusAction}</button></section>}
          {tryRunActive && <section className="try-run-start-guard" data-try-run-start-guard="active" data-try-run-active-status={tryRunActiveStatus}><strong>{t.tryRunActiveGuardTitle}</strong><span>{t.tryRunActiveStatus(tryRunActiveStatus)}</span><small>{t.tryRunActiveRefreshDetail}</small><small>{t.tryRunActiveStaleDetail}</small></section>}
          {tryInputRecoveryReadyVisible && <section className="try-input-recovery-ready" data-try-input-recovery-ready="restored" data-try-input-recovery-confidence="valid-input-preview"><strong>{t.tryInputRecoveryReadyTitle}</strong><small>{t.tryInputRecoveryReadyDetail}</small><button type="button" data-try-input-recovery-ready-action="focus-preview" onClick={() => runInputPreviewRef.current?.focus()}>{t.tryInputRecoveryReadyAction}</button></section>}
          <div className="run-actions"><button className="wide" data-try-run-mode-action="draft" onClick={() => startRun(true)} disabled={tryRunActive || tryInputErrorBlockingRun}>{t.runDraftButton}</button><button className="wide secondary" data-try-run-mode-action="published" onClick={() => startRun(false)} disabled={!activeVersion || tryRunActive || tryInputErrorBlockingRun}>{t.runPublishedButton}</button></div>
          {tryInputErrorBlockingRun && <section className="try-input-action-guard" data-try-input-action-guard="blocked"><strong>{t.tryInputErrorGuardTitle}</strong><small>{t.tryInputErrorGuardDetail}</small><button type="button" data-try-input-action-guard-focus="error" onClick={() => tryInputErrorRef.current?.focus()}>{t.tryInputErrorGuardFocusAction}</button></section>}
          {!activeVersion && <p className="muted">{t.noPublishedVersion}</p>}
          </section>
          <section className="customer-progress-panel" data-customer-run-interface="step-progress" data-customer-run-current-step={customerCurrentStep?.id || 'none'}>
            <div className="customer-progress-head"><strong>{t.customerProgressTitle}</strong><small>{t.customerProgressHelp}</small></div>
            <div className="customer-data-flow">{customerDataFlowItems.map(item => <article className={item.ready ? 'ready' : ''} key={item.label}><span>{item.label}</span><b>{item.ready ? t.tryReady : t.tryNeedsAttention}</b><small>{item.detail}</small></article>)}</div>
            <div className="customer-step-list">{customerStepProgressItems.length ? customerStepProgressItems.map(item => <article className={item.status} key={item.id} data-customer-run-step-status={item.status}><span>{item.index}</span><div><strong>{item.title}</strong><small>{item.type} · {item.detail}</small></div><b>{t.customerStepStatus(item.status)}</b></article>) : <p className="muted">{t.customerNoWorkflowSteps}</p>}</div>
          </section>
          <section className="customer-result-panel" data-customer-run-interface="result-card" data-customer-result-state={customerResultState}>
            <div className="customer-result-head"><strong>{t.customerResultTitle}</strong><small>{run ? t.customerResultStatus(run.status) : t.customerResultEmpty}</small></div>
            {japaneseLearningWorkflow && <section className="scenario-result-expectation" data-japanese-learning-result-expectation="spoken-summary">
              <div><strong>{t.japaneseLearningResultExpectationTitle}</strong><small>{t.japaneseLearningResultExpectationHelp}</small></div>
              <ul>{t.japaneseLearningResultChecklist.map(item => <li key={item}>{item}</li>)}</ul>
            </section>}
            {tryResultMarkdownSource ? <MarkdownResultCard className="customer-result-markdown" dataSurface="customer-run-result" source={tryResultMarkdownSource} emptyLabel={t.tryResultPreviewEmpty} title={t.customerResultRenderedTitle} description={t.customerResultRenderedHelp} openLabel={t.markdownOpenRendered} closeLabel={t.markdownCloseRendered} /> : tryResultErrorPreview ? <p className="try-result-error-preview">{tryResultErrorPreview}</p> : <p className="muted">{t.customerResultEmpty}</p>}
            <div className="try-result-next-action" data-try-result-next-action={tryResultNextAction.id}><span>{t.tryResultNextAction}</span><strong>{tryResultNextAction.label}</strong><small>{tryResultNextAction.detail}</small><button type="button" data-try-result-focus-target={tryResultNextAction.target} onClick={() => focusTryResultRecoveryTarget(tryResultNextAction.target)}>{t.tryResultFocusAction}</button></div>
          </section>
          {run && <div className="run-result" data-run-status={run.status} ref={runResultRef} tabIndex={-1}><b>{run.status}</b>{tryRunStatusRecencyVisible && <section className="try-status-recency" data-try-status-recency="last-checked" data-try-status-recency-status={run.status}><strong>{t.tryStatusRecencyLabel}: {runStatusCheckedAt}</strong><small>{t.tryStatusRecencyDetail}</small></section>}<section className="try-run-mode" data-try-run-mode={lastRunMode}><span>{t.tryRunModeLabel}</span><strong>{tryRunModeTitle}</strong><small>{tryRunModeDetail}</small></section><section className="try-result-outcome" data-try-result-outcome="summary"><div className="try-result-head"><strong>{t.tryResultOutcomeTitle}</strong><small>{t.tryResultStatusMeaning(run.status)}</small></div><div className="try-result-list">{tryResultOutcomeItems.map(item => <article className={item.ready ? 'ready' : ''} key={item.label}><span>{item.label}</span><b>{item.ready ? t.tryReady : t.tryNeedsAttention}</b><small>{item.detail}</small></article>)}</div><div className="try-result-next-action" data-try-result-next-action={tryResultNextAction.id}><span>{t.tryResultNextAction}</span><strong>{tryResultNextAction.label}</strong><small>{tryResultNextAction.detail}</small><button type="button" data-try-result-focus-target={tryResultNextAction.target} onClick={() => focusTryResultRecoveryTarget(tryResultNextAction.target)}>{t.tryResultFocusAction}</button></div></section><section className="try-result-preview" data-try-result-preview="markdown-rendered-output" data-try-result-error-preview={tryResultErrorPreview ? 'present' : 'empty'}><div className="try-result-preview-head"><strong>{t.tryResultPreviewTitle}</strong><small>{tryResultMarkdownSource ? t.tryResultPreviewHelp : tryResultErrorPreview ? t.tryResultErrorPreviewHelp : t.tryResultPreviewEmpty}</small></div>{tryResultMarkdownSource ? <MarkdownResultCard dataSurface="try-run-result" source={tryResultMarkdownSource} emptyLabel={t.tryResultPreviewEmpty} title={t.tryResultRenderedTitle} description={t.tryResultRenderedHelp} openLabel={t.markdownOpenRendered} closeLabel={t.markdownCloseRendered} rawLabel={t.tryResultRawJsonTitle} rawSource={tryResultRawPayload} /> : tryResultErrorPreview ? <p className="try-result-error-preview">{tryResultErrorPreview}</p> : <p className="muted">{t.tryResultPreviewEmpty}</p>}{tryResultHiddenOutputCount > 0 && <small className="try-result-preview-more">{t.tryResultPreviewMore(tryResultHiddenOutputCount)}</small>}</section><p className="run-recovery-hint" data-try-guidance="run-result-recovery">{runRecoveryHint}</p>{tryCancelConfirmVisible && <section className="try-cancel-confirm" data-try-cancel-confirm="pending" data-try-cancel-status={run.status}><strong>{t.tryCancelConfirmTitle}</strong><small>{t.tryCancelConfirmDetail}</small><div><button type="button" className="danger-link" data-try-cancel-confirm-action="stop" onClick={cancelRun}>{t.tryCancelConfirmStopAction}</button><button type="button" className="wide secondary" data-try-cancel-confirm-action="keep-waiting" onClick={() => setCancelConfirmRunId(null)}>{t.tryCancelConfirmKeepAction}</button></div></section>}{tryCancelProgressVisible && <section className="try-cancel-progress" data-try-cancel-progress={tryCancelProgressState} data-try-cancel-progress-status={run.status}><strong>{tryCancelProgressState === 'completed' ? t.tryCancelProgressCompletedTitle : t.tryCancelProgressRequestedTitle}</strong><small>{tryCancelProgressState === 'completed' ? t.tryCancelProgressCompletedDetail : t.tryCancelProgressRequestedDetail}</small></section>}<button className="danger-link" data-try-cancel-action="request-confirmation" onClick={cancelRun} disabled={['succeeded', 'failed', 'paused', 'cancelled'].includes(run.status)}>{cancelConfirmRunId === run.id ? t.tryCancelConfirmationPending : t.cancelRun}</button>{run.status === 'paused' && <><label>{t.humanInput}</label><textarea ref={runHumanInputRef} value={humanValues} onChange={event => setHumanValues(event.target.value)} /><button onClick={resumeRun}>{t.resume}</button></>}</div>}
          {pendingPermission && <div className="permission-card" ref={runPermissionRef} tabIndex={-1}><h3>{t.permissionWaiting}</h3><p>{t.permissionHelp}</p><p>{t.permissionTool}: <code>{pendingPermission.tool || '-'}</code>{pendingPermission.node_id ? <> · <code>{pendingPermission.node_id}</code></> : null}</p><pre>{JSON.stringify(pendingPermission.input || {}, null, 2)}</pre><div className="run-actions"><button className="wide" onClick={() => resolvePermission(pendingPermission, 'allow')}>{t.approvePermission}</button><button className="wide secondary" onClick={() => resolvePermission(pendingPermission, 'deny')}>{t.denyPermission}</button></div></div>}
          {visibleTraceEventsForRun.length > 0 && <><h3>{t.traceTitle}</h3><section className="trace-readability-panel" data-trace-guidance="summary" ref={runTraceRef} tabIndex={-1}>
            <div className="trace-readability-head"><strong>{t.traceReadabilityTitle}</strong><small>{t.traceReadabilityHelp}</small></div>
            <div className="trace-readability-list">{traceSummaryItems.map(item => <article className={item.ready ? 'ready' : ''} key={item.label}><span>{item.label}</span><b>{item.ready ? t.tryReady : t.tryNeedsAttention}</b><small>{item.detail}</small></article>)}</div>
            <p className="trace-guidance" data-trace-guidance="next-action">{traceGuidance}</p>
          </section><pre className="trace-log">{JSON.stringify(visibleTraceEventsForRun, null, 2)}</pre></>}
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
          <section className="monitor-readability-panel" data-monitor-guidance="summary">
            <div className="monitor-readability-head"><strong>{t.monitorGuidanceTitle}</strong><small>{t.monitorGuidanceHelp}</small></div>
            <div className="monitor-readability-list">{monitorReadabilityItems.map(item => <article className={item.ready ? 'ready' : ''} key={item.label}><span>{item.label}</span><b>{item.ready ? t.tryReady : t.tryNeedsAttention}</b><small>{item.detail}</small></article>)}</div>
            <p className="monitor-guidance" data-monitor-guidance="next-action">{monitorGuidance}</p>
          </section>
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
      <section
        aria-label={t.canvasKeyboardLabel}
        className="canvas-wrap"
        data-canvas-keyboard="wasd-pan"
        onKeyDownCapture={handleCanvasKeyDown}
        onMouseDown={focusCanvasForKeyboard}
        ref={canvasWrapRef}
        tabIndex={0}
      >
        {authRequired && <form className="auth-card studio-auth-card" onSubmit={saveToken}>
          <div><strong>{t.authTitle}</strong><p>{t.authCopy}</p></div>
          <input type="password" value={tokenInput} placeholder={t.authPlaceholder} onChange={event => setTokenInput(event.target.value)} />
          <div className="auth-actions"><button>{t.authSave}</button><button type="button" className="ghost" onClick={() => { clearClientToken(); setTokenInput('') }}>{t.authClear}</button></div>
        </form>}
        <div className="canvas-toolbar" data-canvas-toolbar="layout-navigation">
          <button data-canvas-action="arrange" disabled={!nodes.length || canvasArranging} onClick={arrangeCanvasNodes} type="button">{canvasArranging ? t.canvasArrangeBusy : t.canvasArrangeButton}</button>
          <span className="canvas-keyboard-hint" data-canvas-keyboard-hint="wasd-pan" title={t.canvasKeyboardHintDetail}>{t.canvasKeyboardHint}</span>
        </div>
        <div className="canvas-guidance">
          {safeDraftLanding && <section className="safe-draft-landing" data-safe-draft-landing="active">
            <div>
              <strong>{t.safeDraftLandingTitle}</strong>
              <p>{t.safeDraftLandingCopy}</p>
              <small>{t.safeDraftLandingNoModel}</small>
            </div>
            <div className="safe-draft-actions">
              <button data-safe-draft-action="inspect" onClick={() => setStudioTab('edit')} type="button">{t.safeDraftActionInspect}</button>
              <button data-safe-draft-action="acceptance" onClick={() => setStudioTab('test')} type="button">{t.safeDraftActionAcceptance}</button>
              <button data-safe-draft-action="try" onClick={() => setStudioTab('run')} type="button">{t.safeDraftActionTry}</button>
              <button data-safe-draft-action="build_later" onClick={() => setStudioTab('build')} type="button">{t.safeDraftActionBuildLater}</button>
              <button className="dismiss" data-safe-draft-action="dismiss" onClick={dismissSafeDraftLanding} type="button">{t.safeDraftActionDismiss}</button>
            </div>
          </section>}
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
              onClick={() => setStudioTab(action.target)}
              type="button"
            >
              <span>{action.label}</span>
              <b>{action.ready ? t.readyLabel : t.needsActionLabel}</b>
              <small>{action.detail}</small>
              <em>{t.nextActionOpen}</em>
            </button>)}</div>
          </section>
        </div>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} deleteKeyCode={['Backspace', 'Delete']} onInit={instance => { flowRef.current = instance; scheduleFitView(nodes) }} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onNodesDelete={deleted => { void persistDeletedNodes(deleted as StudioNode[]) }} onEdgesDelete={deleted => { void persistDeletedEdges(deleted) }} onNodeClick={(_, node) => chooseNode(node)} onNodeContextMenu={(event, node) => { event.preventDefault(); addWorkflowEditReference(node.id); chooseNode(node); setNotice(t.workflowEditReferenceAdded(node.data.title || node.id)) }} onSelectionChange={({ nodes: selectedNodes }) => setWorkflowEditReferencesFromSelection(selectedNodes as StudioNode[])} onEdgeClick={(_, edge) => chooseEdge(edge)} onPaneClick={() => setSelectedNode(null)} onNodeDragStop={(_, node) => mutation('update_node', { node_id: node.id, changes: { position: node.position } })} selectionOnDrag selectionMode={SelectionMode.Partial} fitView fitViewOptions={{ padding: 0.22 }} colorMode="dark">
          <Background color="#283142" gap={24} size={1}/><MiniMap pannable zoomable nodeColor={node => accents[(node.data as { blockType?: string } | undefined)?.blockType || ''] || '#64748b'}/><Controls/>
        </ReactFlow>
        {notice && <button className="toast" onClick={() => setNotice('')}>{notice}</button>}
      </section>
      <aside className="block-panel"><div className="block-heading"><span>{t.bricks}</span><small>{t.available(blocks.length)}</small></div>{Object.entries(grouped).map(([category, items]) => <div className="block-group" key={category}><h4>{items?.[0] ? blockCategory(items[0]) : category}</h4>{items?.map(block => <button onClick={() => addBlock(block)} key={block.type}><i style={{ background: accents[block.type] || '#64748b' }}/><span><b>{blockTitle(block)}</b><small>{blockDescription(block)}</small></span><em>+</em></button>)}</div>)}</aside>
    </div>
  </main>
}
