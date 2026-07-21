#!/usr/bin/env node

import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const args = new Map()
for (let index = 2; index < process.argv.length; index += 2) args.set(process.argv[index], process.argv[index + 1])
const apiBase = args.get('--api') || 'http://127.0.0.1:18120'
const webBase = args.get('--web') || 'http://127.0.0.1:13120'
const token = args.get('--token') || 'v0412-browser'
const hostPort = Number(args.get('--host-port') || 18121)
const debugPort = Number(args.get('--debug-port') || 19240)
const outputDir = resolve(args.get('--output') || 'docs/evidence/v0.4.12/browser')
const chromePath = args.get('--chrome') || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const profileDir = resolve('.tmp/v0412-browser/chrome-profile')
mkdirSync(outputDir, { recursive: true })
rmSync(profileDir, { recursive: true, force: true })

async function api(path, init = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...(init.headers || {}) },
  })
  const body = await response.text()
  if (!response.ok) throw new Error(`${response.status} ${path}: ${body}`)
  return body ? JSON.parse(body) : null
}

const hostStats = { valid_reads: 0, invalid_reads: 0 }
const host = createServer((request, response) => {
  const path = new URL(request.url, `http://127.0.0.1:${hostPort}`).pathname
  let body
  if (path.startsWith('/valid/')) {
    hostStats.valid_reads += 1
    body = { id: path.split('/').pop(), name: 'Browser contract item' }
  } else if (path.startsWith('/invalid/')) {
    hostStats.invalid_reads += 1
    body = { id: path.split('/').pop() }
  } else {
    response.writeHead(404, { 'Content-Type': 'application/json' })
    response.end('{"error":"not found"}')
    return
  }
  const encoded = JSON.stringify(body)
  response.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(encoded) })
  response.end(encoded)
})

function listenHost() {
  return new Promise((resolveListen, rejectListen) => {
    host.once('error', rejectListen)
    host.listen(hostPort, '127.0.0.1', resolveListen)
  })
}

function openapi(path, title) {
  return JSON.stringify({
    openapi: '3.1.0',
    info: { title, version: '1' },
    paths: {
      [`/${path}/{item_id}`]: {
        get: {
          operationId: `${path}GeneratedItem`,
          parameters: [{ name: 'item_id', in: 'path', required: true, schema: { type: 'string', example: 'browser' } }],
          responses: { 200: { description: 'item', content: { 'application/json': { schema: { $ref: '#/components/schemas/Item' } } } } },
        },
      },
    },
    components: { schemas: { Item: { type: 'object', properties: { id: { type: 'string' }, name: { type: 'string' } }, required: ['id', 'name'] } } },
  })
}

