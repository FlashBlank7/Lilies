'use client'

import {groupProjectFiles, type ProjectFile} from './project-files'
import {acceptsFile,fileExtensions,fileFormatError} from '@/lib/file-formats'

export default function ProjectFileField({name,label,value,files,disabled,onChange,accept}:{name:string;label:string;value:string;files:ProjectFile[];disabled:boolean;onChange:(value:string)=>void;accept?:string[]}) {
  const basename=(path:string)=>path.split('/').pop()||path
  const eligible=files.filter(file=>acceptsFile(file.path,accept))
  const extensions=fileExtensions(accept),error=fileFormatError(label.replace(/ \*$/, ''),value,accept)
  const helpId=`file-format-${name}`
  return <div>
    <label>{label}<select aria-label={`为 ${name} 选择项目文件`} aria-invalid={!!error} aria-describedby={extensions.length?helpId:undefined} disabled={disabled} value={value} onChange={e=>onChange(e.target.value)}>
      <option value="">不选择文件</option>
      {value&&!eligible.some(f=>f.path===value)&&<option value={value} disabled={!!error}>{basename(value)}（{error?'格式不支持，请重新选择':'当前填写的路径'}）</option>}
      {groupProjectFiles(eligible).map(group=><optgroup key={group.label} label={group.label}>{group.files.map(file=><option key={file.path} value={file.path}>{file.label}</option>)}</optgroup>)}
    </select></label>
    {extensions.length>0&&<p id={helpId} role={error?'alert':undefined}>{error||`支持 ${extensions.map(ext=>ext.slice(1).toUpperCase()).join('、')} 文件。`}{!error&&!eligible.length?' 项目中暂无符合格式的文件，请先在项目资料中添加。':''}</p>}
    <details><summary>查看或手动填写文件路径</summary><label>文件路径<input aria-label={name} aria-invalid={!!error} aria-describedby={extensions.length?helpId:undefined} disabled={disabled} value={value} onChange={e=>onChange(e.target.value)}/></label></details>
  </div>
}
