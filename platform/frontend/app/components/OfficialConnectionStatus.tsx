'use client'

import Link from 'next/link'
import {useAccount} from './AuthBoundary'

export type OfficialConnection = {connection_status?:'unknown'|'blocked'|'checked';connection_message?:string;connection_checked_at?:number|null}
export default function OfficialConnectionStatus({connection}:{connection:OfficialConnection}) {
  const account=useAccount()
  const needsHelp=connection.connection_status!=='checked'
  return <div role="status">
    <p>官方智能体 · {connection.connection_message || '尚未检查登录状态'}</p>
    {needsHelp && (account?.role==='admin' ? <Link href="/official-agent">检查与修复官方智能体连接</Link> : <p>请联系管理员检查官方智能体连接。修复后可重新发送，原对话会保留。</p>)}
  </div>
}
