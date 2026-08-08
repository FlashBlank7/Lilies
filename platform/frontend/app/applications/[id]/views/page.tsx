'use client'

/**
 * 界面方案：标注哪些环节对使用者可见，同一条工作流生成不同的使用界面。
 * 隐藏在服务端投影层执行——被隐藏环节的输出不会离开后端。
 */

import Link from 'next/link'
import { use, useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import styles from './views.module.css'

type NodeItem = { id: string; title: string; type: string }

type ViewItem = {
  view_id: string
  name: string
  layout: 'auto' | 'form' | 'chat'
  hidden_nodes: string[]
}

type ViewsPayload = {
  nodes: NodeItem[]
  default_hidden_nodes: string[]
  views: ViewItem[]
}

const LAYOUT_LABEL: Record<string, string> = {
  auto: '自动（有回答环节→对话，否则表单）',
  form: '表单（填输入 → 看结果）',
  chat: '对话（像聊天一样一问一答）',
}

const TERMINAL_TYPES = new Set(['start', 'end', 'answer', 'schedule_trigger'])

export default function ViewsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const [payload, setPayload] = useState<ViewsPayload | null>(null)
  const [drafts, setDrafts] = useState<Record<string, ViewItem>>({})
  const [newId, setNewId] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const next = await api<ViewsPayload>(`/api/v1/applications/${id}/views`)
      setPayload(next)
      setDrafts(Object.fromEntries(next.views.map(view => [view.view_id, { ...view }])))
      setError('')
    } catch (err) {
      setError(String((err as Error).message || err))
    }
  }, [id])

  useEffect(() => { void refresh() }, [refresh])

  // 可标注的环节：终端和触发环节不参与（它们是合同，永远可见/不可见由平台定）。
  const stageNodes = (payload?.nodes || []).filter(node => !TERMINAL_TYPES.has(node.type))

  async function save(viewId: string) {
    const draft = drafts[viewId]
    if (!draft) return
    setNotice('')
    try {
      await api(`/api/v1/applications/${id}/views/${viewId}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: draft.name || viewId,
          layout: draft.layout,
          hidden_nodes: draft.hidden_nodes,
        }),
      })
      setNotice(`「${draft.name || viewId}」已保存`)
      void refresh()
    } catch (err) {
      setError(String((err as Error).message || err))
    }
  }

  async function remove(viewId: string) {
    try {
      await api(`/api/v1/applications/${id}/views/${viewId}`, { method: 'DELETE' })
      setNotice('已删除')
      void refresh()
    } catch (err) {
      setError(String((err as Error).message || err))
    }
  }

  async function copyLink(viewId?: string) {
    try {
      const result = await api<{ code: string }>(`/api/v1/applications/${id}/access-code`)
      const viewParam = viewId ? `&view=${viewId}` : ''
      const url = `${window.location.origin}/use/${id}?code=${result.code}${viewParam}`
      // 剪贴板在非 HTTPS 或被浏览器拦截时会拒绝——失败不吃链接，展示出来手动复制。
      let copied = false
      try {
        await navigator.clipboard?.writeText(url)
        copied = true
      } catch { /* 降级为手动复制 */ }
      setNotice(copied ? `链接已复制：${url}` : `浏览器不让自动复制，请手动复制：${url}`)
    } catch (err) {
      setError(String((err as Error).message || err))
    }
  }

  function create() {
    const viewId = newId.trim().toLowerCase()
    if (!viewId) return
    setDrafts(current => ({
      ...current,
      [viewId]: { view_id: viewId, name: viewId, layout: 'auto', hidden_nodes: [...(payload?.default_hidden_nodes || [])] },
    }))
    setNewId('')
  }

  function toggleNode(viewId: string, nodeId: string) {
    setDrafts(current => {
      const draft = current[viewId]
      if (!draft) return current
      const hidden = new Set(draft.hidden_nodes)
      if (hidden.has(nodeId)) hidden.delete(nodeId)
      else hidden.add(nodeId)
      return { ...current, [viewId]: { ...draft, hidden_nodes: [...hidden] } }
    })
  }

  return <main className={styles.shell}>
    <header className={styles.topbar}>
      <Link className={styles.back} href={`/applications/${id}/session`}>← 会话</Link>
      <strong>界面方案</strong>
      <span className={styles.sub}>标注环节显隐，同一条工作流生成不同的使用界面</span>
    </header>

    <div className={styles.body}>
      {(notice || error) && <div className={error ? styles.error : styles.notice}>{error || notice}</div>}

      <section className={styles.card}>
        <h2>零标注默认界面</h2>
        <p className={styles.hint}>
          不做任何配置，使用页也能自动长出来：数据整形类环节自动隐藏
          {(() => {
            const hiddenTitles = stageNodes
              .filter(node => (payload?.default_hidden_nodes || []).includes(node.id))
              .map(node => node.title)
            return hiddenTitles.length ? `（${hiddenTitles.join('、')}）` : ''
          })()}
          ，业务环节的输出作为"过程"可展开审查；有回答环节的工作流自动变成对话界面。
        </p>
        <button className={styles.ghost} onClick={() => void copyLink()} type="button">复制默认使用链接</button>
      </section>

      {Object.values(drafts).map(draft => <section className={styles.card} key={draft.view_id}>
        <div className={styles.viewHead}>
          <input
            className={styles.nameInput}
            onChange={event => setDrafts(current => ({
              ...current,
              [draft.view_id]: { ...draft, name: event.target.value },
            }))}
            value={draft.name}
          />
          <code>{draft.view_id}</code>
        </div>
        <label className={styles.layoutRow}>
          界面形态
          <select
            onChange={event => setDrafts(current => ({
              ...current,
              [draft.view_id]: { ...draft, layout: event.target.value as ViewItem['layout'] },
            }))}
            value={draft.layout}
          >
            {Object.entries(LAYOUT_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <div className={styles.nodeList}>
          {stageNodes.map(node => {
            const visible = !draft.hidden_nodes.includes(node.id)
            return <label className={visible ? styles.nodeOn : styles.nodeOff} key={node.id}>
              <input checked={visible} onChange={() => toggleNode(draft.view_id, node.id)} type="checkbox" />
              <b>{node.title}</b>
              <small>{visible ? '使用者可见' : '对使用者隐藏'}</small>
            </label>
          })}
          {stageNodes.length === 0 && <p className={styles.hint}>这条工作流没有可标注的中间环节。</p>}
        </div>
        <div className={styles.actions}>
          <button onClick={() => void save(draft.view_id)} type="button">保存</button>
          <button className={styles.ghost} onClick={() => void copyLink(draft.view_id)} type="button">复制此界面的使用链接</button>
          <button className={styles.danger} onClick={() => void remove(draft.view_id)} type="button">删除</button>
        </div>
      </section>)}

      <section className={styles.card}>
        <h2>新建界面方案</h2>
        <div className={styles.createRow}>
          <input
            onChange={event => setNewId(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') create() }}
            placeholder="标识，如 operator、manager（小写字母数字）"
            value={newId}
          />
          <button onClick={create} type="button">新建</button>
        </div>
        <p className={styles.hint}>常见做法：给一线操作员一个只看结论的极简版，给主管一个能展开中间环节的审查版。</p>
      </section>
    </div>
  </main>
}
