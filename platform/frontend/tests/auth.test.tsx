import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import AccountForm from '@/app/components/AccountForm'
import ProjectAccessMembers from '@/app/components/ProjectAccessMembers'
import { api } from '@/lib/platform'

const { replace, refresh } = vi.hoisted(() => ({ replace: vi.fn(), refresh: vi.fn() }))
vi.mock('next/navigation', () => ({ useRouter: () => ({ replace, refresh }), useSearchParams: () => new URLSearchParams() }))
vi.mock('@/lib/platform', () => ({ api: vi.fn() }))
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api).mockResolvedValue({} as never) })
afterEach(cleanup)

it('registers an ordinary account without a registration token', async () => {
  render(<AccountForm register />)
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: ' 新用户 ' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '注册并进入平台' }))
  await waitFor(() => expect(replace).toHaveBeenCalledWith('/projects'))
  expect(api).toHaveBeenCalledWith('/api/v1/auth/register', { method: 'POST', body: JSON.stringify({ name: '新用户', password: 'password123' }) })
  expect(screen.queryByLabelText('注册令牌')).not.toBeInTheDocument()
})

it('keeps a mismatched password on the form without sending it', async () => {
  render(<AccountForm register />)
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: '甲' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'different123' } })
  fireEvent.click(screen.getByRole('button', { name: '注册并进入平台' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('两次输入的密码不一致')
  expect(api).not.toHaveBeenCalled()
})

it('shows a failed login and allows another attempt', async () => {
  vi.mocked(api).mockRejectedValueOnce(new Error('用户名或密码不正确'))
  render(<AccountForm />)
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: '甲' } })
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
  fireEvent.click(screen.getByRole('button', { name: '登录' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('用户名或密码不正确')
  expect(screen.getByRole('button', { name: '登录' })).not.toBeDisabled()
  expect(replace).not.toHaveBeenCalled()
})

it('lets the project owner add a registered collaborator', async () => {
  const owner = { id: 'owner', name: '甲', role: 'owner', status: 'active' }
  vi.mocked(api).mockResolvedValueOnce([owner] as never).mockResolvedValueOnce([owner, { id: 'member', name: '乙', role: 'collaborator', status: 'active' }] as never)
  const changed = vi.fn()
  render(<ProjectAccessMembers projectId="project" canManage onChanged={changed} />)
  await screen.findByText('甲 · 负责人')
  fireEvent.change(screen.getByLabelText('成员用户名'), { target: { value: '乙' } })
  fireEvent.click(screen.getByRole('button', { name: '添加成员' }))
  expect(await screen.findByText('乙 · 协作者')).toBeInTheDocument()
  expect(api).toHaveBeenLastCalledWith('/api/v1/projects/project/access-members', { method: 'POST', body: JSON.stringify({ name: '乙', role: 'collaborator' }) })
  expect(changed).toHaveBeenCalled()
})
