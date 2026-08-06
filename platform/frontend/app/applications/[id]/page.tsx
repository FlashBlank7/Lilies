'use client'

import '@xyflow/react/dist/style.css'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { Play } from 'lucide-react'
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
import { use, useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type FormEvent, type KeyboardEvent, type MouseEvent } from 'react'
import {
  api,
  clearClientToken,
  getClientToken,
  idempotency,
  isAuthError,
  saveClientToken,
  type AcceptanceRepairPreview,
  type Block,
  type BlockEditorField,
  type CapabilityModule,
  type CapabilityModuleInsertResult,
  type Draft,
  type NaturalLanguageWorkflowEditResult,
  type PublicationDecision,
  type WorkflowNode,
  withFrontendToken,
} from '@/lib/platform'
import { defaultLocale, isLocale, messages, nextLocale, type Locale } from '@/lib/i18n'
import { MarkdownDocument, MarkdownResultCard } from '@/lib/markdown'
import { classifyRuntimeStatus, runtimeCommit, runtimeVersion, type RuntimeHealth } from '@/lib/runtime-status'
import {
  boundedCanvasMenuPosition,
  buildNaturalLanguageEditRequest,
  draftIdentityChanged,
  naturalLanguageEditContextMatches,
  normalizeWorkflowEditSelection,
  selectionForEdgeContextMenu,
  selectionForNodeContextMenu,
  selectionForRightDrag,
  type WorkflowEditSelection,
} from '@/lib/workflow-edit-selection'
import surfaceStyles from '@/app/surface-boundaries.module.css'
import { ScheduleOperationsPanel } from '@/app/schedule-operations-panel'
import { ConnectorOperationsPanel } from '@/app/connector-operations-panel'
import {
  BlockCatalogPanel,
  BlockInstanceDetails,
  BlockPurpose,
  UndefinedBusinessWorkflowNotice,
} from './block-catalog-panel'
import blockCatalogStyles from './block-catalog-panel.module.css'

