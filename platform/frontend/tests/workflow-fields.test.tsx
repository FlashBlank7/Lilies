import { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type Block } from '@/lib/platform'
import { outputPaths, upstreamNodes, type FieldNode } from '@/lib/workflow-fields'
import { WorkflowValueField, WorkflowObjectFields, WorkflowInputFields, type WorkflowFieldProps } from '@/app/components/WorkflowValueField'
import WorkflowImageFields from '@/app/components/WorkflowImageFields'
import WorkflowConditionFields from '@/app/components/WorkflowConditionFields'
vi.mock('@/lib/platform', () => ({ api: vi.fn() }))
afterEach(() => { cleanup(); vi.mocked(api).mockReset() })
const nodes: FieldNode[] = [
  {id:'start',title:'输入',type:'start',config:{inputs:[{name:'score',type:'number'}]}},
  {id:'model',title:'分析',type:'llm',config:{structured_output:{type:'object',properties:{score:{type:'number'}}}}},
  {id:'current',title:'当前',type:'end',config:{}},
  {id:'later',title:'下游',type:'end',config:{}},
  {id:'other',title:'另一个分支',type:'code',config:{}},
]
const edges = [{source:'start',target:'model'},{source:'model',target:'current'},{source:'current',target:'later'},{source:'start',target:'other'}]
const props = {nodes,edges,nodeId:'current',label:'测试字段'}

it('offers transitive upstream nodes and structured fields without downstream or sibling choices', () => {
  expect(upstreamNodes(nodes,edges,'current').map(n=>n.id)).toEqual(['start','model'])
  const change=vi.fn()
  render(<WorkflowValueField {...props} value={JSON.stringify({$ref:{node_id:'model',path:['structured','score']}})} onChange={change}/> )
  const choices=screen.getByLabelText('测试字段的节点')
  expect(choices.querySelector('option[value="later"]')).toBeNull()
  expect(choices.querySelector('option[value="other"]')).toBeNull()
  expect(screen.getByLabelText('测试字段的输出字段')).toHaveValue('["structured","score"]')
  fireEvent.change(screen.getByLabelText('测试字段的输出字段'),{target:{value:'["text"]'}})
  expect(JSON.parse(change.mock.calls[0][0])).toEqual({$ref:{node_id:'model',path:['text']}})
})

it('preserves an existing invalid reference for correction instead of silently changing it',()=>{
  render(<WorkflowValueField {...props} value='{"$ref":{"node_id":"later","path":["output",0]}}' onChange={vi.fn()} disabled/>)
  expect(screen.getByLabelText('测试字段的节点')).toHaveValue('later')
  expect(screen.getByRole('status')).toHaveTextContent('尚未连接为上游')
  expect(screen.getByLabelText('测试字段的节点')).toBeDisabled()
  expect(screen.getByLabelText('测试字段的字段路径')).toBeDisabled()
})

it('uses actual block outputs and variable assignments instead of template input variables',()=>{
  expect(outputPaths({id:'v',title:'v',type:'variable_assigner',config:{assignments:{score:1}}})).toEqual([['output','score']])
  expect(outputPaths({id:'t',title:'t',type:'template_transform',config:{variables:{name:'input'}}},[{type:'template_transform',output_ports:[{name:'text',value_type:'string'}]} as Block])).toEqual([['text']])
})

it('switches model choices with the unsaved LLM role and ignores stale connection responses',async()=>{
  let resolveMain!:(value:unknown)=>void
  vi.mocked(api).mockImplementation(async path=>path.endsWith('/agent-session')?new Promise(resolve=>{resolveMain=resolve}):{model:'vision-model'} as never)
  const {rerender}=render(<WorkflowValueField {...props} projectId="p" field="model" modelRole="main" value="" onChange={vi.fn()}/>)
  rerender(<WorkflowValueField {...props} projectId="p" field="model" modelRole="vision" value="" onChange={vi.fn()}/>)
  await screen.findByRole('option',{name:'vision-model'})
  resolveMain({model:'old-main'})
  await waitFor(()=>expect(screen.queryByRole('option',{name:'old-main'})).not.toBeInTheDocument())
  expect(vi.mocked(api).mock.calls.map(c=>c[0])).toEqual(['/api/v1/projects/p/agent-session','/api/v1/projects/p/vision-model'])
})

