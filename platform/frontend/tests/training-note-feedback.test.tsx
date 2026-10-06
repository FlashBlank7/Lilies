import { useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import type { ConversationFocus } from '@/lib/project-progress'
import ModelingPanel from '@/app/components/ModelingPanel'
import ProjectConversation from '@/app/components/ProjectConversation'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
vi.mock('recharts', () => ({ ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>, BarChart: () => null, Bar: () => null, CartesianGrid: () => null, LineChart: () => null, Line: () => null, Tooltip: () => null, XAxis: () => null, YAxis: () => null }))

const failure = 'ValueError: min_samples_split 必须为至少 2 的整数'
const missingReason = '本次训练未保存具体失败原因，可查看关联运行或请智能体帮助排查。'
const study = { id: 'study', name: '设备质量研究', dataset_id: 'dataset', item_id: 'item', status: 'running', next_action: '正在训练模型', trials_used: 4, budget: { seconds: 1800, trials: 10 }, best: { candidate_id: 'best', slot: 0, score: .25, model: 'linear' }, evaluation: { metric: 'mae', split: 'group' } }
const dataset = { id: 'dataset', name: '设备.csv', status: 'profiled', mapping: { kind: 'tabular', target: 'quality' } }
const candidates = [
  { id: 'running', task_id: 'running-task', run_id: 'running-run', status: 'running', hypothesis: '当前训练方案', current: { model: 'forest', slot: 0 }, trials: [] },
  { id: 'historical', task_id: 'historical-task', run_id: 'historical-run', status: 'completed', hypothesis: '历史失败方案', trials: [{ slot: 2, model: 'forest', status: 'failed', seconds: 1.25, error: failure }] },
  { id: 'best', task_id: 'best-task', run_id: 'best-run', status: 'completed', hypothesis: '当前最佳方案', trials: [{ slot: 0, model: 'linear', status: 'completed', metrics: { mae: .25 }, seconds: 2 }] },
]
const notePath = '/api/v1/projects/p/modeling/studies/study/candidates/historical/trials/2/note'
const savedNote = {
  markdown: '# 随机森林 · 试验 3 · 训练笔记\n\n## 固定划分\n\n保留原评价方式。',
  note: { title: '随机森林 · 试验 3 · 训练笔记', trial: { status: 'failed', seconds: 1.25, error: failure }, gaps: [], evaluation: { metric: 'mae' }, comparison: [] },
}

beforeEach(() => {
  vi.mocked(api).mockReset(); sessionStorage.clear()
  vi.mocked(api).mockImplementation(async path => {
    if (path === notePath) return savedNote as never
    if (path.includes('/candidates?')) return candidates as never
    if (path.includes('/modeling/studies?')) return [study] as never
    if (path.includes('/datasets?')) return [dataset] as never
    if (path.endsWith('/datasets/dataset')) return dataset as never
    throw new Error('Unexpected request: ' + path)
  })
})
afterEach(cleanup)

async function openHistoricalNote(onContext = vi.fn()) {
  render(<ModelingPanel projectId="p" onContext={onContext} />)
  const open = await screen.findByRole('button', { name: '查看结果' })
  await act(async () => { fireEvent.click(open) })
  await act(async () => { fireEvent.click(screen.getByRole('tab', { name: '实验' })) })
  expect(within(screen.getByRole('row', { name: /线性模型/ })).getByText('训练完成')).toBeVisible()
  const historical = await screen.findByRole('button', { name: '查看随机森林第 3 次训练笔记' })
  await act(async () => { fireEvent.click(historical) })
  return screen.getByRole('region', { name: '逐模型训练笔记' })
}

function expectOnlyReads() {
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true)
}

it('shows the saved failure before the chart and prepares the historical candidate context', async () => {
  const onContext = vi.fn()
  const note = await openHistoricalNote(onContext)
  const reason = await within(note).findByRole('region', { name: '失败原因' })
  expect(reason).toBeVisible()
  expect(reason).toHaveTextContent(failure)
  const axis = within(note).getByRole('combobox', { name: '训练笔记曲线横轴' })
  expect(reason.compareDocumentPosition(axis) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(within(note).queryByRole('link', { name: '下载本次模型' })).not.toBeInTheDocument()
  expect(onContext).not.toHaveBeenCalled()
  await act(async () => { fireEvent.click(within(note).getByRole('button', { name: '让智能体改进这次实验' })) })
  expect(onContext).toHaveBeenCalledTimes(1)
  expect(onContext).toHaveBeenCalledWith(expect.objectContaining({
    label: '设备质量研究', study_id: 'study', dataset_id: 'dataset', item_id: 'item',
    candidate_id: 'historical', task_id: 'historical-task',
  }), expect.any(String), 'task')
  const message = onContext.mock.calls[0][1] as string
  expect(message).toContain(savedNote.note.title)
  expect(message).toContain(failure)
  expectOnlyReads()
})

it('reports a missing saved cause without inventing one in the prepared message', async () => {
  const original = vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async (path, options) => path === notePath
    ? { ...savedNote, note: { ...savedNote.note, trial: { status: 'failed', seconds: 1.25 } } } as never
    : original(path, options))
  const onContext = vi.fn()
  const note = await openHistoricalNote(onContext)
  const reason = await within(note).findByRole('region', { name: '失败原因' })
  expect(reason).toHaveTextContent(missingReason)
  expect(reason).not.toHaveTextContent(failure)
  await act(async () => { fireEvent.click(within(note).getByRole('button', { name: '让智能体改进这次实验' })) })
  expect(onContext).toHaveBeenCalledTimes(1)
  expect(onContext.mock.calls[0][0]).toEqual(expect.objectContaining({ candidate_id: 'historical', task_id: 'historical-task' }))
  expect(onContext.mock.calls[0][1]).toContain(savedNote.note.title)
  expect(onContext.mock.calls[0][1]).not.toContain(failure)
  expectOnlyReads()
})

