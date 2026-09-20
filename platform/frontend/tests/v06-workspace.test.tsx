import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import WorkflowComposer from '@/app/components/WorkflowComposer'
import {WorkflowValueField} from '@/app/components/WorkflowValueField'
import {ProjectTaskOutput} from '@/app/components/ProjectRunPanel'
import ProjectModels from '@/app/components/ProjectModels'
import type {ProjectTask} from '@/lib/project-progress'
vi.mock('@/lib/platform',()=>({api:vi.fn(),withFrontendToken:(p:string)=>p}))
vi.mock('@/app/components/ModelingPanel',()=>({default:()=>null}))
afterEach(()=>{cleanup();vi.mocked(api).mockReset()})
it('applies one generated graph and undoes against the saved revision without starting a task',async()=>{
 const before={nodes:[],edges:[]};const onChanged=vi.fn()
 vi.mocked(api).mockImplementation(async(path)=>path.endsWith('/workflow-generation')?{workflow_id:'w',previous_workflow:before,draft:{revision:8}} as never:{revision:7} as never)
 render(<WorkflowComposer projectId="p" workflowId="w" onChanged={onChanged}/>);
 fireEvent.change(screen.getByLabelText('工作流描述'),{target:{value:'创建未绑定的预测节点'}});fireEvent.click(screen.getByText('应用修改'))
 await screen.findByText('已保存到画布，可继续编辑或运行。');expect(onChanged).toHaveBeenCalledWith('w')
 expect(JSON.parse(vi.mocked(api).mock.calls.find(([p])=>p.endsWith('/workflow-generation'))![1]!.body as string).expected_revision).toBe(7)
 fireEvent.click(screen.getByText('撤销 AI 修改'))
 await waitFor(()=>expect(onChanged).toHaveBeenCalledTimes(2))
 const call=vi.mocked(api).mock.calls.find(([p,o])=>p.endsWith('/workflows/w/draft')&&o?.method==='PUT')!
 expect(JSON.parse(call[1]!.body as string)).toMatchObject({expected_revision:8,workflow:before})
 expect(vi.mocked(api).mock.calls.some(([p])=>p.includes('/tasks'))).toBe(false)
})
it('preserves a concurrent human edit by displaying the conflict and offering no undo',async()=>{
 vi.mocked(api).mockImplementation(async(p)=>{if(p.endsWith('/workflow-generation'))throw new Error('画布已被修改');return {revision:2} as never})
 const changed=vi.fn();render(<WorkflowComposer projectId="p" workflowId="w" onChanged={changed}/>);
 fireEvent.change(screen.getByLabelText('工作流描述'),{target:{value:'更改输出'}});fireEvent.click(screen.getByText('应用修改'))
 expect(await screen.findByRole('alert')).toHaveTextContent('画布已被修改');expect(changed).not.toHaveBeenCalled();expect(screen.queryByText('撤销 AI 修改')).not.toBeInTheDocument()
})
it('selects a stable model resource without JSON and permits leaving it unconfigured',async()=>{
 vi.mocked(api).mockResolvedValue([{model_ref:'quality',name:'质量预测',status:'unbound'}] as never)
 const change=vi.fn();render(<WorkflowValueField projectId="p" field="model_ref" nodeId="predict" nodes={[]} label="预测模型" value="" onChange={change}/>)
 await screen.findByRole('option',{name:'质量预测 · 待绑定'});fireEvent.change(screen.getByLabelText('预测模型',{selector:'select'}),{target:{value:'quality'}})
 expect(change).toHaveBeenCalledWith('quality');expect(screen.getByRole('option',{name:'稍后配置'})).toBeInTheDocument()
})
it('creates a prediction workflow before training or credentials exist',async()=>{
 vi.mocked(api).mockImplementation(async(p,o)=>{
 if(p.endsWith('/members'))return {id:'w'} as never
 if(p.endsWith('/draft')&&!o)return {revision:0} as never
 return [] as never
 });const changed=vi.fn();render(<ProjectModels projectId="p" onWorkflow={changed} onTask={vi.fn()} onTalk={vi.fn()}/>);
 fireEvent.click(screen.getByRole('button',{name:'创建预测工作流'}));await waitFor(()=>expect(changed).toHaveBeenCalledWith('w'))
 const call=vi.mocked(api).mock.calls.find(([p,o])=>p.endsWith('/workflows/w/draft')&&o?.method==='PUT')!
 expect(JSON.parse(call[1]!.body as string).workflow.nodes[1].config.model_ref).toBe('prediction')
 expect(vi.mocked(api).mock.calls.some(([p])=>p.endsWith('/train'))).toBe(false)
})
it('downloads prediction output within its project and rejects path traversal',()=>{
 const task:ProjectTask={id:'t',request_key:'r',purpose:'business',item_id:'',feedback_task_id:'',message:'',error:'',created_at:'',updated_at:'',status:'succeeded',mode:'workflow',outputs:{csv_download:'datasets/d/files/run/output/predictions.csv',result:{artifact:'datasets/d/files/run/output/predictions.csv'},bad:{artifact:'datasets/d/files/../secret'}},presentation:{}}
 render(<ProjectTaskOutput projectId="p" task={task}/>);
 expect(screen.getAllByRole('link')).toHaveLength(1);expect(screen.getByRole('link')).toHaveAttribute('href','/api/platform/api/v1/projects/p/datasets/d/files/run/output/predictions.csv')
})

