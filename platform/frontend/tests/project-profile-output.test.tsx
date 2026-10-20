import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import { ProjectTaskOutput } from '@/app/components/ProjectRunPanel'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
afterEach(cleanup)

it('shows profile duplicate locations and source with the complete JSON and CSV downloads', () => {
  const markdown = '# 数据体检\n\n来源：requirement-package/data.csv\n\n4 行，2 列，完全重复 2 行。'
    + '\n\n行号为源文件中记录起始物理行，包含表头和空白行占用的行号。'
    + '\n\n完全重复指逐字段相同的额外记录，不含首次出现行；仅标记复核，未自动删除或修改原资料。'
    + '\n\n## 完全重复记录\n\n| 重复行 | 首次出现行 |\n| --- | --- |\n| 5 | 2 |\n| 8 | 2 |'
  render(<ProjectTaskOutput projectId="p" task={{ id: 'profile', status: 'succeeded', outputs: {
    markdown, result: { duplicates: 2, issues: [
      { source_file: 'requirement-package/data.csv', sheet: '', row: 5, first_row: 2, reason: '完全重复', fields: 'id,value' },
      { source_file: 'requirement-package/data.csv', sheet: '', row: 8, first_row: 2, reason: '完全重复', fields: 'id,value' },
    ], artifacts: [
      { label: '明细 CSV', file_path: 'results/examples/run/details.csv' },
      { label: '结构化结果 JSON', file_path: 'results/examples/run/result.json' },
      { label: '报告 Markdown', file_path: 'results/examples/run/report.md' },
    ] },
  } } as never} />)
  const table = screen.getByRole('table')
  expect(within(table).getByRole('columnheader', { name: '重复行' })).toBeVisible()
  expect(within(table).getByRole('columnheader', { name: '首次出现行' })).toBeVisible()
  expect(within(table).getByRole('row', { name: '5 2' })).toBeVisible()
  expect(within(table).getByRole('row', { name: '8 2' })).toBeVisible()
  expect(screen.getByText('来源：requirement-package/data.csv')).toBeVisible()
  expect(screen.getByText(/未自动删除或修改原资料/)).toBeVisible()
  expect(screen.getByRole('link', { name: '明细 CSV ↓' })).toHaveAttribute('href', '/api/platform/api/v1/applications/p/workspace/files/results/examples/run/details.csv?download=1')
  expect(screen.getByRole('link', { name: '结构化结果 JSON ↓' })).toHaveAttribute('href', '/api/platform/api/v1/applications/p/workspace/files/results/examples/run/result.json?download=1')
  expect(api).not.toHaveBeenCalled()
})
