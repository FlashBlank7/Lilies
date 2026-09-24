import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import ExampleProjects from '@/app/components/ExampleProjects'
import ExampleProjectGuide from '@/app/components/ExampleProjectGuide'
vi.mock('@/lib/platform',()=>({api:vi.fn()}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>({id:'employee'})}))
const sample={id:'meeting',version:1,name:'会议纪要',category:'日常办公',description:'摘要和待办',question:'请整理会议记录',exercise:'改为按负责人分组',requires:['大模型连接'],featured:true,steps:['查看资料','运行流程'],files:[{name:'input.txt',path:'requirement-package/input.txt',size:123}],workflow_count:1,workflows:[{id:'w',name:'整理会议'}],manual_path:'requirement-package/使用说明.md'}
beforeEach(()=>{vi.mocked(api).mockReset();sessionStorage.clear()})
afterEach(cleanup)
it('filters examples and only creates an independent project on explicit action',async()=>{
 const created=vi.fn()
 vi.mocked(api).mockImplementation(async(path,options)=> options?{project_id:'new'} as never:[sample,{...sample,id:'regression',name:'回归',category:'机器学习'}] as never)
 render(<ExampleProjects onCreated={created}/> )
 await screen.findByRole('button',{name:'了解示例：会议纪要'})
 fireEvent.click(screen.getByRole('button',{name:'机器学习'}))
 expect(screen.queryByRole('button',{name:'了解示例：会议纪要'})).not.toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'全部'}))
 fireEvent.click(screen.getByRole('button',{name:'了解示例：会议纪要'}))
 expect(vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST')).toHaveLength(0)
 fireEvent.change(screen.getByLabelText('示例项目名称'),{target:{value:'我的练习'}})
 fireEvent.click(screen.getByRole('button',{name:'创建我的示例项目'}))
 await waitFor(()=>expect(created).toHaveBeenCalledWith('new'))
 const [path,options]=vi.mocked(api).mock.calls.find(([,o])=>o?.method==='POST')!
 expect(path).toBe('/api/v1/example-projects/meeting/instantiate')
 expect(JSON.parse(options!.body as string)).toMatchObject({name:'我的练习',request_key:expect.any(String)})
 expect(vi.mocked(api).mock.calls.some(([p])=>p.endsWith('/tasks')||p.endsWith('/messages'))).toBe(false)
})
it('retries a failed create using the same request key',async()=>{
 let attempts=0
 vi.mocked(api).mockImplementation(async(_,options)=>{if(!options)return [sample] as never;if(++attempts===1)throw new Error('连接中断');return {project_id:'new'} as never})
 render(<ExampleProjects onCreated={()=>{}}/> )
 fireEvent.click(await screen.findByRole('button',{name:'了解示例：会议纪要'}))
 fireEvent.click(screen.getByRole('button',{name:'创建我的示例项目'}))
 await screen.findByRole('alert')
 fireEvent.click(screen.getByRole('button',{name:'创建我的示例项目'}))
 await waitFor(()=>expect(attempts).toBe(2))
 const calls=vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST')
 expect(calls[0][1]!.body).toBe(calls[1][1]!.body)
})
it('prepares a message and exposes actual files, workflows and run forms without executing',async()=>{
 vi.mocked(api).mockResolvedValue(sample as never)
 const talk=vi.fn(),run=vi.fn(),file=vi.fn(),workflow=vi.fn()
 render(<ExampleProjectGuide projectId="p" onTalk={talk} onRun={run} onFile={file} onWorkflow={workflow} onSettings={()=>{}}/> )
 fireEvent.click(await screen.findByRole('button',{name:'准备这条消息'}))
 expect(talk).toHaveBeenCalledWith(expect.stringContaining(sample.question))
 fireEvent.click(screen.getByText('操作步骤、工作流与修改练习'))
 fireEvent.click(screen.getByRole('button',{name:'填写运行参数'}))
 expect(run).toHaveBeenCalledWith('w')
 fireEvent.click(screen.getByRole('button',{name:'input.txt'}))
 expect(file).toHaveBeenCalledWith('requirement-package/input.txt')
 expect(vi.mocked(api).mock.calls.every(([,o])=>!o?.method)).toBe(true)
})
