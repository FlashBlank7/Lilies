type Pair = {left:string;right:string;rho:number|null;p_value:number|null;samples:number;reason?:string}
export type DataAnalysisResult = {
  operation:'numeric'|'spearman'|'outliers';rows:number;output_rows?:number;affected_rows?:number;converted_cells?:number;
  handling?:string;columns?:string[];bounds?:Record<string,{lower:number|null;upper:number|null}>;
  pairs?:Pair[];warnings?:string[];
}
const number = (value:number|null|undefined) => value == null ? '无法计算' : Number(value).toPrecision(6)

export default function DataAnalysisResults({result}:{result:DataAnalysisResult}) {
  const title = {numeric:'数值整理',spearman:'Spearman 相关分析',outliers:'异常值处理'}[result.operation]
  return <section aria-label={title}>
    <h3>{title}</h3>
    <p>输入 {result.rows} 行{result.output_rows!=null && ` · 输出 ${result.output_rows} 行`}</p>
    {result.columns?.length ? <p>字段：{result.columns.join('、')}</p> : null}
    {result.operation==='numeric' && <p>转换 {result.converted_cells ?? 0} 个单元格，涉及 {result.affected_rows ?? 0} 行；原文件保留。</p>}
    {result.operation==='outliers' && <>
      <p>按所选规则识别 {result.affected_rows ?? 0} 行 · {{flag:'只标记，保留全部数据',drop:'删除标记行，另存结果',clip:'截到边界，另存结果'}[result.handling || ''] || result.handling}</p>
      {result.bounds && <div style={{overflowX:'auto'}}><table><thead><tr><th>字段</th><th>下限</th><th>上限</th></tr></thead><tbody>
        {Object.entries(result.bounds).map(([column,bound])=><tr key={column}><td>{column}</td><td>{bound.lower==null?'未设置':number(bound.lower)}</td><td>{bound.upper==null?'未设置':number(bound.upper)}</td></tr>)}
      </tbody></table></div>}
    </>}
    {result.pairs?.length ? <div style={{overflowX:'auto'}}><table><thead><tr><th>字段对</th><th>相关系数</th><th>p 值</th><th>有效样本</th><th>说明</th></tr></thead><tbody>
      {result.pairs.map(pair=><tr key={JSON.stringify([pair.left,pair.right])}><td>{pair.left} / {pair.right}</td><td>{number(pair.rho)}</td><td>{number(pair.p_value)}</td><td>{pair.samples}</td><td>{pair.reason || '—'}</td></tr>)}
    </tbody></table></div> : null}
    {result.warnings?.map((message,i)=><p key={i}>{message}</p>)}
  </section>
}
