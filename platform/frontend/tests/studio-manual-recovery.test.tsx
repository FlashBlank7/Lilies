import { Suspense } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import Studio from '@/app/applications/[id]/page'
import { api, PlatformApiError, type Draft, type WorkflowNode } from '@/lib/platform'

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }))
vi.mock('@/lib/platform', async original => ({ ...await original<object>(), api: vi.fn(), getClientToken: () => '' }))
vi.mock('@xyflow/react', async original => ({
  ...await original<object>(),
  ReactFlow: ({ nodes, onNodeClick }: { nodes: { id: string }[]; onNodeClick: (event: unknown, node: { id: string }) => void }) => <>{nodes.map(node => <button key={node.id} onClick={event => onNodeClick(event, node)}>Select {node.id}</button>)}</>,
}))
vi.mock('@/app/applications/[id]/block-catalog-panel', () => ({ BlockCatalogPanel: () => null, BlockInstanceDetails: () => null, BlockPurpose: () => null, UndefinedBusinessWorkflowNotice: () => null }))
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); window.history.replaceState(null, '', '/') })

function node(id: string, config: Record<string, unknown>, type = 'tool'): WorkflowNode {
  return { id, title: id, type, config, position: { x: 0, y: 0 }, block_version: 1, description: '', retry: { enabled: false, max_attempts: 1, delay_seconds: 0 }, error_strategy: 'fail' }
}
type Write = { expected_revision: number; op: string; data: { node_id?: string; changes?: Partial<WorkflowNode>; workflow?: Draft['snapshot']['workflow'] } }
async function setup(workflow?: Draft['snapshot']['workflow']) {
  window.history.replaceState(null, '', '/?tab=edit')
  const state = {
    draft: { application_id: 'p', revision: 1, content_hash: 'one', validation_report: {}, evidence: { state: 'missing', current_hash: 'one', change_summary: [], revalidate_endpoint: '' }, snapshot: {
      name: '保留名称', description: '保留说明', requirement: '', mode: 'workflow', tests: [{ name: '保留测试' }], agents: {},
      workflow: workflow ? structuredClone(workflow) : { nodes: [node('group', { tool_name: 'table', input: { group: 'daily' } }), node('other', { untouched: true })], edges: [], viewport: {} },
    } } as Draft,
    writes: [] as Write[],
    beforeWrite: null as (() => void) | null,
  }
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft') && options?.method === 'POST') {
      const body = JSON.parse(options.body as string) as Write
      state.writes.push(body)
      state.beforeWrite?.()
      if (body.expected_revision !== state.draft.revision) throw new PlatformApiError(409, 'Conflict', 'revision changed')
      if (body.op === 'replace_workflow') state.draft.snapshot.workflow = structuredClone(body.data.workflow!)
      else Object.assign(state.draft.snapshot.workflow.nodes.find(item => item.id === body.data.node_id)!, structuredClone(body.data.changes))
      state.draft.revision += 1
      state.draft.content_hash = String(state.draft.revision)
      // The real mutation endpoint returns revision/hash, not a complete draft.
      return { revision: state.draft.revision, content_hash: state.draft.content_hash } as never
    }
    if (path.endsWith('/draft')) return structuredClone(state.draft) as never
    if (path.endsWith('/project')) return { project_id: null } as never
    if (path === '/health') return { status: 'ok' } as never
    return [] as never
  })
  const params = Promise.resolve({ id: 'p' })
  const view = await act(async () => render(<Suspense><Studio params={params} /></Suspense>))
  await screen.findByRole('button', { name: '撤销上次手动保存' })
  const editor = () => view.container.querySelector('textarea.json-editor') as HTMLTextAreaElement
  const save = async (config: Record<string, unknown>) => {
    fireEvent.change(editor(), { target: { value: JSON.stringify(config) } })
    fireEvent.click(view.container.querySelector('[data-config-editor-action="save"]')!)
    await waitFor(() => expect(view.container.querySelector('[data-config-editor-action="save"]')).not.toBeDisabled())
  }
  return { state, params, editor, save, ...view }
}

it('undoes the last configuration save once and retains everything outside that save', async () => {
  const { state, editor, save } = await setup()
  const before = structuredClone(state.draft.snapshot)
  fireEvent.click(screen.getByRole('button', { name: 'Select group' }))
  await save({ tool_name: 'table', input: { group: 'weekly' } })
  expect(state.draft.snapshot.workflow.nodes[0].config.input).toEqual({ group: 'weekly' })
  fireEvent.click(screen.getByRole('button', { name: '撤销上次手动保存' }))
  await screen.findByText('已撤销上次手动保存，配置及关联连线已恢复。')
  expect(state.draft.snapshot).toEqual(before)
  expect(JSON.parse(editor().value)).toEqual(before.workflow.nodes[0].config)
  expect(state.writes.map(write => write.expected_revision)).toEqual([1, 2])
  expect(screen.getByRole('button', { name: '撤销上次手动保存' })).toBeDisabled()
})

it('keeps only the most recent successful manual save', async () => {
  const { state, save } = await setup()
  fireEvent.click(screen.getByRole('button', { name: 'Select group' }))
  await save({ group: 'weekly' })
  await save({ group: 'monthly' })
  fireEvent.click(screen.getByRole('button', { name: '撤销上次手动保存' }))
  await screen.findByText('已撤销上次手动保存，配置及关联连线已恢复。')
  expect(state.draft.snapshot.workflow.nodes[0].config).toEqual({ group: 'weekly' })
  expect(state.writes.map(write => write.expected_revision)).toEqual([1, 2, 3])
})

