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

it('distinguishes same-named downloads using saved result sections and keeps their actual links',()=>{
  const task={id:'t',status:'succeeded',outputs:{
    profile:{markdown:'# 数据体检\n\n2条重复',artifacts:[{label:'报告 Markdown',file_path:'results/profile/report.md'},{label:'结构化结果 JSON',file_path:'results/profile/result.json'}]},
    summary:{markdown:'# 数据汇总\n\nA:30',artifacts:[{label:'报告 Markdown',file_path:'results/summary/report.md'},{label:'结构化结果 JSON',file_path:'results/summary/result.json'},{label:'明细 CSV',file_path:'results/summary/details.csv'}]},
  }}
  const {rerender}=render(<ProjectTaskOutput projectId="p" task={task as never}/>)
  for(const [section,path] of [['数据体检','profile'],['数据汇总','summary']]) {
    expect(screen.getByRole('link',{name:`${section} · 报告 Markdown ↓`})).toHaveAttribute('href',`/api/platform/api/v1/applications/p/workspace/files/results/${path}/report.md?download=1`)
    expect(screen.getByRole('link',{name:`${section} · 结构化结果 JSON ↓`})).toBeVisible()
  }
  expect(screen.getByRole('link',{name:'明细 CSV ↓'})).toBeVisible()
  expect(api).not.toHaveBeenCalled()
  // Presentation may flatten the same files; their saved section still applies.
  rerender(<ProjectTaskOutput projectId="p" task={{...task,presentation:{artifacts:[...task.outputs.profile.artifacts,...task.outputs.summary.artifacts]}} as never}/>)
  expect(screen.getByRole('link',{name:'数据体检 · 报告 Markdown ↓'})).toBeVisible()
})

it('uses output fields when headings are absent and rejects unsafe download paths',()=>{
  render(<ProjectTaskOutput projectId="p" task={{id:'t',status:'succeeded',outputs:{
    first:{artifacts:[{label:'报告',file_path:'results/first/report.md'}]},
    second:{artifacts:[{label:'报告',file_path:'results/second/report.md'},{label:'不可下载',file_path:'results/../private.md'}]},
  }} as never}/>)
  expect(screen.getByRole('link',{name:'first · 报告 ↓'})).toBeVisible()
  expect(screen.getByRole('link',{name:'second · 报告 ↓'})).toBeVisible()
  expect(screen.queryByRole('link',{name:'不可下载 ↓'})).not.toBeInTheDocument()
})

it('deduplicates flattened and nested report artifacts by full path and keeps their specific saved source',()=>{
  const profile={markdown:'# 数据体检',artifacts:[
    {label:'报告 Markdown',file_path:'results/profile/report.md'},
    {label:'结构化结果 JSON',file_path:'results/profile/result.json'},
  ]}
  const summary={markdown:'# 数据汇总',artifacts:[
    {label:'报告 Markdown',file_path:'results/summary/report.md'},
    {label:'结构化结果 JSON',file_path:'results/summary/result.json'},
    {label:'明细 CSV',file_path:'results/summary/details.csv'},
  ]}
  const flattened=[...profile.artifacts,...summary.artifacts]
  const report={markdown:'# 设备日报',artifacts:flattened,profile,summary}
  const task={id:'t',status:'succeeded',outputs:{markdown:'# 设备日报',
    artifacts:flattened.map(entry=>({...entry,label:entry.file_path})),report,profile,summary}}
  const before=JSON.stringify(task)
  render(<ProjectTaskOutput projectId="p" task={task as never}/>)
  const downloads=screen.getAllByRole('link').filter(link=>link.getAttribute('href')?.endsWith('?download=1'))
  expect(downloads).toHaveLength(5)
  expect(new Set(downloads.map(link=>link.getAttribute('href'))).size).toBe(5)
  for(const [section,path] of [['数据体检','profile'],['数据汇总','summary']]) {
    expect(screen.getByRole('link',{name:`${section} · 报告 Markdown ↓`})).toHaveAttribute('href',`/api/platform/api/v1/applications/p/workspace/files/results/${path}/report.md?download=1`)
    expect(screen.getByRole('link',{name:`${section} · 结构化结果 JSON ↓`})).toBeVisible()
  }
  expect(screen.getByRole('link',{name:'明细 CSV ↓'})).toBeVisible()
  expect(JSON.stringify(task)).toBe(before)
  expect(api).not.toHaveBeenCalled()
})

it('keeps same-name files in different directories and disambiguates only the distinct paths',()=>{
  const entries=[
    {label:'报告 Markdown',file_path:'results/first/report.md'},
    {label:'报告 Markdown',file_path:'results/second/report.md'},
  ]
  render(<ProjectTaskOutput projectId="p" task={{id:'t',status:'succeeded',outputs:{
    result:{markdown:'# 数据体检',artifacts:[entries[0],entries[0],entries[1]]},
  }} as never}/>)
  const downloads=screen.getAllByRole('link').filter(link=>link.getAttribute('href')?.endsWith('?download=1'))
  expect(downloads).toHaveLength(2)
  for(const entry of entries) expect(screen.getByRole('link',{name:`数据体检 · ${entry.file_path} · 报告 Markdown ↓`})).toHaveAttribute('href',`/api/platform/api/v1/applications/p/workspace/files/${entry.file_path}?download=1`)
})

