import {cleanup,fireEvent,render,screen,within} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import ReadableRunResults,{RawResult} from '@/app/components/ReadableRunResults'
import {collectRunResults,type ResultTask} from '@/lib/run-results'
vi.mock('@/lib/platform',()=>({withFrontendToken:(path:string)=>path}))
afterEach(()=>{cleanup();vi.unstubAllGlobals()})
const report='# 六项检查\n\n| 项目 | 结果 |\n| --- | --- |\n| 周期 | 存在差异 |'
const task:ResultTask={id:'t',status:'succeeded',outputs:{result:{report:{markdown:report,counts:{对应:2,差异:2,待确认:2}}},step_4:{markdown:report}},runs:[{id:'r',name:'引用核验',status:'succeeded',outputs:{markdown:report}}]}

it('reads nested Markdown once, preserves sources, and opens the full report',()=>{
  render(<ReadableRunResults projectId="p" task={task}/> )
  expect(screen.getAllByRole('button',{name:'查看完整报告'})).toHaveLength(1)
  expect(screen.getByRole('table')).toHaveTextContent('存在差异')
  expect(screen.getByText(/引用核验/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'查看完整报告'}))
  expect(within(screen.getByRole('dialog')).getByRole('table')).toHaveTextContent('存在差异')
})

it('does not select one of several different reports as the final report',()=>{
  render(<ReadableRunResults projectId="p" task={{...task,outputs:{a:{markdown:'# 分析A'},b:{markdown:'# 分析B'}},runs:[]}}/> )
  expect(screen.getAllByRole('button',{name:/查看完整报告/})).toHaveLength(2)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

it('shows partial products as partial and clears a report dialog on task change',()=>{
  const view=render(<ReadableRunResults key="t" projectId="p" task={task}/>)
  fireEvent.click(screen.getByRole('button',{name:'查看完整报告'}))
  view.rerender(<ReadableRunResults key="failed" projectId="p" task={{id:'failed',status:'failed',outputs:{},runs:[{id:'stage',name:'已完成分析',status:'succeeded',outputs:{markdown:'# 阶段分析'}}]}}/>)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.queryByText('六项检查')).not.toBeInTheDocument()
  expect(screen.getByText('阶段产物',{exact:true})).toBeInTheDocument()
  expect(screen.getByText(/本次任务尚未执行成功/)).toBeInTheDocument()
})

it('bounds table previews without losing IDs, zero, false or null in the original data',()=>{
  const rows=Array.from({length:10000},(_,i)=>({编号:String(i).padStart(4,'0'),值:0,确定:false,缺失:null}))
  const output={rows};render(<><ReadableRunResults projectId="p" task={{id:'t',status:'succeeded',outputs:output}}/><RawResult value={output}/></>)
  fireEvent.click(screen.getByText(/记录 · 本次输出/))
  const table=screen.getByRole('table');expect(within(table).getAllByRole('row')).toHaveLength(51)
  expect(table).toHaveTextContent('0000');expect(table).toHaveTextContent('空值');expect(table).toHaveTextContent('否')
  expect(screen.queryByRole('link',{name:'下载完整 JSON'})).not.toBeInTheDocument()
  expect(rows).toHaveLength(10000);expect(rows[0]).toEqual({编号:'0000',值:0,确定:false,缺失:null})
})

it('never presents an unversioned file as this run report',()=>{
  render(<ReadableRunResults projectId="p" task={{id:'old',status:'succeeded',outputs:{paths:{report:'results/report.md'}}}}/>)
  expect(screen.getByText(/无法确认当前文件/)).toBeInTheDocument()
  expect(screen.queryByRole('button',{name:'查看完整报告'})).not.toBeInTheDocument()
  expect(screen.getByText(/以下入口读取当前项目文件/)).toBeInTheDocument()
})

it('reads and downloads the run-owned file; a missing copy never falls back to workspace',async()=>{
  const fetcher=vi.fn().mockResolvedValue(new Response('missing',{status:404}));vi.stubGlobal('fetch',fetcher)
  const file={path:'results/report.md',run_id:'r',status:'saved',sha256:'a'.repeat(64)}
  render(<ReadableRunResults projectId="p" task={{id:'t',status:'succeeded',runs:[{id:'r',status:'succeeded',delivery_files:[file]}]}}/>)
  fireEvent.click(screen.getByRole('button',{name:'查看完整报告'}))
  await screen.findByRole('alert')
  expect(fetcher.mock.calls[0][0]).toContain('/projects/p/tasks/t/deliveries/r/')
  expect(fetcher.mock.calls.every(([url])=>!url.includes('/workspace/'))).toBe(true)
  expect(screen.getByRole('link',{name:'下载 report.md'}).getAttribute('href')).toContain(file.sha256)
})

it('does not mutate the object used by downstream input selection',()=>{
  const input=JSON.stringify(task);collectRunResults(task);expect(JSON.stringify(task)).toBe(input)
})

it('preserves legacy task explanations and merges identical tables with reordered keys',()=>{
  const result=collectRunResults({id:'t',status:'succeeded',presentation:{message:'已有任务说明'},outputs:{rows:[{a:1,b:2}]},runs:[{id:'r',status:'succeeded',outputs:{rows:[{b:2,a:1}]}}]})
  expect(result.reports.map(r=>r.text)).toEqual(['已有任务说明'])
  expect(result.views).toHaveLength(1)
})

it('keeps distinct large tables whose first fifty rows are identical',()=>{
  const a=Array.from({length:51},(_,i)=>({id:i,value:0}));const b=a.map(r=>({...r}));b[50].value=1
  const result=collectRunResults({id:'t',status:'succeeded',outputs:{first:{rows:a},second:{rows:b}}})
  expect(result.views).toHaveLength(2)
  expect((result.views[1].value as typeof b)[50].value).toBe(1)
})

it('keeps child report links bound to the child while merging identical file cards',()=>{
  const path='results/data.csv',sha256='a'.repeat(64)
  const saved=(run_id:string)=>({path,run_id,status:'saved',sha256})
  const value:ResultTask={id:'t',status:'succeeded',outputs:{results_csv:path},runs:[
    {id:'main',name:'主流程',status:'succeeded',outputs:{results_csv:path},delivery_files:[saved('main')]},
    {id:'child',name:'分析成员',status:'succeeded',outputs:{markdown:'# 子报告\n\n[子报告明细](results/data.csv)',results_csv:path},delivery_files:[saved('child')]}]}
  render(<ReadableRunResults projectId="p" task={value}/>)
  expect(screen.getByRole('link',{name:'子报告明细'})).toHaveAttribute('href',expect.stringContaining('/deliveries/child/'+sha256))
  expect(screen.getAllByRole('link',{name:'下载完整结果 CSV ↓'})).toHaveLength(1)
  expect(screen.getByText(/主流程 \/ 分析成员 · 已保存/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'查看完整报告'}))
  expect(within(screen.getByRole('dialog')).getByRole('link',{name:'子报告明细'})).toHaveAttribute('href',expect.stringContaining('/deliveries/child/'))
})

