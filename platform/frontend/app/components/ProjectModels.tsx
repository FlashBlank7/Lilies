'use client'

import { clientId } from '@/lib/client-id'

import { useCallback, useEffect, useState } from 'react'
import Papa from 'papaparse'
import { api, withFrontendToken } from '@/lib/platform'
import ModelingPanel from './ModelingPanel'
import {WorkflowValueField} from './WorkflowValueField'
import styles from './workspace-tools.module.css'

type Dataset = { id: string; name: string; mapping: { target: string } }
type Study = { id: string; name: string; status: string; best?: {candidate_id: string; slot: number; score: number} }
type Model = { model_ref: string; name: string; revision: number; status: string; candidate_id?: string }
type Trial = { slot: number; status: string; model: string; metrics: Record<string, number> }
type Candidate = { id: string; trials: Trial[] }
const statusNames:Record<string,string>={unbound:'待绑定',ready:'可用',registered:'已创建',running:'训练中',queued:'排队中',failed:'失败',interrupted:'已停止',completed:'已完成',finished:'已结束',sealed:'最终评价已完成',budget_exhausted:'预算已用完',target_reached:'达到验证目标'}
const ref = (node_id: string, ...path: string[]) => ({ $ref: { node_id, path } })

export default function ProjectModels({ projectId, onWorkflow, onTask, onTalk }: { projectId: string; onWorkflow: (id: string) => void; onTask: (id: string) => void; onTalk: (message: string) => void }) {
  const base = `/api/v1/projects/${projectId}`
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [studies, setStudies] = useState<Study[]>([])
  const [models, setModels] = useState<Model[]>([])
  const [dataset, setDataset] = useState('')
  const [predictionDataset, setPredictionDataset] = useState('')
  const [modelRef, setModelRef] = useState('prediction')
  const [studyId, setStudyId] = useState('')
  const [trials, setTrials] = useState<{candidate: string; trial: Trial}[]>([])
  const [trial, setTrial] = useState('')
  const [algorithm, setAlgorithm] = useState('forest')
  const [problem, setProblem] = useState('regression')
  const [csv, setCsv] = useState('')
  const [csvName, setCsvName] = useState('data.csv')
  const [file, setFile] = useState<File>()
  const [columns, setColumns] = useState<string[]>([])
  const [target, setTarget] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [packagePath, setPackagePath] = useState('')
  const [environments, setEnvironments] = useState<string[]>([])
  const [environment, setEnvironment] = useState('')
  const refresh = useCallback(async () => {
    const [d, s, m] = await Promise.all([api<Dataset[]>(base+'/datasets?limit=100'), api<Study[]>(base+'/modeling/studies?limit=100&summary=true'), api<Model[]>(base+'/models')])
    setDatasets(d); setStudies(s); setModels(m)
  }, [base])
  useEffect(() => { void refresh().catch(e => setError(String(e))); const timer = window.setInterval(() => void refresh().catch(e => setError(String(e))), 3000); return () => window.clearInterval(timer) }, [refresh])
  useEffect(() => { setTrials([]); setTrial(''); if (!studyId) return; let current = true
    void api<Candidate[]>(base+`/modeling/studies/${studyId}/candidates?limit=100`).then(items => { if(current) setTrials(items.flatMap(c => c.trials.filter(t => t.status==='completed').map(t => ({candidate:c.id, trial:t})))) }).catch(e=>setError(String(e)))
    return () => { current = false }
  }, [base, studyId, studies.find(s=>s.id===studyId)?.best?.candidate_id])
  async function act(fn:()=>Promise<void>) { setBusy(true); setError(''); setMessage(''); try { await fn(); await refresh() } catch(e) { setError(String(e)) } finally { setBusy(false) } }
  async function upload() {
    if(!file) return
    const form = new FormData(); form.append('file',file); form.append('mapping', JSON.stringify({kind:'tabular',target}))
    const response = await fetch(withFrontendToken('/api/platform'+base+'/datasets/upload'), {method:'POST', body:form})
    const data = await response.json(); if(!response.ok) throw new Error(data.detail || '上传失败')
    setDataset(data.id); setMessage(target?'数据已导入，可以开始训练。':'预测数据已导入，可在运行表单选择。')
  }
  async function train() {
    const study = await api<Study>(base+'/modeling/studies', {method:'POST',body:JSON.stringify({dataset_id:dataset, request_key:clientId(), name:'模型训练', evaluation:{problem,metric:problem==='regression'?'mae':'macro_f1'}, budget:{seconds:600,trials:5,trial_seconds:120}})})
    setStudyId(study.id)
    const task = await api<{id:string}>(base+`/modeling/studies/${study.id}/train`, {method:'POST',body:JSON.stringify({request_key:clientId(),engine:'sklearn',models:[algorithm],batch_size:1})})
    setMessage('训练已启动。你现在也可以创建预测工作流。'); onTask(task.id)
  }
  async function saveModel(bind:boolean) {
    const chosen = trials.find(t=>`${t.candidate}:${t.trial.slot}`===trial)
    if(bind && !chosen) throw new Error('请选择一个已完成的模型版本')
    await api(base+`/models/${modelRef}`,{method:'PUT',body:JSON.stringify({name:modelRef,expected_revision:models.find(m=>m.model_ref===modelRef)?.revision||0,study_id:bind?studyId:'',candidate_id:bind?chosen!.candidate:'',slot:bind?chosen!.trial.slot:0})})
    setMessage(bind?'模型已绑定。已有工作流下次运行使用此版本。':'模型引用已创建，可以稍后绑定。')
  }
  async function importModel() {
    await api(base+`/models/${modelRef}/import`,{method:'POST',body:JSON.stringify({name:modelRef,source_path:packagePath,environment,expected_revision:models.find(m=>m.model_ref===modelRef)?.revision||0})})
    setMessage('模型包与预处理已验证并绑定。已有运行保留原版本，新运行使用本次版本。')
  }
  async function predict() {
    const task=await api<{id:string}>(base+`/models/${modelRef}/predict`,{method:'POST',body:JSON.stringify({dataset_id:predictionDataset,request_key:clientId()})})
    onTask(task.id)
  }
  async function createWorkflow() {
    if(!models.some(m=>m.model_ref===modelRef)) await saveModel(false)
    const member=await api<{id:string}>(base+'/members',{method:'POST',body:JSON.stringify({name:`${modelRef} · 预测`})})
    const draft=await api<{revision:number}>(`/api/v1/applications/${member.id}/draft`)
    const workflow={nodes:[{id:'start',type:'start',title:'选择预测数据',config:{inputs:[{name:'dataset_id',type:'string',required:true}]},position:{x:40,y:120}},{id:'predict',type:'model_predict',title:'模型预测',config:{model_ref:modelRef,dataset_id:ref('$inputs','dataset_id')},position:{x:340,y:120}},{id:'end',type:'end',title:'预测结果',config:{outputs:{result:ref('predict','output')}},position:{x:640,y:120}}],edges:[{id:'start-predict',source:'start',target:'predict'},{id:'predict-end',source:'predict',target:'end'}]}
    await api(base+`/workflows/${member.id}/draft`,{method:'PUT',body:JSON.stringify({expected_revision:draft.revision,workflow})});onWorkflow(member.id)
  }
  return <>
    <section className={styles.section}><h2>训练模型</h2><p>上传数据后独立训练。工作流可以在训练前、训练中或训练后创建。</p>
      <div className={styles.row}><label>上传表格<input aria-label="上传训练数据" type="file" accept=".csv,.tsv" onChange={async e=>{const f=e.target.files?.[0];setFile(f);if(f){const p=Papa.parse<string[]>(await f.text(),{preview:1});setColumns(p.data[0]||[]);setTarget('')}}} /></label>
        <label>预测目标<select aria-label="预测目标字段" value={target} onChange={e=>setTarget(e.target.value)}><option value="">无标签数据（用于预测）</option>{columns.map(c=><option key={c}>{c}</option>)}</select></label><button disabled={busy||!file} onClick={()=>void act(upload)}>导入数据</button></div>
      <details><summary>粘贴表格数据</summary><p>也可以从表格复制 CSV 或 TSV 文本，首行是字段名。</p><label>文件名<input aria-label="表格文件名" value={csvName} onChange={e=>setCsvName(e.target.value)}/></label><textarea className={styles.editor} aria-label="表格数据" value={csv} onChange={e=>setCsv(e.target.value)}/><button disabled={!csv.trim()||busy} onClick={()=>{setFile(new File([csv],csvName||'data.csv',{type:'text/csv'}));setColumns(Papa.parse<string[]>(csv,{preview:1}).data[0]||[]);setTarget('');setMessage('表格已准备，请选择预测目标后导入数据。')}}>使用这份表格</button></details>
      <div className={styles.row}><label>训练数据集<select aria-label="训练数据集" value={dataset} onChange={e=>setDataset(e.target.value)}><option value="">选择数据集</option>{datasets.filter(d=>d.mapping.target).map(d=><option key={d.id} value={d.id}>{d.name} · {d.mapping.target}</option>)}</select></label>
        <label>任务类型<select value={problem} onChange={e=>setProblem(e.target.value)}><option value="regression">数值预测</option><option value="classification">分类</option></select></label>
        <label>算法<select value={algorithm} onChange={e=>setAlgorithm(e.target.value)}><option value="forest">随机森林</option><option value="linear">线性模型</option><option value="hist_gradient">梯度提升</option><option value="svm">支持向量机</option></select></label><button disabled={busy||!dataset} onClick={()=>void act(train)}>开始独立训练</button></div>
    </section>
    <section className={styles.section}><h2>可调用模型</h2><p>模型名称是工作流的稳定引用。先创建工作流，再绑定训练结果也可以。</p>
      <div className={styles.row}><label>模型名称<input aria-label="模型引用名称" value={modelRef} onChange={e=>setModelRef(e.target.value)} /></label><button disabled={busy||!modelRef||models.some(m=>m.model_ref===modelRef)} onClick={()=>void act(()=>saveModel(false))}>创建待绑定模型</button><button disabled={busy||!modelRef} onClick={()=>void act(createWorkflow)}>创建预测工作流</button></div>
      <div className={styles.row}><label>训练记录<select aria-label="绑定训练记录" value={studyId} onChange={e=>setStudyId(e.target.value)}><option value="">选择训练记录</option>{studies.map(s=><option key={s.id} value={s.id}>{s.name} · {statusNames[s.status]||s.status}</option>)}</select></label>
        <label>模型版本<select aria-label="绑定模型版本" value={trial} onChange={e=>setTrial(e.target.value)}><option value="">选择已完成的版本</option>{trials.map(t=><option key={`${t.candidate}:${t.trial.slot}`} value={`${t.candidate}:${t.trial.slot}`}>{t.trial.model} · {Object.entries(t.trial.metrics).map(([k,v])=>`${k} ${v.toPrecision(4)}`).join(' / ')}</option>)}</select></label><button disabled={busy||!trial||!modelRef} onClick={()=>void act(()=>saveModel(true))}>绑定模型版本</button></div>
      <details onToggle={event=>{if(event.currentTarget.open&&!environments.length)void api<string[]>(base+'/model-environments').then(setEnvironments).catch(e=>setError(String(e)))}}><summary>导入已有模型包</summary><p>先将模型上传到项目资料。模型包须包含预处理与预测器的 sklearn Pipeline；选择与原训练一致的本地环境，平台验证后绑定为独立版本。</p>
        <WorkflowValueField label="已有模型包" projectId={projectId} field="file_path" value={packagePath} onChange={setPackagePath} nodes={[]} nodeId="import" allowReference={false}/>
        <label>计算环境<select aria-label="模型包计算环境" value={environment} onChange={e=>setEnvironment(e.target.value)}><option value="">选择匹配的本地环境</option>{environments.map(name=><option key={name}>{name}</option>)}</select></label>
        <button disabled={busy||!modelRef||!packagePath||!environment} onClick={()=>void act(importModel)}>验证并绑定已有模型包</button>
      </details>
      {models.length ? <table className={styles.table}><thead><tr><th>模型名称</th><th>状态</th><th>绑定修订</th></tr></thead><tbody>{models.map(m=><tr key={m.model_ref}><td><button onClick={()=>setModelRef(m.model_ref)}>{m.name}</button></td><td>{statusNames[m.status]||m.status}</td><td>{m.revision}</td></tr>)}</tbody></table>:<p>尚未创建模型引用。</p>}
      <div className={styles.row}><label>新数据<select aria-label="直接预测的数据集" value={predictionDataset} onChange={e=>setPredictionDataset(e.target.value)}><option value="">选择无标签数据</option>{datasets.filter(d=>!d.mapping.target).map(d=><option key={d.id} value={d.id}>{d.name}</option>)}</select></label><button disabled={busy||!predictionDataset||!modelRef} onClick={()=>void act(predict)}>直接预测</button></div><p>直接预测使用所选模型，无需创建工作流；结果保存在运行记录中。</p>
      {message&&<p role="status">{message}</p>}{error&&<p role="alert">{error}</p>}
    </section>
    <ModelingPanel projectId={projectId} onTask={onTask} onContext={(context,message)=>onTalk(message||`请分析并继续改进模型：${context.label}，研究 ${context.study_id||''}`)} />
  </>
}
