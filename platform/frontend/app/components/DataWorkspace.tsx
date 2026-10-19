'use client'
import {useCallback,useEffect,useRef,useState} from 'react'
import {Database,FileText,Image as ImageIcon,Boxes,GripVertical,Upload,Play,ChevronDown,ChevronRight,Search} from 'lucide-react'
import {api,withFrontendToken} from '@/lib/platform'
import {clientId} from '@/lib/client-id'
import {MarkdownDocument} from '@/lib/markdown'
import {type DataObject,type Tool,type Mapping,type Selection,type TableView,type Operation,emptySelection,TOOL_MIME,DATA_MIME,statusNames,compatible} from '@/lib/data-workspace'
import WorkspaceTable from './WorkspaceTable'
import WorkspaceOperationDialog from './WorkspaceOperationDialog'
import DataAnalysisResults from './DataAnalysisResults'
import ClassificationResults,{type ClassificationResult} from './ClassificationResults'
import styles from './data-workspace.module.css'

type Catalog={tools:Tool[];workflows:Tool[];models?:DataObject[]}
type Listing={objects:DataObject[];operations:Operation[];has_more:boolean;offset:number;total:number}
type Context={object:DataObject;selection:Selection;mapping:Mapping;view?:TableView}
type Dialog=Context&{tool:Tool;initialParameters?:Record<string,unknown>;initialRequest?:Operation['request']}
const kindNames:Record<string,string>={table:'数据表',model:'模型',analysis:'分析结果',document:'文档与报告',image:'图片',file:'文件'}
const icon=(kind:string)=>kind==='table'?<Database size={20}/>:kind==='model'?<Boxes size={20}/>:kind==='image'?<ImageIcon size={20}/>:<FileText size={20}/>

function Heatmap({value}:{value:Record<string,unknown>}) {
  const pairs=value.pairs as {left:string;right:string;rho:number|null;samples:number}[]|undefined
  if(!pairs?.length)return null
  const columns=[...new Set(pairs.flatMap(p=>[p.left,p.right]))]
  return <details><summary>相关性热力图</summary><div className={styles.tableWrap}><table className={styles.heat}><thead><tr><th></th>{columns.map(c=><th key={c}>{c}</th>)}</tr></thead><tbody>{columns.map(a=><tr key={a}><th>{a}</th>{columns.map(b=>{const p=pairs.find(p=>(p.left===a&&p.right===b)||(p.left===b&&p.right===a)),rho=a===b?1:p?.rho;return <td key={b} title={p?`有效样本 ${p.samples}`:''} style={{background:rho==null?'#edf0ed':rho>=0?`rgba(54,135,96,${.1+Math.abs(rho)*.65})`:`rgba(198,103,70,${.1+Math.abs(rho)*.65})`}}>{rho==null?'—':rho.toFixed(2)}</td>})}</tr>)}</tbody></table></div></details>
}

const detailNames:Record<string,string>={rows:'样本行数',columns:'字段',name:'名称',dtype:'类型',missing:'缺失',unique:'不同取值数',mean:'均值',min:'最小值',max:'最大值',empty_rows:'整行为空',nonempty_rows:'非空行',duplicates:'重复行',evaluation:'评价设置',problem:'任务类型',metric:'评价指标',split:'划分方式',folds:'验证折数',holdout_fraction:'独立测试比例',budget:'计算预算',seconds:'总秒数',trials:'模型数量',trial_seconds:'单次秒数',candidate:'模型方案',models:'模型',features:'特征设置',preprocessing:'训练预处理',outliers:'异常处理',scaling:'数值缩放',lower:'下限',upper:'上限',handling:'处理方式',method:'方法',iqr_factor:'IQR 系数'}
function ResultDetails({value,depth=0}:{value:unknown;depth?:number}) {
  if(value==null||typeof value!=='object')return <span>{String(value??'—')}</span>
  if(depth>4)return <span>详细结构请查看原始输出文件。</span>
  if(Array.isArray(value))return <div>{value.slice(0,50).map((item,i)=><div key={i}><ResultDetails value={item} depth={depth+1}/></div>)}{value.length>50&&<p>仅展开前 50 项，完整数据可在结果文件中查看。</p>}</div>
  return <dl>{Object.entries(value).filter(([k])=>!['preview','artifacts','image'].includes(k)).map(([key,item])=><div key={key}><dt><strong>{detailNames[key]||key}</strong></dt><dd><ResultDetails value={item} depth={depth+1}/></dd></div>)}</dl>
}

