'use client'
import { useState } from 'react'
import { parseField } from '@/lib/workflow-fields'
import styles from './workflow-fields.module.css'

type InputField = { name: string; type?: string; required?: boolean; [key: string]: unknown }
const typeLabels: Record<string, string> = { string: '文本', number: '数字', boolean: '是 / 否', file: '文件', file_list: '文件列表', object: '对象', array: '数组', any: '任意类型' }

function InputRow({ item, index, names, human, onChange, onDelete }: {
  item: InputField; index: number; names: string[]; human: boolean
  onChange: (item: InputField) => void; onDelete: () => void
}) {
  const [invalidName, setInvalidName] = useState<string | null>(null)
  const [optionsDraft, setOptionsDraft] = useState<{ text: string; value: string } | null>(null)
  const [invalidNumber, setInvalidNumber] = useState(false)
  const type = item.type ?? 'string'
  const optionsEditable = item.options === undefined || (Array.isArray(item.options) && item.options.every(option => typeof option === 'string'))
  const options = optionsEditable && Array.isArray(item.options) ? item.options as string[] : []
  const optionsValue = JSON.stringify(item.options)
  const hasDefault = Object.prototype.hasOwnProperty.call(item, 'default')
  const simpleDefault = ['string', 'number', 'boolean'].includes(type)
  const defaultMatchesType = typeof item.default === type
  const number = index + 1
  function update(patch: Record<string, unknown>) { onChange({ ...item, ...patch }) }

  return <span className={styles.entry}>
    <label>字段名称<input aria-label={`输入名称 ${number}`} value={invalidName ?? item.name} onChange={event => {
      const name = event.target.value
      if (!name.trim() || names.some((other, i) => i !== index && other === name)) { setInvalidName(name); return }
      setInvalidName(null); update({ name })
    }} /></label>
    {invalidName !== null && <small role="alert">字段名称不能为空或重复；当前仍保留“{item.name}”。</small>}
    <label>显示名称<input aria-label={`${human ? '回答名称' : '输入显示名称'} ${number}`} value={String(item.label ?? '')} placeholder="例如：汇总维度" onChange={event => update({ label: event.target.value })} /></label>
    {!human && <label>说明<textarea aria-label={`输入说明 ${number}`} rows={2} value={String(item.description ?? '')} onChange={event => update({ description: event.target.value })} /></label>}
    <label>类型<select aria-label={`输入类型 ${number}`} value={type} onChange={event => { setInvalidNumber(false); update({ type: event.target.value }) }}>
      {!typeLabels[type] && <option value={type}>{type} · 当前类型</option>}
      {Object.entries(typeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
    </select></label>
    {(human || type === 'string') && (optionsEditable ? <label>可选{human ? '答案' : '项'}（每行一个，留空自由填写）<textarea
      aria-label={`${human ? '回答' : '输入'}选项 ${number}`} rows={3}
      value={optionsDraft && optionsDraft.value === optionsValue ? optionsDraft.text : options.join('\n')}
      onChange={event => {
        const next = event.target.value.split('\n').filter(option => option.trim() !== '')
        setOptionsDraft({ text: event.target.value, value: JSON.stringify(next) })
        update({ options: next })
      }}
    /></label> : <small className={styles.help}>现有选项包含复杂配置，已保留；请在高级 JSON 中编辑。</small>)}
    {!human && <>
      <span className={styles.row}><label className={styles.checkbox}><input aria-label={`使用默认值 ${number}`} type="checkbox" checked={hasDefault} disabled={!simpleDefault && !hasDefault} onChange={event => {
        setInvalidNumber(false)
        if (event.target.checked) update({ default: type === 'number' ? 0 : type === 'boolean' ? false : options[0] ?? '' })
        else { const next = { ...item }; delete next.default; onChange(next) }
      }} />使用默认值</label></span>
      {hasDefault && simpleDefault && defaultMatchesType ? <>
        <label>默认值{type === 'boolean' ? <select aria-label={`输入默认值 ${number}`} value={String(item.default)} onChange={event => update({ default: event.target.value === 'true' })}>
          <option value="true">是</option><option value="false">否</option>
        </select> : type === 'number' ? <input aria-label={`输入默认值 ${number}`} type="number" step="any" value={invalidNumber ? '' : String(item.default)} onChange={event => {
          const value = event.target.value
          if (value === '' || !Number.isFinite(Number(value))) { setInvalidNumber(true); return }
          setInvalidNumber(false); update({ default: Number(value) })
        }} /> : options.length ? <select aria-label={`输入默认值 ${number}`} value={String(item.default)} onChange={event => update({ default: event.target.value })}>
          {!options.includes(String(item.default)) && <option value={String(item.default)}>{String(item.default) || '空文本'} · 当前值（不在选项中）</option>}
          {options.map((option, i) => <option key={i} value={option}>{option || '空文本'}</option>)}
        </select> : <input aria-label={`输入默认值 ${number}`} value={String(item.default)} onChange={event => update({ default: event.target.value })} />}</label>
        {invalidNumber && <small role="alert">请输入有效数字；当前仍保留 {String(item.default)}。不需要默认值时，请关闭“使用默认值”。</small>}
        {type === 'string' && options.length > 0 && !options.includes(String(item.default)) && <small role="alert">当前默认值不在可选项中，请重新选择或补回选项。</small>}
        {type === 'string' && !options.length && <small className={styles.help}>留空会保存为空文本；不需要默认值时，请关闭“使用默认值”。</small>}
      </> : hasDefault ? <details><summary>查看现有默认值</summary><pre>{JSON.stringify(item.default, null, 2)}</pre><small className={styles.help}>已保留原值和类型；请在高级 JSON 中修改，或关闭“使用默认值”移除。</small></details> : !simpleDefault && <small className={styles.help}>此类型的默认值可在高级 JSON 中设置。</small>}
    </>}
    <span className={styles.row}><label className={styles.checkbox}><input aria-label={`必填 ${number}`} type="checkbox" checked={item.required ?? true} onChange={event => update({ required: event.target.checked })} />必填</label></span>
    <button type="button" onClick={onDelete}>删除输入</button>
  </span>
}

export function WorkflowInputFields({ value, onChange, human = false }: { value: string; onChange: (value: string) => void; human?: boolean }) {
  const parsed = parseField(value), items = Array.isArray(parsed) ? parsed as InputField[] : []
  const names = items.map(item => item.name)
  return <span className={styles.entries}>{items.map((item, index) => <InputRow key={index} item={item} index={index} names={names} human={human}
    onChange={next => onChange(JSON.stringify(items.map((other, i) => i === index ? next : other)))}
    onDelete={() => onChange(JSON.stringify(items.filter((_, i) => i !== index)))}
  />)}<button type="button" onClick={() => {
    let index = items.length + 1
    while (names.includes('input_' + index)) index++
    onChange(JSON.stringify([...items, { name: 'input_' + index, type: 'string', required: false, ...(human ? { label: '补充说明', options: [] } : {}) }]))
  }}>添加输入</button></span>
}