it.each(['direct','wrapped'])('shows %s prediction values while deduplicating the CSV link',(shape)=>{
 const result={artifact:'datasets/d/files/run/output/predictions.csv',preview:[{prediction:303.1389},{prediction:306.1019}]}
 const task:ProjectTask={id:'t',request_key:'r',purpose:'business',item_id:'',feedback_task_id:'',message:'',error:'',created_at:'',updated_at:'',status:'succeeded',mode:'prediction',outputs:shape==='direct'?result:{result,csv_download:result.artifact},presentation:{}}
 render(<ProjectTaskOutput projectId="p" task={task}/>);
 expect(screen.getByRole('cell',{name:'303.139'})).toBeInTheDocument();expect(screen.getByRole('cell',{name:'306.102'})).toBeInTheDocument();expect(screen.getAllByRole('link')).toHaveLength(1)
})

it('applies the visible canvas revision and sends selection or nested scope in one request',async()=>{
 const nodes=[{id:'loop',type:'iteration',title:'处理每条数据',block_version:1,description:'',position:{x:0,y:0},retry:{enabled:false,max_attempts:1,delay_seconds:0},error_strategy:'fail' as const,config:{workflow:{nodes:[{id:'inner',type:'loop',title:'内循环',config:{workflow:{nodes:[],edges:[]}}}],edges:[]}}}]
 vi.mocked(api).mockResolvedValue({workflow_id:'w',previous_workflow:{nodes:[],edges:[]},draft:{revision:6}} as never)
 render(<WorkflowComposer projectId="p" workflowId="w" revision={5} nodes={nodes} selectedNodeIds={['loop']} onChanged={vi.fn()}/>);
 fireEvent.change(screen.getByLabelText('工作流描述'),{target:{value:'修改结果'}})
 fireEvent.change(screen.getByLabelText('修改范围'),{target:{value:'selection'}})
 fireEvent.click(screen.getByText('应用修改'));await screen.findByText('已保存到画布，可继续编辑或运行。')
 expect(vi.mocked(api)).toHaveBeenCalledTimes(1)
 expect(JSON.parse(vi.mocked(api).mock.calls[0][1]!.body as string)).toMatchObject({expected_revision:5,node_ids:['loop'],workflow_path:[]})
 fireEvent.change(screen.getByLabelText('修改范围'),{target:{value:JSON.stringify(['loop','inner'])}})
 fireEvent.click(screen.getByText('应用修改'));await waitFor(()=>expect(vi.mocked(api)).toHaveBeenCalledTimes(2))
 expect(JSON.parse(vi.mocked(api).mock.calls[1][1]!.body as string)).toMatchObject({expected_revision:5,node_ids:[],workflow_path:['loop','inner']})
})

it('opens the selected scope from the canvas action and blocks AI changes while form edits are unsaved',async()=>{
 const onChanged=vi.fn()
 const {rerender}=render(<WorkflowComposer projectId="p" workflowId="w" selectedNodeIds={['end']} editRequest={0} disabled onChanged={onChanged}/>);
 // jsdom has no scrolling implementation; the actual browser path is also exercised.
 const scroll=vi.fn(); HTMLElement.prototype.scrollIntoView=scroll
 rerender(<WorkflowComposer projectId="p" workflowId="w" selectedNodeIds={['end']} editRequest={1} disabled onChanged={onChanged}/>);
 expect(screen.getByLabelText('工作流描述')).toHaveFocus()
 fireEvent.change(screen.getByLabelText('工作流描述'),{target:{value:'修改输出'}})
 expect(screen.getByText('应用修改')).toBeDisabled()
 expect(vi.mocked(api)).not.toHaveBeenCalled()
 expect(scroll).toHaveBeenCalledOnce()
})


it('keeps the container path when AI edits selected inner nodes',async()=>{
 vi.mocked(api).mockResolvedValue({workflow_id:'w',previous_workflow:{nodes:[],edges:[]},draft:{revision:9}} as never)
 render(<WorkflowComposer projectId="p" workflowId="w" revision={8} selectedNodeIds={['same']} selectionPath={['outer','inner']} editRequest={1} onChanged={vi.fn()}/>);
 fireEvent.change(screen.getByLabelText('工作流描述'),{target:{value:'只修改当前循环的这个积木'}})
 fireEvent.click(screen.getByRole('button',{name:'应用修改'}))
 await waitFor(()=>expect(api).toHaveBeenCalled())
 expect(JSON.parse(vi.mocked(api).mock.calls[0][1]!.body as string)).toMatchObject({expected_revision:8,node_ids:['same'],workflow_path:['outer','inner']})
})
