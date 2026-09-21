export type ProgressQuestion = { id: string; text: string; impact: string; next_action: string; answer: string }
export type ProgressResult = { label: string; task_id: string; file_path: string }
export type RequirementComparison = {
  requirement: string; source: string; current: string; gap: string
  status: 'met' | 'partial' | 'unmet' | 'unverified'; results: ProgressResult[]
}
export type WorkflowNote = { workflow_id: string; purpose: string; inputs: string; outputs: string }
export type ProgressItem = {
  id: string; title: string; goal: string; availability: 'not_ready' | 'trial' | 'usable'
  status: 'planned' | 'working' | 'waiting' | 'paused' | 'done'; summary: string; next_action: string
  workflow_ids: string[]; task_ids: string[]
  deliverable?: string; completion_criteria?: string[]; delivery_request_id?: string
  results: { label: string; task_id: string; file_path: string }[]; questions: ProgressQuestion[]
  blocker: { kind: string; owner: string; reason: string; next_action: string } | null
  requirements?: RequirementComparison[]
}
export type ProjectProgress = { revision: number; value: { goal: string; summary: string; items: ProgressItem[]; workflows?: WorkflowNote[] }; updated_at: string | null }
export type WorkflowOutline = {
  revision: number
  nodes: { id: string; type: string; title: string; workflow_id: string; branches: { id: string; conditions: unknown[] }[]; default_branch: string }[]
  edges: { source: string; target: string; branch: string | null }[]
}
export type ProjectTopology = { members: ProjectMember[]; calls: { source: string; target: string; node_id: string; label: string }[]; flows?: Record<string, WorkflowOutline> }
export const comparisonNames = { met: '已满足此项', partial: '部分满足', unmet: '尚未满足', unverified: '尚未验证' }
export type ProjectMember = { id: string; name: string; description: string; revision: number; purpose: string }
export type ConversationFocus = { nonce: number; item_id?: string; question_id?: string; task_id?: string; label: string; message?: string; mode?: 'task' | 'workflow' }
export type ProjectTask = {
  id: string; request_key: string; status: string; mode: string; purpose: string; item_id: string; workflow_id?: string
  feedback_task_id: string; message: string; error: string; inputs?: object; outputs?: Record<string, unknown>
  presentation: { message?: string; markdown?: string; artifacts?: { label: string; file_path: string }[] }
  created_at: string; updated_at: string; runs?: { id: string; status: string; application_id: string; draft_revision: number; reuse?: {source_run_id: string | null; nodes: string[]; titles?: string[]} }[]
}
export type ProjectActivity = {
  id: string; operation_id: string; request_id: string; title: string; status: string
  started_at: string; ended_at?: string; duration_seconds?: number | null; time: string
  tool_name: string; summary: string; arguments?: string; result?: string
  workflow_id?: string; workflow_name?: string; item_id?: string; task_id?: string
}
export const availabilityNames = { not_ready: '尚不可试用', trial: '可试用', usable: '可使用' }
export const workNames = { planned: '待推进', working: '建设中', waiting: '等待条件', paused: '已暂停', done: '本项工作完成' }
export const taskNames: Record<string, string> = { queued: '等待开始', running: '处理中', succeeded: '运行完成', waiting_input: '等待补充', interrupted: '已中断', failed: '运行失败', paused: '等待输入' }
