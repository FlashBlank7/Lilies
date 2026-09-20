import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import AuthBoundary from '@/app/components/AuthBoundary'
import { api } from '@/lib/platform'

const { router } = vi.hoisted(() => ({ router: { replace: vi.fn() } }))
vi.mock('next/navigation', () => ({ useRouter: () => router, usePathname: () => '/projects', useSearchParams: () => new URLSearchParams() }))
vi.mock('@/lib/platform', () => ({ api: vi.fn(), clearClientToken: vi.fn() }))
class Channel {
  static latest: Channel
  onmessage: (() => void) | null = null
  constructor() { Channel.latest = this }
  close() {}
}
const user = (name: string) => ({ user: { id: name, name, role: 'member', status: 'active' } })
function WorkingPage() {
  const [value, setValue] = useState('')
  return <input aria-label="页面中的项目内容" value={value} onChange={event => setValue(event.target.value)} />
}
beforeEach(() => { vi.clearAllMocks(); vi.stubGlobal('BroadcastChannel', Channel); vi.mocked(api).mockResolvedValue(user('甲') as never) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('clears the previous account page when another tab changes accounts', async () => {
  render(<AuthBoundary><WorkingPage /></AuthBoundary>)
  await screen.findByRole('link', { name: '甲' })
  fireEvent.change(screen.getByLabelText('页面中的项目内容'), { target: { value: '甲的资料' } })
  vi.mocked(api).mockResolvedValue(user('乙') as never)
  act(() => { Channel.latest.onmessage?.() })
  await screen.findByRole('link', { name: '乙' })
  expect(screen.getByLabelText('页面中的项目内容')).toHaveValue('')
  expect(screen.queryByRole('link', { name: '甲' })).not.toBeInTheDocument()
})

it('does not redirect a valid new session when an older request fails', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => user('乙') }))
  render(<AuthBoundary><WorkingPage /></AuthBoundary>)
  await screen.findByRole('link', { name: '甲' })
  act(() => { window.dispatchEvent(new Event('lilies:unauthorized')) })
  await waitFor(() => expect(screen.getByRole('link', { name: '乙' })).toBeInTheDocument())
  expect(router.replace).not.toHaveBeenCalled()
})
