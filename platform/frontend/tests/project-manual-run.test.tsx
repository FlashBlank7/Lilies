import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import ProjectRunPanel, { ProjectRunEvents, ProjectTaskOutput } from '@/app/components/ProjectRunPanel'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (value: string) => value }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
beforeEach(() => { vi.mocked(api).mockReset() })
const members = [{ id: 'member', name: '解析资料', description: '', revision: 1, purpose: 'business' }]
const fields = [{ name: 'document', type: 'string', required: true }, { name: 'count', type: 'number', default: 2 }, { name: 'enabled', type: 'boolean', default: false }]

it('can stop a waiting question from the manual run page without submitting an answer',async()=>{
  let task={id:'t',status:'waiting_input',outputs:{},runs:[{id:'r',status:'paused',waiting_input:{node_id:'ask',title:'补充',context:'查看图片',fields:[{name:'label',label:'结论',type:'string'}]}}]}
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path.endsWith('/draft'))return {snapshot:{workflow:{nodes:[{type:'start',config:{inputs:[]}}]}}} as never
    if(path.endsWith('/workspace/files'))return [] as never
    if(path.endsWith('/stop')){task={...task,status:'interrupted'};return task as never}
    if(path.endsWith('/tasks/t')||options?.method==='POST')return task as never
    throw Error(path)
  })
  await act(async()=>{render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="member"/> )})
  fireEvent.click(screen.getByRole('button',{name:'启动工作流'}))
  await screen.findByRole('form',{name:'补充工作流信息'})
  fireEvent.click(screen.getByRole('button',{name:'停止运行'}))
  await waitFor(()=>expect(screen.queryByRole('form')).not.toBeInTheDocument())
  expect(vi.mocked(api).mock.calls.some(([path])=>path.endsWith('/input')||path.endsWith('/resume'))).toBe(false)
})

it('offers newly created files after completion without resetting edited inputs or starting another run', async () => {
  let finished=false
  vi.mocked(api).mockImplementation(async (path,options)=>{
    if(path.endsWith('/draft'))return {snapshot:{workflow:{nodes:[{type:'start',config:{inputs:[{name:'source_path',type:'file',default:'requirement-package/source.csv'},{name:'method_path',type:'file',default:''},{name:'note',type:'string',default:'原说明'}]}}]}}} as never
    if(path.endsWith('/workspace/files'))return [{path:'requirement-package/source.csv'},...(finished?[{path:'results/run/method.json'}]:[])] as never
    if(options?.method==='POST'){finished=true;return {id:'done',status:'succeeded',outputs:{markdown:'方法已保存'}} as never}
    throw new Error(path)
  })
  await act(async()=>{render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="member"/>)})
  fireEvent.change(screen.getByRole('textbox',{name:'note'}),{target:{value:'本次修改保留'}})
  fireEvent.click(screen.getByRole('button',{name:'启动工作流'}))
  await screen.findByText('方法已保存')
  await waitFor(()=>expect(screen.getByRole('combobox',{name:'为 method_path 选择项目文件'})).toHaveTextContent('results/run/method.json'))
  fireEvent.change(screen.getByRole('combobox',{name:'为 method_path 选择项目文件'}),{target:{value:'results/run/method.json'}})
  expect(screen.getByRole('textbox',{name:'method_path'})).toHaveValue('results/run/method.json')
  expect(screen.getByRole('textbox',{name:'note'})).toHaveValue('本次修改保留')
  expect(vi.mocked(api).mock.calls.filter(([path])=>path.endsWith('/draft'))).toHaveLength(1)
  expect(vi.mocked(api).mock.calls.filter(([,options])=>options?.method==='POST')).toHaveLength(1)
})

