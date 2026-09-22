import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectConversation from '@/app/components/ProjectConversation'
import ProjectSpace from '@/app/components/ProjectSpace'

const { mark } = vi.hoisted(() => ({ mark: vi.fn() }))
vi.mock('@/app/components/Onboarding', () => ({ useOnboarding: () => ({ mark, active: true }) }))
vi.mock('@/lib/platform',()=>({api:vi.fn(),withFrontendToken:(p:string)=>p}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>({id:'user'})}))
vi.mock('@/app/components/ModelingPanel',()=>({default:()=>null}))
vi.mock('@/app/components/ModelConnectionPanel',()=>({default:()=>null}))
vi.mock('@/app/components/ProjectMaterials',()=>({default:()=> <div>项目文件上传</div>}))
beforeEach(()=>{mark.mockClear();vi.mocked(api).mockReset();sessionStorage.clear()})
afterEach(()=>cleanup())
const member={id:'old',name:'质量分析',description:'分析质量数据',purpose:'business',revision:1}
const card={id:'new',name:'质量分析与预测',revision:1,node_count:2,nodes:[{id:'s',title:'输入',type:'start'},{id:'e',title:'输出',type:'end'}]}
function setup(){
  let generated=false
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path.endsWith('/space/official-workflows'))return [{id:'tabular-classification',name:'表格分类训练',description:'分析、特征和独立测试',version:1}] as never
    if(path.endsWith('/space/official-workflows/tabular-classification'))return {workflow_id:'installed'} as never
    if(path.endsWith('/space'))return {workflows:[{...member,node_count:2,allowed:true,inputs:[{name:'file'}]}],files:[{path:'requirement-package/data.csv',size:20}]} as never
    if(path.endsWith('/workflow-generation')){generated=true;return {workflow_id:'new',workflow_card:card,previous_workflow:{nodes:[],edges:[]},draft:{revision:1}} as never}
    return {provider:'api',status:'idle',revision:generated?2:1,has_more:false,first_cursor:'m',last_cursor:generated?'new-message':'m',events:generated?[{id:'new-message',kind:'assistant',text:'已保存',workflow:card}]:[{id:'m',kind:'assistant',text:'已经分析数据'}]} as never
  })
}
const props={id:'p',conversationId:'c',items:[],members:[member],onUpdated:vi.fn(),onSent:vi.fn(),onWorkflow:vi.fn()}

it('switches modes without losing the draft, references project resources and saves a new workflow without sending a task',async()=>{
  setup();render(<ProjectConversation {...props}/>);await screen.findByText('已经分析数据')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'),{target:{value:'把已有分析和预测组合成新流程'}})
  fireEvent.click(screen.getByRole('button',{name:'创建工作流'}))
  await screen.findByLabelText('requirement-package/data.csv')
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('把已有分析和预测组合成新流程')
  fireEvent.click(screen.getByRole('button',{name:'完成任务'}))
  expect(screen.getByRole('button',{name:'发送'})).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'创建工作流'}))
  fireEvent.click(screen.getByText('参考已有工作流（已选 0）'))
  fireEvent.click(screen.getByLabelText('质量分析'))
  fireEvent.click(screen.getByText('关联项目文件（已选 0）'))
  fireEvent.click(screen.getByLabelText('requirement-package/data.csv'))
  fireEvent.click(screen.getByRole('button',{name:'生成新工作流'}))
  const result=await screen.findByRole('article',{name:'已生成工作流'})
  expect(result).toHaveTextContent('质量分析与预测')
  const calls=vi.mocked(api).mock.calls.filter(([p])=>p.endsWith('/workflow-generation'))
  expect(calls).toHaveLength(1)
  expect(calls[0][0]).toBe('/api/v1/projects/p/conversations/c/workflow-generation')
  expect(JSON.parse(calls[0][1]!.body as string)).toMatchObject({workflow_id:'',reference_workflow_ids:['old'],file_paths:['requirement-package/data.csv']})
  expect(vi.mocked(api).mock.calls.some(([p,o])=>o?.method==='POST'&&(p.endsWith('/messages')||p.endsWith('/tasks')))).toBe(false)
  fireEvent.click(within(result).getByRole('button',{name:'通过智能体使用'}))
  expect((screen.getByLabelText('给项目统筹的消息') as HTMLTextAreaElement).value).toContain('质量分析与预测')
  expect(screen.getByRole('button',{name:'完成任务'})).toHaveAttribute('aria-pressed','true')
})

