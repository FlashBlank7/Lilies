import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import ModelingPanel from '@/app/components/ModelingPanel'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (s: string) => s }))
vi.mock('recharts', () => ({ ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>, BarChart: () => <div>分布图</div>, Bar: () => null, CartesianGrid: () => null, LineChart: () => <div>改进曲线</div>, Line: () => null, Tooltip: () => null, XAxis: () => null, YAxis: () => null }))
afterEach(cleanup)
const study = { id: 's', name: '温度预测', dataset_id: 'd', item_id: 'i', status: 'interrupted', next_action: '继续已保存的实验', trials_used: 2, baseline: { mae: .8 }, best: { candidate_id: 'c', slot: 0, score: .4, model: 'linear' }, budget: { seconds: 1800, trials: 30 }, evaluation: { metric: 'mae', split: 'group', target_score: .3 } }
const candidate = { id: 'c', task_id: 't', run_id: 'r', status: 'interrupted', hypothesis: '加入窗口均值', trials: [{ slot: 0, status: 'completed', model: 'linear', metrics: { mae: .4 }, seconds: 2 }], features_result: [{ name: 'temperature__mean', source: 'temperature', calculation: 'mean', window_seconds: 60 }] }
const dataset = { id: 'd', name: '设备数据', status: 'profiled', mapping: { target: 'y', kind: 'tabular' }, profile: { sampled: false, rows: 100, duplicates: 2, columns: [{ name: 'x', dtype: 'float64', unique: 90, missing: 3, outliers: 1, distribution: [{ label: '1–2', count: 10 }] }] } }
const trainingNote = { markdown: '## 固定划分\n\n本次使用按组隔离，保留全部验证样本。', note: { title: '线性模型 · 试验 1 · 训练笔记', trial: { status: 'completed' }, gaps: [], evaluation: { metric: 'mae' }, comparison: [{ sequence: 1, score: .4, best_score: .4, cumulative_seconds: 2 }] } }
beforeEach(() => {
  vi.mocked(api).mockReset(); sessionStorage.clear()
  vi.mocked(api).mockImplementation(async path => {
    if (path.endsWith('/trials/0/note')) return trainingNote as never
    if (path.endsWith('/datasets/d')) return dataset as never
    if (path.includes('/candidates')) return [candidate] as never
    if (path.includes('/modeling/studies')) return [study] as never
    if (path.includes('/datasets')) return [dataset] as never
    return {} as never
  })
})

it('uploads the selected file and retains its data tab and conversation context after remount', async () => {
  const original = vi.mocked(api).getMockImplementation()!
  const uploaded = { ...dataset, id: 'uploaded', name: 'new.csv', profile: undefined }
  let saved = false
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/datasets/upload')) { saved = true; return uploaded as never }
    if (path.endsWith('/datasets/uploaded')) return uploaded as never
    if (path.includes('/datasets?')) return (saved ? [uploaded, dataset] : [dataset]) as never
    return original(path, options)
  })
  const onContext = vi.fn()
  const mounted = render(<ModelingPanel projectId="p" onContext={onContext} />)
  await screen.findByText('温度预测')
  const file = new File(['x\n4\n'], 'new.csv', { type: 'text/csv' })
  fireEvent.change(screen.getByLabelText('上传建模数据'), { target: { files: [file] } })
  await waitFor(() => expect(screen.getByRole('combobox', { name: '选择数据集' })).toHaveValue('uploaded'))
  const call = vi.mocked(api).mock.calls.find(([path]) => path.endsWith('/datasets/upload'))!
  expect((call[1]?.body as FormData).get('file')).toBe(file)
  expect(onContext).toHaveBeenCalledWith({ dataset_id: 'uploaded', label: 'new.csv' }, expect.any(String))
  fireEvent.click(screen.getByRole('button', { name: '关闭阅读窗口' }))
  mounted.unmount()
  render(<ModelingPanel projectId="p" onContext={onContext} />)
  fireEvent.click(await screen.findByRole('button', { name: '查看结果' }))
  expect(screen.getByRole('tab', { name: '数据' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('combobox', { name: '选择数据集' })).toHaveValue('uploaded')
})

