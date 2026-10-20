import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import {FeedbackButton,FeedbackNavigation,FeedbackProvider} from '@/app/components/UserFeedback'
import FeedbackPage from '@/app/feedback/page'
import ResultFeedback from '@/app/components/ResultFeedback'

const state=vi.hoisted(()=>({role:'member',id:'',replace:vi.fn()}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>({id:'alice',role:state.role})}))
vi.mock('next/navigation',()=>({useRouter:()=>({replace:state.replace}),useSearchParams:()=>new URLSearchParams(state.id?{id:state.id}:{})}))
vi.mock('@/lib/platform',()=>({api:vi.fn(),idempotency:()=>crypto.randomUUID()}))
afterEach(cleanup)
beforeEach(()=>{vi.mocked(api).mockReset();state.role='member';state.id='';state.replace.mockClear();URL.createObjectURL=vi.fn(()=>'blob:preview');URL.revokeObjectURL=vi.fn()})
const source={project_id:'project',conversation_id:'conversation',request_id:'request',page:'conversation' as const}
function mount(){return render(<FeedbackProvider><textarea aria-label="原消息草稿" defaultValue="未发送的工作要求"/><FeedbackButton source={source} excerpt="原回答的内容"/></FeedbackProvider>)}
function submitted(){const [path,init]=vi.mocked(api).mock.calls.find(([p,init])=>p==='/api/v1/feedback'&&init?.method==='POST')!;const form=init!.body as FormData;return {path,form,payload:JSON.parse(form.get('payload') as string)}}

it('opens without model calls, preserves the composer and submits only chosen context',async()=>{
  vi.mocked(api).mockResolvedValue({id:'f'} as never);mount()
  fireEvent.click(screen.getByRole('button',{name:'反馈给平台'}))
  expect(api).not.toHaveBeenCalled()
  expect(screen.getByLabelText('附上这段内容（可编辑）')).not.toBeChecked()
  fireEvent.change(screen.getByLabelText('意见或问题'),{target:{value:'这里需要给出理由'}})
  fireEvent.click(screen.getByRole('button',{name:'提交反馈'}))
  expect(await screen.findByText('已收到你的反馈。后续回复会显示在反馈记录中。')).toBeInTheDocument()
  const {form,payload}=submitted()
  expect(payload).toMatchObject({text:'这里需要给出理由',source,excerpt:''})
  expect(form.has('image')).toBe(false)
  expect(screen.getByLabelText('原消息草稿')).toHaveValue('未发送的工作要求')
  expect(api).toHaveBeenCalledTimes(1)
})

it('keeps unsent feedback on escape, allows detaching source and previews a selected screenshot',async()=>{
  vi.mocked(api).mockResolvedValue({id:'f'} as never);mount()
  const trigger=screen.getByRole('button',{name:'反馈给平台'});trigger.focus();fireEvent.click(trigger)
  fireEvent.change(screen.getByLabelText('意见或问题'),{target:{value:'希望说明更简洁'}})
  fireEvent(screen.getByRole('dialog'),new Event('cancel',{bubbles:true,cancelable:true}))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();expect(trigger).toHaveFocus()
  fireEvent.click(trigger);expect(screen.getByLabelText('意见或问题')).toHaveValue('希望说明更简洁')
  fireEvent.click(screen.getByLabelText('附带当前操作的定位信息'))
  fireEvent.click(screen.getByLabelText('附上这段内容（可编辑）'))
  fireEvent.change(screen.getByLabelText('附带片段'),{target:{value:'仅这段文字'}})
  const image=new File(['image'],'selected.png',{type:'image/png'})
  fireEvent.change(screen.getByLabelText('截图（可选，最多3 MB）'),{target:{files:[image]}})
  expect(await screen.findByAltText('即将提交的截图')).toHaveAttribute('src','blob:preview')
  fireEvent.click(screen.getByRole('button',{name:'提交反馈'}));await screen.findByRole('status')
  expect(submitted().payload).toMatchObject({source:{},excerpt:'仅这段文字'})
  expect(submitted().form.get('image')).toBe(image)
})

it('keeps text on failure and retries with the same request key',async()=>{
  vi.mocked(api).mockRejectedValueOnce(new Error('网络暂时中断')).mockResolvedValueOnce({id:'f'} as never);mount()
  fireEvent.click(screen.getByRole('button',{name:'反馈给平台'}));fireEvent.change(screen.getByLabelText('意见或问题'),{target:{value:'发生错误'}})
  fireEvent.click(screen.getByRole('button',{name:'提交反馈'}));await screen.findByRole('alert')
  expect(screen.getByLabelText('意见或问题')).toHaveValue('发生错误')
  const first=submitted().payload.request_key
  fireEvent.click(screen.getByRole('button',{name:'提交反馈'}));await screen.findByRole('status')
  const body=vi.mocked(api).mock.calls[1][1]?.body as FormData
  expect(JSON.parse(body.get('payload') as string).request_key).toBe(first)
})

