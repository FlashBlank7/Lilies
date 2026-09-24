import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import SharedMethods from '@/app/components/SharedMethods'

vi.mock('@/lib/platform',()=>({api:vi.fn()}))
afterEach(cleanup)
beforeEach(()=>{
  vi.mocked(api).mockReset()
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path==='/api/v1/projects')return [{id:'p',name:'My project'},{id:'target',name:'Receiving project'}] as never
    if(path.endsWith('/space'))return {workflows:[{id:'w',name:'Existing workflow'}]} as never
    if(path.endsWith('/skills'))return [{id:'method',name:'Measurement check'},{id:'other',name:'Another method'}] as never
    if(path.endsWith('/skills/method'))return {id:'method',revision:3,references:{'check.py':'print(5)','private.txt':'Do not share'}} as never
    if(path.endsWith('/skills/other'))return {id:'other',revision:2,references:{'readme.md':'Other text'}} as never
    if(path.endsWith('/preview'))return {skill:{references:{'check.py':'print(5)'}}} as never
    if(options?.method==='POST')return {id:'shared'} as never
    return [] as never
  })
})

it('shares only explicitly chosen reference names and invalidates the preview when selection changes',async()=>{
  render(<SharedMethods projectId="p" onChanged={vi.fn()}/>)
  fireEvent.click(screen.getByRole('button',{name:'分享本项目的方法或流程'}))
  fireEvent.change(await screen.findByLabelText('要分享的内容'),{target:{value:'skill:method'}})
  const code=await screen.findByRole('checkbox',{name:/check.py/})
  expect(code).not.toBeChecked()
  expect(screen.getByRole('checkbox',{name:/private.txt/})).not.toBeChecked()
  fireEvent.click(code)
  expect(vi.mocked(api).mock.calls.some(([,o])=>o?.method==='POST')).toBe(false)
  fireEvent.click(screen.getByRole('button',{name:'预览分享内容'}))
  expect(await screen.findByText('将分享的定义')).toBeInTheDocument()
  fireEvent.click(code)
  expect(screen.queryByText('将分享的定义')).not.toBeInTheDocument()
  fireEvent.click(code)
  fireEvent.click(screen.getByRole('button',{name:'分享给所选项目'}))
  expect(await screen.findByText('已分享给所选项目。原项目及已安装副本保持独立。')).toBeInTheDocument()
  const calls=vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST')
  expect(calls).toHaveLength(2)
  for(const [,options] of calls){
    const body=JSON.parse(String(options?.body))
    expect(body.reference_names).toEqual(['check.py'])
    expect(body.skill_revision).toBe(3)
    expect(body.skill_id).toBe('method')
  }
})

it('discards old reference selections and late responses when choosing a different method',async()=>{
  let finish:(value:unknown)=>void=()=>{}
  const healthy=vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation((path,options)=>path.endsWith('/skills/method')
    ?new Promise(resolve=>{finish=resolve}):healthy(path,options))
  render(<SharedMethods projectId="p" onChanged={vi.fn()}/>)
  fireEvent.click(screen.getByRole('button',{name:'分享本项目的方法或流程'}))
  const choice=await screen.findByLabelText('要分享的内容')
  fireEvent.change(choice,{target:{value:'skill:method'}})
  expect(screen.getByRole('button',{name:'分享给所选项目'})).toBeDisabled()
  fireEvent.change(choice,{target:{value:'skill:other'}})
  fireEvent.click(await screen.findByRole('checkbox',{name:/readme.md/}))
  await act(async()=>{finish({id:'method',revision:3,references:{'check.py':'late'}})})
  expect(screen.queryByRole('checkbox',{name:/check.py/})).not.toBeInTheDocument()
  fireEvent.change(choice,{target:{value:'workflow:w'}})
  await waitFor(()=>expect(screen.queryByText('一起分享的引用文件')).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('button',{name:'分享给所选项目'}))
  await screen.findByText('已分享给所选项目。原项目及已安装副本保持独立。')
  const sent=vi.mocked(api).mock.calls.find(([,o])=>o?.method==='POST')!
  const body=JSON.parse(String(sent[1]?.body))
  expect(body.workflow_id).toBe('w')
  expect(body.reference_names).toBeUndefined()
  expect(body.skill_revision).toBeUndefined()
})
