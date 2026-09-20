import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import ProjectMaterials from '@/app/components/ProjectMaterials'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
beforeEach(() => { vi.mocked(api).mockReset() })

it('adds new files and refreshes the visible material list without starting a model', async () => {
  let uploaded = false
  vi.mocked(api).mockImplementation(async (path, request) => {
    if (request?.method === 'POST') { uploaded = true; return { name: '新设计.pdf' } as never }
    return (uploaded ? [{ path: 'requirement-package/追加资料/upload/新设计.pdf', size: 7 }] : []) as never
  })
  render(<ProjectMaterials id="project" onOpenFile={vi.fn()} />)
  const file = new File(['new pdf'], '新设计.pdf')
  fireEvent.change(screen.getByLabelText('添加项目资料'), { target: { files: [file] } })
  expect(await screen.findByRole('status')).toHaveTextContent('已添加：新设计.pdf')
  expect(await screen.findByText('企业资料 · 1 个文件')).toBeInTheDocument()
  const writes = vi.mocked(api).mock.calls.filter(([, request]) => request?.method === 'POST')
  expect(writes).toHaveLength(1)
  expect(writes[0][0]).toBe('/api/v1/projects/project/materials')
  expect((writes[0][1]?.body as FormData).get('file')).toBe(file)
})

it('keeps successful uploads visible when a later file fails and allows retry', async () => {
  vi.mocked(api).mockImplementation(async (_path, request) => {
    if (request?.method === 'POST') {
      const file = (request.body as FormData).get('file') as File
      if (file.name === 'bad.zip') throw new Error('文件过大')
      return { name: file.name } as never
    }
    return [] as never
  })
  render(<ProjectMaterials id="project" onOpenFile={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('添加项目资料'), { target: { files: [new File(['a'], 'good.pdf'), new File(['b'], 'bad.zip')] } })
  expect(await screen.findByRole('alert')).toHaveTextContent('文件过大')
  expect(screen.getByRole('status')).toHaveTextContent('good.pdf')
  await waitFor(() => expect(screen.getByRole('button', { name: '添加资料' })).toBeEnabled())
})

it('selects original materials from another project and keeps successful copies on retry', async () => {
  const files = [{ path: 'requirement-package/客户输入/设计.pdf', size: 500 },
    { path: 'requirement-package/客户输入/源码.zip', size: 188 * 1024 * 1024 },
    { path: 'results/旧报告.pdf', size: 100 }]
  const copied: string[] = []
  let failSource = true
  vi.mocked(api).mockImplementation(async (path, request) => {
    if (path === '/api/v1/projects') return [{ id: 'source', name: '原资料项目' }, { id: 'target', name: '当前项目' }] as never
    if (path === '/api/v1/applications/source/workspace/files') return files as never
    if (path.endsWith('/materials/copy')) {
      const body = JSON.parse(request?.body as string)
      expect(body.source_project_id).toBe('source')
      if (body.source_path.endsWith('源码.zip') && failSource) throw new Error('复制失败，请重试')
      copied.push(body.source_path)
      return { name: body.source_path.split('/').pop() } as never
    }
    return copied.map((path, i) => ({ path: `requirement-package/追加资料/${i}/${path.split('/').pop()}`, size: 100 })) as never
  })
  render(<ProjectMaterials id="target" onOpenFile={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '从已有项目选择资料' }))
  fireEvent.change(await screen.findByRole('combobox', { name: '资料来源项目' }), { target: { value: 'source' } })
  const design = await screen.findByRole('checkbox', { name: /设计.pdf/ })
  const source = screen.getByRole('checkbox', { name: /源码.zip/ })
  expect(screen.queryByRole('checkbox', { name: /旧报告/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('option', { name: '当前项目' })).not.toBeInTheDocument()
  expect(source.closest('label')).toHaveTextContent('188.0 MB')
  fireEvent.click(design); fireEvent.click(source)
  fireEvent.click(screen.getByRole('button', { name: '添加所选资料' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('复制失败，请重试')
  expect(screen.getByRole('status')).toHaveTextContent('设计.pdf')
  expect(design).not.toBeChecked(); expect(source).toBeChecked()
  failSource = false
  fireEvent.click(screen.getByRole('button', { name: '添加所选资料' }))
  await waitFor(() => expect(copied).toEqual(files.slice(0, 2).map(file => file.path)))
  expect(await screen.findByText('企业资料 · 2 个文件')).toBeInTheDocument()
})
