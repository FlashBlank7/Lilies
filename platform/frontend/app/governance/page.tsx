'use client'

import Link from 'next/link'
import { useSearchParams } from 'next/navigation'
import { Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Boxes,
  CalendarClock,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleDollarSign,
  Clock3,
  FileCheck2,
  Filter,
  Gauge,
  GitBranch,
  KeyRound,
  ListTree,
  LoaderCircle,
  MessagesSquare,
  PlugZap,
  RefreshCw,
  Search,
  ServerCog,
  ShieldCheck,
  TriangleAlert,
  Users,
  X,
} from 'lucide-react'
import {
  api,
  isAuthError,
  saveClientToken,
  type GovernanceAlert,
  type GovernanceAlerts,
  type GovernanceEvidence,
  type GovernanceDurableJobs,
  type GovernanceConnectorOperations,
  type GovernanceOverview,
  type GovernancePolicy,
  type GovernanceReliability,
  type GovernanceSupport,
  type GovernanceTask,
  type GovernanceTaskPage,
  type GovernanceTrace,
  type GovernanceTraceNode,
  type GovernanceUsage,
  type PlatformPolicyControlsUpdateResponse,
} from '@/lib/platform'
import { ScheduleOperationsPanel } from '@/app/schedule-operations-panel'
import styles from './governance.module.css'


const CONSOLE_TABS = [
  'Overview',
  'Cost & Tokens',
  'Reliability',
  'Durable Jobs',
  'Connector Operations',
  'Policy & Risk',
  'Trace Explorer',
  'Capability Evidence',
  'Alerts & Incidents',
] as const

const TASK_PAGE_SIZE = 100

type ConsoleTab = typeof CONSOLE_TABS[number]
type ApplicationOption = { id: string; name: string }
type Filters = {
  applicationId: string
  ownerId: string
  status: string
  kind: string
  model: string
  query: string
}
type ConnectorFilters = {
  connectorId: string
  tenantId: string
  operationId: string
  status: string
  emergencyStop: '' | 'true' | 'false'
}

const TAB_ICONS = {
  'Overview': Gauge,
  'Cost & Tokens': CircleDollarSign,
  'Reliability': Activity,
  'Durable Jobs': CalendarClock,
  'Connector Operations': PlugZap,
  'Policy & Risk': ShieldCheck,
  'Trace Explorer': ListTree,
  'Capability Evidence': FileCheck2,
  'Alerts & Incidents': AlertTriangle,
} satisfies Record<ConsoleTab, typeof Gauge>

function shortId(value: string | null | undefined) {
  return value ? value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value : '—'
}

