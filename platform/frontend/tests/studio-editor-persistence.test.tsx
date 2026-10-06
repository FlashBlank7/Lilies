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

it.each([true,false])('opens the run step in its nested current canvas without changing the draft (exists=%s)',async exists=>{
  window.history.replaceState(null,'','/?tab=edit&node_path='+encodeURIComponent(JSON.stringify(['each',exists?'inside':'deleted'])))
  const inner={id:'inside',title:'循环内部处理',type:'code',position:{x:0,y:0},config:{message:'这是当前草稿'}}
  const draft={application_id:'p',revision:2,content_hash:'current',validation_report:{},snapshot:{name:'当前流程',description:'',requirement:'',mode:'workflow',tests:[],agents:{},workflow:{nodes:[{id:'each',title:'逐项处理',type:'iteration',position:{x:0,y:0},config:{workflow:{nodes:[inner],edges:[]}}}],edges:[]}}}
  vi.mocked(api).mockImplementation(async path=>{
    if(path.endsWith('/draft'))return draft as never
    if(path.startsWith('/api/v1/blocks?application_id='))return [{type:'code',title:'代码',category:'tool',input_ports:[],output_ports:[],editor:{fields:[]},config_schema:{}},{type:'iteration',title:'循环',category:'logic',input_ports:[],output_ports:[],editor:{fields:[]},config_schema:{}}] as never
    if(path.endsWith('/project'))return {project_id:null} as never
    if(path==='/health')return {status:'ok'} as never
    return [] as never
  })
  try {
    const {container}=await act(async()=>render(<Suspense><Studio params={Promise.resolve({id:'p'})}/></Suspense>))
    if(exists){
      await screen.findByText('已定位到当前草稿中的对应积木。这里的修改仅影响后续运行，原运行记录保持不变。')
      expect(screen.getByRole('button',{name:'Select inside'})).toBeInTheDocument()
      expect(screen.queryByRole('button',{name:'Select each'})).not.toBeInTheDocument()
      expect(JSON.parse((container.querySelector('textarea.json-editor') as HTMLTextAreaElement).value)).toEqual(inner.config)
    }else{
      await screen.findByText('当前草稿中已找不到本次运行的这个步骤。原运行记录仍保留，可返回结果查看。')
      expect(screen.getByRole('button',{name:'Select each'})).toBeInTheDocument()
    }
    expect(vi.mocked(api).mock.calls.some(([,options])=>options?.method==='POST')).toBe(false)
  }finally{window.history.replaceState(null,'','/')}
})

it('opens the prediction resource form from readiness and saves only the model the employee explicitly selects',async()=>{
  window.history.replaceState(null,'','/?tab=edit&node_path='+encodeURIComponent(JSON.stringify(['predict'])))
  let draft={application_id:'w',revision:1,content_hash:'one',validation_report:{},snapshot:{name:'批量预测',description:'',requirement:'',mode:'workflow',tests:[],agents:{},workflow:{
    nodes:[{id:'predict',title:'模型预测',type:'model_predict',position:{x:0,y:0},config:{model_ref:'',dataset_id:'dataset-to-predict'}}],edges:[],
  }}}
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path.endsWith('/draft')&&options?.method==='POST'){
      const {data}=JSON.parse(options.body as string)
      draft={...draft,revision:2,snapshot:{...draft.snapshot,workflow:{...draft.snapshot.workflow,nodes:[{...draft.snapshot.workflow.nodes[0],config:data.changes.config}]}}}
      return draft as never
    }
    if(path.endsWith('/draft'))return draft as never
    if(path.startsWith('/api/v1/blocks?'))return [{type:'model_predict',title:'模型预测',category:'model',input_ports:[],output_ports:[],config_schema:{},editor:{fields:[{path:'model_ref',label:'调用模型',label_zh:'调用模型',control:'reference_or_text'}]}}] as never
    if(path.endsWith('/project'))return {project_id:'p'} as never
    if(path==='/api/v1/projects/p')return {id:'p',name:'测试项目',members:[]} as never
    if(path.endsWith('/models'))return [{model_ref:'prediction',name:'已训练预测模型',status:'ready'}] as never
    if(path==='/health')return {status:'ok'} as never
    return [] as never
  })
  try{
    const {container}=await act(async()=>render(<Suspense><Studio params={Promise.resolve({id:'w'})}/></Suspense>))
    await screen.findByRole('option',{name:'已训练预测模型'})
    const selector=screen.getByRole('combobox',{name:'调用模型'})
    expect(selector).toHaveValue('')
    expect(vi.mocked(api).mock.calls.some(([,options])=>options?.method)).toBe(false)
    fireEvent.change(selector,{target:{value:'prediction'}})
    expect(vi.mocked(api).mock.calls.some(([,options])=>options?.method)).toBe(false)
    fireEvent.click(container.querySelector('[data-config-editor-action="save"]')!)
    await waitFor(()=>expect(draft.revision).toBe(2))
    const writes=vi.mocked(api).mock.calls.filter(([,options])=>options?.method)
    expect(writes).toHaveLength(1)
    expect(writes[0][0]).toBe('/api/v1/applications/w/draft')
    expect(JSON.parse(writes[0][1]!.body as string)).toMatchObject({op:'update_node',data:{node_id:'predict',changes:{config:{model_ref:'prediction',dataset_id:'dataset-to-predict'}}}})
  }finally{window.history.replaceState(null,'','/')}
})

