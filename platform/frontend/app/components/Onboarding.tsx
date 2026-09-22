'use client'

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import { api } from '@/lib/platform'
import styles from './onboarding.module.css'

export const guideSteps = [
  { id: 'project', title: '进入项目', text: '项目集中保存资料、工作流、模型和结果。成员共享资源，各自对话独立。没有公司项目时，请让负责人在「设置 → 项目成员」添加你的用户名；也可以自行创建项目。', action: '已打开项目' },
  { id: 'materials', title: '准备资料', text: '在项目空间勾选本次要处理的文件，也可以点击「添加资料」。没有资料时先上传；选择文件后，可带着所选资料开始对话。', action: '已选择或添加资料' },
  { id: 'workflow', title: '找到工作流', text: '查看已有流程的用途与输入要求。没有流程时，可从官方机器学习流程加入独立可编辑副本。加入流程不会启动训练，模型也可以稍后绑定。', action: '已查看或加入工作流' },
  { id: 'conversation', title: '通过对话调用', text: '若尚无会话，先在对话页点击「新建会话」。在项目空间点击「让智能体调用」，检查带入的流程和资料，再亲自点击发送。模型未连接时，负责人可在设置中配置，普通成员需联系负责人；无需配置也能继续阅读教程。', action: '已发送任务请求' },
  { id: 'results', title: '查看运行与结果', text: '通过对话结果卡或运行记录查看状态、停止运行和检查错误。打开结果文件可预览并下载原文件。尚无运行时先了解入口，不会生成虚假结果。', action: '已打开运行详情' },
  { id: 'next', title: '了解后续用法', text: '在对话页新建或打开会话后，「完成任务」让智能体直接处理问题；「创建工作流」随时生成可复用流程，不要求任务或训练已完成。工作流画布支持表单编辑，之后可再次调用。', action: '已切换对话用途' },
] as const
export type GuideStep = typeof guideSteps[number]['id']
type State = { status: 'new' | 'active' | 'skipped' | 'finished'; step: GuideStep; completed_steps: GuideStep[]; project_id: string | null }
type Patch = Partial<State> & { restart?: boolean }
type GuideContext = { active: boolean; unavailable: () => void; mark: (step: GuideStep, projectId: string) => void; open: () => void }
const Context = createContext<GuideContext>({ active: false, unavailable: () => {}, mark: () => {}, open: () => {} })
export const useOnboarding = () => useContext(Context)
export function TutorialLauncher() { const { open } = useOnboarding(); return <button onClick={open}>使用教程</button> }
const endpoint = '/api/v1/me/onboarding'
const initial: State = { status: 'skipped', step: 'project', completed_steps: [], project_id: null }

