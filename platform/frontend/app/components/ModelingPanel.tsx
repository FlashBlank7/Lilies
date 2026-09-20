'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { BarChart, Bar, CartesianGrid, LineChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Activity, ArrowUpRight, Database, Download, Upload } from 'lucide-react'
import { api, withFrontendToken } from '@/lib/platform'
import ReadingDialog from './ReadingDialog'
import TrainingNote from './TrainingNote'
import styles from './modeling.module.css'

type Metric = Record<string, number | null>
type Distribution = { label: string; count: number }[]
type Profile = { rows: number; duplicates: number; sampled: boolean; columns: { name: string; dtype: string; missing: number; unique: number; outliers?: number; distribution?: Distribution }[]; time?: { start: string; end: string; median_interval_seconds: number; preview?: { sampled: boolean; series: string; group: string; points: {time: string; value: number}[] } }; runtime?: { seconds: number; peak_memory_bytes: number }; labels?: Profile }
type Dataset = { id: string; name: string; status: string; mapping: { target?: string; kind: string }; profile?: Profile; preview?: Profile }
type Trial = { completed_at?: string; slot: number; status: string; model: string; metrics?: Metric; baseline?: Metric; seconds: number; error?: string; warnings?: string[]; importance?: { name: string; value: number }[]; group_errors?: { group: string; samples: number; error: number }[] }
type Candidate = { search_decision?: { stage: string; label: string; parent_score?: number | null }; created_at?: string; id: string; status: string; hypothesis: string; parent_id?: string; feedback_task_id?: string; task_id: string; run_id: string; trials: Trial[]; error?: string; current?: { model: string; slot: number }; features_result?: { name: string; source: string; calculation: string; window_seconds?: number; condition?: string }[] }
type Study = { search_strategy?: string; repair_candidate_id?: string; test_result?: { metrics: Metric; rows: number; label: string }; id: string; name: string; dataset_id: string; status: string; next_action: string; error?: string; item_id?: string; budget: { seconds: number; trials: number }; trials_used: number; baseline?: Metric; best?: { candidate_id: string; slot: number; model: string; score: number }; evaluation: { metric: string; split: string; target_score?: number | null }; split?: { missing_labels: number; evaluation_label: string } }
export type ModelingContext = { dataset_id?: string; study_id?: string; candidate_id?: string; task_id?: string; item_id?: string; label: string }
const statusNames: Record<string, string> = { registered: '已导入', ready: '准备实验', running: '正在计算', queued: '等待计算资源', completed: '本批已完成', failed: '本批失败', interrupted: '已中断，可继续', budget_exhausted: '本次预算已用完', target_reached: '验证指标达到目标', finished: '本次搜索已结束', sealed: '最终评价已完成' }
const modelNames: Record<string, string> = { linear: '线性模型', forest: '随机森林', hist_gradient: '梯度提升', svm: '支持向量机', autogluon: 'AutoGluon' }
const num = (n: number | null | undefined) => n == null ? '尚无结果' : n.toLocaleString('zh-CN', { maximumSignificantDigits: 5 })

