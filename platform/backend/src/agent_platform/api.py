from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import PRODUCT_PHASE, __version__
from .config import Settings, get_settings
from .applications import ApplicationService
from .agent_runtime_factory import build_agent_runtime_core
from .adaptive_monitoring import (
    adaptive_monitoring_refresh_loop,
    adaptive_monitoring_schedule_status,
    adaptive_monitoring_status_with_history,
    record_adaptive_monitoring_refresh,
)
from .blocks import BlockRegistry, build_block_registry
from .builder import WorkflowBuilder
from .builder_benchmark import BuilderBenchmark, BuilderBenchmarkCase, BuilderBenchmarkSuiteCase
from .capability_contracts import (
    CapabilityBuildContract,
    VerificationStatus,
    capability_contract_routing,
    complete_capability_contract_scaffolding,
    evaluate_capability_contract,
    legacy_intake_capability_contract,
    reference_capability_contracts,
    render_workflow_build_plan,
)
from .capability_evidence import (
    ArtifactCategory,
    CapabilityEvidenceCreateRequest,
)
from .complexity_router import (
    classify_requirement,
    complexity_router_default_safety_gate,
    limited_default_enablement_plan_status,
    operator_override_plan_status,
    requirement_classification_contract_status,
    rollout_metrics_prerequisites_status,
    runtime_activation_for_build,
    runtime_activation_rollout_metrics,
    validate_operator_override,
)
from .connector_sdk import (
    ConnectorAdapterError,
    ConnectorCallback,
    ConnectorConflict,
    ConnectorDenied,
    ConnectorDomainPolicy,
    ConnectorEmbeddingEnvelope,
    ConnectorExecutionRequest,
    ConnectorManifest,
    ConnectorService,
    ConnectorTenantBinding,
)
from .factory import AgentFactory
from .evaluation_harness import (
    EvaluationApplyRequest,
    EvaluationHarness,
    EvaluationPlanRequest,
    EvaluationRunRequest,
)
from .draft_patch_preview import (
    DraftPatchPreviewer,
    DraftPatchPreviewRequest,
    DraftPatchPreviewResponse,
)
from .durable_jobs import DurableJobConflict, DurableJobStore
from .acceptance_repair import (
    AcceptanceRepairApplyRequest,
    AcceptanceRepairPreviewer,
    AcceptanceRepairPreviewRequest,
)
from .governed_memory import (
    GovernedMemoryPermission,
    GovernedMemorySource,
    GovernedMemorySurface,
    GovernedMemoryViolation,
    MemoryStatus,
    RetentionClass,
)
from .governance import (
    GovernanceService,
    GovernanceTaskFilters,
    ensure_platform_capability_evidence,
)
from .models import (
    ChatMessage,
    ContentBlock,
    GenerationRequest,
    MessageRequest,
    PermissionDecision,
    SessionCreateRequest,
)
from .openapi_connector import (
    ConnectorContractRunRequest,
    OpenAPIMaterialError,
    OpenAPIConnectorGenerationRequest,
    OpenAPIConnectorService,
)
from .permissions import PermissionBroker
from .platform_harness import PlatformHarness, PlatformHarnessViolation
from .providers import ModelProvider
from .runtime import AgentRuntime
from .reference_modules import ensure_codex_reference_module
from .sandbox import SandboxManager
from .scenarios import ScenarioCatalog
from .scheduler import WorkflowScheduler
from .storage import Storage
from .template_models import TemplateCreateRequest
from .template_strategy import (
    ALLOWED_REUSE_DEPTHS,
    build_suggestion_payload,
    score_template_matches,
    suggestion_default_metadata,
)
from .template_store import TemplateStore
from .tools import ToolRegistry
from .workflow_models import (
    ApplicationCreateRequest,
    ApplicationSnapshot,
    BuildRequest,
    DraftOperation,
    ResumeRunRequest,
    ManualScheduleTriggerRequest,
    PublishApplicationRequest,
    WorkflowRunRequest,
)
from .workflow_runtime import WorkflowRuntime
from .workflow_storage import PublishGateError, RevisionConflict, WorkflowStorage
from .web_collection import ControlledWebCollector


RUNTIME_ROUTE_CHECKS: dict[str, tuple[str, str]] = {
    "health": ("GET", "/health"),
    "applications_list": ("GET", "/api/v1/applications"),
    "applications_create": ("POST", "/api/v1/applications"),
    "application_detail": ("GET", "/api/v1/applications/{application_id}"),
    "draft_detail": ("GET", "/api/v1/applications/{application_id}/draft"),
    "smoke_cleanup": ("POST", "/api/v1/applications/{application_id}/smoke-cleanup"),
    "requirement_intake": ("POST", "/api/v1/requirements/complete"),
    "scenario_catalog": ("GET", "/api/v1/scenarios"),
    "scenario_apply": ("POST", "/api/v1/applications/{application_id}/scenarios/{scenario_id}/apply"),
    "capability_contract_validate": ("POST", "/api/v1/capability-contracts/validate"),
    "application_capability_contract": ("GET", "/api/v1/applications/{application_id}/capability-contract"),
    "capability_modules": ("GET", "/api/v1/capability-modules"),
    "capability_evidence": ("GET", "/api/v1/capability-evidence"),
    "governance_overview": ("GET", "/api/v1/governance/overview"),
    "governance_usage": ("GET", "/api/v1/governance/usage"),
    "evaluation_profiles": ("GET", "/api/v1/evaluation/profiles"),
    "evaluation_plan": ("POST", "/api/v1/applications/{application_id}/evaluation/plan"),
    "durable_jobs": ("GET", "/api/v1/applications/{application_id}/durable-jobs"),
    "connector_manifests": ("GET", "/api/v1/connectors/manifests"),
    "connector_generations": ("GET", "/api/v1/connectors/generations"),
    "connector_test_run": ("POST", "/api/v1/applications/{application_id}/connector-test-runs"),
    "embedding_invoke": ("POST", "/api/v1/embedding/invoke"),
    "governance_connectors": ("GET", "/api/v1/governance/connectors"),
}


def _repo_root() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _git_text(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _git_has_output(repo_root: Path, *args: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(result.stdout.strip())


def _git_differs(repo_root: Path, *args: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 1


@lru_cache(maxsize=1)
def runtime_git_identity() -> dict[str, str | bool]:
    repo_root = _repo_root()
    if repo_root is None:
        return {
            "commit": "unknown",
            "branch": "unknown",
            "tracked_dirty": False,
            "untracked_present": False,
        }
    return {
        "commit": _git_text(repo_root, "rev-parse", "--short", "HEAD"),
        "branch": _git_text(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
        "tracked_dirty": (
            _git_differs(repo_root, "diff", "--quiet", "HEAD", "--")
            or _git_differs(repo_root, "diff", "--cached", "--quiet", "--")
        ),
        "untracked_present": _git_has_output(repo_root, "ls-files", "--others", "--exclude-standard"),
    }


def route_available(app: FastAPI, method: str, path: str) -> bool:
    for route in app.routes:
        route_path = getattr(route, "path", "")
        route_methods = set(getattr(route, "methods", set()) or set())
        if route_path == path and method.upper() in route_methods:
            return True
    return False


def route_availability(app: FastAPI) -> dict[str, bool]:
    return {
        name: route_available(app, method, path)
        for name, (method, path) in RUNTIME_ROUTE_CHECKS.items()
    }


@dataclass(slots=True)
class Services:
    settings: Settings
    storage: Storage
    provider: ModelProvider
    tools: ToolRegistry
    sandboxes: SandboxManager
    permissions: PermissionBroker
    runtime: AgentRuntime
    factory: AgentFactory
    blocks: BlockRegistry
    workflow_store: WorkflowStorage
    durable_jobs: DurableJobStore
    harness: PlatformHarness
    connectors: ConnectorService
    openapi_connectors: OpenAPIConnectorService
    applications: ApplicationService
    workflow_runtime: WorkflowRuntime
    evaluation_harness: EvaluationHarness
    builder: WorkflowBuilder
    scheduler: WorkflowScheduler
    templates: TemplateStore
    governance: GovernanceService
    scenarios: ScenarioCatalog
    benchmark: BuilderBenchmark
    draft_patcher: DraftPatchPreviewer
    acceptance_repairer: AcceptanceRepairPreviewer
    governed_memory: GovernedMemorySurface
    worker_supervisor: Any | None
    worker_process_manager: Any | None
    background_tasks: set[asyncio.Task[Any]]


class PlatformTaskLeaseRequest(BaseModel):
    worker_id: str | None = None
    lease_seconds: float | None = Field(default=None, gt=0)


class DurableJobActionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class PlatformTaskLeaseReleaseRequest(BaseModel):
    worker_id: str | None = None
    next_status: Literal["queued", "running"] = "queued"


class PlatformWorkerSupervisionStartRequest(BaseModel):
    poll_seconds: float | None = Field(default=None, gt=0)
    limit: int | None = Field(default=None, ge=1, le=500)


class PlatformWorkerProcessStartRequest(BaseModel):
    command: list[str] | None = None
    cwd: str | None = None


class SmokeCleanupRequest(BaseModel):
    smoke_marker: str = Field(pattern=r"^v\d+\.\d+\.\d+-smoke$")
    dry_run: bool = True


class ScenarioApplyRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    expected_content_hash: str = Field(min_length=64, max_length=64)
    replace_existing: bool = False
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)


class ModuleInsertRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    expected_content_hash: str = Field(min_length=64, max_length=64)
    prefix: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    x: float = 0
    y: float = 0
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)


class PlatformSecretCreateRequest(BaseModel):
    owner_id: str
    name: str
    value: str = Field(min_length=1, repr=False)
    description: str = ""


class ConnectorBindingUpsertRequest(BaseModel):
    binding: ConnectorTenantBinding
    expected_revision: int = Field(default=0, ge=0)


class ConnectorPolicyUpsertRequest(BaseModel):
    policy: ConnectorDomainPolicy
    expected_revision: int = Field(default=0, ge=0)


class ConnectorEmergencyStopRequest(BaseModel):
    enabled: bool
    reason: str = Field(min_length=3, max_length=1000)
    expected_revision: int = Field(ge=1)


class ConnectorAuthorizationCreateRequest(BaseModel):
    connector_id: str
    connector_version: int = Field(ge=1)
    tenant_id: str
    actor_id: str
    profile_id: str
    operation_id: str
    payload: dict[str, Any]
    expires_in_seconds: int = Field(default=300, ge=1, le=3600)
    max_uses: int = Field(default=1, ge=1, le=100)


class ConnectorCompensationRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=300)
    actor_roles: list[str] = Field(min_length=1, max_length=40)
    authorization_id: str = ""
    idempotency_key: str = Field(min_length=1, max_length=300)


class ConnectorExerciseRequest(BaseModel):
    connector_id: str
    connector_version: int = Field(ge=1)
    tenant_id: str
    kind: Literal["emergency_stop", "compensation"]
    execution_id: str = ""


class ConnectorTestRunRequest(BaseModel):
    request: dict[str, Any]
    idempotency_key: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=1,
        max_length=300,
    )
    use_draft: bool = False


class GovernedMemoryCreateRequest(BaseModel):
    permission: GovernedMemoryPermission
    content: str = Field(min_length=1, max_length=20_000)
    source: GovernedMemorySource
    retention_class: RetentionClass
    reason: str = Field(min_length=1, max_length=1000)
    expires_at: str | None = None


class GovernedMemoryReadRequest(BaseModel):
    permission: GovernedMemoryPermission
    reason: str = Field(min_length=1, max_length=1000)


class GovernedMemoryUpdateRequest(BaseModel):
    permission: GovernedMemoryPermission
    content: str = Field(min_length=1, max_length=20_000)
    source: GovernedMemorySource
    reason: str = Field(min_length=1, max_length=1000)


class GovernedMemoryExpireRequest(BaseModel):
    permission: GovernedMemoryPermission
    reason: str = Field(min_length=1, max_length=1000)
    now: str | None = None


class PlatformPolicyControlsUpdateRequest(BaseModel):
    network_egress_policy: Literal["full", "allowlist", "none"] | None = None
    network_egress_allowlist: list[str] | None = None
    cancellation_policy: Literal["enabled", "disabled"] | None = None
    secret_policy_enabled: bool | None = None
    worker_lease_seconds: float | None = Field(default=None, ge=0)
    limits: dict[str, int] | None = None
    reason: str = Field(min_length=1, max_length=1000)


def governance_task_filters(
    task_id: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    owner_id: str | None = None,
    application_id: str | None = None,
    workflow_id: str | None = None,
    model: str | None = None,
    parent_task_id: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    query: str = "",
) -> GovernanceTaskFilters:
    return GovernanceTaskFilters(
        task_id=task_id,
        kind=kind,
        status=status,
        owner_id=owner_id,
        application_id=application_id,
        workflow_id=workflow_id,
        model=model,
        parent_task_id=parent_task_id,
        created_from=created_from,
        created_to=created_to,
        query=query,
    )


class RequirementClassificationRequest(BaseModel):
    requirement: str = Field(default="", max_length=4000)


class RequirementIntakeOptionEffect(BaseModel):
    axis: Literal[
        "functional_capability",
        "runtime_guarantee",
        "external_contract",
        "execution_envelope",
        "carrier",
        "evidence",
        "runtime_interface",
        "permission_boundary",
        "target_user",
    ]
    target_id: str = Field(default="", max_length=160)
    action: Literal["include", "require", "exclude", "configure", "raise_envelope"]
    value: str = Field(min_length=1, max_length=500)


class RequirementIntakeOption(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    impact: str = Field(default="", max_length=1000)
    recommended: bool = False
    effects: list[RequirementIntakeOptionEffect] = Field(default_factory=list, max_length=12)


class RequirementIntakeSelectedOption(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    label: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=1000)
    impact: str = Field(default="", max_length=1000)
    effects: list[RequirementIntakeOptionEffect] = Field(default_factory=list, max_length=12)


class RequirementIntakeAnswer(BaseModel):
    question_id: str = Field(min_length=1, max_length=120)
    question: str = Field(default="", max_length=1000)
    choice_type: Literal["single", "multi"] | None = None
    selected_option_ids: list[str] = Field(default_factory=list, max_length=8)
    selected_options: list[RequirementIntakeSelectedOption] = Field(default_factory=list, max_length=8)
    custom_answer: str = Field(default="", max_length=4000)
    answer: str | None = Field(default=None, max_length=4000)


class RequirementIntakeRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=30_000)
    locale: Literal["zh", "en"] = "zh"
    answers: list[RequirementIntakeAnswer] = Field(default_factory=list, max_length=32)
    max_questions: int = Field(default=5, ge=1, le=8)


class RequirementIntakeQuestion(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=1000)
    why: str = Field(default="", max_length=1000)
    decision_axis: Literal[
        "functional_capability",
        "runtime_guarantee",
        "external_contract",
        "execution_envelope",
        "carrier",
        "evidence",
        "runtime_interface",
        "permission_boundary",
        "target_user",
    ] = "functional_capability"
    choice_type: Literal["single", "multi"] = "single"
    options: list[RequirementIntakeOption] = Field(default_factory=list, max_length=5)
    custom_allowed: bool = True
    custom_placeholder: str = Field(default="", max_length=500)
    placeholder: str = Field(default="", max_length=500)


class RequirementIntakeResponse(BaseModel):
    task_id: str
    status: Literal["needs_input", "ready"]
    confidence: float = Field(ge=0, le=1)
    reasoning_summary: str = Field(default="", max_length=2000)
    detected_goal: str = Field(default="", max_length=2000)
    missing: list[str] = Field(default_factory=list, max_length=12)
    questions: list[RequirementIntakeQuestion] = Field(default_factory=list, max_length=8)
    completed_requirement: str | None = Field(default=None, max_length=30_000)
    capability_build_contract: CapabilityBuildContract | None = None
    capability_closure: dict[str, Any] | None = None
    workflow_intent: dict[str, Any] = Field(default_factory=dict)
    raw_text: str = Field(default="", max_length=4000)
    usage: dict[str, Any] = Field(default_factory=dict)


class OperatorOverrideRequest(BaseModel):
    mode: str = Field(default="disabled", max_length=80)
    reason: str = Field(default="", max_length=1000)


def _json_object_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.I)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.S)
        if not match:
            raise ValueError("model did not return JSON object") from None
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("model returned JSON but not an object")
    return value


def _requirement_intake_system(locale: str) -> str:
    language = "Chinese" if locale == "zh" else "English"
    contract_schema = json.dumps(
        CapabilityBuildContract.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "You are Lilies' workflow requirement intake agent. "
        "Your job is similar to Claude Code plan-mode questioning, but for editable workflow generation, not code execution. "
        "Analyze the user's workflow request and decide whether Lilies has enough information to build a useful editable workflow. "
        "If crucial information is missing, return status needs_input and ask option-based targeted questions. "
        "Do not ask open-ended free-text questions as the primary interaction. "
        "Every needs_input question must include 2 to 5 concrete selectable options; never return more than five options for one question. "
        "Use choice_type single for mutually exclusive decisions and multi when the customer may select several capabilities. "
        "Recommend the lowest-friction option that satisfies the original request; never recommend extra review gates, permissions, tools, integrations, state, or orchestration merely because they sound safer or more complete. "
        "For a low-risk non-mutating pure-text request that explicitly excludes external systems, the default must be automatic execution without human approval, network access, tool calls, durable state, or E2+ infrastructure unless the user explicitly asks for one of them. "
        "The first option should normally be recommended and should set recommended true, but semantic fit with the original request is more important than option order. "
        "Use custom_allowed only as an optional Other/custom escape hatch; the default path must be selecting options. "
        "Questions must be workflow-building decisions: target user, functional capability (F), runtime guarantee (G), external contract (X), E0-E5 execution envelope, carrier, runtime interface, permission boundary, and evidence. "
        "Every question must set decision_axis. Every option must include at least one typed effect that says what capability, envelope, carrier, evidence, runtime-interface, permission-boundary, or target-user decision the selection changes. "
        "Do not fill missing fields with generic placeholders. Do not invent a target customer, runtime tools, permissions, or acceptance criteria. "
        "Treat prior_answers as cumulative authoritative decisions from every completed clarification round. Never re-ask an answered question, and do not ask another question on an already-covered decision axis unless the answers are contradictory. Once target user, core capabilities, runtime interface or envelope, permission boundary, and acceptance evidence are covered, return ready; optional refinements must not create an endless clarification loop. "
        "When the request is ready, return status ready and capability_build_contract. The contract is the authoritative Builder input; completed_requirement may be null because the server renders the customer plan from the same contract. "
        "The model, not a fixed scenario template, must infer the contract contents from the original requirement and selected answers. Preserve the exact original source_requirement. "
        "Use stable F.*, G.*, and X.* ids. Every required F/G/X capability, including every runtime guarantee, needs a proposed carrier decision, an explicit workflow/evaluation/platform/external owner, and at least one evidence-plan item. "
        "Carrier planning is many-to-one: related capabilities may share one node or module. For a cohesive E0/E1 pure-text transformation, propose one structured model node by default; do not create one serial LLM call per capability unless separate tools, permissions, branches, state boundaries, or independently editable behavior require it. "
        "Each carrier_decision.capability_id must contain exactly one declared F/G/X id. Express many-to-one reuse as one carrier decision per capability with the same resource_hint; never concatenate multiple capability ids into one field. "
        "Step traceability is supplied by workflow-runtime node events plus a structured step_log in the result; it does not justify serial model calls or a separate Event Recorder block. Identical resource_hint values mean those capabilities should bind to the same carrier. "
        "Do not duplicate step_log or execution trace as both an F capability and a G guarantee; model it as an observability or audit G guarantee when it is runtime evidence rather than a customer business function. "
        "Do not invent human review or approval. Preserve it only when the original request or a selected answer explicitly requires it. "
        "An explicit absence of external dependencies is a claim-scope exclusion, not an X capability: do not create X.no_external, X.no_tools, or similar negative external contracts. "
        "E0-E5 is cumulative execution context: E0 one-shot cognition, E1 stateful workflow, E2 interactive tool loop, E3 durable task, E4 governed automation, E5 production embedding. Risk is a separate field and must not be inferred from envelope alone. "
        "Unavailable external contracts must use availability=unavailable with a reason and blocked_by_environment evidence; do not misreport them as workflow graph defects or live success. "
        "Distinguish workflow_runtime, evaluation_harness, platform_harness, and external_system ownership. Choose among atomic_block, reusable_module, runtime_service, platform_control, and connector_external_contract before implying a new block. "
        "Always answer in JSON only, no markdown fences. "
        f"Use {language} for user-visible text. "
        "JSON schema: {"
        "\"status\":\"needs_input|ready\","
        "\"confidence\":0.0,"
        "\"reasoning_summary\":\"short rationale\","
        "\"detected_goal\":\"what the user is trying to build\","
        "\"missing\":[\"specific missing facts\"],"
        "\"questions\":[{\"id\":\"stable_snake_case\",\"label\":\"short label\",\"question\":\"decision question\",\"why\":\"why it matters\",\"decision_axis\":\"functional_capability|runtime_guarantee|external_contract|execution_envelope|carrier|evidence|runtime_interface|permission_boundary|target_user\",\"choice_type\":\"single|multi\",\"options\":[{\"id\":\"stable_option_id\",\"label\":\"option label\",\"description\":\"what this means\",\"impact\":\"how it changes the workflow\",\"recommended\":true,\"effects\":[{\"axis\":\"functional_capability|runtime_guarantee|external_contract|execution_envelope|carrier|evidence|runtime_interface|permission_boundary|target_user\",\"target_id\":\"F.example\",\"action\":\"include|require|exclude|configure|raise_envelope\",\"value\":\"specific effect\"}]}],\"custom_allowed\":true,\"custom_placeholder\":\"optional custom answer placeholder\"}],"
        "\"completed_requirement\":\"string or null\","
        "\"workflow_intent\":{\"target_user\":\"\",\"runtime_input\":\"\",\"runtime_output\":\"\",\"core_steps\":[\"\"],\"permissions\":[\"\"],\"acceptance_cases\":[\"\"]},"
        "\"capability_build_contract\":{}"
        "}. "
        "For a vague request such as 'make a workflow like Codex', do not complete the requirement directly. "
        "Return option questions covering Codex-like capability scope, target user, runtime interface, permission/tool boundary, and acceptance strategy. "
        "For capability scope, use a multi-select question with no more than five concrete options; combine related ideas such as tool execution and real acceptance evidence when needed. "
        f"CapabilityBuildContract JSON schema: {contract_schema}"
    )


