import type { Block } from './platform'

export type FieldNode = { id: string; title: string; type: string; config: Record<string, unknown> }
export type FieldEdge = { source: string; target: string }
export type ValueReference = { node_id: string; path: (string | number)[] }

export function parseField(value: string): unknown {
  try { return JSON.parse(value) } catch { return value }
}

export function valueReference(value: unknown): ValueReference | undefined {
  if (!value || typeof value !== 'object' || !('$ref' in value)) return
  const ref = value.$ref as ValueReference
  if (ref && typeof ref.node_id === 'string' && Array.isArray(ref.path)) return ref
}

export function upstreamNodes(nodes: FieldNode[], edges: FieldEdge[], nodeId: string): FieldNode[] {
  const found = new Set<string>(), pending = [nodeId]
  while (pending.length) {
    const target = pending.pop()!
    for (const edge of edges) {
      if (edge.target === target && edge.source !== nodeId && !found.has(edge.source)) {
        found.add(edge.source); pending.push(edge.source)
      }
    }
  }
  return nodes.filter(node => found.has(node.id))
}

function schemaPaths(schema: unknown, prefix: string[] = [], depth = 0): string[][] {
  if (!schema || typeof schema !== 'object' || depth > 5) return []
  const properties = (schema as { properties?: Record<string, unknown> }).properties
  return Object.entries(properties || {}).flatMap(([key, child]) => {
    const path = [...prefix, key]
    return [path, ...schemaPaths(child, path, depth + 1)]
  })
}

export function outputPaths(node: FieldNode | undefined, blocks: Block[] = []): string[][] {
  if (!node) return []
  if (['start', 'schedule_trigger', 'event_subscription_trigger'].includes(node.type)) {
    const inputs = node.config.inputs
    return Array.isArray(inputs) ? inputs.filter(item => typeof item?.name === 'string').map(item => [item.name]) : []
  }
  const ports = blocks.find(block => block.type === node.type)?.output_ports.map(port => [port.name]) || []
  let dynamic: string[][] = []
  if (node.type === 'llm') dynamic = [['text'], ['structured'], ...schemaPaths(node.config.structured_output, ['structured'])]
  if (['end', 'answer'].includes(node.type)) dynamic = Object.keys(node.config.outputs || {}).map(key => [key])
  if (node.type === 'variable_assigner') dynamic = Object.keys(node.config.assignments || {}).map(key => ['output', key])
  const values = [...ports, ...dynamic]
  return values.length ? [...new Map(values.map(path => [JSON.stringify(path), path])).values()] : [['output']]
}

export const workflowFieldLabels: Record<string, string> = {
  inputs: '输入字段', outputs: '输出字段', input: '输入值', variables: '变量', assignments: '赋值',
  cases: '条件分支', default_branch: '默认分支', headers: '请求头', query: '查询参数',
  method: '请求方法', url: '请求地址', body: '请求内容', timeout_seconds: '超时秒数',
  model_ref: '模型资源', dataset_id: '数据集', file_path: '项目文件', template: '文本模板',
  code: '代码', items: '待处理列表', parallelism: '同时处理数量', max_iterations: '最多循环次数',
}
export const workflowModelHelp: Record<string, string> = {
  system: '描述模型在本次调用中的角色和规则。', prompt: '填写本次问题，或选择上游字段。',
  model: '选择此用途已配置的项目模型；留空时使用项目连接的默认值。',
  model_role: '主模型用于文本处理；视觉模型使用独立配置的图片理解连接。',
  images: '选择项目图片，或引用上游返回的图片列表。',
  max_output_tokens: '本次回答的最大 token 数，最终受模型服务限制。',
  structured_output: '需要固定结构的输出时，在高级配置中填写 JSON Schema；可以留空。',
}
