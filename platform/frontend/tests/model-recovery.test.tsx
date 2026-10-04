import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import ProjectConversation from '@/app/components/ProjectConversation'
import ProjectRunPanel, {ProjectTaskOutput} from '@/app/components/ProjectRunPanel'
import ModelConnectionPanel from '@/app/components/ModelConnectionPanel'
import {api} from '@/lib/platform'

vi.mock('@/lib/platform',()=>({api:vi.fn(),withFrontendToken:(v:string)=>v}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>({id:'employee',role:'member'})}))
vi.mock('@/app/components/Onboarding',()=>({useOnboarding:()=>({mark:vi.fn()})}))
vi.mock('@/app/components/ModelingPanel',()=>({default:()=>null}))
vi.mock('@/app/components/ConversationWorkflowCreator',()=>({default:()=>null}))
beforeEach(()=>{vi.mocked(api).mockReset();sessionStorage.clear()})
afterEach(cleanup)

it('explains a disabled send and lets the owner configure a model without losing the message or sending it',async()=>{
 let configured=false
 vi.mocked(api).mockImplementation(async(path,options)=>{
  if(path.endsWith('/assistant-source'))return {task:'api',allowed:false} as never
  if(path.endsWith('/agent-session')){
   if(options?.method==='PUT')configured=true
   return {provider:configured?'api':null,model_egress_enabled:true} as never
  }
  return {provider:configured?'api':null,status:'idle',revision:configured?2:1,events:[],error:'',has_more:false,requirements:{}} as never
 })
 render(<ProjectConversation id="p" conversationId="chat" items={[]} onUpdated={vi.fn()} onSent={vi.fn()}/> )
 await screen.findByText(/此项目尚未连接模型，暂时不能发送/)
 fireEvent.change(screen.getByLabelText('给项目统筹的消息'),{target:{value:'请整理本次会议，不要丢失这条请求'}})
 expect(screen.getByRole('button',{name:'发送'})).toBeDisabled()
 fireEvent.click(screen.getByRole('button',{name:'连接模型'}))
 await screen.findByLabelText('API Key')
 fireEvent.change(screen.getByLabelText('API 地址'),{target:{value:'https://model.example.test/v1'}})
 fireEvent.change(screen.getByLabelText('API Key'),{target:{value:'offline-test-key'}})
 fireEvent.change(screen.getByLabelText('模型'),{target:{value:'offline-model'}})
 fireEvent.click(screen.getByRole('button',{name:'保存模型连接'}))
 await waitFor(()=>expect(screen.getByRole('button',{name:'发送'})).toBeEnabled())
 expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('请整理本次会议，不要丢失这条请求')
 expect(vi.mocked(api).mock.calls.some(([path])=>path.endsWith('/messages'))).toBe(false)
})

it('tells collaborators whom to contact instead of offering a configuration they cannot save',async()=>{
 vi.mocked(api).mockResolvedValue({provider:null,status:'idle',revision:1,events:[],requirements:{}} as never)
 render(<ProjectConversation id="p" items={[]} canConfigureModel={false} onUpdated={vi.fn()} onSent={vi.fn()}/> )
 expect(await screen.findByText(/请联系项目负责人配置模型连接/)).toBeVisible()
 expect(screen.queryByRole('button',{name:'连接模型'})).not.toBeInTheDocument()
})

it('shows the deployment restriction separately while still allowing a connection to be saved',async()=>{
 vi.mocked(api).mockResolvedValue({provider:null,model_egress_enabled:false} as never)
 render(<ModelConnectionPanel base="/api/v1/projects/p" connected={false} running={false} onSaved={vi.fn().mockResolvedValue(undefined)}/> )
 fireEvent.click(screen.getByRole('button',{name:'连接模型'}))
 expect(await screen.findByText(/平台尚未允许外部模型 API 调用/)).toBeVisible()
 expect(screen.getByRole('button',{name:'保存模型连接'})).toBeEnabled()
 expect(vi.mocked(api).mock.calls.every(([,options])=>!options?.method)).toBe(true)
})

