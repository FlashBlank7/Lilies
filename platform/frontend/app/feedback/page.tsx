'use client'

import Link from 'next/link'
import {useEffect,useRef,useState} from 'react'
import {useRouter,useSearchParams} from 'next/navigation'
import {api,idempotency} from '@/lib/platform'
import {feedbackCategories,feedbackStatuses,imageError,postFeedback,type FeedbackDetail,type FeedbackRow} from '@/lib/feedback'
import {useAccount} from '../components/AuthBoundary'
import {FeedbackButton} from '../components/UserFeedback'
import styles from '../projects/projects.module.css'
import feedback from '../components/feedback.module.css'

export default function FeedbackPage(){
  const user=useAccount(),router=useRouter(),params=useSearchParams(),id=params.get('id')||''
  const [scope,setScope]=useState(user?.role==='admin'?'all':'mine'),[status,setStatus]=useState(''),[category,setCategory]=useState(''),[q,setQ]=useState('')
  const [feature,setFeature]=useState('')
  const [rows,setRows]=useState<FeedbackRow[]>([]),[total,setTotal]=useState(0),[offset,setOffset]=useState(0),[refresh,setRefresh]=useState(0)
  const [detail,setDetail]=useState<FeedbackDetail|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(false)
  const [detailError,setDetailError]=useState('')
  const reload=()=>setRefresh(v=>v+1)
  useEffect(()=>{window.addEventListener('lilies:feedback-changed',reload);return()=>window.removeEventListener('lilies:feedback-changed',reload)},[])
  useEffect(()=>{
    let active=true;setLoading(true);setError('')
    const query=new URLSearchParams({scope,offset:String(offset),q});if(status)query.set('status',status);if(category)query.set('category',category)
    if(feature)query.set('page',feature)
    void api<{items:FeedbackRow[];total:number}>('/api/v1/feedback?'+query).then(r=>{if(active){setRows(r.items);setTotal(r.total)}}).catch(e=>{if(active)setError(String(e))}).finally(()=>{if(active)setLoading(false)})
    return()=>{active=false}
  },[scope,status,category,q,feature,offset,refresh])
  useEffect(()=>{
    let active=true;setDetailError('');setDetail(old=>old?.id===id?old:null)
    if(id)void api<FeedbackDetail>('/api/v1/feedback/'+encodeURIComponent(id)).then(async r=>{
      if(!active)return;setDetail(r)
      try{await api('/api/v1/feedback/'+r.id+'/read',{method:'POST',body:JSON.stringify({revision:r.revision})});if(active){setRows(rows=>rows.map(row=>row.id===r.id?{...row,read_revision:r.revision}:row));window.dispatchEvent(new Event('lilies:feedback-read'))}}
      catch(e){if(active)setDetailError('已打开反馈，但未读状态暂未更新：'+String(e))}
    }).catch(e=>{if(active)setDetailError(String(e))})
    return()=>{active=false}
  },[id,refresh])
  function filter(set:(s:string)=>void,v:string){set(v);setOffset(0)}
  return <main className={styles.page}>
    <header className={styles.header}><div><h1>意见反馈</h1><p>告诉我们哪里需要改进，也欢迎分享好用的方法。反馈仅提交者和平台管理员可见。</p></div><FeedbackButton>提出意见</FeedbackButton></header>
    <div className={feedback.filters}>
      {user?.role==='admin'&&<label>查看范围<select value={scope} onChange={e=>filter(setScope,e.target.value)}><option value="all">用户反馈</option><option value="mine">我的反馈</option></select></label>}
      <label>状态<select value={status} onChange={e=>filter(setStatus,e.target.value)}><option value="">全部状态</option>{Object.entries(feedbackStatuses).map(([v,t])=><option key={v} value={v}>{t}</option>)}</select></label>
      <label>类型<select value={category} onChange={e=>filter(setCategory,e.target.value)}><option value="">全部类型</option>{Object.entries(feedbackCategories).map(([v,t])=><option key={v} value={v}>{t}</option>)}</select></label>
      <label>来自功能<select value={feature} onChange={e=>filter(setFeature,e.target.value)}><option value="">全部功能</option><option value="conversation">项目对话</option><option value="workflow">工作流</option><option value="run">运行与结果</option><option value="general">一般建议</option></select></label>
      <label>查找反馈<input maxLength={100} value={q} onChange={e=>filter(setQ,e.target.value)} placeholder="意见、项目或提交者"/></label><button onClick={reload}>刷新反馈</button>
    </div>
    {error&&<p role="alert" className={styles.error}>{error}</p>}
    <div className={feedback.layout}><section aria-label="反馈列表" className={feedback.items}>
      <small>{loading?'正在读取…':`共 ${total} 条反馈`}</small>
      {rows.map(row=><button key={row.id} className={`${feedback.item} ${row.id===id?feedback.selected:''}`} aria-pressed={row.id===id} onClick={()=>router.replace('/feedback?id='+row.id)}>
        <strong>{row.revision>row.read_revision?'● 未读 · ':''}{row.summary}</strong><span>{feedbackStatuses[row.status]} · {feedbackCategories[row.category]}</span><small>{row.user_name} · {row.project_name||'一般建议'} · {new Date(row.updated_at).toLocaleString()}</small>
      </button>)}
      {!loading&&!rows.length&&!error&&<p>还没有符合条件的反馈。可以随时提出意见，无需先运行任务。</p>}
      <div className={styles.actions}><button disabled={!offset||loading} onClick={()=>setOffset(Math.max(0,offset-40))}>上一页</button><button disabled={offset+rows.length>=total||loading} onClick={()=>setOffset(offset+40)}>下一页</button></div>
    </section><section className={styles.panel} aria-label="反馈详情">
      {detailError&&<p role="alert" className={styles.error}>{detailError}</p>}
      {!detail&&!detailError&&<p>{id?'正在打开反馈…':'选择一条反馈，查看回复和处理进展。'}</p>}
      {detail&&<><h2>{feedbackStatuses[detail.status]} · {feedbackCategories[detail.category]}</h2><small>{detail.user_name} 提交 · 编号 {detail.id}</small>
        {detail.source.project_id&&<details><summary>关联操作：{detail.project_name}</summary><div className={feedback.context}><dl>{Object.entries(detail.source).filter(([k,v])=>v&&k!=='page').map(([k,v])=><div key={k}><dt>{({project_id:'项目',conversation_id:'会话',request_id:'回答',task_id:'运行',workflow_id:'工作流'} as Record<string,string>)[k]}</dt><dd>{v}</dd></div>)}</dl>
          {detail.source_available?<Link href={'/projects/'+encodeURIComponent(detail.source.project_id)}>打开项目</Link>:<p>原操作已不可访问；提交的意见与回复仍保留。</p>}</div></details>}
        {detail.excerpt&&<details><summary>提交时主动附上的内容</summary><pre>{detail.excerpt}</pre></details>}
        {detail.messages.map(m=><article key={m.id} className={feedback.message}><small>{m.user_name}{m.role==='admin'?' · 管理员':''} · {new Date(m.created_at).toLocaleString()}</small>{m.status&&<p><strong>状态更新：{feedbackStatuses[m.status]}</strong></p>}<p>{m.text}</p>{m.media_type&&<a href={`/api/platform/api/v1/feedback/${detail.id}/messages/${m.id}/image`} target="_blank" rel="noreferrer"><img className={feedback.screenshot} loading="lazy" src={`/api/platform/api/v1/feedback/${detail.id}/messages/${m.id}/image`} alt={`${m.user_name}主动附上的截图`}/></a>}</article>)}
        <ReplyForm key={detail.id} detail={detail} admin={user?.role==='admin'} onSaved={reload}/>
      </>}
    </section></div>
  </main>
}

