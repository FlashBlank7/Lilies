export type FieldRule = {dtype?: 'number'|'category'|'text';description?:string;unit?:string;meaning?:'known'|'pending';intervals?:'pending'|'midpoint'|'lower'|'upper';categories?:string[];category_labels?:Record<string,string>;ordered_values?:string[];missing_tokens?:Record<string,string>;pending_tokens?:string[];allow_missing?:string[];lower?:number|null;upper?:number|null}
export type Mapping = {kind?:string;sheet?:number|string;header_row?:number;skip_after_header?:number;target?:string;id_column?:string;group_column?:string;invalid_rows?:string;field_rules?:Record<string,FieldRule>;[key:string]:unknown}
export type DataObject = {id:string;version:string;kind:string;name:string;path?:string;format?:string;bytes?:number;dataset_id?:string;mapping?:Mapping;source_id?:string;operation_id?:string;value?:Record<string,unknown>;metrics?:Record<string,number>;features?:{columns?:string[]};binding?:{study_id:string;candidate_id:string;slot:number};classification?:unknown;test_result?:{metrics?:Record<string,number>;rows?:number};download?:string}
export type Filter = {column:string;operator:string;value?:string|number|null}
export type Selection = {columns:string[];filters:Filter[];row_indices:number[]|null}
export const emptySelection = ():Selection => ({columns:[],filters:[],row_indices:null})
export type TableView = {object:DataObject;columns:string[];selected_columns:string[];rows:{row_id:number;source_row:number;source_line:number;values:Record<string,unknown>;raw:Record<string,unknown>}[];total_rows:number;selected_rows:number;offset:number;limit:number;mapping:Mapping;sheets:string[];statistics:{name:string;numeric:boolean;unavailable:number;rule:FieldRule;distribution?:{label:string;count:number}[];categories_truncated?:boolean}[];points:{x:number;y:number;row_id:number}[];points_sampled:boolean;states:{counts?:Record<string,number>};cells:{row:number;column:string;status:string;reason:string;raw:string;value:unknown}[]}
export type Input = {name:string;label?:string;description?:string;type:string;required?:boolean;default?:unknown;resource_kind?:string;purpose?:string;accepted_formats?:string[];required_columns?:string[];options?:string[]}
export type Tool = {id:string;name:string;description:string;revision?:number;inputs?:Input[];allowed?:boolean;outputs?:string[]}
export type Operation = {id:string;name:string;source:{id:string;version:string};status:string;error:string;task_id:string;workflow_id:string;dataset_id?:string;created_at:string;request:{tool:string;selection:Selection;mapping?:Mapping;parameters:Record<string,unknown>;model?:{id:string;version:string};bindings?:Record<string,{id:string;version:string}>};results:DataObject[];inputs?:{role:string;id:string;version:string;name:string}[];task?:{created_at:string;updated_at:string}}
export const TOOL_MIME = 'application/x-lilies-workspace-tool'
export const DATA_MIME = 'application/x-lilies-workspace-data'
export const statusNames:Record<string,string> = {queued:'等待运行',running:'正在处理',succeeded:'已完成',failed:'处理失败',interrupted:'已停止',waiting_input:'等待补充',configuring:'准备中'}
export function inputKind(field:Input) {return field.resource_kind || (field.name==='dataset_id'?'dataset':field.type==='file'||['source_path','file_path'].includes(field.name)?'file':'')}
export function compatible(object:DataObject, tool:Tool) {
  if (!tool.id.startsWith('workflow:')) return object.kind==='table'
  return !!tool.inputs?.some(field=>fieldCompatible(object,field))
}
export function fieldCompatible(object:DataObject, field:Input) {
  const kind=inputKind(field)
  return kind==='dataset'?object.kind==='table':kind==='model'?object.kind==='model':kind==='result'?object.kind==='analysis':kind==='file'?!!(object.path||object.dataset_id)&&(!field.accepted_formats?.length||field.accepted_formats.some(x=>'.'+x.replace(/^\./,'')===object.format)):false
}
