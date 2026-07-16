#!/usr/bin/env node

import { spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { createServer } from 'node:http'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const args = new Map()
for (let index = 2; index < process.argv.length; index += 2) {
  args.set(process.argv[index], process.argv[index + 1])
}

const apiBase = args.get('--api') || 'http://127.0.0.1:18110'
const webBase = args.get('--web') || 'http://127.0.0.1:13110'
const token = args.get('--token') || 'v0410-browser'
const customerPort = Number(args.get('--customer-port') || 18111)
const debugPort = Number(args.get('--debug-port') || 19230)
const outputDir = resolve(args.get('--output') || 'docs/workingon/v0.4.10/browser')
const chromePath = args.get('--chrome') || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const profileDir = resolve('.tmp/v0410-browser/chrome-profile')

mkdirSync(outputDir, { recursive: true })
rmSync(profileDir, { recursive: true, force: true })

async function api(path, init = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      ...(init.headers || {}),
    },
  })
  const text = await response.text()
  if (!response.ok) throw new Error(`${response.status} ${path}: ${text}`)
  return text ? JSON.parse(text) : null
}

function schema(schemaId, fields) {
  return {
    schema_id: schemaId,
    version: 1,
    fields: fields.map(([name, valueType, required]) => ({ name, value_type: valueType, required })),
    additional_properties: false,
  }
}

function manifest() {
  return {
    schema_version: '1.0',
    connector_id: 'customer_system',
    version: 1,
    title: 'Browser Controlled Customer System',
    description: 'Controlled browser verification contract.',
    domain: 'customer_case',
    created_at: new Date().toISOString(),
    operations: [
      {
        id: 'get_case', title: 'Get case', kind: 'read', method: 'GET', path: '/cases/{case_id}',
        request_schema: schema('browser.case.read.request', [['case_id', 'string', true]]),
        response_schema: schema('browser.case.read.response', [['case_id', 'string', true], ['summary', 'string', true]]),
        required_roles: ['operator'],
      },
      {
        id: 'update_case', title: 'Update case', kind: 'write', method: 'PATCH', path: '/cases/{case_id}',
        request_schema: schema('browser.case.update.request', [['case_id', 'string', true], ['decision', 'string', true]]),
        response_schema: schema('browser.case.update.response', [['case_id', 'string', true], ['status', 'string', true], ['external_id', 'string', true], ['compensation_payload', 'object', true]]),
        required_roles: ['operator'], compensation_operation_id: 'restore_case',
      },
      {
        id: 'restore_case', title: 'Restore case', kind: 'compensate', method: 'POST', path: '/cases/{case_id}/compensate',
        request_schema: schema('browser.case.restore.request', [['case_id', 'string', true], ['previous_decision', 'string', true]]),
        response_schema: schema('browser.case.restore.response', [['case_id', 'string', true], ['status', 'string', true], ['previous_decision', 'string', true]]),
        required_roles: ['operator'],
      },
    ],
    deployment_profiles: [{
      id: 'test', environment: 'test', base_url: `http://127.0.0.1:${customerPort}`,
      auth_type: 'bearer', allowed_hosts: ['127.0.0.1'], available: true,
      timeout_seconds: 5, claim_ceiling: 'H3', excluded_claims: ['customer production readiness'],
    }],
    callback_schema: schema('browser.case.callback', [['phase', 'string', true], ['note', 'string', false]]),
  }
}

const customerStats = { reads: 0, writes: 0, compensations: 0, bearer_injected: [] }
const customerServer = createServer((request, response) => {
  const send = (status, body) => {
    const encoded = JSON.stringify(body)
    response.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(encoded) })
    response.end(encoded)
  }
  const path = new URL(request.url, `http://127.0.0.1:${customerPort}`).pathname
  customerStats.bearer_injected.push(/^Bearer\s+\S+$/.test(request.headers.authorization || ''))
  if (request.method === 'GET' && path.startsWith('/cases/')) {
    customerStats.reads += 1
    send(200, { case_id: path.split('/').pop(), summary: 'Controlled browser customer case' })
    return
  }
  let raw = ''
  request.on('data', chunk => { raw += chunk })
  request.on('end', () => {
    const body = raw ? JSON.parse(raw) : {}
    if (request.method === 'PATCH' && path.startsWith('/cases/')) {
      customerStats.writes += 1
      const caseId = path.split('/').pop()
      send(200, { case_id: caseId, status: 'updated', external_id: `external-${caseId}`, compensation_payload: { case_id: caseId, previous_decision: 'pending' } })
    } else if (request.method === 'POST' && path.endsWith('/compensate')) {
      customerStats.compensations += 1
      send(200, { case_id: path.split('/').at(-2), status: 'compensated', previous_decision: body.previous_decision || '' })
    } else {
      send(404, { error: 'not found' })
    }
  })
})

