#!/usr/bin/env node

import { spawn } from 'node:child_process'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const args = new Map()
for (let index = 2; index < process.argv.length; index += 2) {
  args.set(process.argv[index], process.argv[index + 1])
}

const apiBase = args.get('--api') || 'http://127.0.0.1:18101'
const webBase = args.get('--web') || 'http://127.0.0.1:13101'
const token = args.get('--token') || 'v048-browser'
const outputDir = resolve(args.get('--output') || 'docs/workingon-archives/v0.4.8/browser')
const chromePath = args.get('--chrome') || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const debugPort = Number(args.get('--debug-port') || 19228)

mkdirSync(outputDir, { recursive: true })
const profileDir = resolve('.tmp/v048-browser/chrome-profile')
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
  const body = await response.text()
  if (!response.ok) throw new Error(`${response.status} ${path}: ${body}`)
  return body ? JSON.parse(body) : null
}

function capabilityContract() {
  return {
    schema_version: '1.0',
    contract_id: 'browser.evaluation.component.v1',
    generation_source: 'reference',
    source_requirement: 'Build a deterministic greeting workflow for browser evaluation.',
    target_user: 'Evaluation operator',
    business_goal: 'Return a deterministic greeting with traceable local evidence.',
    start_inputs: [{ name: 'query', label: 'Query', value_type: 'string', required: true, description: 'Name to greet.' }],
    functional_capabilities: [{
      id: 'F.echo', kind: 'F', title: 'Echo result',
      description: 'Transform the declared input into one customer-readable output.',
      required: true, requires: [], excludes: [], required_envelope: 'E1',
      acceptance: ['A terminal greeting exists.'], inputs: ['query'], outputs: ['greeting'],
    }],
    runtime_guarantees: [{
      id: 'G.trace', kind: 'G', title: 'Observable result',
      description: 'Expose the terminal result and execution trace.',
      required: true, requires: ['F.echo'], excludes: [], required_envelope: 'E1',
      acceptance: ['The run has a terminal status.'], guarantee_type: 'observability',
    }],
    external_contracts: [], required_envelope: 'E1', risk_level: 'low', risk_reasons: [],
    carrier_decisions: [
      { capability_id: 'F.echo', carrier_type: 'reusable_module', resource_hint: 'template transform', rationale: 'A deterministic editable transform implements the output.', status: 'bound', implementation_refs: ['template'] },
      { capability_id: 'G.trace', carrier_type: 'runtime_service', resource_hint: 'workflow runtime', rationale: 'Workflow Runtime records terminal state.', status: 'bound', implementation_refs: ['end'] },
    ],
    platform_coverage: [
      { capability_id: 'F.echo', owner: 'workflow_runtime', status: 'available', surface: 'deterministic workflow', notes: '' },
      { capability_id: 'G.trace', owner: 'platform_harness', status: 'available', surface: 'workflow and Platform Harness trace', notes: '' },
    ],
    evidence_plan: [{
      capability_ids: ['F.echo', 'G.trace'], target_level: 'H2', environment: 'sandbox',
      expected_status: 'component_verified', required_evidence: ['generated component cases', 'runtime test report'],
      claim_scope: 'Deterministic local component behavior only.',
    }],
    workflow_outline: ['Read query', 'Format greeting', 'Return greeting'],
    runtime_interface: 'Submit a query and receive a greeting.',
    claim_scope: { ceiling: 'component_verified', verified: ['deterministic local greeting shape'], excluded: ['live provider quality', 'production reliability'] },
    unresolved_decisions: [],
  }
}

