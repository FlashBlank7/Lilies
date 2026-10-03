'use client'

export default function ProjectFileField({name,label,value,files,disabled,onChange}:{name:string;label:string;value:string;files:{path:string}[];disabled:boolean;onChange:(value:string)=>void}) {
  const basename=(path:string)=>path.split('/').pop()||path
  return <div>
    <label>{label}<select aria-label={`为 ${name} 选择项目文件`} disabled={disabled} value={value} onChange={e=>onChange(e.target.value)}>
      <option value="">不选择文件</option>
      {value&&!files.some(f=>f.path===value)&&<option value={value}>{basename(value)}（当前填写的路径）</option>}
      {files.map(file=><option key={file.path} value={file.path}>{basename(file.path)}{files.filter(f=>basename(f.path)===basename(file.path)).length>1?` · ${file.path.split('/').slice(0,-1).join('/')}`:''}</option>)}
    </select></label>
    <details><summary>查看或手动填写文件路径</summary><label>文件路径<input aria-label={name} disabled={disabled} value={value} onChange={e=>onChange(e.target.value)}/></label></details>
  </div>
}
