import {useLayoutEffect} from 'react'
import {act,cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import ProjectImage from '@/app/components/ProjectImage'

vi.mock('@/lib/platform',()=>({withFrontendToken:(value:string)=>value}))
afterEach(cleanup)

function ImmediatelyBrokenImage(){
  useLayoutEffect(()=>{screen.getByRole('img',{name:'待复核图片'}).dispatchEvent(new Event('error'))},[])
  return <ProjectImage projectId="p" path="results/first.png" label="待复核图片"/>
}

it('keeps an image failure raised before passive effects instead of erasing the warning',async()=>{
  await act(async()=>{render(<ImmediatelyBrokenImage/>)})
  expect(screen.getByRole('alert')).toHaveTextContent('不要凭缺失图片猜测结论')
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  expect(screen.getByRole('link',{name:'下载图片：待复核图片'})).toHaveAttribute('download')
})

it('preserves a failure for the same image and resets it when the file or project changes',()=>{
  const view=render(<ProjectImage projectId="p" path="results/first.png" label="样本一"/>)
  fireEvent.error(screen.getByRole('img',{name:'样本一'}))
  view.rerender(<ProjectImage projectId="p" path="results/first.png" label="样本一更名"/>)
  expect(screen.getByRole('alert')).toBeVisible()
  view.rerender(<ProjectImage projectId="p" path="results/second.png" label="样本二"/>)
  const second=screen.getByRole('img',{name:'样本二'})
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(second).toHaveAttribute('src','/api/platform/api/v1/applications/p/workspace/files/results/second.png')
  fireEvent.error(second)
  view.rerender(<ProjectImage projectId="other" path="results/second.png" label="另一项目"/>)
  expect(screen.getByRole('img',{name:'另一项目'})).toHaveAttribute('src','/api/platform/api/v1/applications/other/workspace/files/results/second.png')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
