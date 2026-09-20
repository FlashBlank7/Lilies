'use client'

import Link from 'next/link'
import { MessageSquare, Workflow, Files, Wrench, ChartNoAxesCombined, ArrowUpRight, X } from 'lucide-react'
import AppShell from '@/app/components/AppShell'
import ProjectFileReader from '@/app/components/ProjectFileReader'
import ProjectWorkflowOverview from '@/app/components/ProjectWorkflowOverview'
import ReadingDialog from '@/app/components/ReadingDialog'
import { use, useCallback, useEffect, useState } from 'react'
import { api, withFrontendToken } from '@/lib/platform'
import { MarkdownDocument } from '@/lib/markdown'
import { projectFileFromLink, resolveProjectLink } from '@/lib/project-links'
import { availabilityNames, workNames, taskNames, type ProjectProgress, type ProjectMember, type ProjectTask, type ConversationFocus, type ProgressItem, type ProjectTopology } from '@/lib/project-progress'
import ProjectConversation from '@/app/components/ProjectConversation'
import ProjectMaterials from '@/app/components/ProjectMaterials'
import ProjectRunPanel, { ProjectTaskOutput, ProjectRunEvents } from '@/app/components/ProjectRunPanel'
import ModelConnectionPanel from '@/app/components/ModelConnectionPanel'
import ProjectCapabilities from '@/app/components/ProjectCapabilities'
import DeveloperTools from './DeveloperTools'
import styles from '../projects.module.css'

type Project = { id: string; name: string; members: ProjectMember[]; agent_modules_enabled: boolean }
const emptyProgress: ProjectProgress = { revision: 0, value: { goal: '', summary: '', items: [] }, updated_at: null }

