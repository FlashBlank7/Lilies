from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_runtime_status_tracks_current_patch_line() -> None:
    source = (ROOT / "platform/frontend/lib/runtime-status.ts").read_text(encoding="utf-8")

    assert "expectedRuntimeProductPhase = 'v0.4.x'" in source
    assert "expectedRuntimeVersionPattern = /^v0\\.4\\.\\d+$/" in source
    assert "runtime.product_phase !== expectedRuntimeProductPhase" in source
    assert "expectedRuntimeVersionPattern.test(runtime.version)" in source
    assert "expectedRuntimeVersion = 'v0.3.6'" not in source


def test_home_success_feedback_is_not_rendered_as_an_error() -> None:
    source = (ROOT / "platform/frontend/app/page.tsx").read_text(encoding="utf-8")

    assert "const [notice, setNotice] = useState('')" in source
    assert 'className="success-banner" role="status"' in source
    assert 'className="error-banner" role="alert"' in source
    assert "showNotice(result.status === 'ready'" in source
    assert "setError(result.status === 'ready'" not in source


def test_requirement_intake_keeps_answers_from_every_clarification_round() -> None:
    source = (ROOT / "platform/frontend/app/page.tsx").read_text(encoding="utf-8")
    backend = (ROOT / "platform/backend/src/agent_platform/api.py").read_text(
        encoding="utf-8"
    )

    assert "const [requirementAnswerHistory, setRequirementAnswerHistory]" in source
    assert "function mergeRequirementIntakeAnswers(" in source
    assert "mergeRequirementIntakeAnswers(\n        requirementAnswerHistory" in source
    assert "setRequirementAnswerHistory(answers)" in source
    assert "setRequirementSelections({})" in source
    assert '"answered_question_ids"' in backend
    assert '"answered_decision_axes"' in backend
    assert "prior_answers contains the cumulative selections from every earlier round" in backend
    assert "max_length=32" in backend


def test_customer_runtime_exposes_a_stable_loading_state() -> None:
    source = (ROOT / "platform/frontend/app/runtime/[id]/page.tsx").read_text(encoding="utf-8")
    platform_types = (ROOT / "platform/frontend/lib/platform.ts").read_text(encoding="utf-8")

    assert "data-runtime-loading={loading ? 'true' : 'false'}" in source
    assert "const loadGenerationRef = useRef(0)" in source
    assert "if (generation !== loadGenerationRef.current) return" in source
    assert "if (generation === loadGenerationRef.current) setLoading(false)" in source
    assert "data-runtime-ready={runtimeReady ? 'true' : 'false'}" in source
    assert "data-runtime-run-id={run?.id || ''}" in source
    assert "displaySnapshot?.capability_build_contract?.business_goal" in source
    assert 'data-runtime-purpose="true"' in source
    assert "capability_build_contract?:" in platform_types


def test_customer_runtime_guides_invalid_input_and_guards_retry() -> None:
    source = (ROOT / "platform/frontend/app/runtime/[id]/page.tsx").read_text(
        encoding="utf-8"
    )
    styles = (ROOT / "platform/frontend/app/runtime/[id]/runtime.module.css").read_text(
        encoding="utf-8"
    )

    assert "class RuntimeInputError extends Error" in source
    assert "const startRunLockedRef = useRef(false)" in source
    assert "if (startRunLockedRef.current) return" in source
    assert "startRunLockedRef.current = true" in source
    assert "startRunLockedRef.current = false" in source
    assert "data-runtime-invalid={invalidFieldName === field.name ? 'true' : 'false'}" in source
    assert "aria-invalid={invalidFieldName === field.name ? 'true' : undefined}" in source
    assert "runtimeInputRefs.current.get(caught.fieldName)" in source
    assert "input?.focus()" in source
    assert "if (invalidFieldName === name)" in source
    assert "function customerErrorMessage(error: unknown)" in source
    assert "if (error instanceof Error) return error.message" in source
    assert "if (run.status === 'failed') return ''" in source
    assert "function resultAvailabilityLabel(" in source
    assert "run?.status !== 'failed'" in source
    assert "if (run?.status === 'failed') return '未生成'" in source
    assert 'data-customer-runtime-action="retry"' in source
    assert "disabled={starting || running}" in source
    assert "正在重新运行" in source
    assert '[data-runtime-invalid="true"]' in styles
    assert ".recovery button:disabled" in styles