it('selects a declared workflow option without JSON or automatically starting a task', async () => {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft')) return {snapshot:{workflow:{nodes:[{type:'start',config:{inputs:[{name:'mode',label:'评价方式',type:'string',required:true,default:'数值预测',options:['数值预测','类别判断']}]}}]}}} as never
    if (path.endsWith('/workspace/files')) return [] as never
    if (options?.method==='POST') return {id:'t',status:'succeeded',outputs:{markdown:'已评价'}} as never
    throw new Error(path)
  })
  await act(async()=>{render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="member"/>)})
  const select=screen.getByRole('combobox',{name:'mode'})
  expect(select).toHaveValue('数值预测')
  fireEvent.change(select,{target:{value:'类别判断'}})
  expect(vi.mocked(api).mock.calls.some(([,options])=>options?.method==='POST')).toBe(false)
  fireEvent.click(screen.getByRole('button',{name:'启动工作流'}))
  await screen.findByText('已评价')
  const call=vi.mocked(api).mock.calls.find(([,options])=>options?.method==='POST')!
  expect(JSON.parse(call[1]!.body as string).inputs).toEqual({mode:'类别判断'})
})

it('shows workflow training, fixed-class test results and model download without JSON',()=>{
  render(<ProjectTaskOutput projectId="p" task={{status:'succeeded',mode:'workflow',outputs:{
    training:{id:'candidate',study_id:'study',trials:[{slot:0,model:'forest',status:'completed',metrics:{macro_f1:.5},baseline:{macro_f1:.2}}]},
    test:{rows:10,label:'保留测试',metrics:{macro_f1:.5,roc_auc:null},classification:{note:'固定类别',classes:[{label:'rare',samples:0,precision:0,recall:0,f1:0}]}}
  }} as never}/> )
  expect(screen.getByRole('heading',{name:'训练比较'})).toBeInTheDocument()
  expect(screen.getByText('0 · 缺少此类测试样本')).toBeInTheDocument()
  expect(screen.getByText(/roc_auc: 无法计算/)).toBeInTheDocument()
  expect(screen.getByRole('link',{name:'下载模型与训练记录 ↓'})).toHaveAttribute('href','/api/platform/api/v1/projects/p/modeling/studies/study/candidates/candidate/download')
})

it.each(['secure', 'http'])('runs and downloads through project tasks on %s origins without an agent', async origin => {
  if (origin === 'http') vi.stubGlobal('crypto', { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) })
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft')) return { snapshot: { workflow: { nodes: [{ type: 'start', config: { inputs: fields } }] } } } as never
    if (path.endsWith('/workspace/files')) return [{ path: 'requirement-package/design.pdf' }] as never
    if (options?.method === 'POST') return { id: 't', status: 'succeeded', inputs: {}, outputs: { markdown: '已完成解析', artifacts: [{ label: '报告', file_path: 'results/run/report.pdf' }] }, presentation: { artifacts: [] } } as never
    throw new Error(path)
  })
  await act(async () => { render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="member" />) })
  fireEvent.change(screen.getByRole('combobox', { name: '为 document 选择项目文件' }), { target: { value: 'requirement-package/design.pdf' } })
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  await screen.findByText('已完成解析')
  const call = vi.mocked(api).mock.calls.find(([, options]) => options?.method === 'POST')!
  expect(call[0]).toBe('/api/v1/projects/p/tasks')
  expect(JSON.parse(call[1]!.body as string)).toMatchObject({ mode: 'workflow', workflow_id: 'member', inputs: { document: 'requirement-package/design.pdf', count: 2, enabled: false }, purpose: 'customer_trial' })
  expect(screen.getByRole('link', { name: '报告 ↓' })).toHaveAttribute('href', '/api/platform/api/v1/applications/p/workspace/files/results/run/report.pdf?download=1')
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('agent-session/messages'))).toBe(false)
  const firstKey = JSON.parse(call[1]!.body as string).request_key
  expect(firstKey).toMatch(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/)
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  await waitFor(() => expect(vi.mocked(api).mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(2))
  const second = vi.mocked(api).mock.calls.filter(([, options]) => options?.method === 'POST')[1]
  expect(JSON.parse(second[1]!.body as string).request_key).not.toBe(firstKey)
})

it('rejects missing required input before starting a task', async () => {
  vi.mocked(api).mockImplementation(async path => path.endsWith('/draft') ? { snapshot: { workflow: { nodes: [{ type: 'start', config: { inputs: fields } }] } } } as never : [] as never)
  await act(async () => { render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="member" />) })
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('请填写 document'))
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options?.method)).toBe(true)
})

