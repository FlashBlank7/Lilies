import {cleanup, render, screen, within} from '@testing-library/react'
import {afterEach, expect, it, vi} from 'vitest'
import {ProjectTaskOutput} from '@/app/components/ProjectRunPanel'
import type {ProjectTask} from '@/lib/project-progress'

vi.mock('@/app/components/UserFeedback',()=>({FeedbackButton:()=>null}))
afterEach(cleanup)

it('shows the outlier counts, applied bounds and zero-IQR explanation from workflow output',()=>{
  const warning='字段 pressure 的四分位距为 0；其他取值都会标记为异常。'
  render(<ProjectTaskOutput projectId="p" task={{status:'succeeded',outputs:{outliers:{operation:'outliers',rows:11,output_rows:11,
    affected_rows:2,handling:'flag',columns:['pressure'],bounds:{pressure:{lower:22,upper:22}},warnings:[warning]}}} as unknown as ProjectTask}/> )
  const section=screen.getByRole('region',{name:'异常值处理'})
  expect(within(section).getByText('输入 11 行 · 输出 11 行')).toBeInTheDocument()
  expect(within(section).getByText(/识别 2 行.*只标记/)).toBeInTheDocument()
  expect(within(section).getAllByRole('cell',{name:'22.0000'})).toHaveLength(2)
  expect(within(section).getByText(warning)).toBeInTheDocument()
})

it('shows undefined correlation with its sample count instead of replacing it with zero',()=>{
  render(<ProjectTaskOutput projectId="p" task={{status:'succeeded',outputs:{operation:'spearman',rows:5,columns:['x','y'],
    pairs:[{left:'x',right:'y',rho:null,p_value:null,samples:2,reason:'有效样本少于 3'}]}} as unknown as ProjectTask}/> )
  const section=screen.getByRole('region',{name:'Spearman 相关分析'})
  expect(within(section).getAllByRole('cell',{name:'无法计算'})).toHaveLength(2)
  expect(within(section).getByRole('cell',{name:'2'})).toBeInTheDocument()
  expect(within(section).getByText('有效样本少于 3')).toBeInTheDocument()
})

it('shows numeric conversion counts from older results without warnings',()=>{
  render(<ProjectTaskOutput projectId="p" task={{status:'succeeded',outputs:{prepared:{operation:'numeric',rows:5,output_rows:5,
    converted_cells:3,affected_rows:2}}} as unknown as ProjectTask}/> )
  expect(screen.getByText('转换 3 个单元格，涉及 2 行；原文件保留。')).toBeInTheDocument()
})
