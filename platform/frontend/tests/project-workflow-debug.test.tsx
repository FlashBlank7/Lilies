import {useState} from 'react'
import {act,cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectWorkflowDebug from '@/app/components/ProjectWorkflowDebug'
import StructuredValueEditor,{type Value} from '@/app/components/StructuredValueEditor'
vi.mock('@/lib/platform',()=>({api:vi.fn()}))
afterEach(()=>{cleanup();vi.mocked(api).mockReset()})
const flow={id:'w',name:'筛选目标',description:'选出待处理记录',revision:2,inputs:[{name:'rows',type:'array'}],outputs:['targets'],services:['inventory'],calls:[]}
const main={...flow,id:'p',name:'主流程',inputs:[],services:[],calls:[{node_id:'step_1',workflow_id:'w',inputs:{}}]}
const service={id:'s',revision:1,name:'库存模拟',implementation:'records',initial_state:{records:[]},scenarios:{故障:{failures:[]}}}
const testCase={id:'c',name:'正常样例',requirement:'',inputs:{rows:[{id:'A',height:null},{id:'B',height:3}]},debug:{bindings:{inventory:{service_id:'s',scenario:''}},source:{kind:'synthetic'}}}
const progress={revision:1,value:{goal:'清理已观看影片',summary:'',items:[],workflows:[]}}
function mock(overrides:Record<string,unknown>={}){
  vi.mocked(api).mockImplementation(async(path,init)=>{
    if(path in overrides)return overrides[path] as never
    if(path.endsWith('/debug/overview'))return {workflows:[main,flow],progress,tasks:[]} as never
    if(path.endsWith('/simulators'))return [service] as never
    if(path.endsWith('/http-connections'))return [] as never
    if(path.endsWith('/debug-cases'))return (init?.method==='PUT'?{case:testCase,revision:3}:{cases:[testCase],revision:2}) as never
    throw Error('unexpected '+path)
  })
}
function view(id='w'){return render(<ProjectWorkflowDebug projectId="p" selectedId={id} onSelect={vi.fn()} onEdit={vi.fn()} onChanged={vi.fn()} onTalk={vi.fn()}/>)}

it('edits rows and nulls through typed fields without changing other rows',()=>{
  let saved:Value=null
  function Form(){const[v,set]=useState<Value>([{id:'A',height:null},{id:'B',height:3}]);return <StructuredValueEditor label="rows" value={v} onChange={next=>{saved=next;set(next)}}/>}
  render(<Form/>);fireEvent.change(screen.getByLabelText('rows 第2行 height'),{target:{value:'4.2'}})
  expect(saved).toEqual([{id:'A',height:null},{id:'B',height:4.2}])
})

it('saves input and explicit binding before a single simulation request',async()=>{
  mock({'/api/v1/projects/p/debug/runs':{id:'t',workflow_id:'w',purpose:'simulation',status:'failed',error:'模拟故障',outputs:{},inputs:{},created_at:'',updated_at:''}})
  view();await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例'))
  fireEvent.click(screen.getByRole('button',{name:'运行此流程（模拟）'}));fireEvent.click(screen.getByRole('button',{name:'运行此流程（模拟）'}))
  await screen.findByText('模拟故障')
  const sends=vi.mocked(api).mock.calls.filter(([p])=>p.endsWith('/debug/runs'))
  expect(sends).toHaveLength(1)
  expect(JSON.parse(sends[0][1]!.body as string)).toMatchObject({workflow_id:'w',case_id:'c'})
  const saved=vi.mocked(api).mock.calls.find(([p,o])=>p.endsWith('/debug-cases')&&o?.method==='PUT')!
  expect(JSON.parse(saved[1]!.body as string)).toMatchObject({inputs:testCase.inputs,bindings:testCase.debug.bindings})
  expect(screen.getByRole('button',{name:'继续原任务'})).toBeInTheDocument()
  expect(screen.getByRole('button',{name:'修改后修复（保留原状态）'})).toBeInTheDocument()
  expect(screen.getByRole('button',{name:'重置并重跑'})).toBeInTheDocument()
  expect(vi.mocked(api).mock.calls.some(([p,o])=>p.endsWith('/tasks')&&o?.method==='POST')).toBe(false)
})

it('retains boundary fields while saving the project goal',async()=>{
  mock({'/api/v1/projects/p/progress':progress});view();await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例'))
  fireEvent.change(screen.getByLabelText('平台负责什么'),{target:{value:'编排、目标筛选、回读核对'}})
  fireEvent.click(screen.getByRole('button',{name:'保存目标与边界'}))
  await screen.findByText('目标与边界已保存')
  const sent=vi.mocked(api).mock.calls.find(([p])=>p.endsWith('/progress'))!
  expect(JSON.parse(sent[1]!.body as string)).toMatchObject({expected_revision:1,value:{platform_scope:'编排、目标筛选、回读核对',items:[]}})
})

it('shows a failed current run without an older successful report',async()=>{
  mock({'/api/v1/projects/p/debug/overview':{workflows:[main,flow],progress,tasks:[{id:'old',workflow_id:'w',status:'succeeded',message:'旧结果'}]},
    '/api/v1/projects/p/debug/runs/old':{id:'old',status:'succeeded',inputs:{},outputs:{report:'旧报告'},outdated:true},
    '/api/v1/projects/p/debug/runs':{id:'new',workflow_id:'w',status:'failed',error:'缺少必要动作',inputs:{},outputs:{},created_at:'',updated_at:''}})
  view();await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例'));fireEvent.click(screen.getByRole('button',{name:/旧结果 ·/}));await screen.findByText(/旧报告/)
  fireEvent.click(screen.getByRole('button',{name:'运行此流程（模拟）'}));await screen.findByText('缺少必要动作')
  expect(within(screen.getByLabelText('本次调试结果')).queryByText(/旧报告/)).toBeNull()
})

it('saves workflow composition with real upstream output selectors',async()=>{
  mock({'/api/v1/projects/p/debug/chain':{revision:3}});view();await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例'))
  fireEvent.click(screen.getByRole('button',{name:'串联验证'}));fireEvent.click(screen.getByRole('button',{name:'添加流程'}))
  fireEvent.change(screen.getByLabelText('第 2 步'),{target:{value:'w'}})
  const sources=screen.getAllByLabelText('rows · 来源');fireEvent.change(sources[1],{target:{value:'step_1'}})
  fireEvent.change(screen.getByLabelText('输出字段'),{target:{value:'targets'}})
  fireEvent.click(screen.getByRole('button',{name:'保存到主流程'}))
  await waitFor(()=>expect(api).toHaveBeenCalledWith('/api/v1/projects/p/debug/chain',expect.objectContaining({method:'PUT'})))
  const sent=vi.mocked(api).mock.calls.find(([p])=>p.endsWith('/debug/chain'))!
  expect(JSON.parse(sent[1]!.body as string).steps[1].inputs).toEqual({rows:{$ref:{node_id:'step_1',path:['output','targets']}}})
})


it('selects the newly saved upstream output instead of a preexisting downstream sample',async()=>{
  const done={id:'old',workflow_id:'w',status:'succeeded',inputs:{},outputs:{targets:[{id:'fresh'}]}}
  const copied={...testCase,id:'copied',name:'来自上游',inputs:{rows:[{id:'fresh'}]},debug:{source:{kind:'run',task_id:'old'},bindings:{}}}
  let saved=false
  mock({'/api/v1/projects/p/debug/overview':{workflows:[main,flow],progress,tasks:[{...done,message:'上游运行'}]},'/api/v1/projects/p/debug/runs/old':done})
  const fallback=vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async(path,init)=>{
    if(path.endsWith('/members/p/debug-cases')){
      if(init?.method==='PUT'){saved=true;return {case:copied,revision:4} as never}
      return {cases:saved?[testCase,copied]:[testCase],revision:3} as never
    }
    return fallback(path,init)
  })
  function Form(){const[id,set]=useState('w');return <ProjectWorkflowDebug projectId="p" selectedId={id} onSelect={set} onEdit={vi.fn()} onChanged={vi.fn()} onTalk={vi.fn()}/>}
  render(<Form/>);await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例'))
  fireEvent.click(screen.getByRole('button',{name:/上游运行 ·/}));await screen.findByLabelText('下游工作流')
  fireEvent.change(screen.getByLabelText('下游工作流'),{target:{value:'p'}})
  fireEvent.click(screen.getByRole('button',{name:'保存为下游样例'}))
  await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('来自上游'))
  expect(screen.getByLabelText('已保存样例')).toHaveValue('copied')
  expect(screen.getByLabelText('inventory模拟服务')).toHaveValue('')
})

