'use client'
import { useEffect, useId, useState } from 'react'
import { api, type Block } from '@/lib/platform'
import { outputPaths, parseField, upstreamNodes, valueReference, type FieldNode, type FieldEdge } from '@/lib/workflow-fields'
import styles from './workflow-fields.module.css'

export type WorkflowFieldProps = {
  value: string; onChange: (value: string) => void; nodes: FieldNode[]; edges?: FieldEdge[]; blocks?: Block[]
  nodeId: string; label: string; projectId?: string; field?: string; allowReference?: boolean; disabled?: boolean; modelRole?: string
}
const resourceFields = new Set(['model_ref', 'knowledge_ref', 'dataset_id', 'model', 'file_path', 'source_path', 'labels_path', 'features.transformer_path'])
const fileFields = new Set(['file_path', 'source_path', 'labels_path', 'features.transformer_path'])

export function WorkflowValueField({ value, onChange, nodes, edges = [], blocks = [], nodeId, label, projectId, field, allowReference = true, disabled = false, modelRole = 'main' }: WorkflowFieldProps) {
  const reference = valueReference(parseField(value))
  const [options, setOptions] = useState<{ value: string; label: string }[]>([])
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(false)
  const [retry, setRetry] = useState(0)
  const pathId = useId()
  const resource = resourceFields.has(field || '')
  useEffect(() => {
    setOptions([]); setError(false); setLoading(false)
    if (!projectId || !resource) return
    let active = true
    const route = field === 'knowledge_ref' ? '/knowledge' : field === 'model_ref' ? '/models' : field === 'dataset_id' ? '/datasets?limit=100' : field === 'model' ? (modelRole === 'vision' ? '/vision-model' : '/agent-session') : ''
    const url = fileFields.has(field || '') ? `/api/v1/applications/${projectId}/workspace/files` : `/api/v1/projects/${projectId}${route}`
    setLoading(true)
    void api<unknown>(url).then(data => {
      if (!active) return
      if (Array.isArray(data)) setOptions(data.map(item => ({ value: item.knowledge_ref || item.model_ref || item.id || item.path, label: `${item.name || item.path}${item.status === 'unbound' ? ' · 待绑定' : item.status === 'pending' ? ' · 待建立索引' : ''}` })))
      else { const connection = data as { model?: string }; setOptions(connection.model ? [{ value: connection.model, label: connection.model }] : []) }
    }).catch(() => { if (active) setError(true) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [projectId, field, resource, modelRole, retry])
  const selectable = upstreamNodes(nodes, edges, nodeId)
  const selectedNode = nodes.find(node => reference?.node_id === '$inputs' ? ['start', 'schedule_trigger', 'event_subscription_trigger'].includes(node.type) : node.id === reference?.node_id)
  const paths = outputPaths(selectedNode, blocks)
  const unknownSource = reference && reference.node_id !== '$inputs' && !selectable.some(node => node.id === reference.node_id)
  function setRef(id: string, path: (string | number)[]) { onChange(JSON.stringify({ $ref: { node_id: id, path } })) }
  return <span className={styles.field}>
    {allowReference && <select disabled={disabled} aria-label={`${label}的来源`} value={reference ? 'reference' : 'literal'} onChange={event => event.target.value === 'literal' ? onChange('') : setRef(selectable[0]?.id || '$inputs', [])}>
      <option value="literal">直接填写 / 选择资源</option><option value="reference">引用节点输出</option>
    </select>}
    {reference ? <>
      <select disabled={disabled} aria-label={`${label}的节点`} value={reference.node_id} onChange={event => setRef(event.target.value, [])}>
        <option value="$inputs">本次运行输入</option>{unknownSource && <option value={reference.node_id}>{selectedNode?.title || reference.node_id} · 未连接为上游</option>}
        {selectable.map(node => <option key={node.id} value={node.id}>{node.title}</option>)}
      </select>
      {unknownSource && <small role="status">此引用尚未连接为上游；请先连线或重新选择。</small>}
      <select disabled={disabled} aria-label={`${label}的输出字段`} value={!reference.path.length || paths.some(path => JSON.stringify(path) === JSON.stringify(reference.path)) ? JSON.stringify(reference.path) : 'custom'} onChange={event => setRef(reference.node_id, JSON.parse(event.target.value))}>
        <option value="[]">整个节点输出</option>{reference.path.length > 0 && !paths.some(path => JSON.stringify(path) === JSON.stringify(reference.path)) && <option value="custom" disabled>自定义字段路径</option>}{paths.map(path => <option key={JSON.stringify(path)} value={JSON.stringify(path)}>{path.join('.')}</option>)}
      </select>
      <details><summary>自定义字段路径</summary><input disabled={disabled} aria-label={`${label}的字段路径`} list={pathId} value={reference.path.join('.')} placeholder="例如 output.result" onChange={event => setRef(reference.node_id, event.target.value ? event.target.value.split('.') : [])} /><datalist id={pathId}>{paths.map(path => <option key={JSON.stringify(path)} value={path.join('.')} />)}</datalist></details>
    </> : resource ? <>
      <select disabled={disabled || loading} aria-label={label} value={value} onChange={event => onChange(event.target.value)}>
        <option value="">{loading ? '正在读取项目资源…' : field === 'model' ? '使用项目连接的模型 / 稍后配置' : '稍后配置'}</option>
        {value && !options.some(option => option.value === value) && <option value={value}>{value} · 当前配置</option>}
        {options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      {!loading && !error && !options.length && <small className={styles.help}>尚无可选资源，可以先保存流程，稍后在项目中配置。</small>}
      {field === 'model' && <details><summary>指定同一 API 的其他模型</summary><input disabled={disabled} aria-label={`${label}的模型名称`} value={value} placeholder="填写服务支持的模型名；留空沿用项目配置" onChange={event => onChange(event.target.value)} /><small>使用当前项目连接的地址与凭据；模型不可用时会报错，不会换成其他模型。</small></details>}
      {fileFields.has(field || '') && <details><summary>手动填写项目文件路径</summary><input disabled={disabled} aria-label={`${label}的项目路径`} value={value} onChange={event => onChange(event.target.value)} /></details>}
    </> : <textarea disabled={disabled} aria-label={label} rows={2} value={value} onChange={event => onChange(event.target.value)} />}
    {error && <span role="alert">资源列表读取失败。<button type="button" disabled={disabled} onClick={() => setRetry(value => value + 1)}>重试</button></span>}
  </span>
}

function literalKind(value: unknown) { return valueReference(value) ? 'reference' : value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value }
export function WorkflowObjectFields(props: WorkflowFieldProps) {
  const object = parseField(props.value), entries = object && typeof object === 'object' && !Array.isArray(object) ? Object.entries(object) : []
  const [names, setNames] = useState<Record<number, string>>({})
  function change(index: number, key: string, value: unknown) {
    if (!key || entries.some(([name], i) => i !== index && name === key)) { setNames(previous => ({ ...previous, [index]: key })); return }
    setNames(previous => { const next = { ...previous }; delete next[index]; return next })
    props.onChange(JSON.stringify(Object.fromEntries(entries.map((entry, i) => i === index ? [key, value] : entry))))
  }
  return <span className={styles.entries}>{entries.map(([key, value], index) => {
    const kind = literalKind(value)
    return <span key={index} className={styles.entry}>
      <label>字段名<input disabled={props.disabled} aria-label={`${props.label}字段名 ${index + 1}`} value={names[index] ?? key} onChange={event => change(index, event.target.value, value)} /></label>
      {names[index] !== undefined && <small role="alert">字段名不能为空或重复；当前仍保留“{key}”。</small>}
      {kind !== 'reference' && <label>值类型<select disabled={props.disabled} aria-label={`${props.label} ${key}的值类型`} value={kind} onChange={event => change(index, key, ({ string: '', number: 0, boolean: false, object: {}, array: [], null: null } as Record<string, unknown>)[event.target.value])}>
        <option value="string">文本</option><option value="number">数字</option><option value="boolean">是 / 否</option><option value="object">对象（高级）</option><option value="array">数组（高级）</option><option value="null">空值</option>
      </select></label>}
      {kind === 'boolean' ? <label>值<select disabled={props.disabled} aria-label={`${props.label} ${key}`} value={String(value)} onChange={event => change(index, key, event.target.value === 'true')}><option value="true">是</option><option value="false">否</option></select></label>
        : kind === 'number' ? <label>值<input disabled={props.disabled} aria-label={`${props.label} ${key}`} type="number" value={String(value)} onChange={event => { if (event.target.value !== '' && Number.isFinite(Number(event.target.value))) change(index, key, Number(event.target.value)) }} /></label>
          : kind === 'null' ? <small>空值</small>
            : <WorkflowValueField {...props} label={`${props.label} ${key}`} field={key} value={typeof value === 'string' ? value : JSON.stringify(value)} onChange={next => { const parsed = parseField(next); change(index, key, valueReference(parsed) ? parsed : kind === 'string' || kind === 'reference' ? next : parsed) }} />}
      <button disabled={props.disabled} type="button" onClick={() => { setNames({}); props.onChange(JSON.stringify(Object.fromEntries(entries.filter((_, i) => i !== index)))) }}>删除字段</button>
    </span>
  })}<button disabled={props.disabled} type="button" onClick={() => { let index = entries.length + 1; while (entries.some(([name]) => name === 'field_' + index)) index++; props.onChange(JSON.stringify({ ...Object.fromEntries(entries), ['field_' + index]: '' })) }}>添加字段</button></span>
}

export function WorkflowInputFields({ value, onChange, human = false }: { value: string; onChange: (value: string) => void; human?: boolean }) {
  const parsed = parseField(value), items = Array.isArray(parsed) ? parsed as { name: string; type: string; required?: boolean; [key: string]: unknown }[] : []
  const labels: Record<string, string> = { string: '文本', number: '数字', boolean: '是 / 否', file: '文件', file_list: '文件列表', object: '对象', array: '数组', any: '任意类型' }
  function update(index: number, patch: Record<string, unknown>) { onChange(JSON.stringify(items.map((item, i) => i === index ? { ...item, ...patch } : item))) }
  return <span className={styles.entries}>{items.map((item, index) => <span key={index} className={styles.entry}>
    <label>名称<input aria-label={`输入名称 ${index + 1}`} value={item.name} onChange={event => update(index, { name: event.target.value })} /></label>
    {human && <><label>显示名称<input aria-label={`回答名称 ${index + 1}`} value={String(item.label || "")} onChange={e=>update(index,{label:e.target.value})}/></label><label>可选答案（每行一个，留空自由填写）<textarea aria-label={`回答选项 ${index + 1}`} value={Array.isArray(item.options)?item.options.join("\n"):""} onChange={e=>update(index,{options:e.target.value.split("\n").filter(Boolean)})}/></label></>}
    <label>类型<select aria-label={`输入类型 ${index + 1}`} value={item.type} onChange={event => update(index, { type: event.target.value })}>{Object.entries(labels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    <span className={styles.row}><label className={styles.checkbox}><input aria-label={`必填 ${index + 1}`} type="checkbox" checked={!!item.required} onChange={event => update(index, { required: event.target.checked })} />必填</label></span>
    <button type="button" onClick={() => onChange(JSON.stringify(items.filter((_, i) => i !== index)))}>删除输入</button>
  </span>)}<button type="button" onClick={() => { let index = items.length + 1; while (items.some(item => item.name === 'input_' + index)) index++; onChange(JSON.stringify([...items, { name: 'input_' + index, type: 'string', required: false, ...(human ? {label:'补充说明',options:[]} : {}) }])) }}>添加输入</button></span>
}

export function WorkflowArrayFields(props: WorkflowFieldProps) {
  const parsed = parseField(props.value), items = Array.isArray(parsed) ? parsed : []
  return <span className={styles.entries}>{items.map((item, index) => <span className={styles.entry} key={index}>
    <WorkflowValueField {...props} label={`${props.label} ${index + 1}`} field="item" value={typeof item === 'string' ? item : JSON.stringify(item)} onChange={value => props.onChange(JSON.stringify(items.map((entry, i) => i === index ? valueReference(parseField(value)) ? parseField(value) : value : entry)))} />
    <button disabled={props.disabled || items.length <= 1} type="button" onClick={() => props.onChange(JSON.stringify(items.filter((_, i) => i !== index)))}>删除值</button>
  </span>)}<button disabled={props.disabled} type="button" onClick={() => props.onChange(JSON.stringify([...items, '']))}>添加值</button></span>
}