class CdpClient {
  constructor(url) { this.url = url; this.socket = null; this.nextId = 1; this.pending = new Map(); this.listeners = new Map() }
  async connect() {
    this.socket = new WebSocket(this.url)
    await new Promise((resolveOpen, rejectOpen) => {
      this.socket.addEventListener('open', resolveOpen, { once: true })
      this.socket.addEventListener('error', rejectOpen, { once: true })
    })
    this.socket.addEventListener('message', event => {
      const message = JSON.parse(event.data)
      if (message.id) {
        const pending = this.pending.get(message.id)
        if (!pending) return
        this.pending.delete(message.id)
        if (message.error) pending.reject(new Error(`${pending.method}: ${message.error.message}`))
        else pending.resolve(message.result || {})
      } else for (const listener of this.listeners.get(message.method) || []) listener(message.params || {})
    })
  }
  send(method, params = {}) {
    const id = this.nextId++
    return new Promise((resolveCommand, rejectCommand) => {
      this.pending.set(id, { method, resolve: resolveCommand, reject: rejectCommand })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }
  on(method, listener) { this.listeners.set(method, [...(this.listeners.get(method) || []), listener]) }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text)
    return result.result?.value
  }
  async waitFor(expression, timeoutMs = 30000) {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      try { if (await this.evaluate(expression)) return } catch { /* Page may still be loading. */ }
      await new Promise(resolveWait => setTimeout(resolveWait, 150))
    }
    throw new Error(`Timed out waiting for ${expression}`)
  }
  async navigate(url) {
    await this.send('Page.navigate', { url })
    await this.waitFor('document.readyState === "complete"')
  }
  async click(selector) {
    const encoded = JSON.stringify(selector)
    await this.waitFor(`Boolean(document.querySelector(${encoded}) && !document.querySelector(${encoded}).disabled)`)
    await this.evaluate(`document.querySelector(${encoded}).click(); true`)
  }
  async fill(selector, value) {
    await this.waitFor(`Boolean(document.querySelector(${JSON.stringify(selector)}))`)
    await this.evaluate(`(() => {
      const element = document.querySelector(${JSON.stringify(selector)})
      const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
      Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, ${JSON.stringify(value)})
      element.dispatchEvent(new Event('input', { bubbles: true }))
      return true
    })()`)
  }
  async screenshot(name) {
    const result = await this.send('Page.captureScreenshot', { format: 'png', fromSurface: true })
    const path = resolve(outputDir, name)
    writeFileSync(path, Buffer.from(result.data, 'base64'))
    return { path: name, sha256: createHash('sha256').update(readFileSync(path)).digest('hex') }
  }
}

async function waitForDebugger() {
  const deadline = Date.now() + 15000
  while (Date.now() < deadline) {
    try { if ((await fetch(`http://127.0.0.1:${debugPort}/json/version`)).ok) return } catch { /* Starting. */ }
    await new Promise(resolveWait => setTimeout(resolveWait, 100))
  }
  throw new Error('Chrome DevTools endpoint did not start')
}

async function fillGeneration(client, { connectorId, path, title }) {
  await client.fill('[data-openapi-field="connector-id"]', connectorId)
  await client.fill('[data-openapi-field="domain"]', 'browser_contract')
  await client.fill('[data-openapi-field="base-url"]', `http://127.0.0.1:${hostPort}`)
  await client.fill('[data-openapi-field="allowed-hosts"]', '127.0.0.1')
  await client.fill('[data-openapi-field="document"]', openapi(path, title))
  await client.click('[data-connector-action="generate-openapi"]')
  await client.waitFor(`document.querySelector('[data-openapi-generation-review]')?.innerText.includes('/${path}/{item_id}')`)
}

