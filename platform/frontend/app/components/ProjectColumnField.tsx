'use client'

import {useEffect, useState} from 'react'
import {api} from '@/lib/platform'

export type ColumnChoices = {columns?: string[]; loading?: boolean; message?: string; sheet?: string}

export function useProjectColumns(projectId: string, requests: string) {
  const [choices, setChoices] = useState<Record<string, ColumnChoices>>({})
  useEffect(() => {
    let current = true
    const paths = JSON.parse(requests) as {path: string; sheet: string}[]
    setChoices(Object.fromEntries(paths.map(({path, sheet}) => [columnKey(path, sheet), {loading: true}])))
    for (const {path, sheet} of paths) {
      const key = columnKey(path, sheet)
      const query = new URLSearchParams({path, ...(sheet ? {sheet} : {})})
      void api<{columns: string[]; sheet: string}>(`/api/v1/applications/${projectId}/workspace/table-columns?${query}`).then(result => {
        if (current) setChoices(previous => ({...previous, [key]: result}))
      }).catch(error => {
        if (current) setChoices(previous => ({...previous, [key]: {message: `暂时无法列出字段：${String(error)}。可以手动填写字段名。`}}))
      })
    }
    return () => { current = false }
  }, [projectId, requests])
  return choices
}

export function columnKey(path: string, sheet: string) { return JSON.stringify([path, sheet]) }
export function canReadColumns(path: string) { return !!path && !path.startsWith('datasets/') && /\.(csv|tsv|xlsx)$/i.test(path) }
export function unknownColumn(label: string, value: string, choices?: ColumnChoices) {
  return value && choices?.columns && !choices.columns.includes(value)
    ? `${label}“${value}”不在当前表格中。可用字段：${choices.columns.join('、')}` : ''
}

export default function ProjectColumnField({name, label, value, source, choices, required, disabled, onChange}: {
  name: string; label: string; value: string; source: string; choices?: ColumnChoices; required?: boolean; disabled: boolean; onChange: (value: string) => void
}) {
  const error = unknownColumn(label, value, choices)
  const helpId = `column-help-${name}`
  const columns = choices?.columns || []
  const message = error || (choices?.loading ? '正在读取表头…' : choices?.message)
    || (!source ? '先选择数据表即可查看字段，也可以手动填写。' : !canReadColumns(source) ? '当前资料暂不支持列出字段，请手动填写。' : '')
  return <div>
    <label>{label}{required ? ' *' : ''}<select aria-label={`为 ${name} 选择表格字段`} aria-invalid={!!error} aria-describedby={helpId} disabled={disabled || !columns.length} value={value} onChange={event => onChange(event.target.value)}>
      <option value="">{required ? '请选择字段' : '不选择字段'}</option>
      {value && !columns.includes(value) && <option value={value}>{value}（当前填写的字段）</option>}
      {columns.map(column => <option key={column} value={column}>{column}</option>)}
    </select></label>
    <p id={helpId} role={error ? 'alert' : 'status'}>{message || `${choices?.sheet ? `工作表：${choices.sheet}。` : ''}可用字段：${columns.join('、')}`}</p>
    <details><summary>手动填写字段名</summary><label>字段名<input aria-label={name} aria-invalid={!!error} aria-describedby={helpId} disabled={disabled} value={value} onChange={event => onChange(event.target.value)}/></label></details>
  </div>
}