def _requirement_intake_prompt(body: RequirementIntakeRequest) -> str:
    answers = [
        {
            "question_id": answer.question_id,
            "question": answer.question,
            "choice_type": answer.choice_type,
            "selected_option_ids": answer.selected_option_ids,
            "selected_options": [option.model_dump(mode="json") for option in answer.selected_options],
            "custom_answer": answer.custom_answer,
            "legacy_answer": answer.answer or "",
        }
        for answer in body.answers
    ]
    answered_axes = sorted({
        effect.axis
        for answer in body.answers
        for option in answer.selected_options
        for effect in option.effects
    })
    return json.dumps(
        {
            "requirement": body.requirement,
            "prior_answers": answers,
            "answered_question_ids": [answer.question_id for answer in body.answers],
            "answered_decision_axes": answered_axes,
            "selected_option_count": sum(
                len(answer.selected_option_ids) for answer in body.answers
            ),
            "max_questions": body.max_questions,
            "instruction": (
                "prior_answers contains the cumulative selections from every earlier round. "
                "Do not ask those questions again or reopen a covered decision axis without a concrete contradiction. "
                "Return needs_input if the most important workflow design facts are still missing. "
                "If returning needs_input, return selectable options with typed decision axes and effects rather than free-text questions. "
                "As soon as target user, core capability, runtime interface or execution envelope, permission boundary, and acceptance evidence are covered, return ready. "
                "Return ready only when capability_build_contract can guide editable workflow generation, carrier selection, routing, and scoped evidence without generic fallback fields."
            ),
        },
        ensure_ascii=False,
    )


def _validate_requirement_intake_response(result: RequirementIntakeResponse) -> None:
    if result.status == "needs_input":
        if not result.questions:
            raise ValueError("needs_input response must include option-based targeted questions")
        for question in result.questions:
            option_count = len(question.options)
            if option_count < 2 or option_count > 5:
                raise ValueError("needs_input questions must include 2 to 5 selectable options")
            option_ids = [option.id for option in question.options]
            if len(option_ids) != len(set(option_ids)):
                raise ValueError("needs_input question options must have unique ids")
            if any(not option.effects for option in question.options):
                raise ValueError("needs_input options must declare typed capability effects")
            if not any(option.recommended for option in question.options):
                question.options[0].recommended = True
    if result.status == "ready":
        if not (result.completed_requirement or "").strip():
            raise ValueError("ready response must include a rendered workflow build plan")
        if result.capability_build_contract is None:
            raise ValueError("ready response must include capability_build_contract")
        closure = evaluate_capability_contract(result.capability_build_contract)
        if not closure.valid:
            raise ValueError(
                "ready capability build contract is invalid: "
                + "; ".join(closure.blocking_errors)
            )


_NO_EXTERNAL_RE = re.compile(
    r"(?:不(?:调用|接入|访问|连接|依赖|使用)|无需|无|禁止|不要).{0,12}"
    r"(?:外部(?:系统|API| API|服务|工具)?|网络|数据库)"
    r"|\b(?:without|no|do not use|does not use|must not use|avoid)\b.{0,30}"
    r"\b(?:external|network|api|connector|database|tool)s?\b",
    re.I,
)
_NO_HUMAN_REVIEW_RE = re.compile(
    r"(?:无需|不需(?:要)?|不要|不设|取消|跳过|无)(?:任何)?(?:人工|主管|用户|操作员)?"
    r"(?:审核|审批|确认|复核)"
    r"|\b(?:without|no|skip|disable)\b.{0,16}\b(?:human|manual|operator)?\s*"
    r"(?:review|approval|confirmation)\b",
    re.I,
)
_HUMAN_REVIEW_RE = re.compile(
    r"(?:人工|主管|用户|操作员).{0,8}(?:审核|审批|确认|复核)"
    r"|(?:审核|审批|复核).{0,8}(?:人工|主管|用户|操作员)"
    r"|\b(?:human|manual|operator).{0,16}(?:review|approval|confirmation)\b"
    r"|\bapproval\s+gate\b",
    re.I,
)
_SEPARATE_CARRIER_RE = re.compile(
    r"(?:独立|分别|分开).{0,10}(?:积木|节点|模型调用|步骤|编辑)"
    r"|可独立编辑|每个能力.{0,8}(?:一个|单独)"
    r"|\b(?:separate|independent|independently editable).{0,20}"
    r"(?:block|node|model call|step)s?\b"
    r"|\bone\s+(?:block|node|model call)\s+per\s+capability\b",
    re.I,
)
_LOW_FRICTION_RE = re.compile(
    r"自动|直接|即时|轻量|最少|单次|本地|无需审核|不需要审核|无审批"
    r"|\b(?:automatic|direct|immediate|minimal|single-pass|local|no approval)\b",
    re.I,
)
_EXTERNAL_USE_RE = re.compile(
    r"外部(?:系统|API| API|服务|工具)|访问网络|连接数据库|调用工具"
    r"|\b(?:external system|external api|network access|connector|database|tool execution)\b",
    re.I,
)
_LOCAL_RESOURCE_USE_RE = re.compile(
    r"本地文件|工作区文件|文件系统|读取文件|访问文件"
    r"|\b(?:local files?|workspace files?|filesystem|file access|read files?)\b",
    re.I,
)
_HIGHER_ENVELOPE_RE = re.compile(r"\bE[2-5]\b|持久任务|定时调度|生产嵌入|durable|scheduled|production", re.I)
_TRACE_CAPABILITY_RE = re.compile(
    r"step[_ .-]?trace|traceability|step[_ .-]?log|execution[_ .-]?log"
    r"|步骤(?:可)?追踪|步骤日志|执行日志",
    re.I,
)
_TRACE_OUTPUT_RE = re.compile(r"step[_ .-]?log|trace|execution[_ .-]?log|步骤日志|执行日志", re.I)


def _states_no_external(text: str) -> bool:
    return bool(_NO_EXTERNAL_RE.search(text or ""))


def _states_no_human_review(text: str) -> bool:
    return bool(_NO_HUMAN_REVIEW_RE.search(text or ""))


def _requests_human_review(text: str) -> bool:
    without_negated_review = _NO_HUMAN_REVIEW_RE.sub("", text or "")
    return bool(_HUMAN_REVIEW_RE.search(without_negated_review))


def _requirement_decision_text(body: RequirementIntakeRequest) -> str:
    parts = [body.requirement]
    for answer in body.answers:
        parts.extend((answer.custom_answer, answer.answer or ""))
        for option in answer.selected_options:
            parts.extend((option.label, option.description, option.impact))
            parts.extend(effect.value for effect in option.effects)
    return "\n".join(part for part in parts if part)


def _intake_option_text(option: dict[str, Any]) -> str:
    parts = [
        str(option.get("id") or ""),
        str(option.get("label") or ""),
        str(option.get("description") or ""),
        str(option.get("impact") or ""),
    ]
    effects = option.get("effects")
    if isinstance(effects, list):
        for effect in effects:
            if not isinstance(effect, dict):
                continue
            parts.extend(
                str(effect.get(key) or "")
                for key in ("axis", "action", "target_id", "value")
            )
    return "\n".join(parts)


def _option_excludes_external(option: dict[str, Any], text: str) -> bool:
    if _states_no_external(text):
        return True
    effects = option.get("effects")
    return bool(
        isinstance(effects, list)
        and any(
            isinstance(effect, dict)
            and effect.get("axis") == "external_contract"
            and effect.get("action") == "exclude"
            for effect in effects
        )
    )


def _option_friction_score(
    option: dict[str, Any],
    *,
    explicit_no_external: bool,
    explicit_human_review: bool,
    explicit_local_resources: bool,
) -> int:
    text = _intake_option_text(option)
    score = 0
    if _LOW_FRICTION_RE.search(text):
        score -= 5
    if _states_no_human_review(text):
        score -= 20
    if not explicit_human_review and _requests_human_review(text):
        score += 60
    if (
        explicit_no_external
        and not _option_excludes_external(option, text)
        and _EXTERNAL_USE_RE.search(text)
    ):
        score += 60
    if not explicit_local_resources and _LOCAL_RESOURCE_USE_RE.search(text):
        score += 50
    if _HIGHER_ENVELOPE_RE.search(text):
        score += 20
    if _SEPARATE_CARRIER_RE.search(text):
        score += 10
    return score


def _normalize_intake_recommendations(
    question: dict[str, Any],
    body: RequirementIntakeRequest,
) -> None:
    options = question.get("options")
    if not isinstance(options, list) or not options:
        return
    if question.get("decision_axis") == "target_user":
        return

    decision_text = _requirement_decision_text(body)
    explicit_no_external = _states_no_external(body.requirement)
    explicit_human_review = _requests_human_review(decision_text)
    explicit_local_resources = bool(
        _LOCAL_RESOURCE_USE_RE.search(body.requirement)
    )
    if not explicit_no_external:
        return

    scores = [
        _option_friction_score(
            option,
            explicit_no_external=explicit_no_external,
            explicit_human_review=explicit_human_review,
            explicit_local_resources=explicit_local_resources,
        )
        if isinstance(option, dict)
        else 100
        for option in options
    ]
    compatible_indexes = [index for index, score in enumerate(scores) if score < 50]
    if not compatible_indexes:
        return

    if question.get("choice_type") == "single":
        best_index = min(compatible_indexes, key=lambda index: (scores[index], index))
        for index, option in enumerate(options):
            if isinstance(option, dict):
                option["recommended"] = index == best_index
        return

    for index, option in enumerate(options):
        if isinstance(option, dict) and scores[index] >= 50:
            option["recommended"] = False
    if not any(
        isinstance(option, dict) and option.get("recommended")
        for option in options
    ):
        best_index = min(compatible_indexes, key=lambda index: (scores[index], index))
        options[best_index]["recommended"] = True


_INTAKE_DECISION_AXES = {
    "functional_capability",
    "runtime_guarantee",
    "external_contract",
    "execution_envelope",
    "carrier",
    "evidence",
    "runtime_interface",
    "permission_boundary",
    "target_user",
}
_INTAKE_AXIS_ALIASES = {
    "capability": "functional_capability",
    "function": "functional_capability",
    "guarantee": "runtime_guarantee",
    "runtime": "runtime_guarantee",
    "external": "external_contract",
    "integration": "external_contract",
    "envelope": "execution_envelope",
    "interface": "runtime_interface",
    "permissions": "permission_boundary",
    "permission": "permission_boundary",
    "user": "target_user",
    "acceptance": "evidence",
    "verification": "evidence",
}
_INTAKE_EFFECT_ACTIONS = {
    "include",
    "require",
    "exclude",
    "configure",
    "raise_envelope",
}
_INTAKE_ACTION_ALIASES = {
    "set": "configure",
    "select": "configure",
    "specify": "configure",
    "update": "configure",
    "add": "include",
    "enable": "include",
    "use": "include",
    "must": "require",
    "enforce": "require",
    "remove": "exclude",
    "omit": "exclude",
    "disable": "exclude",
    "forbid": "exclude",
    "raise": "raise_envelope",
    "upgrade": "raise_envelope",
}


def _intake_protocol_token(value: Any) -> str:
    return re.sub(r"[\s-]+", "_", str(value or "").strip().casefold())


def _normalize_intake_axis(value: Any, *, fallback: str) -> str:
    token = _intake_protocol_token(value)
    normalized = _INTAKE_AXIS_ALIASES.get(token, token)
    if normalized in _INTAKE_DECISION_AXES:
        return normalized
    fallback_token = _intake_protocol_token(fallback)
    fallback_axis = _INTAKE_AXIS_ALIASES.get(fallback_token, fallback_token)
    return (
        fallback_axis
        if fallback_axis in _INTAKE_DECISION_AXES
        else "functional_capability"
    )


def _normalize_intake_option_effects(
    option: dict[str, Any],
    *,
    question_axis: str,
) -> None:
    raw_effects = option.get("effects")
    effects = raw_effects if isinstance(raw_effects, list) else []
    normalized: list[dict[str, Any]] = []
    for raw_effect in effects[:12]:
        if not isinstance(raw_effect, dict):
            continue
        axis = _normalize_intake_axis(
            raw_effect.get("axis"),
            fallback=question_axis,
        )
        action_token = _intake_protocol_token(raw_effect.get("action"))
        action = _INTAKE_ACTION_ALIASES.get(action_token, action_token)
        if action not in _INTAKE_EFFECT_ACTIONS:
            action = "configure"
        option_id = str(option.get("id") or "option")
        target_id = str(
            raw_effect.get("target_id")
            or f"{axis}.{option_id}"
        ).strip()[:160]
        value = str(
            raw_effect.get("value")
            or option.get("impact")
            or option.get("label")
            or option_id
        ).strip()[:500]
        normalized.append({
            "axis": axis,
            "target_id": target_id,
            "action": action,
            "value": value or option_id,
        })
    if not normalized:
        option_id = str(option.get("id") or "legacy_option")
        normalized = [{
            "axis": question_axis,
            "target_id": f"{question_axis}.{option_id}"[:160],
            "action": "configure",
            "value": str(
                option.get("impact")
                or option.get("label")
                or option_id
            )[:500],
        }]
    option["effects"] = normalized


def _capability_text(capability: dict[str, Any]) -> str:
    return "\n".join(
        str(capability.get(key) or "")
        for key in ("id", "title", "description", "availability_reason")
    )


def _is_negative_external_contract(capability: dict[str, Any]) -> bool:
    if capability.get("availability") != "not_required":
        return False
    text = _capability_text(capability)
    return (
        _states_no_external(text)
        or "no_external" in str(capability.get("id") or "").casefold()
        or "without_external" in str(capability.get("id") or "").casefold()
    )


def _references_any(text: str, identifiers: set[str]) -> bool:
    lowered = (text or "").casefold()
    return any(identifier.casefold() in lowered for identifier in identifiers)


