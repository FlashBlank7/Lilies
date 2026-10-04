import { Suspense } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import ProjectPage from '@/app/projects/[id]/page'
import ProjectSpace from '@/app/components/ProjectSpace'
import Onboarding from '@/app/components/Onboarding'

const { account, router } = vi.hoisted(() => ({ account: { id: 'alice' }, router: { push: vi.fn() } }))
vi.mock('next/navigation', () => ({ useRouter: () => router, usePathname: () => '/projects/p', useSearchParams: () => new URLSearchParams() }))
vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
vi.mock('@/app/components/AuthBoundary', () => ({ useAccount: () => account }))
vi.mock('@/app/components/ModelConnectionPanel', () => ({ default: () => null }))
vi.mock('@/app/components/AssistantSourcePanel', () => ({ default: () => null }))

const paths = ['requirement-package/input.csv', 'requirement-package/reference.csv']
const member = { id: 'workflow', name: '质量分析', description: '', purpose: 'business', revision: 1 }
let files = paths.map(path => ({ path, size: 20 }))
beforeEach(() => {
  account.id = 'alice'
  sessionStorage.clear()
  files = paths.map(path => ({ path, size: 20 }))
  vi.mocked(api).mockReset()
  vi.mocked(api).mockImplementation(async path => {
    if (path.endsWith('/space')) return { workflows: [{ ...member, node_count: 2, allowed: true, inputs: [] }], files, files_truncated: false } as never
    if (path.endsWith('/progress')) return { revision: 1, value: { goal: '', summary: '', items: [] } } as never
    if (path.endsWith('/topology')) return { members: [member], calls: [] } as never
    if (path.endsWith('/conversations')) return [{ id: 'chat', title: '我的会话', status: 'idle' }] as never
    if (path.includes('/conversations/chat')) return { provider: 'api', status: 'idle', error: '', revision: 1, events: [], has_more: false, first_cursor: '', last_cursor: '' } as never
    if (path === '/api/v1/projects/p') return { id: 'p', name: '资料分析', access_role: 'collaborator', members: [member] } as never
    if (path.endsWith('/example')) return null as never
    return [] as never
  })
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('keeps the canvas open when the tutorial records the first workflow view and navigates only on next', async () => {
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(() => [{ top: 30, left: 30, width: 120, height: 50 }] as unknown as DOMRectList)
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ top: 30, left: 30, right: 150, bottom: 80, width: 120, height: 50, x: 30, y: 30, toJSON: () => ({}) })
  HTMLElement.prototype.scrollIntoView = vi.fn()
  const normal = vi.mocked(api).getMockImplementation()!
  let finishSave!: () => void
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path !== '/api/v1/me/onboarding') return normal(path, options)
    if (!options) return { status: 'active', step: 'workflow', completed_steps: ['project', 'materials'], project_id: 'p' } as never
    const progress = JSON.parse(options.body as string)
    if (progress.step === 'workflow') return new Promise(resolve => { finishSave = () => resolve(progress) })
    return progress
  })
  const events = vi.spyOn(window, 'dispatchEvent')
  const navigation = () => events.mock.calls.map(([event]) => event).filter(event => event.type === 'lilies:guide-navigate')
  await act(async () => { render(<Suspense><Onboarding><ProjectPage params={Promise.resolve({ id: 'p' })} /></Onboarding></Suspense>) })
  await screen.findByText('第 3 / 6 步')
  fireEvent.click(await screen.findByRole('button', { name: '查看与编辑' }))
  expect(await screen.findByTitle('业务工作流画布')).toBeVisible()
  await waitFor(() => expect(finishSave).toBeDefined())
  expect(navigation().map(event => (event as CustomEvent).detail)).toEqual(['workflow'])
  await act(async () => { finishSave() })
  expect(screen.getByTitle('业务工作流画布')).toBeVisible()
  expect(screen.getByText('已完成操作 · 3 / 6')).toBeInTheDocument()
  expect(navigation().map(event => (event as CustomEvent).detail)).toEqual(['workflow'])
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  await screen.findByText('第 4 / 6 步')
  expect(await screen.findByRole('button', { name: '让智能体调用' })).toBeVisible()
  expect(navigation().map(event => (event as CustomEvent).detail)).toEqual(['workflow', 'conversation'])
  expect(screen.queryByTitle('业务工作流画布')).not.toBeInTheDocument()
})

