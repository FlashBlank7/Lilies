import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ProjectConversations from '@/app/components/ProjectConversations'
import { api } from '@/lib/platform'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
vi.mock('@/app/components/AuthBoundary', () => ({ useAccount: () => ({ id: 'user' }) }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })
beforeEach(() => { vi.mocked(api).mockReset(); sessionStorage.clear() })

const base = '/api/v1/projects/p/conversations'
const a = { id: 'a', title: '设计分析', status: 'running' }
const b = { id: 'b', title: '模型训练', status: 'idle' }
const updated = vi.fn()
function setup(rows = [a, b], onSent = vi.fn()) {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === base) return (options?.method === 'POST' ? { ...b, id: 'new', title: '新会话' } : rows) as never
    if (options?.method === 'PATCH') return { ...a, title: JSON.parse(options.body as string).title } as never
    const current = path.startsWith(base + '/a') ? a : b
    return { provider: 'api', status: current.status, revision: 1, events: options ? [] : [
      { id: current.id, kind: 'assistant', text: current.title + '的记录', time: '' }],
      has_more: false, first_cursor: current.id, last_cursor: current.id, error: '' } as never
  })
  render(<ProjectConversations id="p" items={[]} canConfigureModel={false} onUpdated={updated} onSent={onSent} />)
  return onSent
}

it('switches histories and unsent drafts without stopping a background conversation', async () => {
  setup()
  await screen.findByText('设计分析的记录')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '甲草稿' } })
  fireEvent.change(screen.getByLabelText('选择会话'), { target: { value: 'b' } })
  await screen.findByText('模型训练的记录')
  expect(screen.queryByText('设计分析的记录')).not.toBeInTheDocument()
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '乙草稿' } })
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/stop'))).toBe(false)
  fireEvent.change(screen.getByLabelText('选择会话'), { target: { value: 'a' } })
  await screen.findByText('设计分析的记录')
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('甲草稿')
  fireEvent.click(screen.getByRole('button', { name: '停止' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith(base + '/a/stop', { method: 'POST' }))
  expect(vi.mocked(api).mock.calls.some(([path]) => path === base + '/b/stop')).toBe(false)
})

it('creates and renames a conversation without starting a model turn', async () => {
  setup([])
  fireEvent.click(await screen.findByRole('button', { name: '新建会话' }))
  await waitFor(() => expect(screen.getByLabelText('选择会话')).toHaveValue('new'))
  expect(api).toHaveBeenCalledWith(base, { method: 'POST', body: JSON.stringify({ title: '新会话' }) })
  fireEvent.change(screen.getByLabelText('会话名称'), { target: { value: '新数据预测' } })
  fireEvent.click(screen.getByRole('button', { name: '重命名' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith(base + '/new', { method: 'PATCH', body: JSON.stringify({ title: '新数据预测' }) }))
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/messages'))).toBe(false)
})

it('a slow send in the previous conversation cannot clear the newly selected draft', async () => {
  const sent = setup()
  await screen.findByText('设计分析的记录')
  const healthy = vi.mocked(api).getMockImplementation()!
  let finish!: (value: never) => void
  vi.mocked(api).mockImplementation(async (path, options) => path === base + '/a/messages'
    ? new Promise(resolve => { finish = resolve }) : healthy(path, options))
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '发给甲' } })
  fireEvent.click(screen.getByRole('button', { name: '发送补充' }))
  await waitFor(() => expect(finish).toBeDefined())
  fireEvent.change(screen.getByLabelText('选择会话'), { target: { value: 'b' } })
  await screen.findByText('模型训练的记录')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '保留乙的输入' } })
  const count = sent.mock.calls.length
  await act(async () => { finish({} as never) })
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('保留乙的输入')
  expect(sent).toHaveBeenCalledTimes(count)
})
