const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { Module } = require('node:module')
const { join } = require('node:path')
const ts = require('typescript')

const modulePath = join(__dirname, 'collaboration-view-model.ts')
const source = readFileSync(modulePath, 'utf8')
const compiled = new Module(modulePath, module)
compiled.filename = modulePath
compiled.paths = module.paths
compiled._compile(
  ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: modulePath,
  }).outputText,
  modulePath,
)

const {
  CollaborationOperationLedger,
  canDecideCollaborationReport,
  collaborationMobileView,
  collaborationTimeline,
  derivedStatusView,
  reportStateView,
  semanticRedactedInput,
} = compiled.exports

function report(overrides = {}) {
  return {
    report_id: 'report-1',
    route: 'capability_approval',
    status: 'awaiting_user_review',
    summary: '缺少平台级能力',
    requested_outcome: '增加通用能力',
    ...overrides,
  }
}

test('report state says what happened, who owns it, why, and what comes next', () => {
  for (const status of ['awaiting_user_review', 'implementing', 'verification_failed', 'independently_verified']) {
    const view = reportStateView(report({ status }))
    assert.ok(view.what)
    assert.ok(view.owner)
    assert.ok(view.why)
    assert.ok(view.nextAction)
  }
  assert.equal(canDecideCollaborationReport(report()), true)
  assert.equal(canDecideCollaborationReport(report({ status: 'approved_for_codex' })), false)
  assert.equal(canDecideCollaborationReport(report({ route: 'environment' })), false)
})

test('idempotency key survives a failed retry and rotates only after completion', () => {
  let sequence = 0
  const ledger = new CollaborationOperationLedger(() => `key-${++sequence}`)
  assert.equal(ledger.keyFor('approve:r1:1'), 'key-1')
  assert.equal(ledger.keyFor('approve:r1:1'), 'key-1')
  ledger.complete('approve:r1:1')
  assert.equal(ledger.keyFor('approve:r1:1'), 'key-2')
  assert.equal(ledger.keyFor('reject:r1:1'), 'key-3')
})

test('timeline merges assignment and collaboration evidence in chronological order', () => {
  const items = collaborationTimeline({
    channel: {},
    reports: [],
    timeline: [{
      message_id: 'message-1',
      seq: 1,
      message_type: 'approval',
      sender_role: 'user',
      payload_schema: 'approval.v1',
      payload: { decision: 'approve', reason: '允许建设通用能力' },
      evidence_refs: [],
      created_at: '2026-07-24T10:00:02Z',
    }],
    context: {
      observable_events: [{
        seq: 4,
        event_type: 'tool.started',
        kind: 'tool',
        title: 'workflow.test',
        summary: '调用已开始',
        status: 'started',
        actor: 'lilies',
        tool_name: 'workflow.test',
        redacted_input: { application_id: 'app-1', nested: { secret: 'hidden' } },
        evidence_refs: [],
        created_at: '2026-07-24T10:00:01Z',
      }],
      applications: [],
    },
  })
  assert.deepEqual(items.map(item => item.title), ['workflow.test', '已批准并发送'])
  assert.deepEqual(items[0].redactedInput, [
    { label: 'application id', value: 'app-1' },
    { label: 'nested', value: '结构化内容（已脱敏）' },
  ])
  assert.equal(JSON.stringify(items).includes('"secret":"hidden"'), false)
})

test('a later permission outcome makes the historical request read-only after refresh', () => {
  const items = collaborationTimeline({
    channel: {},
    reports: [],
    timeline: [],
    context: {
      observable_events: [{
        seq: 10,
        event_type: 'permission.requested',
        kind: 'permission',
        title: '运行权限请求',
        summary: '等待用户决定',
        status: 'pending',
        actor: 'lilies',
        tool_name: 'workflow.run',
        redacted_input: {},
        permission_request: {
          request_id: 'permission-1',
          tool_name: 'workflow.run',
          input_digest: `sha256:${'a'.repeat(64)}`,
          redacted_input: {},
          status: 'pending',
        },
        evidence_refs: [],
        created_at: '2026-07-24T10:00:01Z',
      }, {
        seq: 11,
        event_type: 'permission.resolved',
        kind: 'permission',
        title: '运行权限已处理',
        summary: '已允许一次',
        status: 'allow',
        actor: 'user',
        tool_name: 'workflow.run',
        permission_request_id: 'permission-1',
        redacted_input: {},
        evidence_refs: [],
        created_at: '2026-07-24T10:00:02Z',
      }],
      applications: [],
    },
  })
  assert.equal(items[0].permissionResolved, true)
  assert.equal(items[0].permissionOutcome, 'allow')
  assert.equal(items[0].status, 'allow')
})

