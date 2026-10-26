import {useState} from 'react'
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectServiceConnections from '@/app/components/ProjectServiceConnections'
import WorkflowResultChecks from '@/app/components/WorkflowResultChecks'
import WorkflowRunRecovery from '@/app/components/WorkflowRunRecovery'
import {ProjectTaskOutput} from '@/app/components/ProjectRunPanel'
import {WorkflowValueField} from '@/app/components/WorkflowValueField'
import type {ProjectTask} from '@/lib/project-progress'
vi.mock('@/lib/platform',()=>({api:vi.fn(),withFrontendToken:(path:string)=>path}))
afterEach(()=>{cleanup();vi.mocked(api).mockReset()})
const connection={id:'c',name:'NAS',base_url:'http://localhost:1234',allowed_paths:['/items/**'],allowed_methods:['GET','POST'],auth_header:'X-Api-Key',has_credential:true,enabled:true,idempotency_header:'Idempotency-Key',revision:1}

it('edits a connection without fetching or resubmitting the old credential',async()=>{
  vi.mocked(api).mockResolvedValue([connection] as never)
  render(<ProjectServiceConnections projectId="p" canManage/>);await screen.findByText('NAS · 已启用')
  fireEvent.click(screen.getByRole('button',{name:'编辑连接'}))
  expect(screen.getByLabelText('凭据值')).toHaveValue('')
  fireEvent.change(screen.getByLabelText('连接名称'),{target:{value:'媒体库'}})
  fireEvent.click(screen.getByRole('button',{name:'保存连接'}))
  await waitFor(()=>expect(api).toHaveBeenCalledWith('/api/v1/projects/p/http-connections/c',expect.objectContaining({method:'PUT'})))
  const body=JSON.parse(vi.mocked(api).mock.calls.find(c=>c[1]?.method==='PUT')![1]!.body as string)
  expect(body).toMatchObject({name:'媒体库',credential:null,expected_revision:1,allowed_paths:['/items/**']})
})

it('keeps connection configuration read-only for collaborators and offers connections to HTTP nodes',async()=>{
  vi.mocked(api).mockResolvedValue([connection] as never)
  render(<ProjectServiceConnections projectId="p" canManage={false}/>);await screen.findByText('NAS · 已启用')
  expect(screen.queryByRole('button',{name:'编辑连接'})).toBeNull()
  expect(screen.queryByRole('button',{name:'添加服务连接'})).toBeNull()
  render(<WorkflowValueField projectId="p" field="connection_id" label="连接" nodes={[]} nodeId="h" value="" onChange={vi.fn()} allowReference={false}/>)
  await screen.findByRole('option',{name:'NAS'})
  expect(api).toHaveBeenCalledWith('/api/v1/projects/p/http-connections')
})

it('configures named checks with an upstream value, typed expectation and failure reason',()=>{
  let saved:unknown
  function Editor(){const[value,setValue]=useState('[]');return <WorkflowResultChecks nodes={[{id:'read',type:'http_request',title:'回读',config:{}},{id:'end',type:'end',title:'结束',config:{}}]} edges={[{source:'read',target:'end'}]} nodeId="end" label="结果检查" value={value} onChange={v=>{saved=JSON.parse(v);setValue(v)}}/>}
  render(<Editor/>);fireEvent.click(screen.getByRole('button',{name:'添加结果检查'}))
  fireEvent.change(screen.getByLabelText('检查名称 1'),{target:{value:'取消监控'}})
  fireEvent.change(screen.getByLabelText('检查 1实际值的来源'),{target:{value:'reference'}})
  fireEvent.change(screen.getByLabelText('检查 1实际值的字段路径'),{target:{value:'output.monitored'}})
  fireEvent.change(screen.getByLabelText('检查 1预期值'),{target:{value:'false'}})
  fireEvent.change(screen.getByLabelText('检查 1失败说明'),{target:{value:'仍被监控'}})
  expect(saved).toEqual([{name:'取消监控',value:{$ref:{node_id:'read',path:['output','monitored']}},operator:'equals',expected:false,failure_message:'仍被监控'}])
})

it('distinguishes resuming a checkpoint and creating a repair task, blocking duplicate clicks',async()=>{
  let resolve!:(value:unknown)=>void
  vi.mocked(api).mockImplementation(()=>new Promise(r=>{resolve=r}))
  const task={id:'old',workflow_id:'w',mode:'workflow',status:'failed',inputs:{target:'movie-10'},purpose:'business'} as ProjectTask
  const onTask=vi.fn()
  const view=render(<WorkflowRunRecovery projectId="p" task={task} onTask={onTask}/>)
  fireEvent.click(screen.getByRole('button',{name:'继续原任务'}));fireEvent.click(screen.getByRole('button',{name:'继续原任务'}))
  expect(api).toHaveBeenCalledTimes(1)
  expect(api).toHaveBeenCalledWith('/api/v1/projects/p/tasks/old/resume',expect.anything())
  resolve({...task,status:'running'});await waitFor(()=>expect(onTask).toHaveBeenCalled())
  view.unmount();render(<WorkflowRunRecovery projectId="p" task={task} onTask={onTask}/>)
  fireEvent.click(screen.getByRole('button',{name:'修改流程后重新运行'}))
  const [path,options]=vi.mocked(api).mock.lastCall!
  expect(path).toBe('/api/v1/projects/p/tasks')
  expect(JSON.parse(options!.body as string)).toMatchObject({recovery_task_id:'old',workflow_id:'w',inputs:{target:'movie-10'}})
})


it('renders one recovery action after repeated task polling updates',async()=>{
  vi.mocked(api).mockResolvedValue({status:'configured',issues:[]} as never)
  const task={id:'old',workflow_id:'w',mode:'workflow',status:'running',inputs:{},outputs:{},runs:[]} as unknown as ProjectTask
  const view=render(<ProjectTaskOutput projectId="p" task={task}/>)
  for(const status of ['failed','running','failed','failed']){
    view.rerender(<ProjectTaskOutput projectId="p" task={{...task,status} as ProjectTask}/>)
  }
  expect(screen.getAllByRole('button',{name:'继续原任务'})).toHaveLength(1)
  expect(screen.getAllByRole('button',{name:'修改流程后重新运行'})).toHaveLength(1)
})
