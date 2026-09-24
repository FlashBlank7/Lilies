'use client'
import {useEffect,useRef,useState} from 'react'
import {api} from '@/lib/platform'
import {MarkdownDocument} from '@/lib/markdown'
import {resolveProjectLink} from '@/lib/project-links'
import {taskNames,type ProjectTask} from '@/lib/project-progress'
import styles from './workspace-tools.module.css'

type Form={node_id:string;title:string;description?:string;context?:unknown;fields:{name:string;label:string;type:string;required?:boolean;options?:string[]}[]}
function AnswerForm({projectId,taskId,runId,form,onSubmitted}:{projectId:string;taskId:string;runId:string;form:Form;onSubmitted:(task:ProjectTask)=>void}){
  const [values,setValues]=useState<Record<string,string>>({}),[busy,setBusy]=useState(false),[error,setError]=useState('')
  const lock=useRef(false)
  async function submit(event:React.FormEvent){
    event.preventDefault();if(lock.current)return
    lock.current=true;setBusy(true);setError('')
    try{
      const answer:Record<string,unknown>={}
      for(const field of form.fields){
        const value=values[field.name]??''
        if(!value.trim()){if(field.required)throw Error('请填写：'+field.label);continue}
        if(field.type==='number'){if(!Number.isFinite(Number(value)))throw Error(field.label+'需要有效数字');answer[field.name]=Number(value)}
        else if(field.type==='boolean')answer[field.name]=value==='true'
        else if(['object','array','any','file_list'].includes(field.type)){try{answer[field.name]=JSON.parse(value)}catch{throw Error(field.label+'需要有效JSON')}}
        else answer[field.name]=value
      }
      const next=await api<ProjectTask>(`/api/v1/projects/${projectId}/tasks/${taskId}/runs/${runId}/input`,{method:'POST',body:JSON.stringify({node_id:form.node_id,values:answer,resume:true})})
      onSubmitted(next)
    }catch(e){setError(String(e))}finally{lock.current=false;setBusy(false)}
  }
  return <form className={styles.section} onSubmit={submit} aria-label="补充工作流信息">
    <h3>{form.title||'需要你的补充'}</h3>{form.description&&<p>{form.description}</p>}
    {form.context!=null&&<MarkdownDocument source={typeof form.context==='string'?form.context:JSON.stringify(form.context,null,2)} resolveLink={href=>resolveProjectLink(projectId,href)} emptyLabel=""/>}
    {form.fields.map(field=><label key={field.name}>{field.label}{field.required?' *':''}
      {field.options?.length||field.type==='boolean'?<select aria-label={field.label} required={field.required} disabled={busy} value={values[field.name]||''} onChange={e=>setValues(v=>({...v,[field.name]:e.target.value}))}>
        <option value="">请选择</option>{field.type==='boolean'?<><option value="true">是</option><option value="false">否</option></>:field.options?.map(v=><option key={v} value={v}>{v}</option>)}
      </select>:field.type==='number'?<input aria-label={field.label} type="number" step="any" required={field.required} disabled={busy} value={values[field.name]||''} onChange={e=>setValues(v=>({...v,[field.name]:e.target.value}))}/>
      :<textarea aria-label={field.label} required={field.required} disabled={busy} rows={3} value={values[field.name]||''} onChange={e=>setValues(v=>({...v,[field.name]:e.target.value}))}/>}
    </label>)}
    <button type="submit" disabled={busy}>{busy?'正在提交…':'提交并继续这次运行'}</button>
    <small>回答保存在本次运行中，已完成的步骤不会从头执行。</small>{error&&<p role="alert">{error}</p>}
  </form>
}

export default function ProjectTaskInput({projectId,taskId,initialTask,onTask}:{projectId:string;taskId:string;initialTask?:ProjectTask;onTask?:(task:ProjectTask)=>void}){
  const [task,setTask]=useState(initialTask),[error,setError]=useState(''),[reload,setReload]=useState(0)
  const callback=useRef(onTask);callback.current=onTask
  useEffect(()=>{
    let alive=true,timer:ReturnType<typeof setTimeout>
    async function refresh(){try{
      const next=await api<ProjectTask>(`/api/v1/projects/${projectId}/tasks/${taskId}`)
      if(!alive)return;setTask(next);setError('');callback.current?.(next)
      if(['running','queued','waiting_input'].includes(next.status))timer=setTimeout(()=>void refresh(),2000)
    }catch(e){if(alive)setError(String(e))}}
    void refresh();return()=>{alive=false;clearTimeout(timer)}
  },[projectId,taskId,reload])
  const waiting=task?.status==='waiting_input'?task.runs?.filter(r=>r.status==='paused'&&r.waiting_input):[]
  return <>{waiting?.map(run=><AnswerForm key={run.id+run.waiting_input!.node_id} projectId={projectId} taskId={taskId} runId={run.id} form={run.waiting_input!} onSubmitted={next=>{setTask(next);callback.current?.(next);setReload(v=>v+1)}}/>)}
    {task&&['running','queued'].includes(task.status)&&<p role="status">{taskNames[task.status]}，已保存的结果仍可查看。</p>}
    {error&&<p role="alert">暂时无法读取待补信息。<button onClick={()=>setReload(v=>v+1)}>重试</button></p>}
  </>
}
