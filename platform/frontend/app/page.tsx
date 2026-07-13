'use client'

import Link from 'next/link'
import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { api, clearClientToken, getClientToken, idempotency, isAuthError, saveClientToken } from '@/lib/platform'
import { defaultLocale, isLocale, messages, nextLocale, type Locale } from '@/lib/i18n'
import { classifyRuntimeStatus, runtimeCommit, runtimeVersion, type RuntimeHealth } from '@/lib/runtime-status'

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
const APP_FILTERS = ['all', 'needs_acceptance', 'ready_to_publish', 'published'] as const
type AppFilter = typeof APP_FILTERS[number]
const APP_SORTS = ['readiness', 'revision', 'name'] as const
type AppSort = typeof APP_SORTS[number]
type AppActionTab = 'edit' | 'test' | 'run' | 'monitor'
type AppQuickAction = { id: string; tab: AppActionTab; label: string }
type AppListUrlState = { filter?: AppFilter; q?: string; sort?: AppSort }
type Copy = (typeof messages)[Locale]

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

function appReadinessState(item: Application): Exclude<AppFilter, 'all'> {
  if (item.active_version) return 'published'
  if (item.tested_hash) return 'ready_to_publish'
  return 'needs_acceptance'
}

function appReadinessRank(item: Application) {
  const state = appReadinessState(item)
  if (state === 'published') return 0
  if (state === 'ready_to_publish') return 1
  return 2
}

function isAppFilter(value: string | null): value is AppFilter {
  return Boolean(value && APP_FILTERS.includes(value as AppFilter))
}

function isAppSort(value: string | null): value is AppSort {
  return Boolean(value && APP_SORTS.includes(value as AppSort))
}

function requirementReadiness(requirement: string, t: Copy) {
  const text = requirement.trim()
  const normalized = text.toLocaleLowerCase()
  const signals = [
    { id: 'audience', label: t.requirementSignalAudience, detail: t.requirementSignalAudienceHint, ready: /(客户|用户|负责人|顾问|运营|审阅|customer|user|owner|operator|consultant|reviewer)/i.test(normalized) },
    { id: 'outcome', label: t.requirementSignalOutcome, detail: t.requirementSignalOutcomeHint, ready: /(输出|生成|给出|判断|分类|摘要|清单|result|output|generate|classify|summary|checklist)/i.test(normalized) },
    { id: 'acceptance', label: t.requirementSignalAcceptance, detail: t.requirementSignalAcceptanceHint, ready: /(验收|测试|必须|覆盖|acceptance|test|must|cover|verify)/i.test(normalized) },
    { id: 'detail', label: t.requirementSignalDetail, detail: t.requirementSignalDetailHint, ready: text.length >= 80 },
  ]
  const readyCount = signals.filter(signal => signal.ready).length
  return { signals, readyCount, total: signals.length, ready: readyCount >= 3 }
}

function createActionState(requirement: string, readinessReady: boolean, busy: boolean, draftBusy: boolean, buildIntentConfirmed: boolean, t: Copy) {
  if (busy || draftBusy) return { id: 'busy', tone: 'busy', title: t.createActionBusyTitle, detail: t.createActionBusyDetail }
  if (requirement.trim().length < 10) return { id: 'add_detail', tone: 'attention', title: t.createActionAddDetailTitle, detail: t.createActionAddDetailDetail }
  if (buildIntentConfirmed) return { id: 'confirm_team', tone: 'warning', title: t.createActionConfirmTeamTitle, detail: t.createActionConfirmTeamDetail }
  if (readinessReady) return { id: 'save_draft', tone: 'ready', title: t.createActionSaveDraftTitle, detail: t.createActionSaveDraftDetail }
  return { id: 'improve_requirement', tone: 'attention', title: t.createActionImproveTitle, detail: t.createActionImproveDetail }
}

function recommendedCreateAction(actionId: string, t: Copy) {
  if (actionId === 'busy') return { target: 'wait', tone: 'busy', label: t.recommendedActionBusyLabel, detail: t.recommendedActionBusyDetail, disabled: true }
  if (actionId === 'save_draft') return { target: 'safe_draft', tone: 'ready', label: t.recommendedActionSaveDraftLabel, detail: t.recommendedActionSaveDraftDetail, disabled: false }
  if (actionId === 'confirm_team') return { target: 'guarded_build_button', tone: 'warning', label: t.recommendedActionTeamGuardLabel, detail: t.recommendedActionTeamGuardDetail, disabled: false }
  if (actionId === 'improve_requirement') return { target: 'requirement_focus', tone: 'attention', label: t.recommendedActionImproveLabel, detail: t.recommendedActionImproveDetail, disabled: false }
  return { target: 'requirement_focus', tone: 'attention', label: t.recommendedActionAddDetailLabel, detail: t.recommendedActionAddDetailDetail, disabled: false }
}