it('negative rating offers optional text without starting a task or submitting a ticket',async()=>{
  vi.mocked(api).mockResolvedValue({helpful:false} as never)
  render(<FeedbackProvider><ResultFeedback base="/api/v1/projects/project/conversations/conversation" requestId="request" source={source}/></FeedbackProvider>)
  fireEvent.click(screen.getByRole('button',{name:'需要修改'}))
  fireEvent.click(await screen.findByRole('button',{name:'说明哪里需要改进'}))
  expect(screen.getByRole('dialog')).toBeInTheDocument()
  expect(api).toHaveBeenCalledTimes(1)
  expect(vi.mocked(api).mock.calls[0][1]?.method).toBe('PUT')
})

const detail={id:'f',summary:'找不到文件',user_id:'alice',user_name:'Alice',category:'usability',status:'resolved',project_name:'项目',source:{project_id:'p'},source_available:false,excerpt:'所选内容',revision:2,read_revision:0,messages:[{id:'m',user_id:'admin',user_name:'管理员',role:'admin',text:'已经添加入口',status:'resolved',created_at:'2026-09-28T01:00:00Z',media_type:''}]}
function pageApi(){vi.mocked(api).mockImplementation(async(path,init)=>{
  if(path==='/api/v1/feedback/unread')return {count:1} as never
  if(path.startsWith('/api/v1/feedback?'))return {items:[detail],total:1} as never
  if(path==='/api/v1/feedback/f'&&!init)return detail as never
  return {id:'f',ok:true} as never
})}

it('shows private replies, marks only the displayed revision read and lets the author reopen',async()=>{
  state.id='f';pageApi();render(<FeedbackProvider><FeedbackPage/></FeedbackProvider>)
  expect(await screen.findByText('已经添加入口')).toBeInTheDocument()
  expect(screen.getByText('原操作已不可访问；提交的意见与回复仍保留。')).toBeInTheDocument()
  expect(screen.queryByLabelText('处理状态')).not.toBeInTheDocument()
  await waitFor(()=>expect(api).toHaveBeenCalledWith('/api/v1/feedback/f/read',{method:'POST',body:JSON.stringify({revision:2})}))
  fireEvent.change(screen.getByLabelText('回复或补充'),{target:{value:'问题仍存在'}})
  fireEvent.click(screen.getByLabelText('问题仍存在，重新打开'))
  fireEvent.click(screen.getByRole('button',{name:'发送回复'}));await screen.findByText('回复已保存')
  const call=vi.mocked(api).mock.calls.find(([p])=>p.endsWith('/messages'))!
  expect(JSON.parse((call[1]?.body as FormData).get('payload') as string)).toMatchObject({text:'问题仍存在',status:'received',expected_revision:2})
})

it('admin can filter and explain a status change; failed reply stays editable',async()=>{
  state.role='admin';state.id='f';pageApi();const impl=vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async(path,init)=>{if(path.endsWith('/messages'))throw Error('反馈已有新进展');return impl(path,init)})
  render(<FeedbackProvider><FeedbackPage/></FeedbackProvider>);await screen.findByText('已经添加入口')
  fireEvent.change(screen.getByLabelText('类型'),{target:{value:'usability'}})
  await waitFor(()=>expect(vi.mocked(api).mock.calls.some(([p])=>p.includes('category=usability')&&p.includes('scope=all'))).toBe(true))
  fireEvent.change(screen.getByLabelText('来自功能'),{target:{value:'run'}})
  await waitFor(()=>expect(vi.mocked(api).mock.calls.some(([p])=>p.includes('page=run'))).toBe(true))
  fireEvent.change(screen.getByLabelText('处理状态'),{target:{value:'declined'}})
  expect(screen.getByRole('button',{name:'发送回复'})).toBeDisabled()
  fireEvent.change(screen.getByLabelText('回复或补充'),{target:{value:'暂时需要更多复现信息'}})
  fireEvent.click(screen.getByRole('button',{name:'发送回复'}));await screen.findByRole('alert')
  expect(screen.getByLabelText('回复或补充')).toHaveValue('暂时需要更多复现信息')
})

it('navigation refreshes unread after reading and does not block use on count failure',async()=>{
  vi.mocked(api).mockResolvedValueOnce({count:2} as never).mockResolvedValueOnce({count:0} as never).mockRejectedValueOnce(Error('offline'))
  render(<FeedbackProvider><FeedbackNavigation/></FeedbackProvider>)
  expect(await screen.findByRole('link',{name:'反馈记录（2条未读）'})).toBeInTheDocument()
  window.dispatchEvent(new Event('lilies:feedback-read'))
  await screen.findByRole('link',{name:'反馈记录'})
  window.dispatchEvent(new Event('focus'))
  await waitFor(()=>expect(screen.getByRole('link',{name:'反馈记录'})).toHaveAttribute('title','未读提醒暂未更新，打开后可重试'))
  fireEvent.click(screen.getByRole('button',{name:'意见反馈'}));expect(screen.getByRole('dialog')).toBeInTheDocument()
})
