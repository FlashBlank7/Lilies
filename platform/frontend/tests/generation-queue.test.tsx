import {act,cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {useWorkflowGeneration} from '@/lib/use-workflow-generation'
import {api} from '@/lib/platform'
vi.mock('@/lib/platform',()=>({api:vi.fn()}))
const {identity}=vi.hoisted(()=>({identity:{id:'u'}}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>identity}))
const sharedKey='u:project:p:test:pending-generation'
const pending={job_id:'j',submitted:'Earlier',request_key:'key',signature:'{}',started_at:Date.now()-65000}
const locksDescriptor=Object.getOwnPropertyDescriptor(navigator,'locks')
beforeEach(()=>{vi.mocked(api).mockReset();sessionStorage.clear();localStorage.clear();identity.id='u'})
afterEach(()=>{cleanup();vi.restoreAllMocks();Object.defineProperty(navigator,'locks',locksDescriptor||{value:undefined,configurable:true})})
function Example({saved,scope='test',instruction='Create',project='p'}:{saved:(r:{workflow_id:string},s:string)=>void;scope?:string;instruction?:string;project?:string}){const job=useWorkflowGeneration<{workflow_id:string}>(project,scope,saved);return <><input aria-label="本地草稿" defaultValue={instruction}/><button disabled={job.busy} onClick={()=>void job.start(`/api/v1/projects/${project}/workflow-generation`,{instruction},instruction)}>生成</button><p>{job.status}</p>{job.jobId&&<button onClick={()=>void job.stop()}>停止生成</button>}<p>{job.error}</p></>}
it('accepts a queued job and retrieves the saved workflow without submitting again',async()=>{
  vi.mocked(api).mockImplementation(async(path)=>path.endsWith('/workflow-generation')?{job_id:'j',status:'queued'} as never:{job_id:'j',status:'completed',result:{workflow_id:'w'}} as never)
  const saved=vi.fn();render(<Example saved={saved}/>);fireEvent.click(screen.getByText('生成'))
  await waitFor(()=>expect(saved).toHaveBeenCalledWith({workflow_id:'w'},'Create'))
  expect(vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST')).toHaveLength(1)
})
it('continues a persisted request after remount without spending another generation',async()=>{
  sessionStorage.setItem('u:test:pending-generation',JSON.stringify({job_id:'j',submitted:'Earlier',request_key:'key',signature:'{}'}))
  vi.mocked(api).mockResolvedValue({job_id:'j',status:'completed',result:{workflow_id:'old'}} as never)
  const saved=vi.fn();render(<Example saved={saved}/>)
  await waitFor(()=>expect(saved).toHaveBeenCalledWith({workflow_id:'old'},'Earlier'))
  expect(vi.mocked(api).mock.calls.every(([,o])=>!o?.method)).toBe(true)
})

it('does not apply an old workflow result after switching the active workflow',async()=>{
  sessionStorage.setItem('u:first:pending-generation',JSON.stringify({job_id:'old',submitted:'Earlier',request_key:'key',signature:'{}'}))
  let resolve!: (value:never)=>void
  vi.mocked(api).mockImplementation(()=>new Promise(done=>{resolve=done}))
  const saved=vi.fn();const view=render(<Example saved={saved} scope="first"/>)
  await waitFor(()=>expect(api).toHaveBeenCalledOnce())
  view.rerender(<Example saved={saved} scope="second"/>)
  resolve({job_id:'old',status:'completed',result:{workflow_id:'old-workflow'}} as never)
  await waitFor(()=>expect(screen.getByText('生成')).toBeEnabled())
  expect(saved).not.toHaveBeenCalled()
  expect(sessionStorage.getItem('u:first:pending-generation')).toContain('old')
})

it('shows the existing job in a new tab and completes both views without changing their drafts',async()=>{
  const finish:((value:never)=>void)[]=[]
  vi.mocked(api).mockImplementation((path)=>path.endsWith('/workflow-generation')?Promise.resolve({job_id:'j',status:'queued'} as never):new Promise(resolve=>finish.push(resolve)))
  const first=vi.fn(),second=vi.fn()
  const a=render(<Example saved={first}/>);fireEvent.click(within(a.container).getByText('生成'))
  await waitFor(()=>expect(finish).toHaveLength(1))
  // A new tab has no original tab's session data, only shared browser storage.
  sessionStorage.clear()
  const b=render(<Example saved={second} instruction="未提交的新草稿"/>)
  await waitFor(()=>expect(finish).toHaveLength(2))
  expect(within(b.container).getByText('生成')).toBeDisabled()
  expect(within(b.container).getByText(/已等待/)).toBeInTheDocument()
  expect(vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST')).toHaveLength(1)
  await act(async()=>{finish.forEach(resolve=>resolve({job_id:'j',status:'completed',result:{workflow_id:'w'}} as never))})
  expect(first).toHaveBeenCalledWith({workflow_id:'w'},'Create')
  expect(second).toHaveBeenCalledWith({workflow_id:'w'},'Create')
  expect(within(b.container).getByLabelText('本地草稿')).toHaveValue('未提交的新草稿')
  expect(localStorage.getItem(sharedKey)).toBe('null')
  expect(sessionStorage.getItem('u:test:pending-generation')).toBeNull()
})

it('follows a storage event and shows time already spent waiting',async()=>{
  let finish!:(value:never)=>void
  vi.mocked(api).mockImplementation(()=>new Promise(resolve=>{finish=resolve}))
  const saved=vi.fn();render(<Example saved={saved}/>)
  const value=JSON.stringify({...pending,started_at:Date.now()-65000})
  localStorage.setItem(sharedKey,value)
  act(()=>window.dispatchEvent(new StorageEvent('storage',{key:sharedKey,newValue:value})))
  await waitFor(()=>expect(api).toHaveBeenCalledWith('/api/v1/projects/p/generation-jobs/j'))
  expect(screen.getByText(/已等待 1 分/)).toBeInTheDocument()
  expect(screen.getByText('生成')).toBeDisabled()
  await act(async()=>finish({job_id:'j',status:'error',error:'请配置模型'} as never))
  expect(screen.getByText(/请配置模型/)).toBeInTheDocument()
  expect(screen.getByText('生成')).toBeEnabled()
  expect(saved).not.toHaveBeenCalled()
  expect(localStorage.getItem(sharedKey)).toBe('null')
})

it('serializes simultaneous submissions only until the shared job id is saved',async()=>{
  let tail=Promise.resolve()
  const request=vi.fn((_key:string,submit:()=>Promise<void>)=>{const next=tail.then(submit);tail=next.catch(()=>{});return next})
  Object.defineProperty(navigator,'locks',{value:{request},configurable:true})
  let accept!:(value:never)=>void
  const finish:((value:never)=>void)[]=[]
  vi.mocked(api).mockImplementation(path=>path.endsWith('/workflow-generation')?new Promise(resolve=>{accept=resolve}):new Promise(resolve=>finish.push(resolve)))
  const first=render(<Example saved={vi.fn()}/>),second=render(<Example saved={vi.fn()}/>)
  fireEvent.click(within(first.container).getByText('生成'))
  fireEvent.click(within(second.container).getByText('生成'))
  await waitFor(()=>expect(api).toHaveBeenCalledTimes(1))
  await act(async()=>accept({job_id:'j',status:'queued'} as never))
  await waitFor(()=>expect(finish).toHaveLength(2))
  expect(request).toHaveBeenCalledTimes(2)
  expect(vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST')).toHaveLength(1)
  await act(async()=>finish.forEach(resolve=>resolve({job_id:'j',status:'completed',result:{workflow_id:'w'}} as never)))
})

it('keeps account and project scopes separate and ignores late results after account changes',async()=>{
  localStorage.setItem(sharedKey,JSON.stringify(pending))
  let finish!:(value:never)=>void
  vi.mocked(api).mockImplementation(()=>new Promise(resolve=>{finish=resolve}))
  const saved=vi.fn(),view=render(<Example saved={saved}/>)
  await waitFor(()=>expect(api).toHaveBeenCalledOnce())
  identity.id='other';view.rerender(<Example saved={saved}/>)
  expect(screen.getByText('生成')).toBeEnabled()
  expect(screen.queryByText(/已等待/)).not.toBeInTheDocument()
  await act(async()=>finish({job_id:'j',status:'completed',result:{workflow_id:'old'}} as never))
  expect(saved).not.toHaveBeenCalled()
  expect(localStorage.getItem(sharedKey)).toContain('Earlier')
  identity.id='u';view.rerender(<Example saved={saved} project="other-project"/>)
  expect(screen.getByText('生成')).toBeEnabled()
  expect(api).toHaveBeenCalledOnce()
})

it('stops a restored shared job and clears pending state without applying a result',async()=>{
  localStorage.setItem(sharedKey,JSON.stringify(pending))
  let stopped=false
  vi.mocked(api).mockImplementation(async path=>{
    if(path.endsWith('/stop')){stopped=true;return {} as never}
    return {job_id:'j',status:stopped?'interrupted':'running'} as never
  })
  const saved=vi.fn();render(<Example saved={saved}/>)
  fireEvent.click(await screen.findByText('停止生成'))
  await waitFor(()=>expect(screen.getByText(/生成已停止/)).toBeInTheDocument(),{timeout:2500})
  expect(vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST').map(([path])=>path)).toEqual(['/api/v1/projects/p/generation-jobs/j/stop'])
  expect(screen.getByText('生成')).toBeEnabled()
  expect(saved).not.toHaveBeenCalled()
  expect(localStorage.getItem(sharedKey)).toBe('null')
})

it('falls back to legacy session storage when browser shared storage is blocked',async()=>{
  sessionStorage.setItem('u:test:pending-generation',JSON.stringify(pending))
  vi.spyOn(window,'localStorage','get').mockImplementation(()=>{throw new Error('Storage blocked')})
  vi.mocked(api).mockResolvedValue({job_id:'j',status:'completed',result:{workflow_id:'w'}} as never)
  const saved=vi.fn();render(<Example saved={saved}/>)
  await waitFor(()=>expect(saved).toHaveBeenCalledWith({workflow_id:'w'},'Earlier'))
  expect(vi.mocked(api).mock.calls.every(([,o])=>!o?.method)).toBe(true)
  expect(sessionStorage.getItem('u:test:pending-generation')).toBeNull()
})

it('retries the same failed body with its key and allows a corrected body to use a new key',async()=>{
  vi.mocked(api).mockRejectedValue(new Error('参数需要修正'))
  const view=render(<Example saved={vi.fn()}/>)
  fireEvent.click(screen.getByText('生成'))
  await screen.findByText(/参数需要修正/)
  fireEvent.click(screen.getByText('生成'))
  await waitFor(()=>expect(api).toHaveBeenCalledTimes(2))
  await waitFor(()=>expect(screen.getByText('生成')).toBeEnabled())
  view.rerender(<Example saved={vi.fn()} instruction="Corrected"/>)
  fireEvent.click(screen.getByText('生成'))
  await waitFor(()=>expect(api).toHaveBeenCalledTimes(3))
  const bodies=vi.mocked(api).mock.calls.map(([,o])=>JSON.parse(String(o?.body)))
  expect(bodies[0].request_key).toBe(bodies[1].request_key)
  expect(bodies[2].request_key).not.toBe(bodies[1].request_key)
  expect(bodies[2].instruction).toBe('Corrected')
})
