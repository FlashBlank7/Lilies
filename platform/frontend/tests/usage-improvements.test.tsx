import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ImprovementsPage from '@/app/improvements/page'
import { api } from '@/lib/platform'

const mocks = vi.hoisted(() => ({ push: vi.fn(), user: { id: 'admin-one', role: 'admin' as 'admin' | 'member' } }))
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: mocks.push }) }))
vi.mock('@/lib/platform', () => ({ api: vi.fn() }))
vi.mock('@/app/components/AuthBoundary', () => ({ useAccount: () => mocks.user }))

const endpoint = '/api/v1/admin/improvements'
const recovery = {
  id: 'repair', kind: 'recovery', project_id: 'project-a', workflow_id: 'workflow-a', project_name: '库存项目', workflow_name: '分配流程',
  title: '调整后运行成功', explanation: '较早的失败任务之后出现了成功任务。', limitation: '还需确认业务结果是否符合需求。', next_step: '检查改动是否可供后续使用。', count: 2, status: 'new',
  workflow_changed: true, inputs_changed: false,
  tasks: [
    { id: 'task-before', status: 'failed', created_at: '2026-09-28T09:00:00Z', updated_at: '2026-09-28T09:01:00Z', revision: 1, purpose: 'business', error_kind: '数据格式问题' },
    { id: 'task-after', status: 'succeeded', created_at: '2026-09-28T09:05:00Z', updated_at: '2026-09-28T09:06:00Z', revision: 2, purpose: 'business', error_kind: '' },
  ],
}
const reusable = { ...recovery, id: 'reuse', kind: 'reusable_method', title: '同一流程多次成功', status: 'working', tasks: [] }
const operation = { ...recovery, id: 'operation', kind: 'operation_error', title: '编辑操作反复报错', status: 'dismissed', tasks: [], operations: [
  { id: 'operation-one', created: 1790586000, feature: 'edit_error', outcome: 'HTTP 409', resource_id: 'workflow-a' },
] }
function report() {
  return { items: [recovery, reusable, operation].map(item => ({ ...item })), last_scan: '2026-09-28T09:10:00Z', error: '', sampled_tasks: 100, truncated: true, window_days: 30, limit: 100, notes: ['任务成功不代表业务结果有用。'] }
}
function settings() {
  return { enabled: false, project_ids: [] as string[], daily_limit: 1, projects: [
    { id: 'project-a', name: '库存项目', available: true, reason: '' },
    { id: 'project-b', name: '客户分析', available: false, reason: '此项目未选择官方智能体' },
  ], error: '' }
}

