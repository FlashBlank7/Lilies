import {act, cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectSpace from '@/app/components/ProjectSpace'

const {account, mark} = vi.hoisted(()=>({account:{id:'alice'},mark:vi.fn()}))
vi.mock('@/lib/platform',()=>({api:vi.fn()}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>account}))
vi.mock('@/app/components/Onboarding',()=>({useOnboarding:()=>({active:false,mark})}))
vi.mock('@/app/components/ProjectMaterials',()=>({default:()=>null}))
vi.mock('@/app/components/SharedMethods',()=>({default:()=>null}))

const source={id:'source',name:'质量分析',display_name:'部门质量分析',revision:7,description:'',node_count:2,allowed:true,inputs:[]}
const other={...source,id:'other',name:'日报',display_name:'部门日报'}
let workflows=[source,other]
const space=()=>({workflows,files:[],files_truncated:false})
const props=()=>({projectId:'p',onWorkflow:vi.fn(),onFile:vi.fn(),onTalk:vi.fn(),onChanged:vi.fn()})
const copyCalls=()=>vi.mocked(api).mock.calls.filter(([path,options])=>path.endsWith('/agent-tools')&&options?.method==='POST')
const body=(index:number)=>JSON.parse(copyCalls()[index][1]!.body as string)
function deferred<T>() {let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done});return {promise,resolve}}
async function openCopy(label=source.display_name) {
  const row=(await screen.findByText(label)).closest('tr')!
  const button=within(row).getByRole('button',{name:'复制并编辑'})
  fireEvent.click(button)
  return button
}
const submit=()=>fireEvent.submit(screen.getByRole('form',{name:'复制工作流'}))

beforeEach(()=>{
  account.id='alice';workflows=[source,other];sessionStorage.clear();mark.mockClear()
  vi.mocked(api).mockReset()
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path.endsWith('/space'))return space() as never
    if(options?.method==='POST')return {workflow_id:'copy'} as never
    return [] as never
  })
})
afterEach(()=>{cleanup();vi.restoreAllMocks()})