it('keeps materials through viewing a workflow and appends them to the existing conversation draft', async () => {
  await act(async () => { render(<Suspense><ProjectPage params={Promise.resolve({ id: 'p' })} /></Suspense>) })
  fireEvent.change(await screen.findByLabelText('给项目统筹的消息'), { target: { value: '保留我的分析要求' } })
  fireEvent.click(screen.getByRole('tab', { name: '项目空间' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: 'input.csv' }))
  fireEvent.click(screen.getByRole('button', { name: '查看与编辑' }))
  await screen.findByTitle('业务工作流画布')
  expect(screen.queryByRole('checkbox', { name: 'input.csv' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('tab', { name: '项目空间' }))
  expect(await screen.findByRole('checkbox', { name: 'input.csv' })).toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: '带着所选资料开始对话' }))
  const expected = `保留我的分析要求\n\n请帮我分析和处理以下资料；项目中有适合的工作流时可以直接复用。\n本次资料：\n- ${paths[0]}`
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue(expected)
  fireEvent.click(screen.getByRole('tab', { name: '项目空间' }))
  await screen.findByRole('checkbox', { name: 'input.csv' })
  fireEvent.click(screen.getByRole('button', { name: '带着所选资料开始对话' }))
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue(expected)
  expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
})

const props = { projectId: 'p', onWorkflow: vi.fn(), onFile: vi.fn(), onTalk: vi.fn(), onChanged: vi.fn() }
it.each(['project', 'account'])('keeps each %s selection separate and restores it when returning', async scope => {
  const view = render(<ProjectSpace {...props} />)
  fireEvent.click(await screen.findByRole('checkbox', { name: 'input.csv' }))
  if (scope === 'account') account.id = 'bob'
  view.rerender(<ProjectSpace {...props} projectId={scope === 'project' ? 'other' : 'p'} />)
  expect(await screen.findByRole('checkbox', { name: 'input.csv' })).not.toBeChecked()
  expect(screen.getByRole('button', { name: '带着所选资料开始对话' })).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox', { name: 'reference.csv' }))
  account.id = 'alice'
  view.rerender(<ProjectSpace {...props} />)
  expect(await screen.findByRole('checkbox', { name: 'input.csv' })).toBeChecked()
  expect(screen.getByRole('checkbox', { name: 'reference.csv' })).not.toBeChecked()
  if (scope === 'account') account.id = 'bob'
  view.rerender(<ProjectSpace {...props} projectId={scope === 'project' ? 'other' : 'p'} />)
  expect(await screen.findByRole('checkbox', { name: 'input.csv' })).not.toBeChecked()
  expect(screen.getByRole('checkbox', { name: 'reference.csv' })).toBeChecked()
})

it.each(['project', 'account'])('ignores a file refresh that finishes after changing %s', async scope => {
  const view = render(<ProjectSpace {...props} />)
  fireEvent.click(await screen.findByRole('checkbox', { name: 'input.csv' }))
  let finish!: (value: never) => void
  vi.mocked(api).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  fireEvent.click(screen.getByRole('button', { name: '刷新空间' }))
  if (scope === 'account') account.id = 'bob'
  const nextProject = scope === 'project' ? 'other' : 'p'
  view.rerender(<ProjectSpace {...props} projectId={nextProject} />)
  fireEvent.click(await screen.findByRole('checkbox', { name: 'reference.csv' }))
  await act(async () => { finish({ workflows: [], files: [{ path: 'requirement-package/old-only.csv', size: 10 }], files_truncated: false } as never) })
  expect(screen.queryByRole('checkbox', { name: 'old-only.csv' })).not.toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: 'reference.csv' })).toBeChecked()
  expect(screen.getByRole('checkbox', { name: 'input.csv' })).not.toBeChecked()
  view.unmount()
  render(<ProjectSpace {...props} projectId={nextProject} />)
  expect(await screen.findByRole('checkbox', { name: 'reference.csv' })).toBeChecked()
  expect(screen.getByRole('checkbox', { name: 'input.csv' })).not.toBeChecked()
})

it('preserves a cleared selection after leaving the project space', async () => {
  const view = render(<ProjectSpace {...props} />)
  fireEvent.click(await screen.findByRole('checkbox', { name: 'input.csv' }))
  view.unmount()
  const reopened = render(<ProjectSpace {...props} />)
  expect(await screen.findByRole('checkbox', { name: 'input.csv' })).toBeChecked()
  fireEvent.click(screen.getByRole('checkbox', { name: 'input.csv' }))
  reopened.unmount()
  render(<ProjectSpace {...props} />)
  expect(await screen.findByRole('checkbox', { name: 'input.csv' })).not.toBeChecked()
  expect(screen.getByRole('button', { name: '带着所选资料开始对话' })).toBeDisabled()
})

it('stops carrying removed files into conversation after refreshing the space', async () => {
  const talk = vi.fn()
  render(<ProjectSpace {...props} onTalk={talk} />)
  fireEvent.click(await screen.findByRole('checkbox', { name: 'input.csv' }))
  fireEvent.click(screen.getByRole('checkbox', { name: 'reference.csv' }))
  files = files.slice(1)
  fireEvent.click(screen.getByRole('button', { name: '刷新空间' }))
  await waitFor(() => expect(screen.queryByRole('checkbox', { name: 'input.csv' })).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: '带着所选资料开始对话' }))
  expect(talk).toHaveBeenCalledWith(expect.stringContaining(paths[1]))
  expect(talk.mock.calls[0][0]).not.toContain(paths[0])
})
