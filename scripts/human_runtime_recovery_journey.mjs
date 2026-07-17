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
const debugPort = Number(args.get('--debug-port') || 19249)
const outputDir = resolve(args.get('--output') || '.tmp/human-runtime-recovery-journey')
const chromePath = args.get('--chrome') || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const profileDir = resolve(outputDir, 'chrome-profile')
const smokeMarker = 'v0.4.11-smoke'
const recoveryInput = '请确认退款条件，并给出一段可以直接发送给客户的说明。'

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

async function waitForRun(runId, status) {
  const deadline = Date.now() + 30000
  while (Date.now() < deadline) {
    const run = await platformApi(`/api/v1/runs/${runId}`)
    if (run.status === status) return run
    if (['succeeded', 'failed', 'paused', 'cancelled'].includes(run.status)) {
      throw new Error(`Run ${runId} reached ${run.status}, expected ${status}`)
    }
    await new Promise(resolveWait => setTimeout(resolveWait, 100))
  }
  throw new Error(`Run ${runId} did not reach ${status}`)
}

async function seedRecoveryApplication() {
  const application = await platformApi('/api/v1/applications', {
    method: 'POST',
    body: JSON.stringify({
      name: `${smokeMarker} customer runtime recovery journey`,
      requirement: `${smokeMarker}: verify required input guidance and exactly-once retry behavior.`,
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
  await mutate('set_metadata', {
    name: `${smokeMarker} 客户退款说明助手`,
    description: '接收退款请求并生成可直接发送给客户的说明。',
  })
  await mutate('add_node', {
    node: {
      id: 'start',
      type: 'start',
      title: '填写退款请求',
      description: '接收本次需要处理的客户退款问题。',
      config: {
        inputs: [{
          name: 'request',
          label: '客户退款请求',
          description: '说明客户遇到的问题和希望得到的处理。',
          type: 'string',
          required: true,
        }],
      },
      position: { x: 80, y: 120 },
    },
  })
  await mutate('add_node', {
    node: {
      id: 'answer',
      type: 'answer',
      title: '客户说明',
      description: '返回可直接使用的客户说明。',
      config: { answer: { $ref: { node_id: 'start', path: ['request'] } } },
      position: { x: 420, y: 120 },
    },
  })
  await mutate('add_edge', {
    edge: { id: 'start_answer', source: 'start', target: 'answer' },
  })
  const failedStart = await platformApi(`/api/v1/applications/${application.id}/runs`, {
    method: 'POST',
    body: JSON.stringify({ inputs: {}, use_draft: true, workspace_path: '.' }),
  })
  const failedRun = await waitForRun(failedStart.run_id, 'failed')
  return { application_id: application.id, revision, failed_run: failedRun }
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
  seeded: null,
  initial_failed_state: null,
  missing_input_state: null,
  retry_samples: [],
  retry_requests: [],
  final_state: null,
  cleanup: null,
  screenshots: [],
  console_errors: [],
  failed_requests: [],
  error: null,
}

let applicationId = ''
let client
let exitCode = 0

try {
  evidence.seeded = await seedRecoveryApplication()
  applicationId = evidence.seeded.application_id
  await waitForDebugger()
  const targetResponse = await fetch(
    `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(`${webBase}/runtime/${applicationId}`)}`,
    { method: 'PUT' },
  )
  const target = await targetResponse.json()
  client = new CdpClient(target.webSocketDebuggerUrl)
  await client.connect()
  await client.send('Page.enable')
  await client.send('Runtime.enable')
  await client.send('Network.enable')
  client.on('Runtime.exceptionThrown', event => {
    evidence.console_errors.push({ type: 'exception', detail: event.exceptionDetails?.exception?.description || event.exceptionDetails?.text || 'unknown' })
  })
  client.on('Console.messageAdded', event => {
    if (event.message?.level === 'error') evidence.console_errors.push({ type: 'console', detail: event.message.text })
  })
  client.on('Network.requestWillBeSent', event => {
    const url = event.request?.url || ''
    if (event.request?.method === 'POST' && url.endsWith(`/api/v1/applications/${applicationId}/runs`)) {
      evidence.retry_requests.push({ request_id: event.requestId, url, at: new Date().toISOString() })
    }
  })
  client.on('Network.responseReceived', event => {
    const url = event.response?.url || ''
    if (event.response?.status >= 400 && !url.endsWith('/favicon.ico')) {
      evidence.failed_requests.push({ status: event.response.status, url })
    }
  })
  client.on('Network.loadingFailed', event => {
    if (!event.canceled && event.errorText !== 'net::ERR_ABORTED') {
      evidence.failed_requests.push({ status: 0, url: event.errorText })
    }
  })

  await client.navigate(`${webBase}/runtime/${applicationId}`)
  await client.waitFor(`document.querySelector('[data-customer-runtime="true"]')?.dataset.runtimeReady === 'true'`, 60000)
  await client.waitFor(`document.querySelector('[data-run-status]')?.dataset.runStatus === 'failed'`, 30000)
  evidence.initial_failed_state = await client.evaluate(`(() => ({
    run_id: document.querySelector('[data-customer-runtime="true"]')?.dataset.runtimeRunId || '',
    status: document.querySelector('[data-run-status]')?.dataset.runStatus || '',
    retry_label: (document.querySelector('[data-customer-runtime-action="retry"]') || [...document.querySelectorAll('button')].find(item => item.innerText.includes('重新运行')))?.innerText || '',
    raw_error_visible: document.querySelector('[data-markdown-surface="customer-runtime-result"]')?.innerText.includes('missing required input') || false,
    empty_result_visible: Boolean([...document.querySelectorAll('span')].find(item => item.innerText === '尚无运行结果')),
    recovery_in_view: (() => {
      const button = document.querySelector('[data-customer-runtime-action="retry"]') || [...document.querySelectorAll('button')].find(item => item.innerText.includes('重新运行'))
      const rect = button?.getBoundingClientRect()
      return Boolean(rect && rect.top >= 0 && rect.bottom <= window.innerHeight)
    })(),
    summary_result: [...document.querySelectorAll('aside dl > div')].find(item => item.querySelector('dt')?.innerText === '结果')?.querySelector('dd')?.innerText || '',
  }))()`)
  evidence.screenshots.push({ id: 'failed-run', ...(await client.screenshot('01-failed-run.png')) })

  await client.evaluate(`(() => {
    const button = document.querySelector('[data-customer-runtime-action="retry"]') || [...document.querySelectorAll('button')].find(item => item.innerText.includes('重新运行'))
    button.click()
    return true
  })()`)
  await client.waitFor(`Boolean(document.querySelector('[role="alert"]'))`)
  evidence.missing_input_state = await client.evaluate(`(() => {
    const field = document.querySelector('[data-runtime-input="request"]')
    const input = field?.querySelector('input, textarea')
    return {
      run_id: document.querySelector('[data-customer-runtime="true"]')?.dataset.runtimeRunId || '',
      alert: document.querySelector('[role="alert"]')?.innerText || '',
      invalid_field: field?.dataset.runtimeInvalid || '',
      aria_invalid: input?.getAttribute('aria-invalid') || '',
      focused: document.activeElement === input,
    }
  })()`)
  evidence.missing_input_state.retry_request_count = evidence.retry_requests.length
  evidence.screenshots.push({ id: 'missing-input', ...(await client.screenshot('02-missing-input.png')) })

  await client.evaluate(`(() => {
    const input = document.querySelector('[data-runtime-input="request"] input')
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    setter.call(input, ${JSON.stringify(recoveryInput)})
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
    return input.value
  })()`)
  await client.waitFor(`!document.querySelector('[role="alert"]')`)
  await client.evaluate(`(() => {
    const sample = () => ({
      at: performance.now(),
      disabled: Boolean((document.querySelector('[data-customer-runtime-action="retry"]') || [...document.querySelectorAll('button')].find(item => item.innerText.includes('重新运行') || item.innerText.includes('正在重新运行')))?.disabled),
      label: (document.querySelector('[data-customer-runtime-action="retry"]') || [...document.querySelectorAll('button')].find(item => item.innerText.includes('重新运行') || item.innerText.includes('正在重新运行')))?.innerText || '',
      run_id: document.querySelector('[data-customer-runtime="true"]')?.dataset.runtimeRunId || '',
      status: document.querySelector('[data-run-status]')?.dataset.runStatus || '',
    })
    window.__liliesRetryProbe = { samples: [sample()] }
    const observer = new MutationObserver(() => window.__liliesRetryProbe.samples.push(sample()))
    observer.observe(document.body, { attributes: true, childList: true, characterData: true, subtree: true })
    window.__liliesRetryProbe.observer = observer
    const button = document.querySelector('[data-customer-runtime-action="retry"]') || [...document.querySelectorAll('button')].find(item => item.innerText.includes('重新运行'))
    button.click()
    button.click()
    return true
  })()`)
  await client.waitFor(`document.querySelector('[data-customer-runtime="true"]')?.dataset.runtimeRunId !== ${JSON.stringify(evidence.seeded.failed_run.id)}`, 30000, 20)
  await client.waitFor(`document.querySelector('[data-run-status]')?.dataset.runStatus === 'succeeded'`, 30000)
  await new Promise(resolveWait => setTimeout(resolveWait, 500))
  evidence.retry_samples = await client.evaluate(`(() => {
    window.__liliesRetryProbe.observer.disconnect()
    const unique = []
    for (const sample of window.__liliesRetryProbe.samples) {
      const key = JSON.stringify(sample)
      if (unique.at(-1)?.key !== key) unique.push({ ...sample, key })
    }
    return unique.map(({ key, ...sample }) => sample)
  })()`)
  evidence.final_state = await client.evaluate(`(() => ({
    run_id: document.querySelector('[data-customer-runtime="true"]')?.dataset.runtimeRunId || '',
    status: document.querySelector('[data-run-status]')?.dataset.runStatus || '',
    result: document.querySelector('[data-markdown-surface="customer-runtime-result"]')?.innerText?.trim() || '',
    completed_steps: document.querySelectorAll('[data-step-status="completed"], [data-step-status="skipped"]').length,
    total_steps: document.querySelectorAll('[data-step-status]').length,
  }))()`)
  evidence.screenshots.push({ id: 'recovered-run', ...(await client.screenshot('03-recovered-run.png')) })

  if (!evidence.missing_input_state.focused || evidence.missing_input_state.aria_invalid !== 'true') {
    throw new Error(`Required input was not focused and marked invalid: ${JSON.stringify(evidence.missing_input_state)}`)
  }
  if (/RuntimeInputError|Error:/.test(evidence.missing_input_state.alert)) {
    throw new Error(`Required input guidance leaked an internal error type: ${evidence.missing_input_state.alert}`)
  }
  if (evidence.initial_failed_state.raw_error_visible) {
    throw new Error('Customer Runtime exposed the internal missing-input error as a result')
  }
  if (evidence.initial_failed_state.empty_result_visible || !evidence.initial_failed_state.recovery_in_view || evidence.initial_failed_state.summary_result !== '未生成') {
    throw new Error(`Failed run did not prioritize recovery guidance: ${JSON.stringify(evidence.initial_failed_state)}`)
  }
  if (evidence.missing_input_state.retry_request_count !== 0) {
    throw new Error(`Empty retry unexpectedly created a run: ${JSON.stringify(evidence.retry_requests)}`)
  }
  if (!evidence.retry_samples.some(item => item.disabled && item.label.includes('正在重新运行'))) {
    throw new Error(`Retry did not expose a disabled loading state: ${JSON.stringify(evidence.retry_samples)}`)
  }
  if (evidence.retry_requests.length !== 1) {
    throw new Error(`Double-click created ${evidence.retry_requests.length} runs instead of one`)
  }
  if (evidence.final_state.status !== 'succeeded' || !evidence.final_state.result.includes(recoveryInput)) {
    throw new Error(`Recovered run did not return the supplied input: ${JSON.stringify(evidence.final_state)}`)
  }
  if (evidence.console_errors.length) throw new Error(`Browser console errors: ${JSON.stringify(evidence.console_errors)}`)
  if (evidence.failed_requests.length) throw new Error(`Failed browser requests: ${JSON.stringify(evidence.failed_requests)}`)
} catch (error) {
  exitCode = 1
  evidence.error = error instanceof Error ? error.stack || error.message : String(error)
  if (client) {
    try {
      evidence.screenshots.push({ id: 'failure', ...(await client.screenshot('99-failure.png')) })
    } catch {
      // Preserve the original failure when screenshot capture is unavailable.
    }
  }
} finally {
  if (applicationId) {
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
  evidence.chrome_output_tail = chromeOutput.join('').split('\n').filter(Boolean).slice(-20)
  const outputPath = resolve(outputDir, 'journey.json')
  writeFileSync(outputPath, `${JSON.stringify(evidence, null, 2)}\n`)
  console.log(JSON.stringify({
    output: outputPath,
    error: evidence.error,
    initial_failed_state: evidence.initial_failed_state,
    missing_input_state: evidence.missing_input_state,
    retry_samples: evidence.retry_samples,
    retry_request_count: evidence.retry_requests.length,
    final_state: evidence.final_state,
    cleanup: evidence.cleanup,
  }, null, 2))
  try {
    client?.socket?.close()
  } catch {
    // Browser teardown is best effort.
  }
  chrome.kill('SIGTERM')
}

process.exitCode = exitCode
