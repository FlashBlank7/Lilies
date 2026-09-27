import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ApplyWorkflowCopy from '@/app/improvements/ApplyWorkflowCopy'
import { api } from '@/lib/platform'

vi.mock('@/lib/platform', () => ({ api: vi.fn() }))
afterEach(() => { cleanup(); vi.mocked(api).mockReset() })
const original = { nodes: [{ id: 'input', type: 'start', title: '数量', config: { required: true } }], edges: [], viewport: {} }
const replacement = { nodes: [{ id: 'input', type: 'start', title: '数量', config: { default: 1 } }], edges: [], viewport: {} }
const target = { revision: 3, snapshot: { name: '原流程', workflow: original } }
const source = { revision: 8, snapshot: { name: '修复副本', workflow: replacement } }
const endpoint = '/api/v1/projects/project/workflows/original/draft'
const props = { projectId: 'project', sourceId: 'copy', targetId: 'original', onClose: vi.fn() }
function reads() {
  vi.mocked(api).mockImplementation(async path => {
    if (path.endsWith('/original/draft')) return target as never
    if (path.endsWith('/copy/draft')) return source as never
    return { members: [{ id: 'original' }, { id: 'copy' }] } as never
  })
}

it('reads a concrete comparison without running, changing drafts, or calling an agent', async () => {
  reads(); render(<ApplyWorkflowCopy {...props} />)
  expect(await screen.findByText('修改积木：数量')).toBeInTheDocument()
  expect(screen.getByText(/来源草稿的完整流程图/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '查看来源画布' })).toHaveAttribute('href', '/applications/copy?tab=edit')
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options)).toBe(true)
})

it('applies exactly the displayed graph once and undoes against the returned revision', async () => {
  reads(); const read = vi.mocked(api).getMockImplementation()!
  let finish!: (value: unknown) => void
  vi.mocked(api).mockImplementation(async (path, options) => options ? await new Promise<unknown>(resolve => { finish = resolve }) as never : read(path, options))
  render(<ApplyWorkflowCopy {...props} />)
  const apply = await screen.findByRole('button', { name: '应用到原工作流' })
  fireEvent.click(apply); fireEvent.click(apply)
  let writes = vi.mocked(api).mock.calls.filter(([, options]) => options)
  expect(writes).toHaveLength(1)
  expect(writes[0][0]).toBe(endpoint)
  expect(JSON.parse(writes[0][1]!.body as string)).toMatchObject({ expected_revision: 3, workflow: replacement })
  // Another writer can save r5 before the server's final draft read. Our write is r4.
  await act(async () => finish({ workflow_id: 'original', revision: 4, draft: { ...target, revision: 5 }, previous_workflow: original }))
  expect(await screen.findByRole('status')).toHaveTextContent('已更新原工作流，本次保存为 r4。没有启动运行。')
  fireEvent.click(screen.getByRole('button', { name: '撤销本次更新' }))
  writes = vi.mocked(api).mock.calls.filter(([, options]) => options)
  expect(writes).toHaveLength(2)
  expect(JSON.parse(writes[1][1]!.body as string)).toMatchObject({ expected_revision: 4, workflow: original })
  await act(async () => finish({ workflow_id: 'original', revision: 5, draft: { ...target, revision: 5 }, previous_workflow: replacement }))
  expect(await screen.findByRole('status')).toHaveTextContent('已撤销本次更新')
  expect(vi.mocked(api).mock.calls.filter(([, options]) => options).every(([path, options]) => path === endpoint && options?.method === 'PUT')).toBe(true)
})

it('reports concurrent edits and never automatically reloads or overwrites them', async () => {
  reads(); const read = vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (options) throw new Error('画布已被修改，请刷新后合并；本次未覆盖已有改动')
    return read(path, options)
  })
  render(<ApplyWorkflowCopy {...props} />)
  fireEvent.click(await screen.findByRole('button', { name: '应用到原工作流' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('本次未覆盖已有改动')
  expect(vi.mocked(api).mock.calls.filter(([, options]) => options)).toHaveLength(1)
  expect(screen.queryByRole('button', { name: '撤销本次更新' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '重新读取草稿' })).toBeEnabled()
})

it('does not offer apply if either workflow has left this project', async () => {
  reads(); const read = vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async (path, options) => path === '/api/v1/projects/project' ? { members: [{ id: 'original' }] } as never : read(path, options))
  render(<ApplyWorkflowCopy {...props} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('同一项目中的两个不同工作流')
  expect(screen.queryByRole('button', { name: '应用到原工作流' })).not.toBeInTheDocument()
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options)).toBe(true)
})

it('disables an identical update and retries failed reads without mutating anything', async () => {
  let fail = true
  vi.mocked(api).mockImplementation(async path => {
    if (fail) throw new Error('暂时无法读取')
    return (path.endsWith('/draft') ? target : { members: [{ id: 'original' }, { id: 'copy' }] }) as never
  })
  render(<ApplyWorkflowCopy {...props} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法读取')
  fail = false
  fireEvent.click(screen.getByRole('button', { name: '重新读取草稿' }))
  expect(await screen.findByText('两个流程图内容相同，无需更新。')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '应用到原工作流' })).toBeDisabled()
  await waitFor(() => expect(vi.mocked(api).mock.calls).toHaveLength(6))
})