it('remaps existing call IDs and never redirects a removed upstream to another step',async()=>{
  const ref=(id:string)=>({$ref:{node_id:id,path:['output','targets']}})
  mock({'/api/v1/projects/p/debug/overview':{workflows:[{...main,calls:[{node_id:'collect',workflow_id:'w',inputs:{}},{node_id:'filter',workflow_id:'w',inputs:{rows:ref('collect')}},{node_id:'check',workflow_id:'w',inputs:{rows:ref('filter')}}]},flow],progress,tasks:[]},'/api/v1/projects/p/debug/chain':{revision:4}})
  view();await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例'))
  fireEvent.click(screen.getByRole('button',{name:'串联验证'}))
  expect(screen.getAllByLabelText('rows · 来源')[2]).toHaveValue('step_2')
  fireEvent.click(screen.getAllByRole('button',{name:'移除此步'})[1])
  expect(screen.getAllByLabelText('rows · 来源')[1]).toHaveValue('missing_step_2')
  expect(screen.getByRole('option',{name:'来源已移除，请重新选择'})).toBeInTheDocument()
  fireEvent.change(screen.getAllByLabelText('rows · 来源')[1],{target:{value:'step_1'}})
  fireEvent.change(screen.getByLabelText('输出字段'),{target:{value:'targets'}})
  fireEvent.click(screen.getByRole('button',{name:'保存到主流程'}))
  await waitFor(()=>expect(api).toHaveBeenCalledWith('/api/v1/projects/p/debug/chain',expect.objectContaining({method:'PUT'})))
  const sent=vi.mocked(api).mock.calls.find(([p])=>p.endsWith('/debug/chain'))!
  expect(JSON.parse(sent[1]!.body as string).steps[1].inputs.rows).toEqual(ref('step_1'))
})