it.each(['workflow_id','id'])('copies the selected revision with an editable name and opens the returned %s',async field=>{
  const callbacks=props(),normal=vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async(path,options)=>options?.method==='POST'?{[field]:'new-copy'} as never:normal(path,options))
  render(<ProjectSpace {...callbacks}/>)
  await openCopy()
  const dialog=screen.getByRole('dialog',{name:'复制工作流'})
  expect(within(dialog).getByText('来源：部门质量分析 · 版本 7')).toBeVisible()
  expect(within(dialog).getByText('副本可独立修改，不影响原工作流；同项目的文件、模型和子流程仍共享。')).toBeVisible()
  const input=within(dialog).getByLabelText('副本名称')
  expect(input).toHaveValue('部门质量分析副本');expect(input).toHaveFocus()
  fireEvent.change(input,{target:{value:'  我的质量分析  '}})
  submit()
  await waitFor(()=>expect(callbacks.onWorkflow).toHaveBeenCalledWith('new-copy'))
  expect(copyCalls()).toHaveLength(1)
  expect(copyCalls()[0][0]).toBe('/api/v1/projects/p/agent-tools')
  expect(body(0)).toEqual({name:'project_workflows',arguments:{action:'copy',workflow_id:'source',expected_revision:7,name:'我的质量分析',request_key:expect.any(String)}})
  expect(body(0).arguments.request_key).not.toBe('')
  expect(callbacks.onChanged).toHaveBeenCalledTimes(1)
  expect(callbacks.onTalk).not.toHaveBeenCalled()
  expect(vi.mocked(api).mock.calls.filter(([,options])=>options?.method==='POST')).toHaveLength(1)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

it('cancels without a request and returns focus to the selected row',async()=>{
  render(<ProjectSpace {...props()}/>)
  const trigger=await openCopy()
  fireEvent.click(screen.getByRole('button',{name:'取消'}))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus();expect(copyCalls()).toHaveLength(0)
})

it('keeps the default copy name within the API limit for a 100-character source name',async()=>{
  const label='验'.repeat(100),callbacks=props()
  workflows=[{...source,name:label,display_name:label}]
  render(<ProjectSpace {...callbacks}/>);await openCopy(label)
  expect(screen.getByLabelText('副本名称')).toHaveValue('验'.repeat(98)+'副本')
  submit();await waitFor(()=>expect(callbacks.onWorkflow).toHaveBeenCalledWith('copy'))
  expect(body(0).arguments.name).toHaveLength(100)
})

it('ignores duplicate submissions while the copy is pending',async()=>{
  const pending=deferred<{workflow_id:string}>(),normal=vi.mocked(api).getMockImplementation()!,callbacks=props()
  vi.mocked(api).mockImplementation((path,options)=>options?.method==='POST'?pending.promise as never:normal(path,options))
  render(<ProjectSpace {...callbacks}/>);await openCopy()
  submit();submit()
  expect(copyCalls()).toHaveLength(1)
  expect(screen.getByLabelText('副本名称')).toBeDisabled()
  expect(screen.getByRole('button',{name:'正在处理…'})).toBeDisabled()
  await act(async()=>{pending.resolve({workflow_id:'copy'})})
  expect(callbacks.onWorkflow).toHaveBeenCalledTimes(1)
})

it('retries a lost response with identical parameters and key, including after closing the dialog',async()=>{
  const callbacks=props(),normal=vi.mocked(api).getMockImplementation()!
  let attempts=0
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(options?.method==='POST'&&++attempts===1)throw new Error('网络连接中断')
    return normal(path,options)
  })
  render(<ProjectSpace {...callbacks}/>);await openCopy();submit()
  expect(await screen.findByRole('alert')).toHaveTextContent('网络连接中断')
  expect(callbacks.onWorkflow).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button',{name:'取消'}));await openCopy();submit()
  await waitFor(()=>expect(callbacks.onWorkflow).toHaveBeenCalledWith('copy'))
  expect(body(1)).toEqual(body(0))
})

it('keeps the request key if the response lacks a copy id instead of reporting success',async()=>{
  const callbacks=props(),normal=vi.mocked(api).getMockImplementation()!
  let attempts=0
  vi.mocked(api).mockImplementation(async(path,options)=>options?.method==='POST'&&++attempts===1?{} as never:normal(path,options))
  render(<ProjectSpace {...callbacks}/>);await openCopy();submit()
  expect(await screen.findByRole('alert')).toHaveTextContent('未收到副本编号')
  expect(callbacks.onWorkflow).not.toHaveBeenCalled();expect(callbacks.onChanged).not.toHaveBeenCalled()
  submit();await waitFor(()=>expect(callbacks.onWorkflow).toHaveBeenCalledWith('copy'))
  expect(body(1)).toEqual(body(0))
})

it('uses a new key when the requested name or source changes',async()=>{
  const normal=vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async(path,options)=>{if(options?.method==='POST')throw new Error('网络连接中断');return normal(path,options)})
  render(<ProjectSpace {...props()}/>);await openCopy();submit();await screen.findByRole('alert')
  fireEvent.change(screen.getByLabelText('副本名称'),{target:{value:'另一份副本'}})
  submit();await screen.findByRole('alert')
  expect(body(1).arguments.request_key).not.toBe(body(0).arguments.request_key)
  expect(body(1).arguments.name).toBe('另一份副本')
  fireEvent.click(screen.getByRole('button',{name:'取消'}));await openCopy(other.display_name);submit();await screen.findByRole('alert')
  expect(body(2).arguments.workflow_id).toBe(other.id)
  expect(new Set(copyCalls().map((_,index)=>body(index).arguments.request_key)).size).toBe(3)
})

