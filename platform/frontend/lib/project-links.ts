import { withFrontendToken } from './platform'

export function resolveProjectLink(projectId: string, href: string): string {
  const relative = href.trim().replace(/^\.\//, '')
  if (relative.startsWith(`/api/v1/projects/${encodeURIComponent(projectId)}/`))
    return withFrontendToken(`/api/platform${relative}`)
  if (!/^(results|solution|requirement-package|requirements)\//.test(relative)) return href
  let path: string
  try { path = decodeURIComponent(relative) } catch { return '' }
  if (/[\\\u0000-\u001f]/.test(path) || path.split('/').some(part => part === '..' || part === '.')) return ''
  return withFrontendToken(`/api/platform/api/v1/applications/${encodeURIComponent(projectId)}/workspace/files/${path.split('/').map(encodeURIComponent).join('/')}`)
}

export function projectFilePathFromLink(projectId: string, href: string): string | null {
  const prefix = `/api/platform/api/v1/applications/${encodeURIComponent(projectId)}/workspace/files/`
  if (!href.startsWith(prefix)) return null
  let path: string
  try { path = decodeURIComponent(href.slice(prefix.length).split('?')[0]) } catch { return null }
  if (!/^(results|solution|requirement-package|requirements)\//.test(path) || /[\\\u0000-\u001f]/.test(path) || path.split('/').some(p => p === '..' || p === '.')) return null
  return path
}

export function projectFileFromLink(projectId: string, href: string): string | null {
  const path = projectFilePathFromLink(projectId, href)
  return path && /\.(md|txt|csv|json|html?)$/i.test(path) ? path : null
}

export function resolveProjectFileLink(projectId: string, sourcePath: string, href: string): string {
  const target = href.trim()
  if (/^(results|solution|requirement-package|requirements)\//.test(target) || /^(\w+:|\/|#)/.test(target))
    return resolveProjectLink(projectId, target)
  if (projectFileFromLink(projectId, resolveProjectLink(projectId, sourcePath)) !== sourcePath) return ''
  let decoded: string
  try { decoded = decodeURIComponent(target) } catch { return '' }
  if (/[\\\u0000-\u001f]/.test(decoded)) return ''
  const parts = sourcePath.split('/').slice(0, -1)
  for (const part of decoded.split('/')) {
    if (!part || part === '.') continue
    if (part === '..') { if (!parts.length) return ''; parts.pop() }
    else parts.push(part)
  }
  const path = parts.join('/')
  return /^(results|solution|requirement-package|requirements)\//.test(path) ? resolveProjectLink(projectId, path) : ''
}
