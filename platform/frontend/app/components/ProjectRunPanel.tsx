'use client'

import { clientId } from '@/lib/client-id'

import Link from 'next/link'
import ProjectTaskInput from './ProjectTaskInput'
import WorkflowInputTable, {type InputColumn} from './WorkflowInputTable'
import KnowledgeResults, {isKnowledgeSearchResult} from './KnowledgeResults'
import FeatureResults from './FeatureResults'
import { WorkflowValueField } from './WorkflowValueField'
import { useEffect, useRef, useState } from 'react'
import { api, withFrontendToken } from '@/lib/platform'
import { MarkdownDocument } from '@/lib/markdown'
import { resolveProjectLink } from '@/lib/project-links'
import { taskNames, type ProjectMember, type ProjectTask } from '@/lib/project-progress'
import styles from '@/app/projects/projects.module.css'

type Field = { name: string; label?: string; type: string; required?: boolean; default?: unknown; description?: string; options?: string[]; columns?: InputColumn[] }
type Draft = { snapshot: { workflow: { nodes: { type: string; config: { inputs?: Field[] } }[] } } }
type FileEntry = { path: string }

export function ProjectRunEvents({ runs, members }: { runs: ProjectTask['runs']; members: ProjectMember[] }) {
  const [events, setEvents] = useState<{ id: number; type: string; data: unknown }[]>([])
  const [truncated, setTruncated] = useState(false)
  const [error, setError] = useState('')
  return <>{runs?.map(run => <p key={run.id}><button onClick={async () => { setError(''); try { const result = await api<{ events: typeof events; truncated: boolean }>(`/api/v1/runs/${run.id}/events/list?after=0&limit=1000`); setEvents(result.events); setTruncated(result.truncated) } catch (cause) { setError(String(cause)) } }}>{members.find(member => member.id === run.application_id)?.name || '成员'} · {taskNames[run.status] || run.status} · 查看步骤输入输出</button></p>)}
    {error && <p role="alert">{error}</p>}{truncated && <p>当前显示最近 1000 条事件，较早事件未包含在此窗口中。</p>}
    {events.map(event => <details key={event.id}><summary>{event.type}</summary><pre>{JSON.stringify(event.data, null, 2)}</pre></details>)}
  </>
}

