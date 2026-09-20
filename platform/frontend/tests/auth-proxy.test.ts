// @vitest-environment node
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { NextRequest } from 'next/server'
import { GET, POST } from '@/app/api/platform/[...path]/route'

const context = (path: string) => ({ params: Promise.resolve({ path: path.split('/') }) })
const request = (path: string, init: RequestInit = {}) => new NextRequest('http://platform.test/api/platform/' + path, { ...init, signal: init.signal ?? undefined })
beforeEach(() => {
  vi.stubEnv('AGENT_PLATFORM_URL', 'http://backend.test')
  vi.stubEnv('API_TOKEN', 'must-not-be-forwarded')
  vi.stubEnv('AUTH_PROXY_SECRET', '')
  vi.stubGlobal('fetch', vi.fn())
})
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })

it('never supplies the server administrator token to anonymous browser requests', async () => {
  const response = await GET(request('api/v1/projects', { headers: { 'x-agent-platform-token': 'must-not-be-forwarded' } }), context('api/v1/projects'))
  expect(response.status).toBe(401)
  expect(fetch).not.toHaveBeenCalled()
})

it('stores login credentials in an HttpOnly cookie without returning the token body', async () => {
  vi.mocked(fetch).mockResolvedValue(Response.json({ user: { name: '甲' }, token: 'session-secret', expires_at: Date.now()/1000+86400 }))
  const response = await POST(request('api/v1/auth/login', { method: 'POST', headers: { origin: 'http://platform.test', host: 'platform.test', 'content-type': 'application/json' }, body: JSON.stringify({ name: '甲', password: 'password123' }) }), context('api/v1/auth/login'))
  expect(response.status).toBe(200)
  expect(await response.json()).toEqual({ user: { name: '甲' } })
  expect(response.headers.get('set-cookie')).toContain('HttpOnly')
  expect(response.headers.get('set-cookie')).toContain('SameSite=lax')
  const [, init] = vi.mocked(fetch).mock.calls[0]
  expect(new Headers(init?.headers).has('authorization')).toBe(false)
})

it('rejects cross-origin writes before contacting the backend', async () => {
  const response = await POST(request('api/v1/projects', { method: 'POST', headers: { origin: 'https://attacker.test', host: 'platform.test', cookie: 'lilies_session=secret' }, body: '{}' }), context('api/v1/projects'))
  expect(response.status).toBe(403)
  expect(fetch).not.toHaveBeenCalled()
})

it('authenticates downloads with the session cookie and preserves file headers', async () => {
  vi.mocked(fetch).mockResolvedValue(new Response('report', { headers: { 'content-type': 'text/csv', 'content-disposition': 'attachment; filename=report.csv' } }))
  const path='api/v1/runs/run/artifacts/report.csv'
  const response=await GET(request(path, { headers: { cookie: 'lilies_session=user-session' } }), context(path))
  expect(await response.text()).toBe('report')
  expect(response.headers.get('content-disposition')).toContain('report.csv')
  const [, init]=vi.mocked(fetch).mock.calls[0]
  expect(new Headers(init?.headers).get('authorization')).toBe('Bearer user-session')
})

it('does not erase a newer browser session when an old request returns 401', async () => {
  vi.mocked(fetch).mockResolvedValue(Response.json({ detail: '登录已失效' }, { status: 401 }))
  const response = await GET(request('api/v1/me', { headers: { cookie: 'lilies_session=old-session' } }), context('api/v1/me'))
  expect(response.status).toBe(401)
  expect(response.headers.get('set-cookie')).toBeNull()
})

it.each(['', 'wrong-key', 'x'.repeat(32)])('strips untrusted client identities (key %s)', async supplied => {
  vi.stubEnv('AUTH_PROXY_SECRET', 'p'.repeat(32))
  vi.mocked(fetch).mockResolvedValue(Response.json({ detail: 'limited' }, { status: 429, headers: {'Retry-After':'35'} }))
  const response=await POST(request('api/v1/auth/register', {method:'POST',headers:{
    origin:'http://platform.test',host:'platform.test',
    'x-lilies-proxy-key':supplied,'x-lilies-client-ip':'192.0.2.10','x-forwarded-for':'192.0.2.11','x-real-ip':'192.0.2.12',
  },body:'{}'}),context('api/v1/auth/register'))
  const headers=new Headers(vi.mocked(fetch).mock.calls[0][1]?.headers)
  expect(headers.has('x-lilies-proxy-key')).toBe(false)
  expect(headers.has('x-lilies-client-ip')).toBe(false)
  expect(headers.has('x-forwarded-for')).toBe(false)
  expect(response.status).toBe(429)
  expect(response.headers.get('retry-after')).toBe('35')
})

it.each(['192.0.2.10', '2001:db8::a'])('passes the authenticated ingress identity %s without exposing the key',async address=>{
  const secret='p'.repeat(32)
  vi.stubEnv('AUTH_PROXY_SECRET',secret)
  vi.mocked(fetch).mockResolvedValue(Response.json({detail:'limited'},{status:429}))
  const response=await POST(request('api/v1/auth/register',{method:'POST',headers:{
    origin:'http://platform.test',host:'platform.test','x-lilies-proxy-key':secret,'x-lilies-client-ip':address,
  },body:'{}'}),context('api/v1/auth/register'))
  const headers=new Headers(vi.mocked(fetch).mock.calls[0][1]?.headers)
  expect(headers.get('x-lilies-client-ip')).toBe(address)
  expect(headers.get('x-lilies-proxy-key')).toBe(secret)
  expect(response.headers.has('x-lilies-proxy-key')).toBe(false)
  expect(await response.text()).not.toContain(secret)
})

it.each(['192.0.2.1, 192.0.2.2','not-an-ip','fe80::1%eth0'])('discards malformed ingress identity %s',async address=>{
  const secret='p'.repeat(32)
  vi.stubEnv('AUTH_PROXY_SECRET',secret)
  vi.mocked(fetch).mockResolvedValue(Response.json({detail:'limited'},{status:429}))
  await POST(request('api/v1/auth/login',{method:'POST',headers:{
    origin:'http://platform.test',host:'platform.test','x-lilies-proxy-key':secret,'x-lilies-client-ip':address,
  },body:'{}'}),context('api/v1/auth/login'))
  expect(new Headers(vi.mocked(fetch).mock.calls[0][1]?.headers).has('x-lilies-client-ip')).toBe(false)
})