def _normalize_raw_carrier_decisions(
    raw_contract: dict[str, Any],
) -> dict[str, Any]:
    capability_ids = [
        item.get("id")
        for key in ("functional_capabilities", "runtime_guarantees", "external_contracts")
        for item in raw_contract.get(key, [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    decisions = raw_contract.get("carrier_decisions")
    if not isinstance(decisions, list):
        return raw_contract

    exact = {
        decision.get("capability_id"): decision
        for decision in decisions
        if isinstance(decision, dict)
        and decision.get("capability_id") in capability_ids
    }
    normalized = list(exact.values())
    normalized_ids = set(exact)
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        raw_id = str(decision.get("capability_id") or "")
        if raw_id in exact:
            continue
        tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,119}", raw_id))
        for capability_id in capability_ids:
            if capability_id not in tokens or capability_id in normalized_ids:
                continue
            normalized.append({
                **decision,
                "capability_id": capability_id,
            })
            normalized_ids.add(capability_id)
    raw_contract["carrier_decisions"] = normalized
    return raw_contract


def _ensure_explicit_runtime_guarantees(
    raw_contract: dict[str, Any],
    body: RequirementIntakeRequest,
) -> dict[str, Any]:
    decision_text = _requirement_decision_text(body)
    if not _TRACE_CAPABILITY_RE.search(decision_text):
        return raw_contract

    guarantees = raw_contract.setdefault("runtime_guarantees", [])
    if not isinstance(guarantees, list):
        guarantees = []
        raw_contract["runtime_guarantees"] = guarantees
    trace_guarantees = [
        item
        for item in guarantees
        if isinstance(item, dict)
        and _TRACE_CAPABILITY_RE.search(_capability_text(item))
    ]
    if trace_guarantees:
        for guarantee in trace_guarantees:
            guarantee["guarantee_type"] = "observability"
        return raw_contract

    existing_ids = {
        str(item.get("id"))
        for key in ("functional_capabilities", "runtime_guarantees", "external_contracts")
        for item in raw_contract.get(key, [])
        if isinstance(item, dict) and item.get("id")
    }
    capability_id = "G.step_traceability"
    suffix = 2
    while capability_id in existing_ids:
        capability_id = f"G.step_traceability_{suffix}"
        suffix += 1
    envelope = str(raw_contract.get("required_envelope") or "E1")
    if envelope not in {"E0", "E1", "E2", "E3", "E4", "E5"}:
        envelope = "E1"
    guarantees.append({
        "id": capability_id,
        "title": "步骤可追踪" if body.locale == "zh" else "Traceable workflow steps",
        "description": (
            "由工作流运行时记录节点进度，并在结果中保留结构化步骤证据。"
            if body.locale == "zh"
            else "Record node progress in the workflow runtime and retain structured "
            "step evidence in the result."
        ),
        "required_envelope": envelope,
        "guarantee_type": "observability",
        "acceptance": [
            (
                "运行记录包含可定位的节点步骤，客户结果包含结构化步骤证据。"
                if body.locale == "zh"
                else "The run exposes addressable node steps and the customer result "
                "contains structured step evidence."
            )
        ],
    })
    return raw_contract


def _normalize_ready_capability_contract(
    contract: CapabilityBuildContract,
    body: RequirementIntakeRequest,
) -> CapabilityBuildContract:
    data = contract.model_dump(mode="json")
    decision_text = _requirement_decision_text(body)
    explicit_no_external = _states_no_external(body.requirement)
    explicit_human_review = _requests_human_review(decision_text)
    explicit_separate_carriers = bool(_SEPARATE_CARRIER_RE.search(decision_text))
    low_risk_text_scope = (
        data.get("risk_level") == "low"
        and data.get("required_envelope") in {"E0", "E1"}
        and all(
            item.get("value_type") == "string"
            for item in data.get("start_inputs", [])
            if isinstance(item, dict) and item.get("required", True)
        )
    )

    removed_external_ids = {
        item["id"]
        for item in data.get("external_contracts", [])
        if isinstance(item, dict) and _is_negative_external_contract(item)
    }
    removed_review_ids: set[str] = set()
    if low_risk_text_scope and explicit_no_external and not explicit_human_review:
        removed_review_ids.update(
            item["id"]
            for item in data.get("functional_capabilities", [])
            if isinstance(item, dict) and _requests_human_review(_capability_text(item))
        )
        removed_review_ids.update(
            item["id"]
            for item in data.get("runtime_guarantees", [])
            if isinstance(item, dict)
            and (
                item.get("guarantee_type") == "permission"
                or _requests_human_review(_capability_text(item))
            )
        )
    has_trace_guarantee = any(
        item.get("guarantee_type") in {"audit", "observability"}
        and _TRACE_CAPABILITY_RE.search(_capability_text(item))
        for item in data.get("runtime_guarantees", [])
        if isinstance(item, dict) and item.get("required", True)
    )
    removed_duplicate_function_ids = {
        item["id"]
        for item in data.get("functional_capabilities", [])
        if isinstance(item, dict)
        and has_trace_guarantee
        and _TRACE_CAPABILITY_RE.search(_capability_text(item))
        and item.get("outputs")
        and all(_TRACE_OUTPUT_RE.search(str(output)) for output in item["outputs"])
    }
    removed_ids = (
        removed_external_ids
        | removed_review_ids
        | removed_duplicate_function_ids
    )

    data["external_contracts"] = [
        item
        for item in data.get("external_contracts", [])
        if item.get("id") not in removed_external_ids
    ]
    data["functional_capabilities"] = [
        item
        for item in data.get("functional_capabilities", [])
        if item.get("id") not in removed_review_ids
        and item.get("id") not in removed_duplicate_function_ids
    ]
    data["runtime_guarantees"] = [
        item
        for item in data.get("runtime_guarantees", [])
        if item.get("id") not in removed_review_ids
    ]

    for key in ("functional_capabilities", "runtime_guarantees", "external_contracts"):
        known_capability_ids = {
            item["id"]
            for capability_key in (
                "functional_capabilities",
                "runtime_guarantees",
                "external_contracts",
            )
            for item in data.get(capability_key, [])
        }
        for capability in data.get(key, []):
            capability["requires"] = [
                item for item in capability.get("requires", []) if item not in removed_ids
            ]
            capability["excludes"] = [
                item
                for item in capability.get("excludes", [])
                if item not in removed_ids and item in known_capability_ids
            ]

    data["carrier_decisions"] = [
        item
        for item in data.get("carrier_decisions", [])
        if item.get("capability_id") not in removed_ids
    ]
    data["platform_coverage"] = [
        item
        for item in data.get("platform_coverage", [])
        if item.get("capability_id") not in removed_ids
    ]
    normalized_evidence = []
    for item in data.get("evidence_plan", []):
        capability_ids = [
            capability_id
            for capability_id in item.get("capability_ids", [])
            if capability_id not in removed_ids
        ]
        if capability_ids:
            item["capability_ids"] = capability_ids
            normalized_evidence.append(item)
    data["evidence_plan"] = normalized_evidence

    if removed_review_ids:
        data["workflow_outline"] = [
            item
            for item in data.get("workflow_outline", [])
            if not _references_any(item, removed_review_ids)
            and not _requests_human_review(item)
        ]
        data["risk_reasons"] = [
            item
            for item in data.get("risk_reasons", [])
            if not _requests_human_review(item)
        ]
        if _requests_human_review(str(data.get("runtime_interface") or "")):
            data["runtime_interface"] = (
                "用户在客户运行界面提交启动输入，工作流自动完成处理，并展示结构化结果、"
                "当前步骤和运行证据。"
                if body.locale == "zh"
                else "The user submits the start input in the customer runtime; the workflow "
                "runs automatically and shows the structured result, current step, and evidence."
            )

    claim_scope = data.get("claim_scope")
    if isinstance(claim_scope, dict):
        claim_scope["verified"] = [
            item
            for item in claim_scope.get("verified", [])
            if not _references_any(item, removed_ids)
            and not (removed_review_ids and _requests_human_review(item))
        ]
        if removed_external_ids and not any(
            "外部" in item or "external" in item.casefold()
            for item in claim_scope.get("excluded", [])
        ):
            claim_scope.setdefault("excluded", []).append(
                "外部系统集成" if body.locale == "zh" else "External system integration"
            )

    data["unresolved_decisions"] = [
        item
        for item in data.get("unresolved_decisions", [])
        if not _references_any(item, removed_ids)
    ]

    remaining_required_external = [
        item
        for item in data.get("external_contracts", [])
        if item.get("required", True) and item.get("availability") != "not_required"
    ]
    share_text_carrier = (
        low_risk_text_scope
        and explicit_no_external
        and not remaining_required_external
        and not explicit_separate_carriers
    )
    if share_text_carrier:
        functional_ids = {
            item["id"]
            for item in data.get("functional_capabilities", [])
            if item.get("required", True)
        }
        functional_titles = [
            item["title"]
            for item in data.get("functional_capabilities", [])
            if item.get("required", True)
        ]
        guarantee_types = {
            item["id"]: item.get("guarantee_type")
            for item in data.get("runtime_guarantees", [])
            if item.get("required", True)
        }
        outline = data.get("workflow_outline", [])
        model_step_indexes = [
            index
            for index, item in enumerate(outline)
            if re.search(r"LLM|模型|model", item, re.I)
        ]
        if len(model_step_indexes) > 1:
            first_model_step = model_step_indexes[0]
            combined_model_step = (
                "使用一个结构化模型步骤同时完成："
                + "、".join(functional_titles)
                if body.locale == "zh"
                else "Use one structured model step for: "
                + ", ".join(functional_titles)
            )
            data["workflow_outline"] = [
                *outline[:first_model_step],
                combined_model_step,
                *[
                    item
                    for index, item in enumerate(outline[first_model_step + 1 :], first_model_step + 1)
                    if index not in model_step_indexes
                ],
            ]
        for decision in data.get("carrier_decisions", []):
            capability_id = decision.get("capability_id")
            if (
                capability_id in functional_ids
                and decision.get("carrier_type") in {"atomic_block", "reusable_module"}
            ):
                decision.update({
                    "carrier_type": "atomic_block",
                    "resource_hint": "shared:model_turn:structured_workflow_result",
                    "rationale": (
                        "One structured Model Turn covers the related text capabilities; "
                        "runtime node events and the structured step_log preserve traceability."
                    ),
                    "status": "proposed",
                    "implementation_refs": [],
                })
            elif guarantee_types.get(capability_id) in {"state", "audit", "observability"}:
                decision.update({
                    "carrier_type": "runtime_service",
                    "resource_hint": "runtime:workflow_runtime",
                    "rationale": (
                        "The workflow runtime records node progress and structured result evidence; "
                        "no extra model call or Event Recorder node is required."
                    ),
                    "status": "proposed",
                    "implementation_refs": [],
                })
        for coverage in data.get("platform_coverage", []):
            if guarantee_types.get(coverage.get("capability_id")) in {
                "state",
                "audit",
                "observability",
            }:
                coverage.update({
                    "owner": "workflow_runtime",
                    "surface": "runtime:workflow_runtime",
                    "notes": (
                        "Runtime node events and structured outputs provide step evidence "
                        "without adding a separate workflow node."
                    ),
                })

    return CapabilityBuildContract.model_validate(data)


def _normalize_requirement_intake_payload(
    payload: dict[str, Any],
    body: RequirementIntakeRequest,
) -> dict[str, Any]:
    if not isinstance(payload.get("workflow_intent"), dict):
        payload["workflow_intent"] = {}
    if payload.get("status") == "needs_input":
        payload["completed_requirement"] = None
        payload["capability_build_contract"] = None
        payload["capability_closure"] = None
        questions = payload.get("questions")
        if not isinstance(questions, list):
            return payload
        for question in questions:
            if not isinstance(question, dict):
                continue
            for text_key in ("why", "placeholder", "custom_placeholder"):
                if question.get(text_key) is None:
                    question[text_key] = ""
            if question.get("custom_allowed") is None:
                question["custom_allowed"] = True
            question["decision_axis"] = _normalize_intake_axis(
                question.get("decision_axis"),
                fallback="functional_capability",
            )
            options = question.get("options")
            if isinstance(options, list) and len(options) > 5:
                question["options"] = options[:5]
            if isinstance(question.get("options"), list):
                for option in question["options"]:
                    if not isinstance(option, dict):
                        continue
                    for text_key in ("description", "impact"):
                        if option.get(text_key) is None:
                            option[text_key] = ""
                    if option.get("recommended") is None:
                        option["recommended"] = False
                    _normalize_intake_option_effects(
                        option,
                        question_axis=question["decision_axis"],
                    )
            _normalize_intake_recommendations(question, body)
        return payload

    if payload.get("status") != "ready":
        return payload
    raw_contract = payload.get("capability_build_contract")
    if isinstance(raw_contract, dict):
        raw_contract = dict(raw_contract)
        raw_contract["source_requirement"] = body.requirement
        raw_contract["generation_source"] = "model"
        raw_contract = _normalize_raw_carrier_decisions(raw_contract)
        raw_contract = _ensure_explicit_runtime_guarantees(raw_contract, body)
        contract = _normalize_ready_capability_contract(
            complete_capability_contract_scaffolding(
                CapabilityBuildContract.model_validate(raw_contract)
            ),
            body,
        )
    else:
        workflow_intent = payload.get("workflow_intent")
        contract = legacy_intake_capability_contract(
            requirement=body.requirement,
            workflow_intent=workflow_intent if isinstance(workflow_intent, dict) else {},
            completed_requirement=str(payload.get("completed_requirement") or body.requirement),
        )
    closure = evaluate_capability_contract(contract)
    payload["capability_build_contract"] = contract.model_dump(mode="json")
    payload["capability_closure"] = closure.model_dump(mode="json")
    payload["completed_requirement"] = render_workflow_build_plan(
        contract,
        locale=body.locale,
    )
    return payload


async def complete_requirement_intake(
    services: Services,
    body: RequirementIntakeRequest,
) -> RequirementIntakeResponse:
    task_id = str(uuid4())
    model = services.settings.deepseek_runtime_model
    await services.harness.start_task(
        task_id,
        kind="requirement_intake",
        owner_id="requirement-intake",
        resource_id=task_id,
        metadata={
            "origin": "home_requirement_completion",
            "requirement_preview": body.requirement[:200],
            "answer_count": len(body.answers),
            "model": model,
        },
    )
    try:
        await services.harness.record_usage(
            task_id,
            "model_call",
            metadata={"model": model, "mode": "requirement_intake"},
        )
        stream = services.provider.stream(
            model=model,
            system=_requirement_intake_system(body.locale),
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text=_requirement_intake_prompt(body))])],
            tools=[],
            max_output_tokens=12_000,
            thinking_enabled=False,
            effort="low",
            user_id=task_id,
        )
        response = await services.runtime._collect_stream(
            task_id,
            stream,
            "requirement_intake.model",
            model,
            timeout_seconds=min(services.settings.deepseek_timeout_seconds, 120.0),
        )
        await services.harness.record_model_usage(
            task_id,
            response.usage,
            model=model,
            provider=services.provider.provider_name_for(model),
            metadata={"phase": "requirement_intake"},
        )
        text = "".join(block.text or "" for block in response.blocks if block.type == "text")
        payload = _json_object_from_text(text)
        payload["task_id"] = task_id
        payload["raw_text"] = text[:4000]
        payload["usage"] = response.usage.model_dump(mode="json")
        result = RequirementIntakeResponse.model_validate(
            _normalize_requirement_intake_payload(payload, body)
        )
        _validate_requirement_intake_response(result)
        await services.harness.finish_task(
            task_id,
            status="succeeded",
            metadata={"intake_status": result.status, "question_count": len(result.questions)},
        )
        return result
    except Exception as error:
        await services.harness.finish_task(task_id, status="failed", error=str(error))
        raise


def _workflow_edit_needs_model(preview: DraftPatchPreviewResponse) -> bool:
    if not preview.supported:
        return True
    if preview.intent != "update_workflow_requirement":
        return False
    warnings = " ".join(preview.warnings).casefold()
    return "deterministic" in warnings or "later builder-team" in warnings


async def _model_workflow_edit_preview(
    services: Services,
    *,
    task_id: str,
    snapshot: ApplicationSnapshot,
    revision: int,
    body: DraftPatchPreviewRequest,
) -> DraftPatchPreviewResponse:
    model = services.settings.deepseek_runtime_model
    block_catalog = [
        {
            "type": block.type,
            "title": block.title,
            "description": block.description,
        }
        for block in services.blocks.list()
    ]
    system = (
        "You are Lilies' whole-workflow editing planner. Translate one natural-language "
        "instruction into precise, reviewable draft operations over the supplied workflow. "
        "Referenced nodes are context, not an edit-scope restriction. Preserve everything the "
        "user did not ask to change. Never store the instruction itself as the workflow requirement "
        "or description as a substitute for a structural edit. Resolve human node titles to the "
        "existing node ids. Use only the listed block types and only these operations: add_node, "
        "update_node, remove_node, add_edge, remove_edge, set_metadata, upsert_agent, add_test, "
        "remove_test, set_capability_build_contract. For update_node, use data.node_id, "
        "data.changes, and data.merge_config. For set_metadata, include only fields explicitly "
        "requested. If the instruction is ambiguous, return supported=false with one concise "
        "clarification question and no operations. Return JSON only with keys supported, intent, "
        "message, operations, warnings."
    )
    prompt = json.dumps(
        {
            "instruction": body.instruction,
            "reference_node_ids": body.reference_node_ids,
            "expected_revision": revision,
            "snapshot": snapshot.model_dump(mode="json"),
            "available_blocks": block_catalog,
            "output_example": {
                "supported": True,
                "intent": "multi_operation_edit",
                "message": "Preview two precise workflow changes.",
                "operations": [
                    {
                        "op": "update_node",
                        "data": {
                            "node_id": "existing_node_id",
                            "changes": {"title": "New title"},
                            "merge_config": True,
                        },
                    }
                ],
                "warnings": [],
            },
        },
        ensure_ascii=False,
    )
    await services.harness.record_usage(
        task_id,
        "model_call",
        metadata={"model": model, "mode": "whole_workflow_edit_preview"},
    )
    stream = services.provider.stream(
        model=model,
        system=system,
        messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text=prompt)])],
        tools=[],
        max_output_tokens=8_000,
        thinking_enabled=False,
        effort="low",
        user_id=task_id,
    )
    model_response = await services.runtime._collect_stream(
        task_id,
        stream,
        "workflow_edit.model",
        model,
        timeout_seconds=min(services.settings.deepseek_timeout_seconds, 120.0),
    )
    await services.harness.record_model_usage(
        task_id,
        model_response.usage,
        model=model,
        provider=services.provider.provider_name_for(model),
        metadata={"phase": "whole_workflow_edit_preview"},
    )
    model_text = "".join(
        block.text or "" for block in model_response.blocks if block.type == "text"
    )
    payload = _json_object_from_text(model_text)
    if payload.get("supported") is False:
        return DraftPatchPreviewResponse(
            supported=False,
            intent="unsupported",
            message=str(payload.get("message") or "The workflow edit needs clarification."),
            operations=[],
            warnings=[str(item) for item in payload.get("warnings", []) if str(item).strip()],
            reference_node_ids=body.reference_node_ids,
        )
    raw_operations = payload.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ValueError("AI workflow edit preview returned no draft operations")
    if len(raw_operations) > 40:
        raise ValueError("AI workflow edit preview exceeded the 40-operation review boundary")

    parsed_operations: list[DraftOperation] = []
    explicit_requirement_change = bool(re.search(r"requirement|goal|需求|目标", body.instruction, re.I))
    explicit_removal = bool(re.search(r"remove|delete|删除|移除|去掉", body.instruction, re.I))
    for raw_operation in raw_operations:
        if not isinstance(raw_operation, dict):
            raise ValueError("AI workflow edit operation must be an object")
        normalized = dict(raw_operation)
        normalized["expected_revision"] = revision
        normalized["idempotency_key"] = str(uuid4())
        operation = DraftOperation.model_validate(normalized)
        if operation.op == "remove_node" and not explicit_removal:
            raise ValueError("AI workflow edit attempted node removal without an explicit removal request")
        if (
            operation.op == "set_metadata"
            and "requirement" in operation.data
            and not explicit_requirement_change
        ):
            raise ValueError("AI workflow edit attempted to overwrite the requirement without a requirement-change request")
        parsed_operations.append(operation)
    services.applications.validate_preview_operations(snapshot, parsed_operations)
    intent = str(payload.get("intent") or "multi_operation_edit")
    if intent not in {
        "multi_operation_edit",
        "rename_node",
        "update_node_description",
        "remove_disconnected_node",
        "update_workflow_metadata",
        "update_workflow_requirement",
        "update_start_inputs",
        "upsert_template_transform",
    }:
        intent = "multi_operation_edit"
    warnings = [str(item) for item in payload.get("warnings", []) if str(item).strip()]
    warnings.append("AI-generated whole-workflow preview; inspect every operation before applying.")
    return DraftPatchPreviewResponse(
        supported=True,
        intent=intent,
        message=str(payload.get("message") or "Preview AI-planned whole-workflow edit."),
        operations=[
            operation.model_dump(mode="json", exclude={"idempotency_key"})
            for operation in parsed_operations
        ],
        warnings=warnings,
        reference_node_ids=body.reference_node_ids,
    )


def deadline_summary(max_elapsed_seconds: float | None) -> dict[str, Any]:
    return {
        "enabled": max_elapsed_seconds is not None,
        "max_elapsed_seconds": max_elapsed_seconds,
    }


def annotate_build_deadline(build: dict[str, Any]) -> dict[str, Any]:
    build["deadline"] = deadline_summary(build.get("max_elapsed_seconds"))
    return build


