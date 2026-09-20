'use client'

import { useEffect, useState } from 'react'
import { api } from '@/lib/platform'

export default function ProjectCapabilities({ projectId, enabled, onSaved }: {
  projectId: string; enabled: boolean; onSaved: () => void
}) {
  const [selection, setSelection] = useState(enabled)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { setSelection(enabled) }, [enabled])
  return <details>
    <summary>搭建能力 · {enabled ? '允许智能体积木' : '禁止智能体积木'}</summary>
    <p>按本项目要求选择。禁止时，智能体积木不会出现在搭建手册和节点列表中，已有节点也无法运行。模型、代码和循环积木仍可用于搭建专用智能体。</p>
    <label>智能体积木 <select aria-label="智能体积木" value={selection ? 'allowed' : 'forbidden'} onChange={event => setSelection(event.target.value === 'allowed')}>
      <option value="forbidden">禁止使用</option>
      <option value="allowed">允许使用</option>
    </select></label>{' '}
    <button disabled={saving || selection === enabled} onClick={async () => {
      setSaving(true); setError('')
      try {
        await api(`/api/v1/projects/${projectId}/capabilities`, { method: 'PUT', body: JSON.stringify({ agent_modules_enabled: selection }) })
        onSaved()
      } catch (cause) { setError(String(cause)) } finally { setSaving(false) }
    }}>{saving ? '正在保存…' : '保存搭建能力'}</button>
    {error && <p role="alert">{error}</p>}
  </details>
}
