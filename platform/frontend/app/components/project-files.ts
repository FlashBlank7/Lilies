export type ProjectFile = {
  path: string; size?: number; modified_at?: string
  related_run?: {
    run_id: string; task_id: string; workflow_id: string; workflow_name: string; created_at: string
    file_parameters: {name: string; label: string; value: string}[]
    input_parameters: {name: string; label: string; value: string}[]
  }
}

const groupNames = ['原始资料', '说明', '运行结果', '其他'] as const

function fileGroup(file: ProjectFile): number {
  if (file.path.startsWith('results/')) return 2
  const name = file.path.split('/').pop() || file.path
  if (/^(?:readme|instructions|guide|使用说明|项目说明|流程说明|操作说明)(?:[. _-]|$)/i.test(name)
    || file.path.startsWith('requirements/') || /^solution\/.*\.(?:md|txt)$/i.test(file.path)) return 1
  return file.path.startsWith('requirement-package/') ? 0 : 3
}

function shortDirectory(path: string): string {
  const parts = path.split('/').slice(0, -1)
  if (['requirement-package', 'results', 'solution'].includes(parts[0])) parts.shift()
  return parts.length ? '目录 ' + (parts.length > 2 ? '… / ' : '') + parts.slice(-2).map(part =>
    /^[a-f0-9]{8}-[a-f0-9-]{27}$/i.test(part) ? part.slice(0, 8)
      : part.length > 24 ? part.slice(0, 12) + '…' + part.slice(-6) : part).join(' / ') : '根目录'
}

function modifiedTime(value?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return '更新 ' + date.toLocaleString('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'})
}

function relatedRun(file: ProjectFile): string {
  const run = file.related_run
  if (!run) return ''
  const date = new Date(run.created_at)
  const time = Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
  return [`关联运行：${run.workflow_name}`, time,
    ...[...run.file_parameters.slice(0, 2), ...run.input_parameters.slice(0, 3)]
      .map(parameter => `${parameter.label}：${parameter.value}`)].filter(Boolean).join(' · ')
}

export function groupProjectFiles(files: ProjectFile[]) {
  const names = new Map<string, number>()
  for (const file of files) {
    const name = file.path.split('/').pop() || file.path
    names.set(name, (names.get(name) || 0) + 1)
  }
  const displayed = files.map(file => {
    const name = file.path.split('/').pop() || file.path
    const group = fileGroup(file)
    const detail = relatedRun(file) || (group === 2 || names.get(name)! > 1
      ? [shortDirectory(file.path), modifiedTime(file.modified_at)].filter(Boolean).join(' · ') : '')
    return {...file, name, group, detail, label: name + (detail ? ' · ' + detail : '')}
  })
  // Two shortened directory names can coincide; keep every choice distinguishable.
  const labels = new Map<string, typeof displayed>()
  for (const file of displayed) labels.set(file.label, [...(labels.get(file.label) || []), file])
  for (const matches of labels.values()) if (matches.length > 1) {
    matches.sort((a, b) => a.path.localeCompare(b.path)).forEach((file, index) => {
      file.detail += ` · 文件 ${index + 1}`
      file.label += ` · 文件 ${index + 1}`
    })
  }
  return groupNames.map((label, index) => ({label, files: displayed.filter(file => file.group === index).sort((a, b) => {
    if (index === 2 && a.modified_at && b.modified_at) {
      const newest = Date.parse(b.modified_at) - Date.parse(a.modified_at)
      if (Number.isFinite(newest) && newest) return newest
    }
    return a.name.localeCompare(b.name, 'zh-CN') || a.path.localeCompare(b.path)
  })})).filter(group => group.files.length)
}