async function listenCustomer() {
  await new Promise((resolveListen, rejectListen) => {
    customerServer.once('error', rejectListen)
    customerServer.listen(customerPort, '127.0.0.1', resolveListen)
  })
}

async function createFixture() {
  const application = await api('/api/v1/applications', {
    method: 'POST',
    body: JSON.stringify({
      name: 'Controlled customer embedding',
      description: 'A customer-system workflow with bounded identity and governed writeback.',
      requirement: 'Read one customer case and preview an idempotent governed update.',
    }),
  })
  let draft = await api(`/api/v1/applications/${application.id}/draft`)
  await api(`/api/v1/applications/${application.id}/scenarios/customer_system_embedding/apply`, {
    method: 'POST',
    body: JSON.stringify({ expected_revision: draft.revision, expected_content_hash: draft.content_hash, idempotency_key: randomUUID() }),
  })
  draft = await api(`/api/v1/applications/${application.id}/draft`)
  await api(`/api/v1/applications/${application.id}/draft`, {
    method: 'POST',
    body: JSON.stringify({
      expected_revision: draft.revision,
      idempotency_key: randomUUID(),
      op: 'update_node',
      data: {
        node_id: 'customer_decision',
        merge_config: false,
        changes: { type: 'template_transform', title: 'Controlled Browser Decision', config: { template: 'approved', variables: {} } },
      },
    }),
  })
  await api('/api/v1/connectors/manifests', { method: 'POST', body: JSON.stringify(manifest()) })
  await api('/api/v1/platform/secrets', {
    method: 'POST',
    body: JSON.stringify({ owner_id: 'browser-tenant', name: 'customer-system', value: 'browser-controlled-secret', description: 'Browser evidence secret' }),
  })
  await api('/api/v1/connectors/bindings', {
    method: 'PUT',
    body: JSON.stringify({
      expected_revision: 0,
      binding: {
        connector_id: 'customer_system', connector_version: 1, tenant_id: 'browser-tenant',
        external_tenant_id: 'browser-acme', profile_id: 'test', secret_ref: 'secret://browser-tenant/customer-system',
        application_ids: [application.id], allowed_operations: ['get_case', 'update_case', 'restore_case'],
        subjects: [{ external_subject: 'browser-subject', actor_id: 'browser-operator', roles: ['operator'] }], enabled: true,
      },
    }),
  })
  await api('/api/v1/connectors/policies', {
    method: 'PUT',
    body: JSON.stringify({
      expected_revision: 0,
      policy: {
        connector_id: 'customer_system', connector_version: 1, tenant_id: 'browser-tenant', domain: 'customer_case',
        allowed_profiles: ['test'], allowed_operations: ['get_case', 'update_case', 'restore_case'], required_roles: ['operator'],
        max_payload_bytes: 10000, mutation_preauthorization_required: true, allow_dry_run: true, allow_compensation_during_stop: true,
      },
    }),
  })
  return application.id
}

