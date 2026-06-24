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
  config_schema: Record<string, unknown>
  input_ports: Array<{ name: string; value_type: string }>
  output_ports: Array<{ name: string; value_type: string }>
}

const root = '/api/platform'

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${root}${path}`, {
    ...init,
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  if (!response.ok) throw new Error((await response.text()) || `${response.status} ${response.statusText}`)
  return response.json() as Promise<T>
}

export function idempotency() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
}
