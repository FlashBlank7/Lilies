import {cleanup,render,screen} from '@testing-library/react'
import {afterEach,expect,it} from 'vitest'
import TaskError from '@/app/components/TaskError'
afterEach(cleanup)

it('shows the final actionable cause while retaining a collapsed complete traceback',()=>{
  const error='node prepare failed: Python 执行失败：Traceback (most recent call last):\n  File "workflow.py", line 66, in prepare\nValueError: 内部计算失败\n\nDuring handling of the above exception, another exception occurred:\n\nTraceback (most recent call last):\n  File "workflow.py", line 80\nRuntimeError: 缺少资料字段：\n- 检测日期\n- 产品批次'
  render(<TaskError error={error}/>);
  expect(screen.getByRole('alert')).toHaveTextContent('缺少资料字段： - 检测日期 - 产品批次')
  expect(screen.getByRole('alert')).not.toHaveTextContent('Traceback')
  expect(screen.getByText('错误详情').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByText(error,{exact:false,normalizer:s=>s}).tagName).toBe('PRE')
})

it('keeps a short model or input error intact and displays no duplicate detail',()=>{
  render(<TaskError error="模型“质量预测”尚未绑定，请在项目模型中绑定版本。"/>);
  expect(screen.getByRole('alert')).toHaveTextContent('模型“质量预测”尚未绑定')
  expect(screen.queryByText('错误详情')).not.toBeInTheDocument()
})

it('does not invent a cause when a traceback has no final exception',()=>{
  render(<TaskError error={'Traceback (most recent call last):\n  File "workflow.py", line 1'}/>);
  expect(screen.getByRole('alert')).toHaveTextContent('执行失败，请展开查看错误详情。')
})
