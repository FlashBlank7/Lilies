export type Position = { x: number; y: number }
export type BuildTranscriptToolCall = {
  tool: string
  arguments: Record<string, unknown>
  result: string
  truncated: boolean
  is_error: boolean
}

export type BuildTranscriptTurn = {
  recorded_at: string
  kind: string
  turn: number
  actor: string
  model: string
  thinking: string
  text: string
  tool_calls: BuildTranscriptToolCall[]
  stop_reason: string | null
  usage: Record<string, number>
  draft_revision: number
}

export type BuildTranscript = {
  build_id: string
  summary: {
    available: boolean
    turn_count: number
    tool_call_count: number
    failed_tool_call_count: number
    actors: string[]
    last_stop_reason: string | null
  }
  records: BuildTranscriptTurn[]
}

export type EvidenceState = 'current' | 'stale' | 'missing'

export type DraftEvidence = {
  state: EvidenceState
  current_hash: string
  last_tested_hash?: string | null
  invalidated_at?: string | null
  invalidated_revision?: number | null
  change_summary: Array<Record<string, unknown>>
  revalidate_endpoint: string
  last_validation_report?: Record<string, unknown>
}

export type PublicationDecision = {
  application_id: string
  allowed: boolean
  requires_confirmation: boolean
  blocked: boolean
  warning_codes: string[]
  warnings: Array<{ code: string; message: string }>
  evidence_state: EvidenceState
  evidence: DraftEvidence
  acknowledged_warnings?: boolean
  decided_at?: string
}

export type WorkflowNode = {
  id: string
  type: string
  block_version: number
  title: string
  description: string
  config: Record<string, unknown>
  position: Position
  retry: { enabled: boolean; max_attempts: number; delay_seconds: number }
  error_strategy: 'fail' | 'continue' | 'error_branch'
}

export type WorkflowEdge = {
  id: string
  source: string
  target: string
  source_port: string
  target_port: string
  branch?: string | null
}

export type LocalLiliesCapabilityItem = {
  id?: string
  title?: string
  description?: string
}

export type Snapshot = {
  name: string
  description: string
  mode: 'workflow' | 'chat'
  requirement: string
  workflow: { nodes: WorkflowNode[]; edges: WorkflowEdge[]; viewport: Record<string, number> }
  agents: Record<string, unknown>
  tests: Array<Record<string, unknown>>
}

export type Draft = {
  application_id: string
  revision: number
  content_hash: string
  tested_hash?: string | null
  validation_report: Record<string, unknown>
  evidence: DraftEvidence
  snapshot: Snapshot
}

export type ModulePort = {
  name: string
  value_type: string
  required: boolean
  description: string
}

export type ModuleKnownBoundary = {
  id: string
  title: string
  description: string
  effect: 'unsupported' | 'blocked_by_environment' | 'degraded' | 'requires_approval'
  capability_ids: string[]
}

export type ReusableModuleContract = {
  schema_version: '1.0'
  capability_ids: string[]
  inputs: ModulePort[]
  outputs: ModulePort[]
  dependencies: Array<{
    module_id: string
    version: number
    capability_ids: string[]
    reason: string
  }>
  required_envelope: 'E0' | 'E1' | 'E2' | 'E3' | 'E4' | 'E5'
  risk_level: 'low' | 'medium' | 'high' | 'critical'
  known_boundaries: ModuleKnownBoundary[]
  claims: Array<{
    capability_id: string
    statement: string
    requested_status: string
    claim_scope: string
  }>
}

export type CapabilityModule = {
  module_id: string
  version: number
  module_ref: string
  content_hash: string
  source: 'builtin' | 'system' | 'user' | 'session_extract'
  status: 'legacy_unverified' | 'draft' | 'verified' | 'deprecated' | 'quarantined'
  created_at: string
  verified_at?: string | null
  verification_errors: string[]
  evidence_record_ids: string[]
  meta: {
    title: string
    description: string
    category: string
    tags: string[]
  }
  contract?: ReusableModuleContract | null
}

