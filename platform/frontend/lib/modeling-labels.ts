type StudySource = { dataset_id?: string; created_at?: string }
type DatasetSource = { id: string; files?: { source?: { original?: string } } }

export function studySourceLabel(study: StudySource, datasets: DatasetSource[]) {
  const source = datasets.find(dataset => dataset.id === study.dataset_id)?.files?.source?.original
  const created = study.created_at ? new Date(study.created_at) : null
  const time = created && !Number.isNaN(created.getTime()) ? created.toLocaleString('zh-CN', { hour12: false }) : ''
  return [source ? `来源：${source}` : '', time ? `创建于 ${time}` : ''].filter(Boolean).join(' · ')
}

type TrainingProgress = {
  status: string
  scheduled_slots?: number
  batch_size?: number
  current?: { slot: number; started_at?: string }
}

export function trainingProgressLabel(candidate: TrainingProgress | undefined, modelName: string, now = Date.now()) {
  if (candidate?.status !== 'running' || !candidate.current) return ''
  const parts = [`正在训练：${modelName}`]
  const slot = candidate.current.slot
  const total = candidate.scheduled_slots ?? candidate.batch_size
  if (Number.isInteger(slot) && slot >= 0) {
    parts.push(typeof total === 'number' && Number.isInteger(total) && total > slot
      ? `本批第 ${slot + 1} / ${total} 次试验` : `本批第 ${slot + 1} 次试验`)
  }
  const started = candidate.current.started_at ? Date.parse(candidate.current.started_at) : NaN
  if (Number.isFinite(started) && now >= started) {
    const seconds = Math.floor((now - started) / 1000)
    const elapsed = seconds < 60 ? `${seconds} 秒`
      : seconds < 3600 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
        : `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`
    parts.push(`本次已运行 ${elapsed}`)
  }
  return parts.join(' · ')
}
