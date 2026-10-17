import {cleanup,render,screen} from '@testing-library/react'
import {afterEach,expect,it} from 'vitest'
import {MarkdownDocument} from '@/lib/markdown'
import {resolveProjectFileLink,resolveProjectLink} from '@/lib/project-links'

afterEach(cleanup)

it('resolves Codex workspace downloads within the current project in replies and reports',()=>{
  const project='p',path='results/模型包.zip'
  for(const prefix of ['sandbox:/workspace/','/workspace/']){
    const source=`[模型下载](${prefix}${path})`
    const {rerender,unmount}=render(<MarkdownDocument source={source} emptyLabel="" resolveLink={href=>resolveProjectLink(project,href)} />)
    const expected=resolveProjectLink(project,path)
    expect(screen.getByRole('link',{name:'模型下载'})).toHaveAttribute('href',expected)
    rerender(<MarkdownDocument source={source} emptyLabel="" resolveLink={href=>resolveProjectFileLink(project,'results/report.md',href)} />)
    expect(screen.getByRole('link',{name:'模型下载'})).toHaveAttribute('href',expected)
    unmount()
    for(const invalid of ['results/../secret','results/%2e%2e/secret','results/%5csecret','results/%00secret','.codex/auth.json','/etc/passwd'])
      expect(resolveProjectLink(project,prefix+invalid)).toBe('')
  }
})

it('opens agent workflow links from conversation replies and saved reports',()=>{
  const project='11111111-1111-4111-8111-111111111111',member='22222222-2222-4222-8222-222222222222'
  const source=`[打开工作流](workflow://${member}) [下载模型](results/models/export/prediction-model.zip)`
  const {rerender}=render(<MarkdownDocument source={source} emptyLabel="" resolveLink={href=>resolveProjectLink(project,href)} />)
  expect(screen.getByRole('link',{name:'打开工作流'})).toHaveAttribute('href',`/applications/${member}?tab=edit`)
  expect(screen.getByRole('link',{name:'下载模型'}).getAttribute('href')).toContain(`/applications/${project}/workspace/files/results/models/export/prediction-model.zip`)
  rerender(<MarkdownDocument source={source} emptyLabel="" resolveLink={href=>resolveProjectFileLink(project,'results/report.md',href)} />)
  expect(screen.getByRole('link',{name:'打开工作流'})).toHaveAttribute('href',`/applications/${member}?tab=edit`)
  rerender(<MarkdownDocument source="[错误地址](workflow://example.com/path) [脚本](javascript:alert%281%29)" emptyLabel="" resolveLink={href=>resolveProjectLink(project,href)} />)
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})

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
