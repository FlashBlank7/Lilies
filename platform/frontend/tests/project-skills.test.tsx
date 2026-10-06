import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectSkills from '@/app/components/ProjectSkills'

vi.mock('@/lib/platform',()=>({api:vi.fn()}))
afterEach(()=>{cleanup();vi.mocked(api).mockReset()})

it('labels a legacy shared method distinctly and saves only editable fields',async()=>{
  const original={id:'original',name:'账单与费用整理使用说明',description:'整理费用',content:'原始步骤',references:{'check.py':'print(5)'},revision:1}
  let copy={...original,id:'shared',display_name:'新员工费用整理方法'}
  vi.mocked(api).mockImplementation(async(path,options)=>{
    if(options?.method==='PUT'){
      const body=JSON.parse(String(options.body))
      copy={...copy,...body,revision:2,display_name:''}
      return copy as never
    }
    if(path.endsWith('/skills/shared'))return copy as never
    if(path.endsWith('/skills'))return [original,copy] as never
    return original as never
  })
  render(<ProjectSkills projectId="p"/>)
  fireEvent.click(await screen.findByRole('button',{name:'新员工费用整理方法'}))
  expect(await screen.findByLabelText('名称')).toHaveValue(original.name)
  expect(screen.getByRole('button',{name:original.name})).toBeInTheDocument()
  expect(screen.getByLabelText('项目说明正文')).toHaveValue(original.content)
  fireEvent.change(screen.getByLabelText('名称'),{target:{value:'员工定制方法'}})
  fireEvent.change(screen.getByLabelText('项目说明正文'),{target:{value:'补充步骤'}})
  fireEvent.click(screen.getByRole('button',{name:'保存说明'}))
  await screen.findByRole('button',{name:'员工定制方法'})
  const saved=vi.mocked(api).mock.calls.find(([,options])=>options?.method==='PUT')!
  expect(saved[0]).toBe('/api/v1/projects/p/skills/shared')
  expect(JSON.parse(String(saved[1]?.body))).toEqual({name:'员工定制方法',description:'整理费用',content:'补充步骤',references:original.references,expected_revision:1})
  await waitFor(()=>expect(screen.getByRole('button',{name:original.name})).toBeInTheDocument())
})
