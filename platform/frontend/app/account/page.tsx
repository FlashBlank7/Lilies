'use client'
import { useState, type FormEvent } from 'react'
import { useRouter } from 'next/navigation'
import { api } from '@/lib/platform'
import { useAccount } from '../components/AuthBoundary'
import styles from '../auth.module.css'

export default function AccountPage() {
  const user = useAccount()
  const router = useRouter()
  const [current, setCurrent] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function save(event: FormEvent) {
    event.preventDefault(); setError('')
    if (password !== confirm) { setError('两次输入的密码不一致'); return }
    setBusy(true)
    try {
      await api('/api/v1/auth/password', { method: 'POST', body: JSON.stringify({ current_password: current, new_password: password }) })
      router.replace('/login')
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  return <main className={styles.page}><section className={styles.card}>
    <h1>账号设置</h1><p>{user?.name}</p><h2>修改密码</h2><p>修改后需要在各台设备重新登录。</p>
    <form className={styles.form} onSubmit={save}>
      <label>当前密码<input type="password" autoComplete="current-password" required value={current} onChange={e => setCurrent(e.target.value)} /></label>
      <label>新密码<input type="password" autoComplete="new-password" required minLength={8} maxLength={1024} value={password} onChange={e => setPassword(e.target.value)} /></label>
      <label>确认新密码<input type="password" autoComplete="new-password" required minLength={8} maxLength={1024} value={confirm} onChange={e => setConfirm(e.target.value)} /></label>
      {error && <p role="alert" className={styles.error}>{error}</p>}
      <button className={styles.primary} disabled={busy}>保存新密码</button>
    </form>
  </section></main>
}
