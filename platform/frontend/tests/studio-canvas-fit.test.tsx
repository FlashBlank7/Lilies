import { Suspense } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Studio from '@/app/applications/[id]/page'
import { api } from '@/lib/platform'

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }))
vi.mock('@/lib/platform', async original => ({ ...await original<object>(), api: vi.fn(), getClientToken: () => '' }))
vi.mock('@/app/applications/[id]/block-catalog-panel', () => ({ BlockCatalogPanel: () => null, BlockInstanceDetails: () => null, BlockPurpose: () => null, UndefinedBusinessWorkflowNotice: () => null }))

// Keep ReactFlow, its measurement store, fit algorithm, and Controls real. JSDOM
// only supplies the browser layout/observation primitives that they consume.
const surface = { width: 592, height: 489 }
const nodeSize = { width: 220, height: 94 }
const observers = new Set<CanvasResizeObserver>()

class CanvasResizeObserver implements ResizeObserver {
  private targets = new Set<Element>()
  constructor(private callback: ResizeObserverCallback) { observers.add(this) }
  observe(target: Element) {
    this.targets.add(target)
    queueMicrotask(() => { if (this.targets.has(target)) this.deliver([target]) })
  }
  unobserve(target: Element) { this.targets.delete(target) }
  disconnect() { this.targets.clear(); observers.delete(this) }
  deliver(targets = [...this.targets]) {
    this.callback(targets.map(target => ({ target }) as ResizeObserverEntry), this)
  }
}

beforeEach(() => {
  surface.width = 592
  surface.height = 489
  window.history.replaceState(null, '', '/?tab=edit&embedded=1')
  window.localStorage.clear()
  vi.stubGlobal('ResizeObserver', CanvasResizeObserver)
  vi.stubGlobal('DOMMatrixReadOnly', class {
    m22: number
    constructor(transform: string) {
      this.m22 = Number(transform.match(/scale\(([^)]+)\)/)?.[1] ?? 1)
    }
  })
  for (const property of ['offsetWidth', 'clientWidth', 'offsetHeight', 'clientHeight'] as const) {
    const axis = property.endsWith('Width') ? 'width' : 'height'
    vi.spyOn(HTMLElement.prototype, property, 'get').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('react-flow__node') ? nodeSize[axis] : surface[axis]
    })
  }
})

afterEach(() => {
  cleanup()
  observers.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.mocked(api).mockReset()
  window.history.replaceState(null, '', '/')
  window.localStorage.clear()
})

function readViewport(container: HTMLElement) {
  const transform = (container.querySelector('.react-flow__viewport') as HTMLElement).style.transform
  const match = transform.match(/translate\(([-\d.e+]+)px,\s*([-\d.e+]+)px\) scale\(([-\d.e+]+)\)/i)
  expect(match).not.toBeNull()
  return { x: Number(match![1]), y: Number(match![2]), zoom: Number(match![3]) }
}

function projectedNodes(container: HTMLElement) {
  const viewport = readViewport(container)
  return [...container.querySelectorAll<HTMLElement>('.react-flow__node')].map(node => {
    const match = node.style.transform.match(/translate\(([-\d.e+]+)px,\s*([-\d.e+]+)px\)/i)!
    const left = viewport.x + Number(match[1]) * viewport.zoom
    const top = viewport.y + Number(match[2]) * viewport.zoom
    return { id: node.dataset.id, visible: node.style.visibility, left, top,
      right: left + node.offsetWidth * viewport.zoom, bottom: top + node.offsetHeight * viewport.zoom }
  })
}

async function expectFitted(container: HTMLElement, ids: string[]) {
  await waitFor(() => {
    const nodes = projectedNodes(container)
    expect(nodes.map(node => node.id).sort()).toEqual([...ids].sort())
    for (const node of nodes) {
      expect(node.visible).toBe('visible')
      expect(node.left).toBeGreaterThanOrEqual(0)
      expect(node.top).toBeGreaterThanOrEqual(0)
      expect(node.right).toBeLessThanOrEqual(surface.width)
      expect(node.bottom).toBeLessThanOrEqual(surface.height)
    }
    // These fixtures are wider than tall. Waiting for both padded edges also
    // waits for the real animated fit to finish before the next interaction.
    const padding = Math.floor((surface.width - surface.width / 1.22) / 2)
    const left = Math.min(...nodes.map(node => node.left))
    const right = Math.max(...nodes.map(node => node.right))
    expect(right - left).toBeCloseTo(surface.width - padding * 2, 3)
    // ReactFlow rounds applied padding to pixels for fractional coordinates.
    expect(Math.abs(left - padding)).toBeLessThanOrEqual(1.01)
    expect(Math.abs(right - (surface.width - padding))).toBeLessThanOrEqual(1.01)
  })
  return readViewport(container)
}

