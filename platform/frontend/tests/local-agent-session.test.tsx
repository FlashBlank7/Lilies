import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import LocalAgentSession from '@/app/components/LocalAgentSession'

vi.mock('@/lib/platform', () => ({ api: vi.fn() }))
afterEach(cleanup)
beforeEach(() => { vi.mocked(api).mockReset() })
const initial = { provider: null as string | null, status: 'idle', phase: 'discuss', revision: 0, events: [], error: '',
  requirements: { status: 'not_started', revision: 0, turns: [], document: '' } }

it('selects Codex without launching it, then sends analysis only to the selected agent', async () => {
  let state = initial
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === '/api/v1/local-agents') return { agents: [{ id: 'codex', detected: true, supported: true, version: 'codex-cli 0.153.4' }] } as never
    if (options?.method === 'PUT') state = { ...initial, provider: 'codex', revision: 1 } as typeof initial
    return state as never
  })
  const mode = vi.fn(), updated = vi.fn()
  render(<LocalAgentSession applicationId="app" onModeChange={mode} onUpdated={updated} />)
  fireEvent.click(await screen.findByRole('button', { name: '使用本机 Codex' }))
  const analyze = await screen.findByRole('button', { name: '分析资料' })
  expect(api).not.toHaveBeenCalledWith(expect.stringContaining('/messages'), expect.anything())
  fireEvent.click(analyze)
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/applications/app/agent-session/messages', {
    method: 'POST', body: JSON.stringify({ message: '请先阅读需求包，说明你的理解和需要与我核对的问题。', intent: 'discuss' }),
  }))
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/requirements/messages'))).toBe(false)
})

it('document confirmation does not build; start and stop are explicit separate actions', async () => {
  let state = { ...initial, provider: 'codex', revision: 2,
    requirements: { status: 'review', revision: 1, turns: [], document: '# 需求\n原样返回输入文本。' } }
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === '/api/v1/local-agents') return { agents: [] } as never
    if (path.endsWith('/requirements/confirm')) state = { ...state, revision: 3, requirements: { ...state.requirements, status: 'confirmed' } }
    if (path.endsWith('/messages')) state = { ...state, status: 'running', phase: 'build', revision: 4 }
    if (path.endsWith('/stop')) state = { ...state, status: 'interrupted', revision: 5 }
    return state as never
  })
  render(<LocalAgentSession applicationId="app" onModeChange={vi.fn()} onUpdated={vi.fn()} />)
  expect(screen.queryByRole('button', { name: '开始搭建' })).not.toBeInTheDocument()
  fireEvent.click(await screen.findByRole('button', { name: '理解正确，确认需求文档' }))
  const start = await screen.findByRole('button', { name: '开始搭建' })
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/messages'))).toBe(false)
  fireEvent.click(start)
  fireEvent.click(await screen.findByRole('button', { name: '停止' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/applications/app/agent-session/stop', { method: 'POST' }))
  expect(await screen.findByRole('button', { name: '继续搭建' })).toBeInTheDocument()
})

it('keeps the reply after connection failure and exposes no provider fallback', async () => {
  vi.mocked(api).mockImplementation(async (path) => {
    if (path === '/api/v1/local-agents') return { agents: [] } as never
    if (path.endsWith('/messages')) throw new Error('Codex 无法连接')
    return { ...initial, provider: 'codex' } as never
  })
  render(<LocalAgentSession applicationId="app" onModeChange={vi.fn()} onUpdated={vi.fn()} />)
  fireEvent.change(await screen.findByLabelText('给 Codex 的消息'), { target: { value: '只按本项目材料分析' } })
  fireEvent.click(screen.getByRole('button', { name: '分析资料' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Codex 无法连接')
  expect(screen.getByLabelText('给 Codex 的消息')).toHaveValue('只按本项目材料分析')
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/builds') || path.endsWith('/requirements/messages'))).toBe(false)
})
