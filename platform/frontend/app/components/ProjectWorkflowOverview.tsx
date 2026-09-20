'use client'

import { useRef, useState } from 'react'
import { ArrowRight } from 'lucide-react'
import { MarkdownDocument } from '@/lib/markdown'
import { availabilityNames, workNames, comparisonNames, type ProjectProgress, type ProjectTopology, type ProgressItem, type ProgressResult, type WorkflowOutline } from '@/lib/project-progress'
import styles from './project-workflow-overview.module.css'

export function WorkflowRoute({ flow, onWorkflow }: { flow: WorkflowOutline; onWorkflow: (id: string) => void }) {
  const nodes = new Map(flow.nodes.map(node => [node.id, node]))
  const roots = flow.nodes.filter(n => !flow.edges.some(e => e.target === n.id))
  function path(id: string, ancestors: Set<string>, depth: number): React.ReactNode {
    const chain: React.ReactNode[] = []
    const seen = new Set(ancestors)
    let current = id
    while (current) {
      if (seen.has(current)) { chain.push(<li key={'repeat-' + current}>返回已有步骤：{nodes.get(current)?.title || current}</li>); break }
      seen.add(current)
      const node = nodes.get(current)
      if (!node) { chain.push(<li key={current}>缺失的步骤：{current}</li>); break }
      const edges = flow.edges.filter(e => e.source === current)
      chain.push(<li key={current}>
        <div className={styles.routeStep}><span>{node.type === 'if_else' ? '判断' : node.type === 'end' || node.type === 'answer' ? '结果' : node.workflow_id ? '调用' : '步骤'}</span>
          {node.workflow_id ? <button onClick={() => onWorkflow(node.workflow_id)}>{node.title}</button> : <strong>{node.title}</strong>}</div>
        {node.type === 'if_else' && <small>按条件选择路径</small>}
        {edges.length > 1 && <div className={styles.branches}>{edges.map((edge, index) => <RouteBranch key={index}
          title={`${edge.branch === node.default_branch ? '其他情况' : edge.branch || '并行路径'} → ${nodes.get(edge.target)?.title || '缺失的步骤'}`}
          initialOpen={depth === 0 && nodes.get(edge.target)?.type !== 'end'}>
          {edge.branch && <details className={styles.conditions}><summary>查看配置条件</summary><pre>{JSON.stringify(node.branches.find(b => b.id === edge.branch)?.conditions || (edge.branch === node.default_branch ? '前面的条件均不满足' : '画布连线上的分支标识：' + edge.branch), null, 2)}</pre></details>}
          {path(edge.target, seen, depth + 1)}
        </RouteBranch>)}</div>}
      </li>)
      if (edges.length !== 1) break
      current = edges[0].target
    }
    return <ol className={styles.route}>{chain}</ol>
  }
  return <div>{roots.length ? roots.map(root => <div key={root.id}>{path(root.id, new Set(), 0)}</div>) : <p>没有可读取的起点，请打开画布检查连接。</p>}</div>
}
function RouteBranch({ title, initialOpen, children }: { title: string; initialOpen: boolean; children: React.ReactNode }) {
  const [open, setOpen] = useState(initialOpen)
  return <details open={open} onToggle={e => setOpen(e.currentTarget.open)}><summary>{title}</summary>{open && children}</details>
}

