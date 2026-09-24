'use client'

import Link from 'next/link'
import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { api, isAuthError } from '@/lib/platform'
import RequirementPackageImport from '@/app/components/RequirementPackageImport'
import AppShell from '@/app/components/AppShell'
import ReadingDialog from '@/app/components/ReadingDialog'
import { ArrowUpRight, FolderOpen, Plus, Search, Upload } from 'lucide-react'
import styles from './projects.module.css'

type Project = { id: string; name: string; description: string; members: { id: string }[] }

export default function Projects() {
  const router = useRouter()
  const [projects, setProjects] = useState<Project[]>([])
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [authRequired, setAuthRequired] = useState(false)
  const [search, setSearch] = useState('')
  const [dialog, setDialog] = useState<'new' | 'import' | null>(null)
  const refresh = useCallback(async () => {
    setLoading(true); setError('')
    try { setProjects(await api<Project[]>('/api/v1/projects')); setAuthRequired(false) }
    catch (e) { setAuthRequired(isAuthError(e)); setError(String(e)) }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void refresh() }, [refresh])
  async function create() {
    if (busy || !name.trim()) return
    setBusy(true); setError('')
    try {
      const project = await api<Project>('/api/v1/projects', { method: 'POST', body: JSON.stringify({ name: name.trim() }) })
      router.push('/projects/' + project.id)
    } catch (e) { setAuthRequired(isAuthError(e)); setError(String(e)) } finally { setBusy(false) }
  }
  const visible = projects.filter(p => (p.name + ' ' + p.description).toLocaleLowerCase().includes(search.toLocaleLowerCase()))
  return <AppShell><main className={`${styles.page} ${styles.projectIndex}`}>
    <header className={styles.header}><div><span className={styles.eyebrow}>你的工作空间</span><h1>打开项目</h1><p>从想法到可以使用的工作流，在项目里一起推进。</p></div>
      <div className={styles.actions}><button onClick={() => setDialog('import')}><Upload size={16} />导入需求包</button><button data-guide-anchor="project-create" className={styles.primary} onClick={() => setDialog('new')}><Plus size={17} />打开新项目</button></div></header>
    {authRequired && <p role="alert">登录已失效，<Link href="/login">请重新登录</Link>。</p>}
    {dialog === 'new' && <ReadingDialog title="打开新项目" onClose={() => setDialog(null)}><section aria-label="打开新项目"><p>先给项目起个名字，打开后再说明需求或补充资料。</p>
      <form className={styles.actions} onSubmit={e => { e.preventDefault(); void create() }}><input aria-label="新项目名称" placeholder="例如：库存分配改进" maxLength={100} value={name} onChange={e => setName(e.target.value)} />
        <button className={styles.primary} disabled={busy || authRequired || !name.trim()}>{busy ? '正在打开…' : '创建并打开'}</button></form>
      {error && <p role="alert" className={styles.error}>{error}</p>}</section></ReadingDialog>}
    {dialog === 'import' && <ReadingDialog title="导入项目资料" onClose={() => setDialog(null)}><RequirementPackageImport disabled={busy || authRequired} onImported={id => router.push('/projects/' + id)} /></ReadingDialog>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    <section aria-label="已有项目" tabIndex={-1} data-guide="project"><div className={styles.sectionHeading}><h2>继续已有项目 <small>{projects.length}</small></h2><label className={styles.search}><Search size={17} /><input aria-label="搜索项目" placeholder="搜索项目…" value={search} onChange={e => setSearch(e.target.value)} /></label></div>
      {loading && <p role="status">正在读取项目…</p>}
      <div className={styles.cards}>{visible.map(project => <article className={styles.card} key={project.id}>
        <div className={styles.projectIcon}><FolderOpen size={24} /></div><h2><Link href={'/projects/' + project.id}>{project.name}</Link></h2><p>{project.description || '打开项目，继续需求沟通、试用与改进。'}</p>
        <Link data-guide-anchor="project-open" className={styles.cardOpen} href={'/projects/' + project.id} aria-label={'打开项目：' + project.name}>打开项目 <ArrowUpRight size={17} /></Link>
      </article>)}</div>
      {!loading && !error && !projects.length && <p>还没有项目。打开一个新项目，或导入已有资料。</p>}
      {!loading && projects.length > 0 && !visible.length && <p role="status">没有匹配的项目，试试其他关键词。</p>}
    </section>
  </main></AppShell>
}
