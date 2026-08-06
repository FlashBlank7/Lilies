'use client'

import Link from 'next/link'
import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  clearClientToken,
  getClientToken,
  idempotency,
  isAuthError,
  PlatformApiError,
  saveClientToken,
  type DraftEvidence,
} from '@/lib/platform'
import { defaultLocale, isLocale, messages, nextLocale, type Locale } from '@/lib/i18n'
import { classifyRuntimeStatus, runtimeCommit, runtimeVersion, type RuntimeHealth } from '@/lib/runtime-status'

type Application = {
  id: string
  name: string
  description: string
  display_description?: string
  mode: string
  active_version?: number | null
  draft_revision: number
  content_hash: string
  tested_hash?: string | null
  evidence?: DraftEvidence
  created_at?: string
  updated_at?: string
}
const APP_FILTERS = ['all', 'needs_acceptance', 'ready_to_publish', 'published'] as const
type AppFilter = typeof APP_FILTERS[number]
const APP_SORTS = ['recent', 'readiness', 'revision', 'name'] as const
type AppSort = typeof APP_SORTS[number]
type AppQuickAction = { id: string; href: string; label: string }
type AppListUrlState = { filter?: AppFilter; q?: string; sort?: AppSort }
type Copy = (typeof messages)[Locale]
type RequirementIntakeChoiceType = 'single' | 'multi'
type RequirementIntakeOptionEffect = {
  axis: 'functional_capability' | 'runtime_guarantee' | 'external_contract' | 'execution_envelope' | 'carrier' | 'evidence' | 'runtime_interface' | 'permission_boundary' | 'target_user'
  target_id?: string
  action: 'include' | 'require' | 'exclude' | 'configure' | 'raise_envelope'
  value: string
}
type RequirementIntakeOption = {
  id: string
  label: string
  description?: string
  impact?: string
  recommended?: boolean
  effects?: RequirementIntakeOptionEffect[]
}
type RequirementClarificationSelection = {
  selectedOptionIds: string[]
  customAnswer: string
}
type RequirementClarificationSelections = Record<string, RequirementClarificationSelection>
type RequirementIntakeAnswer = {
  question_id: string
  question: string
  choice_type: RequirementIntakeChoiceType
  selected_option_ids: string[]
  selected_options: {
    id: string
    label: string
    description: string
    impact: string
    effects: RequirementIntakeOptionEffect[]
  }[]
  custom_answer: string
  answer: string
}
type RequirementIntakeQuestion = {
  id: string
  label: string
  question: string
  why?: string
  decision_axis: 'functional_capability' | 'runtime_guarantee' | 'external_contract' | 'execution_envelope' | 'carrier' | 'evidence' | 'runtime_interface' | 'permission_boundary' | 'target_user'
  choice_type: RequirementIntakeChoiceType
  options: RequirementIntakeOption[]
  custom_allowed?: boolean
  custom_placeholder?: string
  placeholder?: string
}
type CapabilityContractItem = {
  id: string
  title: string
  description: string
  required_envelope: string
  availability?: string
}
type CapabilityCoverageOwner = 'workflow_runtime' | 'evaluation_harness' | 'platform_harness' | 'external_system'
type CapabilityBuildContract = {
  contract_id: string
  source_requirement: string
  target_user: string
  business_goal: string
  functional_capabilities: CapabilityContractItem[]
  runtime_guarantees: CapabilityContractItem[]
  external_contracts: CapabilityContractItem[]
  required_envelope: string
  risk_level: string
  carrier_decisions: { capability_id: string; carrier_type: string; resource_hint: string }[]
  platform_coverage: { capability_id: string; owner: CapabilityCoverageOwner; status: string; surface: string }[]
  evidence_plan: { capability_ids: string[]; target_level: string; environment: string; expected_status: string; claim_scope: string }[]
  claim_scope: { ceiling: string; verified: string[]; excluded: string[] }
  unresolved_decisions: string[]
}
type RequirementIntakeResponse = {
  task_id: string
  status: 'needs_input' | 'ready'
  confidence: number
  reasoning_summary: string
  detected_goal: string
  missing: string[]
  questions: RequirementIntakeQuestion[]
  completed_requirement?: string | null
  capability_build_contract?: CapabilityBuildContract | null
  capability_closure?: { valid: boolean; scoped_gaps?: string[]; blocking_errors?: string[] } | null
  workflow_intent: Record<string, unknown>
  usage: Record<string, unknown>
}

