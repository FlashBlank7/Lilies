#!/usr/bin/env node

import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const args = new Map()
for (let index = 2; index < process.argv.length; index += 2) {
  args.set(process.argv[index], process.argv[index + 1])
}

const webBase = args.get('--web') || 'http://127.0.0.1:3000'
let applicationId = args.get('--application-id') || ''
const seedRepairSmoke = args.get('--seed-repair-smoke') === 'true'
if (!applicationId && !seedRepairSmoke) throw new Error('--application-id or --seed-repair-smoke true is required')
const debugPort = Number(args.get('--debug-port') || 19243)
const outputDir = resolve(args.get('--output') || '.tmp/human-acceptance-journey')
const chromePath = args.get('--chrome') || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const profileDir = resolve(outputDir, 'chrome-profile')
const smokeMarker = 'v0.4.11-smoke'

mkdirSync(outputDir, { recursive: true })
rmSync(profileDir, { recursive: true, force: true })

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
    this.listeners.set(method, [...(this.listeners.get(method) || []), listener])
  }

  waitEvent(method, timeoutMs = 30000) {
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

  async waitFor(expression, timeoutMs = 30000, intervalMs = 100) {
    const deadline = Date.now() + timeoutMs
    let lastError = null
    while (Date.now() < deadline) {
      try {
        if (await this.evaluate(expression)) return
      } catch (error) {
        lastError = error
      }
      await new Promise(resolveWait => setTimeout(resolveWait, intervalMs))
    }
    throw new Error(`Timed out waiting for ${expression}${lastError ? `: ${lastError}` : ''}`)
  }

  async screenshot(name) {
    const response = await this.send('Page.captureScreenshot', { format: 'png', fromSurface: true })
    const path = resolve(outputDir, name)
    writeFileSync(path, Buffer.from(response.data, 'base64'))
    return {
      path,
      sha256: createHash('sha256').update(readFileSync(path)).digest('hex'),
    }
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

async function platformApi(path, options = {}) {
  const response = await fetch(`${webBase}/api/platform${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(`${options.method || 'GET'} ${path} returned ${response.status}: ${JSON.stringify(body)}`)
  return body
}

async function seedRepairApplication() {
  const application = await platformApi('/api/v1/applications', {
    method: 'POST',
    body: JSON.stringify({
      name: `${smokeMarker} acceptance repair journey`,
      requirement: `${smokeMarker}: repair a failed safety and answer acceptance gate.`,
    }),
  })
  let revision = 0
  const mutate = async (op, data) => {
    const result = await platformApi(`/api/v1/applications/${application.id}/draft`, {
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
  await mutate('add_node', {
    node: {
      id: 'start',
      type: 'start',
      title: 'Request',
      description: 'Receives the acceptance repair prompt.',
      config: { inputs: [{ name: 'prompt', label: 'Prompt', type: 'string', required: false }] },
      position: { x: 80, y: 120 },
    },
  })
  await mutate('add_node', {
    node: {
      id: 'end',
      type: 'end',
      title: 'Raw output',
      description: 'Intentionally incomplete before repair.',
      config: { outputs: { output: { $ref: { node_id: 'start', path: ['prompt'] } } } },
      position: { x: 420, y: 120 },
    },
  })
  await mutate('add_edge', { edge: { id: 'start_end', source: 'start', target: 'end' } })
  await mutate('add_test', {
    test: {
      id: 'repair_safety_answer',
      name: 'Safety and answer repair',
      requirement: 'The workflow must expose permission and sandbox boundaries and return a readable answer.',
      inputs: { prompt: 'Confirm the repaired workflow.' },
      assertions: [
        { path: ['answer'], operator: 'exists' },
        { path: ['answer'], operator: 'min_length', expected: 5 },
      ],
      required_node_types: ['start', 'permission_gate', 'sandbox_boundary', 'template_transform', 'answer'],
      mandatory: true,
    },
  })
  return { id: application.id, revision }
}

const chromeOutput = []
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
chrome.stdout.on('data', chunk => chromeOutput.push(String(chunk)))
chrome.stderr.on('data', chunk => chromeOutput.push(String(chunk)))

const evidence = {
  schema_version: '1.0',
  started_at: new Date().toISOString(),
  application_id: applicationId,
  seeded_application: null,
  before: null,
  transitions: [],
  after: null,
  test_request: null,
  test_requests: [],
  repair: null,
  cleanup: null,
  console_errors: [],
  failed_requests: [],
  screenshots: [],
  error: null,
}

let client
let exitCode = 0

try {
  if (seedRepairSmoke) {
    evidence.seeded_application = await seedRepairApplication()
    applicationId = evidence.seeded_application.id
    evidence.application_id = applicationId
  }
  await waitForDebugger()
  const targetResponse = await fetch(
    `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(`${webBase}/applications/${applicationId}?tab=test`)}`,
    { method: 'PUT' },
  )
  const target = await targetResponse.json()
  client = new CdpClient(target.webSocketDebuggerUrl)
  await client.connect()
  await client.send('Page.enable')
  await client.send('Runtime.enable')
  await client.send('Network.enable')
  client.on('Runtime.exceptionThrown', event => {
    evidence.console_errors.push({
      type: 'exception',
      detail: event.exceptionDetails?.exception?.description || event.exceptionDetails?.text || 'unknown',
    })
  })
  client.on('Console.messageAdded', event => {
    if (event.message?.level === 'error') {
      evidence.console_errors.push({ type: 'console', detail: event.message.text })
    }
  })
  client.on('Network.responseReceived', event => {
    const url = event.response?.url || ''
    if (url.includes(`/api/v1/applications/${applicationId}/tests/run`)) {
      const request = { status: event.response.status, url, at: new Date().toISOString() }
      evidence.test_request = request
      evidence.test_requests.push(request)
    }
    if (event.response?.status >= 400 && !url.endsWith('/favicon.ico')) {
      evidence.failed_requests.push({ status: event.response.status, url })
    }
  })
  client.on('Network.loadingFailed', event => {
    if (!event.canceled && event.errorText !== 'net::ERR_ABORTED') {
      evidence.failed_requests.push({ status: 0, url: event.errorText })
    }
  })

  await client.navigate(`${webBase}/applications/${applicationId}?tab=test`)
  await client.waitFor(`Boolean(document.querySelector('[data-detail-tab-url-state="synced"]'))`, 60000)
  await client.waitFor(`document.querySelectorAll('.acceptance-card').length > 0`, 60000)
  const stateExpression = `(() => ({
    running: document.querySelector('[data-acceptance-action="run-all"]')?.dataset.acceptanceRunning || '',
    button: document.querySelector('[data-acceptance-action="run-all"]')?.innerText || '',
    statuses: [...document.querySelectorAll('.acceptance-card-head > span')].map(item => item.innerText),
    cards: document.querySelectorAll('.acceptance-card').length,
    has_report: document.querySelector('.trace-log') !== null,
    has_repair: document.querySelector('[data-acceptance-repair="failed-gate-preview"]') !== null,
    repair_supported: Boolean(document.querySelector('[data-acceptance-repair-action="apply"]') && !document.querySelector('[data-acceptance-repair-action="apply"]').disabled),
    panel_scroll_top: document.querySelector('.left-panel .panel-body')?.scrollTop ?? null,
    panel_scroll_height: document.querySelector('.left-panel .panel-body')?.scrollHeight ?? null,
    panel_client_height: document.querySelector('.left-panel .panel-body')?.clientHeight ?? null,
    window_scroll_y: window.scrollY,
    run_button_rect: (() => {
      const rect = document.querySelector('[data-acceptance-action="run-all"]')?.getBoundingClientRect()
      return rect ? { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right } : null
    })(),
  }))()`
  evidence.before = await client.evaluate(stateExpression)
  await client.evaluate(`(() => {
    const sample = () => ({
      at: performance.now(),
      running: document.querySelector('[data-acceptance-action="run-all"]')?.dataset.acceptanceRunning || '',
      button: document.querySelector('[data-acceptance-action="run-all"]')?.innerText || '',
      statuses: [...document.querySelectorAll('.acceptance-card-head > span')].map(item => item.innerText),
    })
    window.__liliesAcceptanceProbe = { samples: [sample()] }
    const observer = new MutationObserver(() => window.__liliesAcceptanceProbe.samples.push(sample()))
    observer.observe(document.body, { attributes: true, childList: true, characterData: true, subtree: true })
    window.__liliesAcceptanceProbe.observer = observer
    document.querySelector('[data-acceptance-action="run-all"]').click()
    return true
  })()`)
  await client.waitFor(
    `window.__liliesAcceptanceProbe?.samples.some(item => item.running === 'true')`,
    10000,
    20,
  )
  evidence.screenshots.push({ id: 'acceptance-running', ...(await client.screenshot('acceptance-running.png')) })
  await client.waitFor(
    `Boolean(window.__liliesAcceptanceProbe?.samples.some(item => item.running === 'true') && document.querySelector('[data-acceptance-action="run-all"]')?.dataset.acceptanceRunning === 'false' && document.querySelector('.trace-log'))`,
    240000,
    100,
  )
  await new Promise(resolveWait => setTimeout(resolveWait, 1000))
  evidence.transitions = await client.evaluate(`(() => {
    window.__liliesAcceptanceProbe.observer.disconnect()
    const unique = []
    for (const sample of window.__liliesAcceptanceProbe.samples) {
      const key = JSON.stringify({ running: sample.running, button: sample.button, statuses: sample.statuses })
      if (unique.at(-1)?.key !== key) unique.push({ ...sample, key })
    }
    return unique.map(({ key, ...sample }) => sample)
  })()`)
  evidence.after = await client.evaluate(stateExpression)
  evidence.screenshots.push({ id: 'acceptance-finished', ...(await client.screenshot('acceptance-finished.png')) })

  const runningTransition = evidence.transitions.find(item => item.running === 'true')
  if (!runningTransition) throw new Error('Acceptance never exposed a visible running state')
  if (!runningTransition.statuses.length || runningTransition.statuses.some(status => !status.includes('运行'))) {
    throw new Error(`Acceptance cards did not visibly enter running state: ${JSON.stringify(runningTransition)}`)
  }
  if (evidence.after.running !== 'false' || !evidence.after.has_report) {
    throw new Error(`Acceptance did not finish with a visible report: ${JSON.stringify(evidence.after)}`)
  }
  if (!evidence.test_request || evidence.test_request.status !== 200) {
    throw new Error(`Acceptance request did not return HTTP 200: ${JSON.stringify(evidence.test_request)}`)
  }
  if (seedRepairSmoke) {
    if (!evidence.after.has_repair || !evidence.after.repair_supported) {
      throw new Error(`Failed acceptance did not produce an applicable repair preview: ${JSON.stringify(evidence.after)}`)
    }
    if (!evidence.after.statuses.some(status => /失败|failed/i.test(status))) {
      throw new Error(`Seeded acceptance did not visibly fail before repair: ${JSON.stringify(evidence.after.statuses)}`)
    }
    evidence.repair = {
      preview: await client.evaluate(`(() => ({
        text: document.querySelector('[data-acceptance-repair="failed-gate-preview"]')?.innerText || '',
        apply_label: document.querySelector('[data-acceptance-repair-action="apply"]')?.innerText || '',
        apply_disabled: Boolean(document.querySelector('[data-acceptance-repair-action="apply"]')?.disabled),
      }))()`),
      transitions: [],
      after: null,
    }
    evidence.screenshots.push({ id: 'acceptance-repair-preview', ...(await client.screenshot('acceptance-repair-preview.png')) })
    await client.evaluate(`(() => {
      const sample = () => ({
        at: performance.now(),
        running: document.querySelector('[data-acceptance-action="run-all"]')?.dataset.acceptanceRunning || '',
        button: document.querySelector('[data-acceptance-action="run-all"]')?.innerText || '',
        statuses: [...document.querySelectorAll('.acceptance-card-head > span')].map(item => item.innerText),
        repair_visible: document.querySelector('[data-acceptance-repair="failed-gate-preview"]') !== null,
      })
      window.__liliesRepairProbe = { samples: [sample()] }
      const observer = new MutationObserver(() => window.__liliesRepairProbe.samples.push(sample()))
      observer.observe(document.body, { attributes: true, childList: true, characterData: true, subtree: true })
      window.__liliesRepairProbe.observer = observer
      document.querySelector('[data-acceptance-repair-action="apply"]').click()
      return true
    })()`)
    await client.waitFor(`window.__liliesRepairProbe?.samples.some(item => item.running === 'true')`, 30000, 20)
    evidence.screenshots.push({ id: 'acceptance-repair-rerun', ...(await client.screenshot('acceptance-repair-rerun.png')) })
    await client.waitFor(`(() => {
      const statuses = [...document.querySelectorAll('.acceptance-card-head > span')]
      return document.querySelector('[data-acceptance-action="run-all"]')?.dataset.acceptanceRunning === 'false'
        && statuses.length > 0
        && statuses.every(item => item.classList.contains('passed'))
        && document.querySelector('.trace-log')
        && !document.querySelector('[data-acceptance-repair="failed-gate-preview"]')
    })()`, 240000, 100)
    await new Promise(resolveWait => setTimeout(resolveWait, 750))
    evidence.repair.transitions = await client.evaluate(`(() => {
      window.__liliesRepairProbe.observer.disconnect()
      const unique = []
      for (const sample of window.__liliesRepairProbe.samples) {
        const key = JSON.stringify({ running: sample.running, button: sample.button, statuses: sample.statuses, repair_visible: sample.repair_visible })
        if (unique.at(-1)?.key !== key) unique.push({ ...sample, key })
      }
      return unique.map(({ key, ...sample }) => sample)
    })()`)
    evidence.repair.after = await client.evaluate(stateExpression)
    evidence.screenshots.push({ id: 'acceptance-repair-passed', ...(await client.screenshot('acceptance-repair-passed.png')) })
    if (evidence.test_requests.length < 2 || evidence.test_requests.some(item => item.status !== 200)) {
      throw new Error(`Repair did not trigger a second successful acceptance request: ${JSON.stringify(evidence.test_requests)}`)
    }
    if (evidence.repair.after.statuses.some(status => !/通过|passed/i.test(status))) {
      throw new Error(`Acceptance did not pass after repair: ${JSON.stringify(evidence.repair.after)}`)
    }
  }
  if (evidence.console_errors.length) {
    throw new Error(`Browser console errors: ${JSON.stringify(evidence.console_errors)}`)
  }
  if (evidence.failed_requests.length) {
    throw new Error(`Failed browser requests: ${JSON.stringify(evidence.failed_requests)}`)
  }
} catch (error) {
  exitCode = 1
  evidence.error = error instanceof Error ? error.stack || error.message : String(error)
} finally {
  if (seedRepairSmoke && applicationId) {
    try {
      evidence.cleanup = await platformApi(`/api/v1/applications/${applicationId}/smoke-cleanup`, {
        method: 'POST',
        body: JSON.stringify({ smoke_marker: smokeMarker, dry_run: false }),
      })
    } catch (error) {
      evidence.cleanup = { error: error instanceof Error ? error.message : String(error) }
      if (!evidence.error) evidence.error = `Smoke cleanup failed: ${evidence.cleanup.error}`
      exitCode = 1
    }
  }
  evidence.finished_at = new Date().toISOString()
  evidence.chrome_output = chromeOutput.join('').slice(-4000)
  const outputPath = resolve(outputDir, 'journey.json')
  writeFileSync(outputPath, JSON.stringify(evidence, null, 2))
  console.log(JSON.stringify({
    output: outputPath,
    error: evidence.error,
    before: evidence.before,
    transitions: evidence.transitions,
    after: evidence.after,
    requests: evidence.test_requests,
    repair: evidence.repair,
    cleanup: evidence.cleanup,
    failed_requests: evidence.failed_requests,
  }, null, 2))
  try {
    client?.socket?.close()
  } catch {
    // Browser teardown is best effort.
  }
  chrome.kill('SIGTERM')
}

process.exitCode = exitCode
