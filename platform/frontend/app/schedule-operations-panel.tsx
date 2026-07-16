'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  CalendarClock,
  Check,
  CircleAlert,
  Clock3,
  FileText,
  History,
  LoaderCircle,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Square,
} from 'lucide-react'
import {
  api,
  idempotency,
  isAuthError,
  type CollectionReceipt,
  type DurableJob,
  type DurableJobEvent,
  type ScheduleStatus,
} from '@/lib/platform'
import { MarkdownResultCard } from '@/lib/markdown'
import styles from './schedule-operations.module.css'


type Audience = 'engineer' | 'customer'
type Locale = 'zh' | 'en'

type Props = {
  applicationId: string
  audience: Audience
  hasSchedule: boolean
  locale?: Locale
  onAuthRequired?: () => void
}

const ACTIVE = new Set(['queued', 'running', 'retry_wait', 'paused'])

function dateTime(value: string | null | undefined, locale: Locale) {
  if (!value) return locale === 'zh' ? '尚无记录' : 'Not recorded'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(locale === 'zh' ? 'zh-CN' : 'en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function statusLabel(status: DurableJob['status'] | undefined, locale: Locale) {
  const labels: Record<DurableJob['status'], [string, string]> = {
    queued: ['等待开始', 'Queued'],
    running: ['正在运行', 'Running'],
    retry_wait: ['等待重试', 'Retry scheduled'],
    paused: ['等待继续', 'Paused'],
    succeeded: ['已完成', 'Succeeded'],
    failed: ['需要处理', 'Failed'],
    cancelled: ['已停止', 'Cancelled'],
  }
  if (!status) return locale === 'zh' ? '尚未运行' : 'Not run'
  return labels[status][locale === 'zh' ? 0 : 1]
}

function longestText(value: unknown, depth = 0): string {
  if (depth > 5) return ''
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    return value.map(item => longestText(item, depth + 1)).sort((a, b) => b.length - a.length)[0] || ''
  }
  if (!value || typeof value !== 'object') return ''
  return Object.values(value as Record<string, unknown>)
    .map(item => longestText(item, depth + 1))
    .sort((a, b) => b.length - a.length)[0] || ''
}

