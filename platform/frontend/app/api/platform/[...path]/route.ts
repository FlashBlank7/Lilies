import fs from 'fs'
import path from 'path'
import { NextRequest, NextResponse } from 'next/server'

export const dynamic = 'force-dynamic'

const localEnvCache = new Map<string, string>()
let localEnvSearched = false

function parseEnvValue(text: string, key: string) {
  const pattern = new RegExp(`^\\s*(?:export\\s+)?${key}\\s*=\\s*(.*)\\s*$`)
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(pattern)
    if (!match) continue
    const rawValue = match[1].trim()
    if ((rawValue.startsWith('"') && rawValue.endsWith('"')) || (rawValue.startsWith("'") && rawValue.endsWith("'"))) {
      return rawValue.slice(1, -1)
    }
    return rawValue.replace(/\s+#.*$/, '')
  }
  return ''
}

function loadLocalEnv() {
  if (localEnvSearched) return
  localEnvSearched = true
  let current = process.cwd()
  for (let depth = 0; depth < 6; depth += 1) {
    const candidate = path.join(current, '.env')
    try {
      if (fs.existsSync(candidate)) {
        const text = fs.readFileSync(candidate, 'utf-8')
        const platformUrl = parseEnvValue(text, 'AGENT_PLATFORM_URL')
        if (platformUrl) localEnvCache.set('AGENT_PLATFORM_URL', platformUrl)
        if (platformUrl) return
      }
    } catch {
      // The configured backend URL takes precedence over local development defaults.
    }
    const parent = path.dirname(current)
    if (parent === current) break
    current = parent
  }
}

function localEnvValue(key: string) {
  loadLocalEnv()
  return localEnvCache.get(key) || ''
}

function platformBaseUrl() {
  return (process.env.AGENT_PLATFORM_URL || localEnvValue('AGENT_PLATFORM_URL') || 'http://127.0.0.1:8000').replace(/\/$/, '')
}

const sessionCookie = 'lilies_session'

const LOCAL_LILIES_QUERY_SECRET_KEYS = new Set([
  'access_token',
  'api_key',
  'api_token',
  'authorization',
  'bootstrap_credential',
  'credential',
  'frontend_token',
  'pairing_code',
  'password',
  'prepared_access_token',
  'previous_access_token',
  'secret',
  'token',
])

function isLocalLiliesPath(pathParts: string[]) {
  return pathParts.length >= 3
    && pathParts[0] === 'api'
    && pathParts[1] === 'v1'
    && pathParts[2] === 'local-lilies'
}

function containsQuerySecret(searchParams: URLSearchParams) {
  return Array.from(searchParams.keys()).some(key => LOCAL_LILIES_QUERY_SECRET_KEYS.has(key.toLowerCase()))
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params
  const base = platformBaseUrl()
  const searchParams = new URLSearchParams(request.nextUrl.searchParams)
  if (isLocalLiliesPath(path) && containsQuerySecret(searchParams)) {
    return new Response(JSON.stringify({
      detail: {
        code: 'query_secret_rejected',
        message: 'Local Lilies authentication is accepted only in request headers.',
      },
    }), {
      status: 400,
      headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
    })
  }
  const endpoint = '/' + path.join('/')
  const publicAuth = ['/api/v1/auth/login', '/api/v1/auth/register'].includes(endpoint)
  const origin = request.headers.get('origin')
  const write = !['GET', 'HEAD'].includes(request.method)
  const protocol = request.headers.get('x-forwarded-proto') || request.nextUrl.protocol.replace(':', '')
  let sameOrigin = false
  try { sameOrigin = Boolean(origin && new URL(origin).origin === `${protocol}://${request.headers.get('host')}`) } catch { /* Invalid origins are rejected. */ }
  if (write && !sameOrigin) {
    return NextResponse.json({ detail: '请求来源不正确，请从平台页面重试' }, { status: 403 })
  }
  const token = request.cookies.get(sessionCookie)?.value
  if (!publicAuth && !token) {
    return NextResponse.json({ detail: '请先登录' }, { status: 401 })
  }
  for (const key of ['frontend_token', 'token', 'access_token']) searchParams.delete(key)
  const query = searchParams.toString()
  const target = `${base}/${path.join('/')}${query ? `?${query}` : ''}`
  const headers = new Headers()
  if (token && !publicAuth) headers.set('Authorization', `Bearer ${token}`)
  const contentType = request.headers.get('content-type')
  if (contentType) headers.set('content-type', contentType)
  const accept = request.headers.get('accept')
  if (accept) headers.set('accept', accept)
  const lastEventId = request.headers.get('last-event-id')
  if (lastEventId) headers.set('last-event-id', lastEventId)
  const init: RequestInit = { method: request.method, headers, cache: 'no-store', signal: request.signal }
  if (!['GET', 'HEAD'].includes(request.method)) init.body = await request.arrayBuffer()
  const response = await fetch(target, init)
  const responseHeaders = new Headers()
  responseHeaders.set('content-type', response.headers.get('content-type') || 'application/json')
  responseHeaders.set('cache-control', 'no-store')
  const disposition = response.headers.get('content-disposition')
  if (disposition) responseHeaders.set('content-disposition', disposition)
  const retryAfter = response.headers.get('retry-after')
  if (retryAfter) responseHeaders.set('retry-after', retryAfter)
  const responseLastEventId = response.headers.get('last-event-id')
  if (responseLastEventId) responseHeaders.set('last-event-id', responseLastEventId)
  if (publicAuth && response.ok) {
    const result = await response.json()
    const outgoing = NextResponse.json({ user: result.user }, { status: response.status, headers: responseHeaders })
    outgoing.cookies.set(sessionCookie, result.token, {
      httpOnly: true, sameSite: 'lax', path: '/',
      secure: request.nextUrl.protocol === 'https:' || request.headers.get('x-forwarded-proto') === 'https',
      expires: new Date(result.expires_at * 1000),
    })
    return outgoing
  }
  const outgoing = new NextResponse(response.body, { status: response.status, headers: responseHeaders })
  // A late 401 from an older request must not erase a newly established session.
  if (endpoint === '/api/v1/auth/logout' || (endpoint === '/api/v1/auth/password' && response.ok)) {
    outgoing.cookies.set(sessionCookie, '', { httpOnly: true, sameSite: 'lax', path: '/', maxAge: 0 })
  }
  return outgoing
}

export const GET = proxy
export const POST = proxy
export const PUT = proxy
export const PATCH = proxy
export const DELETE = proxy
