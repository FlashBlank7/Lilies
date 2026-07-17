export const expectedRuntimeProductPhase = 'v0.4.x'
export const expectedRuntimeVersionPattern = /^v0\.4\.\d+$/

export type RuntimeStatusState = 'checking' | 'connected' | 'auth_required' | 'stale' | 'unavailable'

export type RuntimeHealth = {
  status?: string
  runtime?: {
    version?: string
    product_phase?: string
    current_code_ready?: boolean
    git?: {
      commit?: string
      branch?: string
      tracked_dirty?: boolean
      untracked_present?: boolean
    }
    route_availability?: Record<string, boolean>
  }
}

export function classifyRuntimeStatus(
  health: RuntimeHealth | null,
  options: { authRequired?: boolean; unavailable?: boolean } = {},
): RuntimeStatusState {
  if (options.authRequired) return 'auth_required'
  if (options.unavailable) return 'unavailable'
  if (!health) return 'checking'
  const runtime = health.runtime
  if (!runtime) return 'stale'
  if (runtime.product_phase !== expectedRuntimeProductPhase) return 'stale'
  if (!runtime.version || !expectedRuntimeVersionPattern.test(runtime.version)) return 'stale'
  if (runtime.current_code_ready !== true) return 'stale'
  if (runtime.route_availability && Object.values(runtime.route_availability).some(value => value !== true)) return 'stale'
  return 'connected'
}

export function runtimeCommit(health: RuntimeHealth | null) {
  return health?.runtime?.git?.commit || '-'
}

export function runtimeVersion(health: RuntimeHealth | null) {
  return health?.runtime?.version || 'unknown'
}
