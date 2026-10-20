'use client'

import { useEffect, useState } from 'react'
import { api, withFrontendToken } from '@/lib/platform'
import styles from './RequirementPackage.module.css'
import { groupProjectFiles } from './project-files'

type FileEntry = { path: string; size: number }

export default function RequirementPackageMaterials({ applicationId, requirement, onOpenFile, expanded = false }: {
  applicationId: string; requirement: string; onOpenFile?: (path: string) => void; expanded?: boolean
}) {
  const [files, setFiles] = useState<FileEntry[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    setFiles([])
    setError('')
    void api<FileEntry[]>(`/api/v1/applications/${applicationId}/workspace/files`)
      .then(result => { if (active) setFiles(result.filter(file => file.path.startsWith('requirement-package/'))) })
      .catch(cause => { if (active) setError(String(cause)) })
    return () => { active = false }
  }, [applicationId])

  if (!files.length) return error && requirement.includes('requirement-package/')
    ? <p className={styles.error} role="alert">需求包资料暂时无法加载：{error}</p> : null
  const contents = <>
    <p>这是企业提供的输入资料，需求文档将在沟通后由平台整理。</p>
      <ul>{groupProjectFiles(files).flatMap(group => group.files).map(file => <li key={file.path}>
        <a href={withFrontendToken(`/api/platform/api/v1/applications/${applicationId}/workspace/files/${encodeURIComponent(file.path)}?download=1`)}
          title={file.path} download={onOpenFile && /\.(md|txt|csv|json|html?)$/i.test(file.path) ? undefined : file.name} onClick={event => { if (onOpenFile && /\.(md|txt|csv|json|html?)$/i.test(file.path)) { event.preventDefault(); onOpenFile(file.path) } }}>{file.label}</a>
        <small>{file.size! < 1024 * 1024 ? `${Math.ceil(file.size! / 1024)} KB` : `${(file.size! / 1024 / 1024).toFixed(1)} MB`}</small>
      </li>)}</ul>
  </>
  return expanded ? <section className={styles.materials} aria-label="企业资料">
    <h3>企业资料 · {files.length} 个文件</h3>{contents}
  </section> : <details className={styles.materials}>
    <summary>企业资料 · {files.length} 个文件</summary>{contents}
  </details>
}
