'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/lib/platform'
import { MarkdownDocument } from '@/lib/markdown'
import styles from './LocalAgentSession.module.css'

export type AgentMode = 'loading' | 'none' | 'codex' | 'classic'
type Agent = { id: string; detected: boolean; supported: boolean; path?: string; version?: string }
type Event = { id: string; kind: string; text: string; time: string; arguments?: string; result?: string; success?: boolean }
type Requirements = {
  status: string; revision: number; document: string
  turns: { analysis: { detected_goal: string; questions: { id: string; question: string; why?: string }[] } }[]
}
type Session = {
  provider: 'codex' | 'classic' | null; status: string; phase: 'discuss' | 'build' | 'operate';
  revision: number; events: Event[]; error: string; executable?: string; model?: string
  requirements?: Requirements
}
const labels: Record<string, string> = {
  project_file: '查看或整理项目文件', block_catalog: '查阅积木说明',
  workflow_draft: '查看或编辑工作流', workflow_run: '运行与检查工作流', requirements_submit: '整理需求理解',
  project_workflows: '管理成员工作流', project_records: '查看业务记录', project_task_result: '汇报任务结果',
}

export default function LocalAgentSession({ applicationId, onModeChange, onUpdated, project = false }: {
  applicationId: string; onModeChange: (mode: AgentMode) => void; onUpdated: () => unknown
  project?: boolean
}) {
  const [session, setSession] = useState<Session | null>(null)
  const [agents, setAgents] = useState<Agent[]>([])
  const [choice, setChoice] = useState('codex')
  const [executable, setExecutable] = useState('')
  const [model, setModel] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(false)
  const revision = useRef(-1)
  const configured = useRef(false)
  const base = `/api/v1/${project ? 'projects' : 'applications'}/${applicationId}`

  const refresh = useCallback(async () => {
    try {
      const state = await api<Session>(`${base}/agent-session`)
      setSession(state)
      onModeChange(state.provider || 'none')
      if (!configured.current && state.provider) {
        configured.current = true
        setChoice(state.provider)
        setExecutable(state.executable || '')
        setModel(state.model || '')
      }
      if (revision.current !== state.revision) {
        revision.current = state.revision
        void onUpdated()
      }
    } catch (cause) { setError(String(cause)) }
  }, [base, onModeChange, onUpdated])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 1500)
    void api<{ agents: Agent[] }>('/api/v1/local-agents').then(result => setAgents(result.agents))
      .catch(cause => setError(String(cause)))
    return () => window.clearInterval(timer)
  }, [refresh])

  const running = session?.status === 'connecting' || session?.status === 'running'
  const requirements = session?.requirements
  const confirmed = requirements?.status === 'confirmed'
  const analysis = requirements?.turns.at(-1)?.analysis

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setError('')
    try { await action(); await refresh() }
    catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }

  function select() {
    void act(async () => {
      await api(`${base}/agent-session`, { method: 'PUT', body: JSON.stringify({ provider: choice, executable, model }) })
      setEditing(false)
    })
  }

  function send(intent: 'discuss' | 'build' | 'operate') {
    const text = message.trim() || (intent === 'build'
      ? '开始搭建。请根据已确认需求编辑、运行并检查工作流。'
      : session?.events.length ? '请从本项目上次停止的位置继续。' : '请先阅读需求包，说明你的理解和需要与我核对的问题。')
    void act(async () => {
      await api(`${base}/agent-session/messages`, { method: 'POST', body: JSON.stringify({ message: text, intent }) })
      setMessage('')
    })
  }

  return <section className={styles.panel} aria-label="项目 Agent">
    <div className={styles.heading}>
      <strong>{project ? '项目统筹' : '搭建者'}{session?.provider ? ` · ${session.provider === 'codex' ? '本机 Codex' : '平台内置 Agent'}` : ''}</strong>
      {session?.provider && <button type="button" disabled={running} onClick={() => setEditing(!editing)}>更换</button>}
    </div>
    {!session && !error && <p>正在读取本机 Agent 配置…</p>}
    {session && (!session.provider || editing) && <div className={styles.setup}>
      <p>选择谁来阅读资料、与你沟通并搭建工作流。选择后由你发出第一条指令。</p>
      <label>本项目搭建者<select aria-label="本项目搭建者" value={choice} onChange={e => setChoice(e.target.value)}>
        <option value="codex">本机 Codex</option>
        {!project && <option value="classic">平台内置 Agent</option>}
        <option value="claude" disabled>Claude Code（后续接入）</option>
      </select></label>
      {choice === 'codex' && <>
        <small>沿用本机 Codex 的登录。点击分析或搭建后才开始运行。</small>
        <small>{agents.find(a => a.id === 'codex')?.detected
          ? `已检测到 ${agents.find(a => a.id === 'codex')?.version}` : '可手动指定 Codex 程序路径'}</small>
        <label>Codex 路径<input aria-label="Codex 路径" value={executable} placeholder="留空使用本机检测结果"
          onChange={e => setExecutable(e.target.value)} /></label>
        <label>模型<input aria-label="Codex 模型" value={model} placeholder="留空使用 Codex 默认模型"
          onChange={e => setModel(e.target.value)} /></label>
      </>}
      {agents.find(a => a.id === 'claude')?.detected && <small>也检测到 Claude Code；第一期暂不启动。</small>}
      <button type="button" onClick={select} disabled={busy}>使用{choice === 'codex' ? '本机 Codex' : '平台内置 Agent'}</button>
    </div>}
    {error && <p className={styles.error} role="alert">{error}</p>}
    {session?.provider === 'codex' && <>
      {session.events.length === 0 && <p>Codex 将使用本项目需求包和平台积木说明，从需求沟通开始。</p>}
      {analysis && session.phase === 'discuss' && <div className={styles.understanding}>
        <strong>当前需求理解</strong><p>{analysis.detected_goal}</p>
        {analysis.questions.length > 0 && <ul>{analysis.questions.map(question =>
          <li key={question.id}>{question.question}{question.why && <small>{question.why}</small>}</li>)}</ul>}
      </div>}
      {requirements?.document && <details open={requirements.status === 'review'} className={styles.document}>
        <summary>{confirmed ? '已确认的需求文档' : '核对需求文档'}</summary>
        <MarkdownDocument source={requirements.document} emptyLabel="" />
        {requirements.status === 'review' && <button type="button" disabled={busy || running} onClick={() => void act(async () => {
          await api(`${base}/requirements/confirm`, { method: 'POST', body: JSON.stringify({ revision: requirements.revision }) })
          void onUpdated()
        })}>理解正确，确认需求文档</button>}
      </details>}
      <div className={styles.events} aria-label="Codex 项目会话">
        {session.events.filter(event => !project || !['tool', 'tool_started'].includes(event.kind)).map(event => event.kind === 'tool' || event.kind === 'tool_started'
          ? <details key={event.id} className={styles.tool}>
              <summary>{labels[event.text] || event.text} · {event.kind === 'tool_started' ? '开始' : event.success ? '完成' : '失败'}</summary>
              <pre>{event.arguments || event.result}</pre>
            </details>
          : <article key={event.id} className={event.kind === 'user' ? styles.user : styles.message}>
              <small>{event.kind === 'user' ? '你' : event.kind === 'assistant' ? 'Codex' : '平台'}</small>
              <p>{event.text}</p>
            </article>)}
        {project && session.events.some(event => event.kind === 'tool') && <details className={styles.tool}>
          <summary>工具调用记录 · {session.events.filter(event => event.kind === 'tool').length} 次</summary>
          {session.events.filter(event => event.kind === 'tool').map(event => <details key={event.id}>
            <summary>{labels[event.text] || event.text} · {event.success ? '完成' : '失败'}</summary><pre>{event.result}</pre>
          </details>)}
        </details>}
      </div>
      {session.error && <p className={styles.error} role="alert">{session.error}</p>}
      <div className={styles.composer}>
        {running && <p role="status">{session.status === 'connecting' ? '正在连接本机 Codex…' : 'Codex 正在处理…'}</p>}
        <textarea aria-label="给 Codex 的消息" value={message} onChange={e => setMessage(e.target.value)}
          placeholder={running ? '补充要求，或点击停止' : confirmed ? '补充搭建要求，或说明想调整什么' : '回复问题、纠正理解，或补充材料说明'} />
        <div className={styles.actions}>
          {running ? <>
            <button type="button" disabled={busy || !message.trim() || session.status === 'connecting'} onClick={() => send(session.phase)}>发送补充</button>
            <button type="button" disabled={busy} onClick={() => void act(() => api(`${base}/agent-session/stop`, { method: 'POST' }))}>停止</button>
          </> : <>
            {confirmed && <button type="button" disabled={busy} onClick={() => send('build')}>{session.phase === 'build' ? '继续搭建' : '开始搭建'}</button>}
            <button type="button" disabled={busy || (confirmed && !message.trim())} onClick={() => send('discuss')}>
              {confirmed ? '调整需求' : session.events.length ? '继续沟通' : '分析资料'}
            </button>
          </>}
        </div>
      </div>
    </>}
  </section>
}
