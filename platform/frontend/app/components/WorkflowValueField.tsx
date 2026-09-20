'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/platform'
type Node = {id:string;title:string;type:string;config:Record<string,unknown>}
type Props = {value:string;onChange:(value:string)=>void;nodes:Node[];nodeId:string;label:string;projectId?:string;field?:string;allowReference?:boolean;disabled?:boolean}
function parse(value:string):unknown {try{return JSON.parse(value)}catch{return value}}
export function WorkflowValueField({value,onChange,nodes,nodeId,label,projectId,field,allowReference=true,disabled=false}:Props){
  const parsed=parse(value) as {$ref?:{node_id:string;path:string[]}}
  const reference=typeof parsed==='object'&&parsed!==null?parsed.$ref:undefined
  const [options,setOptions]=useState<{value:string;label:string}[]>([])
  const [error,setError]=useState('')
  useEffect(()=>{if(!projectId||!['model_ref','dataset_id','model'].includes(field||''))return;let active=true
    const route=field==='model_ref'?'/models':field==='dataset_id'?'/datasets?limit=100':'/agent-session'
    void api<unknown>(`/api/v1/projects/${projectId}`+route).then(data=>{if(!active)return
      if(Array.isArray(data))setOptions(data.map(d=>({value:d.model_ref||d.id,label:`${d.name}${d.status==='unbound'?' · 待绑定':''}`})))
      else {const session=data as {model?:string};setOptions(session.model?[{value:session.model,label:session.model}]:[])}
    }).catch(e=>{if(active)setError(String(e))});return()=>{active=false}
  },[projectId,field])
  const selectable=nodes.filter(n=>n.id!==nodeId)
  const selectedNode=nodes.find(n=>reference?.node_id==='$inputs'?n.type==='start':n.id===reference?.node_id)
  const paths=selectedNode?.type==='start' ? ((selectedNode.config.inputs||[]) as {name:string}[]).map(i=>i.name) : selectedNode?.type==='llm'?['text','structured']:selectedNode?.type==='end'?Object.keys(selectedNode.config.outputs||{}):['output']
  function setRef(id:string,path:string[]){onChange(JSON.stringify({$ref:{node_id:id,path}}))}
  return <span style={{display:'grid',gap:8}}>
    {allowReference&&<select disabled={disabled} aria-label={`${label}的来源`} value={reference?'reference':'literal'} onChange={e=>e.target.value==='literal'?onChange(''):setRef(selectable[0]?.id||'$inputs',[])}><option value="literal">直接填写 / 选择资源</option><option value="reference">引用节点输出</option></select>}
    {reference?<><select aria-label={`${label}的节点`} value={reference.node_id} onChange={e=>setRef(e.target.value,[])}><option value="$inputs">本次运行输入</option>{selectable.map(n=><option key={n.id} value={n.id}>{n.title}</option>)}</select>
      <input aria-label={`${label}的字段路径`} list={`${nodeId}-${field}-paths`} value={reference.path.join('.')} placeholder="选择字段或填写 output.result" onChange={e=>setRef(reference.node_id,e.target.value?e.target.value.split('.'):[])}/><datalist id={`${nodeId}-${field}-paths`}>{paths.map(p=><option key={p} value={p}/>)}</datalist></>
      :field==='model_ref'||field==='dataset_id'||field==='model'?<select disabled={disabled} aria-label={label} value={value} onChange={e=>onChange(e.target.value)}><option value="">{field==='model'?'项目模型 / 稍后配置':'稍后配置'}</option>{value&&!options.some(o=>o.value===value)&&<option value={value}>{value}</option>}{options.map(o=><option key={o.value} value={o.value}>{o.label}</option>)}</select>
      :<textarea aria-label={label} rows={2} value={value} onChange={e=>onChange(e.target.value)}/>}
    {error&&<small role="alert">资源列表读取失败，可刷新后重试。</small>}
  </span>
}
export function WorkflowObjectFields(props:Props){
  const object=parse(props.value),entries=object&&typeof object==='object'&&!Array.isArray(object)?Object.entries(object):[]
  function change(index:number,key:string,value:unknown){const next=entries.map((entry,i)=>i===index?[key,value]:entry);props.onChange(JSON.stringify(Object.fromEntries(next)))}
  return <span style={{display:'grid',gap:12}}>{entries.map(([key,value],i)=><span key={i} style={{display:'grid',gap:8}}><input aria-label={`${props.label}字段名 ${i+1}`} value={key} onChange={e=>change(i,e.target.value,value)}/><WorkflowValueField {...props} label={`${props.label} ${key}`} field={key} value={typeof value==='string'?value:JSON.stringify(value)} onChange={next=>change(i,key,parse(next))}/><button type="button" onClick={()=>props.onChange(JSON.stringify(Object.fromEntries(entries.filter((_,j)=>i!==j))))}>删除字段</button></span>)}<button type="button" onClick={()=>props.onChange(JSON.stringify({...Object.fromEntries(entries),['field_'+(entries.length+1)]:''}))}>添加字段</button></span>
}
export function WorkflowInputFields({value,onChange}:{value:string;onChange:(s:string)=>void}){
  const parsed=parse(value),items=Array.isArray(parsed)?parsed as {name:string;type:string;required?:boolean;[key:string]:unknown}[]:[]
  function update(i:number,patch:Record<string,unknown>){onChange(JSON.stringify(items.map((v,j)=>i===j?{...v,...patch}:v)))}
  return <span style={{display:'grid',gap:12}}>{items.map((item,i)=><span key={i} style={{display:'flex',gap:8,flexWrap:'wrap'}}><input aria-label={`输入名称 ${i+1}`} value={item.name} onChange={e=>update(i,{name:e.target.value})}/><select aria-label={`输入类型 ${i+1}`} value={item.type} onChange={e=>update(i,{type:e.target.value})}>{['string','number','boolean','file','object','array','any'].map(t=><option key={t}>{t}</option>)}</select><span><input aria-label={`必填 ${i+1}`} type="checkbox" checked={!!item.required} onChange={e=>update(i,{required:e.target.checked})}/>必填</span><button type="button" onClick={()=>onChange(JSON.stringify(items.filter((_,j)=>j!==i)))}>删除输入</button></span>)}<button type="button" onClick={()=>onChange(JSON.stringify([...items,{name:'input_'+(items.length+1),type:'string',required:false}]))}>添加输入</button></span>
}
