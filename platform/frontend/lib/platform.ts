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
