export type Position = { x: number; y: number }

export type WorkflowNode = {
  id: string
  type: string
  block_version: number
  title: string
  description: string
  config: Record<string, unknown>
  position: Position
  retry: { enabled: boolean; max_attempts: number; delay_seconds: number }
  error_strategy: 'fail' | 'continue' | 'error_branch'
}

export type WorkflowEdge = {
  id: string
  source: string
  target: string
  source_port: string
  target_port: string
  branch?: string | null
}

export type Snapshot = {
  name: string
  description: string
  mode: 'workflow' | 'chat'
  requirement: string
  workflow: { nodes: WorkflowNode[]; edges: WorkflowEdge[]; viewport: Record<string, number> }
  agents: Record<string, unknown>
  tests: Array<Record<string, unknown>>
}

export type Draft = {
  application_id: string
  revision: number
  content_hash: string
  tested_hash?: string | null
  validation_report: Record<string, unknown>
  snapshot: Snapshot
}

export type Block = {
  type: string
  title: string
  description: string
  category: string
  block_kind?: 'business_workflow' | 'agent_architecture' | 'legacy_compatibility'
  manual_summary?: string
  when_to_use?: string[]
  examples?: Array<Record<string, unknown>>
  anti_patterns?: string[]
  common_errors?: string[]
  claude_architecture_mapping?: string | null
  composability_constraints?: string[]
  editor?: { i18n?: Record<string, { title?: string; description?: string; category?: string }> }
  config_schema: Record<string, unknown>
  input_ports: Array<{ name: string; value_type: string }>
  output_ports: Array<{ name: string; value_type: string }>
}

export type PlatformTaskKind =
  | 'workflow_run'
  | 'builder_build'
  | 'test_suite'
  | 'scheduler_trigger'
  | 'scheduler_manual_trigger'
  | 'benchmark'
  | 'draft_patch_preview'

export type PlatformTaskStatus = 'queued' | 'running' | 'paused' | 'succeeded' | 'failed' | 'cancelled'

export type PlatformUsageRecord = {
  usage_type: string
  amount: number
  metadata: Record<string, unknown>
  created_at: string
}

export type PlatformTaskRecord = {
  id: string
  kind: PlatformTaskKind
  owner_id: string
  resource_id: string
  status: PlatformTaskStatus
  parent_task_id?: string | null
  metadata: Record<string, unknown>
  usage_counts: Record<string, number>
  usage: PlatformUsageRecord[]
  error: string
  worker_id?: string | null
  lease_expires_at?: string | null
  lease_version?: number
  created_at: string
  updated_at: string
  finished_at?: string | null
}

export type PlatformPolicyDecision = {
  id: string
  label: string
  surface: string
  server_name: string
  platform_policy: string
  agent_network_policy: string
  sandbox_network_policy?: string | null
  allowed: boolean
  mode: string
  reason: string
  operator_action: string
}

export type PlatformPolicyControls = {
  network_egress_policy: string
  network_egress_allowlist: string[]
  secret_policy_enabled: boolean
  worker_id: string
  worker_lease_seconds: number
  limits: Record<string, number>
  e08_boundary: {
    current_slice: string
    source: string
    comparison_evidence: string
    soft_passmode: {
      layer: string
      enforcement: string
      statement: string
    }
    hard_boundary: {
      layer: string
      enforcement: string
      statement: string
    }
    not_full_sidecar_completion: boolean
    remaining_full_boundary: string[]
    controls: Array<{
      id: string
      label: string
      layer: string
      status: string
      value: unknown
    }>
  }
  stdio_mcp: {
    sandboxed_no_network_supported: boolean
    allowlist_supported: boolean
    decisions: PlatformPolicyDecision[]
  }
}

export type DraftPatchOperation = {
  expected_revision?: number
  op: string
  data: Record<string, unknown>
}

export type DraftPatchPreview = {
  task_id: string
  supported: boolean
  intent: 'rename_node' | 'update_node_description' | 'remove_disconnected_node' | 'unsupported'
  message: string
  operations: DraftPatchOperation[]
  warnings: string[]
}

export type BuilderBenchmarkHistoryRecord = {
  id: string
  status: string
  owner_id: string
  resource_id: string
  created_at: string
  updated_at: string
  finished_at?: string | null
  metadata: Record<string, unknown>
  usage_counts: Record<string, number>
  error: string
}

export type AdaptiveMonitoringCase = {
  family: string
  mode: string
  build_status: string
  effective_depth: string
  reuse_depth_source: string
  benchmark_passed: boolean | null
  timeout_like: boolean
  available_overrides: string[]
  source: string
}

export type AdaptiveMonitoringRefreshRecord = {
  refreshed_at: string
  status: string
  critical_alert_count: number
  warning_alert_count: number
  override_options_visible: boolean
  source: string
  source_generated_at?: string | null
}

export type AdaptiveMonitoringStatus = {
  status: 'healthy' | 'attention' | 'missing_evidence'
  version: string
  source: string
  generated_at?: string | null
  critical_alert_count: number
  warning_alert_count: number
  override_options_visible: boolean
  available_overrides: string[]
  cases: AdaptiveMonitoringCase[]
  alerts: Array<Record<string, unknown>>
  conclusion: string
  last_refresh?: AdaptiveMonitoringRefreshRecord | null
  history: AdaptiveMonitoringRefreshRecord[]
  history_count: number
  history_path: string
}

const root = '/api/platform'
const tokenKey = 'foundry.apiToken'

export function getClientToken() {
  if (typeof window === 'undefined') return ''
  return window.localStorage.getItem(tokenKey) || ''
}

export function saveClientToken(token: string) {
  if (typeof window === 'undefined') return
  const value = token.trim()
  if (value) window.localStorage.setItem(tokenKey, value)
  else window.localStorage.removeItem(tokenKey)
}

export function clearClientToken() {
  if (typeof window === 'undefined') return
  window.localStorage.removeItem(tokenKey)
}

export function isAuthError(error: unknown) {
  return String(error).includes('401') || String(error).toLowerCase().includes('invalid api token')
}

export function withFrontendToken(path: string) {
  const token = getClientToken()
  if (!token) return path
  const separator = path.includes('?') ? '&' : '?'
  return `${path}${separator}frontend_token=${encodeURIComponent(token)}`
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getClientToken()
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    ...(token ? { 'X-Agent-Platform-Token': token } : {}),
    ...(init?.headers || {}),
  }
  const response = await fetch(`${root}${path}`, {
    ...init,
    cache: 'no-store',
    headers,
  })
  if (!response.ok) {
    const body = await response.text()
    throw new Error(`${response.status} ${response.statusText}${body ? `: ${body}` : ''}`)
  }
  return response.json() as Promise<T>
}

export function idempotency() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
}
