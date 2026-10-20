import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import TrainingComparison, { type TrainingTrial } from '@/app/components/TrainingComparison'
import { ProjectTaskOutput } from '@/app/components/ProjectRunPanel'
import { api } from '@/lib/platform'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (value: string) => value }))
beforeEach(() => { vi.mocked(api).mockReset() })
afterEach(cleanup)

function trial(slot: number, metrics: Record<string, unknown>, baseline: Record<string, unknown>, model = 'linear'): TrainingTrial {
  return { slot, model, status: 'completed', metrics, baseline } as TrainingTrial
}

function metricSelector() { return screen.getByRole('combobox', { name: '比较指标' }) }
function selectMetric(metric: string) { fireEvent.change(metricSelector(), { target: { value: metric } }) }
function rows() { return within(screen.getByRole('table', { name: '训练指标比较' })).getAllByRole('row').slice(1) }
function cellText(row: HTMLElement) {
  return within(row).getAllByRole('cell').map(cell => (cell.textContent || '').replace(/\s+/g, ''))
}

function expectValues(row: HTMLElement, validation: string, baseline: string, difference: string | RegExp) {
  const cells = cellText(row)
  expect(cells[0]).toBe(validation)
  expect(cells[1]).toBe(baseline)
  if (typeof difference === 'string') expect(cells[2]).toBe(difference.replace(/\s+/g, ''))
  else expect(cells[2]).toMatch(difference)
}

