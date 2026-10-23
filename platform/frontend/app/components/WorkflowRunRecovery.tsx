'use client'
import {useRef,useState} from 'react'
import {api} from '@/lib/platform'
import type {ProjectTask} from '@/lib/project-progress'

export default function WorkflowRunRecovery({projectId,task,onTask}:{projectId:string;task:ProjectTask;onTask?:(task:ProjectTask)=>void}){
  const [busy,setBusy]=useState(false),[error,setError]=useState('')
  const lock=useRef(false),requestKey=useRef('')
  if(task.mode!=='workflow'||!['failed','interrupted'].includes(task.status))return null
  async function run(repair:boolean){if(lock.current)return;lock.current=true;setBusy(true);setError('')
    try{if(repair&&!requestKey.current)requestKey.current=crypto.randomUUID()
      const next=await api<ProjectTask>(`/api/v1/projects/${projectId}/tasks${repair?'':'/'+task.id+'/resume'}`,{method:'POST',body:JSON.stringify(repair?{request_key:requestKey.current,workflow_id:task.workflow_id,inputs:task.inputs||{},recovery_task_id:task.id,purpose:task.purpose||'business'}:{message:'继续原任务'})})
      onTask?.(next)
    }catch(cause){setError(String(cause))}finally{lock.current=false;setBusy(false)}}
  return <section aria-label="继续或修复运行"><h3>继续处理剩余操作</h3><p>继续原任务使用原流程和输入，保留已确认完成的步骤。连接停用或范围修改后不能继续。</p><button disabled={busy} onClick={()=>void run(false)}>继续原任务</button>
    <p>需要修改步骤时，先<a href={`/applications/${task.workflow_id}?tab=edit`} target="_top">编辑并保存工作流</a>，再创建修复运行。原目标和进度会提供给新流程，旧报告不作为新结果。</p><button disabled={busy} onClick={()=>void run(true)}>修改流程后重新运行</button>{error&&<p role="alert">{error}</p>}</section>
}
