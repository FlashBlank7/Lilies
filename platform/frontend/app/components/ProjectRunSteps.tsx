'use client'

import {useEffect, useState} from 'react'
import Link from 'next/link'
import {api} from '@/lib/platform'
import {resolveProjectLink} from '@/lib/project-links'
import type {ProjectTask} from '@/lib/project-progress'
import TaskError from './TaskError'
import styles from './project-run-steps.module.css'

export type RunStep = {
  id:string; node_path:string[]; title:string; description:string; type:string; status:string
  scope?:string; input_source?:string; input_preview:unknown; output_preview:unknown
  error?:string|null; duration_ms?:number|null
}
type StepPage = {run_id:string;application_id:string;name:string;status:string;steps:RunStep[];total:number;next_offset?:number|null;error?:string;draft_revision?:number|null}
const statuses:Record<string,string> = {pending:'尚未执行',running:'正在处理',completed:'已完成',reused:'复用已有结果',skipped:'此分支未执行',waiting:'等待补充',failed:'失败',interrupted:'已中断',warning:'完成，需留意异常'}
const labels:Record<string,string> = {source_path:'资料文件',second_path:'第二份资料',output:'处理结果',outputs:'输出',result:'结果',logs:'执行说明',markdown:'报告正文',rows:'记录数',columns:'字段',preview:'数据预览',artifacts:'结果文件',file_path:'文件',path:'路径',summary:'汇总',message:'说明',items:'条目',count:'数量',error:'错误',value:'数值',inputs:'输入',group_by:'汇总维度',mark_duplicates:'标记疑似重复',suspected_duplicates:'疑似重复数',duplicate_records:'重复记录',missing:'缺失',operation:'处理方式',horizon:'预测步数',omitted_items:'未展示条目数',omitted_fields:'未展示字段'}
const operations:Record<string,string> = {expenses:'费用整理',profile:'数据体检',summary:'数据汇总',join:'多表关联'}

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
  return <dl className={styles.fields}>{entries.slice(0,12).map(([key,item])=><div key={key}><dt>{labels[key]||key}</dt><dd><Preview value={key==='operation'&&typeof item==='string'?(operations[item]||item):item} projectId={projectId} depth={depth+1}/></dd></div>)}{entries.length>12&&<div><dt>更多内容</dt><dd>另有 {entries.length-12} 个字段，见原始输入输出。</dd></div>}</dl>
}

function Step({step,projectId,applicationId,index}:{step:RunStep;projectId:string;applicationId:string;index:number}) {
  const [expanded,setExpanded]=useState<boolean|null>(null)
  const needsAttention=['failed','waiting','warning','running'].includes(step.status)
  const show=expanded??needsAttention
  const path=Array.isArray(step.node_path)?step.node_path:[]
  return <li className={styles.step} data-status={step.status}>
    <div className={styles.number} aria-hidden="true">{index+1}</div>
    <div className={styles.content}>
      <div className={styles.heading}><strong>{step.title||step.id}</strong><span className={styles.badge}>{statuses[step.status]||step.status}</span>{step.duration_ms!=null&&<small>用时 {(step.duration_ms/1000).toLocaleString(undefined,{maximumFractionDigits:2})} 秒</small>}</div>
      {step.scope&&<small>{step.scope}</small>}
      {step.description&&<p>{step.description}</p>}
      {step.error&&<TaskError error={step.error}/>}
      <div className={styles.actions}><button aria-expanded={show} onClick={()=>setExpanded(!show)}>{show?'收起本步输入与产物':'查看本步输入与产物'}</button>{path.length>0&&applicationId&&<Link href={`/applications/${encodeURIComponent(applicationId)}?tab=edit&node_path=${encodeURIComponent(JSON.stringify(path))}`} target="_blank" rel="noreferrer">在当前画布定位 ↗</Link>}</div>
      {show&&<div className={styles.previews}>
        <section aria-label={`${step.title}的输入`}><h4>输入</h4><small>{step.input_source||'本次运行记录'}</small><Preview value={step.input_preview} projectId={projectId}/></section>
        <section aria-label={`${step.title}的产物`}><h4>产物</h4>{step.output_preview==null?<p className={styles.muted}>{step.status==='pending'?'尚未产生输出':step.status==='skipped'?'未进入此分支':'没有保存的输出'}</p>:<Preview value={step.output_preview} projectId={projectId}/>}</section>
      </div>}
    </div>
  </li>
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
      {page.error&&!page.steps.some(step=>step.error)&&<TaskError error={page.error}/>}
      {page.steps.length?<ol className={styles.steps}>{page.steps.map((step,i)=><Step key={run.id+step.id} step={step} projectId={projectId} applicationId={page.application_id} index={offset+i}/>)}</ol>:<p>尚无步骤记录。</p>}
      {(offset>0 || page.next_offset!=null)&&<nav className={styles.actions} aria-label="步骤分页"><button disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-100))}>前100步</button><span>第 {offset+1}–{offset+page.steps.length} 步</span><button disabled={page.next_offset==null} onClick={()=>setOffset(page.next_offset!)}>后100步</button></nav>}
    </>}
  </section>
}

export default function ProjectRunSteps({projectId,runs}:{projectId:string;runs:ProjectTask['runs']}) {
  const available=runs?.filter(run=>run.id)
  if(!available?.length)return null
  return <section className={styles.panel} aria-label="处理步骤"><h3>处理步骤</h3>{available.map(run=><Run key={run.id} projectId={projectId} run={run}/>)}</section>
}
