'use client'

/**
 * 使用页：普通用户的全部世界。
 * 一个链接 + 一个访问码；填表（Excel/粘贴/格子，永不写 JSON）→ 点一下 → 看得懂的结果。
 * 页面不出现任何 Studio 痕迹；鉴权只有应用级访问码。
 */

import { use as usePromise, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import OutputView from '@/app/components/OutputView'
import styles from './use.module.css'

type UseInput = {
  name: string
  label?: string
  type?: string
  required?: boolean
  default?: unknown
  example?: unknown
  description?: string
}

type ViewStageNode = { id: string; title: string; type: string }

type ViewDefinition = {
  view_id: string
  name: string
  layout: 'form' | 'chat'
  stage_nodes: ViewStageNode[]
}

type ViewTab = { view_id: string; name: string; layout: 'form' | 'chat' }

type Definition = {
  application_name: string
  version: number | null
  snapshot: { requirement?: string; workflow: { nodes: Array<{ id: string; type: string; title?: string; config?: Record<string, unknown> }> } }
  acceptance?: { accepted: boolean; stamp?: string; passed_cases?: number; total_cases?: number }
  view?: ViewDefinition
  views?: ViewTab[]
}

type RunStage = { node_id: string; title: string; type: string; outputs: Record<string, unknown> }

type UseRun = {
  id: string
  status: string
  outputs: Record<string, unknown>
  stages?: RunStage[]
  state: { waiting_node_id?: string | null; error?: string | null }
}

type ChatMessage = {
  role: 'user' | 'assistant'
  text: string
  outputs?: Record<string, unknown>
  stages?: RunStage[]
  failed?: boolean
}

function primaryAnswerText(outputs: Record<string, unknown>): string {
  for (const value of Object.values(outputs)) {
    if (typeof value === 'string' && value.trim()) return value
  }
  return ''
}

// 对话记忆：工作流若声明了历史类输入字段，发送时自动带上最近几轮内容。
const MEMORY_FIELD_NAMES = new Set([
  'history', 'chat_history', 'conversation', 'conversation_history', 'context',
  '对话历史', '历史', '上下文',
])

type TableValue = { columns: string[]; rows: Array<Record<string, unknown>> }
type HistoryEntry = { run_id: string; at: string; inputs: Record<string, unknown> }

const numberLike = (value: string) => value.trim() !== '' && Number.isFinite(Number(value))

function coerceCell(value: string): unknown {
  return numberLike(value) ? Number(value) : value
}

export default function UsePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = usePromise(params)
  const [code, setCode] = useState('')
  const [codeInput, setCodeInput] = useState('')
  const [definition, setDefinition] = useState<Definition | null>(null)
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [tables, setTables] = useState<Record<string, TableValue>>({})
  const [pasteOpen, setPasteOpen] = useState<Record<string, boolean>>({})
  const [run, setRun] = useState<UseRun | null>(null)
  const [artifacts, setArtifacts] = useState<Array<{ name: string; size: number }>>([])
  const [history, setHistory] = useState<HistoryEntry[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [humanValues, setHumanValues] = useState<Record<string, unknown>>({})
  const [viewId, setViewId] = useState('')
  const [chat, setChat] = useState<ChatMessage[]>([])
  const chatSeenRef = useRef<Set<string>>(new Set())
  const pollRef = useRef<number | null>(null)

  const storageKey = `lilies-use-${id}`

  const call = useCallback(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    const joiner = path.includes('?') ? '&' : '?'
    const viewParam = viewId ? `&view=${encodeURIComponent(viewId)}` : ''
    const response = await fetch(`/api/platform/api/v1/use/${id}${path}${joiner}code=${encodeURIComponent(code)}${viewParam}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
    if (!response.ok) {
      const body = await response.json().catch(() => ({ detail: response.statusText }))
      throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail))
    }
    return response.json() as Promise<T>
  }, [id, code, viewId])

  // 访问码：URL ?code= 优先，其次本地记忆；?view= 决定界面方案
  useEffect(() => {
    const query = new URLSearchParams(window.location.search)
    const fromUrl = query.get('code') || ''
    const remembered = window.localStorage.getItem(`${storageKey}-code`) || ''
    const chosen = fromUrl || remembered
    if (chosen) setCode(chosen)
    setViewId(query.get('view') || '')
    const savedHistory = window.localStorage.getItem(`${storageKey}-history`)
    if (savedHistory) setHistory(JSON.parse(savedHistory) as HistoryEntry[])
  }, [storageKey])

  useEffect(() => {
    if (!code) return
    window.localStorage.setItem(`${storageKey}-code`, code)
    call<Definition>('/definition')
      .then(next => { setDefinition(next); setError('') })
      .catch(err => { setDefinition(null); setError(String(err.message || err)) })
  }, [code, call, storageKey])

  const inputs = useMemo<UseInput[]>(() => {
    const start = definition?.snapshot.workflow.nodes.find(node => node.type === 'start' || node.type === 'schedule_trigger')
    const settings = (start?.config as { settings?: { inputs?: UseInput[] } } | undefined)?.settings
    return settings?.inputs || []
  }, [definition])

  const terminalIds = useMemo(() => new Set(
    (definition?.snapshot.workflow.nodes || [])
      .filter(node => node.type === 'end' || node.type === 'answer')
      .map(node => node.id),
  ), [definition])

  function isTableInput(input: UseInput) {
    return (input.type || 'string') === 'array' || (input.type || '') === 'file_list'
  }

  function currentInputs(): Record<string, unknown> {
    const merged: Record<string, unknown> = {}
    for (const input of inputs) {
      if (isTableInput(input)) {
        const table = tables[input.name]
        if (table && table.rows.length) merged[input.name] = table.rows
      } else if ((input.type || 'string') === 'object') {
        const raw = values[input.name]
        if (typeof raw === 'string' && raw.trim()) {
          try { merged[input.name] = JSON.parse(raw) } catch { merged[input.name] = raw }
        } else if (raw !== undefined && raw !== '') merged[input.name] = raw
      } else {
        const raw = values[input.name]
        if (raw !== undefined && raw !== '') merged[input.name] = raw
      }
    }
    return merged
  }

  async function importTable(input: UseInput, payload: { filename: string; text?: string; content_base64?: string }) {
    setError('')
    try {
      const parsed = await call<TableValue>('/parse-table', { method: 'POST', body: JSON.stringify(payload) })
      setTables(current => ({ ...current, [input.name]: parsed }))
      setPasteOpen(current => ({ ...current, [input.name]: false }))
    } catch (err) {
      setError(String((err as Error).message || err))
    }
  }

  function onUpload(input: UseInput, file: File) {
    const reader = new FileReader()
    reader.onload = () => {
      const base64 = String(reader.result).split(',')[1] || ''
      void importTable(input, { filename: file.name, content_base64: base64 })
    }
    reader.readAsDataURL(file)
  }

  function editCell(name: string, rowIndex: number, column: string, raw: string) {
    setTables(current => {
      const table = current[name]
      if (!table) return current
      const rows = table.rows.map((row, index) => index === rowIndex ? { ...row, [column]: coerceCell(raw) } : row)
      return { ...current, [name]: { ...table, rows } }
    })
  }

  function stopPolling() {
    if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null }
  }

  const refreshRun = useCallback(async (runId: string) => {
    const next = await call<UseRun>(`/runs/${runId}`)
    setRun(next)
    if (!['queued', 'running'].includes(next.status)) {
      stopPolling()
      if (next.status === 'succeeded') {
        call<Array<{ name: string; size: number }>>(`/runs/${runId}/artifacts`).then(setArtifacts).catch(() => setArtifacts([]))
      }
    }
  }, [call])

  async function start() {
    setBusy(true)
    setError('')
    setArtifacts([])
    try {
      const payload = currentInputs()
      for (const input of inputs) {
        if (input.required && payload[input.name] === undefined) {
          throw new Error(`请先填写「${input.label || input.name}」`)
        }
      }
      if (definition?.view?.layout === 'chat' && memoryField) {
        const filled = payload[memoryField.name]
        if (filled === undefined || filled === '') {
          const recent = chat.slice(-6)
            .map(message => `${message.role === 'user' ? '用户' : '助手'}：${message.text}`)
            .join('\n')
          if (recent) payload[memoryField.name] = recent
        }
      }
      const started = await call<{ run_id: string }>('/runs', { method: 'POST', body: JSON.stringify({ inputs: payload }) })
      if (definition?.view?.layout === 'chat') {
        const spoken = Object.entries(payload)
          .filter(([key, value]) => key !== memoryField?.name
            && (typeof value === 'string' || typeof value === 'number'))
          .map(([key, value]) => inputs.length > 1 ? `${key}: ${String(value)}` : String(value))
          .join('\n') || '（开始处理）'
        setChat(current => [...current, { role: 'user', text: spoken }])
        // 发送后清空文本输入，像聊天一样
        setValues(current => {
          const next = { ...current }
          for (const input of inputs) {
            if (!isTableInput(input) && (input.type || 'string') === 'string' && input.name !== memoryField?.name) {
              delete next[input.name]
            }
          }
          return next
        })
      }
      const entry: HistoryEntry = { run_id: started.run_id, at: new Date().toLocaleString(), inputs: payload }
      const nextHistory = [entry, ...history].slice(0, 20)
      setHistory(nextHistory)
      window.localStorage.setItem(`${storageKey}-history`, JSON.stringify(nextHistory))
      setRun({ id: started.run_id, status: 'queued', outputs: {}, state: {} })
      stopPolling()
      pollRef.current = window.setInterval(() => { void refreshRun(started.run_id).catch(() => undefined) }, 2500)
    } catch (err) {
      setError(String((err as Error).message || err))
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => () => stopPolling(), [])

  const waitingNode = useMemo(() => {
    if (!run || run.status !== 'paused' || !run.state.waiting_node_id) return null
    return definition?.snapshot.workflow.nodes.find(node => run.state.waiting_node_id?.endsWith(node.id)) || null
  }, [run, definition])

  const waitingFields = useMemo<UseInput[]>(() => {
    const config = waitingNode?.config as { fields?: UseInput[]; title?: string } | undefined
    return config?.fields || [{ name: 'confirmed', label: '确认', type: 'boolean' }]
  }, [waitingNode])

  const mergedOutputs = useMemo(() => {
    const outputs = run?.outputs || {}
    const keys = Object.keys(outputs)
    // 后端投影通常已给出扁平的"字段名→值"；只有当所有键都是终端节点 id 且值为对象时才按节点合并。
    const looksPerNode = keys.length > 0 && keys.every(key =>
      terminalIds.has(key) && outputs[key] && typeof outputs[key] === 'object' && !Array.isArray(outputs[key]))
    if (!looksPerNode) return outputs
    const merged: Record<string, unknown> = {}
    for (const value of Object.values(outputs)) Object.assign(merged, value as Record<string, unknown>)
    return merged
  }, [run, terminalIds])

  const view = definition?.view
  const isChat = view?.layout === 'chat'
  const viewTabs = definition?.views || []

  // WaaS：界面在服务里切换。换标签 → 重取定义与当前运行（服务端按新界面重新投影）。
  function switchView(nextViewId: string) {
    if (nextViewId === viewId) return
    setViewId(nextViewId)
    const query = new URLSearchParams(window.location.search)
    if (nextViewId) query.set('view', nextViewId)
    else query.delete('view')
    window.history.replaceState(null, '', `${window.location.pathname}?${query.toString()}`)
  }

  useEffect(() => {
    if (run && !['queued', 'running'].includes(run.status)) {
      void refreshRun(run.id).catch(() => undefined)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewId])

  const memoryField = useMemo(
    () => isChat ? inputs.find(input => MEMORY_FIELD_NAMES.has(input.name.toLowerCase())) : undefined,
    [inputs, isChat],
  )

  // 对话布局：运行到达终态时，把结果作为"她的回答"追加进消息流（每次运行只追加一次）。
  useEffect(() => {
    if (!isChat || !run) return
    if (!['succeeded', 'failed'].includes(run.status)) return
    if (chatSeenRef.current.has(run.id)) return
    chatSeenRef.current.add(run.id)
    if (run.status === 'succeeded') {
      setChat(current => [...current, {
        role: 'assistant',
        text: primaryAnswerText(mergedOutputs) || '处理完成，详情见下方。',
        outputs: mergedOutputs,
        stages: run.stages || [],
      }])
    } else {
      setChat(current => [...current, {
        role: 'assistant',
        text: `没有跑成：${run.state.error || '处理失败'}`,
        failed: true,
      }])
    }
  }, [isChat, run, mergedOutputs])

  function reuse(entry: HistoryEntry) {
    const nextValues: Record<string, unknown> = {}
    const nextTables: Record<string, TableValue> = {}
    for (const input of inputs) {
      const value = entry.inputs[input.name]
      if (value === undefined) continue
      if (isTableInput(input) && Array.isArray(value)) {
        const rows = value as Array<Record<string, unknown>>
        const columns = rows.length ? Object.keys(rows[0]) : []
        nextTables[input.name] = { columns, rows }
      } else if ((input.type || '') === 'object') {
        nextValues[input.name] = JSON.stringify(value, null, 2)
      } else {
        nextValues[input.name] = value
      }
    }
    setValues(nextValues)
    setTables(nextTables)
    void refreshRun(entry.run_id).catch(() => undefined)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  if (!code || (!definition && error)) {
    return <main className={styles.shell}>
      <div className={`${styles.card} ${styles.codeCard}`}>
        <h2>输入访问码</h2>
        <p className={styles.hint}>访问码在给你链接的人那里；通常链接里自带，不需要手动输入。</p>
        {error && <p className={styles.error}>{error}</p>}
        <input
          onChange={event => setCodeInput(event.target.value)}
          onKeyDown={event => { if (event.key === 'Enter' && codeInput.trim()) { setError(''); setCode(codeInput.trim()) } }}
          placeholder="访问码"
          type="text"
          value={codeInput}
        />
        <button onClick={() => { if (codeInput.trim()) { setError(''); setCode(codeInput.trim()) } }} type="button">进入</button>
      </div>
    </main>
  }

  return <main className={styles.shell}>
    <div className={styles.wrap}>
      <header className={styles.head}>
        <h1>{definition?.application_name || '…'}</h1>
        {definition?.snapshot.requirement && <p>{definition.snapshot.requirement.split('系统对接说明')[0].slice(0, 220)}</p>}
        <div className={styles.badges}>
          {definition?.version != null && <span className={`${styles.badge} ${styles.badgeMuted}`}>正式版 v{definition.version}</span>}
          {definition?.acceptance && (definition.acceptance.accepted
            ? <span className={styles.badge}>✓ 已通过独立验收（{definition.acceptance.passed_cases}/{definition.acceptance.total_cases} 用例 · {definition.acceptance.stamp}）</span>
            : <span className={`${styles.badge} ${styles.badgeMuted}`}>验收整改中</span>)}
        </div>
        {viewTabs.length > 1 && <nav className={styles.viewTabs} aria-label="界面切换">
          {viewTabs.map(tab => <button
            className={(viewId || '') === tab.view_id || (!viewId && tab.view_id === 'default')
              ? `${styles.viewTab} ${styles.viewTabActive}`
              : styles.viewTab}
            key={tab.view_id || 'auto'}
            onClick={() => switchView(tab.view_id)}
            type="button"
          >{tab.name}</button>)}
        </nav>}
      </header>

      {isChat && <section className={styles.card}>
        <h2>对话</h2>
        {chat.length === 0 && <p className={styles.hint}>在下方输入内容，点「发送」开始第一轮对话。</p>}
        <div className={styles.chatStream}>
          {chat.map((message, index) => <div
            className={message.role === 'user' ? styles.chatUser : styles.chatAssistant}
            key={index}
          >
            <div className={message.failed ? `${styles.chatBubble} ${styles.chatFailed}` : styles.chatBubble}>
              <p>{message.text}</p>
              {message.role === 'assistant' && !message.failed && message.outputs
                && Object.keys(message.outputs).length > 0
                && primaryAnswerText(message.outputs) !== message.text && <details>
                <summary>查看完整结果</summary>
                <OutputView outputs={message.outputs} />
              </details>}
              {message.role === 'assistant' && (message.stages?.length || 0) > 0 && <details>
                <summary>查看过程（{message.stages!.length} 个环节）</summary>
                {message.stages!.map(stage => <div className={styles.stage} key={stage.node_id}>
                  <b>{stage.title}</b>
                  <OutputView outputs={stage.outputs} />
                </div>)}
              </details>}
            </div>
          </div>)}
          {(run?.status === 'queued' || run?.status === 'running') && <div className={styles.chatAssistant}>
            <div className={styles.chatBubble}><span className={styles.typing}>正在处理…</span></div>
          </div>}
        </div>
      </section>}

      <section className={styles.card}>
        <h2>{isChat ? '输入' : '填写本次要处理的内容'}</h2>
        {isChat && memoryField && <p className={styles.hint}>这个对话会自动带上最近几轮的内容，不用重复背景。</p>}
        {inputs.length === 0 && <p className={styles.hint}>这个流程不需要输入，直接开始即可。</p>}
        {inputs.filter(input => !(isChat && input.name === memoryField?.name)).map(input => {
          const label = input.label || input.name
          const exampleText = input.example !== undefined && input.example !== null
            ? (typeof input.example === 'string' ? input.example : JSON.stringify(input.example))
            : ''
          if (isTableInput(input)) {
            const table = tables[input.name]
            return <div className={styles.field} key={input.name}>
              <label>{label}{input.required ? '' : <small>（选填）</small>}<small>表格数据</small></label>
              <div className={styles.tableTools}>
                <label>
                  上传 Excel / CSV
                  <input accept=".xlsx,.csv,.txt" onChange={event => {
                    const file = event.target.files?.[0]
                    if (file) onUpload(input, file)
                    event.target.value = ''
                  }} type="file" />
                </label>
                <button onClick={() => setPasteOpen(current => ({ ...current, [input.name]: !current[input.name] }))} type="button">
                  从 Excel 复制粘贴
                </button>
                {table && <span className={styles.hint}>已导入 {table.rows.length} 行</span>}
              </div>
              {pasteOpen[input.name] && <textarea
                className={styles.pasteBox}
                onPaste={event => {
                  const text = event.clipboardData.getData('text')
                  if (text.trim()) { event.preventDefault(); void importTable(input, { filename: 'paste.txt', text }) }
                }}
                placeholder={'直接在 Excel 里选中区域（含表头）→ 复制 → 在这里粘贴'}
                readOnly={false}
                value=""
                onChange={() => undefined}
              />}
              {table && <>
                <div className={styles.grid}>
                  <table>
                    <thead><tr>{table.columns.map(column => <th key={column}>{column}</th>)}</tr></thead>
                    <tbody>
                      {table.rows.slice(0, 200).map((row, rowIndex) => <tr key={rowIndex}>
                        {table.columns.map(column => <td key={column}>
                          <input
                            onChange={event => editCell(input.name, rowIndex, column, event.target.value)}
                            value={row[column] === undefined || row[column] === null ? '' : String(row[column])}
                          />
                        </td>)}
                      </tr>)}
                    </tbody>
                  </table>
                </div>
                <div className={styles.rowOps}>
                  <button onClick={() => setTables(current => {
                    const existing = current[input.name]
                    if (!existing) return current
                    const empty = Object.fromEntries(existing.columns.map(column => [column, '']))
                    return { ...current, [input.name]: { ...existing, rows: [...existing.rows, empty] } }
                  })} type="button">+ 加一行</button>
                  <button onClick={() => setTables(current => {
                    const existing = current[input.name]
                    if (!existing) return current
                    return { ...current, [input.name]: { ...existing, rows: existing.rows.slice(0, -1) } }
                  })} type="button">− 删末行</button>
                  <button onClick={() => setTables(current => {
                    const next = { ...current }
                    delete next[input.name]
                    return next
                  })} type="button">清空重来</button>
                </div>
              </>}
              {!table && exampleText && <span className={styles.hint}>示例：{exampleText.slice(0, 160)}</span>}
            </div>
          }
          const type = input.type || 'string'
          return <div className={styles.field} key={input.name}>
            <label>{label}{input.required ? '' : <small>（选填）</small>}</label>
            {type === 'boolean'
              ? <label><input
                  checked={Boolean(values[input.name])}
                  onChange={event => setValues(current => ({ ...current, [input.name]: event.target.checked }))}
                  type="checkbox"
                /> 是</label>
              : type === 'number'
                ? <input
                    onChange={event => setValues(current => ({ ...current, [input.name]: event.target.value === '' ? '' : Number(event.target.value) }))}
                    placeholder={exampleText}
                    type="number"
                    value={values[input.name] === undefined ? '' : String(values[input.name])}
                  />
                : (type === 'object'
                  ? <textarea
                      onChange={event => setValues(current => ({ ...current, [input.name]: event.target.value }))}
                      placeholder={exampleText || '按对接说明填写'}
                      value={String(values[input.name] ?? '')}
                    />
                  : <textarea
                      onChange={event => setValues(current => ({ ...current, [input.name]: event.target.value }))}
                      placeholder={exampleText}
                      rows={2}
                      value={String(values[input.name] ?? '')}
                    />)}
            {input.description && <span className={styles.hint}>{input.description}</span>}
          </div>
        })}
        <div className={styles.runRow}>
          <button disabled={busy || run?.status === 'running' || run?.status === 'queued'} onClick={() => void start()} type="button">
            {busy ? '正在开始…' : isChat ? '发送' : '开始处理'}
          </button>
          {definition?.version == null && <span className={styles.runNote}>（这个流程还没发布正式版，暂时无法使用）</span>}
        </div>
        {error && <p className={styles.error} style={{ marginTop: 12 }}>{error}</p>}
      </section>

      {run && !(isChat && (
        run.status === 'failed'
        || (run.status === 'succeeded' && artifacts.length === 0)
        || run.status === 'queued' || run.status === 'running'
      )) && <section className={styles.card}>
        <h2>结果</h2>
        {(run.status === 'queued' || run.status === 'running') && <div className={styles.status}><i />正在处理，请稍候…</div>}
        {run.status === 'paused' && <div>
          <p className={styles.hint}>{(waitingNode?.config as { title?: string } | undefined)?.title || '流程需要你确认一下才能继续：'}</p>
          {waitingFields.map(field => <div className={styles.field} key={field.name}>
            <label>{field.label || field.name}</label>
            {(field.type || 'string') === 'boolean'
              ? <label><input
                  checked={Boolean(humanValues[field.name])}
                  onChange={event => setHumanValues(current => ({ ...current, [field.name]: event.target.checked }))}
                  type="checkbox"
                /> 确认</label>
              : <input
                  onChange={event => setHumanValues(current => ({ ...current, [field.name]: (field.type === 'number' ? Number(event.target.value) : event.target.value) }))}
                  type={field.type === 'number' ? 'number' : 'text'}
                  value={String(humanValues[field.name] ?? '')}
                />}
          </div>)}
          <div className={styles.runRow}>
            <button onClick={() => {
              void call<UseRun>(`/runs/${run.id}/resume`, { method: 'POST', body: JSON.stringify({ values: humanValues }) })
                .then(next => { setRun(next); if (['queued', 'running'].includes(next.status)) { stopPolling(); pollRef.current = window.setInterval(() => { void refreshRun(run.id).catch(() => undefined) }, 2500) } })
                .catch(err => setError(String((err as Error).message || err)))
            }} type="button">提交并继续</button>
          </div>
        </div>}
        {run.status === 'failed' && <p className={styles.error}>没有跑成：{run.state.error || '处理失败'}。可以调整内容后再试一次；反复失败请联系给你链接的人。</p>}
        {run.status === 'succeeded' && !isChat && <>
          <OutputView outputs={mergedOutputs} />
          {(run.stages?.length || 0) > 0 && <div className={styles.stages}>
            <b style={{ fontSize: 13 }}>过程（供审查）</b>
            {run.stages!.map(stage => <details className={styles.stage} key={stage.node_id}>
              <summary>{stage.title}</summary>
              <OutputView outputs={stage.outputs} />
            </details>)}
          </div>}
          {artifacts.length > 0 && <div className={styles.artifacts} style={{ marginTop: 14 }}>
            <b style={{ fontSize: 13 }}>生成的文件</b>
            {artifacts.map(item => <a
              href={`/api/platform/api/v1/use/${id}/runs/${run.id}/artifacts/${encodeURIComponent(item.name)}?code=${encodeURIComponent(code)}`}
              key={item.name}
            >⬇ {item.name}（{Math.max(1, Math.round(item.size / 1024))} KB）</a>)}
          </div>}
        </>}
        {run.status === 'succeeded' && isChat && artifacts.length > 0 && <div className={styles.artifacts}>
          <b style={{ fontSize: 13 }}>生成的文件</b>
          {artifacts.map(item => <a
            href={`/api/platform/api/v1/use/${id}/runs/${run.id}/artifacts/${encodeURIComponent(item.name)}?code=${encodeURIComponent(code)}`}
            key={item.name}
          >⬇ {item.name}（{Math.max(1, Math.round(item.size / 1024))} KB）</a>)}
        </div>}
      </section>}

      {history.length > 0 && <section className={styles.card}>
        <h2>我的历史</h2>
        <div className={styles.history}>
          {history.map(entry => <div className={styles.historyItem} key={entry.run_id}>
            <b>{entry.at}</b>
            <span className={styles.hint}>{entry.run_id.slice(0, 8)}</span>
            <button onClick={() => reuse(entry)} type="button">查看 / 用这次的输入再跑</button>
          </div>)}
        </div>
      </section>}

      <footer className={styles.foot}>
        这个页面由工作流平台自动生成；数据只用于本次处理。需要调整处理方式时，请联系给你链接的人。
      </footer>
    </div>
  </main>
}
