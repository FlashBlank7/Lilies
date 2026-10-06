import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {api} from '@/lib/platform'
import Onboarding,{TutorialLauncher,useOnboarding} from '@/app/components/Onboarding'
const {push,location}=vi.hoisted(()=>({push:vi.fn(),location:{path:'/projects'}}))
vi.mock('next/navigation',()=>({useRouter:()=>({push}),usePathname:()=>location.path}))
vi.mock('@/lib/platform',()=>({api:vi.fn()}))
const fresh=()=>({status:'new',step:'project',completed_steps:[] as string[],project_id:null as string|null})
let saved=fresh()
function Page(){const guide=useOnboarding();return <><TutorialLauncher/><input aria-label="我的草稿" defaultValue="不要覆盖"/><button data-guide-anchor="project-open" onClick={()=>guide.mark('project','p')}>打开项目</button><section data-guide="materials"><button onClick={()=>guide.mark('materials','p')}>选择资料</button><input aria-label="资料备注"/></section><button data-guide-anchor="workflow-call" onClick={()=>guide.mark('workflow','p','conversation')}>调用流程</button></>}
function Tutorial(){return <Onboarding><Page/></Onboarding>}
beforeEach(()=>{
 saved=fresh();location.path='/projects';vi.clearAllMocks()
 vi.spyOn(HTMLElement.prototype,'getClientRects').mockImplementation(()=>[{top:30,left:30,width:120,height:50}] as unknown as DOMRectList)
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({top:30,left:30,right:150,bottom:80,width:120,height:50,x:30,y:30,toJSON:()=>({})})
 HTMLElement.prototype.scrollIntoView=vi.fn()
 vi.mocked(api).mockImplementation(async(url,options)=>{
  expect(url).toBe('/api/v1/me/onboarding')
  if(options){const patch=JSON.parse(options.body as string);if(patch.restart)saved={...fresh(),status:'active'};saved={...saved,...patch,completed_steps:[...new Set([...saved.completed_steps,...(patch.completed_steps||[])])]}}
  return {...saved} as never
 })
})
afterEach(()=>{cleanup();vi.restoreAllMocks();vi.unstubAllGlobals()})
it('starts new accounts automatically with a blocking spotlight and no business calls',async()=>{
 render(<Tutorial/>);expect(await screen.findByText('第 1 / 6 步')).toBeInTheDocument()
 await waitFor(()=>expect(saved.status).toBe('active'))
 expect(document.querySelectorAll('[data-guide-shield]').length).toBe(4)
 expect(screen.queryByRole('button',{name:'开始引导'})).not.toBeInTheDocument()
 expect(screen.getByLabelText('我的草稿')).toHaveValue('不要覆盖')
 expect(saved.completed_steps).toEqual([])
})
it('old accounts stay quiet; clicking the launcher starts immediately',async()=>{
 saved.status='finished';render(<Tutorial/>);await waitFor(()=>expect(api).toHaveBeenCalledOnce())
 expect(screen.queryByRole('complementary',{name:'使用教程'})).not.toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'使用教程'}))
 expect(await screen.findByText('第 1 / 6 步')).toBeInTheDocument()
 await waitFor(()=>expect(saved.status).toBe('active'))
})
it('keeps the spotlight through project navigation and records only successful actions',async()=>{
 const view=render(<Tutorial/>);await screen.findByText('第 1 / 6 步')
 fireEvent.click(screen.getByRole('button',{name:'打开项目'}))
 location.path='/projects/p';view.rerender(<Tutorial/>)
 expect(await screen.findByText('第 2 / 6 步')).toBeInTheDocument()
 await waitFor(()=>expect(saved.completed_steps).toEqual(['project']))
 expect(document.querySelector('[data-guide-overlay]')).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'下一步'}))
 await screen.findByText('第 3 / 6 步');await waitFor(()=>expect(saved.step).toBe('workflow'))
 expect(saved.completed_steps).toEqual(['project'])
})
it('keeps guidance open while editing the highlighted input and blocks keyboard access to background',async()=>{
 saved={...fresh(),status:'active',step:'materials',project_id:'p'};location.path='/projects/p'
 render(<Tutorial/>);await screen.findByText('第 2 / 6 步')
 const input=screen.getByLabelText('资料备注');input.focus();fireEvent.change(input,{target:{value:'保留备注'}})
 expect(input).toHaveFocus();expect(screen.getByRole('complementary',{name:'使用教程'})).toBeInTheDocument()
 screen.getByLabelText('我的草稿').focus();expect(screen.getByRole('complementary',{name:'使用教程'})).toHaveFocus()
})
it('asks gently before skipping, can return, and persists the confirmed skip',async()=>{
 const view=render(<Tutorial/>);await screen.findByText('第 1 / 6 步')
 fireEvent.click(screen.getByRole('button',{name:'跳过引导'}))
 expect(screen.getByText(/跳过后，寻找资料/)).toBeInTheDocument()
 expect(saved.status).toBe('active')
 fireEvent.click(screen.getByRole('button',{name:'继续引导'}));expect(screen.getByText('第 1 / 6 步')).toBeInTheDocument()
 fireEvent.keyDown(document,{key:'Escape'})
 fireEvent.click(screen.getByRole('button',{name:'暂时跳过'}))
 await waitFor(()=>expect(saved.status).toBe('skipped'))
 expect(document.querySelector('[data-guide-overlay]')).not.toBeInTheDocument()
 view.unmount();render(<Tutorial/>);await waitFor(()=>expect(api).toHaveBeenCalledTimes(4))
 expect(screen.queryByRole('complementary',{name:'使用教程'})).not.toBeInTheDocument()
})
it('follows an open native dialog without hiding the tutorial behind its backdrop',async()=>{
 render(<Tutorial/>);await screen.findByText('第 1 / 6 步')
 const dialog=document.createElement('dialog');dialog.setAttribute('open','');document.body.append(dialog)
 await waitFor(()=>expect(dialog.querySelector('[data-guide-overlay]')).not.toBeNull())
 dialog.remove();await waitFor(()=>expect(screen.getByRole('complementary',{name:'使用教程'})).toBeInTheDocument())
})
it('preserves unsaved local progress on failure, retries, and still allows skipping',async()=>{
 render(<Tutorial/>);await screen.findByText('第 1 / 6 步');await waitFor(()=>expect(saved.status).toBe('active'))
 vi.mocked(api).mockRejectedValueOnce(new Error('offline'))
 fireEvent.click(screen.getByRole('button',{name:'下一步'}))
 expect(await screen.findByRole('alert')).toHaveTextContent('教程进度暂未保存')
 expect(screen.getByText('第 2 / 6 步')).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'重试'}));await waitFor(()=>expect(saved.step).toBe('materials'))
})
it('ignores previous-account responses and restores launcher focus after explicit skip',async()=>{
 let resolve!:(value:unknown)=>void
 vi.mocked(api).mockImplementationOnce(()=>new Promise(r=>{resolve=r}) as never)
 const view=render(<Onboarding key="old"><Page/></Onboarding>);saved.status='skipped'
 view.rerender(<Onboarding key="new"><Page/></Onboarding>)
 await act(async()=>{resolve({...fresh(),status:'active'})})
 await waitFor(()=>expect(screen.queryByRole('complementary',{name:'使用教程'})).not.toBeInTheDocument())
 const launcher=screen.getByRole('button',{name:'使用教程'});launcher.focus();fireEvent.click(launcher)
 await screen.findByText('第 1 / 6 步');fireEvent.click(screen.getByRole('button',{name:'跳过引导'}));fireEvent.click(screen.getByRole('button',{name:'暂时跳过'}))
 await waitFor(()=>expect(launcher).toHaveFocus())
})

