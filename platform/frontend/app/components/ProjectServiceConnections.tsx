'use client'
import {useEffect,useState} from 'react'
import {api} from '@/lib/platform'

export type ServiceConnection={id:string;name:string;base_url:string;allowed_paths:string[];allowed_methods:string[];auth_header:string;has_credential:boolean;enabled:boolean;idempotency_header:string;revision:number}
const blank={name:'',base_url:'',allowed_paths:'/api/**',allowed_methods:['GET'],auth_header:'',credential:'',enabled:true,idempotency_header:''}
export default function ProjectServiceConnections({projectId,canManage}:{projectId:string;canManage:boolean}) {
  const [items,setItems]=useState<ServiceConnection[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false)
  const [editing,setEditing]=useState<ServiceConnection|null|undefined>(undefined),[form,setForm]=useState(blank)
  const base=`/api/v1/projects/${projectId}/http-connections`
  async function refresh(){setItems(await api<ServiceConnection[]>(base))}
  useEffect(()=>{let active=true;void api<ServiceConnection[]>(base).then(value=>{if(active)setItems(value)}).catch(cause=>{if(active)setError(String(cause))});return()=>{active=false}},[base])
  function edit(item:ServiceConnection|null){setEditing(item);setForm(item?{...item,allowed_paths:item.allowed_paths.join('\n'),credential:''}:blank);setError('')}
  async function save(event:React.FormEvent){event.preventDefault();if(busy)return;setBusy(true);setError('');try{
    await api(base+(editing?'/'+editing.id:''),{method:editing?'PUT':'POST',body:JSON.stringify({name:form.name,base_url:form.base_url,allowed_paths:form.allowed_paths.split('\n').map(s=>s.trim()).filter(Boolean),allowed_methods:form.allowed_methods,auth_header:form.auth_header,credential:form.credential||null,enabled:form.enabled,idempotency_header:form.idempotency_header,expected_revision:editing?.revision||0})})
    await refresh();setEditing(undefined);setForm(blank)
  }catch(cause){setError(String(cause))}finally{setBusy(false)}}
  return <section aria-label="服务连接"><h2>服务连接</h2><p>工作流通过这些连接调用服务。助手只能查看用途和允许范围，凭据由后端发送时填入。</p>
    {error&&<p role="alert">{error}</p>}
    {!items.length&&<p>尚未配置服务连接。可以先搭建和保存工作流。</p>}
    {items.map(item=><article key={item.id}><h3>{item.name} · {item.enabled?'已启用':'已停用'}</h3><p>{item.base_url}</p><p>{item.allowed_methods.join('、')} · {item.allowed_paths.join('、')}</p><p>{item.has_credential?'已配置凭据（不显示）':'无认证凭据'}{item.idempotency_header?' · 服务支持幂等请求':''}</p>{canManage&&<button onClick={()=>edit(item)}>编辑连接</button>}</article>)}
    {canManage&&editing===undefined&&<button onClick={()=>edit(null)}>添加服务连接</button>}
    {canManage&&editing!==undefined&&<form onSubmit={save}><fieldset disabled={busy}><legend>{editing?'编辑服务连接':'添加服务连接'}</legend>
      <label>连接名称<input required value={form.name} onChange={e=>setForm({...form,name:e.target.value})}/></label>
      <label>基础地址<input type="url" required placeholder="https://service.example/api" value={form.base_url} onChange={e=>setForm({...form,base_url:e.target.value})}/></label>
      <label>允许路径（每行一项）<textarea required value={form.allowed_paths} onChange={e=>setForm({...form,allowed_paths:e.target.value})}/></label><p>/items 为精确路径，/items/** 包含其下路径。相对于基础地址，不包括查询参数。</p>
      <fieldset><legend>允许的方法</legend>{['GET','HEAD','POST','PUT','PATCH','DELETE'].map(method=><label key={method}><input type="checkbox" checked={form.allowed_methods.includes(method)} onChange={e=>setForm({...form,allowed_methods:e.target.checked?[...form.allowed_methods,method]:form.allowed_methods.filter(m=>m!==method)})}/>{method}</label>)}</fieldset>
      <label>认证请求头<input placeholder="例如 Authorization 或 X-Api-Key" value={form.auth_header} onChange={e=>setForm({...form,auth_header:e.target.value})}/></label>
      <label>凭据值<input type="password" autoComplete="new-password" placeholder={editing?.has_credential?'留空保留原凭据':'例如 Bearer …'} value={form.credential} onChange={e=>setForm({...form,credential:e.target.value})}/></label>
      <label>服务支持的幂等请求头<input placeholder="仅服务明确支持时填写，例如 Idempotency-Key" value={form.idempotency_header} onChange={e=>setForm({...form,idempotency_header:e.target.value})}/></label><p>读取最多尝试三次。写入只有服务支持幂等，或结果查询确认未执行时才自动重发。不要为不支持的服务填写幂等请求头。</p>
      <label><input type="checkbox" checked={form.enabled} onChange={e=>setForm({...form,enabled:e.target.checked})}/>启用连接</label><p>停用立即阻止后续请求。修改地址或范围后需重新运行；只更新凭据可以继续原任务。</p>
      <button type="submit">{busy?'正在保存…':'保存连接'}</button><button type="button" onClick={()=>setEditing(undefined)}>取消</button>
    </fieldset></form>}
  </section>
}