it('does not run before the selected workflow samples have loaded',async()=>{
  let finish!:(value:unknown)=>void
  const waiting=new Promise(resolve=>{finish=resolve})
  mock({'/api/v1/projects/p/members/w/debug-cases':waiting})
  view();await screen.findByText('筛选目标 · 独立调试')
  expect(screen.getByRole('button',{name:'运行此流程（模拟）'})).toBeDisabled()
  expect(screen.getByRole('button',{name:'保存样例'})).toBeDisabled()
  finish({revision:2,cases:[testCase]})
  await waitFor(()=>expect(screen.getByRole('button',{name:'运行此流程（模拟）'})).toBeEnabled())
  expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例')
})

it('loads samples once after the workflow is known and preserves the user selection',async()=>{
  let ready!:(value:unknown)=>void
  const overview=new Promise(resolve=>{ready=resolve})
  const second={...testCase,id:'second',name:'另一场景'}
  mock({'/api/v1/projects/p/debug/overview':overview,'/api/v1/projects/p/members/w/debug-cases':{revision:2,cases:[testCase,second]}})
  view()
  expect(screen.getByLabelText('已保存样例')).toBeDisabled()
  expect(vi.mocked(api).mock.calls.filter(([p])=>p.endsWith('/debug-cases'))).toHaveLength(0)
  ready({workflows:[main,flow],progress,tasks:[]})
  await waitFor(()=>expect(screen.getByLabelText('已保存样例')).toBeEnabled())
  fireEvent.change(screen.getByLabelText('已保存样例'),{target:{value:'second'}})
  expect(screen.getByLabelText('样例名称')).toHaveValue('另一场景')
  expect(vi.mocked(api).mock.calls.filter(([p])=>p.endsWith('/debug-cases'))).toHaveLength(1)
})

it('clears the previous success when a new run is rejected before task creation',async()=>{
  mock({'/api/v1/projects/p/debug/overview':{workflows:[main,flow],progress,tasks:[{id:'old',workflow_id:'w',status:'succeeded',message:'旧结果'}]},
    '/api/v1/projects/p/debug/runs/old':{id:'old',status:'succeeded',inputs:{},outputs:{report:'旧报告'}}})
  const normal=vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async(path,init)=>{if(path.endsWith('/debug/runs'))throw Error('缺少模拟服务绑定');return normal(path,init)})
  view();await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例'))
  fireEvent.click(screen.getByRole('button',{name:/旧结果 ·/}));await screen.findByText(/旧报告/)
  fireEvent.click(screen.getByRole('button',{name:'运行此流程（模拟）'}));await screen.findByRole('alert')
  expect(screen.queryByLabelText('本次调试结果')).not.toBeInTheDocument()
  expect(screen.queryByText(/旧报告/)).not.toBeInTheDocument()
})