it('shows the actual input and run time before results and updates both when opening a different run', () => {
  const task = (id: string, source: string, created: string) => ({
    id, status: 'succeeded', created_at: created,
    inputs: {source_path: source}, outputs: {training: {
      id: `${id}-candidate`, study_id: `${id}-study`,
      trials: [trial(0, {accuracy: .9}, {accuracy: .6})],
    }},
  })
  const first = task('first', 'requirement-package/a/classification-2.csv', '2026-10-06T03:08:05Z')
  const {rerender} = render(<ProjectTaskOutput projectId="p" task={first as never}/>)
  const source = screen.getByRole('region', {name: '本次运行来源'})
  expect(source).toHaveTextContent('输入资料：classification-2.csv')
  expect(source.querySelector('time')).toHaveAttribute('datetime', first.created_at)
  expect(source.compareDocumentPosition(screen.getByRole('region', {name: '训练比较'})) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  const next = task('second', 'requirement-package/b/classification.csv', '2026-10-06T02:57:25Z')
  rerender(<ProjectTaskOutput projectId="p" task={next as never}/>)
  expect(source).toHaveTextContent('输入资料：classification.csv')
  expect(source).not.toHaveTextContent('classification-2.csv')
  expect(source.querySelector('time')).toHaveAttribute('datetime', next.created_at)
  rerender(<ProjectTaskOutput projectId="p" task={{id:'old',status:'succeeded',created_at:'invalid',outputs:{}} as never}/>)
  expect(screen.queryByRole('region', {name: '本次运行来源'})).not.toBeInTheDocument()
  expect(api).not.toHaveBeenCalled()
})

it('offers metrics from every trial and baseline, preserving trial order and comparing each row to its own baseline', () => {
  render(<TrainingComparison trials={[
    trial(3, { accuracy: .8, mae: 2 }, { accuracy: .4, mae: 5 }),
    trial(0, { accuracy: .6, rmse: 7 }, { accuracy: .55, rmse: 6, baseline_only: 12 }),
  ]}/> )
  const section = within(screen.getByRole('region', { name: '训练比较' }))
  expect(section.getByRole('heading', { name: '训练比较', level: 3 })).toBeInTheDocument()
  const options = [...(metricSelector() as HTMLSelectElement).options].map(option => option.value)
  expect(new Set(options)).toEqual(new Set(['accuracy', 'mae', 'rmse', 'baseline_only']))
  expect(metricSelector()).toHaveValue('accuracy')
  expect(rows()[0]).toHaveTextContent(/试验\s*4|第\s*4\s*次/)
  expect(rows()[1]).toHaveTextContent(/试验\s*1|第\s*1\s*次/)
  expectValues(rows()[0], '80%', '40%', '+40 个百分点')
  expectValues(rows()[1], '60%', '55%', '+5 个百分点')

  selectMetric('mae')
  expectValues(rows()[0], '2', '5', '-3（原单位）')
  expectValues(rows()[1], '未记录', '未记录', '—')
  selectMetric('baseline_only')
  expectValues(rows()[0], '未记录', '未记录', '—')
  expectValues(rows()[1], '未记录', '12', '—')
  expect(api).not.toHaveBeenCalled()
})

it.each(['accuracy', 'precision', 'recall'])('formats %s as percentages and reports percentage point differences', metric => {
  render(<TrainingComparison trials={[
    trial(0, { [metric]: .812345 }, { [metric]: .412345 }),
    trial(1, { [metric]: 0 }, { [metric]: 0 }),
  ]}/> )
  expectValues(rows()[0], '81.235%', '41.235%', '+40 个百分点')
  expectValues(rows()[1], '0%', '0%', /^0(?:个百分点)?$/)
})

it.each(['mae', 'rmse'])('keeps %s in its original units, rounds display values, and subtracts the unrounded values', metric => {
  render(<TrainingComparison trials={[
    trial(0, { [metric]: 1.23456 }, { [metric]: 2.45678 }),
  ]}/> )
  expectValues(rows()[0], '1.2346', '2.4568', '-1.2222（原单位）')
  expect(within(screen.getByRole('region', { name: '训练比较' })).getByText(/越低越好/)).toBeInTheDocument()
})

it.each([
  { metric: 'r2', validation: -.35, baseline: -.8, validationText: '-0.35', baselineText: '-0.8', difference: '+0.45（值差）' },
  { metric: 'macro_f1', validation: .65, baseline: .4, validationText: '0.65', baselineText: '0.4', difference: '+0.25（值差）' },
  { metric: 'roc_auc', validation: .875, baseline: .5, validationText: '0.875', baselineText: '0.5', difference: '+0.375（值差）' },
])('shows $metric as a score difference, including valid negative R² values', ({ metric, validation, baseline, validationText, baselineText, difference }) => {
  render(<TrainingComparison trials={[trial(0, { [metric]: validation }, { [metric]: baseline })]}/> )
  expectValues(rows()[0], validationText, baselineText, difference)
  expect(cellText(rows()[0]).slice(0, 3).join(' ')).not.toContain('%')
})

it('distinguishes missing, unavailable, and zero metrics and never computes a difference from invalid values', () => {
  const invalid = [
    { value: undefined, display: '未记录' },
    { value: null, display: '无法计算' },
    { value: Number.NaN, display: '无法计算' },
    { value: Number.POSITIVE_INFINITY, display: '无法计算' },
    { value: Number.NEGATIVE_INFINITY, display: '无法计算' },
    { value: '0.5', display: '无法计算' },
    { value: '', display: '无法计算' },
    { value: false, display: '无法计算' },
  ]
  const trials = [
    ...invalid.map(({ value }, slot) => trial(slot, { mae: value }, { mae: 1 })),
    ...invalid.map(({ value }, slot) => trial(slot + invalid.length, { mae: 1 }, { mae: value })),
    trial(invalid.length * 2, {}, {}),
    trial(invalid.length * 2 + 1, { mae: 0 }, { mae: 0 }),
  ]
  render(<TrainingComparison trials={trials}/> )
  invalid.forEach(({ display }, index) => {
    expectValues(rows()[index], display, '1', '—')
    expectValues(rows()[index + invalid.length], '1', display, '—')
  })
  expectValues(rows()[invalid.length * 2], '未记录', '未记录', '—')
  expectValues(rows()[invalid.length * 2 + 1], '0', '0', /^0(?:（原单位）)?$/)
  expect(screen.getByRole('table', { name: '训练指标比较' })).not.toHaveTextContent(/NaN|Infinity/)
})

it('keeps models, statuses, and failure details visible in older records without metric or baseline objects', () => {
  render(<TrainingComparison trials={[
    { slot: 0, model: 'forest', status: 'failed', error: '训练样本数不足' },
    { slot: 1, model: 'linear', status: 'completed' },
    { slot: 2, model: 'svm', status: 'running' },
  ]}/> )
  expect(screen.getByText('本次训练未保存可比较的指标。')).toBeInTheDocument()
  expect(screen.queryByRole('combobox', { name: '比较指标' })).not.toBeInTheDocument()
  const savedRows = rows()
  expect(savedRows).toHaveLength(3)
  const models = ['forest', 'linear', 'svm']
  savedRows.forEach((row, index) => {
    expect(within(row).getByRole('rowheader')).toHaveTextContent(models[index])
    expect(row).toHaveTextContent(new RegExp(`试验\\s*${index + 1}|第\\s*${index + 1}\\s*次`))
    expectValues(row, '未记录', '未记录', '—')
  })
  expect(cellText(savedRows[0])[3]).toBe('失败训练样本数不足')
  expect(cellText(savedRows[1])[3]).toBe('已完成')
  expect(cellText(savedRows[2])[3]).toBe('训练中')
  expect(api).not.toHaveBeenCalled()
})

it('shows unknown metrics as raw differences without assuming which direction is better', () => {
  render(<TrainingComparison trials={[
    trial(0, { mae: 2, custom_score: .42 }, { mae: 5, custom_score: .4 }),
    trial(1, { mae: 3, custom_score: .2 }, { mae: 6, custom_score: .4 }),
  ]}/> )
  selectMetric('custom_score')
  expectValues(rows()[0], '0.42', '0.4', '+0.02（值差）')
  expectValues(rows()[1], '0.2', '0.4', '-0.2（值差）')
  expect(within(screen.getByRole('region', { name: '训练比较' })).getByText(/评价方向未说明/)).toBeInTheDocument()
  for (const row of rows()) expect(row).not.toHaveTextContent(/更好|更差|改善|退步|优于|劣于|提升|下降/)
})

it('resets the metric and updates same-named models when switching tasks, without mixing in independent test scores', () => {
  const task = (id: string, accuracy: number, baseline: number, r2: number, testScore: number) => ({
    id, status: 'succeeded', outputs: {
      training: { id: `${id}-candidate`, study_id: `${id}-study`, trials: [
        trial(0, { accuracy, r2 }, { accuracy: baseline, r2: -.1 }),
      ] },
      evaluation: { rows: 8, label: '保留测试', metrics: { accuracy: testScore, holdout_only: 123.45 } },
    },
  })
  const { rerender } = render(<ProjectTaskOutput projectId="p" task={task('first', .9, .5, -.4, .77777) as never}/> )
  expectValues(rows()[0], '90%', '50%', '+40 个百分点')
  expect([...(metricSelector() as HTMLSelectElement).options].map(option => option.value)).not.toContain('holdout_only')
  expect(screen.getByRole('table', { name: '训练指标比较' })).not.toHaveTextContent(/77\.777|123\.45/)
  expect(within(screen.getByRole('region', { name: '独立测试' })).getByText(/accuracy: 0.77777/)).toBeInTheDocument()
  selectMetric('r2')
  expectValues(rows()[0], '-0.4', '-0.1', '-0.3（值差）')

  rerender(<ProjectTaskOutput projectId="p" task={task('second', .6, .2, .25, .88888) as never}/> )
  expect(metricSelector()).toHaveValue('accuracy')
  expectValues(rows()[0], '60%', '20%', '+40 个百分点')
  const training = screen.getByRole('region', { name: '训练比较' })
  expect(training).not.toHaveTextContent(/90%|50%|-0\.4|77\.777|88\.888|123\.45/)
  selectMetric('r2')
  expectValues(rows()[0], '0.25', '-0.1', '+0.35（值差）')
  expect(within(screen.getByRole('region', { name: '独立测试' })).getByText(/accuracy: 0.88888/)).toBeInTheDocument()
  expect(api).not.toHaveBeenCalled()
})
