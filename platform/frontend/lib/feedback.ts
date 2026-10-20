import {api} from './platform'

export const feedbackCategories = {result:'结果不对',usability:'操作不方便',runtime:'运行问题',idea:'功能建议',other:'其他'}
export const feedbackStatuses = {received:'已收到',working:'处理中',resolved:'已解决',declined:'暂不处理'}
export type FeedbackSource = {project_id?:string;conversation_id?:string;request_id?:string;task_id?:string;workflow_id?:string;page?:'general'|'conversation'|'workflow'|'run'}
export type FeedbackRow = {id:string;summary:string;user_name:string;category:keyof typeof feedbackCategories;status:keyof typeof feedbackStatuses;project_name:string;updated_at:string;revision:number;read_revision:number}
export type FeedbackDetail = FeedbackRow & {user_id:string;source:FeedbackSource;source_available:boolean;excerpt:string;messages:{id:string;user_id:string;user_name:string;role:string;text:string;status:keyof typeof feedbackStatuses|null;created_at:string;media_type:string}[]}

export async function postFeedback(path:string, payload:object, image:File|null) {
  const body=new FormData();body.append('payload',JSON.stringify(payload));if(image)body.append('image',image)
  const result=await api<{id:string}>(path,{method:'POST',body})
  window.dispatchEvent(new Event('lilies:feedback-changed'))
  return result
}

export function imageError(file:File|null) {
  if(!file)return ''
  if(!['image/png','image/jpeg','image/webp'].includes(file.type))return '请选择PNG、JPEG或WebP截图'
  return file.size>3*1024*1024?'截图不能超过3 MB':''
}