it.each(['', ' '])('sends cleared optional defaults (%j) so switching inputs cannot restore an old file or requirement version', async empty => {
  const defaults = [
    { name: 'previous_versions', type: 'array', default: ['results/old-requirements.json'] },
    { name: 'package_path', type: 'string', default: 'requirement-package/old.zip' },
    { name: 'document', type: 'file', default: 'requirement-package/old.pdf' },
    { name: 'count', type: 'number', default: 2 },
  ]
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft')) return { snapshot: { workflow: { nodes: [{ type: 'start', config: { inputs: defaults } }] } } } as never
    if (path.endsWith('/workspace/files')) return [] as never
    if (options?.method === 'POST') return { id: 't', status: 'succeeded', outputs: { markdown: '使用新资料完成' } } as never
    throw new Error(path)
  })
  await act(async () => { render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="member" />) })
  for (const name of ['previous_versions', 'package_path', 'document']) {
    fireEvent.change(screen.getByRole('textbox', { name }), { target: { value: name === 'previous_versions' ? empty : '' } })
  }
  fireEvent.click(screen.getByRole('button', { name: '启动工作流' }))
  await screen.findByText('使用新资料完成')
  const call = vi.mocked(api).mock.calls.find(([, options]) => options?.method === 'POST')!
  expect(JSON.parse(call[1]!.body as string).inputs).toEqual({ previous_versions: [], package_path: '', document: '', count: 2 })
})

it('loads persisted step input/output and errors from the selected run', async () => {
  vi.mocked(api).mockResolvedValue({ events: [{ id: 1, type: 'node_failed', data: { node_id: 'parse', inputs: { file: 'design.pdf' }, error: '文档无法解析' } }], truncated: false })
  render(<ProjectRunEvents members={members} runs={[{ id: 'run-1', status: 'failed', application_id: 'member', draft_revision: 2 }]} />)
  fireEvent.click(screen.getByRole('button', { name: '解析资料 · 运行失败 · 查看步骤输入输出' }))
  expect(await screen.findByText('node_failed')).toBeInTheDocument()
  expect(screen.getByText(/文档无法解析/)).toHaveTextContent('design.pdf')
  expect(api).toHaveBeenCalledWith('/api/v1/runs/run-1/events/list?after=0&limit=1000')
})

it('distinguishes threshold selection from independent test performance',()=>{
  render(<ProjectTaskOutput projectId="p" task={{status:'succeeded',outputs:{test:{rows:2,label:'保留测试',metrics:{accuracy:.5},acceptance:{selection:{status:'selected',threshold:.8,target_accuracy:.9,validation:{accepted:20,accuracy:.95,coverage:.5}},test:{accepted:1,review:1,accuracy:0,coverage:.5}}}}} as never}/>)
  expect(screen.getByRole('heading',{name:'自动采纳与人工复核'})).toBeInTheDocument()
  expect(screen.getByText(/这是选择依据，不是独立测试成绩/)).toBeInTheDocument()
  expect(screen.getByText(/采纳部分准确率 0.000/)).toBeInTheDocument()
})

it('recomputes the current draft using historical inputs and a scoped reuse task', async () => {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft')) return {snapshot:{workflow:{nodes:[{type:'start',config:{inputs:[{name:'source_path',type:'string',default:'new-default.csv'}]}}]}}} as never
    if (path.endsWith('/workspace/files')) return [] as never
    if (options?.method==='POST') return {id:'new',status:'succeeded',outputs:{markdown:'新报告'},runs:[{id:'r',reuse:{source_run_id:'old-run',nodes:['train']}}]} as never
    throw new Error(path)
  })
  await act(async () => {render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="member" reuseTask={{id:'old',workflow_id:'member',inputs:{source_path:'original.csv'}} as never}/>)})
  expect(screen.getByRole('textbox',{name:'source_path'})).toHaveValue('original.csv')
  fireEvent.click(screen.getByRole('button',{name:'启动工作流'}))
  expect(await screen.findByText(/复用 1 个已完成步骤/)).toHaveTextContent('train')
  const call=vi.mocked(api).mock.calls.find(([,options])=>options?.method==='POST')!
  expect(JSON.parse(call[1]!.body as string)).toMatchObject({reuse_task_id:'old',workflow_id:'member',inputs:{source_path:'original.csv'}})
})
