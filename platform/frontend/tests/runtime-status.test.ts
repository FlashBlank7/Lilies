import { expect, it } from 'vitest'
import { classifyRuntimeStatus } from '@/lib/runtime-status'
const health = { status:'ok', runtime:{ product_phase:'v0.5.x', version:'v0.5.0-dev', current_code_ready:true, route_availability:{ projects:true } } }
it('recognizes the current development runtime without a false connection warning', () => {
  expect(classifyRuntimeStatus(health)).toBe('connected')
  expect(classifyRuntimeStatus({ ...health, runtime:{ ...health.runtime, version:'v0.5.0' } })).toBe('connected')
})
it('retains route, version, authentication and unavailable checks', () => {
  expect(classifyRuntimeStatus({ ...health, runtime:{ ...health.runtime, route_availability:{ projects:false } } })).toBe('stale')
  expect(classifyRuntimeStatus({ ...health, runtime:{ ...health.runtime, version:'v0.4.9' } })).toBe('stale')
  expect(classifyRuntimeStatus(health, {authRequired:true})).toBe('auth_required')
  expect(classifyRuntimeStatus(null, {unavailable:true})).toBe('unavailable')
})
