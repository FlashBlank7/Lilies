'use client'

import { useEffect } from 'react'

export default function ErrorBoundary({ error, reset }: { error: Error & { digest?: string }, reset: () => void }) {
  useEffect(() => {
    console.error(error)
  }, [error])

  return (
    <main className="home-shell">
      <section className="hero">
        <div className="eyebrow">Studio runtime error</div>
        <h1>页面没有崩掉，<br/><em>只是抓到一个前端错误。</em></h1>
        <p>{error.message || 'Unknown frontend error'}</p>
        <div className="create-footer">
          <span>请先点击重试；如果仍出现，把这段错误发给 Codex。</span>
          <button onClick={reset}>重试渲染 →</button>
        </div>
      </section>
    </main>
  )
}
