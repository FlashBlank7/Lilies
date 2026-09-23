'use client'
import { useState } from 'react'
import Link from 'next/link'
import { api } from '@/lib/platform'
import { useAccount } from './AuthBoundary'
import ReadingDialog from './ReadingDialog'
import styles from '@/app/projects/projects.module.css'

type Source = {allowed: boolean; task:'api'|'official'; generation:'inherit'|'api'|'official'; service_enabled?:boolean; model?:string; thinking?:string}
export default function AssistantSourcePanel({base,running,onSaved}:{base:string;running:boolean;onSaved:()=>Promise<unknown>}) {
  const account = useAccount()
  const [value,setValue] = useState<Source>()
  const [capability,setCapability] = useState(false)
  const [open,setOpen] = useState(false)
  const [busy,setBusy] = useState(false)
  const [error,setError] = useState('')
  async function edit() {
    setBusy(true);setError('')
    try {const [source,project]=await Promise.all([api<Source>(base+'/assistant-source'),api<{agent_modules_enabled:boolean}>(base)]);setValue(source);setCapability(project.agent_modules_enabled);setOpen(true)} catch(e){setError(String(e))} finally{setBusy(false)}
  }
  async function save() {
    if(!value)return
    setBusy(true);setError('')
    try {
      if(account?.role==='admin')await api(base+'/capabilities',{method:'PUT',body:JSON.stringify({agent_modules_enabled:capability})})
      await api(base+'/assistant-source',{method:'PUT',body:JSON.stringify({allowed:value.allowed,task:value.task,generation:value.generation})})
      await onSaved();setOpen(false)
    } catch(e){setError(String(e))} finally{setBusy(false)}
  }
  return <div className={styles.modelConnection}><button disabled={busy||running} onClick={()=>void edit()}>智能体来源</button>
    {open&&value&&<ReadingDialog title="项目智能体设置" onClose={()=>setOpen(false)}><div className={styles.modelSetup}>
      <p>选择处理任务及生成工作流的智能体。工作流中的 LLM、视觉与 Embedding 节点继续使用各自的 API 连接。</p>
      {account?.role==='admin'&&<><label><input type="checkbox" checked={capability} onChange={e=>setCapability(e.target.checked)}/> 允许项目使用完整智能体能力</label><label><input type="checkbox" checked={value.allowed} onChange={e=>setValue({...value,allowed:e.target.checked})}/> 授权此项目使用官方智能体</label><Link href="/official-agent">管理官方智能体服务</Link></>}
      <label>任务助手<select value={value.task} onChange={e=>setValue({...value,task:e.target.value as Source['task']})}><option value="api">项目 API 模型</option><option value="official" disabled={!value.allowed||!capability}>官方智能体{!value.allowed||!capability?'（需管理员授权）':''}</option></select></label>
      <label>工作流生成<select value={value.generation} onChange={e=>setValue({...value,generation:e.target.value as Source['generation']})}><option value="inherit">沿用任务助手</option><option value="api">项目工作流生成 API</option><option value="official" disabled={!value.allowed||!capability}>官方智能体</option></select></label>
      {value.task==='official'||value.generation==='official'?<p>{value.service_enabled?'服务已启用':'服务尚未启用，请联系管理员'}。模型：{value.model}，思考强度：{value.thinking}。不需要填写密钥。</p>:null}
      <button disabled={busy||running} onClick={()=>void save()}>保存智能体设置</button>{error&&<p role="alert">{error}</p>}
    </div></ReadingDialog>}{!open&&error&&<p role="alert">{error}</p>}
  </div>
}
