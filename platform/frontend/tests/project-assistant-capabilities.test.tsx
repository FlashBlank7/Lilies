import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectSpace from '@/app/components/ProjectSpace'
import Settings from '@/app/admin/project-assistants/page'
const {account}=vi.hoisted(()=>({account:{id:'owner',role:'member'}}))
vi.mock('@/lib/platform',()=>({api:vi.fn()}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>account}))
vi.mock('@/app/components/Onboarding',()=>({useOnboarding:()=>({active:false,mark:vi.fn()})}))
vi.mock('@/app/components/ProjectMaterials',()=>({default:()=>null}))
vi.mock('@/app/components/SharedMethods',()=>({default:()=>null}))
afterEach(cleanup)
beforeEach(()=>{vi.mocked(api).mockReset();account.role='member'})
it('a human adds and removes an assistant-built draft before the assistant can call it',async()=>{
 let enabled=false
 vi.mocked(api).mockImplementation(async(path,options)=>{
  if(path.endsWith('/assistant-callable')){enabled=JSON.parse(options!.body as string).enabled;return {} as never}
  if(path.endsWith('/space'))return {workflows:[{id:'w',name:'分析草稿',revision:1,node_count:2,inputs:[],allowed:true,assistant_callable:enabled}],files:[]} as never
  return [] as never
 })
 const onTalk=vi.fn()
 render(<ProjectSpace projectId="p" onWorkflow={vi.fn()} onFile={vi.fn()} onTalk={onTalk} onChanged={vi.fn()}/> )
 const row=(await screen.findByText('分析草稿')).closest('tr')!
 expect(within(row).getByRole('button',{name:'让智能体调用'})).toBeDisabled()
 fireEvent.click(within(row).getByRole('button',{name:'加入助手可调用能力'}))
 await waitFor(()=>expect(within(row).getByRole('button',{name:'让智能体调用'})).toBeEnabled())
 fireEvent.click(within(row).getByRole('button',{name:'让智能体调用'}))
 expect(onTalk).toHaveBeenCalledOnce()
 fireEvent.click(within(row).getByRole('button',{name:'从助手可调用能力移除'}))
 await waitFor(()=>expect(within(row).getByRole('button',{name:'让智能体调用'})).toBeDisabled())
})
it('ordinary users cannot open administrator assistant settings',()=>{
 render(<Settings/>);expect(screen.getByText('此页面仅供平台管理员使用。')).toBeVisible();expect(api).not.toHaveBeenCalled()
})
