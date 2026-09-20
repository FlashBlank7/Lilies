'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import { parseField, valueReference } from '@/lib/workflow-fields'
import { WorkflowValueField, type WorkflowFieldProps } from './WorkflowValueField'
import styles from './workflow-fields.module.css'

export default function WorkflowImageFields(props: WorkflowFieldProps) {
  const parsed = parseField(props.value), reference = valueReference(parsed)
  const items = Array.isArray(parsed) ? parsed as { file_path?: string; [key: string]: unknown }[] : []
  const [files, setFiles] = useState<{ path: string }[]>([])
  const [error, setError] = useState(false), [loading, setLoading] = useState(false), [retry, setRetry] = useState(0)
  useEffect(() => {
    setFiles([]); setError(false); setLoading(false)
    if (!props.projectId) return
    let active = true; setLoading(true)
    void api<{ path: string }[]>(`/api/v1/applications/${props.projectId}/workspace/files`).then(value => { if (active) setFiles(value.filter(file => /\.(png|jpe?g)$/i.test(file.path))) }).catch(() => { if (active) setError(true) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [props.projectId, retry])
  return <span className={styles.entries}>
    <select aria-label={`${props.label}的来源`} disabled={props.disabled} value={reference ? 'reference' : 'files'} onChange={event => props.onChange(event.target.value === 'files' ? '[]' : JSON.stringify({ $ref: { node_id: '$inputs', path: [] } }))}>
      <option value="files">选择项目图片</option><option value="reference">引用上游图片列表</option>
    </select>
    {reference ? <WorkflowValueField {...props} allowReference={false} /> : <>
      {items.map((item, index) => <span className={styles.entry} key={index}>
        <select disabled={props.disabled || loading} aria-label={`${props.label} ${index + 1}`} value={item.file_path || ''} onChange={event => props.onChange(JSON.stringify(items.map((value, i) => i === index ? { ...value, file_path: event.target.value } : value)))}>
          <option value="">{loading ? '正在读取图片…' : '选择图片 / 稍后配置'}</option>
          {item.file_path && !files.some(file => file.path === item.file_path) && <option value={item.file_path}>{item.file_path} · 当前配置</option>}
          {files.map(file => { const parts = file.path.split('/'); const folder = parts.at(-2) || ''; return <option value={file.path} key={file.path} title={file.path}>{parts.at(-1)}{folder ? ` · ${folder.length > 24 ? folder.slice(0, 8) : folder}` : ''}</option> })}
        </select>
        <button disabled={props.disabled} type="button" onClick={() => props.onChange(JSON.stringify(items.filter((_, i) => i !== index)))}>移除图片</button>
      </span>)}
      <button disabled={props.disabled} type="button" onClick={() => props.onChange(JSON.stringify([...items, { file_path: '' }]))}>添加图片</button>
      {!loading && !error && !files.length && <small className={styles.help}>项目中还没有 PNG 或 JPEG 图片，可先保存流程，再到“资料与知识”上传。</small>}
    </>}
    {error && <span role="alert">图片列表读取失败。<button type="button" disabled={props.disabled} onClick={() => setRetry(value => value + 1)}>重试</button></span>}
  </span>
}
