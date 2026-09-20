'use client'

import Link from 'next/link'
import { WorkflowValueField } from './WorkflowValueField'
import { useEffect, useRef, useState } from 'react'
import { api, withFrontendToken } from '@/lib/platform'
import { MarkdownDocument } from '@/lib/markdown'
import { resolveProjectLink } from '@/lib/project-links'
import { taskNames, type ProjectMember, type ProjectTask } from '@/lib/project-progress'
import styles from '@/app/projects/projects.module.css'

type Field = { name: string; label?: string; type: string; required?: boolean; default?: unknown; description?: string }
type Draft = { snapshot: { workflow: { nodes: { type: string; config: { inputs?: Field[] } }[] } } }
type FileEntry = { path: string }

export function ProjectRunEvents({ runs, members }: { runs: ProjectTask['runs']; members: ProjectMember[] }) {
  const [events, setEvents] = useState<{ id: number; type: string; data: unknown }[]>([])
  const [truncated, setTruncated] = useState(false)
  const [error, setError] = useState('')
  return <>{runs?.map(run => <p key={run.id}><button onClick={async () => { setError(''); try { const result = await api<{ events: typeof events; truncated: boolean }>(`/api/v1/runs/${run.id}/events/list?after=0&limit=1000`); setEvents(result.events); setTruncated(result.truncated) } catch (cause) { setError(String(cause)) } }}>{members.find(member => member.id === run.application_id)?.name || '成员'} · {taskNames[run.status] || run.status} · 查看步骤输入输出</button></p>)}
    {error && <p role="alert">{error}</p>}{truncated && <p>当前显示最近 1000 条事件，较早事件未包含在此窗口中。</p>}
    {events.map(event => <details key={event.id}><summary>{event.type}</summary><pre>{JSON.stringify(event.data, null, 2)}</pre></details>)}
  </>
}

