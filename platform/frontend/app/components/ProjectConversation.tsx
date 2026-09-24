'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/lib/platform'
import { MarkdownDocument } from '@/lib/markdown'
import { resolveProjectLink } from '@/lib/project-links'
import { taskNames, type ProjectActivity as Activity, type ProjectTask, type ProjectMember, type ConversationFocus, type ProgressItem } from '@/lib/project-progress'
import ProjectActivity from './ProjectActivity'
import ModelConnectionPanel from './ModelConnectionPanel'
import AssistantSourcePanel from './AssistantSourcePanel'
import SaveMethod from './SaveMethod'
import ResultFeedback from './ResultFeedback'
import { useAccount } from './AuthBoundary'
import { useOnboarding } from './Onboarding'
import ReadingDialog from './ReadingDialog'
import ModelingPanel, { type ModelingContext } from './ModelingPanel'
import ConversationWorkflowCreator, {type WorkflowCard} from './ConversationWorkflowCreator'
import workflowStyles from './conversation-workflow.module.css'
import { FileText, ArrowUpRight } from 'lucide-react'
import styles from '@/app/projects/projects.module.css'

type Event = { id: string; kind: string; text: string; time: string; result?: string; arguments?: string; success?: boolean; request_id?: string; item_id?: string; task_id?: string; purpose?: string; workflow?: WorkflowCard }
type Session = {
  provider: string | null; queue_reason?: string; status: string; error: string; revision: number; events: Event[]
  has_more: boolean; first_cursor: string; last_cursor: string; active_item_id?: string
  project_task_id?: string; conversation_context?: { item_id?: string; task_id?: string }
  request_id?: string; current_activity?: Activity | null; request_activity?: Record<string, Activity>
  requirements: { status: string; document: string; revision: number }
}

