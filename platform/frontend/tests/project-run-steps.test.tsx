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
  expect(screen.getByRole('button',{name:/逐批处理 · 第 1 轮/})).toHaveAttribute('aria-expanded','false')
  expect(screen.getByRole('button',{name:/逐批处理 · 第 2 轮/})).toHaveAttribute('aria-expanded','true')
  fireEvent.click(screen.getByRole('button',{name:/逐批处理 · 第 1 轮/}))
  expect(screen.getAllByText('读取费用表')).toHaveLength(2)
})

it('groups three saved iteration items and opens only the matching inputs, outputs and canvas location',async()=>{
  const container={...step,id:'each',node_path:['each'],title:'逐批检查',type:'iteration'}
  const items=Array.from({length:3},(_,index)=>[
    {...step,id:`each[${index}].start`,node_path:['each','start'],title:'本项输入',type:'start',scope:`逐批检查 · 第 ${index+1} 轮`,input_preview:{item:`批次${index+1}`},output_preview:{item:`批次${index+1}`}},
    {...step,id:`each[${index}].read`,node_path:['each','read'],scope:`逐批检查 · 第 ${index+1} 轮`,input_preview:{item:`批次${index+1}`},output_preview:{summary:`第${index+1}项的结果`}},
  ]).flat()
  vi.mocked(api).mockResolvedValue({...page,steps:[container,...items],total:7})
  render(<ProjectRunSteps projectId="p" runs={[run]}/> )
  for(let index=1;index<=3;index++)expect(await screen.findByRole('button',{name:new RegExp(`逐批检查 · 第 ${index} 项`)})).toHaveAttribute('aria-expanded','false')
  const second=screen.getByRole('button',{name:/逐批检查 · 第 2 项/})
  expect(second).toHaveTextContent('2 个步骤')
  expect(second).toHaveTextContent('已完成 2')
  fireEvent.click(second)
  const group=screen.getByRole('list',{name:'逐批检查 · 第 2 项的步骤'})
  expect(within(group).getAllByRole('listitem')).toHaveLength(2)
  expect(within(group).getAllByRole('listitem')[0].querySelector('[aria-hidden="true"]')).toHaveTextContent('4')
  fireEvent.click(within(group).getAllByRole('button',{name:'查看本步输入与产物'})[1])
  expect(within(group).getByRole('region',{name:'读取费用表的输入'})).toHaveTextContent('批次2')
  expect(within(group).getByRole('region',{name:'读取费用表的产物'})).toHaveTextContent('第2项的结果')
  expect(screen.queryByText('批次1')).not.toBeInTheDocument()
  expect(screen.queryByText('批次3')).not.toBeInTheDocument()
  const href=within(group).getAllByRole('link',{name:'在当前画布定位 ↗'})[1].getAttribute('href')!
  expect(JSON.parse(new URL(href,'http://localhost').searchParams.get('node_path')!)).toEqual(['each','read'])
})

it('keeps nested and same-named sibling rounds separate and exposes the entire waiting ancestor chain',async()=>{
  const outer={...step,id:'outer',node_path:['outer'],title:'按清单检查',type:'iteration'}
  const records=[outer,...[0,1].flatMap(index=>[
    {...step,id:`outer[${index}].inner`,node_path:['outer','inner'],title:'重复核对',type:'loop',scope:`按清单检查 · 第 ${index+1} 轮`},
    {...step,id:`outer[${index}].inner[1].read`,node_path:['outer','inner','read'],title:'核对字段',scope:`按清单检查 · 第 ${index+1} 轮 / 重复核对 · 第 2 轮`,status:index===1?'waiting':'completed',input_preview:{item:index===1?'第二清单输入':'第一清单输入'},output_preview:null},
  ]),
    {...step,id:'sibling',node_path:['sibling'],title:'重复核对',type:'loop'},
    {...step,id:'sibling[1].read',node_path:['sibling','read'],title:'兄弟步骤',scope:'重复核对 · 第 2 轮',status:'failed',error:'兄弟循环异常'},
  ]
  vi.mocked(api).mockResolvedValue({...page,steps:records,total:records.length})
  render(<ProjectRunSteps projectId="p" runs={[run]}/> )
  expect(await screen.findByRole('button',{name:/按清单检查 · 第 1 项/})).toHaveAttribute('aria-expanded','false')
  expect(screen.getByRole('button',{name:/按清单检查 · 第 2 项/})).toHaveAttribute('aria-expanded','true')
  const second=screen.getByRole('list',{name:'按清单检查 · 第 2 项的步骤'})
  expect(within(second).getByRole('button',{name:/重复核对 · 第 2 轮/})).toHaveAttribute('aria-expanded','true')
  expect(within(second).getByRole('region',{name:'核对字段的输入'})).toHaveTextContent('第二清单输入')
  expect(screen.queryByText('第一清单输入')).not.toBeInTheDocument()
  expect(screen.getAllByRole('button',{name:/重复核对 · 第 2 轮/})).toHaveLength(2)
  expect(screen.getByRole('alert')).toHaveTextContent('兄弟循环异常')
  fireEvent.click(screen.getByRole('button',{name:/按清单检查 · 第 1 项/}))
  const first=screen.getByRole('list',{name:'按清单检查 · 第 1 项的步骤'})
  fireEvent.click(within(first).getByRole('button',{name:/重复核对 · 第 2 轮/}))
  fireEvent.click(within(within(first).getByRole('list',{name:'重复核对 · 第 2 轮的步骤'})).getByRole('button',{name:'查看本步输入与产物'}))
  expect(within(first).getByRole('region',{name:'核对字段的输入'})).toHaveTextContent('第一清单输入')
})

