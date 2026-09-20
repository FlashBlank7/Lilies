'use client'

import Link from 'next/link'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, clearClientToken } from '@/lib/platform'
import styles from '../auth.module.css'

export type AccountUser = { id: string; name: string; role: 'admin' | 'member'; status: string }
const AccountContext = createContext<AccountUser | null>(null)
export const useAccount = () => useContext(AccountContext)

export default function AuthBoundary({ children }: { children: ReactNode }) {
  const path = usePathname()
  const router = useRouter()
  const embedded = useSearchParams().get('embedded') === '1'
  const [user, setUser] = useState<AccountUser | null>(null)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const publicPage = path === '/login' || path === '/register'
  useEffect(() => {
    clearClientToken()
    if (publicPage) { setUser(null); return }
    let current = true
    const login = () => {
      // A request started before an account switch can fail after the new login.
      // Check the current cookie without emitting another unauthorized event.
      void fetch('/api/platform/api/v1/me', { cache: 'no-store' }).then(async response => {
        if (!current) return
        if (response.ok) { setUser((await response.json()).user); return }
        if (response.status === 401) {
          setUser(null)
          router.replace('/login?next=' + encodeURIComponent(window.location.pathname + window.location.search))
        }
      }).catch(cause => { if (current) setError(String(cause)) })
    }
    window.addEventListener('lilies:unauthorized', login)
    setError('')
    const check = () => { void api<{ user: AccountUser }>('/api/v1/me').then(result => {
      if (current) setUser(result.user)
    }).catch(cause => { if (current) setError(String(cause)) }) }
    check()
    const channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('lilies-account') : null
    if (channel) channel.onmessage = () => { setUser(null); setRetry(n => n + 1) }
    window.addEventListener('focus', check)
    return () => { current = false; channel?.close(); window.removeEventListener('focus', check); window.removeEventListener('lilies:unauthorized', login) }
  }, [path, publicPage, retry, router])

  async function logout() {
    try { await api('/api/v1/auth/logout', { method: 'POST' }); setUser(null); router.replace('/login') }
    catch (cause) { setError(String(cause)) }
  }
  if (publicPage) return children
  if (!user) return <main className={styles.loading}>
    {error ? <><p role="alert">{error}</p><button onClick={() => setRetry(n => n + 1)}>重新连接</button><Link href="/login">前往登录</Link></> : <p role="status">正在打开平台…</p>}
  </main>
  return <AccountContext.Provider key={user.id} value={user}>
    {!embedded && <nav className={styles.accountBar} aria-label="账号导航">
      <Link href="/projects">Lilies</Link><span className={styles.spacer} />
      {user.role === 'admin' && <Link href="/users">账号管理</Link>}
      <Link href="/account">{user.name}</Link><button onClick={() => void logout()}>退出登录</button>
    </nav>}
    {error && <p role="alert" className={styles.error}>{error}</p>}{children}
  </AccountContext.Provider>
}
