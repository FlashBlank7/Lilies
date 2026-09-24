'use client'

import { useEffect, useState } from 'react'
import { api, withFrontendToken } from '@/lib/platform'
import styles from './RequirementPackage.module.css'

export type DiscussionPhase = 'loading' | 'none' | 'pending' | 'confirmed'
type Question = { id: string; question: string; why: string; choice_type: 'single' | 'multi'; options: {
  id: string; label: string; description: string
}[] }
type Discussion = {
  enabled: boolean
  status: 'not_started' | 'discussing' | 'review' | 'confirmed'
  revision: number
  turns: { user: string; analysis: { detected_goal: string; reasoning_summary: string; questions: Question[] } }[]
  document: string
  document_path?: string
}

export default function RequirementDiscussion({ applicationId, onPhaseChange, onConfirmed }: {
  applicationId: string
  onPhaseChange: (phase: DiscussionPhase) => void
  onConfirmed: () => void
}) {
  const [discussion, setDiscussion] = useState<Discussion | null>(null)
  const [message, setMessage] = useState('')
  const [choices, setChoices] = useState<Record<string, string[]>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const base = `/api/v1/applications/${applicationId}/requirements`

  useEffect(() => {
    let active = true
    setDiscussion(null)
    onPhaseChange('loading')
    void api<Discussion>(base).then(result => { if (active) setDiscussion(result) })
      .catch(cause => { if (active) setError(String(cause)) })
    return () => { active = false }
  }, [base, onPhaseChange])

  useEffect(() => {
    if (discussion) onPhaseChange(!discussion.enabled ? 'none' : discussion.status === 'confirmed' ? 'confirmed' : 'pending')
  }, [discussion, onPhaseChange])

  if (!discussion) return <div className={styles.discussion}>
    {error ? <p role="alert">资料状态无法加载：{error}。请刷新重试。</p> : <p>正在读取项目资料状态…</p>}
  </div>
  if (!discussion.enabled) return null
  const questions = discussion.turns.at(-1)?.analysis.questions || []
  const selectedText = questions.flatMap(question => {
    const values = choices[question.id] || []
    return values.length ? [`${question.question}：${question.options.filter(o => values.includes(o.id)).map(o => o.label).join('、')}`] : []
  }).join('\n')

  async function discuss() {
    if (!discussion || busy) return
    setBusy(true)
    setError('')
    onPhaseChange('pending')
    try {
      const result = await api<Discussion>(`${base}/messages`, {
        method: 'POST', body: JSON.stringify({ revision: discussion.revision,
          message: [selectedText, message.trim()].filter(Boolean).join('\n') }),
      })
      setDiscussion(result)
      setMessage('')
      setChoices({})
    } catch (cause) {
      setError(String(cause))
      onPhaseChange(discussion.status === 'confirmed' ? 'confirmed' : 'pending')
    } finally { setBusy(false) }
  }

  async function confirm() {
    if (!discussion || busy) return
    setBusy(true)
    setError('')
    try {
      const result = await api<Discussion>(`${base}/confirm`, {
        method: 'POST', body: JSON.stringify({ revision: discussion.revision }),
      })
      setDiscussion(result)
      onConfirmed()
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }

  return <section className={styles.discussion} aria-label="需求沟通">
    <strong>{discussion.status === 'confirmed' ? '需求文档已确认' : '先理解项目，再形成需求'}</strong>
    {discussion.status === 'not_started' && <p>莉莉丝会阅读企业资料，说明她对项目的理解，再与你核对问题和目标。沟通后的需求文档由她整理。</p>}
    {discussion.turns.map((turn, index) => <article className={styles.discussionTurn} key={index}>
      <p className={styles.ownerMessage}>{turn.user}</p>
      <b>莉莉丝的理解</b>
      <p>{turn.analysis.detected_goal}</p>
      <p>{turn.analysis.reasoning_summary}</p>
    </article>)}
    {discussion.status !== 'confirmed' && questions.map(question => <fieldset key={question.id} disabled={busy}>
      <legend>{question.question}</legend>
      {question.why && <p>{question.why}</p>}
      {question.options.map(option => <label className={styles.answerOption} key={option.id}>
        <input type={question.choice_type === 'multi' ? 'checkbox' : 'radio'} name={question.id}
          checked={(choices[question.id] || []).includes(option.id)} onChange={event => {
            const current = choices[question.id] || []
            setChoices({ ...choices, [question.id]: question.choice_type === 'single' ? [option.id]
              : event.target.checked ? [...current, option.id] : current.filter(id => id !== option.id) })
          }} />
        <span>{option.label}{option.description && <small>{option.description}</small>}</span>
      </label>)}
    </fieldset>)}
    {discussion.document && <details className={styles.requirementDraft} open={discussion.status === 'review'}>
      <summary>{discussion.status === 'confirmed' ? '查看已确认的需求文档' : '核对平台整理的需求文档'}</summary>
      <pre>{discussion.document}</pre>
      {discussion.status === 'review' && <button type="button" disabled={busy} onClick={() => void confirm()}>理解正确，确认需求文档</button>}
    </details>}
    {discussion.document_path && discussion.status === 'confirmed' && <p>
      <a download="需求文档.md" href={withFrontendToken(`/api/platform/api/v1/applications/${applicationId}/workspace/files/${encodeURIComponent(discussion.document_path)}?download=1`)}>下载需求文档</a>
      {' · '}可以开始搭建，也可以继续讨论调整需求。
    </p>}
    <textarea aria-label="补充或修正需求理解" placeholder={discussion.status === 'not_started'
      ? '可以先补充一句项目背景，也可以直接分析资料。'
      : '回复问题，或指出哪里理解有误…'}
      value={message} disabled={busy} onChange={event => setMessage(event.target.value)} />
    {error && <p className={styles.error} role="alert">{error}</p>}
    <button type="button" disabled={busy || (discussion.status !== 'not_started' && !message.trim() && !selectedText)}
      onClick={() => void discuss()}>{busy ? '正在处理…' : discussion.status === 'not_started' ? '分析资料' : '发送并继续沟通'}</button>
  </section>
}