async function createFixture() {
  const application = await api('/api/v1/applications', {
    method: 'POST',
    body: JSON.stringify({
      name: 'Browser evaluation greeting',
      description: 'A deterministic workflow used for local browser verification.',
      requirement: 'Build a deterministic greeting workflow with local component evidence.',
      capability_build_contract: capabilityContract(),
    }),
  })
  let revision = 0
  const mutate = async (op, data) => {
    const result = await api(`/api/v1/applications/${application.id}/draft`, {
      method: 'POST',
      body: JSON.stringify({
        expected_revision: revision,
        idempotency_key: crypto.randomUUID(),
        op,
        data,
      }),
    })
    revision = result.revision
  }
  await mutate('add_node', { node: { id: 'start', type: 'start', title: 'Input', config: { inputs: [{ name: 'query', type: 'string' }] } } })
  await mutate('add_node', { node: { id: 'template', type: 'template_transform', title: 'Greeting', config: { template: 'Hello {{ query }}', variables: { query: { $ref: { node_id: 'start', path: ['query'] } } } } } })
  await mutate('add_node', { node: { id: 'end', type: 'end', title: 'Result', config: { outputs: { greeting: { $ref: { node_id: 'template', path: ['text'] } } } } } })
  await mutate('add_edge', { edge: { id: 'edge-start-template', source: 'start', target: 'template' } })
  await mutate('add_edge', { edge: { id: 'edge-template-end', source: 'template', target: 'end', source_port: 'text' } })
  await mutate('add_test', { test: { id: 'customer_greeting', name: 'Customer greeting', requirement: 'A greeting is returned.', inputs: { query: 'Ada' }, assertions: [{ path: ['greeting'], operator: 'equals', expected: 'Hello Ada' }], mandatory: true } })
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
        return
      }
      for (const listener of this.listeners.get(message.method) || []) listener(message.params || {})
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
    const listeners = this.listeners.get(method) || []
    listeners.push(listener)
    this.listeners.set(method, listeners)
  }

  waitEvent(method, timeoutMs = 15000) {
    return new Promise((resolveEvent, rejectEvent) => {
      const timer = setTimeout(() => rejectEvent(new Error(`Timed out waiting for ${method}`)), timeoutMs)
      const listener = params => {
        clearTimeout(timer)
        const listeners = this.listeners.get(method) || []
        this.listeners.set(method, listeners.filter(item => item !== listener))
        resolveEvent(params)
      }
      this.on(method, listener)
    })
  }

  async evaluate(expression) {
    const response = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    })
    if (response.exceptionDetails) {
      throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text)
    }
    return response.result?.value
  }

  async navigate(url) {
    const loaded = this.waitEvent('Page.loadEventFired')
    await this.send('Page.navigate', { url })
    await loaded
  }

  async waitFor(expression, timeoutMs = 20000) {
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
    throw new Error(`Timed out waiting for: ${expression}${lastError ? ` (${lastError})` : ''}`)
  }

  async click(selector) {
    const encoded = JSON.stringify(selector)
    await this.waitFor(`Boolean(document.querySelector(${encoded}) && !document.querySelector(${encoded}).disabled)`)
    await this.evaluate(`document.querySelector(${encoded}).click(); true`)
  }

  async screenshot(name) {
    const response = await this.send('Page.captureScreenshot', { format: 'png', fromSurface: true })
    const path = resolve(outputDir, name)
    writeFileSync(path, Buffer.from(response.data, 'base64'))
    return path
  }
}