export default function Onboarding({ children }: { children: ReactNode }) {
  const router = useRouter()
  const pathname = usePathname()
  const [state, setState] = useState<State | null>(null)
  const current = useRef<State | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [target, setTarget] = useState<GuideStep | null>(null)
  const [height, setHeight] = useState(0)
  const panel = useRef<HTMLElement>(null)
  const opener = useRef<HTMLElement | null>(null)
  const alive = useRef(true)
  const queue = useRef(Promise.resolve())
  const pending = useRef<Patch | null>(null)
  const saveState = useCallback((value: State) => { current.current = value; setState(value) }, [])
  const refresh = useCallback(async (autoOpen = false) => {
    try {
      const result = await api<State>(endpoint)
      if (!alive.current) return
      saveState(result); setError('')
      if (autoOpen && (result.status === 'new' || result.status === 'active')) setExpanded(true)
    } catch (cause) { if (alive.current) setError(String(cause)) }
  }, [saveState])
  useEffect(() => { alive.current = true; void refresh(true); return () => { alive.current = false } }, [refresh])

  const update = useCallback((patch: Patch) => {
    queue.current = queue.current.then(async () => {
      if (!alive.current) return
      const next = patch.restart ? patch : { ...pending.current, ...patch, completed_steps: [...new Set([...(pending.current?.completed_steps || []), ...(patch.completed_steps || [])])] }
      pending.current = next; setBusy(true)
      try {
        const result = await api<State>(endpoint, { method: 'PATCH', body: JSON.stringify(next) })
        if (!alive.current) return
        pending.current = null; saveState(result); setError('')
      } catch (cause) { if (alive.current) setError(String(cause)) }
      finally { if (alive.current) setBusy(false) }
    })
  }, [saveState])
  const mark = useCallback((step: GuideStep, projectId: string) => {
    if (current.current?.status !== 'active') return
    if (current.current.completed_steps.includes(step) && current.current.project_id === projectId) return
    update({ completed_steps: [step], project_id: projectId })
  }, [update])
  const unavailable = useCallback(() => {
    if (current.current?.status !== 'active') return
    update({ project_id: null, step: 'project' }); setTarget('project'); setExpanded(true); router.push('/projects')
  }, [router, update])
  const open = useCallback(() => {
    opener.current = document.activeElement as HTMLElement
    setExpanded(true); void refresh(); requestAnimationFrame(() => panel.current?.focus())
  }, [refresh])
  const close = () => { setExpanded(false); setTarget(null); opener.current?.focus() }

  useEffect(() => {
    if (!expanded) return
    const editing = (event: FocusEvent) => {
      const element = event.target as HTMLElement | null
      if (element && !panel.current?.contains(element) && element.matches('input, textarea, select, [contenteditable="true"]')) setExpanded(false)
    }
    document.addEventListener('focusin', editing)
    return () => document.removeEventListener('focusin', editing)
  }, [expanded])
  useEffect(() => {
    if (!expanded || !panel.current) { setHeight(0); return }
    const measure = () => setHeight((panel.current?.getBoundingClientRect().height || 0) + 24)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure); observer.observe(panel.current)
    return () => observer.disconnect()
  }, [expanded, state?.status])
  useEffect(() => {
    if (!target) return
    let highlighted: HTMLElement | null = null
    let scrolled = false
    const highlight = () => {
      const candidates = [...document.querySelectorAll<HTMLElement>(`[data-guide="${target}"]`), ...document.querySelectorAll<HTMLElement>(`[data-guide-fallback="${target}"]`)]
      const next = candidates.find(el => el.getClientRects().length > 0) || null
      if (next === highlighted) return
      highlighted?.classList.remove(styles.highlight); highlighted = next
      next?.classList.add(styles.highlight)
      if (next && !scrolled) { next.scrollIntoView({ block: 'center', behavior: 'smooth' }); next.focus({ preventScroll: true }); scrolled = true }
    }
    highlight()
    const observer = new MutationObserver(highlight)
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden', 'data-guide', 'data-guide-fallback'] })
    return () => { observer.disconnect(); highlighted?.classList.remove(styles.highlight) }
  }, [target, pathname])

  function go(step: GuideStep) {
    setTarget(step); setExpanded(false)
    const match = pathname.match(/^\/projects\/([^/]+)$/)
    const projectId = match?.[1] || state?.project_id
    if (step === 'project' || !projectId) {
      setTarget('project'); router.push('/projects'); return
    }
    if (match) window.dispatchEvent(new CustomEvent('lilies:guide-navigate', { detail: step }))
    else router.push(`/projects/${encodeURIComponent(projectId)}?guide=${step}`)
  }
  const value = state || initial
  const index = Math.max(0, guideSteps.findIndex(s => s.id === value.step))
  const step = guideSteps[index]
  return <Context.Provider value={{ active: value.status === 'active', unavailable, mark, open }}>{children}
    {expanded ? <>
      <div aria-hidden="true" style={{ height }} />
      <aside ref={panel} tabIndex={-1} className={styles.panel} aria-label="使用教程" onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); close() } }}>
        <header><strong>使用教程</strong><button onClick={close} aria-label="收起教程">收起</button></header>
        {!state && !error && <p role="status">正在读取教程…</p>}
        {state && value.status !== 'active' && <>
          <h2>{value.status === 'new' ? '欢迎使用 Lilies' : value.status === 'finished' ? '已结束讲解，可随时重看' : '从项目开始使用'}</h2>
          <p>在项目中放入资料，通过对话使用已有工作流，也可以创建自己的流程。</p>
          <p>教程不会自动发送消息或运行任务。完成讲解不代表已经完成实际操作。</p>
          <div className={styles.actions}><button disabled={busy} onClick={() => update({ status: 'active' })}>{value.status === 'new' ? '开始引导' : '继续查看'}</button>
            {value.status !== 'new' && <button disabled={busy} onClick={() => update({ restart: true })}>从头查看</button>}
            <button onClick={() => { update({ status: 'skipped' }); close() }}>稍后再看</button></div>
        </>}
        {state && value.status === 'active' && <>
          <small>第 {index + 1} / {guideSteps.length} 步 · 翻页只推进讲解</small>
          <h2>{step.title}</h2><p>{step.text}</p>
          <div className={styles.actions}><button onClick={() => go(step.id)}>去操作</button>
            {step.id === 'conversation' && <button onClick={() => { setExpanded(false); const match = pathname.match(/^\/projects\/([^/]+)$/); if (match) window.dispatchEvent(new CustomEvent('lilies:guide-navigate', { detail: 'settings' })); else if (value.project_id) router.push(`/projects/${encodeURIComponent(value.project_id)}?guide=settings`); else router.push('/projects') }}>查看模型设置</button>}
          </div>
          <details><summary>操作清单 · {value.completed_steps.length} / {guideSteps.length}</summary><ul className={styles.checklist}>{guideSteps.map(s => <li key={s.id}><span aria-label={value.completed_steps.includes(s.id) ? '已操作' : '未操作'}>{value.completed_steps.includes(s.id) ? '✓' : '○'}</span> {s.action}</li>)}</ul></details>
          <div className={styles.actions}>
            <button disabled={busy || index === 0} onClick={() => { setTarget(null); update({ step: guideSteps[index - 1].id }) }}>上一步</button>
            <button disabled={busy} onClick={() => { setTarget(null); if (index < guideSteps.length - 1) update({ step: guideSteps[index + 1].id }); else update({ status: 'finished' }) }}>{index < guideSteps.length - 1 ? '下一步' : '结束讲解'}</button>
            <button onClick={() => { update({ status: 'skipped' }); close() }}>稍后继续</button>
          </div>
        </>}
        {error && <p role="alert">教程进度暂未保存或读取，正常使用不受影响。<button disabled={busy} onClick={() => pending.current ? update(pending.current) : void refresh()}>重试</button></p>}
      </aside>
    </> : (value.status === 'active' || error) && <div className={styles.collapsed}><button onClick={open}>{error ? '教程暂不可用 · 重试' : '继续教程'}</button></div>}
  </Context.Provider>
}
