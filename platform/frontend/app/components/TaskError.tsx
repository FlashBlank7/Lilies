'use client'

/** Keep the actionable final exception visible, including multi-line details. */
export default function TaskError({error}:{error?:string}) {
  if (!error?.trim()) return null
  const full=error.trim()
  const traceback=full.includes('Traceback (most recent call last)')
  const matches=[...full.matchAll(/^([A-Za-z_][\w.]*(?:Error|Exception)|Exception|KeyboardInterrupt):[ \t]*/gm)]
  const last=matches.at(-1)
  const reason=traceback
    ? last ? full.slice(last.index!+last[0].length).trim() || last[1] : '执行失败，请展开查看错误详情。'
    : full
  const summary=reason.length>1000 ? reason.slice(0,1000)+'…（完整内容见错误详情）' : reason
  return <div>
    <p role="alert" style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{summary}</p>
    {summary!==full && <details><summary>错误详情</summary><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{full}</pre></details>}
  </div>
}
