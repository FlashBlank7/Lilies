import { Suspense } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import Studio from '@/app/applications/[id]/page'
import { api } from '@/lib/platform'

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }))
vi.mock('@/lib/platform', async importOriginal => ({ ...await importOriginal<object>(), api: vi.fn(), getClientToken: () => '' }))
vi.mock('@xyflow/react', async importOriginal => ({
  ...await importOriginal<object>(),
  ReactFlow: ({ nodes, onNodeClick }: { nodes: { id: string }[]; onNodeClick: (event: unknown, node: { id: string }) => void }) => <>{nodes.map(node => <button key={node.id} onClick={event => onNodeClick(event, node)}>Select {node.id}</button>)}</>,
}))
vi.mock('@/app/applications/[id]/block-catalog-panel', () => ({ BlockCatalogPanel: () => null, BlockInstanceDetails: () => null, BlockPurpose: () => null, UndefinedBusinessWorkflowNotice: () => null }))
afterEach(() => { cleanup(); vi.mocked(api).mockReset() })

it('edits a concrete analysis operation and retains hidden settings when switching operations', async () => {
  const config = {dataset_id:'dataset', analysis:{operation:'outliers', columns:['temperature'], lower:0}}
  const draft = {application_id:'p', revision:1, content_hash:'one', validation_report:{},
    snapshot:{name:'Analysis', description:'', requirement:'', mode:'workflow', tests:[], agents:{}, workflow:{
      nodes:[{id:'stats',title:'数据分析',type:'data_analysis',position:{x:0,y:0},config}],edges:[]}}}
  const saved: unknown[]=[]
  vi.mocked(api).mockImplementation(async (path,options)=>{
    if(path.endsWith('/draft') && options?.method==='POST'){
      saved.push(JSON.parse(options.body as string).data.changes.config)
      return draft as never
    }
    if(path.endsWith('/draft')) return draft as never
    if(path.startsWith('/api/v1/blocks?application_id=')) return [{type:'data_analysis',title:'数据分析',category:'integration',input_ports:[],output_ports:[],config_schema:{},editor:{fields:[
      {path:'analysis.operation',label:'分析操作',control:'enum',options:['outliers','spearman','numeric'],option_labels:{outliers:'异常值处理',spearman:'Spearman 相关分析',numeric:'数值整理'}},
      {path:'analysis.columns',label:'分析列',control:'string_list'},
      {path:'analysis.lower',label:'固定下限',control:'number',visible_when:{'analysis.operation':['outliers']}},
      {path:'analysis.missing',label:'缺失值处理',control:'enum',options:['pairwise','raise'],default_value:'pairwise',visible_when:{'analysis.operation':['spearman']}},
      {path:'analysis.numeric.missing_tokens',label:'缺失占位符',control:'string_list',visible_when:{'analysis.operation':['numeric']}},
      {path:'analysis.numeric.intervals',label:'区间值处理',control:'enum',options:['reject','midpoint'],default_value:'reject',visible_when:{'analysis.operation':['numeric']}},
    ]}}] as never
    if(path.endsWith('/project')) return {project_id:null} as never
    if(path==='/health') return {status:'ok'} as never
    return [] as never
  })
  const params=Promise.resolve({id:'p'})
  const {container}=await act(async()=>render(<Suspense><Studio params={params}/></Suspense>))
  fireEvent.click(await screen.findByRole('button',{name:'Select stats'}))
  expect(screen.getByRole('spinbutton',{name:'固定下限'})).toHaveValue(0)
  expect(screen.queryByRole('combobox',{name:'缺失值处理'})).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('combobox',{name:'分析操作'}),{target:{value:'spearman'}})
  expect(screen.queryByRole('spinbutton',{name:'固定下限'})).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox',{name:'分析列'}),{target:{value:'temperature\nquality'}})
  fireEvent.change(screen.getByRole('combobox',{name:'缺失值处理'}),{target:{value:'raise'}})
  expect(screen.queryByRole('textbox',{name:'缺失占位符'})).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('combobox',{name:'分析操作'}),{target:{value:'numeric'}})
  expect(screen.queryByRole('combobox',{name:'缺失值处理'})).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox',{name:'缺失占位符'}),{target:{value:'/\nNA'}})
  fireEvent.change(screen.getByRole('combobox',{name:'区间值处理'}),{target:{value:'midpoint'}})
  fireEvent.click(container.querySelector('[data-config-editor-action="save"]')!)
  await waitFor(()=>expect(saved).toHaveLength(1))
  expect(saved[0]).toEqual({dataset_id:'dataset',analysis:{operation:'numeric',columns:['temperature','quality'],lower:0,missing:'raise',numeric:{missing_tokens:['/','NA'],intervals:'midpoint'}}})
})
