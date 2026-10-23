'use client'
import {useEffect,useState} from 'react'
import Link from 'next/link'
import {api} from '@/lib/platform'
import {useAccount} from '@/app/components/AuthBoundary'
import AssistantSourcePanel from '@/app/components/AssistantSourcePanel'
import ModelConnectionPanel from '@/app/components/ModelConnectionPanel'

export default function ProjectAssistantSettings(){
  const account=useAccount()
  const [projects,setProjects]=useState<{id:string;name:string}[]>([])
  const [selected,setSelected]=useState('')
  const [error,setError]=useState('')
  useEffect(()=>{if(account?.role==='admin')void api<{id:string;name:string}[]>('/api/v1/projects').then(rows=>{setProjects(rows);setSelected(rows[0]?.id||'')}).catch(e=>setError(String(e)))},[account?.role])
  if(account?.role!=='admin')return <main><h1>项目助手配置</h1><p>此页面仅供平台管理员使用。</p></main>
  const base='/api/v1/projects/'+selected
  return <main style={{padding:32,maxWidth:900,margin:'auto'}}><h1>项目助手配置</h1>
    <p>管理员选择各项目的任务助手、生成服务及模型连接。业务人员在项目空间装配工作流、加入预设和任务智能体；助手只能调用已加入的能力。</p>
    {error&&<p role="alert">{error}</p>}
    <label>配置项目<select value={selected} onChange={e=>setSelected(e.target.value)}>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
    {selected&&<section key={selected}><AssistantSourcePanel base={base} running={false} onSaved={async()=>{}}/>
      <ModelConnectionPanel base={base} connected={true} running={false} onSaved={async()=>{}}/>
      <ModelConnectionPanel base={base} role="generation" connected={false} running={false} onSaved={async()=>{}}/>
      <p><Link href={'/projects/'+selected}>打开项目空间</Link></p></section>}
    <p><Link href="/official-agent">管理平台 Codex 服务账号与用量</Link></p>
  </main>
}