it('keeps the previous result after upload failure and allows choosing the same file again', async () => {
  const original = vi.mocked(api).getMockImplementation()!
  let attempts = 0
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/datasets/upload')) {
      if (!attempts++) throw new Error('上传连接中断，请重试')
      return dataset as never
    }
    return original(path, options)
  })
  const onContext = vi.fn()
  render(<ModelingPanel projectId="p" onContext={onContext} />)
  await screen.findByText('温度预测')
  const file = new File(['x\n4\n'], 'input.csv')
  fireEvent.change(screen.getByLabelText('上传建模数据'), { target: { files: [file] } })
  expect(await screen.findByRole('alert')).toHaveTextContent('上传连接中断')
  expect(onContext).not.toHaveBeenCalled()
  expect(screen.getByText('0.4')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('上传建模数据'), { target: { files: [file] } })
  await waitFor(() => expect(onContext).toHaveBeenCalledTimes(1))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('opens the exact trial note and model download, then returns to the experiment list', async () => {
  const onContext = vi.fn()
  render(<ModelingPanel projectId="p" onContext={onContext} />)
  fireEvent.click(await screen.findByRole('button', { name: '查看结果' }))
  fireEvent.click(screen.getByRole('tab', { name: '实验' }))
  fireEvent.click(await screen.findByRole('button', { name: '查看线性模型第 1 次训练笔记' }))
  expect(await screen.findByText('本次使用按组隔离，保留全部验证样本。')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '下载本次模型' })).toHaveAttribute('href', '/api/platform/api/v1/projects/p/modeling/studies/s/candidates/c/download?slot=0')
  expect(screen.getByRole('link', { name: '下载笔记与原始记录' })).toHaveAttribute('href', '/api/platform/api/v1/projects/p/modeling/studies/s/candidates/c/trials/0/note/download')
  fireEvent.change(screen.getByRole('combobox', { name: '训练笔记曲线横轴' }), { target: { value: 'cumulative_seconds' } })
  fireEvent.click(screen.getByRole('button', { name: '返回实验列表' }))
  expect(await screen.findByText('加入窗口均值')).toBeInTheDocument()
  expect(onContext).not.toHaveBeenCalled()
})

it('keeps compact conversation controls small while preserving results, notes and upload access', async () => {
  render(<ModelingPanel compact projectId="p" onContext={vi.fn()} />)
  expect(await screen.findByText('最佳 MAE 0.4')).toBeInTheDocument()
  expect(screen.getByLabelText('上传建模数据')).toBeInTheDocument()
  expect(screen.queryByText('继续已保存的实验')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '查看建模结果' }))
  expect(await screen.findByText('继续已保存的实验')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '下载模型' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('tab', { name: '实验' }))
  fireEvent.click(await screen.findByRole('button', { name: '查看线性模型第 1 次训练笔记' }))
  expect(await screen.findByText('本次使用按组隔离，保留全部验证样本。')).toBeInTheDocument()
})

it('keeps failed trials readable and retries a failed note request without claiming a model exists', async () => {
  const original = vi.mocked(api).getMockImplementation()!
  let failed = false
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/trials/0/note')) {
      if (!failed) { failed = true; throw new Error('连接中断') }
      return { ...trainingNote, note: { ...trainingNote.note, trial: { status: 'failed' }, gaps: ['训练失败，没有实际拟合参数。'] } } as never
    }
    return original(path, options)
  })
  render(<ModelingPanel projectId="p" onContext={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: '查看结果' }))
  fireEvent.click(screen.getByRole('tab', { name: '实验' }))
  fireEvent.click(await screen.findByRole('button', { name: '查看线性模型第 1 次训练笔记' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('连接中断')
  fireEvent.click(screen.getByRole('button', { name: '重试加载笔记' }))
  expect(await screen.findByText('有 1 项信息未记录')).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: '下载本次模型' })).not.toBeInTheDocument()
})

it('shows measured comparison, scopes feedback and resumes the original task', async () => {
  const onContext = vi.fn()
  render(<ModelingPanel projectId="p" onContext={onContext} />)
  expect(await screen.findByText('温度预测')).toBeInTheDocument()
  expect(screen.getByText('0.4')).toBeInTheDocument()
  expect(screen.getByText('0.8')).toBeInTheDocument()
  expect(screen.queryByText('50%')).not.toBeInTheDocument()
  await screen.findByRole('button', { name: '继续原任务' })
  fireEvent.click(screen.getByRole('button', { name: '继续原任务' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/projects/p/tasks/t/resume', { method: 'POST', body: JSON.stringify({ message: '继续已保存的建模实验' }) }))
  fireEvent.click(screen.getByRole('button', { name: '反馈这个结果' }))
  expect(onContext).toHaveBeenCalledWith(expect.objectContaining({ study_id: 's', candidate_id: 'c', task_id: 't', item_id: 'i' }), undefined)
})

it('opens data and feature details without sending a message and preserves the selected tab', async () => {
  const onContext = vi.fn()
  const mounted = render(<ModelingPanel projectId="p" onContext={onContext} />)
  fireEvent.click(await screen.findByRole('button', { name: '查看结果' }))
  expect(screen.getByRole('link', { name: '下载模型' })).toHaveAttribute('href', '/api/platform/api/v1/projects/p/modeling/studies/s/candidates/c/download')
  fireEvent.click(screen.getByRole('tab', { name: '数据' }))
  expect(await screen.findByText(/完整扫描.*100 行/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('tab', { name: '特征' }))
  expect(await screen.findByText('temperature__mean')).toBeInTheDocument()
  expect(onContext).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '关闭阅读窗口' }))
  mounted.unmount()
  render(<ModelingPanel projectId="p" onContext={onContext} />)
  fireEvent.click(await screen.findByRole('button', { name: '查看结果' }))
  expect(screen.getByRole('tab', { name: '特征' })).toHaveAttribute('aria-selected', 'true')
  fireEvent.click(await screen.findByRole('button', { name: '建议停用' }))
  expect(onContext).toHaveBeenCalledWith(expect.objectContaining({ candidate_id: 'c' }), expect.stringContaining('temperature'))
})

it('shows an actionable connection error rather than inventing a result', async () => {
  vi.mocked(api).mockRejectedValue(new Error('连接失败'))
  render(<ModelingPanel projectId="p" onContext={vi.fn()} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('连接失败')
  expect(screen.queryByText('当前最佳')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '重试连接' })).toBeInTheDocument()
})

