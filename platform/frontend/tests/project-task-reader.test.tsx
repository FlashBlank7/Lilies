import { Suspense, type ReactNode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectPage from '@/app/projects/[id]/page'
import { api } from '@/lib/platform'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
vi.mock('@/app/components/AppShell', () => ({ default: ({ children, navigation }: { children: ReactNode; navigation: ReactNode }) => <>{navigation}{children}</> }))
vi.mock('@/app/components/ProjectConversation', () => ({ default: () => null }))
vi.mock('@/app/projects/[id]/DeveloperTools', () => ({ default: () => null }))
afterEach(() => { cleanup(); vi.mocked(api).mockReset() })

it('reopens and stops a persisted manual run, then returns to its member input form without starting an agent', async () => {
  const task = { id: 't', request_key: 'request', status: 'running', mode: 'workflow', workflow_id: 'member', purpose: 'customer_trial', item_id: '', created_at: '2026-09-17T00:00:00Z', presentation: {}, inputs: {}, outputs: {} }
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/conversations')) return [] as never
    if (path.endsWith('/stop')) return { ...task, status: 'interrupted' } as never
    if (path.endsWith('/tasks/t')) return task as never
    if (path.includes('/tasks?purpose=customer_trial')) return [task] as never
    if (path.includes('/tasks?compact=true') || path.endsWith('/workspace/files')) return [] as never
    if (path.endsWith('/progress')) return { revision: 0, value: { goal: '', summary: '', items: [] } } as never
    if (path.endsWith('/draft')) return { snapshot: { workflow: { nodes: [] } } } as never
    if (options) throw new Error(path)
    return { id: 'p', name: 'Review project', members: [{ id: 'member', name: '设计评审', revision: 1, purpose: 'business' }] } as never
  })
  const params = Promise.resolve({ id: 'p' })
  await act(async () => { render(<Suspense><ProjectPage params={params} /></Suspense>) })
  fireEvent.click(await screen.findByRole('button', { name: '设计评审 · 处理中' }))
  fireEvent.click(await screen.findByRole('button', { name: '停止运行' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/projects/p/tasks/t/stop', { method: 'POST' }))
  fireEvent.click(await screen.findByRole('button', { name: '再次运行此工作流' }))
  expect(await screen.findByRole('combobox', { name: '入口工作流' })).toHaveValue('member')
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('agent-session/messages'))).toBe(false)
})
