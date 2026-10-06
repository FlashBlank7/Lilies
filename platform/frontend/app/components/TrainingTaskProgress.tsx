'use client'

import { useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import TrainingComparison, { type TrainingTrial } from './TrainingComparison'

type SavedCandidate = {
  id: string; study_id: string; task_id?: string; error?: string; trials: TrainingTrial[]
}

export default function TrainingTaskProgress({ projectId, taskId, studyId, candidateId }: {
  projectId: string; taskId: string; studyId: string; candidateId: string
}) {
  const [candidate, setCandidate] = useState<SavedCandidate>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let active = true
    setCandidate(undefined); setError('')
    if (!studyId || !candidateId) return () => { active = false }
    const path = `/api/v1/projects/${encodeURIComponent(projectId)}/modeling/studies/${encodeURIComponent(studyId)}/candidates/${encodeURIComponent(candidateId)}`
    void api<SavedCandidate>(path).then(value => {
      if (!active) return
      if (value.id !== candidateId || value.study_id !== studyId || (value.task_id && value.task_id !== taskId) || !Array.isArray(value.trials)) {
        throw new Error('训练记录与所选运行不一致，请重新读取')
      }
      setCandidate(value)
    }).catch(cause => { if (active) setError(String(cause)) })
    return () => { active = false }
  }, [projectId, taskId, studyId, candidateId, retry])

  return <section aria-label="已保存的训练进度">
    <h3>已保存的训练进度</h3>
    {!studyId || !candidateId ? <p>这次运行未保存训练记录的关联，无法读取试验详情。</p>
      : error ? <p role="alert">训练记录读取失败：{error} <button onClick={() => setRetry(value => value + 1)}>重试读取训练记录</button></p>
        : !candidate ? <p role="status">正在读取这次运行已保存的训练记录…</p>
          : <>
            <p>{candidate.error ? `本批训练记录：${candidate.error}` : '这批训练没有保存整体停止原因；下方是已保存的各次试验记录。'}</p>
            {candidate.trials.length ? <TrainingComparison trials={candidate.trials} /> : <p>还没有已保存的试验结果。</p>}
          </>}
  </section>
}
