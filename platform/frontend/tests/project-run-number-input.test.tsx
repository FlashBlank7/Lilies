import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import ProjectRunPanel from '@/app/components/ProjectRunPanel'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (value: string) => value }))
afterEach(cleanup)
beforeEach(() => { vi.mocked(api).mockReset() })

const members = [{ id: 'workflow', name: '数值计算', description: '', revision: 1, purpose: 'business' }]
type NumberField = { name: string; label?: string; type: string; default?: number; required?: boolean }
const field: NumberField = { name: 'amount', label: '调整金额', type: 'number', default: 0, required: true }

async function show(input: NumberField = field, launch: () => Promise<unknown> = async () => ({ id: 'task', status: 'succeeded', outputs: { markdown: '计算完成' } })) {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/readiness')) return { status: 'configured', issues: [] } as never
    if (path.endsWith('/draft')) return { snapshot: { workflow: { nodes: [{ type: 'start', config: { inputs: [input] } }] } } } as never
    if (path.endsWith('/workspace/files')) return [] as never
    if (path.endsWith('/tasks') && options?.method === 'POST') return await launch() as never
    throw new Error(path)
  })
  await act(async () => { render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="workflow" />) })
}

function submissions() {
  return vi.mocked(api).mock.calls.filter(([path, options]) => path.endsWith('/tasks') && options?.method === 'POST')
}

it.each([
  { initial: 0, value: undefined, expected: 0 },
  { initial: 5, value: '0', expected: 0 },
  { initial: 0, value: '1.25', expected: 1.25 },
  { initial: 0, value: '-2.5', expected: -2.5 },
])('submits numeric $expected from default $initial', async ({ initial, value, expected }) => {
  await show({ ...field, default: initial })
  const input = screen.getByRole('spinbutton', { name: '调整金额' })
  expect(input).toHaveValue(initial)
  expect(input).toHaveAttribute('step', 'any')
  if (value !== undefined) fireEvent.change(input, { target: { value } })
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  await screen.findByText('计算完成')
  expect(submissions()).toHaveLength(1)
  expect(JSON.parse(submissions()[0][1]!.body as string).inputs).toEqual({ amount: expected })
})

it('uses the field name when no readable label exists and omits an optional cleared number', async () => {
  await show({ name: 'amount', type: 'number', default: 5 })
  const input = screen.getByRole('spinbutton', { name: 'amount' })
  fireEvent.change(input, { target: { value: '' } })
  expect(input).toHaveValue(null)
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  await screen.findByText('计算完成')
  expect(JSON.parse(submissions()[0][1]!.body as string).inputs).toEqual({})
})

it('blocks an empty required number without submitting a task', async () => {
  await show()
  const input = screen.getByRole('spinbutton', { name: '调整金额' })
  fireEvent.change(input, { target: { value: '' } })
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('请填写 amount')
  expect(submissions()).toHaveLength(0)
  expect(input).toBeEnabled()
})

it('disables the number while starting and while the task is active', async () => {
  let complete!: (task: unknown) => void
  await show(field, () => new Promise(resolve => { complete = resolve }))
  const input = screen.getByRole('spinbutton', { name: '调整金额' })
  expect(input).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '正在启动…' })).toBeDisabled())
  expect(input).toBeDisabled()
  await act(async () => { complete({ id: 'task', status: 'running', runs: [], outputs: {} }) })
  expect(screen.getByRole('button', { name: '正在运行…' })).toBeDisabled()
  expect(input).toBeDisabled()
  expect(submissions()).toHaveLength(1)
})
