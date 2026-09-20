'use client'

import { usePathname, useSearchParams } from 'next/navigation'
import type { ReactNode } from 'react'
import AppShell from './AppShell'

export default function PlatformLayout({ children }: { children: ReactNode }) {
  const path = usePathname()
  const params = useSearchParams()
  if (path === '/' || path.startsWith('/projects') || params.get('embedded') === '1') return <>{children}</>
  return <AppShell compact={/^\/applications\/[^/]+$/.test(path)}>{children}</AppShell>
}
