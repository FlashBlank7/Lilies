'use client'

import Link from 'next/link'
import {useEffect, useState} from 'react'
import {api, type Draft} from '@/lib/platform'
import {containerWorkflow, type WorkflowGraph} from '@/lib/workflow-scope'
import ModelConnectionPanel from './ModelConnectionPanel'

export type Readiness = {status:'configured'|'needs_setup';issues:{code:string;message:string;setup:string;node?:string;help?:string}[];note:string;revision?:number}

function missingModelNodes(graph: WorkflowGraph, scope: string[] = []): {title: string; path: string[]}[] {
  return graph.nodes.flatMap(node => {
    const path = [...scope, node.id]
    const nested = containerWorkflow(node)
    return [
      ...(node.type === 'model_predict' && (node.config.model_ref === '' || node.config.model_ref === undefined) ? [{title: node.title || node.id, path}] : []),
      ...(nested ? missingModelNodes(nested, path) : []),
    ]
  })
}

function ModelSelectionLinks({projectId,workflowId,revision}:{projectId:string;workflowId?:string;revision?:number}) {
  const [nodes,setNodes]=useState<ReturnType<typeof missingModelNodes>>([])
  useEffect(()=>{
    let active=true
    setNodes([])
    if(workflowId)void api<Draft>(`/api/v1/applications/${workflowId}/draft`).then(draft=>{
      if(active)setNodes(missingModelNodes(draft.snapshot.workflow))
    }).catch(()=>{})
    return()=>{active=false}
  },[workflowId,revision])
  return <>
    <span> 请在预测积木的“调用模型”中选择已有模型，再保存配置。</span>
    {nodes.length ? nodes.map(node=><span key={JSON.stringify(node.path)}> <Link target="_blank" href={`/applications/${workflowId}?tab=edit&node_path=${encodeURIComponent(JSON.stringify(node.path))}`}>选择预测模型 · {node.title}</Link></span>)
      : <><span> </span><Link target="_blank" href={workflowId?`/applications/${workflowId}?tab=edit`:`/projects/${projectId}?tab=flow`}>打开工作流选择预测模型</Link></>}
  </>
}

export default function WorkflowReadiness({value,projectId,workflowId,onSettings,canConfigureModel=false,onRecheck}:{value?:Readiness;projectId?:string;workflowId?:string;onSettings?:()=>void;canConfigureModel?:boolean;onRecheck?:()=>Promise<unknown>}) {
  if(!value)return null
  return <section aria-label="运行准备">
    <p><strong>{value.status==='configured'?'基础资源检查通过':'运行前需要准备'}</strong></p>
    {!!value.issues.length&&<ul>{value.issues.map(issue=><li key={issue.code}>
      {issue.node&&<strong>{issue.node}：</strong>}{issue.code==='resource:'?'此预测积木尚未选择模型资源。':issue.message}
      {issue.help&&<details><summary>管理员处理方法</summary><p>{issue.help}</p></details>}
      {projectId && issue.code==='resource:' ? <ModelSelectionLinks projectId={projectId} workflowId={workflowId} revision={value.revision}/>
        : projectId && canConfigureModel && ['model:main','model:vision'].includes(issue.code)
        ? <ModelConnectionPanel base={`/api/v1/projects/${projectId}`} connected running={false} role={issue.code==='model:vision'?'vision':'main'} onSaved={onRecheck||(()=>Promise.resolve())}/>
        : issue.setup==='admin' ? <span> 请联系平台管理员处理。</span> : projectId ? <><span> </span>{onSettings&&issue.setup==='settings'?<button onClick={onSettings}>查看项目设置</button>:<Link href={`/projects/${projectId}?tab=${issue.setup}`}>{({models:'配置预测模型',materials:'配置资料与知识',flow:'打开工作流',settings:'查看项目设置'} as Record<string,string>)[issue.setup]||'查看配置'}</Link>}<span> 无配置权限时请联系项目负责人。</span></>:<span> 创建后在项目中配置。</span>}
    </li>)}</ul>}
    <details><summary>检查范围</summary><p>{value.note}</p></details>
  </section>
}
