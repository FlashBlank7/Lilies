import type { ProjectTask } from './project-progress'

type Artifact = { file_path: string; label: string; section: string }

/** Use the saved result's heading/field, never the current draft, to name downloads. */
export function taskArtifacts(task: ProjectTask): Artifact[] {
  const output = task.outputs || {}
  const sections = [['', output], ...Object.entries(output)] as [string, unknown][]
  const collected: Artifact[] = []
  for (const [field, value] of sections) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue
    const result = value as Record<string, unknown>
    const heading = typeof result.markdown === 'string' ? result.markdown.match(/^#{1,6}\s+(.+)$/m)?.[1].trim().slice(0, 80) : ''
    const entries = Array.isArray(result.artifacts) ? result.artifacts : typeof result.file === 'string' ? [{file_path: result.file}] : []
    for (const entry of entries) {
      if (!entry || typeof entry !== 'object' || typeof entry.file_path !== 'string') continue
      collected.push({file_path: entry.file_path, label: typeof entry.label === 'string' && entry.label ? entry.label : entry.file_path.split('/').pop() || '', section: heading || field})
    }
  }
  const entries = task.presentation?.artifacts?.length ? task.presentation.artifacts.map(entry => ({
    file_path: entry.file_path, label: entry.label || entry.file_path.split('/').pop() || '',
    section: collected.find(item => item.file_path === entry.file_path)?.section || '',
  })) : collected
  const valid = entries.filter(entry => /^(results|solution)\//.test(entry.file_path) && !entry.file_path.split('/').includes('..'))
  return valid.map(entry => {
    if (valid.filter(other => other.label === entry.label).length < 2) return entry
    const section = entry.section || entry.file_path.split('/').slice(0, -1).join('/')
    const sameSection = valid.filter(other => other.label === entry.label && other.section === entry.section).length > 1
    return {...entry, label: `${section}${entry.section && sameSection ? ` · ${entry.file_path}` : ''} · ${entry.label}`}
  })
}
