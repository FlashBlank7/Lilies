'use client'

import {useEffect, useRef, useState, type ReactNode} from 'react'
import {createPortal} from 'react-dom'
import styles from './onboarding.module.css'

type Rect = {top:number;left:number;width:number;height:number}
const visible = (el:HTMLElement) => !el.closest('[hidden]') && el.getClientRects().length > 0
const focusable = 'button:not(:disabled),a[href],input:not(:disabled):not([type="hidden"]),textarea:not(:disabled),select:not(:disabled),summary,[tabindex="0"]'

/** Four shields leave the actual control clickable, without cloning it or changing its stacking context. */
export default function GuideSpotlight({selectors,paused,children,onSkip}:{selectors:string[];paused:boolean;children:ReactNode;onSkip:()=>void}) {
  const panel=useRef<HTMLElement>(null),target=useRef<HTMLElement|null>(null)
  const [host,setHost]=useState<HTMLElement|null>(null),[rect,setRect]=useState<Rect|null>(null)
  const [viewport,setViewport]=useState({width:0,height:0}),[panelHeight,setPanelHeight]=useState(260)
  const signature=selectors.join(',')
  useEffect(()=>{
    let frame=0,previous:HTMLElement|null=null,reserved:HTMLDialogElement|null=null
    const measure=()=>{
      frame=0
      const dialog=Array.from(document.querySelectorAll<HTMLDialogElement>('dialog[open]')).filter(visible).at(-1)
      if(reserved!==dialog){reserved?.removeAttribute('data-guide-reserve');reserved=dialog||null}
      // A native modal fills most of a small viewport. Reserve a separate area
      // for the guide instead of covering the modal's form and submit button.
      dialog?.setAttribute('data-guide-reserve','true')
      setHost(dialog||document.body)
      const candidates=selectors.flatMap(selector=>Array.from(document.querySelectorAll<HTMLElement>(selector)))
      const next=paused?null:dialog||candidates.find(visible)||null
      target.current=next
      if(next!==previous){
        previous?.classList.remove(styles.highlight)
        next?.classList.add(styles.highlight)
        if(next&&!dialog)next.scrollIntoView({block:'center',behavior:'instant'})
        previous=next
      }
      const width=window.innerWidth,height=window.innerHeight
      setViewport(old=>old.width===width&&old.height===height?old:{width,height})
      const box=next?.getBoundingClientRect()
      const value=box?{top:Math.max(8,box.top-6),left:Math.max(8,box.left-6),width:Math.max(0,Math.min(width-8,box.right+6)-Math.max(8,box.left-6)),height:Math.max(0,Math.min(height-8,box.bottom+6)-Math.max(8,box.top-6))}:null
      setRect(old=>JSON.stringify(old)===JSON.stringify(value)?old:value)
      const heightOfPanel=panel.current?.getBoundingClientRect().height
      if(heightOfPanel)setPanelHeight(heightOfPanel)
    }
    const schedule=()=>{if(!frame)frame=requestAnimationFrame(measure)}
    measure()
    const observer=new MutationObserver(schedule)
    observer.observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['hidden','open','data-guide','data-guide-anchor','disabled']})
    const resize=typeof ResizeObserver!=='undefined'?new ResizeObserver(schedule):null
    resize?.observe(document.body);if(panel.current)resize?.observe(panel.current)
    window.addEventListener('resize',schedule);document.addEventListener('scroll',schedule,true)
    return()=>{observer.disconnect();resize?.disconnect();cancelAnimationFrame(frame);previous?.classList.remove(styles.highlight);reserved?.removeAttribute('data-guide-reserve');document.removeEventListener('scroll',schedule,true);window.removeEventListener('resize',schedule)}
  },[signature,paused]) // selectors are represented by their stable signature
  useEffect(()=>{
    const allowed=(node:HTMLElement)=>panel.current?.contains(node)||(!paused&&target.current?.contains(node))
    const focus=(event:FocusEvent)=>{if(event.target instanceof HTMLElement&&!allowed(event.target))panel.current?.focus({preventScroll:true})}
    const key=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){event.preventDefault();event.stopPropagation();onSkip();return}
      if(event.key!=='Tab')return
      const roots=[...(!paused&&target.current?[target.current]:[]),...(panel.current?[panel.current]:[])]
      const controls=[...new Set(roots.flatMap(root=>[...(root.matches(focusable)?[root]:[]),...Array.from(root.querySelectorAll<HTMLElement>(focusable))]))].filter(visible)
      if(!controls.length){event.preventDefault();panel.current?.focus();return}
      const current=controls.indexOf(document.activeElement as HTMLElement)
      event.preventDefault();controls[(current+(event.shiftKey?-1:1)+controls.length)%controls.length].focus()
    }
    document.addEventListener('focusin',focus);document.addEventListener('keydown',key,true)
    panel.current?.focus({preventScroll:true})
    return()=>{document.removeEventListener('focusin',focus);document.removeEventListener('keydown',key,true)}
  },[host,paused,onSkip])
  if(!host)return null
  const {width,height}=viewport, gap=14, cardWidth=Math.min(352,width-24)
  let left=Math.max(12,(width-cardWidth)/2),top=Math.max(12,height-panelHeight-12),maxHeight=Math.min(400,height*.46)
  if(host instanceof HTMLDialogElement){top=height-Math.min(panelHeight,maxHeight)-12}
  else if(rect){
    if(width-rect.left-rect.width>cardWidth+gap+12){left=rect.left+rect.width+gap;top=Math.max(12,Math.min(rect.top,height-panelHeight-12))}
    else if(rect.left>cardWidth+gap+12){left=rect.left-cardWidth-gap;top=Math.max(12,Math.min(rect.top,height-panelHeight-12))}
    else if(rect.top>panelHeight+gap+12){top=rect.top-panelHeight-gap}
    else if(height-rect.top-rect.height>panelHeight+gap+12){top=rect.top+rect.height+gap}
    else {
      const above=rect.top-gap-12,below=height-rect.top-rect.height-gap-12
      if(Math.max(above,below)>=110){
        maxHeight=Math.min(maxHeight,Math.max(above,below))
        top=above>=below?Math.max(12,rect.top-gap-Math.min(panelHeight,maxHeight)):rect.top+rect.height+gap
      } else {maxHeight=Math.min(maxHeight,height*.32);top=height-Math.min(panelHeight,maxHeight)-12}
    }
  }
  const shield=(style:React.CSSProperties,key:string)=><div key={key} className={styles.shield} style={style} aria-hidden="true" data-guide-shield="" />
  return createPortal(<div data-guide-overlay="">
    {rect?<>{shield({top:0,left:0,width:'100%',height:rect.top},'top')}{shield({top:rect.top,left:0,width:rect.left,height:rect.height},'left')}{shield({top:rect.top,left:rect.left+rect.width,right:0,height:rect.height},'right')}{shield({top:rect.top+rect.height,left:0,right:0,bottom:0},'bottom')}<div className={styles.ring} aria-hidden="true" style={rect}/></>:shield({inset:0},'all')}
    <aside ref={panel} tabIndex={-1} className={styles.panel} aria-label="使用教程" style={{left,top,width:cardWidth,maxHeight}}>{children}</aside>
  </div>,host)
}