it('requires an explicit source refresh after a conflict and only copies the newer revision on resubmission',async()=>{
  const normal=vi.mocked(api).getMockImplementation()!,callbacks=props()
  let attempts=0
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(options?.method==='POST'&&++attempts===1)throw Object.assign(new Error('源工作流已更新，请读取当前修订后再复制'),{status:409})
    return normal(path,options)
  })
  render(<ProjectSpace {...callbacks}/>);await openCopy();submit()
  await screen.findByRole('alert')
  workflows=[{...source,revision:8},other]
  expect(screen.getByRole('button',{name:'创建副本并编辑'})).toBeDisabled()
  submit();expect(copyCalls()).toHaveLength(1)
  fireEvent.click(screen.getByRole('button',{name:'刷新来源版本'}))
  await screen.findByText('来源：部门质量分析 · 版本 8')
  expect(copyCalls()).toHaveLength(1)
  expect(callbacks.onWorkflow).not.toHaveBeenCalled()
  submit();await waitFor(()=>expect(callbacks.onWorkflow).toHaveBeenCalledWith('copy'))
  expect(body(0).arguments.expected_revision).toBe(7)
  expect(body(1).arguments.expected_revision).toBe(8)
  expect(body(1).arguments.request_key).not.toBe(body(0).arguments.request_key)
})

it('keeps a removed or unreadable source blocked instead of silently copying another version',async()=>{
  const normal=vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async(path,options)=>{if(options?.method==='POST')throw Object.assign(new Error('源工作流已更新'),{status:409});return normal(path,options)})
  render(<ProjectSpace {...props()}/>);await openCopy();submit();await screen.findByRole('alert')
  workflows=[other]
  fireEvent.click(screen.getByRole('button',{name:'刷新来源版本'}))
  await waitFor(()=>expect(screen.getByRole('alert')).toHaveTextContent('来源工作流已不在本项目'))
  expect(screen.getByRole('button',{name:'创建副本并编辑'})).toBeDisabled()
  submit();expect(copyCalls()).toHaveLength(1)
})

it.each(['project','account'])('ignores a pending copy response after switching %s',async scope=>{
  const pending=deferred<{workflow_id:string}>(),normal=vi.mocked(api).getMockImplementation()!,callbacks=props()
  vi.mocked(api).mockImplementation((path,options)=>options?.method==='POST'?pending.promise as never:normal(path,options))
  const view=render(<ProjectSpace {...callbacks}/>);await openCopy();submit()
  if(scope==='account')account.id='bob'
  view.rerender(<ProjectSpace {...callbacks} projectId={scope==='project'?'other-project':'p'}/>)
  await screen.findByText(source.display_name)
  const callsBefore=vi.mocked(api).mock.calls.length
  await act(async()=>{pending.resolve({workflow_id:'old-copy'})})
  expect(vi.mocked(api).mock.calls).toHaveLength(callsBefore)
  expect(callbacks.onWorkflow).not.toHaveBeenCalled();expect(callbacks.onChanged).not.toHaveBeenCalled();expect(mark).not.toHaveBeenCalled()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

it('does not navigate back when the project changes during the refresh after a successful copy',async()=>{
  const pending=deferred<ReturnType<typeof space>>(),normal=vi.mocked(api).getMockImplementation()!,callbacks=props()
  let reads=0
  vi.mocked(api).mockImplementation((path,options)=>path==='/api/v1/projects/p/space'&&++reads===2?pending.promise as never:normal(path,options))
  const view=render(<ProjectSpace {...callbacks}/>);await openCopy();submit()
  await waitFor(()=>expect(reads).toBe(2))
  view.rerender(<ProjectSpace {...callbacks} projectId="other-project"/>)
  await screen.findByText(source.display_name)
  await act(async()=>{pending.resolve(space())})
  expect(callbacks.onWorkflow).not.toHaveBeenCalled();expect(callbacks.onChanged).not.toHaveBeenCalled()
})
