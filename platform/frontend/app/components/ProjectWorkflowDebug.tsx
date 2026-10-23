'use client'
import {useCallback,useEffect,useRef,useState} from 'react'
import {api} from '@/lib/platform'
import {clientId} from '@/lib/client-id'
import StructuredValueEditor,{type Value} from './StructuredValueEditor'
import styles from './project-debug.module.css'

type Binding={service_id:string;scenario:string}
type Workflow={id:string;name:string;description:string;revision:number;inputs:{name:string;label?:string;type:string;default?:Value}[];outputs:string[];services:string[];calls:{nested?:boolean;node_id:string;workflow_id:string;inputs:Record<string,Value>}[]}
type Simulator={id:string;revision:number;name:string;description:string;implementation:'records'|'python';code:string;allowed_paths:string[];allowed_methods:string[];idempotency_header:string;initial_state:Record<string,Value>;scenarios:Record<string,Record<string,Value>>}
type Source={kind:'synthetic'|'upload'|'run';label?:string;task_id?:string;run_id?:string;path?:(string|number)[];input_name?:string}
type Case={id:string;name:string;requirement:string;inputs:Record<string,Value>;debug?:{bindings?:Record<string,Binding>;source?:Source}}
type Run={id:string;application_id:string;status:string;inputs:Record<string,Value>;outputs:Record<string,Value>;error?:string}
type Task={id:string;workflow_id:string;status:string;purpose:string;message:string;error:string;inputs:Record<string,Value>;outputs:Record<string,Value>;runs?:Run[];services?:Record<string,{state:Value;requests:Value[]}>;outdated?:boolean;environment?:string;versions?:Record<string,number>;service_versions?:Record<string,{name:string;revision:number;scenario:string}>;created_at:string;updated_at:string}
type Progress={revision:number;value:{goal:string;summary:string;platform_scope?:string;external_scope?:string;open_questions?:string;completion?:string;[key:string]:unknown}}
type Overview={workflows:Workflow[];progress:Progress;tasks:Task[]}
type Step={workflow_id:string;inputs:Record<string,Value>}
const statusNames:Record<string,string>={running:'运行中',queued:'等待开始',succeeded:'执行成功',failed:'未完成',interrupted:'已中断',waiting_input:'等待输入'}
const newService=():Omit<Simulator,'id'|'revision'>=>({name:'记录模拟服务',description:'分页读取与状态修改',implementation:'records',code:'',allowed_paths:['/**'],allowed_methods:['GET','POST'],idempotency_header:'Idempotency-Key',initial_state:{records:[{id:1,value:5},{id:2,value:8}],page_size:3},scenarios:{}})
function paths(value:Value,prefix:(string|number)[]=[]):{label:string;path:(string|number)[]}[]{
  const rows=[{label:prefix.join('.')||'完整输出',path:prefix}]
  if(prefix.length<5&&value&&typeof value==='object')for(const [key,child] of Object.entries(value))rows.push(...paths(child,[...prefix,Array.isArray(value)?Number(key):key]))
  return rows.slice(0,150)
}
function aliases(workflows:Workflow[],id:string,seen=new Set<string>()):string[]{
  if(seen.has(id))return[];seen.add(id)
  const flow=workflows.find(w=>w.id===id)
  return Array.from(new Set([...(flow?.services||[]),...(flow?.calls.flatMap(c=>aliases(workflows,c.workflow_id,seen))||[])]))
}

function remapReferences(value:Value,names:Record<string,string>):Value{
  if(Array.isArray(value))return value.map(v=>remapReferences(v,names))
  if(!value||typeof value!=='object')return value
  if('$ref' in value&&value.$ref&&typeof value.$ref==='object'&&!Array.isArray(value.$ref)){
    const ref=value.$ref;return {...value,$ref:{...ref,node_id:names[String(ref.node_id)]??ref.node_id}}
  }
  return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,remapReferences(v,names)]))
}
function chainSteps(calls:Workflow['calls']):Step[]{
  const names=Object.fromEntries(calls.map((c,i)=>[c.node_id,'step_'+(i+1)]))
  return calls.map(c=>({workflow_id:c.workflow_id,inputs:remapReferences(c.inputs,names) as Record<string,Value>}))
}

