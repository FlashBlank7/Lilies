'use client'

import '@xyflow/react/dist/style.css'
import Link from 'next/link'
import { Background, ReactFlow, type Edge, type Node } from '@xyflow/react'
import { use, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  api,
  withFrontendToken,
  type BuildTranscript,
  type BuildTranscriptTurn,
  type Draft,
} from '@/lib/platform'
import OutputView from '@/app/components/OutputView'
import BuildAdvancedConfig, { defaultBuildOptions, buildOptionsPayload, type BuildOptions } from '@/app/components/BuildAdvancedConfig'
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

type WorkspaceFile = { path: string; size: number }

const ACTIVE = new Set(['queued', 'building'])

// 纯工具轮没有可读文字时，把工具调用翻译成一句业主能懂的动作描述。
function describeToolCall(tool: string, args: Record<string, unknown>): string {
  const node = (args.node && typeof args.node === 'object' ? args.node : args) as Record<string, unknown>
  const name = String(node.title || node.node_id || args.node_id || args.id || '').trim()
  const quoted = name ? `「${name}」` : ''
  switch (tool) {
    case 'draft_add_node': return `添加了${quoted || '一个'}环节`
    case 'draft_update_node': return `调整了${quoted || '环节'}的配置`
    case 'draft_remove_node': return `移除了${quoted || '一个'}环节`
    case 'draft_upsert_agent': return '配置了智能体环节'
    case 'draft_connect': return '接好了环节之间的流向'
    case 'draft_remove_edge': return '断开了一条流向'
    case 'draft_inspect': case 'draft_validate': return '核对了当前草稿'
    case 'draft_publish': return '发布了正式版'
    case 'define_view': return `定义了使用界面方案${quoted}`
    case 'test_add': return '准备了测试用例'
    case 'test_remove': return '清理了测试用例'
    case 'test_run': return '跑了一遍测试'
    case 'run_inspect': return '检查了运行流水账'
    case 'catalog_get': case 'catalog_search': case 'manual_get': case 'manual_search': return '查了积木手册'
    case 'build_plan': return '更新了施工计划'
    case 'architecture_blueprint': return '画了架构草图'
    case 'template_expand': return '套用了现成模板'
    case 'template_list': case 'template_suggestions': return '翻了模板库'
    case 'spawn_teammate': case 'task': case 'send_message': return '安排了帮手分工'
    case 'ask_owner': return '向你提了一个问题'
    default:
      if (tool.startsWith('knowledge')) return '整理了知识库'
      if (tool.startsWith('workspace')) return '处理了工作区文件'
      if (tool.startsWith('connector')) return '调试了外部连接'
      return '做了一步内部操作'
  }
}

function describeTurn(record: { tool_calls: Array<{ tool: string; arguments: Record<string, unknown> }> }): string {
  const phrases: string[] = []
  for (const call of record.tool_calls) {
    const phrase = describeToolCall(call.tool, call.arguments || {})
    if (!phrases.includes(phrase)) phrases.push(phrase)
  }
  if (!phrases.length) return ''
  return phrases.slice(0, 3).join('，') + (phrases.length > 3 ? '……' : '')
}

// 长会话按"工作段"折叠：每条业主发言是天然的段界。少于这个数就不分组。
const SEGMENT_MIN_RECORDS = 15

type Segment = { start: number; records: BuildTranscriptTurn[]; turns: number; startedAt: string }

function formatSegmentTime(iso: string): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

