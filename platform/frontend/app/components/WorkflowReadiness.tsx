'use client'

import Link from 'next/link'

export type Readiness = {status:'configured'|'needs_setup';issues:{code:string;message:string;setup:string;node?:string;help?:string}[];note:string;revision?:number}
export default function WorkflowReadiness({value,projectId,onSettings}:{value?:Readiness;projectId?:string;onSettings?:()=>void}) {
  if(!value)return null
  return <section aria-label="运行准备">
    <p><strong>{value.status==='configured'?'基础资源检查通过':'运行前需要准备'}</strong></p>
    {!!value.issues.length&&<ul>{value.issues.map(issue=><li key={issue.code}>
      {issue.node&&<strong>{issue.node}：</strong>}{issue.message}
      {issue.help&&<details><summary>管理员处理方法</summary><p>{issue.help}</p></details>}
      {issue.setup==='admin' ? <span> 请联系平台管理员处理。</span> : projectId ? <><span> </span>{onSettings&&issue.setup==='settings'?<button onClick={onSettings}>查看项目设置</button>:<Link href={`/projects/${projectId}?tab=${issue.setup}`}>{({models:'配置预测模型',materials:'配置资料与知识',flow:'打开工作流',settings:'查看项目设置'} as Record<string,string>)[issue.setup]||'查看配置'}</Link>}<span> 无配置权限时请联系项目负责人。</span></>:<span> 创建后在项目中配置。</span>}
    </li>)}</ul>}
    <details><summary>检查范围</summary><p>{value.note}</p></details>
  </section>
}
