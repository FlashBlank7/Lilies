'use client'

import Link from 'next/link'
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import styles from '../projects.module.css'

type Member = { id: string; name: string; revision: number }
type Project = { id: string; name: string; description: string; main_workflow_id: string; members: Member[] }
type RecordRow = { collection: string; key: string; revision: number; value: Record<string, unknown> }
type Run = { id: string; application_id: string; status: string; inputs: object; outputs: object; error: string; draft_revision?: number; waiting_node?: { type: string; config: object } }
type Task = { id: string; request_key: string; status: string; mode: string; inputs: object; outputs: Record<string, unknown>; error: string; message: string; runs?: Run[]; supplements?: object[] }
const statuses: Record<string, string> = { queued: '等待开始', running: '处理中', succeeded: '已完成', waiting_input: '等待补充', interrupted: '已中断', failed: '失败', cancelled: '已停止', paused: '等待输入' }
const tabs = [ ['workflows', '工作流与协作画布'], ['records', '业务记录'], ['tasks', '项目任务']] as const
function json(text: string) { const value = JSON.parse(text); if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('请输入 JSON 对象'); return value }
function taskTitle(task: Task) {
  const message = task.outputs?.message
  return task.mode === 'agent' && typeof message === 'string' && message.trim()
    ? message.trim() : task.request_key
}

