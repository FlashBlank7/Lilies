'use client'

import '@xyflow/react/dist/style.css'
import Link from 'next/link'
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  addEdge,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  useEdgesState,
  useNodesState,
} from '@xyflow/react'
import { use, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type Block, type Draft, type WorkflowNode, idempotency } from '@/lib/platform'

type StudioNode = Node<{ title: string; blockType: string; description: string; status?: string }>
type Version = { version: number; content_hash: string; created_at: string; validation_report: Record<string, unknown> }
type Build = { id: string; status: string; error?: string; team_state: { tasks: Array<Record<string, unknown>>; teammates: Record<string, Record<string, unknown>>; repair_cycles: number } }
type Run = { id: string; status: string; outputs: Record<string, unknown>; error?: string; state: { waiting_node_id?: string | null } }

const accents: Record<string, string> = {
  start: '#8b5cf6', llm: '#3b82f6', claude_agent: '#f97316', tool: '#10b981',
  if_else: '#eab308', question_classifier: '#eab308', end: '#ec4899', answer: '#ec4899',
  human_input: '#ef4444', iteration: '#14b8a6', loop: '#14b8a6', http_request: '#06b6d4',
  schedule_trigger: '#a855f7',
}

function BrickNode({ data, selected }: NodeProps<StudioNode>) {
  const accent = accents[data.blockType] || '#64748b'
  return <div className={`brick-node ${selected ? 'selected' : ''}`} style={{ '--accent': accent } as React.CSSProperties}>
    <Handle type="target" position={Position.Left} />
    <div className="brick-type">{data.blockType.replaceAll('_', ' ')}</div>
    <strong>{data.title}</strong>
    <small>{data.description || '已配置积木'}</small>
    {data.status && <span className={`node-status ${data.status}`}>{data.status}</span>}
    <Handle type="source" position={Position.Right} />
  </div>
}

const nodeTypes = { brick: BrickNode }

function visiblePositions(workflowNodes: WorkflowNode[], workflowEdges: Draft['snapshot']['workflow']['edges']) {
  const depth = new Map(workflowNodes.map(node => [node.id, 0]))
  const incoming = new Map(workflowNodes.map(node => [node.id, 0]))
  const outgoing = new Map(workflowNodes.map(node => [node.id, [] as string[]]))

  workflowEdges.forEach(edge => {
    incoming.set(edge.target, (incoming.get(edge.target) || 0) + 1)
    outgoing.get(edge.source)?.push(edge.target)
  })

  const queue = workflowNodes.filter(node => incoming.get(node.id) === 0).map(node => node.id)
  for (let index = 0; index < queue.length; index += 1) {
    const source = queue[index]
    for (const target of outgoing.get(source) || []) {
      depth.set(target, Math.max(depth.get(target) || 0, (depth.get(source) || 0) + 1))
      incoming.set(target, (incoming.get(target) || 1) - 1)
      if (incoming.get(target) === 0) queue.push(target)
    }
  }

  const rows = new Map<number, number>()
  return new Map(workflowNodes.map(node => {
    if (node.position.x !== 0 || node.position.y !== 0) return [node.id, node.position]
    const column = depth.get(node.id) || 0
    const row = rows.get(column) || 0
    rows.set(column, row + 1)
    return [node.id, { x: 90 + column * 280, y: 110 + row * 130 }]
  }))
}

function defaultConfig(type: string): Record<string, unknown> {
  const configs: Record<string, Record<string, unknown>> = {
    start: { inputs: [] },
    schedule_trigger: { timezone: 'Asia/Tokyo', hour: 8, minute: 0, inputs: {} },
    llm: { system: 'You are a helpful assistant.', prompt: { $ref: { node_id: '$inputs', path: ['query'] } } },
    claude_agent: { agent_id: '', task: { $ref: { node_id: '$inputs', path: ['query'] } } },
    tool: { tool_name: 'Read', input: {} },
    if_else: { cases: [{ id: 'true', conditions: [{ value: true, operator: 'equals', expected: true }] }], default_branch: 'else' },
    question_classifier: { input: { $ref: { node_id: '$inputs', path: ['query'] } }, classes: ['class_a', 'class_b'] },
    parameter_extractor: { input: { $ref: { node_id: '$inputs', path: ['query'] } }, fields: [{ name: 'value', type: 'string' }] },
    template_transform: { template: '{{ value }}', variables: { value: '' } },
    variable_assigner: { assignments: {} }, variable_aggregator: { variables: [null], mode: 'first_non_null' },
    http_request: { method: 'GET', url: 'https://example.com', headers: {}, query: {} },
    iteration: { items: [], workflow: { nodes: [], edges: [], viewport: { x: 0, y: 0, zoom: 0.8 } }, output_node_id: '', output_path: [] },
    loop: { workflow: { nodes: [], edges: [], viewport: { x: 0, y: 0, zoom: 0.8 } }, break_condition: { value: false, operator: 'equals', expected: true }, break_value: false, output_node_id: '' },
    human_input: { title: '需要你的输入', fields: [{ name: 'value', label: 'Value', type: 'string' }] },
    end: { outputs: {} }, answer: { answer: '' },
  }
  return configs[type] || {}
}

