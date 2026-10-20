'use client'
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import { useAccount } from '../components/AuthBoundary'
import styles from '../projects/projects.module.css'

type User = { id: string; name: string; role: string; status: string }
export default function UsersPage() {
  const account = useAccount()
  const [users, setUsers] = useState<User[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [resetId, setResetId] = useState('')
  const [password, setPassword] = useState('')
  const [message, setMessage] = useState('')
  const refresh = useCallback(async () => {
    try { setUsers(await api<User[]>('/api/v1/users')) } catch (cause) { setError(String(cause)) }
  }, [])
  useEffect(() => { if (account?.role === 'admin') void refresh() }, [account, refresh])
  async function status(user: User) {
    setBusy(true); setError(''); setMessage('')
    try {
      await api(`/api/v1/users/${user.id}/status`, { method: 'POST', body: JSON.stringify({ status: user.status === 'active' ? 'disabled' : 'active' }) })
      await refresh()
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  async function reset() {
    setBusy(true); setError(''); setMessage('')
    try {
      await api(`/api/v1/users/${resetId}/password`, { method: 'POST', body: JSON.stringify({ password }) })
      setPassword(''); setResetId(''); setMessage('密码已重置，该账号需要重新登录。请将新密码告知账号本人。')
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  if (account?.role !== 'admin') return <main className={styles.page}><h1>账号管理</h1><p>只有平台管理员可以管理账号。</p></main>
  return <main className={styles.page}><h1>账号管理</h1><p>用户通过注册页自行创建账号。管理员可以停用账号、恢复使用或重置密码。</p>
    {error && <p role="alert" className={styles.error}>{error}</p>}{message && <p role="status">{message}</p>}
    <section className={styles.panel}><table><thead><tr><th>用户名</th><th>身份</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>{users.map(user => <tr key={user.id}><td>{user.name}</td><td>{user.role === 'admin' ? '管理员' : '用户'}</td><td>{user.status === 'active' ? '正常' : '已停用'}</td>
        <td><button disabled={busy} onClick={() => void status(user)}>{user.status === 'active' ? '停用' : '恢复'}</button><button disabled={busy} onClick={() => { setResetId(user.id); setPassword(''); setMessage('') }}>重置密码</button></td></tr>)}</tbody></table>
    </section>
    {resetId && <section className={styles.panel}><h2>重置 {users.find(user => user.id === resetId)?.name} 的密码</h2>
      <form className={styles.actions} onSubmit={event => { event.preventDefault(); void reset() }}>
        <label>新密码<input type="password" autoComplete="new-password" minLength={8} maxLength={1024} required value={password} onChange={event => setPassword(event.target.value)} /></label>
        <button disabled={busy}>保存新密码</button><button type="button" disabled={busy} onClick={() => { setResetId(''); setPassword('') }}>取消</button>
      </form></section>}
  </main>
}
