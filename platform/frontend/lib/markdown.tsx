'use client'

import { useEffect, useMemo, useState, type ReactNode } from 'react'

type MarkdownBlock =
  | { kind: 'heading'; depth: number; text: string }
  | { kind: 'paragraph'; text: string }
  | { kind: 'blockquote'; text: string }
  | { kind: 'list'; ordered: boolean; items: string[] }
  | { kind: 'code'; language: string; code: string }
  | { kind: 'table'; header: string[]; rows: string[][] }
  | { kind: 'rule' }

export type MarkdownDocumentProps = {
  source: string
  emptyLabel: string
  compact?: boolean
  className?: string
}

export type MarkdownResultCardProps = MarkdownDocumentProps & {
  title: string
  description: string
  openLabel: string
  closeLabel: string
  rawLabel?: string
  rawSource?: string
  dataSurface?: string
}

function isBlank(line: string) {
  return line.trim() === ''
}

function isFence(line: string) {
  return /^```/.test(line.trim())
}

function isHeading(line: string) {
  return /^#{1,6}\s+/.test(line.trim())
}

function isRule(line: string) {
  return /^(\*\s*){3,}$|^(-\s*){3,}$|^(_\s*){3,}$/.test(line.trim())
}

function isBlockquote(line: string) {
  return /^\s*>\s?/.test(line)
}

function isListItem(line: string) {
  return /^\s*(?:[-*+]\s+|\d+[.)]\s+)/.test(line)
}

function isTableSeparator(line: string) {
  const cells = splitTableRow(line)
  return cells.length > 1 && cells.every(cell => /^:?-{3,}:?$/.test(cell.trim()))
}

function splitTableRow(line: string) {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '')
  return trimmed.split('|').map(cell => cell.trim())
}

function isTableStart(lines: string[], index: number) {
  return index + 1 < lines.length && lines[index].includes('|') && isTableSeparator(lines[index + 1])
}

function startsBlock(lines: string[], index: number) {
  const line = lines[index] || ''
  return isBlank(line) || isFence(line) || isHeading(line) || isRule(line) || isBlockquote(line) || isListItem(line) || isTableStart(lines, index)
}

function normalizeSerializedMarkdown(source: string) {
  const normalized = source.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  const hasEscapedBlockStructure = /(?:^|\\n)(?:#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```)/.test(normalized)
  if (!hasEscapedBlockStructure) return normalized
  return normalized.replace(/\\r\\n|\\n|\\r/g, '\n')
}

function parseMarkdownBlocks(source: string): MarkdownBlock[] {
  const lines = normalizeSerializedMarkdown(source).split('\n')
  const blocks: MarkdownBlock[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]
    if (isBlank(line)) {
      index += 1
      continue
    }

    if (isFence(line)) {
      const language = line.trim().replace(/^```/, '').trim()
      const code: string[] = []
      index += 1
      while (index < lines.length && !isFence(lines[index])) {
        code.push(lines[index])
        index += 1
      }
      if (index < lines.length) index += 1
      blocks.push({ kind: 'code', language, code: code.join('\n') })
      continue
    }

    const heading = line.trim().match(/^(#{1,6})\s+(.+)$/)
    if (heading) {
      blocks.push({ kind: 'heading', depth: heading[1].length, text: heading[2] })
      index += 1
      continue
    }

    if (isRule(line)) {
      blocks.push({ kind: 'rule' })
      index += 1
      continue
    }

    if (isTableStart(lines, index)) {
      const header = splitTableRow(lines[index])
      const rows: string[][] = []
      index += 2
      while (index < lines.length && lines[index].includes('|') && !isBlank(lines[index])) {
        rows.push(splitTableRow(lines[index]))
        index += 1
      }
      blocks.push({ kind: 'table', header, rows })
      continue
    }

    if (isBlockquote(line)) {
      const quoteLines: string[] = []
      while (index < lines.length && isBlockquote(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ''))
        index += 1
      }
      blocks.push({ kind: 'blockquote', text: quoteLines.join('\n') })
      continue
    }

    if (isListItem(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line)
      const items: string[] = []
      while (index < lines.length && isListItem(lines[index])) {
        const nextOrdered = /^\s*\d+[.)]\s+/.test(lines[index])
        if (nextOrdered !== ordered) break
        items.push(lines[index].replace(/^\s*(?:[-*+]\s+|\d+[.)]\s+)/, ''))
        index += 1
      }
      blocks.push({ kind: 'list', ordered, items })
      continue
    }

    const paragraph: string[] = []
    while (index < lines.length && !startsBlock(lines, index)) {
      paragraph.push(lines[index])
      index += 1
    }
    blocks.push({ kind: 'paragraph', text: paragraph.join('\n') })
  }

  return blocks
}

function safeHref(value: string) {
  const trimmed = value.trim()
  if (/^(https?:|mailto:)/i.test(trimmed) || trimmed.startsWith('/') || trimmed.startsWith('#')) return trimmed
  return ''
}

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = []
  let buffer = ''
  let index = 0

  const pushText = () => {
    if (!buffer) return
    nodes.push(buffer)
    buffer = ''
  }

  while (index < text.length) {
    if (text[index] === '`') {
      const end = text.indexOf('`', index + 1)
      if (end > index + 1) {
        pushText()
        nodes.push(<code key={`${keyPrefix}-code-${index}`}>{text.slice(index + 1, end)}</code>)
        index = end + 1
        continue
      }
    }

    if (text[index] === '[') {
      const labelEnd = text.indexOf(']', index + 1)
      const hrefStart = labelEnd >= 0 ? text.indexOf('(', labelEnd) : -1
      const hrefEnd = hrefStart === labelEnd + 1 ? text.indexOf(')', hrefStart + 1) : -1
      if (labelEnd > index && hrefEnd > hrefStart) {
        const href = safeHref(text.slice(hrefStart + 1, hrefEnd))
        pushText()
        if (href) {
          nodes.push(<a key={`${keyPrefix}-link-${index}`} href={href} rel="noreferrer" target={href.startsWith('http') ? '_blank' : undefined}>{renderInline(text.slice(index + 1, labelEnd), `${keyPrefix}-link-${index}`)}</a>)
        } else {
          nodes.push(text.slice(index, hrefEnd + 1))
        }
        index = hrefEnd + 1
        continue
      }
    }

    const strongMarker = text.startsWith('**', index) ? '**' : text.startsWith('__', index) ? '__' : ''
    if (strongMarker) {
      const end = text.indexOf(strongMarker, index + 2)
      if (end > index + 2) {
        pushText()
        nodes.push(<strong key={`${keyPrefix}-strong-${index}`}>{renderInline(text.slice(index + 2, end), `${keyPrefix}-strong-${index}`)}</strong>)
        index = end + 2
        continue
      }
    }

    const marker = text[index]
    if (marker === '*' || marker === '_') {
      const end = text.indexOf(marker, index + 1)
      if (end > index + 1) {
        pushText()
        nodes.push(<em key={`${keyPrefix}-em-${index}`}>{renderInline(text.slice(index + 1, end), `${keyPrefix}-em-${index}`)}</em>)
        index = end + 1
        continue
      }
    }

    buffer += text[index]
    index += 1
  }

  pushText()
  return nodes
}

