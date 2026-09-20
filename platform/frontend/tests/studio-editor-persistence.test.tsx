import { Suspense } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import Studio from '@/app/applications/[id]/page'
import { api } from '@/lib/platform'

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }))
vi.mock('@/lib/platform', async importOriginal => ({ ...await importOriginal<object>(), api: vi.fn(), getClientToken: () => '' }))
vi.mock('@xyflow/react', async importOriginal => ({
  ...await importOriginal<object>(),
  ReactFlow: ({ nodes, onNodeClick }: { nodes: { id: string }[]; onNodeClick: (event: unknown, node: { id: string }) => void }) => <>{nodes.map(node => <button key={node.id} onClick={event => onNodeClick(event, node)}>Select {node.id}</button>)}</>,
}))
vi.mock('@/app/applications/[id]/block-catalog-panel', () => ({ BlockCatalogPanel: () => null, BlockInstanceDetails: () => null, BlockPurpose: () => null, UndefinedBusinessWorkflowNotice: () => null }))
afterEach(() => { cleanup(); vi.mocked(api).mockReset() })

it.each([
  { $ref: { node_id: 'prepare', path: ['json', 'requirements'] } },
  [{ title: '保留数组对象' }],
  '123',
].map(items => ({ items })))('preserves untyped schema values through form and JSON editing: %j', async ({ items }) => {
  const config = { items, workflow: { nodes: [], edges: [] } }
  const draft = { application_id: 'p', revision: 1, content_hash: 'one', validation_report: {},
    snapshot: { name: 'Editor', description: '', requirement: 'Edit a reference', mode: 'workflow', tests: [], agents: {}, workflow: {
      nodes: [{ id: 'each', title: 'each', type: 'iteration', position: { x: 0, y: 0 }, config }], edges: [] } } }
  const saved: unknown[] = []
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft') && options?.method === 'POST') {
      saved.push(JSON.parse(options.body as string).data.changes.config)
      return draft as never
    }
    if (path.endsWith('/draft')) return draft as never
    if (path.startsWith('/api/v1/blocks?application_id=')) return [{ type: 'iteration', title: 'Iteration', category: 'logic', input_ports: [], output_ports: [], editor: { fields: [] },
      config_schema: { properties: { items: { title: 'Items' }, workflow: { type: 'object' } }, required: ['items', 'workflow'] } }] as never
    if (path.endsWith('/project')) return { project_id: null } as never
    if (path === '/health') return { status: 'ok' } as never
    return [] as never
  })
  const params = Promise.resolve({ id: 'p' })
  const { container } = await act(async () => render(<Suspense><Studio params={params} /></Suspense>))
  fireEvent.click(await screen.findByRole('button', { name: 'Select each' }))
  fireEvent.click(container.querySelector('[data-config-editor-mode="json"]')!)
  const editor = () => container.querySelector('textarea.json-editor') as HTMLTextAreaElement
  expect(JSON.parse(editor().value)).toEqual(config)
  fireEvent.click(container.querySelector('[data-config-editor-mode="form"]')!)
  fireEvent.click(container.querySelector('[data-config-editor-mode="json"]')!)
  fireEvent.click(container.querySelector('[data-config-editor-action="save"]')!)
  await waitFor(() => expect(saved).toHaveLength(1))
  expect(saved[0]).toEqual(config)
})

it.each(['same', 'other'])('preserves new edits to the %s node when an earlier save finishes', async target => {
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  let saves = 0
  const saved: { node_id: string; changes: { config: object } }[] = []
  let draft = { application_id: 'p', revision: 1, content_hash: 'one', validation_report: {},
    snapshot: { name: 'Editor', description: '', requirement: 'Test manual editing', mode: 'workflow', tests: [], agents: {}, workflow: {
      nodes: ['prepare', 'report'].map(id => ({ id, title: id, type: 'tool', position: { x: 0, y: 0 }, config: { tool_name: 'Bash', input: { command: id } } })), edges: [] } } }
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft') && options?.method === 'POST') {
      const { data } = JSON.parse(options.body as string)
      saved.push(data)
      if (++saves === 1) await pending
      draft = { ...draft, revision: draft.revision + 1, snapshot: { ...draft.snapshot, workflow: { ...draft.snapshot.workflow,
        nodes: draft.snapshot.workflow.nodes.map(node => node.id === data.node_id ? { ...node, config: data.changes.config } : node) } } }
      return draft as never
    }
    if (path.endsWith('/draft')) return draft as never
    if (path.endsWith('/project')) return { project_id: null } as never
    if (path === '/health') return { status: 'ok' } as never
    return [] as never
  })
  const params = Promise.resolve({ id: 'p' })
  const { container } = await act(async () => render(<Suspense><Studio params={params} /></Suspense>))
  fireEvent.click(await screen.findByRole('button', { name: 'Select prepare' }))
  const editor = () => container.querySelector('textarea.json-editor') as HTMLTextAreaElement
  const save = () => container.querySelector('[data-config-editor-action="save"]') as HTMLButtonElement
  fireEvent.change(editor(), { target: { value: '{"tool_name":"Bash","input":{"command":"first"}}' } })
  fireEvent.click(save())
  await waitFor(() => expect(saved).toHaveLength(1))
  if (target === 'other') fireEvent.click(screen.getByRole('button', { name: 'Select report' }))
  const edited = '{"tool_name":"Bash","input":{"command":"new unsaved code"}}'
  fireEvent.change(editor(), { target: { value: edited } })
  await act(async () => { release(); await pending })
  await waitFor(() => expect(container).toHaveTextContent('r2'))
  expect(editor().value).toBe(edited)
  fireEvent.click(save())
  await waitFor(() => expect(saved).toHaveLength(2))
  expect(saved[1]).toMatchObject({ node_id: target === 'other' ? 'report' : 'prepare', changes: { config: JSON.parse(edited) } })
})