it('matches literal node ids and keeps unverified paths visible without grouping by their scope',async()=>{
  vi.mocked(api).mockResolvedValue({...page,steps:[
    {...step,id:'each.part[7]',node_path:['each.part[7]'],title:'批次 / 检查 · 第 2 轮',type:'iteration'},
    {...step,id:'each.part[7][0].leaf[2]',node_path:['each.part[7]','leaf[2]'],title:'正确步骤',scope:'批次 / 检查 · 第 2 轮 · 第 1 轮',status:'running'},
    {...step,id:'wrong[0].read',node_path:['other','read'],title:'路径不匹配',scope:'批次 / 检查 · 第 2 轮 · 第 1 轮'},
    {...step,id:'read',node_path:[],title:'没有路径',scope:'批次 / 检查 · 第 2 轮 · 第 1 轮'},
  ],total:4})
  render(<ProjectRunSteps projectId="p" runs={[run]}/> )
  expect(await screen.findByRole('button',{name:/批次 \/ 检查 · 第 2 轮 · 第 1 项/})).toHaveAttribute('aria-expanded','true')
  const group=screen.getByRole('list',{name:'批次 / 检查 · 第 2 轮 · 第 1 项的步骤'})
  expect(group).toHaveTextContent('正确步骤')
  expect(group).not.toHaveTextContent('路径不匹配')
  expect(group).not.toHaveTextContent('没有路径')
  expect(screen.getByText('路径不匹配')).toBeVisible()
  expect(screen.getByText('没有路径')).toBeVisible()
})

it('keeps groups at page boundaries readable without a parent row or a whole-round success claim',async()=>{
  vi.mocked(api).mockImplementation(async path=>({...page,total:103,next_offset:path.includes('offset=100')?null:100,steps:path.includes('offset=100')?[
    {...step,id:'each[1].read',node_path:['each','read'],title:'第二页处理',scope:'逐批检查 · 第 2 轮'},
    {...step,id:'each[1].end',node_path:['each','end'],title:'第二页产物',scope:'逐批检查 · 第 2 轮',status:'waiting'},
    {...step,id:'each[2].read',node_path:['each','read'],title:'下一项处理',scope:'逐批检查 · 第 3 轮'},
  ]:[{...step,id:'each[1].start',node_path:['each','start'],title:'第一页输入',scope:'逐批检查 · 第 2 轮'}]}))
  render(<ProjectRunSteps projectId="p" runs={[run]}/> )
  expect(await screen.findByRole('button',{name:/逐批检查 · 第 2 轮/})).toHaveTextContent('本页 1 个步骤')
  fireEvent.click(screen.getByRole('button',{name:'后100步'}))
  await screen.findByText('第二页产物')
  const group=screen.getByRole('button',{name:/逐批检查 · 第 2 轮/})
  expect(group).toHaveTextContent('本页 2 个步骤')
  expect(group).toHaveTextContent('已完成 1')
  expect(group).toHaveTextContent('等待补充 1')
  expect(screen.queryByText('第一页输入')).not.toBeInTheDocument()
  const rows=within(screen.getByRole('list',{name:'逐批检查 · 第 2 轮的步骤'})).getAllByRole('listitem')
  expect(rows[0].querySelector('[aria-hidden="true"]')).toHaveTextContent('101')
  expect(rows[1].querySelector('[aria-hidden="true"]')).toHaveTextContent('102')
  fireEvent.click(screen.getByRole('button',{name:'前100步'}))
  await waitFor(()=>expect(screen.getByRole('button',{name:/逐批检查 · 第 2 轮/})).toHaveTextContent('本页 1 个步骤'))
  fireEvent.click(screen.getByRole('button',{name:/逐批检查 · 第 2 轮/}))
  expect(screen.getByText('第一页输入')).toBeVisible()
  expect(api).toHaveBeenCalledWith('/api/v1/runs/r/steps?offset=100&limit=100')
})

