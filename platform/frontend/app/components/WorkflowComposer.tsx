'use client'

import { clientId } from '@/lib/client-id'

import { useEffect, useRef, useState } from 'react'
import { api, type WorkflowNode } from '@/lib/platform'
import styles from './workspace-tools.module.css'

type Graph = { nodes: unknown[]; edges: unknown[] }
function innerWorkflows(nodes: WorkflowNode[], path: string[] = [], prefix = ''): { path: string[]; label: string }[] {
  return nodes.flatMap(node => {
    const workflow = node.config.workflow as { nodes?: WorkflowNode[] } | undefined
    if (!['loop', 'iteration'].includes(node.type) || !Array.isArray(workflow?.nodes)) return []
    const next = [...path, node.id], label = prefix + (node.title || node.id)
    return [{ path: next, label }, ...innerWorkflows(workflow.nodes, next, label + ' / ')]
  })
}

type Edit = { workflow_id: string; previous_workflow: Graph; draft: { revision: number } }

export default function WorkflowComposer({ projectId, workflowId = '', onChanged, nodes = [], selectedNodeIds = [], selectionPath = [], revision, disabled = false, editRequest = 0 }: { projectId: string; workflowId?: string; onChanged: (id: string) => void; nodes?: WorkflowNode[]; selectedNodeIds?: string[]; selectionPath?: string[]; revision?: number; disabled?: boolean; editRequest?: number }) {
  const [instruction, setInstruction] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [scope, setScope] = useState('whole')
  const inputRef = useRef<HTMLInputElement>(null)
  const canvasScope = JSON.stringify(selectionPath)
  useEffect(() => { setScope(selectionPath.length ? canvasScope : 'whole') }, [canvasScope])
  useEffect(() => {
    if (!editRequest) return
    setScope(selectedNodeIds.length ? 'selection' : selectionPath.length ? JSON.stringify(selectionPath) : 'whole')
    inputRef.current?.focus({ preventScroll: true })
    inputRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [editRequest])
  const scopes = innerWorkflows(nodes)
  const scopeValid = scope === 'whole' || (scope === 'selection' ? selectedNodeIds.length > 0 : scopes.some(item => JSON.stringify(item.path) === scope))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [last, setLast] = useState<Edit>()
  const base = `/api/v1/projects/${projectId}`
  async function generate() {
    setBusy(true); setError('')
    try {
      const draft = revision !== undefined ? { revision } : workflowId ? await api<{ revision: number }>(`/api/v1/applications/${workflowId}/draft`) : undefined
      const result = await api<Edit>(base + '/workflow-generation', { method: 'POST', body: JSON.stringify({ instruction, node_ids: scope === 'selection' ? selectedNodeIds : [], workflow_path: scope === 'selection' ? selectionPath : scope !== 'whole' ? JSON.parse(scope) : [], advanced_blocks: advanced, workflow_id: workflowId, expected_revision: draft?.revision, name: instruction.slice(0,60) }) })
      setLast(result); onChanged(result.workflow_id)
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  async function undo() {
    if (!last) return
    setBusy(true); setError('')
    try {
      await api(base + `/workflows/${last.workflow_id}/draft`, { method: 'PUT', body: JSON.stringify({ expected_revision: last.draft.revision, workflow: last.previous_workflow, request_key: clientId() }) })
      onChanged(last.workflow_id); setLast(undefined)
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  return <section className={styles.section} aria-label="生成工作流">
    <div><h2>{workflowId ? '让 AI 修改工作流' : '生成工作流'}</h2><p>描述需要的流程。模型和数据可以稍后配置，生成后可直接编辑。</p></div>
    {workflowId && nodes.length > 0 && <label>修改范围<select aria-label="修改范围" value={scope} onChange={event => setScope(event.target.value)}><option value="whole">整个工作流</option><option value="selection" disabled={!selectedNodeIds.length}>选中节点（{selectedNodeIds.length}）</option>{scopes.map(item => <option key={JSON.stringify(item.path)} value={JSON.stringify(item.path)}>循环内部：{item.label}</option>)}</select></label>}
    {scope === 'selection' && <p>只修改选区，保留与外部节点的连接。需要改变连接端点时，请扩大选区。</p>}
    {disabled && <p>先保存当前节点配置，再应用 AI 修改。</p>}
    <div className={styles.row}><input ref={inputRef} aria-label="工作流描述" placeholder="例如：读取数据，调用预测模型，导出结果" value={instruction} onChange={e => setInstruction(e.target.value)} />
      <button disabled={disabled || busy || !instruction.trim() || !scopeValid} onClick={() => void generate()}>{busy ? '正在处理…' : workflowId ? '应用修改' : '生成工作流'}</button>
      {last?.workflow_id === workflowId && <button disabled={disabled || busy} onClick={() => void undo()}>撤销 AI 修改</button>}</div>
    <details><summary>生成选项</summary><label><input type="checkbox" checked={advanced} onChange={e=>setAdvanced(e.target.checked)}/>使用高级积木（仍受项目权限限制）</label></details>
    {last && <p role="status">已保存到画布，可继续编辑或运行。</p>}{error && <p role="alert">{error}</p>}
  </section>
}
