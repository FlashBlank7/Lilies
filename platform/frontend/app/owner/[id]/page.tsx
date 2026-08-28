'use client'

/**
 * 业主会话面（免登录，业主码定向访问）：客户在这里回答莉莉丝的提问、
 * 随时插话改要求、看交付说明、按验收单一键返修。看不到工作台/画布/导出。
 */

import { use, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import styles from './owner.module.css'

type OwnerState = {
  application: { id: string; name: string }
  build: { id: string; status: string; pending_question: string | null; updated_at: string } | null
  acceptance: { accepted: boolean; passed_cases?: number; total_cases?: number } | null
  published_version: number | null
}

type TranscriptRecord = {
  kind?: string
  text?: string
  turn?: number
  tool_calls?: Array<{ tool: string; arguments: Record<string, unknown> }>
}

const ACTIVE = new Set(['queued', 'building'])
const STATUS: Record<string, { label: string; tone: string }> = {
  queued: { label: '排队中', tone: 'working' },
  building: { label: '搭建中', tone: 'working' },
  needs_attention: { label: '等你回复', tone: 'waiting' },
  ready: { label: '已就绪', tone: 'done' },
  published: { label: '已交付', tone: 'done' },
  cancelled: { label: '已暂停', tone: 'waiting' },
  failed: { label: '遇到问题', tone: 'waiting' },
}

// 搭建方的正文由**后端**统一挡掉（api._owner_safe_records，那里有完整理由）。
// 前端这里不再自己判：先前用「是不是中文」判过，线上主力引擎思考时说中文，
// 判据方向完全失效。真到了这里的正文就是能给业主看的。
// 纯工具轮翻译成业主能读的动作行（与会话页同一套话术的精简版）
function describeAction(record: TranscriptRecord): string {
  const phrases: string[] = []
  for (const call of record.tool_calls || []) {
    const tool = call.tool
    const phrase = tool.startsWith('draft_add') ? '搭了一个环节'
      : tool.startsWith('draft_connect') ? '接通了环节流向'
      : tool.startsWith('draft_update') ? '调整了环节配置'
      : tool.startsWith('draft_remove') ? '清理了环节'
      : tool.startsWith('test_run') ? '跑了一遍测试'
      : tool.startsWith('test') ? '准备了测试用例'
      : tool === 'draft_publish' ? '发布了正式版'
      : tool === 'run_inspect' ? '检查了运行记录'
      : tool === 'ask_owner' ? '向你提了一个问题'
      : tool.startsWith('catalog') || tool.startsWith('manual') ? '查了资料'
      : '推进了一步工作'
    if (!phrases.includes(phrase)) phrases.push(phrase)
  }
  return phrases.slice(0, 3).join('，')
}

export default function OwnerPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const code = useSearchParams().get('code') || ''
  const [state, setState] = useState<OwnerState | null>(null)
  const [records, setRecords] = useState<TranscriptRecord[]>([])
  const [denied, setDenied] = useState(false)
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState(false)
  const [notice, setNotice] = useState('')
  const streamRef = useRef<HTMLDivElement>(null)

  const api = useCallback(async (path: string, init?: RequestInit) => {
    const response = await fetch(`/api/platform/api/v1/owner/${id}${path}`, init)
    if (response.status === 403) { setDenied(true); throw new Error('denied') }
    if (!response.ok) throw new Error((await response.json()).detail || response.statusText)
    return response.json()
  }, [id])

  const refresh = useCallback(async () => {
    try {
      const [nextState, transcript] = await Promise.all([
        api(`/state?code=${encodeURIComponent(code)}`),
        api(`/transcript?code=${encodeURIComponent(code)}`),
      ])
      setState(nextState)
      setRecords(transcript.records || [])
    } catch { /* denied 已置位或瞬时网络问题，下轮再试 */ }
  }, [api, code])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 4000)
    return () => window.clearInterval(timer)
  }, [refresh])
  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight, behavior: 'smooth' })
  }, [records.length])

  // 兜底不能把英文状态码原样抬给客户看
  const status = state?.build ? STATUS[state.build.status] || { label: '进行中', tone: 'working' } : null
  const building = Boolean(state?.build && ACTIVE.has(state.build.status))

  const deliveryNote = useMemo(() => {
    if (!state?.build || !['ready', 'published'].includes(state.build.status)) return ''
    for (let index = records.length - 1; index >= 0; index -= 1) {
      const record = records[index]
      if (record.kind !== 'owner' && record.kind !== 'event' && (record.text || '').trim().length > 40) {
        return (record.text || '').trim()
      }
    }
    return ''
  }, [state, records])

  async function send() {
    const text = message.trim()
    if (!text || sending) return
    setSending(true)
    setNotice('')
    try {
      const result = await api('/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, message: text }),
      })
      setNotice(result.delivered === 'live' ? '已送达，下一轮开工前会读到。' : '已带着你的新要求继续。')
      setMessage('')
      window.setTimeout(() => void refresh(), 600)
    } catch (error) {
      if (!denied) setNotice(String(error))
    } finally {
      setSending(false)
    }
  }

  async function repair() {
    setNotice('')
    try {
      const result = await api('/repair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      })
      setNotice(`已按验收单发起返修（${result.failure_items} 项失败项），已经开工了。`)
      window.setTimeout(() => void refresh(), 600)
    } catch (error) {
      if (!denied) setNotice(String(error))
    }
  }

  if (!code || denied) {
    return <main className={styles.denied}><p>链接无效或已更换——请联系服务方获取新的业主链接。</p></main>
  }

  return <main className={styles.shell}><div className={styles.card}>
    <header className={styles.head}>
      <h1>{state?.application.name || '…'}</h1>
      {status && <span className={styles.status} data-tone={status.tone}>{status.label}</span>}
    </header>

    {state?.acceptance && <div className={styles.acceptance}>
      <span>独立验收：<b data-bad={state.acceptance.accepted ? undefined : '1'}>
        {state.acceptance.accepted ? '全部通过' : `${state.acceptance.passed_cases ?? '?'} / ${state.acceptance.total_cases ?? '?'} 通过`}
      </b></span>
      {!state.acceptance.accepted && !building && <button className={styles.repair} onClick={() => void repair()}>按验收单返修</button>}
    </div>}

    {deliveryNote && <div className={styles.delivery}><b>交付说明</b>{deliveryNote}</div>}

    <div className={styles.stream} ref={streamRef}>
      {!records.length && <div className={styles.empty}>
        {state?.build ? '正在准备，稍等片刻…' : '服务方还没有为这个应用发起搭建。'}
      </div>}
      {records.map((record, index) => {
        if (record.kind === 'owner') return <div className={`${styles.msg} ${styles.owner}`} key={index}>{record.text}</div>
        if (record.kind === 'event') return record.text
          ? <div className={styles.action} key={index}>· {record.text}</div> : null
        const text = (record.text || '').trim()
        if (text) return <div className={`${styles.msg} ${styles.lilith}`} key={index}><em>搭建方</em>{text}</div>
        const action = describeAction(record)
        if (action) return <div className={styles.action} key={index}>⚙ {action}</div>
        // 英文自言自语的那一轮：不把原文抬出去，也别让时间线突然断掉
        return text ? <div className={styles.action} key={index}>⚙ 在琢磨下一步</div> : null
      })}
      {state?.build?.pending_question && <div className={styles.question}>
        <b>这里有个问题等你回复</b>{state.build.pending_question}
      </div>}
    </div>

    <div className={styles.composer}>
      {notice && <div className={styles.notice}>{notice}</div>}
      <div className={styles.row}>
        <textarea
          placeholder={state?.build?.pending_question ? '回答上面的问题，搭建会继续。'
            : building ? '想调整就直接说，下一轮开工前会读到。'
            : state?.build ? '想改什么？直接说，会在现有成果上继续。'
            : '等服务方发起搭建后，这里就可以对话了。'}
          value={message}
          disabled={!state?.build}
          onChange={event => setMessage(event.target.value)}
          onKeyDown={event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) void send() }}
        />
        <button disabled={sending || !message.trim() || !state?.build} onClick={() => void send()}>
          {sending ? '发送中…' : '发送'}
        </button>
      </div>
    </div>
  </div></main>
}
