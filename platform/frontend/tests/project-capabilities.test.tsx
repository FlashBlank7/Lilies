import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import ProjectCapabilities from '@/app/components/ProjectCapabilities'

vi.mock('@/lib/platform', () => ({ api: vi.fn() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('saves an explicit selection and keeps the effective state until the server confirms it', async () => {
  const saved = vi.fn()
  vi.mocked(api).mockResolvedValue({ agent_modules_enabled: true })
  const { rerender } = render(<ProjectCapabilities projectId="project" enabled={false} onSaved={saved} />)
  fireEvent.click(screen.getByText('搭建能力 · 禁止智能体积木'))
  fireEvent.change(screen.getByLabelText('智能体积木'), { target: { value: 'allowed' } })
  expect(screen.getByText('搭建能力 · 禁止智能体积木')).toBeInTheDocument()
  expect(api).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '保存搭建能力' }))
  await waitFor(() => expect(saved).toHaveBeenCalledOnce())
  expect(api).toHaveBeenCalledWith('/api/v1/projects/project/capabilities', {
    method: 'PUT', body: JSON.stringify({ agent_modules_enabled: true }),
  })
  rerender(<ProjectCapabilities projectId="project" enabled={true} onSaved={saved} />)
  expect(screen.getByText('搭建能力 · 允许智能体积木')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '保存搭建能力' })).toBeDisabled()
})

it('preserves the effective prohibition when saving fails', async () => {
  const saved = vi.fn()
  vi.mocked(api).mockRejectedValue(new Error('保存失败'))
  render(<ProjectCapabilities projectId="project" enabled={false} onSaved={saved} />)
  fireEvent.click(screen.getByText('搭建能力 · 禁止智能体积木'))
  fireEvent.change(screen.getByLabelText('智能体积木'), { target: { value: 'allowed' } })
  fireEvent.click(screen.getByRole('button', { name: '保存搭建能力' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('保存失败')
  expect(saved).not.toHaveBeenCalled()
  expect(screen.getByText('搭建能力 · 禁止智能体积木')).toBeInTheDocument()
})