export default function ProjectWorkflowDebug({projectId,selectedId,onSelect,onEdit,onChanged,onTalk}: {
  projectId:string;selectedId:string;onSelect:(id:string)=>void;onEdit:(id:string)=>void;onChanged:()=>unknown;onTalk:(message:string)=>void
}) {
  const base='/api/v1/projects/'+projectId
  const [overview,setOverview]=useState<Overview>()
  const [progress,setProgress]=useState<Progress>()
  const [services,setServices]=useState<Simulator[]>([])
  const [cases,setCases]=useState<Case[]>([])
  const [caseWorkflowId,setCaseWorkflowId]=useState('')
  const [revision,setRevision]=useState(0)
  const [caseId,setCaseId]=useState('')
  const [caseName,setCaseName]=useState('正常输入')
  const [inputs,setInputs]=useState<Record<string,Value>>({})
  const [bindings,setBindings]=useState<Record<string,Binding>>({})
  const [source,setSource]=useState<Source>({kind:'synthetic'})
  const [task,setTask]=useState<Task>()
  const [error,setError]=useState('')
  const [notice,setNotice]=useState('')
  const [busy,setBusy]=useState(false)
  const pending=useRef(false)
  const pendingCase=useRef<{workflowId:string;caseId:string}|undefined>(undefined)
  const [pane,setPane]=useState<'debug'|'chain'|'services'>('debug')
  const [editService,setEditService]=useState<Omit<Simulator,'id'|'revision'>>(newService)
  const [serviceId,setServiceId]=useState('')
  const [serviceRevision,setServiceRevision]=useState(0)
  const [steps,setSteps]=useState<Step[]>([])
  const [downstream,setDownstream]=useState('')
  const [outputPath,setOutputPath]=useState('[]')
  const [inputName,setInputName]=useState('')
  const [connections,setConnections]=useState<{id:string;name:string}[]>([])
  const [realBindings,setRealBindings]=useState<Record<string,string>>({})
  const [runKind,setRunKind]=useState<'simulation'|'real'>('simulation')
  const workflow=overview?.workflows.find(w=>w.id===selectedId)
  const needed=aliases(overview?.workflows||[],selectedId)
  useEffect(()=>{setDownstream('');setOutputPath('[]');setInputName('')},[task?.id,selectedId])
  const load=useCallback(async()=>{
    const [next,ss,cs]=await Promise.all([api<Overview>(base+'/debug/overview'),api<Simulator[]>(base+'/simulators'),api<{id:string;name:string}[]>(base+'/http-connections')])
    setOverview(next);setServices(ss);setConnections(cs);return next
  },[base])
  useEffect(()=>{let live=true;void load().then(next=>{if(live){setProgress(next.progress);setSteps(chainSteps((next.workflows.find(w=>w.id===projectId)?.calls||[]).filter(c=>!c.nested)))}}).catch(e=>setError(String(e)));return()=>{live=false}},[load,projectId])
  useEffect(()=>{
    let live=true;setCaseWorkflowId('');setTask(undefined);setCaseId('');setSource({kind:'synthetic'});setBindings({});setInputs({});setNotice('')
    if(!workflow){setCases([]);return()=>{live=false}}
    void api<{revision:number;cases:Case[]}>(base+'/members/'+selectedId+'/debug-cases').then(next=>{
      if(!live)return;setCaseWorkflowId(selectedId);setRevision(next.revision);setCases(next.cases)
      const first=next.cases.find(c=>pendingCase.current?.workflowId===selectedId&&pendingCase.current.caseId===c.id)||next.cases[0];
      if(pendingCase.current?.workflowId===selectedId)pendingCase.current=undefined;
      if(first){setCaseId(first.id);setCaseName(first.name);setInputs(first.inputs);setBindings(first.debug?.bindings||{});setSource(first.debug?.source||{kind:'synthetic'})}
      else {setCaseName('正常输入');setInputs(Object.fromEntries((workflow?.inputs||[]).map(i=>[i.name,i.default??({number:0,boolean:false,object:{},array:[]}[i.type]??'')])))}
    }).catch(e=>{if(live)setError(String(e))});return()=>{live=false}
  },[base,selectedId,workflow?.id])
  useEffect(()=>{
    if(!task||!['running','queued'].includes(task.status))return
    let live=true
    const timer=setInterval(()=>{void api<Task>(base+(runKind==='simulation'?'/debug/runs/':'/tasks/')+task.id).then(next=>{if(live){setTask(next);if(!['running','queued'].includes(next.status))void load()}}).catch(e=>{if(live)setError(String(e))})},700)
    return()=>{live=false;clearInterval(timer)}
  },[base,task?.id,task?.status,runKind,load])
  async function act(fn:()=>Promise<unknown>){if(pending.current)return;pending.current=true;setBusy(true);setError('');setNotice('');try{await fn()}catch(e){setError(String(e))}finally{pending.current=false;setBusy(false)}}
  async function refreshResult(){
    if(!task||runKind!=='simulation')return
    const next=await api<Task>(base+'/debug/runs/'+task.id)
    setTask(current=>current?.id===next.id?next:current)
  }
  async function saveCase(refresh=true){
    if(caseWorkflowId!==selectedId)throw new Error('正在加载此流程的输入样例，请稍候')
    const saved=await api<{case:Case;revision:number}>(base+'/members/'+selectedId+'/debug-cases',{method:'PUT',body:JSON.stringify({id:caseId,name:caseName,inputs,bindings,source,expected_revision:revision})})
    setCaseId(saved.case.id);setRevision(saved.revision);setCases(current=>[...current.filter(c=>c.id!==saved.case.id),saved.case]);setNotice('样例及服务绑定已保存');await load();if(refresh)await refreshResult();return saved.case.id
  }
  async function start(recovery=''){
    setTask(undefined);setRunKind('simulation');const id=await saveCase(false)
    const next=await api<Task>(base+'/debug/runs',{method:'POST',body:JSON.stringify({workflow_id:selectedId,case_id:id,request_key:clientId(),recovery_task_id:recovery})})
    setTask(next);await load()
  }
  function selectCase(id:string){const item=cases.find(c=>c.id===id);setCaseId(id);setTask(undefined);setCaseName(item?.name||'另一组输入');setInputs(item?.inputs||{});setBindings(item?.debug?.bindings||{});setSource(item?.debug?.source||{kind:'synthetic'})}
  async function upload(file:File){
    if(file.size>5_000_000)throw new Error('样例文件请小于 5 MB')
    let data:Value
    if(file.name.toLowerCase().endsWith('.csv')){const Papa=(await import('papaparse')).default;const parsed=Papa.parse<Record<string,string>>(await file.text(),{header:true,skipEmptyLines:false});if(parsed.errors.length)throw new Error(parsed.errors[0].message);data=parsed.data}
    else data=JSON.parse(await file.text()) as Value
    if(Array.isArray(data)){const target=workflow?.inputs.find(i=>i.type==='array')?.name;if(!target)throw new Error('当前工作流没有列表输入，请上传包含输入字段名的样例');setInputs({...inputs,[target]:data})}
    else if(data&&typeof data==='object')setInputs(data)
    else throw new Error('样例应包含输入字段或表格行')
    setSource({kind:'upload',label:file.name});setCaseId('');setCaseName(file.name)
  }
  async function useOutput(){
    if(!task||!downstream)return
    const current=await api<{revision:number}>(base+'/members/'+downstream+'/debug-cases')
    const saved=await api<{case:Case}>(base+'/members/'+downstream+'/debug-cases',{method:'PUT',body:JSON.stringify({name:'来自 '+(workflow?.name||'上游')+' 的实际输出',expected_revision:current.revision,source:{kind:'run',task_id:task.id,path:JSON.parse(outputPath),input_name:inputName},bindings:{}})})
    pendingCase.current={workflowId:downstream,caseId:saved.case.id};onSelect(downstream);setPane('debug');setNotice('已固定上游输出，保存为下游样例 '+saved.case.name)
  }
  const chooseService=(service:Simulator)=>{const {id,revision,...spec}=service;setServiceId(id);setServiceRevision(revision);setEditService(spec)}
  return <div className={styles.root} aria-label="方案与调试">
    <header className={styles.header}><div><h2>方案与调试</h2><p>先把每条流程调通，再把实际结果接起来。任何阶段都可以保存和修改。</p><div className={styles.path}>{['问题与边界','拆分工作流','准备样例与模拟服务','单独调试','串联验证','接入真实服务'].map(s=><span key={s}>{s}</span>)}</div></div><button onClick={()=>onTalk('请按分析问题与边界、拆分工作流、准备模拟服务及输入、独立调试、串联验证的路径完成本项目。使用 project_services 和 project_debug 保存并运行，展示实际结果；不调用真实服务。')}>让助手协助搭建</button></header>
    {error&&<p className={styles.error} role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
    <details className={styles.panel}><summary>问题与能力边界 · {progress?.value.goal||'待补充业务目标'}</summary>{progress&&<>
      {([['goal','业务目标'],['completion','完成条件'],['platform_scope','平台负责什么'],['external_scope','外部服务负责什么'],['open_questions','待明确事项']] as const).map(([key,label])=><label key={key}>{label}<textarea value={String(progress.value[key]||'')} onChange={e=>setProgress({...progress,value:{...progress.value,[key]:e.target.value}})}/></label>)}
      <button disabled={busy} onClick={()=>void act(async()=>{const saved=await api<Progress>(base+'/progress',{method:'PUT',body:JSON.stringify({expected_revision:progress.revision,value:progress.value})});setProgress(saved);setNotice('目标与边界已保存')})}>保存目标与边界</button>
    </>}</details>
    <div className={styles.grid}><aside className={styles.panel}><h3>项目工作流</h3>{overview?.workflows.map(w=><article key={w.id} className={styles.card} data-selected={w.id===selectedId}>
      <strong>{w.id===projectId?'主流程 · ':''}{w.name}</strong><p>{w.description||'打开画布补充用途与步骤'}</p><p>输入：{w.inputs.map(i=>i.label||i.name).join('、')||'无需填写'}<br/>输出：{w.outputs.join('、')||'待声明'}</p><p>服务：{aliases(overview.workflows,w.id).join('、')||'无外部服务'}</p>
      {!!w.calls.length&&<p>成员调用：{w.calls.map(c=>overview.workflows.find(x=>x.id===c.workflow_id)?.name||c.workflow_id).join(' → ')}</p>}
      {!!overview?.workflows.some(x=>x.calls.some(c=>c.workflow_id===w.id))&&<p className={styles.muted}>被 {overview.workflows.filter(x=>x.calls.some(c=>c.workflow_id===w.id)).map(x=>x.name).join('、')} 调用</p>}
      <div className={styles.row}><button onClick={()=>{onSelect(w.id);setPane('debug')}}>{w.id===projectId?'调试整条链':'单独调试'}</button><button onClick={()=>onEdit(w.id)}>编辑</button><button onClick={()=>{onSelect(w.id);setPane('debug');const recent=overview.tasks.find(t=>t.workflow_id===w.id);if(recent)void api<Task>(base+'/debug/runs/'+recent.id).then(next=>{setRunKind('simulation');setTask(next)}).catch(e=>setError(String(e)))}}>查看结果</button></div>
    </article>)}</aside><div>
      <nav className={styles.row} aria-label="调试操作"><button aria-pressed={pane==='debug'} onClick={()=>setPane('debug')}>单独调试</button><button aria-pressed={pane==='chain'} onClick={()=>setPane('chain')}>串联验证</button><button aria-pressed={pane==='services'} onClick={()=>setPane('services')}>模拟服务</button></nav>
      {pane==='debug'&&<section className={styles.panel}><h3>{workflow?.name} · {selectedId===projectId?'整条链':'独立调试'}</h3><p className={styles.muted}>模拟运行只使用下方绑定，不访问真实服务。下游可以直接使用样例输入。</p>
        <label>已保存样例<select disabled={busy||caseWorkflowId!==selectedId} value={caseId} onChange={e=>selectCase(e.target.value)}><option value="">新建样例</option>{cases.map(c=><option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
        <label>样例名称<input value={caseName} onChange={e=>setCaseName(e.target.value)}/></label><p className={styles.muted}>来源：{source.kind==='run'?'已固定的历史运行输出':source.kind==='upload'?'上传文件':'合成输入'}{source.label?' · '+source.label:''}</p>
        <label>上传样例（JSON 或 CSV）<input type="file" accept=".json,.csv" onChange={e=>{const file=e.target.files?.[0];if(file)void act(()=>upload(file));e.target.value=''}}/></label>
        <StructuredValueEditor label="工作流输入" value={inputs} onChange={v=>{if(v&&typeof v==='object'&&!Array.isArray(v)){setInputs(v);if(source.kind==='run')setSource({kind:'synthetic',label:'基于历史输出编辑'})}}}/>
        {needed.map(alias=><div className={styles.step} key={alias}><label>{alias} · 模拟服务<select aria-label={alias+'模拟服务'} value={bindings[alias]?.service_id||''} onChange={e=>setBindings({...bindings,[alias]:{service_id:e.target.value,scenario:''}})}><option value="">请选择服务</option>{services.map(s=><option key={s.id} value={s.id}>{s.name} · r{s.revision}</option>)}</select></label><label>场景<select value={bindings[alias]?.scenario||''} onChange={e=>setBindings({...bindings,[alias]:{...bindings[alias],scenario:e.target.value}})}><option value="">初始状态</option>{Object.keys(services.find(s=>s.id===bindings[alias]?.service_id)?.scenarios||{}).map(s=><option key={s}>{s}</option>)}</select></label></div>)}
        {!!needed.length&&!services.length&&<p>尚无模拟服务。<button onClick={()=>setPane('services')}>添加预设或自写实现</button></p>}
        <div className={styles.row}><button disabled={busy||caseWorkflowId!==selectedId||!caseName.trim()} onClick={()=>void act(saveCase)}>保存样例</button><button className={styles.primary} disabled={busy||caseWorkflowId!==selectedId||!caseName.trim()} onClick={()=>void act(()=>start())}>{selectedId===projectId?'运行整条链（模拟）':'运行此流程（模拟）'}</button></div>
        <details><summary>配置真实连接</summary><p>真实运行只调用本项目已配置的连接。<a href={'/projects/'+projectId+'?tab=settings'}>前往服务连接设置</a></p>{needed.map(alias=><label key={alias}>{alias}<select value={realBindings[alias]||''} onChange={e=>setRealBindings({...realBindings,[alias]:e.target.value})}><option value="">请选择真实连接</option>{connections.map(c=><option key={c.id} value={c.id}>{c.name}</option>)}</select></label>)}<button disabled={busy||caseWorkflowId!==selectedId||needed.some(a=>!realBindings[a])} onClick={()=>void act(async()=>{setRunKind('real');setTask(undefined);setTask(await api<Task>(base+'/tasks',{method:'POST',body:JSON.stringify({request_key:clientId(),workflow_id:selectedId,inputs,service_bindings:realBindings})}))})}>使用真实连接运行</button></details>
      </section>}
      {pane==='services'&&<section className={styles.panel}><h3>模拟服务</h3><p className={styles.muted}>预设和自写实现使用相同接口。服务状态只在模拟会话内变化。</p><div className={styles.row}>{services.map(s=><button key={s.id} onClick={()=>chooseService(s)}>{s.name} · r{s.revision}</button>)}<button onClick={()=>{setServiceId('');setServiceRevision(0);setEditService(newService())}}>新增模拟服务</button></div>
        <label>服务名称<input value={editService.name} onChange={e=>setEditService({...editService,name:e.target.value})}/></label><label>接口说明<textarea value={editService.description} onChange={e=>setEditService({...editService,description:e.target.value})}/></label>
        <label>实现方式<select value={editService.implementation} onChange={e=>setEditService({...editService,implementation:e.target.value as 'records'|'python'})}><option value="records">预设：分页记录与状态修改</option><option value="python">自写 Python 模拟实现</option></select></label>
        {editService.implementation==='records'?<p>GET /records 分页；GET /records/编号 查询；POST /update、/delete 修改。records 为初始记录，page_size 为每页条数。</p>:<><p>handle(request, state, context) 返回 response、state，可用 fault="disconnect" 模拟响应丢失。由平台在无网络沙箱运行。</p><label>实现代码<textarea value={editService.code} onChange={e=>setEditService({...editService,code:e.target.value})} rows={12}/></label></>}
        <label>允许路径（每行一项）<textarea value={editService.allowed_paths.join('\n')} onChange={e=>setEditService({...editService,allowed_paths:e.target.value.split('\n')})}/></label><div className={styles.row}>{['GET','HEAD','POST','PUT','PATCH','DELETE'].map(method=><label key={method}><input type="checkbox" checked={editService.allowed_methods.includes(method)} onChange={e=>setEditService({...editService,allowed_methods:e.target.checked?[...editService.allowed_methods,method]:editService.allowed_methods.filter(m=>m!==method)})}/>{method}</label>)}</div>
        <label>幂等请求头（实现确实支持时填写）<input value={editService.idempotency_header} onChange={e=>setEditService({...editService,idempotency_header:e.target.value})}/></label>
        <StructuredValueEditor label="初始状态" value={editService.initial_state} onChange={v=>{if(v&&typeof v==='object'&&!Array.isArray(v))setEditService({...editService,initial_state:v})}}/>
        <details><summary>场景：覆盖初始状态中的字段</summary><StructuredValueEditor label="场景" value={editService.scenarios} onChange={v=>{if(v&&typeof v==='object'&&!Array.isArray(v))setEditService({...editService,scenarios:v as Simulator['scenarios']})}}/></details>
        <button className={styles.primary} disabled={busy||!editService.name.trim()} onClick={()=>void act(async()=>{const next=await api<Simulator>(base+'/simulators'+(serviceId?'/'+serviceId:''),{method:serviceId?'PUT':'POST',body:JSON.stringify({...editService,expected_revision:serviceRevision})});chooseService(next);await load();await refreshResult();setNotice('模拟服务已保存，可在单独调试中选择并绑定')})}>保存模拟服务</button>
      </section>}
      {pane==='chain'&&<section className={styles.panel}><h3>串联验证</h3><p>以下配置会保存为主流程的普通调用节点。下游取本次上游的实际输出；独立样例仍可保留。</p>
        {steps.map((step,i)=><div className={styles.step} key={i}><label>第 {i+1} 步<select value={step.workflow_id} onChange={e=>setSteps(steps.map((s,j)=>j===i?{workflow_id:e.target.value,inputs:{}}:s))}><option value="">选择工作流</option>{overview?.workflows.filter(w=>w.id!==projectId).map(w=><option key={w.id} value={w.id}>{w.name}</option>)}</select></label>
          {overview?.workflows.find(w=>w.id===step.workflow_id)?.inputs.map(field=>{const val=step.inputs[field.name];const selector=val&&typeof val==='object'&&!Array.isArray(val)&&'$ref' in val?val.$ref as {node_id:string;path:Value[]}:undefined;return <div key={field.name}><label>{field.label||field.name} · 来源<select value={selector?.node_id||''} onChange={e=>setSteps(steps.map((s,j)=>j===i?{...s,inputs:{...s.inputs,[field.name]:e.target.value?{$ref:{node_id:e.target.value,path:['output']}}:''}}:s))}><option value="">固定输入</option>{selector&&!steps.slice(0,i).some((_,j)=>selector.node_id==='step_'+(j+1))&&<option value={selector.node_id}>来源已移除，请重新选择</option>}{steps.slice(0,i).map((s,j)=><option value={'step_'+(j+1)} key={j}>第 {j+1} 步 · {overview?.workflows.find(w=>w.id===s.workflow_id)?.name}</option>)}</select></label>{selector?<label>输出字段<select value={String(selector.path[1]||'')} onChange={e=>setSteps(steps.map((s,j)=>j===i?{...s,inputs:{...s.inputs,[field.name]:{$ref:{node_id:selector.node_id,path:['output',...(e.target.value?[e.target.value]:[])]}}}}:s))}><option value="">完整输出</option>{overview?.workflows.find(w=>w.id===steps[Number(selector.node_id.replace('step_',''))-1]?.workflow_id)?.outputs.map(key=><option key={key}>{key}</option>)}</select></label>:<StructuredValueEditor label={field.label||field.name} value={val??field.default??''} onChange={v=>setSteps(steps.map((s,j)=>j===i?{...s,inputs:{...s.inputs,[field.name]:v}}:s))}/>}</div>})}
          <button onClick={()=>{const names=Object.fromEntries(steps.map((_,j)=>['step_'+(j+1),j===i?'missing_step_'+(j+1):'step_'+(j<i?j+1:j)]));setSteps(steps.filter((_,j)=>j!==i).map(s=>({...s,inputs:remapReferences(s.inputs,names) as Record<string,Value>})))}}>移除此步</button></div>)}
        <div className={styles.row}><button onClick={()=>setSteps([...steps,{workflow_id:'',inputs:{}}])}>添加流程</button><button disabled={busy||!steps.length||steps.some(s=>!s.workflow_id)} onClick={()=>void act(async()=>{const main=overview?.workflows.find(w=>w.id===projectId);await api(base+'/debug/chain',{method:'PUT',body:JSON.stringify({expected_revision:main?.revision,steps})});await load();await refreshResult();void onChanged();onSelect(projectId);setPane('debug');setNotice('串联主流程已保存，请选择模拟服务后运行整条链')})}>保存到主流程</button></div>
        <button onClick={()=>{onSelect(projectId);setPane('debug')}}>选择主流程并运行整条链</button>
      </section>}
      {task&&<section className={styles.panel} aria-label="本次调试结果"><div className={styles.header}><h3>本次结果 · {statusNames[task.status]||task.status}</h3><span className={styles.badge}>{runKind==='simulation'?'模拟调试':'真实服务运行'}</span></div><p className={styles.muted}>任务 {task.id}{task.created_at&&task.updated_at?' · '+Math.max(0,(Date.parse(task.updated_at)-Date.parse(task.created_at))/1000).toFixed(1)+' 秒':''}</p>
        {task.versions&&<p className={styles.muted}>本次流程版本：{Object.entries(task.versions).map(([id,revision])=>(overview?.workflows.find(w=>w.id===id)?.name||id)+' r'+revision).join('；')}</p>}{task.service_versions&&<p className={styles.muted}>本次模拟服务：{Object.values(task.service_versions).map(s=>s.name+' r'+s.revision+' · '+(s.scenario||'初始状态')).join('；')}</p>}
        {task.outdated&&<p>此结果对应旧配置。历史结果已保留，当前修改需要重新调试。</p>}{task.error&&<p className={styles.error}>{task.error}</p>}<pre className={styles.output}>{JSON.stringify(task.outputs,null,2)}</pre>
        {runKind==='simulation'&&['failed','interrupted'].includes(task.status)&&<div className={styles.row}><button disabled={busy} onClick={()=>void act(async()=>setTask(await api<Task>(base+'/debug/runs/'+task.id+'/resume',{method:'POST'})))}>继续原任务</button><button disabled={busy} onClick={()=>void act(()=>start(task.id))}>修改后修复（保留原状态）</button><button disabled={busy} onClick={()=>void act(()=>start())}>重置并重跑</button></div>}
        {task.runs?.map(run=><details key={run.id}><summary>{overview?.workflows.find(w=>w.id===run.application_id)?.name} · {statusNames[run.status]||run.status}</summary><p>实际输入</p><pre className={styles.output}>{JSON.stringify(run.inputs,null,2)}</pre><p>实际输出</p><pre className={styles.output}>{JSON.stringify(run.outputs,null,2)}</pre>{run.error&&<p>{run.error}</p>}</details>)}
        {task.services&&Object.entries(task.services).map(([alias,data])=><details key={alias}><summary>{alias} · {data.requests.length} 次 HTTP 请求与服务状态</summary><pre className={styles.output}>{JSON.stringify(data,null,2)}</pre></details>)}
        {task.status==='succeeded'&&<details open><summary>使用此输出调试下游</summary><label>下游工作流<select value={downstream} onChange={e=>{setDownstream(e.target.value);setInputName('')}}><option value="">请选择</option>{overview?.workflows.filter(w=>w.id!==selectedId).map(w=><option key={w.id} value={w.id}>{w.name}</option>)}</select></label><label>取用输出<select value={outputPath} onChange={e=>setOutputPath(e.target.value)}>{paths(task.outputs).map(p=><option key={p.label} value={JSON.stringify(p.path)}>{p.label}</option>)}</select></label><label>填入下游输入<select value={inputName} onChange={e=>setInputName(e.target.value)}><option value="">将完整对象作为全部输入</option>{overview?.workflows.find(w=>w.id===downstream)?.inputs.map(i=><option key={i.name} value={i.name}>{i.label||i.name}</option>)}</select></label><button disabled={busy||!downstream} onClick={()=>void act(useOutput)}>保存为下游样例</button></details>}
      </section>}
      <section className={styles.panel}><h3>历史模拟运行</h3>{overview?.tasks.filter(t=>t.workflow_id===selectedId).slice(0,20).map(t=><div className={styles.row} key={t.id}><button onClick={()=>void act(async()=>{setRunKind('simulation');setTask(await api<Task>(base+'/debug/runs/'+t.id))})}>{t.message||'模拟运行'} · {statusNames[t.status]||t.status} · {t.id.slice(0,8)}</button></div>)}{!overview?.tasks.some(t=>t.workflow_id===selectedId)&&<p>尚未运行。单独调试通过后，仍需验证整条链。</p>}</section>
    </div></div>
  </div>
}
