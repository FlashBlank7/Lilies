import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import OfficialConnectionStatus from '@/app/components/OfficialConnectionStatus'
import WorkflowReadiness from '@/app/components/WorkflowReadiness'
import {ProjectTaskOutput} from '@/app/components/ProjectRunPanel'
import ProjectSpace from '@/app/components/ProjectSpace'
import {api} from '@/lib/platform'
let role='member'
vi.mock('@/lib/platform',()=>({api:vi.fn(),withFrontendToken:(v:string)=>v}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>({id:'employee',role})}))
vi.mock('@/app/components/Onboarding',()=>({useOnboarding:()=>({mark:vi.fn()})}))
vi.mock('@/app/components/ProjectMaterials',()=>({default:()=> <p>上传资料入口</p>}))
vi.mock('@/app/components/SharedMethods',()=>({default:()=> <p>共享方法</p>}))
beforeEach(()=>{role='member';vi.mocked(api).mockReset()})
afterEach(cleanup)
it('keeps a blocked connection visible to employees and offers the admin repair page only to admins',()=>{
 const state={connection_status:'blocked' as const,connection_message:'认证失败，请重新连接'}
 const {rerender}=render(<OfficialConnectionStatus connection={state}/> )
 expect(screen.getByText(/请联系管理员检查/)).toBeVisible()
 expect(screen.queryByRole('link')).not.toBeInTheDocument()
 role='admin';rerender(<OfficialConnectionStatus connection={state}/> )
 expect(screen.getByRole('link',{name:'检查与修复官方智能体连接'})).toHaveAttribute('href','/official-agent')
 expect(screen.queryByText(/已连接/)).not.toBeInTheDocument()
 rerender(<OfficialConnectionStatus connection={{}}/> )
 expect(screen.getByText(/尚未检查登录状态/)).toBeVisible()
})
it('explains resource requirements without executing anything and exposes settings',()=>{
 const settings=vi.fn()
 render(<WorkflowReadiness projectId="p" onSettings={settings} value={{status:'needs_setup',note:'创建和编辑不受影响',issues:[{code:'model',message:'工作流大模型尚未配置',setup:'settings',node:'整理纪要'}]}}/> )
 expect(screen.getByText(/工作流大模型尚未配置/)).toBeVisible()
 fireEvent.click(screen.getByRole('button',{name:'查看项目设置'}))
 expect(settings).toHaveBeenCalledOnce();expect(api).not.toHaveBeenCalled()
})
it('shows duplicate records beyond the ordinary preview and keeps totals unchanged',()=>{
 render(<ProjectTaskOutput projectId="p" task={{id:'t',status:'succeeded',outputs:{result:{suspected_duplicates:1,duplicate_records:[{date:'2026-09-01',merchant:'示例商户',amount:'24.00',currency:'CNY',source_file:'requirement-package/bill.csv',source_row:27}],preview:[]}}} as never}/>)
 expect(screen.getByRole('heading',{name:'需要复核：1 条疑似重复费用'})).toBeVisible()
 expect(screen.getByRole('cell',{name:'示例商户'})).toBeVisible()
 expect(screen.getByRole('cell',{name:'bill.csv · 第 27 行'})).toBeVisible()
 expect(screen.getByText(/汇总金额仍包含这些记录/)).toBeVisible()
})
it('puts project workflows and files before the public catalog and keeps paths in details',async()=>{
 vi.mocked(api).mockImplementation(async path => path.endsWith('/official-workflows')?[{id:'template',name:'公共模板',description:'示例'}] as never:{workflows:[{id:'w',name:'我的流程',description:'处理资料',revision:3,node_count:2,allowed:true,inputs:[]}],files:[{path:'requirement-package/unique/input.csv',size:15}],files_truncated:false} as never)
 render(<ProjectSpace projectId="p" onWorkflow={vi.fn()} onFile={vi.fn()} onTalk={vi.fn()} onChanged={vi.fn()}/> )
 await screen.findByText('我的流程');await screen.findByText('input.csv')
 const headings=screen.getAllByRole('heading',{level:2}).map(v=>v.textContent)
 expect(headings.indexOf('可供调用的工作流')).toBeLessThan(headings.indexOf('公共工作流市场'))
 expect(headings.indexOf('待处理文件')).toBeLessThan(headings.indexOf('公共工作流市场'))
 expect(screen.getByText('requirement-package/unique/input.csv')).not.toBeVisible()
 fireEvent.click(screen.getByText('文件位置'))
 await waitFor(()=>expect(screen.getByText('requirement-package/unique/input.csv')).toBeVisible())
 expect(vi.mocked(api).mock.calls.every(([,options])=>!options?.method)).toBe(true)
})
