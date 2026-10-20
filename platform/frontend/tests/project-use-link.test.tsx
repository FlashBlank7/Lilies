import { Suspense } from 'react'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import UsePage from '@/app/use/[id]/page'
import { api } from '@/lib/platform'

const {router}=vi.hoisted(()=>({router:{replace:vi.fn()}}))
vi.mock('next/navigation',()=>({useRouter:()=>router}))
vi.mock('@/lib/platform',()=>({api:vi.fn()}))
vi.mock('@/app/components/AuthBoundary',()=>({useAccount:()=>({id:'user',role:'member'})}))
afterEach(()=>{cleanup();vi.mocked(api).mockReset();router.replace.mockReset()})

it('opens the project task form from a legacy use link without reading a global definition or starting a run',async()=>{
  vi.mocked(api).mockResolvedValue({project_id:'p'} as never)
  await act(async()=>{render(<Suspense><UsePage params={Promise.resolve({id:'w'})}/></Suspense>)})
  await waitFor(()=>expect(router.replace).toHaveBeenCalledWith('/projects/p?run=w'))
  expect(vi.mocked(api).mock.calls).toEqual([['/api/v1/applications/w/project']])
  expect(screen.queryByRole('textbox',{name:/访问码/})).not.toBeInTheDocument()
})

it('keeps an inaccessible workflow inaccessible instead of falling back to the old access-code form',async()=>{
  vi.mocked(api).mockRejectedValue(new Error('没有访问此项目的权限'))
  await act(async()=>{render(<Suspense><UsePage params={Promise.resolve({id:'w'})}/></Suspense>)})
  expect(await screen.findByRole('alert')).toHaveTextContent('没有访问此项目的权限')
  expect(router.replace).not.toHaveBeenCalled()
  expect(screen.queryByRole('textbox',{name:/访问码/})).not.toBeInTheDocument()
})