it('does not claim offline table features were available before prediction', async () => {
  const original = vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.includes('/candidates')) return [{ ...candidate, features_result: [{ name: 'power_mean', source: 'power_mean', calculation: '原始字段' }] }] as never
    return original(path, options)
  })
  render(<ModelingPanel projectId="p" onContext={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: '查看结果' }))
  fireEvent.click(screen.getByRole('tab', { name: '特征' }))
  expect(await screen.findByText('power_mean', { selector: 'small' })).toBeInTheDocument()
  expect(screen.getByText('未设置时间窗口')).toBeInTheDocument()
  expect(screen.getByText('未记录可用时点条件，需结合数据来源确认')).toBeInTheDocument()
  expect(screen.queryByText('预测时点前可用记录')).not.toBeInTheDocument()
  expect(screen.queryByText(/特征窗口以预测时点为上限/)).not.toBeInTheDocument()
})

it('shows the recorded time window and availability condition for time series features', async () => {
  const original = vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.includes('/candidates')) return [{ ...candidate, features_result: [{ ...candidate.features_result[0], condition: '测量时间及可用时间不晚于预测时点' }] }] as never
    return original(path, options)
  })
  render(<ModelingPanel projectId="p" onContext={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: '查看结果' }))
  fireEvent.click(screen.getByRole('tab', { name: '特征' }))
  expect(await screen.findByText('60 秒')).toBeInTheDocument()
  expect(screen.getByText('测量时间及可用时间不晚于预测时点')).toBeInTheDocument()
  expect(screen.queryByText('未记录可用时点条件，需结合数据来源确认')).not.toBeInTheDocument()
})

it('shows measured constant prediction warnings and binds repair to the failed candidate', async () => {
  const original = vi.mocked(api).getMockImplementation()!
  const warning = '第 1 折对不同实测值给出了相同预测；请检查样本量、特征和参数。'
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.includes('/candidates')) return [{ ...candidate, trials: [{ ...candidate.trials[0], warnings: [warning] }] }] as never
    if (path.includes('/modeling/studies')) return [{ ...study, repair_candidate_id: 'c', next_action: '读取错误并提交修复候选' }] as never
    return original(path, options)
  })
  const onContext = vi.fn()
  render(<ModelingPanel projectId="p" onContext={onContext} />)
  expect(await screen.findByText(warning)).toBeInTheDocument()
  fireEvent.click(await screen.findByRole('button', { name: '修复并继续研究' }))
  expect(onContext).toHaveBeenCalledWith(expect.objectContaining({ candidate_id: 'c', study_id: 's' }), expect.stringContaining('关联的修复候选'))
})

it('ignores a previous study response arriving after the selected study changes', async () => {
  const original = vi.mocked(api).getMockImplementation()!
  const pending: ((value: unknown) => void)[] = []
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.includes('/studies/s/candidates')) return new Promise(resolve => pending.push(resolve)) as never
    if (path.includes('/studies/s2/candidates')) return [{ ...candidate, id: 'new', hypothesis: '新研究独立候选' }] as never
    if (path.includes('/modeling/studies')) return [study, { ...study, id: 's2', name: '新研究' }] as never
    return original(path, options)
  })
  render(<ModelingPanel projectId="p" onContext={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: '查看结果' }))
  fireEvent.click(screen.getByRole('tab', { name: '实验' }))
  fireEvent.change(screen.getByRole('combobox', { name: '选择建模研究' }), { target: { value: 's2' } })
  expect(await screen.findByRole('heading', { name: '新研究独立候选' })).toBeInTheDocument()
  await act(async () => { pending.forEach(resolve => resolve([candidate])) })
  expect(screen.queryByRole('heading', { name: candidate.hypothesis })).not.toBeInTheDocument()
  expect(screen.getByRole('heading', { name: '新研究独立候选' })).toBeInTheDocument()
})


it('explains AIDE branch selection and keeps each actual trial note accessible', async () => {
  const original = vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.includes('/candidates') && !path.endsWith('/note')) return [{ ...candidate, search_decision: { stage: 'debug', label: '修复失败方案' } }] as never
    if (path.includes('/modeling/studies') && !path.includes('/candidates')) return [{ ...study, search_strategy: 'aide' }] as never
    return original(path, options)
  })
  render(<ModelingPanel projectId="p" onContext={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: '查看结果' }))
  fireEvent.click(screen.getByRole('tab', { name: '实验' }))
  expect(await screen.findByText(/AIDE 选择搜索分支/)).toBeInTheDocument()
  expect(await screen.findByText(/AIDE：修复失败方案/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '查看线性模型第 1 次训练笔记' }))
  expect(await screen.findByText('本次使用按组隔离，保留全部验证样本。')).toBeInTheDocument()
})
