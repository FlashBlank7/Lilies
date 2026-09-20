'use client'

import { useEffect, useMemo, useState } from 'react'
import Papa from 'papaparse'
import { MarkdownDocument } from '@/lib/markdown'
import { projectFileFromLink, resolveProjectFileLink, resolveProjectLink } from '@/lib/project-links'
import ReadingDialog from './ReadingDialog'
import styles from '@/app/projects/projects.module.css'

export default function ProjectFileReader({ projectId, path, onClose, onTask }: { projectId: string; path: string; onClose: () => void; onTask?: () => void }) {
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const url = resolveProjectLink(projectId, path)
  const valid = projectFileFromLink(projectId, url) === path
  useEffect(() => {
    if (!valid) return
    const controller = new AbortController()
    setText(null); setError('')
    void (async () => {
      const response = await fetch(url, { signal: controller.signal })
      if (!response.ok) throw new Error(`文件读取失败（${response.status}）`)
      if (Number(response.headers.get('content-length') || 0) > 2 * 1024 * 1024) { controller.abort(); throw new Error('文件较大，请下载查看完整内容。') }
      const content = await response.text()
      if (!controller.signal.aborted) setText(content)
    })().catch(e => { if (e.name !== 'AbortError') setError(String(e.message || e)) })
    return () => controller.abort()
  }, [url, valid, attempt])
  const csv = useMemo(() => /\.csv$/i.test(path) && text !== null ? Papa.parse<string[]>(text, { preview:201, skipEmptyLines:true }) : null, [path, text])
  const html = /\.html?$/i.test(path)
  return <ReadingDialog wide title={path.split('/').pop() || '项目文件'} onClose={onClose}>
    {!valid ? <p role="alert">只能预览当前项目的资料与结果文件。</p> : <>
      <div className={styles.actions}><a href={url} download>下载原文件</a>{onTask && <button onClick={onTask}>查看关联结果</button>}<small>{path}</small></div>
      {error && <p role="alert" className={styles.error}>{error} <button onClick={() => setAttempt(v => v + 1)}>重新读取</button></p>}
      {text === null && !error && <p role="status">正在读取文件…</p>}
      {text !== null && (csv ? <>
        {csv.errors.length > 0 && <p role="alert">表格格式存在问题：{csv.errors[0].message}。可下载原文件核对。</p>}
        {csv.meta.truncated && <p>预览前 200 行，完整内容请下载原文件。</p>}
        {(csv.data[0]?.length || 0) > 6 && <p>表格较宽，可在表格内横向滚动。</p>}
        <div className={`markdown-table-wrap ${styles.fileTable}`} role="region" aria-label="表格预览" tabIndex={0}><table style={{ minWidth:Math.max(480, (csv.data[0]?.length || 0) * 130) }}><thead><tr>{csv.data[0]?.map((cell, index) => <th key={index}>{cell}</th>)}</tr></thead><tbody>{csv.data.slice(1).map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>)}</tbody></table></div>
      </> : html ? <iframe title="项目报告预览" className={styles.reportFrame} sandbox="" srcDoc={`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:"><style>body{font-family:system-ui;color:#202b3d;padding:16px}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #e1e6ed}</style>${text}`} />
        : /\.md$/i.test(path) ? <MarkdownDocument source={text} emptyLabel="文件为空" resolveLink={href => resolveProjectFileLink(projectId, path, href)} /> : <pre>{text}</pre>)}
    </>}
  </ReadingDialog>
}
