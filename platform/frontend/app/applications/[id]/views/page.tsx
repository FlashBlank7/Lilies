'use client'

/**
 * 界面方案：每个工作流自动生成一组界面（管理/极简/对话），这里查看、改名、
 * 调整环节显隐，或从现有界面派生新界面。编辑即落库覆盖同名自动界面；
 * 隐藏在服务端投影层执行——被隐藏环节的输出不会离开后端。
 */

import Link from 'next/link'
import { use, useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/platform'
import styles from './views.module.css'

type NodeItem = { id: string; title: string; type: string }

type AutoView = {
  storage_id: string
  view_id: string
  name: string
  layout: 'form' | 'chat'
  hidden_nodes: string[]
}

type StoredView = {
  view_id: string
  name: string
  layout: 'auto' | 'form' | 'chat'
  hidden_nodes: string[]
}

type ViewsPayload = {
  nodes: NodeItem[]
  default_hidden_nodes: string[]
  auto_views: AutoView[]
  views: StoredView[]
}

type ViewCard = {
  storageId: string
  linkViewId: string
  name: string
  layout: 'auto' | 'form' | 'chat'
  hiddenNodes: string[]
  isAuto: boolean
  customized: boolean
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
  const [cards, setCards] = useState<ViewCard[]>([])
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const next = await api<ViewsPayload>(`/api/v1/applications/${id}/views`)
      setPayload(next)
      const storedById = new Map(next.views.map(view => [view.view_id, view]))
      const autoIds = new Set(next.auto_views.map(view => view.storage_id))
      const merged: ViewCard[] = next.auto_views.map(auto => {
        const stored = storedById.get(auto.storage_id)
        return {
          storageId: auto.storage_id,
          linkViewId: auto.view_id,
          name: stored?.name ?? auto.name,
          layout: stored?.layout ?? auto.layout,
          hiddenNodes: [...(stored?.hidden_nodes ?? auto.hidden_nodes)],
          isAuto: true,
          customized: Boolean(stored),
        }
      })
      for (const stored of next.views) {
        if (autoIds.has(stored.view_id)) continue
        merged.push({
          storageId: stored.view_id,
          linkViewId: stored.view_id,
          name: stored.name,
          layout: stored.layout,
          hiddenNodes: [...stored.hidden_nodes],
          isAuto: false,
          customized: true,
        })
      }
      setCards(merged)
      setError('')
    } catch (err) {
      setError(String((err as Error).message || err))
    }
  }, [id])

  useEffect(() => { void refresh() }, [refresh])

  const stageNodes = (payload?.nodes || []).filter(node => !TERMINAL_TYPES.has(node.type))

  function patchCard(storageId: string, patch: Partial<ViewCard>) {
    setCards(current => current.map(card =>
      card.storageId === storageId ? { ...card, ...patch } : card))
  }

  async function save(card: ViewCard) {
    setNotice('')
    try {
      await api(`/api/v1/applications/${id}/views/${card.storageId}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: card.name || card.storageId,
          layout: card.layout,
          hidden_nodes: card.hiddenNodes,
        }),
      })
      setNotice(`「${card.name}」已保存`)
      void refresh()
    } catch (err) {
      setError(String((err as Error).message || err))
    }
  }

  async function reset(card: ViewCard) {
    try {
      await api(`/api/v1/applications/${id}/views/${card.storageId}`, { method: 'DELETE' })
      setNotice(card.isAuto ? `「${card.name}」已恢复为自动生成` : '已删除')
      void refresh()
    } catch (err) {
      setError(String((err as Error).message || err))
    }
  }

  function derive(from: ViewCard) {
    let index = 1
    const taken = new Set(cards.map(card => card.storageId))
    while (taken.has(`custom-${index}`)) index += 1
    setCards(current => [...current, {
      storageId: `custom-${index}`,
      linkViewId: `custom-${index}`,
      name: `${from.name}·副本`,
      layout: from.layout,
      hiddenNodes: [...from.hiddenNodes],
      isAuto: false,
      customized: false,
    }])
    setNotice('新界面已派生，调整后点「保存」生效。')
  }

  async function openUsePage(linkViewId: string) {
    try {
      const result = await api<{ code: string }>(`/api/v1/applications/${id}/access-code`)
      const viewParam = linkViewId ? `&view=${linkViewId}` : ''
      window.open(`/use/${id}?code=${result.code}${viewParam}`, '_blank')
    } catch (err) {
      setError(String((err as Error).message || err))
    }
  }

  async function copyLink(linkViewId: string) {
    try {
      const result = await api<{ code: string }>(`/api/v1/applications/${id}/access-code`)
      const viewParam = linkViewId ? `&view=${linkViewId}` : ''
      const url = `${window.location.origin}/use/${id}?code=${result.code}${viewParam}`
      let copied = false
      try {
        await navigator.clipboard?.writeText(url)
        copied = true
      } catch { /* 剪贴板被浏览器拦截时降级为手动复制 */ }
      setNotice(copied ? `链接已复制：${url}` : `浏览器不让自动复制，请手动复制：${url}`)
    } catch (err) {
      setError(String((err as Error).message || err))
    }
  }

  return <main className={styles.shell}>
    <header className={styles.topbar}>
      <Link className={styles.back} href={`/applications/${id}/session`}>← 会话</Link>
      <strong>界面方案</strong>
      <span className={styles.sub}>每个工作流自动生成一组界面；在这里改名、调整环节显隐，或派生新界面</span>
    </header>

    <div className={styles.body}>
      {(notice || error) && <div className={error ? styles.error : styles.notice}>{error || notice}</div>}

      {cards.map(card => <section className={styles.card} key={card.storageId}>
        <div className={styles.viewHead}>
          <input
            className={styles.nameInput}
            onChange={event => patchCard(card.storageId, { name: event.target.value })}
            value={card.name}
          />
          {card.isAuto && !card.customized && <span className={styles.tagAuto}>自动生成</span>}
          {card.isAuto && card.customized && <span className={styles.tagCustom}>已自定义</span>}
          {!card.isAuto && <span className={styles.tagCustom}>自建界面</span>}
        </div>
        <label className={styles.layoutRow}>
          界面形态
          <select
            onChange={event => patchCard(card.storageId, { layout: event.target.value as ViewCard['layout'] })}
            value={card.layout}
          >
            {Object.entries(LAYOUT_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <div className={styles.nodeList}>
          {stageNodes.map(node => {
            const visible = !card.hiddenNodes.includes(node.id)
            return <label className={visible ? styles.nodeOn : styles.nodeOff} key={node.id}>
              <input
                checked={visible}
                onChange={() => patchCard(card.storageId, {
                  hiddenNodes: visible
                    ? [...card.hiddenNodes, node.id]
                    : card.hiddenNodes.filter(item => item !== node.id),
                })}
                type="checkbox"
              />
              <b>{node.title}</b>
              <small>{visible ? '使用者可见' : '对使用者隐藏'}</small>
            </label>
          })}
          {stageNodes.length === 0 && <p className={styles.hint}>这条工作流没有可标注的中间环节。</p>}
        </div>
        <div className={styles.actions}>
          <button onClick={() => void save(card)} type="button">保存</button>
          <button className={styles.ghost} onClick={() => void openUsePage(card.linkViewId)} type="button">打开</button>
          <button className={styles.ghost} onClick={() => void copyLink(card.linkViewId)} type="button">复制链接</button>
          <button className={styles.ghost} onClick={() => derive(card)} type="button">派生新界面</button>
          {card.customized && <button className={styles.danger} onClick={() => void reset(card)} type="button">
            {card.isAuto ? '恢复自动' : '删除'}
          </button>}
        </div>
      </section>)}

      <p className={styles.hint}>
        「管理 / 极简 / 对话」界面是平台按工作流结构自动生成的；改名或调整显隐后即成为你的定制版，
        随时可以恢复自动。想要介于两者之间的界面（比如主管只看关键判断环节），从最接近的界面「派生」再调。
      </p>
    </div>
  </main>
}
