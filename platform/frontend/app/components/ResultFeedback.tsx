'use client'
import {useState} from 'react'
import {api} from '@/lib/platform'
import {FeedbackButton} from './UserFeedback'
import {type FeedbackSource} from '@/lib/feedback'
export default function ResultFeedback({base,requestId,source,excerpt}:{base:string;requestId:string;source?:FeedbackSource;excerpt?:string}){
  const [value,setValue]=useState<boolean>(),[busy,setBusy]=useState(false),[error,setError]=useState('')
  async function save(helpful:boolean){setBusy(true);setError('');try{await api(base+'/feedback/'+encodeURIComponent(requestId),{method:'PUT',body:JSON.stringify({helpful})});setValue(helpful)}catch(e){setError(String(e))}finally{setBusy(false)}}
  return <span aria-label="结果反馈"><button disabled={busy} aria-pressed={value===true} onClick={()=>void save(true)}>有用</button><button disabled={busy} aria-pressed={value===false} onClick={()=>void save(false)}>需要修改</button><FeedbackButton source={source} excerpt={excerpt} category="result">{value===false?'说明哪里需要改进':'反馈给平台'}</FeedbackButton>{error&&<span role="alert">{error}</span>}</span>
}