// 单条记录的渲染：业主气泡 / 事件徽章 / 智能体轮次卡片。
function renderRecord(record: BuildTranscriptTurn, key: number) {
  return record.kind === 'owner'
    ? <article className={styles.ownerTurn} key={key}>
        <div className={styles.ownerBubble}>{record.text}</div>
      </article>
    : record.kind === 'event'
    ? <div className={styles.eventBadge} data-event={record.event || ''} key={key}>
        {record.text}
      </div>
    : <article className={styles.turn} key={key}>
    <div className={styles.turnHead}>
      <b>第 {record.turn} 轮</b>
      <span>{record.actor}</span>
      <small>r{record.draft_revision}</small>
    </div>
    {record.thinking && <details className={styles.thinking}>
      <summary>思考<i>{record.thinking.replace(/\s+/g, ' ').slice(0, 48)}…</i></summary>
      <pre>{record.thinking}</pre>
    </details>}
    {record.text
      ? <p className={styles.speech}>{record.text}</p>
      : record.tool_calls.length > 0 && <p className={styles.action}>{describeTurn(record)}</p>}
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
  </article>
}

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
  // 会话页默认不自动发布：搭好后先看交付说明再决定。
  const [buildOptions, setBuildOptions] = useState<BuildOptions>(() => defaultBuildOptions(false))
  const [exportOpen, setExportOpen] = useState(false)
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFile[] | null>(null)
  const [workspaceError, setWorkspaceError] = useState('')
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

  // 展开导出面板时才去拉工作区文件列表，收起时不打扰后端。
  function toggleExport() {
    const next = !exportOpen
    setExportOpen(next)
    if (!next) return
    setWorkspaceFiles(null)
    setWorkspaceError('')
    void api<{ files?: WorkspaceFile[] } | WorkspaceFile[]>(`/api/v1/applications/${id}/workspace/files`)
      .then(result => setWorkspaceFiles(Array.isArray(result) ? result : result.files || []))
      .catch(error => {
        setWorkspaceFiles([])
        setWorkspaceError(`工作区文件读不出来：${String(error)}`)
      })
  }

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
        const options = buildOptionsPayload(buildOptions)
        if (options.error !== undefined) {
          setNotice(options.error)
          return
        }
        const requirement = text
          ? `${app?.requirement || ''}\n\n补充要求：${text}`.trim()
          : app?.requirement || ''
        const started = await api<{ build_id: string }>(`/api/v1/applications/${id}/builds`, {
          method: 'POST',
          body: JSON.stringify({ requirement, ...options.payload }),
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

  // 记录多了就按业主发言切成工作段，只展开最后一段；记录少时保持原样平铺。
  const segments = useMemo<Segment[]>(() => {
    const records = transcript?.records || []
    if (records.length < SEGMENT_MIN_RECORDS) return []
    const result: Segment[] = []
    records.forEach((record, index) => {
      if (record.kind === 'owner' || result.length === 0) {
        result.push({ start: index, records: [], turns: 0, startedAt: formatSegmentTime(record.recorded_at || '') })
      }
      const segment = result[result.length - 1]
      segment.records.push(record)
      if (record.kind !== 'owner' && record.kind !== 'event') segment.turns += 1
    })
    return result
  }, [transcript])

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
      if (record.kind !== 'owner' && record.kind !== 'event' && (record.text || '').trim().length > 40) {
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
        <button
          className={styles.share}
          onClick={() => {
            void api<{ code: string }>(`/api/v1/applications/${id}/access-code`)
              .then(result => { window.open(`/use/${id}?code=${result.code}`, '_blank') })
              .catch(error => setNotice(String(error)))
          }}
          type="button"
        >打开使用页</button>
        <button
          className={styles.shareGhost}
          onClick={() => {
            void api<{ code: string; use_path: string }>(`/api/v1/applications/${id}/access-code`, { method: 'POST', body: '{}' })
              .then(async result => {
                const url = `${window.location.origin}/use/${id}?code=${result.code}`
                let copied = false
                try {
                  await navigator.clipboard?.writeText(url)
                  copied = true
                } catch { /* 剪贴板被浏览器拦截时降级为手动复制 */ }
                setNotice(copied
                  ? `使用链接已复制（旧链接同时失效）：${url}`
                  : `浏览器不让自动复制，请手动复制（旧链接已失效）：${url}`)
              })
              .catch(error => setNotice(String(error)))
          }}
          type="button"
        >分享给使用者</button>
        <button
          className={styles.shareGhost}
          onClick={() => {
            void api<{ code: string }>(`/api/v1/applications/${id}/owner-code`)
              .then(async result => {
                const url = `${window.location.origin}/owner/${id}?code=${result.code}`
                let copied = false
                try {
                  await navigator.clipboard?.writeText(url)
                  copied = true
                } catch { /* 剪贴板被浏览器拦截时降级为手动复制 */ }
                setNotice(copied ? `业主链接已复制：${url}` : `请手动复制业主链接：${url}`)
              })
              .catch(error => setNotice(String(error)))
          }}
          type="button"
        >业主链接</button>
        <Link href={`/applications/${id}/pm`}>请监理</Link>
        <Link href={`/applications/${id}/views`}>界面方案</Link>
        <Link href={`/applications/${id}`}>画布编辑</Link>
        <Link href={`/runtime/${id}`}>试运行</Link>
        <div className={styles.exportWrap}>
          <button className={styles.exportBtn} onClick={toggleExport} type="button">
            导出{exportOpen ? ' ▴' : ' ▾'}
          </button>
          {exportOpen && <div className={styles.exportPanel}>
            <a
              className={styles.exportItem}
              download
              href={withFrontendToken(`/api/platform/api/v1/applications/${id}/delivery-package?profile=customer`)}
            >客户交付包 (zip)</a>
            <a
              className={styles.exportItem}
              download
              href={withFrontendToken(`/api/platform/api/v1/applications/${id}/delivery-package?profile=expert`)}
            >专家材料包 (zip)</a>
            <a
              className={styles.exportItem}
              download
              href={withFrontendToken(`/api/platform/api/v1/applications/${id}/export`)}
            >工作流定义 (JSON)</a>
            <div className={styles.exportSection}>
              <small>工作区文件</small>
              {workspaceFiles === null
                ? <p className={styles.exportEmpty}>正在读取…</p>
                : workspaceError
                ? <p className={styles.exportEmpty}>{workspaceError}</p>
                : workspaceFiles.length === 0
                ? <p className={styles.exportEmpty}>工作区暂无文件</p>
                : workspaceFiles.map(file => <a
                    className={styles.exportFile}
                    download
                    href={withFrontendToken(`/api/platform/api/v1/applications/${id}/workspace/files/${encodeURIComponent(file.path)}`)}
                    key={file.path}
                  >
                    <span>{file.path}</span>
                    <small>{(file.size / 1024).toFixed(1)} KB</small>
                  </a>)}
            </div>
          </div>}
        </div>
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
          {segments.length > 0
            ? segments.map((segment, segIndex) => <details
                className={styles.segment}
                key={segment.start}
                open={segIndex === segments.length - 1}
              >
                <summary>第 {segIndex + 1} 段 · {segment.turns} 轮{segment.startedAt ? ` · ${segment.startedAt}` : ''}</summary>
                <div className={styles.segmentBody}>
                  {segment.records.map((record, offset) => renderRecord(record, segment.start + offset))}
                </div>
              </details>)
            : transcript?.records.map((record, index) => renderRecord(record, index))}
          {pendingQuestion && <div className={styles.question}>
            <b>莉莉丝在等你回复</b>
            <p>{pendingQuestion}</p>
          </div>}
          {building && <div className={styles.working}><span/>莉莉丝正在搭建…</div>}
        </div>
        <div className={styles.composer}>
          {notice && <div className={styles.notice}>{notice}</div>}
          {!build && <BuildAdvancedConfig disabled={sending} onChange={setBuildOptions} value={buildOptions} />}
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
