'use client'
import {useEffect,useRef,useState} from 'react'
import {api} from './platform'
import {clientId} from './client-id'
import {useAccount} from '@/app/components/AuthBoundary'
type Job<T>={job_id:string;status:string;error?:string;result?:T}
type Pending={job_id?:string;submitted:string;request_key:string;signature:string;endpoint?:string;project_id?:string;started_at?:number;error?:string}
const changedEvent='lilies:pending-generation'
export function useWorkflowGeneration<T>(projectId:string, storageKey:string,onResult:(result:T,submitted:string)=>void){
  const account=useAccount()
  const legacyKey=`${account?.id||'local'}:${storageKey}:pending-generation`
  const key=`${account?.id||'local'}:project:${projectId}:${storageKey}:pending-generation`
  const [busy,setBusy]=useState(false),[status,setStatus]=useState(''),[error,setError]=useState(''),[jobId,setJobId]=useState('')
  const [startedAt,setStartedAt]=useState<number>(),[now,setNow]=useState(Date.now())
  const live=useRef(true),epoch=useRef(0),locked=useRef(false),submitting=useRef(false),polling=useRef(''),sharedAvailable=useRef(true),callback=useRef(onResult),currentKey=useRef(key),observed=useRef<Pending|null>(null)
  callback.current=onResult;currentKey.current=key
  function read():Pending|null{
    if(sharedAvailable.current)try{const value=localStorage.getItem(key);if(value!==null)return JSON.parse(value)}catch{sharedAvailable.current=false}
    try{const saved=JSON.parse(sessionStorage.getItem(legacyKey)||'null');return saved?.project_id&&saved.project_id!==projectId?null:saved}catch{return null}
  }
  function store(value:Pending|null){
    const record=value?{...value,project_id:projectId}:null
    try{if(record)sessionStorage.setItem(legacyKey,JSON.stringify(record));else sessionStorage.removeItem(legacyKey)}catch{}
    // A null marker prevents another tab's stale session restoring a finished job.
    try{localStorage.setItem(key,JSON.stringify(record))}catch{sharedAvailable.current=false}
    window.dispatchEvent(new CustomEvent(changedEvent,{detail:key}))
  }
  function active(run:number){return live.current&&epoch.current===run&&currentKey.current===key}
  function clear(pending:Pending){if(read()?.request_key===pending.request_key)store(null)}
  async function poll(pending:Pending,run:number){
    setJobId(pending.job_id!);setBusy(true);setError('');setStartedAt(pending.started_at||Date.now());setStatus('正在读取生成进度');locked.current=true
    try{
      while(active(run)){
        const job=await api<Job<T>>(`/api/v1/projects/${projectId}/generation-jobs/${pending.job_id}`)
        if(!active(run))return
        setStatus(job.status==='queued'?job.error||'排队中':job.status==='running'?'正在生成工作流':'正在保存工作流')
        if(job.status==='completed'&&job.result){clear(pending);callback.current(job.result,pending.submitted);setJobId('');return}
        if(['error','interrupted'].includes(job.status)){clear(pending);setJobId('');throw new Error(job.error||'生成已停止，原草稿保持不变')}
        await new Promise(resolve=>setTimeout(resolve,1000))
      }
    }catch(e){if(active(run))setError(String(e))}finally{if(active(run)){polling.current='';setBusy(false);locked.current=false;setStatus('');setStartedAt(undefined)}}
  }
  function resume(pending:Pending){
    if(polling.current===pending.job_id)return
    polling.current=pending.job_id!
    return poll(pending,++epoch.current)
  }
  useEffect(()=>{
    live.current=true;++epoch.current;locked.current=false;submitting.current=false;polling.current='';sharedAvailable.current=true;observed.current=null
    setBusy(false);setJobId('');setError('');setStatus('');setStartedAt(undefined)
    function sync(saved=read()){
      if(saved)observed.current=saved
      if(!saved){
        try{sessionStorage.removeItem(legacyKey)}catch{}
        if(!polling.current&&!submitting.current){setBusy(false);setStatus('');setStartedAt(undefined)}
        return
      }
      if(submitting.current)return
      if(saved.job_id){void resume(saved);return}
      // Retry a lost submission response with its original request key. A Web
      // Lock waits for an in-flight submission before checking for its job id.
      setStartedAt(saved.error?undefined:saved.started_at);setStatus(saved.error?'':'正在提交工作流，可接续上次请求');setError(saved.error||'')
    }
    const saved=read()
    if(saved)store({...saved,started_at:saved.started_at||Date.now()})
    const storage=(event:StorageEvent)=>{if(event.key===key){try{sync(JSON.parse(event.newValue||'null'))}catch{sync()}}else if(event.key===null)sync()}
    const local=(event:Event)=>{if((event as CustomEvent).detail===key)sync()}
    window.addEventListener('storage',storage);window.addEventListener(changedEvent,local);sync()
    return()=>{live.current=false;epoch.current++;window.removeEventListener('storage',storage);window.removeEventListener(changedEvent,local)}
  },[key])
  useEffect(()=>{if(!startedAt)return;setNow(Date.now());const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer)},[startedAt])
  async function start(endpoint:string,body:Record<string,unknown>,submitted:string){
    if(locked.current)return
    const run=epoch.current
    observed.current=read();locked.current=true;submitting.current=true;setBusy(true);setError('');setStatus('正在提交');setStartedAt(Date.now())
    let accepted:Pending|undefined
    async function submit(){
      if(!active(run))return
      const old=read()||observed.current
      if(old?.job_id){accepted=old;return}
      const signature=JSON.stringify(body)
      const pending:Pending=old&&(!old.error||old.signature===signature)?{...old,error:undefined}:{submitted,signature,endpoint,request_key:clientId(),started_at:Date.now()}
      store(pending)
      try{
        const result=await api<T|Job<T>>(pending.endpoint||endpoint,{method:'POST',body:JSON.stringify({...JSON.parse(pending.signature),request_key:pending.request_key})})
        if(result&&typeof result==='object'&&'job_id' in result){pending.job_id=String(result.job_id);store(pending);accepted=pending}
        else{clear(pending);if(active(run))callback.current(result as T,pending.submitted)}
      }catch(e){store({...pending,error:String(e)});throw e}
    }
    try{
      // Release before polling so other tabs can immediately follow the job.
      if(navigator.locks?.request)await navigator.locks.request(key,submit)
      else await submit()
    }catch(e){if(active(run))setError(String(e))}finally{
      if(active(run)){submitting.current=false;locked.current=false;if(accepted)void resume(accepted);else{setBusy(false);setStatus('');setStartedAt(undefined)}}
    }
  }
  async function stop(){
    if(!jobId)return
    const run=epoch.current
    try{await api(`/api/v1/projects/${projectId}/generation-jobs/${jobId}/stop`,{method:'POST'})}catch(e){if(active(run))setError(String(e))}
  }
  const elapsed=Math.max(0,Math.floor((now-(startedAt||now))/1000))
  const waited=elapsed<60?`${elapsed} 秒`:`${Math.floor(elapsed/60)} 分 ${elapsed%60} 秒`
  return {busy,status:status&&startedAt?`${status} · 已等待 ${waited}`:status,error,jobId,start,stop}
}
