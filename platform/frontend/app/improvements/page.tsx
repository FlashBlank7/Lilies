'use client'

import Link from 'next/link'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { api } from '@/lib/platform'
import { useAccount } from '../components/AuthBoundary'
import base from '../projects/projects.module.css'
import styles from './improvements.module.css'

type Kind = 'repeated_failure' | 'recovery' | 'reusable_method' | 'operation_error'
type Status = 'new' | 'working' | 'dismissed'
type Task = { id: string; status: string; created_at: string; updated_at: string; revision: number | null; purpose: string; error_kind: string }
type Improvement = {
  id: string; kind: Kind; project_id: string; workflow_id: string; project_name: string; workflow_name: string
  title: string; explanation: string; limitation: string; next_step: string; count: number; tasks: Task[]
  operations?: { id: string; created: number | string; feature: string; outcome: string; resource_id: string }[]
  workflow_changed?: boolean | null; inputs_changed?: boolean | null; status: Status; active?: boolean
  handoff?: { conversation_id: string; status: string; error: string }
}
type Report = { items: Improvement[]; last_scan: string | number | null; error: string; automatic_error?: string; sampled_tasks: number; truncated: boolean; window_days: number; limit: number; notes: string[] }
type AutomationSettings = {
  enabled: boolean; project_ids: string[]; daily_limit: number
  projects: { id: string; name: string; available: boolean; reason: string }[]; error: string
}

const endpoint = '/api/v1/admin/improvements'
const kindNames: Record<Kind, string> = { repeated_failure: '反复失败', recovery: '失败后成功', reusable_method: '重复成功', operation_error: '操作报错' }
const statusNames: Record<Status, string> = { new: '待处理', working: '处理中', dismissed: '已忽略' }
const resultNames: Record<string, string> = { succeeded: '成功', completed: '已完成', failed: '失败', error: '失败', interrupted: '已中断', cancelled: '已取消', queued: '排队中', running: '运行中', waiting_input: '等待补充', idle: '本轮已结束', submitted: '已提交' }
const featureNames: Record<string, string> = { chat: '发送对话', generate: '生成工作流', edit: '编辑工作流', upload: '上传资料', train: '训练模型', predict: '运行预测', download: '下载文件', save_method: '保存方法', reuse: '复用方法', share: '分享方法', workflow_run: '运行工作流', chat_result: '对话处理', generation_result: '工作流生成' }
const errorNames: Record<string, string> = { timeout: '请求超时', validation: '输入或配置有误', permission: '权限不足', resource: '资源不可用', model: '模型请求失败', unknown: '运行报错' }
const knownErrors = new Set(['缺少输入或资源', '执行超时', '模型或连接问题', '计算环境问题', '数据格式问题', '运行错误'])

function time(value: string | number | null) {
  if (!value) return '尚未整理'
  const date = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false })
}

