type StudySource = { dataset_id?: string; created_at?: string }
type DatasetSource = { id: string; files?: { source?: { original?: string } } }

export function studySourceLabel(study: StudySource, datasets: DatasetSource[]) {
  const source = datasets.find(dataset => dataset.id === study.dataset_id)?.files?.source?.original
  const created = study.created_at ? new Date(study.created_at) : null
  const time = created && !Number.isNaN(created.getTime()) ? created.toLocaleString('zh-CN', { hour12: false }) : ''
  return [source ? `来源：${source}` : '', time ? `创建于 ${time}` : ''].filter(Boolean).join(' · ')
}
