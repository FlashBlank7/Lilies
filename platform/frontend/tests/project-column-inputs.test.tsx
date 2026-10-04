import {act, cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {api} from '@/lib/platform'
import ProjectRunPanel from '@/app/components/ProjectRunPanel'

vi.mock('@/lib/platform', () => ({api: vi.fn(), withFrontendToken: (path: string) => path}))
const members = [{id:'summary', name:'汇总报告', display_name:'汇总报告 · 第二份', description:'', revision:1, purpose:'business'}]
const source = 'requirement-package/data.csv', replacement = 'requirement-package/新数据.xlsx'
const fields = [
  {name:'source_path', type:'file', default:source},
  {name:'group', label:'分组字段', type:'string', default:'device', required:true},
  {name:'value', label:'数值字段', type:'string', default:'value', required:true},
  {name:'optional_column', label:'可选字段', type:'string', default:'', required:false, column_source:'source_path'},
]
let defaults = fields
beforeEach(() => {
  defaults = fields
  vi.mocked(api).mockReset()
  vi.mocked(api).mockImplementation(async (path, options) => {
    if(path.endsWith('/readiness')) return {status:'configured', issues:[]} as never
    if(path.endsWith('/draft')) return {snapshot:{workflow:{nodes:[{type:'start',config:{inputs:defaults}}]}}} as never
    if(path.endsWith('/workspace/files')) return [{path:source}, {path:replacement}] as never
    if(path.includes('/table-columns?')) return {columns: new URLSearchParams(path.split('?')[1]).get('path')===source ? ['device','value','sample_id'] : ['产线','金额'], sheet:path.includes('xlsx') ? '当前数据' : ''} as never
    if(options?.method==='POST') return {id:'done',status:'succeeded',outputs:{markdown:'汇总完成'}} as never
    throw Error(path)
  })
})
afterEach(cleanup)

it('offers headers for legacy summary defaults, preserves edits when files change and submits chosen columns', async () => {
  await act(async () => {render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="summary"/>)})
  expect(screen.getByRole('option',{name:'汇总报告 · 第二份'})).toHaveValue('summary')
  const group = screen.getByRole('combobox',{name:'为 group 选择表格字段'})
  expect(group).toHaveValue('device')
  expect(within(group).getAllByRole('option').map(option=>option.textContent)).toEqual(['请选择字段','device','value','sample_id'])
  expect(api).toHaveBeenCalledWith(expect.stringContaining('/table-columns?'))
  expect(vi.mocked(api).mock.calls.filter(([path])=>path.includes('/table-columns?'))).toHaveLength(1)
  fireEvent.change(group,{target:{value:'sample_id'}})
  fireEvent.change(screen.getByRole('combobox',{name:'为 source_path 选择项目文件'}),{target:{value:replacement}})
  await waitFor(()=>expect(group).toHaveAttribute('aria-invalid','true'))
  expect(group).toHaveValue('sample_id')
  expect(screen.getByRole('textbox',{name:'value'})).toHaveValue('value')
  expect(screen.getAllByRole('alert')[0]).toHaveTextContent('可用字段：产线、金额')
  expect(screen.getByRole('combobox',{name:'为 optional_column 选择表格字段'})).toHaveAttribute('aria-invalid','false')
  fireEvent.click(screen.getByRole('button',{name:'启动工作流'}))
  expect(vi.mocked(api).mock.calls.some(([,options])=>options?.method==='POST')).toBe(false)
  fireEvent.change(group,{target:{value:'产线'}})
  fireEvent.change(screen.getByRole('combobox',{name:'为 value 选择表格字段'}),{target:{value:'金额'}})
  fireEvent.click(screen.getByRole('button',{name:'启动工作流'}))
  await screen.findByText('汇总完成')
  const submitted=vi.mocked(api).mock.calls.find(([,options])=>options?.method==='POST')!
  expect(JSON.parse(submitted[1]!.body as string).inputs).toEqual({source_path:replacement,group:'产线',value:'金额',optional_column:''})
})

it.each(['', 'datasets/d/files/data.csv', 'requirement-package/notes.md'])('keeps manual column input usable for %j without requesting unrelated headers', async path => {
  defaults = fields.map(field=>field.name==='source_path'?{...field,default:path}:field)
  await act(async () => {render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="summary"/>)})
  const input=screen.getByRole('textbox',{name:'group'})
  expect(input).toHaveValue('device')
  fireEvent.change(input,{target:{value:'manual_name'}})
  expect(input).toHaveValue('manual_name')
  expect(input).toHaveAttribute('aria-invalid','false')
  expect(vi.mocked(api).mock.calls.some(([path])=>path.includes('/table-columns?'))).toBe(false)
  expect(screen.getByRole('combobox',{name:'为 group 选择表格字段'})).toBeDisabled()
})

it('retains saved manual values when reading a header fails', async () => {
  const implementation=vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation(async(path,options)=>{if(path.includes('/table-columns?'))throw Error('表头为空');return implementation(path,options)})
  await act(async () => {render(<ProjectRunPanel projectId="p" members={members} initialWorkflowId="summary"/>)})
  expect(screen.getByRole('textbox',{name:'group'})).toHaveValue('device')
  expect(screen.getAllByText(/暂时无法列出字段/)).toHaveLength(3)
  expect(screen.getByRole('textbox',{name:'group'})).toHaveAttribute('aria-invalid','false')
})
