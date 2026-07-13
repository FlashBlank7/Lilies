import fs from 'fs'
import path from 'path'
import { NextRequest } from 'next/server'

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
        const token = parseEnvValue(text, 'API_TOKEN')
        const platformUrl = parseEnvValue(text, 'AGENT_PLATFORM_URL')
        if (token) localEnvCache.set('API_TOKEN', token)
        if (platformUrl) localEnvCache.set('AGENT_PLATFORM_URL', platformUrl)
        if (token || platformUrl) return
      }
    } catch {
      // Local development auth should fall through to explicit browser token or process env.
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

function proxyApiToken(browserToken: string | null) {
  return browserToken || process.env.API_TOKEN || localEnvValue('API_TOKEN') || 'change-me'
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params
  const base = platformBaseUrl()
  const searchParams = new URLSearchParams(request.nextUrl.searchParams)
  const browserToken = request.headers.get('x-agent-platform-token') || searchParams.get('frontend_token')
  searchParams.delete('frontend_token')
  const query = searchParams.toString()
  const target = `${base}/${path.join('/')}${query ? `?${query}` : ''}`
  const headers = new Headers()
  headers.set('Authorization', `Bearer ${proxyApiToken(browserToken)}`)
  const contentType = request.headers.get('content-type')
  if (contentType) headers.set('content-type', contentType)
  const init: RequestInit = { method: request.method, headers, cache: 'no-store' }
  if (!['GET', 'HEAD'].includes(request.method)) init.body = await request.arrayBuffer()
  const response = await fetch(target, init)
  const responseHeaders = new Headers()
  responseHeaders.set('content-type', response.headers.get('content-type') || 'application/json')
  responseHeaders.set('cache-control', 'no-store')
  return new Response(response.body, { status: response.status, headers: responseHeaders })
}

export const GET = proxy
export const POST = proxy
export const PUT = proxy
export const PATCH = proxy
export const DELETE = proxy