async function setup(spacing: number) {
  const innerNodes = Array.from({ length: 9 }, (_, index) => ({
    id: `step-${index}`, title: `内部步骤 ${index + 1}`, type: 'code', config: {},
    position: { x: 100 + index * spacing, y: 100 + index % 2 * 160 },
  }))
  const draft = { application_id: 'fit-workflow', revision: 1, content_hash: 'unchanged', validation_report: {}, snapshot: {
    name: '逐批处理', description: '', requirement: '逐批处理并汇总结果', mode: 'workflow', tests: [], agents: {},
    workflow: { nodes: [
      { id: 'each', title: '逐批循环', type: 'iteration', position: { x: 80, y: 120 },
        config: { items: [1, 2, 3], item_name: 'item', output_node_id: 'step-8', workflow: { nodes: innerNodes, edges: [] } } },
      { id: 'done', title: '汇总结果', type: 'end', position: { x: 520, y: 120 }, config: {} },
    ], edges: [] },
  } }
  vi.mocked(api).mockImplementation(async path => {
    if (path.endsWith('/draft')) return structuredClone(draft) as never
    if (path.startsWith('/api/v1/blocks?')) return ['iteration', 'code', 'end'].map(type => ({
      type, title: type, category: 'logic', input_ports: [], output_ports: [], config_schema: {}, editor: { fields: [] },
    })) as never
    if (path.endsWith('/project')) return { project_id: 'fit-project' } as never
    if (path === '/api/v1/projects/fit-project') return { id: 'fit-project', name: '画布测试', members: [] } as never
    if (path === '/health') return { status: 'ok' } as never
    return [] as never
  })
  const view = await act(async () => render(<Suspense><Studio params={Promise.resolve({ id: 'fit-workflow' })}/></Suspense>))
  await expectFitted(view.container, ['each', 'done'])
  return { ...view, innerIds: innerNodes.map(node => node.id) }
}

it.each([
  { label: 'the reported 2,150px loop', spacing: 241.25 },
  { label: 'a wider 7,420px loop', spacing: 900 },
])('fits $label into a 592px embedded canvas and keeps zoom controls usable', async ({ spacing }) => {
  const { container, innerIds } = await setup(spacing)
  fireEvent.doubleClick(screen.getByTestId('rf__node-each'))
  const fitted = await expectFitted(container, innerIds)
  expect(fitted.zoom).toBeLessThan(0.5)

  const zoomOut = screen.getByRole('button', { name: 'Zoom Out' })
  const zoomIn = screen.getByRole('button', { name: 'Zoom In' })
  expect(zoomOut).toBeEnabled()
  expect(zoomIn).toBeEnabled()
  fireEvent.click(zoomOut)
  await waitFor(() => expect(readViewport(container).zoom).toBeLessThan(fitted.zoom))
  fireEvent.click(zoomIn)
  fireEvent.click(zoomIn)
  fireEvent.click(zoomIn)
  await waitFor(() => expect(projectedNodes(container).some(node => node.left < 0 || node.right > surface.width)).toBe(true))
  fireEvent.click(screen.getByRole('button', { name: 'Fit View' }))
  await expectFitted(container, innerIds)

  fireEvent.click(screen.getByRole('button', { name: /返回上层/ }))
  await expectFitted(container, ['each', 'done'])
  expect(vi.mocked(api).mock.calls.filter(([, options]) => options?.method && options.method !== 'GET')).toEqual([])
})

it('uses the resized canvas dimensions when fitting the open loop again', async () => {
  surface.width = 1120
  const { container, innerIds } = await setup(241.25)
  fireEvent.doubleClick(screen.getByTestId('rf__node-each'))
  const desktop = await expectFitted(container, innerIds)

  await act(async () => {
    surface.width = 592
    for (const observer of observers) observer.deliver()
  })
  fireEvent.click(screen.getByRole('button', { name: 'Fit View' }))
  const narrow = await expectFitted(container, innerIds)
  expect(narrow.zoom).toBeLessThan(desktop.zoom)
  expect(screen.getByRole('button', { name: 'Zoom Out' })).toBeEnabled()
  expect(vi.mocked(api).mock.calls.filter(([, options]) => options?.method && options.method !== 'GET')).toEqual([])
})
