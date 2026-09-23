'use client'
import {useEffect,useState} from 'react'
import {api} from '@/lib/platform'
import ReadingDialog from './ReadingDialog'
import styles from './workspace-tools.module.css'
type Item={id:string;name:string;description:string;limitations:string}
type Choice={id:string;name:string}
export default function SharedMethods({projectId,onChanged}:{projectId:string;onChanged:()=>unknown}) {
  const base=`/api/v1/projects/${projectId}`
  const [items,setItems]=useState<Item[]>([]),[open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('')
  const [projects,setProjects]=useState<Choice[]>([]),[workflows,setWorkflows]=useState<Choice[]>([]),[skills,setSkills]=useState<Choice[]>([])
  const [selected,setSelected]=useState(''),[targets,setTargets]=useState<string[]>([projectId]),[name,setName]=useState(''),[description,setDescription]=useState(''),[limitations,setLimitations]=useState(''),[preview,setPreview]=useState('')
  async function refresh(){setItems(await api<Item[]>(base+'/space/shared-methods'))}
  useEffect(()=>{void refresh().catch(e=>setError(String(e)))},[base])
  async function begin(){setBusy(true);setError('');try{const [p,w,s]=await Promise.all([api<Choice[]>('/api/v1/projects'),api<{workflows:Choice[]}>(base+'/space'),api<Choice[]>(base+'/skills')]);setProjects(p);setWorkflows(w.workflows);setSkills(s);setOpen(true)}catch(e){setError(String(e))}finally{setBusy(false)}}
  function body(){const [type,...id]=selected.split(':');return {name,description,limitations,target_project_ids:targets,...(type==='workflow'?{workflow_id:id.join(':')}:{skill_id:id.join(':')})}}
  async function share(onlyPreview=false){setBusy(true);setError('');try{const result=await api(base+'/space/shared-methods'+(onlyPreview?'/preview':''),{method:'POST',body:JSON.stringify(body())});if(onlyPreview)setPreview(JSON.stringify(result,null,2));else{setOpen(false);setNotice('已分享给所选项目。原项目及已安装副本保持独立。');await refresh()}}catch(e){setError(String(e))}finally{setBusy(false)}}
  async function install(item:Item){setBusy(true);setError('');try{await api(base+'/space/shared-methods/'+item.id+'/install',{method:'POST'});setNotice(`已加入「${item.name}」，可在工作流和项目说明中查看。`);void onChanged()}catch(e){setError(String(e))}finally{setBusy(false)}}
  return <section className={styles.section}><h2>内部共享方法与流程</h2><p>主动分享给指定项目；加入后得到独立可编辑副本，后续分享不会覆盖它。</p><button disabled={busy} onClick={()=>void begin()}>分享本项目的方法或流程</button>
    {!items.length&&<p>还没有分享给本项目的内容。</p>}{items.map(item=><div key={item.id} className={styles.row}><div><strong>{item.name}</strong><p>{item.description}</p>{item.limitations&&<small>适用限制：{item.limitations}</small>}</div><button disabled={busy} onClick={()=>void install(item)}>加入项目 · {item.name}</button></div>)}
    {notice&&<p role="status">{notice}</p>}{!open&&error&&<p role="alert">{error}</p>}
    {open&&<ReadingDialog title="分享方法或工作流" onClose={()=>setOpen(false)}><div className={styles.section}>
      <p>分享选定定义及关联子流程，不复制项目文件、模型包、连接和聊天。模型及数据绑定会清空；代码和提示词属于分享内容，请检查其中是否写有私人信息。方法说明的引用资料不随分享复制。</p>
      <label>要分享的内容<select value={selected} onChange={e=>{setSelected(e.target.value);setPreview('');const [kind,id]=e.target.value.split(':');setName((kind==='workflow'?workflows:skills).find(x=>x.id===id)?.name||'')}}><option value="">请选择</option><optgroup label="工作流">{workflows.map(x=><option key={x.id} value={'workflow:'+x.id}>{x.name}</option>)}</optgroup><optgroup label="方法说明">{skills.map(x=><option key={x.id} value={'skill:'+x.id}>{x.name}</option>)}</optgroup></select></label>
      <label>分享名称<input value={name} maxLength={100} onChange={e=>setName(e.target.value)}/></label><label>用途与输入输出<textarea value={description} maxLength={1000} onChange={e=>setDescription(e.target.value)}/></label><label>适用条件与限制<textarea value={limitations} maxLength={2000} onChange={e=>setLimitations(e.target.value)}/></label>
      <fieldset><legend>可以使用的项目</legend>{projects.map(p=><label key={p.id}><input type="checkbox" checked={targets.includes(p.id)} onChange={e=>setTargets(e.target.checked?[...targets,p.id]:targets.filter(x=>x!==p.id))}/>{p.name}</label>)}</fieldset>
      <div className={styles.row}><button disabled={busy||!selected||!name.trim()||!targets.length} onClick={()=>void share(true)}>预览分享内容</button><button disabled={busy||!selected||!name.trim()||!targets.length} onClick={()=>void share()}>分享给所选项目</button></div>
      {preview&&<details open><summary>将分享的定义</summary><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{preview}</pre></details>}{error&&<p role="alert">{error}</p>}
    </div></ReadingDialog>}
  </section>
}
