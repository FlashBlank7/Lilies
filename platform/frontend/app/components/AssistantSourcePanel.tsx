'use client'
import { useState } from 'react'
import Link from 'next/link'
import { api } from '@/lib/platform'
import { useAccount } from './AuthBoundary'
import ReadingDialog from './ReadingDialog'
import OfficialConnectionStatus, {type OfficialConnection} from './OfficialConnectionStatus'
import styles from '@/app/projects/projects.module.css'

type Source = OfficialConnection & {allowed: boolean; task:'api'|'official'; generation:'inherit'|'api'|'official'; service_enabled?:boolean; model?:string; thinking?:string}
export default function AssistantSourcePanel({base,running,onSaved}:{base:string;running:boolean;onSaved:()=>Promise<unknown>}) {
  const account = useAccount()
  const [value,setValue] = useState<Source>()
  const [open,setOpen] = useState(false)
  const [busy,setBusy] = useState(false)
  const [error,setError] = useState('')
  async function edit() {
    setBusy(true);setError('')
    try {const source=await api<Source>(base+'/assistant-source');setValue(source);setOpen(true)} catch(e){setError(String(e))} finally{setBusy(false)}
  }
  async function save() {
    if(!value)return
    setBusy(true);setError('')
    try {
      await api(base+'/assistant-source',{method:'PUT',body:JSON.stringify({allowed:value.task==='official'||value.generation==='official',task:value.task,generation:value.generation})})
      await onSaved();setOpen(false)
    } catch(e){setError(String(e))} finally{setBusy(false)}
  }
  if(account?.role!=='admin')return null
  return <div className={styles.modelConnection}><button disabled={busy||running} onClick={()=>void edit()}>配置助手来源</button>
    {open&&value&&<ReadingDialog title="项目智能体设置" onClose={()=>setOpen(false)}><div className={styles.modelSetup}>
      <p>选择处理任务及生成工作流的智能体。工作流中的 LLM、视觉与 Embedding 节点继续使用各自的 API 连接。</p>
      <p>由平台管理员配置。任务助手和工作流生成可以分别选择，项目可调用能力在项目空间中管理。</p><Link href="/official-agent">管理 Codex 服务</Link>
      <label>任务助手<select value={value.task} onChange={e=>setValue({...value,task:e.target.value as Source['task']})}><option value="api">项目 API 模型</option><option value="official">平台 Codex 服务</option></select></label>
      <label>工作流生成<select value={value.generation} onChange={e=>setValue({...value,generation:e.target.value as Source['generation']})}><option value="inherit">沿用任务助手</option><option value="api">项目工作流生成 API</option><option value="official">平台 Codex 服务</option></select></label>
      {value.task==='official'||value.generation==='official'?<><p>{value.service_enabled?'服务已启用':'服务尚未启用，请联系管理员'}。模型：{value.model}，思考强度：{value.thinking}。不需要填写密钥。</p><OfficialConnectionStatus connection={value}/></>:null}
      <button disabled={busy||running} onClick={()=>void save()}>保存智能体设置</button>{error&&<p role="alert">{error}</p>}
    </div></ReadingDialog>}{!open&&error&&<p role="alert">{error}</p>}
  </div>
}
