import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import ProjectTaskInput from '@/app/components/ProjectTaskInput'
import {api} from '@/lib/platform'
vi.mock('@/lib/platform',()=>({api:vi.fn(),withFrontendToken:(value:string)=>value}))
afterEach(()=>{cleanup();vi.clearAllMocks()})
const paused={id:'t',status:'waiting_input',runs:[{id:'r',status:'paused',waiting_input:{node_id:'ask',title:'理解标签',context:'target 代表什么？',fields:[
  {name:'choice',label:'是否了解',type:'string',required:true,options:['可以补充','不清楚']},
  {name:'count',label:'样本数量',type:'number',required:false},{name:'answer',label:'补充说明',type:'string',required:false}]}}]}
it('displays the persisted question, accepts unknown and resumes once without JSON',async()=>{
  let current=paused
  vi.mocked(api).mockImplementation(async(_,options)=>{if(options?.method==='POST'){current={...paused,status:'succeeded',runs:[]} ;return current as never}return current as never})
  const onTask=vi.fn()
  render(<ProjectTaskInput projectId="p" taskId="t" onTask={onTask}/>)
  expect(await screen.findByText('target 代表什么？')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('是否了解'),{target:{value:'不清楚'}})
  fireEvent.change(screen.getByLabelText('样本数量'),{target:{value:'12'}})
  fireEvent.click(screen.getByRole('button',{name:'提交并继续这次运行'}))
  await waitFor(()=>expect(screen.queryByRole('form')).not.toBeInTheDocument())
  const posts=vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST')
  expect(posts).toHaveLength(1)
  expect(posts[0][0]).toBe('/api/v1/projects/p/tasks/t/runs/r/input')
  expect(JSON.parse(posts[0][1]!.body as string)).toEqual({node_id:'ask',resume:true,values:{choice:'不清楚',count:12}})
  expect(onTask).toHaveBeenCalledWith(expect.objectContaining({status:'succeeded'}))
})
it('restores waiting forms after remount, preserves answers on conflict and never resumes stopped tasks',async()=>{
  let current=paused
  vi.mocked(api).mockImplementation(async(_,options)=>{if(options?.method==='POST')throw Error('问题已变化，请刷新');return current as never})
  const first=render(<ProjectTaskInput projectId="p" taskId="t"/>)
  await screen.findByLabelText('是否了解');first.unmount()
  const second=render(<ProjectTaskInput projectId="p" taskId="t"/>)
  await screen.findByLabelText('是否了解')
  fireEvent.change(screen.getByLabelText('是否了解'),{target:{value:'可以补充'}})
  fireEvent.change(screen.getByLabelText('补充说明'),{target:{value:'是产品类别'}})
  fireEvent.click(screen.getByRole('button',{name:'提交并继续这次运行'}))
  await screen.findByRole('alert');expect(screen.getByLabelText('补充说明')).toHaveValue('是产品类别')
  second.unmount();current={...paused,status:'interrupted'}
  await act(async()=>{render(<ProjectTaskInput projectId="p" taskId="t"/>)})
  expect(screen.queryByRole('form')).not.toBeInTheDocument()
})

it('shows project pictures with the persisted question, handles a broken image and submits only explicit answers',async()=>{
  vi.mocked(api).mockResolvedValue({...paused,runs:[{...paused.runs[0],waiting_input:{...paused.runs[0].waiting_input,
    context:{markdown:'## 样本 A\n\n机器原判断仍保留。',images:[{path:'results/frozen/image.png',label:'待复核图片'}]}}}]} as never)
  render(<ProjectTaskInput projectId="p" taskId="t"/>)
  const image=await screen.findByRole('img',{name:'待复核图片'})
  expect(image).toHaveAttribute('src','/api/platform/api/v1/applications/p/workspace/files/results/frozen/image.png')
  expect(screen.getByRole('heading',{name:'样本 A'})).toBeInTheDocument()
  expect(vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST')).toHaveLength(0)
  fireEvent.error(image)
  expect(screen.getByRole('alert')).toHaveTextContent('不要凭缺失图片猜测结论')
  expect(screen.getByRole('link',{name:'下载图片：待复核图片'})).toHaveAttribute('download')
  expect(screen.getByLabelText('是否了解')).toHaveValue('')
})

it('never renders external, cross-project or traversal images from workflow context',async()=>{
  vi.mocked(api).mockResolvedValue({...paused,runs:[{...paused.runs[0],waiting_input:{...paused.runs[0].waiting_input,
    context:{markdown:'需要核对图片',images:[{path:'https://example.invalid/a.png'},{path:'/api/platform/api/v1/applications/other/workspace/files/results/a.png'},{path:'results/../private/a.png'}]}}}]} as never)
  render(<ProjectTaskInput projectId="p" taskId="t"/>)
  await screen.findByText('需要核对图片')
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  expect(screen.getAllByRole('alert')).toHaveLength(3)
})