async function waitForDebugger() {
  const deadline = Date.now() + 15000
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/version`)
      if (response.ok) return response.json()
    } catch {
      // Chrome is still starting.
    }
    await new Promise(resolveWait => setTimeout(resolveWait, 100))
  }
  throw new Error('Chrome DevTools endpoint did not start')
}

const applicationId = await createFixture()
const chrome = spawn(chromePath, [
  '--headless=new',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  `--remote-debugging-port=${debugPort}`,
  `--user-data-dir=${profileDir}`,
  '--window-size=1440,960',
  'about:blank',
], { stdio: ['ignore', 'pipe', 'pipe'] })

const consoleErrors = []
const failedRequests = []
const ignoredRequests = []
const screenshots = []
let client

try {
  await waitForDebugger()
  const targetResponse = await fetch(
    `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(`${webBase}/icon.svg`)}`,
    { method: 'PUT' },
  )
  const target = await targetResponse.json()
  client = new CdpClient(target.webSocketDebuggerUrl)
  await client.connect()
  await client.send('Page.enable')
  await client.send('Runtime.enable')
  await client.send('Network.enable')
  await client.send('Console.enable')
  client.on('Runtime.exceptionThrown', event => consoleErrors.push({ type: 'exception', detail: event.exceptionDetails?.text || 'unknown' }))
  client.on('Console.messageAdded', event => {
    if (event.message?.level === 'error') consoleErrors.push({ type: 'console', detail: event.message.text })
  })
  client.on('Network.responseReceived', event => {
    if (event.response?.status >= 400) {
      const item = { status: event.response.status, url: event.response.url }
      if (new URL(event.response.url).pathname === '/favicon.ico') ignoredRequests.push(item)
      else failedRequests.push(item)
    }
  })
  client.on('Network.loadingFailed', event => {
    if (!event.canceled) failedRequests.push({ status: 0, url: event.errorText })
  })

  await client.evaluate(`localStorage.setItem('foundry.apiToken', ${JSON.stringify(token)}); true`)
  consoleErrors.length = 0
  failedRequests.length = 0
  await client.navigate(`${webBase}/applications/${applicationId}?tab=test`)
  await client.waitFor(`Boolean(document.querySelector('[data-evaluation-harness="studio"]'))`)
  await client.waitFor(`document.querySelectorAll('[data-evaluation-profile]').length === 6`)

  await client.click('[data-evaluation-action="preview"]')
  await client.waitFor(`Boolean(document.querySelector('[data-evaluation-plan="ready"]'))`)
  screenshots.push(await client.screenshot('evaluation-plan-desktop.png'))

  await client.click('[data-evaluation-action="apply"]')
  await client.waitFor(`Boolean(document.querySelector('[data-evaluation-action="run"]') && !document.querySelector('[data-evaluation-action="run"]').disabled)`)
  await client.click('[data-evaluation-action="run"]')
  await client.waitFor(`Boolean(document.querySelector('[data-evaluation-run="completed"]'))`, 30000)
  await client.waitFor(`document.querySelector('[data-evaluation-run="completed"]').innerText.includes('component_verified')`)
  await client.evaluate(`document.querySelector('[data-evaluation-run="completed"]').scrollIntoView({ block: 'start' }); true`)
  await new Promise(resolveWait => setTimeout(resolveWait, 300))
  screenshots.push(await client.screenshot('evaluation-result-desktop.png'))

  const desktop = await client.evaluate(`(() => {
    const harness = document.querySelector('[data-evaluation-harness="studio"]')
    const bounds = harness.getBoundingClientRect()
    const overflow = [...harness.querySelectorAll('*')].map(element => {
      const rect = element.getBoundingClientRect()
      const parent = element.parentElement?.getBoundingClientRect()
      const outsideParent = Boolean(parent && rect.width > 0 && (
        rect.width > parent.width + 2 || rect.right > parent.right + 2 || rect.left < parent.left - 2
      ))
      const computed = getComputedStyle(element)
      return { outsideParent, tag: element.tagName, className: String(element.className || ''), inlineStyle: element.getAttribute('style') || '', computedWidth: computed.width, boxSizing: computed.boxSizing, padding: computed.padding, margin: computed.margin, width: rect.width, height: rect.height, left: rect.left, right: rect.right, parentWidth: parent?.width || 0, parentLeft: parent?.left || 0, parentRight: parent?.right || 0, scrollWidth: element.scrollWidth }
    }).filter(item => item.outsideParent).slice(0, 20)
    return { viewport: [innerWidth, innerHeight], documentOverflow: document.documentElement.scrollWidth > innerWidth, harnessOverflow: harness.scrollWidth > harness.clientWidth, harnessWidth: bounds.width, overflow }
  })()`)

  await client.send('Emulation.setDeviceMetricsOverride', {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  })
  await client.evaluate(`document.querySelector('[data-evaluation-harness="studio"]').scrollIntoView({ block: 'start' }); true`)
  await new Promise(resolveWait => setTimeout(resolveWait, 300))
  screenshots.push(await client.screenshot('evaluation-mobile.png'))
  const mobile = await client.evaluate(`(() => {
    const harness = document.querySelector('[data-evaluation-harness="studio"]')
    const bounds = harness.getBoundingClientRect()
    const overflow = [...harness.querySelectorAll('*')].map(element => {
      const rect = element.getBoundingClientRect()
      const parent = element.parentElement?.getBoundingClientRect()
      const outsideParent = Boolean(parent && rect.width > 0 && (
        rect.width > parent.width + 2 || rect.right > parent.right + 2 || rect.left < parent.left - 2
      ))
      const computed = getComputedStyle(element)
      return { outsideParent, tag: element.tagName, className: String(element.className || ''), inlineStyle: element.getAttribute('style') || '', computedWidth: computed.width, boxSizing: computed.boxSizing, padding: computed.padding, margin: computed.margin, width: rect.width, height: rect.height, left: rect.left, right: rect.right, parentWidth: parent?.width || 0, parentLeft: parent?.left || 0, parentRight: parent?.right || 0, scrollWidth: element.scrollWidth }
    }).filter(item => item.outsideParent).slice(0, 20)
    return { viewport: [innerWidth, innerHeight], documentOverflow: document.documentElement.scrollWidth > innerWidth, harnessOverflow: harness.scrollWidth > harness.clientWidth, harnessWidth: bounds.width, overflow }
  })()`)

  await client.send('Emulation.clearDeviceMetricsOverride')
  await client.navigate(`${webBase}/runtime/${applicationId}`)
  await client.waitFor(`Boolean(document.querySelector('[data-customer-runtime="true"]'))`)
  const runtimeDisclosure = await client.evaluate(`({
    evaluationHarness: Boolean(document.querySelector('[data-evaluation-harness]')),
    evaluationText: document.body.innerText.includes('Evaluation Harness'),
    profileText: document.body.innerText.includes('H0') || document.body.innerText.includes('H5'),
  })`)

  const runHistory = await api(`/api/v1/applications/${applicationId}/evaluation/runs`)
  const evidence = {
    version: 'v0.4.8',
    application_id: applicationId,
    journey: ['preview_plan', 'apply_generated_cases', 'run_h2_component', 'inspect_history', 'verify_customer_runtime_disclosure'],
    latest_run: {
      outcome: runHistory[0]?.outcome,
      achieved_status: runHistory[0]?.achieved_status,
      passed: runHistory[0]?.passed,
      generated_test_count: runHistory[0]?.generated_test_ids?.length || 0,
      executed_test_count: runHistory[0]?.executed_test_ids?.length || 0,
    },
    desktop,
    mobile,
    runtime_disclosure: runtimeDisclosure,
    console_errors: consoleErrors,
    failed_requests: failedRequests,
    ignored_browser_requests: ignoredRequests,
    screenshots,
  }
  writeFileSync(resolve(outputDir, 'browser-evidence.json'), `${JSON.stringify(evidence, null, 2)}\n`)
  if (evidence.latest_run.achieved_status !== 'component_verified' || evidence.latest_run.passed !== true) throw new Error('H2 browser run did not reach component_verified')
  if (desktop.documentOverflow || desktop.harnessOverflow || desktop.overflow.length || mobile.documentOverflow || mobile.harnessOverflow || mobile.overflow.length) throw new Error('Evaluation Harness overflow detected')
  if (runtimeDisclosure.evaluationHarness || runtimeDisclosure.evaluationText || runtimeDisclosure.profileText) throw new Error('Customer Runtime disclosed Evaluation Harness internals')
  if (consoleErrors.length || failedRequests.length) throw new Error('Browser journey produced console errors or failed requests')
  process.stdout.write(`${JSON.stringify(evidence, null, 2)}\n`)
} finally {
  if (client?.socket?.readyState === WebSocket.OPEN) client.socket.close()
  chrome.kill('SIGTERM')
}
