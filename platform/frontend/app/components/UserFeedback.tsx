'use client'

import Link from 'next/link'
import {createContext,useContext,useEffect,useRef,useState,type ReactNode} from 'react'
import {api,idempotency} from '@/lib/platform'
import {feedbackCategories,imageError,postFeedback,type FeedbackSource} from '@/lib/feedback'
import styles from './feedback.module.css'

type DraftContext={source?:FeedbackSource;excerpt?:string;category?:keyof typeof feedbackCategories}
const FeedbackContext=createContext<(context?:DraftContext)=>void>(()=>{})

export function FeedbackButton({source,excerpt,category,children='反馈给平台'}:DraftContext & {children?:ReactNode}) {
  const open=useContext(FeedbackContext)
  return <button type="button" onClick={()=>open({source,excerpt,category})}>{children}</button>
}

export function FeedbackNavigation() {
  const [count,setCount]=useState(0),[failed,setFailed]=useState(false)
  useEffect(()=>{
    let active=true
    const refresh=()=>{if(document.visibilityState==='hidden')return;void api<{count:number}>('/api/v1/feedback/unread').then(r=>{if(active){setCount(r.count);setFailed(false)}}).catch(()=>{if(active)setFailed(true)})}
    refresh();const timer=setInterval(refresh,30000)
    window.addEventListener('focus',refresh);window.addEventListener('lilies:feedback-changed',refresh);window.addEventListener('lilies:feedback-read',refresh)
    return()=>{active=false;clearInterval(timer);window.removeEventListener('focus',refresh);window.removeEventListener('lilies:feedback-changed',refresh);window.removeEventListener('lilies:feedback-read',refresh)}
  },[])
  return <><FeedbackButton>意见反馈</FeedbackButton><Link href="/feedback" title={failed?'未读提醒暂未更新，打开后可重试':undefined}>反馈记录{count>0?`（${count}条未读）`:''}</Link></>
}

export function FeedbackProvider({children}:{children:ReactNode}) {
  const [open,setOpen]=useState(false),[context,setContext]=useState<DraftContext>({})
  const [text,setText]=useState(''),[category,setCategory]=useState<keyof typeof feedbackCategories>('other')
  const [linked,setLinked]=useState(true),[include,setInclude]=useState(false),[excerpt,setExcerpt]=useState('')
  const [image,setImage]=useState<File|null>(null),[preview,setPreview]=useState('')
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[saved,setSaved]=useState('')
  const requestKey=useRef(''),dialog=useRef<HTMLDialogElement>(null),returnFocus=useRef<HTMLElement|null>(null)
  function launch(next:DraftContext={}) {
    if(!open)returnFocus.current=document.activeElement as HTMLElement|null
    if(!text.trim()&&!image&&!saved){setContext(next);setCategory(next.category||'other');setExcerpt(next.excerpt?.slice(0,6000)||'');setLinked(true);setInclude(false);requestKey.current=idempotency()}
    setOpen(true)
  }
  function close(){if(busy)return;setOpen(false);if(saved){setText('');setImage(null);setSaved('');setError('');setContext({})}}
  useEffect(()=>{
    if(!open)return
    const previous=returnFocus.current
    dialog.current?.showModal()
    dialog.current?.querySelector('textarea')?.focus()
    return()=>{dialog.current?.close();previous?.focus()}
  },[open])
  useEffect(()=>{if(!image){setPreview('');return}const url=URL.createObjectURL(image);setPreview(url);return()=>URL.revokeObjectURL(url)},[image])
  const changed=()=>{requestKey.current=idempotency();setError('')}
  async function submit(e:React.FormEvent){
    e.preventDefault();if(busy)return;setBusy(true);setError('')
    try{const r=await postFeedback('/api/v1/feedback',{request_key:requestKey.current||idempotency(),text,category,source:linked?context.source||{}:{},excerpt:include?excerpt:''},image);setSaved(r.id)}catch(e){setError(String(e))}finally{setBusy(false)}
  }
  return <FeedbackContext.Provider value={launch}>{children}{open&&<dialog ref={dialog} className={styles.drawer} aria-labelledby="feedback-title" onCancel={e=>{e.preventDefault();close()}}>
    <header><h2 id="feedback-title">反馈给平台</h2><button type="button" disabled={busy} onClick={close} aria-label="关闭意见反馈">关闭</button></header>
    {saved?<div className={styles.form}><p role="status">已收到你的反馈。后续回复会显示在反馈记录中。</p><Link href={'/feedback?id='+saved} onClick={close}>查看这条反馈</Link><button onClick={close}>继续使用平台</button></div>:<form className={styles.form} onSubmit={submit}><fieldset disabled={busy}>
      <p>遇到了什么问题，或者希望怎样改进？只有你和平台管理员可见。提交不会运行任务或调用模型。</p>
      <label>意见或问题<textarea autoFocus required maxLength={8000} value={text} onChange={e=>{setText(e.target.value);changed()}} placeholder="例如：这个结果没有解释为什么这样判断，希望能给出依据。" /></label>
      <label>类型（可不选）<select value={category} onChange={e=>{setCategory(e.target.value as typeof category);changed()}}>{Object.entries(feedbackCategories).map(([v,t])=><option key={v} value={v}>{t}</option>)}</select></label>
      {context.source?.project_id&&<section className={styles.context}><label className={styles.check}><input type="checkbox" checked={linked} onChange={e=>{setLinked(e.target.checked);changed()}}/>附带当前操作的定位信息</label><p>关联{context.source.page==='conversation'?'当前项目对话':context.source.page==='workflow'?'当前工作流':'这次运行结果'}。只附带编号，不附带项目文件、对话全文或运行日志。</p><details><summary>查看关联编号</summary><dl>{Object.entries(context.source).filter(([k,v])=>v&&k!=='page').map(([k,v])=><div key={k}><dt>{({project_id:'项目',conversation_id:'会话',request_id:'回答',task_id:'运行',workflow_id:'工作流'} as Record<string,string>)[k]}</dt><dd>{v}</dd></div>)}</dl></details></section>}
      {!!context.excerpt&&<section className={styles.context}><label className={styles.check}><input type="checkbox" checked={include} onChange={e=>{setInclude(e.target.checked);changed()}}/>附上这段内容（可编辑）</label>{include&&<label>附带片段<textarea maxLength={6000} value={excerpt} onChange={e=>{setExcerpt(e.target.value);changed()}}/></label>}</section>}
      <label>截图（可选，最多3 MB）<input type="file" accept="image/png,image/jpeg,image/webp" onChange={e=>{const f=e.target.files?.[0]||null,problem=imageError(f);if(problem){setError(problem);e.target.value='';return}setImage(f);changed()}}/></label>
      {preview&&<><img className={styles.screenshot} src={preview} alt="即将提交的截图"/><button type="button" onClick={()=>{setImage(null);changed()}}>移除截图</button></>}
      <small>请检查所选片段与截图，移除不希望分享的内容。关闭面板会保留本次未提交的文字。</small>
      {error&&<p role="alert" className={styles.error}>{error}</p>}
      <div className={styles.actions}><button type="submit" disabled={busy||!text.trim()}>{busy?'正在提交…':'提交反馈'}</button><button type="button" disabled={busy} onClick={close}>暂不提交</button></div>
    </fieldset></form>}
  </dialog>}</FeedbackContext.Provider>
}
