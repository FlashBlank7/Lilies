'use client'

import Link from 'next/link'
import { useEffect, useRef, useState } from 'react'
import ReadingDialog from '../components/ReadingDialog'
import { api, type Draft, type Snapshot } from '@/lib/platform'
import { clientId } from '@/lib/client-id'
import base from '../projects/projects.module.css'
import styles from './improvements.module.css'

type Graph = Snapshot['workflow']
type Edit = { workflow_id: string; previous_workflow: Graph; revision: number }

// Compare values independently of JSON object key order.
function canonical(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
  if (value && typeof value === 'object') return '{' + Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, entry]) => JSON.stringify(key) + ':' + canonical(entry)).join(',') + '}'
  return JSON.stringify(value) ?? 'null'
}

export default function ApplyWorkflowCopy({ projectId, sourceId, targetId, onClose }: {
  projectId: string; sourceId: string; targetId: string; onClose: () => void
}) {
  const [drafts, setDrafts] = useState<{ source: Draft; target: Draft } | null>(null)
  const [edit, setEdit] = useState<Edit | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [undone, setUndone] = useState(false)
  const lock = useRef(false)
  const alive = useRef(false)
  const endpoint = `/api/v1/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(targetId)}/draft`

  useEffect(() => {
    alive.current = true
    let active = true
    setLoading(true); setError(''); setDrafts(null)
    void Promise.all([
      api<{ members: { id: string }[] }>(`/api/v1/projects/${encodeURIComponent(projectId)}`),
      api<Draft>(`/api/v1/applications/${encodeURIComponent(sourceId)}/draft`),
      api<Draft>(`/api/v1/applications/${encodeURIComponent(targetId)}/draft`),
    ]).then(([project, source, target]) => {
      if (!active) return
      if (sourceId === targetId || ![sourceId, targetId].every(id => project.members.some(member => member.id === id))) throw new Error('请选择同一项目中的两个不同工作流。')
      setDrafts({ source, target })
    }).catch(cause => { if (active) setError(String(cause)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false; alive.current = false }
  }, [projectId, sourceId, targetId, attempt])

  async function save(undo = false) {
    if (!drafts || lock.current || (undo && !edit)) return
    lock.current = true; setBusy(true); setError('')
    try {
      const result = await api<Edit>(endpoint, { method: 'PUT', body: JSON.stringify({
        expected_revision: undo ? edit!.revision : drafts.target.revision,
        workflow: undo ? edit!.previous_workflow : drafts.source.snapshot.workflow,
        request_key: clientId(),
      }) })
      if (!alive.current) return
      if (undo) { setEdit(null); setUndone(true) } else setEdit(result)
    } catch (cause) { if (alive.current) setError(String(cause)) }
    finally { lock.current = false; if (alive.current) setBusy(false) }
  }

  const source = drafts?.source.snapshot.workflow, target = drafts?.target.snapshot.workflow
  const changes = source && target ? [
    ...target.nodes.filter(node => !source.nodes.some(n => n.id === node.id)).map(node => ({ id: node.id, title: node.title || node.id, label: '移除', before: node, after: null })),
    ...source.nodes.flatMap(node => {
      const before = target.nodes.find(n => n.id === node.id)
      return !before || canonical(before) !== canonical(node) ? [{ id: node.id, title: node.title || node.id, label: before ? '修改' : '新增', before: before || null, after: node }] : []
    }),
  ] : []
  const unchanged = source && target && canonical(source) === canonical(target)

  return <ReadingDialog title="用此流程更新原流程" wide onClose={() => { if (!lock.current) onClose() }}>
    <div className={styles.copyBody}>
      {loading && <p role="status">正在读取两个工作流的当前草稿…</p>}
      {error && <p role="alert" className={base.error}>{error}</p>}
      {drafts && <>
        <p>来源：<strong>{drafts.source.snapshot.name}</strong> · r{drafts.source.revision}</p>
        <p>更新对象：<strong>{drafts.target.snapshot.name}</strong> · r{drafts.target.revision}</p>
        <p>将来源草稿的完整流程图保存到原工作流，保留原编号和名称。原有入口之后运行新草稿；历史结果和正在运行的任务保留原快照。</p>
        <p>这里比较的是当前草稿，可能与上方历史运行的版本不同。子流程、模型、文件和连接不会被复制或替换，仍按图中的引用使用。</p>
        <div className={base.actions}>
          <Link href={`/applications/${encodeURIComponent(sourceId)}?tab=edit`} target="_blank" rel="noopener noreferrer">查看来源画布</Link>
          <Link href={`/applications/${encodeURIComponent(targetId)}?tab=edit`} target="_blank" rel="noopener noreferrer">查看原流程画布</Link>
        </div>
        {!edit && !undone && <>
          <h3>将应用的改动</h3>
          {unchanged ? <p>两个流程图内容相同，无需更新。</p> : <>
            <ul>{changes.map(change => <li key={change.id}><details><summary>{change.label}积木：{change.title}</summary><p>原配置</p><pre>{JSON.stringify(change.before, null, 2)}</pre><p>来源配置</p><pre>{JSON.stringify(change.after, null, 2)}</pre></details></li>)}</ul>
            {canonical(source?.edges) !== canonical(target?.edges) && <p>连线将由 {target?.edges.length} 条更新为 {source?.edges.length} 条，端点或条件也可能变化。</p>}
            <details><summary>查看完整流程图差异</summary><p>原流程图</p><pre>{JSON.stringify(target, null, 2)}</pre><p>应用后的流程图</p><pre>{JSON.stringify(source, null, 2)}</pre></details>
          </>}
        </>}
        {edit && <p role="status">已更新原工作流，本次保存为 r{edit.revision}。没有启动运行。</p>}
        {undone && <p role="status">已撤销本次更新，原流程图已恢复。没有启动运行。</p>}
      </>}
      <div className={base.actions}>
        {!edit && !undone && <button disabled={loading || busy} onClick={() => setAttempt(value => value + 1)}>重新读取草稿</button>}
        {drafts && !edit && !undone && <button disabled={busy || !!unchanged} onClick={() => void save()}>{busy ? '正在保存…' : '应用到原工作流'}</button>}
        {edit && <button disabled={busy} onClick={() => void save(true)}>{busy ? '正在撤销…' : '撤销本次更新'}</button>}
        <button disabled={busy} onClick={onClose}>关闭</button>
      </div>
    </div>
  </ReadingDialog>
}