export default function ImprovementsPage() {
  const user = useAccount()
  const router = useRouter()
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [status, setStatus] = useState<Status | 'all'>('all')
  const [kind, setKind] = useState<Kind | 'all'>('all')
  const [settings, setSettings] = useState<AutomationSettings | null>(null)
  const [settingsLoading, setSettingsLoading] = useState(true)
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [settingsError, setSettingsError] = useState('')
  const [settingsMessage, setSettingsMessage] = useState('')
  const [settingsAttempt, setSettingsAttempt] = useState(0)
  const savingSettings = useRef(false)
  const reading = useRef(false)
  const mutating = useRef(false)
  const generation = useRef(0)
  const admin = user?.role === 'admin'

  const refresh = useCallback(async () => {
    if (!admin) return
    reading.current = true
    const current = ++generation.current
    try {
      const result = await api<Report>(endpoint)
      if (current === generation.current) { setReport(result); setError('') }
    } catch (cause) { if (current === generation.current) setError(String(cause)) }
    finally { if (current === generation.current) { reading.current = false; setLoading(false) } }
  }, [admin])

  useEffect(() => {
    if (!admin) return
    void refresh()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible' && !mutating.current && !reading.current) void refresh()
    }, 30_000)
    return () => { window.clearInterval(timer); generation.current += 1 }
  }, [admin, refresh, user?.id])

  useEffect(() => {
    if (!admin) return
    let current = true
    setSettingsLoading(true); setSettingsError('')
    void api<AutomationSettings>(endpoint + '/settings').then(result => {
      if (current) { setSettings(result); setSettingsError(result.error || '') }
    }).catch(cause => { if (current) setSettingsError(String(cause)) })
      .finally(() => { if (current) setSettingsLoading(false) })
    return () => { current = false }
  }, [admin, user?.id, settingsAttempt])

  function editSettings(value: Partial<AutomationSettings>) {
    setSettings(previous => previous && { ...previous, ...value })
    setSettingsMessage(''); setSettingsError('')
  }

  async function saveSettings() {
    if (!admin || !settings || savingSettings.current) return
    setSettingsError(''); setSettingsMessage('')
    const projectIds = settings.project_ids.filter(id => settings.projects.some(project => project.id === id && project.available))
    if (settings.enabled && !projectIds.length) { setSettingsError('开启自动处理前，请至少选择一个可用项目。'); return }
    if (!Number.isInteger(settings.daily_limit) || settings.daily_limit < 1 || settings.daily_limit > 10) {
      setSettingsError('过去 24 小时的处理上限需为 1 到 10 条。'); return
    }
    savingSettings.current = true; setSettingsSaving(true)
    try {
      const result = await api<AutomationSettings>(endpoint + '/settings', { method: 'PUT', body: JSON.stringify({ enabled: settings.enabled, project_ids: projectIds, daily_limit: settings.daily_limit }) })
      setSettings(result); setSettingsError(result.error || '')
      if (!result.error) setSettingsMessage(result.enabled ? '自动处理设置已保存，后台稍后会处理符合条件的线索。' : '自动处理已关闭，不会再启动新的处理会话。')
    } catch (cause) { setSettingsError(String(cause)) }
    finally { savingSettings.current = false; setSettingsSaving(false) }
  }

  async function change(path: string, method: string, body?: object) {
    if (!admin || mutating.current) return
    mutating.current = true; setBusy(path); setError('')
    try {
      await api(endpoint + path, { method, ...(body ? { body: JSON.stringify(body) } : {}) })
      await refresh()
    } catch (cause) { setError(String(cause)) }
    finally { mutating.current = false; setBusy('') }
  }

  async function start(item: Improvement) {
    if (!admin || !user || mutating.current) return
    mutating.current = true; setBusy('/' + item.id + '/start'); setError('')
    try {
      const result = await api<{ project_id: string; conversation_id: string; status: string }>(endpoint + '/' + item.id + '/start', { method: 'POST' })
      sessionStorage.setItem(`lilies:user:${user.id}:project:${result.project_id}:conversation`, result.conversation_id)
      router.push('/projects/' + result.project_id)
    } catch (cause) { setError(String(cause)) }
    finally { mutating.current = false; setBusy('') }
  }

  if (!admin) return <main className={base.page}><h1>使用改进</h1><p>只有平台管理员可以查看使用改进。</p></main>
  const items = (report?.items || []).filter(item => (status === 'all' || item.status === status) && (kind === 'all' || item.kind === kind))
  return <main className={`${base.page} ${styles.page}`}>
    <header className={base.header}><div><span className={base.eyebrow}>平台管理</span><h1>使用改进</h1><p>从实际使用中找到值得修复的问题和可以复用的方法。</p></div>
      <button className={base.primary} disabled={!!busy} onClick={() => void change('/scan', 'POST')}>{busy === '/scan' ? '正在整理…' : '立即整理'}</button>
    </header>
    <section className={styles.automation} aria-label="自动处理设置"><details open><summary>自动处理设置</summary>
      <p>后台整理不调用模型；开启自动处理后使用所选项目已有官方智能体，沿用额度设置。工作流中的模型和计算仍按项目配置执行。</p>
      <p>仅在官方智能体空闲时启动，已启动的任务不自动抢占。自动失败不反复重试；关闭后不再启动新项，已经启动的会话可在项目内停止。</p>
      {settingsLoading && <p role="status">正在读取自动处理设置…</p>}
      {settingsError && <p role="alert" className={base.error}>{settingsError}</p>}
      {settingsMessage && <p role="status" className={styles.saved}>{settingsMessage}</p>}
      {!settingsLoading && !settings && <button onClick={() => setSettingsAttempt(value => value + 1)}>重新读取设置</button>}
      {settings && <form onSubmit={event => { event.preventDefault(); void saveSettings() }}><fieldset disabled={settingsSaving || settingsLoading} className={styles.settingsFields}>
        <label className={styles.checkbox}><input type="checkbox" checked={settings.enabled} onChange={event => editSettings({ enabled: event.target.checked })} />自动交给官方智能体</label>
        <fieldset className={styles.projectChoices}><legend>允许自动处理的项目</legend><p>仅可选择已获准并使用官方智能体的项目。自动处理会按项目已有权限修改资源。</p>
          {settings.projects.map(project => <label key={project.id} className={styles.projectChoice}><input type="checkbox" aria-label={project.name} disabled={!project.available} checked={project.available && settings.project_ids.includes(project.id)} onChange={event => editSettings({ project_ids: event.target.checked ? [...settings.project_ids, project.id] : settings.project_ids.filter(id => id !== project.id) })} /><span>{project.name}{!project.available && <small>{project.reason || '此项目暂不能自动处理'}</small>}</span></label>)}
          {!settings.projects.some(project => project.available) && <p>暂无可自动处理的项目，请先为项目配置并授权官方智能体。</p>}
        </fieldset>
        <div className={styles.settingsFooter}><label>过去 24 小时最多处理<input type="number" min={1} max={10} step={1} required aria-label="过去24小时最多处理条数" value={settings.daily_limit || ''} onChange={event => editSettings({ daily_limit: Number(event.target.value) })} /><span>条线索</span></label><button className={base.primary} type="submit">{settingsSaving ? '正在保存设置…' : '保存自动处理设置'}</button></div>
      </fieldset></form>}
    </details></section>
    <section className={styles.intro} aria-label="整理说明"><p>自动整理和“立即整理”都不调用模型。这里展示的是改进线索，仍需结合项目验证，不是对结果的结论。</p><p>个人会话正文未收集。页面显示时每 30 秒刷新一次。</p>
      {report && <small>最近整理：{time(report.last_scan)} · 最近 {report.window_days} 天 · 已查看 {report.sampled_tasks} 个任务</small>}
      {report?.truncated && <p role="status" className={styles.notice}>本次最多查看 {report.limit} 个任务，样本已截断，不能代表全部使用情况。</p>}
      {!!report?.notes?.length && <details><summary>统计范围与限制</summary>{report.notes.map((note, index) => <p key={index}>{note}</p>)}</details>}
    </section>
    {(error || report?.error) && <p role="alert" className={base.error}>{error || report?.error}</p>}
    {report?.automatic_error && <p role="alert" className={base.error}>后台自动处理：{report.automatic_error}</p>}
    <div className={styles.filters}><label>处理状态<select aria-label="处理状态" value={status} onChange={event => setStatus(event.target.value as Status | 'all')}><option value="all">全部状态</option>{Object.entries(statusNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>线索类别<select aria-label="线索类别" value={kind} onChange={event => setKind(event.target.value as Kind | 'all')}><option value="all">全部类别</option>{Object.entries(kindNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><small>显示 {items.length} 条线索</small></div>
    {loading && <p role="status">正在读取改进线索…</p>}
    {!loading && !items.length && <p className={styles.empty}>{report?.items.length ? '当前筛选下没有线索。' : '暂未发现改进线索。继续使用项目后，可以再次整理。'}</p>}
    <section className={styles.items} aria-label="改进线索">{items.map(item => <article key={item.id} className={styles.item} aria-label={item.title}>
      <div className={styles.tags}><span className={base.tag}>{kindNames[item.kind]}</span><span className={styles.status} data-status={item.status}>{statusNames[item.status]}</span><small>{item.count} 次相关记录</small></div>
      <h2>{item.title}</h2><p className={styles.project}><Link href={'/projects/' + item.project_id}>{item.project_name || '查看项目'}</Link>{item.workflow_name && <span> / {item.workflow_name}</span>}</p>
      {item.active === false && <p className={styles.limitation}>本轮未再观察到：可能已变化，或不在当前样本中</p>}
      <p>{item.explanation}</p><p className={styles.limitation}>{item.limitation}</p><p><strong>下一步：</strong>{item.next_step}</p>
      <details className={styles.trace}><summary>查看使用轨迹（{item.tasks.length + (item.operations?.length || 0)} 条）</summary>
        {item.workflow_changed != null && <p>工作流内容{item.workflow_changed ? '发生变化' : '未变化'}。{item.inputs_changed != null && `任务输入${item.inputs_changed ? '发生变化' : '未变化'}。`}</p>}
        <ol>{item.tasks.map(task => <li key={task.id}><div className={styles.traceHeading}><strong>{resultNames[task.status] || '状态未知'}</strong><span>{task.purpose === 'build_test' ? '工作流测试' : task.purpose === 'business' ? '业务任务' : '未分类任务'}</span><time>{time(task.created_at)}</time></div>
          <small>任务 <code>{task.id}</code>{task.revision != null && ` · 草稿版本 ${task.revision}`}{task.error_kind && ` · ${knownErrors.has(task.error_kind) ? task.error_kind : errorNames[task.error_kind] || '运行报错'}`}</small><small className={styles.updated}>最近更新：{time(task.updated_at)}</small></li>)}
          {item.operations?.map(operation => <li key={operation.id}><div className={styles.traceHeading}><strong>{featureNames[operation.feature.replace(/_error$/, '')] || '项目操作'}</strong><span>{/^HTTP [1-5]\d{2}$/.test(operation.outcome) ? operation.outcome : resultNames[operation.outcome] || '操作报错'}</span><time>{time(operation.created)}</time></div><small>操作 <code>{operation.id}</code>{operation.resource_id && <> · 资源 <code>{operation.resource_id}</code></>}</small></li>)}</ol>
      </details>
      <div className={`${base.actions} ${styles.actions}`}>
        {item.status !== 'working' && <button disabled={!!busy} onClick={() => void change('/' + item.id, 'PATCH', { status: 'working' })}>标为处理中</button>}
        {item.status !== 'dismissed' && <button disabled={!!busy} onClick={() => void change('/' + item.id, 'PATCH', { status: 'dismissed' })}>忽略</button>}
        {item.status !== 'new' && <button disabled={!!busy} onClick={() => void change('/' + item.id, 'PATCH', { status: 'new' })}>重新打开</button>}
        <a href={'/api/platform' + endpoint + '/' + encodeURIComponent(item.id) + '/brief'} download={'改进任务-' + item.id + '.md'}>下载改进任务（Markdown）</a>
      </div>
      <div className={styles.handoff}><div><p>点击后会使用项目现有模型连接，产生模型用量，并按项目原有权限处理和修改项目资源。</p><small>重复点击会复用同一个处理会话。{item.handoff?.conversation_id && ` 已有处理会话：${resultNames[item.handoff.status] || '已建立'}。`}</small>{item.handoff?.error && <p className={base.error}>{item.handoff.error}</p>}</div>
        <button className={base.primary} disabled={!!busy} onClick={() => void start(item)}>{busy === '/' + item.id + '/start' ? '正在打开处理会话…' : '让项目智能体处理'}</button>
      </div>
    </article>)}</section>
  </main>
}