export default function Studio({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [blocks, setBlocks] = useState<Block[]>([])
  const [versions, setVersions] = useState<Version[]>([])
  const [nodes, setNodes, onNodesChange] = useNodesState<StudioNode>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [selected, setSelected] = useState<WorkflowNode | null>(null)
  const [configText, setConfigText] = useState('{}')
  const [build, setBuild] = useState<Build | null>(null)
  const [events, setEvents] = useState<Array<{ type: string; data: Record<string, unknown> }>>([])
  const [tab, setTab] = useState<'build' | 'edit' | 'test' | 'run'>('build')
  const [requirement, setRequirement] = useState('')
  const [runInputs, setRunInputs] = useState('{"query":"你好"}')
  const [run, setRun] = useState<Run | null>(null)
  const [humanValues, setHumanValues] = useState('{}')
  const [notice, setNotice] = useState('')
  const eventSource = useRef<EventSource | null>(null)

  const syncCanvas = useCallback((next: Draft) => {
    setDraft(next)
    setRequirement(next.snapshot.requirement)
    const positions = visiblePositions(next.snapshot.workflow.nodes, next.snapshot.workflow.edges)
    setNodes(next.snapshot.workflow.nodes.map(item => ({
      id: item.id, type: 'brick', position: positions.get(item.id) || item.position,
      data: { title: item.title, blockType: item.type, description: item.description },
    })))
    setEdges(next.snapshot.workflow.edges.map(item => ({
      id: item.id, source: item.source, target: item.target, label: item.branch || undefined,
      animated: Boolean(item.branch), style: { stroke: item.branch ? '#eab308' : '#465166' },
    })))
  }, [setEdges, setNodes])

  const refresh = useCallback(async () => {
    const [next, nextBlocks, nextVersions] = await Promise.all([
      api<Draft>(`/api/v1/applications/${id}/draft`),
      api<Block[]>('/api/v1/blocks'),
      api<Version[]>(`/api/v1/applications/${id}/versions`),
    ])
    syncCanvas(next)
    setBlocks(nextBlocks)
    setVersions(nextVersions)
  }, [id, syncCanvas])

  useEffect(() => { refresh().catch(error => setNotice(String(error))) }, [refresh])
  useEffect(() => {
    const buildId = new URLSearchParams(window.location.search).get('build')
    if (buildId) watchBuild(buildId)
    else api<Build[]>(`/api/v1/applications/${id}/builds`).then(items => {
      if (!items[0]) return
      setBuild(items[0])
      if (['queued', 'building'].includes(items[0].status)) watchBuild(items[0].id)
    }).catch(() => undefined)
    return () => eventSource.current?.close()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function mutation(op: string, data: Record<string, unknown>) {
    if (!draft) return
    try {
      await api(`/api/v1/applications/${id}/draft`, {
        method: 'POST',
        body: JSON.stringify({ expected_revision: draft.revision, idempotency_key: idempotency(), op, data }),
      })
      await refresh()
      setNotice('草稿已保存，测试状态已失效')
    } catch (error) {
      setNotice(String(error))
      await refresh()
    }
  }

  const onConnect = useCallback(async (connection: Connection) => {
    if (!connection.source || !connection.target) return
    setEdges(current => addEdge(connection, current))
    await mutation('add_edge', { edge: {
      id: idempotency(), source: connection.source, target: connection.target,
      source_port: 'output', target_port: 'input',
    } })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  async function addBlock(block: Block) {
    const index = draft?.snapshot.workflow.nodes.length || 0
    await mutation('add_node', { node: {
      id: `${block.type}-${Date.now()}`, type: block.type, block_version: 1, title: block.title,
      description: block.description, config: defaultConfig(block.type), position: { x: 120 + index * 55, y: 120 + (index % 4) * 90 },
      retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
    } })
  }

  function chooseNode(node: StudioNode) {
    const value = draft?.snapshot.workflow.nodes.find(item => item.id === node.id) || null
    setSelected(value)
    setConfigText(JSON.stringify(value?.config || {}, null, 2))
    setTab('edit')
  }

  async function saveConfig() {
    if (!selected) return
    try {
      await mutation('update_node', { node_id: selected.id, changes: { config: JSON.parse(configText) }, merge_config: false })
    } catch (error) { setNotice(`JSON 配置无效：${error}`) }
  }

  async function startBuild() {
    const result = await api<{ build_id: string }>(`/api/v1/applications/${id}/builds`, {
      method: 'POST', body: JSON.stringify({ requirement, auto_publish: true }),
    })
    history.replaceState(null, '', `?build=${result.build_id}`)
    watchBuild(result.build_id)
  }

  function watchBuild(buildId: string) {
    eventSource.current?.close()
    setTab('build')
    const source = new EventSource(`/api/platform/api/v1/builds/${buildId}/events`)
    eventSource.current = source
    const names = ['build.started', 'build.operation', 'build.turn.completed', 'team.teammate.spawned', 'team.teammate.idle', 'tests.completed', 'build.published', 'build.completed', 'build.needs_attention']
    names.forEach(type => source.addEventListener(type, async raw => {
      const event = raw as MessageEvent
      const data = JSON.parse(event.data)
      setEvents(current => [...current.slice(-199), { type, data }])
      if (type === 'build.operation' || type === 'build.published') await refresh()
      if (type === 'build.completed' || type === 'build.needs_attention') {
        source.close()
        const current = await api<Build>(`/api/v1/builds/${buildId}`)
        setBuild(current)
        await refresh()
      }
    }))
    const poll = window.setInterval(() => api<Build>(`/api/v1/builds/${buildId}`).then(value => {
      setBuild(value)
      if (['published', 'ready', 'needs_attention', 'cancelled'].includes(value.status)) window.clearInterval(poll)
    }).catch(() => undefined), 1500)
  }

  async function runTests() {
    setNotice('正在运行真实验收…')
    const result = await api<{ passed: boolean }>(`/api/v1/applications/${id}/tests/run`, { method: 'POST' })
    setNotice(result.passed ? '所有强制测试通过' : '测试失败，请查看运行记录')
    await refresh()
  }

  async function publish() {
    const result = await api<{ version: number }>(`/api/v1/applications/${id}/versions`, { method: 'POST' })
    setNotice(`版本 v${result.version} 已发布`)
    await refresh()
  }

  async function startRun(useDraft = false) {
    const result = await api<{ run_id: string }>(`/api/v1/applications/${id}/runs`, {
      method: 'POST', body: JSON.stringify({ inputs: JSON.parse(runInputs), use_draft: useDraft, workspace_path: '.' }),
    })
    setTab('run')
    const poll = window.setInterval(async () => {
      const current = await api<Run>(`/api/v1/runs/${result.run_id}`)
      setRun(current)
      if (['succeeded', 'failed', 'paused', 'cancelled'].includes(current.status)) window.clearInterval(poll)
    }, 500)
  }

  async function resumeRun() {
    if (!run) return
    await api(`/api/v1/runs/${run.id}/resume`, { method: 'POST', body: JSON.stringify({ values: JSON.parse(humanValues) }) })
    setRun({ ...run, status: 'running' })
    const poll = window.setInterval(async () => {
      const current = await api<Run>(`/api/v1/runs/${run.id}`)
      setRun(current)
      if (['succeeded', 'failed', 'paused', 'cancelled'].includes(current.status)) window.clearInterval(poll)
    }, 500)
  }

  const grouped = useMemo(() => Object.groupBy(blocks, block => block.category), [blocks])
  const tested = draft?.tested_hash && draft.tested_hash === draft.content_hash

  return <main className="studio-shell">
    <header className="studio-header">
      <Link href="/" className="back">←</Link>
      <div className="studio-title"><strong>{draft?.snapshot.name || 'Loading…'}</strong><span>{draft?.snapshot.mode} · draft r{draft?.revision ?? 0}</span></div>
      <div className="header-center"><span className={tested ? 'verified' : 'unverified'}>{tested ? '✓ 已验证' : '● 未验证'}</span>{versions[0] && <span>active v{versions[0].version}</span>}</div>
      <div className="header-actions"><button className="ghost" onClick={() => startRun(true)}>调试草稿</button><button onClick={publish} disabled={!tested}>发布版本</button></div>
    </header>
    <div className="studio-grid">
      <aside className="left-panel">
        <div className="panel-tabs">{(['build', 'edit', 'test', 'run'] as const).map(item => <button className={tab === item ? 'active' : ''} onClick={() => setTab(item)} key={item}>{item}</button>)}</div>
        {tab === 'build' && <div className="panel-body">
          <div className="panel-kicker">BUILDER TEAM</div><h2>让团队继续搭建</h2>
          <textarea className="requirement-input" value={requirement} onChange={event => setRequirement(event.target.value)} />
          <button className="wide" onClick={startBuild}>启动智能体团队</button>
          {build && <div className="build-status"><b>{build.status}</b><span>{Object.keys(build.team_state.teammates).length} teammates · {build.team_state.tasks.length} tasks · {build.team_state.repair_cycles} repairs</span>{build.error && <p>{build.error}</p>}</div>}
          <div className="event-log">{events.map((event, index) => <div key={index}><span>{event.type}</span><pre>{JSON.stringify(event.data, null, 2)}</pre></div>)}</div>
        </div>}
        {tab === 'edit' && <div className="panel-body">
          <div className="panel-kicker">NODE INSPECTOR</div><h2>{selected?.title || '选择一个积木'}</h2>
          {selected ? <><label>Block config · JSON</label><textarea className="json-editor" value={configText} onChange={event => setConfigText(event.target.value)} /><button className="wide" onClick={saveConfig}>保存配置</button><button className="danger-link" onClick={() => mutation('remove_node', { node_id: selected.id })}>删除节点</button></> : <p className="muted">点击画布中的节点，调整 Prompt、变量引用、模型、Agent 或工具配置。</p>}
        </div>}
        {tab === 'test' && <div className="panel-body">
          <div className="panel-kicker">DELIVERY GATE</div><h2>{draft?.snapshot.tests.length || 0} 个验收用例</h2>
          <button className="wide" onClick={runTests}>运行全部真实测试</button>
          <div className="test-list">{draft?.snapshot.tests.map((test, index) => <pre key={index}>{JSON.stringify(test, null, 2)}</pre>)}</div>
          <h3>版本历史</h3>{versions.map(version => <div className="version-row" key={version.version}><span>v{version.version}</span><small>{version.content_hash.slice(0, 9)}</small><button onClick={async () => { await api(`/api/v1/applications/${id}/versions/${version.version}/restore`, { method: 'POST' }); await refresh() }}>加载编辑</button></div>)}
        </div>}
        {tab === 'run' && <div className="panel-body">
          <div className="panel-kicker">RUN APPLICATION</div><h2>执行已发布版本</h2>
          <textarea className="json-editor short" value={runInputs} onChange={event => setRunInputs(event.target.value)} /><button className="wide" onClick={() => startRun(false)}>运行</button>
          {run && <div className="run-result"><b>{run.status}</b><pre>{JSON.stringify(run.outputs || run.error, null, 2)}</pre>{run.status === 'paused' && <><label>Human Input</label><textarea value={humanValues} onChange={event => setHumanValues(event.target.value)} /><button onClick={resumeRun}>提交并恢复</button></>}</div>}
        </div>}
      </aside>
      <section className="canvas-wrap">
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onNodeClick={(_, node) => chooseNode(node)} onNodeDragStop={(_, node) => mutation('update_node', { node_id: node.id, changes: { position: node.position } })} fitView colorMode="dark">
          <Background color="#283142" gap={24} size={1}/><MiniMap pannable zoomable nodeColor={node => accents[node.data.blockType as string] || '#64748b'}/><Controls/>
        </ReactFlow>
        {notice && <button className="toast" onClick={() => setNotice('')}>{notice}</button>}
      </section>
      <aside className="block-panel"><div className="block-heading"><span>BRICKS</span><small>{blocks.length} available</small></div>{Object.entries(grouped).map(([category, items]) => <div className="block-group" key={category}><h4>{category}</h4>{items?.map(block => <button onClick={() => addBlock(block)} key={block.type}><i style={{ background: accents[block.type] || '#64748b' }}/><span><b>{block.title}</b><small>{block.description}</small></span><em>+</em></button>)}</div>)}</aside>
    </div>
  </main>
}
