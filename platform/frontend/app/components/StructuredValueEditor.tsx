'use client'
import {useState} from 'react'
import styles from './project-debug.module.css'

export type Value = null | string | number | boolean | Value[] | {[key:string]:Value}
const kind=(v:Value)=>v===null?'null':Array.isArray(v)?'array':typeof v
const empty=(type:string):Value=>({string:'',number:0,boolean:false,object:{},array:[],null:null}[type] as Value)

/** Small typed form for sample data. JSON is never needed to edit nested inputs. */
export default function StructuredValueEditor({value,onChange,label='输入',depth=0}: {
  value:Value;onChange:(value:Value)=>void;label?:string;depth?:number
}) {
  const [key,setKey]=useState('')
  const type=kind(value)
  if(depth>12)return <p>内容层级较深，请上传样例或让助手修改此字段。</p>
  const scalar=(v:Value,change:(v:Value)=>void,name:string)=> <StructuredValueEditor value={v} onChange={change} label={name} depth={depth+1}/>
  return <div className={styles.value}>
    <div className={styles.valueHeading}><strong>{label}</strong><select aria-label={label+'类型'} value={type} onChange={e=>onChange(empty(e.target.value))}>
      <option value="string">文本</option><option value="number">数字</option><option value="boolean">是 / 否</option><option value="null">空值</option><option value="object">字段组</option><option value="array">列表</option>
    </select></div>
    {type==='string'&&<input aria-label={label} value={String(value)} onChange={e=>onChange(e.target.value)}/>}
    {type==='number'&&<input aria-label={label} type="number" step="any" value={Number(value)} onChange={e=>{if(e.target.value!==''&&Number.isFinite(Number(e.target.value)))onChange(Number(e.target.value))}}/>}
    {type==='boolean'&&<select aria-label={label} value={String(value)} onChange={e=>onChange(e.target.value==='true')}><option value="true">是</option><option value="false">否</option></select>}
    {type==='object'&&<>
      {Object.entries(value as {[key:string]:Value}).map(([name,item])=><div className={styles.entry} key={name}>{item&&typeof item==='object'?<details><summary>{name} · {Array.isArray(item)?item.length+' 项':'字段组'}</summary>{scalar(item,next=>onChange({...value as object,[name]:next}),name)}</details>:scalar(item,next=>onChange({...value as object,[name]:next}),name)}<button type="button" aria-label={'移除字段 '+name} onClick={()=>{const next={...value as {[key:string]:Value}};delete next[name];onChange(next)}}>移除</button></div>)}
      <div className={styles.row}><input aria-label={label+'新增字段名称'} placeholder="字段名称" value={key} onChange={e=>setKey(e.target.value)}/><button type="button" disabled={!key.trim()||Object.hasOwn(value as object,key.trim())||['__proto__','prototype','constructor'].includes(key.trim())} onClick={()=>{onChange({...value as object,[key.trim()]:''});setKey('')}}>添加字段</button></div>
    </>}
    {type==='array'&&<>
      {(value as Value[]).length>0&&(value as Value[]).every(row=>row!==null&&!Array.isArray(row)&&typeof row==='object'&&Object.values(row).every(v=>v===null||typeof v!=='object'))
        ? <div className={styles.scroll}><table><thead><tr>{Array.from(new Set((value as {[key:string]:Value}[]).flatMap(Object.keys))).map(k=><th key={k}>{k}</th>)}<th/></tr></thead><tbody>{(value as {[key:string]:Value}[]).map((row,i)=><tr key={i}>{Array.from(new Set((value as {[key:string]:Value}[]).flatMap(Object.keys))).map(k=><td key={k}>{scalar(row[k]??null,next=>onChange((value as Value[]).map((other,j)=>j===i?{...row,[k]:next}:other)),`${label} 第${i+1}行 ${k}`)}</td>)}<td><button type="button" aria-label={`移除${label}第${i+1}行`} onClick={()=>onChange((value as Value[]).filter((_,j)=>j!==i))}>移除</button></td></tr>)}</tbody></table></div>
        : (value as Value[]).map((row,i)=><details key={i} open={depth===0&&Array.isArray(value)&&value.length<5}><summary>{label} · 第 {i+1} 项</summary>{scalar(row,next=>onChange((value as Value[]).map((v,j)=>j===i?next:v)),`${label} 第${i+1}项`)}<button type="button" onClick={()=>onChange((value as Value[]).filter((_,j)=>j!==i))}>移除此项</button></details>)}
      <button type="button" onClick={()=>{const first=(value as Value[])[0];const next=first&&typeof first==='object'&&!Array.isArray(first)?Object.fromEntries(Object.entries(first).map(([k,v])=>[k,empty(kind(v))])):{};onChange([...value as Value[],next])}}>添加一行</button>
    </>}
  </div>
}
