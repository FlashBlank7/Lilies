'use client'
import {useState} from 'react'
import {api} from '@/lib/platform'
import {clientId} from '@/lib/client-id'
import ReadingDialog from './ReadingDialog'
import styles from './workspace-tools.module.css'
export default function SaveMethod({projectId,text}:{projectId:string;text:string}) {
  const [open,setOpen]=useState(false),[name,setName]=useState(''),[content,setContent]=useState(text),[busy,setBusy]=useState(false),[error,setError]=useState(''),[saved,setSaved]=useState(false)
  const [id]=useState(()=>clientId())
  async function save(){setBusy(true);setError('');try{await api(`/api/v1/projects/${projectId}/skills/${id}`,{method:'PUT',body:JSON.stringify({name,description:'从项目结果保存的方法',content,references:{},expected_revision:0})});setSaved(true);setOpen(false)}catch(e){setError(String(e))}finally{setBusy(false)}}
  return <><button onClick={()=>setOpen(true)} disabled={saved}>{saved?'方法已保存':'保存方法'}</button>{open&&<ReadingDialog title="保存为项目方法" onClose={()=>setOpen(false)}><div className={styles.section}><p>编辑为可复用的操作说明。保存不调用模型；智能体之后可按用途找到它。</p><label>方法名称<input value={name} maxLength={100} onChange={e=>setName(e.target.value)}/></label><label>方法正文<textarea className={styles.editor} value={content} maxLength={80000} onChange={e=>setContent(e.target.value)}/></label><button disabled={busy||!name.trim()||!content.trim()} onClick={()=>void save()}>保存到项目</button>{error&&<p role="alert">{error}</p>}</div></ReadingDialog>}</>
}
