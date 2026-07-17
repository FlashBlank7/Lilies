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
const debugPort = Number(args.get('--debug-port') || 19240)
const outputDir = resolve(args.get('--output') || '.tmp/human-customer-journey')
const chromePath = args.get('--chrome') || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const requirement = args.get('--requirement') || 'Build a workflow for a customer support lead. It accepts one complaint, classifies urgency and issue type, and returns a concise reply suggestion with reasons and a next action. It must not call external systems.'
const existingApplicationId = args.get('--application-id') || ''
const runtimeInput = args.get('--runtime-input') || '我收到的商品已经破损，而且客服两天没有回复，请尽快给出处理建议。'
const expectReadableStructuredResult = args.get('--expect-readable-structured-result') === 'true'
const viewportWidth = Number(args.get('--viewport-width') || 1440)
const viewportHeight = Number(args.get('--viewport-height') || 960)
const profileDir = resolve('.tmp/human-customer-journey/chrome-profile')

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

  async waitFor(expression, timeoutMs = 30000) {
    const deadline = Date.now() + timeoutMs
    let lastError = null
    while (Date.now() < deadline) {
      try {
        if (await this.evaluate(expression)) return
      } catch (error) {
        lastError = error
      }
      await new Promise(resolveWait => setTimeout(resolveWait, 200))
    }
    throw new Error(`Timed out waiting for ${expression}${lastError ? `: ${lastError}` : ''}`)
  }

  async click(selector) {
    const encoded = JSON.stringify(selector)
    await this.waitFor(`Boolean(document.querySelector(${encoded}) && !document.querySelector(${encoded}).disabled)`)
    await this.evaluate(`document.querySelector(${encoded}).click(); true`)
  }

  async fill(selector, value) {
    const encodedSelector = JSON.stringify(selector)
    const encodedValue = JSON.stringify(value)
    await this.waitFor(`Boolean(document.querySelector(${encodedSelector}))`)
    return this.evaluate(`(() => {
      const element = document.querySelector(${encodedSelector})
      const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
      Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, ${encodedValue})
      element.dispatchEvent(new Event('input', { bubbles: true }))
      element.dispatchEvent(new Event('change', { bubbles: true }))
      return element.value
    })()`)
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

const chromeOutput = []
const chrome = spawn(chromePath, [
  '--headless=new',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  `--remote-debugging-port=${debugPort}`,
  `--user-data-dir=${profileDir}`,
  `--window-size=${viewportWidth},${viewportHeight}`,
  'about:blank',
], { stdio: ['ignore', 'pipe', 'pipe'] })
chrome.stdout.on('data', chunk => chromeOutput.push(String(chunk)))
chrome.stderr.on('data', chunk => chromeOutput.push(String(chunk)))

const evidence = {
  schema_version: '1.0',
  started_at: new Date().toISOString(),
  web_base: webBase,
  requirement,
  journey_mode: existingApplicationId ? 'existing-application-runtime' : 'requirement-to-runtime',
  intake_rounds: [],
  application: null,
  runtime: null,
  screenshots: [],
  console_errors: [],
  failed_requests: [],
  ignored_requests: [],
  viewport: null,
  error: null,
}

let client
let exitCode = 0

async function submitIntake(previousTaskId = '') {
  await client.click('.requirement-completion-actions button:nth-of-type(2)')
  const encodedPrevious = JSON.stringify(previousTaskId)
  await client.waitFor(`(() => {
    const summary = document.querySelector('[data-requirement-intake-task]')
    const error = document.querySelector('.error-banner')?.innerText?.trim()
    return Boolean(error || (summary && summary.dataset.requirementIntakeTask !== ${encodedPrevious}))
  })()`, 180000)
  const result = await client.evaluate(`(() => {
    const panel = document.querySelector('[data-requirement-completion="ai-workflow-intake"]')
    const summary = document.querySelector('[data-requirement-intake-task]')
    return {
      status: panel?.dataset.requirementIntakeStatus || '',
      task_id: summary?.dataset.requirementIntakeTask || '',
      question_count: document.querySelectorAll('.requirement-question-card').length,
      plan_length: document.querySelector('[data-requirement-completion-plan="workflow-requirement"] pre')?.innerText.length || 0,
      error_text: document.querySelector('.error-banner')?.innerText || '',
    }
  })()`)
  if (result.error_text) throw new Error(`Requirement intake failed: ${result.error_text}`)
  return result
}

async function chooseRecommendedOptions() {
  const questionCount = await client.evaluate(`document.querySelectorAll('.requirement-question-card').length`)
  const selections = []
  for (let index = 0; index < questionCount; index += 1) {
    const selection = await client.evaluate(`(() => {
      const question = document.querySelectorAll('.requirement-question-card')[${index}]
      const labels = [...question.querySelectorAll('label.requirement-option-card')]
      const selected = labels.find(label => label.querySelector('i')) || labels[0]
      const input = selected?.querySelector('input')
      if (!input) return null
      input.click()
      return {
        question: question.querySelector('.requirement-question-head span')?.innerText || '',
        option: selected.querySelector('b')?.innerText || '',
        type: input.type,
      }
    })()`)
    if (!selection) continue
    await client.waitFor(`document.querySelectorAll('.requirement-question-card')[${index}].querySelector('input:checked') !== null`)
    selections.push(selection)
  }
  return selections
}

async function fillRuntimeField(index, value) {
  const encodedValue = JSON.stringify(value)
  return client.evaluate(`(() => {
    const field = document.querySelectorAll('[data-runtime-input]')[${index}]
    const element = field?.querySelector('input, textarea')
    if (!element) return null
    if (element instanceof HTMLInputElement && element.type === 'checkbox') {
      if (!element.checked) element.click()
      return { name: field.dataset.runtimeInput || '', type: 'checkbox', value: element.checked }
    }
    const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, ${encodedValue})
    element.dispatchEvent(new Event('input', { bubbles: true }))
    element.dispatchEvent(new Event('change', { bubbles: true }))
    return { name: field.dataset.runtimeInput || '', type: element.type || element.tagName.toLowerCase(), value: element.value }
  })()`)
}

try {
  await waitForDebugger()
  const targetResponse = await fetch(
    `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(`${webBase}/`)}`,
    { method: 'PUT' },
  )
  const target = await targetResponse.json()
  client = new CdpClient(target.webSocketDebuggerUrl)
  await client.connect()
  await client.send('Page.enable')
  await client.send('Runtime.enable')
  await client.send('Network.enable')
  await client.send('Emulation.setDeviceMetricsOverride', {
    width: viewportWidth,
    height: viewportHeight,
    deviceScaleFactor: 1,
    mobile: viewportWidth < 768,
  })
  client.on('Runtime.exceptionThrown', event => {
    evidence.console_errors.push({ type: 'exception', detail: event.exceptionDetails?.exception?.description || event.exceptionDetails?.text || 'unknown' })
  })
  client.on('Console.messageAdded', event => {
    if (event.message?.level === 'error') evidence.console_errors.push({ type: 'console', detail: event.message.text })
  })
  client.on('Network.responseReceived', event => {
    if (event.response?.status < 400) return
    const item = { status: event.response.status, url: event.response.url }
    if (new URL(event.response.url).pathname === '/favicon.ico') evidence.ignored_requests.push(item)
    else evidence.failed_requests.push(item)
  })
  client.on('Network.loadingFailed', event => {
    const item = { status: 0, url: event.errorText }
    if (event.canceled || event.errorText === 'net::ERR_ABORTED') evidence.ignored_requests.push(item)
    else evidence.failed_requests.push(item)
  })

  let applicationState
  if (existingApplicationId) {
    applicationState = {
      id: existingApplicationId,
      url: `${webBase}/applications/${existingApplicationId}`,
      build_id: null,
      safe_draft: false,
      build_status: 'existing',
    }
    evidence.application = applicationState
  } else {
    await client.navigate(`${webBase}/`)
    await client.waitFor(`document.querySelector('[data-runtime-status]')?.dataset.runtimeStatus === 'connected'`, 30000)
    await client.fill('form.create-card > textarea', requirement)
    evidence.screenshots.push({ id: 'requirement-entered', ...(await client.screenshot('01-requirement-entered.png')) })

    let intake = await submitIntake()
    evidence.intake_rounds.push({ ...intake, selections: [] })
    for (let round = 0; intake.status === 'needs_input' && round < 3; round += 1) {
      const selections = await chooseRecommendedOptions()
      evidence.intake_rounds.at(-1).selections = selections
      await client.waitFor(`!document.querySelector('.requirement-completion-actions button:nth-of-type(2)').disabled`)
      intake = await submitIntake(intake.task_id)
      evidence.intake_rounds.push({ ...intake, selections: [] })
    }
    if (intake.status !== 'ready') throw new Error(`Requirement intake did not become ready: ${intake.status}`)
    if (intake.plan_length < 80) throw new Error(`Requirement intake plan is unexpectedly short: ${intake.plan_length}`)

    evidence.screenshots.push({ id: 'requirement-ready', ...(await client.screenshot('02-requirement-ready.png')) })
    await client.click('.requirement-completion-actions button:nth-of-type(3)')
    await client.waitFor(`document.querySelector('[data-build-action="home-start-builder-team"]')?.dataset.buildIntent === 'confirmed'`)
    await client.click('[data-build-action="home-start-builder-team"]')
    await client.waitFor(`location.pathname.startsWith('/applications/')`, 180000)
    await client.waitFor(`Boolean(document.querySelector('[data-detail-tab-url-state="synced"]'))`, 60000)

    applicationState = await client.evaluate(`(() => ({
      id: location.pathname.split('/').filter(Boolean).at(-1),
      url: location.href,
      build_id: new URL(location.href).searchParams.get('build'),
      safe_draft: new URL(location.href).searchParams.get('safeDraft') === '1',
    }))()`)
    evidence.application = applicationState
    if (applicationState.build_id) {
      await client.waitFor(`(() => {
        const value = document.querySelector('.build-status b')?.innerText?.trim().toLowerCase()
        return ['published', 'ready', 'failed', 'cancelled'].includes(value)
      })()`, 600000)
      Object.assign(evidence.application, await client.evaluate(`(() => ({
        build_status: document.querySelector('.build-status b')?.innerText?.trim().toLowerCase() || '',
        build_detail: document.querySelector('.build-status')?.innerText || '',
        node_count: document.querySelectorAll('.react-flow__node').length,
        page_error: document.querySelector('.error')?.innerText || '',
      }))()`))
    } else {
      Object.assign(evidence.application, await client.evaluate(`(() => ({
        build_status: 'safe_draft',
        node_count: document.querySelectorAll('.react-flow__node').length,
        page_error: document.querySelector('.error')?.innerText || '',
      }))()`))
    }
    evidence.screenshots.push({ id: 'application-loaded', ...(await client.screenshot('03-application-loaded.png')) })
  }

  await client.navigate(`${webBase}/runtime/${applicationState.id}`)
  await client.waitFor(`Boolean(document.querySelector('[data-customer-runtime="true"]'))`, 60000)
  const runtimeReady = `(() => {
    const root = document.querySelector('[data-customer-runtime="true"]')
    const purpose = document.querySelector('[data-runtime-purpose="true"]')?.innerText?.trim()
    return root?.dataset.runtimeLoading === 'false' && root?.dataset.runtimeReady === 'true' && Boolean(purpose)
  })()`
  await client.waitFor(runtimeReady, 60000)
  await new Promise(resolveWait => setTimeout(resolveWait, 750))
  await client.waitFor(runtimeReady, 60000)
  const initialRuntimeState = await client.evaluate(`(() => ({
    status: document.querySelector('[data-run-status]')?.dataset.runStatus || '',
    input_count: document.querySelectorAll('[data-runtime-input]').length,
    start_disabled: Boolean(document.querySelector('[data-customer-runtime-action="start"]')?.disabled),
    error_text: document.querySelector('[role="alert"]')?.innerText || document.querySelector('.error')?.innerText || '',
    title: document.querySelector('h1')?.innerText || '',
    purpose: document.querySelector('[data-runtime-purpose="true"]')?.innerText?.trim() || '',
    run_id: document.querySelector('[data-customer-runtime="true"]')?.dataset.runtimeRunId || '',
  }))()`)
  evidence.viewport = await client.evaluate(`(() => {
    const start = document.querySelector('[data-customer-runtime-action="start"]')?.getBoundingClientRect()
    return {
      width: window.innerWidth,
      height: window.innerHeight,
      document_width: document.documentElement.scrollWidth,
      horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      start_button_in_view: Boolean(start && start.left >= 0 && start.right <= window.innerWidth && start.top >= 0 && start.top < window.innerHeight),
    }
  })()`)
  evidence.runtime = { initial: initialRuntimeState, filled_inputs: [], final: null }
  evidence.screenshots.push({ id: 'customer-runtime', ...(await client.screenshot('04-customer-runtime.png')) })

  if (evidence.application.build_status === 'failed') throw new Error(`Builder failed: ${evidence.application.build_detail}`)
  if (!initialRuntimeState.purpose) throw new Error('Customer Runtime did not render a workflow purpose')
  if (initialRuntimeState.error_text) throw new Error(`Customer Runtime rendered an error: ${initialRuntimeState.error_text}`)
  if (evidence.viewport.horizontal_overflow || !evidence.viewport.start_button_in_view) {
    throw new Error(`Customer Runtime viewport is not usable: ${JSON.stringify(evidence.viewport)}`)
  }

  const runtimeFields = await client.evaluate(`[...document.querySelectorAll('[data-runtime-input]')].map((field, index) => {
    const element = field.querySelector('input, textarea')
    return { index, name: field.dataset.runtimeInput || '', type: element?.type || element?.tagName?.toLowerCase() || '' }
  })`)
  for (const field of runtimeFields) {
    const value = field.type === 'number' ? '1' : field.type === 'checkbox' ? 'true' : field.type === 'textarea' && /json|array|list/i.test(field.name) ? '[]' : runtimeInput
    const filled = await fillRuntimeField(field.index, value)
    if (filled) evidence.runtime.filled_inputs.push(filled)
  }
  await new Promise(resolveWait => setTimeout(resolveWait, 250))
  const beforeRunId = await client.evaluate(`document.querySelector('[data-customer-runtime="true"]')?.dataset.runtimeRunId || ''`)
  await client.click('[data-customer-runtime-action="start"]')
  await client.waitFor(`(() => {
    const runId = document.querySelector('[data-customer-runtime="true"]')?.dataset.runtimeRunId || ''
    return Boolean(runId && runId !== ${JSON.stringify(beforeRunId)})
  })()`, 30000)
  await client.waitFor(`['succeeded', 'failed', 'paused', 'cancelled'].includes(document.querySelector('[data-run-status]')?.dataset.runStatus || '')`, 240000)
  const finalRuntimeState = await client.evaluate(`(() => ({
    status: document.querySelector('[data-run-status]')?.dataset.runStatus || '',
    run_id: document.querySelector('[data-customer-runtime="true"]')?.dataset.runtimeRunId || '',
    result_text: document.querySelector('[data-markdown-surface="customer-runtime-result"]')?.innerText?.trim() || '',
    completed_steps: document.querySelectorAll('[data-step-status="completed"], [data-step-status="skipped"]').length,
    total_steps: document.querySelectorAll('[data-step-status]').length,
    error_text: document.querySelector('[role="alert"]')?.innerText || document.querySelector('.error')?.innerText || '',
  }))()`)
  evidence.runtime.final = finalRuntimeState
  await client.evaluate(`(() => {
    document.querySelector('[data-markdown-surface="customer-runtime-result"]')?.scrollIntoView({ block: 'start' })
    return true
  })()`)
  await new Promise(resolveWait => setTimeout(resolveWait, 400))
  evidence.runtime.final_viewport = await client.evaluate(`(() => {
    const result = document.querySelector('[data-markdown-surface="customer-runtime-result"]')?.getBoundingClientRect()
    return {
      horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      result_in_width: Boolean(result && result.left >= 0 && result.right <= window.innerWidth),
      result_in_view: Boolean(result && result.top >= 0 && result.top < window.innerHeight),
    }
  })()`)
  evidence.screenshots.push({ id: 'runtime-finished', ...(await client.screenshot('05-runtime-finished.png')) })

  if (finalRuntimeState.status !== 'succeeded') throw new Error(`Customer Runtime finished with ${finalRuntimeState.status}: ${finalRuntimeState.error_text}`)
  if (!finalRuntimeState.result_text) throw new Error('Customer Runtime succeeded without a readable result')
  if (expectReadableStructuredResult && (/"classification"\s*:/.test(finalRuntimeState.result_text) || !finalRuntimeState.result_text.includes('分类结果'))) {
    throw new Error(`Customer Runtime exposed serialized JSON instead of readable sections: ${finalRuntimeState.result_text}`)
  }
  if (finalRuntimeState.completed_steps !== finalRuntimeState.total_steps) throw new Error(`Customer Runtime progress is incomplete: ${finalRuntimeState.completed_steps}/${finalRuntimeState.total_steps}`)
  if (evidence.runtime.final_viewport.horizontal_overflow || !evidence.runtime.final_viewport.result_in_width || !evidence.runtime.final_viewport.result_in_view) {
    throw new Error(`Customer Runtime result is not usable in the viewport: ${JSON.stringify(evidence.runtime.final_viewport)}`)
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
  evidence.finished_at = new Date().toISOString()
  evidence.chrome_output_tail = chromeOutput.join('').split('\n').filter(Boolean).slice(-20)
  const outputPath = resolve(outputDir, 'journey.json')
  writeFileSync(outputPath, `${JSON.stringify(evidence, null, 2)}\n`)
  console.log(JSON.stringify({ output: outputPath, error: evidence.error, application: evidence.application, runtime: evidence.runtime }, null, 2))
  chrome.kill('SIGTERM')
  await new Promise(resolveExit => setTimeout(resolveExit, 300))
  if (chrome.exitCode === null) chrome.kill('SIGKILL')
}

process.exitCode = exitCode
