'use client'

import { useState } from 'react'
import { api } from '@/lib/platform'
import styles from './workspace-tools.module.css'

type Graph = { nodes: unknown[]; edges: unknown[] }
type Edit = { workflow_id: string; previous_workflow: Graph; draft: { revision: number } }

export default function WorkflowComposer({ projectId, workflowId = '', onChanged }: { projectId: string; workflowId?: string; onChanged: (id: string) => void }) {
  const [instruction, setInstruction] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [last, setLast] = useState<Edit>()
  const base = `/api/v1/projects/${projectId}`
  async function generate() {
    setBusy(true); setError('')
    try {
      const draft = workflowId ? await api<{ revision: number }>(`/api/v1/applications/${workflowId}/draft`) : undefined
      const result = await api<Edit>(base + '/workflow-generation', { method: 'POST', body: JSON.stringify({ instruction, advanced_blocks: advanced, workflow_id: workflowId, expected_revision: draft?.revision, name: instruction.slice(0,60) }) })
      setLast(result); onChanged(result.workflow_id)
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  async function undo() {
    if (!last) return
    setBusy(true); setError('')
    try {
      await api(base + `/workflows/${last.workflow_id}/draft`, { method: 'PUT', body: JSON.stringify({ expected_revision: last.draft.revision, workflow: last.previous_workflow, request_key: crypto.randomUUID() }) })
      onChanged(last.workflow_id); setLast(undefined)
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  return <section className={styles.section} aria-label="生成工作流">
    <div><h2>{workflowId ? '让 AI 修改工作流' : '生成工作流'}</h2><p>描述需要的流程。模型和数据可以稍后配置，生成后可直接编辑。</p></div>
    <div className={styles.row}><input aria-label="工作流描述" placeholder="例如：读取数据，调用预测模型，导出结果" value={instruction} onChange={e => setInstruction(e.target.value)} />
      <button disabled={busy || !instruction.trim()} onClick={() => void generate()}>{busy ? '正在处理…' : workflowId ? '应用修改' : '生成工作流'}</button>
      {last?.workflow_id === workflowId && <button disabled={busy} onClick={() => void undo()}>撤销 AI 修改</button>}</div>
    <details><summary>生成选项</summary><label><input type="checkbox" checked={advanced} onChange={e=>setAdvanced(e.target.checked)}/>使用高级积木（仍受项目权限限制）</label></details>
    {last && <p role="status">已保存到画布，可继续编辑或运行。</p>}{error && <p role="alert">{error}</p>}
  </section>
}