it('reopens a collapsed round when polling records a new failure and preserves unchanged choices',async()=>{
  vi.useFakeTimers()
  const active={...step,id:'each[0].read',node_path:['each','read'],scope:'逐批检查 · 第 1 轮',status:'running'}
  vi.mocked(api).mockResolvedValueOnce({...page,status:'running',steps:[active]})
    .mockResolvedValueOnce({...page,status:'running',steps:[active]})
    .mockResolvedValueOnce({...page,status:'failed',steps:[{...active,status:'failed',error:'新错误'}]})
  await act(async()=>{render(<ProjectRunSteps projectId="p" runs={[{...run,status:'running'}]}/> )})
  const round=()=>screen.getByRole('button',{name:/逐批检查 · 第 1 轮/})
  expect(round()).toHaveAttribute('aria-expanded','true')
  fireEvent.click(round())
  await act(async()=>{await vi.advanceTimersByTimeAsync(2000)})
  expect(round()).toHaveAttribute('aria-expanded','false')
  expect(screen.queryByText('读取费用表')).not.toBeInTheDocument()
  await act(async()=>{await vi.advanceTimersByTimeAsync(2000)})
  expect(round()).toHaveAttribute('aria-expanded','true')
  expect(screen.getByRole('alert')).toHaveTextContent('新错误')
  expect(screen.getByText('失败').closest('li')).toHaveAttribute('data-status','failed')
})

it('keeps a manually collapsed round closed when polling only completes an active step',async()=>{
  vi.useFakeTimers()
  const active={...step,id:'each[0].read',node_path:['each','read'],scope:'逐批检查 · 第 1 轮',status:'running'}
  const waiting={...active,id:'each[0].ask',node_path:['each','ask'],title:'核对输入',status:'waiting'}
  vi.mocked(api).mockResolvedValueOnce({...page,status:'running',steps:[active,waiting]})
    .mockResolvedValueOnce({...page,status:'running',steps:[{...active,status:'completed'},waiting]})
    .mockResolvedValue({...page,status:'succeeded',steps:[{...active,status:'completed'},{...waiting,status:'completed'}]})
  await act(async()=>{render(<ProjectRunSteps projectId="p" runs={[{...run,status:'running'}]}/> )})
  const round=()=>screen.getByRole('button',{name:/逐批检查 · 第 1 轮/})
  fireEvent.click(round())
  await act(async()=>{await vi.advanceTimersByTimeAsync(2000)})
  expect(round()).toHaveAttribute('aria-expanded','false')
  expect(round()).toHaveTextContent('已完成 1')
  expect(round()).toHaveTextContent('等待补充 1')
  await act(async()=>{await vi.advanceTimersByTimeAsync(2000)})
  expect(round()).toHaveAttribute('aria-expanded','false')
  expect(round()).toHaveTextContent('已完成 2')
})

