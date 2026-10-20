import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import type { ProjectTask } from '@/lib/project-progress'
import { ProjectTaskOutput } from '@/app/components/ProjectRunPanel'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
beforeEach(() => { vi.mocked(api).mockReset() })
afterEach(cleanup)

const stoppedReason = '用户停止或服务中断'
const trialFailure = '单次训练超过时间预算，计算进程已终止'
const continuation = '继续原运行会保留现有参数、已完成及已失败的试验记录，只重新执行尚未保存结果的试验；已经失败的试验不会自动重试。'
const adjustment = '需要调整参数或重新尝试失败试验时，可选择“让智能体修改”，保留原记录后建立新方案。'
const candidatePath = (candidateId: string) => `/api/v1/projects/p/modeling/studies/study/candidates/${candidateId}`

function task(id = 'stopped-task', candidateId = 'saved-candidate'): ProjectTask {
  return { id, request_key: id, status: 'interrupted', mode: 'training', purpose: 'business',
    item_id: '', feedback_task_id: '', message: '', error: '', presentation: {},
    created_at: '2026-10-06T08:20:00Z', updated_at: '2026-10-06T08:51:00Z',
    inputs: { study_id: 'study', candidate_id: candidateId }, outputs: {}, runs: [] }
}

function candidate(id = 'saved-candidate', taskId = 'stopped-task', error = stoppedReason) {
  return { id, study_id: 'study', task_id: taskId, status: 'interrupted', error,
    hypothesis: '保存的训练方案', engine: 'sklearn', batch_size: 4, scheduled_slots: 4, current: null,
    trials: [
      { slot: 0, model: 'forest', status: 'failed', seconds: 1800.14, error: trialFailure },
      { slot: 1, model: 'linear', status: 'completed', seconds: 1.25,
        metrics: { mae: .25 }, baseline: { mae: 1.5 } },
    ] }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function expectOnlyReads() {
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true)
}

it('reads saved progress for an interrupted training task without turning a trial failure into the task cause', async () => {
  const savedTask = task()
  vi.mocked(api).mockResolvedValue(candidate() as never)
  await act(async () => { render(<ProjectTaskOutput projectId="p" task={savedTask} />) })
  const progress = screen.getByRole('region', { name: '已保存的训练进度' })
  expect(progress).toHaveTextContent('本批训练记录：' + stoppedReason)
  const comparison = within(progress).getByRole('region', { name: '训练比较' })
  expect(within(comparison).getByRole('row', { name: /第 1 次试验/ })).toHaveTextContent(trialFailure)
  expect(within(comparison).getByRole('row', { name: /第 2 次试验/ })).toHaveTextContent('0.25')
  expect(within(comparison).getByRole('row', { name: /第 2 次试验/ })).toHaveTextContent('已完成')
  expect(comparison).not.toHaveTextContent(stoppedReason)
  expect(screen.getByText(/继续原运行会保留现有参数/)).toHaveTextContent(continuation)
  expect(screen.getByText(/需要调整参数或重新尝试失败试验/)).toHaveTextContent(adjustment)
  expect(api).toHaveBeenCalledTimes(1)
  expect(api).toHaveBeenCalledWith(candidatePath('saved-candidate'))
  expect(savedTask.status).toBe('interrupted')
  expect(savedTask.outputs).toEqual({})
  expect(savedTask.presentation).toEqual({})
  expectOnlyReads()
})

it.each(['success', 'error'] as const)('ignores an older candidate %s after switching the selected task', async outcome => {
  const old = deferred<ReturnType<typeof candidate>>()
  const current = deferred<ReturnType<typeof candidate>>()
  vi.mocked(api).mockImplementation(async path => {
    if (path === candidatePath('old-candidate')) return old.promise as never
    if (path === candidatePath('current-candidate')) return current.promise as never
    throw new Error('Unexpected request: ' + path)
  })
  const view = await act(async () => render(<ProjectTaskOutput projectId="p" task={task('old-task', 'old-candidate')} />))
  await act(async () => { view.rerender(<ProjectTaskOutput projectId="p" task={task('current-task', 'current-candidate')} />) })
  expect(api).toHaveBeenCalledWith(candidatePath('current-candidate'))
  await act(async () => { current.resolve(candidate('current-candidate', 'current-task', '当前任务保存的中断说明')) })
  expect(screen.getByRole('region', { name: '已保存的训练进度' })).toHaveTextContent('当前任务保存的中断说明')
  await act(async () => {
    if (outcome === 'success') old.resolve(candidate('old-candidate', 'old-task', '旧任务记录不应覆盖'))
    else old.reject(new Error('旧任务读取失败不应覆盖'))
  })
  expect(screen.getByRole('region', { name: '已保存的训练进度' })).toHaveTextContent('当前任务保存的中断说明')
  expect(screen.queryByText(/旧任务记录不应覆盖|旧任务读取失败不应覆盖/)).not.toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(api).toHaveBeenCalledTimes(2)
  expectOnlyReads()
})

it('offers a read retry after failure and displays the saved results without starting training', async () => {
  vi.mocked(api).mockRejectedValueOnce(new Error('训练记录暂时不可读取')).mockResolvedValueOnce(candidate() as never)
  await act(async () => { render(<ProjectTaskOutput projectId="p" task={task()} />) })
  expect(screen.getByRole('alert')).toHaveTextContent('训练记录暂时不可读取')
  expect(screen.queryByText(trialFailure)).not.toBeInTheDocument()
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重试读取训练记录' })) })
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.getByRole('region', { name: '已保存的训练进度' })).toHaveTextContent(trialFailure)
  expect(api).toHaveBeenCalledTimes(2)
  expect(vi.mocked(api).mock.calls.map(([path]) => path)).toEqual([candidatePath('saved-candidate'), candidatePath('saved-candidate')])
  expectOnlyReads()
})

it.each(['other task', 'existing training output'] as const)('does not request another candidate for %s', async source => {
  const savedTask = source === 'other task'
    ? { ...task(), mode: 'workflow' }
    : { ...task(), outputs: candidate() }
  await act(async () => { render(<ProjectTaskOutput projectId="p" task={savedTask} />) })
  if (source === 'existing training output') {
    expect(screen.getByRole('region', { name: '训练比较' })).toHaveTextContent(trialFailure)
    expect(screen.getByText(/继续原运行会保留现有参数/)).toHaveTextContent(continuation)
  }
  expect(api).not.toHaveBeenCalled()
})
