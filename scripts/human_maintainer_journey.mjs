#!/usr/bin/env node

import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const args = new Map()
for (let index = 2; index < process.argv.length; index += 2) {
  args.set(process.argv[index], process.argv[index + 1])
}

const applicationId = args.get('--application-id') || ''
if (!applicationId) throw new Error('--application-id is required')
const webBase = args.get('--web') || 'http://127.0.0.1:3000'
const debugPort = Number(args.get('--debug-port') || 19242)
const outputDir = resolve(args.get('--output') || '.tmp/human-maintainer-journey')
const chromePath = args.get('--chrome') || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const profileDir = resolve('.tmp/human-maintainer-journey/chrome-profile')

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
  '--window-size=1440,960',
  'about:blank',
], { stdio: ['ignore', 'pipe', 'pipe'] })
chrome.stdout.on('data', chunk => chromeOutput.push(String(chunk)))
chrome.stderr.on('data', chunk => chromeOutput.push(String(chunk)))

const evidence = {
  schema_version: '1.0',
  started_at: new Date().toISOString(),
  application_id: applicationId,
  node_checks: [],
  arrange: null,
  keyboard_pan: null,
  patch_preview: null,
  automation: null,
  integrations: null,
  governance_automation: null,
  home_application: null,
  readable_summary: null,
  console_errors: [],
  failed_requests: [],
  ignored_requests: [],
  screenshots: [],
  error: null,
}

let client
let exitCode = 0

