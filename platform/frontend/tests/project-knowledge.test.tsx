import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react'
import {afterEach, expect, it, vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectKnowledge from '@/app/components/ProjectKnowledge'
import {WorkflowValueField} from '@/app/components/WorkflowValueField'
vi.mock('@/lib/platform', () => ({api: vi.fn()}))
afterEach(() => {cleanup(); vi.mocked(api).mockReset()})
const knowledge = {knowledge_ref:'manual', name:'手册', revision:2, chunk_size:600, chunk_overlap:80, document_prefix:'', query_prefix:'', status:'pending', active_version:'', documents:[], chunk_count:0}
it.each(['search','answer'])('creates an editable %s workflow before a connection or index exists', async (mode) => {
  const definition = {nodes:[{id:'start',type:'start'},{id:'search',type:'knowledge_search',config:{knowledge_ref:'manual'}},{id:'end',type:'end'}],edges:[]}
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/knowledge')) return [knowledge] as never
    if (path.endsWith('/workflow-definition')) return definition as never
    if (path.endsWith('/members')) return {id:'workflow'} as never
    if (path.endsWith('/draft') && !options) return {revision:0} as never
    return {} as never
  })
  const changed = vi.fn()
  render(<ProjectKnowledge projectId="p" canManage onWorkflow={changed} onFile={vi.fn()}/> )
  await screen.findByRole('option', {name:'手册 · 待建立索引'})
  fireEvent.change(screen.getByLabelText('选择知识库'), {target:{value:'manual'}})
  fireEvent.click(screen.getByRole('button', {name: mode === 'search' ? '创建检索工作流' : '创建引用问答工作流'}))
  await waitFor(() => expect(changed).toHaveBeenCalledWith('workflow'))
  const save = vi.mocked(api).mock.calls.find(([p,o]) => p.endsWith('/workflows/workflow/draft') && o?.method === 'PUT')!
  expect(JSON.parse(save[1]!.body as string)).toEqual({expected_revision:0, workflow:definition})
  const definitionCall = vi.mocked(api).mock.calls.find(([p]) => p.endsWith('/workflow-definition'))!
  expect(JSON.parse(definitionCall[1]!.body as string)).toMatchObject({mode,top_k:5,minimum_score:0.3})
  expect(vi.mocked(api).mock.calls.some(([p]) => /\/(tasks|build|search)$/.test(p))).toBe(false)
})
it('shows returned source text and location, and keeps failure visible', async () => {
  vi.mocked(api).mockImplementation(async path => {
    if (path.endsWith('/knowledge')) return [{...knowledge, status:'ready'}] as never
    if (path.endsWith('/search')) return {version:'index-v1', retrieved_count:1, results:[{citation:'[1]', title:'维护说明', text:'断电后检查电缆。', source_path:'requirement-package/manual.pdf', score:0.85, location:{page:3,start:0,end:8}}]} as never
    return {} as never
  })
  const file = vi.fn()
  render(<ProjectKnowledge projectId="p" canManage={false} onWorkflow={vi.fn()} onFile={file}/> )
  await screen.findByRole('option', {name:'手册 · 可用'})
  fireEvent.change(screen.getByLabelText('选择知识库'), {target:{value:'manual'}})
  expect(screen.queryByLabelText('Embedding API Key')).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('检索问题'), {target:{value:'设备如何维护'}})
  fireEvent.click(screen.getByRole('button', {name:'检索'}))
  await screen.findByText('断电后检查电缆。')
  expect(screen.getByText(/第 3 页/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', {name:'查看原资料'}))
  expect(file).toHaveBeenCalledWith('requirement-package/manual.pdf')
  await waitFor(() => expect(screen.getByRole('button', {name:'检索'})).toBeEnabled())
  vi.mocked(api).mockRejectedValue(new Error('索引需要重建'))
  fireEvent.click(screen.getByRole('button', {name:'检索'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('索引需要重建')
})
it('selects a pending project knowledge resource without JSON', async () => {
  vi.mocked(api).mockResolvedValue([knowledge] as never)
  const change=vi.fn()
  render(<WorkflowValueField projectId="p" field="knowledge_ref" nodeId="search" nodes={[]} label="知识库" value="" onChange={change}/>)
  await screen.findByRole('option', {name:'手册 · 待建立索引'})
  fireEvent.change(screen.getByLabelText('知识库',{selector:'select'}), {target:{value:'manual'}})
  expect(change).toHaveBeenCalledWith('manual')
  expect(screen.getByRole('option', {name:'稍后配置'})).toBeInTheDocument()
})

it('renders a saved retrieval run as readable sources and downloads its exact result', async () => {
  const {ProjectTaskOutput} = await import('@/app/components/ProjectRunPanel')
  const result = {knowledge_ref:'manual',version:'fixed-v1',retrieved_count:1,results:[{citation:'[1]',title:'Source',text:'Original saved content',score:0.9,source_path:'',location:{page:2,start:0,end:22}}]}
  render(<ProjectTaskOutput projectId="p" task={{id:'t',status:'succeeded',mode:'workflow',outputs:{result}} as never}/> )
  expect(screen.getByText('Original saved content')).toBeInTheDocument()
  expect(screen.getByText(/第 2 页/)).toBeInTheDocument()
  const link=screen.getByRole('link',{name:'下载检索结果与出处 JSON ↓'})
  expect(JSON.parse(decodeURIComponent(link.getAttribute('href')!.split(',')[1]))).toEqual(result)
  expect(link).toHaveAttribute('download','knowledge-fixed-v1.json')
})

it('presents a cited answer with its original sources, flags unknown references and exports both', async () => {
  const {ProjectTaskOutput} = await import('@/app/components/ProjectRunPanel')
  const knowledge = {knowledge_ref:'manual',version:'index-v1',retrieved_count:1,results:[{citation:'[1]',title:'Care',text:'Do not leave batteries in heat.',score:0.9,source_path:'',location:{page:3,start:0,end:30}}]}
  const answer = '避免高温存放。[1] 其他说法。[9]'
  render(<ProjectTaskOutput projectId="p" task={{id:'t',status:'succeeded',mode:'workflow',outputs:{question:'如何保养？',markdown:answer,knowledge}} as never}/> )
  expect(screen.getByText(answer)).toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('引用 [9] 没有对应')
  expect(screen.getByText('Do not leave batteries in heat.')).toBeInTheDocument()
  const json = screen.getByRole('link',{name:'下载回答与出处 JSON ↓'})
  expect(JSON.parse(decodeURIComponent(json.getAttribute('href')!.split(',')[1]))).toEqual({question:'如何保养？',answer,knowledge})
  const markdown = decodeURIComponent(screen.getByRole('link',{name:'下载回答与出处 Markdown ↓'}).getAttribute('href')!.split(',')[1])
  expect(markdown).toContain(answer)
  expect(markdown).toContain('第 3 页')
  expect(markdown).toContain('Do not leave batteries in heat.')
})
