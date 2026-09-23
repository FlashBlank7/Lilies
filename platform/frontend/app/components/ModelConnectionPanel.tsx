'use client'

import { useState } from 'react'
import { api } from '@/lib/platform'
import ReadingDialog from './ReadingDialog'
import styles from '@/app/projects/projects.module.css'

type ModelRole = 'main' | 'vision' | 'generation'
type Connection = {
  raw_connection?: Connection | null
  mode?: 'inherit' | 'independent'
  provider: string | null; model?: string; thinking?: string
  protocol?: string; base_url?: string; runtime_enabled?: boolean; has_api_key?: boolean
}
const defaults: Connection = { provider: 'api', thinking: 'default', protocol: 'openai', runtime_enabled: true }
const labels: Record<string, string> = { default: '模型默认', off: '关闭思考', enabled: '开启思考', low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高' }

export default function ModelConnectionPanel({ base, connected, running, onSaved, role = 'main' }: {
  base: string; connected: boolean; running: boolean; onSaved: () => Promise<unknown>; role?: ModelRole
}) {
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<'inherit' | 'independent'>('inherit')
  const [inheritedModel, setInheritedModel] = useState('')
  const [value, setValue] = useState<Connection>(defaults)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([])
  const [sourceProject, setSourceProject] = useState('')
  const [sourceRole, setSourceRole] = useState<ModelRole>('main')
  const endpoint = base + (role === 'main' ? '/agent-session' : `/${role}-model`)
  const title = role === 'generation' ? '工作流生成模型设置' : role === 'vision' ? '视觉模型设置' : '项目模型设置'
  async function edit() {
    setBusy(true); setError('')
    try {
      const response = await api<Connection>(endpoint)
      const saved = role === 'main' && response.raw_connection ? response.raw_connection : response
      setMode(saved.mode || 'inherit')
      setInheritedModel(saved.mode === 'inherit' ? saved.model || '尚未配置主模型' : '')
      if (saved.provider === 'api' && !(role === 'generation' && saved.mode === 'inherit')) setValue({ provider: 'api', model: saved.model || '',
        thinking: saved.thinking || 'medium', protocol: saved.protocol || 'openai', base_url: saved.base_url || '',
        runtime_enabled: saved.runtime_enabled || false, has_api_key: saved.has_api_key || false })
      else setValue(defaults)
      setOpen(true)
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  async function save() {
    setBusy(true); setError('')
    try {
      const { has_api_key, mode: unusedMode, ...config } = value
      if (role === 'generation' && mode === 'inherit') await api(endpoint, { method: 'DELETE' })
      else await api(endpoint, { method: 'PUT', body: JSON.stringify({ ...config, ...(key ? { api_key: key } : {}) }) })
      setKey(''); setOpen(false); await onSaved()
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  return <div className={styles.modelConnection}>
    <button disabled={running || busy} onClick={() => void edit()}>{role !== 'main' ? title : connected ? '模型设置' : '连接模型'}</button>
    {!connected && role === 'main' && <small>配置 API 模型，供工作流和项目对话使用。</small>}
    {open && <ReadingDialog title={title} onClose={() => { setOpen(false); setKey('') }}><div className={styles.modelSetup}>
      <p>{role === 'generation' ? '此模型仅用于生成和修改工作流，不改变业务节点及项目对话的模型。' : role === 'vision' ? '使用支持图片输入的 API 模型读取设计图。此连接独立于主模型。' : '连接模型 API，供工作流中的模型节点和 Lilies 使用。'}</p>
      {role === 'generation' && <><label>生成模型来源<select value={mode} onChange={event => setMode(event.target.value as 'inherit' | 'independent')}><option value="inherit">沿用项目主模型</option><option value="independent">独立配置 API 模型</option></select></label>{mode === 'inherit' && <p>沿用主模型{inheritedModel ? `：${inheritedModel}` : ''}。主模型的后续变更也会用于生成。</p>}</>}
      {(role !== 'generation' || mode === 'independent') && <>
      {base.startsWith('/api/v1/projects/') && <details><summary>沿用已有项目连接</summary>
        <button disabled={busy} onClick={async () => { try { setProjects((await api<{ id: string; name: string }[]>('/api/v1/projects')).filter(project => role === 'generation' || !base.endsWith('/' + project.id))) } catch (cause) { setError(String(cause)) } }}>选择已有项目</button>
        {projects.length > 0 && <><label>来源项目<select value={sourceProject} onChange={event => setSourceProject(event.target.value)}><option value="">请选择</option>{projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
          <label>来源模型用途<select value={sourceRole} onChange={event => setSourceRole(event.target.value as ModelRole)}><option value="main">主模型</option><option value="vision">视觉模型</option><option value="generation">独立生成模型</option></select></label>
          <small>复制连接后，可重新打开设置修改模型；密钥无需再次填写。</small>
          <button disabled={busy || !sourceProject} onClick={async () => { setBusy(true); setError(''); try { await api(base + '/model-connection/copy', { method: 'POST', body: JSON.stringify({ source_project_id: sourceProject, source_role: sourceRole, role }) }); setKey(''); setOpen(false); await onSaved() } catch (cause) { setError(String(cause)) } finally { setBusy(false) } }}>使用此连接</button></>}
      </details>}
        <label>API 协议<select value={value.protocol || 'openai'} onChange={e => setValue({ ...value, protocol: e.target.value, thinking: 'default' })}>
          <option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic Messages</option>
        </select></label>
        <label>API 地址<input type="url" value={value.base_url || ''} placeholder={value.protocol === 'anthropic' ? 'https://api.anthropic.com' : 'https://api.openai.com/v1'} onChange={e => setValue({ ...value, base_url: e.target.value })} /></label>
        <label>API Key<input type="password" autoComplete="new-password" value={key} placeholder={value.has_api_key ? '已保存；留空保留原密钥' : '填写服务商提供的 API Key'} onChange={e => setKey(e.target.value)} /></label>
      <label>模型<input value={value.model || ''} onChange={e => setValue({ ...value, model: e.target.value })} placeholder="服务商的准确模型名称" /></label>
      <label>思考模式<select value={value.thinking || 'default'} onChange={e => setValue({ ...value, thinking: e.target.value })}>
        {(value.protocol === 'anthropic' ? ['default', 'off', 'enabled', 'low', 'medium', 'high', 'xhigh', 'max'] : ['default', 'off', 'low', 'medium', 'high', 'xhigh', 'max']).map(x => <option key={x} value={x}>{labels[x]}</option>)}
      </select></label>
      <small>具体模型需支持所选思考模式；不确定时使用模型默认。</small>
      <label className={styles.modelCheckbox}><input type="checkbox" checked={value.runtime_enabled || false} onChange={e => setValue({ ...value, runtime_enabled: e.target.checked })} />{role === 'generation' ? '启用工作流生成（使用此连接的账号额度）' : '工作流可调用此模型（使用对应账号额度）'}</label>
      </>}
      <button disabled={busy || running} onClick={() => void save()}>{busy ? '保存中…' : '保存模型连接'}</button>
      {error && <p role="alert">{error}</p>}
    </div></ReadingDialog>}
    {!open && error && <p role="alert">{error}</p>}
  </div>
}
