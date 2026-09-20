'use client'

import { useEffect, useRef, useState, type ComponentProps } from 'react'
import { api } from '@/lib/platform'
import { useAccount } from './AuthBoundary'
import ProjectConversation from './ProjectConversation'
import styles from './project-conversations.module.css'

type Conversation = { id: string; title: string; status: string; legacy?: boolean }
type Props = Omit<ComponentProps<typeof ProjectConversation>, 'conversationId'>

export default function ProjectConversations(props: Props) {
  const account = useAccount()
  const base = `/api/v1/projects/${props.id}/conversations`
  const storageKey = `lilies:user:${account?.id}:project:${props.id}:conversation`
  const [rows, setRows] = useState<Conversation[]>([])
  const [selected, setSelected] = useState('')
  const [title, setTitle] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const selection = useRef('')
  useEffect(() => {
    let alive = true
    selection.current = ''; setSelected(''); setRows([]); setLoading(true)
    const refresh = async () => {
      try {
        const next = await api<Conversation[]>(base)
        if (!alive) return
        setRows(next); setError('')
        if (!selection.current) {
          let saved = ''
          try { saved = sessionStorage.getItem(storageKey) || '' } catch {}
          const initial = next.find(row => row.id === saved) || next[0]
          if (initial) { selection.current = initial.id; setSelected(initial.id); setTitle(initial.title) }
        }
      } catch (cause) { if (alive) setError(String(cause)) }
      finally { if (alive) setLoading(false) }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 3000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [base, storageKey])

  function choose(row: Conversation) {
    selection.current = row.id; setSelected(row.id); setTitle(row.title); props.onSent()
    try { sessionStorage.setItem(storageKey, row.id) } catch {}
  }
  async function create() {
    setBusy(true); setError('')
    try {
      const row = await api<Conversation>(base, { method: 'POST', body: JSON.stringify({ title: '新会话' }) })
      setRows(previous => [row, ...previous]); choose(row)
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }
  async function rename() {
    setBusy(true); setError('')
    try {
      const row = await api<Conversation>(`${base}/${selected}`, { method: 'PATCH', body: JSON.stringify({ title: title.trim() }) })
      setRows(previous => previous.map(value => value.id === row.id ? { ...value, title: row.title } : value))
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }
  return <div>
    <section className={styles.panel} aria-label="我的项目会话">
      <div className={styles.controls}>
        <label>我的会话<select aria-label="选择会话" value={selected} disabled={loading || busy || !rows.length}
          onChange={event => { const row = rows.find(value => value.id === event.target.value); if (row) choose(row) }}>
          {!rows.length && <option value="">{loading ? '正在读取…' : '尚无会话'}</option>}
          {rows.map(row => <option key={row.id} value={row.id}>{row.title}{['running', 'connecting'].includes(row.status) ? ' · 运行中' : row.status === 'interrupted' ? ' · 已中断' : ''}</option>)}
        </select></label>
        <button disabled={busy || loading} onClick={() => void create()}>新建会话</button>
        {selected && selected !== 'legacy' && <form onSubmit={event => { event.preventDefault(); void rename() }}>
          <input aria-label="会话名称" value={title} maxLength={100} disabled={busy} onChange={event => setTitle(event.target.value)} />
          <button disabled={busy || !title.trim() || title.trim() === rows.find(row => row.id === selected)?.title}>重命名</button>
        </form>}
      </div>
      <p>对话历史独立保存；本项目的资料、模型、工作流和已保存结果可共享。切换会话不会停止正在运行的任务。</p>
      {error && <p role="alert">{error}</p>}
    </section>
    {selected ? <ProjectConversation key={account?.id + ':' + props.id + ':' + selected} {...props} conversationId={selected} />
      : !loading && <p>新建会话，开始分析资料、训练模型或搭建工作流。</p>}
  </div>
}
