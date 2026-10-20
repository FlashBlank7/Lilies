'use client'

import Link from 'next/link'
import { useRouter, useSearchParams } from 'next/navigation'
import { useState, type FormEvent } from 'react'
import { api } from '@/lib/platform'
import styles from '../auth.module.css'

export default function AccountForm({ register = false }: { register?: boolean }) {
  const router = useRouter()
  const params = useSearchParams()
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit(event: FormEvent) {
    event.preventDefault(); setError('')
    if (register && password !== confirmation) { setError('两次输入的密码不一致'); return }
    setBusy(true)
    try {
      await api('/api/v1/auth/' + (register ? 'register' : 'login'), { method: 'POST', body: JSON.stringify({ name: name.trim(), password }) })
      const next = params.get('next') || '/projects'
      router.replace(next.startsWith('/') && !next.startsWith('//') && !next.includes('\\') ? next : '/projects')
      router.refresh()
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  return <main className={styles.page}><section className={styles.card}>
    <Link href="/login" className={styles.brand}>Lilies</Link>
    <h1>{register ? '创建账号' : '欢迎回来'}</h1>
    <p>{register ? '创建自己的项目，邀请同事一起搭建和使用工作流。' : '登录后继续你的项目、智能体和工作流。'}</p>
    <form onSubmit={submit} className={styles.form}>
      <label>用户名<input autoFocus autoComplete="username" maxLength={40} required value={name} onChange={e => setName(e.target.value)} /></label>
      <label>密码<input type="password" autoComplete={register ? 'new-password' : 'current-password'} minLength={register ? 8 : undefined} maxLength={1024} required value={password} onChange={e => setPassword(e.target.value)} /></label>
      {register && <label>确认密码<input type="password" autoComplete="new-password" minLength={8} maxLength={1024} required value={confirmation} onChange={e => setConfirmation(e.target.value)} /></label>}
      {error && <p className={styles.error} role="alert">{error}</p>}
      <button className={styles.primary} disabled={busy}>{busy ? '正在处理…' : register ? '注册并进入平台' : '登录'}</button>
    </form>
    <p>{register ? '已有账号？' : '还没有账号？'} <Link href={register ? '/login' : '/register'}>{register ? '前往登录' : '注册账号'}</Link></p>
    {!register && <p className={styles.hint}>忘记密码请联系平台管理员重置。</p>}
  </section></main>
}
