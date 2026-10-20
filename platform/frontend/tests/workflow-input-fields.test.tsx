import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { WorkflowInputFields } from '@/app/components/WorkflowInputFields'

afterEach(cleanup)
function Editor({ inputs }: { inputs: Record<string, unknown>[] }) {
  const [value, setValue] = useState(JSON.stringify(inputs))
  return <><WorkflowInputFields value={value} onChange={setValue} /><output aria-label="输入配置">{value}</output></>
}
const result = () => JSON.parse(screen.getByLabelText('输入配置').textContent!)

it('edits a labeled enum and keeps Enter available when adding options one character at a time', () => {
  render(<Editor inputs={[{ name: 'group_by', type: 'string', label: '汇总维度', default: '按类别', options: ['按类别', '按商户'], column_source: 'file', custom: { keep: true } }]} />)
  fireEvent.change(screen.getByLabelText('输入显示名称 1'), { target: { value: '报表汇总方式' } })
  fireEvent.change(screen.getByLabelText('输入说明 1'), { target: { value: '选择账单汇总方式' } })
  fireEvent.change(screen.getByLabelText('输入默认值 1'), { target: { value: '按商户' } })
  const options = screen.getByLabelText('输入选项 1')
  for (const text of ['按类别\n按商户\n', '按类别\n按商户\n按', '按类别\n按商户\n按月']) {
    fireEvent.change(options, { target: { value: text } })
    expect(options).toHaveValue(text)
  }
  expect(result()[0]).toEqual({ name: 'group_by', type: 'string', label: '报表汇总方式', description: '选择账单汇总方式', default: '按商户', options: ['按类别', '按商户', '按月'], column_source: 'file', custom: { keep: true } })
  fireEvent.change(options, { target: { value: '按类别' } })
  expect(screen.getByRole('alert')).toHaveTextContent('当前默认值不在可选项中')
  expect(result()[0].default).toBe('按商户')
})

it('uses typed defaults, distinguishes empty text from no default, and retains invalid number edits safely', () => {
  render(<Editor inputs={[{ name: 'text', type: 'string', default: '001' }, { name: 'amount', type: 'number', default: 0 }, { name: 'include', type: 'boolean', default: false }]} />)
  expect(screen.getByLabelText('输入默认值 2')).toHaveAttribute('type', 'number')
  expect(screen.getByLabelText('输入默认值 3')).toHaveValue('false')
  fireEvent.change(screen.getByLabelText('输入默认值 1'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('输入默认值 2'), { target: { value: '12.5' } })
  fireEvent.change(screen.getByLabelText('输入默认值 3'), { target: { value: 'true' } })
  expect(result().map((field: Record<string, unknown>) => field.default)).toEqual(['', 12.5, true])
  fireEvent.change(screen.getByLabelText('输入默认值 2'), { target: { value: '' } })
  expect(screen.getByRole('alert')).toHaveTextContent('当前仍保留 12.5')
  expect(result()[1].default).toBe(12.5)
  fireEvent.click(screen.getByLabelText('使用默认值 1'))
  expect(result()[0]).not.toHaveProperty('default')
  fireEvent.click(screen.getByLabelText('使用默认值 1'))
  expect(result()[0].default).toBe('')
  fireEvent.click(screen.getByLabelText('使用默认值 2'))
  expect(result()[1]).not.toHaveProperty('default')
})

it('retains complex and legacy default types and extra configuration when editing one field', () => {
  const inputs = [
    { name: 'file', type: 'file', accept: ['.csv'], default: 'sample.csv' },
    { name: 'table', type: 'array', columns: [{ name: 'amount', type: 'number' }], default: [{ amount: 0 }] },
    { name: 'settings', type: 'object', default: { enabled: false } },
    { name: 'legacy', type: 'number', default: '001', custom: 'keep' },
    { name: 'nullable', type: 'string', default: null },
    { name: 'legacy_options', type: 'string', options: [{ value: 'x', label: '复杂选项' }], default: 'x' },
  ]
  render(<Editor inputs={inputs} />)
  fireEvent.change(screen.getByLabelText('输入显示名称 1'), { target: { value: '费用文件' } })
  fireEvent.change(screen.getByLabelText('输入说明 2'), { target: { value: '明细行' } })
  fireEvent.change(screen.getByLabelText('输入显示名称 4'), { target: { value: '旧默认值' } })
  expect(result()).toEqual(inputs.map((field, index) => ({ ...field, ...(index === 0 ? { label: '费用文件' } : index === 1 ? { description: '明细行' } : index === 3 ? { label: '旧默认值' } : {}) })))
  expect(screen.queryByLabelText('输入默认值 4')).not.toBeInTheDocument()
  expect(screen.getByText('现有选项包含复杂配置，已保留；请在高级 JSON 中编辑。')).toBeInTheDocument()
})

it('does not replace a field name with a blank or duplicate name', () => {
  render(<Editor inputs={[{ name: 'first', type: 'string' }, { name: 'second', type: 'string' }]} />)
  for (const name of ['', 'second']) {
    fireEvent.change(screen.getByLabelText('输入名称 1'), { target: { value: name } })
    expect(screen.getByRole('alert')).toHaveTextContent('字段名称不能为空或重复')
    expect(result()[0].name).toBe('first')
  }
  fireEvent.change(screen.getByLabelText('输入名称 1'), { target: { value: 'renamed' } })
  expect(result()[0].name).toBe('renamed')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('displays implicit text and required defaults without adding them to the saved configuration', () => {
  render(<Editor inputs={[{ name: 'legacy', default: '001', options: ['001', '002'] }]} />)
  expect(screen.getByLabelText('输入类型 1')).toHaveValue('string')
  expect(screen.getByLabelText('必填 1')).toBeChecked()
  expect(screen.getByLabelText('输入默认值 1')).toHaveValue('001')
  fireEvent.change(screen.getByLabelText('输入显示名称 1'), { target: { value: '旧字段' } })
  expect(result()).toEqual([{ name: 'legacy', default: '001', options: ['001', '002'], label: '旧字段' }])
  fireEvent.change(screen.getByLabelText('输入默认值 1'), { target: { value: '002' } })
  expect(result()[0].default).toBe('002')
})
