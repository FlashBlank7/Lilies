import { NextRequest } from 'next/server'

export const dynamic = 'force-dynamic'

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params
  const base = (process.env.AGENT_PLATFORM_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')
  const searchParams = new URLSearchParams(request.nextUrl.searchParams)
  const browserToken = request.headers.get('x-agent-platform-token') || searchParams.get('frontend_token')
  searchParams.delete('frontend_token')
  const query = searchParams.toString()
  const target = `${base}/${path.join('/')}${query ? `?${query}` : ''}`
  const headers = new Headers()
  headers.set('Authorization', `Bearer ${browserToken || process.env.API_TOKEN || 'change-me'}`)
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
