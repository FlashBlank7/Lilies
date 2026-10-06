import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ProjectConversation from '@/app/components/ProjectConversation'
import { api } from '@/lib/platform'
import type { ProjectTask } from '@/lib/project-progress'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
beforeEach(() => { vi.mocked(api).mockReset(); sessionStorage.clear() })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

const inspectedEvent = (taskId: string, requestId = 'current') => ({
  id: `read-${taskId}`, kind: 'result', result_kind: 'inspected', task_id: taskId,
  request_id: requestId, text: '读取成功', time: '', success: true,
})
function savedTask(id: string, status: string): ProjectTask {
  return { id, status, request_key: id, mode: 'workflow', purpose: 'business', item_id: '',
    feedback_task_id: '', message: '', error: '', inputs: { data_file: `data/${id}.csv` },
    presentation: {}, created_at: id === 'first' ? '2026-10-01T03:00:00Z' : '2026-10-02T04:00:00Z', updated_at: '', runs: [] }
}
async function setup(events: Record<string, unknown>[], tasks: ProjectTask[]) {
  const intervals = vi.spyOn(window, 'setInterval')
  const page = { provider: 'api', status: 'running', error: '', revision: 1, events,
    request_id: 'current', has_more: false, first_cursor: 'first', last_cursor: 'last',
    requirements: { status: 'review', document: '', revision: 1 } }
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === '/api/v1/projects/p/conversations/chat/stop' && options?.method === 'POST') {
      page.status = 'interrupted'; page.events = []; return {} as never
    }
    if (path.startsWith('/api/v1/projects/p/conversations/chat')) return { ...page } as never
    const task = tasks.find(item => path === `/api/v1/projects/p/tasks/${item.id}`)
    if (task) return task as never
    return [] as never
  })
  const onTask = vi.fn()
  render(<ProjectConversation id="p" conversationId="chat" canConfigureModel={false} items={[]}
    tasks={[]} onUpdated={vi.fn()} onSent={vi.fn()} onTask={onTask}/>)
  await screen.findAllByRole('article', { name: '关联业务结果' })
  await waitFor(() => expect(screen.getAllByText(/^输入资料：/)).toHaveLength(events.filter(event => event.result_kind === 'inspected').length))
  const poll = async () => { await act(async () => { (intervals.mock.calls.find(call => call[1] === 1500)![0] as () => void)() }) }
  return { page, onTask, poll }
}
const cards = () => screen.getAllByRole('article', { name: '关联业务结果' })

it('opens two inspected results before the explanation finishes and keeps one card per event after the final response', async () => {
  const events = [inspectedEvent('first'), inspectedEvent('second')]
  const { page, onTask, poll } = await setup(events, [savedTask('first', 'succeeded'), savedTask('second', 'failed')])
  expect(screen.getAllByRole('heading', { name: '已读取的运行结果' })).toHaveLength(2)
  expect(screen.getAllByText('解释尚未完成，可以先查看已有结果。')).toHaveLength(2)
  expect(within(cards()[0]).getByText('输入资料：first.csv')).toBeInTheDocument()
  expect(within(cards()[1]).getByText('输入资料：second.csv')).toBeInTheDocument()
  expect(cards()[0].querySelector('time')).toHaveAttribute('datetime', '2026-10-01T03:00:00Z')
  expect(cards()[1].querySelector('time')).toHaveAttribute('datetime', '2026-10-02T04:00:00Z')
  expect(within(cards()[0]).getByText('运行完成')).toBeInTheDocument()
  expect(within(cards()[1]).getByText('运行失败')).toBeInTheDocument()
  for (const card of cards()) fireEvent.click(within(card).getByRole('button', { name: '查看结果' }))
  expect(onTask.mock.calls).toEqual([['first'], ['second']])
  expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method)).toBe(false)

  page.status = 'idle'
  page.events = [...events, { id: 'answer', kind: 'assistant', request_id: 'current', text: '两次运行的解释已完成。', time: '' }]
  await poll()
  expect(await screen.findByText('两次运行的解释已完成。')).toBeInTheDocument()
  expect(cards()).toHaveLength(2)
  expect(screen.queryByText('解释尚未完成，可以先查看已有结果。')).not.toBeInTheDocument()
  fireEvent.click(within(cards()[1]).getByRole('button', { name: '查看结果' }))
  expect(onTask).toHaveBeenLastCalledWith('second')
  expect(vi.mocked(api).mock.calls.filter(([path]) => /\/tasks\/(first|second)$/.test(path))).toHaveLength(2)
})

it('keeps inspected results and their actual failed or interrupted statuses after stopping the explanation', async () => {
  const { onTask } = await setup([inspectedEvent('first'), inspectedEvent('second')], [savedTask('first', 'failed'), savedTask('second', 'interrupted')])
  fireEvent.click(screen.getByRole('button', { name: '停止' }))
  await screen.findByText(/已停止，进展和结果已保留/)
  expect(cards()).toHaveLength(2)
  expect(within(cards()[0]).getByText('运行失败')).toBeInTheDocument()
  expect(within(cards()[1]).getByText('已中断')).toBeInTheDocument()
  for (const card of cards()) {
    expect(card).not.toHaveTextContent(/运行完成|读取成功|解释尚未完成/)
    fireEvent.click(within(card).getByRole('button', { name: '查看结果' }))
  }
  expect(onTask.mock.calls).toEqual([['first'], ['second']])
  expect(vi.mocked(api).mock.calls.filter(([, options]) => options?.method).map(([path]) => path)).toEqual(['/api/v1/projects/p/conversations/chat/stop'])
})

it('shows the unfinished explanation notice only for inspected cards in the current request', async () => {
  const { page, poll } = await setup([inspectedEvent('first', 'previous'), inspectedEvent('second')], [savedTask('first', 'succeeded'), savedTask('second', 'succeeded')])
  expect(within(cards()[0]).queryByText('解释尚未完成，可以先查看已有结果。')).not.toBeInTheDocument()
  expect(within(cards()[1]).getByText('解释尚未完成，可以先查看已有结果。')).toBeInTheDocument()
  page.request_id = 'next-request'
  page.events = []
  await poll()
  expect(cards()).toHaveLength(2)
  expect(screen.queryByText('解释尚未完成，可以先查看已有结果。')).not.toBeInTheDocument()
})

it('keeps ordinary result-card wording and does not infer an active request when identifiers are absent', async () => {
  const first = { ...inspectedEvent('first'), request_id: undefined }
  const second = { ...inspectedEvent('second'), result_kind: undefined, text: '正常运行结果' }
  const { page, poll } = await setup([first, second], [savedTask('first', 'succeeded'), savedTask('second', 'failed')])
  expect(within(cards()[0]).getByRole('heading', { name: '已读取的运行结果' })).toBeInTheDocument()
  expect(within(cards()[1]).getByRole('heading', { name: '工作流结果' })).toBeInTheDocument()
  expect(cards()[1]).not.toHaveTextContent(/输入资料：|启动时间：|解释尚未完成/)
  page.request_id = ''
  page.events = []
  await poll()
  expect(screen.queryByText('解释尚未完成，可以先查看已有结果。')).not.toBeInTheDocument()
})
