'use client'

import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import {
  idempotency,
  localLiliesErrorMessage,
  localLiliesStatus,
  pairLocalLilies,
  refreshLocalLiliesConnection,
  reconnectLocalLilies,
  type LocalLiliesStatus,
} from '@/lib/platform'
import type { Locale } from '@/lib/i18n'
import { LocalLiliesOperationAttempt } from '@/lib/local-lilies-operation-attempt'

type Props = {
  locale: Locale
  onStatusChange?: (status: LocalLiliesStatus) => void
  onConnectionSelect?: (connectionId: string) => void
}

const disabledStatus: LocalLiliesStatus = {
  enabled: false,
  default_route: false,
  connections: [],
}

export function LocalLiliesConnectionPanel({ locale, onStatusChange, onConnectionSelect }: Props) {
  const zh = locale === 'zh'
  const [status, setStatus] = useState<LocalLiliesStatus>(disabledStatus)
  const [daemonUrl, setDaemonUrl] = useState('http://127.0.0.1:8765')
  const [pairingCode, setPairingCode] = useState('')
  const [fingerprint, setFingerprint] = useState('')
  const [selectedConnectionId, setSelectedConnectionId] = useState('')
  const [pairingNewConnection, setPairingNewConnection] = useState(false)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const pairAttemptRef = useRef(new LocalLiliesOperationAttempt(idempotency))
  const reconnectAttemptRef = useRef(new LocalLiliesOperationAttempt(idempotency))
  const detectedDaemonRef = useRef('')

  const commitStatus = useCallback((next: LocalLiliesStatus) => {
    if (next.discovery?.status === 'available') {
      const detectedDaemon = `${next.discovery.base_url}\n${next.discovery.daemon_fingerprint}`
      if (detectedDaemonRef.current !== detectedDaemon) {
        detectedDaemonRef.current = detectedDaemon
        pairAttemptRef.current.reset()
        setDaemonUrl(next.discovery.base_url)
        setFingerprint(next.discovery.daemon_fingerprint)
      }
    }
    setStatus(next)
    setSelectedConnectionId(current => next.connections.some(item => item.connection_id === current)
      ? current
      : next.connections.find(item => item.status === 'connected')?.connection_id || next.connections.map(item => item.connection_id).shift() || '')
    onStatusChange?.(next)
  }, [onStatusChange])

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const next = await localLiliesStatus()
      commitStatus(next)
      setError('')
    } catch (cause) {
      // A missing/disabled bridge is an explicit state. It must never select the
      // legacy Builder or pretend that a local daemon is connected.
      commitStatus(disabledStatus)
      setError(String(cause))
    } finally {
      setLoading(false)
    }
  }, [commitStatus])

  useEffect(() => {
    void refresh()
  }, [refresh])
  useEffect(() => {
    onConnectionSelect?.(selectedConnectionId)
  }, [onConnectionSelect, selectedConnectionId])
  useEffect(() => {
    reconnectAttemptRef.current.reset()
  }, [selectedConnectionId])

  async function pair(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const previousIds = new Set(status.connections.map(item => item.connection_id))
      const next = await pairLocalLilies({
        idempotency_key: pairAttemptRef.current.current(),
        base_url: daemonUrl.trim(),
        pairing_code: pairingCode.trim(),
        expected_daemon_fingerprint: fingerprint.trim(),
      })
      pairAttemptRef.current.reset()
      reconnectAttemptRef.current.reset()
      commitStatus(next)
      const created = next.connections.find(item => !previousIds.has(item.connection_id))
      if (created) setSelectedConnectionId(created.connection_id)
      setPairingNewConnection(false)
      setPairingCode('')
    } catch (cause) {
      const message = String(cause)
      await refresh()
      setError(message)
    } finally {
      setBusy(false)
    }
  }

  async function reconnect() {
    const connectionId = selectedConnectionId
    if (!connectionId) return
    setBusy(true)
    setError('')
    try {
      commitStatus(await reconnectLocalLilies(
        connectionId,
        pairingCode.trim(),
        reconnectAttemptRef.current.current(),
      ))
      reconnectAttemptRef.current.reset()
      setPairingCode('')
    } catch (cause) {
      // Keep the known connection ID and daemon fingerprint visible so a
      // daemon outage can be diagnosed and retried without losing identity.
      const message = String(cause)
      await refresh()
      setError(message)
    } finally {
      setBusy(false)
    }
  }

  async function refreshConnection() {
    const connectionId = selectedConnectionId
    if (!connectionId) {
      await refresh()
      return
    }
    setBusy(true)
    setError('')
    try {
      await refreshLocalLiliesConnection(connectionId)
      await refresh()
    } catch (cause) {
      const message = String(cause)
      await refresh()
      setError(message)
    } finally {
      setBusy(false)
    }
  }

  const connection = status.connections.find(item => item.connection_id === selectedConnectionId) || null
  const discovery = status.discovery
  const detectedDaemon = discovery?.status === 'available' ? discovery : null
  const discoveryState = loading ? 'checking' : discovery?.status || 'unavailable'
  const enabled = status.enabled
  const connected = connection?.status === 'connected'
  const state = loading ? 'checking' : enabled ? connection?.status || 'unpaired' : 'disabled'
  const visibleError = error || localLiliesErrorMessage(connection?.last_error)

  return <section className="local-lilies-connection" data-local-lilies-feature={enabled ? 'enabled' : 'disabled'} data-local-builder-default={status.default_route ? 'true' : 'false'} data-local-lilies-state={state}>
    <header>
      <div>
        <span>{zh ? '本地执行代理' : 'Local execution agent'}</span>
        <strong>Local Lilies</strong>
      </div>
      <b className={connected ? 'connected' : ''}>{state}</b>
    </header>
    <p>{enabled
      ? zh ? '显式配对本机 daemon 后，平台才能把新 assignment 交给 Lilies。连接失败不会回退到旧 Builder。' : 'Pair this browser flow with the local daemon before assigning work. Connection failures never fall back to the legacy Builder.'
      : zh ? '本地代理功能当前关闭（local_agent_enabled=false）。启用后仍需显式选择 Local Lilies 路线。' : 'The local agent feature is off (local_agent_enabled=false). Enabling it still requires an explicit Local Lilies launch.'}</p>
    <p data-local-lilies-discovery-state={discoveryState} role="status">{detectedDaemon
      ? zh
        ? `已发现本机莉莉丝 ${detectedDaemon.base_url}（版本 ${detectedDaemon.daemon_version || '未知'}）。地址和指纹已预填；仍需在莉莉丝软件中生成一次性配对码并手工输入，平台不会自动配对。`
        : `Local Lilies was detected at ${detectedDaemon.base_url} (version ${detectedDaemon.daemon_version || 'unknown'}). Its address and fingerprint are prefilled; pairing still requires a one-time code entered by you, and the platform never pairs automatically.`
      : zh
        ? `尚未发现可用的本机莉莉丝${discovery?.status === 'unavailable' ? `（${discovery.reason}）` : ''}。启动莉莉丝软件后刷新即可重试。`
        : `No available local Lilies was detected${discovery?.status === 'unavailable' ? ` (${discovery.reason})` : ''}. Start the Lilies app and refresh to try again.`}</p>
    <dl>
      <div><dt>{zh ? '默认构建路线' : 'Default build route'}</dt><dd>{status.default_route ? 'Local Lilies' : zh ? '未设为默认' : 'Not the default'}</dd></div>
      <div><dt>Connection ID</dt><dd><code>{connection?.connection_id || '—'}</code></dd></div>
      <div><dt>Fingerprint</dt><dd><code>{connection?.daemon_fingerprint || detectedDaemon?.daemon_fingerprint || '—'}</code></dd></div>
      <div><dt>{zh ? '最后在线' : 'Last seen'}</dt><dd>{connection?.last_seen_at || '—'}</dd></div>
    </dl>
    {status.connections.length > 0 && <label className="local-lilies-connection-select">
      <span>{zh ? '管理连接' : 'Manage connection'}</span>
      <select value={selectedConnectionId} onChange={event => { reconnectAttemptRef.current.reset(); setSelectedConnectionId(event.target.value); setPairingNewConnection(false) }}>
        {status.connections.map(item => <option value={item.connection_id} key={item.connection_id}>{item.connection_id} · {item.status} · {item.base_url}</option>)}
      </select>
    </label>}
    {connection && <div className="local-lilies-connection-actions">
      <span>{connection.base_url}</span>
      {!connected && <button type="button" disabled={busy || !pairingCode.trim()} onClick={() => void reconnect()}>{busy ? (zh ? '重连中…' : 'Reconnecting…') : (zh ? '用新配对码重连' : 'Reconnect with new code')}</button>}
      <button className="ghost" type="button" disabled={busy} onClick={() => void refreshConnection()}>{zh ? '刷新' : 'Refresh'}</button>
      <button className="ghost" type="button" disabled={busy} onClick={() => { pairAttemptRef.current.reset(); reconnectAttemptRef.current.reset(); setPairingCode(''); setPairingNewConnection(true) }}>{zh ? '配对另一个 daemon' : 'Pair another daemon'}</button>
    </div>}
    {(pairingNewConnection || !connection || !connected) && <form onSubmit={pair}>
      <label><span>Daemon URL</span><input value={daemonUrl} onChange={event => { pairAttemptRef.current.reset(); setDaemonUrl(event.target.value) }} disabled={!enabled || busy} /></label>
      <label><span>{zh ? '一次性配对码' : 'One-time pairing code'}</span><input autoComplete="off" value={pairingCode} onChange={event => setPairingCode(event.target.value)} disabled={!enabled || busy} /></label>
      <label><span>{zh ? '检测到的指纹（请核对）' : 'Detected fingerprint (verify)'}</span><input autoComplete="off" value={fingerprint} onChange={event => { pairAttemptRef.current.reset(); setFingerprint(event.target.value) }} disabled={!enabled || busy} /></label>
      <small>{zh ? '地址和指纹来自本机只读发现。请在莉莉丝软件中核对指纹并生成一次性配对码；发现本身不会授予平台任何权限。' : 'The address and fingerprint come from read-only local discovery. Verify the fingerprint and generate a one-time code in the Lilies app; discovery itself grants the platform no authority.'}</small>
      <button disabled={!enabled || busy || !daemonUrl.trim() || !pairingCode.trim() || !fingerprint.trim()}>{busy ? (zh ? '配对中…' : 'Pairing…') : (zh ? '配对 daemon' : 'Pair daemon')}</button>
    </form>}
    {visibleError && <p className="error-banner" role="alert">{visibleError}</p>}
  </section>
}
