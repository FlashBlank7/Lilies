'use client'

type FeatureResult = {
  rows: number; columns: number; source_rows: number; note: string; row_key?: string
  excluded: {reason: string; rows: number}[]
  preview: Record<string, unknown>[]
  features: {name: string; source: string; calculation: string; condition?: string}[]
}

export default function FeatureResults({result}: {result: FeatureResult}) {
  const rows = result.preview.slice(0, 12)
  const columns = rows.length ? Object.keys(rows[0]).slice(0, 11) : []
  return <section aria-label="制备后的样本与特征">
    <h3>样本与特征</h3>
    <p>原始样本 {result.source_rows} 条，保留 {result.rows} 条，生成 {result.columns} 个特征。</p>
    {result.excluded.filter(item => item.rows > 0).map(item => <p key={item.reason}>{item.reason}：排除 {item.rows} 条。</p>)}
    <p>{result.note}</p>
    {!!rows.length && <div style={{overflowX: 'auto'}}><table><thead><tr>{columns.map(key => <th key={key}>{key === result.row_key ? '样本序号' : key}</th>)}</tr></thead>
      <tbody>{rows.map((row, i) => <tr key={i}>{columns.map(key => <td key={key}>{row[key] == null ? '缺失' : typeof row[key] === 'number' ? String(Number(row[key].toPrecision(6))) : String(row[key])}</td>)}</tr>)}</tbody></table>
      <p>预览前 {rows.length} 行、最多 10 个特征；完整数据及标签见下方下载。</p></div>}
    <details><summary>查看特征来源与计算方式</summary><table><thead><tr><th>特征</th><th>来源</th><th>计算方式</th></tr></thead>
      <tbody>{result.features.slice(0, 50).map(item => <tr key={item.name}><td>{item.name}</td><td>{item.source}</td><td>{item.calculation}{item.condition ? ` · ${item.condition}` : ''}</td></tr>)}</tbody></table>
      {result.features.length > 50 && <p>显示前 50 个特征，完整定义见下载说明。</p>}
    </details>
  </section>
}