function ObjectPreview({projectId,object}:{projectId:string;object:DataObject}) {
  const [data,setData]=useState<{text?:string;format?:string;truncated?:boolean;image?:string;page?:number;pages?:number}>(),[error,setError]=useState(''),[page,setPage]=useState(0)
  useEffect(()=>{let active=true;if(object.kind==='document')void api<typeof data>(`/api/v1/projects/${projectId}/data-workspace/preview`,{method:'POST',body:JSON.stringify({object_id:object.id,version:object.version,offset:page})}).then(v=>{if(active)setData(v)}).catch(e=>{if(active)setError(String(e))});return()=>{active=false}},[projectId,object.id,object.version,page])
  const url=withFrontendToken(`/api/platform/api/v1/projects/${projectId}/data-workspace/file?object_id=${encodeURIComponent(object.id)}&version=${encodeURIComponent(object.version)}`)
  if(object.kind==='model')return <><div className={styles.metric}>{Object.entries(object.metrics||{}).map(([key,value])=><div key={key}><small>{key}</small><strong>{value==null?'—':Number(value).toPrecision(4)}</strong></div>)}</div><p className={styles.muted}>输入特征：{object.features?.columns?.join('、')||'见训练记录'} · 模型版本 {object.version.slice(0,8)}</p>{object.classification&&<ClassificationResults result={object.classification as ClassificationResult}/>}<details><summary>模型固定的字段含义</summary>{Object.entries(object.mapping?.field_rules||{}).map(([field,rule])=><p key={field}>{field} · {rule.description||rule.dtype} {rule.unit} {Object.entries(rule.category_labels||{}).map(([k,v])=>`${k}=${v}`).join('，')}</p>)}</details>{object.test_result&&<p className={styles.notice}>独立测试 {object.test_result.rows} 行：{Object.entries(object.test_result.metrics||{}).map(([k,v])=>`${k}=${Number(v).toPrecision(4)}`).join(' · ')}</p>}{object.download&&<a href={withFrontendToken('/api/platform'+object.download)} download>下载模型与训练记录</a>}</>
  if(object.kind==='analysis')return object.value?.operation?<><DataAnalysisResults result={object.value as never}/><Heatmap value={object.value||{}}/></>:<div className={styles.document}><ResultDetails value={object.value}/></div>
  return <>
    {error&&<p role="alert" className={styles.error}>{error}</p>}
    {object.kind==='image'?<img src={url} alt={object.name} style={{maxWidth:'100%',maxHeight:500}}/>:object.format==='.pdf'?<>{data?.image?<img src={data.image} alt={`PDF 第 ${(data.page||0)+1} 页 ${object.name}`} style={{maxWidth:'100%',border:'1px solid #e0e8e2'}}/>:<p>正在渲染 PDF 页面…</p>}<div className={styles.toolbar}><button disabled={!page} onClick={()=>setPage(p=>p-1)}>上一页 PDF</button><span>第 {page+1} / {data?.pages||'…'} 页</span><button disabled={!data?.pages||page+1>=data.pages} onClick={()=>setPage(p=>p+1)}>下一页 PDF</button></div></>:data?.text!==undefined?['html','htm'].includes(data.format||'')?<iframe className={styles.previewFrame} title={`报告 ${object.name}`} sandbox="" srcDoc={`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">${data.text}`}/>:data.format==='md'?<MarkdownDocument source={data.text} emptyLabel="文档为空"/>:<pre className={styles.document}>{data.text}</pre>:object.kind==='document'?<p className={styles.muted}>正在读取文档…</p>:<p className={styles.muted}>此格式可作为文件传给兼容工作流，暂不支持内容预览。</p>}
    {data?.truncated&&<p className={styles.muted}>正文较长，当前显示前 2 MB，完整内容请下载。</p>}
    {object.path&&<p><a href={url+'&download=true'} download>下载 {object.name}</a></p>}
  </>
}

