'use client'
import { parseField, valueReference } from '@/lib/workflow-fields'
import { WorkflowValueField, type WorkflowFieldProps } from './WorkflowValueField'
import styles from './workflow-fields.module.css'

type Condition = { value: unknown; operator: string; expected?: unknown; [key: string]: unknown }
type Case = { id: string; logical_operator?: string; conditions: Condition[]; [key: string]: unknown }
const operators: Record<string, string> = { equals: '等于', not_equals: '不等于', contains: '包含', not_contains: '不包含', gt: '大于', gte: '大于等于', lt: '小于', lte: '小于等于', exists: '有值', empty: '为空' }

export default function WorkflowConditionFields(props: WorkflowFieldProps) {
  const parsed = parseField(props.value), cases = Array.isArray(parsed) ? parsed as Case[] : []
  function updateCase(index: number, patch: Partial<Case>) { props.onChange(JSON.stringify(cases.map((value, i) => i === index ? { ...value, ...patch } : value))) }
  function updateCondition(caseIndex: number, index: number, patch: Partial<Condition>) { updateCase(caseIndex, { conditions: cases[caseIndex].conditions.map((value, i) => i === index ? { ...value, ...patch } : value) }) }
  return <span className={styles.entries}>{cases.map((item, caseIndex) => <span className={styles.entry} key={caseIndex}>
    <label>分支标识<input disabled={props.disabled} aria-label={`分支标识 ${caseIndex + 1}`} value={item.id} onChange={event => updateCase(caseIndex, { id: event.target.value })} /></label>
    <small className={styles.help}>连线选择此标识；修改已有标识后请同步调整分支连线。</small>
    <label>满足条件<select disabled={props.disabled} aria-label={`分支 ${caseIndex + 1}的条件组合`} value={item.logical_operator || 'and'} onChange={event => updateCase(caseIndex, { logical_operator: event.target.value })}><option value="and">全部满足</option><option value="or">任意满足</option></select></label>
    {item.conditions.map((condition, index) => {
      const label = `分支 ${caseIndex + 1}条件 ${index + 1}`, type = typeof condition.expected === 'number' ? 'number' : typeof condition.expected === 'boolean' ? 'boolean' : 'string'
      return <span className={styles.entry} key={index}>
        <label>比较的值<WorkflowValueField {...props} label={`${label}的值`} field="condition_value" value={typeof condition.value === 'string' ? condition.value : JSON.stringify(condition.value)} onChange={value => updateCondition(caseIndex, index, { value: valueReference(parseField(value)) ? parseField(value) : value })} /></label>
        <select disabled={props.disabled} aria-label={`${label}的比较方式`} value={condition.operator || 'equals'} onChange={event => updateCondition(caseIndex, index, { operator: event.target.value })}>{Object.entries(operators).map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select>
        {!['exists', 'empty'].includes(condition.operator) && <>
          <label>比较值类型<select disabled={props.disabled} aria-label={`${label}的比较值类型`} value={type} onChange={event => updateCondition(caseIndex, index, { expected: event.target.value === 'number' ? 0 : event.target.value === 'boolean' ? false : '' })}><option value="string">文本</option><option value="number">数字</option><option value="boolean">是 / 否</option></select></label>
          {type === 'boolean' ? <select disabled={props.disabled} aria-label={`${label}的比较值`} value={String(condition.expected)} onChange={event => updateCondition(caseIndex, index, { expected: event.target.value === 'true' })}><option value="true">是</option><option value="false">否</option></select>
            : <input disabled={props.disabled} aria-label={`${label}的比较值`} type={type === 'number' ? 'number' : 'text'} value={String(condition.expected ?? '')} onChange={event => { const value = type === 'number' ? Number(event.target.value) : event.target.value; if (type !== 'number' || (event.target.value !== '' && Number.isFinite(value))) updateCondition(caseIndex, index, { expected: value }) }} />}
        </>}
        <button disabled={props.disabled || item.conditions.length <= 1} type="button" onClick={() => updateCase(caseIndex, { conditions: item.conditions.filter((_, i) => i !== index) })}>删除条件</button>
      </span>
    })}
    <button disabled={props.disabled} type="button" onClick={() => updateCase(caseIndex, { conditions: [...item.conditions, { value: '', operator: 'equals', expected: '' }] })}>添加条件</button>
    <button disabled={props.disabled} type="button" onClick={() => props.onChange(JSON.stringify(cases.filter((_, i) => i !== caseIndex)))}>删除分支</button>
  </span>)}<button disabled={props.disabled} type="button" onClick={() => { let index = cases.length + 1; while (cases.some(item => item.id === 'case_' + index)) index++; props.onChange(JSON.stringify([...cases, { id: 'case_' + index, logical_operator: 'and', conditions: [{ value: '', operator: 'equals', expected: '' }] }])) }}>添加分支</button></span>
}