export function ProjectTaskOutput({ projectId, task, onTask }: { projectId: string; task: ProjectTask; onTask?: (task: ProjectTask) => void }) {
  const output = task.outputs || {}
  const markdown = task.presentation?.markdown || (typeof output.markdown === 'string' ? output.markdown : '') || task.presentation?.message || (typeof output.message === 'string' ? output.message : '')
  const results = [output, ...Object.values(output).map(v => typeof v==='string'?{artifact:v}:v)].filter((v):v is Record<string,unknown> => !!v && typeof v==='object' && !Array.isArray(v))
  const artifacts = task.presentation?.artifacts?.length ? task.presentation.artifacts : results.flatMap(result => Array.isArray(result.artifacts) ? result.artifacts : typeof result.file === 'string' ? [{file_path:result.file}] : [])
  const predictions = [...results.reduce((files,result)=>{
    const path=result.artifact
    if(typeof path==='string' && /^datasets\/[\w-]+\/files\/[\w./-]+$/.test(path) && !path.split('/').includes('..') && (!files.has(path)||Array.isArray(result.preview))) files.set(path,result)
    return files
  },new Map<string,Record<string,unknown>>()).values()]
  const knowledgeResults = results.filter(isKnowledgeSearchResult)
  const knowledgeAnswer = knowledgeResults.length === 1 && typeof output.markdown === 'string' && isKnowledgeSearchResult((output.knowledge || {}) as Record<string, unknown>)
  const training = results.find(result => Array.isArray(result.trials) && typeof result.study_id === 'string')
  const trials = training?.trials as {slot:number;model:string;status:string;metrics?:Record<string,number>;baseline?:Record<string,number>;error?:string}[] | undefined
  const evaluation = results.find(result => typeof result.rows === 'number' && result.metrics && typeof result.label === 'string')
  const classification = evaluation?.classification as {classes:{label:string;samples:number;precision:number;recall:number;f1:number}[];note:string} | undefined
  const acceptance = evaluation?.acceptance as {selection:{status:string;threshold:number|null;target_accuracy:number;validation:{accuracy:number;coverage:number;accepted:number}|null};test:{accepted:number;review:number;accuracy:number|null;coverage:number}} | undefined
  return <>
    {task.id && ['waiting_input','running','queued'].includes(task.status) && <ProjectTaskInput projectId={projectId} taskId={task.id} initialTask={task} onTask={onTask}/>}
    {results.filter(result => result.stage === 'before_fold_preprocessing').map((result, i) => <FeatureResults key={i} result={result as unknown as Parameters<typeof FeatureResults>[0]['result']} />)}
    {task.runs?.filter(run => run.reuse?.source_run_id).map(run => <p key={run.id}>使用当前配置创建了新运行，复用 {run.reuse!.nodes.length} 个已完成步骤{run.reuse!.nodes.length ? `（${(run.reuse!.titles || run.reuse!.nodes).join('、')}）` : ''}。其他步骤重新执行，原运行保持不变。</p>)}
    {!knowledgeAnswer && <MarkdownDocument source={markdown} resolveLink={href => resolveProjectLink(projectId, href)} emptyLabel={['queued', 'running'].includes(task.status) ? '正在运行，结果会自动显示。' : '本次运行的输出见下方详情。'} />}
    {knowledgeResults.map((result, i) => <KnowledgeResults key={i} result={result} answer={knowledgeAnswer ? output.markdown as string : undefined} question={typeof output.question === 'string' ? output.question : undefined} />)}
    {!!trials?.length && <section><h3>训练比较</h3><p>先比较模型与简单基线在同一划分下的表现，再看下方独立测试。计算完成只表示训练成功；若效果接近简单基线，应先检查标签、特征和样本覆盖。</p><details><summary>怎么看这些指标？</summary><p>accuracy 是预测正确的比例；macro_f1 平等考虑每个类别，避免多数类掩盖少数类。roc_auc 衡量类别区分能力，不是某个阈值下的准确率。这些值通常越高越好。MAE、RMSE 是数值预测误差，越小越好；R² 越接近 1 越好，也可能为负。</p></details><table><thead><tr><th>模型</th><th>验证指标</th><th>简单基线</th><th>结果</th></tr></thead><tbody>{trials.map(t=><tr key={t.slot}><td>{t.model}</td><td>{Object.entries(t.metrics||{}).map(([k,v])=>`${k}: ${v == null ? '无法计算' : Number(v).toPrecision(5)}`).join(' / ')}</td><td>{Object.entries(t.baseline||{}).map(([k,v])=>`${k}: ${v == null ? '无法计算' : Number(v).toPrecision(5)}`).join(' / ')}</td><td>{t.error|| (t.status==='completed'?'已完成':t.status)}</td></tr>)}</tbody></table></section>}
    {training && typeof training.study_id==='string' && typeof training.id==='string' && <p><a download href={withFrontendToken(`/api/platform/api/v1/projects/${projectId}/modeling/studies/${encodeURIComponent(training.study_id)}/candidates/${encodeURIComponent(training.id)}/download`)}>下载模型与训练记录 ↓</a></p>}
    {evaluation && <section><h3>独立测试</h3><p>{String(evaluation.rows)} 条样本 · {String(evaluation.label)}</p><p>{Object.entries(evaluation.metrics as Record<string,number|null>).map(([k,v])=>`${k}: ${v == null ? '无法计算' : Number(v).toPrecision(5)}`).join(' / ')}</p>
      {classification && <><p>精确率说明判为这一类的结果有多少正确；召回率说明实际属于这一类的样本找回了多少。少数类样本很少时，单次分数不稳定，应保留人工复核并补充样本。</p><p>{classification.note}</p><table><thead><tr><th>类别</th><th>样本数</th><th>精确率</th><th>召回率</th><th>F1</th></tr></thead><tbody>{classification.classes.map(row=><tr key={row.label}><td>{row.label}</td><td>{row.samples}{row.samples===0?' · 缺少此类测试样本':''}</td><td>{row.precision.toFixed(3)}</td><td>{row.recall.toFixed(3)}</td><td>{row.f1.toFixed(3)}</td></tr>)}</tbody></table></>}
    </section>}
    {acceptance && <section><h3>自动采纳与人工复核</h3>
      <p>{acceptance.selection.status==='selected'?`验证数据选择阈值 ${acceptance.selection.threshold?.toPrecision(5)}`:'验证数据未找到满足要求的阈值，全部交由复核。'} · 验证目标准确率 {acceptance.selection.target_accuracy}</p>
      {acceptance.selection.validation && <p>阈值选择时：采纳 {acceptance.selection.validation.accepted} 条，准确率 {acceptance.selection.validation.accuracy.toFixed(3)}。这是选择依据，不是独立测试成绩。</p>}
      <p>固定阈值的独立测试：采纳 {acceptance.test.accepted} 条，复核 {acceptance.test.review} 条；采纳部分准确率 {acceptance.test.accuracy==null?'无法计算':acceptance.test.accuracy.toFixed(3)}，覆盖率 {acceptance.test.coverage.toFixed(3)}。</p>
      <p>自动采纳指采用模型的分类建议，不等同于产品放行；具体工艺规则仍需另行配置。</p>
    </section>}
    {predictions.map((result,i)=>{const rows=Array.isArray(result.preview)?result.preview.slice(0,20) as Record<string,unknown>[]:[];const columns=rows.length?Object.keys(rows[0]).slice(0,8):[]
      return <section key={i}><h3>预测结果</h3><p>结果已保存，本次使用的模型版本固定在运行记录中。</p><p>预测值是模型的判断；分类概率表示模型给出的倾向，不等于业务放行承诺。需要采纳或复核规则时，使用项目中的模型与规则流程；只改规则可复用已有预测，无需重训。</p>
        {!!rows.length&&<div style={{overflowX:'auto'}}><table><thead><tr>{columns.map(key=><th key={key}>{key==='prediction'?'预测值':key}</th>)}</tr></thead><tbody>{rows.map((row,j)=><tr key={j}>{columns.map(key=><td key={key}>{typeof row[key]==='number'?Number(row[key]).toPrecision(6):String(row[key]??'')}</td>)}</tr>)}</tbody></table><p>显示前 {rows.length} 行，完整结果见下载文件。</p></div>}
        <a download href={withFrontendToken(`/api/platform/api/v1/projects/${projectId}/${result.artifact}`)}>下载预测结果 CSV ↓</a></section>})}
    {artifacts.map((entry: unknown, i: number) => {
      if (!entry || typeof entry !== 'object') return null
      const item = entry as { file_path?: string; label?: string }
      if (!item.file_path || !/^(results|solution)\//.test(item.file_path) || item.file_path.split('/').includes('..')) return null
      return <p key={i}><a download href={withFrontendToken(`/api/platform/api/v1/applications/${projectId}/workspace/files/${item.file_path.split('/').map(encodeURIComponent).join('/')}`)}>{item.label || item.file_path.split('/').pop()} ↓</a></p>
    })}
  </>
}

export default function ProjectRunPanel({ projectId, members, initialWorkflowId, reuseTask, onTask }: {
  projectId: string; members: ProjectMember[]; initialWorkflowId?: string; reuseTask?: ProjectTask; onTask?: (task: ProjectTask) => void
}) {
  const [workflowId, setWorkflowId] = useState(initialWorkflowId || projectId)
  const [fields, setFields] = useState<Field[]>([])
  const [values, setValues] = useState<Record<string, string>>({})
  const [files, setFiles] = useState<FileEntry[]>([])
  const [task, setTask] = useState<ProjectTask | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const lock = useRef(false)
  const base = `/api/v1/projects/${projectId}`
  const active = Boolean(task && ['queued', 'running'].includes(task.status))
  useEffect(() => {
    let current = true
    setLoading(true); setError('')
    Promise.all([api<Draft>(`/api/v1/applications/${workflowId}/draft`), api<FileEntry[]>(`/api/v1/applications/${projectId}/workspace/files`)]).then(([draft, available]) => {
      if (!current) return
      const inputs = draft.snapshot.workflow.nodes.find(node => node.type === 'start')?.config.inputs || []
      const previous = workflowId === reuseTask?.workflow_id ? reuseTask.inputs as Record<string, unknown> | undefined : undefined
      setFields(inputs); setValues(Object.fromEntries(inputs.map(field => {
        const value = previous && Object.hasOwn(previous, field.name) ? previous[field.name] : field.default
        return [field.name, value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value)]
      })))
      setFiles(available); setLoading(false)
    }).catch(cause => { if (current) { setError(String(cause)); setLoading(false) } })
    return () => { current = false }
  }, [projectId, workflowId, reuseTask])
  useEffect(() => {
    if (!task || !active) return
    let current = true
    const timer = window.setInterval(() => {
      void api<ProjectTask>(`${base}/tasks/${task.id}`).then(next => { if (current) { setTask(next); onTask?.(next) } }).catch(cause => { if (current) setError(String(cause)) })
    }, 1500)
    return () => { current = false; window.clearInterval(timer) }
  }, [base, task?.id, active, onTask])
  useEffect(() => {
    if (!task || active) return
    let current = true
    void api<FileEntry[]>(`/api/v1/applications/${projectId}/workspace/files`).then(available => {
      if (current) setFiles(available)
    }).catch(cause => { if (current) setError(`结果仍已保存，但文件列表更新失败：${String(cause)}`) })
    return () => { current = false }
  }, [projectId, task?.id, task?.status, active])
  async function start() {
    if (lock.current) return
    lock.current = true; setBusy(true); setError('')
    try {
      const inputs: Record<string, unknown> = {}
      for (const field of fields) {
        const value = values[field.name] ?? ''
        const raw = ['object', 'array', 'any'].includes(field.type) ? value.trim() : value
        if (!raw && field.required) throw new Error(`请填写 ${field.name}`)
        if (!raw) {
          // Omitting a cleared field would restore the workflow's old default.
          if (field.type === 'array') inputs[field.name] = []
          else if (['string', 'file'].includes(field.type)) inputs[field.name] = ''
          continue
        }
        if (field.type === 'number') {
          const number = Number(raw)
          if (!Number.isFinite(number)) throw new Error(`${field.name} 需要有效数字`)
          inputs[field.name] = number
        } else if (field.type === 'boolean') inputs[field.name] = raw === 'true'
        else if (['object', 'array', 'any'].includes(field.type)) {
          try { inputs[field.name] = JSON.parse(raw) } catch { throw new Error(`${field.name} 需要有效 JSON`) }
        } else inputs[field.name] = raw
      }
      const next = await api<ProjectTask>(base + '/tasks', { method: 'POST', body: JSON.stringify({ request_key: clientId(), mode: 'workflow', workflow_id: workflowId, inputs, purpose: 'customer_trial', ...(reuseTask?.workflow_id === workflowId ? {reuse_task_id: reuseTask.id} : {}) }) })
      setTask(next); onTask?.(next)
    } catch (cause) { setError(String(cause)) } finally { setBusy(false); lock.current = false }
  }
  return <section className={styles.panel} aria-label="手动运行工作流">
    <h2>运行工作流</h2><p>选择工作流和本次资料，直接运行当前已保存的配置。</p>
    {reuseTask?.workflow_id === workflowId && <p>按当前配置重算：按各步骤实际读取的输入和依赖判断能否复用。旧运行缺少复用记录时会重新计算。代码默认重跑，可在代码积木配置中声明允许复用。</p>}
    <label>入口工作流<select aria-label="入口工作流" disabled={active || busy} value={workflowId} onChange={event => { setWorkflowId(event.target.value); setTask(null) }}>{members.map(member => <option key={member.id} value={member.id}>{member.id === projectId ? '主流程 · ' : ''}{member.name}</option>)}</select></label>
    <p><Link href={`/applications/${workflowId}?tab=edit`} target="_blank">编辑这条工作流 ↗</Link></p>
    {loading ? <p role="status">正在读取输入配置…</p> : fields.map(field => <div key={field.name}>
      {field.type==='array' && field.columns?.length ? <WorkflowInputTable name={field.name} label={field.label||field.name} columns={field.columns} value={values[field.name]||'[]'} disabled={active||busy} onChange={value=>setValues(previous=>({...previous,[field.name]:value}))}/> : <label>{field.label || field.name}{field.required ? ' *' : ''}
        {field.name === 'dataset_id' ? <WorkflowValueField allowReference={false} disabled={active || busy} projectId={projectId} field="dataset_id" nodeId="run" nodes={[]} label="预测数据集" value={values[field.name] || ''} onChange={next => setValues(previous => ({...previous, [field.name]: next}))} /> : field.type === 'boolean' ? <select aria-label={field.name} disabled={active || busy} value={values[field.name] || ''} onChange={event => setValues(previous => ({ ...previous, [field.name]: event.target.value }))}><option value="">请选择</option><option value="true">是</option><option value="false">否</option></select>
          : field.type === 'string' && field.options?.length ? <select aria-label={field.name} disabled={active || busy} value={values[field.name] || ''} onChange={event => setValues(previous => ({ ...previous, [field.name]: event.target.value }))}><option value="">请选择</option>{field.options.map(option => <option key={option} value={option}>{option}</option>)}</select>
          : <textarea aria-label={field.name} rows={['object', 'array', 'any'].includes(field.type) ? 4 : 2} disabled={active || busy} value={values[field.name] || ''} onChange={event => setValues(previous => ({ ...previous, [field.name]: event.target.value }))} />}
      </label>}{field.description && <p>{field.description}</p>}
      {(field.type === 'file' || /(?:path|file|document|attachment)$/i.test(field.name)) && files.length > 0 && <label>选择项目文件<select aria-label={`为 ${field.name} 选择项目文件`} disabled={active || busy} value="" onChange={event => setValues(previous => ({ ...previous, [field.name]: event.target.value }))}><option value="">从已上传资料或结果中选择…</option>{files.map(file => <option key={file.path} value={file.path}>{file.path}</option>)}</select></label>}
    </div>)}
    <div className={styles.actions}><button className={styles.primary} disabled={loading || busy || active} onClick={() => void start()}>{busy ? '正在启动…' : active ? '正在运行…' : '启动工作流'}</button>
      {active && task && <button disabled={busy} onClick={async () => { setBusy(true); try { const next = await api<ProjectTask>(`${base}/tasks/${task.id}/stop`, { method: 'POST' }); setTask(next); onTask?.(next) } catch (cause) { setError(String(cause)) } finally { setBusy(false) } }}>停止运行</button>}
    </div>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {task && <section aria-label="本次运行结果"><h3>{taskNames[task.status] || task.status}</h3>{task.error && <p role="alert">{task.error}</p>}<ProjectTaskOutput projectId={projectId} task={task} onTask={next=>{setTask(next);onTask?.(next)}} />
      <details><summary>实际输入输出与运行详情</summary><pre>{JSON.stringify({ inputs: task.inputs, outputs: task.outputs }, null, 2)}</pre><ProjectRunEvents key={task.id} runs={task.runs} members={members} />
      </details>
    </section>}
  </section>
}