test('permission outcomes use the exact request id when the same tool has concurrent requests', () => {
  const permission = (seq, requestId) => ({
    seq,
    event_type: 'permission.requested',
    kind: 'permission',
    title: '运行权限请求',
    summary: '等待用户决定',
    status: 'pending',
    actor: 'lilies',
    tool_name: 'workflow.run',
    redacted_input: {},
    permission_request: {
      request_id: requestId,
      tool_name: 'workflow.run',
      input_digest: `sha256:${String(seq).repeat(64).slice(0, 64)}`,
      redacted_input: {},
      status: 'pending',
    },
    evidence_refs: [],
    created_at: `2026-07-24T10:00:0${seq}Z`,
  })
  const items = collaborationTimeline({
    channel: {},
    reports: [],
    timeline: [],
    context: {
      observable_events: [
        permission(1, 'permission-a'),
        permission(2, 'permission-b'),
        {
          seq: 3,
          event_type: 'permission.resolved',
          kind: 'permission',
          title: '运行权限已处理',
          summary: '已允许一次',
          status: 'allow',
          actor: 'user',
          tool_name: 'workflow.run',
          permission_request_id: 'permission-a',
          redacted_input: {},
          evidence_refs: [],
          created_at: '2026-07-24T10:00:03Z',
        },
      ],
      applications: [],
    },
  })
  const pending = items.filter(item => item.permissionRequest)
  assert.equal(pending[0].permissionRequest.request_id, 'permission-a')
  assert.equal(pending[0].permissionResolved, true)
  assert.equal(pending[0].permissionOutcome, 'allow')
  assert.equal(pending[1].permissionRequest.request_id, 'permission-b')
  assert.equal(pending[1].permissionResolved, false)
})

test('server-derived ownership and next action override generic channel copy', () => {
  assert.deepEqual(derivedStatusView({
    current_block: { code: 'capability_approval', label: '平台能力报告等待你的审查' },
    owner: { role: 'user', id: 'studio-user', label: '你' },
    why_waiting: '批准前不会向 Codex 暴露。',
    next_action: { code: 'review_report', label: '阅读证据后作出决定。' },
    unread_count: 3,
  }), {
    label: '平台能力报告等待你的审查',
    what: '平台能力报告等待你的审查',
    owner: '你',
    why: '批准前不会向 Codex 暴露。',
    nextAction: '阅读证据后作出决定。',
    tone: 'attention',
  })
})

test('private reasoning markers are excluded from observable timeline', () => {
  const items = collaborationTimeline({
    channel: {},
    reports: [],
    timeline: [],
    context: {
      observable_events: [{
        seq: 1,
        event_type: 'private_reason.delta',
        kind: 'message',
        title: 'private thinking',
        summary: 'must not render',
        status: 'hidden',
        actor: 'lilies',
        redacted_input: {},
        evidence_refs: [],
        created_at: '2026-07-24T10:00:01Z',
      }],
      applications: [],
    },
  })
  assert.deepEqual(items, [])
})

test('redacted input remains semantic instead of raw JSON', () => {
  assert.deepEqual(semanticRedactedInput({
    query: 'customers',
    count: 3,
    enabled: true,
    rows: [1, 2],
    private_reason: 'do not show',
  }), [
    { label: 'query', value: 'customers' },
    { label: 'count', value: '3' },
    { label: 'enabled', value: 'true' },
    { label: 'rows', value: '2 项（内容已脱敏）' },
  ])
})

test('mobile route follows task to timeline to detail while empty state stays on tasks', () => {
  assert.equal(collaborationMobileView('detail', false), 'tasks')
  assert.equal(collaborationMobileView(null, true), 'timeline')
  assert.equal(collaborationMobileView('detail', true), 'detail')
  assert.equal(collaborationMobileView('tasks', true), 'tasks')
})

test('workspace keeps capability approval, runtime permission, and developer credentials isolated', () => {
  const workspace = readFileSync(join(__dirname, '..', 'app', 'developer', 'collaboration', 'collaboration-workspace.tsx'), 'utf8')
  const platform = readFileSync(join(__dirname, 'platform.ts'), 'utf8')
  const runtime = readFileSync(join(__dirname, '..', 'app', 'runtime', '[id]', 'page.tsx'), 'utf8')
  assert.match(workspace, /data-approval-channel="capability"/)
  assert.match(workspace, /data-permission-channel="runtime"/)
  assert.match(workspace, /允许一次/)
  assert.match(workspace, /能否修改代码仍取决于任务显式授予的 workspace 与 scope/)
  assert.doesNotMatch(workspace, /不会授权修改平台代码/)
  assert.match(workspace, /确认开启自动转发/)
  assert.doesNotMatch(`${workspace}\n${platform}`, /\/api\/v1\/developer\/collaboration/)
  assert.doesNotMatch(runtime, /data-global-developer-collaboration/)
})

