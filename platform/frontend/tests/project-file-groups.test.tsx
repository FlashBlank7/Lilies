import {act, cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectSpace from '@/app/components/ProjectSpace'
import ProjectRunPanel from '@/app/components/ProjectRunPanel'
import {groupProjectFiles} from '@/app/components/project-files'

const {guide} = vi.hoisted(() => ({guide: {active: false, mark: vi.fn()}}))
vi.mock('@/lib/platform', () => ({api: vi.fn(), withFrontendToken: (path: string) => path}))
vi.mock('@/app/components/AuthBoundary', () => ({useAccount: () => ({id: 'employee'})}))
vi.mock('@/app/components/Onboarding', () => ({useOnboarding: () => guide}))
vi.mock('@/app/components/ProjectMaterials', () => ({default: () => <button>添加资料</button>}))
vi.mock('@/app/components/SharedMethods', () => ({default: () => null}))

const first = 'results/examples/12345678-aaaa-4000-8000-123456789aaa/report.md'
const second = 'results/examples/87654321-bbbb-4000-8000-123456789bbb/report.md'
const source = 'requirement-package/追加资料/input.csv'
const instructions = 'requirement-package/追加资料/使用说明.md'
const fixture = [
  {path: first, modified_at: '2026-10-01T03:00:00Z'},
  {path: 'solution/script.py'},
  {path: instructions},
  {path: second, modified_at: '2026-10-02T03:00:00Z'},
  {path: source},
]
const workflow = {id: 'w', name: '我的流程', revision: 1, description: '', purpose: 'business', allowed: true, node_count: 2, inputs: []}
const props = {projectId: 'p', onWorkflow: vi.fn(), onFile: vi.fn(), onTalk: vi.fn(), onChanged: vi.fn()}
let files = fixture
let workflows = [workflow]
beforeEach(() => {
  sessionStorage.clear(); guide.active = false; files = fixture; workflows = [workflow]
  vi.mocked(api).mockReset()
  vi.mocked(api).mockImplementation(async path => {
    if (path.endsWith('/space')) return {workflows, files, files_truncated: false} as never
    if (path.endsWith('/official-workflows')) return [{id: 'template', name: '公共模板', description: '可编辑模板'}] as never
    return [] as never
  })
})
afterEach(cleanup)

it('groups project files, distinguishes repeated reports and keeps selections across groups and refreshes', async () => {
  const talk = vi.fn(), open = vi.fn()
  const view = render(<ProjectSpace {...props} onTalk={talk} onFile={open} />)
  const raw = await screen.findByRole('region', {name: '原始资料'})
  expect(screen.getAllByRole('heading', {level: 4}).map(item => item.textContent)).toEqual(['原始资料（1）', '说明（1）', '运行结果（2）', '其他（1）'])
  const reports = within(screen.getByRole('region', {name: '运行结果'})).getAllByRole('checkbox')
  expect(reports[0]).toHaveAccessibleName(/report.md · 目录 examples \/ 87654321 · 更新/)
  expect(reports[1]).toHaveAccessibleName(/report.md · 目录 examples \/ 12345678 · 更新/)
  expect(reports[0]).toHaveAccessibleName(/2026\/10\/02/)
  expect(screen.getByText(second)).not.toBeVisible()
  fireEvent.click(within(raw).getByRole('checkbox', {name: 'input.csv'}))
  fireEvent.click(screen.getByRole('checkbox', {name: '使用说明.md'}))
  fireEvent.click(reports[0])
  fireEvent.click(screen.getByRole('button', {name: `查看 ${second}`}))
  expect(open).toHaveBeenCalledWith(second)
  files = [...fixture].reverse()
  fireEvent.click(screen.getByRole('button', {name: '刷新空间'}))
  await waitFor(() => expect(api).toHaveBeenCalledTimes(3))
  view.unmount()
  render(<ProjectSpace {...props} onTalk={talk} />)
  expect(await screen.findByRole('checkbox', {name: 'input.csv'})).toBeChecked()
  expect(screen.getByRole('checkbox', {name: '使用说明.md'})).toBeChecked()
  expect(screen.getByRole('checkbox', {name: /report.md · 目录 examples \/ 87654321/})).toBeChecked()
  fireEvent.click(screen.getByRole('button', {name: '带着所选资料开始对话'}))
  expect(talk).toHaveBeenCalledWith(expect.stringContaining(`- ${source}\n- ${instructions}\n- ${second}`))
})

it('keeps the public catalog after project files, compact for established projects and available to open', async () => {
  render(<ProjectSpace {...props} />)
  await screen.findByRole('checkbox', {name: 'input.csv'})
  expect(screen.queryByRole('button', {name: '加入项目 · 公共模板'})).not.toBeInTheDocument()
  const headings = screen.getAllByRole('heading', {level: 2}).map(item => item.textContent)
  expect(headings.indexOf('公共工作流市场')).toBeGreaterThan(headings.indexOf('待处理文件'))
  fireEvent.click(screen.getByRole('button', {name: '浏览公共流程并加入项目'}))
  expect(await screen.findByRole('button', {name: '加入项目 · 公共模板'})).toBeVisible()
})

it.each(['empty', 'onboarding'])('leaves an obvious install entry for an %s project', async mode => {
  if (mode === 'empty') workflows = []
  else guide.active = true
  render(<ProjectSpace {...props} />)
  expect(await screen.findByRole('button', {name: '加入项目 · 公共模板'})).toBeVisible()
})

it('keeps same-prefix directories distinct even without timestamps', () => {
  const groups = groupProjectFiles([{path: first}, {path: first.replace('789aaa', '789ccc')}])
  const labels = groups[0].files.map(file => file.label)
  expect(new Set(labels).size).toBe(2)
  expect(labels.every(label => label.includes('目录') && !label.includes('运行编号') && !label.includes('aaaa-4000'))).toBe(true)
})

it('groups both file inputs and table choices and submits the unchanged full paths', async () => {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/readiness')) return {status: 'configured', issues: [], note: ''} as never
    if (path.endsWith('/draft')) return {snapshot: {workflow: {nodes: [{type: 'start', config: {inputs: [
      {name: 'source_path', type: 'file', default: source},
      {name: 'sources', label: '材料', type: 'array', default: [{path: first, note: '保留说明'}], columns: [{name: 'path', label: '文件', type: 'file'}, {name: 'note', label: '说明'}]},
    ]}}]}}} as never
    if (path.endsWith('/workspace/files')) return fixture as never
    if (options?.method === 'POST') return {id: 'done', status: 'succeeded', outputs: {markdown: '处理完成'}} as never
    return [] as never
  })
  await act(async () => {render(<ProjectRunPanel projectId="p" members={[workflow]} initialWorkflowId="w" />)})
  const select = screen.getByRole('combobox', {name: '为 source_path 选择项目文件'})
  const table = screen.getByRole('combobox', {name: '材料第1行文件'})
  for (const field of [select, table]) {
    expect(within(field).getAllByRole('group').map(item => item.getAttribute('label'))).toEqual(['原始资料', '说明', '运行结果', '其他'])
    const reports = within(field).getAllByRole('option', {name: /^report.md/})
    expect(reports[0]).toHaveTextContent('目录 examples / 87654321')
    expect(reports[0]).not.toHaveTextContent(second)
    expect(reports[0]).toHaveValue(second)
    expect(reports[1]).toHaveValue(first)
  }
  fireEvent.change(select, {target: {value: second}})
  expect(screen.getByRole('textbox', {name: 'source_path'})).toHaveValue(second)
  fireEvent.change(table, {target: {value: instructions}})
  expect(screen.getByRole('textbox', {name: '材料第1行说明'})).toHaveValue('保留说明')
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options?.method)).toBe(true)
  fireEvent.click(screen.getByRole('button', {name: '启动工作流'}))
  await screen.findByText('处理完成')
  const submitted = vi.mocked(api).mock.calls.find(([, options]) => options?.method === 'POST')!
  expect(JSON.parse(submitted[1]!.body as string).inputs).toEqual({source_path: second, sources: [{path: instructions, note: '保留说明'}]})
})
