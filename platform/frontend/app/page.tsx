'use client'

import Link from 'next/link'
import { FormEvent, useEffect, useState } from 'react'
import { api, clearClientToken, getClientToken, isAuthError, saveClientToken } from '@/lib/platform'
import { defaultLocale, isLocale, messages, nextLocale, type Locale } from '@/lib/i18n'

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

function deriveApplicationName(requirement: string) {
  const text = requirement.trim().replace(/\s+/g, ' ')
  if (!text) return '新智能体'
  const first = text.split(/[。.!?\n\r]/)[0].replace(/^[\s"',，,：:；;“”‘’`]+|[\s"',，,：:；;“”‘’`]+$/g, '')
  const cleaned = first
    .replace(/^(请|请帮我|我需要|我想要|帮我|帮我做|搭建|创建|制作|生成|构建|设计)(一个|一款|一个可以|可以|能够|能)?/, '')
    .replace(/^(please|build|create|make|generate|design)\s+(a|an|the)?\s*/i, '')
    .replace(/^[\s，,：:；;]+|[\s，,：:；;]+$/g, '')
  return (cleaned || first || text).slice(0, 32).replace(/[\s，,：:；;]+$/g, '') || '新智能体'
}

export default function Home() {
  const [locale, setLocale] = useState<Locale>(defaultLocale)
  const t = messages[locale]
  const [apps, setApps] = useState<Application[]>([])
  const [requirement, setRequirement] = useState<string>(t.requirementPlaceholder)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [authRequired, setAuthRequired] = useState(false)
  const [tokenInput, setTokenInput] = useState('')

  const refresh = () => api<Application[]>('/api/v1/applications').then(applications => {
    setApps(applications)
    setAuthRequired(false)
    setError('')
  }).catch(error => {
    if (isAuthError(error)) setAuthRequired(true)
    setError(String(error))
  })
  useEffect(() => {
    const stored = globalThis.localStorage?.getItem('foundry.locale')
    if (isLocale(stored)) setLocale(stored)
    setTokenInput(getClientToken())
    void refresh()
  }, [])

  function toggleLocale() {
    const value = nextLocale(locale)
    setLocale(value)
    globalThis.localStorage?.setItem('foundry.locale', value)
  }

  async function create(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const name = deriveApplicationName(requirement)
      const app = await api<Application>('/api/v1/applications', {
        method: 'POST',
        body: JSON.stringify({ name, description: requirement.slice(0, 180), requirement, mode: 'workflow' }),
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

  function saveToken(event: FormEvent) {
    event.preventDefault()
    saveClientToken(tokenInput)
    setError(t.authSaved)
    void refresh()
  }

  return (
    <main className="home-shell">
      <nav className="topbar"><div className="brand"><span>F</span> Foundry</div><div className="topbar-actions"><button className="lang-toggle" onClick={toggleLocale}>{t.switchLabel}</button><div className="status-dot">{t.status}</div></div></nav>
      <section className="hero">
        <div className="eyebrow">{t.eyebrow}</div>
        <h1>{t.heroTitleA}<br/><em>{t.heroTitleB}</em></h1>
        <p>{t.heroCopy}</p>
        <form className="create-card" onSubmit={create}>
          <textarea aria-label={t.requirementAria} value={requirement} onChange={event => setRequirement(event.target.value)} />
          <div className="create-footer"><span>{t.createHint}</span><button disabled={busy || requirement.length < 10}>{busy ? t.createBusy : t.createButton}</button></div>
        </form>
        {authRequired && <form className="auth-card" onSubmit={saveToken}>
          <div><strong>{t.authTitle}</strong><p>{t.authCopy}</p></div>
          <input type="password" value={tokenInput} placeholder={t.authPlaceholder} onChange={event => setTokenInput(event.target.value)} />
          <div className="auth-actions"><button>{t.authSave}</button><button type="button" className="ghost" onClick={() => { clearClientToken(); setTokenInput('') }}>{t.authClear}</button></div>
        </form>}
        {error && <div className="error-banner">{error}</div>}
      </section>
      <section className="apps-section">
        <div className="section-heading"><h2>{t.applications}</h2><span>{t.appCount(apps.length)}</span></div>
        <div className="app-grid">
          {apps.map(item => <Link className="app-card" href={`/applications/${item.id}`} key={item.id}>
            <div className="app-icon">{item.name.slice(0, 1).toUpperCase()}</div>
            <div><h3>{item.name}</h3><p>{item.description || t.fallbackDescription}</p></div>
            <div className="app-meta"><span>{item.active_version ? t.published(item.active_version) : t.draft}</span><span>r{item.draft_revision}</span></div>
          </Link>)}
          {!apps.length && <div className="empty-card">{t.emptyApps}</div>}
        </div>
      </section>
    </main>
  )
}
