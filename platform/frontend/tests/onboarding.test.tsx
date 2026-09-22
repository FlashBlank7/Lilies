import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import Onboarding, { TutorialLauncher, useOnboarding } from '@/app/components/Onboarding'

const { push, location } = vi.hoisted(() => ({ push: vi.fn(), location: { path: '/projects' } }))
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }), usePathname: () => location.path }))
vi.mock('@/lib/platform', () => ({ api: vi.fn() }))
const fresh = () => ({ status: 'new', step: 'project', completed_steps: [] as string[], project_id: null as string | null })
let saved = fresh()
function Page() {
  const guide = useOnboarding()
  return <><TutorialLauncher /><input aria-label="我的草稿" defaultValue="不要覆盖" /><button onClick={() => guide.mark('materials', 'project-a')}>选中已有文件</button></>
}
function Tutorial() { return <Onboarding><Page /></Onboarding> }
beforeEach(() => {
  saved = fresh(); location.path = '/projects'; vi.clearAllMocks()
  vi.mocked(api).mockImplementation(async (url, options) => {
    expect(url).toBe('/api/v1/me/onboarding')
    if (options) {
      const patch = JSON.parse(options.body as string)
      if (patch.restart) saved = { ...fresh(), status: 'active' }
      else saved = { ...saved, ...patch, completed_steps: [...new Set([...saved.completed_steps, ...(patch.completed_steps || [])])] }
    }
    return { ...saved } as never
  })
})
afterEach(cleanup)

it('welcomes new accounts, lets them read every step without fabricating operations or starting work', async () => {
  render(<Tutorial />)
  expect(await screen.findByText('欢迎使用 Lilies')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '开始引导' }))
  for (let i = 1; i <= 5; i++) {
    await screen.findByText(`第 ${i} / 6 步 · 翻页只推进讲解`)
    await waitFor(() => expect(screen.getByRole('button', { name: '下一步' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  }
  await screen.findByText('第 6 / 6 步 · 翻页只推进讲解')
  fireEvent.click(screen.getByRole('button', { name: '结束讲解' }))
  expect(await screen.findByText('已结束讲解，可随时重看')).toBeInTheDocument()
  expect(saved.completed_steps).toEqual([])
  expect(screen.getByLabelText('我的草稿')).toHaveValue('不要覆盖')
  expect(vi.mocked(api).mock.calls.every(([url]) => url === '/api/v1/me/onboarding')).toBe(true)
})

it('skips without repeat welcome and permits explicit restart', async () => {
  const view = render(<Tutorial />)
  fireEvent.click(await screen.findByRole('button', { name: '稍后再看' }))
  await waitFor(() => expect(saved.status).toBe('skipped'))
  view.unmount(); render(<Tutorial />)
  await waitFor(() => expect(api).toHaveBeenCalledTimes(3))
  expect(screen.queryByRole('complementary', { name: '使用教程' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '使用教程' }))
  fireEvent.click(await screen.findByRole('button', { name: '从头查看' }))
  expect(await screen.findByText('第 1 / 6 步 · 翻页只推进讲解')).toBeInTheDocument()
})

it('records a real action and reloads progress in a separate mount', async () => {
  saved.status = 'active'; saved.step = 'materials'
  const view = render(<Tutorial />)
  await screen.findByText('第 2 / 6 步 · 翻页只推进讲解')
  fireEvent.click(screen.getByRole('button', { name: '选中已有文件' }))
  await waitFor(() => expect(saved.completed_steps).toEqual(['materials']))
  view.unmount(); render(<Tutorial />)
  expect(await screen.findByText('操作清单 · 1 / 6')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '去操作' }))
  expect(push).toHaveBeenCalledWith('/projects/project-a?guide=materials')
})

it('navigates actual project tabs without replacing drafts or sending requests', async () => {
  location.path = '/projects/project-a'; saved.status = 'active'; saved.step = 'conversation'
  const listener = vi.fn(); window.addEventListener('lilies:guide-navigate', listener)
  render(<Tutorial />)
  fireEvent.click(await screen.findByRole('button', { name: '去操作' }))
  expect(listener).toHaveBeenCalledOnce()
  expect(listener.mock.calls[0][0].detail).toBe('conversation')
  expect(screen.getByLabelText('我的草稿')).toHaveValue('不要覆盖')
  expect(api).toHaveBeenCalledTimes(1)
  window.removeEventListener('lilies:guide-navigate', listener)
})

it('keeps the page usable on read/write failure and retries unsaved progress', async () => {
  vi.mocked(api).mockRejectedValueOnce(new Error('offline'))
  render(<Tutorial />)
  fireEvent.click(await screen.findByRole('button', { name: '教程暂不可用 · 重试' }))
  fireEvent.click(await screen.findByRole('button', { name: '开始引导' }))
  await screen.findByText('第 1 / 6 步 · 翻页只推进讲解')
  vi.mocked(api).mockRejectedValueOnce(new Error('offline'))
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('正常使用不受影响')
  fireEvent.change(screen.getByLabelText('我的草稿'), { target: { value: '仍可编辑' } })
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  await screen.findByText('第 2 / 6 步 · 翻页只推进讲解')
  expect(screen.getByLabelText('我的草稿')).toHaveValue('仍可编辑')
})

it('does not apply a previous account response to a new account', async () => {
  let resolve!: (value: unknown) => void
  vi.mocked(api).mockImplementationOnce(() => new Promise(r => { resolve = r }) as never)
  const view = render(<Onboarding key="old"><Page /></Onboarding>)
  saved.status = 'skipped'
  view.rerender(<Onboarding key="new"><Page /></Onboarding>)
  await act(async () => { resolve({ ...fresh(), status: 'active' }) })
  expect(screen.queryByRole('complementary', { name: '使用教程' })).not.toBeInTheDocument()
})

it('restores launcher focus when the panel closes with Escape', async () => {
  saved.status = 'skipped'; render(<Tutorial />)
  const launcher = screen.getByRole('button', { name: '使用教程' })
  launcher.focus(); fireEvent.click(launcher)
  const panel = await screen.findByRole('complementary', { name: '使用教程' })
  fireEvent.keyDown(panel, { key: 'Escape' })
  expect(launcher).toHaveFocus()
})


it('collapses when editing the page so the panel does not cover inputs', async () => {
  saved.status = 'active'; render(<Tutorial />)
  await screen.findByRole('complementary', {name: '使用教程'})
  fireEvent.focusIn(screen.getByLabelText('我的草稿'))
  expect(screen.queryByRole('complementary', {name: '使用教程'})).not.toBeInTheDocument()
  expect(screen.getByRole('button', {name: '继续教程'})).toBeInTheDocument()
  expect(saved.status).toBe('active')
})