function ObjectCard({projectId,object,operations,tools,onChoose,onContext,onSave,onStop,initialOpen=false,ancestors=[]}:{projectId:string;object:DataObject;operations:Operation[];tools:Tool[];onChoose:(context:Context,tool:Tool,initialParameters?:Record<string,unknown>,initialRequest?:Operation['request'])=>void;onContext:(c:Context)=>void;onSave:(o:Operation)=>void;onStop:(o:Operation)=>void;initialOpen?:boolean;ancestors?:string[]}) {
  const [open,setOpen]=useState(initialOpen),[over,setOver]=useState(false),[selection,setSelection]=useState<Selection>(emptySelection),[mapping,setMapping]=useState<Mapping>(object.mapping||{}),[view,setView]=useState<TableView>(),[toolId,setToolId]=useState('')
  const mappingVersion=JSON.stringify(object.mapping||{})
  useEffect(()=>{setMapping(object.mapping||{})},[mappingVersion])
  const context={object,selection,mapping,view}
  const current=useRef(context);current.current=context
  useEffect(()=>{if(open)onContext(current.current)},[open,selection,mapping,view,onContext])
  const compatibleTools=tools.filter(t=>t.allowed!==false&&compatible(object,t))
  function choose(tool:Tool) {setOpen(true);onChoose(context,tool)}
  const children=operations.filter(o=>o.source.id===object.id&&!ancestors.includes(o.id))
  return <div>
    <article className={styles.card} data-over={over} aria-label={`${kindNames[object.kind]||'对象'} ${object.name}`} onDragOver={e=>{if(e.dataTransfer.types.includes(TOOL_MIME)){e.preventDefault();setOver(true)}}} onDragLeave={e=>{if(!e.currentTarget.contains(e.relatedTarget as Node))setOver(false)}} onDrop={e=>{e.preventDefault();e.stopPropagation();setOver(false);const tool=tools.find(t=>t.id===e.dataTransfer.getData(TOOL_MIME));if(tool)choose(tool)}}>
      <header className={styles.cardHead} draggable onDragStart={e=>{e.dataTransfer.setData(DATA_MIME,JSON.stringify({id:object.id,selection,mapping}));e.dataTransfer.effectAllowed='copy';onContext(context)}}>
        <div className={styles.objectName}><span className={styles.icon}>{icon(object.kind)}</span><div><strong>{object.name}</strong><span className={styles.tag}>{kindNames[object.kind]} · {object.bytes!=null?`${(object.bytes/1024).toFixed(1)} KB · `:''}{object.version.slice(0,8)}</span></div></div>
        <div className={styles.actions}><GripVertical size={15} color="#95a69a"/><button aria-expanded={open} aria-label={`${open?'收起':'查看'} ${object.name}`} onClick={()=>{setOpen(!open);onContext(context)}}>{open?<ChevronDown size={15}/>:<ChevronRight size={15}/>}</button></div>
      </header>
      {open&&<div className={styles.content}>{object.kind==='table'?<WorkspaceTable projectId={projectId} object={object} selection={selection} mapping={mapping} onSelection={setSelection} onMapping={setMapping} onView={setView}/>:<ObjectPreview projectId={projectId} object={object}/>}
        <div className={styles.toolbar}><select aria-label={`对 ${object.name} 使用工具`} className={styles.inlineSelect} value={toolId} onChange={e=>setToolId(e.target.value)}><option value="">选择工具或工作流</option>{compatibleTools.map(t=><option key={t.id} value={t.id}>{t.name}</option>)}</select><button disabled={!toolId} onClick={()=>{const tool=tools.find(t=>t.id===toolId);if(tool)choose(tool)}}><Play size={12}/> 配置处理</button><span className={styles.muted}>也可将左侧工具拖到此处</span></div>
      </div>}
    </article>
    {children.map(operation=><section className={styles.resultGroup} key={operation.id} aria-label={`处理 ${operation.name}`}>
      <header className={styles.resultHead}><div><h3>{operation.name} · {statusNames[operation.status]||operation.status}</h3><small>{new Date(operation.created_at).toLocaleString()} {operation.task&&`· ${Math.max(0,(Date.parse(operation.task.updated_at)-Date.parse(operation.task.created_at))/1000).toFixed(1)} 秒`}</small></div><div className={styles.actions}>{['queued','running','waiting_input'].includes(operation.status)?<button onClick={()=>onStop(operation)}>停止</button>:<><button onClick={()=>{const tool=tools.find(t=>t.id===operation.request.tool);if(tool)onChoose({...context,selection:operation.request.selection,mapping:operation.request.mapping||mapping},tool,operation.request.parameters,operation.request)}}>修改配置再运行</button><button onClick={()=>onSave(operation)}>保存处理链</button></>}</div></header>
      <details className={styles.history}><summary>本次输入与配置</summary><p>来源：{object.name} · {operation.source.version.slice(0,8)}</p><p>字段：{operation.request.selection.columns.join('、')||'全部'} · 行范围：{operation.request.selection.row_indices?.length??'全部满足筛选的行'}</p>{operation.request.selection.filters.map((f,i)=><p key={i}>{f.column} {f.operator} {f.value}</p>)}{operation.request.model&&<p>模型：{operation.request.model.id} · {operation.request.model.version.slice(0,8)}</p>}{operation.inputs?.map(input=><p key={input.role+input.id}>输入 {input.role}：{input.name} · {input.version.slice(0,8)}</p>)}<ResultDetails value={operation.request.parameters}/><p>运行编号：{operation.task_id}</p><p>历史结果固定使用运行时的数据、规则与模型。修改配置后请重新运行，不会覆盖这里的结果。</p></details>
      {operation.request.mapping&&object.mapping&&JSON.stringify(operation.request.mapping.field_rules)!==JSON.stringify(object.mapping.field_rules)&&<p className={styles.notice}>当前字段规则与这次运行不同。这里保留历史解释；改变取值含义或训练处理后，请重新整理、分析并训练评价。</p>}
      {operation.source.version!==object.version&&<p className={styles.notice}>来源数据已改变：这里是旧版本结果，需要重新运行才能反映当前数据。</p>}
      {operation.error&&<p role="alert" className={styles.error}>{operation.error.replace('请明确 mapping.invalid_rows="exclude"','请在训练配置的“待确认或错误数据”中选择“明确排除并记录原因”')}</p>}
      {!operation.results.length&&<p className={styles.muted}>{['queued','running'].includes(operation.status)?'运行完成的结果会出现在这里，可以继续接工具处理。':'本次尚无可用产物。'}</p>}
      {operation.results.map((result,i)=><ObjectCard key={result.id} projectId={projectId} object={result} operations={operations} tools={tools} onChoose={onChoose} onContext={onContext} onSave={onSave} onStop={onStop} initialOpen={i===0&&result.kind==='analysis'} ancestors={[...ancestors,operation.id]}/>)}
    </section>)}
  </div>
}

