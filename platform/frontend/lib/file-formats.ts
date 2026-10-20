export function fileExtensions(accept?:string[]):string[] {
  return [...new Set((accept||[]).filter(v=>typeof v==='string'&&/^\.[a-z0-9]+$/i.test(v)).map(v=>v.toLowerCase()))]
}

export function acceptsFile(path:string,accept?:string[]):boolean {
  const extensions=fileExtensions(accept)
  return !path.trim()||!extensions.length||extensions.some(ext=>path.trim().toLowerCase().endsWith(ext))
}

export function fileFormatError(label:string,path:string,accept?:string[]):string {
  return acceptsFile(path,accept)?'':`${label}不支持此文件格式，请选择 ${fileExtensions(accept).map(ext=>ext.slice(1).toUpperCase()).join('、')} 文件。`
}
