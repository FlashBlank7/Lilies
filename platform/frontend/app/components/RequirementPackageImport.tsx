'use client'

import { useState } from 'react'
import { api } from '@/lib/platform'
import styles from './RequirementPackage.module.css'

export default function RequirementPackageImport({ locale = 'zh', disabled = false, onImported, project = true }: {
  locale?: 'zh' | 'en'
  disabled?: boolean
  project?: boolean
  onImported: (applicationId: string) => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const zh = locale === 'zh'

  async function upload() {
    if (!file || busy || disabled) return
    if (file.size > 256 * 1024 * 1024) {
      setError(zh ? '需求包不能超过 256 MB。' : 'The package must be no larger than 256 MB.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const body = new FormData()
      body.append('file', file)
      const result = await api<{ application: { id: string } }>(project ? '/api/v1/projects/requirement-packages/import' : '/api/v1/requirement-packages/import', {
        method: 'POST', body,
      })
      onImported(result.application.id)
    } catch (cause) {
      setError(String(cause))
    } finally {
      setBusy(false)
    }
  }

  return <section className={styles.importCard} aria-label={zh ? '导入需求包' : 'Import a requirement package'}>
    <div>
      <strong>{zh ? '已有项目资料？导入需求包' : 'Have project materials? Import a requirement package'}</strong>
      <p>{project ? (zh ? '导入后打开项目，资料保存在项目内。由你发出指令，再开始沟通和搭建。' : 'Open a project with your materials. Discussion and building begin when you ask.') : zh ? '导入项目材料与数据，由莉莉丝分析并与你核对理解，沟通后整理需求文档。' : 'Import project materials and data. Lilies will discuss her understanding with you and prepare the requirement document.'}</p>
    </div>
    <div className={styles.controls}>
      <input aria-label={zh ? '选择需求包 ZIP' : 'Choose requirement ZIP'} type="file" accept=".zip,application/zip"
        disabled={busy || disabled} onChange={event => { setFile(event.target.files?.[0] || null); setError('') }} />
      <button type="button" disabled={!file || busy || disabled} onClick={() => void upload()}>
        {busy ? (zh ? '正在导入…' : 'Importing…') : project ? (zh ? '导入并打开项目' : 'Import and open project') : (zh ? '导入并打开工作流' : 'Import and open workflow')}
      </button>
    </div>
    <small>{zh ? 'ZIP · 最大 256 MB · 包含 requirement.json、简单诉求和项目资料' : 'ZIP · Up to 256 MB · Includes requirement.json, your initial request and project materials'}</small>
    {error && <p className={styles.error} role="alert">{error}</p>}
  </section>
}
