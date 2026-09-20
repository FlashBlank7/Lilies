import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectWorkflowOverview, { WorkflowRoute } from '@/app/components/ProjectWorkflowOverview'
import type { ProjectProgress, ProjectTopology, WorkflowOutline } from '@/lib/project-progress'
afterEach(cleanup)
const progress: ProjectProgress = { revision: 2, updated_at: '', value: { goal: '处理申请', summary: '', workflows: [{ workflow_id: 'm', purpose: '校验有效数量', inputs: '客户数量', outputs: '有效数量与错误原因' }], items: [{
  id: 'allocation', title: '申请分配', goal: '申请得到资源', availability: 'trial', status: 'working', summary: '两条流程共同完成一次离线试用', next_action: '验证释放后继续',
  workflow_ids: ['p', 'm'], task_ids: ['t'], questions: [], results: [], blocker: null,
  requirements: [{ requirement: '资源不足时等待并可恢复', source: '需求第2节', current: '已返回等待，尚未验证恢复', status: 'partial', gap: '需实际释放后验证继续', results: [{ label: '等待结果', task_id: 't', file_path: 'results/report.md' }] }],
}] } }
const topology: ProjectTopology = { members: ['p', 'm'].map(id => ({ id, name: id === 'p' ? '主流程' : '数量校验', description: '旧占位说明', revision: 3, purpose: 'business' })), calls: [{ source: 'p', target: 'm', node_id: 'call', label: '校验' }] }
function setup(selectedId = 'm', value = progress) {
  const callbacks = { onWorkflow: vi.fn(), onTalk: vi.fn(), onTask: vi.fn(), onFile: vi.fn(), onRequirements: vi.fn(), onEdit: vi.fn() }
  render(<ProjectWorkflowOverview projectId="p" selectedId={selectedId} progress={value} topology={topology} {...callbacks} />)
  return callbacks
}
it('explains member responsibility, actual scope and requirement gap without upgrading trial to delivery', () => {
  const cb = setup()
  expect(screen.getByText('校验有效数量')).toBeInTheDocument()
  expect(screen.getByText('客户数量')).toBeInTheDocument()
  expect(screen.getByText('有效数量与错误原因')).toBeInTheDocument()
  const table = screen.getByRole('table')
  expect(table).toHaveTextContent('资源不足时等待并可恢复')
  expect(table).toHaveTextContent('部分满足')
  expect(table).toHaveTextContent('需实际释放后验证继续')
  expect(screen.getByText(/一项能力可能由多条流程共同完成/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '等待结果 · 运行' }))
  expect(cb.onTask).toHaveBeenCalledWith('t')
  fireEvent.click(screen.getByRole('button', { name: '等待结果 · 文件' }))
  expect(cb.onFile).toHaveBeenCalledWith('results/report.md')
  fireEvent.click(screen.getByRole('button', { name: '反馈或继续完善' }))
  expect(cb.onTalk).toHaveBeenCalledWith(progress.value.items[0], '')
})
it('keeps missing comparisons explicit and sends a scoped explanation request, without executing', () => {
  const value = { ...progress, value: { ...progress.value, workflows: [], items: [{ ...progress.value.items[0], requirements: [] }] } }
  const cb = setup('m', value)
  expect(screen.getByText(/尚未逐项对照原需求/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '让统筹补充对照' }))
  expect(cb.onTalk).toHaveBeenCalledWith(value.value.items[0], expect.stringContaining('不运行工作流'))
  expect(cb.onTask).not.toHaveBeenCalled()
})
it('renders actual sequential edges and conditional outcomes, keeping independent calls accessible', () => {
  const make = (id: string, type = 'tool', workflow_id = '') => ({ id, title: id, type, workflow_id, branches: [], default_branch: '' })
  const flow: WorkflowOutline = { revision: 4, nodes: [make('接收', 'start'), make('校验', 'tool', 'm'), { ...make('有资源', 'if_else'), default_branch: 'no' }, make('分配', 'tool', 'allocate'), make('等待', 'end')],
    edges: [{ source: '接收', target: '校验', branch: null }, { source: '校验', target: '有资源', branch: null }, { source: '有资源', target: '分配', branch: 'yes' }, { source: '有资源', target: '等待', branch: 'no' }] }
  const onWorkflow = vi.fn()
  render(<WorkflowRoute flow={flow} onWorkflow={onWorkflow} />)
  const root = screen.getAllByRole('list')[0]
  const steps = within(root).getAllByRole('listitem')
  expect(steps[0]).toHaveTextContent('接收')
  expect(steps[1]).toHaveTextContent('校验')
  expect(screen.getByText('其他情况 → 等待')).toBeInTheDocument()
  expect(screen.getByText('按条件选择路径')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '校验' }))
  expect(onWorkflow).toHaveBeenCalledWith('m')
})

it('lets customers select a capability and retains its context when entering feedback', () => {
  const second = { ...progress.value.items[0], id: 'quality', title: '质量检测', availability: 'not_ready' as const,
    goal: '判定质量', summary: '尚缺质量实测', requirements: [] }
  const cb = setup('p', { ...progress, value: { ...progress.value, items: [...progress.value.items, second] } })
  const overview = screen.getByRole('table', { name: '业务能力概览' })
  expect(overview).toHaveTextContent('尚缺质量实测')
  fireEvent.click(within(overview).getByRole('button', { name: '质量检测' }))
  expect(screen.queryByRole('article', { name: '申请分配需求对照' })).not.toBeInTheDocument()
  expect(screen.getByRole('article', { name: '质量检测需求对照' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '试用这项能力' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '反馈或继续完善' }))
  expect(cb.onTalk).toHaveBeenCalledWith(second, '')
})
