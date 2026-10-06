import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ProjectConversations from '@/app/components/ProjectConversations'
import { api } from '@/lib/platform'
import type { ConversationFocus } from '@/lib/project-progress'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
vi.mock('@/app/components/AuthBoundary', () => ({ useAccount: () => ({ id: 'user' }) }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })
beforeEach(() => { vi.mocked(api).mockReset(); sessionStorage.clear() })

const base = '/api/v1/projects/p/conversations'
const a = { id: 'a', title: '设计分析', status: 'running' }
const b = { id: 'b', title: '模型训练', status: 'idle' }
const updated = vi.fn()
function setup(rows = [a, b], onSent = vi.fn(), focus?: ConversationFocus) {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === base) return (options?.method === 'POST' ? { ...b, id: 'new', title: '新会话' } : rows) as never
    if (options?.method === 'PATCH') return { ...a, title: JSON.parse(options.body as string).title } as never
    const current = path.startsWith(base + '/a') ? a : b
    return { provider: 'api', status: current.status, revision: 1, events: options ? [] : [
      { id: current.id, kind: 'assistant', text: current.title + '的记录', time: '' }],
      has_more: false, first_cursor: current.id, last_cursor: current.id, error: '' } as never
  })
  render(<ProjectConversations id="p" items={[]} focus={focus} canConfigureModel={false} onUpdated={updated} onSent={onSent} />)
  return onSent
}

it('switches histories and unsent drafts without stopping a background conversation', async () => {
  setup()
  await screen.findByText('设计分析的记录')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '甲草稿' } })
  fireEvent.change(screen.getByLabelText('选择会话'), { target: { value: 'b' } })
  await screen.findByText('模型训练的记录')
  expect(screen.queryByText('设计分析的记录')).not.toBeInTheDocument()
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '乙草稿' } })
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/stop'))).toBe(false)
  fireEvent.change(screen.getByLabelText('选择会话'), { target: { value: 'a' } })
  await screen.findByText('设计分析的记录')
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('甲草稿')
  fireEvent.click(screen.getByRole('button', { name: '停止' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith(base + '/a/stop', { method: 'POST' }))
  expect(vi.mocked(api).mock.calls.some(([path]) => path === base + '/b/stop')).toBe(false)
})

it('creates and renames a conversation without starting a model turn', async () => {
  setup([])
  fireEvent.click(await screen.findByRole('button', { name: '新建会话' }))
  await waitFor(() => expect(screen.getByLabelText('选择会话')).toHaveValue('new'))
  expect(api).toHaveBeenCalledWith(base, { method: 'POST', body: JSON.stringify({ title: '新会话' }) })
  fireEvent.change(screen.getByLabelText('会话名称'), { target: { value: '新数据预测' } })
  fireEvent.click(screen.getByRole('button', { name: '重命名' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith(base + '/new', { method: 'PATCH', body: JSON.stringify({ title: '新数据预测' }) }))
  expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/messages'))).toBe(false)
})

it('creates the first conversation once for a prepared request without starting a model turn', async () => {
  const sent = setup([], vi.fn(), {nonce: 1, label: '项目空间', mode: 'workflow', message: '用 requirement-package/data.csv 创建分析流程'})
  await screen.findByText('模型训练的记录')
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('用 requirement-package/data.csv 创建分析流程')
  expect(screen.getByRole('button', {name: '创建工作流'})).toHaveAttribute('aria-pressed', 'true')
  expect(sent).not.toHaveBeenCalled()
  expect(vi.mocked(api).mock.calls.filter(([,options])=>options?.method==='POST').map(([path])=>path)).toEqual([base])
})

it('keeps an existing personal conversation and its draft when preparing a training result request', async () => {
  sessionStorage.setItem('lilies:user:user:project:p:conversation','b')
  sessionStorage.setItem('lilies:user:user:project:p:conversation:b:draft','先保留这条尚未发送的要求')
  setup([a,b],vi.fn(),{nonce:2,label:'业务结果',mode:'workflow',message:'请用当前最佳模型创建预测工作流'})
  await screen.findByText('模型训练的记录')
  expect(screen.getByLabelText('选择会话')).toHaveValue('b')
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('先保留这条尚未发送的要求\n\n请用当前最佳模型创建预测工作流')
  expect(screen.getByRole('button',{name:'创建工作流'})).toHaveAttribute('aria-pressed','true')
  expect(vi.mocked(api).mock.calls.some(([,options])=>options?.method==='POST')).toBe(false)
})

it('does not create a second conversation while the first preparation is still pending', async () => {
  let finish!: (value: never) => void
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path===base)return options?.method==='POST'?new Promise(resolve=>{finish=resolve}):[] as never
    return {provider:'api',status:'idle',revision:1,events:[],error:''} as never
  })
  const props={id:'p',items:[],canConfigureModel:false,onUpdated:updated,onSent:vi.fn()}
  const focus={nonce:3,label:'业务结果',mode:'workflow' as const,message:'准备预测工作流'}
  const mounted=render(<ProjectConversations {...props} focus={focus}/> )
  await waitFor(()=>expect(finish).toBeDefined())
  mounted.rerender(<ProjectConversations {...props} focus={{...focus}}/> )
  expect(screen.getByRole('button',{name:'新建会话'})).toBeDisabled()
  fireEvent.click(screen.getByRole('button',{name:'新建会话'}))
  await act(async()=>{finish({id:'new',title:'新会话',status:'idle'} as never)})
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('准备预测工作流')
  expect(vi.mocked(api).mock.calls.filter(([,options])=>options?.method==='POST')).toHaveLength(1)
})

it('a slow send in the previous conversation cannot clear the newly selected draft', async () => {
  const sent = setup()
  await screen.findByText('设计分析的记录')
  const healthy = vi.mocked(api).getMockImplementation()!
  let finish!: (value: never) => void
  vi.mocked(api).mockImplementation(async (path, options) => path === base + '/a/messages'
    ? new Promise(resolve => { finish = resolve }) : healthy(path, options))
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '发给甲' } })
  fireEvent.click(screen.getByRole('button', { name: '发送补充' }))
  await waitFor(() => expect(finish).toBeDefined())
  fireEvent.change(screen.getByLabelText('选择会话'), { target: { value: 'b' } })
  await screen.findByText('模型训练的记录')
  fireEvent.change(screen.getByLabelText('给项目统筹的消息'), { target: { value: '保留乙的输入' } })
  const count = sent.mock.calls.length
  await act(async () => { finish({} as never) })
  expect(screen.getByLabelText('给项目统筹的消息')).toHaveValue('保留乙的输入')
  expect(sent).toHaveBeenCalledTimes(count)
})
