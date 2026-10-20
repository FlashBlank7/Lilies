'use client'
import {useEffect,useState} from 'react'
import {api} from '@/lib/platform'
import ReadingDialog from './ReadingDialog'
import styles from './workspace-tools.module.css'
type Item={id:string;name:string;description:string;limitations:string}
type Choice={id:string;name:string;display_name?:string;description?:string}
type Skill={id:string;revision:number;references:Record<string,string>}
type SharedWorkflow={id:string;name:string;description:string;workflow:{nodes:{id:string;type:string;config:Record<string,unknown>}[]}}
type Preview={root?:string;workflows?:SharedWorkflow[];skill?:{name?:string;description?:string;references?:Record<string,string>}|null}

function SharePreview({value,description}:{value:Preview;description:string}) {
  const flows=value.workflows||[]
  const root=flows.find(flow=>flow.id===value.root)||flows[0]
  const children=flows.filter(flow=>flow!==root)
  const types=new Set(flows.flatMap(flow=>flow.workflow.nodes.map(node=>node.type)))
  const outputLabels:Record<string,string>={result:'结果与下载文件',markdown:'报告正文',profile:'数据体检结果',summary:'汇总报告结果'}
  return <section aria-label="分享预览"><h3>将分享的内容</h3>
    <p><strong>用途：</strong>{description||root?.description||value.skill?.description||'尚未填写用途说明。'}</p>
    {flows.map(flow=>{
      const inputs=flow.workflow.nodes.filter(node=>node.type==='start').flatMap(node=>Array.isArray(node.config.inputs)?node.config.inputs as {name:string;label?:string;description?:string;required?:boolean}[]:[])
      const outputs=flow.workflow.nodes.filter(node=>node.type==='end').flatMap(node=>Object.keys(node.config.outputs||{}))
      return <div key={flow.id}><h4>{flow===root?'入口流程':'关联子流程'} · {flow.name}</h4>
        {flow.description&&<p>{flow.description}</p>}
        <p><strong>输入：</strong>{inputs.length?inputs.map(field=>(field.label||field.description||field.name)+(field.required===false?'（可选）':'')).join('、'):'无需填写输入'}</p>
        <p><strong>输出：</strong>{outputs.length?outputs.map(name=>outputLabels[name]||name).join('、'):'尚未声明输出'}</p>
      </div>
    })}
    {value.skill&&<p><strong>引用文件：</strong>{Object.keys(value.skill.references||{}).join('、')||'未选择引用文件'}</p>}
    {!!flows.length&&<><p><strong>关联依赖：</strong>{children.length?children.map(flow=>flow.name).join('、')+'（一起复制）':'没有关联子流程'}</p>
      <p><strong>运行准备：</strong>{[types.has('code')?'Python 代码执行':'',types.has('llm')?'配置大模型连接':''].filter(Boolean).join('、')||'按流程输入准备资料'}；项目文件、模型和连接需在接收项目选择。</p></>}
    <details><summary>高级：查看完整定义（JSON、代码与提示词）</summary><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxHeight:360,overflow:'auto'}}>{JSON.stringify(value,null,2)}</pre></details>
  </section>
}
export default function SharedMethods({projectId,onChanged}:{projectId:string;onChanged:()=>unknown}) {
  const base=`/api/v1/projects/${projectId}`
  const [items,setItems]=useState<Item[]>([]),[open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('')
  const [projects,setProjects]=useState<Choice[]>([]),[workflows,setWorkflows]=useState<Choice[]>([]),[skills,setSkills]=useState<Choice[]>([])
  const [selected,setSelected]=useState(''),[targets,setTargets]=useState<string[]>([projectId]),[name,setName]=useState(''),[description,setDescription]=useState(''),[limitations,setLimitations]=useState(''),[preview,setPreview]=useState<Preview|null>(null)
  const [skill,setSkill]=useState<Skill|null>(null),[referenceNames,setReferenceNames]=useState<string[]>([])
  const selectedSkill=selected.startsWith('skill:')?selected.slice(6):''
  const referencesReady=!selectedSkill||skill?.id===selectedSkill
  async function refresh(){setItems(await api<Item[]>(base+'/space/shared-methods'))}
  useEffect(()=>{void refresh().catch(e=>setError(String(e)))},[base])
  useEffect(()=>{
    if(!open||!selectedSkill)return
    let current=true
    void api<Skill>(base+'/skills/'+encodeURIComponent(selectedSkill)).then(value=>{if(current)setSkill({...value,id:selectedSkill})}).catch(e=>{if(current)setError(String(e))})
    return ()=>{current=false}
  },[base,open,selectedSkill])
  async function begin(){setBusy(true);setError('');setSelected('');setSkill(null);setReferenceNames([]);setPreview(null);try{const [p,w,s]=await Promise.all([api<Choice[]>('/api/v1/projects'),api<{workflows:Choice[]}>(base+'/space'),api<Choice[]>(base+'/skills')]);setProjects(p);setWorkflows(w.workflows);setSkills(s);setOpen(true)}catch(e){setError(String(e))}finally{setBusy(false)}}
  function body(){const [type,...id]=selected.split(':');return {name,description,limitations,target_project_ids:targets,...(type==='workflow'?{workflow_id:id.join(':')}:{skill_id:id.join(':'),reference_names:referenceNames,skill_revision:skill?.revision})}}
  async function share(onlyPreview=false){setBusy(true);setError('');try{const result=await api<Preview>(base+'/space/shared-methods'+(onlyPreview?'/preview':''),{method:'POST',body:JSON.stringify(body())});if(onlyPreview)setPreview(result);else{setOpen(false);setNotice('已分享给所选项目。原项目及已安装副本保持独立。');await refresh()}}catch(e){setError(String(e))}finally{setBusy(false)}}
  async function install(item:Item){setBusy(true);setError('');try{await api(base+'/space/shared-methods/'+item.id+'/install',{method:'POST'});setNotice(`已加入「${item.name}」，可在工作流和项目说明中查看。`);void onChanged()}catch(e){setError(String(e))}finally{setBusy(false)}}
  return <section className={styles.section}><h2>内部共享方法与流程</h2><p>主动分享给指定项目；加入后得到独立可编辑副本，后续分享不会覆盖它。</p><button disabled={busy} onClick={()=>void begin()}>分享本项目的方法或流程</button>
    {!items.length&&<p>还没有分享给本项目的内容。</p>}{items.map(item=><div key={item.id} className={styles.row}><div><strong>{item.name}</strong><p>{item.description}</p>{item.limitations&&<small>适用限制：{item.limitations}</small>}</div><button disabled={busy} onClick={()=>void install(item)}>加入项目 · {item.name}</button></div>)}
    {notice&&<p role="status">{notice}</p>}{!open&&error&&<p role="alert">{error}</p>}
    {open&&<ReadingDialog title="分享方法或工作流" onClose={()=>setOpen(false)}><div className={styles.section}>
      <p>分享选定定义及关联子流程，不复制项目文件、模型包、连接和聊天。模型及数据绑定会清空；代码和提示词属于分享内容，请检查其中是否写有私人信息。方法说明可勾选必要的引用文件一起分享，默认不选。</p>
      <label>要分享的内容<select disabled={busy} value={selected} onChange={e=>{setSelected(e.target.value);setPreview(null);setError('');setSkill(null);setReferenceNames([]);const [kind,...ids]=e.target.value.split(':');const choice=(kind==='workflow'?workflows:skills).find(x=>x.id===ids.join(':'));setName(choice?.display_name||choice?.name||'');setDescription(choice?.description||'')}}><option value="">请选择</option><optgroup label="工作流">{workflows.map(x=><option key={x.id} value={'workflow:'+x.id}>{x.display_name||x.name}</option>)}</optgroup><optgroup label="方法说明">{skills.map(x=><option key={x.id} value={'skill:'+x.id}>{x.display_name||x.name}</option>)}</optgroup></select></label>
      {selectedSkill&&<fieldset><legend>一起分享的引用文件</legend>{!referencesReady?<p role="status">正在读取引用文件…</p>:!Object.keys(skill?.references||{}).length?<p>这篇方法没有引用文件。</p>:<><p>只复制勾选文件的本次版本。接收方可独立修改；未勾选的资料不会带入。</p>{Object.entries(skill?.references||{}).map(([file,content])=><label key={file} style={{overflowWrap:'anywhere'}}><input disabled={busy} type="checkbox" checked={referenceNames.includes(file)} onChange={e=>{setReferenceNames(e.target.checked?[...referenceNames,file]:referenceNames.filter(x=>x!==file));setPreview(null)}}/>{file}（{content.length.toLocaleString()} 字符）</label>)}</>}</fieldset>}
      <label>分享名称<input disabled={busy} value={name} maxLength={100} onChange={e=>{setName(e.target.value);setPreview(null)}}/></label><label>用途与输入输出<textarea disabled={busy} value={description} maxLength={1000} onChange={e=>{setDescription(e.target.value);setPreview(null)}}/></label><label>适用条件与限制<textarea disabled={busy} value={limitations} maxLength={2000} onChange={e=>{setLimitations(e.target.value);setPreview(null)}}/></label>
      <fieldset><legend>可以使用的项目</legend>{projects.map(p=><label key={p.id}><input type="checkbox" checked={targets.includes(p.id)} onChange={e=>setTargets(e.target.checked?[...targets,p.id]:targets.filter(x=>x!==p.id))}/>{p.name}</label>)}</fieldset>
      <div className={styles.row}><button disabled={busy||!selected||!name.trim()||!targets.length||!referencesReady} onClick={()=>void share(true)}>预览分享内容</button><button disabled={busy||!selected||!name.trim()||!targets.length||!referencesReady} onClick={()=>void share()}>分享给所选项目</button></div>
      {preview&&<SharePreview value={preview} description={description}/>} {error&&<p role="alert">{error}</p>}
    </div></ReadingDialog>}
  </section>
}
