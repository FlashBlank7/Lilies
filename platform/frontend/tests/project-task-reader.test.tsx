import { Suspense, type ReactNode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectPage from '@/app/projects/[id]/page'
import { api } from '@/lib/platform'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
const navigation = vi.hoisted(() => ({ query: '' }))
vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams(navigation.query) }))
vi.mock('@/app/components/AppShell', () => ({ default: ({ children, navigation }: { children: ReactNode; navigation: ReactNode }) => <>{navigation}{children}</> }))
vi.mock('@/app/components/ProjectConversation', () => ({ default: () => null }))
vi.mock('@/app/projects/[id]/DeveloperTools', () => ({ default: () => null }))
afterEach(() => { cleanup(); vi.mocked(api).mockReset(); navigation.query = '' })

function projectReads(readTask: (id: string) => Promise<unknown>) {
  vi.mocked(api).mockImplementation(async path => {
    if (path.includes('/tasks/')) return await readTask(path.split('/').at(-1)!) as never
    if (path.endsWith('/example')) return null as never
    if (path.endsWith('/conversations') || path.includes('/tasks?') || path.endsWith('/workspace/files')) return [] as never
    if (path.endsWith('/progress')) return { revision: 0, value: { goal: '', summary: '', items: [] } } as never
    return { id: 'p', name: '项目', members: [] } as never
  })
}
function completedTask(id: string) {
  return { id, request_key: id, status: 'succeeded', mode: 'workflow', workflow_id: 'member', purpose: 'build_test', item_id: '',
    created_at: '2026-09-28T00:00:00Z', presentation: { markdown: '保存的结果 ' + id }, inputs: {}, outputs: { quantity: 4 }, runs: [] }
}

it('opens the exact linked task on first load and query changes without starting work', async () => {
  navigation.query = 'task=historical'
  projectReads(async id => completedTask(id))
  const params = Promise.resolve({ id: 'p' })
  const page = await act(async () => render(<Suspense><ProjectPage params={params} /></Suspense>))
  expect(await screen.findByText('保存的结果 historical')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '关闭阅读窗口' }))
  navigation.query = 'task=another'
  await act(async () => page.rerender(<Suspense><ProjectPage params={params} /></Suspense>))
  expect(await screen.findByText('保存的结果 another')).toBeInTheDocument()
  expect(screen.queryByText('保存的结果 historical')).not.toBeInTheDocument()
  expect(api).toHaveBeenCalledWith('/api/v1/projects/p/tasks/historical')
  expect(api).toHaveBeenCalledWith('/api/v1/projects/p/tasks/another')
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options)).toBe(true)
})

it('keeps a missing or unauthorized result error visible after the project finishes loading', async () => {
  navigation.query = 'task=unavailable'
  projectReads(async () => { throw new Error('任务不存在或无权访问') })
  await act(async () => render(<Suspense><ProjectPage params={Promise.resolve({ id: 'p' })} /></Suspense>))
  expect(await screen.findByRole('alert')).toHaveTextContent('无法打开这次运行结果：Error: 任务不存在或无权访问')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(vi.mocked(api).mock.calls.every(([, options]) => !options)).toBe(true)
})

it('ignores a previous linked task that returns after navigating to another result', async () => {
  let finishOld!: (value: unknown) => void
  navigation.query = 'task=old'
  projectReads(async id => id === 'old' ? await new Promise(resolve => { finishOld = resolve }) : completedTask(id))
  const params = Promise.resolve({ id: 'p' })
  const page = await act(async () => render(<Suspense><ProjectPage params={params} /></Suspense>))
  navigation.query = 'task=current'
  await act(async () => page.rerender(<Suspense><ProjectPage params={params} /></Suspense>))
  expect(await screen.findByText('保存的结果 current')).toBeInTheDocument()
  await act(async () => finishOld(completedTask('old')))
  expect(screen.queryByText('保存的结果 old')).not.toBeInTheDocument()
  expect(screen.getByText('保存的结果 current')).toBeInTheDocument()
})