export type CapabilityModuleInsertResult = {
  module: CapabilityModule
  inserted_node_ids: string[]
  inserted_edge_ids: string[]
  draft: Draft
}

export type BlockEditorField = {
  path: string
  label: string
  label_zh?: string
  description?: string
  description_zh?: string
  control: 'text' | 'textarea' | 'number' | 'boolean' | 'enum' | 'string_list' | 'json' | 'reference_or_text' | 'readonly'
  required?: boolean
  minimum?: number
  maximum?: number
  step?: number
  options?: string[]
}

export type BlockEditorNotice = {
  kind: 'boundary' | 'expert' | 'warning' | string
  text: string
  text_zh?: string
}

export type Block = {
  type: string
  title: string
  description: string
  category: string
  block_kind?: 'business_workflow' | 'agent_architecture' | 'legacy_compatibility'
  manual_summary?: string
  when_to_use?: string[]
  examples?: Array<Record<string, unknown>>
  anti_patterns?: string[]
  common_errors?: string[]
  claude_architecture_mapping?: string | null
  composability_constraints?: string[]
  editor?: {
    fields?: BlockEditorField[]
    notices?: BlockEditorNotice[]
    i18n?: Record<string, { title?: string; description?: string; category?: string }>
  }
  config_schema: Record<string, unknown>
  input_ports: Array<{ name: string; value_type: string }>
  output_ports: Array<{ name: string; value_type: string }>
}

export type ConnectorManifest = {
  schema_version: '1.0'
  connector_id: string
  version: number
  title: string
  description: string
  domain: string
  operations: Array<{
    id: string
    title: string
    kind: 'read' | 'write' | 'compensate'
    method: string
    path: string
    required_roles: string[]
    compensation_operation_id?: string | null
    parameters?: Array<{
      input_key: string
      wire_name: string
      location: 'path' | 'query' | 'header' | 'cookie'
      required: boolean
    }>
    request_body?: { input_key: string; required: boolean; content_type: string } | null
    success_status_codes?: number[]
    security_requirements?: string[][]
  }>
  deployment_profiles: Array<{
    id: string
    environment: 'mock' | 'test' | 'live' | 'private'
    available: boolean
    claim_ceiling: 'H2' | 'H3' | 'H4' | 'H5'
    excluded_claims: string[]
  }>
  source_provenance?: Record<string, unknown>
}

export type OpenAPICapabilityGap = {
  code: string
  capability: string
  location: string
  message: string
  fatal: boolean
}

export type OpenAPIConnectorGeneration = {
  id: string
  connector_id: string
  version: number
  status: 'generated' | 'verified' | 'registered'
  provenance: {
    source_kind: 'inline' | 'url'
    source_url: string
    source_digest: string
    openapi_version: string
    title: string
    document_version: string
    size_bytes: number
    fetched_at: string
  }
  manifest: ConnectorManifest
  gaps: OpenAPICapabilityGap[]
  discovered_operation_count: number
  generated_operation_count: number
  mapped_field_count: number
  total_field_count: number
  parse_ms: number
  generate_ms: number
  created_at: string
  evidence_stale: boolean
}

export type ConnectorContractCaseResult = {
  case: {
    id: string
    operation_id: string
    kind: 'positive' | 'negative'
    expected: string
    generated_input: Record<string, unknown>
  }
  status: 'passed' | 'failed' | 'skipped' | 'unsupported' | 'blocked_by_environment'
  actual: string
  duration_ms: number
}

export type ConnectorContractRun = {
  id: string
  generation_id: string
  source_digest: string
  status: 'passed' | 'failed' | 'partial' | 'blocked_by_environment'
  results: ConnectorContractCaseResult[]
  passed: number
  failed: number
  skipped: number
  unsupported: number
  blocked_by_environment: number
  attempts: number
  test_ms: number
  time_to_first_valid_contract_ms?: number | null
  created_at: string
}