function time(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function duration(value: number | null | undefined) {
  if (value === null || value === undefined) return '—'
  if (value < 1) return `${Math.round(value * 1000)} ms`
  if (value < 60) return `${value.toFixed(1)} s`
  return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`
}

function money(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : `$${value.toFixed(value < 0.01 ? 5 : 3)}`
}

function number(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : Intl.NumberFormat().format(value)
}

function supportLabel(value: GovernanceSupport | string | undefined) {
  if (value === 'reported') return 'Reported'
  if (value === 'estimated') return 'Estimated'
  if (value === 'unsupported') return 'Unsupported'
  return 'Not recorded'
}

function SupportBadge({ value }: { value: GovernanceSupport | string | undefined }) {
  return <span className={`${styles.support} ${styles[`support_${value || 'not_recorded'}`]}`}>{supportLabel(value)}</span>
}

function queryString(filters: Filters) {
  const query = new URLSearchParams()
  if (filters.applicationId) query.set('application_id', filters.applicationId)
  if (filters.ownerId) query.set('owner_id', filters.ownerId)
  if (filters.status) query.set('status', filters.status)
  if (filters.kind) query.set('kind', filters.kind)
  if (filters.model) query.set('model', filters.model)
  if (filters.query) query.set('query', filters.query)
  const value = query.toString()
  return value ? `?${value}` : ''
}

function durableQueryString(filters: Filters) {
  const query = new URLSearchParams()
  if (filters.applicationId) query.set('application_id', filters.applicationId)
  if (filters.status) query.set('status', filters.status)
  query.set('limit', '200')
  return `?${query.toString()}`
}

function connectorQueryString(filters: ConnectorFilters, offset: number) {
  const query = new URLSearchParams()
  if (filters.connectorId) query.set('connector_id', filters.connectorId)
  if (filters.tenantId) query.set('tenant_id', filters.tenantId)
  if (filters.operationId) query.set('operation_id', filters.operationId)
  if (filters.status) query.set('status', filters.status)
  if (filters.emergencyStop) query.set('emergency_stop', filters.emergencyStop)
  query.set('limit', '100')
  query.set('offset', String(offset))
  return `?${query.toString()}`
}

function TaskStatus({ task }: { task: GovernanceTask }) {
  return <span className={`${styles.taskStatus} ${styles[`status_${task.status}`]}`}><i />{task.status}</span>
}

function TraceBranch({ node, selected, onSelect, depth = 0 }: { node: GovernanceTraceNode; selected: string; onSelect: (id: string) => void; depth?: number }) {
  return <div className={styles.traceBranch} style={{ '--depth': depth } as React.CSSProperties}>
    <button className={node.id === selected ? styles.traceSelected : ''} onClick={() => onSelect(node.id)}>
      <GitBranch size={14} />
      <span><strong>{node.kind}</strong><small>{shortId(node.id)} · {node.status}</small></span>
      {node.children.length > 0 && <b>{node.children.length}</b>}
    </button>
    {node.children.map(child => <TraceBranch depth={depth + 1} key={child.id} node={child} selected={selected} onSelect={onSelect} />)}
  </div>
}

function GovernanceConsolePageContent() {
  const searchParams = useSearchParams()
  const initialApplicationId = searchParams.get('application_id') || ''
  const [tab, setTab] = useState<ConsoleTab>('Overview')
  const [filters, setFilters] = useState<Filters>({ applicationId: initialApplicationId, ownerId: '', status: '', kind: '', model: '', query: '' })
  const [applications, setApplications] = useState<ApplicationOption[]>([])
  const [overview, setOverview] = useState<GovernanceOverview | null>(null)
  const [tasks, setTasks] = useState<GovernanceTaskPage | null>(null)
  const [taskOffset, setTaskOffset] = useState(0)
  const [usage, setUsage] = useState<GovernanceUsage | null>(null)
  const [reliability, setReliability] = useState<GovernanceReliability | null>(null)
  const [durableJobs, setDurableJobs] = useState<GovernanceDurableJobs | null>(null)
  const [connectors, setConnectors] = useState<GovernanceConnectorOperations | null>(null)
  const [connectorFilters, setConnectorFilters] = useState<ConnectorFilters>({ connectorId: '', tenantId: '', operationId: '', status: '', emergencyStop: '' })
  const [connectorOffset, setConnectorOffset] = useState(0)
  const [policy, setPolicy] = useState<GovernancePolicy | null>(null)
  const [evidence, setEvidence] = useState<GovernanceEvidence | null>(null)
  const [alerts, setAlerts] = useState<GovernanceAlerts | null>(null)
  const [trace, setTrace] = useState<GovernanceTrace | null>(null)
  const [selectedTaskId, setSelectedTaskId] = useState('')
  const [loading, setLoading] = useState(true)
  const [traceLoading, setTraceLoading] = useState(false)
  const [error, setError] = useState('')
  const [authNeeded, setAuthNeeded] = useState(false)
  const [accessKey, setAccessKey] = useState('')
  const [policyReason, setPolicyReason] = useState('Operational governance update')
  const [policyDraft, setPolicyDraft] = useState({ network: 'full', cancellation: 'enabled', secret: true })
  const [policySaving, setPolicySaving] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError('')
    const query = queryString(filters)
    try {
      const [nextApplications, nextOverview, nextTasks, nextUsage, nextReliability, nextDurableJobs, nextConnectors, nextPolicy, nextEvidence, nextAlerts] = await Promise.all([
        api<ApplicationOption[]>('/api/v1/applications'),
        api<GovernanceOverview>(`/api/v1/governance/overview${query}`),
        api<GovernanceTaskPage>(`/api/v1/governance/tasks${query}${query ? '&' : '?'}limit=${TASK_PAGE_SIZE}&offset=${taskOffset}`),
        api<GovernanceUsage>(`/api/v1/governance/usage${query}${query ? '&' : '?'}interval=hour`),
        api<GovernanceReliability>(`/api/v1/governance/reliability${query}`),
        api<GovernanceDurableJobs>(`/api/v1/governance/durable-jobs${durableQueryString(filters)}`),
        api<GovernanceConnectorOperations>(`/api/v1/governance/connectors${connectorQueryString(connectorFilters, connectorOffset)}`),
        api<GovernancePolicy>('/api/v1/governance/policy'),
        api<GovernanceEvidence>('/api/v1/governance/capability-evidence'),
        api<GovernanceAlerts>(`/api/v1/governance/alerts${query}`),
      ])
      setApplications(nextApplications)
      setOverview(nextOverview)
      setTasks(nextTasks)
      setUsage(nextUsage)
      setReliability(nextReliability)
      setDurableJobs(nextDurableJobs)
      setConnectors(nextConnectors)
      setPolicy(nextPolicy)
      setEvidence(nextEvidence)
      setAlerts(nextAlerts)
      setPolicyDraft({
        network: nextPolicy.controls.network_egress_policy,
        cancellation: nextPolicy.controls.cancellation_policy,
        secret: nextPolicy.controls.secret_policy_enabled,
      })
      setAuthNeeded(false)
    } catch (caught) {
      if (isAuthError(caught)) setAuthNeeded(true)
      else setError(String(caught))
    } finally {
      setLoading(false)
    }
  }, [connectorFilters, connectorOffset, filters, taskOffset])

  useEffect(() => { void refresh() }, [refresh])

  const loadTrace = useCallback(async (taskId: string) => {
    setSelectedTaskId(taskId)
    setTraceLoading(true)
    setError('')
    try {
      setTrace(await api<GovernanceTrace>(`/api/v1/governance/traces/${encodeURIComponent(taskId)}`))
      setTab('Trace Explorer')
    } catch (caught) {
      setError(String(caught))
    } finally {
      setTraceLoading(false)
    }
  }, [])

  const models = useMemo(() => {
    const values = new Set<string>()
    tasks?.items.forEach(task => { if (task.model) values.add(task.model) })
    usage?.samples.forEach(sample => { if (sample.model) values.add(sample.model) })
    usage?.dimensions.model?.forEach(item => {
      const model = item.model
      if (typeof model === 'string' && model !== 'not_recorded') values.add(model)
    })
    return [...values].sort()
  }, [tasks, usage])
  const connectorIds = useMemo(() => Array.from(new Set((connectors?.manifests || []).map(item => String(item.connector_id)))).sort(), [connectors])
  const connectorTenantIds = useMemo(() => Array.from(new Set((connectors?.bindings || []).map(item => String(item.tenant_id)))).sort(), [connectors])
  const connectorOperationIds = useMemo(() => Array.from(new Set((connectors?.manifests || []).flatMap(item => Array.isArray(item.operations) ? item.operations.map(String) : []))).sort(), [connectors])

  const usageMax = useMemo(() => Math.max(1, ...(usage?.series || []).map(item => Number(item.input_tokens || 0) + Number(item.output_tokens || 0))), [usage])
  const selectedTask = tasks?.items.find(task => task.id === selectedTaskId)

  async function savePolicy() {
    if (!policy) return
    const patch: Record<string, unknown> = { reason: policyReason }
    if (policyDraft.network !== policy.controls.network_egress_policy) patch.network_egress_policy = policyDraft.network
    if (policyDraft.cancellation !== policy.controls.cancellation_policy) patch.cancellation_policy = policyDraft.cancellation
    if (policyDraft.secret !== policy.controls.secret_policy_enabled) patch.secret_policy_enabled = policyDraft.secret
    if (Object.keys(patch).length === 1) {
      setError('没有需要保存的策略变更。')
      return
    }
    setPolicySaving(true)
    setError('')
    try {
      await api<PlatformPolicyControlsUpdateResponse>('/api/v1/platform/harness/policy-controls', { method: 'PATCH', body: JSON.stringify(patch) })
      await refresh()
    } catch (caught) {
      setError(String(caught))
    } finally {
      setPolicySaving(false)
    }
  }

  function connect() {
    saveClientToken(accessKey)
    void refresh()
  }

  function updateFilter<Key extends keyof Filters>(key: Key, value: Filters[Key]) {
    setTaskOffset(0)
    setFilters(current => ({ ...current, [key]: value }))
  }

  function updateConnectorFilter<Key extends keyof ConnectorFilters>(key: Key, value: ConnectorFilters[Key]) {
    setConnectorOffset(0)
    setConnectorFilters(current => ({ ...current, [key]: value }))
  }

  const resetFilters = () => {
    setTaskOffset(0)
    setFilters({ applicationId: '', ownerId: '', status: '', kind: '', model: '', query: '' })
  }

  if (authNeeded) return <main className={styles.shell} data-governance-console="true"><section className={styles.authState}><KeyRound size={28} /><strong>Governance access required</strong><p>Enter the platform access key to inspect operational evidence.</p><div><input type="password" value={accessKey} onChange={event => setAccessKey(event.target.value)} placeholder="Access key" /><button disabled={!accessKey.trim()} onClick={connect}><Check size={16} />Connect</button></div></section></main>

  return <main className={styles.shell} data-governance-console="true">
    <aside className={styles.sidebar}>
      <div className={styles.brand}><ServerCog size={20} /><div><span>Lilies</span><strong>Governance</strong></div></div>
      <nav aria-label="Governance views">{CONSOLE_TABS.map(item => {
        const Icon = TAB_ICONS[item]
        const count = item === 'Alerts & Incidents' ? alerts?.total : item === 'Trace Explorer' ? trace?.spans.length : undefined
        return <button aria-current={tab === item ? 'page' : undefined} className={tab === item ? styles.activeTab : ''} data-governance-tab={item} key={item} onClick={() => setTab(item)}><Icon size={16} /><span>{item}</span>{count !== undefined && <b>{count}</b>}</button>
      })}</nav>
      <div className={styles.sidebarFooter}><Link href="/"><ArrowLeft size={15} />Applications</Link><Link data-global-developer-collaboration="true" href="/developer/collaboration"><MessagesSquare size={15} />Collaboration</Link>{filters.applicationId && <Link href={`/applications/${filters.applicationId}`}><Boxes size={15} />Engineer Studio</Link>}</div>
    </aside>

    <section className={styles.workspace}>
      <header className={styles.topbar}>
        <div><span>Platform Control Plane</span><h1>{tab}</h1></div>
        <button className={styles.iconButton} onClick={() => void refresh()} disabled={loading} aria-label="Refresh governance data" title="Refresh governance data"><RefreshCw className={loading ? styles.spin : ''} size={17} /></button>
      </header>

      <section className={styles.filters} aria-label="Governance filters">
        <div className={styles.filterLead}><Filter size={15} /><span>Scope</span></div>
        <label><span>Application</span><select value={filters.applicationId} onChange={event => updateFilter('applicationId', event.target.value)}><option value="">All applications</option>{applications.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
        <label><span>Status</span><select value={filters.status} onChange={event => updateFilter('status', event.target.value)}><option value="">All statuses</option>{['queued', 'running', 'retry_wait', 'paused', 'succeeded', 'failed', 'cancelled'].map(item => <option key={item}>{item}</option>)}</select></label>
        <label><span>Kind</span><select value={filters.kind} onChange={event => updateFilter('kind', event.target.value)}><option value="">All task kinds</option>{['workflow_run', 'builder_build', 'agent_generation', 'agent_turn', 'test_suite', 'scheduler_trigger', 'scheduler_manual_trigger', 'benchmark', 'draft_patch_preview', 'requirement_intake'].map(item => <option key={item}>{item}</option>)}</select></label>
        <label><span>Model</span><select value={filters.model} onChange={event => updateFilter('model', event.target.value)}><option value="">All models</option>{models.map(item => <option key={item}>{item}</option>)}</select></label>
        <label className={styles.searchField}><span>Search</span><div><Search size={14} /><input value={filters.query} onChange={event => updateFilter('query', event.target.value)} placeholder="task, owner, error" /></div></label>
        {Object.values(filters).some(Boolean) && <button className={styles.clearButton} onClick={resetFilters} aria-label="Clear filters" title="Clear filters"><X size={15} /></button>}
      </section>

      {error && <div className={styles.errorBanner} role="alert"><TriangleAlert size={17} /><span>{error}</span><button onClick={() => setError('')} aria-label="Dismiss error"><X size={14} /></button></div>}
      {loading && !overview ? <div className={styles.loadingState}><LoaderCircle className={styles.spin} size={24} /><strong>Loading platform evidence</strong></div> : <div className={styles.content}>
        {tab === 'Overview' && <>
          <section className={styles.metricGrid}>
            <article><span>Active</span><strong>{overview?.task_counts.active ?? 0}</strong><small>tasks running now</small><Activity size={18} /></article>
            <article><span>Queued</span><strong>{overview?.task_counts.queued ?? 0}</strong><small>{duration(overview?.queue_delay_seconds.p95)} p95 delay</small><Clock3 size={18} /></article>
            <article><span>Failed</span><strong>{overview?.task_counts.failed ?? 0}</strong><small>in current scope</small><TriangleAlert size={18} /></article>
            <article><span>Workers</span><strong>{overview?.workers.active ?? 0}/{overview?.workers.total ?? 0}</strong><small>{overview?.workers.stale ?? 0} stale</small><Users size={18} /></article>
            <article><span>Duration p50</span><strong>{duration(overview?.duration_seconds.p50)}</strong><small>p95 {duration(overview?.duration_seconds.p95)}</small><Gauge size={18} /></article>
            <article><span>Durable active</span><strong>{overview?.durable_jobs.active ?? 0}</strong><small>{overview?.durable_jobs.retry_wait ?? 0} retry waiting</small><CalendarClock size={18} /></article>
          </section>
          <section className={styles.dataSection}>
            <header><div><h2>Platform tasks</h2><p>Cross-application execution, build, test, scheduler, and agent work.</p></div><span>{tasks?.total || 0} records</span></header>
            <div className={styles.tableWrap}><table><thead><tr><th>Task</th><th>Scope</th><th>Status</th><th>Model</th><th>Duration</th><th>Started</th><th /></tr></thead><tbody>{tasks?.items.map(task => <tr key={task.id}><td><strong>{task.kind}</strong><code>{shortId(task.id)}</code></td><td><span>{task.application_name || task.application_id || task.owner_id}</span><small>{shortId(task.owner_id)}</small></td><td><TaskStatus task={task} /></td><td>{task.model || '—'}</td><td>{duration(task.duration_seconds)}</td><td>{time(task.created_at)}</td><td><button className={styles.rowAction} onClick={() => void loadTrace(task.id)} aria-label={`Open trace ${task.id}`} title="Open trace"><ChevronRight size={15} /></button></td></tr>)}</tbody></table>{!tasks?.items.length && <div className={styles.emptyState}>No tasks match the current scope.</div>}</div>
            {tasks && tasks.total > 0 && <footer className={styles.supportFooter} aria-label="Task pages">
              <span>{tasks.offset + 1}–{Math.min(tasks.offset + tasks.items.length, tasks.total)} of {tasks.total}</span>
              <span>
                <button className={styles.rowAction} disabled={tasks.offset === 0 || loading} onClick={() => setTaskOffset(Math.max(0, tasks.offset - tasks.limit))} aria-label="Previous task page" title="Previous task page"><ChevronLeft size={15} /></button>
                <strong>Page {Math.floor(tasks.offset / tasks.limit) + 1} of {Math.ceil(tasks.total / tasks.limit)}</strong>
                <button className={styles.rowAction} disabled={!tasks.has_more || loading} onClick={() => setTaskOffset(tasks.offset + tasks.limit)} aria-label="Next task page" title="Next task page"><ChevronRight size={15} /></button>
              </span>
            </footer>}
          </section>
          <section className={styles.boundaryNote}><ShieldCheck size={16} /><span>{overview?.claim_boundary}</span></section>
        </>}

        {tab === 'Cost & Tokens' && <>
          <section className={styles.metricGrid}>
            {([['Input tokens', 'input_tokens'], ['Output tokens', 'output_tokens'], ['Cached input tokens', 'cached_input_tokens'], ['Reasoning tokens', 'reasoning_tokens']] as const).map(([label, key]) => <article key={key}><span>{label}</span><strong>{number(usage?.totals[key])}</strong><SupportBadge value={usage?.support[key]} /><Boxes size={18} /></article>)}
            <article><span>Cost</span><strong>{money(usage?.totals.cost_usd)}</strong><SupportBadge value={usage?.support.cost_usd} /><CircleDollarSign size={18} /></article>
          </section>
          <section className={styles.dataSection}><header><div><h2>Usage over time</h2><p>Provider response usage only. Call counts are excluded from token totals.</p></div><span>{usage?.sample_count || 0} model responses</span></header><div className={styles.series}>{usage?.series.length ? usage.series.map(bucket => {
            const total = Number(bucket.input_tokens || 0) + Number(bucket.output_tokens || 0)
            return <div key={String(bucket.start)}><time>{time(String(bucket.start))}</time><div><i style={{ width: `${Math.max(2, (total / usageMax) * 100)}%` }} /></div><strong>{number(total)}</strong><span>{money(Number(bucket.cost_usd || 0))}</span></div>
          }) : <div className={styles.emptyState}>No model usage has been recorded for this scope.</div>}</div></section>
          <section className={styles.splitSections}>
            <div className={styles.dataSection}><header><div><h2>By model</h2><p>Input and output token attribution.</p></div></header><div className={styles.compactList}>{usage?.dimensions.model.map(item => <div key={String(item.model)}><span><strong>{String(item.model)}</strong><small>{number(Number(item.calls))} calls</small></span><b>{number(Number(item.tokens))}</b><em>{money(Number(item.cost_usd))}</em></div>)}</div></div>
            <div className={styles.dataSection}><header><div><h2>Budget observations</h2><p>Remaining values appear only when a cost budget is configured.</p></div></header><div className={styles.compactList}>{usage?.budgets.length ? usage.budgets.map(item => <div key={String(item.task_id)}><span><strong>{shortId(String(item.task_id))}</strong><small>{String(item.model || 'model not recorded')}</small></span><b>{money(typeof item.remaining_usd === 'number' ? item.remaining_usd : null)}</b><SupportBadge value={item.support === 'reported_or_estimated' ? 'estimated' : 'unsupported'} /></div>) : <div className={styles.emptyState}>No configured budget observations.</div>}</div></div>
          </section>
          <section className={styles.boundaryNote}><CircleDollarSign size={16} /><span>{usage?.cost_boundary} {usage?.token_boundary}</span></section>
        </>}

        {tab === 'Reliability' && <>
          <section className={styles.metricGrid}>{['retries', 'timeouts', 'cancelled', 'stale_reconciled', 'lease_expired', 'resumed', 'schedule_deduplicated'].map(key => <article key={key}><span>{key.replaceAll('_', ' ')}</span><strong>{reliability?.metrics[key] || 0}</strong><SupportBadge value={reliability?.support[key === 'stale_reconciled' ? 'stale_reconciliation' : key === 'lease_expired' ? 'lease_expiry' : key === 'schedule_deduplicated' ? 'schedule_deduplication' : key.replace(/s$/, '')]} /><Activity size={18} /></article>)}</section>
          <section className={styles.dataSection}><header><div><h2>Worker heartbeat</h2><p>Current process liveness and active task assignment.</p></div><SupportBadge value={reliability?.support.worker_heartbeat} /></header><div className={styles.tableWrap}><table><thead><tr><th>Worker</th><th>Status</th><th>Liveness</th><th>Active task</th><th>Last seen</th></tr></thead><tbody>{reliability?.workers.map((worker, index) => <tr key={String(worker.worker_id || index)}><td><code>{String(worker.worker_id || 'unknown')}</code></td><td>{String(worker.status || 'unknown')}</td><td><span className={String(worker.liveness) === 'active' ? styles.goodText : styles.badText}>{String(worker.liveness || 'unknown')}</span></td><td>{shortId(String(worker.active_task_id || ''))}</td><td>{time(String(worker.last_seen_at || ''))}</td></tr>)}</tbody></table>{!reliability?.workers.length && <div className={styles.emptyState}>No worker heartbeat has been recorded.</div>}</div></section>
          <section className={styles.dataSection}><header><div><h2>Queue semantics</h2><p>Recorded queue contract and current counts.</p></div></header><dl className={styles.definitionGrid}>{Object.entries(reliability?.queue || {}).filter(([, value]) => ['string', 'number', 'boolean'].includes(typeof value)).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>)}</dl></section>
        </>}

        {tab === 'Durable Jobs' && <>
          <section className={styles.metricGrid}>{['queued', 'active', 'retry_wait', 'paused', 'succeeded', 'failed', 'cancelled'].map(key => <article key={key}><span>{key.replaceAll('_', ' ')}</span><strong>{key === 'active' ? durableJobs?.items.filter(item => item.status === 'running').length || 0 : durableJobs?.counts[key] || 0}</strong><SupportBadge value="reported" /><CalendarClock size={18} /></article>)}</section>
          <section className={styles.dataSection} data-governance-durable-jobs="bounded-local">
            <header><div><h2>Durable schedule jobs</h2><p>Persisted lifecycle, retries, checkpoints, provenance, and alert linkage.</p></div><span>{durableJobs?.observed || 0} observed</span></header>
            <div className={styles.tableWrap}><table><thead><tr><th>Job</th><th>Application</th><th>Status</th><th>Trigger</th><th>Attempts</th><th>Updated</th><th /></tr></thead><tbody>{durableJobs?.items.map(job => <tr key={job.id}><td><strong>{shortId(job.id)}</strong><code>{job.local_date || 'manual'}</code></td><td><Link href={`/applications/${job.application_id}?tab=automation`}>{job.application_name || shortId(job.application_id)}</Link></td><td><span className={`${styles.taskStatus} ${styles[`status_${job.status}`]}`}><i />{job.status}</span></td><td>{job.trigger_kind}</td><td>{job.attempt_count}/{job.max_attempts}</td><td>{time(job.updated_at)}</td><td>{job.platform_task_id && <button className={styles.rowAction} onClick={() => void loadTrace(job.platform_task_id!)} aria-label={`Open durable job trace ${job.id}`} title="Open linked trace"><ChevronRight size={15} /></button>}</td></tr>)}</tbody></table>{!durableJobs?.items.length && <div className={styles.emptyState}>No durable jobs match this scope.</div>}</div>
          </section>
          {filters.applicationId ? <section className={styles.dataSection}><ScheduleOperationsPanel applicationId={filters.applicationId} audience="engineer" hasSchedule onAuthRequired={() => setAuthNeeded(true)} /></section> : <section className={styles.boundaryNote}><Filter size={16} /><span>Select one application to inspect exact attempts, ordered events, collection receipts, and recovery controls.</span></section>}
          <section className={styles.boundaryNote}><ShieldCheck size={16} /><span>{durableJobs?.claim_boundary}</span></section>
        </>}

        {tab === 'Connector Operations' && <div data-governance-connectors="tenant-redacted">
          <section className={styles.dataSection}>
            <header><div><h2>Governed connector scope</h2><p>Filter tenant-safe metadata and redacted operation receipts. Payloads, signatures, and secret references are excluded.</p></div><SupportBadge value={connectors?.support.writeback_receipt} /></header>
            <div className={styles.policyForm}>
              <label><span>Connector</span><select value={connectorFilters.connectorId} onChange={event => updateConnectorFilter('connectorId', event.target.value)}><option value="">All connectors</option>{connectorIds.map(item => <option key={item}>{item}</option>)}</select></label>
              <label><span>Tenant</span><select value={connectorFilters.tenantId} onChange={event => updateConnectorFilter('tenantId', event.target.value)}><option value="">All tenants</option>{connectorTenantIds.map(item => <option key={item}>{item}</option>)}</select></label>
              <label><span>Operation</span><select value={connectorFilters.operationId} onChange={event => updateConnectorFilter('operationId', event.target.value)}><option value="">All operations</option>{connectorOperationIds.map(item => <option key={item}>{item}</option>)}</select></label>
              <label><span>Execution status</span><select value={connectorFilters.status} onChange={event => updateConnectorFilter('status', event.target.value)}><option value="">All statuses</option>{['dry_run', 'executing', 'succeeded', 'failed', 'compensated'].map(item => <option key={item}>{item}</option>)}</select></label>
              <label><span>Emergency stop</span><select value={connectorFilters.emergencyStop} onChange={event => updateConnectorFilter('emergencyStop', event.target.value as ConnectorFilters['emergencyStop'])}><option value="">Any policy state</option><option value="true">Enabled</option><option value="false">Disabled</option></select></label>
            </div>
          </section>

          <section className={styles.metricGrid}>
            {['dry_run', 'executing', 'succeeded', 'failed', 'compensated'].map(status => <article key={status}><span>{status.replaceAll('_', ' ')}</span><strong>{connectors?.counts[status] || 0}</strong><small>redacted receipts in page</small><PlugZap size={18} /></article>)}
            <article><span>Exercises</span><strong>{connectors?.exercises.length || 0}</strong><small>stop and compensation evidence</small><ShieldCheck size={18} /></article>
          </section>

          <section className={styles.dataSection}>
            <header><div><h2>Execution receipts</h2><p>Operational outcome, side-effect state, callback state, and compensability without business payload disclosure.</p></div><span>{connectors?.items.length || 0} on this page</span></header>
            <div className={styles.tableWrap}><table><thead><tr><th>Execution</th><th>Tenant / connector</th><th>Operation</th><th>Status</th><th>Side effect</th><th>Callback</th><th>Updated</th></tr></thead><tbody>{connectors?.items.map(item => <tr key={item.execution_id}><td><strong>{shortId(item.execution_id)}</strong><code>{item.profile_id}</code></td><td><span>{item.tenant_id}</span><small>{item.connector_id}@{item.connector_version}</small></td><td>{item.operation_id}</td><td><span className={`${styles.taskStatus} ${styles[`status_${item.status}`]}`}><i />{item.status}</span></td><td>{item.side_effect_state}{item.compensation_available && <small> · compensable</small>}</td><td>{item.callback_status || 'not requested'}</td><td>{time(item.updated_at)}</td></tr>)}</tbody></table>{!connectors?.items.length && <div className={styles.emptyState}>No connector receipts match this scope.</div>}</div>
            {connectors && (connectors.offset > 0 || connectors.has_more) && <footer className={styles.supportFooter}><span>Offset {connectors.offset}</span><span><button className={styles.rowAction} disabled={connectors.offset === 0 || loading} onClick={() => setConnectorOffset(Math.max(0, connectors.offset - connectors.limit))} aria-label="Previous connector receipt page" title="Previous connector receipt page"><ChevronLeft size={15} /></button><strong>{connectors.items.length} records</strong><button className={styles.rowAction} disabled={!connectors.has_more || loading} onClick={() => setConnectorOffset(connectors.offset + connectors.limit)} aria-label="Next connector receipt page" title="Next connector receipt page"><ChevronRight size={15} /></button></span></footer>}
          </section>

          <section className={styles.splitSections}>
            <div className={styles.dataSection}><header><div><h2>Versioned contracts</h2><p>Immutable connector versions and profile claim ceilings.</p></div><SupportBadge value={connectors?.support.schema_contract} /></header><div className={styles.compactList}>{connectors?.manifests.map(item => <div key={`${String(item.connector_id)}-${String(item.version)}`}><span><strong>{String(item.title)}</strong><small>{String(item.connector_id)}@{String(item.version)} · {String(item.domain)}</small></span><b>{Array.isArray(item.operations) ? item.operations.length : 0} ops</b></div>)}{!connectors?.manifests.length && <div className={styles.emptyState}>No connector contracts in this scope.</div>}</div></div>
            <div className={styles.dataSection}><header><div><h2>Tenant bindings</h2><p>Mapped subjects and allowed operation counts only.</p></div><SupportBadge value={connectors?.support.tenant_identity} /></header><div className={styles.compactList}>{connectors?.bindings.map((item, index) => <div key={`${String(item.connector_id)}-${String(item.tenant_id)}-${index}`}><span><strong>{String(item.tenant_id)}</strong><small>{String(item.connector_id)}@{String(item.connector_version)} · profile {String(item.profile_id)}</small></span><b>{String(item.subject_count)} subjects</b><em>{item.enabled ? 'enabled' : 'disabled'}</em></div>)}{!connectors?.bindings.length && <div className={styles.emptyState}>No tenant bindings in this scope.</div>}</div></div>
          </section>

          <section className={styles.splitSections}>
            <div className={styles.dataSection}><header><div><h2>Writeback policies</h2><p>Revisioned authorization and emergency-stop state.</p></div><SupportBadge value={connectors?.support.policy} /></header><div className={styles.compactList}>{connectors?.policies.map((item, index) => <div key={`${String(item.connector_id)}-${String(item.tenant_id)}-${index}`}><span><strong>{String(item.tenant_id)} · {String(item.domain)}</strong><small>revision {String(item.revision)} · preauthorization {item.mutation_preauthorization_required ? 'required' : 'optional'}</small></span><b className={item.emergency_stop ? styles.badText : styles.goodText}>{item.emergency_stop ? 'STOPPED' : 'active'}</b></div>)}{!connectors?.policies.length && <div className={styles.emptyState}>No policies in this scope.</div>}</div></div>
            <div className={styles.dataSection}><header><div><h2>Control exercises</h2><p>Bounded evidence for emergency-stop denial and explicit compensation.</p></div><SupportBadge value={connectors?.support.compensation_exercise} /></header><div className={styles.compactList}>{connectors?.exercises.map(item => <div key={item.id}><span><strong>{item.kind.replaceAll('_', ' ')}</strong><small>{item.tenant_id} · {time(item.created_at)}</small></span><b className={item.status === 'passed' ? styles.goodText : styles.badText}>{item.status}</b><em>{item.evidence_level}</em></div>)}{!connectors?.exercises.length && <div className={styles.emptyState}>No control exercises recorded.</div>}</div></div>
          </section>
          <section className={styles.supportMatrix}><h2>Evidence support</h2>{Object.entries(connectors?.support || {}).map(([key, value]) => <div key={key}><span>{key.replaceAll('_', ' ')}</span><SupportBadge value={value} /></div>)}</section>
          <section className={styles.boundaryNote}><ShieldCheck size={16} /><span>{connectors?.claim_boundary}</span></section>
        </div>}

        {tab === 'Policy & Risk' && <>
          <section className={styles.policyLayout}>
            <div className={styles.dataSection}><header><div><h2>Active controls</h2><p>Changes are audited. Restart persistence is shown separately.</p></div><SupportBadge value={policy?.support.current_controls} /></header><div className={styles.policyForm}>
              <label><span>Network egress</span><select value={policyDraft.network} onChange={event => setPolicyDraft(current => ({ ...current, network: event.target.value }))}><option value="full">Full</option><option value="allowlist">Allowlist</option><option value="none">None</option></select></label>
              <label><span>Cancellation</span><select value={policyDraft.cancellation} onChange={event => setPolicyDraft(current => ({ ...current, cancellation: event.target.value }))}><option value="enabled">Enabled</option><option value="disabled">Disabled</option></select></label>
              <label className={styles.toggleLabel}><span>Secret policy</span><input type="checkbox" checked={policyDraft.secret} onChange={event => setPolicyDraft(current => ({ ...current, secret: event.target.checked }))} /></label>
              <label className={styles.reasonField}><span>Change reason</span><input value={policyReason} onChange={event => setPolicyReason(event.target.value)} /></label>
              <button className={styles.primaryButton} disabled={policySaving || !policyReason.trim()} onClick={() => void savePolicy()}><ShieldCheck size={16} />{policySaving ? 'Saving' : 'Apply policy'}</button>
            </div></div>
            <div className={styles.dataSection}><header><div><h2>Boundary status</h2><p>Controls with incomplete evidence remain explicit.</p></div></header><dl className={styles.definitionGrid}><div><dt>Worker lease</dt><dd>{policy?.controls.worker_lease_seconds ?? 0}s</dd></div><div><dt>Network allowlist</dt><dd>{policy?.controls.network_egress_allowlist.length || 0} hosts</dd></div><div><dt>Restart persistence</dt><dd><SupportBadge value={policy?.support.restart_persistence} /></dd></div><div><dt>Secret policy</dt><dd>{policy?.controls.secret_policy_enabled ? 'Enabled' : 'Disabled'}</dd></div></dl></div>
          </section>
          <section className={styles.dataSection}><header><div><h2>Policy change audit</h2><p>Actor-independent server audit of mutable controls.</p></div><SupportBadge value={policy?.support.change_audit} /></header><div className={styles.auditList}>{policy?.audit.length ? policy.audit.map(item => <article key={item.id}><ShieldCheck size={15} /><div><strong>{item.type}</strong><span>{String((item.data.audit as Record<string, unknown> | undefined)?.reason || 'No reason recorded')}</span></div><time>{time(item.created_at)}</time></article>) : <div className={styles.emptyState}>No policy changes recorded in this process.</div>}</div></section>
        </>}

        {tab === 'Trace Explorer' && <section className={styles.traceLayout}>
          <div className={styles.dataSection}><header><div><h2>Task graph</h2><p>Select any platform task from Overview to inspect its full parent-child tree.</p></div>{traceLoading && <LoaderCircle className={styles.spin} size={17} />}</header>{trace ? <div className={styles.traceTree}><TraceBranch node={trace.tree} selected={selectedTaskId} onSelect={setSelectedTaskId} /></div> : <div className={styles.emptyState}>No trace selected. Open a task from Overview.</div>}</div>
          <div className={styles.dataSection}><header><div><h2>Span timeline</h2><p>Task transitions, node/tool usage, model usage, and recorded events.</p></div><span>{trace?.spans.length || 0} spans</span></header>{selectedTask && <div className={styles.traceContext}><TaskStatus task={selectedTask} /><code>{selectedTask.id}</code></div>}<div className={styles.spanList}>{trace?.spans.filter(span => !selectedTaskId || span.task_id === selectedTaskId).map((span, index) => <article key={`${String(span.created_at)}-${index}`}><i /><time>{time(String(span.created_at || ''))}</time><div><strong>{String(span.event_type || span.span_type || 'span')}</strong><small>{shortId(String(span.task_id || ''))}</small></div><details><summary>Evidence</summary><pre>{JSON.stringify(span.metadata || {}, null, 2)}</pre></details></article>)}</div>{trace && !trace.spans.length && <div className={styles.emptyState}>This trace has no recorded spans.</div>}{trace?.durable_job && <details open><summary>Linked durable job evidence</summary><pre>{JSON.stringify(trace.durable_job, null, 2)}</pre></details>}</div>
        </section>}

        {tab === 'Capability Evidence' && <>
          <section className={styles.dataSection}><header><div><h2>Capability claims</h2><p>Strongest intact claim per stable capability ID.</p></div><span>{evidence?.capabilities.length || 0} capabilities</span></header><div className={styles.evidenceGrid}>{evidence?.capabilities.map(capability => <article key={capability.capability_id}><div className={styles.evidenceHead}><FileCheck2 size={17} /><span><strong>{capability.capability_id}</strong><small>{capability.evidence_level} · {capability.claim_count} claim records</small></span><b className={capability.integrity === 'intact' ? styles.goodText : styles.badText}>{capability.integrity}</b></div><div className={styles.evidenceStatus}><span>{capability.strongest_status.replaceAll('_', ' ')}</span>{capability.artifact_categories.map(category => <code key={category}>{category}</code>)}</div>{capability.known_gaps.length > 0 && <div className={styles.gapList}>{capability.known_gaps.map((gap, index) => <p key={index}><TriangleAlert size={13} />{String(gap.field || 'gap')}: {String(gap.reason || '')}</p>)}</div>}</article>)}</div></section>
          <section className={styles.supportMatrix}><h2>Evidence coverage</h2>{Object.entries(evidence?.support || {}).map(([key, value]) => <div key={key}><span>{key.replaceAll('_', ' ')}</span><SupportBadge value={value} /></div>)}</section>
          <section className={styles.boundaryNote}><FileCheck2 size={16} /><span>{evidence?.claim_boundary}</span></section>
        </>}

        {tab === 'Alerts & Incidents' && <section className={styles.dataSection}><header><div><h2>Active observations</h2><p>Deterministic local detectors. Unsupported production incident delivery is not implied.</p></div><span>{alerts?.total || 0} open</span></header><div className={styles.alertList}>{alerts?.items.length ? alerts.items.map((alert: GovernanceAlert) => <article className={styles[`severity_${alert.severity}`]} key={alert.id}><AlertTriangle size={17} /><div><span>{alert.detector.replaceAll('_', ' ')}</span><strong>{alert.message}</strong><small>{alert.application_id || alert.worker_id || alert.owner_id || 'platform'} · {alert.source}</small></div><time>{time(alert.source_timestamp)}</time>{alert.task_id && <button className={styles.rowAction} onClick={() => void loadTrace(alert.task_id!)} aria-label="Open incident trace"><ChevronRight size={15} /></button>}</article>) : <div className={styles.emptyState}><ShieldCheck size={22} />No active local observations in this scope.</div>}</div><footer className={styles.supportFooter}>{Object.entries(alerts?.support || {}).map(([key, value]) => <span key={key}>{key.replaceAll('_', ' ')} <SupportBadge value={value} /></span>)}</footer></section>}
      </div>}
    </section>
  </main>
}

export default function GovernanceConsolePage() {
  return <Suspense fallback={<main className={styles.shell} data-governance-console="true"><div className={styles.loadingState}><LoaderCircle className={styles.spin} size={24} /><strong>Loading governance console</strong></div></main>}>
    <GovernanceConsolePageContent />
  </Suspense>
}