export function ProjectTaskOutput({ projectId, task }: { projectId: string; task: ProjectTask }) {
  const output = task.outputs || {}
  const markdown = task.presentation?.markdown || (typeof output.markdown === 'string' ? output.markdown : '') || task.presentation?.message || (typeof output.message === 'string' ? output.message : '')
  const artifacts = task.presentation?.artifacts?.length ? task.presentation.artifacts : (Array.isArray(output.artifacts) ? output.artifacts : [])
  const results = [output, ...Object.values(output).map(v => typeof v==='string'?{artifact:v}:v)].filter((v):v is Record<string,unknown> => !!v && typeof v==='object' && !Array.isArray(v))
  const predictions = [...results.reduce((files,result)=>{
    const path=result.artifact
    if(typeof path==='string' && /^datasets\/[\w-]+\/files\/[\w./-]+$/.test(path) && !path.split('/').includes('..') && (!files.has(path)||Array.isArray(result.preview))) files.set(path,result)
    return files
  },new Map<string,Record<string,unknown>>()).values()]
  const trials = task.mode==='training' && Array.isArray(output.trials) ? output.trials as {slot:number;model:string;status:string;metrics?:Record<string,number>;error?:string}[] : []
  return <>
    <MarkdownDocument source={markdown} resolveLink={href => resolveProjectLink(projectId, href)} emptyLabel={['queued', 'running'].includes(task.status) ? '正在运行，结果会自动显示。' : '本次运行的输出见下方详情。'} />
    {!!trials.length && <table><thead><tr><th>模型</th><th>验证指标</th><th>结果</th></tr></thead><tbody>{trials.map(t=><tr key={t.slot}><td>{t.model}</td><td>{Object.entries(t.metrics||{}).map(([k,v])=>`${k}: ${Number(v).toPrecision(5)}`).join(' / ')}</td><td>{t.error|| (t.status==='completed'?'已完成':t.status)}</td></tr>)}</tbody></table>}
    {task.mode==='training' && typeof output.study_id==='string' && typeof output.id==='string' && <p><a download href={withFrontendToken(`/api/platform/api/v1/projects/${projectId}/modeling/studies/${encodeURIComponent(output.study_id)}/candidates/${encodeURIComponent(output.id)}/download`)}>下载模型与训练记录 ↓</a></p>}
    {predictions.map((result,i)=>{const rows=Array.isArray(result.preview)?result.preview.slice(0,20) as Record<string,unknown>[]:[];const columns=rows.length?Object.keys(rows[0]).slice(0,8):[]
      return <section key={i}><h3>预测结果</h3><p>结果已保存，本次使用的模型版本固定在运行记录中。</p>
        {!!rows.length&&<div style={{overflowX:'auto'}}><table><thead><tr>{columns.map(key=><th key={key}>{key==='prediction'?'预测值':key}</th>)}</tr></thead><tbody>{rows.map((row,j)=><tr key={j}>{columns.map(key=><td key={key}>{typeof row[key]==='number'?Number(row[key]).toPrecision(6):String(row[key]??'')}</td>)}</tr>)}</tbody></table><p>显示前 {rows.length} 行，完整结果见下载文件。</p></div>}
        <a download href={withFrontendToken(`/api/platform/api/v1/projects/${projectId}/${result.artifact}`)}>下载预测结果 CSV ↓</a></section>})}
    {artifacts.map((entry: unknown, i: number) => {
      if (!entry || typeof entry !== 'object') return null
      const item = entry as { file_path?: string; label?: string }
      if (!item.file_path || !/^(results|solution)\//.test(item.file_path) || item.file_path.split('/').includes('..')) return null
      return <p key={i}><a download href={withFrontendToken(`/api/platform/api/v1/applications/${projectId}/workspace/files/${item.file_path.split('/').map(encodeURIComponent).join('/')}`)}>{item.label || item.file_path.split('/').pop()} ↓</a></p>
    })}
  </>
}

export default function ProjectRunPanel({ projectId, members, initialWorkflowId, onTask }: {
  projectId: string; members: ProjectMember[]; initialWorkflowId?: string; onTask?: (task: ProjectTask) => void
}) {
  const [workflowId, setWorkflowId] = useState(initialWorkflowId || projectId)
  const [fields, setFields] = useState<Field[]>([])
  const [values, setValues] = useState<Record<string, string>>({})
  const [files, setFiles] = useState<FileEntry[]>([])
  const [task, setTask] = useState<ProjectTask | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const lock = useRef(false)
  const base = `/api/v1/projects/${projectId}`
  const active = Boolean(task && ['queued', 'running'].includes(task.status))
  useEffect(() => {
    let current = true
    setLoading(true); setError('')
    Promise.all([api<Draft>(`/api/v1/applications/${workflowId}/draft`), api<FileEntry[]>(`/api/v1/applications/${projectId}/workspace/files`)]).then(([draft, available]) => {
      if (!current) return
      const inputs = draft.snapshot.workflow.nodes.find(node => node.type === 'start')?.config.inputs || []
      setFields(inputs); setValues(Object.fromEntries(inputs.map(field => [field.name, field.default == null ? '' : typeof field.default === 'string' ? field.default : JSON.stringify(field.default)])))
      setFiles(available); setLoading(false)
    }).catch(cause => { if (current) { setError(String(cause)); setLoading(false) } })
    return () => { current = false }
  }, [projectId, workflowId])
  useEffect(() => {
    if (!task || !active) return
    let current = true
    const timer = window.setInterval(() => {
      void api<ProjectTask>(`${base}/tasks/${task.id}`).then(next => { if (current) { setTask(next); onTask?.(next) } }).catch(cause => { if (current) setError(String(cause)) })
    }, 1500)
    return () => { current = false; window.clearInterval(timer) }
  }, [base, task?.id, active, onTask])
  async function start() {
    if (lock.current) return
    lock.current = true; setBusy(true); setError('')
    try {
      const inputs: Record<string, unknown> = {}
      for (const field of fields) {
        const value = values[field.name] ?? ''
        const raw = ['object', 'array', 'any'].includes(field.type) ? value.trim() : value
        if (!raw && field.required) throw new Error(`请填写 ${field.name}`)
        if (!raw) {
          // Omitting a cleared field would restore the workflow's old default.
          if (field.type === 'array') inputs[field.name] = []
          else if (['string', 'file'].includes(field.type)) inputs[field.name] = ''
          continue
        }
        if (field.type === 'number') {
          const number = Number(raw)
          if (!Number.isFinite(number)) throw new Error(`${field.name} 需要有效数字`)
          inputs[field.name] = number
        } else if (field.type === 'boolean') inputs[field.name] = raw === 'true'
        else if (['object', 'array', 'any'].includes(field.type)) {
          try { inputs[field.name] = JSON.parse(raw) } catch { throw new Error(`${field.name} 需要有效 JSON`) }
        } else inputs[field.name] = raw
      }
      const next = await api<ProjectTask>(base + '/tasks', { method: 'POST', body: JSON.stringify({ request_key: crypto.randomUUID(), mode: 'workflow', workflow_id: workflowId, inputs, purpose: 'customer_trial' }) })
      setTask(next); onTask?.(next)
    } catch (cause) { setError(String(cause)) } finally { setBusy(false); lock.current = false }
  }
  return <section className={styles.panel} aria-label="手动运行工作流">
    <h2>运行工作流</h2><p>选择工作流和本次资料，直接运行当前已保存的配置。</p>
    <label>入口工作流<select aria-label="入口工作流" disabled={active || busy} value={workflowId} onChange={event => { setWorkflowId(event.target.value); setTask(null) }}>{members.map(member => <option key={member.id} value={member.id}>{member.id === projectId ? '主流程 · ' : ''}{member.name}</option>)}</select></label>
    <p><Link href={`/applications/${workflowId}?tab=edit`} target="_blank">编辑这条工作流 ↗</Link></p>
    {loading ? <p role="status">正在读取输入配置…</p> : fields.map(field => <div key={field.name}>
      <label>{field.label || field.name}{field.required ? ' *' : ''}
        {field.name === 'dataset_id' ? <WorkflowValueField allowReference={false} disabled={active || busy} projectId={projectId} field="dataset_id" nodeId="run" nodes={[]} label="预测数据集" value={values[field.name] || ''} onChange={next => setValues(previous => ({...previous, [field.name]: next}))} /> : field.type === 'boolean' ? <select aria-label={field.name} disabled={active || busy} value={values[field.name] || ''} onChange={event => setValues(previous => ({ ...previous, [field.name]: event.target.value }))}><option value="">请选择</option><option value="true">是</option><option value="false">否</option></select>
          : <textarea aria-label={field.name} rows={['object', 'array', 'any'].includes(field.type) ? 4 : 2} disabled={active || busy} value={values[field.name] || ''} onChange={event => setValues(previous => ({ ...previous, [field.name]: event.target.value }))} />}
      </label>{field.description && <p>{field.description}</p>}
      {['string', 'file'].includes(field.type) && files.length > 0 && <label>选择项目文件<select aria-label={`为 ${field.name} 选择项目文件`} disabled={active || busy} value="" onChange={event => setValues(previous => ({ ...previous, [field.name]: event.target.value }))}><option value="">从已上传资料或结果中选择…</option>{files.map(file => <option key={file.path} value={file.path}>{file.path}</option>)}</select></label>}
    </div>)}
    <div className={styles.actions}><button className={styles.primary} disabled={loading || busy || active} onClick={() => void start()}>{busy ? '正在启动…' : active ? '正在运行…' : '启动工作流'}</button>
      {active && task && <button disabled={busy} onClick={async () => { setBusy(true); try { const next = await api<ProjectTask>(`${base}/tasks/${task.id}/stop`, { method: 'POST' }); setTask(next); onTask?.(next) } catch (cause) { setError(String(cause)) } finally { setBusy(false) } }}>停止运行</button>}
    </div>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {task && <section aria-label="本次运行结果"><h3>{taskNames[task.status] || task.status}</h3>{task.error && <p role="alert">{task.error}</p>}<ProjectTaskOutput projectId={projectId} task={task} />
      <details><summary>实际输入输出与运行详情</summary><pre>{JSON.stringify({ inputs: task.inputs, outputs: task.outputs }, null, 2)}</pre><ProjectRunEvents key={task.id} runs={task.runs} members={members} />
      </details>
    </section>}
  </section>
}
