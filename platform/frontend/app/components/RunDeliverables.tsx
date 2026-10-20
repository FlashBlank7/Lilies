'use client'

import { useState } from 'react'
import { projectFilePathFromLink, resolveProjectLink } from '@/lib/project-links'
import ProjectFileReader from './ProjectFileReader'

/** Named delivery paths belong to this task, never the project's latest file. */
export default function RunDeliverables({projectId, results}: {projectId: string; results: Record<string, unknown>[]}) {
  const [reading, setReading] = useState<string | null>(null)
  const files = new Map<string, {label: string; url: string; preview: boolean}>()
  for (const result of results) {
    const paths = result.paths && typeof result.paths === 'object' && !Array.isArray(result.paths) ? result.paths as Record<string, unknown> : result
    const named = [
      ['report', '完整报告'], ['report_html', typeof paths.report === 'string' ? '报告 HTML 文件' : '完整报告'],
      ['results_excel', '完整结果 Excel'], ['results_csv', '完整结果 CSV'],
      ['model_package', '模型包'], ['manifest', '本次文件清单'],
    ]
    for (const [key, label] of named) {
      const path = paths[key]
      if (typeof path !== 'string') continue
      const url = resolveProjectLink(projectId, path)
      if (projectFilePathFromLink(projectId, url) !== path || files.has(path)) continue
      files.set(path, {label, url, preview: label === '完整报告' && /\.(md|txt|html?)$/i.test(path)})
    }
  }
  if (!files.size) return null
  return <section aria-label="本次交付文件"><h3>本次交付文件</h3><p>以下文件来自本次运行，历史结果仍保留在运行记录中。</p>
    {[...files].map(([path, file]) => <p key={path}>
      {file.preview && <button onClick={() => setReading(path)}>查看{file.label}</button>}{' '}
      <a download href={`${file.url}${file.url.includes('?') ? '&' : '?'}download=1`}>下载{file.label} ↓</a>
    </p>)}
    {reading && <ProjectFileReader projectId={projectId} path={reading} onClose={() => setReading(null)}/>}
  </section>
}