it('retries resource loading and retains an unbound logical model selection',async()=>{
  vi.mocked(api).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce([{model_ref:'quality',name:'质量预测',status:'unbound'}] as never)
  render(<WorkflowValueField {...props} projectId="p" field="model_ref" value="quality" onChange={vi.fn()}/>)
  await screen.findByRole('alert');fireEvent.click(screen.getByRole('button',{name:'重试'}))
  await screen.findByRole('option',{name:'质量预测 · 待绑定'})
  expect(screen.getByLabelText('测试字段')).toHaveValue('quality')
})

function ObjectEditor(){
  const [value,setValue]=useState('{"code":"001","other":"true"}')
  return <><WorkflowObjectFields {...props} value={value} onChange={setValue}/><output aria-label="配置结果">{value}</output></>
}
it('keeps text-looking numbers as text and duplicate field names cannot overwrite other entries',()=>{
  render(<ObjectEditor/>)
  fireEvent.change(screen.getByLabelText('测试字段 code'),{target:{value:'1000'}})
  expect(JSON.parse(screen.getByLabelText('配置结果').textContent!)).toEqual({code:'1000',other:'true'})
  fireEvent.change(screen.getByLabelText('测试字段字段名 1'),{target:{value:'other'}})
  expect(screen.getByRole('alert')).toHaveTextContent('不能为空或重复')
  expect(JSON.parse(screen.getByLabelText('配置结果').textContent!)).toEqual({code:'1000',other:'true'})
  fireEvent.change(screen.getByLabelText('测试字段字段名 1'),{target:{value:'count'}})
  fireEvent.change(screen.getByLabelText('测试字段 count的值类型'),{target:{value:'number'}})
  fireEvent.change(screen.getByLabelText('测试字段 count'),{target:{value:'500'}})
  expect(JSON.parse(screen.getByLabelText('配置结果').textContent!)).toEqual({count:500,other:'true'})
})

it('chooses project PNG/JPEG images and preserves image-specific options',async()=>{
  vi.mocked(api).mockResolvedValue([{path:'requirement-package/drawing.png'},{path:'report.pdf'}] as never)
  function Editor(){const [value,setValue]=useState('[{"file_path":"old.png","detail":"high"}]');return <><WorkflowImageFields {...props} projectId="p" value={value} onChange={setValue}/><output>{value}</output></>}
  render(<Editor/>);await screen.findByRole('option',{name:'drawing.png · requirement-package'})
  expect(screen.queryByRole('option',{name:'report.pdf'})).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('测试字段 1'),{target:{value:'requirement-package/drawing.png'}})
  expect(screen.getByRole('status')).toHaveTextContent('"detail":"high"')
  fireEvent.change(screen.getByLabelText('测试字段的来源'),{target:{value:'reference'}})
  fireEvent.change(screen.getByLabelText('测试字段的节点'),{target:{value:'model'}})
  expect(screen.getByRole('status')).toHaveTextContent('"node_id":"model"')
})

it('edits condition groups with typed comparisons using forms',()=>{
  function Editor(){const [value,setValue]=useState('[{"id":"enough","logical_operator":"and","conditions":[{"value":{"$ref":{"node_id":"model","path":["structured","score"]}},"operator":"gte","expected":10}]}]');return <><WorkflowConditionFields {...props} value={value} onChange={setValue}/><output>{value}</output></>}
  render(<Editor/>)
  fireEvent.change(screen.getByLabelText('分支 1条件 1的比较值'),{target:{value:'500'}})
  fireEvent.change(screen.getByLabelText('分支 1的条件组合'),{target:{value:'or'}})
  fireEvent.click(screen.getByRole('button',{name:'添加条件'}))
  const actual=JSON.parse(screen.getByRole('status').textContent!)[0]
  expect(actual.conditions[0].expected).toBe(500);expect(actual.conditions[0].value.$ref.path).toEqual(['structured','score']);expect(actual.conditions).toHaveLength(2);expect(actual.logical_operator).toBe('or')
})

it('adds uniquely named inputs and only offers runtime-supported types',()=>{
  const changed=vi.fn();render(<WorkflowInputFields value='[{"name":"input_2","type":"string"}]' onChange={changed}/>)
  fireEvent.click(screen.getByRole('button',{name:'添加输入'}))
  expect(JSON.parse(changed.mock.calls[0][0])[1].name).toBe('input_3')
  expect(screen.queryByRole('option',{name:'整数'})).not.toBeInTheDocument()
  expect(screen.getByRole('option',{name:'文件列表'})).toBeInTheDocument()
})
