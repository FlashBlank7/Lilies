'use client'
import {parseField,valueReference} from '@/lib/workflow-fields'
import {WorkflowValueField,type WorkflowFieldProps} from './WorkflowValueField'
import styles from './workflow-fields.module.css'

type Check={name:string;value:unknown;operator:string;expected:unknown;failure_message:string}
const operators={equals:'等于',not_equals:'不等于',contains:'包含',not_contains:'不包含',gt:'大于',gte:'大于等于',lt:'小于',lte:'小于等于',exists:'有值',empty:'为空'}
export default function WorkflowResultChecks(props:WorkflowFieldProps){
  const parsed=parseField(props.value),checks:Check[]=Array.isArray(parsed)?parsed:[]
  function update(index:number,patch:Partial<Check>){props.onChange(JSON.stringify(checks.map((c,i)=>i===index?{...c,...patch}:c)))}
  return <span className={styles.entries}><small>不配置时只表示执行成功。配置后，全部检查通过才表示这些目标已验证；缺少结果也会失败。</small>{checks.map((check,index)=>{
    const type=typeof check.expected==='number'?'number':typeof check.expected==='boolean'?'boolean':'string'
    return <span key={index} className={styles.entry}><label>检查名称<input aria-label={`检查名称 ${index+1}`} disabled={props.disabled} value={check.name} onChange={e=>update(index,{name:e.target.value})}/></label>
      <label>实际值<WorkflowValueField {...props} label={`检查 ${index+1}实际值`} value={typeof check.value==='string'?check.value:JSON.stringify(check.value)} onChange={v=>update(index,{value:valueReference(parseField(v))?parseField(v):v})}/></label>
      <label>比较方式<select aria-label={`检查 ${index+1}比较方式`} disabled={props.disabled} value={check.operator} onChange={e=>update(index,{operator:e.target.value})}>{Object.entries(operators).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label>
      {!['exists','empty'].includes(check.operator)&&<><label>预期值类型<select aria-label={`检查 ${index+1}预期值类型`} value={type} disabled={props.disabled} onChange={e=>update(index,{expected:e.target.value==='number'?0:e.target.value==='boolean'?true:''})}><option value="string">文本</option><option value="number">数字</option><option value="boolean">是 / 否</option></select></label>
      {type==='boolean'?<label>预期值<select aria-label={`检查 ${index+1}预期值`} disabled={props.disabled} value={String(check.expected)} onChange={e=>update(index,{expected:e.target.value==='true'})}><option value="true">是</option><option value="false">否</option></select></label>:type==='number'?<label>预期值<input type="number" aria-label={`检查 ${index+1}预期值`} disabled={props.disabled} value={String(check.expected)} onChange={e=>{if(e.target.value!==''&&Number.isFinite(Number(e.target.value)))update(index,{expected:Number(e.target.value)})}}/></label>:<WorkflowValueField {...props} label={`检查 ${index+1}预期值`} value={typeof check.expected==='string'?check.expected:JSON.stringify(check.expected)} onChange={v=>update(index,{expected:valueReference(parseField(v))?parseField(v):v})}/>}</>}
      <label>失败说明<input aria-label={`检查 ${index+1}失败说明`} disabled={props.disabled} value={check.failure_message} onChange={e=>update(index,{failure_message:e.target.value})}/></label>
      <button type="button" disabled={props.disabled} onClick={()=>props.onChange(JSON.stringify(checks.filter((_,i)=>i!==index)))}>删除检查</button></span>
  })}<button type="button" disabled={props.disabled} onClick={()=>props.onChange(JSON.stringify([...checks,{name:'',value:'',operator:'equals',expected:true,failure_message:'目标尚未满足'}]))}>添加结果检查</button></span>
}