export default function ProjectConversation({ id, conversationId, projectName, canConfigureModel = true, items, tasks = [], members = [], focus, onUpdated, onSent, onTask, onWorkflow, onFeedback }: {
  id: string; conversationId?: string; projectName?: string; canConfigureModel?: boolean; tasks?: ProjectTask[]; members?: ProjectMember[]; onTask?: (id: string) => void; onWorkflow?: (id: string) => void; onFeedback?: (itemId: string, taskId: string) => void; items: ProgressItem[]; focus?: ConversationFocus; onUpdated: () => unknown; onSent: () => void
}) {
  const account = useAccount()
  const guide = useOnboarding()
  const base = '/api/v1/projects/' + id
  const conversationBase = base + (conversationId ? '/conversations/' + conversationId : '/conversation')
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const [reader, setReader] = useState<{ title: string; text: string } | null>(null)
  const [modelingContext, setModelingContext] = useState<ModelingContext | null>(null)
  const [summaries, setSummaries] = useState<Record<string, Activity>>({})
  const [sentNotice, setSentNotice] = useState('')
  const [session, setSession] = useState<Session | null>(null)
  const [events, setEvents] = useState<Event[]>([])
  const [older, setOlder] = useState(false)
  const [tools, setTools] = useState<Event[]>([])
  const [moreTools, setMoreTools] = useState(false)
  const [showTools, setShowTools] = useState(false)
  const [message, setMessage] = useState('')
  const [mode, setMode] = useState<'task'|'workflow'>('task')
  const [editWorkflow, setEditWorkflow] = useState<WorkflowCard>()
  const messageRef = useRef('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [connectionError, setConnectionError] = useState('')
  const draftKey = (account ? 'lilies:user:' + account.id + ':project:' : 'lilies:project:') + id + (conversationId && conversationId !== 'legacy' ? ':conversation:' + conversationId : '') + ':draft'
  const restored = useRef(false)
  const cursor = useRef('')
  const revision = useRef(-1)
  const composer = useRef<HTMLTextAreaElement>(null)
  const historyElement = useRef<HTMLDivElement>(null)
  const followBottom = useRef(true)
  const merge = (previous: Event[], incoming: Event[]) => [...new Map([...previous, ...incoming].map(e => [e.id, e])).values()]
  const refresh = useCallback(async () => {
    try {
      let page: Session
      try {
        page = await api<Session>(conversationBase + (cursor.current ? '?after=' + encodeURIComponent(cursor.current) : ''))
      } catch (cause) {
        if (!cursor.current || !cause || typeof cause !== 'object'
          || !('status' in cause) || cause.status !== 422
          || !('detail' in cause) || cause.detail !== '会话分页位置不存在') throw cause
        // A prior connection may hold an obsolete cursor. Keep the displayed
        // history and composer, then resume incrementally from saved messages.
        cursor.current = ''
        page = await api<Session>(conversationBase)
      }
      if (!mounted.current) return
      if (!cursor.current) setOlder(page.has_more)
      if (page.last_cursor) cursor.current = page.last_cursor
      setSession(page); setSummaries(previous => ({ ...previous, ...page.request_activity })); setEvents(previous => merge(previous, page.events)); setConnectionError('')
      if (revision.current !== page.revision) { revision.current = page.revision; void onUpdated() }
    } catch (cause) { setConnectionError(String(cause)) }
  }, [conversationBase, onUpdated])
  useEffect(() => { try { const saved = sessionStorage.getItem(draftKey); if (saved) {setMessage(saved);messageRef.current=saved} const context = sessionStorage.getItem(draftKey + ':modeling'); if (context) setModelingContext(JSON.parse(context)); if(sessionStorage.getItem(draftKey+':mode')==='workflow')setMode('workflow') } catch {} }, [draftKey])
  useEffect(() => { void refresh(); const timer = window.setInterval(() => void refresh(), 1500); return () => window.clearInterval(timer) }, [refresh])
  useEffect(() => { if (focus) { if (focus.message !== undefined) { const previous = messageRef.current; updateDraft(focus.label === '项目空间' && previous.trim() && !previous.includes(focus.message) ? previous + '\n\n' + focus.message : focus.label === '项目空间' && previous.includes(focus.message) ? previous : focus.message); } changeMode(focus.mode || 'task'); composer.current?.focus() } }, [focus])
  useEffect(() => {
    const el = historyElement.current
    if (!el || !events.length) return
    if (!restored.current) {
      restored.current = true
      try { const saved = sessionStorage.getItem(draftKey + ':scroll'); if (saved !== null) { el.scrollTop = Number(saved); followBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; return } } catch {}
    }
    if (followBottom.current) el.scrollTop = el.scrollHeight
  }, [events.length, draftKey])
  function updateDraft(text: string) { setMessage(text); messageRef.current=text; try { sessionStorage.setItem(draftKey, text) } catch {} }
  function changeMode(value: 'task'|'workflow') {setMode(value);try{sessionStorage.setItem(draftKey+':mode',value)}catch{}}
  function updateModelingContext(value: ModelingContext | null) { setModelingContext(value); try { if (value) sessionStorage.setItem(draftKey + ':modeling', JSON.stringify(value)); else sessionStorage.removeItem(draftKey + ':modeling') } catch {} }
  async function act(action: () => Promise<unknown>) {
    setBusy(true); setError('')
    try { await action(); await refresh() } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  function send(text = message, resume = false) {
    void act(async () => {
      const requestBody = { message: text,
        item_id: focus?.item_id || modelingContext?.item_id || (resume ? session?.active_item_id || session?.conversation_context?.item_id : '') || '',
        question_id: resume ? '' : focus?.question_id || '',
        task_id: focus?.task_id || modelingContext?.task_id || (resume ? session?.conversation_context?.task_id || session?.project_task_id : '') || '',
        ...(modelingContext ? { dataset_id: modelingContext.dataset_id || '', study_id: modelingContext.study_id || '', candidate_id: modelingContext.candidate_id || '' } : {}) }
      const signature = JSON.stringify(requestBody)
      let pending: {signature:string;key:string} | null = null
      try { pending = JSON.parse(sessionStorage.getItem(draftKey + ':request') || 'null') } catch {}
      const request_key = pending?.signature === signature ? pending.key : (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`)
      try { sessionStorage.setItem(draftKey + ':request', JSON.stringify({signature,key:request_key})) } catch {}
      await api(conversationBase + '/messages', {method:'POST',body:JSON.stringify({...requestBody,...(session?.provider === 'official' ? {request_key} : {})})})
      try { sessionStorage.removeItem(draftKey + ':request') } catch {}
      if (!mounted.current) return
      guide.mark('conversation', id); updateDraft(''); updateModelingContext(null); onSent(); followBottom.current = true; setSentNotice(running ? '补充已发送，统筹会接着处理。' : '请求已发送。')
    })
  }
  async function history() {
    const page = await api<Session>(conversationBase + '?before=' + encodeURIComponent(events[0]?.id || ''))
    setSummaries(previous => ({ ...page.request_activity, ...previous })); setEvents(previous => merge(page.events, previous)); setOlder(page.has_more); followBottom.current = false
  }
  async function loadTools(before = '') {
    const page = await api<Session>(conversationBase + '?kind=tools' + (before ? '&before=' + encodeURIComponent(before) : ''))
    setTools(previous => before ? merge(page.events, previous) : page.events); setMoreTools(page.has_more); setShowTools(true)
  }
  const running = session && ['connecting', 'running', 'queued', 'waiting_compute'].includes(session.status)
  const activeItem = items.find(i => i.id === session?.active_item_id)
  return <section className={styles.conversation} aria-label="项目统筹对话">
    <div className={styles.conversationHeader}><div><h2>{projectName || '和统筹继续沟通'}</h2><small>{running ? activeItem ? '正在处理：' + activeItem.title : '统筹正在处理你的请求' : '查看进度、试用已有能力，或告诉我哪里需要调整'}</small></div>
      {running && <button disabled={busy} onClick={() => void act(() => api(conversationId ? conversationBase + '/stop' : base + '/agent-session/stop', { method: 'POST' }))}>停止</button>}</div>
    {canConfigureModel && <AssistantSourcePanel base={base} running={Boolean(running)} onSaved={refresh} />}
    {session?.provider === 'official' && <p>官方智能体 · {session.status === 'queued' ? session.queue_reason || '排队中' : session.status === 'waiting_compute' ? '等待计算完成' : '已连接'}</p>}
    {canConfigureModel ? <ModelConnectionPanel base={base} connected={Boolean(session?.provider)} running={Boolean(running)} onSaved={refresh} /> : !session?.provider && <p>请联系项目负责人配置模型连接，随后即可使用项目对话。</p>}
    {session?.provider && !['api', 'official'].includes(session.provider) && <p>此项目的旧会话使用外部 Agent。请在模型设置中连接模型 API，由 Lilies 继续处理；原有记录会保留。</p>}
    {session?.requirements?.document && <div className={styles.requirements}><FileText size={14} />
      <button onClick={() => setReader({ title: '当前需求文档', text: session.requirements.document })}>当前需求文档</button>
      <small>第 {session.requirements.revision} 版</small>
    </div>}
    <div ref={historyElement} className={styles.chatHistory} onScroll={e => { const el = e.currentTarget; followBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; try { sessionStorage.setItem(draftKey + ':scroll', String(el.scrollTop)) } catch {} }}>
      {older && <button onClick={() => void act(history)} disabled={busy}>加载更早的对话</button>}
      {!events.length && <p>从“请分析这些资料”开始；已有项目可以直接问“现在做到哪了”。</p>}
      {events.map((event, index) => {
        if (event.kind === 'result' && event.purpose === 'build_test') return null
        const result = event.task_id ? tasks.find(t => t.id === event.task_id) : undefined
        const lastInRequest = event.request_id && !events.slice(index + 1).some(e => e.request_id === event.request_id)
        return <div key={event.id}>
          {event.kind === 'result' && event.task_id ? <article className={styles.resultCard} aria-label="关联业务结果"><h3><FileText size={15} /> {event.text || '业务结果'}</h3>
            {result && <span className={styles.tag}>{taskNames[result.status] || result.status}</span>}
            <div className={styles.actions}><button onClick={() => onTask?.(event.task_id!)}>查看结果 <ArrowUpRight size={13} /></button><button onClick={() => onFeedback?.(event.item_id || result?.item_id || '', event.task_id!)}>反馈这个结果</button>
              {result?.feedback_task_id && <button onClick={() => onTask?.(result.feedback_task_id)}>查看修改前的结果</button>}</div>
          </article> : <article className={event.kind === 'user' ? styles.chatUser : styles.chatAssistant}><small>{event.kind === 'user' ? '你' : '项目统筹'}</small><MarkdownDocument source={event.text} emptyLabel="" resolveLink={href => resolveProjectLink(id, href)} />
            {event.kind !== 'user' && event.text.length > 900 && <button onClick={() => setReader({ title: '统筹报告', text: event.text })}>独立阅读全文 <ArrowUpRight size={13} /></button>}
          </article>}
          {event.workflow && <article className={styles.resultCard} aria-label="已生成工作流">
            <h3>{event.workflow.name}</h3><p>已保存到项目空间 · 生成修订 {event.workflow.revision} · {event.workflow.node_count} 个节点</p>
            <details><summary>查看生成步骤</summary><ul>{event.workflow.nodes.map(node=><li key={node.id}>{node.title}</li>)}</ul><p>分支和连线请在画布中查看。</p></details>
            <div className={styles.actions}><button onClick={()=>onWorkflow?.(event.workflow!.id)}>打开工作流画布</button>
              <button onClick={()=>{setEditWorkflow(event.workflow);changeMode('workflow');composer.current?.focus()}}>继续修改此流程</button>
              <button onClick={()=>{changeMode('task');updateDraft(`请调用项目工作流「${event.workflow!.name}」（${event.workflow!.id}），使用项目空间中的资料完成任务。先查看输入要求，有不明确的信息再向我询问。`);composer.current?.focus()}}>通过智能体使用</button></div>
          </article>}
          {event.kind === 'assistant' && event.text && <><SaveMethod projectId={id} text={event.text}/>{lastInRequest && event.request_id && <ResultFeedback base={conversationBase} requestId={event.request_id}/>}</>}
          {lastInRequest && summaries[event.request_id!] && <ProjectActivity projectId={id} conversationId={conversationId} workflowNames={Object.fromEntries(members.map(m => [m.id, m.name]))} active={Boolean(running) && session?.request_id === event.request_id} requestId={event.request_id} current={summaries[event.request_id!]} onTask={onTask} onWorkflow={onWorkflow} />}
        </div>
      })}
      {session?.current_activity && !events.some(e => e.request_id === session.request_id) && <ProjectActivity projectId={id} conversationId={conversationId} workflowNames={Object.fromEntries(members.map(m => [m.id, m.name]))} active={Boolean(running)} requestId={session.request_id} current={session.current_activity} onTask={onTask} onWorkflow={onWorkflow} />}
    </div>
    <ModelingPanel compact projectId={id} onTask={onTask} onContext={(context, text) => { updateModelingContext(context); if (text !== undefined && !message.trim()) updateDraft(text); composer.current?.focus() }} />
    {Boolean(error || session?.error) && <p role="alert" className={`${styles.error} ${styles.notice}`}>{error || session?.error}</p>}
    {connectionError && <div role="alert" className={`${styles.error} ${styles.notice}`}>连接暂时中断，已保存的对话和结果仍保留。<button onClick={() => void refresh()}>重新连接</button><details><summary>连接详情</summary>{connectionError}</details></div>}
    {session?.status === 'interrupted' && !session.error && <p className={`${styles.focus} ${styles.notice}`}>已停止，进展和结果已保留。{activeItem?.next_action ? '继续后：' + activeItem.next_action : '点击继续推进接着处理。'}</p>}
    {!running && activeItem?.status === 'waiting' && <p className={`${styles.focus} ${styles.notice}`}>等待补充：{activeItem.questions.find(q => !q.answer)?.text || activeItem.blocker?.reason} {activeItem.next_action}</p>}
    <div className={styles.composer} data-guide-anchor="conversation-compose">
      <div className={workflowStyles.modes} role="group" aria-label="对话用途" tabIndex={-1} data-guide="next">
        <button aria-pressed={mode==='task'} onClick={()=>{if(mode!=='task')guide.mark('next',id);changeMode('task')}}>完成任务</button>
        <button aria-pressed={mode==='workflow'} onClick={()=>{if(mode!=='workflow')guide.mark('next',id);changeMode('workflow')}}>创建工作流</button>
      </div>
      {modelingContext && <div className={styles.focus}><span>关于建模：{modelingContext.label}</span><button onClick={() => updateModelingContext(null)}>取消建模关联</button></div>}
      {running && !focus && <div className={styles.focus}>补充将发送到：{activeItem?.title || '当前处理的请求'}</div>}
      {focus && <div className={styles.focus}><span>关于：{focus.label}</span><button onClick={onSent}>取消关联</button></div>}
      <textarea ref={composer} aria-label="给项目统筹的消息" value={message} onChange={e => updateDraft(e.target.value)} placeholder={mode==='workflow'?'例如：把刚才的数据分析和模型预测组合成一条新工作流':'例如：调用项目里的质量预测流程，处理刚上传的数据'} />
      {sentNotice && <small role="status">{sentNotice}</small>}
      <ConversationWorkflowCreator projectId={id} conversationId={conversationId} visible={mode==='workflow'} storageKey={draftKey} message={message} members={members} context={modelingContext} taskId={focus?.task_id} target={editWorkflow} onClearTarget={()=>setEditWorkflow(undefined)} onWorkflow={onWorkflow} onSaved={submitted=>{if(submitted && messageRef.current===submitted)updateDraft('');void refresh();void onUpdated();followBottom.current=true}} />
      {mode==='task' && <div className={styles.actions}><button className={styles.primary} disabled={busy || !['api', 'official'].includes(session?.provider || '') || !message.trim()} onClick={() => send()}>{running ? '发送补充' : '发送'}</button>
        {!running && <button disabled={busy || !['api', 'official'].includes(session?.provider || '')} onClick={() => send('继续推进已授权的剩余工作；先读取当前进展与最近反馈。', true)}>继续推进</button>}</div>
      }
    </div>
    <details className={styles.tools} onToggle={e => { if (e.currentTarget.open && !showTools) void act(() => loadTools()) }}><summary>查看操作记录</summary>
      {moreTools && <button disabled={busy} onClick={() => void act(() => loadTools(tools[0]?.id))}>加载更早的操作</button>}
      {tools.map(event => <details key={event.id}><summary>{event.text} · {event.kind === 'tool' ? event.success ? '完成' : '失败' : '过程'}</summary><pre>{event.result || event.arguments}</pre></details>)}
    </details>
    {reader && <ReadingDialog wide title={reader.title} onClose={() => setReader(null)}><MarkdownDocument source={reader.text} emptyLabel="" resolveLink={href => resolveProjectLink(id, href)} /></ReadingDialog>}
  </section>
}
