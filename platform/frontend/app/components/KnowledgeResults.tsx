'use client'

export type KnowledgeResult = { citation: string; title: string; text: string; source_path: string; score: number; location: { page?: number; paragraph?: number; line?: number; start: number; end: number } }
export type KnowledgeSearchResult = { results: KnowledgeResult[]; version: string; retrieved_count: number }

export function isKnowledgeSearchResult(value: Record<string, unknown>): value is Record<string, unknown> & KnowledgeSearchResult {
  return typeof value.knowledge_ref === 'string' && typeof value.version === 'string' && typeof value.retrieved_count === 'number' && Array.isArray(value.results) && value.results.every(item =>
    item && typeof item === 'object' && typeof item.citation === 'string' && typeof item.text === 'string' && typeof item.title === 'string' && typeof item.score === 'number' && item.location && typeof item.location === 'object')
}

export default function KnowledgeResults({result, onFile}: {result: KnowledgeSearchResult; onFile?: (path: string) => void}) {
  return <section aria-label="知识检索结果">
    <h3>检索原文与出处</h3>
    <p>{result.retrieved_count} 条结果 · 索引版本 {result.version}</p>
    {!result.results.length && <p>没有达到相似度要求的资料，请调整问题或补充资料。</p>}
    {result.results.map(r => <article key={r.citation}>
      <h4>{r.citation} {r.title}</h4>
      <p>{r.location.page ? `第 ${r.location.page} 页` : r.location.paragraph ? `第 ${r.location.paragraph} 段` : `第 ${r.location.line || 1} 行起`} · 相似度 {r.score.toFixed(3)}</p>
      <pre style={{whiteSpace:'pre-wrap', overflowWrap:'anywhere'}}>{r.text}</pre>
      {r.source_path && onFile && <button onClick={() => onFile(r.source_path)}>查看原资料</button>}
    </article>)}
    <a download={`knowledge-${result.version}.json`} href={'data:application/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(result, null, 2))}>下载检索结果与出处 JSON ↓</a>
  </section>
}