it.each(['iteration','loop'].flatMap(type=>['interrupted','waiting','running','failed','warning'].map(status=>[type,status])))('keeps %s container previews collapsed at %s while its waiting item and errors stay visible',async(type,status)=>{
  vi.mocked(api).mockResolvedValue({...page,status:'paused',steps:[
    {...step,id:'each',node_path:['each'],title:'逐批检查',type,status,input_preview:{items:['容器原始参数']},output_preview:{summary:'容器保存结果'},error:status==='failed'?'本轮处理失败':null},
    {...step,id:'each[1].human',node_path:['each','human'],title:'补齐本项数量',type:'human_input',status:'waiting',scope:'逐批检查 · 第 2 轮',input_preview:{question:'请填写本项数量'},output_preview:null},
    {...step,id:'bad',node_path:['bad'],title:'核对字段',status:'failed',error:'缺少金额',input_preview:{source_path:'requirement-package/a/expenses.csv'},output_preview:null},
  ],total:3})
  render(<ProjectRunSteps projectId="p" runs={[{...run,status:'paused'}]}/> )
  const container=(await screen.findByText('逐批检查')).closest('li')!
  expect(container).toHaveAttribute('data-status',status)
  expect(within(container).getByRole('button',{name:'查看本步输入与产物'})).toHaveAttribute('aria-expanded','false')
  expect(screen.queryByRole('region',{name:'逐批检查的输入'})).not.toBeInTheDocument()
  expect(screen.queryByRole('region',{name:'逐批检查的产物'})).not.toBeInTheDocument()
  expect(within(container).getByRole('link',{name:'在当前画布定位 ↗'})).toBeVisible()
  if(status==='failed')expect(within(container).getByRole('alert')).toHaveTextContent('本轮处理失败')
  expect(screen.getByRole('button',{name:/逐批检查 · 第 2 (项|轮)/})).toHaveAttribute('aria-expanded','true')
  expect(screen.getByRole('region',{name:'补齐本项数量的输入'})).toHaveTextContent('请填写本项数量')
  expect(screen.getByRole('region',{name:'核对字段的输入'})).toBeVisible()
  expect(within(screen.getByText('核对字段').closest('li')!).getByRole('alert')).toHaveTextContent('缺少金额')
  fireEvent.click(within(container).getByRole('button',{name:'查看本步输入与产物'}))
  expect(screen.getByRole('region',{name:'逐批检查的输入'})).toHaveTextContent('容器原始参数')
  expect(screen.getByRole('region',{name:'逐批检查的产物'})).toHaveTextContent('容器保存结果')
})

it('explains interrupted steps during a confirmed human-input pause without changing their recorded statuses',async()=>{
  vi.mocked(api).mockResolvedValue({...page,status:'paused',steps:[
    {...step,id:'each[0].human',title:'确认费用类别',type:'human_input',status:'waiting',output_preview:null},
    {...step,id:'each[1].read',status:'interrupted',output_preview:null},
    {...step,id:'end',title:'汇总报告',status:'pending',output_preview:null},
    {...step,id:'bad',title:'异常处理',status:'failed',error:'缺少金额',output_preview:null},
  ],total:4})
  render(<ProjectRunSteps projectId="p" runs={[{...run,status:'paused'}]}/> )
  expect(await screen.findByText('正在等待补充信息；部分并行步骤会在继续后重新执行。“已中断”不一定表示整个流程失败。')).toBeVisible()
  expect(screen.getByText('已中断').closest('li')).toHaveAttribute('data-status','interrupted')
  expect(screen.getByText('尚未执行').closest('li')).toHaveAttribute('data-status','pending')
  expect(screen.getByText('等待补充')).toBeVisible()
  expect(screen.getByText('失败')).toBeVisible()
  expect(screen.getByRole('alert')).toHaveTextContent('缺少金额')
})

it.each([
  ['cancelled','human_input','waiting','interrupted'],
  ['interrupted','human_input','waiting','interrupted'],
  ['failed','human_input','waiting','interrupted'],
  ['running','human_input','waiting','interrupted'],
  ['paused','iteration','waiting','interrupted'],
  ['paused','human_input','pending','interrupted'],
  ['paused','human_input','waiting','pending'],
])('does not explain interruption as an input pause for run %s, %s/%s and step %s',async(status,type,humanStatus,otherStatus)=>{
  vi.mocked(api).mockResolvedValue({...page,status,steps:[
    {...step,id:'human',title:'确认费用类别',type,status:humanStatus,output_preview:null},
    {...step,id:'each[1].read',status:otherStatus,output_preview:null},
  ],total:2})
  render(<ProjectRunSteps projectId="p" runs={[{...run,status}]}/> )
  await screen.findByText('确认费用类别')
  expect(screen.queryByText(/部分并行步骤会在继续后重新执行/)).not.toBeInTheDocument()
  if(otherStatus==='interrupted')expect(screen.getByText('已中断')).toBeVisible()
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