it('preserves typed input after a failed generation',async()=>{
  setup();render(<ProjectConversation {...props}/>);await screen.findByText('已经分析数据')
  const healthy=vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async(p,o)=>{if(p.endsWith('/workflow-generation'))throw new Error('连接不可用');return healthy(p,o)})
  fireEvent.click(screen.getByRole('button',{name:'创建工作流'}))
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'),{target:{value:'保留这段需求'}})
  fireEvent.click(screen.getByRole('button',{name:'生成新工作流'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('连接不可用')
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('保留这段需求')
  expect(screen.queryByRole('article',{name:'已生成工作流'})).not.toBeInTheDocument()
})

it('shows shared files and callable workflows together and carries selected materials into conversation',async()=>{
  setup();const talk=vi.fn();render(<ProjectSpace projectId="p" onWorkflow={vi.fn()} onFile={vi.fn()} onTalk={talk} onChanged={vi.fn()}/>)
  await screen.findByText('质量分析')
  fireEvent.click(screen.getByLabelText('requirement-package/data.csv'))
  fireEvent.click(screen.getByRole('button',{name:'让智能体调用'}))
  expect(talk).toHaveBeenCalledWith(expect.stringContaining('requirement-package/data.csv'))
  expect(talk.mock.calls[0][0]).toContain('old')
  fireEvent.click(screen.getByRole('button',{name:'通过对话创建工作流'}))
  expect(talk).toHaveBeenLastCalledWith(expect.stringContaining('requirement-package/data.csv'),'workflow')
  expect(vi.mocked(api).mock.calls.some(([,o])=>o?.method==='POST')).toBe(false)
})

it('installs an editable official workflow and opens its canvas without running it',async()=>{
  setup();const open=vi.fn();render(<ProjectSpace projectId="p" onWorkflow={open} onFile={vi.fn()} onTalk={vi.fn()} onChanged={vi.fn()}/>)
  fireEvent.click(await screen.findByRole('button',{name:'加入项目 · 表格分类训练'}))
  await waitFor(()=>expect(open).toHaveBeenCalledWith('installed'))
  expect(vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST').map(([path])=>path)).toEqual(['/api/v1/projects/p/space/official-workflows/tabular-classification'])
})


it('keeps an unsent draft when the project space supplies a workflow request', async () => {
  setup(); const view = render(<ProjectConversation {...props} />)
  await screen.findByText('已经分析数据')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), {target: {value: '保留我的额外要求'}})
  view.rerender(<ProjectConversation {...props} focus={{nonce: 1, label: '项目空间', message: '请调用质量分析流程'}} />)
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('保留我的额外要求\n\n请调用质量分析流程')
  expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  expect(mark).not.toHaveBeenCalledWith('conversation', 'p')
  fireEvent.click(screen.getByRole('button', {name: '发送'}))
  await waitFor(() => expect(mark).toHaveBeenCalledWith('conversation', 'p'))
})

it('marks actual file selection and workflow selection without running the workflow', async () => {
  setup(); render(<ProjectSpace projectId="p" onWorkflow={vi.fn()} onFile={vi.fn()} onTalk={vi.fn()} onChanged={vi.fn()} />)
  fireEvent.click(await screen.findByLabelText('requirement-package/data.csv'))
  expect(mark).toHaveBeenCalledWith('materials', 'p')
  fireEvent.click(screen.getByRole('button', {name: '让智能体调用'}))
  expect(mark).toHaveBeenCalledWith('workflow', 'p')
  expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
})

it('does not mark a failed task request as completed', async () => {
  setup(); const healthy = vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/messages')) throw new Error('模型未配置')
    return healthy(path, options)
  })
  render(<ProjectConversation {...props} />); await screen.findByText('已经分析数据')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), {target: {value: '请使用流程'}})
  fireEvent.click(screen.getByRole('button', {name: '发送'}))
  await screen.findByText('Error: 模型未配置')
  expect(mark).not.toHaveBeenCalledWith('conversation', 'p')
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('请使用流程')
})
