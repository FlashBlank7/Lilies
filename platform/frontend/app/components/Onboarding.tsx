'use client'

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import { api } from '@/lib/platform'
import styles from './onboarding.module.css'
import GuideSpotlight from './GuideSpotlight'

export const guideSteps = [
  { id: 'project', title: '进入项目', text: '项目集中保存资料、工作流、模型和结果。成员共享资源，各自对话独立。没有公司项目时，请让负责人在「设置 → 项目成员」添加你的用户名；也可以点击「从示例创建」，获得资料齐全、可编辑的练习项目。', action: '已打开项目' },
  { id: 'materials', title: '准备资料', text: '在亮起的区域选择已有文件，或点击「添加资料」。完成后会继续介绍工作流。暂时没有资料，也可以先了解下一步。', action: '已选择或添加资料' },
  { id: 'workflow', title: '找到工作流', text: '点击「查看与编辑」了解已有流程，也可以从官方目录加入流程。进入画布后引导会保留；看完点击下一步，准备通过对话调用。', action: '已查看或加入工作流' },
  { id: 'conversation', title: '通过对话调用', text: '若尚无会话，先在对话页点击「新建会话」。在项目空间点击「让智能体调用」，检查带入的流程和资料，再亲自点击发送。模型未连接时，负责人可在设置中配置，普通成员需联系负责人；无需配置也能继续阅读教程。', action: '已发送任务请求' },
  { id: 'results', title: '查看运行与结果', text: '通过对话结果卡或运行记录查看状态、停止运行和检查错误。打开结果文件可预览并下载原文件。尚无运行时先了解入口，不会生成虚假结果。', action: '已打开运行详情' },
  { id: 'next', title: '了解后续用法', text: '在对话页新建或打开会话后，「完成任务」让智能体直接处理问题；「创建工作流」随时生成可复用流程，不要求任务或训练已完成。工作流画布支持表单编辑，之后可再次调用。', action: '已切换对话用途' },
] as const
export type GuideStep = typeof guideSteps[number]['id']
type State = { status: 'new' | 'active' | 'skipped' | 'finished'; step: GuideStep; completed_steps: GuideStep[]; project_id: string | null }
type Patch = Partial<State> & { restart?: boolean }
type GuideContext = { active: boolean; unavailable: () => void; mark: (step: GuideStep, projectId: string, nextStep?: GuideStep) => void; open: () => void }
const Context = createContext<GuideContext>({ active: false, unavailable: () => {}, mark: () => {}, open: () => {} })
export const useOnboarding = () => useContext(Context)
export function TutorialLauncher() { const { open } = useOnboarding(); return <button onClick={open}>使用教程</button> }
const endpoint = '/api/v1/me/onboarding'
const initial: State = { status: 'skipped', step: 'project', completed_steps: [], project_id: null }

