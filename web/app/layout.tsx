import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'Foundry — Agent Workflow Studio',
  description: 'Claude-brain workflow construction with editable bricks',
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  )
}