export default function Home() {
  const [locale, setLocale] = useState<Locale>(defaultLocale)
  const t = messages[locale]
  const requirementInputRef = useRef<HTMLTextAreaElement>(null)
  const buildButtonRef = useRef<HTMLButtonElement>(null)
  const [apps, setApps] = useState<Application[]>([])
  const [requirement, setRequirement] = useState<string>(t.requirementPlaceholder)
  const [selectedExampleId, setSelectedExampleId] = useState<string | null>(null)
  const [appFilter, setAppFilter] = useState<AppFilter>('all')
  const [appSearch, setAppSearch] = useState('')
  const [appSort, setAppSort] = useState<AppSort>('readiness')
  const [busy, setBusy] = useState(false)
  const [draftBusy, setDraftBusy] = useState(false)
  const [buildIntentConfirmed, setBuildIntentConfirmed] = useState(false)
  const [error, setError] = useState('')
  const [authRequired, setAuthRequired] = useState(false)
  const [tokenInput, setTokenInput] = useState('')
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth | null>(null)
  const [runtimeUnavailable, setRuntimeUnavailable] = useState(false)
  const writeAppListUrlState = useCallback((updates: AppListUrlState, options: { replace?: boolean } = {}) => {
    if (typeof window === 'undefined') return
    const query = new URLSearchParams(window.location.search)
    if (updates.filter !== undefined) {
      if (updates.filter === 'all') query.delete('filter')
      else query.set('filter', updates.filter)
    }
    if (updates.q !== undefined) {
      const value = updates.q.trim()
      if (value) query.set('q', value)
      else query.delete('q')
    }
    if (updates.sort !== undefined) {
      if (updates.sort === 'readiness') query.delete('sort')
      else query.set('sort', updates.sort)
    }
    const nextQuery = query.toString()
    const nextUrl = `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ''}`
    if (nextUrl === `${window.location.pathname}${window.location.search}`) return
    if (options.replace) window.history.replaceState(null, '', nextUrl)
    else window.history.pushState(null, '', nextUrl)
  }, [])
  const setAppListFilter = useCallback((value: AppFilter) => {
    setAppFilter(value)
    writeAppListUrlState({ filter: value })
  }, [writeAppListUrlState])
  const setAppListSearch = useCallback((value: string) => {
    setAppSearch(value)
    writeAppListUrlState({ q: value }, { replace: true })
  }, [writeAppListUrlState])
  const setAppListSort = useCallback((value: AppSort) => {
    setAppSort(value)
    writeAppListUrlState({ sort: value })
  }, [writeAppListUrlState])
  const clearAppListSearch = useCallback(() => {
    setAppListSearch('')
  }, [setAppListSearch])
  const resetAppListView = useCallback(() => {
    setAppFilter('all')
    setAppSearch('')
    setAppSort('readiness')
    writeAppListUrlState({ filter: 'all', q: '', sort: 'readiness' })
  }, [writeAppListUrlState])
  const syncAppListStateFromLocation = useCallback(() => {
    if (typeof window === 'undefined') return
    const query = new URLSearchParams(window.location.search)
    const filter = query.get('filter')
    const sort = query.get('sort')
    setAppFilter(isAppFilter(filter) ? filter : 'all')
    setAppSort(isAppSort(sort) ? sort : 'readiness')
    setAppSearch(query.get('q') || '')
  }, [])
  const selectedCustomerExample = t.customerExamples.find(item => item.id === selectedExampleId)
  const createReadiness = requirementReadiness(requirement, t)
  const createAction = createActionState(requirement, createReadiness.ready, busy, draftBusy, buildIntentConfirmed, t)
  const recommendedAction = recommendedCreateAction(createAction.id, t)
  const runtimeStatus = classifyRuntimeStatus(runtimeHealth, { authRequired, unavailable: runtimeUnavailable })
  const runtimeStatusText = runtimeStatus === 'connected'
    ? t.runtimeStatusConnected(runtimeVersion(runtimeHealth))
    : runtimeStatus === 'auth_required'
      ? t.runtimeStatusAuthRequired
      : runtimeStatus === 'stale'
        ? t.runtimeStatusStale(runtimeVersion(runtimeHealth))
        : runtimeStatus === 'unavailable'
          ? t.runtimeStatusUnavailable
          : t.runtimeStatusChecking
  const runtimeStatusDetail = runtimeStatus === 'connected'
    ? t.runtimeStatusDetailConnected(runtimeCommit(runtimeHealth))
    : runtimeStatus === 'auth_required'
      ? t.runtimeStatusDetailAuthRequired
      : runtimeStatus === 'stale'
        ? t.runtimeStatusDetailStale
        : runtimeStatus === 'unavailable'
          ? t.runtimeStatusDetailUnavailable
          : t.runtimeStatusDetailChecking
  const appCardReadiness = (item: Application) => [
    { label: t.appCardDraftState, value: `r${item.draft_revision}`, ready: true },
    { label: t.appCardAcceptanceState, value: item.tested_hash ? t.verified : t.unverified, ready: Boolean(item.tested_hash) },
    { label: t.appCardPublishState, value: item.active_version ? t.published(item.active_version) : t.draft, ready: Boolean(item.active_version) },
  ]
  const appCardNextAction = (item: Application) => item.active_version
    ? t.appNextActionTryMonitor
    : item.tested_hash
      ? t.appNextActionPublish
      : t.appNextActionRunAcceptance
  const appCardQuickActions = (item: Application): AppQuickAction[] => {
    const state = appReadinessState(item)
    if (state === 'published') return [
      { id: 'try', tab: 'run', label: t.appActionTry },
      { id: 'monitor', tab: 'monitor', label: t.appActionMonitor },
    ]
    if (state === 'ready_to_publish') return [
      { id: 'acceptance', tab: 'test', label: t.appActionAcceptance },
      { id: 'publish_check', tab: 'test', label: t.appActionPublishCheck },
    ]
    return [
      { id: 'edit', tab: 'edit', label: t.appActionEdit },
      { id: 'acceptance', tab: 'test', label: t.appActionAcceptance },
    ]
  }
  const appFilterOptions: Array<{ id: AppFilter; label: string }> = [
    { id: 'all', label: t.appFilterAll },
    { id: 'needs_acceptance', label: t.appFilterNeedsAcceptance },
    { id: 'ready_to_publish', label: t.appFilterReadyToPublish },
    { id: 'published', label: t.appFilterPublished },
  ]
  const appSortOptions: Array<{ id: AppSort; label: string }> = [
    { id: 'readiness', label: t.appSortReadiness },
    { id: 'revision', label: t.appSortRevision },
    { id: 'name', label: t.appSortName },
  ]
  const appFilterCount = (filter: AppFilter) => filter === 'all' ? apps.length : apps.filter(item => appReadinessState(item) === filter).length
  const statusFilteredApps = appFilter === 'all' ? apps : apps.filter(item => appReadinessState(item) === appFilter)
  const normalizedAppSearch = appSearch.trim().toLocaleLowerCase()
  const searchedApps = normalizedAppSearch
    ? statusFilteredApps.filter(item => `${item.name} ${item.description}`.toLocaleLowerCase().includes(normalizedAppSearch))
    : statusFilteredApps
  const visibleApps = [...searchedApps].sort((left, right) => {
    if (appSort === 'name') return left.name.localeCompare(right.name)
    if (appSort === 'revision') return right.draft_revision - left.draft_revision || left.name.localeCompare(right.name)
    return appReadinessRank(left) - appReadinessRank(right) || right.draft_revision - left.draft_revision || left.name.localeCompare(right.name)
  })
  const currentAppFilterLabel = appFilterOptions.find(option => option.id === appFilter)?.label || t.appFilterAll
  const currentAppSortLabel = appSortOptions.find(option => option.id === appSort)?.label || t.appSortReadiness
  const appListViewDirty = appFilter !== 'all' || Boolean(normalizedAppSearch) || appSort !== 'readiness'

  const refresh = () => api<Application[]>('/api/v1/applications').then(applications => {
    setApps(applications)
    setAuthRequired(false)
    setError('')
  }).catch(error => {
    if (isAuthError(error)) setAuthRequired(true)
    setError(String(error))
  })
  const refreshRuntimeStatus = () => api<RuntimeHealth>('/health').then(health => {
    setRuntimeHealth(health)
    setRuntimeUnavailable(false)
  }).catch(() => {
    setRuntimeHealth(null)
    setRuntimeUnavailable(true)
  })
  useEffect(() => {
    const stored = globalThis.localStorage?.getItem('foundry.locale')
    if (isLocale(stored)) setLocale(stored)
    setTokenInput(getClientToken())
    void refreshRuntimeStatus()
    void refresh()
  }, [])
  useEffect(() => {
    syncAppListStateFromLocation()
    window.addEventListener('popstate', syncAppListStateFromLocation)
    return () => window.removeEventListener('popstate', syncAppListStateFromLocation)
  }, [syncAppListStateFromLocation])

  function toggleLocale() {
    const value = nextLocale(locale)
    setLocale(value)
    globalThis.localStorage?.setItem('foundry.locale', value)
  }
  function clearCustomerExample() {
    setSelectedExampleId(null)
  }

  async function create(event: FormEvent) {
    event.preventDefault()
    if (!buildIntentConfirmed) {
      setBuildIntentConfirmed(true)
      setError(t.buildIntentHomeConfirm)
      return
    }
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

  async function runRecommendedCreateAction() {
    if (recommendedAction.disabled) return
    setError('')
    if (recommendedAction.target === 'requirement_focus') {
      requirementInputRef.current?.focus()
      return
    }
    if (recommendedAction.target === 'safe_draft') {
      await saveDraftOnly()
      return
    }
    if (recommendedAction.target === 'guarded_build_button') {
      buildButtonRef.current?.focus()
      setError(t.recommendedActionTeamGuardDetail)
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
    setBuildIntentConfirmed(false)
    setError('')
  }

  return (
    <main className="home-shell">
      <nav className="topbar"><div className="brand"><span>F</span> Foundry</div><div className="topbar-actions"><button className="lang-toggle" onClick={toggleLocale}>{t.switchLabel}</button><div className={`status-dot runtime-status ${runtimeStatus}`} data-runtime-status={runtimeStatus}><span>{runtimeStatusText}</span><small>{runtimeStatusDetail}</small></div></div></nav>
      <section className="hero">
        <div className="eyebrow">{t.eyebrow}</div>
        <h1>{t.heroTitleA}<br/><em>{t.heroTitleB}</em></h1>
        <p>{t.heroCopy}</p>
        <form className="create-card" onSubmit={create}>
          <textarea ref={requirementInputRef} aria-label={t.requirementAria} value={requirement} onChange={event => { setRequirement(event.target.value); setBuildIntentConfirmed(false) }} />
          {selectedCustomerExample && <section className="selected-scenario-summary" data-selected-scenario-summary="active">
            <div><span>{t.selectedScenarioSummaryTitle} · {selectedCustomerExample.role}</span><strong>{selectedCustomerExample.title}</strong><p>{selectedCustomerExample.need}</p><small>{selectedCustomerExample.acceptanceSignal}</small></div>
            <button onClick={clearCustomerExample} type="button">{t.clearSelectedScenario}</button>
          </section>}
          <section className={`requirement-readiness ${createReadiness.ready ? 'ready' : 'needs-detail'}`} data-requirement-readiness="summary">
            <div className="requirement-readiness-head"><strong>{t.requirementReadinessTitle}</strong><span>{t.requirementReadinessScore(createReadiness.readyCount, createReadiness.total)}</span></div>
            <p>{createReadiness.ready ? t.requirementReadinessReady : t.requirementReadinessNeedsDetail}</p>
            <div className="requirement-readiness-list">{createReadiness.signals.map(signal => <article className={signal.ready ? 'ready' : ''} key={signal.id}>
              <b>{signal.label}</b>
              <small>{signal.detail}</small>
            </article>)}</div>
          </section>
          <section className={`create-action-explainer ${createAction.tone}`} data-create-action-state={createAction.id}>
            <strong>{createAction.title}</strong>
            <span>{createAction.detail}</span>
          </section>
          <section className={`recommended-create-action ${recommendedAction.tone}`} data-recommended-create-action={createAction.id} data-recommended-action-target={recommendedAction.target}>
            <div><strong>{t.recommendedActionTitle}</strong><span>{recommendedAction.detail}</span></div>
            <button type="button" disabled={recommendedAction.disabled || busy || draftBusy} onClick={() => void runRecommendedCreateAction()}>{recommendedAction.label}</button>
          </section>
          <div className="create-footer">
            <div className="create-copy"><span>{t.createHint}</span><small>{t.safeDraftHint}</small><small className="build-intent-copy" data-build-intent={buildIntentConfirmed ? 'confirmed' : 'needs-confirmation'}>{buildIntentConfirmed ? t.buildIntentHomeArmed : t.buildIntentHomeSafe}</small></div>
            <div className="create-actions">
              <button className="secondary-action" disabled={busy || draftBusy || requirement.length < 10} onClick={saveDraftOnly} type="button">{draftBusy ? t.saveDraftOnlyBusy : t.saveDraftOnlyButton}</button>
              <button ref={buildButtonRef} className={`build-action ${buildIntentConfirmed ? 'armed' : ''}`} data-build-action="home-start-builder-team" data-build-intent={buildIntentConfirmed ? 'confirmed' : 'needs-confirmation'} disabled={busy || draftBusy || requirement.length < 10}>{busy ? t.createBusy : buildIntentConfirmed ? t.createConfirmButton : t.createButton}</button>
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
      <section className="apps-section" data-app-list-url-state="synced">
        <div className="section-heading"><h2>{t.applications}</h2><span>{t.appCount(apps.length)}</span></div>
        {apps.length > 0 && <div className="app-filter-toolbar" data-app-list-filter="status">
          {appFilterOptions.map(option => <button className={appFilter === option.id ? 'active' : ''} onClick={() => setAppListFilter(option.id)} key={option.id} type="button">
            <span>{option.label}</span><b>{appFilterCount(option.id)}</b>
          </button>)}
        </div>}
        {apps.length > 0 && <div className="app-search-sort" data-app-list-search-sort="controls">
          <input aria-label={t.appSearchLabel} placeholder={t.appSearchPlaceholder} value={appSearch} onChange={event => setAppListSearch(event.target.value)} />
          <label>{t.appSortLabel}<select value={appSort} onChange={event => setAppListSort(event.target.value as AppSort)}>
            {appSortOptions.map(option => <option value={option.id} key={option.id}>{option.label}</option>)}
          </select></label>
        </div>}
        {apps.length > 0 && <div className="app-list-view-state" data-app-list-view-summary="active">
          <span>{t.appListSummaryCount(visibleApps.length, apps.length)}</span>
          <span>{t.appListSummaryFilter(currentAppFilterLabel)}</span>
          {normalizedAppSearch && <span>{t.appListSummarySearch(appSearch.trim())}</span>}
          <span>{t.appListSummarySort(currentAppSortLabel)}</span>
          <div className="app-list-view-actions">
            {normalizedAppSearch && <button onClick={clearAppListSearch} type="button">{t.appListClearSearch}</button>}
            <button disabled={!appListViewDirty} onClick={resetAppListView} type="button">{t.appListResetView}</button>
          </div>
        </div>}
        <div className="app-grid">
          {visibleApps.map(item => <article className="app-card" data-app-card-action-state={appReadinessState(item)} key={item.id}>
            <Link className="app-card-main" href={`/applications/${item.id}`} aria-label={`${t.appActionOpen}: ${item.name}`}>
              <div className="app-icon">{item.name.slice(0, 1).toUpperCase()}</div>
              <div><h3>{item.name}</h3><p>{item.description || t.fallbackDescription}</p>
                <div className="app-readiness" data-app-card-guidance="readiness">{appCardReadiness(item).map(signal => <span className={signal.ready ? 'ready' : ''} key={signal.label}><b>{signal.label}</b>{signal.value}</span>)}</div>
                <small className="app-next-action" data-app-card-guidance="next-action">{appCardNextAction(item)}</small>
              </div>
              <div className="app-meta"><span>{item.active_version ? t.published(item.active_version) : t.draft}</span><span>r{item.draft_revision}</span></div>
            </Link>
            <div className="app-card-actions" data-app-card-quick-actions="navigation">
              {appCardQuickActions(item).map(action => <Link href={`/applications/${item.id}?tab=${action.tab}`} data-app-card-action={action.id} key={action.id}>{action.label}</Link>)}
            </div>
          </article>)}
          {apps.length > 0 && !visibleApps.length && <div className="empty-card"><strong>{normalizedAppSearch ? t.appSearchEmpty : t.appFilterEmpty}</strong><span>{normalizedAppSearch ? t.appSearchEmptyHelp : t.appFilterEmptyHelp}</span></div>}
          {!apps.length && <div className="empty-card"><strong>{t.emptyApps}</strong><span>{t.emptyAppsNextAction}</span></div>}
        </div>
      </section>
    </main>
  )
}
