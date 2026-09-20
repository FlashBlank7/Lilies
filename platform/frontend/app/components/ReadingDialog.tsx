'use client'

import { useEffect, useId, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'
import styles from './reading-dialog.module.css'

export default function ReadingDialog({ title, children, onClose, wide = false, storageKey }: {
  title: string; children: ReactNode; onClose: () => void; wide?: boolean; storageKey?: string
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const body = useRef<HTMLDivElement>(null)
  const titleId = useId()
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const dialog = ref.current
    dialog?.showModal()
    return () => { dialog?.close(); previous?.focus() }
  }, [])
  useEffect(() => { if (storageKey && body.current) { try { body.current.scrollTop = Number(sessionStorage.getItem(storageKey) || 0) } catch {} } }, [storageKey])
  return <dialog ref={ref} className={styles.dialog} data-wide={wide} aria-labelledby={titleId}
    onCancel={e => { e.preventDefault(); onClose() }} onClick={e => { if (e.target === ref.current) onClose() }}>
    <div className={styles.inner}>
      <header><h2 id={titleId}>{title}</h2><button autoFocus onClick={onClose} aria-label="关闭阅读窗口"><X size={19} /></button></header>
      <div ref={body} className={styles.body} onScroll={e => { if (storageKey) { try { sessionStorage.setItem(storageKey, String(e.currentTarget.scrollTop)) } catch {} } }}>{children}</div>
    </div>
  </dialog>
}
