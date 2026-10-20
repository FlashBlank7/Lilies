'use client'

import {MarkdownDocument} from '@/lib/markdown'

export type KnowledgeResult = { citation: string; title: string; text: string; source_path: string; score: number; location: { page?: number; paragraph?: number; line?: number; start: number; end: number } }
export type KnowledgeSearchResult = { results: KnowledgeResult[]; version: string; retrieved_count: number }

export function isKnowledgeSearchResult(value: Record<string, unknown>): value is Record<string, unknown> & KnowledgeSearchResult {
  return typeof value.knowledge_ref === 'string' && typeof value.version === 'string' && typeof value.retrieved_count === 'number' && Array.isArray(value.results) && value.results.every(item =>
    item && typeof item === 'object' && typeof item.citation === 'string' && typeof item.text === 'string' && typeof item.title === 'string' && typeof item.score === 'number' && item.location && typeof item.location === 'object')
}

export default function KnowledgeResults({result, onFile, answer, question}: {result: KnowledgeSearchResult; onFile?: (path: string) => void; answer?: string; question?: string}) {
  const cited = [...new Set((answer || '').match(/\[\d+\]/g) || [])]
  const unknown = cited.filter(citation => !result.results.some(item => item.citation === citation))
  const download = answer === undefined ? result : {question, answer, knowledge: result}
  const report = answer === undefined ? '' : `# 知识库回答\n\n${question || ''}\n\n${answer}\n\n## 检索原文与出处\n\n索引版本：${result.version}\n\n` + result.results.map(r => `${r.citation} ${r.title}（${r.location.page ? `第 ${r.location.page} 页` : r.location.paragraph ? `第 ${r.location.paragraph} 段` : `第 ${r.location.line || 1} 行起`}）\n\n${r.text}`).join('\n\n')
  return <section aria-label="知识检索结果">
    {answer !== undefined && <>
      <h3>知识库回答</h3>
      <MarkdownDocument source={answer} emptyLabel="模型没有返回回答正文。" />
      {!!unknown.length && <p role="alert">回答中的引用 {unknown.join('、')} 没有对应的检索原文，请核对回答。</p>}
      {!cited.length && !!result.results.length && <p role="status">回答未标注原文引用，请结合下方原文核对。</p>}
      <a download={`knowledge-answer-${result.version}.md`} href={'data:text/markdown;charset=utf-8,' + encodeURIComponent(report)}>下载回答与出处 Markdown ↓</a>
    </>}
    <h3>检索原文与出处</h3>
    <p>{result.retrieved_count} 条结果 · 索引版本 {result.version}</p>
    {!result.results.length && <p>没有达到相似度要求的资料，请调整问题或补充资料。</p>}
    {result.results.map(r => <article key={r.citation}>
      <h4>{r.citation} {r.title}</h4>
      <p>{r.location.page ? `第 ${r.location.page} 页` : r.location.paragraph ? `第 ${r.location.paragraph} 段` : `第 ${r.location.line || 1} 行起`} · 相似度 {r.score.toFixed(3)}</p>
      <pre style={{whiteSpace:'pre-wrap', overflowWrap:'anywhere'}}>{r.text}</pre>
      {r.source_path && onFile && <button onClick={() => onFile(r.source_path)}>查看原资料</button>}
    </article>)}
    <a download={`knowledge-${result.version}.json`} href={'data:application/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(download, null, 2))}>{answer === undefined ? '下载检索结果与出处 JSON ↓' : '下载回答与出处 JSON ↓'}</a>
  </section>
}
