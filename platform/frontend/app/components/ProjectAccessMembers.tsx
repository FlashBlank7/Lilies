'use client'
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import styles from '../projects/projects.module.css'

type Member = { id: string; name: string; status: string; role: 'owner' | 'collaborator' }
export default function ProjectAccessMembers({ projectId, canManage, onChanged }: { projectId: string; canManage: boolean; onChanged: () => void }) {
  const base = `/api/v1/projects/${projectId}/access-members`
  const [members, setMembers] = useState<Member[]>([])
  const [name, setName] = useState('')
  const [role, setRole] = useState('collaborator')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const refresh = useCallback(async () => {
    try { setMembers(await api<Member[]>(base)) } catch (cause) { setError(String(cause)) }
  }, [base])
  useEffect(() => { void refresh() }, [refresh])
  async function add() {
    setBusy(true); setError('')
    try {
      setMembers(await api<Member[]>(base, { method: 'POST', body: JSON.stringify({ name: name.trim(), role }) }))
      setName(''); onChanged()
    } catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  async function remove(id: string) {
    setBusy(true); setError('')
    try { await api(`${base}/${id}`, { method: 'DELETE' }); await refresh() }
    catch (cause) { setError(String(cause)) } finally { setBusy(false) }
  }
  return <section aria-label="项目成员"><h2>项目成员</h2>
    <p>成员共享项目资料、模型、工作流和运行结果。负责人管理成员与模型连接。</p>
    {members.length > 0 ? <ul>{members.map(member => <li key={member.id}>
      {member.name} · {member.role === 'owner' ? '负责人' : '协作者'}{member.status !== 'active' && ' · 账号已停用'}
      {canManage && member.role !== 'owner' && <button disabled={busy} onClick={() => void remove(member.id)} aria-label={`移除 ${member.name}`}>移除</button>}
    </li>)}</ul> : <p>尚未分配项目负责人。管理员可在下面指定。</p>}
    {canManage && <form className={styles.actions} onSubmit={event => { event.preventDefault(); void add() }}>
      <label>成员用户名<input required maxLength={40} value={name} onChange={e => setName(e.target.value)} placeholder="输入已注册的准确用户名" /></label>
      <label>项目角色<select value={role} onChange={e => setRole(e.target.value)}><option value="collaborator">协作者</option><option value="owner">转交为负责人</option></select></label>
      <button disabled={busy || !name.trim()}>{role === 'owner' ? '指定负责人' : '添加成员'}</button>
    </form>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
  </section>
}
