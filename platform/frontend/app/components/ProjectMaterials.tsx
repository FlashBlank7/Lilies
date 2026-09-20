'use client'

import { useRef, useState } from 'react'
import { api } from '@/lib/platform'
import RequirementPackageMaterials from './RequirementPackageMaterials'

export default function ProjectMaterials({ id, onOpenFile }: { id: string; onOpenFile: (path: string) => void }) {
  const input = useRef<HTMLInputElement>(null)
  const [revision, setRevision] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [added, setAdded] = useState<string[]>([])
  const [choosing, setChoosing] = useState(false)
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([])
  const [sourceProject, setSourceProject] = useState('')
  const [sourceFiles, setSourceFiles] = useState<{ path: string; size: number }[]>([])
  const [selected, setSelected] = useState<string[]>([])
  async function chooseProject(projectId: string) {
    setSourceProject(projectId); setSourceFiles([]); setSelected([]); setError('')
    if (!projectId) return
    setBusy(true)
    try {
      const files = await api<{ path: string; size: number }[]>(`/api/v1/applications/${projectId}/workspace/files`)
      setSourceFiles(files.filter(file => file.path.startsWith('requirement-package/')))
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  async function copySelected() {
    setBusy(true); setError(''); setAdded([])
    try {
      for (const path of selected) {
        const result = await api<{ name: string }>(`/api/v1/projects/${id}/materials/copy`, {
          method: 'POST', body: JSON.stringify({ source_project_id: sourceProject, source_path: path }),
        })
        setAdded(previous => [...previous, result.name]); setRevision(previous => previous + 1)
        setSelected(previous => previous.filter(item => item !== path))
      }
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  async function upload(files: File[]) {
    setBusy(true); setError(''); setAdded([])
    try {
      for (const file of files) {
        if (file.size > 256 * 1024 * 1024) throw new Error(`${file.name} 超过 256 MB`)
        const form = new FormData(); form.append('file', file)
        const result = await api<{ name: string }>(`/api/v1/projects/${id}/materials`, { method: 'POST', body: form })
        setAdded(previous => [...previous, result.name]); setRevision(previous => previous + 1)
      }
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  return <>
    <button disabled={busy} onClick={() => input.current?.click()}>{busy ? '正在上传…' : '添加资料'}</button>
    <input ref={input} type="file" hidden multiple aria-label="添加项目资料" disabled={busy} onChange={event => {
      const files = Array.from(event.target.files || []); event.target.value = ''; if (files.length) void upload(files)
    }} />
    <p>可添加设计文档、源码包或其他输入，每个文件最多 256 MB。上传后在“运行工作流”中选择本次资料，也可以通过对话使用。</p>
    <button disabled={busy} onClick={async () => {
      setBusy(true); setError('')
      try {
        setProjects((await api<{ id: string; name: string }[]>('/api/v1/projects')).filter(project => project.id !== id))
        setChoosing(true)
      } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
    }}>从已有项目选择资料</button>
    {choosing && <section aria-label="选择已有项目资料">
      <p>将所选原始资料保存为本项目的独立文件，每个文件最多 256 MB。</p>
      <label>来源项目<select aria-label="资料来源项目" value={sourceProject} disabled={busy} onChange={event => void chooseProject(event.target.value)}>
        <option value="">请选择项目</option>{projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}
      </select></label>
      {sourceFiles.length > 0 ? <ul>{sourceFiles.map(file => <li key={file.path}><label>
        <input type="checkbox" disabled={busy} checked={selected.includes(file.path)} onChange={event => setSelected(previous => event.target.checked ? [...previous, file.path] : previous.filter(path => path !== file.path))} />
        {file.path.slice('requirement-package/'.length)}（{file.size < 1024 * 1024 ? `${Math.ceil(file.size / 1024)} KB` : `${(file.size / 1024 / 1024).toFixed(1)} MB`}）
      </label></li>)}</ul> : sourceProject && !busy && <p>该项目没有可选择的原始资料。</p>}
      <button disabled={busy || !selected.length} onClick={() => void copySelected()}>{busy ? '正在读取或添加资料…' : '添加所选资料'}</button>
    </section>}
    {added.length > 0 && <p role="status">已添加：{added.join('、')}。资料已保存为独立文件。</p>}
    {error && <p role="alert">{error}</p>}
    <RequirementPackageMaterials key={revision} onOpenFile={onOpenFile} applicationId={id} requirement="requirement-package/" />
  </>
}
