import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectFileReader from '@/app/components/ProjectFileReader'
vi.mock('@/lib/platform', () => ({ withFrontendToken: (path: string) => path }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('previews quoted Chinese CSV without changing identifiers or losing the download', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response('棒号,原因\n001,"余料102毫米，未纳入200毫米"\n002,"多行\n说明"'))
  vi.stubGlobal('fetch', fetcher)
  render(<ProjectFileReader projectId="p" path="results/明细.csv" onClose={() => {}} />)
  const table = await screen.findByRole('table')
  expect(table).toHaveTextContent('001')
  expect(table).toHaveTextContent('余料102毫米，未纳入200毫米')
  expect(table).toHaveTextContent('多行 说明')
  expect(screen.getByRole('link', { name:'下载原文件' })).toHaveAttribute('download')
})

it('refuses traversal before fetching any file', () => {
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  render(<ProjectFileReader projectId="p" path="results/../other/secret.md" onClose={() => {}} />)
  expect(screen.getByRole('alert')).toHaveTextContent('只能预览当前项目')
  expect(fetcher).not.toHaveBeenCalled()
})

it('isolates generated HTML from the project page', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<h1>可打印明细</h1><script>parent.alert(1)</script>')))
  render(<ProjectFileReader projectId="p" path="results/report.html" onClose={() => {}} />)
  const report = await screen.findByTitle('项目报告预览')
  expect(report).toHaveAttribute('sandbox', '')
  expect(report.getAttribute('srcdoc')).toContain("default-src 'none'")
  expect(screen.queryByRole('heading', { name:'可打印明细' })).not.toBeInTheDocument()
})

it('opens generated training note links through the frontend API proxy', async () => {
  const note = '/api/v1/projects/p/modeling/studies/s/candidates/c/trials/0/note/download'
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(`[下载训练笔记](${note})`)))
  render(<ProjectFileReader projectId="p" path="results/report.md" onClose={() => {}} />)
  expect(await screen.findByRole('link', { name:'下载训练笔记' })).toHaveAttribute('href', `/api/platform${note}`)
})