export function ScheduleOperationsPanel({
  applicationId,
  audience,
  hasSchedule,
  locale = 'zh',
  onAuthRequired,
}: Props) {
  const [schedule, setSchedule] = useState<ScheduleStatus | null>(null)
  const [jobs, setJobs] = useState<DurableJob[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [selected, setSelected] = useState<DurableJob | null>(null)
  const [events, setEvents] = useState<DurableJobEvent[]>([])
  const [receipts, setReceipts] = useState<CollectionReceipt[]>([])
  const [loading, setLoading] = useState(hasSchedule)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [publishedScheduleMissing, setPublishedScheduleMissing] = useState(false)

  const loadJob = useCallback(async (jobId: string) => {
    if (!jobId) {
      setSelected(null)
      setEvents([])
      setReceipts([])
      return
    }
    const [job, nextEvents, nextReceipts] = await Promise.all([
      api<DurableJob>(`/api/v1/durable-jobs/${encodeURIComponent(jobId)}`),
      api<DurableJobEvent[]>(`/api/v1/durable-jobs/${encodeURIComponent(jobId)}/events`),
      api<CollectionReceipt[]>(`/api/v1/durable-jobs/${encodeURIComponent(jobId)}/receipts`),
    ])
    setSelected(job)
    setEvents(nextEvents)
    setReceipts(nextReceipts)
  }, [])

  const load = useCallback(async (quiet = false) => {
    if (!hasSchedule) {
      setLoading(false)
      return
    }
    if (!quiet) setLoading(true)
    setError('')
    try {
      const [nextSchedule, nextJobs] = await Promise.all([
        api<ScheduleStatus>(`/api/v1/applications/${applicationId}/schedule-status`),
        api<DurableJob[]>(`/api/v1/applications/${applicationId}/durable-jobs?limit=50`),
      ])
      setSchedule(nextSchedule)
      setJobs(nextJobs)
      setPublishedScheduleMissing(false)
      const nextSelected = nextJobs.some(item => item.id === selectedId) ? selectedId : nextJobs[0]?.id || ''
      setSelectedId(nextSelected)
      await loadJob(nextSelected)
    } catch (caught) {
      if (isAuthError(caught)) onAuthRequired?.()
      else if (String(caught).includes('404')) setPublishedScheduleMissing(true)
      else setError(String(caught).replace(/^Error:\s*/, ''))
    } finally {
      setLoading(false)
    }
  }, [applicationId, hasSchedule, loadJob, onAuthRequired, selectedId])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    const active = jobs.some(job => ACTIVE.has(job.status))
    if (!active) return
    const timer = window.setInterval(() => void load(true), 2000)
    return () => window.clearInterval(timer)
  }, [jobs, load])

  async function selectJob(jobId: string) {
    setSelectedId(jobId)
    setError('')
    try {
      await loadJob(jobId)
    } catch (caught) {
      setError(String(caught).replace(/^Error:\s*/, ''))
    }
  }

  async function runNow() {
    setBusy('run')
    setError('')
    try {
      await api(`/api/v1/applications/${applicationId}/schedules/trigger`, {
        method: 'POST',
        body: JSON.stringify({ inputs: {}, idempotency_key: idempotency() }),
      })
      await load(true)
    } catch (caught) {
      if (isAuthError(caught)) onAuthRequired?.()
      else setError(String(caught).replace(/^Error:\s*/, ''))
    } finally {
      setBusy('')
    }
  }

  async function jobAction(action: 'retry' | 'resume' | 'cancel') {
    if (!selected) return
    setBusy(action)
    setError('')
    try {
      await api(`/api/v1/durable-jobs/${encodeURIComponent(selected.id)}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ expected_revision: selected.revision }),
      })
      await load(true)
    } catch (caught) {
      if (isAuthError(caught)) onAuthRequired?.()
      else setError(String(caught).replace(/^Error:\s*/, ''))
    } finally {
      setBusy('')
    }
  }

  const latest = selected || schedule?.latest_job || jobs[0] || null
  const digest = useMemo(() => longestText(latest?.result), [latest?.result])
  const scheduledAt = schedule?.schedule.next_fire_at
  const active = Boolean(latest && ACTIVE.has(latest.status))

  if (!hasSchedule) {
    return <section className={`${styles.root} ${styles[audience]}`} data-automation-state="not-configured">
      <div className={styles.empty}><CalendarClock size={22} /><strong>{locale === 'zh' ? '尚未配置定时运行' : 'No schedule configured'}</strong><span>{locale === 'zh' ? '添加并发布“定时触发”积木后，这里会显示运行计划和历史。' : 'Add and publish a schedule trigger to manage automation here.'}</span></div>
    </section>
  }

  if (loading && !schedule) {
    return <section className={`${styles.root} ${styles[audience]}`} data-automation-state="loading"><div className={styles.empty}><LoaderCircle className={styles.spin} size={22} /><strong>{locale === 'zh' ? '正在读取自动化状态' : 'Loading automation'}</strong></div></section>
  }

  if (publishedScheduleMissing) {
    return <section className={`${styles.root} ${styles[audience]}`} data-automation-state="unpublished">
      <div className={styles.empty}><Pause size={22} /><strong>{locale === 'zh' ? '定时配置尚未生效' : 'Schedule is not active'}</strong><span>{locale === 'zh' ? '草稿里已有定时触发积木。发布包含该积木的版本后才能运行。' : 'Publish the draft containing the schedule trigger to activate it.'}</span></div>
    </section>
  }

  return <section className={`${styles.root} ${styles[audience]}`} data-automation-audience={audience} data-automation-state={latest?.status || 'empty'}>
    <header className={styles.header}>
      <div><span>{audience === 'engineer' ? 'Automation workspace' : locale === 'zh' ? '自动运行' : 'Scheduled run'}</span><h2>{locale === 'zh' ? '定时工作流' : 'Scheduled workflow'}</h2><p>{audience === 'engineer' ? 'Durable lifecycle, attempts, events, receipts, and recovery controls.' : locale === 'zh' ? '工作流会按计划运行，你也可以在需要时立即执行一次。' : 'This workflow runs on schedule and can also be started now.'}</p></div>
      <div className={styles.headerActions}>
        <button aria-label={locale === 'zh' ? '刷新' : 'Refresh'} disabled={Boolean(busy)} onClick={() => void load()} title={locale === 'zh' ? '刷新状态' : 'Refresh status'} type="button"><RefreshCw className={loading ? styles.spin : ''} size={15} /></button>
        <button className={styles.primary} disabled={Boolean(busy) || active} onClick={() => void runNow()} type="button"><Play size={15} fill="currentColor" />{busy === 'run' ? (locale === 'zh' ? '正在启动' : 'Starting') : locale === 'zh' ? '立即运行' : 'Run now'}</button>
      </div>
    </header>

    {error && <div className={styles.error} role="alert"><CircleAlert size={16} /><span>{error}</span></div>}

    <dl className={styles.scheduleFacts}>
      <div><dt><CalendarClock size={14} />{locale === 'zh' ? '下一次' : 'Next run'}</dt><dd>{dateTime(scheduledAt, locale)}</dd></div>
      <div><dt><Clock3 size={14} />{locale === 'zh' ? '运行时间' : 'Schedule'}</dt><dd>{String(schedule?.schedule.hour ?? 0).padStart(2, '0')}:{String(schedule?.schedule.minute ?? 0).padStart(2, '0')} · {schedule?.schedule.timezone}</dd></div>
      <div><dt><History size={14} />{locale === 'zh' ? '最近状态' : 'Latest status'}</dt><dd><span className={styles[`status_${latest?.status || 'empty'}`]}>{statusLabel(latest?.status, locale)}</span></dd></div>
    </dl>

    {audience === 'customer' ? <div className={styles.customerBody} data-customer-schedule-view="bounded">
      {!latest ? <div className={styles.empty}><CalendarClock size={21} /><strong>{locale === 'zh' ? '还没有运行记录' : 'No runs yet'}</strong><span>{locale === 'zh' ? '等待下一次计划时间，或点击“立即运行”。' : 'Wait for the next schedule or choose Run now.'}</span></div> : <>
        <section className={styles.customerProgress}>
          <div><span className={styles.statusDot} /><div><strong>{statusLabel(latest.status, locale)}</strong><small>{latest.status === 'retry_wait' ? (locale === 'zh' ? `将在 ${dateTime(latest.next_attempt_at, locale)} 自动重试` : `Retry at ${dateTime(latest.next_attempt_at, locale)}`) : latest.status === 'failed' ? (locale === 'zh' ? '本次没有完成，可以再次尝试。' : 'This run did not finish and can be tried again.') : latest.status === 'succeeded' ? (locale === 'zh' ? `完成于 ${dateTime(latest.finished_at, locale)}` : `Finished ${dateTime(latest.finished_at, locale)}`) : locale === 'zh' ? '状态变化会自动刷新。' : 'Status updates automatically.'}</small></div></div>
          <div className={styles.recoveryActions}>
            {(latest.status === 'failed' || latest.status === 'cancelled') && <button disabled={Boolean(busy)} onClick={() => void jobAction('retry')} type="button"><RotateCcw size={14} />{locale === 'zh' ? '再次尝试' : 'Try again'}</button>}
            {(latest.status === 'paused' || latest.status === 'retry_wait') && <button disabled={Boolean(busy)} onClick={() => void jobAction('resume')} type="button"><Play size={14} />{locale === 'zh' ? '现在继续' : 'Continue now'}</button>}
            {(latest.status === 'queued' || latest.status === 'running') && <button disabled={Boolean(busy)} onClick={() => void jobAction('cancel')} type="button"><Square size={13} fill="currentColor" />{locale === 'zh' ? '停止本次运行' : 'Stop this run'}</button>}
          </div>
        </section>

        <section className={styles.provenance}>
          <div className={styles.sectionTitle}><ShieldCheck size={16} /><div><strong>{locale === 'zh' ? '本次来源' : 'Sources used'}</strong><small>{locale === 'zh' ? '只显示实际访问并留下记录的来源。' : 'Only sources with a recorded collection receipt are shown.'}</small></div><span>{receipts.length}</span></div>
          {receipts.length ? <ul>{receipts.map(receipt => <li key={receipt.id}><span className={styles[`receipt_${receipt.status}`]}>{receipt.status}</span><a href={receipt.canonical_url || receipt.requested_url} rel="noreferrer" target="_blank">{receipt.title || receipt.host || receipt.requested_url}</a><small>{dateTime(receipt.collected_at, locale)}</small>{receipt.error && <p>{receipt.error}</p>}</li>)}</ul> : <div className={styles.inlineEmpty}>{locale === 'zh' ? '本次运行尚无来源记录。' : 'No source record for this run yet.'}</div>}
        </section>

        <section className={styles.digest}>
          <div className={styles.sectionTitle}><FileText size={16} /><div><strong>{locale === 'zh' ? '最新结果' : 'Latest result'}</strong><small>{locale === 'zh' ? '完成后会在这里显示可读摘要。' : 'A readable digest appears here when complete.'}</small></div></div>
          {digest ? <MarkdownResultCard source={digest} emptyLabel={locale === 'zh' ? '暂无结果' : 'No result'} title={locale === 'zh' ? '运行结果' : 'Run result'} description={locale === 'zh' ? '已按可读格式整理' : 'Readable digest'} openLabel={locale === 'zh' ? '展开阅读' : 'Open'} closeLabel={locale === 'zh' ? '关闭' : 'Close'} dataSurface="scheduled-customer-result" /> : <div className={styles.inlineEmpty}>{active ? (locale === 'zh' ? '正在生成结果。' : 'Generating the result.') : locale === 'zh' ? '这次运行没有生成可展示的摘要。' : 'No readable digest was produced.'}</div>}
        </section>
      </>}
    </div> : <div className={styles.engineerBody} data-engineer-automation-workspace="true">
      <div className={styles.jobList}>
        <div className={styles.sectionTitle}><History size={16} /><div><strong>Job history</strong><small>Immutable attempts remain available after recovery actions.</small></div><span>{jobs.length}</span></div>
        {jobs.length ? jobs.map(job => <button aria-pressed={job.id === selectedId} className={job.id === selectedId ? styles.selectedJob : ''} key={job.id} onClick={() => void selectJob(job.id)} type="button"><span className={styles[`status_${job.status}`]}>{job.status}</span><strong>{job.local_date || job.trigger_kind}</strong><small>{job.trigger_kind} · attempt {job.attempt_count}/{job.max_attempts}</small></button>) : <div className={styles.inlineEmpty}>No durable jobs have been enqueued.</div>}
      </div>

      {selected && <div className={styles.engineerDetail}>
        <div className={styles.technicalFacts}><span><b>Job</b><code>{selected.id}</code></span><span><b>Run</b><code>{selected.run_id || 'not attached'}</code></span><span><b>Worker</b><code>{selected.lease_owner || 'none'}</code></span><span><b>Fence</b><code>v{selected.lease_version}</code></span><span><b>Lease</b><code>{dateTime(selected.lease_expires_at, 'en')}</code></span><span><b>Revision</b><code>r{selected.revision}</code></span></div>
        {selected.alert && <div className={styles.error}><CircleAlert size={16} /><span>{String(selected.alert.message || selected.error || 'Durable job alert')}</span></div>}
        <div className={styles.recoveryActions}>
          {(selected.status === 'failed' || selected.status === 'cancelled') && <button disabled={Boolean(busy)} onClick={() => void jobAction('retry')} type="button"><RotateCcw size={14} />Retry</button>}
          {(selected.status === 'paused' || selected.status === 'retry_wait') && <button disabled={Boolean(busy)} onClick={() => void jobAction('resume')} type="button"><Play size={14} />Resume now</button>}
          {(selected.status === 'queued' || selected.status === 'running') && <button disabled={Boolean(busy)} onClick={() => void jobAction('cancel')} type="button"><Square size={13} />Cancel</button>}
        </div>

        <section className={styles.evidenceSection}><div className={styles.sectionTitle}><History size={16} /><div><strong>Attempts</strong><small>Worker, fence, run, terminal evidence.</small></div><span>{selected.attempts?.length || 0}</span></div><div className={styles.tableWrap}><table><thead><tr><th>#</th><th>Status</th><th>Worker / fence</th><th>Run</th><th>Started</th></tr></thead><tbody>{selected.attempts?.map(attempt => <tr key={attempt.attempt_number}><td>{attempt.attempt_number}</td><td>{attempt.status}</td><td><code>{attempt.worker_id}</code><small>v{attempt.lease_version}</small></td><td><code>{attempt.run_id || '—'}</code></td><td>{dateTime(attempt.started_at, 'en')}</td></tr>)}</tbody></table></div></section>
        <section className={styles.evidenceSection}><div className={styles.sectionTitle}><Clock3 size={16} /><div><strong>Ordered events</strong><small>Persisted lifecycle and collection transitions.</small></div><span>{events.length}</span></div><ol className={styles.eventList}>{events.map(event => <li key={event.sequence}><time>{dateTime(event.created_at, 'en')}</time><strong>{event.event_type}</strong><details><summary>Payload</summary><pre>{JSON.stringify(event.data, null, 2)}</pre></details></li>)}</ol></section>
        <section className={styles.evidenceSection}><div className={styles.sectionTitle}><ShieldCheck size={16} /><div><strong>Provenance receipts</strong><small>Permission basis, robots decision, content hash, and source outcome.</small></div><span>{receipts.length}</span></div><div className={styles.receiptList}>{receipts.map(receipt => <article key={receipt.id}><div><span className={styles[`receipt_${receipt.status}`]}>{receipt.status}</span><strong>{receipt.title || receipt.canonical_url}</strong></div><a href={receipt.canonical_url || receipt.requested_url} rel="noreferrer" target="_blank">{receipt.canonical_url || receipt.requested_url}</a><dl><div><dt>Permission</dt><dd>{receipt.permission_basis}</dd></div><div><dt>Robots</dt><dd>{receipt.robots_checked ? String(receipt.robots_allowed) : 'not checked'}</dd></div><div><dt>Hash</dt><dd><code>{receipt.content_hash || '—'}</code></dd></div></dl>{receipt.error && <p>{receipt.error}</p>}</article>)}</div></section>
      </div>}
      <footer className={styles.boundary}><ShieldCheck size={15} /><span>Local H3 durable execution and controlled allowlisted collection. No production SLO, distributed failover, arbitrary-site permission, or external notification claim.</span></footer>
    </div>}
  </section>
}
