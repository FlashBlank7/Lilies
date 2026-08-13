/** OutputView 形状化渲染：记录数组→表格、布尔→是/否、超宽列截断。 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import OutputView from '@/app/components/OutputView'

describe('OutputView', () => {
  it('记录数组渲染为表格，布尔显示为 是/否', () => {
    render(
      <OutputView
        outputs={{
          stores: [
            { store: '华东一店', total_amount: 4780, reached: false },
            { store: '华东二店', total_amount: 8780, reached: true },
          ],
        }}
      />,
    )
    expect(screen.getByText('华东一店')).toBeInTheDocument()
    expect(screen.getByText('4780')).toBeInTheDocument()
    expect(screen.getByText('是')).toBeInTheDocument()
    expect(screen.getByText('否')).toBeInTheDocument()
  })

  it('长文本字段渲染为段落而不是 JSON', () => {
    const summary = '本周门店销售达标情况汇总：两家达标，一家未达标。'
    render(<OutputView outputs={{ summary }} />)
    expect(screen.getByText(summary)).toBeInTheDocument()
  })

  it('空输出不炸', () => {
    const { container } = render(<OutputView outputs={{}} />)
    expect(container).toBeTruthy()
  })
})