export type ConnectorBinding = {
  connector_id: string
  connector_version: number
  tenant_id: string
  external_tenant_id: string
  profile_id: string
  secret_ref: string
  application_ids: string[]
  allowed_operations: string[]
  subjects: Array<{ external_subject: string; actor_id: string; roles: string[] }>
  enabled: boolean
  revision: number
}

export type ConnectorPolicy = {
  connector_id: string
  connector_version: number
  tenant_id: string
  domain: string
  allowed_profiles: string[]
  allowed_operations: string[]
  required_roles: string[]
  max_payload_bytes: number
  mutation_preauthorization_required: boolean
  allow_dry_run: boolean
  allow_compensation_during_stop: boolean
  emergency_stop: boolean
  emergency_reason: string
  revision: number
}

export type ConnectorReceipt = {
  execution_id: string
  connector_id: string
  connector_version: number
  tenant_id: string
  profile_id: string
  operation_id: string
  operation_kind: 'read' | 'write' | 'compensate'
  status: 'executing' | 'dry_run' | 'succeeded' | 'failed' | 'compensated'
  side_effect_state: 'none' | 'applied' | 'unknown' | 'compensated'
  external_reference: string
  compensation_available: boolean
  compensation_execution_id: string
  callback_status: string
  replayed: boolean
  created_at: string
  updated_at: string
  claim_scope: string
}

export type ConnectorExecutionPage = {
  items: ConnectorReceipt[]
  offset: number
  limit: number
  has_more: boolean
  claim_boundary: string
}

export type ConnectorExecutionDetail = {
  receipt: ConnectorReceipt
  request_payload: Record<string, unknown>
  response: Record<string, unknown>
  response_hash: string
  error: string
  authorization_id: string
  idempotency_key: string
  actor_id: string
  actor_roles: string[]
  application_id: string
}

export type ConnectorExercise = {
  id: string
  connector_id: string
  connector_version: number
  tenant_id: string
  kind: 'emergency_stop' | 'compensation'
  profile_id: string
  status: 'passed' | 'failed' | 'blocked_by_environment'
  evidence_level: 'H0' | 'H3'
  evidence: Record<string, unknown>
  excluded_claims: string[]
  created_at: string
}

export type GovernanceConnectorOperations = {
  items: ConnectorReceipt[]
  offset: number
  limit: number
  has_more: boolean
  counts: Record<string, number>
  manifests: Array<Record<string, unknown>>
  bindings: Array<Record<string, unknown>>
  policies: Array<Record<string, unknown>>
  exercises: ConnectorExercise[]
  support: Record<string, string>
  claim_boundary: string
}

export type PlatformTaskKind =
  | 'workflow_run'
  | 'builder_build'
  | 'agent_generation'
  | 'agent_turn'
  | 'test_suite'
  | 'scheduler_trigger'
  | 'scheduler_manual_trigger'
  | 'benchmark'
  | 'draft_patch_preview'
  | 'requirement_intake'
  | 'evaluation_run'

export type EvaluationLevel = 'H0' | 'H1' | 'H2' | 'H3' | 'H4' | 'H5'
export type EvaluationExecutionMode = 'plan_only' | 'static' | 'runtime' | 'observation'
export type EvaluationEligibility = 'ready' | 'blocked_by_environment' | 'unsupported'
export type EvaluationOutcome = 'completed' | 'failed' | 'blocked' | 'unsupported'
export type EvaluationVerificationStatus =
  | 'design_only'
  | 'static_verified'
  | 'component_verified'
  | 'integration_verified'
  | 'live_verified'
  | 'production_observed'
  | 'blocked_by_environment'
  | 'unsupported'

export type EvaluationProfile = {
  id: string
  title: string
  description: string
  level: EvaluationLevel
  maximum_status: EvaluationVerificationStatus
  compatible_environment_kinds: string[]
  execution_mode: EvaluationExecutionMode
  workflow_execution_allowed: boolean
  draft_test_apply_allowed: boolean
  external_mutation_allowed: boolean
  required_evidence_categories: string[]
  excluded_claims: string[]
}

