'use client'

import {useCallback, useEffect, useRef, useState} from 'react'
import {api} from '@/lib/platform'
import ProjectMaterials from './ProjectMaterials'
import styles from './workspace-tools.module.css'

type Workflow = {id:string;name:string;description:string;revision:number;node_count:number;allowed:boolean;inputs:{name:string;description?:string}[]}
type Space = {workflows:Workflow[];files:{path:string;size:number}[];files_truncated:boolean}
export default function ProjectSpace({projectId,onWorkflow,onFile,onTalk,onChanged}:{projectId:string;onWorkflow:(id:string)=>void;onFile:(path:string)=>void;onTalk:(message:string,mode?:'task'|'workflow')=>void;onChanged:()=>unknown}) {
  const base=`/api/v1/projects/${projectId}`
  const [space,setSpace]=useState<Space>()
  const [name,setName]=useState('')
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState('')
  const [selected,setSelected]=useState<string[]>([])
  const upload=useRef<HTMLInputElement>(null)
  const refresh=useCallback(async()=>{try{setSpace(await api<Space>(base+'/space'));setError('')}catch(e){setError(String(e))}},[base])
  useEffect(()=>{void refresh()},[refresh])
  async function add(file?:File) {
    setBusy(true);setError('')
    try {
      let id:string
      if(file){
        if(file.size>5*1024*1024)throw new Error('工作流定义文件需小于 5 MB')
        const value=JSON.parse(await file.text())
        const workflow=value.snapshot?.workflow || value.workflow || value
        if(!Array.isArray(workflow.nodes)||!Array.isArray(workflow.edges))throw new Error('请选择包含节点和连线的工作流 JSON 文件')
        const result=await api<{workflow_id:string}>(base+'/space/workflows',{method:'POST',body:JSON.stringify({name:name.trim()||value.snapshot?.name||value.name||file.name.replace(/\.json$/i,''),description:value.snapshot?.description||value.description||'',workflow})})
        id=result.workflow_id
      }else{
        id=(await api<{id:string}>(base+'/members',{method:'POST',body:JSON.stringify({name:name.trim()})})).id
      }
      setName('');await refresh();void onChanged();onWorkflow(id)
    }catch(e){setError(String(e))}finally{setBusy(false)}
  }
  const fileContext=selected.length?`\n本次资料：\n${selected.map(p=>'- '+p).join('\n')}`:''
  return <div aria-label="项目空间">
    <section className={styles.section}><h2>本项目的工作环境</h2><p>把工作流和资料放在这里，项目智能体就能发现并使用。每次处理产生独立运行结果，成员共享项目资源，各自对话独立。</p>
      <div className={styles.row}><button onClick={()=>onTalk('请查看项目空间中的已有工作流和资料，帮我选择合适的流程处理这次任务。'+fileContext)}>与智能体完成任务</button><button onClick={()=>onTalk('请根据当前项目已有能力，创建一条新的可复用工作流。'+fileContext,'workflow')}>通过对话创建工作流</button><button onClick={()=>void refresh()}>刷新空间</button></div>
    </section>
    <section className={styles.section}><h2>可供调用的工作流</h2><p>加入后保存在当前项目，智能体按需查看输入并调用；模型或数据可以稍后配置。</p>
      <div className={styles.row}><label>工作流名称<input aria-label="加入空间的工作流名称" value={name} onChange={e=>setName(e.target.value)} maxLength={100}/></label><button disabled={busy||!name.trim()} onClick={()=>void add()}>添加空白流程</button><button disabled={busy} onClick={()=>upload.current?.click()}>导入已有工作流</button>
        <input hidden type="file" accept=".json,application/json" ref={upload} aria-label="导入工作流定义" onChange={e=>{const file=e.target.files?.[0];e.target.value='';if(file)void add(file)}}/></div>
      <small>导入工作流 JSON 定义；所需模型、连接和子流程仍使用本项目资源。</small>
      {!space ? <p role="status">正在读取工作流…</p> : !space.workflows.length ? <p>尚无工作流，可以先添加或通过对话创建。</p> : <table className={styles.table}><thead><tr><th>工作流</th><th>输入与状态</th><th>操作</th></tr></thead><tbody>{space.workflows.map(w=><tr key={w.id}><td><strong>{w.name}</strong><p>{w.description || '打开画布补充步骤和用途。'}</p></td><td>{w.inputs.map(i=>i.name).join('、')||'未声明输入'}<p>{!w.allowed?'项目未允许此流程中的能力':w.node_count?`${w.node_count} 个节点 · 修订 ${w.revision}`:'空白草稿'}</p></td><td><div className={styles.row}><button disabled={!w.allowed||!w.node_count} onClick={()=>onTalk(`请调用项目工作流「${w.name}」（${w.id}）。先查看输入要求，结合本次资料填写；无法确定的信息再向我询问。${fileContext}`)}>让智能体调用</button><button onClick={()=>onWorkflow(w.id)}>查看与编辑</button></div></td></tr>)}</tbody></table>}
    </section>
    <section className={styles.section}><h2>待处理文件</h2><ProjectMaterials id={projectId} onOpenFile={onFile} onChanged={()=>{void refresh();void onChanged()}} />
      {!!space?.files.length && <><h3>选择本次任务的资料</h3>{space.files.map(f=><label key={f.path} className={styles.fileChoice}><input type="checkbox" checked={selected.includes(f.path)} onChange={e=>setSelected(p=>e.target.checked?[...p,f.path]:p.filter(x=>x!==f.path))}/>{f.path}</label>)}<button disabled={!selected.length} onClick={()=>onTalk('请用项目中合适的已有工作流处理以下资料。'+fileContext)}>带着所选资料开始对话</button></>}
      {space?.files_truncated&&<p>文件较多，当前显示部分文件；智能体可按目录继续查找。</p>}
    </section>
    {error&&<p role="alert">{error}</p>}
  </div>
}