it('keeps a same-step workflow selection from suppressing navigation to results',async()=>{
 saved={...fresh(),status:'active',step:'conversation',project_id:'p'};location.path='/projects/p'
 const listener=vi.fn();window.addEventListener('lilies:guide-navigate',listener)
 render(<Tutorial/>);await screen.findByText('第 4 / 6 步')
 fireEvent.click(screen.getByRole('button',{name:'调用流程'}));listener.mockClear()
 fireEvent.click(screen.getByRole('button',{name:'下一步'}))
 await waitFor(()=>expect(listener.mock.calls.at(-1)?.[0].detail).toBe('results'))
 window.removeEventListener('lilies:guide-navigate',listener)
})
it('can finish the explanation without inventing completed operations',async()=>{
 render(<Tutorial/>);await screen.findByText('第 1 / 6 步')
 for(let step=1;step<6;step++){fireEvent.click(screen.getByRole('button',{name:'下一步'}));await screen.findByText(`第 ${step+1} / 6 步`)}
 fireEvent.click(screen.getByRole('button',{name:'完成引导'}))
 await waitFor(()=>expect(saved.status).toBe('finished'))
 expect(saved.completed_steps).toEqual([])
 expect(screen.queryByRole('complementary',{name:'使用教程'})).not.toBeInTheDocument()
})

