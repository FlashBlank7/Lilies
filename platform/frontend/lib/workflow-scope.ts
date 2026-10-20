import type { Draft, WorkflowNode } from './platform'

export type WorkflowGraph = Draft['snapshot']['workflow']

export function containerWorkflow(node: WorkflowNode | undefined | null): WorkflowGraph | undefined {
  if (!node || !['iteration', 'loop'].includes(node.type)) return
  const graph = node.config.workflow as WorkflowGraph | undefined
  return graph && Array.isArray(graph.nodes) && Array.isArray(graph.edges) ? graph : undefined
}

export function workflowAtPath(graph: WorkflowGraph | undefined, path: string[]): WorkflowGraph | undefined {
  for (const id of path) graph = containerWorkflow(graph?.nodes.find(node => node.id === id))
  return graph
}

/** Translate a local edit into one revision-checked edit of its outer container. */
export function scopedMutation(graph: WorkflowGraph, path: string[], op: string, data: Record<string, unknown>) {
  if (!path.length) return { op, data }
  const cloned = structuredClone(graph)
  const current = workflowAtPath(cloned, path)
  if (!current) throw new Error('当前循环已被修改或删除，请重新打开')
  if (op === 'add_node') {
    const node = data.node as WorkflowNode
    if (current.nodes.some(value => value.id === node.id)) throw new Error('循环内已有同名积木')
    current.nodes.push(node)
  } else if (op === 'update_node') {
    const node = current.nodes.find(value => value.id === data.node_id)
    if (!node) throw new Error('循环内的积木已被删除')
    const changes = data.changes as Partial<WorkflowNode>
    const config = changes.config && (data.merge_config === false ? changes.config : { ...node.config, ...changes.config })
    Object.assign(node, changes, config ? { config } : {})
  } else if (op === 'remove_node') {
    current.nodes = current.nodes.filter(value => value.id !== data.node_id)
    current.edges = current.edges.filter(value => value.source !== data.node_id && value.target !== data.node_id)
  } else if (op === 'add_edge') {
    const edge = data.edge as WorkflowGraph['edges'][number]
    if (current.edges.some(value => value.id === edge.id)) throw new Error('循环内已有同名连线')
    current.edges.push(edge)
  } else if (op === 'remove_edge') {
    current.edges = current.edges.filter(value => value.id !== data.edge_id)
  } else if (op === 'replace_workflow') {
    Object.assign(current, data.workflow)
  } else throw new Error('此操作不支持在循环内执行')
  const outer = cloned.nodes.find(node => node.id === path[0])!
  return { op: 'update_node', data: { node_id: outer.id, changes: { config: outer.config }, merge_config: false } }
}

/** Expose the runtime's implicit inputs in selectors without editing the saved graph. */
export function scopedFieldNodes(root: WorkflowGraph | undefined, path: string[]) {
  const current = workflowAtPath(root, path)
  if (!current || !root) return []
  const names = new Set<string>()
  for (let depth = 0; depth <= path.length; depth++) {
    const graph = workflowAtPath(root, path.slice(0, depth))
    for (const node of graph?.nodes || []) {
      if (node.type === 'start' && Array.isArray(node.config.inputs)) {
        for (const field of node.config.inputs) if (typeof field.name === 'string') names.add(field.name)
      }
    }
    if (depth === path.length) break
    const container = graph?.nodes.find(node => node.id === path[depth])
    if (!container) break
    Object.keys(container.config.variables || {}).forEach(name => names.add(name))
    const config = container.config
    const builtins = container.type === 'iteration' ? [String(config.item_name || 'item'), 'index']
      : ['iteration', 'previous', String(config.state_input_name || 'loop_state'), String(config.feedback_input_name || 'tool_feedback')]
    builtins.forEach(name => names.add(name))
  }
  return current.nodes.map(node => node.type !== 'start' ? node : {
    ...node, config: { ...node.config, inputs: [...names].map(name => ({ name })) },
  })
}
