'use client'

import { useId } from 'react'
import type { BlockEditorField } from '@/lib/platform'
import type { Locale } from '@/lib/i18n'

export default function BlockOptionField({ field, label, locale, value, onChange }: {
  field: BlockEditorField
  label: string
  locale: Locale
  value: string
  onChange: (value: string) => void
}) {
  const helpId = useId()
  const optionLabel = (option: string) => locale === 'zh' ? field.option_labels_zh?.[option] || option : option
  if (field.control === 'string_list') {
    const selected = value.split(/[\n,]/).map(item => item.trim()).filter(Boolean)
    const options = [...new Set([...(field.options || []), ...selected])]
    return <div role="group" aria-label={label}>{options.map(option => <label key={option}>
      <input type="checkbox" checked={selected.includes(option)} onChange={event => onChange(
        (event.target.checked ? [...selected, option] : selected.filter(item => item !== option)).join('\n'),
      )} /> {optionLabel(option)}
    </label>)}</div>
  }
  const help = locale === 'zh' ? field.option_descriptions_zh?.[value] : undefined
  return <>
    <select aria-label={label} aria-describedby={help ? helpId : undefined} value={value} onChange={event => onChange(event.target.value)}>
      {!field.required && field.default_value == null && <option value="" />}
      {value && !field.options?.includes(value) && <option value={value}>{value}</option>}
      {field.options?.map(option => <option key={option} value={option}>{optionLabel(option)}</option>)}
    </select>
    {help && <small id={helpId}>{help}</small>}
  </>
}
