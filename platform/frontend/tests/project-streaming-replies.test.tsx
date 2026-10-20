import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import ProjectConversation from '@/app/components/ProjectConversation'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
vi.mock('@/app/components/AuthBoundary', () => ({ useAccount: () => ({ id: 'user' }) }))
vi.mock('@/app/components/Onboarding', () => ({ useOnboarding: () => ({ mark: vi.fn() }) }))
vi.mock('@/app/components/ModelingPanel', () => ({ default: () => null }))
vi.mock('@/app/components/ProjectActivity', () => ({ default: () => null }))
vi.mock('@/app/components/OfficialConnectionStatus', () => ({ default: () => null }))
vi.mock('@/app/components/UserFeedback', () => ({ FeedbackButton: () => null }))
vi.mock('@/app/components/SaveMethod', () => ({ default: () => <button>保存为方法</button> }))
vi.mock('@/app/components/ResultFeedback', () => ({ default: () => <button>评价回答</button> }))

type Message = { id: string; kind: string; text: string; time: string; request_id: string; incomplete?: boolean }
const partial: Message = { id: 'answer', kind: 'assistant', text: '## 初步结果\n\n已读取第一份。', time: '', request_id: 'request' }
const question: Message = { id: 'question', kind: 'user', text: '请解释结果', time: '', request_id: 'request' }
const props = { id: 'p', conversationId: 'chat', canConfigureModel: false, items: [], onUpdated: vi.fn(), onSent: vi.fn() }
const draftKey = 'lilies:user:user:project:p:conversation:chat:draft'
function session() {
  return { provider: 'api', status: 'running', error: '', revision: 1, events: [question],
    request_id: 'request', has_more: false, first_cursor: 'question', last_cursor: 'question',
    streaming_message: { ...partial } as Message | null,
    requirements: { status: 'review', document: '', revision: 1 } }
}
let intervals: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  vi.mocked(api).mockReset(); sessionStorage.clear()
  intervals = vi.spyOn(window, 'setInterval')
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })
async function poll() {
  const callback = intervals.mock.calls.filter((call: unknown[]) => call[1] === 1500).at(-1)![0] as () => void
  await act(async () => { callback() })
}
function respond(page: ReturnType<typeof session>) {
  vi.mocked(api).mockImplementation(async () => ({ ...page, events: [...page.events] }) as never)
}

it('shows full Markdown snapshots after the cursor, preserves the draft, and replaces the partial with one final answer', async () => {
  const page = session(); respond(page)
  render(<ProjectConversation {...props} />)
  await screen.findByRole('heading', { name: '初步结果' })
  expect(screen.getByText('正在回复')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '保存为方法' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '评价回答' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '我还要补充条件' } })

  page.events = []
  page.streaming_message = { ...partial, text: '## 初步结果\n\n已读取两份，**可以比较**。' }
  await poll()
  expect(screen.getByText('可以比较').tagName).toBe('STRONG')
  expect(screen.queryByText('已读取第一份。')).not.toBeInTheDocument()
  expect(vi.mocked(api).mock.calls.at(-1)![0]).toBe('/api/v1/projects/p/conversations/chat?after=question')
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('我还要补充条件')

  page.status = 'idle'; page.streaming_message = null
  page.events = [{ ...partial, text: '解释已经完成。' }]; page.last_cursor = 'answer'
  await poll(); await poll()
  expect(screen.getAllByText('解释已经完成。')).toHaveLength(1)
  expect(screen.queryByText('正在回复')).not.toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: '初步结果' })).not.toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: '保存为方法' })).toHaveLength(1)
  expect(screen.getAllByRole('button', { name: '评价回答' })).toHaveLength(1)
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('我还要补充条件')
})

it.each(['interrupted', 'error'])('keeps generated text without result actions after %s', async status => {
  const page = session(); respond(page)
  const original = vi.mocked(api).getMockImplementation()!
  const finish = () => {
    page.status = status; page.error = status === 'error' ? '连接已断开' : ''
    page.events = [{ ...partial, incomplete: true }]; page.streaming_message = null; page.last_cursor = 'answer'
  }
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/stop') && options?.method === 'POST') { finish(); return {} as never }
    return original(path, options)
  })
  render(<ProjectConversation {...props} />)
  await screen.findByText('正在回复')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '未发送的补充' } })
  if (status === 'interrupted') fireEvent.click(screen.getByRole('button', { name: '停止' }))
  else { finish(); await poll() }
  await screen.findByText('回复未完成')
  expect(screen.getAllByText('已读取第一份。')).toHaveLength(1)
  expect(screen.queryByText('正在回复')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '保存为方法' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '评价回答' })).not.toBeInTheDocument()
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('未发送的补充')
})

