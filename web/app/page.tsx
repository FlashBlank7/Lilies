'use client'

import Link from 'next/link'
import { FormEvent, useEffect, useState } from 'react'
import { api } from '@/lib/platform'

type Application = {
  id: string
  name: string
  description: string
  mode: string
  active_version?: number | null
  draft_revision: number
  content_hash: string
  tested_hash?: string | null
}

export default function Home() {
  const [apps, setApps] = useState<Application[]>([])
  const [requirement, setRequirement] = useState('搭建一个能够理解用户问题、按意图分类并调用 Claude Agent 完成任务的工作流。')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const refresh = () => api<Application[]>('/api/v1/applications').then(setApps).catch(error => setError(String(error)))
  useEffect(() => { void refresh() }, [])

  async function create(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const app = await api<Application>('/api/v1/applications', {
        method: 'POST',
        body: JSON.stringify({ name: 'Untitled Agent', description: requirement.slice(0, 180), requirement, mode: 'workflow' }),
      })
      const build = await api<{ build_id: string }>(`/api/v1/applications/${app.id}/builds`, {
        method: 'POST', body: JSON.stringify({ requirement, auto_publish: true }),
      })
      window.location.href = `/applications/${app.id}?build=${build.build_id}`
    } catch (cause) {
      setError(String(cause))
      setBusy(false)
    }
  }

  return (
    <main className="home-shell">
      <nav className="topbar"><div className="brand"><span>F</span> Foundry</div><div className="status-dot">DeepSeek · environment</div></nav>
      <section className="hero">
        <div className="eyebrow">CLAUDE BRAIN · EDITABLE BRICKS</div>
        <h1>描述结果，<br/><em>团队来搭建。</em></h1>
        <p>智能体不是吐出一团代码。它们看到积木、协作搭建、真实运行、修到测试通过，再交付一个你仍能继续编辑的工作流。</p>
        <form className="create-card" onSubmit={create}>
          <textarea aria-label="应用需求" value={requirement} onChange={event => setRequirement(event.target.value)} />
          <div className="create-footer"><span>自动拆解需求 · 搭建 · 测试 · 发布</span><button disabled={busy || requirement.length < 10}>{busy ? '团队启动中…' : '开始搭建 →'}</button></div>
        </form>
        {error && <div className="error-banner">{error}</div>}
      </section>
      <section className="apps-section">
        <div className="section-heading"><h2>Applications</h2><span>{apps.length} 个应用</span></div>
        <div className="app-grid">
          {apps.map(item => <Link className="app-card" href={`/applications/${item.id}`} key={item.id}>
            <div className="app-icon">{item.name.slice(0, 1).toUpperCase()}</div>
            <div><h3>{item.name}</h3><p>{item.description || '等待进一步描述'}</p></div>
            <div className="app-meta"><span>{item.active_version ? `v${item.active_version} 已发布` : '草稿'}</span><span>r{item.draft_revision}</span></div>
          </Link>)}
          {!apps.length && <div className="empty-card">还没有应用。上面那段话，就是第一块砖。</div>}
        </div>
      </section>
    </main>
  )
}
