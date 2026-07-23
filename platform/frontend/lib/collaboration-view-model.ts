import type {
  CollaborationChannel,
  CollaborationChannelDetail,
  CollaborationDerivedStatus,
  CollaborationMessageEnvelope,
  CollaborationObservableEvent,
  CollaborationPermissionRequest,
  CollaborationReport,
  CollaborationReportStatus,
} from './platform'

export type CollaborationTone = 'neutral' | 'progress' | 'attention' | 'danger' | 'success'

export type CollaborationStateView = {
  label: string
  what: string
  owner: string
  why: string
  nextAction: string
  tone: CollaborationTone
}

export type CollaborationTimelineItem = {
  id: string
  source: 'assignment' | 'collaboration'
  sourceSeq: number
  kind: 'message' | 'tool' | 'permission' | 'report' | 'decision' | 'developer' | 'verification' | 'control' | 'context'
  title: string
  summary: string
  status: string
  actor: string
  occurredAt: string
  evidenceRefs: string[]
  toolName?: string
  durationMs?: number
  redactedInput?: Array<{ label: string; value: string }>
  permissionRequest?: CollaborationPermissionRequest
  permissionResolved?: boolean
  permissionOutcome?: string
  reportId?: string
}

const STATUS_COPY: Record<CollaborationReportStatus, CollaborationStateView> = {
  observed: {
    label: '已发现',
    what: '莉莉丝发现了可能属于平台能力或缺陷的问题。',
    owner: '莉莉丝',
    why: '先保留现场，再判断是否满足升级条件。',
    nextAction: '继续收集最小复现与证据。',
    tone: 'attention',
  },
  evidence_collecting: {
    label: '收集证据中',
    what: '问题仍在复查，尚未进入人工决策。',
    owner: '莉莉丝',
    why: '当前证据不足以安全地交给开发者。',
    nextAction: '等待莉莉丝补齐文档、尝试路线和复现证据。',
    tone: 'progress',
  },
  needs_more_evidence: {
    label: '需要更多证据',
    what: '本次升级暂未通过，报告需要补充证据。',
    owner: '莉莉丝',
    why: '审批者指出现有信息不足以支持开发动作。',
    nextAction: '按审批理由补充证据并重新提交。',
    tone: 'attention',
  },
  awaiting_user_review: {
    label: '等待你的判断',
    what: '能力或缺陷报告已具备人工审阅条件。',
    owner: '你',
    why: '把任务交给 Codex 可能会修改平台代码，必须由你授权。',
    nextAction: '批准并发送、要求更多证据，或拒绝。',
    tone: 'attention',
  },
  rejected: {
    label: '已拒绝',
    what: '本次平台升级请求已被拒绝。',
    owner: '莉莉丝',
    why: '审批决定不允许把这份报告交给开发者。',
    nextAction: '继续不依赖该能力的工作，或基于新证据重新报告。',
    tone: 'danger',
  },
  approved_for_codex: {
    label: '已批准开发',
    what: '报告已获授权，可以发送给隔离的开发者通道。',
    owner: '平台',
    why: '你已明确批准这次能力级开发请求。',
    nextAction: '等待 Codex 领取报告。',
    tone: 'progress',
  },
  implementing: {
    label: '开发处理中',
    what: 'Codex 正在处理已批准的平台报告。',
    owner: 'Codex',
    why: '报告已完成授权并由开发者租约锁定。',
    nextAction: '等待提交、测试证据和已知限制。',
    tone: 'progress',
  },
  ready_for_lilies_verification: {
    label: '等待莉莉丝复验',
    what: 'Codex 已返回实现结果和复验步骤。',
    owner: '莉莉丝',
    why: '开发完成不等于原任务已恢复。',
    nextAction: '由莉莉丝按原任务现场重新探测。',
    tone: 'attention',
  },
  lilies_verified: {
    label: '莉莉丝已复验',
    what: '莉莉丝已确认原任务现场恢复。',
    owner: '独立验证器',
    why: '还需要与实现者分离的最终验证。',
    nextAction: '等待独立验证结论。',
    tone: 'progress',
  },
  verification_failed: {
    label: '验证失败',
    what: '复验或独立验证发现结果与预期不一致。',
    owner: 'Codex / 莉莉丝',
    why: '现有改动还不能支持原任务的成功声明。',
    nextAction: '根据差异修复后再次复验。',
    tone: 'danger',
  },
  independently_verified: {
    label: '独立验证通过',
    what: '报告的解决结果已由独立验证器确认。',
    owner: '平台',
    why: '实现、莉莉丝复验和独立验证证据链均已闭合。',
    nextAction: '继续原任务或查看最终交付物。',
    tone: 'success',
  },
  withdrawn: {
    label: '已撤回',
    what: '莉莉丝撤回了这份报告。',
    owner: '莉莉丝',
    why: '问题不再成立，或已由其他路线解决。',
    nextAction: '继续原任务。',
    tone: 'neutral',
  },
  reported: {
    label: '已报告',
    what: '任务说明或环境问题已进入对应处理路线。',
    owner: '平台',
    why: '这类问题不需要修改平台能力。',
    nextAction: '等待任务作者或环境负责人响应。',
    tone: 'attention',
  },
  routed_to_task_author: {
    label: '已转交任务作者',
    what: '问题已送往任务说明或环境负责人。',
    owner: '任务作者',
    why: '根因位于任务包或运行环境，而不是平台能力。',
    nextAction: '等待修订任务或恢复环境。',
    tone: 'progress',
  },
  task_package_amended: {
    label: '任务已修订',
    what: '任务作者已提供新的任务版本。',
    owner: '莉莉丝',
    why: '原任务说明存在缺口，现已形成可执行修订。',
    nextAction: '按新任务版本重新检查并继续。',
    tone: 'progress',
  },
  rejected_with_evidence: {
    label: '修订被拒绝',
    what: '任务作者基于证据拒绝了修订请求。',
    owner: '莉莉丝',
    why: '任务包维持原定义。',
    nextAction: '依据拒绝证据调整路线或补充新证据。',
    tone: 'danger',
  },
  lilies_rechecks: {
    label: '莉莉丝重新检查',
    what: '任务修订后正在原现场重新检查。',
    owner: '莉莉丝',
    why: '必须确认新任务版本真的解除阻塞。',
    nextAction: '等待重新检查结果。',
    tone: 'progress',
  },
  environment_failed: {
    label: '环境故障',
    what: '执行环境未达到任务需要的条件。',
    owner: '环境负责人',
    why: '当前阻塞来自外部服务、凭证或运行环境。',
    nextAction: '恢复环境或给出可验证的替代条件。',
    tone: 'danger',
  },
  environment_restored: {
    label: '环境已恢复',
    what: '环境负责人已报告运行条件恢复。',
    owner: '莉莉丝',
    why: '仍需回到原任务现场验证恢复是否有效。',
    nextAction: '执行健康检查和原路径复验。',
    tone: 'progress',
  },
  unresolved: {
    label: '环境仍未解决',
    what: '环境响应没有解除当前阻塞。',
    owner: '环境负责人',
    why: '恢复条件尚不满足，且没有等价替代证据。',
    nextAction: '等待环境变化；其余独立工作继续。',
    tone: 'danger',
  },
  lilies_health_checks: {
    label: '环境健康检查',
    what: '莉莉丝正在验证环境恢复状态。',
    owner: '莉莉丝',
    why: '环境方的声明需要在真实任务路径中确认。',
    nextAction: '等待健康检查结果。',
    tone: 'progress',
  },
}