type CanvasPoint = { x: number; y: number }
type StudioPort = {
  name: string
  value_type: string
}
type StudioNode = Node<{
  title: string
  blockType: string
  description: string
  status?: string
  compositionLabel?: string
  operationLabel?: string
  inputPorts: StudioPort[]
  outputPorts: StudioPort[]
}>
type CanvasSelectionBox = { left: number; top: number; width: number; height: number }
type WorkflowEditContextMenu = WorkflowEditSelection & { x: number; y: number }
type StudioChromePreferences = {
  catalogExpanded: boolean
  guidanceExpanded: boolean
  headerExpanded: boolean
  leftPanelExpanded: boolean
  toolbarExpanded: boolean
  undefinedBusinessExpanded: boolean
}
type Copy = (typeof messages)[Locale]
const CORE_STUDIO_TABS = ['build', 'edit', 'test', 'automation'] as const
const VISIBLE_STUDIO_TABS = [...CORE_STUDIO_TABS, 'integrations'] as const
const STUDIO_TABS = [...VISIBLE_STUDIO_TABS, 'run', 'monitor'] as const
type StudioTab = typeof STUDIO_TABS[number]
type ConfigEditorMode = 'form' | 'json'
const STUDIO_CHROME_STORAGE_KEY = 'lilies.studio.chrome.v1'
const BLOCK_DRAG_MIME = 'application/x-lilies-block-type'
const DEFAULT_STUDIO_CHROME: StudioChromePreferences = {
  catalogExpanded: true,
  guidanceExpanded: true,
  headerExpanded: true,
  leftPanelExpanded: true,
  toolbarExpanded: true,
  undefinedBusinessExpanded: true,
}
type Version = { version: number; content_hash: string; created_at: string; validation_report: Record<string, unknown>; publication_decision?: PublicationDecision }
type Build = {
  id: string
  status: string
  error?: string
  max_elapsed_seconds?: number | null
  deadline?: { enabled: boolean; max_elapsed_seconds?: number | null }
  team_state: { tasks: Array<Record<string, unknown>>; teammates: Record<string, Record<string, unknown>>; repair_cycles: number }
}
type TestAssertion = { path?: string[]; operator?: string; expected?: unknown }
type AcceptanceResult = {
  test_id?: string
  name?: string
  mandatory?: boolean
  passed?: boolean
  run_id?: string
  run_status?: string
  run_error?: string
  failure_code?: string
  failed_node?: Record<string, unknown> | null
  outputs?: Record<string, unknown>
  readable_report?: Record<string, unknown>
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

const accents: Record<string, string> = {
  start: '#8b5cf6', llm: '#3b82f6', claude_agent: '#f97316', tool: '#10b981',
  if_else: '#eab308', question_classifier: '#eab308', end: '#ec4899', answer: '#ec4899',
  human_input: '#ef4444', iteration: '#14b8a6', loop: '#14b8a6', http_request: '#06b6d4',
  schedule_trigger: '#a855f7', web_collection: '#0891b2', collection_digest: '#16a34a',
  connector_action: '#d97745',
}

function BrickNode({ data, selected }: NodeProps<StudioNode>) {
  const blockType = safeText(data?.blockType, 'unknown')
  const title = safeText(data?.title, blockType)
  const description = safeText(data?.description, '已配置积木')
  const accent = accents[blockType] || '#64748b'
  const inputPorts = Array.isArray(data?.inputPorts) ? data.inputPorts : []
  const outputPorts = Array.isArray(data?.outputPorts) ? data.outputPorts : []
  const portRows = Math.max(inputPorts.length, outputPorts.length)
  const nodeHeight = Math.max(92, 72 + portRows * 22)
  const portTop = (index: number) => 72 + index * 22
  return <div
    className={`brick-node ${selected ? 'selected' : ''}`}
    data-node-input-port-count={inputPorts.length}
    data-node-output-port-count={outputPorts.length}
    style={{ '--accent': accent, minHeight: nodeHeight } as React.CSSProperties}
  >
    {inputPorts.map((port, index) => <div className="brick-port brick-port-input" data-node-input-port={port.name} key={`input-${port.name}`} style={{ top: portTop(index) }}>
      <Handle id={port.name} title={`${port.name}: ${port.value_type}`} type="target" position={Position.Left} />
      <span>{port.name}</span><small>{port.value_type}</small>
    </div>)}
    <div className="brick-type">{blockType.replaceAll('_', ' ')}</div>
    <strong>{title}</strong>
    <small>{description}</small>
    {(data.compositionLabel || data.operationLabel) && <div className={blockCatalogStyles.nodeFacts}>
      {data.compositionLabel && <span>{data.compositionLabel}</span>}
      {data.operationLabel && <code>{data.operationLabel}</code>}
    </div>}
    {data.status && <span className={`node-status ${data.status}`}>{data.status}</span>}
    {outputPorts.map((port, index) => <div className="brick-port brick-port-output" data-node-output-port={port.name} key={`output-${port.name}`} style={{ top: portTop(index) }}>
      <span>{port.name}</span><small>{port.value_type}</small>
      <Handle id={port.name} title={`${port.name}: ${port.value_type}`} type="source" position={Position.Right} />
    </div>)}
  </div>
}

const nodeTypes = { brick: BrickNode }
const CANVAS_LAYOUT_ORIGIN = { x: 90, y: 110 }
const CANVAS_LAYOUT_COLUMN_WIDTH = 300
const CANVAS_LAYOUT_ROW_HEIGHT = 150
const CANVAS_PAN_STEP = 80

function safeCanvasPosition(value: unknown, fallback: CanvasPoint = CANVAS_LAYOUT_ORIGIN): CanvasPoint {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fallback
  const record = value as Record<string, unknown>
  const x = typeof record.x === 'number' && Number.isFinite(record.x) ? record.x : fallback.x
  const y = typeof record.y === 'number' && Number.isFinite(record.y) ? record.y : fallback.y
  return { x, y }
}

function safeStudioNodeData(
  node: Partial<WorkflowNode>,
  fallbackDescription: string,
  locale: Locale,
  block?: Block,
) {
  const blockType = safeWorkflowNodeType(node)
  const title = safeText(node.title, blockType)
  const config = asRecord(node.config)
  const nestedWorkflow = asRecord(config.workflow)
  const nestedNodeCount = Array.isArray(nestedWorkflow.nodes) ? nestedWorkflow.nodes.length : 0
  const operationLabel = typeof config.operation_id === 'string' && config.operation_id
    ? config.operation_id
    : undefined
  return {
    title,
    blockType,
    description: safeText(node.description, fallbackDescription),
    compositionLabel: nestedNodeCount
      ? (locale === 'zh' ? `内层 ${nestedNodeCount} 个节点` : `${nestedNodeCount} nested nodes`)
      : undefined,
    operationLabel,
    inputPorts: block?.input_ports.map(port => ({ name: port.name, value_type: port.value_type })) || [],
    outputPorts: block?.output_ports.map(port => ({ name: port.name, value_type: port.value_type })) || [],
  }
}

function parseStudioChromePreferences(value: string | null): StudioChromePreferences {
  if (!value) return DEFAULT_STUDIO_CHROME
  try {
    const parsed = JSON.parse(value) as Partial<StudioChromePreferences>
    return Object.fromEntries(
      Object.entries(DEFAULT_STUDIO_CHROME).map(([key, fallback]) => [
        key,
        typeof parsed[key as keyof StudioChromePreferences] === 'boolean'
          ? parsed[key as keyof StudioChromePreferences]
          : fallback,
      ]),
    ) as StudioChromePreferences
  } catch {
    return DEFAULT_STUDIO_CHROME
  }
}

function compatiblePortTypes(source: string, target: string) {
  return source === 'any' || target === 'any' || source === target
}

function portByName(ports: StudioPort[], requested: string | null | undefined, conventionalName: string) {
  if (requested) return ports.find(port => port.name === requested) || null
  if (ports.length === 1) return ports[0]
  return ports.find(port => port.name === conventionalName) || null
}

function connectionWouldCreateCycle(
  sourceId: string,
  targetId: string,
  edges: Draft['snapshot']['workflow']['edges'],
) {
  if (sourceId === targetId) return true
  const outgoing = new Map<string, string[]>()
  edges.forEach(edge => {
    const targets = outgoing.get(edge.source) || []
    targets.push(edge.target)
    outgoing.set(edge.source, targets)
  })
  const pending = [targetId]
  const visited = new Set<string>()
  while (pending.length) {
    const current = pending.pop()
    if (!current || visited.has(current)) continue
    if (current === sourceId) return true
    visited.add(current)
    pending.push(...(outgoing.get(current) || []))
  }
  return false
}

function resolveConnectionContract(
  connection: Connection,
  workflow: Draft['snapshot']['workflow'] | undefined,
  blocks: Block[],
) {
  if (!connection.source || !connection.target || !workflow) {
    return { error: 'missing_connection_endpoint' } as const
  }
  const sourceNode = workflow.nodes.find(node => node.id === connection.source)
  const targetNode = workflow.nodes.find(node => node.id === connection.target)
  if (!sourceNode || !targetNode) return { error: 'missing_connection_node' } as const
  if (connectionWouldCreateCycle(sourceNode.id, targetNode.id, workflow.edges)) {
    return { error: 'workflow_cycle' } as const
  }
  const sourceBlock = blocks.find(block => block.type === sourceNode.type)
  const targetBlock = blocks.find(block => block.type === targetNode.type)
  if (!sourceBlock || !targetBlock) return { error: 'missing_block_contract' } as const
  const sourcePort = portByName(sourceBlock.output_ports, connection.sourceHandle, 'output')
  const targetPort = portByName(targetBlock.input_ports, connection.targetHandle, 'input')
  if (!sourcePort) return { error: sourceBlock.output_ports.length ? 'choose_source_port' : 'source_has_no_output' } as const
  if (!targetPort) return { error: targetBlock.input_ports.length ? 'choose_target_port' : 'target_has_no_input' } as const
  if (!compatiblePortTypes(sourcePort.value_type, targetPort.value_type)) {
    return { error: 'incompatible_port_types', sourcePort, targetPort } as const
  }
  const duplicate = workflow.edges.some(edge => (
    edge.source === sourceNode.id
    && edge.target === targetNode.id
    && edge.source_port === sourcePort.name
    && edge.target_port === targetPort.name
  ))
  if (duplicate) return { error: 'duplicate_connection' } as const
  return { sourceNode, targetNode, sourcePort, targetPort, error: null } as const
}

function connectionErrorMessage(
  result: ReturnType<typeof resolveConnectionContract>,
  locale: Locale,
) {
  const zh = locale === 'zh'
  switch (result.error) {
    case 'workflow_cycle':
      return zh ? '不能创建循环连线；请使用“循环”积木表达循环。' : 'This edge would create a cycle. Use a Loop brick.'
    case 'source_has_no_output':
      return zh ? '起点积木没有输出端口，不能从这里连出。' : 'The source brick has no output port.'
    case 'target_has_no_input':
      return zh ? '终点积木没有输入端口，不能连入这里。' : 'The target brick has no input port.'
    case 'choose_source_port':
      return zh ? '该积木有多个输出，请从标有名称的输出端口拖线。' : 'Choose one of the named source ports.'
    case 'choose_target_port':
      return zh ? '该积木有多个输入，请连到一个明确的输入端口。' : 'Choose one of the named target ports.'
    case 'incompatible_port_types':
      return zh
        ? `端口类型不兼容：${result.sourcePort?.name}:${result.sourcePort?.value_type} → ${result.targetPort?.name}:${result.targetPort?.value_type}`
        : `Incompatible ports: ${result.sourcePort?.name}:${result.sourcePort?.value_type} → ${result.targetPort?.name}:${result.targetPort?.value_type}`
    case 'duplicate_connection':
      return zh ? '这两个端口已经连接。' : 'These ports are already connected.'
    case 'missing_block_contract':
      return zh ? '缺少积木端口合同，请刷新积木库后重试。' : 'Block port metadata is unavailable. Refresh and retry.'
    default:
      return zh ? '无法识别这次连线的起点或终点。' : 'The connection endpoints could not be resolved.'
  }
}

function readableWorkflowPurpose(snapshot: Draft['snapshot'] | undefined, fallback: string) {
  if (!snapshot) return fallback
  const goalMatch = snapshot.requirement.match(/(?:^|\n)\s{0,3}#{0,4}\s*(?:业务目标|Business goal)\s*[:：]?\s*\n+([\s\S]*?)(?=\n\s{0,3}#{1,6}\s+|$)/i)
  const source = snapshot.capability_build_contract?.business_goal?.trim()
    || goalMatch?.[1]?.trim()
    || snapshot.description.trim()
    || snapshot.requirement.trim()
  return source
    .replace(/^\s{0,3}#{1,6}\s*/gm, '')
    .replace(/^\s*[-*+]\s*/gm, '')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 600) || fallback
}

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
    const position = safeCanvasPosition(node.position, { x: 0, y: 0 })
    if (position.x !== 0 || position.y !== 0) return [node.id, position]
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

type ConfigEditorValue = string | boolean
type ConfigEditorValues = Record<string, ConfigEditorValue>

function schemaFieldControl(path: string, schema: Record<string, unknown>): BlockEditorField['control'] {
  if (Array.isArray(schema.enum)) return 'enum'
  if (schema.type === 'boolean') return 'boolean'
  if (schema.type === 'integer' || schema.type === 'number') return 'number'
  if (schema.type === 'object' || schema.type === 'array' || schema.$ref || schema.anyOf) return 'json'
  return /(prompt|system|template|description|instruction)/i.test(path) ? 'textarea' : 'text'
}

function editorFieldsForBlock(block: Block | undefined): BlockEditorField[] {
  if (!block) return []
  const hints = block.editor?.fields
  if (hints?.length) return hints
  const schema = asRecord(block.config_schema)
  const properties = asRecord(schema.properties)
  const required = new Set(asStringArray(schema.required))
  return Object.entries(properties).map(([path, raw]) => {
    const fieldSchema = asRecord(raw)
    return {
      path,
      label: safeText(fieldSchema.title, path.replaceAll('_', ' ')),
      description: safeText(fieldSchema.description),
      control: schemaFieldControl(path, fieldSchema),
      required: required.has(path),
      minimum: typeof fieldSchema.minimum === 'number' ? fieldSchema.minimum : undefined,
      maximum: typeof fieldSchema.maximum === 'number' ? fieldSchema.maximum : undefined,
      step: fieldSchema.type === 'integer' ? 1 : undefined,
      options: Array.isArray(fieldSchema.enum) ? fieldSchema.enum.map(String) : undefined,
    }
  })
}

function configValueAtPath(config: Record<string, unknown>, path: string): unknown {
  return path.split('.').reduce<unknown>((current, part) => asRecord(current)[part], config)
}

function serializeConfigEditorValue(field: BlockEditorField, value: unknown): ConfigEditorValue {
  if (field.control === 'boolean') return value === true
  if (field.control === 'string_list') return Array.isArray(value) ? value.map(String).join('\n') : ''
  if (field.control === 'json') return value === undefined ? '' : JSON.stringify(value, null, 2)
  if (field.control === 'reference_or_text' && value !== undefined && typeof value !== 'string') return JSON.stringify(value, null, 2)
  if (value === undefined || value === null) return ''
  return String(value)
}

function configEditorValues(fields: BlockEditorField[], config: Record<string, unknown>): ConfigEditorValues {
  return Object.fromEntries(fields.map(field => [field.path, serializeConfigEditorValue(field, configValueAtPath(config, field.path))]))
}

function cloneConfig(config: Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(JSON.stringify(config || {})) as Record<string, unknown>
}

function parseConfigObject(source: string): Record<string, unknown> {
  const value: unknown = JSON.parse(source)
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('configuration must be a JSON object')
  }
  return value as Record<string, unknown>
}

function setConfigValueAtPath(config: Record<string, unknown>, path: string, value: unknown) {
  const parts = path.split('.')
  const leaf = parts.pop()
  if (!leaf) return
  let current = config
  for (const part of parts) {
    const next = asRecord(current[part])
    current[part] = next
    current = next
  }
  current[leaf] = value
}

function deleteConfigValueAtPath(config: Record<string, unknown>, path: string) {
  const parts = path.split('.')
  const leaf = parts.pop()
  if (!leaf) return
  let current = config
  for (const part of parts) {
    const next = current[part]
    if (!next || typeof next !== 'object' || Array.isArray(next)) return
    current = next as Record<string, unknown>
  }
  delete current[leaf]
}

function parseReferenceOrText(value: string): unknown {
  const trimmed = value.trim()
  if (/^(true|false|null|-?\d+(\.\d+)?)$/.test(trimmed) || trimmed.startsWith('{') || trimmed.startsWith('[')) {
    return JSON.parse(trimmed)
  }
  return value
}

function configFromEditorValues(base: Record<string, unknown>, fields: BlockEditorField[], values: ConfigEditorValues) {
  const config = cloneConfig(base)
  for (const field of fields) {
    const raw = values[field.path]
    const text = typeof raw === 'string' ? raw : ''
    if (field.control !== 'boolean' && !text.trim() && !field.required) {
      deleteConfigValueAtPath(config, field.path)
      continue
    }
    if (field.control !== 'boolean' && !text.trim() && field.required) throw new Error(`${field.label}: required`)
    let value: unknown
    if (field.control === 'boolean') value = raw === true
    else if (field.control === 'number') {
      const numeric = Number(text)
      if (!Number.isFinite(numeric)) throw new Error(`${field.label}: expected a number`)
      if (field.step === 1 && !Number.isInteger(numeric)) throw new Error(`${field.label}: expected an integer`)
      if (field.minimum !== undefined && numeric < field.minimum) throw new Error(`${field.label}: minimum ${field.minimum}`)
      if (field.maximum !== undefined && numeric > field.maximum) throw new Error(`${field.label}: maximum ${field.maximum}`)
      value = numeric
    } else if (field.control === 'enum') {
      if (field.options?.length && !field.options.includes(text)) throw new Error(`${field.label}: unsupported option`)
      value = text
    } else if (field.control === 'string_list') {
      value = text.split(/[\n,]/).map(item => item.trim()).filter(Boolean)
    } else if (field.control === 'json') {
      try { value = JSON.parse(text) } catch { throw new Error(`${field.label}: invalid JSON`) }
    } else if (field.control === 'reference_or_text') {
      try { value = parseReferenceOrText(text) } catch { throw new Error(`${field.label}: invalid reference or JSON value`) }
    } else {
      value = text
    }
    setConfigValueAtPath(config, field.path, value)
  }
  return config
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
    web_collection: {
      sources: [],
      allowed_hosts: ['configure.invalid'],
      permission_basis: 'Configure approved sources and permission basis before running.',
      respect_robots: true,
      robots_failure_policy: 'deny',
      timeout_seconds: 20,
      max_content_bytes: 1_000_000,
      max_sources: 20,
      fail_on_source_error: false,
    },
    collection_digest: { collection: [], topic: 'Daily collection', include_unchanged: false, max_items: 20 },
    json_schema_validate: { value: {}, schema: { type: 'object' }, max_errors: 25 },
    regex_extract: { text: '', fields: [{ name: 'value', pattern: '^(.{1,1000})$', group: 1, type: 'string', required: true, flags: [] }] },
    record_deduplicate: { records: [], key_paths: [['id']], missing_key_policy: 'error' },
    record_match: {
      source: {},
      candidates: [],
      conditions: [{ name: 'id', source_path: ['id'], candidate_path: ['id'], comparator: 'exact', weight: 1, required: true }],
      conflict_checks: [],
      min_score: 1,
      ambiguity_threshold: 0,
      result_limit: 20,
    },
    typed_json_artifact: { value: {}, filename: 'records.json', lineage: [] },
    typed_workbook: {
      spec: {
        sheets: [{
          name: 'Results',
          columns: [{ key: 'value', header: 'Value', type: 'string', nullable: false }],
          rows: [],
        }],
      },
      filename: 'results.xlsx',
      formula_policy: 'reject',
      lineage: [],
    },
    connector_action: { connector_id: '', connector_version: 1, operation_id: '', tenant_id: '', actor_id: '', actor_roles: [], profile_id: '', payload: {}, idempotency_key: '', authorization_id: '', execution_mode: 'dry_run' },
    iteration: {
      items: [],
      workflow: {
        nodes: [
          { id: 'nested-start', type: 'start', title: 'Nested input', config: { inputs: [] }, position: { x: 40, y: 80 } },
          { id: 'nested-end', type: 'end', title: 'Nested result', config: { outputs: { item: { $ref: { node_id: '$inputs', path: ['item'], optional: true } } } }, position: { x: 300, y: 80 } },
        ],
        edges: [{ id: 'nested-start-end', source: 'nested-start', target: 'nested-end', source_port: 'output', target_port: 'input' }],
        viewport: { x: 0, y: 0, zoom: 0.8 },
      },
      variables: {},
      item_name: 'item',
      output_node_id: 'nested-end',
      output_path: [],
      parallelism: 4,
    },
    loop: {
      workflow: {
        nodes: [
          { id: 'loop-start', type: 'start', title: 'Loop input', config: { inputs: [] }, position: { x: 40, y: 80 } },
          { id: 'loop-end', type: 'end', title: 'Loop result', config: { outputs: { state: { $ref: { node_id: '$inputs', path: ['loop_state'], optional: true } } } }, position: { x: 300, y: 80 } },
        ],
        edges: [{ id: 'loop-start-end', source: 'loop-start', target: 'loop-end', source_port: 'output', target_port: 'input' }],
        viewport: { x: 0, y: 0, zoom: 0.8 },
      },
      variables: {},
      initial_state: null,
      state_input_name: 'loop_state',
      state_update: { $ref: { node_id: 'loop-end', path: ['state'], optional: true } },
      feedback_input_name: 'tool_feedback',
      feedback_value: null,
      break_condition: { value: false, operator: 'equals', expected: true },
      break_value: false,
      max_iterations: 10,
      output_node_id: 'loop-end',
      checkpoint_each_iteration: false,
    },
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

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(item => String(item)) : []
}

function workflowTests(draft: Draft | null): Record<string, unknown>[] {
  return (draft?.snapshot.tests || []).map(test => asRecord(test))
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

function currentAcceptanceReport(
  draft: Draft | null,
  inMemoryReport: Record<string, unknown> | null,
) {
  if (inMemoryReport) return inMemoryReport
  const persisted = asRecord(draft?.evidence?.last_validation_report)
  if (!Object.keys(persisted).length) return null
  const validation = asRecord(persisted.validation)
  return validation.content_hash === draft?.content_hash ? persisted : null
}

function acceptanceOutputValue(result: AcceptanceResult): unknown {
  const outputs = asRecord(result.outputs)
  if (!Object.keys(outputs).length) return undefined
  for (const key of ['answer', 'result', 'output', 'text', 'content']) {
    if (Object.prototype.hasOwnProperty.call(outputs, key)) return outputs[key]
  }
  if (Object.keys(outputs).length === 1) return Object.values(outputs)[0]
  return outputs
}

function acceptanceDisplayValue(value: unknown, emptyLabel: string) {
  if (value === undefined || value === null) return emptyLabel
  if (typeof value === 'string') return value || emptyLabel
  return JSON.stringify(value, null, 2)
}

function acceptanceFailureReasons(result: AcceptanceResult, t: Copy): string[] {
  const reasons: string[] = []
  const failureCode = String(result.failure_code || '')
  const failedNode = asRecord(result.failed_node)
  const failedNodeLabel = String(failedNode.title || failedNode.id || '')
  if (failureCode === 'structured_output_invalid') {
    reasons.push(t.acceptanceFailureStructuredOutput)
  } else if (failureCode === 'draft_validation_failed') {
    reasons.push(t.acceptanceFailureDraftValidation)
  } else if (failureCode === 'node_execution_failed') {
    reasons.push(t.acceptanceFailureNode(failedNodeLabel || t.acceptanceUnknownBrick))
  } else if (failureCode === 'workflow_run_failed') {
    reasons.push(t.acceptanceFailureWorkflow)
  }

  const readable = asRecord(result.readable_report)
  for (const check of asStringArray(readable.failed_checks)) {
    if (check.startsWith('missing required node types:')) {
      reasons.push(`${t.acceptanceFailureMissingBricks}: ${check.split(':').slice(1).join(':').trim()}`)
    } else if (
      check.startsWith('missing required tool nodes:')
      || check.startsWith('missing required tool evidence:')
    ) {
      reasons.push(`${t.acceptanceFailureMissingTools}: ${check.split(':').slice(1).join(':').trim()}`)
    } else if (check.startsWith('tool calls below minimum:')) {
      reasons.push(`${t.acceptanceFailureToolCalls}: ${check.split(':').slice(1).join(':').trim()}`)
    } else if (check === 'output URLs are not fully backed by tool evidence') {
      reasons.push(t.acceptanceFailureCitations)
    }
  }

  const failedAssertions = (result.assertions || []).filter(assertion => !assertion.passed)
  if (failedAssertions.length) reasons.push(t.acceptanceFailureAssertions(failedAssertions.length))
  if (!reasons.length && !result.passed) reasons.push(t.acceptanceFailureUnknown)
  return [...new Set(reasons)]
}

function acceptanceRunErrorReport(draft: Draft | null, error: unknown): Record<string, unknown> {
  const tests = workflowTests(draft)
  const message = String(error)
  return {
    passed: false,
    validation: {
      valid: false,
      errors: [message],
      warnings: [],
      revision: draft?.revision ?? null,
      content_hash: draft?.content_hash ?? null,
      test_count: tests.length,
    },
    summary: {
      total: tests.length,
      passed: 0,
      failed: tests.length,
      mandatory_failed: tests.filter(test => test.mandatory !== false).length,
      frames: tests.map((test, index) => ({
        test_id: String(test.id || `test-${index}`),
        title: String(test.name || test.id || `test-${index}`),
        category: 'runtime',
        status: 'failed',
      })),
    },
    tests: tests.map((test, index) => ({
      test_id: String(test.id || `test-${index}`),
      name: String(test.name || test.id || `test-${index}`),
      mandatory: test.mandatory !== false,
      passed: false,
      run_id: '',
      run_status: 'request_failed',
      run_error: message,
      failure_code: 'workflow_run_failed',
      failed_node: null,
      outputs: {},
      assertions: (Array.isArray(test.assertions) ? test.assertions : []).map(item => ({
        ...asRecord(item),
        passed: false,
        error: message,
      })),
      tool_evidence: { used_tools: [] },
      readable_report: {
        title: String(test.name || test.id || `test-${index}`),
        category: 'runtime',
        purpose: String(test.requirement || ''),
        status: 'failed',
        mandatory: test.mandatory !== false,
        failed_checks: [message],
        failed_assertions: [],
      },
    })),
  }
}

function detailBuildActionState(requirement: string, build: Build | null, buildIntentConfirmed: boolean, t: Copy) {
  if (build && ['queued', 'building'].includes(build.status)) return { id: 'busy', tone: 'busy', title: t.detailBuildActionBusyTitle, detail: t.detailBuildActionBusyDetail }
  if (requirement.trim().length < 10) return { id: 'add_detail', tone: 'attention', title: t.detailBuildActionAddDetailTitle, detail: t.detailBuildActionAddDetailDetail }
  if (buildIntentConfirmed) return { id: 'confirm_team', tone: 'warning', title: t.detailBuildActionConfirmTitle, detail: t.detailBuildActionConfirmDetail }
  return { id: 'arm_team', tone: 'ready', title: t.detailBuildActionArmTitle, detail: t.detailBuildActionArmDetail }
}

function recommendedDetailBuildAction(actionId: string, t: Copy) {
  if (actionId === 'busy') return { target: 'wait', tone: 'busy', label: t.detailBuildRecommendedBusyLabel, detail: t.detailBuildRecommendedBusyDetail, disabled: true }
  if (actionId === 'arm_team' || actionId === 'confirm_team') return { target: 'guarded_build_button', tone: actionId === 'confirm_team' ? 'warning' : 'ready', label: t.detailBuildRecommendedGuardLabel, detail: t.detailBuildRecommendedGuardDetail, disabled: false }
  return { target: 'requirement_focus', tone: 'attention', label: t.detailBuildRecommendedAddDetailLabel, detail: t.detailBuildRecommendedAddDetailDetail, disabled: false }
}

export default function Studio({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const router = useRouter()
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
  const [configEditorMode, setConfigEditorMode] = useState<ConfigEditorMode>('json')
  const [configFieldValues, setConfigFieldValues] = useState<ConfigEditorValues>({})
  const [configEditorBase, setConfigEditorBase] = useState<Record<string, unknown>>({})
  const [build, setBuild] = useState<Build | null>(null)
  const [events, setEvents] = useState<Array<{ type: string; data: Record<string, unknown> }>>([])
  const [tab, setTab] = useState<StudioTab>('build')
  const [safeDraftLanding, setSafeDraftLanding] = useState(false)
  const [requestedAssignmentId, setRequestedAssignmentId] = useState('')
  const [requirement, setRequirement] = useState('')
  const [buildDeadlineSeconds, setBuildDeadlineSeconds] = useState('')
  const [buildIntentConfirmed, setBuildIntentConfirmed] = useState(false)
  const [publicationDecision, setPublicationDecision] = useState<PublicationDecision | null>(null)
  const [publicationBusy, setPublicationBusy] = useState(false)
  const [testReport, setTestReport] = useState<Record<string, unknown> | null>(null)
  const [capabilityModules, setCapabilityModules] = useState<CapabilityModule[]>([])
  const [capabilityModulesLoading, setCapabilityModulesLoading] = useState(false)
  const [capabilityModulesError, setCapabilityModulesError] = useState('')
  const [insertingModuleRef, setInsertingModuleRef] = useState('')
  const [patchInstruction, setPatchInstruction] = useState('')
  const [workflowEditReferenceIds, setWorkflowEditReferenceIds] = useState<string[]>([])
  const [workflowEditReferenceEdgeIds, setWorkflowEditReferenceEdgeIds] = useState<string[]>([])
  const [workflowEditContextMenu, setWorkflowEditContextMenu] = useState<WorkflowEditContextMenu | null>(null)
  const [canvasSelectionBox, setCanvasSelectionBox] = useState<CanvasSelectionBox | null>(null)
  const [patchPreview, setPatchPreview] = useState<NaturalLanguageWorkflowEditResult | null>(null)
  const [patchPreviewLoading, setPatchPreviewLoading] = useState(false)
  const [patchApplyLoading, setPatchApplyLoading] = useState(false)
  const [acceptanceRepairPreview, setAcceptanceRepairPreview] = useState<AcceptanceRepairPreview | null>(null)
  const [acceptanceRepairInstruction, setAcceptanceRepairInstruction] = useState('')
  const [acceptanceRepairTestId, setAcceptanceRepairTestId] = useState<string | null>(null)
  const [acceptanceRepairLoading, setAcceptanceRepairLoading] = useState(false)
  const [acceptanceRepairApplying, setAcceptanceRepairApplying] = useState(false)
  const [testsRunning, setTestsRunning] = useState(false)
  const [canvasArranging, setCanvasArranging] = useState(false)
  const [notice, setNotice] = useState('')
  const [authRequired, setAuthRequired] = useState(false)
  const [tokenInput, setTokenInput] = useState('')
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth | null>(null)
  const [runtimeUnavailable, setRuntimeUnavailable] = useState(false)
  const [studioChrome, setStudioChrome] = useState<StudioChromePreferences>(DEFAULT_STUDIO_CHROME)
  const [studioChromeLoaded, setStudioChromeLoaded] = useState(false)
  const eventSource = useRef<EventSource | null>(null)
  const draftRef = useRef<Draft | null>(null)
  const selectedId = useRef<string | null>(null)
  const selectedEdgeId = useRef<string | null>(null)
  const flowRef = useRef<ReactFlowInstance<StudioNode, Edge> | null>(null)
  const canvasWrapRef = useRef<HTMLElement>(null)
  const workflowEditInputRef = useRef<HTMLTextAreaElement>(null)
  const patchInstructionRef = useRef('')
  const workflowEditMenuPrimaryRef = useRef<HTMLButtonElement>(null)
  const workflowEditSelectionRef = useRef<WorkflowEditSelection>({ nodeIds: [], edgeIds: [] })
  const workflowEditPreviewGenerationRef = useRef(0)
  const rightDragSelectionRef = useRef<{ clientX: number; clientY: number; moved: boolean } | null>(null)
  const rightDragCleanupRef = useRef<(() => void) | null>(null)
  const suppressPaneContextMenuRef = useRef(false)
  const detailBuildRequirementRef = useRef<HTMLTextAreaElement>(null)
  const detailBuildStartButtonRef = useRef<HTMLButtonElement>(null)
  const acceptanceRepairRef = useRef<HTMLElement>(null)
  const initialLoadStartedRef = useRef(false)
  const latestRevision = useRef(0)
  const lastFitSignature = useRef('')
  const buildPoll = useRef<number | null>(null)
  const buildRefreshTimer = useRef<number | null>(null)
  const mutationQueueRef = useRef<Promise<void>>(Promise.resolve())
  const setStudioTab = useCallback((next: StudioTab, options: { replace?: boolean } = {}) => {
    if (next === 'run') {
      router.push(`/runtime/${id}`)
      return
    }
    if (next === 'monitor') {
      router.push(`/governance?application_id=${id}`)
      return
    }
    setTab(next)
    if (typeof window === 'undefined') return
    const query = new URLSearchParams(window.location.search)
    if (query.get('tab') === next) return
    query.set('tab', next)
    const nextUrl = `${window.location.pathname}?${query.toString()}`
    if (options.replace) window.history.replaceState(null, '', nextUrl)
    else window.history.pushState(null, '', nextUrl)
  }, [id, router])
  const syncStudioTabFromLocation = useCallback(() => {
    if (typeof window === 'undefined') return
    const query = new URLSearchParams(window.location.search)
    const requestedTab = query.get('tab')
    if (requestedTab === 'run') {
      router.replace(`/runtime/${id}`)
      return
    }
    if (requestedTab === 'monitor') {
      router.replace(`/governance?application_id=${id}`)
      return
    }
    if (isStudioTab(requestedTab)) setTab(requestedTab)
    setSafeDraftLanding(query.get('safeDraft') === '1')
    setRequestedAssignmentId(query.get('assignment') || '')
  }, [id, router])

  function dismissSafeDraftLanding() {
    setSafeDraftLanding(false)
    if (typeof window === 'undefined') return
    const query = new URLSearchParams(window.location.search)
    query.delete('safeDraft')
    const nextQuery = query.toString()
    window.history.replaceState(null, '', `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ''}`)
  }

  function toggleStudioChrome(key: keyof StudioChromePreferences) {
    setStudioChrome(current => ({ ...current, [key]: !current[key] }))
  }

  useEffect(() => {
    try {
      setStudioChrome(parseStudioChromePreferences(window.localStorage.getItem(STUDIO_CHROME_STORAGE_KEY)))
    } catch {
      setStudioChrome(DEFAULT_STUDIO_CHROME)
    }
    setStudioChromeLoaded(true)
  }, [])

  useEffect(() => {
    if (!studioChromeLoaded) return
    try {
      window.localStorage.setItem(STUDIO_CHROME_STORAGE_KEY, JSON.stringify(studioChrome))
    } catch {
      // Layout preferences are optional and never block workflow editing.
    }
  }, [studioChrome, studioChromeLoaded])

  function setSelectedNode(value: WorkflowNode | null) {
    selectedId.current = value?.id || null
    selectedEdgeId.current = null
    setSelected(value)
    setSelectedEdge(null)
    setConfigText(JSON.stringify(value?.config || {}, null, 2))
    setConfigEditorBase(cloneConfig(value?.config || {}))
    const fields = editorFieldsForBlock(blocks.find(block => block.type === value?.type))
    setConfigFieldValues(configEditorValues(fields, value?.config || {}))
    setConfigEditorMode(fields.length ? 'form' : 'json')
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

  const syncCanvas = useCallback((next: Draft, availableBlocks: Block[] = blocks) => {
    if (next.revision < latestRevision.current) return
    const previousDraft = draftRef.current
    if (draftIdentityChanged(previousDraft, next)) {
      workflowEditPreviewGenerationRef.current += 1
      setPatchPreview(null)
    }
    latestRevision.current = next.revision
    draftRef.current = next
    setDraft(next)
    setRequirement(next.snapshot.requirement)
    const workflowEdges = validWorkflowEdges(next.snapshot.workflow.nodes, next.snapshot.workflow.edges)
    const workflowEditSelection = normalizeWorkflowEditSelection(
      workflowEditSelectionRef.current,
      next.snapshot.workflow.nodes.map(node => node.id),
      workflowEdges,
    )
    workflowEditSelectionRef.current = workflowEditSelection
    setWorkflowEditReferenceIds(workflowEditSelection.nodeIds)
    setWorkflowEditReferenceEdgeIds(workflowEditSelection.edgeIds)
    const workflowEditNodeIds = new Set(workflowEditSelection.nodeIds)
    const workflowEditEdgeIds = new Set(workflowEditSelection.edgeIds)
    const positions = visiblePositions(next.snapshot.workflow.nodes, workflowEdges)
    const blocksByType = new Map(availableBlocks.map(block => [block.type, block]))
    const renderNodes: StudioNode[] = next.snapshot.workflow.nodes.map(item => ({
      id: item.id,
      type: 'brick',
      position: positions.get(item.id) || safeCanvasPosition(item.position),
      data: safeStudioNodeData(item, t.configuredBrick, locale, blocksByType.get(item.type)),
      selected: workflowEditNodeIds.has(item.id),
    }))
    setNodes(renderNodes)
    setEdges(workflowEdges.map(item => {
      const selected = item.id === selectedEdgeId.current || workflowEditEdgeIds.has(item.id)
      return {
        id: item.id, source: item.source, target: item.target, label: item.branch || undefined,
        sourceHandle: item.source_port,
        targetHandle: item.target_port,
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
        setConfigEditorBase(cloneConfig(updated.config || {}))
        const fields = editorFieldsForBlock(blocksByType.get(updated.type))
        setConfigFieldValues(configEditorValues(fields, updated.config || {}))
      } else {
        selectedId.current = null
        setSelected(null)
        setConfigText('{}')
        setConfigEditorBase({})
        setConfigFieldValues({})
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
  }, [blocks, locale, setEdges, setNodes, t.configuredBrick])

  const refresh = useCallback(async () => {
    try {
      const [next, nextBlocks, nextVersions] = await Promise.all([
        api<Draft>(`/api/v1/applications/${id}/draft`),
        api<Block[]>('/api/v1/blocks'),
        api<Version[]>(`/api/v1/applications/${id}/versions`),
      ])
      setBlocks(nextBlocks)
      syncCanvas(next, nextBlocks)
      setVersions(nextVersions)
      setAuthRequired(false)
      return next
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      throw error
    }
  }, [id, syncCanvas])

  const scheduleBuildRefresh = useCallback((buildId: string, delay = 80) => {
    if (buildRefreshTimer.current) window.clearTimeout(buildRefreshTimer.current)
    buildRefreshTimer.current = window.setTimeout(() => {
      buildRefreshTimer.current = null
      void Promise.all([
        refresh(),
        api<Build>(`/api/v1/builds/${buildId}`),
      ]).then(([, currentBuild]) => {
        setBuild(currentBuild)
      }).catch(error => setNotice(String(error)))
    }, delay)
  }, [refresh])

  const refreshCapabilityModules = useCallback(async () => {
    setCapabilityModulesLoading(true)
    setCapabilityModulesError('')
    try {
      const modules = await api<CapabilityModule[]>('/api/v1/capability-modules?all_versions=true')
      setCapabilityModules(modules)
      setAuthRequired(false)
      return modules
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setCapabilityModulesError(String(error))
      throw error
    } finally {
      setCapabilityModulesLoading(false)
    }
  }, [])

  useEffect(() => {
    if (initialLoadStartedRef.current) return
    initialLoadStartedRef.current = true
    const stored = globalThis.localStorage?.getItem('foundry.locale')
    if (isLocale(stored)) setLocale(stored)
    setTokenInput(getClientToken())
    refreshRuntimeStatus()
    refresh().catch(error => setNotice(String(error)))
    refreshCapabilityModules().catch(error => setNotice(String(error)))
  }, [refresh, refreshCapabilityModules])
  useEffect(() => {
    window.addEventListener('popstate', syncStudioTabFromLocation)
    return () => window.removeEventListener('popstate', syncStudioTabFromLocation)
  }, [syncStudioTabFromLocation])
  useEffect(() => () => {
    rightDragCleanupRef.current?.()
  }, [])
  useEffect(() => {
    if (!acceptanceRepairPreview) return
    const frame = window.requestAnimationFrame(() => {
      acceptanceRepairRef.current?.focus({ preventScroll: true })
      acceptanceRepairRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [acceptanceRepairPreview])
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
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function mutation(op: string, data: Record<string, unknown>) {
    const queued = mutationQueueRef.current.then(async (): Promise<Draft | null> => {
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
        await refresh().catch(() => undefined)
        return null
      }
    })
    mutationQueueRef.current = queued.then(() => undefined, () => undefined)
    return queued
  }

  const onConnect = useCallback(async (connection: Connection) => {
    const contract = resolveConnectionContract(connection, draftRef.current?.snapshot.workflow, blocks)
    if (contract.error) {
      setNotice(connectionErrorMessage(contract, locale))
      return
    }
    const edgeId = idempotency()
    const persistedConnection: Edge = {
      id: edgeId,
      source: contract.sourceNode.id,
      target: contract.targetNode.id,
      sourceHandle: contract.sourcePort.name,
      targetHandle: contract.targetPort.name,
    }
    setEdges(current => addEdge(persistedConnection, current))
    const next = await mutation('add_edge', { edge: {
      id: edgeId, source: contract.sourceNode.id, target: contract.targetNode.id,
      source_port: contract.sourcePort.name, target_port: contract.targetPort.name,
    } })
    const target = next?.snapshot.workflow.nodes.find(item => item.id === contract.targetNode.id)
    if (target) {
      const config = configAfterConnect(target, contract.sourceNode.id, contract.sourcePort.name)
      if (config !== target.config) {
        await mutation('update_node', { node_id: target.id, changes: { config }, merge_config: false })
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blocks, locale])

  async function addBlock(block: Block, requestedPosition?: CanvasPoint) {
    const index = draftRef.current?.snapshot.workflow.nodes.length || 0
    const nodeId = `${block.type}-${Date.now()}`
    const position = requestedPosition || { x: 120 + index * 55, y: 120 + (index % 4) * 90 }
    const next = await mutation('add_node', { node: {
      id: nodeId, type: block.type, block_version: 1, title: blockTitle(block),
      description: blockDescription(block), config: defaultConfig(block.type), position,
      retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
    } })
    const added = next?.snapshot.workflow.nodes.find(item => item.id === nodeId)
    if (!added) return
    setSelectedNode(added)
    setNodes(current => current.map(node => ({ ...node, selected: node.id === nodeId })))
    setStudioTab('edit')
    setNotice(locale === 'zh'
      ? `已添加“${blockTitle(block)}”，请检查用途和配置。`
      : `Added “${blockTitle(block)}”. Review its purpose and configuration.`)
  }

  function allowBlockDrop(event: DragEvent<HTMLElement>) {
    if (!Array.from(event.dataTransfer.types).includes(BLOCK_DRAG_MIME)) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }

  function dropBlockOnCanvas(event: DragEvent<HTMLElement>) {
    const blockType = event.dataTransfer.getData(BLOCK_DRAG_MIME)
    if (!blockType) return
    event.preventDefault()
    const block = blocks.find(item => item.type === blockType)
    if (!block) {
      setNotice(locale === 'zh' ? '找不到这个积木，请刷新积木库。' : 'This brick is no longer available. Refresh the library.')
      return
    }
    const position = flowRef.current?.screenToFlowPosition({ x: event.clientX, y: event.clientY })
    void addBlock(block, position ? safeCanvasPosition(position) : undefined)
  }

  async function insertCapabilityModule(module: CapabilityModule) {
    const current = draftRef.current
    if (!current || module.status !== 'verified' || insertingModuleRef) return
    setInsertingModuleRef(module.module_ref)
    setCapabilityModulesError('')
    const base = module.module_id.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 42)
    const prefix = `module_${base}_${module.version}_${Date.now().toString(36)}`.slice(0, 80)
    try {
      const result = await api<CapabilityModuleInsertResult>(
        `/api/v1/applications/${id}/capability-modules/${encodeURIComponent(module.module_id)}/versions/${module.version}/insert`,
        {
          method: 'POST',
          body: JSON.stringify({
            expected_revision: current.revision,
            expected_content_hash: current.content_hash,
            prefix,
            x: 120,
            y: 120,
            idempotency_key: idempotency(),
          }),
        },
      )
      syncCanvas(result.draft)
      setNotice(t.moduleRegistryInserted(module.meta.title, module.version))
      setStudioTab('edit')
    } catch (error) {
      if (isAuthError(error)) setAuthRequired(true)
      setCapabilityModulesError(String(error))
      await refresh()
    } finally {
      setInsertingModuleRef('')
    }
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
      const currentPosition = safeCanvasPosition(node.position, { x: 0, y: 0 })
      return position && (position.x !== currentPosition.x || position.y !== currentPosition.y)
    })
    setNodes(renderNodes => renderNodes.map(node => ({ ...node, position: positions.get(node.id) || safeCanvasPosition(node.position) })))
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
    if (event.key === 'Escape' && workflowEditContextMenu) {
      event.preventDefault()
      setWorkflowEditContextMenu(null)
      canvasWrapRef.current?.focus({ preventScroll: true })
      return
    }
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

  function updateWorkflowEditSelection(
    selection: WorkflowEditSelection,
    options: { selectCanvas?: boolean; invalidatePreview?: boolean } = {},
  ) {
    const workflow = draftRef.current?.snapshot.workflow
    const availableNodes = workflow?.nodes.map(node => node.id) || flowRef.current?.getNodes().map(node => node.id) || []
    const availableEdges = workflow?.edges || flowRef.current?.getEdges().map(edge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
    })) || []
    const normalized = normalizeWorkflowEditSelection(selection, availableNodes, availableEdges)
    const previous = workflowEditSelectionRef.current
    const changed = previous.nodeIds.join('\u0000') !== normalized.nodeIds.join('\u0000')
      || previous.edgeIds.join('\u0000') !== normalized.edgeIds.join('\u0000')
    workflowEditSelectionRef.current = normalized
    setWorkflowEditReferenceIds(current => {
      const ids = normalized.nodeIds
      if (current.length === ids.length && current.every((id, index) => id === ids[index])) return current
      return ids
    })
    setWorkflowEditReferenceEdgeIds(current => current.join('\u0000') === normalized.edgeIds.join('\u0000') ? current : normalized.edgeIds)
    if (changed && options.invalidatePreview !== false) {
      workflowEditPreviewGenerationRef.current += 1
      setPatchPreview(null)
    }
    if (changed) setWorkflowEditContextMenu(null)
    if (options.selectCanvas) {
      const selectedNodeIds = new Set(normalized.nodeIds)
      const selectedEdgeIds = new Set(normalized.edgeIds)
      setNodes(current => current.map(node => ({ ...node, selected: selectedNodeIds.has(node.id) })))
      setEdges(current => current.map(edge => {
        const selected = selectedEdgeIds.has(edge.id)
        const highlighted = selected || selectedEdgeId.current === edge.id
        return {
          ...edge,
          selected,
          style: {
            ...(edge.style || {}),
            stroke: highlighted ? '#ff8a50' : (edge.label ? '#eab308' : '#465166'),
            strokeWidth: highlighted ? 3 : 1,
          },
        }
      }))
    }
    return normalized
  }

  function addWorkflowEditReference(nodeId: string) {
    const current = workflowEditSelectionRef.current
    updateWorkflowEditSelection({
      nodeIds: current.nodeIds.includes(nodeId) ? current.nodeIds : [...current.nodeIds, nodeId],
      edgeIds: current.edgeIds,
    })
  }

  function removeWorkflowEditReference(nodeId: string) {
    const current = workflowEditSelectionRef.current
    const nextNodeIds = current.nodeIds.filter(item => item !== nodeId)
    const workflowEdges = draftRef.current?.snapshot.workflow.edges || []
    const next = selectionForRightDrag(nextNodeIds, workflowEdges)
    updateWorkflowEditSelection(next, { selectCanvas: true })
  }

  function clearWorkflowEditReferences() {
    updateWorkflowEditSelection({ nodeIds: [], edgeIds: [] }, { selectCanvas: true })
    setWorkflowEditContextMenu(null)
  }

  function setWorkflowEditReferencesFromSelection(selectedNodes: StudioNode[], selectedEdges: Edge[]) {
    const nodeSelection = selectionForRightDrag(
      selectedNodes.map(node => node.id),
      draftRef.current?.snapshot.workflow.edges || [],
    )
    updateWorkflowEditSelection({
      nodeIds: nodeSelection.nodeIds,
      edgeIds: nodeSelection.nodeIds.length ? nodeSelection.edgeIds : selectedEdges.map(edge => edge.id),
    })
  }

  function openWorkflowEditContextMenu(clientX: number, clientY: number, selection: WorkflowEditSelection) {
    const normalized = updateWorkflowEditSelection(selection, { selectCanvas: true })
    const selectedWorkflowNode = normalized.nodeIds.length === 1
      ? draftRef.current?.snapshot.workflow.nodes.find(node => node.id === normalized.nodeIds[0]) || null
      : null
    setSelectedNode(selectedWorkflowNode)
    const bounds = canvasWrapRef.current?.getBoundingClientRect()
    if (!bounds) return
    const position = boundedCanvasMenuPosition(
      { x: clientX - bounds.left, y: clientY - bounds.top },
      { width: bounds.width, height: bounds.height },
      { width: 280, height: 172 },
    )
    setWorkflowEditContextMenu({ ...normalized, ...position })
    window.requestAnimationFrame(() => workflowEditMenuPrimaryRef.current?.focus({ preventScroll: true }))
  }

  function openWorkflowEditPanel() {
    setWorkflowEditContextMenu(null)
    setStudioTab('edit')
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        workflowEditInputRef.current?.focus({ preventScroll: true })
        workflowEditInputRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      })
    })
  }

  function handleNodeContextMenu(event: MouseEvent<Element>, node: StudioNode) {
    event.preventDefault()
    event.stopPropagation()
    const workflowEdges = draftRef.current?.snapshot.workflow.edges || []
    const selection = selectionForNodeContextMenu(node.id, workflowEditSelectionRef.current, workflowEdges)
    setNotice(t.workflowEditReferenceAdded(safeText(node.data?.title, node.id)))
    openWorkflowEditContextMenu(event.clientX, event.clientY, selection)
  }

  function handleEdgeContextMenu(event: MouseEvent<Element>, edge: Edge) {
    event.preventDefault()
    event.stopPropagation()
    const selection = selectionForEdgeContextMenu(
      { id: edge.id, source: edge.source, target: edge.target },
      workflowEditSelectionRef.current,
    )
    openWorkflowEditContextMenu(event.clientX, event.clientY, selection)
  }

  function handleSelectionContextMenu(event: MouseEvent<Element>, selectedNodes: StudioNode[]) {
    event.preventDefault()
    event.stopPropagation()
    const workflowEdges = draftRef.current?.snapshot.workflow.edges || []
    const nodeIds = selectedNodes.map(node => node.id)
    const selection = selectionForRightDrag(nodeIds, workflowEdges)
    openWorkflowEditContextMenu(event.clientX, event.clientY, selection)
  }

  function handlePaneContextMenu(event: MouseEvent<Element> | globalThis.MouseEvent) {
    event.preventDefault()
    if (suppressPaneContextMenuRef.current) {
      suppressPaneContextMenuRef.current = false
      return
    }
    openWorkflowEditContextMenu(
      event.clientX,
      event.clientY,
      workflowEditSelectionRef.current,
    )
  }

  function handleCanvasMouseDownCapture(event: MouseEvent<HTMLElement>) {
    if (event.button !== 2) return
    const target = event.target instanceof Element ? event.target : null
    if (!target?.closest('.react-flow__pane')) return
    event.preventDefault()
    event.stopPropagation()
    setWorkflowEditContextMenu(null)
    const origin = { clientX: event.clientX, clientY: event.clientY, moved: false }
    rightDragSelectionRef.current = origin

    const cleanup = () => {
      window.removeEventListener('mousemove', handleMove)
      window.removeEventListener('mouseup', handleUp)
      rightDragCleanupRef.current = null
    }
    const handleMove = (moveEvent: globalThis.MouseEvent) => {
      const current = rightDragSelectionRef.current
      const bounds = canvasWrapRef.current?.getBoundingClientRect()
      if (!current || !bounds) return
      const moved = current.moved
        || Math.abs(moveEvent.clientX - current.clientX) >= 4
        || Math.abs(moveEvent.clientY - current.clientY) >= 4
      rightDragSelectionRef.current = { ...current, moved }
      if (!moved) return
      setCanvasSelectionBox({
        left: Math.min(current.clientX, moveEvent.clientX) - bounds.left,
        top: Math.min(current.clientY, moveEvent.clientY) - bounds.top,
        width: Math.abs(moveEvent.clientX - current.clientX),
        height: Math.abs(moveEvent.clientY - current.clientY),
      })
    }
    const handleUp = (upEvent: globalThis.MouseEvent) => {
      const current = rightDragSelectionRef.current
      rightDragSelectionRef.current = null
      setCanvasSelectionBox(null)
      cleanup()
      if (!current?.moved) return
      upEvent.preventDefault()
      suppressPaneContextMenuRef.current = true
      window.setTimeout(() => { suppressPaneContextMenuRef.current = false }, 0)
      const instance = flowRef.current
      if (!instance) return
      const start = instance.screenToFlowPosition({ x: current.clientX, y: current.clientY })
      const end = instance.screenToFlowPosition({ x: upEvent.clientX, y: upEvent.clientY })
      const selectedNodes = instance.getIntersectingNodes({
        x: Math.min(start.x, end.x),
        y: Math.min(start.y, end.y),
        width: Math.abs(end.x - start.x),
        height: Math.abs(end.y - start.y),
      }, true)
      const selection = selectionForRightDrag(
        selectedNodes.map(node => node.id),
        draftRef.current?.snapshot.workflow.edges || [],
      )
      if (!selection.nodeIds.length) {
        clearWorkflowEditReferences()
        setNotice(t.workflowEditSelectionEmpty)
        return
      }
      openWorkflowEditContextMenu(upEvent.clientX, upEvent.clientY, selection)
    }
    rightDragCleanupRef.current?.()
    rightDragCleanupRef.current = cleanup
    window.addEventListener('mousemove', handleMove)
    window.addEventListener('mouseup', handleUp)
  }

  function switchConfigEditorMode(nextMode: ConfigEditorMode) {
    if (!selected || nextMode === configEditorMode) return
    const fields = editorFieldsForBlock(blocks.find(block => block.type === selected.type))
    try {
      if (nextMode === 'json') {
        const config = configFromEditorValues(configEditorBase, fields, configFieldValues)
        setConfigText(JSON.stringify(config, null, 2))
        setConfigEditorBase(config)
      } else {
        const config = parseConfigObject(configText)
        setConfigEditorBase(config)
        setConfigFieldValues(configEditorValues(fields, config))
      }
      setConfigEditorMode(nextMode)
    } catch (error) {
      setNotice(nextMode === 'form' ? t.invalidJson(String(error)) : t.configFieldInvalid(String(error)))
    }
  }

  async function saveConfig() {
    if (!selected) return
    try {
      const fields = editorFieldsForBlock(blocks.find(block => block.type === selected.type))
      const config = configEditorMode === 'form' && fields.length
        ? configFromEditorValues(configEditorBase, fields, configFieldValues)
        : parseConfigObject(configText)
      setConfigText(JSON.stringify(config, null, 2))
      setConfigEditorBase(config)
      const next = await mutation('update_node', { node_id: selected.id, changes: { config }, merge_config: false })
      await reconcileIncomingEdges(selected.id, config, next)
    } catch (error) {
      setNotice(configEditorMode === 'form' ? t.configFieldInvalid(String(error)) : t.invalidJson(String(error)))
    }
  }

  async function previewDraftPatch() {
    const instruction = patchInstruction.trim()
    const current = draftRef.current
    if (!instruction) {
      setNotice(t.patchPreviewEmpty)
      return
    }
    if (!current) return
    const selection = {
      nodeIds: [...workflowEditSelectionRef.current.nodeIds],
      edgeIds: [...workflowEditSelectionRef.current.edgeIds],
    }
    const previewContext = {
      instruction,
      selection,
      revision: current.revision,
      contentHash: current.content_hash,
    }
    const requestGeneration = workflowEditPreviewGenerationRef.current + 1
    workflowEditPreviewGenerationRef.current = requestGeneration
    setPatchPreviewLoading(true)
    setPatchPreview(null)
    try {
      const result = await api<NaturalLanguageWorkflowEditResult>(`/api/v1/applications/${id}/draft/natural-language-edit`, {
        method: 'POST',
        body: JSON.stringify(buildNaturalLanguageEditRequest(
          instruction,
          selection,
          current,
          idempotency(),
          true,
        )),
      })
      const latestDraft = draftRef.current
      const latestContext = latestDraft ? {
        instruction: patchInstructionRef.current.trim(),
        selection: workflowEditSelectionRef.current,
        revision: latestDraft.revision,
        contentHash: latestDraft.content_hash,
      } : null
      if (
        requestGeneration !== workflowEditPreviewGenerationRef.current
        || !latestContext
        || !naturalLanguageEditContextMatches(previewContext, latestContext)
      ) return
      setPatchPreview(result)
      setNotice(result.supported ? t.patchPreviewReady : t.patchPreviewUnsupported)
    } catch (error) {
      if (requestGeneration === workflowEditPreviewGenerationRef.current) {
        setNotice(String(error))
      }
    } finally {
      setPatchPreviewLoading(false)
    }
  }

  async function applyDraftPatch() {
    if (!patchPreview?.supported || !patchPreview.operations.length) return
    setPatchApplyLoading(true)
    try {
      const result = await api<NaturalLanguageWorkflowEditResult>(`/api/v1/applications/${id}/draft/natural-language-edit`, {
        method: 'POST',
        body: JSON.stringify(buildNaturalLanguageEditRequest(
          patchInstruction,
          { nodeIds: patchPreview.node_ids, edgeIds: patchPreview.edge_ids },
          {
            revision: patchPreview.expected_revision,
            content_hash: patchPreview.expected_content_hash,
          },
          idempotency(),
          false,
          { taskId: patchPreview.task_id, digest: patchPreview.preview_digest },
        )),
      })
      if (!result.applied) throw new Error(result.message || t.patchPreviewUnsupported)
      syncCanvas(result.draft)
      await refresh()
      workflowEditPreviewGenerationRef.current += 1
      setPatchPreview(null)
      patchInstructionRef.current = ''
      setPatchInstruction('')
      setNotice(t.patchApplied)
    } catch (error) {
      setNotice(String(error))
      await refresh().catch(() => undefined)
    } finally {
      setPatchApplyLoading(false)
    }
  }

  async function previewAcceptanceRepair(
    report: Record<string, unknown> | null = currentAcceptanceReport(draftRef.current, testReport),
    testId: string | null = acceptanceRepairTestId,
  ) {
    setAcceptanceRepairLoading(true)
    setAcceptanceRepairPreview(null)
    if (testId) setAcceptanceRepairTestId(testId)
    try {
      const result = await api<AcceptanceRepairPreview>(`/api/v1/applications/${id}/tests/repair-preview`, {
        method: 'POST',
        body: JSON.stringify({
          report,
          test_id: testId,
          instruction: acceptanceRepairInstruction.trim() || undefined,
          reference_node_ids: workflowEditReferenceIds,
        }),
      })
      setAcceptanceRepairPreview(result)
      setAcceptanceRepairInstruction(result.instruction)
      setNotice(result.supported ? t.acceptanceRepairReady : t.acceptanceRepairUnavailable)
      return result
    } catch (error) {
      setNotice(String(error))
      return null
    } finally {
      setAcceptanceRepairLoading(false)
    }
  }

  async function applyAcceptanceRepair() {
    if (!acceptanceRepairPreview?.supported || !acceptanceRepairPreview.operations.length) return
    setAcceptanceRepairApplying(true)
    try {
      const result = await api<{ revision: number; content_hash: string; evidence_state: string }>(`/api/v1/applications/${id}/tests/repair-apply`, {
        method: 'POST',
        body: JSON.stringify({
          expected_revision: acceptanceRepairPreview.expected_revision,
          expected_content_hash: acceptanceRepairPreview.expected_content_hash,
          operations: acceptanceRepairPreview.operations,
          idempotency_key: idempotency(),
        }),
      })
      if (result.content_hash === acceptanceRepairPreview.expected_content_hash) throw new Error(t.acceptanceRepairNoHashChange)
      await refresh()
      setAcceptanceRepairPreview(null)
      setAcceptanceRepairInstruction('')
      setAcceptanceRepairTestId(null)
      setTestReport(null)
      setNotice(t.acceptanceRepairApplied)
      setStudioTab('test')
      await runTests()
    } catch (error) {
      setNotice(String(error))
      await refresh().catch(() => undefined)
    } finally {
      setAcceptanceRepairApplying(false)
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
        scheduleBuildRefresh(buildId)
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
    setAcceptanceRepairPreview(null)
    setAcceptanceRepairInstruction('')
    setAcceptanceRepairTestId(null)
    setTestReport(null)
    setTestsRunning(true)
    try {
      const result = await api<{ passed: boolean } & Record<string, unknown>>(`/api/v1/applications/${id}/tests/run`, { method: 'POST' })
      setTestReport(result)
      setPublicationDecision(null)
      setNotice(result.passed ? t.testsPassed : t.testsFailed)
      if (!result.passed) await previewAcceptanceRepair(result)
      await refresh()
    } catch (error) {
      setTestReport(acceptanceRunErrorReport(draftRef.current, error))
      setNotice(String(error))
    } finally {
      setTestsRunning(false)
    }
  }

  async function publish(acknowledgeWarnings = false) {
    if (publicationBusy) return
    setPublicationBusy(true)
    try {
      const decision = await api<PublicationDecision>(`/api/v1/applications/${id}/publication-decision`)
      setPublicationDecision(decision)
      if (decision.blocked) {
        setNotice(t.publicationBlockedNotice)
        return
      }
      if (decision.requires_confirmation && !acknowledgeWarnings) {
        setNotice(t.publicationConfirmationNotice)
        return
      }
      const result = await api<{ version: number; publication_decision: PublicationDecision }>(`/api/v1/applications/${id}/versions`, {
        method: 'POST',
        body: JSON.stringify({ acknowledge_warnings: acknowledgeWarnings }),
      })
      setNotice(t.published(result.version))
      setPublicationDecision(null)
      await refresh()
    } catch (error) {
      setNotice(String(error))
    } finally {
      setPublicationBusy(false)
    }
  }

  const tested = draft?.tested_hash && draft.tested_hash === draft.content_hash
  const evidenceState = draft?.evidence?.state || (tested ? 'current' : 'missing')
  const evidenceStateLabel = evidenceState === 'current' ? t.evidenceStateCurrent : evidenceState === 'stale' ? t.evidenceStateStale : t.evidenceStateMissing
  const activeVersion = versions[0]?.version
  const displayedTestReport = useMemo(
    () => currentAcceptanceReport(draft, testReport),
    [draft, testReport],
  )
  const acceptanceCaseViews = useMemo(
    () => acceptanceCases(draft, displayedTestReport),
    [draft, displayedTestReport],
  )
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
      detail: t.nextActionRunReady,
    },
    {
      label: t.readinessMonitor,
      ready: true,
      detail: t.nextActionMonitorHelp,
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
      value: t.nextActionRunReady,
      ready: Boolean(draft),
    },
    {
      label: t.detailSignalMonitor,
      value: t.nextActionMonitorHelp,
      ready: true,
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
      detail: t.nextActionRunReady,
      ready: Boolean(draft),
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
      detail: t.nextActionMonitorHelp,
      ready: true,
      target: 'monitor',
    },
  ]
  const acceptancePassedCount = acceptanceCaseViews.filter(test => test.result?.passed).length
  const acceptanceFailedCount = acceptanceCaseViews.filter(test => test.result && !test.result.passed).length
  const acceptancePrimaryFailure = acceptanceCaseViews
    .filter(test => test.result && !test.result.passed)
    .flatMap(test => acceptanceFailureReasons(test.result as AcceptanceResult, t))[0] || ''
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
      const config = node.config && typeof node.config === 'object' && !Array.isArray(node.config) ? node.config as Record<string, unknown> : {}
      const type = safeWorkflowNodeType(node)
      const detail = type === 'tool' ? ` · ${safeText(config.tool_name, t.unboundTool)}` : ''
      return `${node.id}: ${type}${detail} → ${next}`
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
    return workflow.nodes.map((node, index) => ({
      id: node.id,
      title: `${index + 1}. ${safeText(node.title, node.id)}`,
      detail: safeText(node.description, t.nodeInspectorNoDescription),
    }))
  }, [draft, t.nodeInspectorNoDescription])
  const workflowPurposeSummary = useMemo(
    () => readableWorkflowPurpose(draft?.snapshot, t.fallbackDescription),
    [draft, t.fallbackDescription],
  )
  const selectedBlockDefinition = blocks.find(block => block.type === selected?.type)
  const selectedEditorFields = editorFieldsForBlock(selectedBlockDefinition)
  const selectedEditorNotices = selectedBlockDefinition?.editor?.notices || []
  const selectedConfigKeys = safeConfigKeys(selected?.config)
  const selectedNodeSummary = selected ? [
    { label: t.nodeInspectorRole, value: safeWorkflowNodeType(selected), detail: safeText(selected.description, t.nodeInspectorNoDescription) },
    { label: t.nodeInspectorConfig, value: t.nodeConfigSummary(selectedConfigKeys.length), detail: selectedConfigKeys.length ? selectedConfigKeys.slice(0, 4).join(', ') : t.nodeInspectorNoConfig },
    { label: t.nodeInspectorSafeNext, value: t.nodeInspectorSafeNextValue, detail: t.nodeInspectorSafeNextDetail },
  ] : []
  const detailBuildAction = detailBuildActionState(requirement, build, buildIntentConfirmed, t)
  const detailBuildRecommendedAction = recommendedDetailBuildAction(detailBuildAction.id, t)
  const businessDefinitionMissing = Boolean(draft && !(
    draft.snapshot.requirement.trim()
    || draft.snapshot.capability_build_contract?.business_goal?.trim()
  ))

  return <main className="studio-shell" data-studio-chrome="collapsible">
    <header className="studio-header" data-collapsed={studioChrome.headerExpanded ? 'false' : 'true'} id="studio-header">
      <Link href="/" className="back">←</Link>
      <div className="studio-title"><b className={surfaceStyles.studioLabel}>Engineer Studio</b><strong>{draft?.snapshot.name || t.loading}</strong><span>{draft?.snapshot.mode === 'chat' ? t.modeChat : t.modeWorkflow} · {t.draft} r{draft?.revision ?? 0}</span></div>
      <div className="header-center"><span className={`evidence-state ${evidenceState}`} data-evidence-state={evidenceState}>{evidenceStateLabel}</span>{activeVersion && <span>{t.activeVersion(activeVersion)}</span>}<span className={`runtime-chip ${runtimeStatus}`} data-runtime-status={runtimeStatus} title={runtimeStatusDetail}>{runtimeStatusText}</span></div>
      <div className={`header-actions ${surfaceStyles.studioActions}`}>
        <button className="lang-toggle" onClick={toggleLocale}>{t.switchLabel}</button>
        <Link className={surfaceStyles.surfaceLink} href={`/runtime/${id}`}><Play size={14} /><span>{t.debugDraft}</span></Link>
        <button data-publication-action="open" onClick={() => void publish()} disabled={publicationBusy}>{publicationBusy ? t.publicationChecking : t.publishVersion}</button>
        <button
          aria-controls="studio-header"
          aria-expanded={studioChrome.headerExpanded}
          className="studio-chrome-toggle header-toggle"
          data-studio-chrome-toggle="header"
          onClick={() => toggleStudioChrome('headerExpanded')}
          title={studioChrome.headerExpanded ? (locale === 'zh' ? '收起顶栏' : 'Collapse header') : (locale === 'zh' ? '展开顶栏' : 'Expand header')}
          type="button"
        >{studioChrome.headerExpanded ? '⌃' : '⌄'}<span>{locale === 'zh' ? '顶栏' : 'Header'}</span></button>
      </div>
    </header>
    {businessDefinitionMissing && <UndefinedBusinessWorkflowNotice
      expanded={studioChrome.undefinedBusinessExpanded}
      locale={locale}
      nodeCount={draft?.snapshot.workflow.nodes.length || 0}
      onToggle={() => toggleStudioChrome('undefinedBusinessExpanded')}
    />}
    {publicationDecision && (publicationDecision.requires_confirmation || publicationDecision.blocked) && <section className={`publication-decision-banner ${publicationDecision.blocked ? 'blocked' : 'warning'}`} data-publication-decision={publicationDecision.blocked ? 'blocked' : 'confirmation'}>
      <div><strong>{publicationDecision.blocked ? t.publicationBlockedTitle : t.publicationConfirmationTitle}</strong><span>{publicationDecision.evidence_state === 'stale' ? t.publicationStaleDetail : t.publicationMissingDetail}</span></div>
      <ul>{publicationDecision.warnings.map(warning => <li key={warning.code}>{warning.message}</li>)}</ul>
      <div className="publication-decision-actions">
        <button type="button" data-publication-action="revalidate" onClick={() => { setStudioTab('test'); void runTests() }}>{t.evidenceRevalidate}</button>
        <button type="button" className="ghost" data-publication-action="inspect" onClick={() => setStudioTab('test')}>{t.evidenceInspect}</button>
        {!publicationDecision.blocked && <button type="button" data-publication-action="confirm" onClick={() => void publish(true)}>{t.publicationConfirm}</button>}
        <button type="button" className="ghost" data-publication-action="dismiss" aria-label={t.publicationDismiss} onClick={() => setPublicationDecision(null)}>×</button>
      </div>
    </section>}
    <div
      className="studio-grid"
      data-left-panel={studioChrome.leftPanelExpanded ? 'expanded' : 'collapsed'}
      data-right-panel={studioChrome.catalogExpanded ? 'expanded' : 'collapsed'}
    >
      <aside className="left-panel" data-collapsed={studioChrome.leftPanelExpanded ? 'false' : 'true'} id="studio-left-panel">
        <button
          aria-controls="studio-left-panel"
          aria-expanded={studioChrome.leftPanelExpanded}
          className="studio-panel-toggle left-panel-toggle"
          data-studio-chrome-toggle="left-panel"
          onClick={() => toggleStudioChrome('leftPanelExpanded')}
          title={studioChrome.leftPanelExpanded ? (locale === 'zh' ? '收起工作区' : 'Collapse workspace') : (locale === 'zh' ? '展开工作区' : 'Expand workspace')}
          type="button"
        ><span aria-hidden="true">{studioChrome.leftPanelExpanded ? '‹' : '›'}</span><b>{locale === 'zh' ? '工作区' : 'Workspace'}</b></button>
        <div className={`panel-tabs ${surfaceStyles.threeTabs}`} data-detail-tab-url-state="synced">{VISIBLE_STUDIO_TABS.map(item => <button aria-pressed={tab === item} className={tab === item ? 'active' : ''} data-studio-tab={item} onClick={() => setStudioTab(item)} key={item} type="button">{item === 'build' ? t.buildTab : item === 'edit' ? t.editTab : item === 'test' ? t.testTab : item === 'automation' ? locale === 'zh' ? '自动化' : 'Automation' : locale === 'zh' ? '集成' : 'Integrations'}</button>)}</div>
        {tab === 'build' && <div className="panel-body">
          <div className="panel-kicker">{locale === 'zh' ? '莉莉丝 Builder' : 'Lilies Builder'}</div><h2>{t.continueBuild}</h2>
          <textarea ref={detailBuildRequirementRef} className="requirement-input" value={requirement} onChange={event => { setRequirement(event.target.value); setBuildIntentConfirmed(false) }} />
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
          <button ref={detailBuildStartButtonRef} className={`wide build-action ${buildIntentConfirmed ? 'armed' : ''}`} data-build-action="detail-start-legacy-builder" data-build-route="legacy_builder" data-build-intent={buildIntentConfirmed ? 'confirmed' : 'needs-confirmation'} onClick={startBuild}>{buildIntentConfirmed ? (locale === 'zh' ? `确认启动旧 Builder · ${t.startTeamConfirm}` : `Confirm legacy Builder · ${t.startTeamConfirm}`) : (locale === 'zh' ? `旧 Builder（开发） · ${t.startTeam}` : `Legacy Builder (developer) · ${t.startTeam}`)}</button>
          {build && <div className="build-status"><b>{build.status}</b><span>{Object.keys(build.team_state.teammates).length} teammates · {build.team_state.tasks.length} tasks · {build.team_state.repair_cycles} repairs</span><span>{build.deadline?.enabled && build.deadline.max_elapsed_seconds ? t.buildDeadlineActive(build.deadline.max_elapsed_seconds) : t.buildDeadlineInactive}</span>{build.error && <p>{build.error}</p>}</div>}
          <section className="module-registry" data-module-registry="versioned-evidence">
            <div className="module-registry-head"><div><strong>{t.moduleRegistryTitle}</strong><small>{t.moduleRegistryHelp}</small></div><button type="button" onClick={() => void refreshCapabilityModules()} disabled={capabilityModulesLoading}>{capabilityModulesLoading ? t.moduleRegistryLoading : t.moduleRegistryRefresh}</button></div>
            {capabilityModulesError && <p className="error-banner">{capabilityModulesError}</p>}
            <div className="module-registry-list">{capabilityModules.map(module => {
              const contract = module.contract
              const statusLabel = module.status === 'verified' ? t.moduleRegistryVerified : module.status === 'legacy_unverified' ? t.moduleRegistryLegacy : module.status === 'quarantined' ? t.moduleRegistryQuarantined : module.status === 'deprecated' ? t.moduleRegistryDeprecated : t.moduleRegistryDraft
              return <article className={`module-registry-item ${module.status}`} data-module-ref={module.module_ref} data-module-status={module.status} key={module.module_ref}>
                <div className="module-registry-item-head"><div><strong>{module.meta.title}</strong><code>{module.module_ref}</code></div><span>{statusLabel}</span></div>
                <p>{module.meta.description}</p>
                {contract ? <>
                  <div className="module-contract-facts"><span><b>{contract.required_envelope}</b>{t.moduleRegistryEnvelope}</span><span><b>{contract.risk_level}</b>{t.moduleRegistryRisk}</span><span><b>{module.evidence_record_ids.length}</b>{t.moduleRegistryEvidence}</span></div>
                  <div className="module-capability-list" aria-label={t.moduleRegistryCapabilities}>{contract.capability_ids.map(capability => <code key={capability}>{capability}</code>)}</div>
                  <dl className="module-port-list"><div><dt>{t.moduleRegistryInputs}</dt><dd>{contract.inputs.map(port => `${port.name}:${port.value_type}`).join(', ')}</dd></div><div><dt>{t.moduleRegistryOutputs}</dt><dd>{contract.outputs.map(port => `${port.name}:${port.value_type}`).join(', ')}</dd></div></dl>
                  <div className="module-boundary-list"><strong>{t.moduleRegistryBoundaries}</strong>{contract.known_boundaries.map(boundary => <p key={boundary.id}><b>{boundary.title}</b><span>{boundary.description}</span></p>)}</div>
                </> : <p className="module-contract-missing">{t.moduleRegistryContractMissing}</p>}
                {module.verification_errors.length > 0 && <ul className="module-verification-errors">{module.verification_errors.map(error => <li key={error}>{error}</li>)}</ul>}
                <button type="button" className="module-insert-action" disabled={module.status !== 'verified' || Boolean(insertingModuleRef)} onClick={() => void insertCapabilityModule(module)}>{insertingModuleRef === module.module_ref ? t.moduleRegistryInserting : module.status === 'verified' ? t.moduleRegistryInsert(module.version) : t.moduleRegistryUnavailable}</button>
              </article>
            })}</div>
            {!capabilityModulesLoading && capabilityModules.length === 0 && <p className="muted">{t.moduleRegistryEmpty}</p>}
          </section>
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
            <p data-workflow-readable-purpose="true"><b>{t.workflowReadablePurpose}</b>{workflowPurposeSummary}</p>
            <div className="workflow-readable-steps">{workflowStepSummaryItems.length ? workflowStepSummaryItems.map(item => <article key={item.id}><strong>{item.title}</strong><small>{item.detail}</small></article>) : <p className="muted">{t.nodeInspectorNoConfig}</p>}</div>
          </section>
          <section
            className="workflow-edit-dialog"
            data-application-id={id}
            data-workflow-edit-dialog="selection-aware"
            data-workflow-edit-endpoint="natural-language-edit"
            data-workflow-edit-reference-count={workflowEditReferenceIds.length}
            data-workflow-edit-reference-edge-count={workflowEditReferenceEdgeIds.length}
          >
            <div className="patch-panel-head"><strong>{t.patchPreviewTitle}</strong><small>{t.patchPreviewHelp}</small></div>
            <div className="workflow-edit-references" data-workflow-edit-references={workflowEditReferenceIds.length ? 'present' : 'empty'}>
              <div><strong>{t.workflowEditReferenceTitle}</strong><small>{t.workflowEditReferenceHelp}</small></div>
              {workflowEditReferenceNodes.length ? <div className="workflow-edit-reference-list">{workflowEditReferenceNodes.map(node => <button type="button" key={node.id} data-workflow-edit-reference-node={node.id} onClick={() => removeWorkflowEditReference(node.id)}>{safeText(node.title, node.id)}<span>{safeWorkflowNodeType(node)}</span></button>)}</div> : <p className="muted">{t.workflowEditReferenceEmpty}</p>}
              {workflowEditReferenceEdgeIds.length > 0 && <p className="workflow-edit-edge-count">{t.workflowEditSelectedEdges(workflowEditReferenceEdgeIds.length)}</p>}
              {(workflowEditReferenceIds.length > 0 || workflowEditReferenceEdgeIds.length > 0) && <button type="button" className="ghost" data-workflow-edit-reference-action="clear" onClick={clearWorkflowEditReferences}>{t.workflowEditReferenceClear}</button>}
            </div>
            <textarea
              aria-label={t.patchPreviewTitle}
              className="patch-input"
              data-workflow-edit-input="instruction"
              placeholder={t.patchPreviewPlaceholder}
              ref={workflowEditInputRef}
              value={patchInstruction}
              onChange={event => {
                patchInstructionRef.current = event.target.value
                setPatchInstruction(event.target.value)
                workflowEditPreviewGenerationRef.current += 1
                setPatchPreview(null)
              }}
            />
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
          {selected ? <>
            {selectedBlockDefinition && <BlockPurpose block={selectedBlockDefinition} locale={locale} />}
            <BlockInstanceDetails locale={locale} node={selected} />
            <section className="safe-edit-guide" data-node-inspector="safe-edit-guide"><strong>{t.nodeInspectorSafeEditTitle}</strong><span>{t.nodeInspectorSafeEditHelp}</span></section>
            <div className="config-editor-heading"><strong>{t.configLabel}</strong><div className="config-editor-tabs" role="tablist">
              <button type="button" role="tab" aria-selected={configEditorMode === 'form'} data-config-editor-mode="form" disabled={!selectedEditorFields.length} onClick={() => switchConfigEditorMode('form')}>{t.configFormTab}</button>
              <button type="button" role="tab" aria-selected={configEditorMode === 'json'} data-config-editor-mode="json" onClick={() => switchConfigEditorMode('json')}>{t.configJsonTab}</button>
            </div></div>
            {selectedEditorNotices.length > 0 && <div className="config-editor-notices">{selectedEditorNotices.map((item, index) => <p key={`${item.kind}-${index}`} data-config-editor-notice={item.kind}>{locale === 'zh' ? item.text_zh || item.text : item.text}</p>)}</div>}
            {configEditorMode === 'form' ? <div className="config-form" data-config-editor="schema-form">
              {selectedEditorFields.length ? selectedEditorFields.map(field => {
                const label = locale === 'zh' ? field.label_zh || field.label : field.label
                const description = locale === 'zh' ? field.description_zh || field.description : field.description
                const value = configFieldValues[field.path]
                const update = (next: ConfigEditorValue) => setConfigFieldValues(current => ({ ...current, [field.path]: next }))
                return <label className={`config-form-field ${field.control === 'boolean' ? 'boolean' : ''}`} data-config-field={field.path} key={field.path}>
                  <span className="config-form-label"><b>{label}</b>{field.required && <em>{t.configRequired}</em>}</span>
                  {description && <small>{description}</small>}
                  {field.control === 'boolean' ? <input type="checkbox" checked={value === true} onChange={event => update(event.target.checked)} />
                    : field.control === 'enum' ? <select value={String(value ?? '')} onChange={event => update(event.target.value)}>{!field.required && <option value="" />}{field.options?.map(option => <option key={option} value={option}>{option}</option>)}</select>
                      : ['textarea', 'json', 'reference_or_text', 'string_list'].includes(field.control) ? <textarea className={field.control === 'json' ? 'config-json-field' : ''} spellCheck={field.control !== 'json'} value={String(value ?? '')} onChange={event => update(event.target.value)} />
                        : <input type={field.control === 'number' ? 'number' : 'text'} readOnly={field.control === 'readonly'} min={field.minimum} max={field.maximum} step={field.step} value={String(value ?? '')} onChange={event => update(event.target.value)} />}
                </label>
              }) : <p className="muted">{t.configFormNoFields}</p>}
            </div> : <div className="config-expert" data-config-editor="expert-json"><p className="muted">{t.configExpertHelp}</p><textarea className="json-editor" value={configText} onChange={event => setConfigText(event.target.value)} /></div>}
            <button className="wide" data-config-editor-action="save" onClick={saveConfig}>{t.saveConfig}</button><button className="danger-link" onClick={deleteSelectedNode}>{t.deleteNode}</button>
          </> : <p className="muted">{selectedEdge ? t.edgeSelectedHint : t.nodeHelp}</p>}
        </div>}
        {tab === 'test' && <div className="panel-body">
          <section className={`draft-evidence-panel ${evidenceState}`} data-draft-evidence={evidenceState}>
            <div><strong>{t.evidenceStateTitle}: {evidenceStateLabel}</strong><small>{evidenceState === 'current' ? t.evidenceCurrentDetail : evidenceState === 'stale' ? t.evidenceStaleDetail : t.evidenceMissingDetail}</small></div>
            {draft?.evidence?.change_summary?.length ? <ul>{draft.evidence.change_summary.slice(-3).map((item, index) => <li key={`${String(item.revision || '')}-${index}`}>{String(item.operation || t.evidenceChanged)} · r{String(item.revision || '?')}</li>)}</ul> : null}
            <div className="draft-evidence-actions"><button type="button" disabled={testsRunning} onClick={() => void runTests()}>{testsRunning ? t.testsRunning : t.evidenceRevalidate}</button></div>
            {draft?.evidence?.last_validation_report && <details><summary>{t.evidenceInspect}</summary><pre>{JSON.stringify(draft.evidence.last_validation_report, null, 2)}</pre></details>}
          </section>
          <div className="panel-kicker">{t.deliveryGate}</div><h2>{t.acceptanceCases(acceptanceCaseViews.length)}</h2>
          <p className="muted">{t.acceptanceHelp}</p>
          <section className="acceptance-readiness-panel" data-acceptance-guidance="readiness-summary">
            <div className="acceptance-readiness-head"><strong>{t.acceptanceReadinessTitle}</strong><small>{t.acceptanceReadinessHelp}</small></div>
            <div className="acceptance-readiness-list">{acceptanceReadinessItems.map(item => <article className={item.ready ? 'ready' : ''} key={item.label}><span>{item.label}</span><b>{item.ready ? t.tryReady : t.tryNeedsAttention}</b><small>{item.detail}</small></article>)}</div>
            <p className="publish-guidance" data-acceptance-guidance="publish-next-action">{publishGuidance}</p>
          </section>
          {displayedTestReport && <section className={`acceptance-outcome-summary ${displayedTestReport.passed ? 'passed' : 'failed'}`} data-acceptance-outcome={displayedTestReport.passed ? 'passed' : 'failed'}>
            <strong>{displayedTestReport.passed ? t.acceptanceOutcomePassed(acceptancePassedCount, acceptanceCaseViews.length) : t.acceptanceOutcomeFailed(acceptanceFailedCount, acceptanceCaseViews.length)}</strong>
            <p>{displayedTestReport.passed ? t.acceptanceOutcomePassedDetail : acceptancePrimaryFailure || t.acceptanceFailureUnknown}</p>
          </section>}
          {displayedTestReport && !Boolean(displayedTestReport.passed) && <section className={`acceptance-repair-panel ${acceptanceRepairPreview?.supported ? 'supported' : ''}`} data-acceptance-repair="failed-gate-preview" ref={acceptanceRepairRef} tabIndex={-1}>
            <div className="acceptance-repair-head">
              <div><strong>{t.acceptanceRepairTitle}</strong><small>{t.acceptanceRepairHelp}</small></div>
              <span>{acceptanceRepairPreview ? (acceptanceRepairPreview.supported ? t.patchSupported : t.patchUnsupported) : t.tryNeedsAttention}</span>
            </div>
            <div className="acceptance-repair-actions">
              <button className="wide secondary" data-acceptance-repair-action="preview" onClick={() => previewAcceptanceRepair()} disabled={acceptanceRepairLoading}>{acceptanceRepairLoading ? t.acceptanceRepairPreviewing : t.acceptanceRepairPreview}</button>
              <button className="wide" data-acceptance-repair-action="apply" onClick={applyAcceptanceRepair} disabled={!acceptanceRepairPreview?.supported || acceptanceRepairPreview.operations.length === 0 || acceptanceRepairApplying}>{acceptanceRepairApplying ? t.acceptanceRepairApplying : t.acceptanceRepairApply}</button>
            </div>
            {acceptanceRepairPreview ? <div className="acceptance-repair-body">
              <p>{acceptanceRepairPreview.message}</p>
              <label className="acceptance-repair-instruction"><span>{t.acceptanceRepairInstruction}</span><textarea value={acceptanceRepairInstruction} onChange={event => setAcceptanceRepairInstruction(event.target.value)} /></label>
              <MarkdownResultCard
                source={acceptanceRepairPreview.rationale_markdown}
                emptyLabel={t.acceptanceRepairNoPreview}
                title={t.acceptanceRepairRationaleTitle}
                description={t.acceptanceRepairRationaleHelp}
                openLabel={t.markdownOpenRendered}
                closeLabel={t.markdownCloseRendered}
                rawLabel={t.engineeringDetails}
                rawSource={JSON.stringify(acceptanceRepairPreview.repair_context, null, 2)}
                dataSurface="acceptance-repair-rationale"
              />
              {acceptanceRepairPreview.missing_node_types.length > 0 && <div><b>{t.acceptanceRepairMissingNodes}</b><code>{acceptanceRepairPreview.missing_node_types.join(', ')}</code></div>}
              {acceptanceRepairPreview.unsupported_node_types.length > 0 && <div><b>{t.acceptanceRepairUnsupportedNodes}</b><code>{acceptanceRepairPreview.unsupported_node_types.join(', ')}</code></div>}
              {acceptanceRepairPreview.fixes.length > 0 && <details open><summary>{t.acceptanceRepairFixes}</summary><ul>{acceptanceRepairPreview.fixes.map((fix, index) => <li key={index}><code>{String(fix.kind || 'repair')}</code>{fix.node_type ? ` · ${String(fix.node_type)}` : ''}{fix.node_id ? ` · ${String(fix.node_id)}` : ''}</li>)}</ul></details>}
              {acceptanceRepairPreview.warnings.length > 0 && <ul>{acceptanceRepairPreview.warnings.map(item => <li key={item}>{item}</li>)}</ul>}
              {acceptanceRepairPreview.operations.length > 0 && <details><summary>{t.acceptanceRepairOperations}</summary><pre>{JSON.stringify(acceptanceRepairPreview.operations, null, 2)}</pre></details>}
              <details><summary>{t.acceptanceRepairContext}</summary><pre>{JSON.stringify({ task_id: acceptanceRepairPreview.task_id, preview_source: acceptanceRepairPreview.preview_source, repair_context: acceptanceRepairPreview.repair_context, workflow_edit_preview: acceptanceRepairPreview.workflow_edit_preview }, null, 2)}</pre></details>
            </div> : <p className="muted">{t.acceptanceRepairNoPreview}</p>}
          </section>}
          <button className="wide" data-acceptance-action="run-all" data-acceptance-running={testsRunning ? 'true' : 'false'} onClick={runTests} disabled={testsRunning}>{testsRunning ? t.testing : t.runAllTests}</button>
          <div className="acceptance-list">{acceptanceCaseViews.map(test => {
            const statusClass = testsRunning ? 'running' : test.result ? (test.result.passed ? 'passed' : 'failed') : 'pending'
            const statusText = testsRunning ? t.testing : test.result ? (test.result.passed ? t.passedLabel : t.failedLabel) : t.notRunLabel
            const result = test.result
            const assertions = result?.assertions || []
            const evidence = asRecord(result?.tool_evidence)
            const failedNode = asRecord(result?.failed_node)
            const outputValue = result ? acceptanceOutputValue(result) : undefined
            const outputText = acceptanceDisplayValue(outputValue, t.acceptanceNoFinalOutput)
            const intermediateOutput = String(failedNode.output_preview || '')
            const failureReasons = result ? acceptanceFailureReasons(result, t) : []
            const assertionGatePassed = assertions.every(assertion => assertion.passed)
            const structureGatePassed = evidence.required_node_types_passed !== false && evidence.required_tool_nodes_passed !== false
            const toolGatePassed = evidence.required_tools_passed !== false && evidence.minimum_calls_passed !== false && evidence.citation_passed !== false
            const gateResults = result ? [
              { label: t.acceptanceGateRun, passed: result.run_status ? result.run_status === 'succeeded' : Boolean(result.passed) },
              { label: t.acceptanceGateAssertions, passed: assertionGatePassed },
              { label: t.acceptanceGateStructure, passed: structureGatePassed },
              { label: t.acceptanceGateTools, passed: toolGatePassed },
            ] : []
            return <section className="acceptance-card" key={test.id}>
              <div className="acceptance-card-head"><div><strong>{test.name}</strong><small>{test.requirement || t.noRequirementText}</small></div><span className={statusClass}>{statusText}</span></div>
              <div className="acceptance-grid">
                <div><h4>{t.businessRequirement}</h4><p>{test.mandatory ? t.mandatoryLabel : t.optionalLabel}</p><pre>{JSON.stringify(test.inputs, null, 2)}</pre></div>
                <div><h4>{t.outputAssertions}</h4>{test.assertions.length ? <ul>{test.assertions.map((assertion, index) => <li key={index}><code>{(assertion.path || ['output']).join('.')}</code> {assertion.operator || 'exists'} {assertion.expected !== undefined ? <code>{JSON.stringify(assertion.expected)}</code> : null}</li>)}</ul> : <p>{t.noAssertions}</p>}</div>
                <div><h4>{t.structureGate}</h4>{test.requiredNodeTypes.length || test.requiredToolNodes.length ? <ul>{test.requiredNodeTypes.length > 0 && <li>{t.requiredBrickTypes}: <code>{test.requiredNodeTypes.join(', ')}</code></li>}{test.requiredToolNodes.length > 0 && <li>{t.requiredToolNodes}: <code>{test.requiredToolNodes.join(', ')}</code></li>}</ul> : <p>{t.noStructureGate}</p>}</div>
                <div><h4>{t.toolEvidence}</h4>{test.requiredTools.length || test.minimumToolCalls || test.requireCitedToolUrls ? <ul>{test.requiredTools.length > 0 && <li>{t.requiredRuntimeTools}: <code>{test.requiredTools.join(', ')}</code></li>}{test.minimumToolCalls > 0 && <li>{t.minToolCalls}: <code>{test.minimumToolCalls}</code></li>}<li>{test.requireCitedToolUrls ? t.citedUrlsRequired : t.citedUrlsNotRequired}</li></ul> : <p>{t.noToolGate}</p>}</div>
              </div>
              {result && <div className="acceptance-result">
                <div className="acceptance-result-head"><h4>{t.latestResult}</h4><span>{t.runStatus}: <b>{result.run_status || (result.passed ? 'succeeded' : 'failed')}</b></span></div>
                <div className="acceptance-gate-verdicts" data-acceptance-gate-verdicts="visible">{gateResults.map(gate => <span data-state={gate.passed ? 'passed' : 'failed'} key={gate.label}><b>{gate.label}</b><em>{gate.passed ? t.passedLabel : t.failedLabel}</em></span>)}</div>
                <div className="acceptance-run-meta"><p>{t.runId}: <code>{result.run_id || '-'}</code></p><p>{t.usedTools}: <code>{asStringArray(evidence.used_tools).join(', ') || '-'}</code></p><p>{t.assertionPassCount}: <code>{assertions.filter(item => item.passed).length}/{assertions.length}</code></p></div>
                {!result.passed && <div className="acceptance-failure-reasons" data-acceptance-failure-reasons="visible">
                  <strong>{t.acceptanceWhyFailed}</strong>
                  {Boolean(failedNode.id) && <p>{t.acceptanceFailedAtBrick}: <code>{String(failedNode.title || failedNode.id)} ({String(failedNode.id)})</code></p>}
                  <ul>{failureReasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
                  {result.run_error && <details><summary>{t.acceptanceTechnicalError}</summary><code>{result.run_error}</code></details>}
                </div>}
                <div className="acceptance-actual-output" data-acceptance-actual-output={outputValue === undefined ? 'missing' : 'present'}>
                  <strong>{t.acceptanceActualOutput}</strong>
                  {outputValue === undefined ? <p>{t.acceptanceNoFinalOutput}</p> : typeof outputValue === 'string'
                    ? <MarkdownDocument compact emptyLabel={t.acceptanceNoFinalOutput} source={outputText} />
                    : <pre>{outputText}</pre>}
                  {outputValue !== undefined && <details><summary>{t.acceptanceRawOutput}</summary><pre>{JSON.stringify(result.outputs, null, 2)}</pre></details>}
                </div>
                {intermediateOutput && <div className="acceptance-intermediate-output" data-acceptance-intermediate-output="present">
                  <strong>{t.acceptanceIntermediateOutput}</strong>
                  <small>{t.acceptanceIntermediateOutputHelp}</small>
                  <MarkdownDocument compact emptyLabel={t.acceptanceNoFinalOutput} source={intermediateOutput} />
                </div>}
                <div className="acceptance-assertion-comparison" data-acceptance-assertion-comparison="visible">
                  <strong>{t.acceptanceAssertionComparison}</strong>
                  {assertions.length ? assertions.map((assertion, index) => {
                    const assertionPassed = assertion.passed === true
                    const path = asStringArray(assertion.path).join('.') || '<root>'
                    const expected = assertion.expected === undefined || assertion.expected === null ? String(assertion.operator || 'exists') : acceptanceDisplayValue(assertion.expected, t.acceptanceNotProduced)
                    const actual = Object.prototype.hasOwnProperty.call(assertion, 'actual') ? acceptanceDisplayValue(assertion.actual, t.acceptanceNotProduced) : t.acceptanceNotProduced
                    return <article data-state={assertionPassed ? 'passed' : 'failed'} key={`${path}-${index}`}>
                      <div><code>{path}</code><span>{String(assertion.operator || 'exists')}</span><b>{assertionPassed ? t.passedLabel : t.failedLabel}</b></div>
                      <p>{t.acceptanceExpectedValue}: <code>{expected}</code></p>
                      <p>{t.acceptanceActualValue}: <code>{actual}</code></p>
                      {Boolean(assertion.error) && <small>{String(assertion.error)}</small>}
                    </article>
                  }) : <p>{t.noAssertions}</p>}
                </div>
                {!result.passed && <button type="button" className="acceptance-case-repair" data-acceptance-repair-case={test.id} onClick={() => void previewAcceptanceRepair(displayedTestReport, test.id)}>{t.acceptanceRepairThisCase}</button>}
              </div>}
              <details><summary>{t.engineeringDetails}</summary><pre>{JSON.stringify(test.raw, null, 2)}</pre></details>
            </section>
          })}</div>
          {displayedTestReport && <details className="acceptance-full-report"><summary>{t.latestReport}</summary><pre className="trace-log">{JSON.stringify(displayedTestReport, null, 2)}</pre></details>}
          <h3>{t.versionHistory}</h3>{versions.map(version => <div className="version-row" key={version.version}><span>v{version.version}</span><small>{version.content_hash.slice(0, 9)}</small><button onClick={async () => { await api(`/api/v1/applications/${id}/versions/${version.version}/restore`, { method: 'POST' }); await refresh() }}>{t.loadEdit}</button></div>)}
        </div>}
        {tab === 'automation' && <div className="panel-body" data-studio-workspace="automation">
          <ScheduleOperationsPanel
            applicationId={id}
            audience="engineer"
            hasSchedule={Boolean(draft?.snapshot.workflow.nodes.some(node => node.type === 'schedule_trigger'))}
            locale={locale}
            onAuthRequired={() => setAuthRequired(true)}
          />
        </div>}
        {tab === 'integrations' && <div className="panel-body" data-studio-workspace="integrations">
          <ConnectorOperationsPanel
            applicationId={id}
            locale={locale}
            onAuthRequired={() => setAuthRequired(true)}
          />
        </div>}
      </aside>
      <section
        aria-label={t.canvasKeyboardLabel}
        className="canvas-wrap"
        data-canvas-keyboard="wasd-pan"
        data-canvas-drop-target="block-catalog"
        onDragOver={allowBlockDrop}
        onDrop={dropBlockOnCanvas}
        onKeyDownCapture={handleCanvasKeyDown}
        onMouseDown={focusCanvasForKeyboard}
        onMouseDownCapture={handleCanvasMouseDownCapture}
        ref={canvasWrapRef}
        tabIndex={0}
      >
        {authRequired && <form className="auth-card studio-auth-card" onSubmit={saveToken}>
          <div><strong>{t.authTitle}</strong><p>{t.authCopy}</p></div>
          <input type="password" value={tokenInput} placeholder={t.authPlaceholder} onChange={event => setTokenInput(event.target.value)} />
          <div className="auth-actions"><button>{t.authSave}</button><button type="button" className="ghost" onClick={() => { clearClientToken(); setTokenInput('') }}>{t.authClear}</button></div>
        </form>}
        <div className="canvas-toolbar" data-canvas-toolbar="layout-navigation" data-collapsed={studioChrome.toolbarExpanded ? 'false' : 'true'}>
          <button
            data-canvas-action="natural-language-edit"
            onClick={openWorkflowEditPanel}
            type="button"
          >
            {workflowEditReferenceIds.length || workflowEditReferenceEdgeIds.length ? t.canvasWorkflowEditButton : t.canvasWorkflowEditWholeButton}
          </button>
          <button data-canvas-action="arrange" disabled={!nodes.length || canvasArranging} onClick={arrangeCanvasNodes} type="button">{canvasArranging ? t.canvasArrangeBusy : t.canvasArrangeButton}</button>
          <span className="canvas-keyboard-hint" data-canvas-selection-hint="right-drag">{t.canvasRightDragHint}</span>
          <span className="canvas-keyboard-hint" data-canvas-keyboard-hint="wasd-pan" title={t.canvasKeyboardHintDetail}>{t.canvasKeyboardHint}</span>
          <button
            aria-expanded={studioChrome.guidanceExpanded}
            className="canvas-toolbar-guidance-toggle"
            data-studio-chrome-toggle="guidance"
            onClick={() => toggleStudioChrome('guidanceExpanded')}
            type="button"
          >{studioChrome.guidanceExpanded ? (locale === 'zh' ? '收起提示' : 'Hide guidance') : (locale === 'zh' ? '展开提示' : 'Show guidance')}</button>
          <button
            aria-expanded={studioChrome.toolbarExpanded}
            className="canvas-toolbar-toggle"
            data-studio-chrome-toggle="toolbar"
            onClick={() => toggleStudioChrome('toolbarExpanded')}
            title={studioChrome.toolbarExpanded ? (locale === 'zh' ? '收起画布工具' : 'Collapse canvas tools') : (locale === 'zh' ? '展开画布工具' : 'Expand canvas tools')}
            type="button"
          >{studioChrome.toolbarExpanded ? '×' : (locale === 'zh' ? '工具' : 'Tools')}</button>
        </div>
        <div className="canvas-guidance" data-collapsed={studioChrome.guidanceExpanded ? 'false' : 'true'}>
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
        <ReactFlow
          colorMode="dark"
          deleteKeyCode={['Backspace', 'Delete']}
          edges={edges}
          fitView
          fitViewOptions={{ padding: 0.22 }}
          nodeTypes={nodeTypes}
          nodes={nodes}
          onConnect={onConnect}
          onEdgeClick={(_, edge) => chooseEdge(edge)}
          onEdgeContextMenu={handleEdgeContextMenu}
          onEdgesChange={onEdgesChange}
          onEdgesDelete={deleted => { void persistDeletedEdges(deleted) }}
          onInit={instance => { flowRef.current = instance; scheduleFitView(nodes) }}
          onNodeClick={(_, node) => chooseNode(node)}
          onNodeContextMenu={handleNodeContextMenu}
          onNodeDragStop={(_, node) => mutation('update_node', { node_id: node.id, changes: { position: safeCanvasPosition(node.position) } })}
          onNodesChange={onNodesChange}
          onNodesDelete={deleted => { void persistDeletedNodes(deleted as StudioNode[]) }}
          onPaneClick={() => {
            setWorkflowEditContextMenu(null)
            setSelectedNode(null)
          }}
          onPaneContextMenu={handlePaneContextMenu}
          onSelectionChange={({ nodes: selectedNodes, edges: selectedEdges }) => setWorkflowEditReferencesFromSelection(selectedNodes as StudioNode[], selectedEdges)}
          onSelectionContextMenu={handleSelectionContextMenu}
          panOnDrag={[0, 1]}
          selectionMode={SelectionMode.Partial}
          selectionOnDrag
        >
          <Background color="#283142" gap={24} size={1}/><MiniMap pannable zoomable nodeColor={node => accents[(node.data as { blockType?: string } | undefined)?.blockType || ''] || '#64748b'}/><Controls/>
        </ReactFlow>
        {canvasSelectionBox && <div
          aria-hidden="true"
          className="workflow-edit-selection-box"
          data-workflow-edit-selection-box="right-drag"
          style={{
            height: canvasSelectionBox.height,
            left: canvasSelectionBox.left,
            top: canvasSelectionBox.top,
            width: canvasSelectionBox.width,
          }}
        />}
        {workflowEditContextMenu && <div
          aria-label={t.workflowEditContextMenuLabel}
          className="workflow-edit-context-menu"
          data-workflow-edit-context-menu="open"
          role="menu"
          style={{ left: workflowEditContextMenu.x, top: workflowEditContextMenu.y }}
        >
          <strong>{t.workflowEditContextTitle}</strong>
          <span>{t.workflowEditSelectionSummary(workflowEditContextMenu.nodeIds.length, workflowEditContextMenu.edgeIds.length)}</span>
          <p>{workflowEditContextMenu.nodeIds.length || workflowEditContextMenu.edgeIds.length ? t.workflowEditContextHelp : t.workflowEditContextWholeWorkflow}</p>
          <button ref={workflowEditMenuPrimaryRef} role="menuitem" type="button" onClick={openWorkflowEditPanel}>{t.workflowEditContextOpen}</button>
          <button className="secondary" role="menuitem" type="button" onClick={() => {
            setWorkflowEditContextMenu(null)
            canvasWrapRef.current?.focus({ preventScroll: true })
          }}>{t.workflowEditContextCancel}</button>
        </div>}
        {notice && <button className="toast" onClick={() => setNotice('')}>{notice}</button>}
      </section>
      <BlockCatalogPanel
        blocks={blocks}
        expanded={studioChrome.catalogExpanded}
        locale={locale}
        onAdd={addBlock}
        onToggle={() => toggleStudioChrome('catalogExpanded')}
      />
    </div>
  </main>
}
