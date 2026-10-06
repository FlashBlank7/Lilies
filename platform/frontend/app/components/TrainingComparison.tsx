'use client'

import { useState } from 'react'
import styles from './training-comparison.module.css'

export type TrainingTrial = {
  slot?: number; model?: string; status?: string; error?: string
  metrics?: unknown; baseline?: unknown
}
type MetricInfo = { label: string; explanation: string; unit: 'percent' | 'original' | 'score' }
const metricInfo: Record<string, MetricInfo> = {
  accuracy: { label: '准确率（accuracy）', explanation: '预测正确的样本占比，越高越好。', unit: 'percent' },
  balanced_accuracy: { label: '平衡准确率（balanced_accuracy）', explanation: '各类别召回率的平均值，越高越好。', unit: 'percent' },
  precision: { label: '精确率（precision）', explanation: '判为目标类别的样本中，判断正确的比例，越高越好。', unit: 'percent' },
  recall: { label: '召回率（recall）', explanation: '实际属于目标类别的样本中，被找回的比例，越高越好。', unit: 'percent' },
  f1: { label: 'F1 分数（f1）', explanation: '综合精确率与召回率的分数，越高越好。', unit: 'score' },
  macro_f1: { label: '宏平均 F1（macro_f1）', explanation: '平等考虑各类别的 F1 分数，越高越好。', unit: 'score' },
  roc_auc: { label: 'ROC AUC（roc_auc）', explanation: '衡量类别区分能力，越高越好；它不是某个阈值下的准确率。', unit: 'score' },
  auprc: { label: 'PR AUC（auprc）', explanation: '精确率与召回率曲线下的面积，越高越好。', unit: 'score' },
  mae: { label: '平均绝对误差（MAE）', explanation: '预测值与实际值的平均绝对距离，越低越好，沿用预测目标的单位。', unit: 'original' },
  rmse: { label: '均方根误差（RMSE）', explanation: '对较大误差更敏感，越低越好，沿用预测目标的单位。', unit: 'original' },
  r2: { label: '决定系数（R²）', explanation: '衡量对目标变化的解释程度，越高越好，最大为 1，也可能为负。', unit: 'score' },
}
const statuses: Record<string, string> = { completed: '已完成', failed: '失败', running: '训练中', queued: '等待训练', interrupted: '已中断' }
const numberFormat = new Intl.NumberFormat('zh-CN', { maximumSignificantDigits: 5 })
function metrics(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function finite(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) }
function infoFor(metric: string) { return Object.hasOwn(metricInfo, metric) ? metricInfo[metric] : undefined }
function formatValue(value: unknown, info: MetricInfo | undefined) {
  if (value === undefined) return '未记录'
  if (!finite(value)) return '无法计算'
  const displayed = info?.unit === 'percent' ? value * 100 : value
  if (!Number.isFinite(displayed)) return '无法计算'
  return numberFormat.format(displayed === 0 ? 0 : displayed) + (info?.unit === 'percent' ? '%' : '')
}
function difference(validation: unknown, baseline: unknown, info: MetricInfo | undefined) {
  if (!finite(validation) || !finite(baseline)) return '—'
  const value = (validation - baseline) * (info?.unit === 'percent' ? 100 : 1)
  if (!Number.isFinite(value)) return '—'
  const unit = info?.unit === 'percent' ? ' 个百分点' : info?.unit === 'original' ? '（原单位）' : '（值差）'
  return (value > 0 ? '+' : '') + numberFormat.format(value === 0 ? 0 : value) + unit
}

export default function TrainingComparison({ trials }: { trials: TrainingTrial[] }) {
  const choices = [...new Set(trials.flatMap(trial => [...Object.keys(metrics(trial.metrics)), ...Object.keys(metrics(trial.baseline))]))]
  const [selected, setSelected] = useState('')
  const metric = choices.includes(selected) ? selected : choices[0] || ''
  const info = infoFor(metric)
  return <section aria-label="训练比较" className={styles.comparison}>
    <h3>训练比较</h3>
    <p>基线是用简单规则预测的参考方法；每行比较模型和基线在相同数据划分下的结果。</p>
    {choices.length ? <>
      <label className={styles.selector}>比较指标<select value={metric} onChange={event => setSelected(event.target.value)}>
        {choices.map(key => <option key={key} value={key}>{infoFor(key)?.label || key}</option>)}
      </select></label>
      <p aria-live="polite">{info?.explanation || '按记录显示原值，评价方向未说明。'}</p>
    </> : <p>本次训练未保存可比较的指标。</p>}
    <div role="region" aria-label="训练比较表" tabIndex={0} className={styles.scroll}>
      <table aria-label="训练指标比较">
        <thead><tr><th scope="col">模型</th><th scope="col">验证值</th><th scope="col">简单基线</th><th scope="col">差值（验证 − 基线）</th><th scope="col">状态</th></tr></thead>
        <tbody>{trials.map((trial, index) => {
          const validation = metrics(trial.metrics)[metric], baseline = metrics(trial.baseline)[metric]
          const trialNumber = typeof trial.slot === 'number' && Number.isInteger(trial.slot) && trial.slot >= 0 ? trial.slot + 1 : undefined
          return <tr key={index}>
            <th scope="row"><span className={styles.model}>{trial.model || '未记录模型'}{trialNumber !== undefined && <small>第 {trialNumber} 次试验</small>}</span></th>
            <td data-label="验证值">{formatValue(validation, info)}</td><td data-label="简单基线">{formatValue(baseline, info)}</td><td data-label="差值（验证 − 基线）">{difference(validation, baseline, info)}</td>
            <td data-label="状态">{statuses[trial.status || ''] || trial.status || '未记录'}{trial.error && <small>{trial.error}</small>}</td>
          </tr>
        })}</tbody>
      </table>
    </div>
    <p>差值为验证值减去基线，不是相对提升比例。只有两边都有有效数值时才计算；差值本身不能说明效果是否显著，也不能解释原因。</p>
  </section>
}
