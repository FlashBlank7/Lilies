import { Suspense } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import Studio from '@/app/applications/[id]/page'
import { api } from '@/lib/platform'
import { workflowAtPath, type WorkflowGraph } from '@/lib/workflow-scope'

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }))
vi.mock('@/lib/platform', async original => ({ ...await original<object>(), api: vi.fn(), getClientToken: () => '' }))
vi.mock('@xyflow/react', async original => ({
  ...await original<object>(),
  ReactFlow: ({ nodes, edges, onNodeClick, onEdgeClick }: {
    nodes: {id:string}[]; edges:{id:string}[];
    onNodeClick:(event:unknown,node:{id:string})=>void; onEdgeClick:(event:unknown,edge:{id:string})=>void;
  }) => <>{nodes.map(node=><button key={node.id} onClick={event=>onNodeClick(event,node)}>Select {node.id}</button>)}
    {edges.map(edge=><button key={edge.id} onClick={event=>onEdgeClick(event,edge)}>Edge {edge.id}</button>)}</>,
}))
vi.mock('@/app/applications/[id]/block-catalog-panel', () => ({ BlockCatalogPanel: () => null, BlockInstanceDetails: () => null, BlockPurpose: () => null, UndefinedBusinessWorkflowNotice: () => null }))
afterEach(()=>{cleanup();vi.mocked(api).mockReset()})

const end=(value:unknown)=>({id:'same',title:'结果',type:'end',position:{x:400,y:80},config:{outputs:{message:value}}})
async function setup(humanAnswers=false) {
  const human={id:'review',title:'人工复核',type:'human_input',position:{x:220,y:80},config:{title:'填写复核结果',fields:[
    {name:'actual_defects',label:'实际缺陷',type:'string',required:true},
    {name:'review_notes',label:'复核说明',type:'string',required:true},
  ]}}
  const inner={nodes:[{id:'start',title:'输入',type:'start',config:{inputs:[]},position:{x:40,y:80}},...(humanAnswers?[human]:[]),
    end(humanAnswers?{$ref:{node_id:'review',path:['output','actual_defects']}}:'inner')],
    edges:humanAnswers?[
      {id:'start-review',source:'start',target:'review',source_port:'output',target_port:'input'},
      {id:'review-result',source:'review',target:'same',source_port:'output',target_port:'input'},
    ]:[{id:'e',source:'start',target:'same',source_port:'output',target_port:'input'}]}
  const container=(id:string,workflow:unknown)=>({id,type:'iteration',title:id,position:{x:40,y:80},config:{items:[1],item_name:'part',variables:{},parallelism:1,output_node_id:'same',output_path:[],workflow}})
  const draft={application_id:'p',revision:1,content_hash:'one',validation_report:{},snapshot:{name:'Nested',description:'',requirement:'',mode:'workflow',tests:[],agents:{},workflow:{nodes:[container('outer',{nodes:[container('inner',inner),end('middle')],edges:[]}),end('outer')],edges:[]}}}
  const saved: {op:string; data:Record<string,unknown>; expected_revision:number}[]=[]
  const blocks=[
    {type:'iteration',title:'迭代',category:'logic',input_ports:[],output_ports:[],config_schema:{properties:{}},editor:{fields:[
      {path:'workflow',label:'Workflow',label_zh:'内部流程',control:'json'},
      {path:'output_node_id',label:'Output',label_zh:'输出积木',control:'text'},
    ]}},
    {type:'end',title:'结束',category:'flow',input_ports:[],output_ports:[],config_schema:{properties:{}},editor:{fields:[{path:'outputs',label:'Outputs',label_zh:'输出字段',control:'json'}]}},
    ...(humanAnswers?[{type:'human_input',title:'人工输入',category:'input',input_ports:[],output_ports:[{name:'output',value_type:'object'}],config_schema:{properties:{}},editor:{fields:[]}}]:[]),
  ]
  vi.mocked(api).mockImplementation(async (path,options)=>{
    if(path.endsWith('/draft') && options?.method==='POST'){
      const body=JSON.parse(options.body as string);saved.push(body)
      const node=draft.snapshot.workflow.nodes.find(node=>node.id===body.data.node_id)!
      Object.assign(node,body.data.changes);draft.revision++;draft.content_hash=String(draft.revision)
      return structuredClone(draft) as never
    }
    if(path.endsWith('/draft')) return structuredClone(draft) as never
    if(path.startsWith('/api/v1/blocks?')) return blocks as never
    if(path.endsWith('/project')) return {project_id:null} as never
    if(path==='/health') return {status:'ok'} as never
    return [] as never
  })
  const params=Promise.resolve({id:'p'})
  const view=await act(async()=>render(<Suspense><Studio params={params}/></Suspense>))
  return {draft,saved,...view}
}

