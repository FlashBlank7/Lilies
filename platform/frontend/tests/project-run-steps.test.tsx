import {act,cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectRunSteps,{type RunStep} from '@/app/components/ProjectRunSteps'
import {ProjectTaskOutput} from '@/app/components/ProjectRunPanel'

vi.mock('@/lib/platform',()=>({api:vi.fn(),withFrontendToken:(v:string)=>v}))
afterEach(()=>{cleanup();vi.useRealTimers()})
beforeEach(()=>{vi.mocked(api).mockReset()})
const run={id:'r',application_id:'w',status:'succeeded',draft_revision:3}
const step:RunStep={id:'read',node_path:['read'],title:'读取费用表',description:'按月份和币种整理费用',type:'code',status:'completed',input_source:'按本次记录还原',input_preview:{source_path:'requirement-package/a/expenses.csv'},output_preview:{rows:4,summary:[{month:'2026-10',amount:'36.50'}]},duration_ms:120}
const page={run_id:'r',application_id:'w',name:'费用整理',status:'succeeded',steps:[step],total:1,next_offset:null,draft_revision:3}

it('shows saved business steps by default in the actual result component and opens readable input/output',async()=>{
  vi.mocked(api).mockResolvedValue(page)
  render(<ProjectTaskOutput projectId="p" task={{id:'t',status:'succeeded',outputs:{markdown:'费用报告'},runs:[run]} as never}/> )
  const steps=await screen.findByRole('region',{name:'处理步骤'})
  await within(steps).findByText('读取费用表')
  expect(within(steps).getByText('已完成')).toBeInTheDocument()
  expect(within(steps).getByText('用时 0.12 秒')).toBeInTheDocument()
  fireEvent.click(within(steps).getByRole('button',{name:'查看本步输入与产物'}))
  expect(screen.getByText('按本次记录还原')).toBeInTheDocument()
  expect(screen.getByRole('link',{name:'expenses.csv ↗'})).toHaveAttribute('href','/api/platform/api/v1/applications/p/workspace/files/requirement-package/a/expenses.csv')
  expect(screen.getByText('36.50')).toBeInTheDocument()
  expect(steps.querySelector('pre')).toBeNull()
  const href=screen.getByRole('link',{name:'在当前画布定位 ↗'}).getAttribute('href')!
  expect(JSON.parse(new URL(href,'http://localhost').searchParams.get('node_path')!)).toEqual(['read'])
  expect(api).toHaveBeenCalledWith('/api/v1/runs/r/steps?offset=0&limit=100')
})

it('keeps loop occurrences separate, opens failures and waiting input, and distinguishes skipped work',async()=>{
  vi.mocked(api).mockResolvedValue({...page,steps:[
    {...step,id:'loop[0].read',node_path:['loop','read'],scope:'逐批处理 · 第 1 轮'},
    {...step,id:'loop[1].read',node_path:['loop','read'],scope:'逐批处理 · 第 2 轮',status:'failed',error:'缺少费用字段：金额',output_preview:null},
    {...step,id:'ask',title:'确认费用类别',status:'waiting',input_preview:{question:'差旅是否含税？'},output_preview:null},
    {...step,id:'unused',title:'其他类别处理',status:'skipped',output_preview:null},
  ],total:4})
  render(<ProjectRunSteps projectId="p" runs={[run]}/> )
  expect(await screen.findByText('逐批处理 · 第 2 轮')).toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('缺少费用字段：金额')
  expect(screen.getByText('差旅是否含税？')).toBeInTheDocument()
  expect(screen.getByText('此分支未执行')).toBeInTheDocument()
  expect(screen.getAllByText('读取费用表')).toHaveLength(2)
})

it('does not accept a late response from the previously selected run',async()=>{
  let resolve!:(v:unknown)=>void
  vi.mocked(api).mockImplementation(async path=>path.includes('/r/steps')?await new Promise(r=>{resolve=r}):{...page,run_id:'new',name:'新运行',steps:[{...step,title:'新资料'}]} as never)
  const {rerender}=render(<ProjectRunSteps projectId="p" runs={[run]}/> )
  await waitFor(()=>expect(api).toHaveBeenCalledTimes(1))
  rerender(<ProjectRunSteps projectId="p" runs={[{...run,id:'new'}]}/> )
  await screen.findByText('新资料')
  await act(async()=>resolve(page))
  expect(screen.queryByText('读取费用表')).not.toBeInTheDocument()
})

it('pages long runs without dropping late steps and retries read errors without launching work',async()=>{
  vi.mocked(api).mockRejectedValueOnce(Error('offline')).mockImplementation(async path=>({...page,total:101,next_offset:path.includes('offset=100')?null:100,steps:[{...step,title:path.includes('offset=100')?'末尾报告':'读取费用表'}]}))
  render(<ProjectRunSteps projectId="p" runs={[run]}/> )
  fireEvent.click(await screen.findByRole('button',{name:'重新读取步骤'}))
  fireEvent.click(await screen.findByRole('button',{name:'后100步'}))
  await screen.findByText('末尾报告')
  expect(screen.getByRole('button',{name:'后100步'})).toBeDisabled()
  fireEvent.click(screen.getByRole('button',{name:'前100步'}))
  await screen.findByText('读取费用表')
  expect(vi.mocked(api).mock.calls.every(call=>call.length===1)).toBe(true)
})

it('refreshes active steps and stops polling after completion',async()=>{
  vi.useFakeTimers()
  vi.mocked(api).mockResolvedValueOnce({...page,status:'running',steps:[{...step,status:'running'}]}).mockResolvedValue(page)
  await act(async()=>{render(<ProjectRunSteps projectId="p" runs={[{...run,status:'running'}]}/> )})
  expect(screen.getByText('正在处理')).toBeInTheDocument()
  await act(async()=>{await vi.advanceTimersByTimeAsync(2000)})
  expect(screen.getByText('已完成')).toBeInTheDocument()
  await act(async()=>{await vi.advanceTimersByTimeAsync(10000)})
  expect(api).toHaveBeenCalledTimes(2)
})
