'use client'

/** 用户管理（管理员）：列表 + 启用/禁用。注册走 bench/客户端的注册页（共享注册令牌），
 * 这里只做治理。非管理员令牌会得到 403 提示。 */

import { useEffect, useState } from 'react'
import { api, getClientToken, saveClientToken } from '@/lib/platform'

type User = { id: string; name: string; role: string; status: string; created_at: string }

export default function UsersPage() {
  const [users, setUsers] = useState<User[]>([])
  const [error, setError] = useState('')
  const [token, setToken] = useState('')

  async function refresh() {
    setError('')
    try {
      setUsers(await api<User[]>('/api/v1/users'))
    } catch (cause) {
      setError(String(cause))
    }
  }
  useEffect(() => { void refresh() }, [])

  async function toggle(user: User) {
    await api(`/api/v1/users/${user.id}/status`, {
      method: 'POST',
      body: JSON.stringify({ status: user.status === 'active' ? 'disabled' : 'active' }),
    })
    void refresh()
  }

  return <main style={{ maxWidth: 760, margin: '0 auto', padding: '40px 24px', fontSize: 14 }}>
    <h1 style={{ fontSize: 20, marginBottom: 4 }}>用户管理</h1>
    <p style={{ color: 'var(--lil-muted, #68717f)', fontSize: 13, marginBottom: 20 }}>
      新成员在客户端用「注册令牌 + 自定用户名密码」自助注册；此处只做启用/禁用与角色查看。
    </p>
    {error && <div style={{ background: '#fef3f2', border: '1px solid #f0b4ae', borderRadius: 10, padding: '10px 14px', marginBottom: 14, fontSize: 13 }}>
      {error}
      <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
        <input type="password" placeholder="管理员令牌" value={token} onChange={e => setToken(e.target.value)}
          style={{ flex: 1, border: '1px solid #e4e7ec', borderRadius: 8, padding: '7px 10px' }} />
        <button onClick={() => { saveClientToken(token); void refresh() }}
          style={{ border: 0, borderRadius: 8, background: '#0e7a5f', color: '#fff', padding: '7px 16px', cursor: 'pointer' }}>使用</button>
      </div>
    </div>}
    <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff', border: '1px solid #e4e7ec', borderRadius: 12, overflow: 'hidden' }}>
      <thead><tr style={{ background: '#f7f9fb', textAlign: 'left', fontSize: 12, color: '#68717f' }}>
        <th style={{ padding: '9px 14px' }}>用户名</th><th>角色</th><th>状态</th><th>注册时间</th><th></th>
      </tr></thead>
      <tbody>{users.map(user => <tr key={user.id} style={{ borderTop: '1px solid #eceff3' }}>
        <td style={{ padding: '9px 14px', fontWeight: 600 }}>{user.name}</td>
        <td>{user.role === 'admin' ? '管理员' : '成员'}</td>
        <td><span style={{ fontSize: 11, borderRadius: 99, padding: '2px 10px',
          background: user.status === 'active' ? '#e7f4ef' : '#fff6e3',
          color: user.status === 'active' ? '#0e7a5f' : '#8a5a00' }}>
          {user.status === 'active' ? '正常' : '已禁用'}</span></td>
        <td style={{ color: '#98a1af', fontSize: 12 }}>{(user.created_at || '').slice(0, 10)}</td>
        <td style={{ textAlign: 'right', paddingRight: 14 }}>
          <button onClick={() => void toggle(user)} style={{ border: '1px solid #e4e7ec', background: '#fff',
            borderRadius: 8, padding: '4px 12px', fontSize: 12, cursor: 'pointer',
            color: user.status === 'active' ? '#b42318' : '#0e7a5f' }}>
            {user.status === 'active' ? '禁用' : '启用'}</button>
        </td>
      </tr>)}</tbody>
    </table>
    {!users.length && !error && <p style={{ color: '#98a1af', textAlign: 'center', padding: 30 }}>还没有注册用户</p>}
  </main>
}
