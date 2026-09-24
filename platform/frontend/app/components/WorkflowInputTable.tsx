'use client'

export type InputColumn = {name: string; label?: string; type?: 'string' | 'number' | 'boolean' | 'file'; options?: string[]; default?: unknown}

export default function WorkflowInputTable({name, label, columns, value, disabled, files=[], onChange}: {
  name: string; label: string; columns: InputColumn[]; value: string; disabled?: boolean; files?: {path:string}[]; onChange: (value: string) => void
}) {
  let rows: Record<string,unknown>[]
  try {
    const parsed=JSON.parse(value || '[]')
    if (!Array.isArray(parsed) || parsed.some(row=>!row || typeof row!=='object' || Array.isArray(row))) throw new Error()
    rows=parsed
  } catch {
    return <div><p role="alert">已有内容不是可编辑的行表，请修正高级内容。</p><textarea aria-label={`${label}高级内容`} value={value} disabled={disabled} onChange={e=>onChange(e.target.value)}/></div>
  }
  function update(index: number, field: InputColumn, raw: string) {
    const next=field.type==='number' && raw!=='' ? Number(raw) : field.type==='boolean' && raw!=='' ? raw==='true' : raw
    onChange(JSON.stringify(rows.map((row,i)=>i===index?{...row,[field.name]:next}:row)))
  }
  function move(index: number, step: number) {
    const next=[...rows];[next[index],next[index+step]]=[next[index+step],next[index]];onChange(JSON.stringify(next))
  }
  return <fieldset disabled={disabled} aria-label={label} style={{minWidth:0,border:'1px solid var(--ui-line)',borderRadius:8,padding:12}}>
    <legend>{label}</legend>
    {!rows.length && <p>尚未添加，可按需添加一行。</p>}
    {rows.map((row,i)=><fieldset key={i} style={{minWidth:0,border:'1px solid var(--ui-line)',borderRadius:6,marginBottom:12,padding:12}}>
      <legend>第 {i+1} 行</legend>
      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(min(100%,180px),1fr))',gap:12}}>
        {columns.map(column=>{const title=`${label}第${i+1}行${column.label||column.name}`;return <label key={column.name} style={{display:'grid',gap:6}}>{column.label||column.name}
          {column.type==='file'
            ? <select aria-label={title} value={String(row[column.name]??'')} onChange={e=>update(i,column,e.target.value)}><option value="">选择项目文件…</option>{Boolean(row[column.name])&&!files.some(f=>f.path===row[column.name])&&<option value={String(row[column.name])}>{String(row[column.name])}（当前文件列表中未找到）</option>}{files.map(file=><option key={file.path} value={file.path}>{file.path}</option>)}</select>
            : column.options?.length || column.type==='boolean'
            ? <select aria-label={title} value={String(row[column.name]??'')} onChange={e=>update(i,column,e.target.value)}><option value="">请选择</option>{(column.type==='boolean'?['true','false']:column.options!).map(v=><option key={v} value={v}>{column.type==='boolean'?(v==='true'?'是':'否'):v}</option>)}</select>
            : <input aria-label={title} type={column.type==='number'?'number':'text'} step={column.type==='number'?'any':undefined} value={String(row[column.name]??'')} onChange={e=>update(i,column,e.target.value)}/>}</label>})}
      </div>
      <div style={{display:'flex',gap:8,marginTop:10,flexWrap:'wrap'}}>
        <button type="button" aria-label={`${label}第${i+1}行上移`} disabled={disabled||i===0} onClick={()=>move(i,-1)}>上移</button>
        <button type="button" aria-label={`${label}第${i+1}行下移`} disabled={disabled||i===rows.length-1} onClick={()=>move(i,1)}>下移</button>
        <button type="button" aria-label={`${label}删除第${i+1}行`} onClick={()=>onChange(JSON.stringify(rows.filter((_,index)=>index!==i)))}>删除</button>
      </div>
    </fieldset>)}
    <button type="button" aria-label={`${label}添加一行`} onClick={()=>onChange(JSON.stringify([...rows,Object.fromEntries(columns.map(c=>[c.name,c.default??'']))]))}>添加一行</button>
    <details style={{marginTop:12}}><summary>高级内容</summary><textarea aria-label={`${name}高级内容`} value={value} onChange={e=>onChange(e.target.value)} /></details>
  </fieldset>
}
