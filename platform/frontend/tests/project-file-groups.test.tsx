import {act, cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectSpace from '@/app/components/ProjectSpace'
import ProjectRunPanel from '@/app/components/ProjectRunPanel'
import ProjectFileField from '@/app/components/ProjectFileField'
import WorkflowInputTable from '@/app/components/WorkflowInputTable'
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

it('shows the same related workflow, second and input summaries in file lists and choices', async () => {
  const paths = [first, second].map(path => path.replace('report.md', 'samples.csv'))
  const available = paths.map((path, index) => ({path, related_run: {
    run_id: `run-${index}`, task_id: `task-${index}`, workflow_id: 'w', workflow_name: '样本清洗',
    created_at: `2026-10-03T03:00:0${index + 1}Z`,
    file_parameters: [{name: 'source_path', label: '输入表格', value: 'new-samples.csv'}],
    input_parameters: [
      {name: 'purpose', label: '用途', value: '预测'},
      {name: 'duplicate_policy', label: '重复记录', value: '保留'},
      {name: 'invalid_policy', label: '转换失败处理', value: index ? '排除' : '保留'},
      {name: 'id_columns', label: '标识列', value: 'internal-detail'},
    ],
  }}))
  files = available
  render(<ProjectSpace {...props} />)
  const choices = await screen.findAllByRole('checkbox', {name: /samples.csv · 关联运行：样本清洗/})
  const choose = vi.fn()
  render(<ProjectFileField name="source_path" label="待处理文件" value="" files={available} disabled={false} onChange={choose} />)
  render(<WorkflowInputTable name="sources" label="材料" columns={[{name: 'path', label: '文件', type: 'file'}]} value={'[{"path":""}]'} files={available} onChange={choose} />)
  const select = screen.getByRole('combobox', {name: '为 source_path 选择项目文件'})
  const table = screen.getByRole('combobox', {name: '材料第1行文件'})
  for (const [index, path] of paths.entries()) {
    const option = within(select).getAllByRole('option').find(item => (item as HTMLOptionElement).value === path)!
    expect(option).toHaveTextContent('关联运行：样本清洗')
    expect(option).toHaveTextContent('输入表格：new-samples.csv')
    expect(option).toHaveTextContent(`转换失败处理：${index ? '排除' : '保留'}`)
    expect(option).toHaveTextContent(new RegExp(`:0${index + 1} ·`))
    expect(option).not.toHaveTextContent('internal-detail')
    expect(choices[index]).toHaveAccessibleName(option.textContent!)
    expect(within(table).getByRole('option', {name: option.textContent!})).toHaveValue(path)
  }
  fireEvent.change(select, {target: {value: paths[1]}})
  expect(choose).toHaveBeenCalledWith(paths[1])
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

it('uses grouped short filenames in conversation attachments while submitting their distinct full paths', async () => {
  const {default: ConversationWorkflowCreator} = await import('@/app/components/ConversationWorkflowCreator')
  const saved = vi.fn()
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/space')) return {files: fixture} as never
    if (options?.method === 'POST') return {workflow_id:'generated', draft:{revision:1}, previous_workflow:{}, workflow_card:{id:'generated',name:'新流程'}} as never
    return [] as never
  })
  render(<ConversationWorkflowCreator projectId="p" visible storageKey="conversation-files" message="使用所选结果制作流程" members={[{...workflow,display_name:'我的流程 · 共享副本'}]} context={null} onClearTarget={vi.fn()} onSaved={saved}/>)
  await waitFor(() => expect(screen.getByText('运行结果（2）')).toBeInTheDocument())
  fireEvent.click(screen.getByText('关联项目文件（已选 0）'))
  fireEvent.click(screen.getByText('参考已有工作流（已选 0）'))
  expect(screen.getByRole('checkbox',{name:'我的流程 · 共享副本'})).toBeInTheDocument()
  const reports = within(screen.getByRole('region',{name:'运行结果'})).getAllByRole('checkbox')
  expect(reports[0]).toHaveAccessibleName(/report.md · 目录 examples \/ 87654321/)
  expect(reports[1]).toHaveAccessibleName(/report.md · 目录 examples \/ 12345678/)
  expect(screen.queryByText(second)).not.toBeInTheDocument()
  fireEvent.click(reports[0]); fireEvent.click(reports[1])
  fireEvent.click(screen.getByRole('button',{name:'生成新工作流'}))
  await waitFor(() => expect(saved).toHaveBeenCalledWith('使用所选结果制作流程'))
  const submitted=vi.mocked(api).mock.calls.find(([,options])=>options?.method==='POST')!
  expect(JSON.parse(submitted[1]!.body as string).file_paths).toEqual([second,first])
})
