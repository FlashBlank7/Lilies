import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {useWorkflowGeneration} from '@/lib/use-workflow-generation'
import {api} from '@/lib/platform'
vi.mock('@/lib/platform',()=>({api:vi.fn()}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>({id:'u'})}))
beforeEach(()=>{vi.mocked(api).mockReset();sessionStorage.clear()})
afterEach(cleanup)
function Example({saved,scope='test'}:{saved:(r:{workflow_id:string},s:string)=>void;scope?:string}){const job=useWorkflowGeneration<{workflow_id:string}>('p',scope,saved);return <><button disabled={job.busy} onClick={()=>void job.start('/api/v1/projects/p/workflow-generation',{instruction:'Create'},'Create')}>生成</button><p>{job.status}</p>{job.jobId&&<button onClick={()=>void job.stop()}>停止生成</button>}<p>{job.error}</p></>}
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