class CdpClient {
  constructor(url) {
    this.url = url
    this.socket = null
    this.nextId = 1
    this.pending = new Map()
    this.listeners = new Map()
  }

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
      } else {
        for (const listener of this.listeners.get(message.method) || []) listener(message.params || {})
      }
    })
  }

  send(method, params = {}) {
    const id = this.nextId++
    return new Promise((resolveCommand, rejectCommand) => {
      this.pending.set(id, { method, resolve: resolveCommand, reject: rejectCommand })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }

  on(method, listener) {
    this.listeners.set(method, [...(this.listeners.get(method) || []), listener])
  }

  waitEvent(method, timeoutMs = 20000) {
    return new Promise((resolveEvent, rejectEvent) => {
      const timer = setTimeout(() => rejectEvent(new Error(`Timed out waiting for ${method}`)), timeoutMs)
      const listener = params => {
        clearTimeout(timer)
        this.listeners.set(method, (this.listeners.get(method) || []).filter(item => item !== listener))
        resolveEvent(params)
      }
      this.on(method, listener)
    })
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text)
    return result.result?.value
  }

  async navigate(url) {
    const loaded = this.waitEvent('Page.loadEventFired')
    await this.send('Page.navigate', { url })
    await loaded
  }

  async waitFor(expression, timeoutMs = 30000) {
    const deadline = Date.now() + timeoutMs
    let lastError = null
    while (Date.now() < deadline) {
      try {
        if (await this.evaluate(expression)) return
      } catch (error) {
        lastError = error
      }
      await new Promise(resolveWait => setTimeout(resolveWait, 150))
    }
    throw new Error(`Timed out waiting for ${expression}${lastError ? `: ${lastError}` : ''}`)
  }

  async click(selector) {
    const encoded = JSON.stringify(selector)
    await this.waitFor(`Boolean(document.querySelector(${encoded}) && !document.querySelector(${encoded}).disabled)`)
    await this.evaluate(`document.querySelector(${encoded}).click(); true`)
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
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/version`)
      if (response.ok) return
    } catch {
      // Chrome is still starting.
    }
    await new Promise(resolveWait => setTimeout(resolveWait, 100))
  }
  throw new Error('Chrome DevTools endpoint did not start')
}

function measureExpression(selector) {
  return `(() => {
    const surface = document.querySelector(${JSON.stringify(selector)})
    const rect = surface.getBoundingClientRect()
    const interactiveOutside = [...surface.querySelectorAll('button,input,textarea,select')].map(element => {
      const item = element.getBoundingClientRect()
      return { tag: element.tagName, left: item.left, right: item.right, width: item.width, text: (element.innerText || element.getAttribute('aria-label') || '').slice(0, 60) }
    }).filter(item => item.width > 0 && (item.left < -2 || item.right > innerWidth + 2))
    return {
      present: Boolean(surface), viewport: [innerWidth, innerHeight], width: rect.width,
      documentOverflow: document.documentElement.scrollWidth > innerWidth + 1,
      surfaceOverflow: surface.scrollWidth > surface.clientWidth + 1,
      interactiveOutside,
    }
  })()`
}

await listenCustomer()
const applicationId = await createFixture()
const chrome = spawn(chromePath, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profileDir}`, '--window-size=1440,960', 'about:blank',
], { stdio: ['ignore', 'pipe', 'pipe'] })

const consoleErrors = []
const failedRequests = []
const ignoredRequests = []
const screenshots = []
const layout = {}
let client