const bounds=(left:number,top:number,width:number,height:number)=>({left,top,right:left+width,bottom:top+height,width,height,x:left,y:top,toJSON:()=>({})})
it.each([
 ['below the desktop viewport',1454,670,0,1562,1454,10608],
 ['above the desktop viewport',1454,670,20,-600,1000,200],
 ['left of the desktop viewport',1454,670,-1600,40,1000,200],
 ['right of the desktop viewport',1454,670,1600,40,1000,200],
 ['below the mobile viewport',390,844,0,1200,390,10608],
])('keeps the guide visible and skippable when its target is %s',async(_label,width,height,left,top,targetWidth,targetHeight)=>{
 vi.stubGlobal('innerWidth',width);vi.stubGlobal('innerHeight',height)
 saved={...fresh(),status:'active',step:'materials',project_id:'p'};location.path='/projects/p'
 vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function(this:HTMLElement){
  return this.dataset.guide==='materials'?bounds(left,top,targetWidth,targetHeight):bounds(0,0,Math.min(352,width-24),308)
 })
 render(<Tutorial/>);await screen.findByText('第 2 / 6 步')
 const panel=screen.getByRole('complementary',{name:'使用教程'})
 await waitFor(()=>{
  const renderedHeight=Math.min(308,parseFloat(panel.style.maxHeight))
  expect(parseFloat(panel.style.top)).toBeGreaterThanOrEqual(12)
  expect(parseFloat(panel.style.top)+renderedHeight).toBeLessThanOrEqual(height-12)
  expect(parseFloat(panel.style.left)).toBeGreaterThanOrEqual(12)
  expect(parseFloat(panel.style.left)+parseFloat(panel.style.width)).toBeLessThanOrEqual(width-12)
 })
 fireEvent.click(screen.getByRole('button',{name:'跳过引导'}))
 fireEvent.click(screen.getByRole('button',{name:'暂时跳过'}))
 await waitFor(()=>expect(saved.status).toBe('skipped'))
 expect(screen.queryByRole('complementary',{name:'使用教程'})).not.toBeInTheDocument()
 expect(screen.getByLabelText('我的草稿')).toHaveValue('不要覆盖')
 expect(saved.completed_steps).toEqual([])
})

it('aligns a long materials section at its beginning and keeps the guide reachable after scrolling away',async()=>{
 vi.stubGlobal('innerWidth',1454);vi.stubGlobal('innerHeight',670);vi.stubGlobal('scrollY',2000)
 saved={...fresh(),status:'active',step:'materials',project_id:'p'};location.path='/projects/p'
 let targetTop=24
 vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function(this:HTMLElement){
  return this.dataset.guide==='materials'?bounds(0,targetTop,1454,10608):bounds(0,0,352,308)
 })
 render(<Tutorial/>);await screen.findByText('第 2 / 6 步')
 expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledWith({block:'start',behavior:'instant'})
 const input=screen.getByLabelText('资料备注');input.focus()
 fireEvent.change(input,{target:{value:'保留长列表中的备注'}})
 expect(input).toHaveFocus()
 targetTop=1562;vi.stubGlobal('scrollY',462);fireEvent.scroll(document)
 await waitFor(()=>expect(document.querySelectorAll('[data-guide-shield]')).toHaveLength(1))
 expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledTimes(1)
 const panel=screen.getByRole('complementary',{name:'使用教程'})
 expect(parseFloat(panel.style.top)+Math.min(308,parseFloat(panel.style.maxHeight))).toBeLessThanOrEqual(658)
 fireEvent.keyDown(document,{key:'Escape'})
 fireEvent.click(screen.getByRole('button',{name:'暂时跳过'}))
 await waitFor(()=>expect(saved.status).toBe('skipped'))
 expect(input).toHaveValue('保留长列表中的备注')
 expect(saved.completed_steps).toEqual([])
})

it('reveals the same target again when asynchronously loaded content pushes it out of view',async()=>{
 vi.stubGlobal('innerWidth',1454);vi.stubGlobal('innerHeight',670);vi.stubGlobal('scrollY',0)
 saved={...fresh(),status:'active',step:'materials',project_id:'p'};location.path='/projects/p'
 let targetTop=160
 vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function(this:HTMLElement){
  return this.dataset.guide==='materials'?bounds(248,targetTop,1174,10608):bounds(0,0,352,308)
 })
 vi.mocked(HTMLElement.prototype.scrollIntoView).mockImplementation(()=>{targetTop=160})
 render(<Tutorial/>);await screen.findByText('第 2 / 6 步')
 await waitFor(()=>expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledOnce())
 targetTop=1686
 const loaded=document.createElement('p');loaded.textContent='稍后加载的项目流程'
 document.querySelector('[data-guide="materials"]')!.before(loaded)
 await waitFor(()=>expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledTimes(2))
 expect(screen.getByRole('complementary',{name:'使用教程'})).toBeInTheDocument()
 fireEvent.click(screen.getByRole('button',{name:'选择资料'}))
 await screen.findByText('第 3 / 6 步')
 await waitFor(()=>expect(saved.completed_steps).toEqual(['materials']))
})
