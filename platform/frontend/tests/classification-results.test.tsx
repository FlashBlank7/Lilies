import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import ClassificationResults, { type ClassificationResult } from '@/app/components/ClassificationResults'

afterEach(cleanup)
const report: ClassificationResult = {
  classes: [
    { label: 'pass', samples: 2, predicted_samples: 3, precision: 2/3, recall: 1, f1: .8 },
    { label: 'rare', samples: 1, predicted_samples: 0, precision: null, recall: 0, f1: 0 },
    { label: 'absent', samples: 0, predicted_samples: 0, precision: null, recall: null, f1: null },
  ],
  confusion_matrix: [[2, 0, 0], [1, 0, 0], [0, 0, 0]],
  baseline: { classes: [{ label: 'rare', samples: 1, recall: 0, precision: null, f1: 0 }] },
  folds: [{ fold: 1, classes: [{ label: 'rare', train_samples: 0, validation_samples: 1 }] }],
}

it('distinguishes zero recall from undefined metrics and reveals the actual class counts', () => {
  render(<ClassificationResults result={report}/>)
  const table = screen.getByRole('table', { name: '类别验证指标' })
  const rare = within(table).getByRole('row', { name: /^rare / })
  expect(within(rare).getByText('无法计算')).toBeInTheDocument()
  expect(within(rare).getAllByText('0.000')).toHaveLength(3)
  const absent = within(table).getByRole('row', { name: /^absent / })
  expect(within(absent).getAllByText('无法计算')).toHaveLength(4)
  expect(within(absent).getByText('缺少此类验证样本')).toBeInTheDocument()
  fireEvent.click(screen.getByText('各次验证的类别分布'))
  const folds = screen.getByRole('table', { name: '各次验证的类别分布' })
  expect(within(folds).getByText('训练时未见过此类')).toBeVisible()
  fireEvent.click(screen.getByText('查看混淆矩阵'))
  const matrix = screen.getByRole('table', { name: '验证混淆矩阵' })
  const rareMatrix = within(matrix).getByRole('row', { name: /^rare / })
  expect(within(rareMatrix).getAllByRole('cell').map(cell => cell.textContent)).toEqual(['1', '0', '0'])
})

it('keeps independent test terminology and supports older records without counts or matrices', () => {
  render(<ClassificationResults phase="test" result={{classes:[{label:'old',samples:0,precision:0,recall:0,f1:0}]}}/>)
  expect(screen.getByRole('heading', { name: '各类别测试结果' })).toBeInTheDocument()
  expect(screen.getByText('缺少此类测试样本')).toBeInTheDocument()
  expect(screen.getByText('未记录')).toBeInTheDocument()
  expect(screen.queryByText('基线召回率')).not.toBeInTheDocument()
  expect(screen.queryByText('查看混淆矩阵')).not.toBeInTheDocument()
})