it('opens model configuration at the failed result and rechecks it without retrying the failed run',async()=>{
 let configured=false
 vi.mocked(api).mockImplementation(async(path,options)=>{
  if(path.endsWith('/readiness'))return {status:configured?'configured':'needs_setup',note:'仅检查资源',issues:configured?[]:[{code:'model:main',message:'工作流模型尚未配置',setup:'settings'}]} as never
  if(path.endsWith('/agent-session')){
   if(options?.method==='PUT')configured=true
   return {provider:null,model_egress_enabled:true} as never
  }
  throw Error(path)
 })
 const task={id:'failed',workflow_id:'w',status:'failed',error:'本项目尚未启用工作流模型调用',inputs:{source_path:'original.csv'},outputs:{}}
 render(<ProjectTaskOutput projectId="p" task={task as never} canConfigureModel/> )
 const recovery=await screen.findByRole('region',{name:'修复运行配置'})
 fireEvent.click(await within(recovery).findByRole('button',{name:'模型设置'}))
 await screen.findByLabelText('API Key')
 fireEvent.change(screen.getByLabelText('API 地址'),{target:{value:'http://127.0.0.1:9001/v1'}})
 fireEvent.change(screen.getByLabelText('API Key'),{target:{value:'offline-test'}})
 fireEvent.change(screen.getByLabelText('模型'),{target:{value:'offline-model'}})
 fireEvent.click(screen.getByRole('button',{name:'保存模型连接'}))
 expect(await screen.findByText(/配置已保存。请确认运行准备后重新运行/)).toBeVisible()
 expect(within(recovery).getByText('基础资源检查通过')).toBeVisible()
 expect(screen.getByRole('alert')).toHaveTextContent('本项目尚未启用工作流模型调用')
 expect(task.inputs.source_path).toBe('original.csv')
 expect(vi.mocked(api).mock.calls.some(([path])=>path.endsWith('/tasks')||path.endsWith('/messages'))).toBe(false)
})

it('refreshes both readiness panels after repairing a failed manual run without resetting edited inputs or running again',async()=>{
 let configured=false
 vi.mocked(api).mockImplementation(async(path,options)=>{
  if(path.endsWith('/readiness'))return {status:configured?'configured':'needs_setup',note:'仅检查资源',issues:configured?[]:[{code:'model:main',message:'工作流模型尚未配置',setup:'settings'}]} as never
  if(path.endsWith('/draft'))return {snapshot:{workflow:{nodes:[{type:'start',config:{inputs:[{name:'note',type:'string',default:'默认要求'}]}}]}}} as never
  if(path.endsWith('/workspace/files'))return [] as never
  if(path.endsWith('/tasks')&&options?.method==='POST')return {id:'failed',workflow_id:'w',status:'failed',error:'本项目尚未启用工作流模型调用',inputs:JSON.parse(options.body as string).inputs,outputs:{}} as never
  if(path.endsWith('/agent-session')){
   if(options?.method==='PUT')configured=true
   return {provider:configured?'api':null,model_egress_enabled:false} as never
  }
  throw Error(path)
 })
 render(<ProjectRunPanel projectId="p" members={[{id:'w',name:'整理资料',description:'',revision:1,purpose:'business'}]} initialWorkflowId="w" canConfigureModel/> )
 fireEvent.change(await screen.findByRole('textbox',{name:'note'}),{target:{value:'本次提交的要求'}})
 expect(within(screen.getByRole('region',{name:'运行准备'})).getByText('工作流模型尚未配置')).toBeVisible()
 fireEvent.click(screen.getByRole('button',{name:'启动工作流'}))
 const recovery=await screen.findByRole('region',{name:'修复运行配置'})
 fireEvent.change(screen.getByRole('textbox',{name:'note'}),{target:{value:'修复期间补充的要求'}})
 fireEvent.click(await within(recovery).findByRole('button',{name:'模型设置'}))
 await screen.findByLabelText('API Key')
 fireEvent.change(screen.getByLabelText('API 地址'),{target:{value:'http://127.0.0.1:9001/v1'}})
 fireEvent.change(screen.getByLabelText('API Key'),{target:{value:'offline-test'}})
 fireEvent.change(screen.getByLabelText('模型'),{target:{value:'offline-model'}})
 fireEvent.click(screen.getByRole('button',{name:'保存模型连接'}))
 await waitFor(()=>{
  const readiness=screen.getAllByRole('region',{name:'运行准备'})
  expect(readiness).toHaveLength(2)
  for(const region of readiness)expect(within(region).getByText('基础资源检查通过')).toBeVisible()
 })
 expect(within(recovery).getByText('基础资源检查通过')).toBeVisible()
 expect(screen.getByRole('textbox',{name:'note'})).toHaveValue('修复期间补充的要求')
 expect(screen.getByRole('button',{name:'启动工作流'})).toBeEnabled()
 expect(screen.getByRole('alert')).toHaveTextContent('本项目尚未启用工作流模型调用')
 const writes=vi.mocked(api).mock.calls.filter(([,options])=>options?.method)
 expect(writes.map(([path,options])=>[path,options!.method])).toEqual([
  ['/api/v1/projects/p/tasks','POST'],['/api/v1/projects/p/agent-session','PUT'],
 ])
 expect(JSON.parse(writes[0][1]!.body as string).inputs).toEqual({note:'本次提交的要求'})
 expect(vi.mocked(api).mock.calls.filter(([path])=>path.endsWith('/draft'))).toHaveLength(1)
})