def test_customer_runtime_renders_serialized_structured_results_for_people() -> None:
    source = (ROOT / "platform/frontend/app/runtime/[id]/page.tsx").read_text(
        encoding="utf-8"
    )

    assert "function serializedStructure(value: string)" in source
    assert "function escapeJsonStringControlCharacters(value: string)" in source
    assert "const parsed: unknown = JSON.parse(trimmed)" in source
    assert "JSON.parse(escapeJsonStringControlCharacters(trimmed))" in source
    assert "classification: '分类结果'" in source
    assert "urgency_level: '紧急程度'" in source
    assert "urgent_level: '紧急程度'" in source
    assert "emergency_level: '紧急程度'" in source
    assert "issue_type: '问题类型'" in source
    assert "problem_type: '问题类型'" in source
    assert "problem_category: '问题类型'" in source
    assert "issue_detail: '问题详情'" in source
    assert "reply_suggestion: '回复建议'" in source
    assert "reasoning: '判断依据'" in source
    assert "urgency_reason: '紧急程度说明'" in source
    assert "reply_rationale: '回复理由'" in source
    assert "trace_log: '处理轨迹'" in source
    assert "step_log: '处理步骤'" in source
    assert "input_summary: '输入摘要'" in source
    assert "output_summary: '步骤结果'" in source
    assert "description: '步骤说明'" in source
    assert "next_step: '下一步'" in source
    assert "next_action: '下一步'" in source
    assert "action: '处理内容'" in source
    assert "structured ? resultText(structured, depth) : value" in source
    assert "function normalizedResultFieldKey(key: string)" in source
    assert "if (/(?:urgency|urgent|emergency|priority|severity)/" in source
    assert "if (/(?:issue|problem|complaint).*" in source
    assert "const structuredEntry = entries.find(" in source
    assert "'tool_use_blocks'," in source
    assert "'node_id'," in source
    assert "RESULT_WRAPPER_FIELDS.has(normalizedResultFieldKey" in source
    assert "return resultText(run.outputs || {})" in source
    assert "Object.values(run.outputs || {})" not in source


def test_application_description_uses_the_business_goal_instead_of_raw_markdown() -> None:
    source = (ROOT / "platform/frontend/app/page.tsx").read_text(encoding="utf-8")

    assert "function deriveApplicationDescription(" in source
    assert "contract?.business_goal?.trim()" in source
    assert "description: deriveApplicationDescription(requirement, capabilityBuildContract)" in source
    assert "description: requirement.slice(0, 180)" not in source


def test_engineer_workflow_overview_uses_plain_business_copy() -> None:
    source = (ROOT / "platform/frontend/app/applications/[id]/page.tsx").read_text(
        encoding="utf-8"
    )

    assert "function readableWorkflowPurpose(" in source
    assert "snapshot.capability_build_contract?.business_goal?.trim()" in source
    assert 'data-workflow-readable-purpose="true"' in source
    assert "detail: safeText(node.description, t.nodeInspectorNoDescription)" in source
    assert "next.join(', ')" not in source[source.index("const workflowStepSummaryItems") : source.index("const selectedBlockDefinition")]


def test_builder_events_publish_persisted_live_progress() -> None:
    backend = (ROOT / "platform/backend/src/agent_platform/builder.py").read_text(encoding="utf-8")
    frontend = (ROOT / "platform/frontend/app/applications/[id]/page.tsx").read_text(
        encoding="utf-8"
    )

    persist = "await self.workflow_store.update_build(build_id, team_state=state)"
    emit = 'await self._emit(build_id, "build.operation"'
    assert backend.index(persist, backend.index("value = await self._execute(")) < backend.index(emit)
    assert '"progress": self._team_progress(state)' in backend
    assert "api<Build>(`/api/v1/builds/${buildId}`)" in frontend
    assert "scheduleBuildRefresh(buildId)" in frontend


def test_acceptance_repair_applies_and_reruns_without_manual_navigation() -> None:
    source = (ROOT / "platform/frontend/app/applications/[id]/page.tsx").read_text(
        encoding="utf-8"
    )
    copy = (ROOT / "platform/frontend/lib/i18n.ts").read_text(encoding="utf-8")

    start = source.index("async function applyAcceptanceRepair()")
    end = source.index("async function reconcileIncomingEdges", start)
    apply_block = source[start:end]
    assert "setStudioTab('test')" in apply_block
    assert "await runTests()" in apply_block
    assert "setStudioTab('edit')" not in apply_block
    assert "acceptanceRepairRef.current?.scrollIntoView" in source
    assert "block: 'start'" in source
    assert "ref={acceptanceRepairRef} tabIndex={-1}" in source
    repair_panel = source[source.index('data-acceptance-repair="failed-gate-preview"') :]
    assert repair_panel.index('className="acceptance-repair-actions"') < repair_panel.index(
        'className="acceptance-repair-body"'
    )
    assert "应用修复并重新验收" in copy


