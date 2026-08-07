'use client'

import styles from './output-view.module.css'

/** 形状化输出：按值的形状选渲染，原始 JSON 永远收在折叠里备查。 */

type Rec = Record<string, unknown>

function isRecordArray(value: unknown): value is Rec[] {
  return Array.isArray(value)
    && value.length > 0
    && value.every(item => item !== null && typeof item === 'object' && !Array.isArray(item))
}

function columnsOf(rows: Rec[]): string[] {
  const seen: string[] = []
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!seen.includes(key)) seen.push(key)
    }
  }
  return seen.slice(0, 8)
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function RecordTable({ rows }: { rows: Rec[] }) {
  const columns = columnsOf(rows)
  const shown = rows.slice(0, 50)
  return <div className={styles.tableBox}>
    <table>
      <thead><tr>{columns.map(column => <th key={column}>{column}</th>)}</tr></thead>
      <tbody>
        {shown.map((row, index) => <tr key={index}>
          {columns.map(column => <td key={column}>{cell(row[column])}</td>)}
        </tr>)}
      </tbody>
    </table>
    {rows.length > shown.length && <p className={styles.more}>…共 {rows.length} 行</p>}
  </div>
}

function FieldValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className={styles.muted}>—</span>
  if (typeof value === 'boolean') {
    return <span className={value ? styles.yes : styles.no}>{value ? '是' : '否'}</span>
  }
  if (typeof value === 'number') return <span className={styles.num}>{String(value)}</span>
  if (typeof value === 'string') {
    if (value.length > 90 || value.includes('\n')) {
      return <div className={styles.prose}>{value}</div>
    }
    return <span>{value}</span>
  }
  if (isRecordArray(value)) return <RecordTable rows={value} />
  if (Array.isArray(value)) {
    return <span>{value.map(item => cell(item)).join('、') || '—'}</span>
  }
  return <pre className={styles.json}>{JSON.stringify(value, null, 2)}</pre>
}

export default function OutputView({ outputs }: { outputs: Rec }) {
  const entries = Object.entries(outputs)
  if (entries.length === 0) return <p className={styles.muted}>（没有输出字段）</p>
  return <div className={styles.view}>
    {entries.map(([key, value]) => <div className={styles.field} key={key}>
      <div className={styles.label}>{key}</div>
      <div className={styles.value}><FieldValue value={value} /></div>
    </div>)}
    <details className={styles.raw}>
      <summary>原始 JSON</summary>
      <pre>{JSON.stringify(outputs, null, 2)}</pre>
    </details>
  </div>
}
