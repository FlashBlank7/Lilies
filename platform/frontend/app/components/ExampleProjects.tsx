'use client'

import {useEffect, useRef, useState} from 'react'
import {api} from '@/lib/platform'
import {clientId} from '@/lib/client-id'
import {useAccount} from './AuthBoundary'
import styles from './example-projects.module.css'

export type ExampleProject = {id:string;version:number;name:string;category:string;description:string;question:string;exercise:string;requires:string[];featured:boolean;steps:string[];files:{name:string;size:number;path?:string}[];workflow_count:number;workflows?:{id?:string;key?:string;name:string}[];manual_path?:string}
const categories=['全部','日常办公','数据处理','机器学习','知识问答','流程搭建']

export default function ExampleProjects({onCreated}:{onCreated:(id:string)=>void}){
  const account=useAccount()
  const [items,setItems]=useState<ExampleProject[]>([]),[category,setCategory]=useState('全部')
  const [selected,setSelected]=useState<ExampleProject|null>(null),[name,setName]=useState('')
  const [error,setError]=useState(''),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false)
  const locked=useRef(false),keys=useRef<Record<string,string>>({})
  async function refresh(){setLoading(true);setError('');try{setItems(await api<ExampleProject[]>('/api/v1/example-projects'))}catch(e){setError(String(e))}finally{setLoading(false)}}
  useEffect(()=>{void refresh()},[])
  async function create(){
    if(!selected||locked.current)return
    locked.current=true;setBusy(true);setError('')
    const signature=selected.id+':'+name.trim(),storageKey=`lilies:user:${account?.id}:example-request:${signature}`
    let key=keys.current[signature]
    try{key=key||sessionStorage.getItem(storageKey)||''}catch{}
    if(!key)key=clientId()
    keys.current[signature]=key
    try{sessionStorage.setItem(storageKey,key)}catch{}
    try{
      const result=await api<{project_id:string}>(`/api/v1/example-projects/${selected.id}/instantiate`,{method:'POST',body:JSON.stringify({request_key:key,name:name.trim()})})
      try{sessionStorage.removeItem(storageKey)}catch{}
      delete keys.current[signature];onCreated(result.project_id)
    }catch(e){setError(String(e))}finally{locked.current=false;setBusy(false)}
  }
  return <section className={styles.gallery} aria-label="示例项目目录" data-guide-anchor="example-gallery">
    <p>先选一个用得上的场景。资料、流程和使用说明会放入你自己的项目，之后都可以修改。</p>
    <div className={styles.filters} aria-label="示例分类">{categories.map(value=><button key={value} aria-pressed={category===value} disabled={busy} onClick={()=>{setCategory(value);setSelected(null)}}>{value}</button>)}</div>
    {loading&&<p role="status">正在读取示例…</p>}
    {error&&<p role="alert">{error} {!items.length&&<button onClick={()=>void refresh()}>重试</button>}</p>}
    {selected?<article className={styles.detail}>
      <button disabled={busy} onClick={()=>setSelected(null)}>返回示例列表</button><h3>{selected.name}</h3><p>{selected.description}</p>
      <p><strong>准备条件：</strong>{selected.requires.join(' · ')}。通过对话调用还需要项目智能体连接。</p>
      <p>包含 {selected.files.length} 份自编或合成资料、{selected.workflow_count} 条可编辑流程。创建不会自动运行任务。</p>
      <ol>{selected.steps.map(step=><li key={step}>{step}</li>)}</ol>
      <blockquote>{selected.question}</blockquote><p><strong>修改练习：</strong>{selected.exercise}</p>
      <details><summary>查看示例文件</summary><ul>{selected.files.map(file=><li key={file.name}>{file.name} · {file.size.toLocaleString()} 字节</li>)}</ul></details>
      <label>项目名称<input aria-label="示例项目名称" value={name} maxLength={100} disabled={busy} onChange={e=>setName(e.target.value)} placeholder={selected.name+' · 示例'}/></label>
      <button className={styles.primary} disabled={busy} onClick={()=>void create()}>{busy?'正在准备资料与流程…':'创建我的示例项目'}</button>
    </article>:<div className={styles.grid}>{items.filter(item=>category==='全部'||item.category===category).map(item=><article className={styles.card} key={item.id}>
      <small>{item.category}{item.featured?' · 推荐入门':''}</small><h3>{item.name}</h3><p>{item.question}</p><p className={styles.requirements}>{item.requires.join(' · ')}</p>
      <button onClick={()=>{setSelected(item);setName('');setError('')}} aria-label={'了解示例：'+item.name}>查看与创建</button>
    </article>)}</div>}
  </section>
}
