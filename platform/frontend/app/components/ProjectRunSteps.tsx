'use client'

import {useEffect, useState} from 'react'
import Link from 'next/link'
import {api} from '@/lib/platform'
import {resolveProjectLink} from '@/lib/project-links'
import {MarkdownDocument} from '@/lib/markdown'
import type {ProjectTask} from '@/lib/project-progress'
import TaskError from './TaskError'
import styles from './project-run-steps.module.css'

export type RunStep = {
  id:string; node_path:string[]; title:string; description:string; type:string; status:string
  scope?:string; input_source?:string; input_preview:unknown; output_preview:unknown
  input_summary?:unknown; output_summary?:unknown
  error?:string|null; duration_ms?:number|null
}
type StepPage = {run_id:string;application_id:string;name:string;status:string;steps:RunStep[];total:number;next_offset?:number|null;error?:string;draft_revision?:number|null}
const statuses:Record<string,string> = {pending:'尚未执行',running:'正在处理',completed:'已完成',reused:'复用已有结果',skipped:'此分支未执行',waiting:'等待补充',failed:'失败',interrupted:'已中断',warning:'完成，需留意异常'}
const attentionStatuses = ['failed','waiting','running','warning','interrupted']
const labels:Record<string,string> = {source_path:'资料文件',second_path:'第二份资料',output:'处理结果',outputs:'输出',result:'结果',logs:'执行说明',markdown:'报告正文',rows:'记录数',columns:'字段',preview:'数据预览',artifacts:'结果文件',file_path:'文件',path:'路径',summary:'汇总',message:'说明',items:'条目',count:'数量',error:'错误',value:'数值',inputs:'输入',group_by:'汇总维度',mark_duplicates:'标记疑似重复',suspected_duplicates:'疑似重复数',duplicate_records:'重复记录',missing:'缺失',operation:'处理方式',horizon:'预测步数',omitted_items:'未展示条目数',omitted_fields:'未展示字段'}
const operations:Record<string,string> = {expenses:'费用整理',profile:'数据体检',summary:'数据汇总',join:'多表关联'}
Object.assign(labels,{prepared:'已核对的输入',snapshot_path:'本次输入快照',sources:'资料来源',stock:'物料表',demand:'需求表',stock_path:'物料表',demand_path:'需求表',stock_sheet:'物料工作表',demand_sheet:'需求工作表',config:'本次处理条件',unit:'长度单位',kerf:'单次切缝宽度',kerf_mode:'切缝计数方式',end_allowance:'每根物料共预留长度',max_pieces:'每根最多产出段数',max_types:'每根最多需求种类',search_limit:'最多检查组合分支',length_mode:'需求长度方式',allocation_mode:'范围内长度分配方式',length_precision:'范围长度最多小数位',stocks:'每根物料的处理结果',stock_id:'物料标识',material:'物料类型',candidates:'候选组合数',reason:'无候选原因',patterns_path:'需求数量与分配长度文件',label:'文件说明'})