it('restores the current partial and unsent draft after refreshing the page', async () => {
  const page = session(); respond(page)
  const first = render(<ProjectConversation {...props} />)
  await screen.findByText('正在回复')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '刷新后也保留' } })
  first.unmount()
  render(<ProjectConversation {...props} />)
  await screen.findByText('正在回复')
  expect(screen.getAllByText('已读取第一份。')).toHaveLength(1)
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('刷新后也保留')
  expect(sessionStorage.getItem(draftKey)).toBe('刷新后也保留')
})

it.each([{ id: 'p', conversationId: 'other' }, { id: 'other-project', conversationId: 'chat' }])(
  'ignores an old poll after switching to $id/$conversationId and keeps drafts separate', async destination => {
    const page = session(); respond(page)
    const view = render(<ProjectConversation {...props} />)
    await screen.findByText('正在回复')
    fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '原会话草稿' } })
    let resolveOld!: (value: unknown) => void
    const delayed = new Promise(resolve => { resolveOld = resolve })
    const newBase = `/api/v1/projects/${destination.id}/conversations/${destination.conversationId}`
    vi.mocked(api).mockImplementation(async path => path.startsWith(newBase)
      ? { ...session(), events: [], last_cursor: '', streaming_message: { ...partial, id: 'other-answer', text: '新会话正文' } } as never
      : delayed as never)
    await poll()
    view.rerender(<ProjectConversation {...props} {...destination} />)
    await screen.findByText('新会话正文')
    expect(screen.queryByText('已读取第一份。')).not.toBeInTheDocument()
    expect(screen.queryByText('请解释结果')).not.toBeInTheDocument()
    expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '新会话草稿' } })
    await act(async () => { resolveOld({ ...page, streaming_message: { ...partial, text: '迟到的原会话正文' } }) })
    expect(screen.queryByText('迟到的原会话正文')).not.toBeInTheDocument()
    expect(screen.getByText('新会话正文')).toBeInTheDocument()
    expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('新会话草稿')
    expect(sessionStorage.getItem(draftKey)).toBe('原会话草稿')
  })

it('does not regress to an older streaming snapshot when polls finish out of order', async () => {
  const page = session(); respond(page)
  render(<ProjectConversation {...props} />)
  await screen.findByText('正在回复')
  let resolveOld!: (value: unknown) => void
  vi.mocked(api).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }) as never)
  await poll()
  page.status = 'idle'; page.streaming_message = null; page.events = [{ ...partial, text: '最后的完整答复' }]
  await poll()
  expect(screen.getByText('最后的完整答复')).toBeInTheDocument()
  await act(async () => { resolveOld(session()) })
  expect(screen.queryByText('正在回复')).not.toBeInTheDocument()
  expect(screen.getAllByText('最后的完整答复')).toHaveLength(1)
})

it('follows incremental text only while the reader stays at the bottom', async () => {
  const page = session(); respond(page)
  render(<ProjectConversation {...props} />)
  await screen.findByText('正在回复')
  const history = screen.getByText('请解释结果').closest('article')!.parentElement!.parentElement!
  Object.defineProperty(history, 'clientHeight', { configurable: true, value: 200 })
  Object.defineProperty(history, 'scrollHeight', { configurable: true, value: 1000 })
  history.scrollTop = 800; fireEvent.scroll(history)
  Object.defineProperty(history, 'scrollHeight', { configurable: true, value: 1200 })
  page.streaming_message = { ...partial, text: '继续生成正文' }; await poll()
  expect(history.scrollTop).toBe(1200)
  history.scrollTop = 100; fireEvent.scroll(history)
  Object.defineProperty(history, 'scrollHeight', { configurable: true, value: 1400 })
  page.streaming_message = { ...partial, text: '又生成了一段正文' }; await poll()
  expect(history.scrollTop).toBe(100)
})

it('never renders a reasoning payload as a streaming assistant answer', async () => {
  const page = session()
  page.streaming_message = { ...partial, kind: 'reasoning', text: 'PRIVATE_REASONING_SENTINEL' }
  respond(page); render(<ProjectConversation {...props} />)
  await screen.findByText('请解释结果')
  expect(screen.queryByText('PRIVATE_REASONING_SENTINEL')).not.toBeInTheDocument()
  expect(screen.queryByText('正在回复')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '保存为方法' })).not.toBeInTheDocument()
})
