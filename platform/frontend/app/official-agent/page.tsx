'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'
import { api } from '@/lib/platform'
import { useAccount } from '../components/AuthBoundary'
import UsageSummary from '../components/UsageSummary'
import styles from '../projects/projects.module.css'

type Config = { enabled: boolean; executable: string; version: string; model: string; thinking: string; reserve_percent: number; concurrency: number; max_tokens: number | null; max_seconds: number }
type Model = { model: string; displayName: string; supportedReasoningEfforts: {reasoningEffort: string}[] }
type Job = {id: string; project_id: string; user_id: string; kind: string; status: string; created: number; started?: number; ended?: number; tokens: number | null; error: string; queue_seconds: number; execution_seconds:number}
type State = {config: Config; models?: Model[]; account?: {type: string; email?: string; planType?: string}; error?: string; dispatch_reason?: string; jobs?: Job[];
  rate_limits?: {rateLimits?: Bucket; rateLimitsByLimitId?: Record<string,Bucket>}; login?: {authUrl?: string; verificationUrl?: string; userCode?: string}}
type Bucket = {limitId?: string; primary?: {usedPercent: number; windowDurationMins: number}; secondary?: {usedPercent: number; windowDurationMins: number}}
const base = '/api/v1/admin/official-agent'
const statusLabels: Record<string,string> = {queued:'排队中',running:'执行中',waiting:'等待后续处理',completed:'已完成',error:'失败',interrupted:'已停止'}
export default function OfficialAgentPage() {
  const account = useAccount()
  const [state,setState] = useState<State>()
  const [config,setConfig] = useState<Config>()
  const [busy,setBusy] = useState(false)
  const [error,setError] = useState('')
  const [notice,setNotice] = useState('')
  async function refresh(probe = false) {
    const next = await api<State>(base + (probe ? '?refresh=true' : ''))
    setState(old=>({...old,...next})); setConfig(next.config)
  }
  useEffect(()=>{ if(account?.role==='admin') void refresh().catch(e=>setError(String(e))) },[account?.role])
  async function action(fn:()=>Promise<unknown>, message='') {
    setBusy(true);setError('');setNotice('')
    try { await fn(); setNotice(message) } catch(e) {setError(String(e))} finally {setBusy(false)}
  }
  async function login(mode:string) {
    const next = await api<Partial<State>>(base+'/login',{method:'POST',body:JSON.stringify({mode})})
    setState(old=>({...old!,...next}));if(next.config)setConfig(next.config)
  }
  if(account?.role!=='admin')return <main className={styles.page}><h1>官方智能体</h1><p>请联系管理员配置服务。在已授权项目中可以直接使用。</p></main>
  const buckets = Object.values(state?.rate_limits?.rateLimitsByLimitId || {})
  if(!buckets.length && state?.rate_limits?.rateLimits)buckets.push(state.rate_limits.rateLimits)
  return <main className={styles.page}><h1>官方智能体</h1><p>统一提供给获授权项目。员工使用自己的平台账号，项目资料共享，对话相互独立。</p>
    {error&&<p role="alert" className={styles.error}>{error}</p>}{notice&&<p role="status">{notice}</p>}
    <section className={styles.panel}><h2>服务账号</h2><p>管理员接入来源：服务器 Codex 订阅登录。平台不会收集账号密码。</p>
      <p>{state?.account?.type==='chatgpt'?`已连接 · ${state.account.email || ''} · ${state.account.planType || ''}`:'尚未检查登录状态'}</p>
      <div className={styles.actions}><button disabled={busy} onClick={()=>void action(()=>login('existing'))}>连接服务器已登录账号</button>
        <button disabled={busy} onClick={()=>void action(()=>login('browser'))}>浏览器登录</button><button disabled={busy} onClick={()=>void action(()=>login('device'))}>设备码登录</button>
        <button disabled={busy} onClick={()=>void action(()=>refresh(true))}>刷新账号、模型与额度</button>
        <button disabled={busy} onClick={()=>void action(async()=>{await api(base+'/disconnect',{method:'POST'});setState(undefined);await refresh()},'服务已断开')}>断开服务</button></div>
      {state?.login&&(state.login.authUrl||state.login.verificationUrl)&&<p><a href={state.login.authUrl||state.login.verificationUrl} target="_blank" rel="noreferrer">打开账号登录页面</a> {state.login.userCode&&<>登录码：<code>{state.login.userCode}</code></>} 登录后点击刷新。</p>}
      {state?.error&&<p role="alert">{state.error}</p>}
      <small>浏览器登录需在服务器所在设备完成回调；远程管理可使用设备码。连接只复制登录凭据，不导入个人历史和配置。</small>
    </section>
    {config&&<section className={styles.panel}><h2>调度与用量</h2><form className={styles.modelSetup} onSubmit={e=>{e.preventDefault();void action(async()=>{await api(base,{method:'PUT',body:JSON.stringify(config)});await refresh()},'设置已保存')}}>
      <label><input type="checkbox" checked={config.enabled} onChange={e=>setConfig({...config,enabled:e.target.checked})}/> 启用官方智能体</label>
      <label>模型<select value={config.model} onChange={e=>{const m=state?.models?.find(m=>m.model===e.target.value);setConfig({...config,model:e.target.value,thinking:m?.supportedReasoningEfforts.some(x=>x.reasoningEffort===config.thinking)?config.thinking:m?.supportedReasoningEfforts[0]?.reasoningEffort||config.thinking})}}>
        {!state?.models?.some(m=>m.model===config.model)&&<option value={config.model}>{config.model}（待验证）</option>}{state?.models?.map(m=><option key={m.model} value={m.model}>{m.displayName||m.model}</option>)}</select></label>
      <label>思考强度<select value={config.thinking} onChange={e=>setConfig({...config,thinking:e.target.value})}>
        {!state?.models?.find(m=>m.model===config.model)?.supportedReasoningEfforts.some(e=>e.reasoningEffort===config.thinking)&&<option value={config.thinking}>{config.thinking}（待验证）</option>}
        {state?.models?.find(m=>m.model===config.model)?.supportedReasoningEfforts.map(e=><option key={e.reasoningEffort}>{e.reasoningEffort}</option>)}</select></label>
      <label>个人保留额度（%）<input type="number" min={0} max={100} value={config.reserve_percent} onChange={e=>setConfig({...config,reserve_percent:Number(e.target.value)})}/></label>
      <label>同时执行数量<input type="number" min={1} max={8} value={config.concurrency} onChange={e=>setConfig({...config,concurrency:Number(e.target.value)})}/></label>
      <label><input type="checkbox" checked={config.max_tokens === null} onChange={e=>setConfig({...config,max_tokens:e.target.checked ? null : 32000})}/> 不设每任务 token 上限</label>
      {config.max_tokens !== null && <label>每任务 token 上限<input type="number" min={1000} max={200000} value={config.max_tokens} onChange={e=>setConfig({...config,max_tokens:Number(e.target.value)})}/></label>}
      {config.max_tokens === null && <small>仍记录实际用量，可以随时停止任务；模型执行时限和账号额度设置继续生效。</small>}
      <label>每任务模型执行时限（秒）<input type="number" min={30} max={3600} value={config.max_seconds} onChange={e=>setConfig({...config,max_seconds:Number(e.target.value)})}/></label>
      <details><summary>服务器配置</summary><label>Codex 可执行程序<input value={config.executable} onChange={e=>setConfig({...config,executable:e.target.value})}/></label><p>已验证版本：{config.version||'未连接'}</p></details>
      <button disabled={busy}>保存服务设置</button></form>
      <p>{state?.dispatch_reason||'新任务将在检查账号额度后派发。'}</p>
      {!buckets.length?<p>账号剩余额度：未知</p>:buckets.map((b,i)=><p key={i}>{b.limitId||'账号'}：{[b.primary,b.secondary].filter(Boolean).map(w=>`${w!.windowDurationMins} 分钟窗口剩余 ${Math.max(0,100-w!.usedPercent)}%`).join('；')}</p>)}
      <small>额度保留线只控制新任务，不会中断已经开始的请求。其他客户端也会消耗此账号；token 上限根据服务返回的用量执行，正在生成的响应可能超过阈值。</small>
    </section>}
    <UsageSummary/>
    <section className={styles.panel}><h2>平台任务</h2><button disabled={busy} onClick={()=>void action(()=>refresh())}>刷新任务</button><p>这里仅统计平台发起的任务，与账号总额度分开展示；未知用量不作推算。</p>
      {!state?.jobs?.length?<p>还没有官方智能体任务。</p>:<div style={{overflowX:'auto'}}><table><thead><tr><th>项目</th><th>类型</th><th>状态</th><th>等待</th><th>执行</th><th>token</th><th>说明</th></tr></thead><tbody>{state.jobs.map(j=><tr key={j.id}>
        <td><Link href={'/projects/'+j.project_id}>{j.project_id.slice(0,8)}</Link></td><td>{j.kind==='chat'?'对话':'生成工作流'}</td><td>{statusLabels[j.status]||j.status}</td><td>{j.status==='queued'?'排队中':Math.round(j.queue_seconds)+' 秒'}</td><td>{j.status==='running'?'执行中':Math.round(j.execution_seconds)+' 秒'}</td><td>{j.tokens??'未知'}</td><td>{j.error}{j.kind==='chat'&&['queued','running','waiting'].includes(j.status)&&<button disabled={busy} onClick={()=>void action(async()=>{await api(base+'/jobs/'+j.id+'/stop',{method:'POST'});await refresh()})}>停止任务</button>}</td></tr>)}</tbody></table></div>}
    </section>
  </main>
}