it('deduplicates presentation downloads without adding unlisted outputs and retains explicit labels',()=>{
  const profile={markdown:'# 数据体检',artifacts:[{label:'报告 Markdown',file_path:'results/profile/report.md'}]}
  const summary={markdown:'# 数据汇总',artifacts:[{label:'报告 Markdown',file_path:'results/summary/report.md'}]}
  render(<ProjectTaskOutput projectId="p" task={{id:'t',status:'succeeded',outputs:{
    markdown:'# 设备日报',artifacts:[...profile.artifacts,...summary.artifacts],profile,summary,
    reviewed:{artifacts:[{label:'原说明',file_path:'solution/review.md'}]},
    unused:{artifacts:[{label:'未选文件',file_path:'results/not-presented.json'}]},
  },presentation:{artifacts:[
    ...profile.artifacts,...profile.artifacts,...summary.artifacts,
    {label:'review.md',file_path:'solution/review.md'},
    {label:'已复核说明',file_path:'solution/review.md'},
  ]}} as never}/>)
  const downloads=screen.getAllByRole('link').filter(link=>link.getAttribute('href')?.endsWith('?download=1'))
  expect(downloads).toHaveLength(3)
  expect(screen.getByRole('link',{name:'数据体检 · 报告 Markdown ↓'})).toBeVisible()
  expect(screen.getByRole('link',{name:'数据汇总 · 报告 Markdown ↓'})).toBeVisible()
  expect(screen.getByRole('link',{name:'已复核说明 ↓'})).toHaveAttribute('href','/api/platform/api/v1/applications/p/workspace/files/solution/review.md?download=1')
  expect(screen.queryByRole('link',{name:/未选文件/})).not.toBeInTheDocument()
})

it('shows saved business steps by default in the actual result component and opens readable input/output',async()=>{
  vi.mocked(api).mockResolvedValue(page)
  render(<ProjectTaskOutput projectId="p" task={{id:'t',status:'succeeded',outputs:{markdown:'费用报告'},runs:[run]} as never}/> )
  const steps=await screen.findByRole('region',{name:'处理步骤'})
  await within(steps).findByText('读取费用表')
  expect(within(steps).getByText('已完成')).toBeInTheDocument()
  expect(within(steps).getByText('用时 0.12 秒')).toBeInTheDocument()
  fireEvent.click(within(steps).getByRole('button',{name:'查看本步输入与产物'}))
  expect(screen.getByText('按本次记录还原')).toBeInTheDocument()
  expect(within(screen.getByRole('region',{name:'读取费用表的输入'})).getByRole('link',{name:'expenses.csv ↗'})).toHaveAttribute('href','/api/platform/api/v1/applications/p/workspace/files/requirement-package/a/expenses.csv')
  expect(within(screen.getByRole('region',{name:'读取费用表的产物'})).getByText('36.50')).toBeInTheDocument()
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
  expect(within(screen.getByRole('region',{name:'确认费用类别的输入'})).getByText('差旅是否含税？')).toBeInTheDocument()
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

it('leads with saved business explanations and keeps transport fields in collapsed technical details',async()=>{
  vi.mocked(api).mockResolvedValue({...page,steps:[{...step,
    input_preview:{inputs:{prepared:{snapshot_path:'results/b/input.json',sha256:'test-fingerprint'}}},
    input_summary:{prepared:{snapshot_path:'results/b/input.json'}},
    output_preview:{output:{rows:6,search_states:17,comparison_inputs:{source_path:'results/b/candidates.csv'}}},
    output_summary:{markdown:'共4根物料、6个单料组合。\n\n料三：长度不足；料四：没有同类型需求。\n\n只覆盖本次声明条件，不代表整体排程。'},
  }]})
  render(<ProjectRunSteps projectId="p" runs={[run]}/> )
  fireEvent.click(await screen.findByRole('button',{name:'查看本步输入与产物'}))
  const output=screen.getByRole('region',{name:'读取费用表的产物'})
  expect(within(output).getByText('料三：长度不足；料四：没有同类型需求。')).toBeVisible()
  expect(output).not.toHaveTextContent('search_states')
  expect(output).not.toHaveTextContent('comparison_inputs')
  const input=screen.getByRole('region',{name:'读取费用表的输入'})
  expect(input).toHaveTextContent('已核对的输入')
  expect(input).not.toHaveTextContent('sha256')
  const technical=screen.getByText('技术详情：输入、输出与校验信息').closest('details')!
  expect(technical.open).toBe(false)
  fireEvent.click(within(technical).getByText('技术详情：输入、输出与校验信息'))
  expect(technical.open).toBe(true)
  expect(technical).toHaveTextContent('test-fingerprint')
  expect(technical).toHaveTextContent('search_states')
})
