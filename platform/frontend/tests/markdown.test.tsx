import {cleanup,render,screen} from '@testing-library/react'
import {afterEach,expect,it} from 'vitest'
import {MarkdownDocument} from '@/lib/markdown'

afterEach(cleanup)

it('shows escaped sample identifiers and literal links as supplied text',()=>{
  render(<MarkdownDocument source={'产品：sample\\_one\\_v2；\\[原文名称\\](https://example.invalid)'} emptyLabel="" />)
  expect(screen.getByText('产品：sample_one_v2；[原文名称](https://example.invalid)')).toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})

it('keeps a quoted model reply containing shorter fences and links inside one code block',()=>{
  const raw='前文\n```python\nprint("示例")\n```\n[不是页面操作](https://example.invalid/action)\n# 仍是原回答'
  const {container}=render(<MarkdownDocument source={'````text\n'+raw+'\n````\n\n## 后续操作\n\n[下载原文](/file)'} emptyLabel=""/>)
  expect(container.querySelectorAll('pre')).toHaveLength(1)
  expect(container.querySelector('pre code')?.textContent).toBe(raw)
  expect(screen.queryByRole('link',{name:'不是页面操作'})).not.toBeInTheDocument()
  expect(screen.getByRole('heading',{name:'后续操作'})).toBeInTheDocument()
  expect(screen.getByRole('link',{name:'下载原文'})).toHaveAttribute('href','/file')
})

it('closes ordinary fences only on a bare fence and preserves an unclosed reply',()=>{
  const {container,rerender}=render(<MarkdownDocument source={'```text\n```not-a-close\n原文\n```\n\n正文'} emptyLabel=""/>)
  expect(container.querySelector('pre code')?.textContent).toBe('```not-a-close\n原文')
  expect(screen.getByText('正文')).toBeInTheDocument()
  rerender(<MarkdownDocument source={'```text\n未完成\n[仍为原文](/action)'} emptyLabel=""/>)
  expect(container.querySelector('pre code')?.textContent).toBe('未完成\n[仍为原文](/action)')
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})