function Preview({value,projectId,depth=0}:{value:unknown;projectId:string;depth?:number}) {
  if(value===undefined || value===null)return <span className={styles.muted}>无记录</span>
  if(typeof value==='boolean')return <span>{value?'是':'否'}</span>
  if(typeof value==='string') {
    if(/^(results|solution|requirement-package|requirements)\//.test(value) && !/[\n\r]/.test(value)) {
      const href=resolveProjectLink(projectId,value)
      if(href)return <a href={href} target="_blank" rel="noreferrer" title={value}>{value.split('/').pop()} ↗</a>
    }
    return <span className={styles.text}>{value.length>1200?value.slice(0,1200)+'…':value||'（空文本）'}</span>
  }
  if(typeof value!=='object')return <span>{String(value)}</span>
  if(Array.isArray(value)) {
    if(!value.length)return <span>空列表</span>
    if(depth>=3)return <span>{value.length} 项数据</span>
    return <><ul className={styles.values}>{value.slice(0,10).map((item,i)=><li key={i}><Preview value={item} projectId={projectId} depth={depth+1}/></li>)}</ul>{value.length>10&&<small>仅展示前10项</small>}</>
  }
  const object=value as Record<string,unknown>
  if(object.preview_omitted)return <span>较大内容已收起{typeof object.count==='number'?`（${object.count} 项）`:''}，完整内容见原始输入输出或下载文件。</span>
  const entries=Object.entries(object)
  if(!entries.length)return <span className={styles.muted}>无字段</span>
  if(depth>=4)return <span>{entries.length} 个字段</span>
  return <dl className={styles.fields}>{entries.slice(0,12).map(([key,item])=><div key={key}><dt>{labels[key]||key}</dt><dd>{key==='markdown'&&typeof item==='string'?<MarkdownDocument source={item} resolveLink={href=>resolveProjectLink(projectId,href)} emptyLabel=""/>:<Preview value={key==='operation'&&typeof item==='string'?(operations[item]||item):item} projectId={projectId} depth={depth+1}/>}</dd></div>)}{entries.length>12&&<div><dt>更多内容</dt><dd>另有 {entries.length-12} 个字段，见原始输入输出。</dd></div>}</dl>
}

type StepEntry = {kind:'step';step:RunStep;index:number}
type RoundEntry = {kind:'round';key:string;label:string;children:StepGroupEntry[];steps:RunStep[]}
type StepGroupEntry = StepEntry|RoundEntry

function groupSteps(steps:RunStep[],offset:number):StepGroupEntry[] {
  const entries:StepGroupEntry[]=[],groups=new Map<string,RoundEntry>()
  const savedSteps=new Map(steps.map(step=>[JSON.stringify([step.id,step.node_path]),step]))
  steps.forEach((step,index)=>{
    const path=Array.isArray(step.node_path)?step.node_path:[]
    const occurrences:{key:string;containerId:string;ordinal:number}[]=[]
    let prefix=''
    // Match each literal node id against the saved path. A title/scope alone
    // cannot identify a round, and splitting ids on dots loses valid node ids.
    for(const nodeId of path.slice(0,-1)) {
      const containerId=prefix+nodeId
      if(!step.id.startsWith(containerId))break
      const match=step.id.slice(containerId.length).match(/^\[(\d+)\]\./)
      if(!match)break
      const ordinal=Number(match[1])+1
      if(!Number.isSafeInteger(ordinal))break
      prefix=containerId+match[0]
      occurrences.push({key:JSON.stringify([path.slice(0,occurrences.length+1),prefix]),containerId,ordinal})
    }
    const verified=occurrences.length===path.length-1&&step.id===prefix+path.at(-1)
    const scopeParts=(step.scope||'').split(' / ')
    let children=entries
    if(verified)occurrences.forEach((occurrence,depth)=>{
      let group=groups.get(occurrence.key)
      if(!group) {
        const container=savedSteps.get(JSON.stringify([occurrence.containerId,path.slice(0,depth+1)]))
        const suffix=` · 第 ${occurrence.ordinal} 轮`
        const scope=scopeParts.length===occurrences.length?scopeParts[depth]:''
        const title=container?.title||(scope?.endsWith(suffix)?scope.slice(0,-suffix.length):'')
        const label=title?`${title} · 第 ${occurrence.ordinal} ${container?.type==='iteration'?'项':'轮'}`
          :depth===occurrences.length-1&&step.scope?step.scope:`循环 · 第 ${occurrence.ordinal} 轮`
        group={kind:'round',key:occurrence.key,label,children:[],steps:[]}
        groups.set(occurrence.key,group)
        children.push(group)
      }
      group.steps.push(step)
      children=group.children
    })
    children.push({kind:'step',step,index:offset+index})
  })
  return entries
}

