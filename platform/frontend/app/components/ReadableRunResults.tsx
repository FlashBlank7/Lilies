'use client'
import {useMemo,useState} from 'react'
import {MarkdownDocument} from '@/lib/markdown'
import {collectRunResults,deliveryUrl,isResultPath,resultLabel,type DeliveryFile,type ResultTask} from '@/lib/run-results'
import {resolveProjectLink,resolveProjectFileLink,projectFilePathFromLink} from '@/lib/project-links'
import ReadingDialog from './ReadingDialog'
import ProjectFileReader from './ProjectFileReader'
import styles from './readable-run-results.module.css'

export function ResultValue({value,depth=0}:{value:unknown;depth?:number}){
  if(value===null)return <span>空值</span>
  if(value===undefined)return <span>未提供</span>
  if(typeof value!=='object')return <span className={styles.text}>{typeof value==='boolean'?(value?'是':'否'):String(value)||'（空文本）'}</span>
  if(depth>3)return <span>更多内容见原始结果</span>
  if(Array.isArray(value)){
    if(!value.length)return <span>无记录</span>
    const table=value.slice(0,50).every(r=>r&&typeof r==='object'&&!Array.isArray(r))
    if(table){const columns=Array.from(new Set(value.slice(0,50).flatMap(r=>Object.keys(r)))).slice(0,12)
      return <><p className={styles.muted}>共 {value.length} 条，预览前 {Math.min(50,value.length)} 条、{columns.length} 列；完整字段见下载或原始结果。</p><div className={styles.scroll} tabIndex={0} role="region" aria-label="结果表格"><table><thead><tr>{columns.map(c=><th key={c}>{resultLabel(c)}</th>)}</tr></thead><tbody>{value.slice(0,50).map((r,i)=><tr key={i}>{columns.map(c=><td key={c}><ResultValue value={r[c]} depth={depth+1}/></td>)}</tr>)}</tbody></table></div></>}
    return <><ul>{value.slice(0,20).map((r,i)=><li key={i}><ResultValue value={r} depth={depth+1}/></li>)}</ul>{value.length>20&&<p>仅展示前 20 项，共 {value.length} 项。</p>}</>
  }
  const entries=Object.entries(value)
  return <dl className={styles.fields}>{entries.slice(0,20).map(([key,v])=><div key={key}><dt>{resultLabel(key)}</dt><dd>{v&&typeof v==='object'?<details><summary>展开内容</summary><ResultValue value={v} depth={depth+1}/></details>:<ResultValue value={v} depth={depth+1}/>}</dd></div>)}{entries.length>20&&<p>仅展示前 20 个字段。</p>}</dl>
}

export function RawResult({value,label='原始结果 JSON'}:{value:unknown;label?:string}){
  const [open,setOpen]=useState(false)
  return <details onToggle={e=>setOpen(e.currentTarget.open)}><summary>{label}</summary>{open&&<><a download="result.json" href={'data:application/json;charset=utf-8,'+encodeURIComponent(JSON.stringify(value,null,2))}>下载完整 JSON</a><pre className={styles.raw}>{JSON.stringify(value,null,2).slice(0,20000)}</pre><small>页面最多展示 20,000 字符，下载保留完整内容。</small></>}</details>
}

