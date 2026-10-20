import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import LoginPage from '@/app/login/page'
import RegisterPage from '@/app/register/page'
import { redirect } from 'next/navigation'

vi.mock('next/headers', () => ({ headers: async () => new Headers({ host: '127.0.0.1:3000' }) }))
vi.mock('next/navigation', () => ({ redirect: vi.fn(() => { throw new Error('redirect') }) }))
vi.mock('@/app/components/AccountForm', () => ({ default: () => null }))
beforeEach(() => {
  vi.clearAllMocks()
  vi.stubEnv('AGENT_PLATFORM_URL', 'http://127.0.0.1:8001')
  vi.stubEnv('LILIES_LOCAL_DEBUG_TOKEN', 'd'.repeat(64))
})
afterEach(() => vi.unstubAllEnvs())

it.each([LoginPage, RegisterPage])('opens projects directly from an auth page in debug mode', async page => {
  vi.stubEnv('LILIES_LOCAL_DEBUG', 'true')
  await expect(page()).rejects.toThrow('redirect')
  expect(redirect).toHaveBeenCalledWith('/projects')
})

it.each([LoginPage, RegisterPage])('keeps the account form for normal startup', async page => {
  vi.stubEnv('LILIES_LOCAL_DEBUG', '')
  expect(await page()).toBeTruthy()
  expect(redirect).not.toHaveBeenCalled()
})
