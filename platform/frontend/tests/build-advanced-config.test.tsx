/** 构建高级配置面板：默认值与后端 BuildRequest 一致、留空时限=显式 null、越界报业主语言错误、摘要实时反映当前配置。 */

import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import BuildAdvancedConfig, {
  buildOptionsPayload,
  defaultBuildOptions,
  type BuildOptions,
} from '@/app/components/BuildAdvancedConfig'

describe('buildOptionsPayload', () => {
  it('默认选项产出与后端 BuildRequest 默认值一致的请求体', () => {
    const result = buildOptionsPayload(defaultBuildOptions(true))
    expect(result.error).toBeUndefined()
    expect(result.payload).toEqual({
      auto_publish: true,
      max_turns: 36,
      max_repair_cycles: 4,
      max_elapsed_seconds: 480,
      planning_mode: 'auto',
    })
  })

  it('时限留空时显式发送 null（不限时），而不是静默回落到默认 480 秒', () => {
    const options: BuildOptions = { ...defaultBuildOptions(false), maxElapsedSeconds: '' }
    const result = buildOptionsPayload(options)
    expect(result.payload?.max_elapsed_seconds).toBeNull()
    expect(result.payload?.auto_publish).toBe(false)
  })

  it('回合数越界返回业主语言的校验错误', () => {
    const tooFew = buildOptionsPayload({ ...defaultBuildOptions(true), maxTurns: '3' })
    expect(tooFew.error).toContain('5–200')
    const fraction = buildOptionsPayload({ ...defaultBuildOptions(true), maxRepairCycles: '2.5' })
    expect(fraction.error).toContain('1–30')
    const negative = buildOptionsPayload({ ...defaultBuildOptions(true), maxElapsedSeconds: '-1' })
    expect(negative.error).toContain('86400')
  })

  it('英文 locale 返回英文错误', () => {
    const result = buildOptionsPayload({ ...defaultBuildOptions(true), maxTurns: '999' }, 'en')
    expect(result.error).toContain('between 5 and 200')
  })
})

function Harness({ initial }: { initial: BuildOptions }) {
  const [value, setValue] = useState(initial)
  return <BuildAdvancedConfig onChange={setValue} value={value} />
}

describe('BuildAdvancedConfig', () => {
  it('折叠摘要常驻显示当前配置，修改字段后实时更新', () => {
    const { container } = render(<Harness initial={defaultBuildOptions(false)} />)
    const digest = container.querySelector('[data-build-advanced="digest"]')
    expect(digest?.textContent).toContain('36 轮')
    expect(digest?.textContent).toContain('手动发布')

    const turns = container.querySelector('[data-build-advanced="max-turns"]') as HTMLInputElement
    fireEvent.change(turns, { target: { value: '60' } })
    expect(container.querySelector('[data-build-advanced="digest"]')?.textContent).toContain('60 轮')

    const deadline = container.querySelector('[data-build-advanced="max-elapsed-seconds"]') as HTMLInputElement
    fireEvent.change(deadline, { target: { value: '' } })
    expect(container.querySelector('[data-build-advanced="digest"]')?.textContent).toContain('不限时')
  })

  it('规划模式与自动发布可切换', () => {
    const { container } = render(<Harness initial={defaultBuildOptions(true)} />)
    const planning = container.querySelector('[data-build-advanced="planning-mode"]') as HTMLSelectElement
    fireEvent.change(planning, { target: { value: 'required' } })
    expect(container.querySelector('[data-build-advanced="digest"]')?.textContent).toContain('必须规划')

    const publish = container.querySelector('[data-build-advanced="auto-publish"]') as HTMLInputElement
    expect(publish.checked).toBe(true)
    fireEvent.click(publish)
    expect(container.querySelector('[data-build-advanced="digest"]')?.textContent).toContain('手动发布')
    expect(screen.getByText('测试通过后自动发布')).toBeInTheDocument()
  })
})
