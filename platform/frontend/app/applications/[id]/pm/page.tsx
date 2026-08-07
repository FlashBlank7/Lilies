'use client'

import Link from 'next/link'
import { use, useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import styles from './pm.module.css'

type Check = { check: string; passed: boolean; actual: string }
type CaseRow = { name: string; run_status: string; passed: boolean; checks: Check[]; executed_node_types: string[] }
type Report = {
  stamp: string
  version?: number
  summary: string
  architecture_pass: boolean
  architecture_missing: string[]
  cases: CaseRow[]
  passed_cases: number
  accepted: boolean
}
type Spec = {
  summary: string
  required_node_types: string[]
  cases: Array<{ name: string; expect: Record<string, unknown> }>
  suggestions?: string[]
}
type Application = { id: string; name: string }

export default function PmPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const [app, setApp] = useState<Application | null>(null)
  const [question, setQuestion] = useState('')
  const [explanation, setExplanation] = useState('')
  const [notes, setNotes] = useState('')
  const [examples, setExamples] = useState('')
  const [spec, setSpec] = useState<Spec | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    void api<Application>(`/api/v1/applications/${id}`).then(setApp).catch(() => {})
    void api<Spec>(`/api/v1/applications/${id}/acceptance/spec`).then(setSpec).catch(() => {})
    void api<Report>(`/api/v1/applications/${id}/acceptance/report`).then(setReport).catch(() => {})
  }, [id])

  async function call(kind: string, fn: () => Promise<void>) {
    setBusy(kind)
    setError('')
    try {
      await fn()
    } catch (err) {
      setError(String(err))
    } finally {
      setBusy('')
    }
  }

  return <main className={styles.shell}>
    <header className={styles.topbar}>
      <Link className={styles.back} href={`/applications/${id}/session`}>← 会话空间</Link>
      <span className={styles.title}>{app?.name || '…'}</span>
      <span className={styles.badge}>监理 · 独立于莉莉丝</span>
    </header>

    <div className={styles.wrap}>
      <section className={styles.card}>
        <h2>陪看</h2>
        <p>监理只看你也能看到的材料（需求、会话、流程图、输出）。看不懂莉莉丝在干什么，问它。</p>
        <button
          disabled={busy !== ''}
          onClick={() => call('review', async () => {
            const result = await api<{ notes: string }>(`/api/v1/applications/${id}/pm/review`, { method: 'POST', body: '{}' })
            setNotes(result.notes)
          })}
          type="button"
        >{busy === 'review' ? '巡查中…' : '让监理看一眼当前进度'}</button>
        {notes && <div className={styles.answer}>{notes}</div>}
        <div className={styles.row} style={{ marginTop: 12 }}>
          <input
            onChange={event => setQuestion(event.target.value)}
            placeholder="有具体疑问就写在这里（可留空）"
            value={question}
          />
          <button
            disabled={busy !== ''}
            onClick={() => call('explain', async () => {
              const result = await api<{ explanation: string }>(`/api/v1/applications/${id}/pm/explain`, {
                method: 'POST',
                body: JSON.stringify({ question }),
              })
              setExplanation(result.explanation)
            })}
            type="button"
          >{busy === 'explain' ? '解释中…' : '请监理解释'}</button>
        </div>
        {explanation && <div className={styles.answer}>{explanation}</div>}
      </section>

      <section className={styles.card}>
        <h2>出卷</h2>
        <p>用大白话举例：什么输入该出什么结果、什么绝对不能出现、有没有"必须真的做到"的环节。监理翻译成验收方案。</p>
        <div className={styles.row}>
          <textarea
            onChange={event => setExamples(event.target.value)}
            placeholder={'例如：\n输入这两笔流水和这两张发票（贴数据），第一笔该对上、第二笔对不上。\n金额必须一字不差；报告里不许出现"人民币"。\n必须真的调用我们训练的那个模型。'}
            value={examples}
          />
          <button
            disabled={busy !== '' || examples.trim().length < 5}
            onClick={() => call('spec', async () => {
              const result = await api<Spec>(`/api/v1/applications/${id}/acceptance/spec`, {
                method: 'POST',
                body: JSON.stringify({ examples }),
              })
              setSpec(result)
            })}
            type="button"
          >{busy === 'spec' ? '出卷中…' : '生成验收方案'}</button>
        </div>
        {spec && <div className={styles.specBox}>
          <b>验收口径：</b>{spec.summary}
          <ul>
            {spec.required_node_types.length > 0 && <li>过程要求：必须包含 {spec.required_node_types.join('、')}</li>}
            {spec.cases.map(item => <li key={item.name}>用例：{item.name}</li>)}
          </ul>
          {(spec.suggestions?.length ?? 0) > 0 && <div style={{ marginTop: 10 }}>
            <b>监理的建议（未采纳不生效）：</b>
            <ul>{spec.suggestions!.map(item => <li key={item}>{item}</li>)}</ul>
          </div>}
        </div>}
      </section>

      <section className={styles.card}>
        <h2>监考</h2>
        <p>对"发布版"逐用例真实运行，机械对答案，并核对运行流水账（每个环节是否真实执行——账是引擎记的，谁也改不了）。</p>
        <button
          disabled={busy !== '' || !spec}
          onClick={() => call('run', async () => {
            const result = await api<Report>(`/api/v1/applications/${id}/acceptance/run`, { method: 'POST', body: '{}' })
            setReport(result)
          })}
          type="button"
        >{busy === 'run' ? '验收中（会真实试运行）…' : spec ? '开始验收' : '先生成验收方案'}</button>
        {report && <div>
          <div className={report.accepted ? `${styles.verdict} ${styles.verdictOk}` : `${styles.verdict} ${styles.verdictBad}`}>
            {report.accepted ? '✅ 验收通过' : '❌ 需要整改'}（{report.passed_cases}/{report.cases.length} 用例通过 · 发布版 v{report.version} · {report.stamp}）
          </div>
          {!report.architecture_pass && <p className={styles.err}>结构核验不通过：缺 {report.architecture_missing.join('、')}</p>}
          {report.cases.map(row => <div className={styles.caseBlock} key={row.name}>
            <h3>{row.passed ? '✅' : '❌'} {row.name}（运行：{row.run_status}）</h3>
            <div className={styles.tableBox}>
              <table>
                <thead><tr><th>检查项</th><th>结果</th><th>实际</th></tr></thead>
                <tbody>
                  {row.checks.map((check, index) => <tr key={index}>
                    <td>{check.check}</td>
                    <td>{check.passed ? '通过' : '不通过'}</td>
                    <td>{check.actual}</td>
                  </tr>)}
                </tbody>
              </table>
            </div>
          </div>)}
        </div>}
      </section>

      {error && <p className={styles.err}>{error}</p>}
      <p className={styles.muted}>简单任务不需要监理——莉莉丝自带测试加你自己试跑就够了。这一页只在你需要独立验收或看不懂过程时使用；出卷与解释各消耗一次模型调用，监考只花用例试运行的钱。</p>
    </div>
  </main>
}