function ReplyForm({detail,admin,onSaved}:{detail:FeedbackDetail;admin:boolean;onSaved:()=>void}){
  const [text,setText]=useState(''),[status,setStatus]=useState(''),[image,setImage]=useState<File|null>(null),[preview,setPreview]=useState('')
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('')
  const key=useRef(''),file=useRef<HTMLInputElement>(null)
  function changed(){key.current=idempotency();setError('');setNotice('')}
  useEffect(()=>{if(!image){setPreview('');return}const url=URL.createObjectURL(image);setPreview(url);return()=>URL.revokeObjectURL(url)},[image])
  async function submit(e:React.FormEvent){
    e.preventDefault();if(busy)return;setBusy(true);setError('');key.current ||= idempotency()
    try{await postFeedback('/api/v1/feedback/'+detail.id+'/messages',{request_key:key.current,text,status:status||null,expected_revision:status?detail.revision:null},image);setText('');setStatus('');setImage(null);if(file.current)file.current.value='';key.current='';setNotice('回复已保存');onSaved()}
    catch(e){setError(String(e))}finally{setBusy(false)}
  }
  return <form onSubmit={submit} aria-label="回复反馈"><fieldset disabled={busy} className={feedback.replyFields}><label>回复或补充<textarea required maxLength={8000} value={text} onChange={e=>{setText(e.target.value);changed()}} placeholder={admin?'说明处理进展；暂不处理时请说明原因。':'可以补充情况，或说明问题是否已经解决。'}/></label>
    {admin?<label>处理状态<select value={status} onChange={e=>{setStatus(e.target.value);changed()}}><option value="">保持当前状态</option>{Object.entries(feedbackStatuses).map(([v,t])=><option key={v} value={v}>{t}</option>)}</select></label>:['resolved','declined'].includes(detail.status)&&<label className={feedback.actions}><input type="checkbox" checked={status==='received'} onChange={e=>{setStatus(e.target.checked?'received':'');changed()}}/>问题仍存在，重新打开</label>}
    <label>补充截图（可选，最多3 MB）<input ref={file} type="file" accept="image/png,image/jpeg,image/webp" onChange={e=>{const f=e.target.files?.[0]||null,problem=imageError(f);if(problem){setError(problem);e.target.value='';return}setImage(f);changed()}}/></label>
    {preview&&<><img src={preview} className={feedback.screenshot} alt="即将回复的截图"/><button type="button" onClick={()=>{setImage(null);if(file.current)file.current.value='';changed()}}>移除截图</button></>}
    {error&&<p role="alert" className={styles.error}>{error}</p>}{notice&&<p role="status">{notice}</p>}<button type="submit" disabled={busy||!text.trim()}>{busy?'正在保存…':'发送回复'}</button>
  </fieldset></form>
}
