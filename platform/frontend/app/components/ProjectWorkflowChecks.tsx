'use client'

import Link from 'next/link'
import OutputView from './OutputView'
import styles from './workspace-tools.module.css'

type SavedCase = { id: string; name: string; requirement: string; inputs: Record<string, unknown>; result?: { passed?: boolean; error?: string; run_error?: string; assertions?: Array<Record<string, unknown>>; outputs?: Record<string, unknown> } }

function displayValue(value: unknown): string {
  return value === undefined ? '未产生' : JSON.stringify(value)
}

export default function ProjectWorkflowChecks({ projectId, workflowId, cases, report, running, dirty, onRun }: {
  projectId: string; workflowId: string; cases: SavedCase[]; report: Record<string, unknown> | null;
  running: boolean; dirty: boolean; onRun: () => void;
}) {
  return <section className={styles.section} aria-label="工作流测试">
    <h2>工作流测试</h2>
    <p>使用已保存的示例输入检查输出，也可以直接运行工作流。</p>
    <Link target="_top" href={`/projects/${projectId}?run=${workflowId}`}>填写输入并运行工作流</Link>
    {cases.length ? <>
      <p>{cases.length} 个已保存用例{dirty ? ' · 请先保存当前节点配置' : ''}</p>
      <button disabled={running || dirty} onClick={onRun}>{running ? '正在运行测试…' : '运行已保存的测试'}</button>
      {cases.map(item => <details key={item.id} open={item.result?.passed === false}>
        <summary>{item.name} · {running ? '运行中' : item.result ? item.result.passed ? '通过' : '未通过' : '尚未运行'}</summary>
        {item.requirement && <p>{item.requirement}</p>}
        {(item.result?.run_error || item.result?.error) && <p role="alert">{item.result.run_error || item.result.error}</p>}
        {item.result?.assertions?.filter(assertion => assertion.passed === false).map((assertion, index) => <div key={index}>
          <p>输出不符合预期：<code>{Array.isArray(assertion.path) && assertion.path.length ? assertion.path.join('.') : '完整输出'}</code>{' '}（{String(assertion.operator || 'exists')}）</p>
          <p>预期：<code>{Object.prototype.hasOwnProperty.call(assertion, 'expected') ? displayValue(assertion.expected) : String(assertion.operator || 'exists')}</code></p>
          <p>实际：<code>{displayValue(assertion.actual)}</code></p>
          {Boolean(assertion.error) && <p role="alert">{String(assertion.error)}</p>}
        </div>)}
        {item.result?.outputs && <OutputView outputs={item.result.outputs} />}
        <details><summary>示例输入</summary><pre>{JSON.stringify(item.inputs, null, 2)}</pre></details>
      </details>)}
    </> : <p>尚未保存测试用例。可以先通过运行入口验证当前流程。</p>}
    {report && <details><summary>测试详情</summary><pre>{JSON.stringify(report, null, 2)}</pre></details>}
  </section>
}
