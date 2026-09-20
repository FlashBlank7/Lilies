'use client'

import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import styles from './workspace-tools.module.css'
import KnowledgeResults, {type KnowledgeSearchResult} from './KnowledgeResults'

type Knowledge = { knowledge_ref: string; name: string; revision: number; chunk_size: number; chunk_overlap: number; document_prefix: string; query_prefix: string; status: string; active_version: string; embedding_model: string; chunk_count: number; documents: { id: string; title: string; characters: number; source_path: string }[] }
type Connection = { model?: string; base_url?: string; has_api_key?: boolean; runtime_enabled?: boolean }

export default function ProjectKnowledge({ projectId, canManage, onWorkflow, onFile }: {
  projectId: string; canManage: boolean; onWorkflow: (id: string) => void; onFile: (path: string) => void
}) {
  const base = `/api/v1/projects/${projectId}`
  const [items, setItems] = useState<Knowledge[]>([])
  const [selected, setSelected] = useState('')
  const [reference, setReference] = useState('knowledge')
  const [name, setName] = useState('项目知识库')
  const [size, setSize] = useState(600)
  const [overlap, setOverlap] = useState(80)
  const [documentPrefix, setDocumentPrefix] = useState('')
  const [queryPrefix, setQueryPrefix] = useState('')
  const [connection, setConnection] = useState<Connection>({})
  const [key, setKey] = useState('')
  const [source, setSource] = useState('')
  const [files, setFiles] = useState<{path: string}[]>([])
  const [text, setText] = useState('')
  const [title, setTitle] = useState('')
  const [query, setQuery] = useState('')
  const [topK, setTopK] = useState(5)
  const [minimum, setMinimum] = useState(0.3)
  const [result, setResult] = useState<KnowledgeSearchResult>()
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const current = items.find(item => item.knowledge_ref === selected)
  const refresh = useCallback(async () => {
    setItems(await api<Knowledge[]>(base + '/knowledge'))
  }, [base])
  useEffect(() => {
    let active = true
    Promise.all([api<Knowledge[]>(base + '/knowledge'), api<Connection>(base + '/embedding-model')])
      .then(([list, model]) => { if (active) { setItems(list); setConnection(model) } })
      .catch(cause => { if (active) setError(String(cause)) })
    return () => { active = false }
  }, [base])
  function choose(item?: Knowledge) {
    setSelected(item?.knowledge_ref || ''); setReference(item?.knowledge_ref || 'knowledge')
    setName(item?.name || '项目知识库'); setSize(item?.chunk_size || 600); setOverlap(item?.chunk_overlap ?? 80)
    setDocumentPrefix(item?.document_prefix || ''); setQueryPrefix(item?.query_prefix || '')
    setResult(undefined); setError(''); setMessage('')
  }
  async function act(label: string, work: () => Promise<void>) {
    setBusy(label); setError(''); setMessage('')
    try { await work(); await refresh(); setMessage(label + '完成') }
    catch (cause) { setError(String(cause)) }
    finally { setBusy('') }
  }
  async function save() {
    const saved = await api<Knowledge>(base + '/knowledge/' + reference, { method: 'PUT', body: JSON.stringify({
      name, expected_revision: current?.revision || 0, chunk_size: size, chunk_overlap: overlap, document_prefix: documentPrefix, query_prefix: queryPrefix,
    }) })
    setSelected(saved.knowledge_ref)
  }
  async function addDocument(fromFile: boolean) {
    if (!current) return
    await api(base + `/knowledge/${selected}/documents`, { method: 'POST', body: JSON.stringify({
      expected_revision: current.revision, title, ...(fromFile ? { source_path: source } : { text }),
    }) })
    setText(''); setTitle(''); setSource(''); setResult(undefined)
  }
  async function createWorkflow(mode: 'search' | 'answer') {
    if (!current) return
    const workflow = await api(base + `/knowledge/${selected}/workflow-definition`, {method: 'POST', body: JSON.stringify({mode, top_k: topK, minimum_score: minimum})})
    const member = await api<{id: string}>(base + '/members', {method: 'POST', body: JSON.stringify({name: current.name + (mode === 'answer' ? ' · 引用问答' : ' · 检索')})})
    const draft = await api<{revision: number}>(`/api/v1/applications/${member.id}/draft`)
    await api(base + `/workflows/${member.id}/draft`, {method: 'PUT', body: JSON.stringify({expected_revision: draft.revision, workflow})})
    onWorkflow(member.id)
  }
  return <section className={styles.section} aria-label="项目知识库">
    <h2>项目知识库</h2><p>保存资料、配置切分和 Embedding，再建立语义索引。工作流可提前创建；索引未就绪时运行会提示缺项。</p>
    <div className={styles.row}><label>选择知识库<select aria-label="选择知识库" disabled={!!busy} value={selected} onChange={e => choose(items.find(k => k.knowledge_ref === e.target.value))}>
      <option value="">新建知识库</option>{items.map(k => <option key={k.knowledge_ref} value={k.knowledge_ref}>{k.name} · {k.status === 'ready' ? '可用' : '待建立索引'}</option>)}
    </select></label><button disabled={!!busy} onClick={() => void act('刷新知识库', refresh)}>刷新</button></div>
    <div className={styles.row}>
      <label>名称<input aria-label="知识库名称" disabled={!!busy} value={name} onChange={e => setName(e.target.value)} /></label>
      <label>引用名称<input aria-label="知识库引用名称" disabled={!!busy || !!selected} value={reference} onChange={e => setReference(e.target.value)} placeholder="例如 handbook" /></label>
      <label>片段长度<input aria-label="片段长度" type="number" min={100} max={2000} disabled={!!busy} value={size} onChange={e => setSize(Number(e.target.value))} /></label>
      <label>重叠长度<input aria-label="重叠长度" type="number" min={0} max={500} disabled={!!busy} value={overlap} onChange={e => setOverlap(Number(e.target.value))} /></label>
    </div>
    <details><summary>模型输入前缀（按模型说明填写）</summary><div className={styles.row}>
      <label>文档前缀<input aria-label="文档前缀" disabled={!!busy} value={documentPrefix} onChange={e => setDocumentPrefix(e.target.value)} /></label>
      <label>查询前缀<input aria-label="查询前缀" disabled={!!busy} value={queryPrefix} onChange={e => setQueryPrefix(e.target.value)} /></label>
    </div></details>
    <div className={styles.row}><button disabled={!!busy || !name || !reference || overlap >= size} onClick={() => void act('保存知识库', save)}>保存知识库配置</button>
      <button disabled={!!busy || !current} onClick={() => void act('创建工作流', () => createWorkflow('search'))}>创建检索工作流</button>
      <button disabled={!!busy || !current} onClick={() => void act('创建工作流', () => createWorkflow('answer'))}>创建引用问答工作流</button></div>
    <p>引用问答使用项目主模型回答，Embedding 负责检索；模型可以稍后配置。生成流程不会调用模型或启动任务。</p>
    <details><summary>Embedding 模型连接</summary>
      <p>使用独立的 OpenAI 兼容 embeddings 接口。更换模型或地址后需要重新建立索引；本地回环服务无需密钥。</p>
      {canManage ? <><div className={styles.row}>
        <label>API 地址<input aria-label="Embedding API 地址" disabled={!!busy} value={connection.base_url || ''} onChange={e => setConnection({...connection, base_url: e.target.value})} placeholder="http://127.0.0.1:11434/v1" /></label>
        <label>模型名称<input aria-label="Embedding 模型名称" disabled={!!busy} value={connection.model || ''} onChange={e => setConnection({...connection, model: e.target.value})} /></label>
        <label>API Key<input aria-label="Embedding API Key" type="password" autoComplete="new-password" disabled={!!busy} value={key} onChange={e => setKey(e.target.value)} placeholder={connection.has_api_key ? '留空保留已有密钥' : '本地回环服务可留空'} /></label>
      </div><label><input type="checkbox" disabled={!!busy} checked={!!connection.runtime_enabled} onChange={e => setConnection({...connection, runtime_enabled: e.target.checked})} />启用 Embedding</label>
      <button disabled={!!busy || !connection.model || !connection.base_url} onClick={() => void act('保存模型连接', async () => {
        const saved = await api<Connection>(base + '/embedding-model', {method: 'PUT', body: JSON.stringify({provider: 'api', protocol: 'openai', model: connection.model, base_url: connection.base_url, runtime_enabled: !!connection.runtime_enabled, ...(key ? {api_key: key} : {})})})
        setConnection(saved); setKey('')
      })}>保存 Embedding 连接</button></> : <p>连接由项目负责人配置。当前模型：{connection.model || '未配置'}</p>}
    </details>
    {current && <>
      <h3>知识库资料</h3><p>{current.documents.length} 份资料；{current.status === 'ready' ? `索引可用，${current.chunk_count} 个片段，模型 ${current.embedding_model}` : '资料、配置或模型尚未建立可用索引。已有运行保留原索引版本。'}</p>
      {current.documents.map(d => <div key={d.id} className={styles.row}><span>{d.title} · {d.characters} 字符</span><button disabled={!!busy} aria-label={`移除 ${d.title}`} onClick={() => void act('移除资料', async () => { await api(base + `/knowledge/${selected}/documents/${d.id}?expected_revision=${current.revision}`, {method: 'DELETE'}); setResult(undefined) })}>移除</button></div>)}
      <label>资料标题<input aria-label="知识资料标题" disabled={!!busy} value={title} onChange={e => setTitle(e.target.value)} /></label>
      <details><summary>选择已有项目文件</summary><div className={styles.row}>
        <button disabled={!!busy} onClick={() => void act('读取项目文件', async () => setFiles(await api(`/api/v1/applications/${projectId}/workspace/files`)))}>刷新文件列表</button>
        <label>文件<select aria-label="知识资料文件" value={source} disabled={!!busy} onChange={e => setSource(e.target.value)}><option value="">选择资料</option>{files.filter(f => /\.(pdf|docx|txt|md|csv|tsv|json|py)$/i.test(f.path)).map(f => <option key={f.path} value={f.path}>{f.path}</option>)}</select></label>
        <button disabled={!!busy || !source} onClick={() => void act('添加资料', () => addDocument(true))}>添加所选文件</button>
      </div></details>
      <details><summary>粘贴知识正文</summary><textarea className={styles.editor} aria-label="知识正文" disabled={!!busy} value={text} onChange={e => setText(e.target.value)} /><button disabled={!!busy || !text.trim()} onClick={() => void act('添加资料', () => addDocument(false))}>添加正文</button></details>
      <p>支持可提取文字的 PDF、DOCX 和 UTF-8 文本。单份文件最多 20 MB；扫描件请先提供 OCR 文本。</p>
      <button disabled={!!busy || !current.documents.length} onClick={() => void act('建立索引', async () => { await api(base + `/knowledge/${selected}/build`, {method: 'POST', body: JSON.stringify({expected_revision: current.revision})}); setResult(undefined) })}>建立语义索引</button>
      <h3>测试检索</h3><div className={styles.row}>
        <label>问题<input aria-label="检索问题" value={query} disabled={!!busy} onChange={e => setQuery(e.target.value)} /></label>
        <label>最多返回条数<input aria-label="检索条数" type="number" min={1} max={20} value={topK} disabled={!!busy} onChange={e => setTopK(Number(e.target.value))} /></label>
        <label>最低相似度<input aria-label="最低相似度" type="number" min={-1} max={1} step={0.05} value={minimum} disabled={!!busy} onChange={e => setMinimum(Number(e.target.value))} /></label>
        <button disabled={!!busy || !query.trim()} onClick={() => void act('检索', async () => setResult(await api<KnowledgeSearchResult>(base + `/knowledge/${selected}/search`, {method: 'POST', body: JSON.stringify({query, top_k: topK, minimum_score: minimum})}))) }>检索</button>
      </div>
      {result && <KnowledgeResults result={result} onFile={onFile} />}
    </>}
    {busy && <p role="status">正在{busy}…</p>}{message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}
  </section>
}
