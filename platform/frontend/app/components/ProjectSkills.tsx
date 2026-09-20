'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import styles from './workspace-tools.module.css'
type Skill = {id:string;name:string;description:string;content:string;revision:number;references:Record<string,string>}
export default function ProjectSkills({projectId}:{projectId:string}) {
  const base=`/api/v1/projects/${projectId}/skills`
  const [items,setItems]=useState<Skill[]>([]),[selected,setSelected]=useState<Skill>(),[error,setError]=useState(''),[busy,setBusy]=useState(false)
  async function refresh(){setItems(await api<Skill[]>(base))}
  useEffect(()=>{void refresh().catch(e=>setError(String(e)))},[base])
  return <section className={styles.section}><h2>项目使用说明</h2><p>智能体先看到说明的用途，需要时再读取内容。可以在这里说明已有工作流的使用方法。</p><div className={styles.row}>
    {items.map(s=><button key={s.id} onClick={()=>{void api<Skill>(base+'/'+s.id).then(setSelected).catch(e=>setError(String(e)))}}>{s.name}</button>)}
    <button onClick={()=>setSelected({id:crypto.randomUUID(),name:'新说明',description:'',content:'',references:{},revision:0})}>添加说明</button></div>
    {selected&&<><div className={styles.row}><label>名称<input value={selected.name} onChange={e=>setSelected({...selected,name:e.target.value})}/></label><label>用途<input value={selected.description} onChange={e=>setSelected({...selected,description:e.target.value})}/></label></div>
      <textarea className={styles.editor} aria-label="项目说明正文" value={selected.content} onChange={e=>setSelected({...selected,content:e.target.value})}/>
      <details><summary>引用资料</summary>{Object.entries(selected.references).map(([name,content])=><label key={name}>{name}<textarea className={styles.editor} aria-label={`引用资料 ${name}`} value={content} onChange={e=>setSelected({...selected,references:{...selected.references,[name]:e.target.value}})}/></label>)}<button onClick={()=>setSelected({...selected,references:{...selected.references,[`reference-${Object.keys(selected.references).length+1}.md`]:''}})}>添加引用资料</button></details>
      <button disabled={busy||!selected.name} onClick={async()=>{setBusy(true);setError('');try{const {id,revision,...body}=selected;setSelected(await api<Skill>(base+'/'+id,{method:'PUT',body:JSON.stringify({...body,expected_revision:revision})}));await refresh()}catch(e){setError(String(e))}finally{setBusy(false)}}}>保存说明</button></>}
    {error&&<p role="alert">{error}</p>}
  </section>
}
