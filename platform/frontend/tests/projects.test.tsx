import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import DeveloperTools from '@/app/projects/[id]/DeveloperTools'

vi.mock('@/lib/platform', () => ({ api: vi.fn() }))
vi.mock('@/app/components/LocalAgentSession', () => ({ default: () => <div>项目会话</div> }))
vi.mock('@/app/components/RequirementPackageMaterials', () => ({ default: () => null }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })
beforeEach(() => { vi.mocked(api).mockReset() })
const project = { id: 'p', name: '资源测试', description: '平台测试', main_workflow_id: 'p', members: [{ id: 'p', name: '主流程', revision: 1 }] }
const row = { collection: 'resources', key: 'r1', revision: 2, value: { owner: 'a' } }
const task = { id: 't1', request_key: 'request-1', status: 'waiting_input', mode: 'workflow', inputs: {}, outputs: { reason: '等待资源' }, runs: [] }
async function setup(currentTask = task) {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (options?.method === 'PUT') throw new Error('记录已更新，请重新读取')
    if (path.endsWith('/records')) return [row] as never
    if (path.endsWith('/tasks') && options?.method === 'POST') return currentTask as never
    if (path.endsWith('/tasks')) return [currentTask] as never
    if (path.includes('/tasks/t1')) return currentTask as never
    return project as never
  })
  await act(async () => { render(<DeveloperTools id="p" />) })
}

it('provides distinct workflow and Lilies task entry points with a request identifier', async () => {
  await setup()
  fireEvent.click(await screen.findByRole('button', { name: '交给统筹处理' }))
  fireEvent.change(screen.getByLabelText('业务请求标识'), { target: { value: 'request-1' } })
  fireEvent.change(screen.getByLabelText('业务输入'), { target: { value: '{"application":"a"}' } })
  fireEvent.change(screen.getByLabelText('统筹处理要求'), { target: { value: '处理此申请' } })
  fireEvent.click(screen.getByRole('button', { name: '开始处理' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/projects/p/tasks', {
    method: 'POST', body: JSON.stringify({ request_key: 'request-1', mode: 'agent', workflow_id: 'p', inputs: { application: 'a' }, message: '处理此申请' }),
  }))
  expect(await screen.findByText('业务结果')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('任务补充说明'), { target: { value: '已释放' } })
  fireEvent.click(screen.getByRole('button', { name: '补充并继续' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/projects/p/tasks/t1/resume', { method: 'POST', body: JSON.stringify({ message: '已释放' }) }))
})

it('sends the viewed record revision and retains edits when an update conflicts', async () => {
  const intervals = vi.spyOn(window, 'setInterval')
  await setup()
  fireEvent.click(await screen.findByRole('tab', { name: '业务记录' }))
  fireEvent.click(await screen.findByRole('button', { name: '编辑' }))
  fireEvent.change(screen.getByLabelText('记录内容'), { target: { value: '{"owner":""}' } })
  fireEvent.click(screen.getByRole('button', { name: '保存记录' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/projects/p/records/resources/r1', {
    method: 'PUT', body: JSON.stringify({ value: { owner: '' }, expected_revision: 2 }),
  }))
  expect(await screen.findByRole('alert')).toHaveTextContent('记录已更新')
  expect(screen.getByLabelText('记录内容')).toHaveValue('{"owner":""}')
  await act(async () => { (intervals.mock.calls.at(-1)![0] as () => void)() })
  expect(screen.getByRole('alert')).toHaveTextContent('记录已更新')
})

it.each(['/records', '/tasks/t1'])('clears a transient %s polling failure after the same read recovers', async failedPath => {
  const intervals = vi.spyOn(window, 'setInterval')
  await setup()
  fireEvent.click(await screen.findByRole('tab', { name: '项目任务' }))
  fireEvent.click(await screen.findByRole('button', { name: 'request-1 · 等待补充' }))
  await screen.findByText('业务结果')
  const healthy = vi.mocked(api).getMockImplementation()!
  let offline = true
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (offline && path.endsWith(failedPath)) throw new Error('暂时无法连接')
    return healthy(path, options)
  })
  await act(async () => { (intervals.mock.calls.at(-1)![0] as () => void)() })
  expect(screen.getByRole('alert')).toHaveTextContent('暂时无法连接')
  offline = false
  await act(async () => { (intervals.mock.calls.at(-1)![0] as () => void)() })
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '补充并继续' })).toBeEnabled()
})

it('shows the current agent continuation reason and preserves the original request identifier', async () => {
  const current = { ...task, mode: 'agent', request_key: '等待环境批准',
    outputs: { reason: '', message: '环境已就绪，等待补充测量数据' } }
  await setup(current)
  fireEvent.click(await screen.findByRole('tab', { name: '项目任务' }))
  fireEvent.click(await screen.findByRole('button', { name: '环境已就绪，等待补充测量数据 · 等待补充' }))
  expect(await screen.findByRole('heading', { name: '环境已就绪，等待补充测量数据' })).toBeInTheDocument()
  expect(screen.getByText('业务请求标识：等待环境批准')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '补充并继续' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/projects/p/tasks/t1/resume', {
    method: 'POST', body: JSON.stringify({ message: '' }),
  }))
})
