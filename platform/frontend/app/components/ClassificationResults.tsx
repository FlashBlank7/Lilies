import styles from './modeling.module.css'

type ClassResult = {
  label: string; samples: number; predicted_samples?: number; development_samples?: number
  precision: number | null; recall: number | null; f1: number | null
}
export type ClassificationResult = {
  classes: ClassResult[]; confusion_matrix?: number[][]; note?: string
  baseline?: { classes: ClassResult[] }
  folds?: { fold: number; classes: { label: string; train_samples: number; validation_samples: number }[] }[]
}

export default function ClassificationResults({ result, phase = 'validation' }: { result: ClassificationResult; phase?: 'validation' | 'test' }) {
  const stage = phase === 'test' ? '测试' : '验证'
  const value = (n: number | null | undefined) => n == null ? '无法计算' : n.toFixed(3)
  const baseline = new Map(result.baseline?.classes.map(row => [row.label, row]))
  return <section aria-label={`各类别${stage}结果`}>
    <h4>各类别{stage}结果</h4>
    <p>{phase === 'validation' ? '每条预测来自未用该样本训练的模型。' : ''}精确率表示判为这一类的结果有多少正确；召回率表示实际属于这一类的样本找回了多少。</p>
    <div className={styles.table}><table aria-label={`类别${stage}指标`}><thead><tr>
      <th scope="col">类别</th><th scope="col">{stage}样本</th><th scope="col">预测为此类</th><th scope="col">精确率</th><th scope="col">召回率</th><th scope="col">F1</th>
      {result.baseline && <th scope="col">基线召回率</th>}
    </tr></thead><tbody>{result.classes.map(row => <tr key={row.label}>
      <th scope="row">{row.label}</th><td>{row.samples}{row.samples === 0 && <small>缺少此类{stage}样本</small>}</td>
      <td>{row.predicted_samples ?? '未记录'}</td><td>{value(row.precision)}</td><td>{value(row.recall)}</td><td>{value(row.f1)}</td>
      {result.baseline && <td>{value(baseline.get(row.label)?.recall)}</td>}
    </tr>)}</tbody></table></div>
    <p>没有实际样本时，召回率无法计算；没有预测为该类别的结果时，精确率无法计算。样本很少时，单次分数不能说明稳定效果。</p>
    {!!result.folds?.length && <details><summary>各次验证的类别分布</summary>
      <div className={styles.table}><table aria-label="各次验证的类别分布"><thead><tr><th>折</th><th>类别</th><th>训练样本</th><th>验证样本</th></tr></thead>
        <tbody>{result.folds.flatMap(fold => fold.classes.map(row => <tr key={`${fold.fold}:${row.label}`}>
          <td>{fold.fold}</td><th scope="row">{row.label}</th><td>{row.train_samples}{row.train_samples === 0 && <small>训练时未见过此类</small>}</td>
          <td>{row.validation_samples}{row.validation_samples === 0 && <small>无法评价此类召回率</small>}</td>
        </tr>))}</tbody></table></div>
    </details>}
    {!!result.confusion_matrix?.length && <details><summary>查看混淆矩阵</summary>
      <p>行表示实际类别，列表示预测类别，数值为样本数量。</p>
      <div className={styles.table}><table aria-label={`${stage}混淆矩阵`}><thead><tr><th>实际 / 预测</th>{result.classes.map(row => <th key={row.label} scope="col">{row.label}</th>)}</tr></thead>
        <tbody>{result.confusion_matrix.map((counts, i) => <tr key={i}><th scope="row">{result.classes[i]?.label}</th>{counts.map((count, j) => <td key={j}>{count}</td>)}</tr>)}</tbody></table></div>
    </details>}
    {result.note && <p>{result.note}</p>}
  </section>
}