it('never substitutes a parent file when a child report has no saved version',()=>{
  const path='results/data.csv'
  render(<ReadableRunResults projectId="p" task={{id:'t',status:'succeeded',runs:[
    {id:'main',status:'succeeded',delivery_files:[{path,run_id:'main',status:'saved',sha256:'a'.repeat(64)}]},
    {id:'child',status:'succeeded',outputs:{markdown:'# 子报告\n\n[明细](results/data.csv)'}}]}}/>)
  expect(screen.queryByRole('link',{name:'明细'})).not.toBeInTheDocument()
})

it('offers explicitly unverified current downloads for size-limited files only',()=>{
  render(<ReadableRunResults projectId="p" task={{id:'t',status:'succeeded',outputs:{model_package:'results/model.zip'},runs:[
    {id:'r',status:'succeeded',delivery_files:[
      {path:'results/model.zip',run_id:'r',status:'unverified',reason:'size_limit',error:'文件超出副本保存范围'},
      {path:'results/missing.zip',run_id:'r',status:'unavailable',error:'文件不存在'}]}]}}/>)
  expect(screen.getByRole('link',{name:'下载模型包 ↓'})).toHaveAttribute('href',expect.stringContaining('model.zip'))
  expect(screen.getByText(/以下入口读取当前项目文件，并非已确认的历史原件/)).toBeInTheDocument()
  expect(screen.getByText('文件不存在')).toBeInTheDocument()
  expect(screen.getAllByRole('link')).toHaveLength(1)
})

it('resolves a forwarded report through all of its sources when only the child saved files',()=>{
  const markdown='# 报告\n\n[明细](results/data.csv)',sha256='a'.repeat(64)
  render(<ReadableRunResults projectId="p" task={{id:'t',status:'succeeded',outputs:{markdown},runs:[
    {id:'main',name:'主流程',status:'succeeded',outputs:{markdown}},
    {id:'child',name:'成员',status:'succeeded',outputs:{markdown},delivery_files:[{path:'results/data.csv',run_id:'child',status:'saved',sha256}]}]}}/>)
  expect(screen.getByRole('link',{name:'明细'})).toHaveAttribute('href',expect.stringContaining('/deliveries/child/'+sha256))
  fireEvent.click(screen.getByRole('button',{name:'查看完整报告'}))
  expect(within(screen.getByRole('dialog')).getByRole('link',{name:'明细'})).toHaveAttribute('href',expect.stringContaining('/deliveries/child/'+sha256))
})

it('keeps identical report bodies separate when their linked file versions differ',()=>{
  const markdown='# 报告\n\n[明细](results/data.csv)'
  const runs=['A','B'].map(version=>({id:version,name:'成员 '+version,status:'succeeded',outputs:{markdown},
    delivery_files:[{path:'results/data.csv',run_id:version,status:'saved',sha256:version.toLowerCase().repeat(64)}]}))
  render(<ReadableRunResults projectId="p" task={{id:'t',status:'succeeded',runs}}/>)
  expect(screen.getAllByRole('button',{name:/查看完整报告/})).toHaveLength(2)
  for(const version of ['A','B']){
    fireEvent.click(screen.getByRole('button',{name:'查看完整报告 · 报告 · 成员 '+version}))
    expect(within(screen.getByRole('dialog')).getByRole('link',{name:'明细'})).toHaveAttribute('href',expect.stringContaining('/deliveries/'+version+'/'+version.toLowerCase().repeat(64)))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button',{name:'关闭阅读窗口'}))
  }
})
