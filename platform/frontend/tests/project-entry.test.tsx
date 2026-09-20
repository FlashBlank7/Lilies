import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import Home from '@/app/page'
import Projects from '@/app/projects/page'

const { push, redirect } = vi.hoisted(() => ({ push: vi.fn(), redirect: vi.fn((path: string) => { throw new Error('redirect:' + path) }) }))
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }), redirect }))
vi.mock('@/lib/platform', () => ({ api: vi.fn(), saveClientToken: vi.fn(), isAuthError: (e: { status?: number }) => e.status === 401 }))
afterEach(cleanup)
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api).mockReset() })

it('opens projects from the platform home', () => {
  expect(() => Home()).toThrow('redirect:/projects')
  expect(redirect).toHaveBeenCalledWith('/projects')
})

it('opens new and existing projects without starting analysis or building', async () => {
  vi.mocked(api).mockImplementation(async (_, options) => options?.method === 'POST'
    ? { id: 'new' } as never : [{ id: 'existing', name: '库存改进', description: '按订单分配', members: [] }] as never)
  render(<Projects />)
  expect(await screen.findByRole('link', { name: '打开项目：库存改进' })).toHaveAttribute('href', '/projects/existing')
  expect(screen.queryByRole('link', { name: '历史工作流与开发工具' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '打开新项目' }))
  fireEvent.change(screen.getByLabelText('新项目名称'), { target: { value: '  新业务  ' } })
  fireEvent.click(screen.getByRole('button', { name: '创建并打开' }))
  await waitFor(() => expect(push).toHaveBeenCalledWith('/projects/new'))
  expect(vi.mocked(api).mock.calls).toEqual([
    ['/api/v1/projects'], ['/api/v1/projects', { method: 'POST', body: JSON.stringify({ name: '新业务' }) }],
  ])
})

it('imports materials and opens the resulting project', async () => {
  vi.mocked(api).mockImplementation(async (_, options) => options?.method === 'POST'
    ? { application: { id: 'imported' } } as never : [] as never)
  render(<Projects />)
  fireEvent.click(screen.getByRole('button', { name: '导入需求包' }))
  fireEvent.change(screen.getByLabelText('选择需求包 ZIP'), { target: { files: [new File(['zip'], '材料.zip')] } })
  fireEvent.click(screen.getByRole('button', { name: '导入并打开项目' }))
  await waitFor(() => expect(push).toHaveBeenCalledWith('/projects/imported'))
  expect(vi.mocked(api).mock.calls.map(([path]) => path)).toEqual(['/api/v1/projects', '/api/v1/projects/requirement-packages/import'])
})

it('directs an expired session to login instead of asking for a service token', async () => {
  vi.mocked(api).mockRejectedValueOnce({ status: 401 })
  render(<Projects />)
  expect(await screen.findByRole('link', { name: '请重新登录' })).toHaveAttribute('href', '/login')
  expect(screen.queryByLabelText('访问令牌')).not.toBeInTheDocument()
})
