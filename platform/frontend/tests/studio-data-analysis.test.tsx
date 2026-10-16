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

it('saves nested training preprocessing options while preserving the selected feature columns', async () => {
  const config = {dataset_id:'dataset', features:{columns:['pressure'], preprocessing:{numeric_missing:'auto',outliers:'none'}}}
  const draft = {application_id:'p',revision:1,content_hash:'one',validation_report:{},snapshot:{name:'Training',description:'',requirement:'',mode:'workflow',tests:[],agents:{},workflow:{nodes:[{id:'features',title:'特征提取',type:'feature_extract',position:{x:0,y:0},config}],edges:[]}}}
  const saved: unknown[]=[]
  vi.mocked(api).mockImplementation(async (path,options)=>{
    if(path.endsWith('/draft') && options?.method==='POST'){
      saved.push(JSON.parse(options.body as string).data.changes.config)
      return draft as never
    }
    if(path.endsWith('/draft')) return draft as never
    if(path.startsWith('/api/v1/blocks?application_id=')) return [{type:'feature_extract',title:'特征提取',category:'integration',input_ports:[],output_ports:[],config_schema:{},editor:{fields:[
      {path:'features.preprocessing.numeric_missing',label:'训练：数值缺失值处理',control:'enum',options:['auto','median','mean','constant','error'],default_value:'auto'},
      {path:'features.preprocessing.fill_value',label:'训练：缺失填补固定值',control:'number',default_value:0,visible_when:{'features.preprocessing.numeric_missing':['constant']}},
      {path:'features.preprocessing.scaling',label:'训练：数值缩放',control:'enum',options:['auto','none','standard','robust'],default_value:'auto'},
      {path:'features.preprocessing.outliers',label:'训练：异常值处理',control:'enum',options:['none','iqr_clip'],default_value:'none'},
      {path:'features.preprocessing.outlier_columns',label:'训练：截断字段',control:'string_list',visible_when:{'features.preprocessing.outliers':['iqr_clip']}},
      {path:'features.preprocessing.iqr_factor',label:'训练：IQR 倍数',control:'number',default_value:1.5,visible_when:{'features.preprocessing.outliers':['iqr_clip']}},
    ]}}] as never
    if(path.endsWith('/project')) return {project_id:null} as never
    if(path==='/health') return {status:'ok'} as never
    return [] as never
  })
  const params=Promise.resolve({id:'p'})
  const {container}=await act(async()=>render(<Suspense><Studio params={params}/></Suspense>))
  fireEvent.click(await screen.findByRole('button',{name:'Select features'}))
  expect(screen.queryByRole('spinbutton',{name:'训练：缺失填补固定值'})).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox',{name:'训练：截断字段'})).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('combobox',{name:'训练：数值缺失值处理'}),{target:{value:'constant'}})
  fireEvent.change(screen.getByRole('spinbutton',{name:'训练：缺失填补固定值'}),{target:{value:'-9'}})
  fireEvent.change(screen.getByRole('combobox',{name:'训练：数值缩放'}),{target:{value:'robust'}})
  fireEvent.change(screen.getByRole('combobox',{name:'训练：异常值处理'}),{target:{value:'iqr_clip'}})
  fireEvent.change(screen.getByRole('textbox',{name:'训练：截断字段'}),{target:{value:'pressure'}})
  fireEvent.change(screen.getByRole('spinbutton',{name:'训练：IQR 倍数'}),{target:{value:'2.5'}})
  fireEvent.click(container.querySelector('[data-config-editor-action="save"]')!)
  await waitFor(()=>expect(saved).toHaveLength(1))
  expect(saved[0]).toEqual({dataset_id:'dataset',features:{columns:['pressure'],preprocessing:{numeric_missing:'constant',fill_value:-9,scaling:'robust',outliers:'iqr_clip',outlier_columns:['pressure'],iqr_factor:2.5}}})
})

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