function Step({step,projectId,applicationId,index,grouped=false}:{step:RunStep;projectId:string;applicationId:string;index:number;grouped?:boolean}) {
  const [expanded,setExpanded]=useState<boolean|null>(null)
  const needsAttention=attentionStatuses.includes(step.status)
  const show=expanded??(needsAttention&&!['iteration','loop'].includes(step.type))
  const path=Array.isArray(step.node_path)?step.node_path:[]
  return <li className={styles.step} data-status={step.status}>
    <div className={styles.number} aria-hidden="true">{index+1}</div>
    <div className={styles.content}>
      <div className={styles.heading}><strong>{step.title||step.id}</strong><span className={styles.badge}>{statuses[step.status]||step.status}</span>{step.duration_ms!=null&&<small>用时 {(step.duration_ms/1000).toLocaleString(undefined,{maximumFractionDigits:2})} 秒</small>}</div>
      {step.scope&&!grouped&&<small>{step.scope}</small>}
      {step.description&&<p>{step.description}</p>}
      {step.error&&<TaskError error={step.error}/>}
      <div className={styles.actions}><button aria-expanded={show} onClick={()=>setExpanded(!show)}>{show?'收起本步输入与产物':'查看本步输入与产物'}</button>{path.length>0&&applicationId&&<Link href={`/applications/${encodeURIComponent(applicationId)}?tab=edit&node_path=${encodeURIComponent(JSON.stringify(path))}`} target="_blank" rel="noreferrer">在当前画布定位 ↗</Link>}</div>
      {show&&<div className={styles.previews}>
        <section aria-label={`${step.title}的输入`}><h4>业务输入</h4><small>{step.input_source||'本次运行记录'}</small><Preview value={step.input_summary??step.input_preview} projectId={projectId}/></section>
        <section aria-label={`${step.title}的产物`}><h4>处理结果与依据</h4>{step.output_preview==null?<p className={styles.muted}>{step.status==='pending'?'尚未产生输出':step.status==='skipped'?'未进入此分支':'没有保存的输出'}</p>:<><Preview value={step.output_summary??step.output_preview} projectId={projectId}/><small>摘自本步保存的结果；较长内容仅展示节选，完整内容见结果文件。</small></>}</section>
        <details className={styles.technical}><summary>技术详情：输入、输出与校验信息</summary><h4>输入记录</h4><Preview value={step.input_preview} projectId={projectId}/><h4>输出记录</h4><Preview value={step.output_preview} projectId={projectId}/></details>
      </div>}
    </div>
  </li>
}

function Round({group,projectId,applicationId,paged}:{group:RoundEntry;projectId:string;applicationId:string;paged:boolean}) {
  const [expanded,setExpanded]=useState<{value:boolean;attention:string[]}|null>(null)
  const counts=new Map<string,number>()
  group.steps.forEach(step=>counts.set(step.status,(counts.get(step.status)||0)+1))
  const attention=group.steps.filter(step=>attentionStatuses.includes(step.status)).map(step=>JSON.stringify([step.id,step.status]))
  // A newly failing/waiting step becomes visible even if this round was closed
  // before the next poll. Otherwise retain the user's open/closed preference.
  const hasNewAttention=attention.some(item=>!expanded?.attention.includes(item))
  const show=hasNewAttention?true:expanded?.value??attention.length>0
  const status=attentionStatuses.find(status=>counts.has(status))
  return <li className={styles.round} data-status={status}>
    <button className={styles.roundToggle} aria-expanded={show} onClick={()=>setExpanded({value:!show,attention})}>
      <span className={styles.roundTitle}><span aria-hidden="true">{show?'▾':'▸'}</span><strong>{group.label}</strong></span>
      <span className={styles.roundSummary}><span>{paged?'本页 ':''}{group.steps.length} 个步骤</span>{[...counts].map(([status,count])=><span key={status} className={styles.badge} data-status={status}>{statuses[status]||status} {count}</span>)}<span className={styles.roundAction}>{show?'收起步骤':'展开步骤'}</span></span>
    </button>
    {show&&<ol className={styles.roundSteps} aria-label={`${group.label}的步骤`}><StepEntries entries={group.children} projectId={projectId} applicationId={applicationId} paged={paged} grouped/></ol>}
  </li>
}