try {
  await waitForDebugger()
  const targetResponse = await fetch(
    `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(`${webBase}/applications/${applicationId}?tab=edit`)}`,
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

  await client.navigate(`${webBase}/applications/${applicationId}?tab=edit`)
  await client.waitFor(`Boolean(document.querySelector('[data-detail-tab-url-state="synced"]'))`, 60000)
  await client.waitFor(`document.querySelectorAll('.react-flow__node').length > 0`, 60000)
  evidence.readable_summary = await client.evaluate(`(() => ({
    purpose: document.querySelector('[data-workflow-readable-purpose="true"]')?.innerText || '',
    steps: [...document.querySelectorAll('.workflow-readable-steps article')].map(item => ({
      title: item.querySelector('strong')?.innerText || '',
      detail: item.querySelector('small')?.innerText || '',
    })),
  }))()`)
  const nodeCount = await client.evaluate(`document.querySelectorAll('.react-flow__node').length`)
  for (let index = 0; index < nodeCount; index += 1) {
    const node = await client.evaluate(`(() => {
      const element = document.querySelectorAll('.react-flow__node')[${index}]
      element.click()
      return { id: element.getAttribute('data-id') || '', text: element.innerText }
    })()`)
    await client.waitFor(`document.querySelector('[data-node-inspector="selection-summary"]') !== null`)
    const inspector = await client.evaluate(`(() => ({
      state: document.querySelector('[data-node-inspector="selection-summary"]')?.dataset.nodeInspector || '',
      summary: document.querySelector('[data-node-inspector="selection-summary"]')?.innerText || '',
      selected: document.querySelectorAll('.brick-node.selected').length,
    }))()`)
    evidence.node_checks.push({ ...node, ...inspector })
  }

  await client.evaluate(`(() => {
    const element = document.querySelector('.react-flow__node')
    element.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, button: 2 }))
    return true
  })()`)
  await client.waitFor(`Number(document.querySelector('[data-workflow-edit-dialog]')?.dataset.workflowEditReferenceCount || 0) > 0`)

  const positionsBefore = await client.evaluate(`[...document.querySelectorAll('.react-flow__node')].map(node => node.style.transform)`)
  await client.click('[data-canvas-action="arrange"]')
  await client.waitFor(`!document.querySelector('[data-canvas-action="arrange"]').disabled`, 60000)
  const positionsAfter = await client.evaluate(`[...document.querySelectorAll('.react-flow__node')].map(node => node.style.transform)`)
  evidence.arrange = {
    node_count: nodeCount,
    changed_count: positionsAfter.filter((value, index) => value !== positionsBefore[index]).length,
    completed: true,
  }

  const viewportBefore = await client.evaluate(`document.querySelector('.react-flow__viewport')?.style.transform || ''`)
  await client.evaluate(`(() => {
    const canvas = document.querySelector('[data-canvas-keyboard="wasd-pan"]')
    canvas.focus()
    canvas.dispatchEvent(new KeyboardEvent('keydown', { key: 'd', bubbles: true, cancelable: true }))
    return true
  })()`)
  await client.waitFor(`document.querySelector('.react-flow__viewport')?.style.transform !== ${JSON.stringify(viewportBefore)}`)
  const viewportAfter = await client.evaluate(`document.querySelector('.react-flow__viewport')?.style.transform || ''`)
  evidence.keyboard_pan = { key: 'd', before: viewportBefore, after: viewportAfter, changed: viewportAfter !== viewportBefore }

  await client.fill('[data-workflow-edit-input="instruction"]', '把“问题类型分类”积木标题改为“客诉类别分类”，并把工作流描述更新为面向客服主管的投诉分流和回复建议流程。')
  await client.click('.workflow-edit-dialog .run-actions button:first-child')
  await client.waitFor(`document.querySelector('.patch-result') !== null`, 60000)
  evidence.patch_preview = await client.evaluate(`(() => ({
    supported: document.querySelector('.patch-result')?.classList.contains('supported') || false,
    text: document.querySelector('.patch-result')?.innerText || '',
    operations: document.querySelector('.patch-result pre')?.innerText || '',
    reference_count: Number(document.querySelector('[data-workflow-edit-dialog]')?.dataset.workflowEditReferenceCount || 0),
  }))()`)
  try {
    evidence.patch_preview.parsed_operations = JSON.parse(evidence.patch_preview.operations)
  } catch (error) {
    throw new Error(`Workflow edit preview operations were not valid JSON: ${error}`)
  }

  evidence.screenshots.push({ id: 'maintainer-studio', ...(await client.screenshot('maintainer-studio.png')) })
  if (evidence.node_checks.some(item => item.state !== 'selection-summary' || item.selected !== 1)) {
    throw new Error(`A canvas node did not open a stable inspector: ${JSON.stringify(evidence.node_checks)}`)
  }
  if (!evidence.keyboard_pan.changed) throw new Error('WASD canvas pan did not change the viewport')
  if (!evidence.readable_summary.purpose || /#\s|工作流搭建方案/.test(evidence.readable_summary.purpose)) {
    throw new Error(`Workflow purpose was not a concise natural-language summary: ${JSON.stringify(evidence.readable_summary)}`)
  }
  if (!evidence.readable_summary.steps.length || evidence.readable_summary.steps.some(item => /->|[a-z]+_[a-z]+/.test(item.detail))) {
    throw new Error(`Workflow steps exposed engineering identifiers: ${JSON.stringify(evidence.readable_summary.steps)}`)
  }
  if (!evidence.patch_preview.supported) throw new Error(`Workflow edit preview was unsupported: ${evidence.patch_preview.text}`)
  if (!evidence.patch_preview.operations) throw new Error('Workflow edit preview returned no operations')
  const patchOperations = evidence.patch_preview.parsed_operations
  const renameOperation = patchOperations.find(operation => (
    operation.op === 'update_node'
    && operation.data?.changes?.title === '客诉类别分类'
  ))
  if (!renameOperation) {
    throw new Error(`Workflow edit preview did not rename the requested node: ${evidence.patch_preview.operations}`)
  }
  const descriptionOperation = patchOperations.find(operation => (
    operation.op === 'set_metadata'
    && operation.data?.description === '面向客服主管的投诉分流和回复建议流程'
  ))
  if (!descriptionOperation) {
    throw new Error(`Workflow edit preview did not update the workflow description precisely: ${evidence.patch_preview.operations}`)
  }
  if (patchOperations.some(operation => Object.hasOwn(operation.data || {}, 'requirement'))) {
    throw new Error(`Workflow edit preview unexpectedly overwrote the workflow requirement: ${evidence.patch_preview.operations}`)
  }
  await client.navigate(`${webBase}/applications/${applicationId}?tab=automation`)
  await client.waitFor(`Boolean(document.querySelector('[data-detail-tab-url-state="synced"]'))`, 60000)
  await client.waitFor(`Boolean(document.querySelector('[data-automation-state]'))`, 60000)
  evidence.automation = await client.evaluate(`(() => ({
    state: document.querySelector('[data-automation-state]')?.dataset.automationState || '',
    text: document.querySelector('[data-automation-state]')?.innerText || '',
  }))()`)
  evidence.screenshots.push({ id: 'automation', ...(await client.screenshot('automation.png')) })
  if (evidence.automation.state !== 'not-configured') {
    throw new Error(`Ordinary workflow automation state was misleading: ${JSON.stringify(evidence.automation)}`)
  }
  await client.navigate(`${webBase}/applications/${applicationId}?tab=integrations`)
  await client.waitFor(`Boolean(document.querySelector('[data-detail-tab-url-state="synced"]'))`, 60000)
  await client.waitFor(`Boolean(document.querySelector('[data-engineer-connector-workspace="true"]'))`, 60000)
  await client.waitFor(`document.querySelector('[data-engineer-connector-workspace="true"]')?.dataset.connectorState !== 'loading'`, 60000)
  evidence.integrations = await client.evaluate(`(() => ({
    state: document.querySelector('[data-engineer-connector-workspace="true"]')?.dataset.connectorState || '',
    text: document.querySelector('[data-engineer-connector-workspace="true"]')?.innerText || '',
    alerts: [...document.querySelectorAll('[data-engineer-connector-workspace="true"] [role="alert"]')].map(item => item.innerText),
  }))()`)
  evidence.screenshots.push({ id: 'integrations', ...(await client.screenshot('integrations.png')) })
  if (evidence.integrations.state !== 'not-configured' || evidence.integrations.alerts.length) {
    throw new Error(`Empty connector workspace was not a clean onboarding state: ${JSON.stringify(evidence.integrations)}`)
  }
  await client.navigate(`${webBase}/governance?application_id=${applicationId}`)
  await client.waitFor(`Boolean(document.querySelector('[data-governance-console="true"]'))`, 60000)
  await client.click('[data-governance-tab="Durable Jobs"]')
  await client.waitFor(`Boolean(document.querySelector('[data-automation-state]') && document.querySelector('[data-automation-state]')?.dataset.automationState !== 'loading')`, 60000)
  evidence.governance_automation = await client.evaluate(`(() => ({
    state: document.querySelector('[data-automation-state]')?.dataset.automationState || '',
    text: document.querySelector('[data-automation-state]')?.innerText || '',
  }))()`)
  evidence.screenshots.push({ id: 'governance-automation', ...(await client.screenshot('governance-automation.png')) })
  if (evidence.governance_automation.state !== 'not-configured') {
    throw new Error(`Governance misclassified an ordinary workflow schedule: ${JSON.stringify(evidence.governance_automation)}`)
  }
  await client.navigate(webBase)
  await client.waitFor(`Boolean(document.querySelector('[data-app-list-url-state="synced"]'))`, 60000)
  await client.waitFor(`Boolean(document.querySelector('a[href="/applications/${applicationId}"]'))`, 60000)
  evidence.home_application = await client.evaluate(`(() => {
    const link = document.querySelector('a[href="/applications/${applicationId}"]')
    const card = link?.closest('.app-card')
    return {
      found: Boolean(card),
      name: card?.querySelector('h3')?.innerText || '',
      description: card?.querySelector('p')?.innerText || '',
      text: card?.innerText || '',
    }
  })()`)
  evidence.screenshots.push({ id: 'home-application', ...(await client.screenshot('home-application.png')) })
  if (!evidence.home_application.found || evidence.home_application.description.includes('# 工作流搭建方案')) {
    throw new Error(`Persisted application card was missing or unreadable: ${JSON.stringify(evidence.home_application)}`)
  }
  if (evidence.console_errors.length) throw new Error(`Browser console errors: ${JSON.stringify(evidence.console_errors)}`)
  if (evidence.failed_requests.length) throw new Error(`Failed browser requests: ${JSON.stringify(evidence.failed_requests)}`)
} catch (error) {
  exitCode = 1
  evidence.error = error instanceof Error ? error.stack || error.message : String(error)
} finally {
  evidence.finished_at = new Date().toISOString()
  evidence.chrome_output = chromeOutput.join('').slice(-4000)
  const outputPath = resolve(outputDir, 'journey.json')
  writeFileSync(outputPath, JSON.stringify(evidence, null, 2))
  console.log(JSON.stringify({ output: outputPath, error: evidence.error, nodes: evidence.node_checks.length, patch: evidence.patch_preview }, null, 2))
  try {
    client?.socket?.close()
  } catch {
    // Browser teardown is best effort.
  }
  chrome.kill('SIGTERM')
}

process.exitCode = exitCode