export type EvaluationEnvironment = {
  id: string
  title: string
  description: string
  kind: string
  availability: 'available' | 'unavailable' | 'unknown'
  execution_mode: EvaluationExecutionMode
  workflow_execution_allowed: boolean
  external_mutation_allowed: boolean
  compatible_profile_ids: string[]
  evidence_sources: string[]
  missing_requirements: string[]
  claim_ceiling: EvaluationVerificationStatus
}

export type EvaluationCasePlan = {
  id: string
  family: string
  title: string
  capability_ids: string[]
  capability_kind: 'F' | 'G' | 'X' | 'compatibility'
  executable: boolean
  blockers: string[]
  required_signals: string[]
  test: Record<string, unknown>
}

export type EvaluationPlan = {
  schema_version: '1.0'
  application_id: string
  draft_revision: number
  draft_content_hash: string
  capability_contract_id?: string | null
  profile: EvaluationProfile
  environment: EvaluationEnvironment
  eligibility: EvaluationEligibility
  blockers: string[]
  warnings: string[]
  cases: EvaluationCasePlan[]
  generated_tests: Array<Record<string, unknown>>
  existing_test_ids: string[]
  required_capability_ids: string[]
  covered_capability_ids: string[]
  claim_ceiling: EvaluationVerificationStatus
  verified_claim_candidates: string[]
  excluded_claims: string[]
}

export type EvaluationRunRecord = {
  schema_version: '1.0'
  id: string
  application_id: string
  platform_task_id: string
  draft_revision: number
  draft_content_hash: string
  capability_contract_id?: string | null
  profile_id: string
  profile_level: EvaluationLevel
  environment_id: string
  environment_kind: string
  execution_mode: EvaluationExecutionMode
  eligibility: EvaluationEligibility
  outcome: EvaluationOutcome
  achieved_status: EvaluationVerificationStatus
  passed?: boolean | null
  generated_test_ids: string[]
  executed_test_ids: string[]
  capability_results: Array<Record<string, unknown>>
  blockers: string[]
  verified_claims: string[]
  excluded_claims: string[]
  report: Record<string, unknown>
  created_at: string
  updated_at: string
}

export type PlatformTaskStatus = 'queued' | 'running' | 'paused' | 'succeeded' | 'failed' | 'cancelled'

export type PlatformUsageRecord = {
  usage_type: string
  amount: number
  metadata: Record<string, unknown>
  created_at: string
}

export type PlatformTaskRecord = {
  id: string
  kind: PlatformTaskKind
  owner_id: string
  resource_id: string
  status: PlatformTaskStatus
  parent_task_id?: string | null
  metadata: Record<string, unknown>
  usage_counts: Record<string, number>
  usage: PlatformUsageRecord[]
  error: string
  worker_id?: string | null
  lease_expires_at?: string | null
  lease_version?: number
  created_at: string
  updated_at: string
  finished_at?: string | null
}

export type GovernanceSupport = 'reported' | 'estimated' | 'unsupported' | 'not_recorded'

export type GovernanceTask = PlatformTaskRecord & {
  application_id?: string | null
  application_name?: string | null
  workflow_id?: string | null
  model?: string | null
  duration_seconds?: number | null
  queue_delay_seconds?: number | null
}

export type GovernanceTaskPage = {
  items: GovernanceTask[]
  total: number
  offset: number
  limit: number
  has_more: boolean
  filters: Record<string, unknown>
  support: Record<string, GovernanceSupport>
}

export type GovernanceOverview = {
  generated_at: string
  task_counts: Record<string, number>
  duration_seconds: { p50?: number | null; p95?: number | null; support: GovernanceSupport }
  queue_delay_seconds: { p50?: number | null; p95?: number | null; support: GovernanceSupport }
  workers: { total: number; active: number; stale: number }
  durable_jobs: {
    observed: number
    observation_limit: number
    active: number
    queued: number
    retry_wait: number
    paused: number
    succeeded: number
    failed: number
    cancelled: number
  }
  recent_failures: GovernanceTask[]
  alerts: GovernanceAlert[]
  claim_boundary: string
}

