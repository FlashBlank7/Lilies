import { Suspense } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import Studio from '@/app/applications/[id]/page'
import { api } from '@/lib/platform'
import ProjectWorkflowChecks from '@/app/components/ProjectWorkflowChecks'

vi.mock('next/navigation',()=>({useRouter:()=>({push:vi.fn(),replace:vi.fn()})}))
vi.mock('@/lib/platform',async original=>({...await original<object>(),api:vi.fn(),getClientToken:()=>''}))
vi.mock('@xyflow/react',async original=>({...await original<object>(),ReactFlow:()=>null}))
vi.mock('@/app/applications/[id]/block-catalog-panel',()=>({BlockCatalogPanel:()=>null,BlockInstanceDetails:()=>null,BlockPurpose:()=>null,UndefinedBusinessWorkflowNotice:()=>null}))
afterEach(()=>{cleanup();vi.mocked(api).mockReset()})

it('runs optional project tests and shows failures without launching an AI repair or blocking execution',async()=>{
  const draft={application_id:'w',revision:1,content_hash:'one',validation_report:{},snapshot:{name:'未绑定预测',description:'',requirement:'',mode:'workflow',agents:{},tests:[{id:'case',name:'输入示例',inputs:{score:10},assertions:[]}],workflow:{nodes:[],edges:[]}}}
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path.endsWith('/project')) return {project_id:'p'} as never
    if(path==='/api/v1/projects/p') return {id:'p',name:'项目',members:[]} as never
    if(path.endsWith('/draft')) return draft as never
    if(path.endsWith('/tests/run')) return {passed:false,tests:[{test_id:'case',name:'输入示例',passed:false,run_error:'模型资源尚未绑定',outputs:{}}]} as never
    if(options?.method==='POST') throw new Error('Unexpected write: '+path)
    return [] as never
  })
  await act(async()=>{render(<Suspense><Studio params={Promise.resolve({id:'w'})}/></Suspense>)})
  const run=screen.getByRole('link',{name:'运行工作流'})
  expect(run).toHaveAttribute('href','/projects/p?run=w')
  expect(screen.queryByText('待验收')).not.toBeInTheDocument()
  expect(screen.queryByRole('button',{name:'发布版本'})).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'测试'}))
  expect(vi.mocked(api).mock.calls.filter(([,options])=>options?.method==='POST')).toHaveLength(0)
  fireEvent.click(screen.getByRole('button',{name:'运行已保存的测试'}))
  await screen.findByText('模型资源尚未绑定',{exact:true})
  expect(screen.getByRole('link',{name:'填写输入并运行工作流'})).toHaveAttribute('href','/projects/p?run=w')
  await waitFor(()=>expect(screen.getByRole('button',{name:'运行已保存的测试'})).toBeEnabled())
  expect(vi.mocked(api).mock.calls.filter(([,options])=>options?.method==='POST').map(([path])=>path)).toEqual(['/api/v1/projects/p/members/w/tests/run'])
  expect(vi.mocked(api).mock.calls.some(([path])=>path.includes('repair')||path.endsWith('/builds'))).toBe(false)
})

it('shows failed output comparisons including null and missing values without requiring raw JSON inspection',()=>{
  render(<ProjectWorkflowChecks projectId="p" workflowId="w" cases={[{
    id:'case',name:'输出检查',requirement:'',inputs:{},result:{passed:false,assertions:[
      {passed:false,path:['code'],operator:'equals',expected:'010',actual:'001'},
      {passed:false,path:['optional'],operator:'equals',expected:null},
      {passed:true,path:['value'],operator:'equals',expected:100,actual:100},
    ]},
  }]} report={null} running={false} dirty={false} onRun={()=>{}} />)
  expect(screen.getByText('"010"')).toBeInTheDocument()
  expect(screen.getByText('"001"')).toBeInTheDocument()
  expect(screen.getByText('null')).toBeInTheDocument()
  expect(screen.getByText('未产生')).toBeInTheDocument()
  expect(screen.queryByText('value')).not.toBeInTheDocument()
  expect(screen.getByRole('button',{name:'运行已保存的测试'})).toBeEnabled()
})
