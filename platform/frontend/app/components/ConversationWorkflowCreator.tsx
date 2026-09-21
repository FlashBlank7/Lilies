'use client'

import { useEffect, useRef, useState } from 'react'
import { api } from '@/lib/platform'
import type { ModelingContext } from './ModelingPanel'
import type { ProjectMember } from '@/lib/project-progress'
import styles from './conversation-workflow.module.css'

export type WorkflowCard = { id: string; name: string; revision: number; node_count: number; nodes: {id: string; title: string; type: string}[] }
type Generation = { workflow_id: string; previous_workflow: unknown; draft: { revision: number }; workflow_card: WorkflowCard }
type Props = { projectId: string; conversationId?: string; visible: boolean; storageKey: string; message: string;
  members: ProjectMember[]; context: ModelingContext | null; taskId?: string; target?: WorkflowCard;
  onClearTarget: () => void; onSaved: (submitted: string) => void; onWorkflow?: (id: string) => void }

export default function ConversationWorkflowCreator({projectId, conversationId, visible, storageKey, message, members, context, taskId, target, onClearTarget, onSaved, onWorkflow}: Props) {
  const [name, setName] = useState('')
  const [references, setReferences] = useState<string[]>([])
  const [files, setFiles] = useState<{path: string; size: number}[]>([])
  const [selectedFiles, setSelectedFiles] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [last, setLast] = useState<Generation>()
  const lock = useRef(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    try { const saved = JSON.parse(sessionStorage.getItem(storageKey + ':generation') || '{}'); setName(saved.name || ''); setReferences(saved.references || []); setSelectedFiles(saved.files || []) } catch {}
  }, [storageKey])
  function remember(nextName = name, nextReferences = references, nextFiles = selectedFiles) {
    try { sessionStorage.setItem(storageKey + ':generation', JSON.stringify({name: nextName, references: nextReferences, files: nextFiles})) } catch {}
  }
  useEffect(() => {
    if (!visible) return
    let active = true
    void api<{files: typeof files}>(`/api/v1/projects/${projectId}/space`).then(space => { if(active) setFiles(space.files || []) }).catch(cause => { if(active) setError(String(cause)) })
    return () => { active = false }
  }, [projectId, visible])
  const base = `/api/v1/projects/${projectId}`
  async function generate() {
    if (lock.current) return
    lock.current = true; setBusy(true); setError('')
    const submitted = message
    try {
      const draft = target ? await api<{revision: number}>(`/api/v1/applications/${target.id}/draft`) : undefined
      const result = await api<Generation>(`${base}/conversations/${conversationId || 'legacy'}/workflow-generation`, {method: 'POST', body: JSON.stringify({
        instruction: submitted, name: target?.name || name.trim() || submitted.slice(0, 60),
        workflow_id: target?.id || '', expected_revision: draft?.revision,
        reference_workflow_ids: references, file_paths: selectedFiles,
        dataset_id: context?.dataset_id || '', study_id: context?.study_id || '', candidate_id: context?.candidate_id || '', task_id: taskId || context?.task_id || '',
      })})
      if (!mounted.current) return
      setLast(result); onSaved(submitted)
    } catch(cause) { if(mounted.current) setError(String(cause)) }
    finally { lock.current = false; if(mounted.current) setBusy(false) }
  }
  async function undo() {
    if (!last || lock.current) return
    lock.current = true; setBusy(true); setError('')
    try {
      await api(`${base}/workflows/${last.workflow_id}/draft`, {method:'PUT',body:JSON.stringify({expected_revision:last.draft.revision,workflow:last.previous_workflow})})
      setLast(undefined); onSaved('')
    } catch(cause) { setError(String(cause)) } finally {lock.current=false;setBusy(false)}
  }
  return <div hidden={!visible} className={styles.creator} aria-label="对话创建工作流">
    <p>{target ? `正在修改：${target.name}` : '根据当前对话创建新工作流，保存到本项目。参考流程保持原样。'}{target && <button disabled={busy} onClick={onClearTarget}>改为创建新流程</button>}</p>
    {!target && <label>新工作流名称<input aria-label="对话中的新工作流名称" maxLength={100} value={name} disabled={busy} placeholder="可选，留空时按描述命名" onChange={e=>{setName(e.target.value);remember(e.target.value)}} /></label>}
    <details><summary>参考已有工作流（已选 {references.length}）</summary>
      <p>可选择一条或多条作为只读参考，也可以从空白创建。</p>
      {members.filter(m=>m.purpose !== 'test').map(m=><label key={m.id} className={styles.choice}><input type="checkbox" checked={references.includes(m.id)} disabled={busy || (!references.includes(m.id) && references.length >= 8)} onChange={e=>{const next=e.target.checked?[...references,m.id]:references.filter(id=>id!==m.id);setReferences(next);remember(name,next)}} />{m.name}</label>)}
    </details>
    <details><summary>关联项目文件（已选 {selectedFiles.length}）</summary>
      {!files.length && <p>项目空间中尚无文件，可先创建流程，稍后上传资料。</p>}
      {files.map(file=><label key={file.path} className={styles.choice}><input type="checkbox" checked={selectedFiles.includes(file.path)} disabled={busy || (!selectedFiles.includes(file.path) && selectedFiles.length>=20)} onChange={e=>{const next=e.target.checked?[...selectedFiles,file.path]:selectedFiles.filter(p=>p!==file.path);setSelectedFiles(next);remember(name,references,next)}} />{file.path}</label>)}
    </details>
    <div className={styles.actions}><button disabled={busy || !message.trim()} onClick={()=>void generate()}>{busy?'正在生成…':target?'保存工作流修改':'生成新工作流'}</button>
      {last && <><button disabled={busy} onClick={()=>onWorkflow?.(last.workflow_id)}>查看已保存流程</button><button disabled={busy} onClick={()=>void undo()}>撤销本次生成修改</button></>}
    </div>
    {busy && <p role="status">正在生成并保存，完成后显示在对话中。不会启动业务运行。</p>}
    {last && <p role="status">已保存：{last.workflow_card.name}。可以在对话中查看、继续修改或调用。</p>}
    {error && <p role="alert">{error}</p>}
  </div>
}
