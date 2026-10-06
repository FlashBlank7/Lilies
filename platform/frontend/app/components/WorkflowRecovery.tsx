'use client'

import {useCallback, useEffect, useState} from 'react'
import {api} from '@/lib/platform'
import WorkflowReadiness, {type Readiness} from './WorkflowReadiness'

/** Recheck current configuration while keeping the failed run and edited inputs. */
export default function WorkflowRecovery({projectId,workflowId,canConfigureModel=false,onChanged}:{projectId:string;workflowId:string;canConfigureModel?:boolean;onChanged?:()=>void}) {
  const [value,setValue]=useState<Readiness>()
  const [error,setError]=useState('')
  const [changed,setChanged]=useState(false)
  const refresh=useCallback(async()=>{
    try {setValue(await api<Readiness>(`/api/v1/projects/${projectId}/space/workflows/${workflowId}/readiness`));setError('')}
    catch {setError('暂时无法检查当前配置，可重新检查。原运行记录仍保留。')}
  },[projectId,workflowId])
  useEffect(()=>{void refresh()},[refresh])
  return <section aria-label="修复运行配置">
    <WorkflowReadiness value={value} projectId={projectId} workflowId={workflowId} canConfigureModel={canConfigureModel} onRecheck={async()=>{await refresh();setChanged(true);onChanged?.()}}/>
    {changed&&<p role="status">配置已保存。请确认运行准备后重新运行；原失败记录保留，不会自动重试。</p>}
    {error&&<p role="status">{error}</p>}
    <button onClick={()=>void refresh()}>重新检查当前配置</button>
  </section>
}