export default function DeveloperTools({ id, initialWorkflowId = '', initialTaskId = '' }: { id: string; initialWorkflowId?: string; initialTaskId?: string }) {
  const base = '/api/v1/projects/' + id
  const [project, setProject] = useState<Project | null>(null)
  const [tab, setTab] = useState(initialTaskId ? 'tasks' : 'workflows')
  const [selected, setSelected] = useState(initialWorkflowId || id)
  const [records, setRecords] = useState<RecordRow[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [task, setTask] = useState<Task | null>(null)
  const [error, setError] = useState('')
  const [projectError, setProjectError] = useState('')
  const [dataError, setDataError] = useState('')
  const [taskError, setTaskError] = useState('')
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [collection, setCollection] = useState('')
  const [key, setKey] = useState('')
  const [revision, setRevision] = useState(0)
  const [value, setValue] = useState('{}')
  const [editingRecord, setEditingRecord] = useState(false)
  const [requestKey, setRequestKey] = useState('')
  const [inputs, setInputs] = useState('{}')
  const [message, setMessage] = useState('')
  const [supplement, setSupplement] = useState('')
  const [supplementInputs, setSupplementInputs] = useState('{}')
  const [humanResponses, setHumanResponses] = useState<Record<string, string>>({})
  const [mode, setMode] = useState<'workflow' | 'agent'>('workflow')
  const [runMember, setRunMember] = useState(id)

  const refresh = useCallback(async () => {
    try { const p = await api<Project>(base); setProject(p); setProjectError('') } catch (e) { setProjectError(String(e)) }
  }, [base])
  const refreshData = useCallback(async () => {
    try {
      const [rs, ts] = await Promise.all([api<RecordRow[]>(base + '/records'), api<Task[]>(base + '/tasks')])
      setRecords(rs); setTasks(ts); setDataError('')
    } catch (e) { setDataError(String(e)) }
  }, [base])
  useEffect(() => { void refresh(); void refreshData() }, [refresh, refreshData])
  useEffect(() => { if (initialTaskId) void api<Task>(base + '/tasks/' + initialTaskId).then(setTask).catch(e => setError(String(e))) }, [base, initialTaskId])
  useEffect(() => {
    const timer = window.setInterval(() => {
      void refreshData()
      if (task) void api<Task>(base + '/tasks/' + task.id).then(t => { setTask(t); setTaskError('') }).catch(e => setTaskError(String(e)))
    }, 1500)
    return () => window.clearInterval(timer)
  }, [base, refreshData, task?.id]) // eslint-disable-line react-hooks/exhaustive-deps
  async function act(fn: () => Promise<unknown>) {
    setBusy(true); setError('')
    try { await fn(); await refresh(); await refreshData() } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  function prepareRun(next: 'workflow' | 'agent') { setMode(next); setTab('tasks'); setRequestKey(crypto.randomUUID()); setRunMember(id) }
  async function openTask(t: Task) { setTask(await api<Task>(base + '/tasks/' + t.id)); setTaskError(''); setSupplement(''); setSupplementInputs('{}') }
  function editRecord(row?: RecordRow) {
    setCollection(row?.collection || ''); setKey(row?.key || ''); setRevision(row?.revision || 0)
    setValue(JSON.stringify(row?.value || {}, null, 2)); setEditingRecord(Boolean(row))
  }
  const resumable = task && ['waiting_input', 'interrupted', 'failed'].includes(task.status)
  const resultMessage = task?.outputs?.message || task?.outputs?.reason
  const visibleError = error || projectError || dataError || taskError
  return <section aria-label="开发详情">
    <div className={styles.actions}><button onClick={() => prepareRun('workflow')}>运行协作流程</button><button onClick={() => prepareRun('agent')}>交给统筹处理</button></div>
    <nav className={styles.tabs} role="tablist" aria-label="项目页面">{tabs.map(([key, label]) => <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>{label}</button>)}</nav>
    {visibleError && <p className={styles.error} role="alert">{visibleError}</p>}
    {tab === 'workflows' && <>
      <section className={styles.panel}><h2>项目工作流</h2><p>主流程画布中的调用节点决定协作顺序与分支。每条工作流保留自己的草稿、测试和版本。</p>
        <div className={styles.actions}>{project?.members.map(m => <button key={m.id} aria-selected={selected === m.id} onClick={() => setSelected(m.id)}>{m.id === id ? '主流程 · ' : ''}{m.name} · r{m.revision}</button>)}</div>
        <div className={styles.actions}><input aria-label="成员工作流名称" value={name} onChange={e => setName(e.target.value)} placeholder="新成员名称" />
          <button disabled={busy || !name.trim()} onClick={() => void act(async () => { const m = await api<Member>(base + '/members', { method: 'POST', body: JSON.stringify({ name }) }); setName(''); setSelected(m.id) })}>添加空白工作流</button>
          {selected !== id && <button disabled={busy} onClick={() => void act(async () => { await api(base + '/members/' + selected, { method: 'DELETE' }); setSelected(id) })}>移除此成员</button>}
          <Link href={`/applications/${selected}?tab=edit&embedded=1`} target="_blank">独立打开画布 ↗</Link></div>
      </section>
      <iframe key={selected} title={selected === id ? '协作画布' : '成员工作流画布'} className={styles.canvas} src={`/applications/${selected}?tab=edit&embedded=1`} />
    </>}
    {tab === 'records' && <div className={styles.split}>
      <section className={styles.panel}><h2>{editingRecord ? '编辑业务记录' : '新建业务记录'}</h2>
        <label>集合<input aria-label="记录集合" value={collection} disabled={editingRecord} onChange={e => setCollection(e.target.value)} /></label>
        <label>记录键<input aria-label="记录键" value={key} disabled={editingRecord} onChange={e => setKey(e.target.value)} /></label>
        <p>当前修订号：{revision}。保存时检查是否已有其他更新。</p>
        <label>记录内容（JSON）<textarea aria-label="记录内容" value={value} onChange={e => setValue(e.target.value)} /></label>
        <div className={styles.actions}><button disabled={busy || !collection || !key} onClick={() => void act(async () => {
          const row = await api<RecordRow>(`${base}/records/${encodeURIComponent(collection)}/${encodeURIComponent(key)}`, { method: 'PUT', body: JSON.stringify({ value: json(value), expected_revision: revision }) }); editRecord(row)
        })}>保存记录</button><button onClick={() => editRecord()}>新建另一条</button></div>
      </section>
      <section className={styles.panel}><h2>共享业务记录</h2><table><thead><tr><th>集合 / 记录键</th><th>修订号</th><th>内容</th><th /></tr></thead><tbody>{records.map(row => <tr key={row.collection + '/' + row.key}>
        <td>{row.collection}<br />{row.key}</td><td>{row.revision}</td><td><pre>{JSON.stringify(row.value, null, 2)}</pre></td><td><button onClick={() => editRecord(row)}>编辑</button></td>
      </tr>)}</tbody></table>{!records.length && <p>工作流和人工更新的记录会显示在这里。</p>}</section>
    </div>}
    {tab === 'tasks' && <div className={styles.split}>
      <div><section className={styles.panel}><h2>{mode === 'agent' ? '交给 Lilies 统筹' : '运行项目工作流'}</h2>
        <label>处理方式<select aria-label="任务处理方式" value={mode} onChange={e => setMode(e.target.value as 'workflow' | 'agent')}><option value="workflow">运行协作流程</option><option value="agent">交给统筹处理</option></select></label>
        {mode === 'workflow' && <label>入口工作流<select value={runMember} onChange={e => setRunMember(e.target.value)}>{project?.members.map(m => <option key={m.id} value={m.id}>{m.id === id ? '主流程 · ' : ''}{m.name}</option>)}</select></label>}
        <label>业务请求标识<input aria-label="业务请求标识" value={requestKey} onChange={e => setRequestKey(e.target.value)} placeholder="同一请求重复提交会返回已有任务" /></label>
        <label>业务输入（JSON）<textarea aria-label="业务输入" value={inputs} onChange={e => setInputs(e.target.value)} /></label>
        {mode === 'agent' && <label>处理要求<textarea aria-label="统筹处理要求" value={message} onChange={e => setMessage(e.target.value)} placeholder="说明需要处理什么，由 Lilies 选择流程并检查结果" /></label>}
        <button className={styles.primary} disabled={busy || !requestKey.trim()} onClick={() => void act(async () => { const t = await api<Task>(base + '/tasks', { method: 'POST', body: JSON.stringify({ request_key: requestKey, mode, workflow_id: mode === 'workflow' ? runMember : id, inputs: json(inputs), message: mode === 'agent' ? message : '' }) }); await openTask(t) })}>开始处理</button>
      </section><section className={styles.panel}><h2>项目任务</h2><ul className={styles.list}>{tasks.map(t => <li key={t.id}><button onClick={() => void act(() => openTask(t))}>{taskTitle(t)} · {statuses[t.status] || t.status}</button></li>)}</ul>{!tasks.length && <p>还没有项目任务。</p>}</section></div>
      <section className={styles.panel}>{task ? <>
        <h2>{taskTitle(task)}</h2><span className={styles.tag}>{statuses[task.status] || task.status}</span><span className={styles.tag}>{task.mode === 'agent' ? '项目统筹' : '工作流'}</span>
        <h3>业务结果</h3>{task.error && <p className={styles.error}>{task.error}</p>}
        {typeof resultMessage === 'string' && resultMessage ? <><p>{resultMessage}</p><details><summary>完整结果（JSON）</summary><pre>{JSON.stringify(task.outputs, null, 2)}</pre></details></> : <pre>{JSON.stringify(task.outputs, null, 2)}</pre>}
        <details><summary>请求输入</summary><p>业务请求标识：{task.request_key}</p><pre>{JSON.stringify(task.inputs, null, 2)}</pre><p>{task.message}</p></details>
        <h3>成员运行</h3>{task.runs?.map(run => <details key={run.id} open={['failed', 'running', 'paused'].includes(run.status)}><summary>{project?.members.find(m => m.id === run.application_id)?.name || run.application_id} · {statuses[run.status] || run.status}</summary>
          <small>草稿 r{run.draft_revision ?? '—'} · 运行 {run.id}</small><p>输入</p><pre>{JSON.stringify(run.inputs, null, 2)}</pre><p>输出</p><pre>{JSON.stringify(run.outputs, null, 2)}</pre>{run.error && <p className={styles.error}>{run.error}</p>}
          {run.status === 'paused' && run.waiting_node?.type !== 'tool' && <>
            <p>此步骤需要补充输入：</p><pre>{JSON.stringify(run.waiting_node?.config, null, 2)}</pre>
            <textarea aria-label={`补充运行 ${run.id} 的输入`} value={humanResponses[run.id] || '{}'} onChange={e => setHumanResponses(current => ({ ...current, [run.id]: e.target.value }))} />
            <button disabled={busy} onClick={() => void act(() => api(`${base}/tasks/${task.id}/runs/${run.id}/input`, { method: 'POST', body: JSON.stringify({ values: json(humanResponses[run.id] || '{}') }) }))}>保存步骤输入</button>
          </>}
        </details>)}
        {['running', 'queued'].includes(task.status) && <button disabled={busy} onClick={() => void act(async () => setTask(await api<Task>(`${base}/tasks/${task.id}/stop`, { method: 'POST' })))}>停止任务</button>}
        {resumable && <><p>补充说明或输入；如需释放资源，请先在业务记录中更新，然后继续此任务。</p>
          <label>补充说明<textarea aria-label="任务补充说明" value={supplement} onChange={e => setSupplement(e.target.value)} /></label>
          <label>补充输入（JSON）<textarea aria-label="任务补充输入" value={supplementInputs} onChange={e => setSupplementInputs(e.target.value)} /></label>
          <button disabled={busy} onClick={() => void act(async () => {
            if (supplement || supplementInputs !== '{}') await api(`${base}/tasks/${task.id}/supplements`, { method: 'POST', body: JSON.stringify({ message: supplement, inputs: json(supplementInputs) }) })
            setTask(await api<Task>(`${base}/tasks/${task.id}/resume`, { method: 'POST', body: JSON.stringify({ message: supplement }) }))
            setSupplement(''); setSupplementInputs('{}')
          })}>补充并继续</button></>}
        {!!task.supplements?.length && <details><summary>已提交的补充</summary><pre>{JSON.stringify(task.supplements, null, 2)}</pre></details>}
      </> : <p>选择任务，查看业务结果和各成员的运行进度。</p>}</section>
    </div>}
  </section>
}
