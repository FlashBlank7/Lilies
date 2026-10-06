'use client'
import {useState} from 'react'
import {projectFilePathFromLink,resolveProjectLink} from '@/lib/project-links'

export default function ProjectImage({projectId,path,label}:{projectId:string;path:string;label:string}){
  const url=resolveProjectLink(projectId,path)
  const valid=projectFilePathFromLink(projectId,url)===path&&/\.(png|jpe?g|webp|svg)$/i.test(path)
  if(!valid)return <p role="alert">图片必须是当前项目的PNG、JPEG、WebP或SVG文件。</p>
  return <ProjectImageFile key={url} url={url} label={label}/>
}

function ProjectImageFile({url,label}:{url:string;label:string}){
  const [failed,setFailed]=useState(false)
  return <figure style={{margin:'12px 0',minWidth:0}}>
    <figcaption>{label}</figcaption>
    {failed?<p role="alert">图片读取失败，请检查文件或项目权限；不要凭缺失图片猜测结论。</p>
      :<img src={url} alt={label} onError={()=>setFailed(true)} style={{display:'block',maxWidth:'100%',maxHeight:440,objectFit:'contain',margin:'8px 0'}}/>}
    <a href={`${url}${url.includes('?')?'&':'?'}download=1`} download>下载图片：{label}</a>
  </figure>
}