export default function DataWorkspace({projectId,onWorkflow}:{projectId:string;onWorkflow:(id:string)=>void}) {
  const base=`/api/v1/projects/${projectId}/data-workspace`
  const [listing,setListing]=useState<Listing>(),[catalog,setCatalog]=useState<Catalog>({tools:[],workflows:[]}),[error,setError]=useState(''),[busy,setBusy]=useState(false),[query,setQuery]=useState(''),[kind,setKind]=useState('all'),[toolQuery,setToolQuery]=useState('')
  const [dialog,setDialog]=useState<Dialog>(),[context,setContext]=useState<Context>(),[saving,setSaving]=useState<Operation>(),[chainName,setChainName]=useState(''),[saved,setSaved]=useState<{workflow_id:string;name:string}>()
  const input=useRef<HTMLInputElement>(null),active=useRef(true),generation=useRef(0)
  const tools=[...catalog.tools,...catalog.workflows]
  const refresh=useCallback(async()=>{const current=++generation.current;try{const [data,library]=await Promise.all([api<Listing>(base),api<Catalog>(base+'/catalog')]);if(active.current&&current===generation.current){setListing(data);setCatalog(library);setError('')}}catch(e){if(active.current)setError(String(e))}},[base])
  useEffect(()=>{active.current=true;void refresh();return()=>{active.current=false}},[refresh])
  const running=(listing?.operations||[]).filter(o=>['queued','running','waiting_input'].includes(o.status)).map(o=>o.id).join(',')
  useEffect(()=>{if(!running)return;let live=true,fetching=false;const timer=setInterval(async()=>{if(fetching)return;fetching=true;try{const values=await Promise.all(running.split(',').map(id=>api<Operation>(base+'/operations/'+id)));if(live){setListing(old=>old?{...old,operations:old.operations.map(o=>values.find(v=>v.id===o.id)||o)}:old);if(values.some(o=>!['queued','running','waiting_input'].includes(o.status)))void refresh()}}catch(e){if(live)setError(String(e))}finally{fetching=false}},2000);return()=>{live=false;clearInterval(timer)}},[base,running,refresh])
  const objects=[...new Map([...(listing?.objects||[]),...(listing?.operations.flatMap(o=>o.results)||[]),...(catalog.models||[])].map(o=>[o.id,o])).values()]
  const produced=new Set(listing?.operations.flatMap(o=>o.results.map(r=>r.id))||[])
  const registered=new Set((listing?.operations||[]).filter(o=>o.dataset_id&&o.source.id!=='dataset:'+o.dataset_id).map(o=>'dataset:'+o.dataset_id))
  const roots=(listing?.objects||[]).filter(o=>!produced.has(o.id)&&!registered.has(o.id))
  const filtered=(kind==='all'&&!query?roots:objects).filter(o=>!registered.has(o.id)&&(kind==='all'||o.kind===kind)&&(o.name+(o.path||'')).toLowerCase().includes(query.toLowerCase()))
  async function choose(c:Context,t:Tool,initialParameters?:Record<string,unknown>,initialRequest?:Operation['request']) {
    if(!compatible(c.object,t)){setError(`「${t.name}」不能直接处理${kindNames[c.object.kind]}。请选择兼容的数据，或在工作流中声明所需输入。`);return}
    try {
      let view=c.view
      if(c.object.kind==='table'&&!view)view=await api<TableView>(base+'/preview',{method:'POST',body:JSON.stringify({object_id:c.object.id,version:c.object.version,mapping:c.mapping,selection:c.selection,limit:1})})
      setDialog({...c,view,tool:t,initialParameters,initialRequest});setError('')
    }catch(e){setError(String(e))}
  }
  async function upload(files:File[]) {setBusy(true);setError('');try{for(const file of files){const form=new FormData();form.append('file',file);await api(`/api/v1/projects/${projectId}/materials`,{method:'POST',body:form})}await refresh()}catch(e){setError(String(e))}finally{setBusy(false)}}
  async function save() {if(!saving)return;setBusy(true);setError('');try{const result=await api<{workflow_id:string}>(base+'/operations/'+saving.id+'/save',{method:'POST',body:JSON.stringify({name:chainName,request_key:clientId()})});setSaved({...result,name:chainName});setSaving(undefined);await refresh()}catch(e){setError(String(e))}finally{setBusy(false)}}
  async function stop(o:Operation) {try{await api(`/api/v1/projects/${projectId}/tasks/${o.task_id}/stop`,{method:'POST'});await refresh()}catch(e){setError(String(e))}}
  return <div className={styles.workspace} aria-label="数据工作空间">
    <aside className={styles.library}><h2>工具与工作流</h2><p>拖到数据上，补齐配置后运行。也可以先查看数据，再选择工具。</p><input aria-label="搜索工具与工作流" placeholder="搜索工具或工作流" value={toolQuery} onChange={e=>setToolQuery(e.target.value)}/><div className={styles.libraryList}>{tools.filter(t=>t.allowed!==false&&(t.name+t.description).toLowerCase().includes(toolQuery.toLowerCase())).map(tool=><button key={tool.id} className={styles.tool} draggable onDragStart={e=>{e.dataTransfer.setData(TOOL_MIME,tool.id);e.dataTransfer.effectAllowed='copy'}} onDragOver={e=>{if(e.dataTransfer.types.includes(DATA_MIME))e.preventDefault()}} onDrop={e=>{e.preventDefault();try{const data=JSON.parse(e.dataTransfer.getData(DATA_MIME));const object=objects.find(o=>o.id===data.id);if(object)void choose({object,selection:data.selection,mapping:data.mapping},tool)}catch{setError('拖动的数据已失效，请重新选择')}}} onClick={()=>{if(context)void choose(context,tool);else setError('请先展开一份数据，或将工具拖到数据卡片上。')}}><strong>{tool.name}</strong><small>{tool.description}</small>{tool.id.startsWith('workflow:')&&<small>工作流 · 修订 {tool.revision}</small>}</button>)}</div></aside>
    <main className={styles.main}><header className={styles.heading}><div><h2>数据工作空间</h2><p>从数据开始，逐步分析。每次处理的结果都可以成为下一步的输入。</p></div><div className={styles.actions}><button className={styles.primary} disabled={busy} onClick={()=>input.current?.click()}><Upload size={14}/> 添加数据与资料</button><button onClick={()=>void refresh()}>刷新</button><input ref={input} type="file" multiple hidden aria-label="上传工作空间资料" onChange={e=>{const files=Array.from(e.target.files||[]);e.target.value='';void upload(files)}}/></div></header>
      <div className={styles.toolbar}><Search size={17}/><input aria-label="搜索项目数据" placeholder="搜索数据、模型和报告" value={query} onChange={e=>setQuery(e.target.value)}/></div>
      <div className={styles.tabs}>{[['all','全部'],['table','数据表'],['model','模型'],['analysis','分析结果'],['document','文档与报告'],['image','图片'],['file','其他文件']].map(([k,label])=><button key={k} aria-pressed={kind===k} onClick={()=>setKind(k)}>{label}</button>)}</div>
      {error&&<p role="alert" className={styles.error}>{error}</p>}
      {saved&&<p role="status" className={styles.notice}>已保存「{saved.name}」，可以从左侧拖入再次使用。<button onClick={()=>onWorkflow(saved.workflow_id)}>打开画布编辑</button></p>}
      {!listing?<p role="status">正在读取项目数据…</p>:!filtered.length?<div className={styles.empty}><Database size={32}/><p>{query||kind!=='all'?'当前筛选没有匹配的数据。':'添加一份表格或资料，即可开始。'}</p><p>支持 CSV、TSV、Excel、图片、文档及项目已有结果。</p></div>:filtered.map((object,i)=><ObjectCard key={object.id+object.version} projectId={projectId} object={object} operations={listing.operations} tools={tools} onChoose={(c,t,p,r)=>void choose(c,t,p,r)} onContext={setContext} onSave={o=>{setSaving(o);setChainName(o.name+' · 可复用流程')}} onStop={o=>void stop(o)} initialOpen={i===0&&filtered.length<5}/>)}
      {listing?.has_more&&<button onClick={async()=>{try{const page=await api<Listing>(base+'?offset='+(listing.offset+50));setListing(old=>old?{...page,objects:[...old.objects,...page.objects]}:page)}catch(e){setError(String(e))}}}>加载更多项目对象</button>}
    </main>
    {dialog&&<WorkspaceOperationDialog projectId={projectId} {...dialog} objects={objects} onClose={()=>setDialog(undefined)} onStarted={operation=>{setListing(old=>old?{...old,operations:[...old.operations.filter(o=>o.id!==operation.id),operation]}:old);setDialog(undefined)}}/>}
    {saving&&<div className={styles.backdrop}><section className={styles.dialog} role="dialog" aria-modal="true" aria-label="保存处理链"><h2>保存为可复用工作流</h2><p className={styles.muted}>保存当前处理及必要的数据整理步骤。已选模型作为固定资源，不会因复用预测流程而重新训练。</p><div className={styles.form}><label className={styles.wide}>工作流名称<input aria-label="保存的工作流名称" value={chainName} onChange={e=>setChainName(e.target.value)}/></label></div><>{error&&<p role="alert" className={styles.error}>{error}</p>}</><div className={styles.footer}><button disabled={busy} onClick={()=>setSaving(undefined)}>取消</button><button className={styles.primary} disabled={busy||!chainName.trim()} onClick={()=>void save()}>保存工作流</button></div></section></div>}
  </div>
}