export type GovernanceUsageSample = {
  created_at: string
  task_id: string
  owner_id?: string | null
  application_id?: string | null
  workflow_id?: string | null
  provider?: string | null
  model?: string | null
  input_tokens?: number | null
  output_tokens?: number | null
  cache_read_input_tokens?: number | null
  cache_creation_input_tokens?: number | null
  reasoning_tokens?: number | null
  cost_usd?: number | null
  cost_source?: string
  support?: Record<string, GovernanceSupport | 'not_reported'>
  budget?: Record<string, unknown>
}

export type GovernanceUsage = {
  samples: GovernanceUsageSample[]
  sample_count: number
  returned_sample_count: number
  has_more: boolean
  totals: Record<string, number | null>
  support: Record<string, GovernanceSupport>
  series: Array<Record<string, number | string>>
  interval: 'hour' | 'day'
  dimensions: Record<string, Array<Record<string, string | number>>>
  budgets: Array<Record<string, unknown>>
  cost_boundary: string
  token_boundary: string
}

export type GovernanceReliability = {
  metrics: Record<string, number>
  examples: Record<string, string[]>
  workers: Array<Record<string, unknown>>
  queue: Record<string, unknown>
  support: Record<string, GovernanceSupport>
}

export type GovernanceTraceNode = GovernanceTask & { children: GovernanceTraceNode[] }

export type GovernanceTrace = {
  requested_task_id: string
  root_task_id: string
  ancestors: string[]
  tree: GovernanceTraceNode
  spans: Array<Record<string, unknown>>
  durable_job?: {
    job?: DurableJob
    attempts?: DurableJobAttempt[]
    events?: DurableJobEvent[]
    receipts?: CollectionReceipt[]
    job_id?: string
    missing?: boolean
    reason?: string
  } | null
  support: Record<string, GovernanceSupport>
}

export type GovernancePolicy = {
  controls: PlatformPolicyControls
  audit: Array<{ id: number; type: string; created_at: string; data: Record<string, unknown> }>
  support: Record<string, GovernanceSupport>
}

export type GovernanceCapability = {
  capability_id: string
  strongest_status: string
  evidence_level: string
  claim_count: number
  artifact_categories: string[]
  known_gaps: Array<Record<string, unknown>>
  integrity: string
}

export type GovernanceEvidence = {
  capabilities: GovernanceCapability[]
  records: Array<Record<string, unknown>>
  support: Record<string, GovernanceSupport>
  claim_boundary: string
}

export type GovernanceAlert = {
  id: string
  detector: string
  severity: string
  status: string
  source_timestamp?: string
  task_id?: string
  job_id?: string
  application_id?: string | null
  owner_id?: string | null
  worker_id?: string
  message: string
  source: string
}

export type DurableJobStatus = 'queued' | 'running' | 'retry_wait' | 'paused' | 'succeeded' | 'failed' | 'cancelled'

export type DurableJob = {
  id: string
  idempotency_key: string
  application_id: string
  application_name?: string | null
  version: number
  node_id: string
  trigger_kind: 'schedule' | 'manual' | 'event'
  local_date: string
  status: DurableJobStatus
  payload: Record<string, unknown>
  attempt_count: number
  max_attempts: number
  retry_backoff_seconds: number
  next_attempt_at: string
  lease_owner?: string | null
  lease_expires_at?: string | null
  lease_version: number
  run_id?: string | null
  platform_task_id?: string | null
  checkpoint: Record<string, unknown>
  result: Record<string, unknown>
  error: string
  alert?: Record<string, unknown> | null
  cancel_requested: boolean
  revision: number
  receipt_count?: number
  lease_expired?: boolean
  created_at: string
  updated_at: string
  started_at?: string | null
  finished_at?: string | null
  attempts?: DurableJobAttempt[]
}

