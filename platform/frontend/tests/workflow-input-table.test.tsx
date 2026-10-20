import {act,cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import ProjectRunPanel from '@/app/components/ProjectRunPanel'
import {api} from '@/lib/platform'

vi.mock('@/lib/platform',()=>({api:vi.fn(),withFrontendToken:(v:string)=>v}))
afterEach(()=>{cleanup();vi.mocked(api).mockReset()})

it('selects project files inside rows, keeps metadata and missing selections, and waits for explicit run',async()=>{
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path.endsWith('/draft'))return {snapshot:{workflow:{nodes:[{type:'start',config:{inputs:[{name:'sources',label:'材料',type:'array',default:[{path:'requirement-package/missing.txt',note:'保留说明'}],columns:[{name:'path',label:'文件',type:'file'},{name:'note',label:'说明'}]}]}}]}}} as never
    if(path.endsWith('/workspace/files'))return [{path:'requirement-package/first.txt'},{path:'requirement-package/second.txt'}] as never
    if(options?.method==='POST')return {id:'done',status:'succeeded',outputs:{markdown:'整理完成'}} as never
    throw new Error(path)
  })
  await act(async()=>{render(<ProjectRunPanel projectId="p" members={[{id:'p',name:'比较',revision:1,description:'',purpose:'business'}]}/>)})
  expect(screen.getByRole('combobox',{name:'材料第1行文件'})).toHaveTextContent('当前文件列表中未找到')
  fireEvent.change(screen.getByRole('combobox',{name:'材料第1行文件'}),{target:{value:'requirement-package/first.txt'}})
  fireEvent.click(screen.getByRole('button',{name:'材料添加一行'}))
  fireEvent.change(screen.getByRole('combobox',{name:'材料第2行文件'}),{target:{value:'requirement-package/second.txt'}})
  expect(screen.getByRole('textbox',{name:'材料第1行说明'})).toHaveValue('保留说明')
  expect(vi.mocked(api).mock.calls.some(([,options])=>options?.method==='POST')).toBe(false)
  fireEvent.click(screen.getByRole('button',{name:'启动工作流'}));await screen.findByText('整理完成')
  const request=vi.mocked(api).mock.calls.find(([,options])=>options?.method==='POST')!
  expect(JSON.parse(request[1]!.body as string).inputs.sources).toEqual([{path:'requirement-package/first.txt',note:'保留说明'},{path:'requirement-package/second.txt',note:''}])
})

it('edits, reorders and deletes table rows, preserving other values until explicit run',async()=>{
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(path.endsWith('/draft'))return {snapshot:{workflow:{nodes:[{type:'start',config:{inputs:[{name:'rules',label:'条件',type:'array',default:[{field:'cost',op:'不大于',value:10,extra:'keep'}],columns:[{name:'field',label:'字段'},{name:'op',label:'关系',options:['不大于','不小于'],default:'不大于'},{name:'value',label:'数值',type:'number',default:1}]}]}}]}}} as never
    if(path.endsWith('/workspace/files'))return [] as never
    if(options?.method==='POST')return {id:'t',status:'succeeded',outputs:{markdown:'比较完成'}} as never
    throw new Error(path)
  })
  await act(async()=>{render(<ProjectRunPanel projectId="p" members={[{id:'p',name:'比较',revision:1,description:'',purpose:'business'}]}/>)})
  fireEvent.change(screen.getByRole('spinbutton',{name:'条件第1行数值'}),{target:{value:'0'}})
  fireEvent.click(screen.getByRole('button',{name:'条件添加一行'}))
  fireEvent.change(screen.getByRole('textbox',{name:'条件第2行字段'}),{target:{value:'quality'}})
  fireEvent.change(screen.getByRole('combobox',{name:'条件第2行关系'}),{target:{value:'不小于'}})
  fireEvent.click(screen.getByRole('button',{name:'条件第2行上移'}))
  expect(screen.getByRole('textbox',{name:'条件第1行字段'})).toHaveValue('quality')
  expect(screen.getByRole('spinbutton',{name:'条件第2行数值'})).toHaveValue(0)
  expect(vi.mocked(api).mock.calls.some(([,o])=>o?.method==='POST')).toBe(false)
  fireEvent.click(screen.getByRole('button',{name:'启动工作流'}));await screen.findByText('比较完成')
  const call=vi.mocked(api).mock.calls.find(([,o])=>o?.method==='POST')!
  expect(JSON.parse(call[1]!.body as string).inputs.rules).toEqual([{field:'quality',op:'不小于',value:1},{field:'cost',op:'不大于',value:0,extra:'keep'}])
  fireEvent.click(screen.getByRole('button',{name:'条件删除第2行'}));fireEvent.click(screen.getByRole('button',{name:'条件删除第1行'}))
  fireEvent.click(screen.getByRole('button',{name:'启动工作流'}))
  await act(async()=>{})
  const last=vi.mocked(api).mock.calls.filter(([,o])=>o?.method==='POST').at(-1)!
  expect(JSON.parse(last[1]!.body as string).inputs.rules).toEqual([])
})