it('saves model choices from Chinese form labels using the original schema values', async () => {
  const config = { evaluation: { problem: 'regression', metric: 'mae' }, candidate: { engine: 'sklearn', models: ['linear', 'forest'] } }
  const draft = { application_id: 'p', revision: 1, content_hash: 'one', validation_report: {},
    snapshot: { name: '训练', description: '', requirement: '', mode: 'workflow', tests: [], agents: {}, workflow: {
      nodes: [{ id: 'train', title: '训练模型', type: 'model_train', position: { x: 0, y: 0 }, config }], edges: [] } } }
  const saved: unknown[] = []
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft') && options?.method === 'POST') {
      saved.push(JSON.parse(options.body as string).data.changes.config)
      return draft as never
    }
    if (path.endsWith('/draft')) return draft as never
    if (path.startsWith('/api/v1/blocks?application_id=')) return [{ type: 'model_train', title: '训练', category: 'model', input_ports: [], output_ports: [], config_schema: {},
      editor: { fields: [
        { path: 'candidate.engine', label: 'Engine', label_zh: '训练方式', control: 'enum', options: ['sklearn', 'optuna'],
          option_labels_zh: { sklearn: '基础模型比较（scikit-learn）', optuna: '自动搜索参数（Optuna）' } },
        { path: 'candidate.models', label: 'Models', label_zh: '候选模型', control: 'string_list', required: true,
          options: ['linear', 'forest', 'hist_gradient'], option_labels_zh: { linear: '线性模型', forest: '随机森林', hist_gradient: '梯度提升树' } },
      ] } }] as never
    if (path.endsWith('/project')) return { project_id: null } as never
    if (path === '/health') return { status: 'ok' } as never
    return [] as never
  })
  const { container } = await act(async () => render(<Suspense><Studio params={Promise.resolve({ id: 'p' })} /></Suspense>))
  fireEvent.click(await screen.findByRole('button', { name: 'Select train' }))
  fireEvent.change(screen.getByRole('combobox', { name: '训练方式' }), { target: { value: 'optuna' } })
  fireEvent.click(screen.getByRole('checkbox', { name: '线性模型' }))
  fireEvent.click(screen.getByRole('checkbox', { name: '梯度提升树' }))
  fireEvent.click(container.querySelector('[data-config-editor-action="save"]')!)
  await waitFor(() => expect(saved).toHaveLength(1))
  expect(saved[0]).toEqual({ ...config, candidate: { engine: 'optuna', models: ['forest', 'hist_gradient'] } })
})