export default function ReadableRunResults({projectId,task,showViews=true,hideInline=false}:{projectId:string;task:ResultTask;showViews?:boolean;hideInline?:boolean}){
  const result=useMemo(()=>collectRunResults(task),[task])
  const [reading,setReading]=useState<number|null>(null)
  const [file,setFile]=useState<DeliveryFile|null>(null)
  function reportLink(href:string,runIds?:string[],sourcePath?:string){
    const resolved=sourcePath?resolveProjectFileLink(projectId,sourcePath,href):resolveProjectLink(projectId,href)
    const path=projectFilePathFromLink(projectId,resolved)
    if(path&&isResultPath(path)){
      const matches=result.allFiles.filter(f=>f.path===path&&f.status==='saved'&&(!runIds?.length||runIds.includes(f.run_id)))
      return matches.length&&new Set(matches.map(f=>f.sha256)).size===1?deliveryUrl(projectId,task.id,matches[0]):''
    }
    return resolved
  }
  const overview=result.views.filter(v=>/(概览|统计|适用范围|发现的问题|需要留意)$/.test(v.label))
  const details=result.views.filter(v=>!overview.includes(v))
  const plain=Object.fromEntries(Object.entries(task.outputs||{}).filter(([k,v])=>k!=='markdown'&&k!=='report'&&(v===null||['string','number','boolean'].includes(typeof v))))
  return <section className={styles.root} aria-label="可读运行结果">
    {showViews&&<section aria-label="结果概览"><h3>结果概览</h3>{task.status!=='succeeded'&&<p className={styles.notice}>本次任务尚未执行成功。下方仅展示已保存的阶段产物。</p>}{overview.length?overview.map((v,i)=><div key={i}><h4>{v.label}</h4><ResultValue value={v.value}/></div>):Object.keys(plain).length?<ResultValue value={plain}/>:<p className={styles.muted}>本次没有单独的汇总字段，请查看报告或结果明细。</p>}</section>}
    {!hideInline&&<section aria-label="运行报告"><h3>运行报告</h3>{!result.reports.length&&!result.files.some(f=>/\.(md|html?|pdf)$/i.test(f.path))&&<p>本次未生成报告。</p>}{result.reports.map((report,i)=><article className={styles.report} key={i}><h4>{report.title}</h4><p className={styles.muted}>{report.sources.every(s=>s.stage)?'阶段产物':'本次运行报告'}</p><details><summary>查看报告来源 · {report.sources.length} 处引用</summary><ul>{report.sources.map((source,j)=><li key={j}>{source.label}</li>)}</ul></details><div className={styles.actions}><button onClick={()=>setReading(i)}>查看完整报告{result.reports.length>1?' · '+report.title:''}</button><a download={`report-${task.id}-${i+1}.md`} href={'data:text/markdown;charset=utf-8,'+encodeURIComponent(report.text)}>下载报告</a></div>{result.reports.length===1&&<div className={styles.preview}><MarkdownDocument source={report.text.slice(0,3500)} emptyLabel="" resolveLink={href=>reportLink(href,report.sources.flatMap(s=>s.runId?[s.runId]:[]))}/>{report.text.length>3500&&<p>这里是报告节选，点击“查看完整报告”阅读全部内容。</p>}</div>}</article>)}</section>}
    {!!result.files.length&&<section aria-label="本次交付文件"><h3>本次交付文件</h3>{result.files.map((entry,i)=>{const url=deliveryUrl(projectId,task.id,entry);return <article className={styles.report} key={i}><strong>{entry.label||entry.path.split('/').pop()}</strong><p className={styles.muted}>{entry.source} · {entry.status==='saved'?'已保存本次运行副本':'版本未确认'}</p>{url?<div className={styles.actions}>{/\.(md|txt|csv|json|html?)$/i.test(entry.path)&&<button onClick={()=>setFile(entry)}>查看{ /\.(md|html?)$/i.test(entry.path)?'完整报告':'结果文件'}</button>}<a href={url} download>{entry.label?entry.label+' ↓':'下载 '+entry.path.split('/').pop()}</a></div>:<><p className={styles.notice}>{entry.error||'本次文件不可用'}</p>{entry.status==='unverified'&&<div className={styles.actions}><small>以下入口读取当前项目文件，并非已确认的历史原件。</small>{/\.(md|txt|csv|json|html?)$/i.test(entry.path)&&<button onClick={()=>setFile(entry)}>查看当前文件（版本未确认）</button>}<a download href={resolveProjectLink(projectId,entry.path)+'?download=1'}>{entry.label?entry.label+' ↓':'下载当前文件 '+entry.path.split('/').pop()}</a></div>}</>}</article>})}</section>}
    {showViews&&!!details.length&&<section><h3>结果明细</h3>{details.map((v,i)=><details key={i}><summary>{v.label} · {v.source.label}</summary><ResultValue value={v.value}/></details>)}</section>}
    {result.truncated&&<p>部分深层或较大内容未展开，完整内容保留在原始结果中。</p>}
    {reading!==null&&result.reports[reading]&&<ReadingDialog wide title={result.reports[reading].title} onClose={()=>setReading(null)}><p>任务 {task.id} · {task.status==='succeeded'?'执行成功':'阶段产物，整次任务未完成'}</p><MarkdownDocument source={result.reports[reading].text} emptyLabel="" resolveLink={href=>reportLink(href,result.reports[reading].sources.flatMap(s=>s.runId?[s.runId]:[]))}/></ReadingDialog>}
    {file&&<ProjectFileReader projectId={projectId} path={file.path} versionNotice={file.status!=='saved'?'这是当前项目文件，无法确认是否为所选运行的历史版本。':undefined} snapshot={file.status==='saved'?{taskId:task.id,file}:undefined} resolveLink={href=>reportLink(href,[file.run_id],file.path)} onClose={()=>setFile(null)}/>}
  </section>
}
