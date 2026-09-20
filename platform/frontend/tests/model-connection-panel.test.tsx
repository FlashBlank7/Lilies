import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import ModelConnectionPanel from '@/app/components/ModelConnectionPanel'
import { api } from '@/lib/platform'

vi.mock('@/lib/platform', () => ({ api: vi.fn() }))
const mockApi = vi.mocked(api)
beforeEach(() => { mockApi.mockReset() })

it('copies a project main connection into an independent vision connection without exposing a key', async () => {
  mockApi.mockImplementation(async (path) => path === '/api/v1/projects'
    ? [{ id: 'source', name: 'Existing project' }, { id: 'target', name: 'Current project' }]
    : { provider: null })
  const saved = vi.fn().mockResolvedValue(undefined)
  render(<ModelConnectionPanel base="/api/v1/projects/target" connected={false} running={false} role="vision" onSaved={saved} />)
  fireEvent.click(screen.getByRole('button', { name: '视觉模型设置' }))
  await screen.findByText('沿用已有项目连接')
  fireEvent.click(screen.getByRole('button', { name: '选择已有项目' }))
  await screen.findByLabelText('来源项目')
  fireEvent.change(screen.getByLabelText('来源项目'), { target: { value: 'source' } })
  expect(screen.getByLabelText('来源模型用途')).toHaveValue('main')
  fireEvent.click(screen.getByRole('button', { name: '使用此连接' }))
  await waitFor(() => expect(saved).toHaveBeenCalledOnce())
  const copy = mockApi.mock.calls.find(([path]) => path.endsWith('/model-connection/copy'))!
  expect(JSON.parse(copy[1]!.body as string)).toEqual({ source_project_id: 'source', source_role: 'main', role: 'vision' })
  expect(mockApi.mock.calls.some(([path, options]) => path.endsWith('/agent-session') && options?.method === 'PUT')).toBe(false)
})

it('configures an API model, thinking and workflow access without persisting key in the form', async () => {
  mockApi.mockResolvedValue({})
  const saved = vi.fn().mockResolvedValue(undefined)
  const { rerender } = render(<ModelConnectionPanel base="/project" connected={false} running={false} onSaved={saved} />)
  fireEvent.click(screen.getByRole('button', { name: '连接模型' }))
  await screen.findByLabelText('API 地址')
  fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'https://api.example.test/v1' } })
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'private-key' } })
  fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'review-model' } })
  fireEvent.change(screen.getByLabelText('思考模式'), { target: { value: 'high' } })
  fireEvent.click(screen.getByRole('button', { name: '保存模型连接' }))
  await waitFor(() => expect(saved).toHaveBeenCalledOnce())
  expect(JSON.parse(mockApi.mock.calls.find(([,options])=>options?.method==='PUT')![1]!.body as string)).toMatchObject({ provider: 'api', api_key: 'private-key', model: 'review-model', thinking: 'high', runtime_enabled: true })
  rerender(<ModelConnectionPanel base="/project" connected={true} running={false} onSaved={saved} />)
  expect(screen.queryByDisplayValue('private-key')).not.toBeInTheDocument()
})

it('loads existing settings and keeps the stored key when only thinking changes', async () => {
  mockApi.mockResolvedValue({ provider: 'api', model: 'review-model', base_url: 'https://api.example.test/v1', protocol: 'openai', thinking: 'low', has_api_key: true, runtime_enabled: true })
  render(<ModelConnectionPanel base="/project" connected running={false} onSaved={vi.fn().mockResolvedValue(undefined)} />)
  fireEvent.click(screen.getByRole('button', { name: '模型设置' }))
  await screen.findByLabelText('思考模式')
  expect(screen.getByLabelText('API Key')).toHaveValue('')
  fireEvent.change(screen.getByLabelText('思考模式'), { target: { value: 'high' } })
  fireEvent.click(screen.getByRole('button', { name: '保存模型连接' }))
  await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(2))
  const body = JSON.parse(mockApi.mock.calls[1][1]!.body as string)
  expect(body.thinking).toBe('high')
  expect(body).not.toHaveProperty('api_key')
  expect(body).not.toHaveProperty('has_api_key')
})

it('replaces an old external-agent connection with a new API configuration', async () => {
  mockApi.mockResolvedValue({ provider: 'codex', model: 'old-agent-model', executable: '/old/codex' })
  render(<ModelConnectionPanel base="/project" connected running={false} onSaved={vi.fn().mockResolvedValue(undefined)} />)
  fireEvent.click(screen.getByRole('button', { name: '模型设置' }))
  await screen.findByLabelText('API 地址')
  expect(screen.getByLabelText('模型')).toHaveValue('')
  expect(screen.queryByDisplayValue('/old/codex')).not.toBeInTheDocument()
  await screen.findByLabelText('API 地址')
  fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'https://api.example.test/v1' } })
  fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'model' } })
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'new-key' } })
  fireEvent.click(screen.getByRole('button', { name: '保存模型连接' }))
  await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(2))
  const body = JSON.parse(mockApi.mock.calls[1][1]!.body as string)
  expect(body.provider).toBe('api')
  expect(body).not.toHaveProperty('executable')
})

it('supports Anthropic thinking switches and prevents changes while a turn runs', async () => {
  mockApi.mockResolvedValue({})
  const { rerender } = render(<ModelConnectionPanel base="/project" connected={false} running={false} onSaved={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '连接模型' }))
  await screen.findByLabelText('API 协议')
  fireEvent.change(screen.getByLabelText('API 协议'), { target: { value: 'anthropic' } })
  expect(screen.getByRole('option', { name: '开启思考' })).toBeInTheDocument()
  expect(screen.queryByLabelText('Codex 路径')).not.toBeInTheDocument()
  rerender(<ModelConnectionPanel base="/project" connected running onSaved={vi.fn()} />)
  expect(screen.getByRole('button', { name: '保存模型连接' })).toBeDisabled()
})