it('refreshes the shown result version after saving a changed sample',async()=>{
  let changed=false
  mock({'/api/v1/projects/p/debug/overview':{workflows:[main,flow],progress,tasks:[{id:'old',workflow_id:'w',status:'succeeded',message:'旧结果'}]}})
  const normal=vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async(path,init)=>{
    if(path.endsWith('/debug/runs/old'))return {id:'old',status:'succeeded',inputs:{},outputs:{count:2},outdated:changed} as never
    if(path.endsWith('/debug-cases')&&init?.method==='PUT')changed=true
    return normal(path,init)
  })
  view();await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例'))
  fireEvent.click(screen.getByRole('button',{name:/旧结果 ·/}));await screen.findByLabelText('本次调试结果')
  fireEvent.change(screen.getByLabelText('样例名称'),{target:{value:'修改输入的样例'}})
  fireEvent.click(screen.getByRole('button',{name:'保存样例'}))
  await screen.findByText('此结果对应旧配置。历史结果已保留，当前修改需要重新调试。')
})

it('ignores an older history request after selecting a newer task',async()=>{
  let finish!:(value:unknown)=>void
  const older=new Promise(resolve=>{finish=resolve})
  mock({'/api/v1/projects/p/debug/overview':{workflows:[main,flow],progress,tasks:[{id:'old',workflow_id:'w',status:'succeeded',message:'旧结果'},{id:'new',workflow_id:'w',status:'succeeded',message:'新结果'}]},
    '/api/v1/projects/p/debug/runs/old':older,
    '/api/v1/projects/p/debug/runs/new':{id:'new',status:'succeeded',inputs:{},outputs:{markdown:'# 新报告'}}})
  view();await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例'))
  fireEvent.click(screen.getByRole('button',{name:/旧结果 ·/}))
  fireEvent.click(screen.getByRole('button',{name:/新结果 ·/}))
  await screen.findByRole('button',{name:'查看完整报告'})
  await act(async()=>finish({id:'old',status:'succeeded',inputs:{},outputs:{markdown:'# 旧报告'}}))
  expect(screen.queryByText('旧报告')).not.toBeInTheDocument()
  expect(screen.getByLabelText('本次调试结果')).toHaveTextContent('新报告')
})

it.each(['start','resume'])('keeps a newly selected result when a %s response arrives late',async(operation)=>{
  let finish!:(value:unknown)=>void
  const pendingResponse=new Promise(resolve=>{finish=resolve})
  const failed={id:'failed',status:'failed',workflow_id:'w',inputs:{},outputs:{},error:'临时错误'}
  mock({'/api/v1/projects/p/debug/overview':{workflows:[main,flow],progress,tasks:[{...failed,message:'失败任务'},{id:'history',workflow_id:'w',status:'succeeded',message:'选择的历史任务'}]},
    '/api/v1/projects/p/debug/runs/failed':failed,
    '/api/v1/projects/p/debug/runs/history':{id:'history',status:'succeeded',inputs:{},outputs:{markdown:'# 选择的报告'}},
    '/api/v1/projects/p/debug/runs':pendingResponse,
    '/api/v1/projects/p/debug/runs/failed/resume':pendingResponse})
  view();await waitFor(()=>expect(screen.getByLabelText('样例名称')).toHaveValue('正常样例'))
  if(operation==='resume'){
    fireEvent.click(screen.getByRole('button',{name:/失败任务 ·/}));await screen.findByText('临时错误')
    fireEvent.click(screen.getByRole('button',{name:'继续原任务'}))
  }else fireEvent.click(screen.getByRole('button',{name:'运行此流程（模拟）'}))
  await waitFor(()=>expect(api).toHaveBeenCalledWith(operation==='resume'?'/api/v1/projects/p/debug/runs/failed/resume':'/api/v1/projects/p/debug/runs',expect.objectContaining({method:'POST'})))
  fireEvent.click(screen.getByRole('button',{name:/选择的历史任务 ·/}));await screen.findByText('选择的报告',{selector:'h4'})
  await act(async()=>finish({id:'late',status:'succeeded',inputs:{},outputs:{markdown:'# 迟到的报告'}}))
  expect(screen.getByLabelText('本次调试结果')).toHaveTextContent('选择的报告')
  expect(screen.queryByText('迟到的报告')).not.toBeInTheDocument()
})