function StepEntries({entries,projectId,applicationId,paged,grouped=false}:{entries:StepGroupEntry[];projectId:string;applicationId:string;paged:boolean;grouped?:boolean}) {
  return <>{entries.map(entry=>entry.kind==='round'
    ?<Round key={entry.key} group={entry} projectId={projectId} applicationId={applicationId} paged={paged}/>
    :<Step key={entry.step.id} step={entry.step} projectId={projectId} applicationId={applicationId} index={entry.index} grouped={grouped}/>)}</>
}

function Run({projectId,run}:{projectId:string;run:NonNullable<ProjectTask['runs']>[number]}) {
  const [page,setPage]=useState<StepPage|null>(null),[offset,setOffset]=useState(0),[error,setError]=useState(''),[retry,setRetry]=useState(0)
  useEffect(()=>{
    let alive=true,timer:ReturnType<typeof setTimeout>|undefined
    setPage(null);setError('')
    async function refresh(){
      try {
        const next=await api<StepPage>(`/api/v1/runs/${encodeURIComponent(run.id)}/steps?offset=${offset}&limit=100`)
        if(!alive)return
        if(!next || !Array.isArray(next.steps))throw new Error('未收到步骤记录')
        setPage(next);setError('')
        if(['queued','running','cancelling'].includes(next.status))timer=setTimeout(refresh,2000)
      } catch {
        if(alive)setError('处理步骤暂时无法读取，运行结果仍保留。')
      }
    }
    void refresh()
    return()=>{alive=false;if(timer)clearTimeout(timer)}
  },[run.id,run.status,offset,retry])
  return <section className={styles.run} aria-label="工作流处理步骤">
    <h4>{page?.name||'本次工作流'}{(page?.draft_revision??run.draft_revision)!=null&&<small> · 保存版本 {page?.draft_revision??run.draft_revision}</small>}</h4>
    {error&&<p role="status">{error} <button onClick={()=>setRetry(value=>value+1)}>重新读取步骤</button></p>}
    {!page&&!error&&<p role="status">正在读取处理步骤…</p>}
    {page&&<><p className={styles.muted}>共 {page.total} 个步骤记录。标题、输入和产物来自本次运行；“在当前画布定位”打开可编辑的当前草稿，可能与本次记录不同。</p>
      {page.status==='paused'&&page.steps.some(step=>step.type==='human_input'&&step.status==='waiting')&&page.steps.some(step=>step.status==='interrupted')&&<p className={styles.muted}>正在等待补充信息；部分并行步骤会在继续后重新执行。“已中断”不一定表示整个流程失败。</p>}
      {page.error&&!page.steps.some(step=>step.error)&&<TaskError error={page.error}/>}
      {page.steps.length?<ol className={styles.steps}><StepEntries entries={groupSteps(page.steps,offset)} projectId={projectId} applicationId={page.application_id} paged={offset>0||page.next_offset!=null}/></ol>:<p>尚无步骤记录。</p>}
      {(offset>0 || page.next_offset!=null)&&<nav className={styles.actions} aria-label="步骤分页"><button disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-100))}>前100步</button><span>第 {offset+1}–{offset+page.steps.length} 步</span><button disabled={page.next_offset==null} onClick={()=>setOffset(page.next_offset!)}>后100步</button></nav>}
    </>}
  </section>
}

export default function ProjectRunSteps({projectId,runs}:{projectId:string;runs:ProjectTask['runs']}) {
  const available=runs?.filter(run=>run.id)
  if(!available?.length)return null
  return <section className={styles.panel} aria-label="处理步骤"><h3>处理步骤</h3>{available.map(run=><Run key={run.id} projectId={projectId} run={run}/>)}</section>
}
