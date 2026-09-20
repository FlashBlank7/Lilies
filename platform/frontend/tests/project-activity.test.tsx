import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import ProjectActivity from '@/app/components/ProjectActivity'
vi.mock('@/lib/platform', () => ({ api: vi.fn() }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })
const started = { id:'e1', operation_id:'op1', request_id:'q1', title:'运行资源分配', status:'running', started_at:'2026-09-12T00:00:00Z', time:'', tool_name:'project_action', summary:'', workflow_id:'w1', workflow_name:'资源分配', arguments:'{"quantity":2}' }

it('lazily loads scoped steps, replaces starts with ends and opens linked task/workflow', async () => {
  const onTask = vi.fn(), onWorkflow = vi.fn()
  let reads = 0
  vi.mocked(api).mockImplementation(async path => {
    if (path.endsWith('/tasks/t1')) return { id:'t1', status:'succeeded', runs:[{ id:'run1', application_id:'w1', draft_revision:4, status:'succeeded' }] } as never
    reads++
    return { events: reads === 1 ? [started] : [{ ...started, id:'e2', status:'completed', ended_at:'2026-09-12T00:00:02Z', duration_seconds:2, task_id:'t1', result:'{"quantity":2}' }], first_cursor:'e1', last_cursor:reads === 1 ? 'e1' : 'e2', has_more:false } as never
  })
  render(<ProjectActivity projectId="p" requestId="q1" current={started} onTask={onTask} onWorkflow={onWorkflow} />)
  expect(api).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('运行资源分配 · 资源分配'))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/projects/p/conversation?kind=activity&request_id=q1'))
  await waitFor(() => expect(screen.getByText('操作完成 · 2.0 秒')).toBeInTheDocument(), { timeout:2500 })
  expect(screen.getAllByText('运行资源分配 · 资源分配')).toHaveLength(2) // summary plus a single deduplicated step
  fireEvent.click(screen.getByText('操作完成 · 2.0 秒'))
  fireEvent.click(screen.getByRole('button', { name:'打开工作流画布' }))
  expect(onWorkflow).toHaveBeenCalledWith('w1')
  fireEvent.click(await screen.findByRole('button', { name:/查看关联任务/ }))
  expect(onTask).toHaveBeenCalledWith('t1')
  fireEvent.click(screen.getByText('参数与原始返回'))
  expect(screen.getAllByText('{"quantity":2}')).toHaveLength(2)
})

it('restores interrupted activity from saved summary without claiming success', () => {
  render(<ProjectActivity projectId="p" requestId="q1" current={{ ...started, status:'interrupted', summary:'服务已重启，等待继续' }} />)
  expect(screen.getByText('已中断')).toBeInTheDocument()
  expect(screen.queryByText('操作完成')).not.toBeInTheDocument()
})
