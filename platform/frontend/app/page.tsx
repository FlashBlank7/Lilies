'use client'

import Link from 'next/link'
import { FormEvent, useEffect, useState } from 'react'
import { api, clearClientToken, getClientToken, idempotency, isAuthError, saveClientToken } from '@/lib/platform'
import { defaultLocale, isLocale, messages, nextLocale, type Locale } from '@/lib/i18n'

type Application = {
  id: string
  name: string
  description: string
  mode: string
  active_version?: number | null
  draft_revision: number
  content_hash: string
  tested_hash?: string | null
}

type DraftMutationResult = {
  revision: number
}

function deriveApplicationName(requirement: string) {
  const text = requirement.trim().replace(/\s+/g, ' ')
  if (!text) return '新智能体'
  const first = text.split(/[。.!?\n\r]/)[0].replace(/^[\s"',，,：:；;“”‘’`]+|[\s"',，,：:；;“”‘’`]+$/g, '')
  const cleaned = first
    .replace(/^(请|请帮我|我需要|我想要|帮我|帮我做|搭建|创建|制作|生成|构建|设计)(一个|一款|一个可以|可以|能够|能)?/, '')
    .replace(/^(please|build|create|make|generate|design)\s+(a|an|the)?\s*/i, '')
    .replace(/^[\s，,：:；;]+|[\s，,：:；;]+$/g, '')
  return (cleaned || first || text).slice(0, 32).replace(/[\s，,：:；;]+$/g, '') || '新智能体'
}

async function applyDraftOperation(applicationId: string, expectedRevision: number, op: string, data: Record<string, unknown>) {
  const result = await api<DraftMutationResult>(`/api/v1/applications/${applicationId}/draft`, {
    method: 'POST',
    body: JSON.stringify({ expected_revision: expectedRevision, idempotency_key: idempotency(), op, data }),
  })
  return result.revision
}

async function seedSafeDraftSkeleton(applicationId: string, initialRevision: number) {
  const suffix = Date.now()
  const startId = `safe_start_${suffix}`
  const answerId = `safe_answer_${suffix}`
  const testId = `safe_acceptance_${suffix}`
  let revision = initialRevision
  revision = await applyDraftOperation(applicationId, revision, 'add_node', { node: {
    id: startId, type: 'start', block_version: 1, title: 'Customer Request',
    description: 'Safe draft input created without starting the builder team.',
    config: { inputs: [{ name: 'customer_request', label: 'Customer request', type: 'string', required: true }] },
    position: { x: 120, y: 160 },
    retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
  } })
  revision = await applyDraftOperation(applicationId, revision, 'add_node', { node: {
    id: answerId, type: 'answer', block_version: 1, title: 'Draft Answer',
    description: 'Starter output placeholder; replace this after the builder team or manual editing.',
    config: { answer: { $ref: { node_id: startId, path: ['output', 'customer_request'] } } },
    position: { x: 420, y: 160 },
    retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
  } })
  revision = await applyDraftOperation(applicationId, revision, 'add_edge', { edge: {
    id: `safe_edge_${suffix}`, source: startId, target: answerId, source_port: 'output', target_port: 'input',
  } })
  revision = await applyDraftOperation(applicationId, revision, 'add_test', { test: {
    id: testId,
    name: 'Starter structure check',
    requirement: 'Safe draft contains an editable Start to Answer skeleton before any model build.',
    inputs: { customer_request: 'Summarize a customer request and identify the next owner.' },
    assertions: [],
    required_node_types: ['start', 'answer'],
    required_tool_nodes: [],
    required_tools: [],
    minimum_tool_calls: 0,
    mandatory: true,
    structural_only: true,
    feedback_hints: ['Start the builder team or edit the nodes manually to replace this starter skeleton.'],
  } })
  return revision
}

export default function Home() {
  const [locale, setLocale] = useState<Locale>(defaultLocale)
  const t = messages[locale]
  const [apps, setApps] = useState<Application[]>([])
  const [requirement, setRequirement] = useState<string>(t.requirementPlaceholder)
  const [selectedExampleId, setSelectedExampleId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [draftBusy, setDraftBusy] = useState(false)
  const [error, setError] = useState('')
  const [authRequired, setAuthRequired] = useState(false)
  const [tokenInput, setTokenInput] = useState('')
  const selectedCustomerExample = t.customerExamples.find(item => item.id === selectedExampleId)

  const refresh = () => api<Application[]>('/api/v1/applications').then(applications => {
    setApps(applications)
    setAuthRequired(false)
    setError('')
  }).catch(error => {
    if (isAuthError(error)) setAuthRequired(true)
    setError(String(error))
  })
  useEffect(() => {
    const stored = globalThis.localStorage?.getItem('foundry.locale')
    if (isLocale(stored)) setLocale(stored)
    setTokenInput(getClientToken())
    void refresh()
  }, [])

  function toggleLocale() {
    const value = nextLocale(locale)
    setLocale(value)
    globalThis.localStorage?.setItem('foundry.locale', value)
  }

  async function create(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const name = deriveApplicationName(requirement)
      const app = await api<Application>('/api/v1/applications', {
        method: 'POST',
        body: JSON.stringify({ name, description: requirement.slice(0, 180), requirement, mode: 'workflow' }),
      })
      const build = await api<{ build_id: string }>(`/api/v1/applications/${app.id}/builds`, {
        method: 'POST', body: JSON.stringify({ requirement, auto_publish: true }),
      })
      window.location.href = `/applications/${app.id}?build=${build.build_id}`
    } catch (cause) {
      setError(String(cause))
      setBusy(false)
    }
  }

  async function saveDraftOnly() {
    setDraftBusy(true)
    setError('')
    try {
      const name = deriveApplicationName(requirement)
      const app = await api<Application>('/api/v1/applications', {
        method: 'POST',
        body: JSON.stringify({ name, description: requirement.slice(0, 180), requirement, mode: 'workflow' }),
      })
      await seedSafeDraftSkeleton(app.id, app.draft_revision)
      window.location.href = `/applications/${app.id}?safeDraft=1`
    } catch (cause) {
      setError(String(cause))
      setDraftBusy(false)
    }
  }

  function saveToken(event: FormEvent) {
    event.preventDefault()
    saveClientToken(tokenInput)
    setError(t.authSaved)
    void refresh()
  }

  function applyCustomerExample(example: (typeof t.customerExamples)[number]) {
    setRequirement(example.requirement)
    setSelectedExampleId(example.id)
    setError('')
  }

  return (
    <main className="home-shell">
      <nav className="topbar"><div className="brand"><span>F</span> Foundry</div><div className="topbar-actions"><button className="lang-toggle" onClick={toggleLocale}>{t.switchLabel}</button><div className="status-dot">{t.status}</div></div></nav>
      <section className="hero">
        <div className="eyebrow">{t.eyebrow}</div>
        <h1>{t.heroTitleA}<br/><em>{t.heroTitleB}</em></h1>
        <p>{t.heroCopy}</p>
        <form className="create-card" onSubmit={create}>
          <textarea aria-label={t.requirementAria} value={requirement} onChange={event => setRequirement(event.target.value)} />
          <div className="create-footer">
            <div className="create-copy"><span>{t.createHint}</span><small>{t.safeDraftHint}</small></div>
            <div className="create-actions">
              <button className="secondary-action" disabled={busy || draftBusy || requirement.length < 10} onClick={saveDraftOnly} type="button">{draftBusy ? t.saveDraftOnlyBusy : t.saveDraftOnlyButton}</button>
              <button disabled={busy || draftBusy || requirement.length < 10}>{busy ? t.createBusy : t.createButton}</button>
            </div>
          </div>
        </form>
        <section className="customer-intake-panel" aria-labelledby="customer-intake-title">
          <div className="customer-intake-head">
            <div>
              <h2 id="customer-intake-title">{t.customerIntakeTitle}</h2>
              <p>{t.customerIntakeHelp}</p>
            </div>
            {selectedCustomerExample && <span>{t.selectedScenarioLabel} · {selectedCustomerExample.role}</span>}
          </div>
          <div className="example-grid">
            {t.customerExamples.map(example => <button
              className={`example-card ${selectedExampleId === example.id ? 'active' : ''}`}
              data-customer-example={example.id}
              key={example.id}
              onClick={() => applyCustomerExample(example)}
              type="button"
            >
              <span className="scenario-chip">{example.role}</span>
              <strong>{example.title}</strong>
              <p>{example.need}</p>
              <small>{example.expectedOutcome}</small>
              <em>{example.acceptanceSignal}</em>
              <b>{t.scenarioUseButton}</b>
            </button>)}
          </div>
        </section>
        {authRequired && <form className="auth-card" onSubmit={saveToken}>
          <div><strong>{t.authTitle}</strong><p>{t.authCopy}</p></div>
          <input type="password" value={tokenInput} placeholder={t.authPlaceholder} onChange={event => setTokenInput(event.target.value)} />
          <div className="auth-actions"><button>{t.authSave}</button><button type="button" className="ghost" onClick={() => { clearClientToken(); setTokenInput('') }}>{t.authClear}</button></div>
        </form>}
        {error && <div className="error-banner">{error}</div>}
      </section>
      <section className="customer-section">
        <div className="section-heading"><h2>{t.customerScenariosTitle}</h2><span>{t.customerScenariosHelp}</span></div>
        <div className="scenario-grid">{t.customerScenarios.map(item => <article className="scenario-card" key={item.role}>
          <strong>{item.role}</strong>
          <p>{item.need}</p>
          <small>{item.action}</small>
        </article>)}</div>
        <div className="product-path">{t.productSteps.map(item => <article className="step-card" key={item.title}>
          <b>{item.title}</b>
          <span>{item.text}</span>
        </article>)}</div>
      </section>
      <section className="apps-section">
        <div className="section-heading"><h2>{t.applications}</h2><span>{t.appCount(apps.length)}</span></div>
        <div className="app-grid">
          {apps.map(item => <Link className="app-card" href={`/applications/${item.id}`} key={item.id}>
            <div className="app-icon">{item.name.slice(0, 1).toUpperCase()}</div>
            <div><h3>{item.name}</h3><p>{item.description || t.fallbackDescription}</p></div>
            <div className="app-meta"><span>{item.active_version ? t.published(item.active_version) : t.draft}</span><span>r{item.draft_revision}</span></div>
          </Link>)}
          {!apps.length && <div className="empty-card"><strong>{t.emptyApps}</strong><span>{t.emptyAppsNextAction}</span></div>}
        </div>
      </section>
    </main>
  )
}