export default function ModelingPanel({ projectId, onContext, onTask, compact = false }: { projectId: string; onContext: (context: ModelingContext, message?: string) => void; onTask?: (id: string) => void; compact?: boolean }) {
  const base = '/api/v1/projects/' + projectId
  const [studies, setStudies] = useState<Study[]>([])
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [selected, setSelected] = useState('')
  const [datasetId, setDatasetId] = useState('')
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState('概览')
  const [note, setNote] = useState<{ candidateId: string; slot: number }>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [column, setColumn] = useState('')
  const [more, setMore] = useState(false)
  const [connected, setConnected] = useState(false)
  const [datasetDetail, setDatasetDetail] = useState<Dataset>()
  const key = 'lilies:modeling:' + projectId
  const refresh = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([api<Study[]>(base + '/modeling/studies?limit=50&summary=true'), api<Dataset[]>(base + '/datasets?limit=50&summary=true')])
      if (!Array.isArray(s) || !Array.isArray(d)) throw new Error('建模接口返回格式不正确，请重试连接')
      setStudies(s); setDatasets(d); setConnected(true); setError(previous => previous.includes('PlatformApiError') || previous.includes('建模接口') ? '' : previous)
    } catch (cause) { setError(String(cause)) }
  }, [base])
  useEffect(() => {
    try { const saved = JSON.parse(sessionStorage.getItem(key) || '{}'); setTab(saved.tab || '概览'); setSelected(saved.selected || ''); setDatasetId(saved.datasetId || '') } catch {}
    void refresh()
  }, [key, refresh])
  useEffect(() => { if (!connected) return; const timer = window.setInterval(() => void refresh(), 3000); return () => window.clearInterval(timer) }, [connected, refresh])
  const study = studies.find(s => s.id === selected) || studies[0]
  const datasetSummary = datasets.find(d => d.id === (datasetId || study?.dataset_id)) || datasets[0]
  const dataset = datasetDetail?.id === datasetSummary?.id ? datasetDetail : datasetSummary
  const did = datasetSummary?.id
  const loadDataset = useCallback(async () => {
    if (!did || !open) return
    try { setDatasetDetail(await api<Dataset>(base + '/datasets/' + did)) } catch (cause) { setError(String(cause)) }
  }, [base, did, open])
  useEffect(() => { void loadDataset() }, [loadDataset])
  const sid = study?.id
  const candidateScope = base + '/' + sid
  const currentCandidateScope = useRef(candidateScope)
  currentCandidateScope.current = candidateScope
  const loadCandidates = useCallback(async (offset = 0) => {
    if (!sid) return
    try {
      const items = await api<Candidate[]>(base + '/modeling/studies/' + sid + '/candidates?limit=100&offset=' + offset + '&summary=' + !open)
      if (currentCandidateScope.current !== candidateScope) return
      setCandidates(previous => offset ? [...previous, ...items.filter(item => !previous.some(p => p.id === item.id))] : [...items, ...previous.filter(p => !items.some(item => item.id === p.id))])
      setMore(items.length === 100)
    } catch (cause) { if (currentCandidateScope.current === candidateScope) setError(String(cause)) }
  }, [base, sid, open, candidateScope])
  useEffect(() => { setCandidates([]); setNote(undefined) }, [sid])
  useEffect(() => { if (!sid) return; void loadCandidates(); const timer = window.setInterval(() => void loadCandidates(), 3000); return () => window.clearInterval(timer) }, [loadCandidates, sid])
  function remember(nextTab: string, nextSelected = selected, nextDataset = datasetId) { setTab(nextTab); setSelected(nextSelected); setDatasetId(nextDataset); try { sessionStorage.setItem(key, JSON.stringify({ tab: nextTab, selected: nextSelected, datasetId: nextDataset })) } catch {} }
  async function action(fn: () => Promise<unknown>) { setBusy(true); setError(''); try { await fn(); await refresh(); await loadCandidates(); await loadDataset() } catch (cause) { setError(String(cause)) } finally { setBusy(false) } }
  const active = study && ['ready', 'running', 'queued', 'interrupted'].includes(study.status) ? candidates.find(c => ['running', 'queued', 'interrupted', 'failed'].includes(c.status)) : undefined
  const bestCandidate = candidates.find(c => c.id === study?.best?.candidate_id)
  const bestTrial = bestCandidate?.trials.find(t => t.slot === study?.best?.slot)
  const repairCandidate = candidates.find(c => c.id === study?.repair_candidate_id)
  function feedback(candidate = bestCandidate, message?: string) {
    onContext({ label: study?.name || dataset?.name || '数据分析', study_id: study?.id, dataset_id: study?.dataset_id || dataset?.id, candidate_id: candidate?.id, task_id: candidate?.task_id || undefined, item_id: study?.item_id || undefined }, message)
    setOpen(false)
  }
  const metric = study?.evaluation.metric || 'mae'
  const baseline = study?.baseline?.[metric]
  const target = study?.evaluation.target_score
  const gap = target == null || !study?.best ? null : ['mae', 'rmse'].includes(metric) ? study.best.score - target : target - study.best.score
  const profile = dataset?.profile || dataset?.preview
  const visibleColumns = [...(profile?.columns || []), ...(profile?.labels?.columns.filter(c => c.name === dataset?.mapping.target) || [])]
  const field = visibleColumns.find(c => c.name === column) || visibleColumns.find(c => c.name === dataset?.mapping.target) || visibleColumns[0]
  let seconds = 0
  const orderedTrials = candidates.flatMap(c => c.trials.map(t => ({ c, t }))).sort((a, b) => (a.t.completed_at || a.c.created_at || '').localeCompare(b.t.completed_at || b.c.created_at || '') || a.c.id.localeCompare(b.c.id) || a.t.slot - b.t.slot)
  const curve = orderedTrials.map(({ t }) => { seconds += t.seconds; return { seconds: Number(seconds.toFixed(2)), score: t.metrics?.[metric] ?? null, model: modelNames[t.model] || t.model } })
  async function upload(file: File) {
    await action(async () => {
      const form = new FormData(); form.append('file', file)
      const result = await api<Dataset>(base + '/datasets/upload', { method: 'POST', body: form })
      setDatasets(previous => [result, ...previous.filter(d => d.id !== result.id)])
      setDatasetDetail(result)
      remember('数据', selected, result.id)
      onContext({ dataset_id: result.id, label: result.name }, '请先分析这份数据，和我确认预测目标及评价方式。')
      setOpen(true)
    })
  }
  function report() {
    if (!study) return
    const text = `# ${study.name}\n\n${statusNames[study.status] || study.status}\n\n- 当前指标：${metric} = ${num(study.best?.score)}\n- 朴素参照：${num(baseline)}\n- 目标：${num(target)}\n- 评价：${study.test_result ? '保留测试集 ' + study.test_result.rows + ' 条，' + metric + ' = ' + num(study.test_result.metrics[metric]) : study.split?.evaluation_label || '尚未评估'}\n- 下一步：${study.next_action}\n\n| 模型 | 指标 | 秒 | 状态 |\n|---|---|---|---|\n` + candidates.flatMap(c => c.trials.map(t => `| ${modelNames[t.model] || t.model} | ${num(t.metrics?.[metric])} | ${t.seconds.toFixed(2)} | ${[t.error || statusNames[t.status] || t.status, ...(t.warnings || [])].join('；')} |`)).join('\n')
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' })); const link = document.createElement('a'); link.href = url; link.download = '建模结果.md'; link.click(); URL.revokeObjectURL(url)
  }
  const studyCard = study && <article className={styles.card}>
      <div className={styles.heading}><div><small><Activity size={14} /> {statusNames[study.status] || study.status}</small><h3>{study.name}</h3></div><button onClick={() => setOpen(true)}>查看结果 <ArrowUpRight size={15} /></button></div>
      <div className={styles.metrics}><div><small>当前最佳 · {metric.toUpperCase()}</small><strong>{num(study.best?.score)}</strong><span>{study.best ? modelNames[study.best.model] || study.best.model : '首个模型完成后显示'}</span></div><div><small>朴素参照</small><strong>{num(baseline)}</strong><span>同一评价划分</span></div><div><small>与目标的差距</small><strong>{gap == null ? target == null ? '待确认目标' : '目标 ' + num(target) : gap <= 0 ? '已达到' : num(gap)}</strong><span>{study.trials_used} 次试验已保存</span></div></div>
      <p>{active?.current ? '正在训练：' + (modelNames[active.current.model] || active.current.model) : study.next_action}</p>
      {study.error && <p role="alert">{study.error}</p>}
      {bestTrial?.warnings?.map(w => <p key={w}>{w}</p>)}
      {repairCandidate && <button onClick={() => feedback(repairCandidate, "请读取这次实验的实际错误，提交关联的修复候选并继续；保留已有结果和评价划分。")}>修复并继续研究</button>}
      <div className={styles.actions}><button onClick={() => feedback()}>反馈这个结果</button>{active?.task_id && <><button onClick={() => onTask?.(active.task_id)}>查看运行</button><button disabled={busy} onClick={() => void action(() => api(base + '/tasks/' + active.task_id + (['running', 'queued'].includes(active.status) ? '/stop' : '/resume'), { method: 'POST', body: JSON.stringify({ message: '继续已保存的建模实验' }) }))}>{['running', 'queued'].includes(active.status) ? '停止计算' : '继续原任务'}</button></>}</div>
    </article>
  return <section className={`${styles.panel} ${compact ? styles.compact : ''}`} aria-label="数据与建模">
    <div className={styles.toolbar}><span><Database size={15} /> 数据与建模</span><label className={styles.upload}><Upload size={14} /> 导入数据<input type="file" accept=".csv,.tsv,.xlsx" disabled={busy} aria-label="上传建模数据" onChange={e => { const file = e.target.files?.[0]; if (file) void upload(file); e.target.value = '' }} /></label>{datasets.length > 0 && <button onClick={() => { setTab('数据'); setOpen(true) }}>查看数据</button>}{compact && study && <><small>{study.best ? `最佳 ${metric.toUpperCase()} ${num(study.best.score)}` : statusNames[study.status] || study.status}</small><button onClick={() => setOpen(true)}>查看建模结果 <ArrowUpRight size={13} /></button></>}</div>
    {!compact && studyCard}
    {error && <p role="alert" className={styles.error}>{error} <button onClick={() => void refresh()}>重试连接</button></p>}
    {open && <ReadingDialog wide storageKey={key + ":" + (study?.id || dataset?.id) + ":" + tab + (note ? ":note:" + note.candidateId + ":" + note.slot : "")} title={study?.name || '数据分析'} onClose={() => setOpen(false)}>
      {studies.length > 1 && <label>建模研究<select aria-label="选择建模研究" value={study?.id} onChange={e => remember(tab, e.target.value, '')}>{studies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>}
      <div role="tablist" className={styles.tabs}>{['概览', '数据', '特征', '实验'].map(name => <button key={name} role="tab" aria-selected={tab === name} onClick={() => remember(name)}>{name}</button>)}</div>
      <div role="tabpanel">
        {tab === '概览' && <>{compact && studyCard}<h3>{study?.best ? `${modelNames[study.best.model] || study.best.model} 当前表现最好` : '先得到第一个可用模型'}</h3>{!compact && <p>{study?.next_action || '数据已保存。通过对话说明目标，统筹会分析数据并建立建模研究。'}</p>}<p>{study?.test_result ? '保留测试集 ' + study.test_result.rows + ' 条 · ' + metric.toUpperCase() + ' ' + num(study.test_result.metrics[metric]) + '。这项结果未参与选模' : study?.split?.evaluation_label || '尚未开始评价'}。训练完成与业务目标达成分别判断。</p><div className={styles.actions}><button onClick={() => feedback(undefined, '请用当前最佳模型生成可复用的预测工作流，并说明无标签输入格式。')}>创建预测工作流</button>{study?.best && <a href={withFrontendToken('/api/platform' + base + '/modeling/studies/' + study.id + '/candidates/' + study.best.candidate_id + '/download')}><Download size={14} /> 下载模型</a>}<button onClick={report} disabled={!study}>导出结果报告</button></div>{bestTrial?.group_errors?.length ? <><h4>需要关注的分组</h4><div className={styles.table}><table><thead><tr><th>设备／批次</th><th>验证样本</th><th>平均误差</th></tr></thead><tbody>{bestTrial.group_errors.slice(0, 10).map(g => <tr key={g.group}><td>{g.group}</td><td>{g.samples}</td><td>{num(g.error)}</td></tr>)}</tbody></table></div></> : null}</>}
        {tab === '数据' && <><label>数据版本<select aria-label="选择数据集" value={dataset?.id || ''} onChange={e => remember(tab, selected, e.target.value)}>{datasets.map(d => <option key={d.id} value={d.id}>{d.name} · {d.id.slice(0, 6)}</option>)}</select></label>{dataset && <div className={styles.actions}><button disabled={busy} onClick={() => void action(() => api(base + '/datasets/' + dataset.id + '/profile?sampled=true', { method: 'POST' }))}>快速预览</button><button disabled={busy} onClick={() => void action(() => api(base + '/datasets/' + dataset.id + '/profile', { method: 'POST' }))}>完整分析</button><button onClick={() => { onContext({ dataset_id: dataset.id, label: dataset.name }, '请分析这份数据，确认目标、字段含义和合适的评价划分。'); setOpen(false) }}>和统筹分析</button></div>}{busy && <p role="status">正在分析，完成后显示实际统计。</p>}{profile ? <><p>{profile.sampled ? '抽样预览（最多前 1,000 行）' : '完整扫描'} · {profile.rows.toLocaleString()} 行 · {profile.columns.length} 列 · {profile.duplicates} 行重复。异常值仅提示，未自动删除。</p>{profile.time && <p>{profile.time.start} — {profile.time.end} · 采样间隔中位数 {num(profile.time.median_interval_seconds)} 秒</p>}{profile.labels && <p>标签 {profile.labels.rows} 条 · 重复 {profile.labels.duplicates} 条；标签字段在下方可选。</p>}{profile.time?.preview && <><p>时序抽样预览 · {profile.time.preview.series} · {profile.time.preview.group}（最多 120 点）</p><div className={styles.chart}><ResponsiveContainer width="100%" height={200}><LineChart data={profile.time.preview.points}><CartesianGrid stroke="#E4EAF2" /><XAxis dataKey="time" tick={false} /><YAxis domain={["auto", "auto"]} /><Tooltip /><Line isAnimationActive={false} dataKey="value" name={profile.time.preview.series} stroke="#3F639F" dot={false} /></LineChart></ResponsiveContainer></div></>}<select aria-label="查看字段分布" value={field?.name || ''} onChange={e => setColumn(e.target.value)}>{visibleColumns.map(c => <option key={c.name}>{c.name}</option>)}</select>{field?.distribution && <div className={styles.chart}><ResponsiveContainer width="100%" height={240}><BarChart data={field.distribution}><CartesianGrid vertical={false} stroke="#E4EAF2" /><XAxis dataKey="label" tick={{ fontSize: 11 }} /><YAxis width={45} /><Tooltip /><Bar dataKey="count" name="样本数" fill="#567BAD" radius={[3, 3, 0, 0]} /></BarChart></ResponsiveContainer></div>}<div className={styles.table}><table><thead><tr><th>字段</th><th>类型</th><th>缺失</th><th>不同值</th><th>异常提示</th></tr></thead><tbody>{profile.columns.map(c => <tr key={c.name}><td>{c.name}</td><td>{c.dtype}</td><td>{c.missing}</td><td>{c.unique}</td><td>{c.outliers ?? '—'}</td></tr>)}</tbody></table></div></> : <p>数据尚未分析。可以先快速预览，再完成全量统计。</p>}</>}
        {tab === '特征' && <><p>填补、编码和选择在训练折内拟合。时间范围和适用条件以下方实际记录为准；未记录的条件不代表已验证。</p>{bestTrial?.importance?.length ? <><p>模型内部权重／重要性，不能解释为因果关系。</p><div className={styles.chart}><ResponsiveContainer width="100%" height={280}><BarChart data={bestTrial.importance.slice(0, 10)} layout="vertical"><XAxis type="number" /><YAxis type="category" dataKey="name" width={140} tick={{ fontSize: 11 }} /><Tooltip /><Bar dataKey="value" name="重要性" fill="#567BAD" /></BarChart></ResponsiveContainer></div></> : <p>当前模型尚无贡献分析。</p>}<div className={styles.table}><table><thead><tr><th>特征／来源</th><th>计算方式</th><th>窗口与适用条件</th><th>调整</th></tr></thead><tbody>{(bestCandidate?.features_result || candidates[0]?.features_result || []).map(f => <tr key={f.name}><td>{f.name}<small>{f.source}</small></td><td>{f.calculation}</td><td>{f.window_seconds ? f.window_seconds + ' 秒' : '未设置时间窗口'}<small>{f.condition || '未记录可用时点条件，需结合数据来源确认'}</small></td><td><button onClick={() => feedback(bestCandidate, `请分析停用来源字段「${f.source}」的影响；在相同划分下建立新候选比较，保留原结果。`)}>建议停用</button></td></tr>)}</tbody></table></div></>}
        {tab === '实验' && note && study && <TrainingNote base={base + '/modeling/studies/' + study.id} candidateId={note.candidateId} slot={note.slot} onBack={() => setNote(undefined)} />}
        {tab === '实验' && !note && <>{study?.search_strategy === 'aide' && <p>AIDE 选择搜索分支，项目智能体编写方案，平台计算验证分数。每次选择保存在训练笔记中。</p>}<p>固定评价：{({random: '随机划分', group: '按组隔离', time: '时间向前验证'} as Record<string, string>)[study?.evaluation.split || ''] || '尚未确定'} · {metric.toUpperCase()} · {['mae', 'rmse'].includes(metric) ? '越低越好' : '越高越好'}。失败试验保留并计入预算。</p>{curve.length > 0 && <div className={styles.chart}><ResponsiveContainer width="100%" height={220}><LineChart data={curve}><CartesianGrid stroke="#E4EAF2" /><XAxis dataKey="seconds" type="number" domain={[0, "dataMax"]} tickFormatter={value => `${Number(value).toFixed(1)}s`} name="累计训练秒" /><YAxis domain={['auto', 'auto']} /><Tooltip labelFormatter={v => `${v} 秒累计训练时间`} /><Line isAnimationActive={false} dataKey="score" name={metric.toUpperCase()} stroke="#3F639F" connectNulls={false} /></LineChart></ResponsiveContainer></div>}{candidates.length === 0 && <p>还没有实验。统筹提交方案并运行后，每次训练结果会保存在这里。</p>}{candidates.map(c => <section className={styles.experiment} key={c.id}><h4>{c.hypothesis}</h4><small>{statusNames[c.status] || c.status}{c.search_decision ? ` · AIDE：${c.search_decision.label}` : c.parent_id ? ' · 基于父候选改进' : ''}</small><div className={styles.table}><table><thead><tr><th>模型</th><th>{metric.toUpperCase()}</th><th>耗时</th><th>状态</th><th>训练笔记</th></tr></thead><tbody>{c.trials.map(t => <tr key={t.slot}><td>{modelNames[t.model] || t.model}</td><td>{num(t.metrics?.[metric])}</td><td>{t.seconds.toFixed(1)} 秒</td><td>{t.error || statusNames[t.status] || t.status}{t.warnings?.map(w => <small key={w}>{w}</small>)}</td><td><button aria-label={`查看${modelNames[t.model] || t.model}第 ${t.slot + 1} 次训练笔记`} onClick={() => setNote({ candidateId: c.id, slot: t.slot })}>查看训练笔记</button></td></tr>)}</tbody></table></div>{c.error && <p>{c.error}</p>}<div className={styles.actions}><button onClick={() => feedback(c)}>反馈这次实验</button>{c.task_id && <button onClick={() => onTask?.(c.task_id)}>查看关联运行</button>}{c.feedback_task_id && <button onClick={() => onTask?.(c.feedback_task_id!)}>查看原反馈</button>}</div><details><summary>参数与完整记录</summary><pre>{JSON.stringify(c, null, 2)}</pre></details></section>)}{more && <button onClick={() => void loadCandidates(candidates.length)}>加载更早实验</button>}</>}
      </div>
    </ReadingDialog>}
  </section>
}