it.each(['running','waiting_input'])('reopens and stops a persisted %s run, then returns to its member input form without starting an agent', async status => {
  let task = { id: 't', request_key: 'request', status, mode: 'workflow', workflow_id: 'member', purpose: 'customer_trial', item_id: '', created_at: '2026-09-17T00:00:00Z', presentation: {}, inputs: {}, outputs: {},
    runs:status==='waiting_input'?[{id:'r',status:'paused',waiting_input:{node_id:'review.1.ask',title:'复核图片',context:'请补充你的判断',fields:[{name:'label',label:'结论',type:'string'}]}}]:[] }
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/example')) return null as never
    if (path.endsWith('/conversations')) return [] as never
    if (path.endsWith('/stop')) {task={ ...task, status: 'interrupted' };return task as never}
    if (path.endsWith('/tasks/t')) return task as never
    if (path.includes('/tasks?purpose=customer_trial')) return [task] as never
    if (path.includes('/tasks?compact=true') || path.endsWith('/workspace/files')) return [] as never
    if (path.endsWith('/progress')) return { revision: 0, value: { goal: '', summary: '', items: [] } } as never
    if (path.endsWith('/readiness')) return {status:'configured',issues:[],note:'运行时检查'} as never
    if (path.endsWith('/draft')) return { snapshot: { workflow: { nodes: [] } } } as never
    if (options) throw new Error(path)
    return { id: 'p', name: 'Review project', members: [{ id: 'member', name: '设计评审', revision: 1, purpose: 'business' }] } as never
  })
  const params = Promise.resolve({ id: 'p' })
  await act(async () => { render(<Suspense><ProjectPage params={params} /></Suspense>) })
  fireEvent.click(await screen.findByRole('button', { name: status==='waiting_input'?'设计评审 · 等待补充':'设计评审 · 处理中' }))
  if(status==='waiting_input')expect(screen.queryByRole('button',{name:'继续原运行'})).not.toBeInTheDocument()
  fireEvent.click(await screen.findByRole('button', { name: '停止运行' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/projects/p/tasks/t/stop', { method: 'POST' }))
  fireEvent.click(await screen.findByRole('button', { name: '再次运行此工作流' }))
  expect(await screen.findByRole('combobox', { name: '入口工作流' })).toHaveValue('member')
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('agent-session/messages'))).toBe(false)
})

it('shows actual workflows and an older waiting run without legacy progress items, then removes a stopped run',async()=>{
  let waiting={id:'old',request_key:'r',status:'waiting_input',mode:'workflow',workflow_id:'member',purpose:'customer_trial',item_id:'',created_at:'2026-09-17T00:00:00Z',presentation:{},inputs:{},outputs:{},runs:[{id:'run',status:'paused',waiting_input:{node_id:'ask',title:'这列标签表示什么？',fields:[{name:'answer',label:'标签含义',type:'string'}]}}]}
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path.endsWith('/example'))return null as never
    if(path.endsWith('/conversations')||path.endsWith('/workspace/files'))return [] as never
    if(path.includes('/tasks?compact=true&status=waiting_input'))return waiting.status==='waiting_input'?[waiting] as never:[] as never
    if(path.includes('/tasks?'))return [] as never
    if(path.endsWith('/tasks/old/stop')){waiting={...waiting,status:'interrupted'};return waiting as never}
    if(path.endsWith('/tasks/old'))return waiting as never
    if(path.endsWith('/progress'))return {revision:0,value:{goal:'',summary:'',items:[]}} as never
    if(path.endsWith('/readiness'))return {status:'configured',issues:[],note:'运行时检查'} as never
    if(path.endsWith('/draft'))return {snapshot:{workflow:{nodes:[]}}} as never
    if(options)throw new Error(path)
    return {id:'p',name:'已有流程的项目',members:[{id:'p',name:'主流程',purpose:'business'},{id:'member',name:'数据分析',purpose:'business'},{id:'helper',name:'旧开发工具',purpose:'development'}]} as never
  })
  await act(async()=>{render(<Suspense><ProjectPage params={Promise.resolve({id:'p'})}/></Suspense>)})
  const summary=await screen.findByRole('region',{name:'当前项目进展'})
  expect(summary).toHaveTextContent('2条项目工作流')
  expect(summary).toHaveTextContent('1次运行等待补充')
  expect(screen.queryByText('项能力可以试用')).not.toBeInTheDocument()
  fireEvent.click(within(screen.getByRole('region',{name:'等待补充的运行'})).getByRole('button',{name:'补充：数据分析'}))
  expect(await screen.findByRole('textbox',{name:'标签含义'})).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'停止运行'}))
  await waitFor(()=>expect(summary).toHaveTextContent('0次运行等待补充'))
  expect(screen.queryByRole('region',{name:'等待补充的运行'})).not.toBeInTheDocument()
  expect(vi.mocked(api).mock.calls.some(([path])=>path.includes('agent-session/messages'))).toBe(false)
})