export default function ProjectPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const base = '/api/v1/projects/' + id
  const [project, setProject] = useState<Project | null>(null)
  const [progress, setProgress] = useState<ProjectProgress>(emptyProgress)
  const [tasks, setTasks] = useState<ProjectTask[]>([])
  const [task, setTask] = useState<ProjectTask | null>(null)
  const [file, setFile] = useState('')
  const [fileTaskId, setFileTaskId] = useState('')
  const [reader, setReader] = useState(false)
  const [progressOpen, setProgressOpen] = useState(false)
  const [tab, setTab] = useState('overview')
  const [focus, setFocus] = useState<ConversationFocus>()
  const [topology, setTopology] = useState<ProjectTopology | null>(null)
  const [workflowId, setWorkflowId] = useState('')
  const [editingFlow, setEditingFlow] = useState(false)
  const [flowItemId, setFlowItemId] = useState('')
  const [requirements, setRequirements] = useState<string | null>(null)
  const [developerTaskId, setDeveloperTaskId] = useState('')
  const [error, setError] = useState('')
  const [moreResults, setMoreResults] = useState(false)
  const [runWorkflowId, setRunWorkflowId] = useState(id)
  const [memberName, setMemberName] = useState('')
  const [creatingMember, setCreatingMember] = useState(false)
  const [stopping, setStopping] = useState(false)
  const download = (path: string) => withFrontendToken(`/api/platform/api/v1/applications/${id}/workspace/files/${path.split('/').map(encodeURIComponent).join('/')}`)

  const refresh = useCallback(async () => {
    try {
      const [p, nextProgress, trials, business] = await Promise.all([
        api<Project>(base), api<ProjectProgress>(base + '/progress'),
        api<ProjectTask[]>(base + '/tasks?purpose=customer_trial&compact=true&limit=20'),
        api<ProjectTask[]>(base + '/tasks?purpose=business&compact=true&limit=20'),
      ])
      setProject(p); setProgress(nextProgress)
      setTasks(previous => [...new Map([...previous, ...trials, ...business].map(t => [t.id, t])).values()].sort((a, b) => b.created_at.localeCompare(a.created_at)))
      setMoreResults(trials.length === 20 || business.length === 20); setError('')
    } catch (cause) { setError(String(cause)) }
  }, [base])
  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => { const selected = new URLSearchParams(window.location.search).get('run'); if (selected) { setRunWorkflowId(selected); setTab('run') } }, [id])
  const updateManualTask = useCallback((next: ProjectTask) => setTasks(previous => [next, ...previous.filter(item => item.id !== next.id)]), [])
  useEffect(() => {
    if (!task || !reader || (!['running', 'queued'].includes(task.status) && (task.mode === 'workflow' || task.presentation?.markdown))) return
    const timer = window.setInterval(() => { void api<ProjectTask>(base + '/tasks/' + task.id).then(setTask).catch(e => setError(String(e))) }, 1500)
    return () => window.clearInterval(timer)
  }, [base, reader, task?.id, task?.mode, task?.status, task?.presentation?.markdown]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { try { const saved = sessionStorage.getItem('lilies:project:' + id + ':focus'); if (saved) setFocus({ ...JSON.parse(saved), message: undefined }) } catch {} }, [id])
  const clearFocus = useCallback(() => { setFocus(undefined); try { sessionStorage.removeItem('lilies:project:' + id + ':focus') } catch {} }, [id])
  function talk(item?: ProgressItem, message = '', taskId = '', questionId = '') {
    const next = { nonce: Date.now(), label: item?.title || '业务结果', item_id: item?.id, task_id: taskId, question_id: questionId, message: message || undefined }
    setFocus(next); try { sessionStorage.setItem('lilies:project:' + id + ':focus', JSON.stringify(next)) } catch {}; setTab('overview'); setReader(false); setProgressOpen(false)
  }
  async function showTask(taskId: string) {
    try { setTask(await api<ProjectTask>(base + '/tasks/' + taskId)); setReader(true) } catch (cause) { setError(String(cause)) }
  }
  function showFile(path: string, taskId = '') {
    setFile(path); setFileTaskId(taskId)
  }
  async function showFlow(item?: ProgressItem, selectedId?: string) {
    try { setTopology(await api<ProjectTopology>(base + '/topology')); setWorkflowId(selectedId || item?.workflow_ids[0] || id); setEditingFlow(false); setFlowItemId(item?.id || ''); setTab('flow') } catch (cause) { setError(String(cause)) }
  }
  async function showRequirements() {
    try { const doc = await api<{ document: string }>(base + '/requirements'); setRequirements(doc.document) } catch (cause) { setError(String(cause)) }
  }
  async function olderResults() {
    try {
      const older = await Promise.all(['customer_trial', 'business'].map(purpose => {
        const last = tasks.filter(t => t.purpose === purpose).at(-1)
        return last ? api<ProjectTask[]>(`${base}/tasks?purpose=${purpose}&compact=true&limit=20&before=${last.id}`) : Promise.resolve([])
      }))
      setTasks(previous => [...new Map([...previous, ...older.flat()].map(t => [t.id, t])).values()].sort((a, b) => b.created_at.localeCompare(a.created_at)))
      setMoreResults(older.some(page => page.length === 20))
    } catch (cause) { setError(String(cause)) }
  }
  const items = progress.value.items
  const taskTitle = (value: ProjectTask) => items.find(item => item.id === value.item_id)?.title || value.presentation?.message || project?.members.find(member => member.id === value.workflow_id)?.name || '业务处理'
  const questions = items.flatMap(item => item.questions.filter(q => !q.answer).map(question => ({ item, question })))
  const actionable = items.filter(i => ['planned', 'working'].includes(i.status) && !i.blocker && !i.questions.some(q => !q.answer))
  const ready = items.filter(i => i.availability !== 'not_ready')
  const activeMember = project?.members.find(m => m.id === workflowId)
  return <AppShell projectName={project?.name} navigation={<nav className={styles.projectNav} role="tablist" aria-label="项目页面">
      {([['overview', '项目进展与对话', MessageSquare], ['run', '运行工作流', Workflow], ['results', '业务结果', ChartNoAxesCombined], ['flow', '业务流程', Workflow], ['materials', '项目资料', Files], ['development', '开发详情', Wrench]] as const).map(([key, label, Icon]) =>
        <button key={key} role="tab" aria-label={label} title={label} aria-selected={tab === key} onClick={() => key === 'flow' ? void showFlow() : setTab(key)}><Icon size={17} /><span>{label}</span></button>)}
    </nav>}><main className={styles.page} onClickCapture={event => {
      const anchor = (event.target as HTMLElement).closest('a')
      if (!anchor || anchor.hasAttribute('download') || event.ctrlKey || event.metaKey || event.shiftKey) return
      const path = projectFileFromLink(id, anchor.getAttribute('href') || '')
      if (path) { event.preventDefault(); showFile(path, file ? fileTaskId : reader ? task?.id : '') }
    }}>
    <header className={styles.header}><div><span className={styles.eyebrow}>项目工作空间</span><h1>{project?.name || '正在读取项目…'}</h1>
      <p>手动搭建、直接运行，也可以与统筹一起改进。</p></div><ModelConnectionPanel base={base} role="vision" connected={false} running={false} onSaved={refresh} /></header>
    {project && <section className={styles.panel}><ProjectCapabilities projectId={id} enabled={Boolean(project.agent_modules_enabled)} onSaved={() => { setEditingFlow(false); void refresh() }} /></section>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {tab === 'run' && project && <ProjectRunPanel key={runWorkflowId} projectId={id} members={project.members} initialWorkflowId={runWorkflowId} onTask={updateManualTask} />}
    <div hidden={tab !== 'overview'} className={styles.projectHome}>
      <div><div className={styles.mobileProgress}><span>{ready.length} 项可试用 · {questions.length} 个待回答问题</span><button onClick={() => setProgressOpen(true)}>查看进展</button></div>
        <ProjectConversation id={id} projectName={project?.name} items={items} tasks={tasks} members={project?.members} focus={focus} onUpdated={refresh} onSent={clearFocus} onTask={taskId => void showTask(taskId)} onWorkflow={workflow => void showFlow(undefined, workflow)} onFeedback={(itemId, taskId) => talk(items.find(i => i.id === itemId), '', taskId)} />
      </div>
      <aside className={styles.progressRail} data-open={progressOpen} aria-label="项目进展摘要">
        <div className={styles.mobileProgress}><strong>项目进展</strong><button aria-label="关闭进展面板" onClick={() => setProgressOpen(false)}><X size={16} /></button></div>
        <section className={styles.panel} aria-label="当前项目进展"><h2>项目目标</h2><p>{progress.value.goal || '等待与你一起明确目标'}</p><h2>当前进展</h2>
          <p>{progress.value.summary || '尚未整理进展。可以直接问统筹“目前做到哪了”，由它读取本项目材料和已有结果。'}</p>
          <div className={styles.overviewFacts}><div><strong>{ready.length}</strong><span>项能力可以试用</span></div><div><strong>{actionable.length}</strong><span>项工作可继续推进</span></div><div><strong>{questions.length}</strong><span>个问题需要你的回答</span></div></div>
          {actionable.length > 0 && <p>下一步：{actionable[0].next_action}</p>}
          <button onClick={() => void showFlow()}>查看能力与需求对照</button>
        </section>
        {!!questions.length && <section className={styles.panel}><h2>待回答 · {questions.length}</h2>{questions.map(({ item, question }) => <details key={item.id + ':' + question.id} open={questions.length === 1 || undefined} className={styles.pendingQuestion}><summary>{question.text}</summary><small>{item.title} · {question.impact}</small><p>回答后：{question.next_action}</p><button onClick={() => talk(item, '', '', question.id)}>回答这个问题</button></details>)}</section>}
        {items.map(item => <section key={item.id} className={styles.capability} aria-label={item.title}>
          <div className={styles.capabilityHeading}><h2>{item.title}</h2><span className={styles.tag}>{availabilityNames[item.availability]}</span><span className={styles.tag}>{workNames[item.status]}</span></div>
          <p className={styles.capabilityGoal}>{item.goal}</p><MarkdownDocument source={item.summary} emptyLabel="统筹正在整理此项进展" />
          {item.deliverable && <p><strong>本次交付：</strong>{item.deliverable}</p>}
          {!!item.completion_criteria?.length && <details><summary>本次完成条件</summary><ul>{item.completion_criteria.map((condition, index) => <li key={index}>{condition}</li>)}</ul></details>}
          {item.next_action && <p><strong>接下来：</strong>{item.next_action}</p>}
          {item.blocker && <div className={styles.blocker}><strong>{item.blocker.kind === 'platform' || item.blocker.kind === 'runtime' ? '需要平台处理' : '等待业务条件'}</strong><p>{item.blocker.reason}</p><small>{item.blocker.owner} · {item.blocker.next_action}</small></div>}
          {item.questions.filter(q => Boolean(q.answer)).map(question => <div key={question.id} className={styles.question}><strong>{question.text}</strong><p>{question.impact}</p>
            {question.answer ? <p>你的回答：{question.answer}</p> : <small>回答后：{question.next_action}</small>}
            <button onClick={() => talk(item, '', '', question.id)}>{question.answer ? '补充回答' : '回答这个问题'}</button></div>)}
          <div className={styles.actions}>{item.availability !== 'not_ready' && <button className={styles.primary} onClick={() => talk(item, `我想试用「${item.title}」，请说明需要哪份业务输入，并使用已有工作流处理。`)}>试用</button>}
            {tasks.some(t => t.item_id === item.id) && <button onClick={() => void showTask(tasks.find(t => t.item_id === item.id)!.id)}>查看结果</button>}
            <button onClick={() => talk(item, '')}>反馈问题</button>{!!item.workflow_ids.length && <button onClick={() => void showFlow(item)}>查看业务流程</button>}</div>
          {!!item.results.length && <details><summary>相关结果与资料（{item.results.length}）</summary><div className={styles.resultLinks}>{item.results.map((result, index) => result.file_path && projectFileFromLink(id, resolveProjectLink(id, result.file_path))
            ? <button key={index} onClick={() => showFile(result.file_path, result.task_id)}>{result.label} ↗</button>
            : result.file_path
            ? <a key={index} href={download(result.file_path)} download>{result.label} ↓</a>
            : <button key={index} onClick={() => void showTask(result.task_id)}>{result.label} ↗</button>)}</div></details>}
        </section>)}
        <section className={styles.panel}><h2>最近结果</h2>{tasks.length ? tasks.slice(0, 3).map(t => <div key={t.id} className={styles.resultLinks}><button onClick={() => void showTask(t.id)}>{taskTitle(t)} · {taskNames[t.status] || t.status}</button></div>) : <p>试用后，结果会保留在这里。</p>}</section>
      </aside>
    </div>
    {tab === 'results' && <div>
      <section className={styles.panel}><h2>试用与业务处理</h2><p>选择一次处理查看结果，或把结果反馈给统筹。</p>
        <ul className={styles.list}>{tasks.map(t => <li key={t.id}><button onClick={() => void showTask(t.id)}>{taskTitle(t)} · {taskNames[t.status] || t.status}<small className={styles.taskTime}>{new Date(t.created_at).toLocaleString()}</small></button></li>)}</ul>
        {!tasks.length && <p>还没有客户试用结果。在业务能力卡片中选择“试用”，或直接与统筹对话。</p>}{moreResults && <button onClick={() => void olderResults()}>加载更早的结果</button>}
      </section>
    </div>}
    {reader && task && <ReadingDialog wide title={items.find(i => i.id === task.item_id)?.title || task.presentation?.message || '业务结果'} onClose={() => setReader(false)}>      <div className={styles.readerBody}>{task ? <><span className={styles.tag}>{taskNames[task.status] || task.status}</span>
        {task.error && <p className={styles.error}>{task.error}</p>}
        <ProjectTaskOutput projectId={id} task={task} />
        <div className={styles.actions}>{['running', 'queued'].includes(task.status) ? <button disabled={stopping} onClick={async () => { setStopping(true); try { const next = await api<ProjectTask>(`${base}/tasks/${task.id}/stop`, { method: 'POST' }); setTask(next); updateManualTask(next) } catch (cause) { setError(String(cause)) } finally { setStopping(false) } }}>{stopping ? '正在停止…' : '停止运行'}</button> : task.mode === 'workflow' && <button onClick={() => { setRunWorkflowId(task.workflow_id || id); setReader(false); setTab('run') }}>再次运行此工作流</button>}
          <button onClick={() => talk(items.find(i => i.id === task.item_id), '', task.id)}>反馈这个结果</button>{task.mode !== 'workflow' && ['waiting_input', 'interrupted', 'failed'].includes(task.status) && <button onClick={() => talk(items.find(i => i.id === task.item_id), '继续这个任务，请先检查已有结果和待补条件。', task.id)}>继续处理</button>}</div>
        {task.feedback_task_id && <button onClick={() => void showTask(task.feedback_task_id)}>查看修改前的结果</button>}
        <details><summary>实际运行与原始输入输出</summary><p>请求标识：{task.request_key}</p><pre>{JSON.stringify({ inputs: task.inputs, outputs: task.outputs }, null, 2)}</pre>{task.runs?.map(run => <p key={run.id}>{project?.members.find(m => m.id === run.application_id)?.name} · r{run.draft_revision} · {taskNames[run.status] || run.status}</p>)}
          <ProjectRunEvents key={task.id} runs={task.runs} members={project?.members || []} />
          <button onClick={() => { setDeveloperTaskId(task.id); setTab('development'); setReader(false) }}>打开运行详情</button></details></> : <p>选择一次业务处理，查看结果。</p>}</div>
</ReadingDialog>}
    {tab === 'flow' && topology && <><section className={styles.flowBar}><div className={styles.flowMembers}>{topology.members.filter(m => m.purpose === 'business').map(member => <button key={member.id} aria-label={member.name} title={member.name} aria-selected={workflowId === member.id} onClick={() => { setWorkflowId(member.id); setEditingFlow(false) }}>{member.id === id ? '主流程' : member.name}</button>)}</div></section>
      <div className={styles.actions}><input aria-label="新工作流名称" value={memberName} onChange={event => setMemberName(event.target.value)} placeholder="新工作流名称" />
        <button disabled={creatingMember || !memberName.trim()} onClick={async () => { setCreatingMember(true); try { const member = await api<ProjectMember>(base + '/members', { method: 'POST', body: JSON.stringify({ name: memberName.trim() }) }); setMemberName(''); await refresh(); await showFlow(undefined, member.id); setEditingFlow(true) } catch (cause) { setError(String(cause)) } finally { setCreatingMember(false) } }}>添加空白工作流</button>
        <button onClick={() => { setRunWorkflowId(workflowId || id); setTab('run') }}>运行此工作流</button></div>
      {editingFlow ? <><div className={styles.canvasHeading}><button onClick={() => setEditingFlow(false)}>返回业务说明与需求对照</button><h2>{activeMember?.name || '工作流画布'}</h2><Link href={`/applications/${workflowId}?tab=edit`} target="_blank">打开完整编辑器 <ArrowUpRight size={13} /></Link></div><iframe key={workflowId} title="业务工作流画布" className={styles.canvas} src={`/applications/${workflowId}?tab=edit&embedded=1`} /></>
        : <ProjectWorkflowOverview key={workflowId + flowItemId} initialItemId={flowItemId} projectId={id} selectedId={workflowId} progress={progress} topology={topology} onWorkflow={selected => { setWorkflowId(selected); setEditingFlow(false); window.scrollTo(0, 0) }} onTalk={talk} onTask={taskId => void showTask(taskId)} onFile={showFile} onRequirements={() => void showRequirements()} onEdit={() => setEditingFlow(true)} />}
    </>}
    {requirements !== null && <ReadingDialog wide title="当前需求文档" onClose={() => setRequirements(null)}><div className={styles.readerBody}><MarkdownDocument source={requirements} resolveLink={href => resolveProjectLink(id, href)} emptyLabel="尚未形成需求文档" /></div></ReadingDialog>}
    {tab === 'materials' && <section className={styles.panel}><h2>项目需求资料</h2><ProjectMaterials onOpenFile={showFile} id={id} /></section>}
    {tab === 'development' && <><p>成员草稿、建设测试、共享记录及完整运行历史。这里保留所有开发工具。</p><DeveloperTools id={id} initialTaskId={developerTaskId} /></>}
    {file && <ProjectFileReader projectId={id} path={file} onClose={() => setFile('')} onTask={fileTaskId ? () => { setFile(''); void showTask(fileTaskId) } : undefined} />}
  </main></AppShell>
}