it.each([false, true])('preserves newer edits when they arrive %s during undo', async race => {
  const { state, save } = await setup()
  fireEvent.click(screen.getByRole('button', { name: 'Select group' }))
  await save({ group: 'weekly' })
  const newerEdit = () => {
    state.draft.revision += 1
    state.draft.snapshot.workflow.nodes[1].config = { other_person: 'new edit' }
  }
  if (race) state.beforeWrite = newerEdit
  else newerEdit()
  fireEvent.click(screen.getByRole('button', { name: '撤销上次手动保存' }))
  await screen.findByText('无法撤销：草稿已有后续修改，未覆盖这些修改。请查看最新配置后手动调整。')
  expect(state.draft.snapshot.workflow.nodes[0].config).toEqual({ group: 'weekly' })
  expect(state.draft.snapshot.workflow.nodes[1].config).toEqual({ other_person: 'new edit' })
  expect(state.writes).toHaveLength(race ? 2 : 1)
  expect(screen.queryByText('已撤销上次手动保存，配置及关联连线已恢复。')).not.toBeInTheDocument()
})

it('retains recovery after an undo request fails and allows a successful retry', async () => {
  const { state, save } = await setup()
  fireEvent.click(screen.getByRole('button', { name: 'Select group' }))
  await save({ group: 'weekly' })
  state.beforeWrite = () => { throw new Error('connection lost') }
  fireEvent.click(screen.getByRole('button', { name: '撤销上次手动保存' }))
  await screen.findByText('撤销失败：Error: connection lost')
  expect(state.draft.snapshot.workflow.nodes[0].config).toEqual({ group: 'weekly' })
  expect(screen.getByRole('button', { name: '撤销上次手动保存' })).not.toBeDisabled()
  state.beforeWrite = null
  fireEvent.click(screen.getByRole('button', { name: '撤销上次手动保存' }))
  await screen.findByText('已撤销上次手动保存，配置及关联连线已恢复。')
  expect(state.draft.snapshot.workflow.nodes[0].config.input).toEqual({ group: 'daily' })
})

it('does not offer recovery for a failed save or intercept native text undo', async () => {
  const { state, editor, save } = await setup()
  fireEvent.click(screen.getByRole('button', { name: 'Select group' }))
  state.beforeWrite = () => { throw new Error('save rejected') }
  await save({ group: 'weekly' })
  await screen.findByText('保存失败：Error: save rejected')
  expect(screen.getByRole('button', { name: '撤销上次手动保存' })).toBeDisabled()
  const key = new KeyboardEvent('keydown', { key: 'z', metaKey: true, bubbles: true, cancelable: true })
  fireEvent(editor(), key)
  expect(key.defaultPrevented).toBe(false)
  expect(state.writes).toHaveLength(1)
})

it('protects unsaved text and drops the recovery snapshot when the page is reopened', async () => {
  const { state, editor, save, unmount, params } = await setup()
  fireEvent.click(screen.getByRole('button', { name: 'Select group' }))
  await save({ group: 'weekly' })
  fireEvent.change(editor(), { target: { value: '{"group":"unsaved"}' } })
  fireEvent.click(screen.getByRole('button', { name: '撤销上次手动保存' }))
  await screen.findByText('当前还有未保存的配置，请先保存或重新选择积木放弃编辑，再撤销。')
  expect(editor().value).toBe('{"group":"unsaved"}')
  expect(state.writes).toHaveLength(1)
  unmount()
  await act(async () => render(<Suspense><Studio params={params} /></Suspense>))
  expect(await screen.findByRole('button', { name: '撤销上次手动保存' })).toBeDisabled()
  expect(screen.getByText(/刷新或离开页面后不可用/)).toBeInTheDocument()
})

it('restores a nested configuration without changing same-named nodes outside the container', async () => {
  const workflow = { nodes: [node('loop', { items: [], workflow: { nodes: [node('group', { group: 'inner' })], edges: [] } }, 'iteration'), node('group', { group: 'outer' })], edges: [], viewport: {} }
  const { state, save } = await setup(workflow)
  fireEvent.click(screen.getByRole('button', { name: 'Select loop' }))
  fireEvent.click(screen.getByRole('button', { name: '进入循环内部编辑' }))
  fireEvent.click(screen.getByRole('button', { name: 'Select group' }))
  await save({ group: 'changed inner' })
  fireEvent.click(screen.getByRole('button', { name: '撤销上次手动保存' }))
  await screen.findByText('已撤销上次手动保存，配置及关联连线已恢复。')
  expect(state.draft.snapshot.workflow).toEqual(workflow)
})

it('saves and restores aggregator configuration with its automatic connection changes', async () => {
  const ref = (id: string) => ({ variables: [{ $ref: { node_id: id, path: ['output'] } }] })
  const workflow = { nodes: [node('old', {}), node('new', {}), node('group', ref('old'), 'variable_aggregator')],
    edges: [{ id: 'old-edge', source: 'old', target: 'group', source_port: 'output', target_port: 'input' }], viewport: {} }
  const { state, save } = await setup(workflow)
  fireEvent.click(screen.getByRole('button', { name: 'Select group' }))
  await save(ref('new'))
  expect(state.writes).toHaveLength(1)
  expect(state.draft.snapshot.workflow.edges).toEqual([expect.objectContaining({ source: 'new', target: 'group' })])
  fireEvent.click(screen.getByRole('button', { name: '撤销上次手动保存' }))
  await screen.findByText('已撤销上次手动保存，配置及关联连线已恢复。')
  expect(state.draft.snapshot.workflow).toEqual(workflow)
})