await listenHost()
const application = await api('/api/v1/applications', {
  method: 'POST',
  body: JSON.stringify({ name: 'OpenAPI browser evidence', description: 'Browser verification fixture', requirement: 'Generate a Connector from OpenAPI and run contracts.' }),
})
const chrome = spawn(chromePath, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profileDir}`, '--window-size=1440,960', 'about:blank',
], { stdio: ['ignore', 'pipe', 'pipe'] })
const consoleErrors = []
const failedRequests = []
const screenshots = []
let client

try {
  await waitForDebugger()
  const target = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(webBase)}`, { method: 'PUT' })).json()
  client = new CdpClient(target.webSocketDebuggerUrl)
  await client.connect()
  await client.send('Page.enable'); await client.send('Runtime.enable'); await client.send('Network.enable')
  client.on('Runtime.exceptionThrown', event => consoleErrors.push(event.exceptionDetails?.text || 'unknown exception'))
  client.on('Console.messageAdded', event => { if (event.message?.level === 'error') consoleErrors.push(event.message.text) })
  client.on('Network.responseReceived', event => {
    if (event.response?.status >= 400 && new URL(event.response.url).pathname !== '/favicon.ico') failedRequests.push({ status: event.response.status, url: event.response.url })
  })
  await client.navigate(webBase)
  await client.evaluate(`localStorage.setItem('foundry.apiToken', ${JSON.stringify(token)}); localStorage.setItem('foundry.locale', 'zh'); true`)
  await client.navigate(`${webBase}/applications/${application.id}`)
  await client.click('[data-studio-tab="integrations"]')
  await client.waitFor(`Boolean(document.querySelector('[data-openapi-default-path="true"]'))`)

  await fillGeneration(client, { connectorId: 'browser_valid', path: 'valid', title: 'Browser Valid API' })
  await client.click('[data-connector-action="run-generated-contracts"]')
  await client.waitFor(`document.querySelector('[data-contract-run-status]')?.getAttribute('data-contract-run-status') === 'passed'`)
  const passDisclosure = await client.evaluate(`({
    status: document.querySelector('[data-contract-run-status]').getAttribute('data-contract-run-status'),
    result: document.querySelector('[data-contract-run-status]').innerText,
    registerEnabled: !document.querySelector('[data-connector-action="register-generated"]').disabled,
  })`)
  await client.evaluate(`document.querySelector('[data-openapi-generation-review]').scrollIntoView({ block: 'start' }); true`)
  screenshots.push({ id: 'generated-contract-pass', ...(await client.screenshot('generated-contract-pass.png')) })
  await client.click('[data-connector-action="register-generated"]')
  await client.waitFor(`document.body.innerText.includes('已验证的 Connector 版本已登记')`)

  await fillGeneration(client, { connectorId: 'browser_invalid', path: 'invalid', title: 'Browser Invalid API' })
  await client.click('[data-connector-action="run-generated-contracts"]')
  await client.waitFor(`document.querySelector('[data-contract-run-status]')?.getAttribute('data-contract-run-status') === 'failed'`)
  const failureDisclosure = await client.evaluate(`({
    status: document.querySelector('[data-contract-run-status]').getAttribute('data-contract-run-status'),
    result: document.querySelector('[data-contract-run-status]').innerText,
    registerEnabled: !document.querySelector('[data-connector-action="register-generated"]').disabled,
  })`)
  await client.evaluate(`document.querySelector('[data-contract-run-status]').scrollIntoView({ block: 'center' }); true`)
  screenshots.push({ id: 'generated-contract-failure', ...(await client.screenshot('generated-contract-failure.png')) })

  const layout = await client.evaluate(`(() => {
    const surface = document.querySelector('[data-engineer-connector-workspace="true"]')
    return { viewport: [innerWidth, innerHeight], document_overflow: document.documentElement.scrollWidth > innerWidth + 1, surface_overflow: surface.scrollWidth > surface.clientWidth + 1 }
  })()`)
  const evidence = {
    version: 'v0.4.12', application_id: application.id,
    journey: ['import_openapi', 'review_generated_mapping', 'pass_contract', 'register_verified', 'inspect_failed_contract'],
    pass_disclosure: passDisclosure, failure_disclosure: failureDisclosure,
    host_stats: hostStats, layout, console_errors: consoleErrors, failed_requests: failedRequests, screenshots,
  }
  writeFileSync(resolve(outputDir, 'browser-evidence.json'), `${JSON.stringify(evidence, null, 2)}\n`)
  if (!passDisclosure.registerEnabled || failureDisclosure.registerEnabled) throw new Error('Registration gate did not follow contract status')
  if (!failureDisclosure.result.includes('missing required fields') || !failureDisclosure.result.includes('HTTP status in')) throw new Error('Failure expected/actual evidence is not visible')
  if (layout.document_overflow || layout.surface_overflow) throw new Error(`Layout overflow: ${JSON.stringify(layout)}`)
  if (consoleErrors.length || failedRequests.length) throw new Error(`Browser errors: ${JSON.stringify({ consoleErrors, failedRequests })}`)
  process.stdout.write(`${JSON.stringify(evidence, null, 2)}\n`)
} finally {
  if (client?.socket?.readyState === WebSocket.OPEN) client.socket.close()
  chrome.kill('SIGTERM')
  await new Promise(resolveClose => host.close(resolveClose))
}
