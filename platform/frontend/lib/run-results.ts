import {withFrontendToken} from './platform'
import {taskArtifacts} from './task-artifacts'
import type {ProjectTask} from './project-progress'

export type DeliveryFile={path:string;run_id:string;status:string;sha256?:string;size?:number;error?:string;reason?:string;label?:string}
export type ResultRun={id:string;name?:string;application_id?:string;status:string;outputs?:Record<string,unknown>;delivery_files?:DeliveryFile[]}
export type ResultTask={id:string;status:string;outputs?:Record<string,unknown>;presentation?:{markdown?:string;message?:string;artifacts?:{file_path:string;label:string}[]};runs?:ResultRun[]}
export type ResultSource={label:string;runId?:string;stage:boolean}
export type Report={text:string;title:string;sources:ResultSource[]}
export type ResultView={label:string;value:unknown;source:ResultSource}
export function isResultPath(path:string){
  try{return decodeURIComponent(path)===path&&/^(results|solution)\//.test(path)&&!/[\\\u0000-\u001f]/.test(path)&&!path.split('/').some(p=>!p||p==='.'||p==='..')}catch{return false}
}
export function deliveryUrl(projectId:string,taskId:string,file:DeliveryFile){
  if(file.status!=='saved'||!file.sha256||!/^[a-f0-9]{64}$/.test(file.sha256)||![projectId,taskId,file.run_id].every(id=>/^[\w-]+$/.test(id)))return ''
  return withFrontendToken(`/api/platform/api/v1/projects/${projectId}/tasks/${taskId}/deliveries/${file.run_id}/${file.sha256}?name=${encodeURIComponent(file.path.split('/').pop()||'result')}`)
}
const fileKeys=new Set(['report','report_html','results_excel','results_csv','model_package','manifest','file_path','file','artifact'])
const labels:Record<string,string>={summary:'结果概览',counts:'分类统计',limits:'适用范围',findings:'发现的问题',warnings:'需要留意',rows:'记录',items:'条目',result:'结果',report:'报告',markdown:'报告正文'}
export const resultLabel=(key:string)=>labels[key]||key

function distinguishReportVersions(reports:Report[],files:DeliveryFile[]):Report[]{
  return reports.flatMap(report=>{
    const candidates=files.filter(file=>report.sources.some(source=>source.runId===file.run_id))
    const versions=new Map<string,Set<string>>()
    for(const file of candidates){
      const set=versions.get(file.path)||new Set<string>()
      set.add(file.status+':'+(file.sha256||''));versions.set(file.path,set)
    }
    if(!Array.from(versions.values()).some(set=>set.size>1))return [report]
    // Identical prose may link to different files. Keep those reports separately readable.
    const groups=new Map<string,ResultSource[]>()
    for(const source of report.sources){
      const identity=JSON.stringify(candidates.filter(f=>f.run_id===source.runId).map(f=>[f.path,f.status,f.sha256||'']).sort())
      const sources=groups.get(identity)||[];sources.push(source);groups.set(identity,sources)
    }
    const variants=Array.from(groups.values()).map(sources=>({...report,sources,title:report.title+' · '+sources.map(s=>s.label).join(' / ')}))
    return variants.map((variant,index)=>({...variant,title:variant.title+(variants.some((other,j)=>j!==index&&other.title===variant.title)?` · 来源 ${index+1}`:'')}))
  })
}

/** Presentation only: never change the output used by downstream workflows. */
export function collectRunResults(task:ResultTask){
  const reports:Report[]=[],views:ResultView[]=[],filePaths=new Set<string>(),fileLabels=new Map<string,string>(),reportMap=new Map<string,Report>(),viewKeys=new Set<string>()
  const artifactLabels=new Map(taskArtifacts(task as ProjectTask).map(a=>[a.file_path,a.label]))
  const named:Record<string,string>={report:'完整报告',report_html:'报告 HTML 文件',results_excel:'完整结果 Excel',results_csv:'完整结果 CSV',model_package:'模型包',manifest:'本次文件清单'}
  let budget=600,truncated=false
  function walk(value:unknown,source:ResultSource,path:string[],depth=0){
    if(depth>12||budget--<=0){truncated=true;return}
    if(!value||typeof value!=='object'||Array.isArray(value))return
    const object=value as Record<string,unknown>
    for(const [key,child] of Object.entries(object)){
      const reportText=typeof child==='string'&&(key==='markdown'||key==='report'&&!/^(\w+:|results\/|solution\/)|\.(md|html?|pdf|txt)$/i.test(child.trim()))?child.trim():''
      if(reportText){
        const existing=reportMap.get(reportText)
        const origin={...source,label:[source.label,...path].join(' / ')}
        if(existing){if(!existing.sources.some(s=>s.label===origin.label&&s.runId===origin.runId))existing.sources.push(origin)}
        else {const report={text:reportText,title:reportText.match(/^#{1,6}\s+(.+)$/m)?.[1]||'运行报告',sources:[origin]};reports.push(report);reportMap.set(reportText,report)}
        continue
      }
      if(fileKeys.has(key)&&typeof child==='string'&&isResultPath(child)){filePaths.add(child);if(typeof object.label==='string')fileLabels.set(child,object.label);else if(named[key])fileLabels.set(child,'下载'+named[key]);continue}
      if(key==='artifacts'&&Array.isArray(child)){
        child.slice(0,200).forEach(entry=>walk(entry,source,[...path,key],depth+1));continue
      }
      const resultObject=child&&typeof child==='object'&&!Array.isArray(child)&&('markdown' in child||'artifacts' in child)
      if(!resultObject&&['summary','counts','limits','findings','warnings'].includes(key)||Array.isArray(child)&&child.length&&child.slice(0,5).every(r=>r&&typeof r==='object'&&!Array.isArray(r))){
        // Do not merge large tables based only on an identical preview.
        const origin=Array.isArray(child)&&child.length>50?JSON.stringify([source.runId,source.label,path]):''
        const signature=origin+key+JSON.stringify(Array.isArray(child)?[child.length,child.slice(0,50)]:child,(_key,value)=>value&&typeof value==='object'&&!Array.isArray(value)?Object.fromEntries(Object.entries(value).sort(([a],[b])=>a.localeCompare(b))):value)
        if(!viewKeys.has(signature)){views.push({label:[...path,resultLabel(key)].join(' / '),value:child,source});viewKeys.add(signature)}
        continue
      }
      if(child&&typeof child==='object'&&!Array.isArray(child))walk(child,source,[...path,key],depth+1)
    }
  }
  walk(task.outputs||{},{label:'本次输出',stage:task.status!=='succeeded'},[])
  const explanation=task.presentation?.markdown||task.presentation?.message||(typeof task.outputs?.message==='string'?task.outputs.message:'')
  if(explanation&&!reports.length)walk({markdown:explanation},{label:'本次任务说明',stage:task.status!=='succeeded'},[])
  for(const run of task.runs||[])walk(run.outputs||{},{label:run.name||run.application_id||'成员流程',runId:run.id,stage:true},[])
  walk({artifacts:task.presentation?.artifacts||[]},{label:'本次任务文件',stage:task.status!=='succeeded'},[])
  const files=(task.runs||[]).flatMap(run=>(run.delivery_files||[]).map(file=>({...file,source:run.name||run.application_id||run.id})))
  for(const path of filePaths)if(!files.some(f=>f.path===path))files.push({path,run_id:'',status:'unverified',source:'本次输出',error:'此历史运行未保存文件副本，无法确认当前文件是否为当时版本'})
  const uniqueFiles=files.filter((file,i)=>!files.slice(0,i).some(f=>f.path===file.path&&f.sha256===file.sha256&&f.status===file.status))
  return {reports:distinguishReportVersions(reports,files),views:views.slice(0,30),allFiles:files,files:uniqueFiles.filter(file=>!task.presentation?.artifacts?.length||artifactLabels.has(file.path)).map(file=>({...file,source:Array.from(new Set(files.filter(f=>f.path===file.path&&f.sha256===file.sha256&&f.status===file.status).map(f=>f.source))).join(' / '),label:artifactLabels.get(file.path)||fileLabels.get(file.path)})),truncated:truncated||views.length>30}
}