const CHANNEL_COPY: Record<CollaborationChannel['status'], CollaborationStateView> = {
  created: {
    label: '已创建',
    what: '协作通道已建立，正在等待任务开始。',
    owner: '平台',
    why: '任务、会话和应用已绑定到同一条证据链。',
    nextAction: '等待莉莉丝连接并开始执行。',
    tone: 'neutral',
  },
  active: {
    label: '执行中',
    what: '莉莉丝正在处理任务，协作事件会持续同步。',
    owner: '莉莉丝',
    why: 'Assignment 仍处于可运行状态。',
    nextAction: '关注时间线中的阻塞、权限和交付证据。',
    tone: 'progress',
  },
  disconnected: {
    label: '连接中断',
    what: '平台暂时无法与本地莉莉丝 daemon 通信。',
    owner: '本地执行环境',
    why: '任务状态保留，但新进度暂时不能同步。',
    nextAction: '恢复 daemon 连接后从原游标继续。',
    tone: 'danger',
  },
  closing: {
    label: '停止中',
    what: '平台正在终止 Assignment 并关闭协作通道。',
    owner: '平台',
    why: '你已确认停止当前任务。',
    nextAction: '等待取消与关闭操作完成。',
    tone: 'attention',
  },
  closed: {
    label: '已结束',
    what: '当前协作通道已经关闭。',
    owner: '平台',
    why: '任务正常结束或由用户停止。',
    nextAction: '查看最终证据，或返回任务列表。',
    tone: 'neutral',
  },
  archived: {
    label: '已归档',
    what: '协作通道已转为只读历史记录。',
    owner: '平台',
    why: '保留期策略已将结束的任务归档。',
    nextAction: '仅查看导出的因果证据。',
    tone: 'neutral',
  },
}

