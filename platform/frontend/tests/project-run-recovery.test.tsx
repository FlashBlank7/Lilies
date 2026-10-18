import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import ProjectRunPanel from '@/app/components/ProjectRunPanel'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (value: string) => value }))
afterEach(cleanup)
beforeEach(() => { vi.mocked(api).mockReset() })
const members = [{ id: 'p', name: '数据流程', description: '', revision: 1, purpose: 'business' }]
const oldTask = { id: 'old', status: 'succeeded', inputs: { source_path: 'requirement-package/old.xlsx' }, outputs: { delivery: { paths: { report: 'results/old/report.md' } } } }
const newTask = { id: 'new', status: 'succeeded', inputs: { source_path: 'requirement-package/new.xlsx' }, outputs: { delivery: { paths: { report: 'results/new/report.md' } } } }
async function startAfterSuccess(second: () => Promise<unknown>) {
  let requests = 0
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft')) return { snapshot: { workflow: { nodes: [{ type: 'start', config: { inputs: [{ name: 'source_path', type: 'file', default: 'requirement-package/old.xlsx' }] } }] } } } as never
    if (path.endsWith('/workspace/files')) return [{ path: 'requirement-package/old.xlsx' }, { path: 'requirement-package/new.xlsx' }] as never
    if (options?.method === 'POST') { requests++; return (requests === 1 ? oldTask : requests === 2 ? await second() : newTask) as never }
    throw Error(path)
  })
  await act(async () => { render(<ProjectRunPanel projectId="p" members={members} />) })
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  expect(await screen.findByRole('link', { name: '下载完整报告 ↓' })).toHaveAttribute('href', expect.stringContaining('/results/old/'))
  fireEvent.change(screen.getByRole('combobox', { name: '为 source_path 选择项目文件' }), { target: { value: 'requirement-package/new.xlsx' } })
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
}
it('removes prior success while the new task request is pending, then shows only its report', async () => {
  let resolve!: (value: unknown) => void
  const pending = new Promise(done => { resolve = done })
  await startAfterSuccess(() => pending)
  expect(screen.getByRole('button', { name: '正在启动…' })).toBeDisabled()
  expect(screen.queryByRole('region', { name: '本次运行结果' })).not.toBeInTheDocument()
  expect(screen.queryByRole('link', { name: '下载完整报告 ↓' })).not.toBeInTheDocument()
  await act(async () => { resolve(newTask) })
  expect(await screen.findByRole('link', { name: '下载完整报告 ↓' })).toHaveAttribute('href', expect.stringContaining('/results/new/'))
})
it('keeps rejected creation separate from prior success and retries using the selected input', async () => {
  await startAfterSuccess(async () => { throw Error('HTTP 503 创建任务暂时失败') })
  expect(await screen.findByRole('alert')).toHaveTextContent('503')
  expect(screen.queryByRole('region', { name: '本次运行结果' })).not.toBeInTheDocument()
  expect(screen.getByRole('textbox', { name: 'source_path' })).toHaveValue('requirement-package/new.xlsx')
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  expect(await screen.findByRole('link', { name: '下载完整报告 ↓' })).toHaveAttribute('href', expect.stringContaining('/results/new/'))
  const requests = vi.mocked(api).mock.calls.filter(([, options]) => options?.method === 'POST')
  expect(requests).toHaveLength(3)
  expect(JSON.parse(requests[2][1]!.body as string).inputs).toEqual({ source_path: 'requirement-package/new.xlsx' })
})
it('does not restore prior reports after execution failure and recovers to a new task', async () => {
  await startAfterSuccess(async () => ({ id: 'failed', status: 'failed', outputs: {}, error: '文件类型错误，请重新选表' }))
  await screen.findByRole('heading', { name: '运行失败' })
  expect(screen.queryByRole('link', { name: '下载完整报告 ↓' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  expect(await screen.findByRole('link', { name: '下载完整报告 ↓' })).toHaveAttribute('href', expect.stringContaining('/results/new/'))
})
it('treats omitted required as mandatory, without starting or retaining a prior successful result', async () => {
  await startAfterSuccess(async () => newTask)
  await screen.findByRole('heading', { name: '运行完成' })
  fireEvent.change(screen.getByRole('textbox', { name: 'source_path' }), { target: { value: '' } })
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('请填写 source_path')
  expect(screen.queryByRole('region', { name: '本次运行结果' })).not.toBeInTheDocument()
  expect(vi.mocked(api).mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(2)
})