beforeEach(() => {
  vi.mocked(api).mockReset(); mocks.push.mockReset(); mocks.user.role = 'admin'; sessionStorage.clear()
  vi.mocked(api).mockImplementation(async path => (path.endsWith('/settings') ? settings() : report()) as never)
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

it('only reads on entry and refreshes every 30 seconds while visible without starting the agent', async () => {
  vi.useFakeTimers()
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  await act(async () => { render(<ImprovementsPage />) })
  expect(api).toHaveBeenCalledTimes(2)
  expect(api).toHaveBeenCalledWith(endpoint)
  expect(api).toHaveBeenCalledWith(endpoint + '/settings')
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
  expect(api).toHaveBeenCalledTimes(3)
  visibility.mockReturnValue('hidden')
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
  expect(api).toHaveBeenCalledTimes(3)
  expect(vi.mocked(api).mock.calls.every(([, init]) => !init)).toBe(true)
  expect(screen.getByText(/个人会话正文未收集/)).toBeInTheDocument()
  expect(screen.getByText(/自动整理和“立即整理”都不调用模型/)).toBeInTheDocument()
})

it('filters signals, expands their actual records, scans on request, and exposes a Markdown download', async () => {
  render(<ImprovementsPage />)
  const card = await screen.findByRole('article', { name: recovery.title })
  expect(screen.getByText(/样本已截断/)).toBeInTheDocument()
  fireEvent.click(within(card).getByText('查看使用轨迹（2 条）'))
  expect(within(card).getByText('查看使用轨迹（2 条）').closest('details')).toHaveAttribute('open')
  expect(within(card).getByText('task-before')).toBeInTheDocument()
  expect(within(card).getByText('task-after')).toBeInTheDocument()
  expect(within(card).getByText(/数据格式问题/)).toBeInTheDocument()
  expect(within(card).getByRole('link', { name: '下载改进任务（Markdown）' })).toHaveAttribute('href', '/api/platform' + endpoint + '/repair/brief')
  expect(within(card).getByRole('link', { name: '下载改进任务（Markdown）' })).toHaveAttribute('download', '改进任务-repair.md')
  fireEvent.change(screen.getByLabelText('处理状态'), { target: { value: 'new' } })
  expect(screen.queryByRole('article', { name: reusable.title })).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('处理状态'), { target: { value: 'all' } })
  fireEvent.change(screen.getByLabelText('线索类别'), { target: { value: 'operation_error' } })
  expect(screen.queryByRole('article', { name: recovery.title })).not.toBeInTheDocument()
  expect(screen.getByRole('article', { name: operation.title })).toBeInTheDocument()
  fireEvent.click(screen.getByText('查看使用轨迹（1 条）'))
  expect(screen.getByText('编辑工作流')).toBeInTheDocument()
  expect(screen.getByText('HTTP 409')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '立即整理' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith(endpoint + '/scan', { method: 'POST' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '立即整理' })).toBeEnabled())
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('marks, dismisses, and reopens a signal without invoking the project agent', async () => {
  const data = report()
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/settings')) return settings() as never
    if (options?.method === 'PATCH') { data.items[0].status = JSON.parse(options.body as string).status; return {} as never }
    return { ...data } as never
  })
  render(<ImprovementsPage />)
  let card = await screen.findByRole('article', { name: recovery.title })
  fireEvent.click(within(card).getByRole('button', { name: '标为处理中' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith(endpoint + '/repair', { method: 'PATCH', body: JSON.stringify({ status: 'working' }) }))
  await waitFor(() => expect(within(screen.getByRole('article', { name: recovery.title })).getByRole('button', { name: '重新打开' })).toBeEnabled())
  card = screen.getByRole('article', { name: recovery.title })
  fireEvent.click(within(card).getByRole('button', { name: '忽略' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith(endpoint + '/repair', { method: 'PATCH', body: JSON.stringify({ status: 'dismissed' }) }))
  await waitFor(() => expect(within(card).getByRole('button', { name: '重新打开' })).toBeEnabled())
  fireEvent.click(within(card).getByRole('button', { name: '重新打开' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith(endpoint + '/repair', { method: 'PATCH', body: JSON.stringify({ status: 'new' }) }))
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('starts only after an explicit click and selects the returned conversation before navigating', async () => {
  vi.mocked(api).mockImplementation(async path => path.endsWith('/start')
    ? { project_id: 'project-a', conversation_id: 'improvement-chat', status: 'running' } as never : (path.endsWith('/settings') ? settings() : report()) as never)
  mocks.push.mockImplementation(() => {
    expect(sessionStorage.getItem('lilies:user:admin-one:project:project-a:conversation')).toBe('improvement-chat')
  })
  render(<ImprovementsPage />)
  const card = await screen.findByRole('article', { name: recovery.title })
  expect(mocks.push).not.toHaveBeenCalled()
  expect(within(card).getByText(/产生模型用量，并按项目原有权限处理和修改项目资源/)).toBeInTheDocument()
  fireEvent.click(within(card).getByRole('button', { name: '让项目智能体处理' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith(endpoint + '/repair/start', { method: 'POST' }))
  await waitFor(() => expect(mocks.push).toHaveBeenCalledWith('/projects/project-a'))
})

it('keeps a failed start on this page and permits retrying the same signal', async () => {
  vi.mocked(api).mockImplementation(async path => {
    if (path.endsWith('/start')) throw new Error('请先连接项目模型')
    if (path.endsWith('/settings')) return settings() as never
    return report() as never
  })
  render(<ImprovementsPage />)
  const card = await screen.findByRole('article', { name: recovery.title })
  fireEvent.click(within(card).getByRole('button', { name: '让项目智能体处理' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('请先连接项目模型')
  expect(mocks.push).not.toHaveBeenCalled()
  expect(sessionStorage.getItem('lilies:user:admin-one:project:project-a:conversation')).toBeNull()
  expect(within(card).getByRole('button', { name: '让项目智能体处理' })).toBeEnabled()
})

it('does not read or expose improvements to a member account', async () => {
  mocks.user.role = 'member'
  await act(async () => { render(<ImprovementsPage />) })
  expect(screen.getByText('只有平台管理员可以查看使用改进。')).toBeInTheDocument()
  expect(api).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: '立即整理' })).not.toBeInTheDocument()
})

it('saves selected automatic projects and limits without immediately starting an agent', async () => {
  let finish!: (value: never) => void
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/settings') && options?.method === 'PUT') return new Promise(resolve => { finish = resolve })
    return (path.endsWith('/settings') ? settings() : report()) as never
  })
  render(<ImprovementsPage />)
  const enabled = await screen.findByLabelText('自动交给官方智能体')
  expect(enabled).not.toBeChecked()
  expect(screen.getByLabelText('客户分析')).toBeDisabled()
  expect(screen.getByText('此项目未选择官方智能体')).toBeInTheDocument()
  expect(screen.getByLabelText('过去24小时最多处理条数')).toHaveValue(1)
  fireEvent.click(enabled)
  fireEvent.click(screen.getByLabelText('库存项目'))
  fireEvent.change(screen.getByLabelText('过去24小时最多处理条数'), { target: { value: '3' } })
  fireEvent.click(screen.getByRole('button', { name: '保存自动处理设置' }))
  expect(api).toHaveBeenCalledWith(endpoint + '/settings', { method: 'PUT', body: JSON.stringify({ enabled: true, project_ids: ['project-a'], daily_limit: 3 }) })
  expect(enabled).toBeDisabled()
  expect(screen.getByLabelText('库存项目')).toBeDisabled()
  expect(screen.getByLabelText('过去24小时最多处理条数')).toBeDisabled()
  await act(async () => { finish({ ...settings(), enabled: true, project_ids: ['project-a'], daily_limit: 3 } as never) })
  expect(await screen.findByText('自动处理设置已保存，后台稍后会处理符合条件的线索。')).toBeInTheDocument()
  expect(enabled).toBeEnabled()
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
  expect(mocks.push).not.toHaveBeenCalled()
})

it('requires an eligible selected project before enabling automatic handling', async () => {
  render(<ImprovementsPage />)
  fireEvent.click(await screen.findByLabelText('自动交给官方智能体'))
  fireEvent.click(screen.getByRole('button', { name: '保存自动处理设置' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('开启自动处理前，请至少选择一个可用项目。')
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options)).toBe(true)
})

it('keeps the main list usable when settings cannot load and offers a settings retry', async () => {
  vi.mocked(api).mockImplementation(async path => {
    if (path.endsWith('/settings')) throw new Error('自动设置暂时不可用')
    return report() as never
  })
  render(<ImprovementsPage />)
  expect(await screen.findByRole('article', { name: recovery.title })).toBeInTheDocument()
  expect(await screen.findByRole('alert')).toHaveTextContent('自动设置暂时不可用')
  expect(screen.getByRole('button', { name: '立即整理' })).toBeEnabled()
  expect(screen.getByRole('button', { name: '重新读取设置' })).toBeEnabled()
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('preserves edited settings after a failed save and explains the failure', async () => {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/settings') && options?.method === 'PUT') throw new Error('项目授权已变化，请重新检查')
    return (path.endsWith('/settings') ? settings() : report()) as never
  })
  render(<ImprovementsPage />)
  fireEvent.click(await screen.findByLabelText('自动交给官方智能体'))
  fireEvent.click(screen.getByLabelText('库存项目'))
  fireEvent.click(screen.getByRole('button', { name: '保存自动处理设置' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('项目授权已变化，请重新检查')
  expect(screen.getByLabelText('自动交给官方智能体')).toBeChecked()
  expect(screen.getByLabelText('库存项目')).toBeChecked()
  expect(screen.getByRole('button', { name: '保存自动处理设置' })).toBeEnabled()
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('does not overwrite unsaved settings when the report refreshes', async () => {
  vi.useFakeTimers()
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  await act(async () => { render(<ImprovementsPage />) })
  fireEvent.click(screen.getByLabelText('自动交给官方智能体'))
  fireEvent.click(screen.getByLabelText('库存项目'))
  fireEvent.change(screen.getByLabelText('过去24小时最多处理条数'), { target: { value: '5' } })
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
  expect(screen.getByLabelText('自动交给官方智能体')).toBeChecked()
  expect(screen.getByLabelText('库存项目')).toBeChecked()
  expect(screen.getByLabelText('过去24小时最多处理条数')).toHaveValue(5)
  expect(vi.mocked(api).mock.calls.filter(([path]) => path.endsWith('/settings'))).toHaveLength(1)
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options)).toBe(true)
})

it('identifies an unobserved historical signal without marking it resolved', async () => {
  vi.mocked(api).mockImplementation(async path => (path.endsWith('/settings') ? settings() : {
    ...report(), items: [{ ...recovery, active: false }, { ...reusable, active: true }],
  }) as never)
  render(<ImprovementsPage />)
  const historical = await screen.findByRole('article', { name: recovery.title })
  expect(within(historical).getByText('本轮未再观察到：可能已变化，或不在当前样本中')).toBeInTheDocument()
  expect(within(historical).getByText('待处理')).toBeInTheDocument()
  expect(within(historical).queryByText('已解决')).not.toBeInTheDocument()
  expect(within(screen.getByRole('article', { name: reusable.title })).queryByText(/本轮未再观察到/)).not.toBeInTheDocument()
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options)).toBe(true)
})