def test_acceptance_results_explain_the_verdict_and_survive_refresh() -> None:
    source = (ROOT / "platform/frontend/app/applications/[id]/page.tsx").read_text(
        encoding="utf-8"
    )
    backend = (
        ROOT / "platform/backend/src/agent_platform/workflow_runtime.py"
    ).read_text(encoding="utf-8")
    storage = (
        ROOT / "platform/backend/src/agent_platform/workflow_storage.py"
    ).read_text(encoding="utf-8")

    assert "function currentAcceptanceReport(" in source
    assert "draft?.evidence?.last_validation_report" in source
    assert "validation.content_hash === draft?.content_hash" in source
    assert 'data-acceptance-outcome=' in source
    assert 'data-acceptance-gate-verdicts="visible"' in source
    assert 'data-acceptance-failure-reasons="visible"' in source
    assert "acceptanceFailedAtBrick" in source
    assert "result.run_error" in source
    assert "acceptanceActualOutput" in source
    assert 'data-acceptance-actual-output=' in source
    assert 'data-acceptance-assertion-comparison="visible"' in source
    assert "acceptanceExpectedValue" in source
    assert "acceptanceActualValue" in source
    assert '"run_error": run_error' in backend
    assert '"outputs": record["outputs"]' in backend
    assert '"failed_node": failed_node' in backend
    assert "workflow error: {run_error}" in backend
    assert "await self.workflow_store.record_test_report(" in backend
    assert "async def record_test_report(" in storage
    assert "SET validation_report_json=?,updated_at=?" in storage
    assert '"latest_validation_failed": latest_validation_failed' in storage
    assert '"code": "failed_evidence"' in storage


def test_markdown_preserves_workflow_identifiers_with_underscores() -> None:
    source = (ROOT / "platform/frontend/lib/markdown.tsx").read_text(encoding="utf-8")

    assert "marker === '_'" in source
    assert "/[\\p{L}\\p{N}]/u.test(previous)" in source
    assert "/[\\p{L}\\p{N}]/u.test(next)" in source
    assert "buffer += marker" in source


def test_home_builder_request_uses_the_same_bounded_defaults_as_the_backend() -> None:
    frontend = (ROOT / "platform/frontend/app/page.tsx").read_text(encoding="utf-8")
    models = (
        ROOT / "platform/backend/src/agent_platform/workflow_models.py"
    ).read_text(encoding="utf-8")

    assert "max_turns: 36" in frontend
    assert "max_repair_cycles: 4" in frontend
    assert "max_elapsed_seconds: 480" in frontend
    assert "Field(default=36, ge=5, le=200)" in models
    assert "Field(default=4, ge=1, le=30)" in models
    assert "Field(default=480.0, ge=0.001, le=86_400)" in models


def test_full_customer_journey_waits_for_real_terminal_state_and_cleans_its_fixture() -> None:
    source = (ROOT / "scripts/human_customer_journey.mjs").read_text(encoding="utf-8")

    assert "'needs_attention'" in source
    assert "Builder did not produce a deliverable" in source
    assert "home_visible" in source
    assert "--smoke-marker" in source
    assert "/smoke-cleanup" in source
    assert "diagnostic-snapshot.json" in source
    assert "const submittedRequirement = smokeMarker" in source
    assert "await client.fill('form.create-card > textarea', submittedRequirement)" in source
    assert "const expectResultContains = args.get('--expect-result-contains')" in source
    assert "const expectedResultHeadings =" in source
    assert "const rejectedResultTerms =" in source
    assert "selections.push(...selection)" in source
    assert "evidence.cleanup.deleted !== true" in source
    assert "async function waitForBuildTerminal" in source
    assert "AbortSignal.timeout(timeoutMs)" in source
    assert "did not respond within ${timeoutMs}ms" in source
    assert "result_headings:" in source
    assert "const internalResultHeadings =" in source
    assert "const topCustomerHeadingLevel =" in source
    assert "const duplicateCustomerHeadings =" in source
    assert "const untranslatedResultHeadings =" in source
    assert "const missingResultHeadings =" in source
    assert "const presentRejectedTerms =" in source