def build_services(settings: Settings, provider: ModelProvider | None = None) -> Services:
    agent_core = build_agent_runtime_core(settings, provider)
    storage = agent_core.storage
    tools = agent_core.tools
    sandboxes = agent_core.sandboxes
    permissions = agent_core.permissions
    provider = agent_core.provider
    harness = agent_core.harness
    runtime = agent_core.runtime
    factory = agent_core.factory
    blocks = build_block_registry()
    workflow_store = WorkflowStorage(storage)
    durable_jobs = DurableJobStore(storage)
    applications = ApplicationService(workflow_store, blocks, tools)
    governed_memory = GovernedMemorySurface(storage)
    web_collector = ControlledWebCollector(jobs=durable_jobs, harness=harness)
    connectors = ConnectorService(storage=storage, harness=harness)
    openapi_connectors = OpenAPIConnectorService(
        storage=storage,
        harness=harness,
        connectors=connectors,
    )
    workflow_runtime = WorkflowRuntime(
        storage=storage,
        workflow_store=workflow_store,
        harness=harness,
        applications=applications,
        blocks=blocks,
        provider=provider,
        agent_runtime=runtime,
        tools=tools,
        sandboxes=sandboxes,
        runtime_model=settings.deepseek_runtime_model,
        governed_memory=governed_memory,
        web_collector=web_collector,
        connector_service=connectors,
    )
    evaluation_harness = EvaluationHarness(
        storage=storage,
        workflow_store=workflow_store,
        applications=applications,
        workflow_runtime=workflow_runtime,
        harness=harness,
        live_enabled=settings.evaluation_live_enabled,
        production_observation_enabled=settings.evaluation_production_observation_enabled,
        production_observation_evidence_path=(
            settings.evaluation_production_observation_evidence_path
        ),
    )
    templates = TemplateStore(
        settings.data_dir / "module_registry",
        evidence_root=_repo_root() or Path.cwd(),
        workflow_validator=blocks.validate_workflow,
    )
    scenarios = ScenarioCatalog(blocks, connectors=connectors)
    benchmark = BuilderBenchmark()
    draft_patcher = DraftPatchPreviewer()
    acceptance_repairer = AcceptanceRepairPreviewer(blocks)
    templates_dir = settings.templates_dir
    if templates_dir and templates_dir.is_dir():
        loaded = templates.load_builtins(templates_dir)
        print(f"[api] Loaded {loaded} built-in templates from {templates_dir}")
    reference_module = ensure_codex_reference_module(templates, blocks)
    print(
        f"[api] Reference module {reference_module.module_ref} "
        f"status={reference_module.state.status}"
    )
    ensure_platform_capability_evidence(
        templates.evidence,
        evidence_root=_repo_root() or Path.cwd(),
    )
    governance = GovernanceService(
        storage=storage,
        harness=harness,
        workflow_store=workflow_store,
        templates=templates,
        durable_jobs=durable_jobs,
        connectors=connectors,
    )

    builder = WorkflowBuilder(
        storage=storage,
        workflow_store=workflow_store,
        applications=applications,
        blocks=blocks,
        runtime=workflow_runtime,
        provider=provider,
        agent_runtime=runtime,
        generator_model=settings.deepseek_generator_model,
        core_tools=tools,
        harness=harness,
        template_store=templates,
    )
    scheduler = WorkflowScheduler(
        storage=storage,
        workflow_store=workflow_store,
        blocks=blocks,
        runtime=workflow_runtime,
        harness=harness,
        durable_jobs=durable_jobs,
        poll_seconds=settings.scheduler_poll_seconds,
        worker_offload_enabled=settings.scheduler_worker_offload_enabled,
    )
    services = Services(
        settings=settings,
        storage=storage,
        provider=provider,
        tools=tools,
        sandboxes=sandboxes,
        permissions=permissions,
        runtime=runtime,
        factory=factory,
        blocks=blocks,
        workflow_store=workflow_store,
        durable_jobs=durable_jobs,
        harness=harness,
        connectors=connectors,
        openapi_connectors=openapi_connectors,
        applications=applications,
        workflow_runtime=workflow_runtime,
        evaluation_harness=evaluation_harness,
        builder=builder,
        scheduler=scheduler,
        templates=templates,
        governance=governance,
        scenarios=scenarios,
        benchmark=benchmark,
        draft_patcher=draft_patcher,
        acceptance_repairer=acceptance_repairer,
        governed_memory=governed_memory,
        worker_supervisor=None,
        worker_process_manager=None,
        background_tasks=set(),
    )
    from .worker_runner import (  # pylint: disable=import-outside-toplevel
        ExternalWorkerProcessManager,
        PlatformHarnessWorkerRunner,
        PlatformWorkerSupervisor,
        build_platform_worker_handlers,
    )

    supervised_runner = PlatformHarnessWorkerRunner(
        harness=harness,
        worker_id=harness.worker_id,
        lease_seconds=max(harness.worker_lease_seconds, 60.0),
        handlers=build_platform_worker_handlers(services),
    )
    services.worker_supervisor = PlatformWorkerSupervisor(
        runner=supervised_runner,
        poll_seconds=max(settings.platform_harness_worker_supervision_poll_seconds, 0.001),
        limit=max(settings.platform_harness_worker_supervision_limit, 1),
        background_tasks=services.background_tasks,
    )
    services.worker_process_manager = ExternalWorkerProcessManager(
        command=list(settings.platform_harness_worker_process_command),
        cwd=settings.platform_harness_worker_process_cwd,
        stop_timeout_seconds=max(settings.platform_harness_worker_process_stop_timeout_seconds, 0.001),
    )
    return services