export default function Onboarding({ children }: { children: ReactNode }) {
  const router = useRouter(), pathname = usePathname()
  const [state, setState] = useState<State | null>(null)
  const current = useRef<State | null>(null)
  const [expanded, setExpanded] = useState(false), [confirmSkip, setConfirmSkip] = useState(false)
  const [error, setError] = useState('')
  const opener = useRef<HTMLElement | null>(null), alive = useRef(true), keepPage = useRef(false)
  const queue = useRef(Promise.resolve()), pending = useRef<Patch | null>(null), revision = useRef(0)
  const saveState = useCallback((value: State) => { current.current = value; setState(value) }, [])
  const update = useCallback((patch: Patch) => {
    const previous=current.current||initial
    const next=patch.restart?{...initial,status:'active' as const}:{...previous,...patch,completed_steps:[...new Set([...previous.completed_steps,...(patch.completed_steps||[])])]}
    saveState(next)
    const version=++revision.current
    queue.current=queue.current.then(async()=>{
      if(!alive.current)return
      // Save the full local progress so a failed earlier request cannot discard a later action.
      const payload={...(pending.current||{}),...next,...(patch.restart?{restart:true}:{})}
      pending.current=payload
      try{
        const result=await api<State>(endpoint,{method:'PATCH',body:JSON.stringify(payload)})
        if(!alive.current)return
        pending.current=null
        if(version===revision.current){saveState(result);setError('')}
      }catch(cause){if(alive.current)setError(String(cause))}
    })
  },[saveState])
  const refresh=useCallback(async()=>{
    const version=revision.current
    try{
      const result=await api<State>(endpoint)
      if(!alive.current||version!==revision.current)return
      saveState(result);setError('')
      if(result.status==='new'){setExpanded(true);update({status:'active'})}
      else if(result.status==='active')setExpanded(true)
    }catch(cause){if(alive.current)setError(String(cause))}
  },[saveState,update])
  useEffect(()=>{alive.current=true;void refresh();return()=>{alive.current=false}},[refresh])
  const mark=useCallback((step:GuideStep,projectId:string,nextStep?:GuideStep)=>{
    const value=current.current
    if(value?.status!=='active')return
    if(value.completed_steps.includes(step)&&value.project_id===projectId&&!nextStep)return
    if(nextStep&&nextStep!==value.step)keepPage.current=true
    const auto=['project','materials','conversation'].includes(step)&&value.step===step
    const index=guideSteps.findIndex(s=>s.id===step)
    update({completed_steps:[step],project_id:projectId,...(nextStep?{step:nextStep}:auto?{step:guideSteps[index+1].id}:{})})
  },[update])
  const unavailable=useCallback(()=>{
    if(current.current?.status!=='active')return
    update({project_id:null,step:'project'});setExpanded(true);router.push('/projects')
  },[router,update])
  const open=useCallback(()=>{
    opener.current=document.activeElement as HTMLElement
    setConfirmSkip(false);setExpanded(true)
    if(current.current?.status!=='active')update({restart:true})
  },[update])
  const askSkip=useCallback(()=>setConfirmSkip(true),[])
  function close(){setExpanded(false);setConfirmSkip(false);requestAnimationFrame(()=>opener.current?.focus())}
  const value=state||initial
  const index=Math.max(0,guideSteps.findIndex(s=>s.id===value.step)), step=guideSteps[index]
  // Only step changes navigate. A user's click can open a canvas, dialogue or result
  // within that step without being immediately sent back to its starting page.
  useEffect(()=>{
    if(!expanded||value.status!=='active')return
    if(keepPage.current){keepPage.current=false;return}
    const match=pathname.match(/^\/projects\/([^/]+)$/),projectId=match?.[1]||value.project_id
    if(step.id==='project'){
      if(!match&&pathname!=='/projects')router.push('/projects')
      return
    }
    if(!projectId){if(pathname!=='/projects')router.push('/projects');return}
    if(match)window.dispatchEvent(new CustomEvent('lilies:guide-navigate',{detail:step.id}))
    else router.push(`/projects/${encodeURIComponent(projectId)}?guide=${step.id}`)
  },[expanded,step.id,value.status,pathname]) // Preserve navigation within a step.
  const selectors:Record<GuideStep,string[]>={
    project:['[data-guide-anchor="project-entry"]','[data-guide-anchor="project-open"]','[data-guide-anchor="project-create"]','[data-guide="project"]'],
    materials:['[data-guide="materials"]'],
    workflow:['[data-guide-anchor="workflow-canvas"]','[data-guide-anchor="workflow-view"]','[data-guide-anchor="workflow-install"]','[data-guide="workflow"]'],
    conversation:['[data-guide-anchor="conversation-compose"]','[data-guide-anchor="conversation-new"]','[data-guide-anchor="workflow-call"]','[data-guide="conversation"]'],
    results:['[data-guide="results"]'],
    next:['[data-guide="next"]','[data-guide-anchor="conversation-new"]'],
  }
  function next(){
    if(index===guideSteps.length-1){update({status:'finished'});close();return}
    update({step:guideSteps[index+1].id})
  }
  return <Context.Provider value={{active:value.status==='active',unavailable,mark,open}}>{children}
    {expanded&&<><div aria-hidden="true" style={{height:300}}/><GuideSpotlight selectors={selectors[step.id]} paused={confirmSkip} onSkip={askSkip}>
      {confirmSkip?<><h2>先跳过引导吗？</h2><p>跳过后，寻找资料、调用流程或查看结果时可能会不太顺手。建议花一点时间熟悉入口；也可以随时从顶部「使用教程」重新开始。</p><div className={styles.actions}><button onClick={()=>setConfirmSkip(false)}>继续引导</button><button onClick={()=>{update({status:'skipped'});close()}}>暂时跳过</button></div></>:<>
        <header><small>第 {index+1} / {guideSteps.length} 步</small><button onClick={askSkip}>跳过引导</button></header>
        <h2>{step.title}</h2><p>{step.text}</p>
        <p className={styles.hint}>请操作亮起的区域，引导会随页面继续。教程不会自动发送消息或启动任务。</p>
        <div className={styles.actions}><button disabled={index===0} onClick={()=>update({step:guideSteps[index-1].id})}>上一步</button><button onClick={next}>{index===guideSteps.length-1?'完成引导':'下一步'}</button></div>
        <small>暂时没有资料、流程或结果，也可以先了解下一步；不会记为已完成操作。</small>
        <details><summary>已完成操作 · {value.completed_steps.length} / {guideSteps.length}</summary><ul className={styles.checklist}>{guideSteps.map(s=><li key={s.id}>{value.completed_steps.includes(s.id)?'✓':'○'} {s.action}</li>)}</ul></details>
      </>}
      {error&&<p role="alert">教程进度暂未保存或读取。可以继续引导，或跳过后正常使用。<button onClick={()=>pending.current?update(pending.current):void refresh()}>重试</button></p>}
    </GuideSpotlight></>}
    {!expanded&&error&&<div className={styles.collapsed}><button onClick={open}>教程暂不可用 · 重试</button></div>}
  </Context.Provider>
}