try {
  await waitForDebugger()
  const targetResponse = await fetch(`http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(`${webBase}/icon.svg`)}`, { method: 'PUT' })
  const target = await targetResponse.json()
  client = new CdpClient(target.webSocketDebuggerUrl)
  await client.connect()
  await client.send('Page.enable')
  await client.send('Runtime.enable')
  await client.send('Network.enable')
  client.on('Runtime.exceptionThrown', event => consoleErrors.push({ type: 'exception', detail: event.exceptionDetails?.text || 'unknown' }))
  client.on('Console.messageAdded', event => {
    if (event.message?.level === 'error') consoleErrors.push({ type: 'console', detail: event.message.text })
  })
  client.on('Network.responseReceived', event => {
    if (event.response?.status < 400) return
    const item = { status: event.response.status, url: event.response.url }
    if (new URL(event.response.url).pathname === '/favicon.ico') ignoredRequests.push(item)
    else failedRequests.push(item)
  })
  client.on('Network.loadingFailed', event => {
    const item = { status: 0, url: event.errorText }
    if (event.canceled || event.errorText === 'net::ERR_ABORTED') ignoredRequests.push(item)
    else failedRequests.push(item)
  })

  await client.navigate(`${webBase}/`)
  await client.evaluate(`localStorage.setItem('foundry.apiToken', ${JSON.stringify(token)}); true`)
  await client.navigate(`${webBase}/applications/${applicationId}?tab=integrations`)
  await client.waitFor(`Boolean(document.querySelector('[data-engineer-connector-workspace="true"]'))`)
  await client.click('[data-connector-action="execute"]')
  await client.waitFor(`document.querySelectorAll('[data-connector-status]').length >= 1`)
  await client.evaluate(`document.querySelector('[data-engineer-connector-workspace="true"]').scrollIntoView({ block: 'start' }); true`)
  await new Promise(resolveWait => setTimeout(resolveWait, 300))
  layout.engineer_desktop = await client.evaluate(measureExpression('[data-engineer-connector-workspace="true"]'))
  screenshots.push({ id: 'engineer-integrations-desktop', ...(await client.screenshot('engineer-integrations-desktop.png')) })

  await client.navigate(`${webBase}/runtime/${applicationId}`)
  await client.waitFor(`Boolean(document.querySelector('[data-customer-connector-view="bounded"]'))`)
  await client.waitFor(`Boolean(document.querySelector('[data-runtime-input="request"] textarea'))`)
  await client.evaluate(`(() => {
    const input = document.querySelector('[data-runtime-input="request"] textarea')
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, '{"case_id":"case-browser"}')
    input.dispatchEvent(new Event('input', { bubbles: true }))
    return true
  })()`)
  await client.click('[data-customer-runtime-action="start"]')
  await client.waitFor(`Boolean(document.querySelector('[data-customer-connector-receipt="redacted"]'))`, 40000)
  await client.waitFor(`document.querySelector('[data-run-status]')?.getAttribute('data-run-status') === 'succeeded'`, 40000)
  await client.evaluate(`document.querySelector('[data-customer-connector-receipt="redacted"]').scrollIntoView({ block: 'center' }); true`)
  await new Promise(resolveWait => setTimeout(resolveWait, 300))
  const customerDisclosure = await client.evaluate(`({
    receipt: document.querySelector('[data-customer-connector-receipt="redacted"]').innerText,
    receiptStatus: document.querySelector('[data-connector-receipt-status]').getAttribute('data-connector-receipt-status'),
    sideEffect: document.querySelector('[data-connector-side-effect]').getAttribute('data-connector-side-effect'),
    hasSecretValue: document.body.innerText.includes('browser-controlled-secret'),
    hasSignatureHeader: document.body.innerText.includes('X-Lilies-Signature'),
    hasRawConnectorResult: Boolean(document.querySelector('[data-markdown-surface="customer-runtime-result"]')),
    visibleInputs: [...document.querySelectorAll('[data-runtime-input]')].map(item => item.getAttribute('data-runtime-input')),
    stepTitles: [...document.querySelectorAll('[data-step-status] strong')].map(item => item.innerText),
  })`)
  layout.customer_desktop = await client.evaluate(measureExpression('[data-customer-runtime="true"]'))
  screenshots.push({ id: 'customer-runtime-receipt-desktop', ...(await client.screenshot('customer-runtime-receipt-desktop.png')) })

  await client.send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true })
  await client.evaluate(`document.querySelector('[data-customer-connector-receipt="redacted"]').scrollIntoView({ block: 'center' }); true`)
  await new Promise(resolveWait => setTimeout(resolveWait, 300))
  layout.customer_mobile = await client.evaluate(measureExpression('[data-customer-runtime="true"]'))
  screenshots.push({ id: 'customer-runtime-receipt-mobile', ...(await client.screenshot('customer-runtime-receipt-mobile.png')) })
  await client.send('Emulation.clearDeviceMetricsOverride')

  await client.navigate(`${webBase}/governance`)
  await client.waitFor(`Boolean(document.querySelector('[data-governance-console="true"]'))`)
  await client.click('[data-governance-tab="Connector Operations"]')
  await client.waitFor(`Boolean(document.querySelector('[data-governance-connectors="tenant-redacted"]'))`)
  await client.waitFor(`document.querySelectorAll('[data-governance-connectors="tenant-redacted"] tbody tr').length >= 1`)
  await client.evaluate(`document.querySelector('[data-governance-connectors="tenant-redacted"]').scrollIntoView({ block: 'start' }); true`)
  await new Promise(resolveWait => setTimeout(resolveWait, 300))
  const governanceDisclosure = await client.evaluate(`({
    hasSecretValue: document.body.innerText.includes('browser-controlled-secret'),
    hasRawPayload: document.body.innerText.includes('{"case_id":"case-browser"}'),
    hasUnsupportedProduction: document.body.innerText.includes('unsupported'),
    receiptRows: document.querySelectorAll('[data-governance-connectors="tenant-redacted"] tbody tr').length,
  })`)
  layout.governance_desktop = await client.evaluate(measureExpression('[data-governance-connectors="tenant-redacted"]'))
  screenshots.push({ id: 'governance-connectors-desktop', ...(await client.screenshot('governance-connectors-desktop.png')) })

  const executions = await api(`/api/v1/connectors/executions?tenant_id=browser-tenant&limit=100`)
  const governance = await api('/api/v1/governance/connectors?tenant_id=browser-tenant&limit=100')
  const evidence = {
    version: 'v0.4.10', application_id: applicationId,
    journey: ['engineer_controlled_preview', 'customer_runtime_controlled_dry_run', 'customer_mobile_receipt', 'governance_tenant_redacted_receipts'],
    controlled_customer: customerStats,
    connector: {
      execution_count: executions.items.length,
      operation_ids: [...new Set(executions.items.map(item => item.operation_id))].sort(),
      statuses: [...new Set(executions.items.map(item => item.status))].sort(),
      write_side_effect_states: executions.items.filter(item => item.operation_kind === 'write').map(item => item.side_effect_state),
      claim_boundary: executions.claim_boundary,
    },
    governance: { receipt_count: governance.items.length, support: governance.support, claim_boundary: governance.claim_boundary },
    customer_disclosure: customerDisclosure,
    governance_disclosure: governanceDisclosure,
    layout, console_errors: consoleErrors, failed_requests: failedRequests, ignored_requests: ignoredRequests, screenshots,
  }
  writeFileSync(resolve(outputDir, 'browser-evidence.json'), `${JSON.stringify(evidence, null, 2)}\n`)

  const brokenLayouts = Object.entries(layout).filter(([, item]) => item.documentOverflow || item.surfaceOverflow || item.interactiveOutside.length)
  if (brokenLayouts.length) throw new Error(`Layout overflow detected: ${JSON.stringify(brokenLayouts)}`)
  if (customerDisclosure.hasSecretValue || customerDisclosure.hasSignatureHeader || customerDisclosure.hasRawConnectorResult || customerDisclosure.visibleInputs.join(',') !== 'request') throw new Error('Customer Runtime disclosure boundary failed')
  if (customerDisclosure.stepTitles.some(item => item.startsWith('Map '))) throw new Error('Customer Runtime exposed engineering mapping steps')
  if (customerDisclosure.receiptStatus !== 'dry_run' || customerDisclosure.sideEffect !== 'none') throw new Error('Customer Runtime did not disclose the bounded dry-run receipt')
  if (!customerDisclosure.receipt.includes('仅预演（未写入）') || !customerDisclosure.receipt.includes('未产生')) throw new Error('Customer Runtime exposed technical receipt codes')
  if (governanceDisclosure.hasSecretValue || governanceDisclosure.hasRawPayload || !governanceDisclosure.hasUnsupportedProduction) throw new Error('Governance disclosure boundary failed')
  if (!governance.items.length || !governance.claim_boundary.startsWith('Tenant-safe')) throw new Error('Governance connector evidence is missing')
  if (customerStats.writes !== 0) throw new Error('Customer Runtime dry-run produced a write side effect')
  if (consoleErrors.length || failedRequests.length) throw new Error('Browser journey produced console errors or failed requests')
  process.stdout.write(`${JSON.stringify(evidence, null, 2)}\n`)
} finally {
  if (client?.socket?.readyState === WebSocket.OPEN) client.socket.close()
  chrome.kill('SIGTERM')
  await new Promise(resolveClose => customerServer.close(resolveClose))
}
