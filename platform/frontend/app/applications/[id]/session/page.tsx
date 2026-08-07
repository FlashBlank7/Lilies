'use client'

import '@xyflow/react/dist/style.css'
import Link from 'next/link'
import { Background, ReactFlow, type Edge, type Node } from '@xyflow/react'
import { use, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  type BuildTranscript,
  type Draft,
} from '@/lib/platform'
import OutputView from '@/app/components/OutputView'
import styles from './session.module.css'

type Build = {
  id: string
  status: string
  error?: string | null
  team_state: { revision: number; tasks: unknown[]; pending_question?: string | null }
}

type Application = { id: string; name: string; requirement: string }

type RunRecord = {
  id: string
  status: string
  state: { outputs?: Record<string, Record<string, unknown>>; waiting_node_id?: string | null; error?: string | null }
}

const ACTIVE = new Set(['queued', 'building'])

const STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  building: '搭建中',
  ready: '已就绪',
  published: '已发布',
  needs_attention: '需要处理',
  cancelled: '已取消',
}

export default function Session({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const [app, setApp] = useState<Application | null>(null)
  const [build, setBuild] = useState<Build | null>(null)
  const [transcript, setTranscript] = useState<BuildTranscript | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [latestRun, setLatestRun] = useState<RunRecord | null>(null)
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState(false)
  const [notice, setNotice] = useState('')
  const streamRef = useRef<HTMLDivElement>(null)
  const lastTurnRef = useRef(0)

  const refresh = useCallback(async () => {
    try {
      const [application, builds, currentDraft] = await Promise.all([
        api<Application>(`/api/v1/applications/${id}`),
        api<Build[]>(`/api/v1/applications/${id}/builds`),
        api<Draft>(`/api/v1/applications/${id}/draft`),
      ])
      setApp(application)
      setDraft(currentDraft)
      const latest = builds[0] || null
      setBuild(latest)
      if (latest) {
        const t = await api<BuildTranscript>(`/api/v1/builds/${latest.id}/transcript`)
        setTranscript(t)
      }
      const runs = await api<RunRecord[]>(`/api/v1/applications/${id}/runs?limit=1`).catch(() => [])
      setLatestRun(runs[0] || null)
    } catch {
      /* next poll retries */
    }
  }, [id])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 3000)
    return () => window.clearInterval(timer)
  }, [refresh])

  // Follow the newest turn like a chat.
  useEffect(() => {
    const turns = transcript?.records.length || 0
    if (turns > lastTurnRef.current) {
      lastTurnRef.current = turns
      streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight, behavior: 'smooth' })
    }
  }, [transcript])

  const building = Boolean(build && ACTIVE.has(build.status))

  async function send() {
    const text = message.trim()
    if (sending) return
    if (building && !text) return
    setSending(true)
    setNotice('')
    try {
      if (build && building) {
        await api(`/api/v1/builds/${build.id}/messages`, {
          method: 'POST',
          body: JSON.stringify({ message: text }),
        })
        setNotice('已送达，莉莉丝下一轮开工前会读到。')
      } else if (build) {
        await api(`/api/v1/builds/${build.id}/resume`, {
          method: 'POST',
          body: JSON.stringify({ message: text }),
        })
        setNotice('莉莉丝继续搭建中…')
      } else {
        const requirement = text
          ? `${app?.requirement || ''}\n\n补充要求：${text}`.trim()
          : app?.requirement || ''
        const started = await api<{ build_id: string }>(`/api/v1/applications/${id}/builds`, {
          method: 'POST',
          body: JSON.stringify({ requirement, auto_publish: false }),
        })
        setNotice('莉莉丝开始搭建…')
        setBuild({ id: started.build_id, status: 'queued', team_state: { revision: 0, tasks: [] } })
      }
      setMessage('')
      window.setTimeout(() => void refresh(), 800)
    } catch (error) {
      setNotice(String(error))
    } finally {
      setSending(false)
    }
  }

  const nodes = useMemo<Node[]>(() => {
    const list = draft?.snapshot.workflow.nodes || []
    const allZero = list.every(node => !node.position?.x && !node.position?.y)
    return list.map((node, index) => ({
      id: node.id,
      position: allZero
        ? { x: (index % 3) * 240, y: Math.floor(index / 3) * 120 }
        : { x: node.position?.x || 0, y: node.position?.y || 0 },
      data: { label: `${node.title || node.id} · ${node.type}` },
      draggable: false,
      selectable: false,
    }))
  }, [draft])

  const edges = useMemo<Edge[]>(() => {
    return (draft?.snapshot.workflow.edges || []).map(edge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
    }))
  }, [draft])

  const terminalOutputs = useMemo(() => {
    const perNode = latestRun?.state.outputs || {}
    const terminalIds = new Set(
      (draft?.snapshot.workflow.nodes || [])
        .filter(node => node.type === 'end' || node.type === 'answer')
        .map(node => node.id),
    )
    const merged: Record<string, unknown> = {}
    for (const [nodeId, value] of Object.entries(perNode)) {
      if ((terminalIds.size === 0 || terminalIds.has(nodeId)) && value && typeof value === 'object') {
        Object.assign(merged, value)
      }
    }
    return merged
  }, [latestRun, draft])

  const pendingQuestion = build?.status === 'needs_attention'
    ? build.team_state.pending_question || ''
    : ''
  const statusLabel = pendingQuestion
    ? '等你回复'
    : build ? STATUS_LABEL[build.status] || build.status : '未开始'

  // 交付说明：搭建完成后莉莉丝的最后一段发言，置顶备查。
  const deliveryNote = useMemo(() => {
    if (!build || ACTIVE.has(build.status)) return ''
    if (build.status !== 'ready' && build.status !== 'published') return ''
    const records = transcript?.records || []
    for (let index = records.length - 1; index >= 0; index -= 1) {
      const record = records[index]
      if (record.kind !== 'owner' && (record.text || '').trim().length > 40) {
        return record.text.trim()
      }
    }
    return ''
  }, [build, transcript])

  return <main className={styles.shell}>
    <header className={styles.topbar}>
      <Link href="/" className={styles.back}>← 工作台</Link>
      <div className={styles.title}>
        <strong>{app?.name || '…'}</strong>
        <span className={styles.status} data-status={build?.status || 'none'}>{statusLabel}</span>
        {draft && <small>草稿 r{draft.revision}</small>}
      </div>
      <nav className={styles.links}>
        <Link href={`/applications/${id}/pm`}>请监理</Link>
        <Link href={`/applications/${id}`}>画布编辑</Link>
        <Link href={`/runtime/${id}`}>试运行</Link>
      </nav>
    </header>

    <div className={styles.columns}>
      <section className={styles.chat} aria-label="莉莉丝会话">
        <div className={styles.chatHead}>
          <strong>莉莉丝会话</strong>
          <small>
            {transcript?.summary.available
              ? `${transcript.summary.turn_count} 轮 · ${transcript.summary.tool_call_count} 次工具调用${transcript.summary.failed_tool_call_count ? ` · ${transcript.summary.failed_tool_call_count} 次失败` : ''}`
              : '还没有会话记录'}
          </small>
        </div>
        {deliveryNote && <details className={styles.delivery}>
          <summary>交付说明（莉莉丝）</summary>
          <p>{deliveryNote}</p>
        </details>}
        <div className={styles.stream} ref={streamRef}>
          {!transcript?.summary.available && <div className={styles.empty}>
            <p>发一句话，莉莉丝就开始搭建；她的每一轮思考、每次工具调用都会出现在这里。</p>
          </div>}
          {transcript?.records.map((record, index) => record.kind === 'owner'
            ? <article className={styles.ownerTurn} key={index}>
                <div className={styles.ownerBubble}>{record.text}</div>
              </article>
            : <article className={styles.turn} key={index}>
            <div className={styles.turnHead}>
              <b>第 {record.turn} 轮</b>
              <span>{record.actor}</span>
              <small>r{record.draft_revision}</small>
            </div>
            {record.thinking && <details className={styles.thinking}>
              <summary>思考</summary>
              <pre>{record.thinking}</pre>
            </details>}
            {record.text && <p className={styles.speech}>{record.text}</p>}
            {record.tool_calls.map((call, index) => <details
              className={call.is_error ? `${styles.tool} ${styles.toolFailed}` : styles.tool}
              key={`${call.tool}-${index}`}
              open={call.is_error}
            >
              <summary><code>{call.tool}</code>{call.is_error && <em>失败</em>}</summary>
              <div>
                <small>参数</small>
                <pre>{JSON.stringify(call.arguments, null, 2)}</pre>
                <small>返回</small>
                <pre>{call.result}{call.truncated ? '\n…' : ''}</pre>
              </div>
            </details>)}
          </article>)}
          {pendingQuestion && <div className={styles.question}>
            <b>莉莉丝在等你回复</b>
            <p>{pendingQuestion}</p>
          </div>}
          {building && <div className={styles.working}><span/>莉莉丝正在搭建…</div>}
        </div>
        <div className={styles.composer}>
          {notice && <div className={styles.notice}>{notice}</div>}
          <div className={styles.composerRow}>
            <textarea
              placeholder={building
                ? '她正在搭建。想调整就直接说，下一轮开工前她会读到。'
                : pendingQuestion
                  ? '回答她的问题，构建会继续。'
                  : build
                    ? '想调整什么？直接说，莉莉丝会在当前工作流上继续。'
                    : '直接开始搭建，或补充一句要求再开始。'}
              value={message}
              onChange={event => setMessage(event.target.value)}
              onKeyDown={event => {
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) void send()
              }}
            />
            <button disabled={sending || (building && !message.trim())} onClick={() => void send()} type="button">
              {sending ? '发送中…' : building ? '插话' : build ? '继续搭建' : '开始搭建'}
            </button>
          </div>
        </div>
      </section>

      <section className={styles.right}>
        <div className={styles.canvas} aria-label="工作流实时预览">
          <div className={styles.panelHead}>
            <strong>工作流</strong>
            <small>{nodes.length} 个节点 · {edges.length} 条连线（实时）</small>
          </div>
          <div className={styles.flow}>
            {nodes.length
              ? <ReactFlow
                  edges={edges}
                  fitView
                  fitViewOptions={{ padding: 0.25 }}
                  nodes={nodes}
                  nodesConnectable={false}
                  nodesDraggable={false}
                  panOnDrag
                  proOptions={{ hideAttribution: true }}
                  zoomOnScroll
                >
                  <Background gap={18} size={1} />
                </ReactFlow>
              : <div className={styles.empty}><p>工作流还没有节点。开始搭建后，这里会实时长出来。</p></div>}
          </div>
        </div>
        <div className={styles.outputs} aria-label="最近一次运行输出">
          <div className={styles.panelHead}>
            <strong>输出</strong>
            <small>
              {latestRun
                ? `最近一次运行 · ${latestRun.status}${latestRun.state.waiting_node_id ? ` · 等待 ${latestRun.state.waiting_node_id}` : ''}`
                : '还没有运行记录'}
            </small>
          </div>
          <div className={styles.outputBody}>
            {latestRun
              ? Object.keys(terminalOutputs).length
                ? <OutputView outputs={terminalOutputs} />
                : <p className={styles.muted}>这次运行没有终端输出{latestRun.state.error ? `：${latestRun.state.error}` : '。'}</p>
              : <p className={styles.muted}>发布或有草稿后，去<Link href={`/runtime/${id}`}>试运行</Link>跑一次，结果会显示在这里。</p>}
          </div>
        </div>
      </section>
    </div>
  </main>
}
