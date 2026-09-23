'use client'
import {useEffect,useState} from 'react'
import {api} from '@/lib/platform'
import styles from '@/app/projects/projects.module.css'
type Row={actor:string;feature:string;outcome:string;count:number;tokens:number|null;measured:number;seconds:number}
type Summary={active_users:number;features:Row[];feedback:{helpful:number;count:number}[];notes:string[]}
const names:Record<string,string>={chat:'对话提交',generate:'工作流生成',edit:'编辑工作流',upload:'上传或复用资料',train:'训练',predict:'预测',download:'下载请求',save_method:'保存方法',reuse:'加入已有方法或流程',share:'分享',workflow_run:'运行工作流',chat_result:'官方智能体任务结果',generation_result:'生成任务结果'}
const outcomes:Record<string,string>={submitted:'已提交',completed:'完成',error:'失败',failed:'失败',interrupted:'已停止'}
export default function UsageSummary(){
  const [data,setData]=useState<Summary>(),[error,setError]=useState('')
  async function refresh(){try{setData(await api<Summary>('/api/v1/admin/usage'));setError('')}catch(e){setError(String(e))}}
  useEffect(()=>{void refresh()},[])
  return <section className={styles.panel}><h2>功能使用情况</h2><button onClick={()=>void refresh()}>刷新使用统计</button>{error&&<p role="alert">{error}</p>}{data&&<>
    <p>统计期活跃账号：{data.active_users}。保留近 30 天明细、180 天日汇总。仅记录功能和用量，不记录消息与文件正文。</p>
    <div style={{overflowX:'auto'}}><table><thead><tr><th>发起方</th><th>功能</th><th>状态</th><th>次数</th><th>已报告 token</th></tr></thead><tbody>{data.features.map(r=><tr key={[r.actor,r.feature,r.outcome].join(':')}><td>{r.actor==='employee'?'员工':'智能体工具'}</td><td>{names[r.feature]||r.feature}</td><td>{outcomes[r.outcome]||r.outcome}</td><td>{r.count}</td><td>{r.tokens??'未知'}{r.measured>0&&r.measured<r.count?'（部分已计量）':''}</td></tr>)}</tbody></table></div>
    <p>反馈：有用 {data.feedback.find(r=>r.helpful===1)?.count||0}，需要修改 {data.feedback.find(r=>r.helpful===0)?.count||0}。</p>{data.notes.map(n=><p key={n}><small>{n}</small></p>)}
  </>}</section>
}