export type DurableJobAttempt = {
  job_id: string
  attempt_number: number
  status: 'running' | 'paused' | 'succeeded' | 'failed' | 'cancelled'
  worker_id: string
  lease_version: number
  platform_task_id: string
  run_id?: string | null
  error: string
  started_at: string
  finished_at?: string | null
}

export type DurableJobEvent = {
  sequence: number
  job_id: string
  event_type: string
  data: Record<string, unknown>
  created_at: string
}

export type CollectionReceipt = {
  id: string
  job_id: string
  application_id: string
  run_id: string
  source_key: string
  requested_url: string
  final_url: string
  canonical_url: string
  host: string
  permission_basis: string
  robots_checked: boolean
  robots_allowed?: boolean | null
  status: 'new' | 'changed' | 'unchanged' | 'denied' | 'oversized' | 'failed'
  http_status?: number | null
  content_type: string
  content_bytes: number
  content_hash: string
  title: string
  excerpt: string
  error: string
  collected_at: string
  created_at: string
  updated_at: string
}

export type ScheduleStatus = {
  application_id: string
  status: 'not_configured' | 'draft_unpublished' | 'active'
  draft_has_schedule: boolean
  schedule: {
    application_id: string
    application_name: string
    version: number
    node_id: string
    timezone: string
    hour: number
    minute: number
    durable: boolean
    max_attempts: number
    retry_backoff_seconds: number
    lease_seconds: number
    next_fire_at: string
    last_fire?: Record<string, unknown> | null
    latest_job?: DurableJob | null
  } | null
  job_count: number
  active_job_count: number
  latest_job?: DurableJob | null
  latest_alert?: Record<string, unknown> | null
}

export type GovernanceDurableJobs = {
  items: DurableJob[]
  observed: number
  observation_limit: number
  offset: number
  counts: Record<string, number>
  support: Record<string, GovernanceSupport>
  claim_boundary: string
}

export type GovernanceAlerts = {
  items: GovernanceAlert[]
  total: number
  support: Record<string, GovernanceSupport>
}

export type PlatformPolicyDecision = {
  id: string
  label: string
  surface: string
  server_name: string
  platform_policy: string
  agent_network_policy: string
  sandbox_network_policy?: string | null
  allowed: boolean
  mode: string
  reason: string
  operator_action: string
}

export type PlatformPolicyControls = {
  network_egress_policy: string
  network_egress_allowlist: string[]
  cancellation_policy: 'enabled' | 'disabled'
  secret_policy_enabled: boolean
  worker_id: string
  worker_lease_seconds: number
  limits: Record<string, number>
  e08_boundary: {
    current_slice: string
    source: string
    comparison_evidence: string
    soft_passmode: {
      layer: string
      enforcement: string
      statement: string
    }
    hard_boundary: {
      layer: string
      enforcement: string
      statement: string
    }
    not_full_sidecar_completion: boolean
    remaining_full_boundary: string[]
    controls: Array<{
      id: string
      label: string
      layer: string
      status: string
      value: unknown
    }>
  }
  stdio_mcp: {
    sandboxed_no_network_supported: boolean
    allowlist_supported: boolean
    decisions: PlatformPolicyDecision[]
  }
}

export type PlatformPolicyControlsUpdate = {
  network_egress_policy?: 'full' | 'allowlist' | 'none'
  network_egress_allowlist?: string[]
  cancellation_policy?: 'enabled' | 'disabled'
  secret_policy_enabled?: boolean
  worker_lease_seconds?: number
  limits?: Record<string, number>
  reason: string
}

export type PlatformPolicyControlsUpdateResponse = {
  before: PlatformPolicyControls
  after: PlatformPolicyControls
  audit: {
    version: string
    action: string
    reason: string
    changed_fields: string[]
    not_persistent_across_restart: boolean
    not_full_sidecar_completion: boolean
  }
}

export type DraftPatchOperation = {
  expected_revision?: number
  op: string
  data: Record<string, unknown>
}