def create_app(settings: Settings | None = None, provider: ModelProvider | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.prepare()
    services = build_services(settings, provider)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await services.storage.initialize()
        await services.workflow_store.initialize()
        await services.durable_jobs.initialize()
        await services.connectors.initialize()
        await services.openapi_connectors.initialize()
        await services.workflow_store.fail_interrupted_runs()
        services.scheduler.start()
        adaptive_refresh_task: asyncio.Task[Any] | None = None
        if settings.adaptive_monitoring_refresh_interval_seconds > 0:
            adaptive_refresh_task = asyncio.create_task(
                adaptive_monitoring_refresh_loop(
                    services.settings.data_dir,
                    settings.adaptive_monitoring_refresh_interval_seconds,
                ),
                name="adaptive-monitoring-refresh-loop",
            )
            services.background_tasks.add(adaptive_refresh_task)
        yield
        if services.worker_process_manager is not None and services.worker_process_manager.is_running:
            services.worker_process_manager.stop()
        if services.worker_supervisor is not None and services.worker_supervisor.loop_running:
            await services.worker_supervisor.stop()
        await services.scheduler.stop()
        for task in services.background_tasks:
            task.cancel()
        if adaptive_refresh_task is not None:
            services.background_tasks.discard(adaptive_refresh_task)
        await services.sandboxes.close()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.services = services
    bearer = HTTPBearer(auto_error=False)

    async def require_token(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        supplied = credentials.credentials if credentials else request.query_params.get("token")
        if supplied != settings.api_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API token")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        routes = route_availability(app)
        return {
            "status": "ok",
            "runtime": {
                "version": __version__,
                "product_phase": PRODUCT_PHASE,
                "git": runtime_git_identity(),
                "route_availability": routes,
                "current_code_ready": all(routes.values()),
            },
            "deepseek_configured": bool(settings.deepseek_api_key),
            "docker_available": shutil.which("docker") is not None,
            "provider": services.provider.name,
            "tools": services.tools.names(),
        }

    @app.get("/v1/models", dependencies=[Depends(require_token)])
    async def models() -> dict[str, Any]:
        return {
            "provider": services.provider.name,
            "type": services.provider.name,  # new key, backward compat
            "configured_providers": getattr(services.provider, "configured_providers", ["deepseek"]),
            "configured_models": getattr(services.provider, "configured_models", []),
            "generator_model": settings.deepseek_generator_model,
            "runtime_model": settings.deepseek_runtime_model,
            "capabilities": asdict(services.provider.capabilities(settings.deepseek_runtime_model)),
        }

    @app.get("/api/v1/platform/harness/tasks", dependencies=[Depends(require_token)])
    async def list_platform_harness_tasks(
        kind: str | None = None,
        status: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        tasks = await services.harness.list_tasks(
            kind=kind,
            status=status,
            owner_id=owner_id,
            limit=max(1, min(limit, 500)),
        )
        return [task.model_dump(mode="json") for task in tasks]

    @app.get("/api/v1/platform/harness/tasks/{task_id}", dependencies=[Depends(require_token)])
    async def get_platform_harness_task(task_id: str) -> dict[str, Any]:
        try:
            task = await services.harness.get_task(task_id)
            return task.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/v1/governance/overview", dependencies=[Depends(require_token)])
    async def governance_overview(
        filters: GovernanceTaskFilters = Depends(governance_task_filters),
    ) -> dict[str, Any]:
        return await services.governance.overview(filters)

    @app.get("/api/v1/governance/tasks", dependencies=[Depends(require_token)])
    async def governance_tasks(
        offset: int = 0,
        limit: int = 50,
        filters: GovernanceTaskFilters = Depends(governance_task_filters),
    ) -> dict[str, Any]:
        page = await services.governance.tasks(filters, offset=offset, limit=limit)
        return page.model_dump(mode="json")

    @app.get("/api/v1/governance/usage", dependencies=[Depends(require_token)])
    async def governance_usage(
        provider: str | None = None,
        interval: Literal["hour", "day"] = "hour",
        limit: int = 1000,
        filters: GovernanceTaskFilters = Depends(governance_task_filters),
    ) -> dict[str, Any]:
        return await services.governance.usage(
            filters,
            provider=provider,
            interval=interval,
            limit=limit,
        )

    @app.get("/api/v1/governance/reliability", dependencies=[Depends(require_token)])
    async def governance_reliability(
        filters: GovernanceTaskFilters = Depends(governance_task_filters),
    ) -> dict[str, Any]:
        return await services.governance.reliability(filters)

    @app.get("/api/v1/governance/durable-jobs", dependencies=[Depends(require_token)])
    async def governance_durable_jobs(
        application_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        try:
            return await services.governance.durable_job_operations(
                application_id=application_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/governance/connectors", dependencies=[Depends(require_token)])
    async def governance_connectors(
        connector_id: str | None = None,
        tenant_id: str | None = None,
        operation_id: str | None = None,
        execution_status: str | None = Query(default=None, alias="status"),
        emergency_stop: bool | None = None,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return await services.governance.connector_operations(
            connector_id=connector_id,
            tenant_id=tenant_id,
            operation_id=operation_id,
            status=execution_status,
            emergency_stop=emergency_stop,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v1/governance/traces/{task_id}", dependencies=[Depends(require_token)])
    async def governance_trace(task_id: str) -> dict[str, Any]:
        try:
            return await services.governance.trace(task_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/v1/governance/policy", dependencies=[Depends(require_token)])
    async def governance_policy() -> dict[str, Any]:
        return await services.governance.policy()

    @app.get("/api/v1/governance/capability-evidence", dependencies=[Depends(require_token)])
    async def governance_capability_evidence() -> dict[str, Any]:
        return await services.governance.capability_evidence()

    @app.get("/api/v1/governance/alerts", dependencies=[Depends(require_token)])
    async def governance_alerts(
        filters: GovernanceTaskFilters = Depends(governance_task_filters),
    ) -> dict[str, Any]:
        return await services.governance.alerts(filters)

    @app.get("/api/v1/platform/harness/policy-controls", dependencies=[Depends(require_token)])
    async def get_platform_harness_policy_controls() -> dict[str, Any]:
        return services.harness.policy_controls()

    @app.get("/api/v1/platform/harness/worker-handler-catalog", dependencies=[Depends(require_token)])
    async def get_platform_harness_worker_handler_catalog() -> dict[str, Any]:
        from .worker_runner import build_platform_worker_handlers, platform_worker_handler_catalog

        return platform_worker_handler_catalog(build_platform_worker_handlers(services))

    @app.patch("/api/v1/platform/harness/policy-controls", dependencies=[Depends(require_token)])
    async def patch_platform_harness_policy_controls(
        body: PlatformPolicyControlsUpdateRequest,
    ) -> dict[str, Any]:
        patch_fields = {
            "network_egress_policy": body.network_egress_policy,
            "network_egress_allowlist": body.network_egress_allowlist,
            "cancellation_policy": body.cancellation_policy,
            "secret_policy_enabled": body.secret_policy_enabled,
            "worker_lease_seconds": body.worker_lease_seconds,
            "limits": body.limits,
        }
        if all(value is None for value in patch_fields.values()):
            raise HTTPException(422, "policy controls update requires at least one mutable field")
        try:
            result = services.harness.update_policy_controls(reason=body.reason, **patch_fields)
            await services.governance.record_policy_audit(result)
            return result
        except PlatformHarnessViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/platform/complexity-router/default-safety", dependencies=[Depends(require_token)])
    async def get_complexity_router_default_safety() -> dict[str, Any]:
        return complexity_router_default_safety_gate(
            default_enabled=(
                services.settings.complexity_router_default_mode == "limited_default"
                and services.settings.complexity_router_limited_default_enabled
            )
        )

    @app.get("/api/v1/platform/complexity-router/requirement-classification", dependencies=[Depends(require_token)])
    async def get_complexity_router_requirement_classification_contract() -> dict[str, Any]:
        return requirement_classification_contract_status()

    @app.post("/api/v1/platform/complexity-router/classify-requirement", dependencies=[Depends(require_token)])
    async def post_complexity_router_classify_requirement(
        body: RequirementClassificationRequest,
    ) -> dict[str, Any]:
        return classify_requirement(
            body.requirement,
            default_mode=services.settings.complexity_router_default_mode,
            limited_default_enabled=services.settings.complexity_router_limited_default_enabled,
            min_confidence=services.settings.complexity_router_limited_default_min_confidence,
        )

    @app.post("/api/v1/requirements/complete", dependencies=[Depends(require_token)])
    async def post_requirement_intake_completion(
        body: RequirementIntakeRequest,
    ) -> dict[str, Any]:
        try:
            result = await complete_requirement_intake(services, body)
            return result.model_dump(mode="json")
        except PlatformHarnessViolation as error:
            raise HTTPException(429, str(error)) from error
        except ValueError as error:
            raise HTTPException(502, str(error)) from error

    @app.post("/api/v1/capability-contracts/validate", dependencies=[Depends(require_token)])
    async def validate_capability_build_contract(
        body: CapabilityBuildContract,
        require_bound_carriers: bool = False,
    ) -> dict[str, Any]:
        return evaluate_capability_contract(
            body,
            require_bound_carriers=require_bound_carriers,
        ).model_dump(mode="json")

    @app.get("/api/v1/capability-contracts/reference-scenarios", dependencies=[Depends(require_token)])
    async def list_reference_capability_contracts() -> list[dict[str, Any]]:
        return [
            {
                "contract": contract.model_dump(mode="json"),
                "closure": evaluate_capability_contract(contract).model_dump(mode="json"),
            }
            for contract in reference_capability_contracts()
        ]

    @app.get("/api/v1/platform/complexity-router/default-enableable-plan", dependencies=[Depends(require_token)])
    async def get_complexity_router_default_enableable_plan() -> dict[str, Any]:
        return limited_default_enablement_plan_status(
            default_mode=services.settings.complexity_router_default_mode,
            limited_default_enabled=services.settings.complexity_router_limited_default_enabled,
            min_confidence=services.settings.complexity_router_limited_default_min_confidence,
        )

    @app.get("/api/v1/platform/complexity-router/runtime-activation-metrics", dependencies=[Depends(require_token)])
    async def get_complexity_router_runtime_activation_metrics(limit: int = 100) -> dict[str, Any]:
        builds = await services.workflow_store.list_recent_builds(limit=max(1, min(limit, 500)))
        return runtime_activation_rollout_metrics(builds)

    @app.get("/api/v1/platform/complexity-router/operator-override-plan", dependencies=[Depends(require_token)])
    async def get_complexity_router_operator_override_plan() -> dict[str, Any]:
        return operator_override_plan_status()

    @app.post("/api/v1/platform/complexity-router/validate-operator-override", dependencies=[Depends(require_token)])
    async def post_complexity_router_validate_operator_override(
        body: OperatorOverrideRequest,
    ) -> dict[str, Any]:
        return validate_operator_override(body.mode, body.reason)

    @app.get("/api/v1/platform/complexity-router/rollout-metrics-prerequisites", dependencies=[Depends(require_token)])
    async def get_complexity_router_rollout_metrics_prerequisites(sample_count: int = 0) -> dict[str, Any]:
        return rollout_metrics_prerequisites_status(sample_count)

    @app.post("/api/v1/platform/harness/tasks/{task_id}/lease", dependencies=[Depends(require_token)])
    async def claim_platform_harness_task_lease(
        task_id: str, body: PlatformTaskLeaseRequest
    ) -> dict[str, Any]:
        try:
            task = await services.harness.claim_task_lease(
                task_id,
                worker_id=body.worker_id,
                lease_seconds=body.lease_seconds,
            )
            return task.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except PlatformHarnessViolation as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/v1/platform/harness/tasks/{task_id}/lease/renew", dependencies=[Depends(require_token)])
    async def renew_platform_harness_task_lease(
        task_id: str, body: PlatformTaskLeaseRequest
    ) -> dict[str, Any]:
        try:
            task = await services.harness.renew_task_lease(
                task_id,
                worker_id=body.worker_id,
                lease_seconds=body.lease_seconds,
            )
            return task.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except PlatformHarnessViolation as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/v1/platform/harness/tasks/{task_id}/lease/release", dependencies=[Depends(require_token)])
    async def release_platform_harness_task_lease(
        task_id: str, body: PlatformTaskLeaseReleaseRequest
    ) -> dict[str, Any]:
        try:
            task = await services.harness.release_task_lease(
                task_id,
                worker_id=body.worker_id,
                next_status=body.next_status,
            )
            return task.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except PlatformHarnessViolation as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/v1/platform/harness/leases/reconcile", dependencies=[Depends(require_token)])
    async def reconcile_platform_harness_task_leases() -> list[dict[str, Any]]:
        tasks = await services.harness.reconcile_expired_task_leases()
        return [task.model_dump(mode="json") for task in tasks]

    @app.get("/api/v1/platform/harness/queue-semantics", dependencies=[Depends(require_token)])
    async def get_platform_harness_queue_semantics(limit: int = 100) -> dict[str, Any]:
        return await services.harness.queue_semantics_snapshot(limit=max(1, min(limit, 500)))

    @app.post("/api/v1/platform/harness/queue/requeue-expired", dependencies=[Depends(require_token)])
    async def requeue_platform_harness_expired_queue_tasks() -> list[dict[str, Any]]:
        tasks = await services.harness.requeue_expired_task_leases()
        return [task.model_dump(mode="json") for task in tasks]

    @app.get("/api/v1/platform/harness/worker-heartbeats", dependencies=[Depends(require_token)])
    async def list_platform_harness_worker_heartbeats(limit: int = 100) -> list[dict[str, Any]]:
        rows = await services.harness.list_worker_heartbeats(limit=max(1, min(limit, 500)))
        return [row.model_dump(mode="json") for row in rows]

    @app.get("/api/v1/platform/harness/worker-supervision", dependencies=[Depends(require_token)])
    async def get_platform_harness_worker_supervision() -> dict[str, Any]:
        if services.worker_supervisor is None:
            raise HTTPException(503, "platform worker supervisor unavailable")
        return await services.worker_supervisor.snapshot()

    @app.post("/api/v1/platform/harness/worker-supervision/start", dependencies=[Depends(require_token)])
    async def start_platform_harness_worker_supervision(
        body: PlatformWorkerSupervisionStartRequest | None = None,
    ) -> dict[str, Any]:
        if services.worker_supervisor is None:
            raise HTTPException(503, "platform worker supervisor unavailable")
        body = body or PlatformWorkerSupervisionStartRequest()
        try:
            return await services.worker_supervisor.start(
                poll_seconds=body.poll_seconds,
                limit=body.limit,
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/platform/harness/worker-supervision/stop", dependencies=[Depends(require_token)])
    async def stop_platform_harness_worker_supervision() -> dict[str, Any]:
        if services.worker_supervisor is None:
            raise HTTPException(503, "platform worker supervisor unavailable")
        return await services.worker_supervisor.stop()

    @app.get("/api/v1/platform/harness/worker-process-manager", dependencies=[Depends(require_token)])
    async def get_platform_harness_worker_process_manager() -> dict[str, Any]:
        if services.worker_process_manager is None:
            raise HTTPException(503, "platform worker process manager unavailable")
        return services.worker_process_manager.snapshot()

    @app.post("/api/v1/platform/harness/worker-process-manager/start", dependencies=[Depends(require_token)])
    async def start_platform_harness_worker_process_manager(
        body: PlatformWorkerProcessStartRequest | None = None,
    ) -> dict[str, Any]:
        if services.worker_process_manager is None:
            raise HTTPException(503, "platform worker process manager unavailable")
        body = body or PlatformWorkerProcessStartRequest()
        if body.command is not None:
            services.worker_process_manager.command = [item for item in body.command if item]
        if body.cwd is not None:
            services.worker_process_manager.cwd = body.cwd
        try:
            return services.worker_process_manager.start()
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/platform/harness/worker-process-manager/stop", dependencies=[Depends(require_token)])
    async def stop_platform_harness_worker_process_manager() -> dict[str, Any]:
        if services.worker_process_manager is None:
            raise HTTPException(503, "platform worker process manager unavailable")
        return services.worker_process_manager.stop()

    @app.post("/api/v1/platform/harness/worker-process-manager/restart", dependencies=[Depends(require_token)])
    async def restart_platform_harness_worker_process_manager(
        body: PlatformWorkerProcessStartRequest | None = None,
    ) -> dict[str, Any]:
        if services.worker_process_manager is None:
            raise HTTPException(503, "platform worker process manager unavailable")
        body = body or PlatformWorkerProcessStartRequest()
        if body.command is not None:
            services.worker_process_manager.command = [item for item in body.command if item]
        if body.cwd is not None:
            services.worker_process_manager.cwd = body.cwd
        try:
            return services.worker_process_manager.restart()
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/platform/secrets", status_code=201, dependencies=[Depends(require_token)])
    async def create_platform_secret(body: PlatformSecretCreateRequest) -> dict[str, Any]:
        try:
            return await services.harness.save_secret(
                owner_id=body.owner_id,
                name=body.name,
                value=body.value,
                description=body.description,
            )
        except PlatformHarnessViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/platform/secrets", dependencies=[Depends(require_token)])
    async def list_platform_secrets(owner_id: str | None = None) -> list[dict[str, Any]]:
        return await services.harness.list_secrets(owner_id=owner_id)

    @app.delete("/api/v1/platform/secrets/{owner_id}/{name}", dependencies=[Depends(require_token)])
    async def delete_platform_secret(owner_id: str, name: str) -> dict[str, Any]:
        try:
            deleted = await services.harness.delete_secret(owner_id=owner_id, name=name)
            if not deleted:
                raise HTTPException(404, f"platform secret not found: {owner_id}/{name}")
            return {"owner_id": owner_id, "name": name, "deleted": True}
        except PlatformHarnessViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/connectors/manifests", dependencies=[Depends(require_token)])
    async def list_connector_manifests() -> list[dict[str, Any]]:
        manifests = await services.connectors.list_manifests()
        return [item.model_dump(mode="json") for item in manifests]

    @app.get("/api/v1/connectors/generations", dependencies=[Depends(require_token)])
    async def list_connector_generations() -> list[dict[str, Any]]:
        generations = await services.openapi_connectors.list_generations()
        return [item.model_dump(mode="json") for item in generations]

    @app.post(
        "/api/v1/connectors/generations",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def generate_connector(
        body: OpenAPIConnectorGenerationRequest,
    ) -> dict[str, Any]:
        try:
            generated = await services.openapi_connectors.generate(body)
            return generated.model_dump(mode="json")
        except OpenAPIMaterialError as error:
            raise HTTPException(
                422,
                {
                    "message": str(error),
                    "capability_gap": error.gap.model_dump(mode="json"),
                },
            ) from error
        except httpx.HTTPError as error:
            raise HTTPException(422, f"OpenAPI document fetch failed: {error}") from error

    @app.get(
        "/api/v1/connectors/generations/{generation_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_connector_generation(generation_id: str) -> dict[str, Any]:
        try:
            generated = await services.openapi_connectors.get_generation(generation_id)
            return generated.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, f"connector generation not found: {generation_id}") from error

    @app.get(
        "/api/v1/connectors/generations/{generation_id}/contract-cases",
        dependencies=[Depends(require_token)],
    )
    async def get_generated_contract_cases(generation_id: str) -> list[dict[str, Any]]:
        try:
            cases = await services.openapi_connectors.generate_contract_cases(generation_id)
            return [item.model_dump(mode="json") for item in cases]
        except KeyError as error:
            raise HTTPException(404, f"connector generation not found: {generation_id}") from error

    @app.get(
        "/api/v1/connectors/generations/{generation_id}/contract-runs",
        dependencies=[Depends(require_token)],
    )
    async def list_generated_contract_runs(generation_id: str) -> list[dict[str, Any]]:
        try:
            await services.openapi_connectors.get_generation(generation_id)
            runs = await services.openapi_connectors.list_contract_runs(generation_id)
            return [item.model_dump(mode="json") for item in runs]
        except KeyError as error:
            raise HTTPException(404, f"connector generation not found: {generation_id}") from error

    @app.post(
        "/api/v1/connectors/generations/{generation_id}/contract-runs",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def run_generated_contracts(
        generation_id: str,
        body: ConnectorContractRunRequest,
    ) -> dict[str, Any]:
        try:
            run = await services.openapi_connectors.run_contracts(generation_id, body)
            return run.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, f"connector generation not found: {generation_id}") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/connectors/generations/{generation_id}/register",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def register_generated_connector(generation_id: str) -> dict[str, Any]:
        try:
            manifest = await services.openapi_connectors.register_verified(generation_id)
            return manifest.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, f"connector generation not found: {generation_id}") from error
        except ConnectorConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/connectors/manifests",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def register_connector_manifest(body: ConnectorManifest) -> dict[str, Any]:
        try:
            saved = await services.connectors.register_manifest(body)
            return saved.model_dump(mode="json")
        except ConnectorConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.get(
        "/api/v1/connectors/manifests/{connector_id}/{version}",
        dependencies=[Depends(require_token)],
    )
    async def get_connector_manifest(connector_id: str, version: int) -> dict[str, Any]:
        try:
            manifest = await services.connectors.get_manifest(connector_id, version)
            return manifest.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/connectors/manifests/{connector_id}/{version}/contract",
        dependencies=[Depends(require_token)],
    )
    async def get_connector_contract(connector_id: str, version: int) -> dict[str, Any]:
        try:
            manifest = await services.connectors.get_manifest(connector_id, version)
            return manifest.contract_document()
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/v1/connectors/bindings", dependencies=[Depends(require_token)])
    async def list_connector_bindings(
        connector_id: str | None = None,
        tenant_id: str | None = None,
        application_id: str | None = None,
    ) -> list[dict[str, Any]]:
        bindings = await services.connectors.list_bindings(
            connector_id,
            tenant_id=tenant_id,
            application_id=application_id,
        )
        return [item.model_dump(mode="json") for item in bindings]

    @app.put("/api/v1/connectors/bindings", dependencies=[Depends(require_token)])
    async def upsert_connector_binding(
        body: ConnectorBindingUpsertRequest,
    ) -> dict[str, Any]:
        try:
            saved = await services.connectors.upsert_binding(
                body.binding,
                expected_revision=body.expected_revision,
            )
            return saved.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ConnectorConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/connectors/policies", dependencies=[Depends(require_token)])
    async def list_connector_policies(
        application_id: str | None = None,
    ) -> list[dict[str, Any]]:
        policies = await services.connectors.list_policies(application_id=application_id)
        return [item.model_dump(mode="json") for item in policies]

    @app.put("/api/v1/connectors/policies", dependencies=[Depends(require_token)])
    async def upsert_connector_policy(
        body: ConnectorPolicyUpsertRequest,
    ) -> dict[str, Any]:
        try:
            saved = await services.connectors.set_policy(
                body.policy,
                expected_revision=body.expected_revision,
            )
            return saved.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ConnectorConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/connectors/policies/{connector_id}/{version}/{tenant_id}/emergency-stop",
        dependencies=[Depends(require_token)],
    )
    async def set_connector_emergency_stop(
        connector_id: str,
        version: int,
        tenant_id: str,
        body: ConnectorEmergencyStopRequest,
    ) -> dict[str, Any]:
        try:
            current = await services.connectors.get_policy(connector_id, version, tenant_id)
            saved = await services.connectors.set_policy(
                current.model_copy(
                    update={
                        "emergency_stop": body.enabled,
                        "emergency_reason": body.reason,
                    }
                ),
                expected_revision=body.expected_revision,
            )
            return saved.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ConnectorConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/connectors/authorizations",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def create_connector_authorization(
        body: ConnectorAuthorizationCreateRequest,
    ) -> dict[str, Any]:
        try:
            authorization = await services.connectors.create_authorization(
                connector_id=body.connector_id,
                connector_version=body.connector_version,
                tenant_id=body.tenant_id,
                actor_id=body.actor_id,
                profile_id=body.profile_id,
                operation_id=body.operation_id,
                payload=body.payload,
                expires_in_seconds=body.expires_in_seconds,
                max_uses=body.max_uses,
            )
            return authorization.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ConnectorDenied as error:
            raise HTTPException(403, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/connectors/executions",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def execute_connector_operation(
        body: ConnectorExecutionRequest,
    ) -> dict[str, Any]:
        try:
            record = await services.connectors.execute(body)
            return {
                "receipt": record.public_receipt(),
                "response": record.response,
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ConnectorDenied as error:
            raise HTTPException(403, str(error)) from error
        except ConnectorConflict as error:
            raise HTTPException(409, str(error)) from error
        except ConnectorAdapterError as error:
            raise HTTPException(502, str(error)) from error
        except (ValueError, PlatformHarnessViolation) as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/connectors/executions", dependencies=[Depends(require_token)])
    async def list_connector_executions(
        connector_id: str | None = None,
        tenant_id: str | None = None,
        application_id: str | None = None,
        operation_id: str | None = None,
        execution_status: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        records = await services.connectors.list_executions(
            connector_id=connector_id,
            tenant_id=tenant_id,
            application_id=application_id,
            operation_id=operation_id,
            status=execution_status,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [item.public_receipt() for item in records],
            "offset": offset,
            "limit": limit,
            "has_more": len(records) == limit,
            "claim_boundary": (
                "Tenant-scoped local or controlled-test Connector evidence; "
                "not customer-production reliability evidence."
            ),
        }

    @app.get(
        "/api/v1/connectors/executions/{execution_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_connector_execution(execution_id: str) -> dict[str, Any]:
        try:
            record = await services.connectors.get_execution(execution_id)
            return {
                "receipt": record.public_receipt(),
                "request_payload": record.request_payload,
                "response": record.response,
                "response_hash": record.response_hash,
                "error": record.error,
                "authorization_id": record.authorization_id,
                "idempotency_key": record.idempotency_key,
                "actor_id": record.actor_id,
                "actor_roles": record.actor_roles,
                "application_id": record.application_id,
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/connectors/executions/{execution_id}/events",
        dependencies=[Depends(require_token)],
    )
    async def list_connector_execution_events(
        execution_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return await services.connectors.list_events(
            execution_id=execution_id,
            limit=limit,
            offset=offset,
        )

    @app.post(
        "/api/v1/connectors/executions/{execution_id}/compensate",
        dependencies=[Depends(require_token)],
    )
    async def compensate_connector_execution(
        execution_id: str,
        body: ConnectorCompensationRequest,
    ) -> dict[str, Any]:
        try:
            record = await services.connectors.compensate(
                execution_id,
                actor_id=body.actor_id,
                actor_roles=body.actor_roles,
                authorization_id=body.authorization_id,
                idempotency_key=body.idempotency_key,
            )
            return record.public_receipt()
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ConnectorDenied as error:
            raise HTTPException(403, str(error)) from error
        except ConnectorConflict as error:
            raise HTTPException(409, str(error)) from error
        except ConnectorAdapterError as error:
            raise HTTPException(502, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/connectors/callbacks")
    async def receive_connector_callback(
        body: ConnectorCallback,
        signature: str = Header(alias="X-Lilies-Signature"),
    ) -> dict[str, Any]:
        try:
            record = await services.connectors.record_callback(
                body,
                signature=signature,
            )
            return record.public_receipt()
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ConnectorDenied as error:
            raise HTTPException(403, str(error)) from error
        except ConnectorConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/connectors/exercises",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def run_connector_exercise(body: ConnectorExerciseRequest) -> dict[str, Any]:
        try:
            exercise = await services.connectors.run_exercise(
                connector_id=body.connector_id,
                connector_version=body.connector_version,
                tenant_id=body.tenant_id,
                kind=body.kind,
                execution_id=body.execution_id,
            )
            return exercise.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ConnectorConflict, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/connectors/exercises", dependencies=[Depends(require_token)])
    async def list_connector_exercises(
        connector_id: str | None = None,
        tenant_id: str | None = None,
        application_id: str | None = None,
    ) -> list[dict[str, Any]]:
        exercises = await services.connectors.list_exercises(
            connector_id=connector_id,
            tenant_id=tenant_id,
            application_id=application_id,
        )
        return [item.model_dump(mode="json") for item in exercises]

    @app.post(
        "/api/v1/applications/{application_id}/connector-test-runs",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    async def create_controlled_connector_test_run(
        application_id: str,
        body: ConnectorTestRunRequest,
    ) -> dict[str, Any]:
        try:
            identity = services.connectors.controlled_test_identity(application_id)
            run = await services.workflow_runtime.create_run(
                application_id,
                WorkflowRunRequest(
                    inputs={
                        "tenant_id": identity.tenant_id,
                        "actor_id": identity.actor_id,
                        "actor_roles": identity.actor_roles,
                        "request": body.request,
                        "connector_profile_id": identity.profile_id,
                        "connector_authorization_id": "",
                        "connector_idempotency_key": body.idempotency_key,
                        "write_mode": "dry_run",
                    },
                    use_draft=body.use_draft,
                ),
                origin="connector_controlled_test_runtime",
            )
            return {
                **run,
                "tenant_id": identity.tenant_id,
                "mode": "controlled_test_dry_run",
                "claim_boundary": (
                    "Authenticated Customer Runtime preview against one configured mock/test tenant; "
                    "mutation, live customer identity, and production evidence are excluded."
                ),
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ConnectorDenied as error:
            raise HTTPException(403, str(error)) from error
        except (ValueError, RuntimeError) as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/embedding/invoke", status_code=202)
    async def invoke_embedded_workflow(
        body: ConnectorEmbeddingEnvelope,
        signature: str = Header(alias="X-Lilies-Signature"),
    ) -> dict[str, Any]:
        try:
            identity = await services.connectors.resolve_embedding_identity(
                body,
                signature,
            )
            run = await services.workflow_runtime.create_run(
                identity.application_id,
                WorkflowRunRequest(
                    inputs={
                        "tenant_id": identity.tenant_id,
                        "actor_id": identity.actor_id,
                        "actor_roles": identity.actor_roles,
                        "request": body.request,
                        "connector_profile_id": identity.profile_id,
                        "connector_authorization_id": body.authorization_id,
                        "connector_idempotency_key": body.idempotency_key,
                        "write_mode": body.write_mode,
                    },
                    use_draft=False,
                ),
                origin="connector_embedding",
            )
            return {
                **run,
                "application_id": identity.application_id,
                "tenant_id": identity.tenant_id,
                "write_mode": body.write_mode,
                "claim_boundary": (
                    "Signed tenant-scoped invocation against a published Lilies workflow; "
                    "production identity, SLO, and deployment compliance are not implied."
                ),
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ConnectorDenied as error:
            raise HTTPException(403, str(error)) from error
        except ConnectorConflict as error:
            raise HTTPException(409, str(error)) from error
        except (ValueError, RuntimeError, PlatformHarnessViolation) as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/platform/governed-memory", status_code=201, dependencies=[Depends(require_token)])
    async def create_governed_memory(body: GovernedMemoryCreateRequest) -> dict[str, Any]:
        try:
            item = await services.governed_memory.create(
                permission=body.permission,
                content=body.content,
                source=body.source,
                retention_class=body.retention_class,
                reason=body.reason,
                expires_at=body.expires_at,
            )
            return item.model_dump(mode="json")
        except GovernedMemoryViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/platform/governed-memory", dependencies=[Depends(require_token)])
    async def list_governed_memory(
        owner_id: str,
        scope_id: str,
        actor_id: str,
        purpose: str,
        reason: str,
        status_filter: MemoryStatus | Literal["all"] = "active",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        permission = GovernedMemoryPermission(
            actor_id=actor_id,
            owner_id=owner_id,
            scope_id=scope_id,
            purpose=purpose,
            allowed_operations=["read"],
        )
        try:
            items = await services.governed_memory.list_for_operator(
                owner_id=owner_id,
                scope_id=scope_id,
                permission=permission,
                reason=reason,
                status_filter=status_filter,
                limit=limit,
            )
            return [item.model_dump(mode="json") for item in items]
        except GovernedMemoryViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/platform/governed-memory/{memory_id}/read", dependencies=[Depends(require_token)])
    async def read_governed_memory(memory_id: str, body: GovernedMemoryReadRequest) -> dict[str, Any]:
        try:
            item = await services.governed_memory.read(memory_id, permission=body.permission, reason=body.reason)
            return item.model_dump(mode="json")
        except GovernedMemoryViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.patch("/api/v1/platform/governed-memory/{memory_id}", dependencies=[Depends(require_token)])
    async def update_governed_memory(memory_id: str, body: GovernedMemoryUpdateRequest) -> dict[str, Any]:
        try:
            item = await services.governed_memory.update(
                memory_id,
                permission=body.permission,
                content=body.content,
                source=body.source,
                reason=body.reason,
            )
            return item.model_dump(mode="json")
        except GovernedMemoryViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/platform/governed-memory/{memory_id}/revoke", dependencies=[Depends(require_token)])
    async def revoke_governed_memory(memory_id: str, body: GovernedMemoryReadRequest) -> dict[str, Any]:
        try:
            item = await services.governed_memory.revoke(memory_id, permission=body.permission, reason=body.reason)
            return item.model_dump(mode="json")
        except GovernedMemoryViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/platform/governed-memory/expire", dependencies=[Depends(require_token)])
    async def expire_governed_memory(body: GovernedMemoryExpireRequest) -> dict[str, Any]:
        try:
            expired = await services.governed_memory.expire_due(
                owner_id=body.permission.owner_id,
                permission=body.permission,
                reason=body.reason,
                now=body.now,
            )
            return {"expired": [item.model_dump(mode="json") for item in expired], "expired_count": len(expired)}
        except GovernedMemoryViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/builder-benchmark/history", dependencies=[Depends(require_token)])
    async def list_builder_benchmark_history(
        owner_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        tasks = await services.harness.list_tasks(
            kind="benchmark",
            status=status,
            owner_id=owner_id,
            limit=max(1, min(limit, 500)),
        )
        return [
            {
                "id": task.id,
                "status": task.status,
                "owner_id": task.owner_id,
                "resource_id": task.resource_id,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
                "finished_at": task.finished_at,
                "metadata": task.metadata,
                "usage_counts": task.usage_counts,
                "error": task.error,
            }
            for task in tasks
        ]

    @app.post("/api/v1/builder-benchmark/evaluate", dependencies=[Depends(require_token)])
    async def evaluate_builder_benchmark(body: BuilderBenchmarkCase) -> dict[str, Any]:
        task_id = str(uuid4())
        await services.harness.start_task(
            task_id,
            kind="benchmark",
            owner_id="builder-benchmark",
            resource_id=body.name,
            metadata={"case": body.name},
        )
        try:
            report = services.benchmark.evaluate(body)
            await services.harness.finish_task(
                task_id,
                status="succeeded" if report.passed else "failed",
                metadata={"score": report.score},
            )
            return {"task_id": task_id, "report": report.model_dump(mode="json")}
        except Exception as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise

    @app.post("/api/v1/builder-benchmark/suites/evaluate", dependencies=[Depends(require_token)])
    async def evaluate_builder_benchmark_suite(body: BuilderBenchmarkSuiteCase) -> dict[str, Any]:
        task_id = str(uuid4())
        await services.harness.start_task(
            task_id,
            kind="benchmark",
            owner_id="builder-benchmark-suite",
            resource_id=body.name,
            metadata={
                "suite": body.name,
                "case_count": len(body.cases),
                "minimum_score": body.minimum_score,
                "minimum_pass_rate": body.minimum_pass_rate,
            },
        )
        try:
            await services.harness.record_usage(
                task_id,
                "node_execution",
                amount=max(1, len(body.cases)),
                metadata={
                    "operation": "builder_benchmark_suite",
                    "case_count": len(body.cases),
                },
            )
            report = services.benchmark.evaluate_suite(body)
            await services.harness.finish_task(
                task_id,
                status="succeeded" if report.passed else "failed",
                metadata={
                    "score": report.score,
                    "pass_rate": report.pass_rate,
                    "failed_cases": report.failed_cases,
                },
            )
            return {"task_id": task_id, "report": report.model_dump(mode="json")}
        except Exception as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise

    @app.get("/api/v1/blocks", dependencies=[Depends(require_token)])
    async def list_blocks() -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in services.blocks.list()]

    @app.get("/api/v1/block-manuals", dependencies=[Depends(require_token)])
    async def list_block_manuals(
        query: str = "",
        block_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        return services.blocks.manuals(query=query, block_kind=block_kind)

    @app.get("/api/v1/claude-architecture-blueprint", dependencies=[Depends(require_token)])
    async def claude_architecture_blueprint() -> dict[str, Any]:
        return services.blocks.claude_architecture_blueprint()

    @app.get("/api/v1/blocks/{block_type}", dependencies=[Depends(require_token)])
    async def get_block(block_type: str) -> dict[str, Any]:
        try:
            return services.blocks.get(block_type).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/v1/blocks/{block_type}/manual", dependencies=[Depends(require_token)])
    async def get_block_manual(block_type: str) -> dict[str, Any]:
        try:
            return services.blocks.manual(block_type)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    # ── Templates ───────────────────────────────────────────

    def module_record_payload(record: Any, *, include_workflow: bool = False) -> dict[str, Any]:
        template = record.template
        contract = template.module_contract
        payload: dict[str, Any] = {
            "module_id": record.state.module_id,
            "version": record.state.version,
            "module_ref": record.module_ref,
            "content_hash": record.state.content_hash,
            "source": record.state.source,
            "status": record.state.status,
            "created_at": record.state.created_at,
            "verified_at": record.state.verified_at,
            "verification_errors": record.state.verification_errors,
            "evidence_record_ids": record.state.evidence_record_ids,
            "meta": template.meta.model_dump(mode="json"),
            "contract": contract.model_dump(mode="json") if contract else None,
        }
        if include_workflow:
            payload["workflow"] = template.workflow.model_dump(mode="json")
        return payload

    @app.get("/api/v1/capability-modules", dependencies=[Depends(require_token)])
    async def list_capability_modules(
        all_versions: bool = False,
        status: str | None = None,
        query: str = "",
    ) -> list[dict[str, Any]]:
        allowed_statuses = {
            "legacy_unverified", "draft", "verified", "deprecated", "quarantined"
        }
        if status is not None and status not in allowed_statuses:
            raise HTTPException(422, f"unknown module status: {status}")
        records = services.templates.list_records(
            all_versions=all_versions,
            status=status,  # type: ignore[arg-type]
            query=query,
        )
        return [module_record_payload(record) for record in records]

    @app.get(
        "/api/v1/capability-modules/{module_id}/versions",
        dependencies=[Depends(require_token)],
    )
    async def list_capability_module_versions(module_id: str) -> list[dict[str, Any]]:
        try:
            versions = services.templates.versions(module_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        return [
            module_record_payload(services.templates.get_record(module_id, version))
            for version in versions
        ]

    @app.get(
        "/api/v1/capability-modules/{module_id}/versions/{version}",
        dependencies=[Depends(require_token)],
    )
    async def get_capability_module_version(
        module_id: str,
        version: int,
    ) -> dict[str, Any]:
        try:
            return module_record_payload(
                services.templates.get_record(module_id, version),
                include_workflow=True,
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/capability-modules/{module_id}/versions/{version}/insert",
        dependencies=[Depends(require_token)],
    )
    async def insert_capability_module_version(
        application_id: str,
        module_id: str,
        version: int,
        body: ModuleInsertRequest,
    ) -> dict[str, Any]:
        try:
            record = services.templates.get_record(module_id, version)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        if record.state.status != "verified":
            raise HTTPException(
                409,
                f"only verified exact module versions can be inserted: {record.module_ref}",
            )
        workflow = services.templates.expand_into_workflow(
            module_id,
            version=version,
            prefix=body.prefix,
            x=body.x,
            y=body.y,
        )
        operations = [
            {"op": "add_node", "data": {"node": node.model_dump(mode="json")}}
            for node in workflow.nodes
        ] + [
            {"op": "add_edge", "data": {"edge": edge.model_dump(mode="json")}}
            for edge in workflow.edges
        ]
        try:
            result = await services.applications.apply_operations_atomically(
                application_id,
                expected_revision=body.expected_revision,
                expected_content_hash=body.expected_content_hash,
                idempotency_key=body.idempotency_key,
                change_context_operation="verified_module_insert",
                operations=operations,
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except RevisionConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        updated_draft = await services.workflow_store.get_draft(application_id)
        return {
            "module": module_record_payload(record),
            "inserted_node_ids": [node.id for node in workflow.nodes],
            "inserted_edge_ids": [edge.id for edge in workflow.edges],
            "draft": {
                **updated_draft,
                "operations_applied": result["operations_applied"],
                "previous_content_hash": result["previous_content_hash"],
            },
        }

    @app.post(
        "/api/v1/capability-modules/{module_id}/versions/{version}/evidence",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def add_capability_module_evidence(
        module_id: str,
        version: int,
        body: CapabilityEvidenceCreateRequest,
    ) -> dict[str, Any]:
        try:
            return services.templates.add_evidence(
                module_id,
                version,
                body,
            ).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/capability-modules/{module_id}/versions/{version}/verify",
        dependencies=[Depends(require_token)],
    )
    async def verify_capability_module_version(
        module_id: str,
        version: int,
    ) -> dict[str, Any]:
        try:
            return module_record_payload(services.templates.verify(module_id, version))
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            try:
                record = services.templates.get_record(module_id, version)
                detail = {
                    "message": str(error),
                    "module": module_record_payload(record),
                }
            except KeyError:
                detail = str(error)
            raise HTTPException(422, detail) from error

    @app.get("/api/v1/capability-evidence", dependencies=[Depends(require_token)])
    async def list_capability_evidence(
        capability_id: str | None = None,
        module_id: str | None = None,
        module_version: int | None = None,
        verification_status: VerificationStatus | None = None,
        category: ArtifactCategory | None = None,
    ) -> list[dict[str, Any]]:
        records = services.templates.evidence.list(
            capability_id=capability_id,
            module_id=module_id,
            module_version=module_version,
            verification_status=verification_status,
            category=category,
        )
        return [
            {
                **record.model_dump(mode="json"),
                "artifact_categories": record.artifact_categories,
            }
            for record in records
        ]

    @app.get(
        "/api/v1/capability-evidence/{record_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_capability_evidence(record_id: str) -> dict[str, Any]:
        try:
            record = services.templates.evidence.get(record_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        return {
            **record.model_dump(mode="json"),
            "artifact_categories": record.artifact_categories,
        }

    @app.get("/api/v1/templates", dependencies=[Depends(require_token)])
    async def list_templates(
        category: str | None = None,
        query: str = "",
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for meta in services.templates.list(category=category, query=query):
            record = services.templates.get_record(meta.name, meta.version)
            payloads.append({
                **meta.model_dump(mode="json"),
                "module_ref": record.module_ref,
                "module_status": record.state.status,
                "content_hash": record.state.content_hash,
                "module_contract": (
                    record.template.module_contract.model_dump(mode="json")
                    if record.template.module_contract
                    else None
                ),
            })
        return payloads

    @app.get("/api/v1/templates/categories", dependencies=[Depends(require_token)])
    async def template_categories() -> list[str]:
        return services.templates.categories()

    @app.post(
        "/api/v1/templates/{name}/rate",
        dependencies=[Depends(require_token)],
    )
    async def rate_template(name: str, body: dict[str, Any] = {}) -> dict[str, Any]:
        """Rate a template 1-5. Affects quality_score and ranking."""
        rating = int(body.get("rating", 3))
        if not 1 <= rating <= 5:
            raise HTTPException(422, "rating must be 1-5")
        try:
            template = services.templates.get(name)
        except KeyError:
            raise HTTPException(404, f"template not found: {name}")
        template.meta.rating_sum += rating
        template.meta.rating_count += 1
        return {
            "name": name,
            "rating": template.meta.rating,
            "rating_count": template.meta.rating_count,
            "quality_score": template.meta.quality_score,
        }

    @app.get("/api/v1/templates/suggestions", dependencies=[Depends(require_token)])
    async def suggest_templates(requirement: str = "", reuse_depth: str | None = None) -> list[dict[str, Any]]:
        """Suggest matching templates for a requirement, sorted by relevance."""
        if not requirement:
            return []
        reuse_depth, default_metadata = suggestion_default_metadata(reuse_depth)
        if reuse_depth not in ALLOWED_REUSE_DEPTHS:
            raise HTTPException(422, "reuse_depth must be one of: adaptive, deep, none, shallow")
        if reuse_depth == "none":
            return []
        scored = score_template_matches(
            requirement,
            [
                record.template.meta
                for record in services.templates.list_records(all_versions=True)
            ],
        )
        payloads: list[dict[str, Any]] = []
        for score, meta in scored[:5]:
            record = services.templates.get_record(meta.name, meta.version)
            payloads.append({
                **build_suggestion_payload(
                    meta,
                    score,
                    reuse_depth,
                    default_metadata=default_metadata,
                ),
                "module_ref": record.module_ref,
                "module_status": record.state.status,
                "verified_capability_carrier": record.state.status == "verified",
            })
        return payloads

    @app.get("/api/v1/templates/adaptive-monitoring", dependencies=[Depends(require_token)])
    async def get_adaptive_template_monitoring() -> dict[str, Any]:
        return adaptive_monitoring_status_with_history(services.settings.data_dir)

    @app.post("/api/v1/templates/adaptive-monitoring/refresh", dependencies=[Depends(require_token)])
    async def refresh_adaptive_template_monitoring() -> dict[str, Any]:
        return record_adaptive_monitoring_refresh(services.settings.data_dir)

    @app.get("/api/v1/templates/adaptive-monitoring/schedule", dependencies=[Depends(require_token)])
    async def get_adaptive_template_monitoring_schedule() -> dict[str, Any]:
        interval = services.settings.adaptive_monitoring_refresh_interval_seconds
        running = any(
            task.get_name() == "adaptive-monitoring-refresh-loop" and not task.done()
            for task in services.background_tasks
        )
        return adaptive_monitoring_schedule_status(services.settings.data_dir, interval, running=running)

    @app.post("/api/v1/templates/adaptive-monitoring/schedule/run-once", dependencies=[Depends(require_token)])
    async def run_adaptive_template_monitoring_schedule_once() -> dict[str, Any]:
        record_adaptive_monitoring_refresh(services.settings.data_dir, trigger="manual_schedule_run")
        interval = services.settings.adaptive_monitoring_refresh_interval_seconds
        running = any(
            task.get_name() == "adaptive-monitoring-refresh-loop" and not task.done()
            for task in services.background_tasks
        )
        return adaptive_monitoring_schedule_status(services.settings.data_dir, interval, running=running)

    @app.get("/api/v1/templates/{name}", dependencies=[Depends(require_token)])
    async def get_template(name: str, version: int | None = None) -> dict[str, Any]:
        try:
            record = services.templates.get_record(name, version)
            return {
                **record.template.model_dump(mode="json"),
                "registry": module_record_payload(record),
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/templates/{name}/expand",
        dependencies=[Depends(require_token)],
    )
    async def expand_template(
        name: str,
        version: int | None = None,
        prefix: str = "",
        x: float = 0,
        y: float = 0,
    ) -> dict[str, Any]:
        try:
            wf = services.templates.expand_into_workflow(
                name,
                version=version,
                prefix=prefix,
                x=x,
                y=y,
            )
            return wf.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/publish-template",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def publish_template(
        application_id: str, body: TemplateCreateRequest
    ) -> dict[str, Any]:
        try:
            draft = await services.workflow_store.get_draft(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        app = await services.workflow_store.get_application(application_id)
        name = app["name"].lower().replace(" ", "_").replace("-", "_")
        template = services.templates.register(
            name,
            draft["snapshot"].workflow,
            meta_overrides={
                "title": body.title or app["name"],
                "description": body.description or app["description"],
                "category": body.category,
                "tags": body.tags,
                "icon": body.icon,
                "author": "user",
            },
            module_contract=body.module_contract,
        )
        record = services.templates.get_record(template.meta.name, template.meta.version)
        return {
            **template.model_dump(mode="json"),
            "registry": module_record_payload(record),
        }

    # ── Meta-Cognition (session extraction) ──────────────────

    @app.post(
        "/api/v1/sessions/{session_id}/extract-template",
        dependencies=[Depends(require_token)],
    )
    async def extract_template_from_session(session_id: str) -> dict[str, Any]:
        """Try to extract a workflow template from a session's decision history."""
        try:
            record = await services.storage.get_session(session_id)
        except KeyError:
            raise HTTPException(404, f"session not found: {session_id}")

        from .meta_cognition import DecisionTracker
        from .extraction_gate import ExtractionGate

        # Build a DecisionTracker from session messages
        tracker = DecisionTracker(f"Session {session_id[:8]}")
        messages = record.get("messages", [])
        # Extract decision points from user/assistant message pairs
        decision_count = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and i + 1 < len(messages):
                if messages[i + 1].get("role") == "assistant":
                    question = "".join(
                        b.get("text", "") for b in msg.get("content", [])
                        if b.get("type") == "text"
                    )[:200]
                    answer = "".join(
                        b.get("text", "") for b in messages[i + 1].get("content", [])
                        if b.get("type") == "text"
                    )[:200]
                    if question and answer and len(question) > 20:
                        tracker._current = tracker.ask(question, f"Session {session_id[:8]}")
                        tracker.answer("continue", answer)
                        decision_count += 1

        # Gate check
        gate = ExtractionGate(services.templates)
        should, reason = gate.should_propose(tracker.roots)

        if not should:
            return {"proposed": False, "reason": reason, "decision_points": decision_count}

        wf = tracker.extract_workflow()
        return {
            "proposed": True,
            "workflow": wf.model_dump(mode="json") if wf else None,
            "summary": tracker.summary(),
            "decision_points": decision_count,
            "similar_templates": [],
        }

    @app.post(
        "/api/v1/templates/{name}/merge-check",
        dependencies=[Depends(require_token)],
    )
    async def check_template_merge(
        name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Check if a candidate workflow should be merged into an existing template."""
        from .merge_engine import MergeEngine
        from .workflow_models import WorkflowSpec

        try:
            services.templates.get(name)
        except KeyError:
            raise HTTPException(404, f"template not found: {name}")

        try:
            candidate = WorkflowSpec.model_validate(body.get("candidate", {}))
        except Exception as e:
            raise HTTPException(422, f"invalid candidate workflow: {e}")

        engine = MergeEngine(services.templates)
        result = engine.check_similarity(candidate)

        return {
            "should_merge": result.should_merge,
            "target_template": result.target_template,
            "similarity_score": result.similarity_score,
            "confidence_after": result.confidence_after,
            "diff_summary": result.diff_summary,
        }

    @app.post(
        "/api/v1/templates/{name}/merge",
        dependencies=[Depends(require_token)],
    )
    async def merge_template(
        name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge a candidate workflow into an existing template."""
        from .merge_engine import MergeEngine
        from .workflow_models import WorkflowSpec
        from .template_models import ProvenanceSource
        from datetime import datetime, timezone

        try:
            candidate = WorkflowSpec.model_validate(body.get("candidate", {}))
        except Exception as e:
            raise HTTPException(422, f"invalid candidate workflow: {e}")

        source = ProvenanceSource(
            source_type=body.get("source_type", "session_extract"),
            identifier=body.get("identifier", ""),
            created_at=body.get("created_at", datetime.now(timezone.utc).isoformat()),
            user_id=body.get("user_id"),
        )

        engine = MergeEngine(services.templates)
        confirm = body.get("confirm", False)
        if not confirm:
            # Return what would happen without executing
            sim = engine.check_similarity(candidate)
            return {
                "dry_run": True,
                "should_merge": sim.should_merge,
                "confidence_after": sim.confidence_after,
                "diff_summary": sim.diff_summary,
            }

        merged = engine.merge(candidate, name, source)
        if merged is None:
            raise HTTPException(404, f"merge failed for template: {name}")

        return merged.model_dump(mode="json")

    # ── Orchestration Advisor ──────────────────────────────────

    @app.get(
        "/api/v1/orchestration/advise",
        dependencies=[Depends(require_token)],
    )
    async def orchestration_advise(
        requirement: str = "",
    ) -> dict[str, Any]:
        """Recommend block sequences, blocks, and templates for a requirement."""
        from .orchestration_advisor import OrchestrationAdvisor
        advisor = OrchestrationAdvisor(services.blocks, services.templates)
        return advisor.recommend_all(requirement)

    # ── Observability ─────────────────────────────────────────

    @app.get(
        "/api/v1/runs/{run_id}/metrics",
        dependencies=[Depends(require_token)],
    )
    async def run_metrics(run_id: str) -> dict[str, Any]:
        """Get detailed metrics for a completed workflow run."""
        from .observability import RunAnalyzer, render_metrics_summary
        analyzer = RunAnalyzer(services.storage)
        metrics = await analyzer.analyze(run_id)
        if metrics is None:
            raise HTTPException(404, f"no events found for run: {run_id}")
        return {
            "metrics": {
                "run_id": metrics.run_id,
                "status": metrics.status,
                "total_elapsed_ms": metrics.total_elapsed_ms,
                "total_input_tokens": metrics.total_input_tokens,
                "total_output_tokens": metrics.total_output_tokens,
                "total_cost_usd": metrics.total_cost_usd,
                "node_count": metrics.node_count,
                "tool_call_count": metrics.tool_call_count,
                "error_count": metrics.error_count,
                "failure_pattern": metrics.failure_pattern,
            },
            "node_breakdown": [
                {
                    "node_id": n.node_id,
                    "node_type": n.node_type,
                    "title": n.title,
                    "elapsed_ms": n.elapsed_ms,
                    "input_tokens": n.input_tokens,
                    "output_tokens": n.output_tokens,
                    "cost_usd": n.cost_usd,
                    "failed": n.failed,
                    "retry_count": n.retry_count,
                }
                for n in (metrics.nodes or [])[:20]
            ],
            "summary_markdown": render_metrics_summary(metrics),
        }

    @app.get(
        "/api/v1/applications/{application_id}/failure-patterns",
        dependencies=[Depends(require_token)],
    )
    async def application_failure_patterns(
        application_id: str,
    ) -> list[dict[str, Any]]:
        """Get failure pattern clusters for an application."""
        from .observability import RunAnalyzer
        analyzer = RunAnalyzer(services.storage)
        patterns = await analyzer.failure_patterns(application_id)
        return [
            {
                "pattern_name": p.pattern_name,
                "count": p.count,
                "example_run_ids": p.example_run_ids,
            }
            for p in patterns
        ]

    # ── Module Protocol Validation ────────────────────────────

    @app.get(
        "/api/v1/module-protocol/validate",
        dependencies=[Depends(require_token)],
    )
    async def validate_module_output(
        data: str = "",
    ) -> dict[str, Any]:
        """Check if a JSON value conforms to the ModuleOutput envelope."""
        from .module_protocol import is_envelope
        import json as _json
        try:
            parsed = _json.loads(data) if data else {}
        except _json.JSONDecodeError:
            return {"valid": False, "reason": "invalid JSON"}
        return {
            "valid": is_envelope(parsed),
            "has_result": "result" in parsed,
            "has_structured": "structured" in parsed,
            "has_module_name": "module_name" in parsed,
        }

    # ── Soft Block Strategies ──────────────────────────────────

    @app.get(
        "/api/v1/soft-block/strategies",
        dependencies=[Depends(require_token)],
    )
    async def list_soft_block_strategies(
        family: str | None = None,
    ) -> dict[str, Any]:
        """List available soft-block strategies, grouped by family."""
        from .soft_block import (
            FAMILY_MAP, strategy_help, get_discrete_block_type,
        )
        if family and family in FAMILY_MAP:
            strategies = {
                s: {
                    "help": strategy_help(s),
                    "maps_to": get_discrete_block_type(s),
                }
                for s in FAMILY_MAP[family]
            }
            return {"family": family, "strategies": strategies}

        result = {}
        for fam, strategies in FAMILY_MAP.items():
            result[fam] = {
                s: {
                    "help": strategy_help(s),
                    "maps_to": get_discrete_block_type(s),
                }
                for s in strategies
            }
        return {"families": list(FAMILY_MAP.keys()), "strategies": result}

    # ── Tools ────────────────────────────────────────────────

    @app.get("/api/v1/tools", dependencies=[Depends(require_token)])
    async def list_platform_tools() -> list[dict[str, Any]]:
        result = [
            {"name": name, "type": "core", "published": True}
            for name in services.tools.names()
        ]
        for application in await services.workflow_store.list_applications():
            if application["active_version"] is not None:
                result.append({
                    "name": f"workflow:{application['id']}",
                    "type": "workflow",
                    "title": application["name"],
                    "version": application["active_version"],
                    "published": True,
                })
        return result

    @app.get("/api/v1/schedules", dependencies=[Depends(require_token)])
    async def list_schedules() -> list[dict[str, Any]]:
        return await services.scheduler.list_schedules()

    @app.get(
        "/api/v1/applications/{application_id}/schedule-status",
        dependencies=[Depends(require_token)],
    )
    async def get_application_schedule_status(application_id: str) -> dict[str, Any]:
        try:
            return await services.scheduler.schedule_status(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/applications/{application_id}/durable-jobs",
        dependencies=[Depends(require_token)],
    )
    async def list_application_durable_jobs(
        application_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        jobs = await services.durable_jobs.list(
            application_id,
            limit=limit,
            offset=offset,
        )
        return [item.model_dump(mode="json") for item in jobs]

    @app.get(
        "/api/v1/durable-jobs/{job_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_durable_job(job_id: str) -> dict[str, Any]:
        try:
            job = await services.durable_jobs.get(job_id)
            attempts = await services.durable_jobs.list_attempts(job_id)
            return {
                **job.model_dump(mode="json"),
                "attempts": [item.model_dump(mode="json") for item in attempts],
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/durable-jobs/{job_id}/events",
        dependencies=[Depends(require_token)],
    )
    async def list_durable_job_events(
        job_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        try:
            await services.durable_jobs.get(job_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        return [
            item.model_dump(mode="json")
            for item in await services.durable_jobs.list_events(
                job_id,
                limit=limit,
                offset=offset,
            )
        ]

    @app.get(
        "/api/v1/durable-jobs/{job_id}/receipts",
        dependencies=[Depends(require_token)],
    )
    async def list_durable_job_receipts(
        job_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        try:
            await services.durable_jobs.get(job_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        return [
            item.model_dump(mode="json")
            for item in await services.durable_jobs.list_receipts(
                job_id,
                limit=limit,
                offset=offset,
            )
        ]

    @app.post(
        "/api/v1/durable-jobs/{job_id}/retry",
        dependencies=[Depends(require_token)],
    )
    async def retry_durable_job(
        job_id: str,
        body: DurableJobActionRequest,
    ) -> dict[str, Any]:
        try:
            record = await services.scheduler.retry_durable_job(
                job_id,
                expected_revision=body.expected_revision,
            )
            return record.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except DurableJobConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.post(
        "/api/v1/durable-jobs/{job_id}/resume",
        dependencies=[Depends(require_token)],
    )
    async def resume_durable_job(
        job_id: str,
        body: DurableJobActionRequest,
    ) -> dict[str, Any]:
        try:
            record = await services.scheduler.resume_durable_job(
                job_id,
                expected_revision=body.expected_revision,
            )
            return record.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except DurableJobConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.post(
        "/api/v1/durable-jobs/{job_id}/cancel",
        dependencies=[Depends(require_token)],
    )
    async def cancel_durable_job(
        job_id: str,
        body: DurableJobActionRequest,
    ) -> dict[str, Any]:
        try:
            record = await services.scheduler.cancel_durable_job(
                job_id,
                expected_revision=body.expected_revision,
            )
            return record.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except DurableJobConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/v1/scenarios", dependencies=[Depends(require_token)])
    async def list_scenarios() -> list[dict[str, Any]]:
        return services.scenarios.list()

    @app.get("/api/v1/scenarios/{scenario_id}", dependencies=[Depends(require_token)])
    async def get_scenario(scenario_id: str) -> dict[str, Any]:
        try:
            return services.scenarios.get(scenario_id).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/applications", status_code=201, dependencies=[Depends(require_token)])
    async def create_application(body: ApplicationCreateRequest) -> dict[str, Any]:
        if body.capability_build_contract is not None:
            closure = evaluate_capability_contract(body.capability_build_contract)
            if not closure.valid:
                raise HTTPException(
                    422,
                    "invalid capability build contract: "
                    + "; ".join(closure.blocking_errors),
                )
        return await services.workflow_store.create_application(body)

    @app.get("/api/v1/applications", dependencies=[Depends(require_token)])
    async def list_applications() -> list[dict[str, Any]]:
        return await services.workflow_store.list_applications()

    @app.get("/api/v1/applications/{application_id}", dependencies=[Depends(require_token)])
    async def get_application(application_id: str) -> dict[str, Any]:
        try:
            return await services.workflow_store.get_application(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/applications/{application_id}/smoke-cleanup", dependencies=[Depends(require_token)])
    async def smoke_cleanup_application(
        application_id: str,
        body: SmokeCleanupRequest,
    ) -> dict[str, Any]:
        try:
            cancelled_build_ids: list[str] = []
            cancelled_run_ids: list[str] = []
            if not body.dry_run:
                build_tasks: list[asyncio.Task[Any]] = []
                for build in await services.workflow_store.list_builds(application_id):
                    build_id = str(build["id"])
                    task = services.builder.active.get(build_id)
                    if task is None or task.done():
                        continue
                    services.builder.cancel(build_id)
                    cancelled_build_ids.append(build_id)
                    build_tasks.append(task)
                if build_tasks:
                    await asyncio.gather(*build_tasks, return_exceptions=True)

                run_tasks: list[asyncio.Task[Any]] = []
                for run_id, task in list(services.workflow_runtime.active_tasks.items()):
                    if task.done():
                        continue
                    try:
                        run = await services.workflow_store.get_run(run_id)
                    except KeyError:
                        continue
                    if run["application_id"] != application_id:
                        continue
                    services.workflow_runtime.cancel(run_id)
                    cancelled_run_ids.append(run_id)
                    run_tasks.append(task)
                if run_tasks:
                    await asyncio.gather(*run_tasks, return_exceptions=True)

            result = await services.workflow_store.smoke_cleanup_application(
                application_id,
                smoke_marker=body.smoke_marker,
                dry_run=body.dry_run,
            )
            result["cancelled_build_ids"] = cancelled_build_ids
            result["cancelled_run_ids"] = cancelled_run_ids
            return result
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/applications/{application_id}/draft", dependencies=[Depends(require_token)])
    async def get_application_draft(application_id: str) -> dict[str, Any]:
        try:
            draft = await services.workflow_store.get_draft(application_id)
            draft["snapshot"] = draft["snapshot"].model_dump(mode="json")
            return draft
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/applications/{application_id}/capability-contract",
        dependencies=[Depends(require_token)],
    )
    async def get_application_capability_contract(application_id: str) -> dict[str, Any]:
        try:
            draft = await services.workflow_store.get_draft(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        contract = draft["snapshot"].capability_build_contract
        if contract is None:
            raise HTTPException(404, "application has no capability build contract")
        return {
            "application_id": application_id,
            "revision": draft["revision"],
            "content_hash": draft["content_hash"],
            "contract": contract.model_dump(mode="json"),
            "closure": evaluate_capability_contract(contract).model_dump(mode="json"),
        }

    @app.post(
        "/api/v1/applications/{application_id}/scenarios/{scenario_id}/apply",
        dependencies=[Depends(require_token)],
    )
    async def apply_scenario_to_application(
        application_id: str,
        scenario_id: str,
        body: ScenarioApplyRequest,
    ) -> dict[str, Any]:
        try:
            scenario = services.scenarios.get(scenario_id)
            draft = await services.workflow_store.get_draft(application_id)
            snapshot = draft["snapshot"]
            if (snapshot.workflow.nodes or snapshot.workflow.edges or snapshot.tests) and not body.replace_existing:
                raise ValueError(
                    "draft already contains workflow content; set replace_existing=true to replace it atomically"
                )
            result = await services.applications.apply_operations_atomically(
                application_id,
                expected_revision=body.expected_revision,
                expected_content_hash=body.expected_content_hash,
                operations=[
                    {
                        "op": "set_capability_build_contract",
                        "data": {
                            "contract": scenario.capability_build_contract.model_dump(mode="json")
                        },
                    },
                    {
                        "op": "replace_workflow",
                        "data": {"workflow": scenario.workflow.model_dump(mode="json")},
                    },
                    {
                        "op": "replace_tests",
                        "data": {
                            "tests": [
                                test.model_dump(mode="json")
                                for test in scenario.acceptance_cases
                            ]
                        },
                    },
                ],
                idempotency_key=body.idempotency_key,
                change_context_operation="scenario_apply",
            )
            validation = await services.applications.validate_draft(application_id)
            return {
                **result,
                "scenario": scenario.summary(),
                "validation": validation,
            }
        except RevisionConflict as error:
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/applications/{application_id}/draft", dependencies=[Depends(require_token)])
    async def mutate_application_draft(
        application_id: str, body: DraftOperation
    ) -> dict[str, Any]:
        try:
            return await services.applications.apply_operation(application_id, body)
        except RevisionConflict as error:
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/draft/preview-patch",
        dependencies=[Depends(require_token)],
    )
    async def preview_application_draft_patch(
        application_id: str, body: DraftPatchPreviewRequest
    ) -> dict[str, Any]:
        task_id = str(uuid4())
        await services.harness.start_task(
            task_id,
            kind="draft_patch_preview",
            owner_id=application_id,
            resource_id=application_id,
            metadata={"instruction": body.instruction[:200]},
        )
        try:
            draft = await services.workflow_store.get_draft(application_id)
            response = services.draft_patcher.preview(
                draft["snapshot"],
                int(draft["revision"]),
                body.instruction,
                body.reference_node_ids,
            )
            preview_source = "deterministic"
            if _workflow_edit_needs_model(response):
                preview_source = "model"
                try:
                    response = await _model_workflow_edit_preview(
                        services,
                        task_id=task_id,
                        snapshot=draft["snapshot"],
                        revision=int(draft["revision"]),
                        body=body,
                    )
                except Exception as error:
                    response = DraftPatchPreviewResponse(
                        supported=False,
                        intent="unsupported",
                        message="AI workflow edit preview could not produce a valid, reviewable change.",
                        warnings=[str(error)],
                        reference_node_ids=body.reference_node_ids,
                    )
            await services.harness.finish_task(
                task_id,
                status="succeeded" if response.supported else "failed",
                metadata={
                    "intent": response.intent,
                    "preview_source": preview_source,
                    "operation_count": len(response.operations),
                },
                error="" if response.supported else response.message,
            )
            return {"task_id": task_id, **response.model_dump(mode="json")}
        except KeyError as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/draft/validate",
        dependencies=[Depends(require_token)],
    )
    async def validate_application_draft(application_id: str) -> dict[str, Any]:
        try:
            return await services.applications.validate_draft(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/builds",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    async def create_build(application_id: str, body: BuildRequest) -> dict[str, Any]:
        try:
            await services.workflow_store.get_application(application_id)
            draft = await services.workflow_store.get_draft(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        build_id = str(uuid4())
        capability_contract = draft["snapshot"].capability_build_contract
        if capability_contract is not None:
            router_activation = capability_contract_routing(
                capability_contract,
                requested_planning_mode=body.planning_mode,
            )
            complexity_router = None
            capability_closure = evaluate_capability_contract(
                capability_contract
            ).model_dump(mode="json")
        else:
            router_activation = runtime_activation_for_build(
                body.requirement,
                default_mode=services.settings.complexity_router_default_mode,
                limited_default_enabled=services.settings.complexity_router_limited_default_enabled,
                min_confidence=services.settings.complexity_router_limited_default_min_confidence,
                requested_planning_mode=body.planning_mode,
            )
            complexity_router = router_activation
            capability_closure = None
        await services.workflow_store.create_build(
            build_id,
            application_id,
            body.requirement,
            body.auto_publish,
            body.max_turns,
            body.max_repair_cycles,
            body.max_elapsed_seconds,
            router_activation["effective_planning_mode"],
            complexity_router=complexity_router,
            runtime_builder_policy=router_activation["runtime_builder_policy"],
            capability_build_contract=capability_contract,
            capability_closure=capability_closure,
            capability_routing=router_activation if capability_contract is not None else None,
        )
        services.builder.start(build_id)
        return {
            "build_id": build_id,
            "application_id": application_id,
            "status": "queued",
            "max_elapsed_seconds": body.max_elapsed_seconds,
            "deadline": deadline_summary(body.max_elapsed_seconds),
            "complexity_router": router_activation,
            "routing_source": router_activation.get("routing_source", "legacy_complexity_router"),
            "capability_routing": (
                router_activation if capability_contract is not None else None
            ),
        }

    @app.get(
        "/api/v1/applications/{application_id}/builds",
        dependencies=[Depends(require_token)],
    )
    async def list_application_builds(application_id: str) -> list[dict[str, Any]]:
        builds = await services.workflow_store.list_builds(application_id)
        for build in builds:
            build["team_state"] = build["team_state"].model_dump(mode="json")
            annotate_build_deadline(build)
        return builds

    @app.get("/api/v1/builds/{build_id}", dependencies=[Depends(require_token)])
    async def get_build(build_id: str) -> dict[str, Any]:
        try:
            build = await services.workflow_store.get_build(build_id)
            build["team_state"] = build["team_state"].model_dump(mode="json")
            return annotate_build_deadline(build)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/builds/{build_id}/resume", dependencies=[Depends(require_token)])
    async def resume_build(build_id: str) -> dict[str, Any]:
        try:
            build = await services.workflow_store.get_build(build_id)
            if build["status"] not in {"needs_attention", "cancelled"}:
                raise HTTPException(409, f"build cannot resume from {build['status']}")
            await services.workflow_store.update_build(build_id, status="queued", error="")
            services.builder.start(build_id)
            return {"build_id": build_id, "status": "queued"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/builds/{build_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel_build(build_id: str) -> dict[str, Any]:
        try:
            services.builder.cancel(build_id)
            return {"build_id": build_id, "status": "cancelling"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/tests/run",
        dependencies=[Depends(require_token)],
    )
    async def run_application_tests(application_id: str) -> dict[str, Any]:
        try:
            return await services.workflow_runtime.run_test_suite(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/v1/evaluation/profiles", dependencies=[Depends(require_token)])
    async def list_evaluation_profiles() -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in services.evaluation_harness.profiles()
        ]

    @app.get("/api/v1/evaluation/environments", dependencies=[Depends(require_token)])
    async def list_evaluation_environments() -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in services.evaluation_harness.environments()
        ]

    @app.post(
        "/api/v1/applications/{application_id}/evaluation/plan",
        dependencies=[Depends(require_token)],
    )
    async def preview_evaluation_plan(
        application_id: str,
        body: EvaluationPlanRequest,
    ) -> dict[str, Any]:
        try:
            plan = await services.evaluation_harness.plan(application_id, body)
            return plan.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/evaluation/tests/apply",
        dependencies=[Depends(require_token)],
    )
    async def apply_evaluation_tests(
        application_id: str,
        body: EvaluationApplyRequest,
    ) -> dict[str, Any]:
        try:
            return await services.evaluation_harness.apply_generated_tests(
                application_id,
                body,
            )
        except RevisionConflict as error:
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/evaluation/runs",
        dependencies=[Depends(require_token)],
    )
    async def run_evaluation(
        application_id: str,
        body: EvaluationRunRequest,
    ) -> dict[str, Any]:
        try:
            record = await services.evaluation_harness.run(application_id, body)
            return record.model_dump(mode="json")
        except RevisionConflict as error:
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/applications/{application_id}/evaluation/runs",
        dependencies=[Depends(require_token)],
    )
    async def list_application_evaluation_runs(
        application_id: str,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        try:
            await services.workflow_store.get_application(application_id)
            records = await services.evaluation_harness.list_runs(
                application_id,
                limit=limit,
            )
            return [item.model_dump(mode="json") for item in records]
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/evaluation/runs/{run_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_evaluation_run(run_id: str) -> dict[str, Any]:
        try:
            record = await services.evaluation_harness.get_run(run_id)
            return record.model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/tests/repair-preview",
        dependencies=[Depends(require_token)],
    )
    async def preview_application_test_repair(
        application_id: str, body: AcceptanceRepairPreviewRequest
    ) -> dict[str, Any]:
        task_id = str(uuid4())
        await services.harness.start_task(
            task_id,
            kind="draft_patch_preview",
            owner_id=application_id,
            resource_id=application_id,
            metadata={
                "origin": "acceptance_repair",
                "test_id": body.test_id or "",
            },
        )
        try:
            draft = await services.workflow_store.get_draft(application_id)
            trace_excerpts: list[str] = []
            report = body.report or {}
            for item in report.get("tests", []) if isinstance(report.get("tests"), list) else []:
                if not isinstance(item, dict) or item.get("passed") is not False:
                    continue
                run_id = str(item.get("run_id") or "")
                if not run_id:
                    continue
                for event in (await services.storage.list_events(run_id))[-12:]:
                    details = {
                        key: event.data[key]
                        for key in ("node_id", "status", "error", "tool")
                        if key in event.data
                    }
                    trace_excerpts.append(
                        f"{event.type}: {json.dumps(details, ensure_ascii=False, default=str)}"
                    )
            response = services.acceptance_repairer.preview(
                draft["snapshot"],
                int(draft["revision"]),
                report,
                content_hash=draft["content_hash"],
                test_id=body.test_id,
                instruction=body.instruction,
                reference_node_ids=body.reference_node_ids,
                trace_excerpts=trace_excerpts,
            )
            try:
                workflow_edit = services.draft_patcher.preview(
                    draft["snapshot"],
                    int(draft["revision"]),
                    response.instruction,
                    response.reference_node_ids,
                )
                response.workflow_edit_preview = workflow_edit.model_dump(mode="json")
                response.preview_source = "acceptance_structural+whole_workflow_context"
                workflow_edit_intent = workflow_edit.intent
            except Exception as error:  # Preview failure must remain visible without mutating the draft.
                response.workflow_edit_preview = {
                    "supported": False,
                    "intent": "unsupported",
                    "message": f"whole-workflow edit preview failed: {error}",
                    "operations": [],
                    "warnings": [str(error)],
                    "reference_node_ids": response.reference_node_ids,
                }
                response.preview_source = "acceptance_structural+whole_workflow_preview_failed"
                response.warnings.append(f"Whole-workflow edit preview failed: {error}")
                workflow_edit_intent = "unsupported"
            await services.harness.finish_task(
                task_id,
                status="succeeded" if response.supported else "failed",
                metadata={
                    "origin": "acceptance_repair",
                    "test_id": response.repair_context.test_id,
                    "operation_count": len(response.operations),
                    "workflow_edit_intent": workflow_edit_intent,
                },
                error="" if response.supported else response.message,
            )
            return {"task_id": task_id, **response.model_dump(mode="json")}
        except KeyError as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/tests/repair-apply",
        dependencies=[Depends(require_token)],
    )
    async def apply_application_test_repair(
        application_id: str, body: AcceptanceRepairApplyRequest
    ) -> dict[str, Any]:
        try:
            result = await services.applications.apply_operations_atomically(
                application_id,
                expected_revision=body.expected_revision,
                expected_content_hash=body.expected_content_hash,
                operations=body.operations,
                idempotency_key=body.idempotency_key,
            )
            draft = await services.workflow_store.get_draft(application_id)
            return {
                **result,
                "evidence_state": draft["evidence"]["state"],
                "evidence": draft["evidence"],
            }
        except RevisionConflict as error:
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get(
        "/api/v1/applications/{application_id}/versions",
        dependencies=[Depends(require_token)],
    )
    async def list_application_versions(application_id: str) -> list[dict[str, Any]]:
        return await services.workflow_store.list_versions(application_id)

    @app.get(
        "/api/v1/applications/{application_id}/publication-decision",
        dependencies=[Depends(require_token)],
    )
    async def get_application_publication_decision(application_id: str) -> dict[str, Any]:
        try:
            return await services.workflow_store.publication_decision(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/versions",
        dependencies=[Depends(require_token)],
    )
    async def publish_application(
        application_id: str,
        body: PublishApplicationRequest | None = None,
    ) -> dict[str, Any]:
        try:
            return await services.workflow_store.publish(
                application_id,
                acknowledge_warnings=bool(body and body.acknowledge_warnings),
            )
        except PublishGateError as error:
            raise HTTPException(
                409,
                detail={"message": str(error), "publication_decision": error.decision},
            ) from error
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/versions/{version}/restore",
        dependencies=[Depends(require_token)],
    )
    async def restore_application_version(application_id: str, version: int) -> dict[str, Any]:
        try:
            return await services.workflow_store.restore_version(application_id, version)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/applications/{application_id}/runtime-definition",
        dependencies=[Depends(require_token)],
    )
    async def get_application_runtime_definition(application_id: str) -> dict[str, Any]:
        try:
            application = await services.workflow_store.get_application(application_id)
            active_version = application.get("active_version")
            if active_version is not None:
                published = await services.workflow_store.get_version(
                    application_id,
                    int(active_version),
                )
                return {
                    "application_id": application_id,
                    "source": "published",
                    "version": int(published["version"]),
                    "draft_revision": None,
                    "content_hash": published["content_hash"],
                    "snapshot": published["snapshot"].model_dump(mode="json"),
                }
            draft = await services.workflow_store.get_draft(application_id)
            return {
                "application_id": application_id,
                "source": "draft",
                "version": None,
                "draft_revision": int(draft["revision"]),
                "content_hash": draft["content_hash"],
                "snapshot": draft["snapshot"].model_dump(mode="json"),
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/applications/{application_id}/runs",
        dependencies=[Depends(require_token)],
    )
    async def list_workflow_runs(
        application_id: str,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        try:
            await services.workflow_store.get_application(application_id)
            runs = await services.workflow_store.list_runs(application_id, limit=limit)
            for run in runs:
                run["state"] = run["state"].model_dump(mode="json")
            return runs
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/runs",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    async def create_workflow_run(
        application_id: str, body: WorkflowRunRequest
    ) -> dict[str, Any]:
        try:
            return await services.workflow_runtime.create_run(application_id, body)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, RuntimeError) as error:
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/schedules/trigger",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    async def trigger_application_schedule(
        application_id: str, body: ManualScheduleTriggerRequest
    ) -> dict[str, Any]:
        try:
            return await services.scheduler.trigger_now(
                application_id,
                body.inputs,
                idempotency_key=body.idempotency_key,
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, DurableJobConflict) as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/runs/{run_id}", dependencies=[Depends(require_token)])
    async def get_workflow_run(run_id: str) -> dict[str, Any]:
        try:
            run = await services.workflow_store.get_run(run_id)
            run["state"] = run["state"].model_dump(mode="json")
            return run
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/runs/{run_id}/resume", dependencies=[Depends(require_token)])
    async def resume_workflow_run(run_id: str, body: ResumeRunRequest) -> dict[str, Any]:
        try:
            return await services.workflow_runtime.resume(run_id, body.values)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/v1/runs/{run_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel_workflow_run(run_id: str) -> dict[str, Any]:
        try:
            services.harness.enforce_cancellation_policy()
            services.workflow_runtime.cancel(run_id)
            return {"run_id": run_id, "status": "cancelling"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except PlatformHarnessViolation as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/v1/agent-generations", status_code=202, dependencies=[Depends(require_token)])
    async def generate_agent(body: GenerationRequest) -> dict[str, str]:
        generation_id = str(uuid4())
        if body.workspace_path:
            services.sandboxes.resolve_workspace(body.workspace_path)
        await services.storage.create_generation(generation_id, body.requirement, body.workspace_path)
        task = asyncio.create_task(services.factory.generate(generation_id, body))
        services.background_tasks.add(task)
        task.add_done_callback(services.background_tasks.discard)
        return {"generation_id": generation_id, "status": "queued"}

    @app.get("/v1/agent-generations/{generation_id}", dependencies=[Depends(require_token)])
    async def get_generation(generation_id: str) -> dict[str, Any]:
        try:
            return await services.storage.get_generation(generation_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/v1/agents", dependencies=[Depends(require_token)])
    async def list_agents() -> list[dict[str, Any]]:
        return await services.storage.list_agents()

    @app.get("/v1/agents/{agent_id}", dependencies=[Depends(require_token)])
    async def get_agent(agent_id: str, version: int | None = None) -> dict[str, Any]:
        try:
            spec, resolved_version, version_status = await services.storage.get_agent(agent_id, version)
            return {
                "version": resolved_version,
                "status": version_status,
                "spec": spec.model_dump(mode="json"),
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/v1/agents/{agent_id}/versions/{version}/publish", dependencies=[Depends(require_token)])
    async def publish_agent(agent_id: str, version: int) -> dict[str, Any]:
        try:
            await services.storage.publish_agent(agent_id, version)
            return {"agent_id": agent_id, "version": version, "status": "published"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/v1/sessions", status_code=201, dependencies=[Depends(require_token)])
    async def create_session(body: SessionCreateRequest) -> dict[str, Any]:
        try:
            spec, version, version_status = await services.storage.get_agent(
                body.agent_id, body.agent_version
            )
            if version_status != "published" and body.agent_version is None:
                raise HTTPException(409, "agent version is not published")
            services.sandboxes.resolve_workspace(body.workspace_path)
            session = await services.runtime.create_session(spec, version, body.workspace_path)
            return {"session_id": session.id, "status": "ready"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/v1/sessions/{session_id}", dependencies=[Depends(require_token)])
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            record = await services.storage.get_session(session_id)
            record["messages"] = [item.model_dump(mode="json") for item in record["messages"]]
            return record
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/v1/sessions/{session_id}/messages", status_code=202, dependencies=[Depends(require_token)])
    async def send_message(session_id: str, body: MessageRequest) -> dict[str, str]:
        try:
            turn_id = await services.runtime.start_turn(session_id, body.content)
            return {"session_id": session_id, "turn_id": turn_id, "status": "running"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error

    @app.post(
        "/v1/sessions/{session_id}/permissions/{request_id}",
        dependencies=[Depends(require_token)],
    )
    async def resolve_permission(
        session_id: str, request_id: str, body: PermissionDecision
    ) -> dict[str, str]:
        try:
            services.permissions.resolve(request_id, body, session_id)
            return {"request_id": request_id, "status": "resolved"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/v1/sessions/{session_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel(session_id: str) -> dict[str, str]:
        try:
            services.runtime.cancel(session_id)
            return {"session_id": session_id, "status": "cancelling"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    async def sse_stream(stream_id: str, after: int) -> AsyncIterator[str]:
        iterator = services.storage.subscribe(stream_id, after).__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=15)
                if not done:
                    yield ": keep-alive\n\n"
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.id}\nevent: {event.type}\ndata: {payload}\n\n"
                pending = asyncio.create_task(iterator.__anext__())
        finally:
            pending.cancel()

    @app.get("/v1/streams/{stream_id}/events", dependencies=[Depends(require_token)])
    async def events(stream_id: str, request: Request, after: int = 0) -> StreamingResponse:
        header = request.headers.get("last-event-id")
        if header and header.isdigit():
            after = max(after, int(header))
        return StreamingResponse(
            sse_stream(stream_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/builds/{build_id}/events", dependencies=[Depends(require_token)])
    async def build_events(build_id: str, request: Request, after: int = 0) -> StreamingResponse:
        header = request.headers.get("last-event-id")
        if header and header.isdigit():
            after = max(after, int(header))
        return StreamingResponse(
            sse_stream(build_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/runs/{run_id}/events", dependencies=[Depends(require_token)])
    async def run_events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
        header = request.headers.get("last-event-id")
        if header and header.isdigit():
            after = max(after, int(header))
        return StreamingResponse(
            sse_stream(run_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/streams/{stream_id}", dependencies=[Depends(require_token)])
    async def list_stream_events(stream_id: str, after: int = 0) -> list[dict[str, Any]]:
        events = await services.storage.list_events(stream_id, after)
        return [event.model_dump(mode="json") for event in events]

    # ── Auto meta-cognition hook: Builder → template extraction ──

    async def _auto_extract_from_build(build_id: str) -> None:
        """After a build publishes, try to extract a reusable template from it.
        Runs as a fire-and-forget background task — never blocks the build response.
        """
        try:
            build = await services.workflow_store.get_build(build_id)
            if build.get("status") not in ("published", "ready"):
                return

            from .extraction_gate import ExtractionGate
            from .merge_engine import MergeEngine
            from .template_models import ProvenanceSource
            from datetime import datetime, timezone

            # Use the real DecisionTracker from Builder — each draft_add_node,
            # draft_connect, template_expand, and draft_publish call was recorded.
            tracker = services.builder._trackers.pop(build_id, None)
            if tracker is None or len(tracker.roots) == 0:
                print(f"[auto-extract] Build {build_id[:8]}: no decision data")
                return

            requirement = build.get("requirement", "")
            draft = await services.workflow_store.get_draft(build["application_id"])
            node_types = [node.type for node in draft["snapshot"].workflow.nodes]

            gate = ExtractionGate(services.templates)
            should, reason = gate.should_propose(tracker.roots)
            if not should:
                print(f"[auto-extract] Build {build_id[:8]}: not proposed ({reason})")
                return

            wf = tracker.extract_workflow()
            if wf is None:
                print(f"[auto-extract] Build {build_id[:8]}: no extractable workflow")
                return
            engine = MergeEngine(services.templates)
            sim = engine.check_similarity(wf)

            if sim.should_merge and sim.target_template:
                source = ProvenanceSource(
                    source_type="session_extract",
                    identifier=build_id,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                merged = engine.merge(wf, sim.target_template, source)
                if merged:
                    print(f"[auto-extract] Build {build_id[:8]}: merged into {sim.target_template} v{merged.meta.version} conf={merged.meta.confidence}")
            elif all(not t.name.startswith("build-") for t in services.templates.list()):
                # Register as new template if no obvious match
                tname = f"build-extracted-{build_id[:8]}"
                services.templates.register(tname, wf, meta_overrides={
                    "title": f"Extracted: {requirement[:50]}",
                    "category": "task_management",
                    "tags": node_types[:5],
                    "author": "auto-extract",
                })
                print(f"[auto-extract] Build {build_id[:8]}: registered as {tname}")
        except Exception as exc:
            print(f"[auto-extract] Build {build_id[:8]} failed: {exc}")

    # Wire the meta-cognition hook: every completed build triggers extraction
    services.builder.on_build_complete = _auto_extract_from_build

    # ── PWA DingTalk App ─────────────────────────────────────

    _pwa_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "mobile_app"

    @app.get("/dingtalk.html", response_class=HTMLResponse)
    async def dingtalk_pwa() -> str:
        p = _pwa_dir / "index.html"
        return p.read_text(encoding="utf-8") if p.exists() else "PWA not found"

    @app.get("/dingtalk-icon-192.png")
    async def dingtalk_icon_192():
        from fastapi.responses import FileResponse
        p = _pwa_dir / "dingtalk-icon-192.png"
        return FileResponse(p, media_type="image/png") if p.exists() else HTMLResponse("", status_code=404)

    @app.get("/dingtalk-icon-512.png")
    async def dingtalk_icon_512():
        from fastapi.responses import FileResponse
        p = _pwa_dir / "dingtalk-icon-512.png"
        return FileResponse(p, media_type="image/png") if p.exists() else HTMLResponse("", status_code=404)

    @app.get("/manifest.json")
    async def dingtalk_manifest():
        from fastapi.responses import FileResponse
        p = _pwa_dir / "manifest.json"
        return FileResponse(p, media_type="application/json") if p.exists() else HTMLResponse("", status_code=404)

    @app.get("/sw.js")
    async def dingtalk_sw():
        from fastapi.responses import FileResponse
        p = _pwa_dir / "sw.js"
        return FileResponse(p, media_type="application/javascript") if p.exists() else HTMLResponse("", status_code=404)

    # ── Dashboard ────────────────────────────────────────────

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> str:
        p = _pwa_dir / "dashboard.html"
        return p.read_text(encoding="utf-8") if p.exists() else "<h1>Dashboard not found</h1>"

    # ── Debug ────────────────────────────────────────────────

    @app.get("/debug", response_class=HTMLResponse)
    async def debug_page() -> str:
        return DEBUG_HTML

    return app


app = create_app()


DEBUG_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Agent Platform Debug</title>
<style>body{font:14px system-ui;margin:2rem;max-width:1100px;background:#101215;color:#e8e8e8}
input,textarea,button{font:inherit;padding:.6rem;background:#1b1f24;color:#eee;border:1px solid #444;border-radius:6px}
textarea{width:100%;min-height:100px}button{cursor:pointer;margin:.3rem}pre{white-space:pre-wrap;background:#08090a;padding:1rem;max-height:480px;overflow:auto}.row{display:flex;gap:.6rem}.row input{flex:1}</style></head>
<body><h1>Agent Platform</h1><div class="row"><input id="token" type="password" placeholder="API token"><input id="workspace" value="demo" placeholder="workspace path"></div>
<h2>1. 根据需求生成</h2><textarea id="requirement">生成一个能够分析并修复 Python 项目测试失败的智能体。它必须先运行测试、定位根因、最小化修改并重新验证。</textarea><button onclick="generate()">生成并验证</button>
<h2>2. 运行已发布智能体</h2><div class="row"><input id="agent" placeholder="agent id"><button onclick="session()">创建会话</button><input id="session" placeholder="session id"></div>
<textarea id="message">检查项目，修复失败的测试，并运行测试确认。</textarea><button onclick="send()">发送任务</button><button onclick="approve()">批准最新权限</button>
<h2>事件</h2><pre id="events"></pre>
<script>
let pending=null; const out=document.querySelector('#events');
const token=()=>document.querySelector('#token').value; const headers=()=>({'Authorization':'Bearer '+token(),'Content-Type':'application/json'});
function log(x){out.textContent+=x+'\n';out.scrollTop=out.scrollHeight}
function watch(id){const es=new EventSource('/v1/streams/'+id+'/events?token='+encodeURIComponent(token()));es.onmessage=e=>log(e.data);['generation.started','generation.model.thinking.delta','generation.model.text.delta','generation.spec.created','generation.validation.started','generation.validation.completed','generation.published','generation.failed','model.thinking.delta','model.text.delta','tool.started','tool.completed','tool.failed','permission.requested','turn.completed','turn.failed'].forEach(t=>es.addEventListener(t,e=>{log(t+' '+e.data);const d=JSON.parse(e.data);if(t==='generation.spec.created')document.querySelector('#agent').value=d.agent_id;if(t==='permission.requested')pending=d.request_id}))}
async function generate(){const r=await fetch('/v1/agent-generations',{method:'POST',headers:headers(),body:JSON.stringify({requirement:document.querySelector('#requirement').value,workspace_path:document.querySelector('#workspace').value,auto_publish:true})});const d=await r.json();log(JSON.stringify(d));if(d.generation_id)watch(d.generation_id)}
async function session(){const r=await fetch('/v1/sessions',{method:'POST',headers:headers(),body:JSON.stringify({agent_id:document.querySelector('#agent').value,workspace_path:document.querySelector('#workspace').value})});const d=await r.json();log(JSON.stringify(d));if(d.session_id){document.querySelector('#session').value=d.session_id;watch(d.session_id)}}
async function send(){const id=document.querySelector('#session').value;const r=await fetch('/v1/sessions/'+id+'/messages',{method:'POST',headers:headers(),body:JSON.stringify({content:document.querySelector('#message').value})});log(await r.text())}
async function approve(){if(!pending)return;const id=document.querySelector('#session').value;const r=await fetch('/v1/sessions/'+id+'/permissions/'+pending,{method:'POST',headers:headers(),body:JSON.stringify({behavior:'allow'})});log(await r.text());pending=null}
</script></body></html>"""