it.each([
  { $ref: { node_id: 'prepare', path: ['json', 'requirements'] } },
  [{ title: '保留数组对象' }],
  '123',
].map(items => ({ items })))('preserves untyped schema values through form and JSON editing: %j', async ({ items }) => {
  const config = { items, workflow: { nodes: [], edges: [] } }
  const draft = { application_id: 'p', revision: 1, content_hash: 'one', validation_report: {},
    snapshot: { name: 'Editor', description: '', requirement: 'Edit a reference', mode: 'workflow', tests: [], agents: {}, workflow: {
      nodes: [{ id: 'each', title: 'each', type: 'iteration', position: { x: 0, y: 0 }, config }], edges: [] } } }
  const saved: unknown[] = []
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft') && options?.method === 'POST') {
      saved.push(JSON.parse(options.body as string).data.changes.config)
      return draft as never
    }
    if (path.endsWith('/draft')) return draft as never
    if (path.startsWith('/api/v1/blocks?application_id=')) return [{ type: 'iteration', title: 'Iteration', category: 'logic', input_ports: [], output_ports: [], editor: { fields: [] },
      config_schema: { properties: { items: { title: 'Items' }, workflow: { type: 'object' } }, required: ['items', 'workflow'] } }] as never
    if (path.endsWith('/project')) return { project_id: null } as never
    if (path === '/health') return { status: 'ok' } as never
    return [] as never
  })
  const params = Promise.resolve({ id: 'p' })
  const { container } = await act(async () => render(<Suspense><Studio params={params} /></Suspense>))
  fireEvent.click(await screen.findByRole('button', { name: 'Select each' }))
  fireEvent.click(container.querySelector('[data-config-editor-mode="json"]')!)
  const editor = () => container.querySelector('textarea.json-editor') as HTMLTextAreaElement
  expect(JSON.parse(editor().value)).toEqual(config)
  fireEvent.click(container.querySelector('[data-config-editor-mode="form"]')!)
  fireEvent.click(container.querySelector('[data-config-editor-mode="json"]')!)
  fireEvent.click(container.querySelector('[data-config-editor-action="save"]')!)
  await waitFor(() => expect(saved).toHaveLength(1))
  expect(saved[0]).toEqual(config)
})

it.each(['same', 'other'])('preserves new edits to the %s node when an earlier save finishes', async target => {
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  let saves = 0
  const saved: { node_id: string; changes: { config: object } }[] = []
  let draft = { application_id: 'p', revision: 1, content_hash: 'one', validation_report: {},
    snapshot: { name: 'Editor', description: '', requirement: 'Test manual editing', mode: 'workflow', tests: [], agents: {}, workflow: {
      nodes: ['prepare', 'report'].map(id => ({ id, title: id, type: 'tool', position: { x: 0, y: 0 }, config: { tool_name: 'Bash', input: { command: id } } })), edges: [] } } }
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/draft') && options?.method === 'POST') {
      const { data } = JSON.parse(options.body as string)
      saved.push(data)
      if (++saves === 1) await pending
      draft = { ...draft, revision: draft.revision + 1, snapshot: { ...draft.snapshot, workflow: { ...draft.snapshot.workflow,
        nodes: draft.snapshot.workflow.nodes.map(node => node.id === data.node_id ? { ...node, config: data.changes.config } : node) } } }
      return draft as never
    }
    if (path.endsWith('/draft')) return draft as never
    if (path.endsWith('/project')) return { project_id: null } as never
    if (path === '/health') return { status: 'ok' } as never
    return [] as never
  })
  const params = Promise.resolve({ id: 'p' })
  const { container } = await act(async () => render(<Suspense><Studio params={params} /></Suspense>))
  fireEvent.click(await screen.findByRole('button', { name: 'Select prepare' }))
  const editor = () => container.querySelector('textarea.json-editor') as HTMLTextAreaElement
  const save = () => container.querySelector('[data-config-editor-action="save"]') as HTMLButtonElement
  fireEvent.change(editor(), { target: { value: '{"tool_name":"Bash","input":{"command":"first"}}' } })
  fireEvent.click(save())
  await waitFor(() => expect(saved).toHaveLength(1))
  if (target === 'other') fireEvent.click(screen.getByRole('button', { name: 'Select report' }))
  const edited = '{"tool_name":"Bash","input":{"command":"new unsaved code"}}'
  fireEvent.change(editor(), { target: { value: edited } })
  await act(async () => { release(); await pending })
  await waitFor(() => expect(container).toHaveTextContent('r2'))
  expect(editor().value).toBe(edited)
  fireEvent.click(save())
  await waitFor(() => expect(saved).toHaveLength(2))
  expect(saved[1]).toMatchObject({ node_id: target === 'other' ? 'report' : 'prepare', changes: { config: JSON.parse(edited) } })
})
