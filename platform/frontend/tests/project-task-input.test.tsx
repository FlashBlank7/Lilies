import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import ProjectTaskInput from '@/app/components/ProjectTaskInput'
import {api} from '@/lib/platform'
vi.mock('@/lib/platform',()=>({api:vi.fn()}))
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
