import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import RequirementDiscussion from '@/app/components/RequirementDiscussion'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
beforeEach(() => { vi.mocked(api).mockReset() })

const initial = { enabled: true, status: 'not_started', revision: 0, turns: [], document: '' }
const understood = { ...initial, status: 'discussing', revision: 1, turns: [{ user: '请分析资料', analysis: {
  detected_goal: '按日处理库存', reasoning_summary: '需要确认统计周期。', questions: [{
    id: 'period', question: '统计周期是什么？', why: '', choice_type: 'single', options: [
      { id: 'day', label: '每日', description: '' }, { id: 'month', label: '每月', description: '' },
    ],
  }],
} }] }
const review = { ...understood, status: 'review', revision: 2, document: '# 需求文档\n按月处理库存。',
  turns: [...understood.turns, { user: '每月', analysis: { detected_goal: '按月处理库存', reasoning_summary: '已按你的回复调整。', questions: [] } }] }

it('analyzes, accepts corrections, then requires an explicit document confirmation without building', async () => {
  vi.mocked(api).mockResolvedValueOnce(initial).mockResolvedValueOnce(understood)
    .mockResolvedValueOnce(review).mockResolvedValueOnce({ ...review, status: 'confirmed', document_path: 'requirements/requirements.md' })
  const phase = vi.fn(), confirmed = vi.fn()
  render(<RequirementDiscussion applicationId="app" onPhaseChange={phase} onConfirmed={confirmed} />)
  fireEvent.click(await screen.findByRole('button', { name: '分析资料' }))
  expect(await screen.findByText('按日处理库存')).toBeInTheDocument()
  expect(confirmed).not.toHaveBeenCalled()
  fireEvent.click(screen.getByLabelText('每月'))
  fireEvent.change(screen.getByLabelText('补充或修正需求理解'), { target: { value: '由仓库主管使用。' } })
  fireEvent.click(screen.getByRole('button', { name: '发送并继续沟通' }))
  const confirmButton = await screen.findByRole('button', { name: '理解正确，确认需求文档' })
  expect(screen.getByText(/# 需求文档\s+按月处理库存。/)).toBeInTheDocument()
  expect(confirmed).not.toHaveBeenCalled()
  expect(phase).not.toHaveBeenCalledWith('confirmed')
  const request = vi.mocked(api).mock.calls[2][1]
  expect(JSON.parse(String(request?.body))).toEqual({ revision: 1, message: '统计周期是什么？：每月\n由仓库主管使用。' })
  fireEvent.click(confirmButton)
  await waitFor(() => expect(confirmed).toHaveBeenCalledTimes(1))
  expect(phase).toHaveBeenCalledWith('confirmed')
  expect(screen.getByText('下载需求文档')).toHaveAttribute('download', '需求文档.md')
  expect(vi.mocked(api).mock.calls.every(([path]) => !path.endsWith('/builds'))).toBe(true)
})

it('restores an unconfirmed discussion on reopening and preserves a reply after failure', async () => {
  vi.mocked(api).mockResolvedValueOnce(understood).mockRejectedValueOnce(new Error('模型暂时不可用'))
  const confirmed = vi.fn()
  render(<RequirementDiscussion applicationId="app" onPhaseChange={vi.fn()} onConfirmed={confirmed} />)
  expect(await screen.findByText('按日处理库存')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('补充或修正需求理解'), { target: { value: '每月整理' } })
  fireEvent.click(screen.getByRole('button', { name: '发送并继续沟通' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('模型暂时不可用')
  expect(screen.getByLabelText('补充或修正需求理解')).toHaveValue('每月整理')
  expect(confirmed).not.toHaveBeenCalled()
})