function stringValue(value: unknown) {
  return typeof value === 'string' ? value.trim() : ''
}

function stringArray(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
    : []
}

function hasPrivateMarker(value: string) {
  const lowered = value.toLocaleLowerCase()
  return ['private_reason', 'private-thinking', 'private_thinking', 'thinking_signature'].some(marker => lowered.includes(marker))
}

function semanticInputValue(value: unknown) {
  if (typeof value === 'string') return value.length > 180 ? `${value.slice(0, 177)}…` : value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (value === null || value === undefined) return '未提供'
  if (Array.isArray(value)) return `${value.length} 项（内容已脱敏）`
  return '结构化内容（已脱敏）'
}

export function semanticRedactedInput(value: Record<string, unknown> | undefined) {
  if (!value) return []
  return Object.entries(value)
    .filter(([key]) => !hasPrivateMarker(key))
    .slice(0, 12)
    .map(([key, item]) => ({
      label: key.replaceAll('_', ' '),
      value: semanticInputValue(item),
    }))
}

export function reportStateView(report: CollaborationReport): CollaborationStateView {
  return STATUS_COPY[report.status] || {
    label: report.status,
    what: '报告状态已更新。',
    owner: report.route,
    why: report.summary,
    nextAction: report.requested_outcome,
    tone: 'neutral',
  }
}

export function channelStateView(channel: CollaborationChannel): CollaborationStateView {
  return CHANNEL_COPY[channel.status]
}

export function derivedStatusView(derived: CollaborationDerivedStatus): CollaborationStateView {
  const code = derived.current_block.code
  const tone: CollaborationTone = code === 'none'
    ? 'success'
    : code === 'daemon_connection'
      ? 'danger'
      : code === 'capability_approval' || code === 'runtime_permission' || code === 'independent_verification'
        ? 'attention'
        : 'progress'
  return {
    label: derived.current_block.label,
    what: derived.current_block.label,
    owner: derived.owner.label,
    why: derived.why_waiting,
    nextAction: derived.next_action.label,
    tone,
  }
}

export function canDecideCollaborationReport(report: CollaborationReport) {
  return report.route === 'capability_approval' && report.status === 'awaiting_user_review'
}

