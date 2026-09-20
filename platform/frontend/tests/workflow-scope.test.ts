import { expect, it } from 'vitest'
import { scopedMutation, scopedFieldNodes, workflowAtPath, type WorkflowGraph } from '@/lib/workflow-scope'

const graph = { nodes: [
  {id:'start',type:'start',config:{inputs:[{name:'batch'}]}},
  {id:'same',type:'end',config:{outputs:{value:'outer'}},position:{x:900,y:100}},
  {id:'outer',type:'iteration',config:{item_name:'part',items:[1,2],variables:{limit:8},workflow:{nodes:[
    {id:'start',type:'start',config:{inputs:[]}},
    {id:'inner',type:'loop',config:{state_input_name:'count',workflow:{nodes:[
      {id:'start',type:'start',config:{inputs:[]}},
      {id:'same',type:'end',config:{outputs:{value:'inner'}}},
    ],edges:[{id:'edge',source:'start',target:'same',branch:'yes'}]}}},
  ],edges:[],viewport:{zoom:.8,x:11,y:22}}}},
],edges:[]} as unknown as WorkflowGraph

it('edits only the addressed nested graph when inner and outer identifiers coincide', () => {
  const original = structuredClone(graph)
  const edit = scopedMutation(graph, ['outer','inner'], 'update_node', {node_id:'same',changes:{config:{outputs:{value:'changed'}}},merge_config:false})
  const next = structuredClone(graph)
  Object.assign(next.nodes.find(node=>node.id==='outer')!, edit.data.changes)
  expect(workflowAtPath(next,['outer','inner'])?.nodes.find(node=>node.id==='same')?.config.outputs).toEqual({value:'changed'})
  expect(next.nodes.find(node=>node.id==='same')).toEqual(graph.nodes.find(node=>node.id==='same'))
  expect(workflowAtPath(next,['outer'])?.viewport).toEqual({zoom:.8,x:11,y:22})
  expect(workflowAtPath(next,['outer','inner'])?.edges[0].branch).toBe('yes')
  expect(graph).toEqual(original)
})

it('preserves branch and port data when adding inside a loop and rejects missing scope', () => {
  const edge = {id:'branch',source:'start',target:'same',branch:'no',source_port:'output',target_port:'input'}
  const edit = scopedMutation(graph,['outer','inner'],'add_edge',{edge})
  const next=structuredClone(graph); Object.assign(next.nodes[2],edit.data.changes)
  expect(workflowAtPath(next,['outer','inner'])?.edges.at(-1)).toEqual(edge)
  expect(()=>scopedMutation(graph,['missing'],'remove_node',{node_id:'same'})).toThrow('重新打开')
})

it('offers inherited input, item, index, state and explicit variables without persisting synthetic fields',()=>{
  const fields=scopedFieldNodes(graph,['outer','inner'])
  expect(fields.find(node=>node.id==='start')?.config.inputs).toEqual(
    ['batch','limit','part','index','iteration','previous','count','tool_feedback'].map(name=>({name})))
  expect(workflowAtPath(graph,['outer','inner'])?.nodes[0].config.inputs).toEqual([])
})