test('frontend mutations bind revisions, input digests, and stable idempotency keys', () => {
  const workspace = readFileSync(join(__dirname, '..', 'app', 'developer', 'collaboration', 'collaboration-workspace.tsx'), 'utf8')
  const platform = readFileSync(join(__dirname, 'platform.ts'), 'utf8')
  assert.match(workspace, /expected_report_revision: report\.revision/)
  assert.match(workspace, /expected_channel_revision: channel\.revision/)
  assert.match(workspace, /expected_input_digest: permission\.input_digest/)
  assert.match(workspace, /ledgerRef\.current\.keyFor\(signature\)/)
  assert.match(platform, /assignments\/\$\{encodeURIComponent\(assignmentId\)\}\/permissions\/\$\{encodeURIComponent\(requestId\)\}/)
})

test('workspace renders evidence, substantive developer results, and verifier differences', () => {
  const workspace = readFileSync(join(__dirname, '..', 'app', 'developer', 'collaboration', 'collaboration-workspace.tsx'), 'utf8')
  for (const label of ['预期', '实际', '已经尝试', '证据引用', '复现步骤', '可继续的独立工作', '考虑过的绕行方案', '报告时平台合同', 'Codex 结果', '实现后合同', '已知限制', '莉莉丝复验步骤', '独立验证失败', '这项差异的证据']) {
    assert.match(workspace, new RegExp(label))
  }
  for (const property of [
    'selectedReport.requirement_digest',
    'selectedReport.source_message_id',
    'route.evidence_refs',
    'selectedTimeline.evidenceRefs',
    'response.new_contract_digest',
    'test.evidence_ref',
    'difference.evidence_refs',
  ]) {
    assert.match(workspace, new RegExp(property.replace('.', '\\.')))
  }
  assert.match(workspace, /response\.commit_sha/)
  assert.match(workspace, /response\.tests_run/)
  assert.match(workspace, /difference\.expected/)
  assert.match(workspace, /difference\.actual/)
})

test('desktop, mobile, dialog, and reduced-motion contracts remain reachable', () => {
  const workspace = readFileSync(join(__dirname, '..', 'app', 'developer', 'collaboration', 'collaboration-workspace.tsx'), 'utf8')
  const styles = readFileSync(join(__dirname, '..', 'app', 'developer', 'collaboration', 'collaboration.module.css'), 'utf8')
  assert.match(styles, /grid-template-columns:\s*minmax\(270px,\s*310px\)\s+minmax\(390px,\s*1fr\)\s+minmax\(420px,\s*510px\)/)
  assert.match(styles, /@media \(max-width:\s*980px\)/)
  for (const view of ['tasks', 'timeline', 'detail']) {
    assert.match(styles, new RegExp(`data-mobile-view="${view}"`))
  }
  assert.match(styles, /@media \(prefers-reduced-motion:\s*reduce\)/)
  assert.match(workspace, /event\.key === 'Escape'/)
  assert.match(workspace, /event\.key !== 'Tab'/)
  assert.match(workspace, /event\.shiftKey/)
  assert.match(workspace, /dialog\.contains\(active\)/)
  assert.match(workspace, /restoreFocusRef\.current\?\.focus\(\)/)
  assert.match(workspace, /aria-modal="true"/)
  assert.match(workspace, /confirmLabel="确认开启自动转发"/)
  assert.match(workspace, /confirmLabel="停止并关闭通道"/)
})

test('event reconnect and double-click protection preserve one causal operation', () => {
  const workspace = readFileSync(join(__dirname, '..', 'app', 'developer', 'collaboration', 'collaboration-workspace.tsx'), 'utf8')
  const platform = readFileSync(join(__dirname, 'platform.ts'), 'utf8')
  assert.match(workspace, /openStudioCollaborationEventStream\(selectedChannelId,\s*cursor,\s*controller\.signal\)/)
  assert.match(workspace, /parsed > cursor\) cursor = parsed/)
  assert.match(platform, /'Last-Event-ID': String\(Math\.max\(0,\s*after\)\)/)
  assert.match(workspace, /if \(inFlightRef\.current\.has\(signature\)\) return false/)
  assert.match(workspace, /inFlightRef\.current\.add\(signature\)/)
  assert.match(workspace, /CONTEXT_REFRESH_MS = 5_000/)
  assert.match(workspace, /window\.setInterval\(\(\) =>/)
  assert.match(workspace, /document\.visibilityState !== 'hidden'/)
  assert.match(workspace, /studioCollaborationChannel\(selectedChannelId\)/)
  assert.match(workspace, /window\.clearInterval\(contextRefreshTimer\)/)
})