function headingElement(depth: number, children: ReactNode, key: string) {
  if (depth === 1) return <h1 key={key}>{children}</h1>
  if (depth === 2) return <h2 key={key}>{children}</h2>
  if (depth === 3) return <h3 key={key}>{children}</h3>
  if (depth === 4) return <h4 key={key}>{children}</h4>
  if (depth === 5) return <h5 key={key}>{children}</h5>
  return <h6 key={key}>{children}</h6>
}

function renderBlock(block: MarkdownBlock, index: number) {
  const key = `markdown-block-${index}`
  if (block.kind === 'heading') return headingElement(block.depth, renderInline(block.text, key), key)
  if (block.kind === 'paragraph') return <p key={key}>{renderInline(block.text, key)}</p>
  if (block.kind === 'blockquote') return <blockquote key={key}>{renderInline(block.text, key)}</blockquote>
  if (block.kind === 'list') {
    const items = block.items.map((item, itemIndex) => <li key={`${key}-${itemIndex}`}>{renderInline(item, `${key}-${itemIndex}`)}</li>)
    return block.ordered ? <ol key={key}>{items}</ol> : <ul key={key}>{items}</ul>
  }
  if (block.kind === 'code') return <pre key={key} data-language={block.language || undefined}><code>{block.code}</code></pre>
  if (block.kind === 'table') {
    return <div className="markdown-table-wrap" key={key}><table>
      <thead><tr>{block.header.map((cell, cellIndex) => <th key={`${key}-h-${cellIndex}`}>{renderInline(cell, `${key}-h-${cellIndex}`)}</th>)}</tr></thead>
      <tbody>{block.rows.map((row, rowIndex) => <tr key={`${key}-r-${rowIndex}`}>{block.header.map((_, cellIndex) => <td key={`${key}-r-${rowIndex}-${cellIndex}`}>{renderInline(row[cellIndex] || '', `${key}-r-${rowIndex}-${cellIndex}`)}</td>)}</tr>)}</tbody>
    </table></div>
  }
  return <hr key={key} />
}

export function MarkdownDocument({ source, emptyLabel, compact = false, className = '' }: MarkdownDocumentProps) {
  const blocks = useMemo(() => parseMarkdownBlocks(source.trim()), [source])
  const classes = ['markdown-document', compact ? 'compact' : '', className].filter(Boolean).join(' ')
  if (!blocks.length) return <p className="markdown-empty">{emptyLabel}</p>
  return <div className={classes}>{blocks.map(renderBlock)}</div>
}

export function MarkdownResultCard({
  source,
  emptyLabel,
  compact = true,
  className = '',
  title,
  description,
  openLabel,
  closeLabel,
  rawLabel,
  rawSource,
  dataSurface = 'markdown-result',
}: MarkdownResultCardProps) {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open])

  return <section className={`markdown-result-card ${className}`} data-markdown-surface={dataSurface}>
    <div className="markdown-result-card-head">
      <div><strong>{title}</strong><small>{description}</small></div>
      <button className="markdown-result-action" type="button" onClick={() => setOpen(true)}>{openLabel}</button>
    </div>
    <MarkdownDocument source={source} emptyLabel={emptyLabel} compact={compact} />
    {rawLabel && rawSource && <details className="markdown-raw-details"><summary>{rawLabel}</summary><pre>{rawSource}</pre></details>}
    {open && <div className="markdown-dialog-backdrop" role="presentation" onClick={() => setOpen(false)}>
      <section aria-label={title} aria-modal="true" className="markdown-dialog" role="dialog" onClick={event => event.stopPropagation()}>
        <div className="markdown-dialog-head"><strong>{title}</strong><button type="button" onClick={() => setOpen(false)}>{closeLabel}</button></div>
        <MarkdownDocument source={source} emptyLabel={emptyLabel} />
      </section>
    </div>}
  </section>
}