export type DraftPatchPreview = {
  task_id: string
  supported: boolean
  intent: 'rename_node' | 'update_node_description' | 'remove_disconnected_node' | 'update_workflow_metadata' | 'update_workflow_requirement' | 'update_start_inputs' | 'unsupported'
  message: string
  operations: DraftPatchOperation[]
  warnings: string[]
  reference_node_ids?: string[]
}

export type NaturalLanguageWorkflowEditResult = {
  task_id: string
  supported: boolean
  applied: boolean
  intent: string
  message: string
  operations: DraftPatchOperation[]
  warnings: string[]
  node_ids: string[]
  edge_ids: string[]
  expected_revision: number
  expected_content_hash: string
  preview_source: string
  preview_digest: string
  draft: Draft
  evidence: DraftEvidence
}

export type AcceptanceRepairPreview = {
  task_id: string
  supported: boolean
  message: string
  operations: DraftPatchOperation[]
  warnings: string[]
  fixes: Array<Record<string, unknown>>
  missing_node_types: string[]
  unsupported_node_types: string[]
  expected_revision: number
  expected_content_hash: string
  instruction: string
  rationale_markdown: string
  repair_context: {
    test_id: string
    test_name: string
    requirement: string
    failed_assertions: Array<Record<string, unknown>>
    failed_checks: string[]
    required_node_types: string[]
    required_tool_nodes: string[]
    required_tools: string[]
    run_id: string
    trace_excerpts: string[]
    relevant_node_ids: string[]
    current_revision: number
    current_content_hash: string
  }
  reference_node_ids: string[]
  preview_source: string
  workflow_edit_preview?: DraftPatchPreview | null
}

export type BuilderBenchmarkHistoryRecord = {
  id: string
  status: string
  owner_id: string
  resource_id: string
  created_at: string
  updated_at: string
  finished_at?: string | null
  metadata: Record<string, unknown>
  usage_counts: Record<string, number>
  error: string
}

export type AdaptiveMonitoringCase = {
  family: string
  mode: string
  build_status: string
  effective_depth: string
  reuse_depth_source: string
  benchmark_passed: boolean | null
  timeout_like: boolean
  available_overrides: string[]
  source: string
}

export type AdaptiveMonitoringRefreshRecord = {
  refreshed_at: string
  status: string
  critical_alert_count: number
  warning_alert_count: number
  override_options_visible: boolean
  source: string
  source_generated_at?: string | null
}

export type AdaptiveMonitoringStatus = {
  status: 'healthy' | 'attention' | 'missing_evidence'
  version: string
  source: string
  generated_at?: string | null
  critical_alert_count: number
  warning_alert_count: number
  override_options_visible: boolean
  available_overrides: string[]
  cases: AdaptiveMonitoringCase[]
  alerts: Array<Record<string, unknown>>
  conclusion: string
  last_refresh?: AdaptiveMonitoringRefreshRecord | null
  history: AdaptiveMonitoringRefreshRecord[]
  history_count: number
  history_path: string
}

export type GovernedMemoryStatus = 'active' | 'revoked' | 'expired'

export type GovernedMemoryPermission = {
  actor_id: string
  owner_id: string
  scope_id: string
  purpose: string
  allowed_operations: Array<'create' | 'read' | 'update' | 'revoke' | 'expire'>
  expires_at?: string | null
}

export type GovernedMemorySource = {
  source_type: string
  source_id: string
  captured_at?: string
  evidence_text: string
  evidence_hash?: string
}

export type GovernedMemoryItem = {
  id: string
  owner_id: string
  scope_id: string
  content: string
  source: GovernedMemorySource
  retention_class: 'session' | 'project' | 'user_renewable'
  expires_at: string
  status: GovernedMemoryStatus
  created_at: string
  updated_at: string
  revoked_at?: string | null
  revoked_reason: string
}

const root = '/api/platform'
const tokenKey = 'foundry.apiToken'

function apiErrorRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function findApiErrorString(value: unknown, key: string, depth = 0): string | undefined {
  if (depth > 6) return undefined
  const record = apiErrorRecord(value)
  if (!record) return undefined
  const direct = record[key]
  if (typeof direct === 'string' && direct) return direct
  const preferredChildren = ['detail', 'error', 'data', 'assignment', 'context']
  for (const childKey of preferredChildren) {
    const found = findApiErrorString(record[childKey], key, depth + 1)
    if (found) return found
  }
  for (const [childKey, child] of Object.entries(record)) {
    if (preferredChildren.includes(childKey)) continue
    const found = findApiErrorString(child, key, depth + 1)
    if (found) return found
  }
  return undefined
}

function parseApiErrorBody(text: string): unknown {
  if (!text) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    return text
  }
}

export class PlatformApiError extends Error {
  readonly status: number
  readonly statusText: string
  readonly body: unknown
  readonly detail: unknown
  readonly code: string
  readonly application_id?: string
  readonly assignment_id?: string
  readonly build_id?: string
  readonly session_id?: string

  constructor(status: number, statusText: string, body: unknown) {
    const bodyRecord = apiErrorRecord(body)
    const detail = bodyRecord && 'detail' in bodyRecord ? bodyRecord.detail : body
    const code = findApiErrorString(detail, 'code') || findApiErrorString(body, 'code') || 'http_error'
    const remoteMessage = findApiErrorString(detail, 'message')
      || (typeof detail === 'string' ? detail : '')
    const identifiers = {
      application_id: findApiErrorString(detail, 'application_id') || findApiErrorString(body, 'application_id'),
      assignment_id: findApiErrorString(detail, 'assignment_id') || findApiErrorString(body, 'assignment_id'),
      build_id: findApiErrorString(detail, 'build_id') || findApiErrorString(body, 'build_id'),
      session_id: findApiErrorString(detail, 'session_id') || findApiErrorString(body, 'session_id'),
    }
    const identifierText = Object.entries(identifiers)
      .filter((entry): entry is [string, string] => Boolean(entry[1]))
      .map(([key, value]) => `${key}=${value}`)
      .join(' ')
    super(`${status} ${statusText}${code ? ` [${code}]` : ''}${remoteMessage ? `: ${remoteMessage}` : ''}${identifierText ? ` (${identifierText})` : ''}`)
    this.name = 'PlatformApiError'
    this.status = status
    this.statusText = statusText
    this.body = body
    this.detail = detail
    this.code = code
    this.application_id = identifiers.application_id
    this.assignment_id = identifiers.assignment_id
    this.build_id = identifiers.build_id
    this.session_id = identifiers.session_id
  }
}

export function getClientToken() {
  if (typeof window === 'undefined') return ''
  return window.localStorage.getItem(tokenKey) || ''
}

export function saveClientToken(token: string) {
  if (typeof window === 'undefined') return
  const value = token.trim()
  if (value) window.localStorage.setItem(tokenKey, value)
  else window.localStorage.removeItem(tokenKey)
}

export function clearClientToken() {
  if (typeof window === 'undefined') return
  window.localStorage.removeItem(tokenKey)
}

export function isAuthError(error: unknown) {
  if (error instanceof PlatformApiError) {
    return error.status === 401 || ['invalid_api_token', 'unauthorized'].includes(error.code)
  }
  return String(error).includes('401') || String(error).toLowerCase().includes('invalid api token')
}

export function withFrontendToken(path: string) {
  const token = getClientToken()
  if (!token) return path
  const separator = path.includes('?') ? '&' : '?'
  return `${path}${separator}frontend_token=${encodeURIComponent(token)}`
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getClientToken()
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    ...(token ? { 'X-Agent-Platform-Token': token } : {}),
    ...(init?.headers || {}),
  }
  const response = await fetch(`${root}${path}`, {
    ...init,
    cache: 'no-store',
    headers,
  })
  if (!response.ok) {
    const body = parseApiErrorBody(await response.text())
    throw new PlatformApiError(response.status, response.statusText, body)
  }
  return response.json() as Promise<T>
}

export function idempotency() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
}