function messageSemantics(message: CollaborationMessageEnvelope): Omit<CollaborationTimelineItem, 'id' | 'source' | 'sourceSeq' | 'occurredAt' | 'evidenceRefs'> {
  const payload = message.payload || {}
  const reason = stringValue(payload.reason)
  const summary = stringValue(payload.summary)
  const outcome = stringValue(payload.outcome)
  const status = stringValue(payload.status)
  const reportId = stringValue(payload.report_id)

  if (message.payload_schema === 'report.v1') {
    return {
      kind: 'report',
      title: '莉莉丝提交报告',
      summary: summary || stringValue(payload.requested_outcome) || '报告已进入协作证据链。',
      status: status || 'reported',
      actor: message.sender_role,
      reportId,
    }
  }
  if (message.payload_schema === 'approval.v1') {
    const decision = stringValue(payload.decision)
    return {
      kind: 'decision',
      title: decision === 'approve' ? '已批准并发送' : decision === 'reject' ? '已拒绝报告' : '要求补充证据',
      summary: reason || '审批决定已记录。',
      status: decision,
      actor: message.sender_role,
      reportId,
    }
  }
  if (message.payload_schema === 'developer_response.v1') {
    const changes = stringArray(payload.generic_capability_changes)
    const limits = stringArray(payload.known_limits)
    return {
      kind: 'developer',
      title: 'Codex 返回开发结果',
      summary: changes[0] || limits[0] || outcome || '开发响应已记录。',
      status: outcome,
      actor: message.sender_role,
      reportId,
    }
  }
  if (message.payload_schema === 'verification_claim.v1') {
    return {
      kind: 'verification',
      title: '莉莉丝提交验证声明',
      summary: stringValue(payload.claim) || '任务结果已冻结，等待独立验证。',
      status: status || 'frozen',
      actor: message.sender_role,
      reportId,
    }
  }
  if (message.payload_schema === 'verification_result.v1') {
    const differences = Array.isArray(payload.differences) ? payload.differences.length : 0
    const verdict = stringValue(payload.verdict)
    return {
      kind: 'verification',
      title: verdict === 'independently_verified' ? '独立验证通过' : '独立验证发现差异',
      summary: differences ? `${differences} 项结果与预期不一致。` : '验证器确认了交付声明。',
      status: verdict,
      actor: message.sender_role,
      reportId,
    }
  }
  if (message.payload_schema === 'task_amendment.v1') {
    return {
      kind: 'control',
      title: '任务包已响应',
      summary: reason || stringArray(payload.changes)[0] || '任务说明更新已记录。',
      status: outcome,
      actor: message.sender_role,
      reportId,
    }
  }
  if (message.payload_schema === 'environment_response.v1') {
    return {
      kind: 'control',
      title: '环境负责人已响应',
      summary: reason || summary || '环境状态响应已记录。',
      status: outcome,
      actor: message.sender_role,
      reportId,
    }
  }
  if (message.payload_schema === 'lilies_reprobe_result.v1') {
    return {
      kind: 'verification',
      title: '莉莉丝完成复验',
      summary: summary || reason || '原任务现场复验结果已记录。',
      status: outcome,
      actor: message.sender_role,
      reportId,
    }
  }
  return {
    kind: 'control',
    title: stringValue(payload.kind)?.replaceAll('_', ' ') || '协作状态更新',
    summary: reason || summary || '状态变化已记录在因果链中。',
    status: stringValue(payload.new_value) || status || message.message_type,
    actor: message.sender_role,
    reportId,
  }
}

