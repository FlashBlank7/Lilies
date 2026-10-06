import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import ModelingPanel from '@/app/components/ModelingPanel'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
vi.mock('recharts', () => ({ ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>, BarChart: () => null, Bar: () => null, CartesianGrid: () => null, LineChart: () => null, Line: () => null, Tooltip: () => null, XAxis: () => null, YAxis: () => null }))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const study = { id: 'study', name: '温度预测', dataset_id: 'a', status: 'running', next_action: '正在训练模型', trials_used: 0, budget: { seconds: 1800, trials: 3 }, evaluation: { metric: 'mae', split: 'group' } }
const datasets = ['a', 'b'].map(id => ({ id, name: id + '.csv', status: 'profiled', mapping: { kind: 'tabular', target: 'y' } }))
const detail = (index: number) => ({ ...datasets[index], profile: { sampled: false, rows: index ? 200 : 100, duplicates: 0, columns: [{ name: 'y', dtype: 'float64', missing: 0, unique: 20 }] } })
const candidate = { id: 'candidate', task_id: 'task', run_id: 'run', hypothesis: '当前候选', status: 'running', trials: [], current: { model: 'linear', slot: 0 } }

beforeEach(() => { vi.useFakeTimers(); vi.mocked(api).mockReset(); sessionStorage.clear() })
afterEach(() => { cleanup(); vi.useRealTimers() })

it.each(['success', 'error'])('keeps newer study and dataset lists when an older poll ends with %s', async outcome => {
  const olderStudies = deferred<unknown>()
  const olderDatasets = deferred<unknown>()
  let studyReads = 0, datasetReads = 0
  vi.mocked(api).mockImplementation(async path => {
    if (path.includes('/modeling/studies?')) {
      studyReads += 1
      if (studyReads === 2) return olderStudies.promise as never
      return [studyReads > 2 ? { ...study, status: 'finished', next_action: '本轮训练已完成' } : study] as never
    }
    if (path.includes('/datasets?')) {
      datasetReads += 1
      if (datasetReads === 2) return olderDatasets.promise as never
      return (datasetReads > 2 ? datasets : [datasets[0]]) as never
    }
    if (path.endsWith('/datasets/a')) return detail(0) as never
    return [] as never
  })
  await act(async () => { render(<ModelingPanel projectId="p" onContext={vi.fn()} />) })
  await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
  expect(screen.getByText('本轮训练已完成')).toBeInTheDocument()
  await act(async () => {
    if (outcome === 'error') olderStudies.reject(new Error('旧列表请求失败'))
    else olderStudies.resolve([study])
    olderDatasets.resolve([datasets[0]])
  })
  expect(screen.getByText('本轮训练已完成')).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '查看数据' })) })
  expect(screen.getByRole('option', { name: 'b.csv · b' })).toBeInTheDocument()
})

it.each(['success', 'error'])('keeps the selected dataset details when the previous selection ends with %s', async outcome => {
  const olderDetail = deferred<unknown>()
  vi.mocked(api).mockImplementation(async path => {
    if (path.includes('/modeling/studies?')) return [] as never
    if (path.includes('/datasets?')) return datasets as never
    if (path.endsWith('/datasets/a')) return olderDetail.promise as never
    if (path.endsWith('/datasets/b')) return detail(1) as never
    return [] as never
  })
  await act(async () => { render(<ModelingPanel projectId="p" onContext={vi.fn()} />) })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '查看数据' })) })
  await act(async () => { fireEvent.change(screen.getByRole('combobox', { name: '选择数据集' }), { target: { value: 'b' } }) })
  expect(screen.getByText(/完整扫描.*200 行/)).toBeInTheDocument()
  await act(async () => {
    if (outcome === 'error') olderDetail.reject(new Error('旧数据详情请求失败'))
    else olderDetail.resolve(detail(0))
  })
  expect(screen.getByRole('combobox', { name: '选择数据集' })).toHaveValue('b')
  expect(screen.getByText(/完整扫描.*200 行/)).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.queryByText(/数据尚未分析/)).not.toBeInTheDocument()
})

