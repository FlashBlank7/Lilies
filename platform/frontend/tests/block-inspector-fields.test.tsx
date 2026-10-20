import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import BlockOptionField from '@/app/components/BlockOptionField'
import { BlockInstanceDetails, BlockPurpose } from '@/app/applications/[id]/block-catalog-panel'
import type { Block, BlockEditorField, WorkflowNode } from '@/lib/platform'

afterEach(cleanup)

it('shows translated choices and contextual help while keeping saved engine identifiers', () => {
  const field: BlockEditorField = { path: 'candidate.engine', label: '训练方式', control: 'enum',
    options: ['sklearn', 'optuna'], option_labels_zh: { sklearn: '基础模型比较（scikit-learn）', optuna: '自动搜索参数（Optuna）' },
    option_descriptions_zh: { sklearn: '先建立基础结果。', optuna: '在预算内搜索参数。' } }
  function Form() {
    const [value, setValue] = useState('sklearn')
    return <><BlockOptionField field={field} locale="zh" label="训练方式" value={value} onChange={setValue} /><output aria-label="保存值">{value}</output></>
  }
  render(<Form />)
  expect(screen.getByRole('option', { name: '基础模型比较（scikit-learn）' })).toHaveValue('sklearn')
  expect(screen.getByRole('combobox')).toHaveAccessibleDescription('先建立基础结果。')
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'optuna' } })
  expect(screen.getByRole('combobox')).toHaveAccessibleDescription('在预算内搜索参数。')
  expect(screen.getByLabelText('保存值')).toHaveTextContent('optuna')
})

it('selects candidate models without requiring machine identifiers to be typed, preserving unknown saved choices', () => {
  const field: BlockEditorField = { path: 'candidate.models', label: '候选模型', control: 'string_list',
    options: ['linear', 'forest'], option_labels_zh: { linear: '线性模型', forest: '随机森林' } }
  function Form() {
    const [value, setValue] = useState('linear\ncustom')
    return <><BlockOptionField field={field} locale="zh" label="候选模型" value={value} onChange={setValue} /><output aria-label="保存值">{JSON.stringify(value.split('\n'))}</output></>
  }
  render(<Form />)
  expect(screen.getByRole('checkbox', { name: 'custom' })).toBeChecked()
  fireEvent.click(screen.getByRole('checkbox', { name: '随机森林' }))
  fireEvent.click(screen.getByRole('checkbox', { name: '线性模型' }))
  expect(screen.getByLabelText('保存值')).toHaveTextContent('["custom","forest"]')
})

it('describes arbitrary ports honestly and shows code responsibility and configured inputs', () => {
  const block = { type: 'code', title: 'Python 代码', description: '执行 main(inputs)。', category: 'transform',
    input_ports: [{ name: 'input', value_type: 'any' }], output_ports: [{ name: 'output', value_type: 'any' }], config_schema: {},
    when_to_use: ['Process data.', '按确定规则清洗或汇总。'] } as Block
  const node: WorkflowNode = { id: 'expense', type: 'code', title: '费用汇总', description: '核对金额，按币种分别汇总。',
    block_version: 1, position: { x: 0, y: 0 }, retry: { enabled: false, max_attempts: 1, delay_seconds: 0 }, error_strategy: 'fail',
    config: { inputs: { source_path: '费用.csv', group_by: '按月、类别和币种' } } }
  render(<><BlockPurpose block={block} locale="zh" /><BlockInstanceDetails node={node} locale="zh" /></>)
  expect(screen.getByText('input: 任意类型')).toBeInTheDocument()
  expect(screen.getByText('output: 任意类型')).toBeInTheDocument()
  expect(screen.getByText('按确定规则清洗或汇总。')).toBeInTheDocument()
  expect(screen.queryByText('Process data.')).not.toBeInTheDocument()
  expect(screen.getByText('核对金额，按币种分别汇总。')).toBeInTheDocument()
  expect(screen.getByText('source_path、group_by')).toBeInTheDocument()
  expect(screen.getByText(/表单可改已传入的参数/)).toBeInTheDocument()
})