type DraftMutationResult = {
  revision: number
}
function deriveApplicationName(requirement: string) {
  const raw = requirement.trim()
  const sectionMatch = raw.match(/(?:^|\n)\s{0,3}#{0,4}\s*(?:业务目标|目标|Business goal|Goal)\s*[:：]?\s*\n+([\s\S]*?)(?=\n\s{0,3}#{1,4}\s+|\n\s*(?:启动输入|工作流步骤|运行时界面|可编辑块|权限与边界|验收|下一步|Start input|Workflow steps|Runtime interface|Editable blocks|Permissions|Acceptance|Next)|$)/i)
  const source = sectionMatch?.[1]?.trim() || raw
  const text = source
    .split(/\n+/)
    .map(line => line
      .replace(/^\s{0,3}#{1,6}\s*/, '')
      .replace(/^\s*[-*+]\s*/, '')
      .replace(/^\s*\d+[.)、]\s*/, '')
      .replace(/\*\*/g, '')
      .trim())
    .filter(line => line && !/^(工作流构建需求|工作流搭建方案|请按以下补全需求生成一个可编辑工作流|Workflow-building plan|Workflow requirement)$/i.test(line))
    .join(' ')
    .replace(/\s+/g, ' ')
  if (!text) return '新智能体'
  const first = text.split(/[。.!?\n\r]/)[0].replace(/^[\s"',，,：:；;“”‘’`]+|[\s"',，,：:；;“”‘’`]+$/g, '')
  const cleaned = first
    .replace(/^(请|请帮我|我需要|我想要|帮我|帮我做|搭建|创建|制作|生成|构建|设计)(一个|一款|一个可以|可以|能够|能)?/, '')
    .replace(/^(please|build|create|make|generate|design)\s+(a|an|the)?\s*/i, '')
    .replace(/[，,]\s*(支持|用于|并|以及|and|with).*$/i, '')
    .replace(/^[\s，,：:；;]+|[\s，,：:；;]+$/g, '')
  return (cleaned || first || text).slice(0, 32).replace(/[\s，,：:；;]+$/g, '') || '新智能体'
}

function deriveApplicationDescription(
  requirement: string,
  contract: CapabilityBuildContract | null,
) {
  const goalMatch = requirement.match(/(?:^|\n)\s{0,3}#{0,4}\s*(?:业务目标|Business goal)\s*[:：]?\s*\n+([\s\S]*?)(?=\n\s{0,3}#{1,6}\s+|$)/i)
  const source = contract?.business_goal?.trim() || goalMatch?.[1]?.trim() || requirement.trim()
  return source
    .replace(/^\s{0,3}#{1,6}\s*/gm, '')
    .replace(/^\s*[-*+]\s*/gm, '')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 500)
}

async function applyDraftOperation(applicationId: string, expectedRevision: number, op: string, data: Record<string, unknown>) {
  const result = await api<DraftMutationResult>(`/api/v1/applications/${applicationId}/draft`, {
    method: 'POST',
    body: JSON.stringify({ expected_revision: expectedRevision, idempotency_key: idempotency(), op, data }),
  })
  return result.revision
}

function isJapaneseLearningRequirement(requirement: string) {
  const text = requirement.toLocaleLowerCase()
  return /(日语|日本語|日本人|口语|口語|视频|影片|评论区|コメント|japanese|spoken|expression|video|comment)/i.test(text)
    && /(学生|学习|學習|learner|student|study|summary|总结|表达|表現)/i.test(text)
}

function isCodexWorkspaceRequirement(requirement: string) {
  const text = requirement.toLocaleLowerCase()
  if (/(codex|像\s*codex)/i.test(text)) return true
  const agentSignal = /(coding agent|code agent|代码智能体|編碼智能體|工作区智能体|工作區智能體)/i.test(text)
  const workspaceSignal = /(workspace|repository|repo|codebase|工作区|工作區|代码库|代碼庫|项目文件|專案檔案|文件|代码|代碼)/i.test(text)
  return agentSignal && workspaceSignal
}

async function applyCodexWorkspaceScenario(application: Application) {
  return api<{ revision: number; validation: { valid: boolean; errors: string[] } }>(
    `/api/v1/applications/${application.id}/scenarios/codex_like_workspace_agent/apply`,
    {
      method: 'POST',
      body: JSON.stringify({
        expected_revision: application.draft_revision,
        expected_content_hash: application.content_hash,
        idempotency_key: idempotency(),
      }),
    },
  )
}

async function launchBuilder(application: Application, requirement: string, capabilityBuildContract: CapabilityBuildContract | null) {
  if (isCodexWorkspaceRequirement(requirement) && !capabilityBuildContract) {
    await applyCodexWorkspaceScenario(application)
  }
  return api<{ build_id: string }>(`/api/v1/applications/${application.id}/builds`, {
    method: 'POST',
    body: JSON.stringify({
      requirement,
      auto_publish: true,
      max_turns: 36,
      max_repair_cycles: 4,
      max_elapsed_seconds: 480,
    }),
  })
}

const JAPANESE_LEARNING_COMMENT_FIXTURE_TEMPLATE = `受控样例评论线索（离线验证用，不代表已经抓取真实视频网站）
主题：{{ topic }}
- 评论 A：「それな、課題多すぎてしんどい」用于朋友间强烈附和。
- 评论 B：「ワンチャン間に合う？」表示也许还有机会，语气很口语。
- 评论 C：「普通に助かる」表示真的很有帮助，语气自然但偏随意。

来源边界：这是受控样例评论集，用来验证学习总结的结果形状；接入真实公开视频评论前，不宣称外部采集已完成。`

const JAPANESE_LEARNING_SUMMARY_TEMPLATE = `# 今日日语口语总结：{{ topic }}

受控样例来源：离线评论夹具，用于验证结果形状；真实公开视频评论采集需要在后续版本单独接入证据。

## 1. それな
- 中文含义：对，就是这样；我懂你说的。
- 自然例句：A「課題、今日も多すぎない？」B「それな、ちょっとしんどい。」
- 语气/场景：朋友、同学之间强烈附和，不适合正式汇报。
- 学习提醒：可以理解成比「そうだね」更口语、更有共鸣感。

## 2. ワンチャン
- 中文含义：也许有机会；说不定能成。
- 自然例句：「今から図書館行けば、ワンチャン間に合うかも。」
- 语气/场景：年轻人聊天里常见，带一点侥幸和轻松感。
- 学习提醒：正式场合改用「可能性があります」更安全。

## 3. 普通に助かる
- 中文含义：真的挺有帮助；老实说很救命。
- 自然例句：「ノート共有してくれるの、普通に助かる。」
- 语气/场景：自然表达感谢，比直译的“普通地”更接近“其实很/真的”。
- 学习提醒：这里的「普通に」不是普通程度，而是强调自然真实的评价。

## 来源上下文
{{ comment_clues }}`

async function seedJapaneseLearningDraftSkeleton(applicationId: string, initialRevision: number) {
  const suffix = Date.now()
  const startId = `jp_topic_${suffix}`
  const collectId = `jp_collect_comments_${suffix}`
  const extractId = `jp_extract_expressions_${suffix}`
  const summaryId = `jp_daily_summary_${suffix}`
  const testId = `jp_acceptance_${suffix}`
  let revision = initialRevision
  revision = await applyDraftOperation(applicationId, revision, 'add_node', { node: {
    id: startId, type: 'start', block_version: 1, title: '关注的日语主题',
    description: '学习者输入今天想关注的日语话题，例如校园生活、打工、旅行或敬语。',
    config: { inputs: [{ name: 'topic', label: '关注的日语主题', type: 'string', required: true, default: '校园生活' }] },
    position: { x: 100, y: 160 },
    retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
  } })
  revision = await applyDraftOperation(applicationId, revision, 'add_node', { node: {
    id: collectId, type: 'template_transform', block_version: 1, title: '收集公开视频评论线索',
    description: '占位步骤：围绕主题整理主流视频网站公开评论区中的真实表达线索。',
    config: {
      template: JAPANESE_LEARNING_COMMENT_FIXTURE_TEMPLATE,
      variables: { topic: { $ref: { node_id: startId, path: ['output', 'topic'] } } },
    },
    position: { x: 390, y: 160 },
    retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
  } })
  revision = await applyDraftOperation(applicationId, revision, 'add_node', { node: {
    id: extractId, type: 'template_transform', block_version: 1, title: '提取真实口语表达',
    description: '占位步骤：从评论线索里提取自然说法、语气、使用场景和注意点。',
    config: {
      template: JAPANESE_LEARNING_SUMMARY_TEMPLATE,
      variables: {
        topic: { $ref: { node_id: startId, path: ['output', 'topic'] } },
        comment_clues: { $ref: { node_id: collectId, path: ['text'] } },
      },
    },
    position: { x: 680, y: 160 },
    retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
  } })
  revision = await applyDraftOperation(applicationId, revision, 'add_node', { node: {
    id: summaryId, type: 'answer', block_version: 1, title: '今日日语口语总结',
    description: '面向学习者的最终交付：表达、含义、例句、语气和使用提醒。',
    config: { answer: { $ref: { node_id: extractId, path: ['text'] } } },
    position: { x: 970, y: 160 },
    retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
  } })
  for (const [edgeId, source, target, sourcePort] of [
    [`jp_edge_collect_${suffix}`, startId, collectId, 'output'],
    [`jp_edge_extract_${suffix}`, collectId, extractId, 'text'],
    [`jp_edge_summary_${suffix}`, extractId, summaryId, 'text'],
  ] as const) {
    revision = await applyDraftOperation(applicationId, revision, 'add_edge', { edge: {
      id: edgeId, source, target, source_port: sourcePort, target_port: 'input',
    } })
  }
  revision = await applyDraftOperation(applicationId, revision, 'add_test', { test: {
    id: testId,
    name: 'Japanese learning scenario structure and summary quality',
    requirement: 'Safe draft exposes a topic input, controlled comment fixture, spoken-expression extraction, and a learner-readable daily Japanese summary.',
    inputs: { topic: '校园生活' },
    assertions: [
      { path: ['answer'], operator: 'contains', expected: '今日日语口语总结：校园生活' },
      { path: ['answer'], operator: 'contains', expected: 'それな' },
      { path: ['answer'], operator: 'contains', expected: '中文含义' },
      { path: ['answer'], operator: 'contains', expected: '自然例句' },
      { path: ['answer'], operator: 'contains', expected: '语气/场景' },
      { path: ['answer'], operator: 'contains', expected: '学习提醒' },
      { path: ['answer'], operator: 'contains', expected: '受控样例来源' },
    ],
    required_node_types: ['start', 'template_transform', 'answer'],
    required_tool_nodes: [],
    required_tools: [],
    minimum_tool_calls: 0,
    mandatory: true,
    structural_only: false,
    feedback_hints: ['Keep the controlled fixture boundary visible until live external-video evidence is explicitly implemented.'],
  } })
  return revision
}

async function seedSafeDraftSkeleton(applicationId: string, initialRevision: number, requirement = '') {
  if (isJapaneseLearningRequirement(requirement)) {
    return seedJapaneseLearningDraftSkeleton(applicationId, initialRevision)
  }
  const suffix = Date.now()
  const startId = `safe_start_${suffix}`
  const answerId = `safe_answer_${suffix}`
  const testId = `safe_acceptance_${suffix}`
  let revision = initialRevision
  revision = await applyDraftOperation(applicationId, revision, 'add_node', { node: {
    id: startId, type: 'start', block_version: 1, title: 'Customer Request',
    description: 'Safe draft input created without starting the builder team.',
    config: { inputs: [{ name: 'customer_request', label: 'Customer request', type: 'string', required: true }] },
    position: { x: 120, y: 160 },
    retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
  } })
  revision = await applyDraftOperation(applicationId, revision, 'add_node', { node: {
    id: answerId, type: 'answer', block_version: 1, title: 'Draft Answer',
    description: 'Starter output placeholder; replace this after the builder team or manual editing.',
    config: { answer: { $ref: { node_id: startId, path: ['output', 'customer_request'] } } },
    position: { x: 420, y: 160 },
    retry: { enabled: false, max_attempts: 1, delay_seconds: 0.5 }, error_strategy: 'fail',
  } })
  revision = await applyDraftOperation(applicationId, revision, 'add_edge', { edge: {
    id: `safe_edge_${suffix}`, source: startId, target: answerId, source_port: 'output', target_port: 'input',
  } })
  revision = await applyDraftOperation(applicationId, revision, 'add_test', { test: {
    id: testId,
    name: 'Starter structure check',
    requirement: 'Safe draft contains an editable Start to Answer skeleton before any model build.',
    inputs: { customer_request: 'Summarize a customer request and identify the next owner.' },
    assertions: [],
    required_node_types: ['start', 'answer'],
    required_tool_nodes: [],
    required_tools: [],
    minimum_tool_calls: 0,
    mandatory: true,
    structural_only: true,
    feedback_hints: ['Start the builder team or edit the nodes manually to replace this starter skeleton.'],
  } })
  return revision
}

function appReadinessState(item: Application): Exclude<AppFilter, 'all'> {
  if (item.active_version) return 'published'
  if (item.evidence?.state === 'current' || (!item.evidence && item.tested_hash === item.content_hash)) return 'ready_to_publish'
  return 'needs_acceptance'
}

function appReadinessRank(item: Application) {
  const state = appReadinessState(item)
  if (state === 'published') return 0
  if (state === 'ready_to_publish') return 1
  return 2
}

function isAppFilter(value: string | null): value is AppFilter {
  return Boolean(value && APP_FILTERS.includes(value as AppFilter))
}

function isAppSort(value: string | null): value is AppSort {
  return Boolean(value && APP_SORTS.includes(value as AppSort))
}

function requirementReadiness(requirement: string, t: Copy) {
  const text = requirement.trim()
  const normalized = text.toLocaleLowerCase()
  const signals = [
    { id: 'audience', label: t.requirementSignalAudience, detail: t.requirementSignalAudienceHint, ready: /(客户|用户|负责人|顾问|运营|审阅|学生|学习者|customer|user|owner|operator|consultant|reviewer|learner|student)/i.test(normalized) },
    { id: 'outcome', label: t.requirementSignalOutcome, detail: t.requirementSignalOutcomeHint, ready: /(输出|生成|给出|判断|分类|摘要|清单|result|output|generate|classify|summary|checklist)/i.test(normalized) },
    { id: 'acceptance', label: t.requirementSignalAcceptance, detail: t.requirementSignalAcceptanceHint, ready: /(验收|测试|必须|覆盖|acceptance|test|must|cover|verify)/i.test(normalized) },
    { id: 'detail', label: t.requirementSignalDetail, detail: t.requirementSignalDetailHint, ready: text.length >= 80 },
  ]
  const readyCount = signals.filter(signal => signal.ready).length
  return { signals, readyCount, total: signals.length, ready: readyCount >= 3 }
}

function requirementIntakeAnswers(
  questions: RequirementIntakeQuestion[],
  selections: RequirementClarificationSelections,
): RequirementIntakeAnswer[] {
  return questions
    .map(question => {
      const selection = selections[question.id] || { selectedOptionIds: [], customAnswer: '' }
      const selectedOptions = question.options
        .filter(option => selection.selectedOptionIds.includes(option.id))
        .map(option => ({
          id: option.id,
          label: option.label,
          description: option.description || '',
          impact: option.impact || '',
          effects: option.effects || [],
        }))
      const customAnswer = selection.customAnswer.trim()
      return {
        question_id: question.id,
        question: question.question,
        choice_type: question.choice_type,
        selected_option_ids: selectedOptions.map(option => option.id),
        selected_options: selectedOptions,
        custom_answer: customAnswer,
        answer: [...selectedOptions.map(option => option.label), customAnswer].filter(Boolean).join('; '),
      }
    })
    .filter(answer => answer.selected_option_ids.length > 0 || answer.custom_answer)
}

function mergeRequirementIntakeAnswers(
  history: RequirementIntakeAnswer[],
  current: RequirementIntakeAnswer[],
) {
  const merged = new Map(history.map(answer => [answer.question_id, answer]))
  current.forEach(answer => merged.set(answer.question_id, answer))
  return Array.from(merged.values()).slice(-32)
}

function requirementQuestionAnswered(question: RequirementIntakeQuestion, selections: RequirementClarificationSelections) {
  const selection = selections[question.id]
  return Boolean(selection?.selectedOptionIds.length || selection?.customAnswer.trim())
}

function createActionState(requirement: string, readinessReady: boolean, busy: boolean, draftBusy: boolean, buildIntentConfirmed: boolean, t: Copy) {
  if (busy || draftBusy) return { id: 'busy', tone: 'busy', title: t.createActionBusyTitle, detail: t.createActionBusyDetail }
  if (requirement.trim().length < 10) return { id: 'add_detail', tone: 'attention', title: t.createActionAddDetailTitle, detail: t.createActionAddDetailDetail }
  if (buildIntentConfirmed) return { id: 'confirm_team', tone: 'warning', title: t.createActionConfirmTeamTitle, detail: t.createActionConfirmTeamDetail }
  if (readinessReady) return { id: 'save_draft', tone: 'ready', title: t.createActionSaveDraftTitle, detail: t.createActionSaveDraftDetail }
  return { id: 'improve_requirement', tone: 'attention', title: t.createActionImproveTitle, detail: t.createActionImproveDetail }
}

function recommendedCreateAction(actionId: string, t: Copy) {
  if (actionId === 'busy') return { target: 'wait', tone: 'busy', label: t.recommendedActionBusyLabel, detail: t.recommendedActionBusyDetail, disabled: true }
  if (actionId === 'save_draft') return { target: 'safe_draft', tone: 'ready', label: t.recommendedActionSaveDraftLabel, detail: t.recommendedActionSaveDraftDetail, disabled: false }
  if (actionId === 'confirm_team') return { target: 'guarded_build_button', tone: 'warning', label: t.recommendedActionTeamGuardLabel, detail: t.recommendedActionTeamGuardDetail, disabled: false }
  if (actionId === 'improve_requirement') return { target: 'requirement_focus', tone: 'attention', label: t.recommendedActionImproveLabel, detail: t.recommendedActionImproveDetail, disabled: false }
  return { target: 'requirement_focus', tone: 'attention', label: t.recommendedActionAddDetailLabel, detail: t.recommendedActionAddDetailDetail, disabled: false }
}

export default function Home() {
  const [locale, setLocale] = useState<Locale>(defaultLocale)
  const t = messages[locale]
  const requirementInputRef = useRef<HTMLTextAreaElement>(null)
  const buildButtonRef = useRef<HTMLButtonElement>(null)
  const [apps, setApps] = useState<Application[]>([])
  const [requirement, setRequirement] = useState<string>(t.requirementPlaceholder)
  const [selectedExampleId, setSelectedExampleId] = useState<string | null>(null)
  const [appFilter, setAppFilter] = useState<AppFilter>('all')
  const [appSearch, setAppSearch] = useState('')
  const [appSort, setAppSort] = useState<AppSort>('recent')
  const [busy, setBusy] = useState(false)
  const [draftBusy, setDraftBusy] = useState(false)
  const [requirementIntakeBusy, setRequirementIntakeBusy] = useState(false)
  const [buildIntentConfirmed, setBuildIntentConfirmed] = useState(false)
  const [createdApplicationId, setCreatedApplicationId] = useState('')
  const [requirementSelections, setRequirementSelections] = useState<RequirementClarificationSelections>({})
  const [requirementAnswerHistory, setRequirementAnswerHistory] = useState<RequirementIntakeAnswer[]>([])
  const [requirementIntake, setRequirementIntake] = useState<RequirementIntakeResponse | null>(null)
  const [capabilityBuildContract, setCapabilityBuildContract] = useState<CapabilityBuildContract | null>(null)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [authRequired, setAuthRequired] = useState(false)
  const [tokenInput, setTokenInput] = useState('')
  const [runtimeHealth, setRuntimeHealth] = useState<RuntimeHealth | null>(null)
  const [runtimeUnavailable, setRuntimeUnavailable] = useState(false)
  function clearFeedback() {
    setNotice('')
    setError('')
  }
  function showNotice(message: string) {
    setError('')
    setNotice(message)
  }
  function showError(message: string) {
    setNotice('')
    setError(message)
  }
  const writeAppListUrlState = useCallback((updates: AppListUrlState, options: { replace?: boolean } = {}) => {
    if (typeof window === 'undefined') return
    const query = new URLSearchParams(window.location.search)
    if (updates.filter !== undefined) {
      if (updates.filter === 'all') query.delete('filter')
      else query.set('filter', updates.filter)
    }
    if (updates.q !== undefined) {
      const value = updates.q.trim()
      if (value) query.set('q', value)
      else query.delete('q')
    }
    if (updates.sort !== undefined) {
      if (updates.sort === 'recent') query.delete('sort')
      else query.set('sort', updates.sort)
    }
    const nextQuery = query.toString()
    const nextUrl = `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ''}`
    if (nextUrl === `${window.location.pathname}${window.location.search}`) return
    if (options.replace) window.history.replaceState(null, '', nextUrl)
    else window.history.pushState(null, '', nextUrl)
  }, [])
  const setAppListFilter = useCallback((value: AppFilter) => {
    setAppFilter(value)
    writeAppListUrlState({ filter: value })
  }, [writeAppListUrlState])
  const setAppListSearch = useCallback((value: string) => {
    setAppSearch(value)
    writeAppListUrlState({ q: value }, { replace: true })
  }, [writeAppListUrlState])
  const setAppListSort = useCallback((value: AppSort) => {
    setAppSort(value)
    writeAppListUrlState({ sort: value })
  }, [writeAppListUrlState])
  const clearAppListSearch = useCallback(() => {
    setAppListSearch('')
  }, [setAppListSearch])
  const resetAppListView = useCallback(() => {
    setAppFilter('all')
    setAppSearch('')
    setAppSort('recent')
    writeAppListUrlState({ filter: 'all', q: '', sort: 'recent' })
  }, [writeAppListUrlState])
  const syncAppListStateFromLocation = useCallback(() => {
    if (typeof window === 'undefined') return
    const query = new URLSearchParams(window.location.search)
    const filter = query.get('filter')
    const sort = query.get('sort')
    setAppFilter(isAppFilter(filter) ? filter : 'all')
    setAppSort(isAppSort(sort) ? sort : 'recent')
    setAppSearch(query.get('q') || '')
  }, [])
  const selectedCustomerExample = t.customerExamples.find(item => item.id === selectedExampleId)
  const createReadiness = requirementReadiness(requirement, t)
  const requirementQuestions = requirementIntake?.questions || []
  const requirementAnsweredCount = requirementQuestions.filter(question => requirementQuestionAnswered(question, requirementSelections)).length
  const requirementChoicesComplete = requirementQuestions.every(question => requirementQuestionAnswered(question, requirementSelections))
  const hasRequirementSelections = Object.values(requirementSelections).some(selection => selection.selectedOptionIds.length > 0 || selection.customAnswer.trim())
  const requirementMissingLabels = requirementIntake?.missing?.length
    ? requirementIntake.missing
    : createReadiness.signals.filter(signal => !signal.ready).map(signal => signal.label)
  const requirementCompletionReady = requirementIntake?.status === 'ready' && Boolean(requirementIntake.completed_requirement?.trim())
  const displayedCapabilityContract = requirementIntake?.capability_build_contract || capabilityBuildContract
  const createAction = createActionState(requirement, createReadiness.ready || requirementCompletionReady, busy || requirementIntakeBusy, draftBusy, buildIntentConfirmed, t)
  const recommendedAction = recommendedCreateAction(createAction.id, t)
  const runtimeStatus = classifyRuntimeStatus(runtimeHealth, { authRequired, unavailable: runtimeUnavailable })
  const runtimeStatusText = runtimeStatus === 'connected'
    ? t.runtimeStatusConnected(runtimeVersion(runtimeHealth))
    : runtimeStatus === 'auth_required'
      ? t.runtimeStatusAuthRequired
      : runtimeStatus === 'stale'
        ? t.runtimeStatusStale(runtimeVersion(runtimeHealth))
        : runtimeStatus === 'unavailable'
          ? t.runtimeStatusUnavailable
          : t.runtimeStatusChecking
  const runtimeStatusDetail = runtimeStatus === 'connected'
    ? t.runtimeStatusDetailConnected(runtimeCommit(runtimeHealth))
    : runtimeStatus === 'auth_required'
      ? t.runtimeStatusDetailAuthRequired
      : runtimeStatus === 'stale'
        ? t.runtimeStatusDetailStale
        : runtimeStatus === 'unavailable'
          ? t.runtimeStatusDetailUnavailable
          : t.runtimeStatusDetailChecking
  const appCardReadiness = (item: Application) => [
    { label: t.appCardDraftState, value: `r${item.draft_revision}`, ready: true },
    { label: t.appCardAcceptanceState, value: item.evidence?.state === 'current' ? t.evidenceStateCurrent : item.evidence?.state === 'stale' ? t.evidenceStateStale : t.evidenceStateMissing, ready: item.evidence?.state === 'current' },
    { label: t.appCardPublishState, value: item.active_version ? t.published(item.active_version) : t.draft, ready: Boolean(item.active_version) },
  ]
  const appCardNextAction = (item: Application) => item.active_version
    ? t.appNextActionTryMonitor
    : item.evidence?.state === 'current'
      ? t.appNextActionPublish
      : t.appNextActionRunAcceptance
  const appCardQuickActions = (item: Application): AppQuickAction[] => {
    const state = appReadinessState(item)
    if (state === 'published') return [
      { id: 'try', href: `/runtime/${item.id}`, label: t.appActionTry },
    ]
    if (state === 'ready_to_publish') return [
      { id: 'acceptance', href: `/applications/${item.id}?tab=test`, label: t.appActionAcceptance },
      { id: 'publish_check', href: `/applications/${item.id}?tab=test`, label: t.appActionPublishCheck },
    ]
    return [
      { id: 'edit', href: `/applications/${item.id}?tab=edit`, label: t.appActionEdit },
      { id: 'acceptance', href: `/applications/${item.id}?tab=test`, label: t.appActionAcceptance },
    ]
  }
  const appFilterOptions: Array<{ id: AppFilter; label: string }> = [
    { id: 'all', label: t.appFilterAll },
    { id: 'needs_acceptance', label: t.appFilterNeedsAcceptance },
    { id: 'ready_to_publish', label: t.appFilterReadyToPublish },
    { id: 'published', label: t.appFilterPublished },
  ]
  const appSortOptions: Array<{ id: AppSort; label: string }> = [
    { id: 'recent', label: t.appSortRecent },
    { id: 'readiness', label: t.appSortReadiness },
    { id: 'revision', label: t.appSortRevision },
    { id: 'name', label: t.appSortName },
  ]
  const appFilterCount = (filter: AppFilter) => filter === 'all' ? apps.length : apps.filter(item => appReadinessState(item) === filter).length
  const statusFilteredApps = appFilter === 'all' ? apps : apps.filter(item => appReadinessState(item) === appFilter)
  const normalizedAppSearch = appSearch.trim().toLocaleLowerCase()
  const searchedApps = normalizedAppSearch
    ? statusFilteredApps.filter(item => `${item.name} ${item.display_description || item.description}`.toLocaleLowerCase().includes(normalizedAppSearch))
    : statusFilteredApps
  const visibleApps = [...searchedApps].sort((left, right) => {
    if (appSort === 'recent') return Date.parse(right.updated_at || right.created_at || '') - Date.parse(left.updated_at || left.created_at || '') || right.draft_revision - left.draft_revision || left.name.localeCompare(right.name)
    if (appSort === 'name') return left.name.localeCompare(right.name)
    if (appSort === 'revision') return right.draft_revision - left.draft_revision || left.name.localeCompare(right.name)
    return appReadinessRank(left) - appReadinessRank(right) || right.draft_revision - left.draft_revision || left.name.localeCompare(right.name)
  })
  const currentAppFilterLabel = appFilterOptions.find(option => option.id === appFilter)?.label || t.appFilterAll
  const currentAppSortLabel = appSortOptions.find(option => option.id === appSort)?.label || t.appSortRecent
  const appListViewDirty = appFilter !== 'all' || Boolean(normalizedAppSearch) || appSort !== 'recent'

  const refresh = () => api<Application[]>('/api/v1/applications').then(applications => {
    setApps(applications)
    setAuthRequired(false)
    clearFeedback()
  }).catch(error => {
    if (isAuthError(error)) setAuthRequired(true)
    showError(String(error))
  })
  const refreshRuntimeStatus = () => api<RuntimeHealth>('/health').then(health => {
    setRuntimeHealth(health)
    setRuntimeUnavailable(false)
  }).catch(() => {
    setRuntimeHealth(null)
    setRuntimeUnavailable(true)
  })
  useEffect(() => {
    const stored = globalThis.localStorage?.getItem('foundry.locale')
    if (isLocale(stored)) setLocale(stored)
    setTokenInput(getClientToken())
    void refreshRuntimeStatus()
    void refresh()
  }, [])
  useEffect(() => {
    syncAppListStateFromLocation()
    window.addEventListener('popstate', syncAppListStateFromLocation)
    return () => window.removeEventListener('popstate', syncAppListStateFromLocation)
  }, [syncAppListStateFromLocation])

  function toggleLocale() {
    const value = nextLocale(locale)
    setLocale(value)
    globalThis.localStorage?.setItem('foundry.locale', value)
  }
  function clearCustomerExample() {
    setSelectedExampleId(null)
    setBuildIntentConfirmed(false)
  }

  function updateRequirementOption(question: RequirementIntakeQuestion, optionId: string, checked: boolean) {
    setRequirementSelections(current => {
      const existing = current[question.id] || { selectedOptionIds: [], customAnswer: '' }
      const selectedOptionIds = question.choice_type === 'multi'
        ? checked
          ? Array.from(new Set([...existing.selectedOptionIds, optionId]))
          : existing.selectedOptionIds.filter(id => id !== optionId)
        : checked
          ? [optionId]
          : []
      return { ...current, [question.id]: { ...existing, selectedOptionIds } }
    })
    setBuildIntentConfirmed(false)
  }

  function updateRequirementCustomAnswer(id: string, value: string) {
    setRequirementSelections(current => {
      const existing = current[id] || { selectedOptionIds: [], customAnswer: '' }
      return { ...current, [id]: { ...existing, customAnswer: value } }
    })
    setBuildIntentConfirmed(false)
  }

  async function runRequirementIntake() {
    const text = requirement.trim()
    if (!text) {
      requirementInputRef.current?.focus()
      showError(t.requirementCompletionEmptyRequirement)
      return
    }
    setRequirementIntakeBusy(true)
    clearFeedback()
    try {
      const answers = mergeRequirementIntakeAnswers(
        requirementAnswerHistory,
        requirementIntakeAnswers(requirementQuestions, requirementSelections),
      )
      const result = await api<RequirementIntakeResponse>('/api/v1/requirements/complete', {
        method: 'POST',
        body: JSON.stringify({
          requirement: text,
          locale,
          answers,
          max_questions: 5,
        }),
      })
      setRequirementAnswerHistory(answers)
      setRequirementSelections({})
      setRequirementIntake(result)
      setAuthRequired(false)
      showNotice(result.status === 'ready' ? t.requirementCompletionReadyNotice : t.requirementCompletionNeedsInputNotice)
    } catch (cause) {
      if (isAuthError(cause)) setAuthRequired(true)
      showError(String(cause))
    } finally {
      setRequirementIntakeBusy(false)
    }
  }

  function applyRequirementCompletion() {
    const completed = requirementIntake?.completed_requirement?.trim()
    if (!completed) {
      showError(t.requirementCompletionNoDraft)
      return
    }
    setRequirement(completed)
    setCapabilityBuildContract(requirementIntake?.capability_build_contract || null)
    setSelectedExampleId(null)
    setBuildIntentConfirmed(true)
    setRequirementIntake(null)
    setRequirementSelections({})
    setRequirementAnswerHistory([])
    showNotice(t.requirementCompletionApplied)
  }

  function resetRequirementCompletion() {
    setRequirementSelections({})
    setRequirementAnswerHistory([])
    setRequirementIntake(null)
    setCapabilityBuildContract(null)
    setBuildIntentConfirmed(false)
    clearFeedback()
  }

  async function create(event: FormEvent) {
    event.preventDefault()
    if (!buildIntentConfirmed) {
      setBuildIntentConfirmed(true)
      showNotice(t.buildIntentHomeConfirm)
      return
    }
    setBusy(true)
    clearFeedback()
    let createdApp: Application | null = null
    try {
      const launchContract = displayedCapabilityContract || null
      const name = deriveApplicationName(requirement)
      const app = await api<Application>('/api/v1/applications', {
        method: 'POST',
        body: JSON.stringify({ name, description: deriveApplicationDescription(requirement, launchContract), requirement, mode: 'workflow', capability_build_contract: launchContract }),
      })
      createdApp = app
      setApps(current => [app, ...current.filter(item => item.id !== app.id)])
      setCreatedApplicationId(app.id)
      resetAppListView()
      const build = await launchBuilder(app, requirement, launchContract)
      window.location.href = `/applications/${app.id}?build=${build.build_id}`
    } catch (cause) {
      const context = createdApp ? ` application_id=${createdApp.id}` : ''
      showError(`${String(cause)}${context}`)
      setBusy(false)
    }
  }

  async function saveDraftOnly() {
    setDraftBusy(true)
    clearFeedback()
    try {
      const name = deriveApplicationName(requirement)
      const app = await api<Application>('/api/v1/applications', {
        method: 'POST',
        body: JSON.stringify({ name, description: deriveApplicationDescription(requirement, capabilityBuildContract), requirement, mode: 'workflow', capability_build_contract: capabilityBuildContract }),
      })
      if (isCodexWorkspaceRequirement(requirement) && !capabilityBuildContract) {
        await applyCodexWorkspaceScenario(app)
      } else {
        await seedSafeDraftSkeleton(app.id, app.draft_revision, requirement)
      }
      window.location.href = `/applications/${app.id}?safeDraft=1`
    } catch (cause) {
      showError(String(cause))
      setDraftBusy(false)
    }
  }

  async function runRecommendedCreateAction() {
    if (recommendedAction.disabled) return
    clearFeedback()
    if (recommendedAction.target === 'requirement_focus') {
      requirementInputRef.current?.focus()
      return
    }
    if (recommendedAction.target === 'safe_draft') {
      await saveDraftOnly()
      return
    }
    if (recommendedAction.target === 'guarded_build_button') {
      buildButtonRef.current?.focus()
      showNotice(t.recommendedActionTeamGuardDetail)
    }
  }

  function saveToken(event: FormEvent) {
    event.preventDefault()
    saveClientToken(tokenInput)
    showNotice(t.authSaved)
    void refresh()
  }

  function applyCustomerExample(example: (typeof t.customerExamples)[number]) {
    setRequirement(example.requirement)
    setRequirementSelections({})
    setRequirementAnswerHistory([])
    setRequirementIntake(null)
    setCapabilityBuildContract(null)
    setSelectedExampleId(example.id)
    setBuildIntentConfirmed(false)
    clearFeedback()
  }

  return (
    <main className="home-shell">
      <nav className="topbar"><div className="brand"><span>L</span> Lilies</div><div className="topbar-actions"><button className="lang-toggle" onClick={toggleLocale}>{t.switchLabel}</button><div className={`status-dot runtime-status ${runtimeStatus}`} data-runtime-status={runtimeStatus}><span>{runtimeStatusText}</span><small>{runtimeStatusDetail}</small></div></div></nav>
      <section className="hero">
        <div className="eyebrow">{t.eyebrow}</div>
        <h1>{t.heroTitleA}<br/><em>{t.heroTitleB}</em></h1>
        <p>{t.heroCopy}</p>
        <form className="create-card" onSubmit={create}>
          <textarea ref={requirementInputRef} aria-label={t.requirementAria} value={requirement} onChange={event => { setRequirement(event.target.value); setRequirementIntake(null); setRequirementSelections({}); setRequirementAnswerHistory([]); setCapabilityBuildContract(null); setBuildIntentConfirmed(false) }} />
          {selectedCustomerExample && <section className="selected-scenario-summary" data-selected-scenario-summary="active">
            <div><span>{t.selectedScenarioSummaryTitle} · {selectedCustomerExample.role}</span><strong>{selectedCustomerExample.title}</strong><p>{selectedCustomerExample.need}</p><small>{selectedCustomerExample.acceptanceSignal}</small></div>
            <button onClick={clearCustomerExample} type="button">{t.clearSelectedScenario}</button>
          </section>}
          <section className={`requirement-readiness ${createReadiness.ready ? 'ready' : 'needs-detail'}`} data-requirement-readiness="summary">
            <div className="requirement-readiness-head"><strong>{t.requirementReadinessTitle}</strong><span>{t.requirementReadinessScore(createReadiness.readyCount, createReadiness.total)}</span></div>
            <p>{createReadiness.ready ? t.requirementReadinessReady : t.requirementReadinessNeedsDetail}</p>
            <div className="requirement-readiness-list">{createReadiness.signals.map(signal => <article className={signal.ready ? 'ready' : ''} key={signal.id}>
              <b>{signal.label}</b>
              <small>{signal.detail}</small>
            </article>)}</div>
          </section>
          <section className={`requirement-completion-panel ${requirementCompletionReady ? 'ready' : 'needs-input'}`} data-requirement-completion="ai-workflow-intake" data-requirement-intake-status={requirementIntake?.status || 'not_started'}>
            <div className="requirement-completion-head">
              <div><strong>{t.requirementCompletionTitle}</strong><small>{t.requirementCompletionHelp}</small></div>
              <span>{requirementIntakeBusy ? t.requirementCompletionBusy : requirementIntake ? t.requirementCompletionStatus(requirementIntake.status, requirementIntake.confidence) : t.requirementCompletionNotStarted}</span>
            </div>
            {requirementIntake && <div className="requirement-completion-summary" data-requirement-intake-task={requirementIntake.task_id}>
              {requirementIntake.detected_goal && <p><b>{t.requirementCompletionDetectedGoal}</b>{requirementIntake.detected_goal}</p>}
              {requirementIntake.reasoning_summary && <p><b>{t.requirementCompletionReasoning}</b>{requirementIntake.reasoning_summary}</p>}
            </div>}
            {requirementMissingLabels.length > 0 && <div className="requirement-completion-missing" data-requirement-completion-missing="signals">{requirementMissingLabels.map(label => <span key={label}>{label}</span>)}</div>}
            {requirementQuestions.length > 0 && <div className="requirement-completion-questions">
              {requirementQuestions.map(question => {
                const selection = requirementSelections[question.id] || { selectedOptionIds: [], customAnswer: '' }
                return <article className="requirement-question-card" key={question.id}>
                  <div className="requirement-question-head">
                    <span>{question.label}</span>
                    <small>{question.decision_axis} · {question.choice_type === 'multi' ? t.requirementCompletionMultiChoice : t.requirementCompletionSingleChoice}</small>
                  </div>
                  <p>{question.question}</p>
                  {question.why && <em>{question.why}</em>}
                  <div className="requirement-option-list">
                    {question.options.map(option => {
                      const checked = selection.selectedOptionIds.includes(option.id)
                      return <label className={`requirement-option-card ${checked ? 'selected' : ''}`} key={option.id}>
                        <input
                          type={question.choice_type === 'multi' ? 'checkbox' : 'radio'}
                          name={`requirement-option-${question.id}`}
                          checked={checked}
                          onChange={event => updateRequirementOption(question, option.id, event.target.checked)}
                        />
                        <span><b>{option.label}</b>{option.recommended && <i>{t.requirementCompletionRecommended}</i>}</span>
                        {option.description && <small>{option.description}</small>}
                        {option.impact && <small className="requirement-option-impact">{option.impact}</small>}
                        {Boolean(option.effects?.length) && <span className="requirement-option-effects">
                          {option.effects?.map((effect, index) => <code key={`${effect.axis}-${effect.target_id}-${index}`}>{effect.target_id || effect.axis} · {effect.action}</code>)}
                        </span>}
                      </label>
                    })}
                  </div>
                  {question.custom_allowed && <label className="requirement-custom-answer">
                    <span>{t.requirementCompletionCustomAnswer}</span>
                    <textarea
                      value={selection.customAnswer}
                      placeholder={question.custom_placeholder || question.placeholder || t.requirementCompletionCustomPlaceholder}
                      onChange={event => updateRequirementCustomAnswer(question.id, event.target.value)}
                    />
                  </label>}
                </article>
              })}
            </div>}
            <div className="requirement-completion-plan" data-requirement-completion-plan="workflow-requirement">
              <div><strong>{t.requirementCompletionPlanTitle}</strong><small>{t.requirementCompletionPlanHelp}</small></div>
              {requirementIntake?.completed_requirement
                ? <pre>{requirementIntake.completed_requirement}</pre>
                : <p>{requirementIntake ? t.requirementCompletionNoDraft : t.requirementCompletionStartHint}</p>}
            </div>
            {displayedCapabilityContract && <section className="capability-build-contract" data-capability-build-contract={displayedCapabilityContract.contract_id}>
              <header>
                <div><strong>Capability Build Contract</strong><small>{displayedCapabilityContract.contract_id}</small></div>
                <span>{displayedCapabilityContract.required_envelope} · {displayedCapabilityContract.risk_level} · {displayedCapabilityContract.claim_scope.ceiling}</span>
              </header>
              <p>{displayedCapabilityContract.target_user} · {displayedCapabilityContract.business_goal}</p>
              <div className="capability-contract-groups">
                {([
                  ['F', displayedCapabilityContract.functional_capabilities],
                  ['G', displayedCapabilityContract.runtime_guarantees],
                  ['X', displayedCapabilityContract.external_contracts],
                ] as const).map(([kind, items]) => <article key={kind} data-capability-kind={kind}>
                  <b>{kind}</b>
                  {items.map(item => <span key={item.id}><code>{item.id}</code>{item.title}<small>{item.required_envelope}{item.availability ? ` · ${item.availability}` : ''}</small></span>)}
                </article>)}
              </div>
              <div className="capability-contract-ownership" data-capability-ownership="separated-harness-layers">
                <strong>{locale === 'zh' ? '责任归属' : 'Responsibility ownership'}</strong>
                <dl>
                  {displayedCapabilityContract.platform_coverage.map(coverage => <div key={`${coverage.capability_id}-${coverage.owner}`} data-capability-coverage-owner={coverage.owner}>
                    <dt><code>{coverage.capability_id}</code></dt>
                    <dd>{coverage.owner} · {coverage.status}<small>{coverage.surface}</small></dd>
                  </div>)}
                </dl>
              </div>
              <div className="capability-contract-evidence" data-capability-evidence-plan="scoped">
                <strong>{locale === 'zh' ? '证据目标' : 'Evidence targets'}</strong>
                <dl>
                  {displayedCapabilityContract.evidence_plan.map((evidence, index) => <div key={`${evidence.capability_ids.join('-')}-${index}`}>
                    <dt>{evidence.capability_ids.join(', ')}</dt>
                    <dd>{evidence.target_level} · {evidence.environment} · {evidence.expected_status}<small>{evidence.claim_scope}</small></dd>
                  </div>)}
                </dl>
              </div>
              <footer>
                <span>{displayedCapabilityContract.carrier_decisions.length} carriers</span>
                <span>{displayedCapabilityContract.unresolved_decisions.length} unresolved</span>
                <span>{displayedCapabilityContract.claim_scope.excluded.length} excluded claims</span>
              </footer>
            </section>}
            <div className="requirement-completion-actions">
              <button type="button" className="secondary-action" onClick={resetRequirementCompletion} disabled={!hasRequirementSelections && !requirementIntake}>{t.requirementCompletionReset}</button>
              <button type="button" className="secondary-action" onClick={() => void runRequirementIntake()} disabled={requirementIntakeBusy || !requirement.trim() || (requirementIntake?.status === 'needs_input' && !requirementChoicesComplete)}>
                {requirementIntakeBusy ? t.requirementCompletionBusy : requirementIntake?.status === 'needs_input' ? t.requirementCompletionContinue : t.requirementCompletionRun}
              </button>
              <button type="button" onClick={applyRequirementCompletion} disabled={!requirementCompletionReady}>{t.requirementCompletionApply}</button>
            </div>
            {requirementQuestions.length > 0 && <small className="requirement-completion-count">{t.requirementCompletionQuestionCount(requirementAnsweredCount, requirementQuestions.length)}</small>}
          </section>
          <section className={`create-action-explainer ${createAction.tone}`} data-create-action-state={createAction.id}>
            <strong>{createAction.title}</strong>
            <span>{createAction.detail}</span>
          </section>
          <section className={`recommended-create-action ${recommendedAction.tone}`} data-recommended-create-action={createAction.id} data-recommended-action-target={recommendedAction.target}>
            <div><strong>{t.recommendedActionTitle}</strong><span>{recommendedAction.detail}</span></div>
            <button type="button" disabled={recommendedAction.disabled || busy || draftBusy} onClick={() => void runRecommendedCreateAction()}>{recommendedAction.label}</button>
          </section>
          <div className="create-footer">
            <div className="create-copy"><span>{t.createHint}</span><small>{t.safeDraftHint}</small><small className="build-intent-copy" data-build-intent={buildIntentConfirmed ? 'confirmed' : 'needs-confirmation'}>{buildIntentConfirmed ? t.buildIntentHomeArmed : t.buildIntentHomeSafe}</small></div>
            <div className="create-actions">
              <button className="secondary-action" disabled={busy || draftBusy || requirement.length < 10} onClick={saveDraftOnly} type="button">{draftBusy ? t.saveDraftOnlyBusy : t.saveDraftOnlyButton}</button>
              <button ref={buildButtonRef} className={`build-action ${buildIntentConfirmed ? 'armed' : ''}`} data-build-action="home-start-builder-team" data-build-intent={buildIntentConfirmed ? 'confirmed' : 'needs-confirmation'} disabled={busy || draftBusy || requirement.length < 10}>{busy ? t.createBusy : (locale === 'zh' ? '让莉莉丝搭建' : 'Let Lilies build it')}</button>
            </div>
          </div>
        </form>
        <section className="customer-intake-panel" aria-labelledby="customer-intake-title">
          <div className="customer-intake-head">
            <div>
              <h2 id="customer-intake-title">{t.customerIntakeTitle}</h2>
              <p>{t.customerIntakeHelp}</p>
            </div>
            {selectedCustomerExample && <span>{t.selectedScenarioLabel} · {selectedCustomerExample.role}</span>}
          </div>
          <div className="example-grid">
            {t.customerExamples.map(example => <button
              className={`example-card ${selectedExampleId === example.id ? 'active' : ''}`}
              data-customer-example={example.id}
              key={example.id}
              onClick={() => applyCustomerExample(example)}
              type="button"
            >
              <span className="scenario-chip">{example.role}</span>
              <strong>{example.title}</strong>
              <p>{example.need}</p>
              <small>{example.expectedOutcome}</small>
              <em>{example.acceptanceSignal}</em>
              <b>{t.scenarioUseButton}</b>
            </button>)}
          </div>
        </section>
        {authRequired && <form className="auth-card" onSubmit={saveToken}>
          <div><strong>{t.authTitle}</strong><p>{t.authCopy}</p></div>
          <input type="password" value={tokenInput} placeholder={t.authPlaceholder} onChange={event => setTokenInput(event.target.value)} />
          <div className="auth-actions"><button>{t.authSave}</button><button type="button" className="ghost" onClick={() => { clearClientToken(); setTokenInput('') }}>{t.authClear}</button></div>
        </form>}
        {notice && <div className="success-banner" role="status">{notice}</div>}
        {error && <div className="error-banner" role="alert">{error}</div>}
      </section>
      <section className="customer-section">
        <div className="section-heading"><h2>{t.customerScenariosTitle}</h2><span>{t.customerScenariosHelp}</span></div>
        <div className="scenario-grid">{t.customerScenarios.map(item => <article className="scenario-card" key={item.role}>
          <strong>{item.role}</strong>
          <p>{item.need}</p>
          <small>{item.action}</small>
        </article>)}</div>
        <div className="product-path">{t.productSteps.map(item => <article className="step-card" key={item.title}>
          <b>{item.title}</b>
          <span>{item.text}</span>
        </article>)}</div>
      </section>
      <section className="apps-section" data-app-list-url-state="synced">
        <div className="section-heading"><h2>{t.applications}</h2><span>{t.appCount(apps.length)}</span></div>
        {apps.length > 0 && <div className="app-filter-toolbar" data-app-list-filter="status">
          {appFilterOptions.map(option => <button className={appFilter === option.id ? 'active' : ''} onClick={() => setAppListFilter(option.id)} key={option.id} type="button">
            <span>{option.label}</span><b>{appFilterCount(option.id)}</b>
          </button>)}
        </div>}
        {apps.length > 0 && <div className="app-search-sort" data-app-list-search-sort="controls">
          <input aria-label={t.appSearchLabel} placeholder={t.appSearchPlaceholder} value={appSearch} onChange={event => setAppListSearch(event.target.value)} />
          <label>{t.appSortLabel}<select value={appSort} onChange={event => setAppListSort(event.target.value as AppSort)}>
            {appSortOptions.map(option => <option value={option.id} key={option.id}>{option.label}</option>)}
          </select></label>
        </div>}
        {apps.length > 0 && <div className="app-list-view-state" data-app-list-view-summary="active">
          <span>{t.appListSummaryCount(visibleApps.length, apps.length)}</span>
          <span>{t.appListSummaryFilter(currentAppFilterLabel)}</span>
          {normalizedAppSearch && <span>{t.appListSummarySearch(appSearch.trim())}</span>}
          <span>{t.appListSummarySort(currentAppSortLabel)}</span>
          <div className="app-list-view-actions">
            {normalizedAppSearch && <button onClick={clearAppListSearch} type="button">{t.appListClearSearch}</button>}
            <button disabled={!appListViewDirty} onClick={resetAppListView} type="button">{t.appListResetView}</button>
          </div>
        </div>}
        <div className="app-grid">
          {visibleApps.map(item => <article className="app-card" data-app-card-action-state={appReadinessState(item)} key={item.id}>
            <Link className="app-card-main" href={`/applications/${item.id}`} aria-label={`${t.appActionOpen}: ${item.name}`}>
              <div className="app-icon">{item.name.slice(0, 1).toUpperCase()}</div>
              <div><h3>{item.name}</h3><p>{item.display_description || item.description || t.fallbackDescription}</p>
                <div className="app-readiness" data-app-card-guidance="readiness">{appCardReadiness(item).map(signal => <span className={signal.ready ? 'ready' : ''} key={signal.label}><b>{signal.label}</b>{signal.value}</span>)}</div>
                <small className="app-next-action" data-app-card-guidance="next-action">{appCardNextAction(item)}</small>
              </div>
              <div className="app-meta"><span>{item.active_version ? t.published(item.active_version) : t.draft}</span><span>r{item.draft_revision}</span></div>
            </Link>
            <div className="app-card-actions" data-app-card-quick-actions="navigation">
              {appCardQuickActions(item).map(action => <Link href={action.href} data-app-card-action={action.id} key={action.id}>{action.label}</Link>)}
            </div>
          </article>)}
          {apps.length > 0 && !visibleApps.length && <div className="empty-card"><strong>{normalizedAppSearch ? t.appSearchEmpty : t.appFilterEmpty}</strong><span>{normalizedAppSearch ? t.appSearchEmptyHelp : t.appFilterEmptyHelp}</span></div>}
          {!apps.length && <div className="empty-card"><strong>{t.emptyApps}</strong><span>{t.emptyAppsNextAction}</span></div>}
        </div>
      </section>
    </main>
  )
}
