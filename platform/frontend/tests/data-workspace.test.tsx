import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import DataWorkspace from '@/app/components/DataWorkspace'
import WorkspaceOperationDialog from '@/app/components/WorkspaceOperationDialog'
import WorkspaceTable from '@/app/components/WorkspaceTable'
import {api} from '@/lib/platform'
import {emptySelection,TOOL_MIME,DATA_MIME,type DataObject,type TableView} from '@/lib/data-workspace'
vi.mock('@/lib/platform',()=>({api:vi.fn(),withFrontendToken:(v:string)=>v}))
vi.mock('recharts',()=>Object.fromEntries(['BarChart','Bar','XAxis','YAxis','Tooltip','ResponsiveContainer','ScatterChart','Scatter','CartesianGrid'].map(k=>[k,()=>null])))
const object:DataObject={id:'file:data.csv',version:'v1',kind:'table',name:'芯片数据.csv',format:'.csv',path:'data.csv'}
const tool={id:'spearman',name:'Spearman 相关分析',description:'相关系数'}
const table:TableView={object,columns:['x','y'],selected_columns:['x','y'],rows:[{row_id:0,source_row:3,source_line:5,values:{x:null,y:5},raw:{x:'/',y:'5'}}],total_rows:100100,selected_rows:10000,offset:0,limit:100,mapping:{},sheets:[],statistics:[],points:[],points_sampled:false,states:{counts:{pending:1}},cells:[{row:0,column:'x',status:'pending',reason:'unconfirmed_token',raw:'/',value:null}]}
afterEach(cleanup)
beforeEach(()=>{vi.mocked(api).mockReset();vi.mocked(api).mockImplementation(async(path)=>path.endsWith('/catalog')?{tools:[tool],workflows:[]}:path.endsWith('/preview')?table:path.endsWith('/operations')?{id:'new',name:'相关分析',source:object,status:'succeeded',error:'',created_at:'2026-10-19',request:{tool:'spearman',selection:emptySelection(),parameters:{}},results:[]}:{objects:[object],operations:[],has_more:false,total:1,offset:0})})
it('dropping a tool opens configuration with the current selected field and never runs before confirmation',async()=>{
 render(<DataWorkspace projectId="p" onWorkflow={()=>{}}/> )
 await screen.findByRole('region',{name:'数据预览 芯片数据.csv'})
 fireEvent.click(await screen.findByRole('checkbox',{name:'选择字段 x'}))
 fireEvent.drop(screen.getByRole('article',{name:'数据表 芯片数据.csv'}),{dataTransfer:{getData:(kind:string)=>kind===TOOL_MIME?'spearman':''}})
 const dialog=await screen.findByRole('dialog')
 expect(within(dialog).getByText(/版本 v1.*y/)).toBeInTheDocument()
 expect(vi.mocked(api).mock.calls.some(([path])=>path.endsWith('/operations'))).toBe(false)
 fireEvent.click(within(dialog).getByRole('button',{name:'确认配置并运行'}))
 await waitFor(()=>expect(vi.mocked(api).mock.calls.some(([path])=>path.endsWith('/operations'))).toBe(true))
 const call=vi.mocked(api).mock.calls.find(([p])=>p.endsWith('/operations'))!
 expect(JSON.parse(String(call[1]?.body)).selection.columns).toEqual(['y'])
})
it('accepts reverse data-to-tool drops and carries filters into the dialog',async()=>{
 render(<DataWorkspace projectId="p" onWorkflow={()=>{}}/> )
 const button=await screen.findByRole('button',{name:/Spearman 相关分析.*相关系数/})
 fireEvent.drop(button,{dataTransfer:{getData:(kind:string)=>kind===DATA_MIME?JSON.stringify({id:object.id,mapping:{},selection:{...emptySelection(),filters:[{column:'x',operator:'ge',value:10}]}}):''}})
 expect(await screen.findByText('已包含 1 个筛选条件。')).toBeInTheDocument()
 expect(vi.mocked(api).mock.calls.some(([path])=>path.endsWith('/operations'))).toBe(false)
})
it('retries an uncertain submission with the same key, but a changed configuration uses a new key',async()=>{
 vi.mocked(api).mockRejectedValue(new Error('网络暂时不可用'))
 render(<WorkspaceOperationDialog projectId="p" object={object} tool={{id:'outliers',name:'异常值处理',description:''}} selection={emptySelection()} mapping={{}} view={table} objects={[]} onClose={()=>{}} onStarted={()=>{}}/> )
 const submit=screen.getByRole('button',{name:'确认配置并运行'})
 fireEvent.click(submit);await screen.findByRole('alert')
 fireEvent.click(submit);await waitFor(()=>expect(api).toHaveBeenCalledTimes(2))
 await waitFor(()=>expect(submit).toBeEnabled())
 const keys=()=>vi.mocked(api).mock.calls.map(([,options])=>JSON.parse(String(options?.body)).request_key)
 expect(keys()[0]).toBe(keys()[1])
 fireEvent.change(screen.getByLabelText('IQR 系数'),{target:{value:'2'}})
 fireEvent.click(submit);await waitFor(()=>expect(api).toHaveBeenCalledTimes(3))
 expect(keys()[2]).not.toBe(keys()[1])
})
it('shows raw pending tokens and requests the next server page without fetching the entire table',async()=>{
 render(<WorkspaceTable projectId="p" object={object} selection={emptySelection()} mapping={{}} onSelection={()=>{}} onMapping={()=>{}}/> )
 await screen.findByText('待确认 · 无可用解析值')
 expect(screen.getByRole('checkbox',{name:'选择原表第 5 行'})).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'下一页'}))
 await waitFor(()=>expect(JSON.parse(String(vi.mocked(api).mock.calls.at(-1)?.[1]?.body))).toMatchObject({offset:100,limit:100}))
 expect(screen.getByText('100,100')).toBeInTheDocument()
})
it('failure stays separate from the previous successful report',async()=>{
 const request={tool:'spearman',selection:emptySelection(),parameters:{}}
 vi.mocked(api).mockImplementation(async(path)=>path.endsWith('/catalog')?{tools:[tool],workflows:[]}:path.endsWith('/preview')?table:{objects:[object],operations:[{id:'ok',name:'上次分析',source:object,status:'succeeded',error:'',created_at:'2026-10-19',request,results:[{id:'file:old.html',kind:'document',version:'old',name:'上次报告',path:'old.html',format:'.html'}]},{id:'bad',name:'本次分析',source:object,status:'failed',error:'本次运行字段不匹配',created_at:'2026-10-19',request,results:[]}],has_more:false,total:1,offset:0})
 render(<DataWorkspace projectId="p" onWorkflow={()=>{}}/> )
 const failed=await screen.findByRole('region',{name:'处理 本次分析'})
 expect(within(failed).getByText('本次尚无可用产物。')).toBeInTheDocument()
 expect(within(failed).queryByText('上次报告')).not.toBeInTheDocument()
 expect(screen.getByRole('region',{name:'处理 上次分析'})).toHaveTextContent('上次报告')
})

it('shows recorded category names in validation and confusion tables without inventing an order',async()=>{
 const {default:ClassificationResults}=await import('@/app/components/ClassificationResults')
 render(<ClassificationResults result={{classes:[{label:'2',name:'过压损伤',samples:4,precision:.5,recall:.5,f1:.5}],confusion_matrix:[[2]]}}/>)
 expect(within(screen.getByRole('table',{name:'类别验证指标'})).getByRole('rowheader',{name:'2 · 过压损伤'})).toBeInTheDocument()
 fireEvent.click(screen.getByText('查看混淆矩阵'))
 expect(within(screen.getByRole('table',{name:'验证混淆矩阵'})).getByRole('columnheader',{name:'2 · 过压损伤'})).toBeInTheDocument()
})
