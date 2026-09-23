import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import AssistantSourcePanel from '@/app/components/AssistantSourcePanel'
import OfficialAgentPage from '@/app/official-agent/page'
vi.mock('@/lib/platform',()=>({api:vi.fn()}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>({id:'admin',role:'admin'})}))
afterEach(cleanup)
beforeEach(()=>{vi.mocked(api).mockReset()})
it('saves explicit project permission and source without changing API credentials',async()=>{
  vi.mocked(api).mockImplementation(async(path)=>path.endsWith('/assistant-source')?{allowed:false,task:'api',generation:'inherit'} as never:{agent_modules_enabled:false} as never)
  const saved=vi.fn().mockResolvedValue(undefined)
  render(<AssistantSourcePanel base="/api/v1/projects/p" running={false} onSaved={saved}/> )
  fireEvent.click(screen.getByRole('button',{name:'智能体来源'}))
  fireEvent.click(await screen.findByLabelText('允许项目使用完整智能体能力'))
  fireEvent.click(screen.getByLabelText('授权此项目使用官方智能体'))
  fireEvent.change(screen.getByLabelText('任务助手'),{target:{value:'official'}})
  fireEvent.click(screen.getByRole('button',{name:'保存智能体设置'}))
  await waitFor(()=>expect(saved).toHaveBeenCalledOnce())
  expect(api).toHaveBeenCalledWith('/api/v1/projects/p/assistant-source',{method:'PUT',body:JSON.stringify({allowed:true,task:'official',generation:'inherit'})})
  expect(vi.mocked(api).mock.calls.some(([p])=>p.includes('/agent-session'))).toBe(false)
})
it('does not start login or model calls on opening admin page and shows unknown usage',async()=>{
  vi.mocked(api).mockImplementation(async path=>path.endsWith('/admin/usage')?{active_users:0,features:[],feedback:[],notes:[]} as never:{config:{enabled:false,executable:'codex',version:'',model:'gpt-5.6-luna',thinking:'max',reserve_percent:50,concurrency:1,max_tokens:32000,max_seconds:600},jobs:[]} as never)
  render(<OfficialAgentPage/> )
  expect(await screen.findByLabelText('个人保留额度（%）')).toHaveValue(50)
  expect(screen.getByText('账号剩余额度：未知')).toBeInTheDocument()
  expect(api).toHaveBeenCalledTimes(2)
  expect(api).toHaveBeenCalledWith('/api/v1/admin/official-agent')
})
