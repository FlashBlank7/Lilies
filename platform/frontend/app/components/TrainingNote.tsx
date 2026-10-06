'use client'

import { useEffect, useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, withFrontendToken } from '@/lib/platform'
import { MarkdownDocument } from '@/lib/markdown'
import styles from './modeling.module.css'

type NoteResponse = {
  markdown: string
  note: {
    title: string; trial: { status: string; error?: string; metrics?: Record<string, number | null>; baseline?: Record<string, number | null>; seconds?: number }; gaps: string[]; evaluation: { metric: string }
    comparison: { sequence: number; score: number | null; best_score: number | null; cumulative_seconds: number }[]
  }
}

export default function TrainingNote({ base, candidateId, slot, onBack, onImprove }: { base: string; candidateId: string; slot: number; onBack: () => void; onImprove?: (message: string) => void }) {
  const [result, setResult] = useState<NoteResponse>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [axis, setAxis] = useState<'sequence' | 'cumulative_seconds'>('sequence')
  const url = `${base}/candidates/${candidateId}/trials/${slot}/note`
  const value = (n: number | null | undefined) => n == null ? '未记录' : n.toLocaleString('zh-CN', { maximumSignificantDigits: 5 })
  useEffect(() => {
    let active = true
    setResult(undefined); setError('')
    void api<NoteResponse>(url).then(value => { if (active) setResult(value) }).catch(cause => { if (active) setError(String(cause)) })
    return () => { active = false }
  }, [url, retry])
  return <section className={styles.note} aria-label="逐模型训练笔记">
    <div className={styles.actions}><button onClick={onBack}>返回实验列表</button>
      {result && <><a href={withFrontendToken('/api/platform' + url + '/download')}>下载笔记与原始记录</a>
        {result.note.trial.status === 'completed' && <a href={withFrontendToken(`/api/platform${base}/candidates/${candidateId}/download?slot=${slot}`)}>下载本次模型</a>}</>}
    </div>
    {error ? <p role="alert">训练笔记加载失败：{error} <button onClick={() => setRetry(value => value + 1)}>重试加载笔记</button></p> : !result ? <p role="status">正在读取已保存的训练记录…</p> : <>
      <h3>{result.note.title}</h3>
      <p>{result.note.trial.status === 'completed' ? `验证 ${result.note.evaluation.metric.toUpperCase()}：${value(result.note.trial.metrics?.[result.note.evaluation.metric])} · 参照：${value(result.note.trial.baseline?.[result.note.evaluation.metric])}` : '训练失败，未产出可用模型'} · 耗时 {value(result.note.trial.seconds)} 秒</p>
      {result.note.trial.status === 'failed' && <section aria-label="失败原因" className={styles.failure}>
        <strong>失败原因</strong>
        <p>{result.note.trial.error || '本次训练未保存具体失败原因，可查看关联运行或请智能体帮助排查。'}</p>
      </section>}
      {onImprove && <div className={styles.actions}><button onClick={() => onImprove(
        `请根据“${result.note.title}”的已保存记录，${result.note.trial.status === 'failed' ? '分析失败原因并提出调整建议。\n已保存的失败原因：' + (result.note.trial.error || '未记录，请根据关联运行排查，不推测缺失信息。') : '解释结果并提出改进建议。'}`
      )}>让智能体改进这次实验</button><span>先准备对话，由你确认后发送。</span></div>}
      <p>每次试验保留独立记录。这里的比较只包含同一研究，失败和未被选中的模型也保留。</p>
      {result.note.gaps.length > 0 && <details><summary>有 {result.note.gaps.length} 项信息未记录</summary><ul>{result.note.gaps.map(gap => <li key={gap}>{gap}</li>)}</ul></details>}
      <label>改进曲线横轴 <select aria-label="训练笔记曲线横轴" value={axis} onChange={e => setAxis(e.target.value as typeof axis)}><option value="sequence">试验次数</option><option value="cumulative_seconds">累计试验耗时</option></select></label>
      <div className={styles.chart}><ResponsiveContainer width="100%" height={240}><LineChart data={result.note.comparison}>
        <CartesianGrid stroke="#E4EAF2" /><XAxis dataKey={axis} type="number" allowDecimals={axis !== 'sequence'} domain={['dataMin', 'dataMax']} /><YAxis domain={['auto', 'auto']} />
        <Tooltip labelFormatter={value => axis === 'sequence' ? `试验 ${value}` : `${Number(value).toFixed(2)} 秒（不含 Agent 等待）`} />
        <Line isAnimationActive={false} dataKey="score" name={`验证 ${result.note.evaluation.metric.toUpperCase()}`} stroke="#3F639F" connectNulls={false} />
        <Line isAnimationActive={false} dataKey="best_score" name="历次最佳" stroke="#90A6C5" strokeDasharray="4 4" dot={false} />
      </LineChart></ResponsiveContainer></div>
      <MarkdownDocument source={result.markdown.replace(/^# [^\n]+\n/, '')} emptyLabel="尚无训练笔记" />
    </>}
  </section>
}
