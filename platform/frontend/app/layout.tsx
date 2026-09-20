import type { Metadata } from 'next'
import { Suspense } from 'react'
import PlatformLayout from './components/PlatformLayout'
import AuthBoundary from './components/AuthBoundary'
import './globals.css'
import './theme.css'

export const metadata: Metadata = {
  title: 'Foundry — Agent Workflow Studio',
  description: 'Claude-brain workflow construction with editable bricks',
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body><Suspense fallback={<div aria-label="正在打开页面" /> }><AuthBoundary><PlatformLayout>{children}</PlatformLayout></AuthBoundary></Suspense></body>
    </html>
  )
}