it('offers retry after a failed read and waits for the saved note before offering improvement', async () => {
  const original = vi.mocked(api).getMockImplementation()!
  let fail!: (reason: Error) => void
  let finish!: (value: typeof savedNote) => void
  const first = new Promise((_, reject) => { fail = reject })
  const retry = new Promise<typeof savedNote>(resolve => { finish = resolve })
  let reads = 0
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === notePath) return (++reads === 1 ? first : retry) as never
    return original(path, options)
  })
  const onContext = vi.fn()
  const note = await openHistoricalNote(onContext)
  expect(within(note).getByRole('status')).toHaveTextContent('正在读取')
  expect(within(note).queryByRole('button', { name: '让智能体改进这次实验' })).not.toBeInTheDocument()
  await act(async () => { fail(new Error('离线笔记暂时不可读取')) })
  expect(within(note).getByRole('alert')).toHaveTextContent('离线笔记暂时不可读取')
  expect(within(note).queryByRole('region', { name: '失败原因' })).not.toBeInTheDocument()
  expect(within(note).queryByRole('button', { name: '让智能体改进这次实验' })).not.toBeInTheDocument()
  await act(async () => { fireEvent.click(within(note).getByRole('button', { name: '重试加载笔记' })) })
  expect(within(note).getByRole('status')).toHaveTextContent('正在读取')
  expect(within(note).queryByRole('button', { name: '让智能体改进这次实验' })).not.toBeInTheDocument()
  await act(async () => { finish(savedNote) })
  await waitFor(() => expect(within(note).getByRole('button', { name: '让智能体改进这次实验' })).toBeEnabled())
  expect(within(note).getByRole('region', { name: '失败原因' })).toHaveTextContent(failure)
  expect(reads).toBe(2)
  expect(onContext).not.toHaveBeenCalled()
  expectOnlyReads()
})

const updated = () => {}
function ConversationWithFocus({ initialFocus }: { initialFocus?: ConversationFocus }) {
  const [focus, setFocus] = useState(initialFocus)
  return <ProjectConversation id="p" conversationId="chat" items={[]} canConfigureModel={false}
    focus={focus} onUpdated={updated} onSent={() => setFocus(undefined)} />
}

async function openConversationNote(initialFocus?: ConversationFocus, draft?: string) {
  const original = vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === '/api/v1/projects/p/space') return { files: [] } as never
    if (path === '/api/v1/projects/p/conversations/chat' || path === '/api/v1/projects/p/conversations/chat/messages') return {
      provider: 'api', status: 'idle', error: '', revision: 1, events: [], has_more: false,
      first_cursor: '', last_cursor: '', requirements: { status: 'confirmed', document: '', revision: 1 },
    } as never
    return original(path, options)
  })
  await act(async () => { render(<ConversationWithFocus initialFocus={initialFocus} />) })
  if (draft !== undefined) {
    fireEvent.change(screen.getByRole('textbox', { name: '给项目统筹的消息' }), { target: { value: draft } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '创建工作流' })) })
  }
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '查看建模结果' })) })
  await act(async () => { fireEvent.click(screen.getByRole('tab', { name: '实验' })) })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '查看随机森林第 3 次训练笔记' })) })
  const note = screen.getByRole('region', { name: '逐模型训练笔记' })
  expect(within(note).getByRole('region', { name: '失败原因' })).toHaveTextContent(failure)
  await act(async () => { fireEvent.click(within(note).getByRole('button', { name: '让智能体改进这次实验' })) })
}

it('appends the historical failure to a compact conversation draft and prepares task mode without sending', async () => {
  const draft = '保留我尚未发送的业务要求：不要变更评价划分。'
  await openConversationNote(undefined, draft)
  const prepared = (screen.getByRole('textbox', { name: '给项目统筹的消息' }) as HTMLTextAreaElement).value
  expect.soft(prepared).toContain(draft + '\n\n')
  expect.soft(prepared).toContain(savedNote.note.title)
  expect.soft(prepared).toContain(failure)
  expect.soft(screen.getByRole('button', { name: '完成任务' })).toHaveAttribute('aria-pressed', 'true')
  expect.soft(screen.getByRole('button', { name: '创建工作流' })).toHaveAttribute('aria-pressed', 'false')
  expect(sessionStorage.getItem('lilies:project:p:conversation:chat:draft')).toBe(prepared)
  expectOnlyReads()
})

it('sends the selected historical candidate with its own task instead of an earlier focused result', async () => {
  await openConversationNote({ nonce: 1, label: '之前关联的业务结果',
    task_id: 'old-task', item_id: 'old-item', question_id: 'old-question' })
  expectOnlyReads()
  expect.soft(screen.queryByText('关于：之前关联的业务结果')).not.toBeInTheDocument()
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })
  const writes = vi.mocked(api).mock.calls.filter(([, options]) => options?.method === 'POST')
  expect(writes).toHaveLength(1)
  expect(writes[0][0]).toBe('/api/v1/projects/p/conversations/chat/messages')
  const sent = JSON.parse(writes[0][1]!.body as string)
  expect(sent).toEqual({ message: expect.stringContaining(savedNote.note.title),
    dataset_id: 'dataset', study_id: 'study', candidate_id: 'historical',
    task_id: 'historical-task', item_id: 'item', question_id: '' })
  expect(sent.message).toContain(failure)
})
