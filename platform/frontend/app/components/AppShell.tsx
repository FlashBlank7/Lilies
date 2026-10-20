'use client'

import Link from 'next/link'
import { useState, type ReactNode } from 'react'
import { FolderOpen, PanelLeftClose, PanelLeftOpen, Wrench, Layers } from 'lucide-react'
import styles from './shell.module.css'
import { useAccount } from './AuthBoundary'

export default function AppShell({ children, navigation, projectName, compact = false }: {
  children: ReactNode; navigation?: ReactNode; projectName?: string; compact?: boolean
}) {
  const [collapsed, setCollapsed] = useState(compact)
  const account = useAccount()
  return <div className={styles.shell} data-navigation={collapsed ? 'collapsed' : 'expanded'}>
    <aside className={styles.sidebar} aria-label="平台导航">
      <Link className={styles.brand} href="/projects" aria-label="Foundry · 打开项目"><span className={styles.mark}>F</span><span className={styles.wordmark}>Foundry</span></Link>
      <Link className={styles.allProjects} href="/projects" title="所有项目"><FolderOpen size={19} /><span>所有项目</span></Link>
      {projectName && <div className={styles.projectName} title={projectName}>{projectName}</div>}
      <div className={styles.navigation}>{navigation || <Link href="/projects" title="打开项目"><Layers size={18} /><span>打开项目</span></Link>}</div>
      <div className={styles.sidebarBottom}>
        <button title={collapsed ? '展开导航' : '收起导航'} aria-label={collapsed ? '展开导航' : '收起导航'} aria-expanded={!collapsed} onClick={() => setCollapsed(!collapsed)}>
          {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}<span>收起导航</span>
        </button>
        {account?.role === 'admin' && <Link href="/applications" title="历史工作流与开发工具"><Wrench size={18} /><span>历史工作流与开发工具</span></Link>}
      </div>
    </aside>
    <div className={styles.content}>{children}</div>
  </div>
}
