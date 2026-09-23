'use client'
import {useEffect,useRef,useState} from 'react'
import {api} from './platform'
import {clientId} from './client-id'
import {useAccount} from '@/app/components/AuthBoundary'
type Job<T>={job_id:string;status:string;error?:string;result?:T}
type Pending={job_id?:string;submitted:string;request_key:string;signature:string}
export function useWorkflowGeneration<T>(projectId:string, storageKey:string,onResult:(result:T,submitted:string)=>void){
  const account=useAccount()
  const key=`${account?.id||'local'}:${storageKey}:pending-generation`
  const [busy,setBusy]=useState(false),[status,setStatus]=useState(''),[error,setError]=useState(''),[jobId,setJobId]=useState('')
  const live=useRef(true),epoch=useRef(0),locked=useRef(false),callback=useRef(onResult)
  callback.current=onResult
  function read():Pending|null{try{return JSON.parse(sessionStorage.getItem(key)||'null')}catch{return null}}
  function store(value:Pending|null){try{if(value)sessionStorage.setItem(key,JSON.stringify(value));else sessionStorage.removeItem(key)}catch{}}
  function active(run:number){return live.current&&epoch.current===run}
  async function poll(pending:Pending,run=epoch.current){
    setJobId(pending.job_id!);setBusy(true);locked.current=true
    try{
      while(active(run)){
        const job=await api<Job<T>>(`/api/v1/projects/${projectId}/generation-jobs/${pending.job_id}`)
        if(!active(run))return
        setStatus(job.status==='queued'?job.error||'排队中':job.status==='running'?'正在生成工作流':'正在保存工作流')
        if(job.status==='completed'&&job.result){store(null);callback.current(job.result,pending.submitted);setJobId('');setStatus('');return}
        if(['error','interrupted'].includes(job.status)){store(null);setJobId('');throw new Error(job.error||'生成已停止，原草稿保持不变')}
        await new Promise(resolve=>setTimeout(resolve,1000))
      }
    }catch(e){if(active(run))setError(String(e))}finally{if(active(run)){setBusy(false);locked.current=false}}
  }
  useEffect(()=>{live.current=true;const run=++epoch.current;locked.current=false;setBusy(false);setJobId('');setError('');setStatus('');const saved=read();if(saved?.job_id)void poll(saved,run);return()=>{live.current=false;epoch.current++}},[key])
  async function start(endpoint:string,body:Record<string,unknown>,submitted:string){
    if(locked.current)return
    const run=epoch.current
    const old=read()
    if(old?.job_id){setError('');await poll(old,run);return}
    locked.current=true;setBusy(true);setError('');setStatus('正在提交')
    const signature=JSON.stringify(body),pending:Pending={submitted,signature,request_key:old?.signature===signature?old.request_key:clientId()}
    store(pending)
    try{
      const result=await api<T|Job<T>>(endpoint,{method:'POST',body:JSON.stringify({...body,request_key:pending.request_key})})
      if(result&&typeof result==='object'&&'job_id' in result){pending.job_id=String(result.job_id);store(pending);if(active(run))await poll(pending,run)}
      else{store(null);if(active(run))callback.current(result as T,submitted)}
    }catch(e){if(active(run))setError(String(e))}finally{if(active(run)){setBusy(false);locked.current=false;setStatus('')}}
  }
  async function stop(){if(!jobId)return;await api(`/api/v1/projects/${projectId}/generation-jobs/${jobId}/stop`,{method:'POST'})}
  return {busy,status,error,jobId,start,stop}
}
