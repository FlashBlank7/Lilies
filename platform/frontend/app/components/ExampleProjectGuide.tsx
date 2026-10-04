'use client'

import {useEffect,useState} from 'react'
import {api} from '@/lib/platform'
import {MarkdownDocument} from '@/lib/markdown'
import type {ExampleProject} from './ExampleProjects'
import styles from './example-projects.module.css'
import WorkflowReadiness from './WorkflowReadiness'
import ReadingDialog from './ReadingDialog'

export default function ExampleProjectGuide({projectId,onTalk,onFile,onWorkflow,onRun,onSettings}:{projectId:string;onTalk:(message:string)=>void;onFile:(path:string)=>void;onWorkflow:(id:string)=>void;onRun:(id:string)=>void;onSettings:()=>void}){
  const [guide,setGuide]=useState<ExampleProject|null>(null),[error,setError]=useState(''),[reading,setReading]=useState(false)
  useEffect(()=>{let alive=true;setGuide(null);setError('');setReading(false);api<ExampleProject|null>(`/api/v1/projects/${projectId}/example`).then(value=>{if(alive)setGuide(value)}).catch(()=>{if(alive)setError('示例使用说明暂未读取，请刷新页面重试。')});return()=>{alive=false}},[projectId])
  if(!guide)return error?<p role="status">{error}</p>:null
  const question=guide.question+'\n\n请先查看项目 Skill「'+guide.name+'使用说明」及已有工作流，使用项目中的示例资料。'+(guide.field_notes?.trim()?'\n\n本示例的资料字段说明：\n'+guide.field_notes:'')
  const currentManual=guide.current_manual?.trim()
  return <section className={styles.guide} aria-label="示例项目使用说明">
    <h2>{guide.name} · 从这里开始</h2><p>{guide.question}</p>
    <WorkflowReadiness value={guide.readiness} projectId={projectId} onSettings={onSettings}/>
    <div className={styles.actions}><button onClick={()=>onTalk(question)}>准备这条消息</button>{currentManual&&<button onClick={()=>setReading(true)}>阅读完整使用说明</button>}{guide.manual_path&&<button onClick={()=>onFile(guide.manual_path!)}>{currentManual?'项目保存的说明':'阅读完整使用说明'}</button>}</div>
    <small>消息只填入输入框，由你检查后发送；已有草稿会保留。示例资料为自编或合成。</small>
    <details><summary>操作步骤、工作流与修改练习</summary><p>准备条件：{guide.requires.join(' · ')}。对话需要项目智能体连接。<button onClick={onSettings}>查看项目设置</button></p>
      <ol>{guide.steps.map(s=><li key={s}>{s}</li>)}</ol>
      {guide.workflows?.map(flow=><div className={styles.actions} key={flow.id}><strong>{flow.name}</strong><button onClick={()=>onWorkflow(flow.id!)}>查看与编辑</button><button onClick={()=>onRun(flow.id!)}>填写运行参数</button></div>)}
      <p>修改练习：{guide.exercise}</p>
      <div className={styles.actions}>{guide.files.map(file=><button key={file.path} onClick={()=>onFile(file.path!)}>{file.name}</button>)}</div>
    </details>
    {reading&&currentManual&&<ReadingDialog wide title={guide.name+' · 完整使用说明'} onClose={()=>setReading(false)}><MarkdownDocument source={currentManual} emptyLabel="使用说明暂无内容"/></ReadingDialog>}
  </section>
}