it('uses human forms inside nested loops and changes only the correct inner node',async()=>{
  const {draft,saved,container}=await setup()
  fireEvent.click(await screen.findByRole('button',{name:'Select outer'}))
  expect(screen.getByRole('combobox',{name:'输出积木'})).toHaveValue('same')
  fireEvent.click(screen.getByRole('button',{name:'进入循环内部编辑'}))
  fireEvent.click(screen.getByRole('button',{name:'Select inner'}))
  fireEvent.click(screen.getByRole('button',{name:'进入循环内部编辑'}))
  fireEvent.click(screen.getByRole('button',{name:'Select same'}))
  expect(screen.getByLabelText('输出字段 message')).toHaveValue('inner')
  fireEvent.change(screen.getByLabelText('输出字段 message'),{target:{value:'updated'}})
  fireEvent.click(container.querySelector('[data-config-editor-action="save"]')!)
  await waitFor(()=>expect(saved).toHaveLength(1))
  expect(saved[0]).toMatchObject({expected_revision:1,op:'update_node',data:{node_id:'outer',merge_config:false}})
  expect(draft.snapshot.workflow.nodes[1].config).toEqual({outputs:{message:'outer'}})
  fireEvent.click(screen.getByRole('button',{name:/返回上层/}))
  fireEvent.click(screen.getByRole('button',{name:'Select same'}))
  expect(screen.getByLabelText('输出字段 message')).toHaveValue('middle')
  fireEvent.click(screen.getByRole('button',{name:/返回上层/}))
  fireEvent.click(screen.getByRole('button',{name:'Select outer'}))
  fireEvent.click(screen.getByRole('button',{name:'进入循环内部编辑'}))
  fireEvent.click(screen.getByRole('button',{name:'Select inner'}))
  fireEvent.click(screen.getByRole('button',{name:'进入循环内部编辑'}))
  fireEvent.click(screen.getByRole('button',{name:'Select same'}))
  expect(screen.getByLabelText('输出字段 message')).toHaveValue('updated')
})

it('lists implicit item inputs and deletes only the selected inner edge',async()=>{
  const {saved}=await setup()
  fireEvent.click(await screen.findByRole('button',{name:'Select outer'}))
  fireEvent.click(screen.getByRole('button',{name:'进入循环内部编辑'}))
  fireEvent.click(screen.getByRole('button',{name:'Select inner'}))
  fireEvent.click(screen.getByRole('button',{name:'进入循环内部编辑'}))
  fireEvent.click(screen.getByRole('button',{name:'Select same'}))
  fireEvent.change(screen.getByLabelText('输出字段 message的来源'),{target:{value:'reference'}})
  fireEvent.change(screen.getByLabelText('输出字段 message的节点'),{target:{value:'$inputs'}})
  const fields=screen.getByLabelText('输出字段 message的输出字段')
  expect(Array.from((fields as HTMLSelectElement).options).map(option=>option.value)).toContain('["part"]')
  fireEvent.click(screen.getByRole('button',{name:'Edge e'}))
  fireEvent.click(screen.getByRole('button',{name:'删除此连线'}))
  await waitFor(()=>expect(saved).toHaveLength(2))
  expect(saved.every(edit=>edit.data.node_id==='outer')).toBe(true)
  expect(screen.queryByRole('button',{name:'Edge e'})).not.toBeInTheDocument()
})

it('changes a saved human answer binding through declared fields only in the selected nested loop',async()=>{
  const {draft,saved,container}=await setup(true)
  const expected=structuredClone(draft.snapshot.workflow) as unknown as WorkflowGraph
  const expectedNode=workflowAtPath(expected,['outer','inner'])!.nodes.find(node=>node.id==='same')!
  expectedNode.config={outputs:{message:{$ref:{node_id:'review',path:['output','review_notes']}}}}

  fireEvent.click(await screen.findByRole('button',{name:'Select outer'}))
  fireEvent.click(screen.getByRole('button',{name:'进入循环内部编辑'}))
  fireEvent.click(screen.getByRole('button',{name:'Select inner'}))
  fireEvent.click(screen.getByRole('button',{name:'进入循环内部编辑'}))
  fireEvent.click(screen.getByRole('button',{name:'Select same'}))
  expect(screen.getByLabelText('输出字段 message的节点')).toHaveValue('review')
  const fields=screen.getByRole('combobox',{name:'输出字段 message的输出字段'})
  expect(fields).toHaveValue('["output","actual_defects"]')
  expect(screen.getByRole('option',{name:'output.actual_defects'})).toBeEnabled()
  expect(screen.getByRole('option',{name:'output.review_notes'})).toBeEnabled()
  fireEvent.change(fields,{target:{value:'["output","review_notes"]'}})
  fireEvent.click(container.querySelector('[data-config-editor-action="save"]')!)
  await waitFor(()=>expect(saved).toHaveLength(1))
  expect(saved[0]).toMatchObject({expected_revision:1,op:'update_node',data:{node_id:'outer',merge_config:false}})
  expect(draft.snapshot.workflow).toEqual(expected)

  fireEvent.click(screen.getByRole('button',{name:/返回上层/}))
  fireEvent.click(screen.getByRole('button',{name:'Select same'}))
  expect(screen.getByLabelText('输出字段 message')).toHaveValue('middle')
  fireEvent.click(screen.getByRole('button',{name:/返回上层/}))
  fireEvent.click(screen.getByRole('button',{name:'Select same'}))
  expect(screen.getByLabelText('输出字段 message')).toHaveValue('outer')
  fireEvent.click(screen.getByRole('button',{name:'Select outer'}))
  fireEvent.click(screen.getByRole('button',{name:'进入循环内部编辑'}))
  fireEvent.click(screen.getByRole('button',{name:'Select inner'}))
  fireEvent.click(screen.getByRole('button',{name:'进入循环内部编辑'}))
  fireEvent.click(screen.getByRole('button',{name:'Select same'}))
  expect(screen.getByRole('combobox',{name:'输出字段 message的输出字段'})).toHaveValue('["output","review_notes"]')
  expect(saved).toHaveLength(1)
})
