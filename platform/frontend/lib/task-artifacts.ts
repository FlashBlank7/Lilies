import type { ProjectTask } from './project-progress'

type Artifact = { file_path: string; label: string; section: string }
type ArtifactSource = { artifact: Artifact; fileCount: number; heading: boolean; named: boolean }

const namedLabel = (label: string, path: string) => !!label && label !== path.split('/').pop() && !label.includes(path)

function preferSource(next: ArtifactSource, previous: ArtifactSource): boolean {
  // A child result describes its files more precisely than a report collecting
  // every child's artifacts. Keep the first source when equally informative.
  if (next.fileCount !== previous.fileCount) return next.fileCount < previous.fileCount
  if (next.heading !== previous.heading) return next.heading
  return next.named && !previous.named
}

/** Use the saved result's heading/field, never the current draft, to name downloads. */
export function taskArtifacts(task: ProjectTask): Artifact[] {
  const output = task.outputs || {}
  const sections = [['', output], ...Object.entries(output)] as [string, unknown][]
  const collected = new Map<string, ArtifactSource>()
  for (const [field, value] of sections) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue
    const result = value as Record<string, unknown>
    const heading = typeof result.markdown === 'string' ? result.markdown.match(/^#{1,6}\s+(.+)$/m)?.[1].trim().slice(0, 80) : ''
    const entries = Array.isArray(result.artifacts) ? result.artifacts : typeof result.file === 'string' ? [{file_path: result.file}] : []
    const fileCount = new Set(entries.filter(entry => entry && typeof entry.file_path === 'string').map(entry => entry.file_path)).size
    for (const entry of entries) {
      if (!entry || typeof entry !== 'object' || typeof entry.file_path !== 'string') continue
      const label = typeof entry.label === 'string' && entry.label ? entry.label : entry.file_path.split('/').pop() || ''
      const candidate = {artifact: {file_path: entry.file_path, label, section: heading || field},
        fileCount, heading: !!heading, named: namedLabel(label, entry.file_path)}
      const previous = collected.get(entry.file_path)
      if (!previous || preferSource(candidate, previous)) collected.set(entry.file_path, candidate)
    }
  }
  const presented = new Map<string, {file_path: string; label: string}>()
  for (const entry of task.presentation?.artifacts || []) {
    const previous = presented.get(entry.file_path)
    if (!previous || (namedLabel(entry.label, entry.file_path) && !namedLabel(previous.label, previous.file_path))) presented.set(entry.file_path, entry)
  }
  const entries = presented.size ? [...presented.values()].map(entry => {
    const source = collected.get(entry.file_path)?.artifact
    return {file_path: entry.file_path,
      label: namedLabel(entry.label, entry.file_path) ? entry.label : source?.label || entry.label || entry.file_path.split('/').pop() || '',
      section: source?.section || ''}
  }) : [...collected.values()].map(source => source.artifact)
  const valid = entries.filter(entry => /^(results|solution)\//.test(entry.file_path) && !entry.file_path.split('/').includes('..'))
  return valid.map(entry => {
    if (valid.filter(other => other.label === entry.label).length < 2) return entry
    const section = entry.section || entry.file_path.split('/').slice(0, -1).join('/')
    const sameSection = valid.filter(other => other.label === entry.label && other.section === entry.section).length > 1
    return {...entry, label: `${section}${entry.section && sameSection ? ` · ${entry.file_path}` : ''} · ${entry.label}`}
  })
}