export default function ProjectWorkflowOverview({ projectId, selectedId, progress, topology, onWorkflow, onTalk, onTask, onFile, onRequirements, onEdit, initialItemId }: {
  projectId: string; selectedId: string; initialItemId?: string; progress: ProjectProgress; topology: ProjectTopology
  onWorkflow: (id: string) => void; onTalk: (item: ProgressItem | undefined, message: string) => void
  onTask: (id: string) => void; onFile: (path: string) => void; onRequirements: () => void; onEdit: () => void
}) {
  const member = topology.members.find(m => m.id === selectedId)
  const main = selectedId === projectId
  const items = main ? progress.value.items : progress.value.items.filter(i => i.workflow_ids.includes(selectedId))
  const detailRef = useRef<HTMLElement>(null)
  const [itemId, setItemId] = useState(initialItemId || items[0]?.id || '')
  const [view, setView] = useState<'requirements' | 'path'>('requirements')
  const selectedItem = items.find(item => item.id === itemId) || items[0]
  const note = progress.value.workflows?.find(n => n.workflow_id === selectedId)
  const flow = topology.flows?.[selectedId]
  const explain = (item?: ProgressItem) => onTalk(item, `请仅依据本项目已确认需求、当前工作流和实际结果，整理「${item?.title || member?.name || '当前工作流'}」的用途、输入输出，以及逐项需求对照：原要求及出处、目前达到的程度、尚存差距和下一步；更新项目进展中的流程说明与需求对照。缺少验证就明确写尚未验证。本次只整理说明，不运行工作流、不训练、不改算法或已有任务结果。`)
  function resultLinks(results: ProgressResult[]) { return <div className={styles.resultLinks}>{results.map((r, index) => <span key={index}>
    {r.task_id && <button onClick={() => onTask(r.task_id)}>{r.label} · 运行</button>}
    {r.file_path && <button onClick={() => onFile(r.file_path)}>{r.label} · 文件</button>}
  </span>)}</div> }
  return <div className={styles.overview}>
    <header className={styles.heading}><div><small>{main ? '主流程 · 项目协作入口' : '成员工作流'}</small><h2>{member?.name || '工作流'}</h2>
      <p>{note?.purpose || (main ? progress.value.goal : member?.description) || '尚未整理这条工作流的业务说明。'}</p></div>
      <div className={styles.actions}><button onClick={onRequirements}>查看原需求</button><button onClick={onEdit}>编辑画布</button></div></header>
    <div className={styles.io}><div><h3>需要什么输入</h3><p>{note?.inputs || '尚未整理输入说明，可让统筹根据当前草稿补充。'}</p></div><ArrowRight size={20}/><div><h3>产出什么</h3><p>{note?.outputs || '尚未整理产出说明，请查看下方已有结果。'}</p></div></div>
    {!note && <button onClick={() => explain()}>让统筹补充这条流程的说明</button>}
    <div className={styles.viewTabs} role="tablist" aria-label="工作流说明视图"><button role="tab" aria-selected={view === 'requirements'} onClick={() => setView('requirements')}>用途与需求对照</button><button role="tab" aria-selected={view === 'path'} onClick={() => setView('path')}>协作如何执行</button></div>
    {view === 'requirements' && <section aria-label="需求与当前结果对照"><div className={styles.sectionHeading}><h3>{main ? '业务能力与需求对照' : '这条流程参与解决的需求'}</h3><small>{progress.updated_at ? '整理于 ' + new Date(progress.updated_at).toLocaleString() : '等待统筹整理'}</small></div>
      {!items.length && <p>尚未关联业务事项，不能据此判断达到什么程度。<button onClick={() => explain()}>整理需求对照</button></p>}
      {!main && !!items.length && <p className={styles.hint}>以下是关联业务能力的整体进展；一项能力可能由多条流程共同完成。</p>}
      {items.length > 1 && <div className={styles.tableWrap}><table aria-label="业务能力概览"><thead><tr><th>业务能力</th><th>现在可以做到什么</th><th>下一步</th></tr></thead><tbody>{items.map(item => <tr key={item.id} data-selected={selectedItem?.id === item.id}><td><button aria-pressed={selectedItem?.id === item.id} onClick={() => { setItemId(item.id); detailRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' }) }}>{item.title}</button><small>{availabilityNames[item.availability]} · {workNames[item.status]}</small></td><td>{item.summary || '尚未整理'}</td><td>{item.next_action || '尚未记录'}</td></tr>)}</tbody></table></div>}
      {(selectedItem ? [selectedItem] : []).map(item => <article ref={detailRef} key={item.id} className={styles.capability} aria-label={item.title + '需求对照'}>
        <div className={styles.sectionHeading}><h3>{item.title}</h3><div className={styles.badges}><span>{availabilityNames[item.availability]}</span><span>{workNames[item.status]}</span></div></div>
        <dl className={styles.comparison}><div><dt>要解决什么</dt><dd>{item.goal}</dd></div><div><dt>目前做到什么</dt><dd><MarkdownDocument source={item.summary} emptyLabel="尚未整理实际进展" /></dd></div><div><dt>差距与下一步</dt><dd>{item.blocker?.reason && <p>{item.blocker.reason}</p>}<p>{item.next_action || '尚未记录下一步'}</p></dd></div></dl>
        {!!item.requirements?.length ? <div className={styles.tableWrap}><table><thead><tr><th>原需求</th><th>当前实际情况</th><th>对照结论与差距</th></tr></thead><tbody>{item.requirements.map((check, index) => <tr key={index}>
          <td>{check.requirement}{check.source && <small>{check.source}</small>}</td><td>{check.current}{resultLinks(check.results)}</td><td><strong data-status={check.status}>{comparisonNames[check.status]}</strong><p>{check.gap || (check.status === 'met' ? '已满足上述范围，其他要求见对应条目。' : '尚未说明具体差距。')}</p></td>
        </tr>)}</tbody></table></div> : <div className={styles.unverified}>尚未逐项对照原需求；当前可用程度来自已有进展说明。<button onClick={() => explain(item)}>让统筹补充对照</button></div>}
        {!!item.workflow_ids.length && <div className={styles.memberLinks}><small>共同参与的流程</small>{topology.members.filter(m => item.workflow_ids.includes(m.id) && m.id !== projectId).map(m => <button key={m.id} onClick={() => onWorkflow(m.id)}>{m.name}</button>)}</div>}
        <div className={styles.actions}>{item.availability !== 'not_ready' && <button onClick={() => onTalk(item, `我想试用「${item.title}」，请说明需要哪份业务输入，并使用已有工作流处理。`)}>试用这项能力</button>}<button onClick={() => onTalk(item, '')}>反馈或继续完善</button></div>
        {!!item.results.length && <details><summary>已有结果与资料（{item.results.length}）</summary>{resultLinks(item.results)}</details>}
      </article>)}
    </section>}
    {view === 'path' && <section className={styles.execution} aria-label="实际处理路径"><div className={styles.sectionHeading}><h3>实际处理路径</h3><small>读取当前草稿的连接与分支 · r{flow?.revision ?? member?.revision}</small></div>
      <p>从上到下查看处理顺序，展开分支查看对应路径。点击调用步骤可查看成员职责与进展。</p>
      {flow ? <WorkflowRoute flow={flow} onWorkflow={onWorkflow}/> : <p>未能读取路径，请重新打开此页面或查看画布。</p>}
    </section>}
  </div>
}
