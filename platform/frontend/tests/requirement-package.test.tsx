import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/lib/platform'
import RequirementPackageImport from '@/app/components/RequirementPackageImport'
import RequirementPackageMaterials from '@/app/components/RequirementPackageMaterials'

vi.mock('@/lib/platform', () => ({ api: vi.fn(), withFrontendToken: (path: string) => path }))
beforeEach(() => { vi.mocked(api).mockReset() })

describe('requirement package', () => {
  it('uploads the selected ZIP into a project without starting a build', async () => {
    vi.mocked(api).mockResolvedValue({ application: { id: 'new-app' } })
    const open = vi.fn()
    render(<RequirementPackageImport onImported={open} />)
    const button = screen.getByRole('button', { name: '导入并打开项目' })
    expect(button).toBeDisabled()
    const file = new File(['zip bytes'], '企业需求.zip', { type: 'application/zip' })
    fireEvent.change(screen.getByLabelText('选择需求包 ZIP'), { target: { files: [file] } })
    fireEvent.click(button)
    await waitFor(() => expect(open).toHaveBeenCalledWith('new-app'))
    expect(api).toHaveBeenCalledTimes(1)
    const [path, request] = vi.mocked(api).mock.calls[0]
    expect(path).toBe('/api/v1/projects/requirement-packages/import')
    expect(request?.method).toBe('POST')
    expect((request?.body as FormData).get('file')).toBe(file)
  })

  it('keeps the file selected and allows retry after an import error', async () => {
    vi.mocked(api).mockRejectedValue(new Error('缺少需求正文'))
    const open = vi.fn()
    render(<RequirementPackageImport onImported={open} />)
    fireEvent.change(screen.getByLabelText('选择需求包 ZIP'), { target: { files: [new File(['x'], 'bad.zip')] } })
    fireEvent.click(screen.getByRole('button', { name: '导入并打开项目' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('缺少需求正文')
    expect(screen.getByRole('button', { name: '导入并打开项目' })).toBeEnabled()
    expect(open).not.toHaveBeenCalled()
  })

  it('retains explicitly requested legacy workflow imports', async () => {
    vi.mocked(api).mockResolvedValue({ application: { id: 'legacy' } })
    const open = vi.fn()
    render(<RequirementPackageImport project={false} onImported={open} />)
    fireEvent.change(screen.getByLabelText('选择需求包 ZIP'), { target: { files: [new File(['x'], 'input.zip')] } })
    fireEvent.click(screen.getByRole('button', { name: '导入并打开工作流' }))
    await waitFor(() => expect(open).toHaveBeenCalledWith('legacy'))
    expect(vi.mocked(api).mock.calls[0][0]).toBe('/api/v1/requirement-packages/import')
  })

  it('shows imported documents and download links in the session', async () => {
    vi.mocked(api).mockResolvedValue([
      { path: 'requirement-package/需求.md', size: 100 }, { path: 'outputs/log.txt', size: 2 },
    ])
    render(<RequirementPackageMaterials applicationId="test" requirement="需求正文" />)
    expect(await screen.findByText('企业资料 · 1 个文件')).toBeInTheDocument()
    expect(screen.queryByText('需求正文')).not.toBeInTheDocument()
    expect(screen.getByText('需求.md')).toHaveAttribute('href', '/api/platform/api/v1/applications/test/workspace/files/requirement-package%2F%E9%9C%80%E6%B1%82.md')
    expect(screen.queryByText('outputs/log.txt')).not.toBeInTheDocument()
  })
})
