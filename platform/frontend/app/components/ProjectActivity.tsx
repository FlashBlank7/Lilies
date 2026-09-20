'use client'

import { useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import { taskNames, type ProjectActivity as Activity, type ProjectTask } from '@/lib/project-progress'
import { CheckCircle2, Circle, LoaderCircle, PauseCircle, AlertCircle } from 'lucide-react'
import styles from '@/app/projects/projects.module.css'

const statusNames: Record<string, string> = { running:'正在执行', completed:'操作完成', failed:'执行失败', interrupted:'已中断' }
export function ActivityStatus({ status }: { status: string }) {
  const Icon = status === 'running' ? LoaderCircle : status === 'completed' ? CheckCircle2 : status === 'failed' ? AlertCircle : status === 'interrupted' ? PauseCircle : Circle
  return <Icon size={15} aria-hidden className={status === 'running' ? styles.spinner : undefined} />
}

function RelatedRun({ projectId, taskId, onTask, onWorkflow, workflowNames }: { projectId: string; taskId: string; workflowNames: Record<string, string>; onTask?: (id: string) => void; onWorkflow?: (id: string) => void }) {
  const [task, setTask] = useState<ProjectTask | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true, timer: number | undefined
    const refresh = async () => {
      try {
        const next = await api<ProjectTask>(`/api/v1/projects/${projectId}/tasks/${taskId}`)
        if (!active) return
        setTask(next); setError('')
        if (['running', 'queued'].includes(next.status)) timer = window.setTimeout(() => void refresh(), 2000)
      } catch (e) { if (active) { setError(String(e)); timer = window.setTimeout(() => void refresh(), 2000) } }
    }
    void refresh()
    return () => { active = false; window.clearTimeout(timer) }
  }, [projectId, taskId])
  return <div className={styles.relatedRun}>
    {error && <p role="alert">暂时无法读取运行详情：{error}</p>}
    <button onClick={() => onTask?.(taskId)}>查看关联任务{task ? ' · ' + (taskNames[task.status] || task.status) : ''}</button>
    {task?.runs?.map(run => <div key={run.id}><span>{workflowNames[run.application_id] || '成员工作流'} · {taskNames[run.status] || run.status} · 草稿 r{run.draft_revision}</span><button onClick={() => onWorkflow?.(run.application_id)}>查看成员工作流</button></div>)}
  </div>
}

export default function ProjectActivity({ projectId, conversationId, requestId, current, onTask, onWorkflow, workflowNames = {}, active = current?.status === 'running' }: {
  projectId: string; conversationId?: string; workflowNames?: Record<string, string>; active?: boolean; requestId?: string; current?: Activity | null; onTask?: (id: string) => void; onWorkflow?: (id: string) => void
}) {
  const conversationPath = conversationId ? 'conversations/' + conversationId : 'conversation'
  const [open, setOpen] = useState(false)
  const [operations, setOperations] = useState<Activity[]>([])
  const [before, setBefore] = useState('')
  const [more, setMore] = useState(false)
  const [error, setError] = useState('')
  async function older() {
    try {
      const page = await api<{ events: Activity[]; has_more: boolean; first_cursor: string }>(`/api/v1/projects/${projectId}/${conversationPath}?kind=activity&request_id=${encodeURIComponent(requestId || '')}&before=${encodeURIComponent(before)}`)
      setOperations(previous => [...new Map([...page.events, ...previous].map(a => [a.operation_id, a])).values()])
      setMore(page.has_more); setBefore(page.first_cursor); setError('')
    } catch (e) { setError(String(e)) }
  }
  useEffect(() => {
    if (!open) return
    let alive = true, cursor = ''
    const refresh = async () => {
      try {
        const page = await api<{ events: Activity[]; has_more: boolean; first_cursor: string; last_cursor: string }>(`/api/v1/projects/${projectId}/${conversationPath}?kind=activity&request_id=${encodeURIComponent(requestId || '')}${cursor ? '&after=' + encodeURIComponent(cursor) : ''}`)
        if (!alive) return
        if (!cursor) { setBefore(page.first_cursor); setMore(page.has_more) }
        cursor = page.last_cursor || cursor
        setOperations(previous => [...new Map([...previous, ...page.events].map(a => [a.operation_id, a])).values()]); setError('')
      } catch (e) { if (alive) setError(String(e)) }
    }
    void refresh()
    const timer = active ? window.setInterval(() => void refresh(), 1500) : undefined
    return () => { alive = false; window.clearInterval(timer) }
  }, [projectId, conversationPath, open, requestId, active])
  const visible = operations.filter(a => !requestId || a.request_id === requestId)
  const summary = current || visible.at(-1)
  return <details className={styles.activity} onToggle={e => setOpen(e.currentTarget.open)}>
    <summary><ActivityStatus status={summary?.status || ''} /><span>{summary ? `${summary.title}${summary.workflow_name ? ' · ' + summary.workflow_name : ''}` : '本次执行过程'}</span><small>{summary ? statusNames[summary.status] || summary.status : '查看步骤'}</small></summary>
    {open && <div className={styles.activitySteps}>
      {error && <p role="alert" className={styles.error}>{error}</p>}
      {more && <button onClick={() => void older()}>加载更早的步骤</button>}
      {!visible.length && <p>暂无可关联的操作。历史工具日志可在“查看操作记录”中检查。</p>}
      {visible.map(operation => <details key={operation.operation_id} className={styles.activityStep}>
        <summary><ActivityStatus status={operation.status} /><span>{operation.title}{operation.workflow_name ? ' · ' + operation.workflow_name : ''}</span><small>{statusNames[operation.status]}{operation.duration_seconds != null ? ` · ${operation.duration_seconds.toFixed(1)} 秒` : ''}</small></summary>
        {operation.summary && <p>{operation.summary}</p>}
        {operation.workflow_id && <button onClick={() => onWorkflow?.(operation.workflow_id!)}>打开工作流画布</button>}
        {operation.task_id && <RelatedRun workflowNames={workflowNames} projectId={projectId} taskId={operation.task_id} onTask={onTask} onWorkflow={onWorkflow} />}
        <details><summary>参数与原始返回</summary><small>{operation.tool_name}</small><pre>{operation.arguments || '无参数'}</pre><pre>{operation.result || '等待返回'}</pre></details>
      </details>)}
    </div>}
  </details>
}