it('accepts a slow poll while a newer poll is still pending', async () => {
  const slow = deferred<unknown>()
  const newer = deferred<unknown>()
  let studyReads = 0
  vi.mocked(api).mockImplementation(async path => {
    if (path.includes('/modeling/studies?')) {
      studyReads += 1
      if (studyReads === 2) return slow.promise as never
      if (studyReads === 3) return newer.promise as never
      return [study] as never
    }
    if (path.includes('/datasets?')) return datasets as never
    return [] as never
  })
  await act(async () => { render(<ModelingPanel projectId="p" onContext={vi.fn()} />) })
  await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
  await act(async () => { slow.resolve([{ ...study, next_action: '第一批训练已开始' }]) })
  expect(screen.getByText('第一批训练已开始')).toBeInTheDocument()
  await act(async () => { newer.resolve([{ ...study, status: 'finished', next_action: '本轮训练已完成' }]) })
  expect(screen.getByText('本轮训练已完成')).toBeInTheDocument()
})

it.each(['success', 'error'])('keeps newer candidate progress when an older poll ends with %s', async outcome => {
  const older = deferred<unknown>()
  let reads = 0
  vi.mocked(api).mockImplementation(async path => {
    if (path.includes('/modeling/studies?')) return [study] as never
    if (path.includes('/datasets?')) return datasets as never
    if (path.includes('/candidates?')) {
      reads += 1
      if (reads === 2) return older.promise as never
      return [reads > 2 ? { ...candidate, current: { model: 'forest', slot: 1 } } : candidate] as never
    }
    return [] as never
  })
  await act(async () => { render(<ModelingPanel projectId="p" onContext={vi.fn()} />) })
  await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
  expect(screen.getByText(/正在训练：随机森林/)).toBeInTheDocument()
  await act(async () => {
    if (outcome === 'error') older.reject(new Error('旧候选请求失败'))
    else older.resolve([candidate])
  })
  expect(screen.getByText(/正在训练：随机森林/)).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it.each(['lists', 'candidates'])('keeps the newer %s error when an older request fails later', async source => {
  const older = deferred<unknown>()
  let reads = 0
  vi.mocked(api).mockImplementation(async path => {
    if (path.includes(source === 'lists' ? '/modeling/studies?' : '/candidates?')) {
      reads += 1
      if (reads === 2) return older.promise as never
      if (reads === 3) throw new Error('新轮询失败')
      return (source === 'lists' ? [study] : [candidate]) as never
    }
    if (path.includes('/modeling/studies?')) return [study] as never
    if (path.includes('/datasets?')) return datasets as never
    return [] as never
  })
  await act(async () => { render(<ModelingPanel projectId="p" onContext={vi.fn()} />) })
  await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
  expect(screen.getByRole('alert')).toHaveTextContent('新轮询失败')
  await act(async () => { older.reject(new Error('旧轮询失败')) })
  expect(screen.getByRole('alert')).toHaveTextContent('新轮询失败')
  expect(screen.getByRole('alert')).not.toHaveTextContent('旧轮询失败')
})

it('retains a requested candidate page that arrives after a newer first-page poll', async () => {
  const page = deferred<unknown>()
  const firstPage = Array.from({ length: 100 }, (_, index) => ({ ...candidate, id: 'candidate-' + index, hypothesis: '候选 ' + index, status: 'completed', current: undefined }))
  const refreshed = firstPage.map(item => item.id === 'candidate-0' ? { ...item, hypothesis: '已更新的候选' } : item)
  let polling = false
  vi.mocked(api).mockImplementation(async path => {
    if (path.includes('/modeling/studies?')) return [study] as never
    if (path.includes('/datasets?')) return datasets as never
    if (path.endsWith('/datasets/a')) return detail(0) as never
    if (path.includes('/candidates?')) return (path.includes('offset=100') ? page.promise : polling ? refreshed : firstPage) as never
    return [] as never
  })
  await act(async () => { render(<ModelingPanel projectId="p" onContext={vi.fn()} />) })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '查看结果' })) })
  fireEvent.click(screen.getByRole('tab', { name: '实验' }))
  fireEvent.click(screen.getByRole('button', { name: '加载更早实验' }))
  polling = true
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(screen.getByRole('heading', { name: '已更新的候选' })).toBeInTheDocument()
  await act(async () => { page.resolve([firstPage[0], { ...firstPage[0], id: 'historical', hypothesis: '更早的候选' }]) })
  expect(screen.getByRole('heading', { name: '已更新的候选' })).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: '更早的候选' })).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: '候选 0' })).not.toBeInTheDocument()
})