function permissionOutcomes(events: CollaborationObservableEvent[]) {
  const pending: Array<{ requestId: string; toolName: string }> = []
  const outcomes = new Map<string, string>()
  for (const event of [...events].sort((left, right) => left.seq - right.seq)) {
    if (event.permission_request) {
      pending.push({
        requestId: event.permission_request.request_id,
        toolName: event.permission_request.tool_name,
      })
      continue
    }
    if (!['permission.resolved', 'permission.denied'].includes(event.event_type)) continue
    let index = -1
    if (event.permission_request_id) {
      index = pending.findIndex(candidate => candidate.requestId === event.permission_request_id)
      if (index < 0) continue
    } else if (event.tool_name) {
      for (let candidate = pending.length - 1; candidate >= 0; candidate -= 1) {
        if (pending[candidate].toolName === event.tool_name) {
          index = candidate
          break
        }
      }
    }
    if (index < 0) index = pending.length - 1
    if (index < 0) continue
    const [resolved] = pending.splice(index, 1)
    outcomes.set(resolved.requestId, event.status || (event.event_type === 'permission.denied' ? 'deny' : 'resolved'))
  }
  return outcomes
}

function observableSemantics(
  event: CollaborationObservableEvent,
  permissionOutcomeByRequest: Map<string, string>,
): CollaborationTimelineItem | null {
  if (hasPrivateMarker(event.event_type) || hasPrivateMarker(event.title)) return null
  const permissionOutcome = event.permission_request
    ? permissionOutcomeByRequest.get(event.permission_request.request_id)
    : undefined
  return {
    id: `assignment:${event.seq}:${event.event_type}`,
    source: 'assignment',
    sourceSeq: event.seq,
    kind: event.kind === 'assignment' || event.kind === 'session' ? 'control' : event.kind,
    title: event.title,
    summary: event.summary,
    status: permissionOutcome || event.status,
    actor: event.actor,
    occurredAt: event.created_at,
    evidenceRefs: event.evidence_refs,
    toolName: event.tool_name || undefined,
    durationMs: event.duration_ms ?? undefined,
    redactedInput: semanticRedactedInput(event.redacted_input),
    permissionRequest: event.permission_request || undefined,
    permissionResolved: Boolean(permissionOutcome),
    permissionOutcome,
  }
}

function timestamp(value: string) {
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? Number.MAX_SAFE_INTEGER : parsed
}

export function collaborationTimeline(detail: CollaborationChannelDetail): CollaborationTimelineItem[] {
  const observableEvents = detail.context?.observable_events || []
  const resolvedPermissions = permissionOutcomes(observableEvents)
  const assignmentItems = observableEvents
    .map(event => observableSemantics(event, resolvedPermissions))
    .filter((item): item is CollaborationTimelineItem => item !== null)
  const collaborationItems = detail.timeline
    .filter(message => !hasPrivateMarker(message.payload_schema))
    .map(message => {
      const semantics = messageSemantics(message)
      return {
        id: `collaboration:${message.message_id}`,
        source: 'collaboration' as const,
        sourceSeq: message.seq,
        occurredAt: message.created_at,
        evidenceRefs: message.evidence_refs.map(item => item.evidence_id),
        ...semantics,
      }
    })
  return [...assignmentItems, ...collaborationItems].sort((left, right) => {
    const timeDifference = timestamp(left.occurredAt) - timestamp(right.occurredAt)
    if (timeDifference) return timeDifference
    if (left.source !== right.source) return left.source.localeCompare(right.source)
    return left.sourceSeq - right.sourceSeq
  })
}

export class CollaborationOperationLedger {
  private readonly keys = new Map<string, string>()

  constructor(private readonly keyFactory: () => string) {}

  keyFor(signature: string) {
    const existing = this.keys.get(signature)
    if (existing) return existing
    const created = this.keyFactory()
    this.keys.set(signature, created)
    return created
  }

  complete(signature: string) {
    this.keys.delete(signature)
  }
}

export type CollaborationMobileView = 'tasks' | 'timeline' | 'detail'

export function collaborationMobileView(
  requested: string | null | undefined,
  hasChannel: boolean,
): CollaborationMobileView {
  if (!hasChannel) return 'tasks'
  if (requested === 'detail') return 'detail'
  if (requested === 'tasks') return 'tasks'
  return 'timeline'
}
