from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
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
from .blocks import BlockRegistry, build_block_registry
from . import acceptance_pm
from .build_transcript import BuildTranscriptStore, owner_record
from .builder import WorkflowBuilder
from .builder_registry import BuilderRegistry
from .mechanical_builder import MechanicalBuilder
from .capability_evidence import (
    ArtifactCategory,
    CapabilityEvidenceCreateRequest,
    VerificationStatus,
)
from .customer_runtime_projection import (
    auto_view_tabs,
    default_hidden_nodes,
    project_runtime_application,
    project_runtime_definition,
    project_runtime_events,
    project_runtime_run,
    project_view_definition,
    project_view_run,
    resolve_view_layout,
    synthesize_auto_view,
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
from .draft_patch_preview import (
    DraftPatchPreviewer,
    DraftPatchPreviewRequest,
    DraftPatchPreviewResponse,
    NaturalLanguageDraftEditRequest,
    NaturalLanguageDraftEditResponse,
    validate_selection_operations,
)
from .durable_jobs import DurableJobConflict, DurableJobStore
from .event_automation import (
    EventAutomationService,
    EventSubscriptionCreateRequest,
)
from .governed_memory import (
    GovernedMemoryPermission,
    GovernedMemorySource,
    GovernedMemorySurface,
    GovernedMemoryViolation,
    MemoryStatus,
    RetentionClass,
)
from .forecast_models import (
    EvaluateForecastModelRequest,
    FineTuneForecastModelRequest,
    ForecastInferenceRequest,
    ForecastModelConflict,
    ForecastModelService,
    ImportForecastModelRequest,
    PromoteForecastModelRequest,
    RollbackForecastDeploymentRequest,
    TrainForecastModelRequest,
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
from .knowledge_rag import (
    GroundedAnswerRequest,
    KnowledgeIndexConflict,
    KnowledgeIndexCreateRequest,
    KnowledgeIndexService,
    KnowledgeRetrieveRequest,
    KnowledgeSyncRequest,
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
from .tabular_models import (
    EvaluateTabularModelRequest,
    FineTuneTabularModelRequest,
    ImportTabularModelRequest,
    PromoteTabularModelRequest,
    RollbackTabularDeploymentRequest,
    TabularDriftRequest,
    TabularInferenceRequest,
    TabularModelConflict,
    TabularModelService,
    TrainTabularModelRequest,
)
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
    WorkflowTestSuiteRequest,
)
from .workflow_runtime import WorkflowRuntime
from .workflow_storage import PublishGateError, RevisionConflict, WorkflowStorage
from .web_collection import ControlledWebCollector

logger = logging.getLogger(__name__)

# 已经走完的构建：取消它们没有意义，也不该把状态改写掉
TERMINAL_BUILD_STATUSES = frozenset({"published", "cancelled", "failed"})



RUNTIME_ROUTE_CHECKS: dict[str, tuple[str, str]] = {
    "health": ("GET", "/health"),
    "applications_list": ("GET", "/api/v1/applications"),
    "applications_create": ("POST", "/api/v1/applications"),
    "application_detail": ("GET", "/api/v1/applications/{application_id}"),
    "draft_detail": ("GET", "/api/v1/applications/{application_id}/draft"),
    "smoke_cleanup": ("POST", "/api/v1/applications/{application_id}/smoke-cleanup"),
    "requirement_intake": ("POST", "/api/v1/requirements/complete"),
    "scenario_catalog": ("GET", "/api/v1/scenarios"),
    "scenario_apply": (
        "POST",
        "/api/v1/applications/{application_id}/scenarios/{scenario_id}/apply",
    ),
    "capability_modules": ("GET", "/api/v1/capability-modules"),
    "capability_evidence": ("GET", "/api/v1/capability-evidence"),
    "durable_jobs": ("GET", "/api/v1/applications/{application_id}/durable-jobs"),
    "connector_manifests": ("GET", "/api/v1/connectors/manifests"),
    "connector_generations": ("GET", "/api/v1/connectors/generations"),
    "connector_test_run": ("POST", "/api/v1/applications/{application_id}/connector-test-runs"),
    "embedding_invoke": ("POST", "/api/v1/embedding/invoke"),
    "knowledge_indexes": ("GET", "/api/v1/knowledge-indexes"),
    "knowledge_index_retrieve": (
        "POST",
        "/api/v1/knowledge-indexes/{index_name}/retrieve",
    ),
    "event_subscriptions": ("GET", "/api/v1/event-subscriptions"),
    "event_timers": ("GET", "/api/v1/event-timers"),
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


def _resolve_reachable_git_commit(
    repo_root: Path | None,
    commit_sha: str,
) -> str | None:
    """Return a full commit OID only when it is retained by the current history."""

    if repo_root is None:
        return None
    try:
        resolved = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                "--verify",
                f"{commit_sha}^{{commit}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if resolved.returncode != 0:
            return None
        full_commit = resolved.stdout.strip()
        if commit_sha != full_commit or len(full_commit) not in {40, 64}:
            return None
        reachable = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "merge-base",
                "--is-ancestor",
                full_commit,
                "HEAD",
            ],
            check=False,
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return full_commit if reachable.returncode == 0 else None








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
        "untracked_present": _git_has_output(
            repo_root, "ls-files", "--others", "--exclude-standard"
        ),
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
    builder: WorkflowBuilder
    builders: BuilderRegistry
    scheduler: WorkflowScheduler
    templates: TemplateStore
    scenarios: ScenarioCatalog
    build_transcripts: BuildTranscriptStore
    draft_patcher: DraftPatchPreviewer
    governed_memory: GovernedMemorySurface
    tabular_models: TabularModelService
    forecast_models: ForecastModelService
    knowledge_indexes: KnowledgeIndexService
    event_automation: EventAutomationService
    worker_supervisor: Any | None
    worker_process_manager: Any | None
    background_tasks: set[asyncio.Task[Any]]


class ResumeBuildRequest(BaseModel):
    message: str = Field(default="", max_length=8_000)


class OwnerMessageRequest(BaseModel):
    code: str
    message: str = Field(min_length=1, max_length=8_000)


class OwnerRepairRequest(BaseModel):
    code: str


class BuildMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8_000)


class AssistantChatRequest(BaseModel):
    """本地客户端"一般任务"插件的远端对话入口（模型与计费留在服务端）。"""

    # 值不能限成 str：助手轮次要带 actions（上一轮对谁做了什么），
    # 那是列表——没有它，下一轮的「它」「那个」就无从解析
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=40)
    system: str = Field(default="", max_length=4_000)


class AcceptanceInterviewRequest(BaseModel):
    examples: str = Field(min_length=5, max_length=20_000)


def _summarize_run_ledger(
    events: list[Any], final_outputs: dict[str, Any] | None
) -> tuple[str, list[str]]:
    """机械生成返修证据摘要 + 可疑信号（零模型消耗）。"""

    lines: list[str] = []
    suspicions: list[str] = []
    empty_upstream: list[str] = []
    for event in events:
        data = getattr(event, "data", None) or {}
        kind = getattr(event, "type", "")
        node_id = str(data.get("node_id") or "")
        if kind == "node.completed":
            outputs = data.get("outputs") or {}
            emptiness: list[str] = []
            def _scan(value: Any, path: str) -> None:
                if isinstance(value, list) and not value:
                    emptiness.append(f"{path}=[]")
                elif isinstance(value, dict):
                    for key, item in list(value.items())[:8]:
                        _scan(item, f"{path}.{key}" if path else key)
            _scan(outputs, "")
            note = f"；空集合：{'、'.join(emptiness[:3])}" if emptiness else ""
            lines.append(f"- {node_id} 完成{note}")
            if emptiness:
                empty_upstream.append(node_id)
        elif kind == "node.failed":
            lines.append(f"- {node_id} 失败：{str(data.get('error'))[:160]}")
        elif kind == "workflow.failed":
            lines.append(f"- 整体失败：{str(data.get('error'))[:160]}")
    if empty_upstream and final_outputs:
        filled = [
            key for key, value in final_outputs.items()
            if (isinstance(value, list) and value)
            or (isinstance(value, str) and len(value) > 40)
            or (isinstance(value, dict) and value)
        ]
        if filled:
            suspicions.append(
                "上游节点（" + "、".join(empty_upstream[:3]) + "）返回了空集合，"
                "但最终输出仍然填满（" + "、".join(filled[:4]) + "）——"
                "疑似用格式示例或编造内容充数"
            )
    return "\n".join(lines[:24]), suspicions


class RunRepairRequest(BaseModel):
    note: str = Field(default="", max_length=4_000)


class ViewUpsertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    layout: str = Field(default="auto", pattern=r"^(auto|form|chat)$")
    hidden_nodes: list[str] = Field(default_factory=list, max_length=200)


class UseTableRequest(BaseModel):
    filename: str = Field(default="paste.txt", max_length=200)
    text: str | None = Field(default=None, max_length=2_000_000)
    content_base64: str | None = Field(default=None, max_length=12_000_000)


class OwnerExplainRequest(BaseModel):
    question: str = Field(default="", max_length=2_000)


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


class EventSubscriptionStateRequest(BaseModel):
    enabled: bool


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
    assignment_id: str = ""
    session_id: str = ""
    application_id: str = ""
    run_id: str = ""
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
    selected_options: list[RequirementIntakeSelectedOption] = Field(
        default_factory=list, max_length=8
    )
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
    return (
        "You are Lilies' workflow requirement intake agent. "
        "Your job is similar to Claude Code plan-mode questioning, but for editable workflow generation, not code execution. "
        "Analyze the user's workflow request and decide whether there is enough information to build a useful editable workflow. "
        "If crucial information is missing, return status needs_input and ask option-based targeted questions. "
        "Every needs_input question must include 2 to 5 concrete selectable options; never return more than five options for one question. "
        "Use choice_type single for mutually exclusive decisions and multi when the customer may select several capabilities. "
        "Recommend the lowest-friction option that satisfies the original request; never recommend extra review gates, permissions, tools, integrations, or orchestration merely because they sound safer or more complete. "
        "The first option should normally be recommended and should set recommended true, but semantic fit with the original request is more important than option order. "
        "Use custom_allowed only as an optional Other/custom escape hatch; the default path must be selecting options. "
        "Questions must be workflow-building decisions: target user, what the workflow does, what it reads and returns, permission boundary, and how the owner will know it worked. "
        "Do not fill missing fields with generic placeholders. Do not invent a target customer, runtime tools, permissions, or acceptance criteria. "
        "Do not invent human review or approval. Preserve it only when the original request or a selected answer explicitly requires it. "
        "Treat prior_answers as cumulative authoritative decisions from every completed clarification round. Never re-ask an answered question. "
        "Once target user, core behavior, inputs and outputs, permission boundary, and acceptance signal are covered, return ready; optional refinements must not create an endless clarification loop. "
        "When the request is ready, return status ready and a completed_requirement: a clear, self-contained restatement of the workflow to build, in the user's own domain language. "
        "Always answer in JSON only, no markdown fences. "
        f"Use {language} for user-visible text. "
        "JSON schema: {"
        '"status":"needs_input|ready",'
        '"confidence":0.0,'
        '"reasoning_summary":"short rationale",'
        '"detected_goal":"what the user is trying to build",'
        '"missing":["specific missing facts"],'
        '"questions":[{"id":"stable_snake_case","label":"short label","question":"decision question","why":"why it matters","choice_type":"single|multi","options":[{"id":"stable_option_id","label":"option label","description":"what this means","impact":"how it changes the workflow","recommended":true}],"custom_allowed":true,"custom_placeholder":"optional custom answer placeholder"}],'
        '"completed_requirement":"string or null",'
        '"workflow_intent":{"target_user":"","runtime_input":"","runtime_output":"","core_steps":[""],"permissions":[""],"acceptance_cases":[""]}'
        "}. "
        "For a vague request such as \'make a workflow like Codex\', do not complete the requirement directly. "
        "Return option questions covering capability scope, target user, runtime interface, permission/tool boundary, and acceptance strategy."
    )


def _requirement_intake_prompt(body: RequirementIntakeRequest) -> str:
    answers = [
        {
            "question_id": answer.question_id,
            "question": answer.question,
            "choice_type": answer.choice_type,
            "selected_option_ids": answer.selected_option_ids,
            "selected_options": [
                option.model_dump(mode="json") for option in answer.selected_options
            ],
            "custom_answer": answer.custom_answer,
            "legacy_answer": answer.answer or "",
        }
        for answer in body.answers
    ]
    answered_axes = sorted(
        {
            effect.axis
            for answer in body.answers
            for option in answer.selected_options
            for effect in option.effects
        }
    )
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
            ),
        },
        ensure_ascii=False,
    )


def _validate_requirement_intake_response(result: RequirementIntakeResponse) -> None:
    """Keep option questions usable. A ready answer is never rejected: the owner
    decides whether the completed requirement is good enough to build from."""

    if result.status != "needs_input":
        return
    if not result.questions:
        raise ValueError("needs_input response must include option-based targeted questions")
    for question in result.questions:
        option_count = len(question.options)
        if option_count < 2 or option_count > 5:
            raise ValueError("needs_input questions must include 2 to 5 selectable options")
        option_ids = [option.id for option in question.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("needs_input question options must have unique ids")
        if not any(option.recommended for option in question.options):
            question.options[0].recommended = True




















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






def _capability_text(capability: dict[str, Any]) -> str:
    return "\n".join(
        str(capability.get(key) or "")
        for key in ("id", "title", "description", "availability_reason")
    )




def _references_any(text: str, identifiers: set[str]) -> bool:
    lowered = (text or "").casefold()
    return any(identifier.casefold() in lowered for identifier in identifiers)








def _normalize_requirement_intake_payload(
    payload: dict[str, Any],
    body: RequirementIntakeRequest,
) -> dict[str, Any]:
    if not isinstance(payload.get("workflow_intent"), dict):
        payload["workflow_intent"] = {}
    if payload.get("status") == "needs_input":
        payload["completed_requirement"] = None
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
        return payload

    if payload.get("status") == "ready" and not str(payload.get("completed_requirement") or "").strip():
        payload["completed_requirement"] = body.requirement
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
            messages=[
                ChatMessage(
                    role="user",
                    content=[ContentBlock(type="text", text=_requirement_intake_prompt(body))],
                )
            ],
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
    body: DraftPatchPreviewRequest | NaturalLanguageDraftEditRequest,
) -> DraftPatchPreviewResponse:
    model = services.settings.deepseek_runtime_model
    block_catalog = [
        {
            "type": block.type,
            "version": block.version,
            "title": block.title,
            "description": block.description,
            "config_schema": block.config_schema,
            "input_ports": [port.model_dump(mode="json") for port in block.input_ports],
            "output_ports": [port.model_dump(mode="json") for port in block.output_ports],
        }
        for block in services.blocks.list()
    ]
    system = (
        "You are Lilies' whole-workflow editing planner. Translate one natural-language "
        "instruction into precise, reviewable draft operations over the supplied workflow. "
        "reference_node_ids are context only. node_ids and edge_ids are the user's boxed selection "
        "and therefore the primary edit target. Preserve everything the user did not ask to change. "
        "When a selection exists, modify unselected structure only when required to keep graph "
        "connections valid; explain every such boundary change in warnings. Never store the "
        "instruction itself as the workflow requirement "
        "or description as a substitute for a structural edit. Resolve human node titles to the "
        "existing node ids. Use only the listed block types and only these operations: add_node, "
        "update_node, remove_node, add_edge, remove_edge, set_metadata, upsert_agent, add_test, "
        "remove_test. For update_node, use data.node_id, "
        "data.changes, and data.merge_config. data.changes may contain only NodeSpec fields: "
        "type, block_version, title, description, config, position, retry, error_strategy, "
        "contract, degraded_value, or fallback_value. Put block-specific settings such as "
        "system, prompt, model, and structured_output inside data.changes.config; with "
        "merge_config=true that config object is deep-merged into the existing node config. "
        "For set_metadata, include only fields explicitly "
        "requested. If the instruction is ambiguous, return supported=false with one concise "
        "clarification question and no operations. Return JSON only with keys supported, intent, "
        "message, operations, warnings."
    )
    prompt = json.dumps(
        {
            "instruction": body.instruction,
            "reference_node_ids": getattr(body, "reference_node_ids", []),
            "node_ids": body.node_ids,
            "edge_ids": body.edge_ids,
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
            reference_node_ids=getattr(body, "reference_node_ids", []),
            node_ids=body.node_ids,
            edge_ids=body.edge_ids,
        )
    raw_operations = payload.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ValueError("AI workflow edit preview returned no draft operations")
    if len(raw_operations) > 40:
        raise ValueError("AI workflow edit preview exceeded the 40-operation review boundary")

    parsed_operations: list[DraftOperation] = []
    explicit_requirement_change = bool(
        re.search(r"requirement|goal|需求|目标", body.instruction, re.I)
    )
    explicit_removal = bool(re.search(r"remove|delete|删除|移除|去掉", body.instruction, re.I))
    for raw_operation in raw_operations:
        if not isinstance(raw_operation, dict):
            raise ValueError("AI workflow edit operation must be an object")
        normalized = dict(raw_operation)
        normalized["expected_revision"] = revision
        normalized["idempotency_key"] = str(uuid4())
        operation = DraftOperation.model_validate(normalized)
        if operation.op == "remove_node" and not explicit_removal:
            raise ValueError(
                "AI workflow edit attempted node removal without an explicit removal request"
            )
        if (
            operation.op == "set_metadata"
            and "requirement" in operation.data
            and not explicit_requirement_change
        ):
            raise ValueError(
                "AI workflow edit attempted to overwrite the requirement without a requirement-change request"
            )
        parsed_operations.append(operation)
    services.applications.validate_preview_operations(snapshot, parsed_operations)
    selection_warnings = validate_selection_operations(
        snapshot,
        [
            operation.model_dump(mode="json", exclude={"idempotency_key"})
            for operation in parsed_operations
        ],
        node_ids=body.node_ids,
        edge_ids=body.edge_ids,
    )
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
    warnings.extend(selection_warnings)
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
        reference_node_ids=getattr(body, "reference_node_ids", []),
        node_ids=body.node_ids,
        edge_ids=body.edge_ids,
    )


def _workflow_edit_instruction_digest(instruction: str) -> str:
    return "sha256:" + hashlib.sha256(instruction.strip().encode()).hexdigest()


def _workflow_edit_plan_digest(
    *,
    application_id: str,
    instruction: str,
    revision: int,
    content_hash: str,
    node_ids: list[str],
    edge_ids: list[str],
    operations: list[dict[str, Any]],
) -> str:
    encoded = json.dumps(
        {
            "application_id": application_id,
            "instruction_digest": _workflow_edit_instruction_digest(instruction),
            "revision": revision,
            "content_hash": content_hash,
            "node_ids": node_ids,
            "edge_ids": edge_ids,
            "operations": operations,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _workflow_edit_draft_payload(draft: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: value for key, value in draft.items() if key not in {"snapshot"}},
        "snapshot": draft["snapshot"].model_dump(mode="json"),
    }


def _validate_workflow_edit_response(
    services: Services,
    *,
    snapshot: ApplicationSnapshot,
    revision: int,
    response: DraftPatchPreviewResponse,
) -> DraftPatchPreviewResponse:
    if not response.supported:
        return response
    parsed: list[DraftOperation] = []
    for raw in response.operations:
        normalized = dict(raw)
        normalized["expected_revision"] = revision
        normalized["idempotency_key"] = str(uuid4())
        parsed.append(DraftOperation.model_validate(normalized))
    if not parsed:
        raise ValueError("workflow edit preview returned no draft operations")
    services.applications.validate_preview_operations(snapshot, parsed)
    selection_warnings = validate_selection_operations(
        snapshot,
        [operation.model_dump(mode="json", exclude={"idempotency_key"}) for operation in parsed],
        node_ids=response.node_ids,
        edge_ids=response.edge_ids,
    )
    response.operations = [
        operation.model_dump(mode="json", exclude={"idempotency_key"}) for operation in parsed
    ]
    response.warnings = list(dict.fromkeys([*response.warnings, *selection_warnings]))
    return response


async def _plan_workflow_edit(
    services: Services,
    *,
    task_id: str,
    draft: dict[str, Any],
    body: DraftPatchPreviewRequest | NaturalLanguageDraftEditRequest,
) -> tuple[DraftPatchPreviewResponse, Literal["deterministic", "model"]]:
    snapshot = draft["snapshot"]
    revision = int(draft["revision"])
    response = services.draft_patcher.preview(
        snapshot,
        revision,
        body.instruction,
        getattr(body, "reference_node_ids", []),
        body.node_ids,
        body.edge_ids,
    )
    preview_source: Literal["deterministic", "model"] = "deterministic"
    deterministic_error: Exception | None = None
    if not _workflow_edit_needs_model(response):
        try:
            return (
                _validate_workflow_edit_response(
                    services,
                    snapshot=snapshot,
                    revision=revision,
                    response=response,
                ),
                preview_source,
            )
        except Exception as error:
            deterministic_error = error

    preview_source = "model"
    try:
        response = await _model_workflow_edit_preview(
            services,
            task_id=task_id,
            snapshot=snapshot,
            revision=revision,
            body=body,
        )
        response = _validate_workflow_edit_response(
            services,
            snapshot=snapshot,
            revision=revision,
            response=response,
        )
    except Exception as error:
        warnings = [str(error)]
        if deterministic_error is not None:
            warnings.insert(
                0,
                f"Deterministic preview was outside the selected edit boundary: {deterministic_error}",
            )
        response = DraftPatchPreviewResponse(
            supported=False,
            intent="unsupported",
            message=(
                "The workflow edit could not produce a safe, reviewable change. "
                "Refine the instruction or selection and try again."
            ),
            warnings=warnings,
            reference_node_ids=getattr(body, "reference_node_ids", []),
            node_ids=body.node_ids,
            edge_ids=body.edge_ids,
        )
    return response, preview_source


def _workflow_edit_stored_plan(
    *,
    application_id: str,
    body: NaturalLanguageDraftEditRequest,
    response: DraftPatchPreviewResponse,
    preview_source: Literal["deterministic", "model"],
    preview_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "application_id": application_id,
        "instruction_digest": _workflow_edit_instruction_digest(body.instruction),
        "expected_revision": body.expected_revision,
        "expected_content_hash": body.expected_content_hash,
        "node_ids": response.node_ids,
        "edge_ids": response.edge_ids,
        "supported": response.supported,
        "intent": response.intent,
        "message": response.message,
        "operations": response.operations,
        "warnings": response.warnings,
        "preview_source": preview_source,
        "preview_digest": preview_digest,
    }


async def _load_workflow_edit_stored_plan(
    services: Services,
    *,
    application_id: str,
    body: NaturalLanguageDraftEditRequest,
) -> tuple[
    DraftPatchPreviewResponse,
    str,
    Literal["deterministic", "model"],
]:
    if not body.preview_task_id:
        raise ValueError("preview_task_id is required to apply a reviewed preview")
    record = await services.harness.get_task(body.preview_task_id)
    plan = record.metadata.get("natural_language_edit_plan")
    if (
        record.kind != "draft_patch_preview"
        or record.owner_id != application_id
        or record.resource_id != application_id
        or not isinstance(plan, dict)
        or plan.get("schema_version") != 1
        or plan.get("application_id") != application_id
    ):
        raise ValueError("preview task is not a natural-language edit plan for this application")
    expected = {
        "instruction_digest": _workflow_edit_instruction_digest(body.instruction),
        "expected_revision": body.expected_revision,
        "expected_content_hash": body.expected_content_hash,
        "node_ids": body.node_ids,
        "edge_ids": body.edge_ids,
    }
    for field, value in expected.items():
        if plan.get(field) != value:
            raise RevisionConflict(f"reviewed workflow edit {field} no longer matches")
    preview_digest = str(plan.get("preview_digest") or "")
    if body.expected_preview_digest and body.expected_preview_digest != preview_digest:
        raise RevisionConflict("reviewed workflow edit preview digest no longer matches")
    response = DraftPatchPreviewResponse(
        supported=bool(plan.get("supported")),
        intent=str(plan.get("intent") or "unsupported"),
        message=str(plan.get("message") or ""),
        operations=list(plan.get("operations") or []),
        warnings=[str(item) for item in plan.get("warnings") or []],
        node_ids=[str(item) for item in plan.get("node_ids") or []],
        edge_ids=[str(item) for item in plan.get("edge_ids") or []],
    )
    recomputed = _workflow_edit_plan_digest(
        application_id=application_id,
        instruction=body.instruction,
        revision=body.expected_revision,
        content_hash=body.expected_content_hash,
        node_ids=response.node_ids,
        edge_ids=response.edge_ids,
        operations=response.operations,
    )
    if preview_digest != recomputed:
        raise RevisionConflict("stored workflow edit preview digest is invalid")
    stored_source = str(plan.get("preview_source") or "")
    if stored_source not in {"deterministic", "model"}:
        raise RevisionConflict("stored workflow edit preview source is invalid")
    return response, preview_digest, stored_source


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
    tabular_models = TabularModelService(storage.db_path)
    forecast_models = ForecastModelService(storage.db_path)
    knowledge_indexes = KnowledgeIndexService(storage.db_path)
    event_automation = EventAutomationService(
        storage.db_path,
        harness=harness,
    )
    services: Services


    web_collector = ControlledWebCollector(jobs=durable_jobs, harness=harness)
    connectors = ConnectorService(
        storage=storage,
        harness=harness,
        pre_dispatch_attestations=settings.connector_pre_dispatch_attestations,
        environment_epoch=settings.connector_environment_epoch,
    )
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
        tabular_models=tabular_models,
        forecast_models=forecast_models,
        knowledge_indexes=knowledge_indexes,
        event_automation=event_automation,
    )

    async def run_event_workflow(
        application_id: str,
        inputs: dict[str, Any],
        workspace_path: str,
    ) -> dict[str, Any]:
        return await workflow_runtime.create_run(
            application_id,
            WorkflowRunRequest(
                inputs=inputs,
                use_draft=False,
                workspace_path=workspace_path,
            ),
            origin="event_automation",
        )

    event_automation.bind_run_callback(run_event_workflow)
    templates = TemplateStore(
        settings.data_dir / "module_registry",
        evidence_root=_repo_root() or Path.cwd(),
        workflow_validator=blocks.validate_workflow,
    )
    draft_patcher = DraftPatchPreviewer()
    scenarios = ScenarioCatalog(blocks, connectors=connectors)
    templates_dir = settings.templates_dir
    if templates_dir and templates_dir.is_dir():
        loaded = templates.load_builtins(templates_dir)
        print(f"[api] Loaded {loaded} built-in templates from {templates_dir}")
    reference_module = ensure_codex_reference_module(templates, blocks)
    print(
        f"[api] Reference module {reference_module.module_ref} "
        f"status={reference_module.state.status}"
    )









    formal_assignment_runtime = None
    formal_developer_worker_broker = None
    formal_run_archiver = None
    formal_independent_verification = None
    formal_source_provenance = None






    async def resolve_developer_promotion(
        channel: Any,
        response: Any,
    ) -> bool:
        coordinator = formal_source_provenance
        if coordinator is None or response.commit_sha is None:
            return False
        return await asyncio.to_thread(
            coordinator.promoted_response_is_effective,
            assignment_id=channel.assignment_id,
            channel_id=channel.channel_id,
            report_id=response.report_id,
            report_revision=response.report_revision,
            response_id=response.response_id,
            commit_sha=response.commit_sha,
        )




    async def archive_formal_terminal(assignment_id: Any) -> Any:
        archiver = formal_run_archiver
        if archiver is None:
            return None
        return await archiver.archive_terminal_assignment(assignment_id)






    build_transcripts = BuildTranscriptStore(settings.data_dir / "build_transcripts")
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
        transcripts=build_transcripts,
        sandboxes=sandboxes,
        tabular_models=tabular_models,
    )
    # 多套 builder 并存的注册表：经典单模型实现注册为 classic；
    # 小模型集群等新实现在此并排注册后，即可按构建选择做对照实验。
    builders = BuilderRegistry()
    builders.register("classic", builder)
    # mechanical：状态机协调 + 小模型提案（形态 B）。与 classic 共享全部服务与
    # 执法逻辑，只有大脑（_agent_loop）不同；默认模型走本地小模型端点。
    builders.register("mechanical", MechanicalBuilder(
        storage=storage,
        workflow_store=workflow_store,
        applications=applications,
        blocks=blocks,
        runtime=workflow_runtime,
        provider=provider,
        agent_runtime=runtime,
        generator_model=settings.small_builder_model,
        core_tools=tools,
        harness=harness,
        template_store=templates,
        transcripts=build_transcripts,
        sandboxes=sandboxes,
        tabular_models=tabular_models,
    ))
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
        builder=builder,
        builders=builders,
        scheduler=scheduler,
        templates=templates,
        scenarios=scenarios,
        build_transcripts=build_transcripts,
        draft_patcher=draft_patcher,
        governed_memory=governed_memory,
        tabular_models=tabular_models,
        forecast_models=forecast_models,
        knowledge_indexes=knowledge_indexes,
        event_automation=event_automation,
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
        stop_timeout_seconds=max(
            settings.platform_harness_worker_process_stop_timeout_seconds, 0.001
        ),
    )
    return services


def create_app(settings: Settings | None = None, provider: ModelProvider | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.prepare()
    services = build_services(settings, provider)

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        await services.storage.initialize()
        await services.tabular_models.initialize()
        await services.forecast_models.initialize()
        await services.knowledge_indexes.initialize()
        await services.event_automation.initialize()
        await services.workflow_store.initialize()
        await services.durable_jobs.initialize()
        await services.connectors.initialize()
        await services.openapi_connectors.initialize()
        await services.workflow_store.fail_interrupted_runs()
        await services.workflow_store.fail_interrupted_builds()
        archived = await services.storage.archive_events_before(
            keep_days=settings.event_archive_keep_days
        )
        if archived["removed"]:
            print(
                f"[storage] 事件归档：DB 移除 {archived['removed']} 行"
                f"（冷文件为权威全量），剩余 {archived['remaining']} 行"
            )
        compressed = await services.storage.compress_cold_event_files(
            older_than_days=settings.event_compress_after_days
        )
        if compressed["compressed"]:
            print(
                f"[storage] 冷文件压缩：{compressed['compressed']} 个，"
                f"省 {compressed['bytes_saved'] / 1e6:.1f} MB"
            )
        # 运行产物过期清除（产物可由重跑再生，客户已通过使用页下载）
        import shutil as _shutil
        import time as _time

        artifacts_root = settings.workspace_root / ".workflow-run-artifacts"
        if artifacts_root.is_dir():
            cutoff = _time.time() - settings.run_artifacts_keep_days * 86_400
            purged = 0
            for run_dir in artifacts_root.iterdir():
                try:
                    if run_dir.is_dir() and run_dir.stat().st_mtime < cutoff:
                        _shutil.rmtree(run_dir, ignore_errors=True)
                        purged += 1
                except OSError:
                    continue
            if purged:
                print(f"[storage] 运行产物清理：{purged} 个过期运行目录")
        services.scheduler.start()
        await services.event_automation.start()
        local_lilies_recovery_task: asyncio.Task[Any] | None = None
        lifespan_ready = asyncio.Event()
        adaptive_refresh_task: asyncio.Task[Any] | None = None
        lifespan_ready.set()
        yield
        if (
            services.worker_process_manager is not None
            and services.worker_process_manager.is_running
        ):
            services.worker_process_manager.stop()
        if services.worker_supervisor is not None and services.worker_supervisor.loop_running:
            await services.worker_supervisor.stop()
        await services.event_automation.stop()
        await services.scheduler.stop()
        for task in services.background_tasks:
            task.cancel()
        if adaptive_refresh_task is not None:
            services.background_tasks.discard(adaptive_refresh_task)
        if local_lilies_recovery_task is not None:
            local_lilies_recovery_task.cancel()
            await asyncio.gather(local_lilies_recovery_task, return_exceptions=True)
            services.background_tasks.discard(local_lilies_recovery_task)
        await services.sandboxes.close()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.services = services
    bearer = HTTPBearer(auto_error=False)

    async def require_token(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        supplied = credentials.credentials if credentials else request.query_params.get("token")
        if not supplied:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API token")
        if supplied == settings.api_token:
            request.state.user = {"id": "root", "name": "管理员", "role": "admin"}
            return
        import hashlib
        user = await services.storage.user_by_token_hash(
            hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        )
        if not user or user.get("status") != "active":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API token")
        request.state.user = user

    def _current_user(request: Request) -> dict[str, Any]:
        return getattr(request.state, "user", {"id": "root", "name": "管理员", "role": "admin"})

    def _require_admin(request: Request) -> dict[str, Any]:
        user = _current_user(request)
        if user.get("role") != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
        return user

    async def require_local_lilies_token(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        forbidden_query_keys = {
            "access_token",
            "api_key",
            "api_token",
            "authorization",
            "bootstrap_credential",
            "credential",
            "frontend_token",
            "pairing_code",
            "password",
            "prepared_access_token",
            "previous_access_token",
            "secret",
            "token",
        }
        if any(name.casefold() in forbidden_query_keys for name in request.query_params):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "query_secret_forbidden",
                    "message": "local Lilies platform routes accept credentials only in headers",
                },
            )
        supplied = credentials.credentials if credentials else None
        if supplied != settings.api_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "invalid_api_token", "message": "invalid API token"},
            )

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
            "model_egress_enabled": settings.model_egress_enabled,
            "docker_available": shutil.which("docker") is not None,
            "provider": services.provider.name,
            "tools": services.tools.names(),
        }

    @app.get("/v1/models", dependencies=[Depends(require_token)])
    async def models() -> dict[str, Any]:
        return {
            "provider": services.provider.name,
            "type": services.provider.name,  # new key, backward compat
            "configured_providers": getattr(
                services.provider, "configured_providers", ["deepseek"]
            ),
            "configured_models": getattr(services.provider, "configured_models", []),
            "generator_model": settings.deepseek_generator_model,
            "runtime_model": settings.deepseek_runtime_model,
            "model_egress_enabled": settings.model_egress_enabled,
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

    @app.get("/api/v1/platform/harness/policy-controls", dependencies=[Depends(require_token)])
    async def get_platform_harness_policy_controls() -> dict[str, Any]:
        return services.harness.policy_controls()

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
            return services.harness.update_policy_controls(reason=body.reason, **patch_fields)
        except PlatformHarnessViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.get(
        "/api/v1/platform/harness/worker-handler-catalog", dependencies=[Depends(require_token)]
    )
    async def get_platform_harness_worker_handler_catalog() -> dict[str, Any]:
        from .worker_runner import build_platform_worker_handlers, platform_worker_handler_catalog

        return platform_worker_handler_catalog(build_platform_worker_handlers(services))

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

    @app.post(
        "/api/v1/platform/harness/tasks/{task_id}/lease", dependencies=[Depends(require_token)]
    )
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

    @app.post(
        "/api/v1/platform/harness/tasks/{task_id}/lease/renew",
        dependencies=[Depends(require_token)],
    )
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

    @app.post(
        "/api/v1/platform/harness/tasks/{task_id}/lease/release",
        dependencies=[Depends(require_token)],
    )
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

    @app.post(
        "/api/v1/platform/harness/queue/requeue-expired", dependencies=[Depends(require_token)]
    )
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

    @app.post(
        "/api/v1/platform/harness/worker-supervision/start", dependencies=[Depends(require_token)]
    )
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

    @app.post(
        "/api/v1/platform/harness/worker-supervision/stop", dependencies=[Depends(require_token)]
    )
    async def stop_platform_harness_worker_supervision() -> dict[str, Any]:
        if services.worker_supervisor is None:
            raise HTTPException(503, "platform worker supervisor unavailable")
        return await services.worker_supervisor.stop()

    @app.get(
        "/api/v1/platform/harness/worker-process-manager", dependencies=[Depends(require_token)]
    )
    async def get_platform_harness_worker_process_manager() -> dict[str, Any]:
        if services.worker_process_manager is None:
            raise HTTPException(503, "platform worker process manager unavailable")
        return services.worker_process_manager.snapshot()

    @app.post(
        "/api/v1/platform/harness/worker-process-manager/start",
        dependencies=[Depends(require_token)],
    )
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

    @app.post(
        "/api/v1/platform/harness/worker-process-manager/stop",
        dependencies=[Depends(require_token)],
    )
    async def stop_platform_harness_worker_process_manager() -> dict[str, Any]:
        if services.worker_process_manager is None:
            raise HTTPException(503, "platform worker process manager unavailable")
        return services.worker_process_manager.stop()

    @app.post(
        "/api/v1/platform/harness/worker-process-manager/restart",
        dependencies=[Depends(require_token)],
    )
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
                assignment_id=body.assignment_id,
                session_id=body.session_id,
                application_id=body.application_id,
                run_id=body.run_id,
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

    @app.post(
        "/api/v1/platform/governed-memory", status_code=201, dependencies=[Depends(require_token)]
    )
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

    @app.post(
        "/api/v1/platform/governed-memory/{memory_id}/read", dependencies=[Depends(require_token)]
    )
    async def read_governed_memory(
        memory_id: str, body: GovernedMemoryReadRequest
    ) -> dict[str, Any]:
        try:
            item = await services.governed_memory.read(
                memory_id, permission=body.permission, reason=body.reason
            )
            return item.model_dump(mode="json")
        except GovernedMemoryViolation as error:
            raise HTTPException(422, str(error)) from error

    @app.patch(
        "/api/v1/platform/governed-memory/{memory_id}", dependencies=[Depends(require_token)]
    )
    async def update_governed_memory(
        memory_id: str, body: GovernedMemoryUpdateRequest
    ) -> dict[str, Any]:
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

    @app.post(
        "/api/v1/platform/governed-memory/{memory_id}/revoke", dependencies=[Depends(require_token)]
    )
    async def revoke_governed_memory(
        memory_id: str, body: GovernedMemoryReadRequest
    ) -> dict[str, Any]:
        try:
            item = await services.governed_memory.revoke(
                memory_id, permission=body.permission, reason=body.reason
            )
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
            return {
                "expired": [item.model_dump(mode="json") for item in expired],
                "expired_count": len(expired),
            }
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

    @app.post(
        "/api/v1/event-subscriptions",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def create_event_subscription(
        body: EventSubscriptionCreateRequest,
    ) -> dict[str, Any]:
        try:
            published = await services.workflow_store.get_version(
                body.application_id
            )
            matching_triggers = [
                node
                for node in published["snapshot"].workflow.nodes
                if node.type == "event_subscription_trigger"
                and node.config.get("subscription_name") == body.name
            ]
            if len(matching_triggers) != 1:
                raise ValueError(
                    "published workflow must contain exactly one matching "
                    "event_subscription_trigger"
                )
            if not Path(body.workspace_path).resolve().is_dir():
                raise ValueError("event subscription workspace_path must exist")
            return await services.event_automation.create_subscription(body)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, PlatformHarnessViolation) as error:
            raise HTTPException(422, str(error)) from error

    @app.get(
        "/api/v1/event-subscriptions",
        dependencies=[Depends(require_token)],
    )
    async def list_event_subscriptions(
        application_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await services.event_automation.list_subscriptions(
            application_id=application_id
        )

    @app.get(
        "/api/v1/event-subscriptions/{subscription_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_event_subscription(
        subscription_id: str,
    ) -> dict[str, Any]:
        try:
            return await services.event_automation.get_subscription(
                subscription_id
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/event-subscriptions/{subscription_id}/state",
        dependencies=[Depends(require_token)],
    )
    async def set_event_subscription_state(
        subscription_id: str,
        body: EventSubscriptionStateRequest,
    ) -> dict[str, Any]:
        try:
            return await services.event_automation.set_subscription_enabled(
                subscription_id,
                body.enabled,
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/event-timers",
        dependencies=[Depends(require_token)],
    )
    async def list_event_timers(
        application_id: str | None = None,
        timer_status: str | None = Query(default=None, alias="status"),
    ) -> list[dict[str, Any]]:
        return await services.event_automation.list_timers(
            application_id=application_id,
            status=timer_status,
        )

    @app.get(
        "/api/v1/event-timers/{timer_key}",
        dependencies=[Depends(require_token)],
    )
    async def get_event_timer(timer_key: str) -> dict[str, Any]:
        try:
            return await services.event_automation.get_timer(timer_key)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    def knowledge_http_exception(error: Exception) -> HTTPException:
        if isinstance(error, KeyError):
            return HTTPException(404, str(error))
        if isinstance(error, KnowledgeIndexConflict):
            return HTTPException(409, str(error))
        return HTTPException(422, str(error))

    @app.post("/api/v1/knowledge-indexes", dependencies=[Depends(require_token)])
    async def create_knowledge_index(
        body: KnowledgeIndexCreateRequest,
    ) -> dict[str, Any]:
        try:
            return await services.knowledge_indexes.create_index(body)
        except (KeyError, ValueError) as error:
            raise knowledge_http_exception(error) from error

    @app.get("/api/v1/knowledge-indexes", dependencies=[Depends(require_token)])
    async def list_knowledge_indexes() -> list[dict[str, Any]]:
        return await services.knowledge_indexes.list_indexes()

    @app.get(
        "/api/v1/knowledge-indexes/{index_name}",
        dependencies=[Depends(require_token)],
    )
    async def get_knowledge_index(index_name: str) -> dict[str, Any]:
        try:
            return await services.knowledge_indexes.get_index(index_name)
        except (KeyError, ValueError) as error:
            raise knowledge_http_exception(error) from error

    @app.post(
        "/api/v1/knowledge-indexes/{index_name}/sync",
        dependencies=[Depends(require_token)],
    )
    async def sync_knowledge_index(
        index_name: str,
        body: KnowledgeSyncRequest,
    ) -> dict[str, Any]:
        try:
            return await services.knowledge_indexes.sync(index_name, body)
        except (KeyError, ValueError) as error:
            raise knowledge_http_exception(error) from error

    @app.post(
        "/api/v1/knowledge-indexes/{index_name}/retrieve",
        dependencies=[Depends(require_token)],
    )
    async def retrieve_knowledge(
        index_name: str,
        body: KnowledgeRetrieveRequest,
    ) -> dict[str, Any]:
        try:
            return await services.knowledge_indexes.retrieve(index_name, body)
        except (KeyError, ValueError) as error:
            raise knowledge_http_exception(error) from error

    @app.post(
        "/api/v1/knowledge-indexes/{index_name}/answer",
        dependencies=[Depends(require_token)],
    )
    async def answer_from_knowledge(
        index_name: str,
        body: GroundedAnswerRequest,
    ) -> dict[str, Any]:
        try:
            return await services.knowledge_indexes.answer(index_name, body)
        except (KeyError, ValueError) as error:
            raise knowledge_http_exception(error) from error

    def tabular_http_exception(error: Exception) -> HTTPException:
        if isinstance(error, KeyError):
            return HTTPException(404, str(error))
        if isinstance(error, TabularModelConflict):
            return HTTPException(409, str(error))
        return HTTPException(422, str(error))

    @app.post("/api/v1/tabular-models/train", dependencies=[Depends(require_token)])
    async def train_tabular_model(
        body: TrainTabularModelRequest,
    ) -> dict[str, Any]:
        try:
            return await services.tabular_models.train(body)
        except (KeyError, ValueError) as error:
            raise tabular_http_exception(error) from error

    @app.post("/api/v1/tabular-models/import", dependencies=[Depends(require_token)])
    async def import_tabular_model(
        body: ImportTabularModelRequest,
    ) -> dict[str, Any]:
        try:
            return await services.tabular_models.import_model(body)
        except (KeyError, ValueError) as error:
            raise tabular_http_exception(error) from error

    @app.get("/api/v1/tabular-models", dependencies=[Depends(require_token)])
    async def list_tabular_models() -> list[dict[str, Any]]:
        return await services.tabular_models.list_models()

    @app.get(
        "/api/v1/tabular-models/{model_id}/versions/{version}",
        dependencies=[Depends(require_token)],
    )
    async def get_tabular_model_version(
        model_id: str,
        version: int,
    ) -> dict[str, Any]:
        try:
            return await services.tabular_models.get_version(model_id, version)
        except (KeyError, ValueError) as error:
            raise tabular_http_exception(error) from error

    @app.post(
        "/api/v1/tabular-models/{model_id}/versions/{version}/fine-tune",
        dependencies=[Depends(require_token)],
    )
    async def fine_tune_tabular_model(
        model_id: str,
        version: int,
        body: FineTuneTabularModelRequest,
    ) -> dict[str, Any]:
        try:
            return await services.tabular_models.fine_tune(model_id, version, body)
        except (KeyError, ValueError) as error:
            raise tabular_http_exception(error) from error

    @app.post(
        "/api/v1/tabular-models/{model_id}/versions/{version}/evaluate",
        dependencies=[Depends(require_token)],
    )
    async def evaluate_tabular_model(
        model_id: str,
        version: int,
        body: EvaluateTabularModelRequest,
    ) -> dict[str, Any]:
        try:
            return await services.tabular_models.evaluate(model_id, version, body)
        except (KeyError, ValueError) as error:
            raise tabular_http_exception(error) from error

    @app.get(
        "/api/v1/model-deployments/{deployment_name}",
        dependencies=[Depends(require_token)],
    )
    async def get_tabular_model_deployment(
        deployment_name: str,
    ) -> dict[str, Any]:
        try:
            return await services.tabular_models.get_deployment(deployment_name)
        except (KeyError, ValueError) as error:
            raise tabular_http_exception(error) from error

    @app.post(
        "/api/v1/model-deployments/{deployment_name}/promote",
        dependencies=[Depends(require_token)],
    )
    async def promote_tabular_model(
        deployment_name: str,
        body: PromoteTabularModelRequest,
    ) -> dict[str, Any]:
        try:
            return await services.tabular_models.promote(deployment_name, body)
        except (KeyError, ValueError) as error:
            raise tabular_http_exception(error) from error

    @app.post(
        "/api/v1/model-deployments/{deployment_name}/rollback",
        dependencies=[Depends(require_token)],
    )
    async def rollback_tabular_model(
        deployment_name: str,
        body: RollbackTabularDeploymentRequest,
    ) -> dict[str, Any]:
        try:
            return await services.tabular_models.rollback(deployment_name, body)
        except (KeyError, ValueError) as error:
            raise tabular_http_exception(error) from error

    @app.post(
        "/api/v1/model-deployments/{deployment_name}/predict",
        dependencies=[Depends(require_token)],
    )
    async def predict_tabular_model(
        deployment_name: str,
        body: TabularInferenceRequest,
    ) -> dict[str, Any]:
        try:
            return await services.tabular_models.predict(deployment_name, body)
        except (KeyError, ValueError) as error:
            raise tabular_http_exception(error) from error

    @app.post(
        "/api/v1/model-deployments/{deployment_name}/drift",
        dependencies=[Depends(require_token)],
    )
    async def monitor_tabular_model_drift(
        deployment_name: str,
        body: TabularDriftRequest,
    ) -> dict[str, Any]:
        try:
            return await services.tabular_models.drift(deployment_name, body)
        except (KeyError, ValueError) as error:
            raise tabular_http_exception(error) from error

    def forecast_http_exception(error: Exception) -> HTTPException:
        if isinstance(error, KeyError):
            return HTTPException(404, str(error))
        if isinstance(error, ForecastModelConflict):
            return HTTPException(409, str(error))
        return HTTPException(422, str(error))

    @app.post("/api/v1/forecast-models/train", dependencies=[Depends(require_token)])
    async def train_forecast_model(
        body: TrainForecastModelRequest,
    ) -> dict[str, Any]:
        try:
            return await services.forecast_models.train(body)
        except (KeyError, ValueError) as error:
            raise forecast_http_exception(error) from error

    @app.post("/api/v1/forecast-models/import", dependencies=[Depends(require_token)])
    async def import_forecast_model(
        body: ImportForecastModelRequest,
    ) -> dict[str, Any]:
        try:
            return await services.forecast_models.import_model(body)
        except (KeyError, ValueError) as error:
            raise forecast_http_exception(error) from error

    @app.get("/api/v1/forecast-models", dependencies=[Depends(require_token)])
    async def list_forecast_models() -> list[dict[str, Any]]:
        return await services.forecast_models.list_models()

    @app.get(
        "/api/v1/forecast-models/{model_id}/versions/{version}",
        dependencies=[Depends(require_token)],
    )
    async def get_forecast_model_version(
        model_id: str,
        version: int,
    ) -> dict[str, Any]:
        try:
            return await services.forecast_models.get_version(model_id, version)
        except (KeyError, ValueError) as error:
            raise forecast_http_exception(error) from error

    @app.post(
        "/api/v1/forecast-models/{model_id}/versions/{version}/fine-tune",
        dependencies=[Depends(require_token)],
    )
    async def fine_tune_forecast_model(
        model_id: str,
        version: int,
        body: FineTuneForecastModelRequest,
    ) -> dict[str, Any]:
        try:
            return await services.forecast_models.fine_tune(model_id, version, body)
        except (KeyError, ValueError) as error:
            raise forecast_http_exception(error) from error

    @app.post(
        "/api/v1/forecast-models/{model_id}/versions/{version}/evaluate",
        dependencies=[Depends(require_token)],
    )
    async def evaluate_forecast_model(
        model_id: str,
        version: int,
        body: EvaluateForecastModelRequest,
    ) -> dict[str, Any]:
        try:
            return await services.forecast_models.evaluate(model_id, version, body)
        except (KeyError, ValueError) as error:
            raise forecast_http_exception(error) from error

    @app.get(
        "/api/v1/forecast-deployments/{deployment_name}",
        dependencies=[Depends(require_token)],
    )
    async def get_forecast_deployment(
        deployment_name: str,
    ) -> dict[str, Any]:
        try:
            return await services.forecast_models.get_deployment(deployment_name)
        except (KeyError, ValueError) as error:
            raise forecast_http_exception(error) from error

    @app.post(
        "/api/v1/forecast-deployments/{deployment_name}/promote",
        dependencies=[Depends(require_token)],
    )
    async def promote_forecast_model(
        deployment_name: str,
        body: PromoteForecastModelRequest,
    ) -> dict[str, Any]:
        try:
            return await services.forecast_models.promote(deployment_name, body)
        except (KeyError, ValueError) as error:
            raise forecast_http_exception(error) from error

    @app.post(
        "/api/v1/forecast-deployments/{deployment_name}/rollback",
        dependencies=[Depends(require_token)],
    )
    async def rollback_forecast_model(
        deployment_name: str,
        body: RollbackForecastDeploymentRequest,
    ) -> dict[str, Any]:
        try:
            return await services.forecast_models.rollback(deployment_name, body)
        except (KeyError, ValueError) as error:
            raise forecast_http_exception(error) from error

    @app.post(
        "/api/v1/forecast-deployments/{deployment_name}/predict",
        dependencies=[Depends(require_token)],
    )
    async def predict_forecast_model(
        deployment_name: str,
        body: ForecastInferenceRequest,
    ) -> dict[str, Any]:
        try:
            return await services.forecast_models.predict(deployment_name, body)
        except (KeyError, ValueError) as error:
            raise forecast_http_exception(error) from error

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
        allowed_statuses = {"legacy_unverified", "draft", "verified", "deprecated", "quarantined"}
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
            payloads.append(
                {
                    **meta.model_dump(mode="json"),
                    "module_ref": record.module_ref,
                    "module_status": record.state.status,
                    "content_hash": record.state.content_hash,
                    "module_contract": (
                        record.template.module_contract.model_dump(mode="json")
                        if record.template.module_contract
                        else None
                    ),
                }
            )
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
    async def suggest_templates(
        requirement: str = "", reuse_depth: str | None = None
    ) -> list[dict[str, Any]]:
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
            [record.template.meta for record in services.templates.list_records(all_versions=True)],
        )
        payloads: list[dict[str, Any]] = []
        for score, meta in scored[:5]:
            record = services.templates.get_record(meta.name, meta.version)
            payloads.append(
                {
                    **build_suggestion_payload(
                        meta,
                        score,
                        reuse_depth,
                        default_metadata=default_metadata,
                    ),
                    "module_ref": record.module_ref,
                    "module_status": record.state.status,
                    "verified_capability_carrier": record.state.status == "verified",
                }
            )
        return payloads

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
    async def publish_template(application_id: str, body: TemplateCreateRequest) -> dict[str, Any]:
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
        # storage.get_session 返回的 messages 是 ChatMessage 模型列表，不是 dict——
        # 对模型调 .get() 抛 AttributeError，逃过下面的 except KeyError 直接 500。
        # 与 api.py 里读会话的另一处（sse 那边）保持同一写法：先拍平再处理。
        messages = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in record.get("messages", [])
        ]
        # Extract decision points from user/assistant message pairs
        decision_count = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and i + 1 < len(messages):
                if messages[i + 1].get("role") == "assistant":
                    question = "".join(
                        b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text"
                    )[:200]
                    answer = "".join(
                        b.get("text", "")
                        for b in messages[i + 1].get("content", [])
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
    async def check_template_merge(name: str, body: dict[str, Any]) -> dict[str, Any]:
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
    async def merge_template(name: str, body: dict[str, Any]) -> dict[str, Any]:
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
            FAMILY_MAP,
            strategy_help,
            get_discrete_block_type,
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
        result = []
        for name in services.tools.names():
            definition = services.tools.get(name).definition()
            result.append({
                "name": name,
                "type": "core",
                "published": True,
                "description": definition.description,
                "input_schema": definition.input_schema,
            })
        for application in await services.workflow_store.list_applications():
            if application["active_version"] is not None:
                result.append(
                    {
                        "name": f"workflow:{application['id']}",
                        "type": "workflow",
                        "title": application["name"],
                        "version": application["active_version"],
                        "published": True,
                    }
                )
        return result

    @app.get("/api/v1/tools/program-profiles", dependencies=[Depends(require_token)])
    async def list_program_profiles() -> list[dict[str, Any]]:
        program_tool = services.tools.get("Program")
        public_profiles = getattr(program_tool, "public_profiles", None)
        return public_profiles() if callable(public_profiles) else []

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
            if (
                snapshot.workflow.nodes or snapshot.workflow.edges or snapshot.tests
            ) and not body.replace_existing:
                raise ValueError(
                    "draft already contains workflow content; set replace_existing=true to replace it atomically"
                )
            result = await services.applications.apply_operations_atomically(
                application_id,
                expected_revision=body.expected_revision,
                expected_content_hash=body.expected_content_hash,
                operations=[
                    {
                        "op": "replace_workflow",
                        "data": {"workflow": scenario.workflow.model_dump(mode="json")},
                    },
                    {
                        "op": "replace_tests",
                        "data": {
                            "tests": [
                                test.model_dump(mode="json") for test in scenario.acceptance_cases
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

    @app.post("/api/v1/applications", status_code=201, dependencies=[Depends(require_token)])
    async def create_application(body: ApplicationCreateRequest) -> dict[str, Any]:
        return await services.workflow_store.create_application(body)

    @app.get("/api/v1/applications", dependencies=[Depends(require_token)])
    async def list_applications() -> list[dict[str, Any]]:
        applications = await services.workflow_store.list_applications()
        # 业主关心的是"正式版验收过没有"（监理验收单），不是构建期草稿证据。
        # 首页卡片用它替换刺眼且工程向的"证据已过期"。
        for application in applications:
            report = acceptance_pm.load_report(
                services.settings.data_dir, str(application.get("id"))
            )
            if report:
                application["acceptance"] = {
                    "accepted": bool(report.get("accepted")),
                    "stamp": report.get("stamp"),
                    "passed_cases": report.get("passed_cases"),
                    "total_cases": len(report.get("cases") or []),
                }
        return applications

    @app.get("/api/v1/applications/{application_id}", dependencies=[Depends(require_token)])
    async def get_application(application_id: str) -> dict[str, Any]:
        try:
            return await services.workflow_store.get_application(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/smoke-cleanup", dependencies=[Depends(require_token)]
    )
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
                    task = services.builders.active_task(build_id)
                    if task is None or task.done():
                        continue
                    services.builders.for_build(build).cancel(build_id)
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

    @app.post("/api/v1/applications/{application_id}/draft", dependencies=[Depends(require_token)])
    async def mutate_application_draft(application_id: str, body: DraftOperation) -> dict[str, Any]:
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
            response, preview_source = await _plan_workflow_edit(
                services,
                task_id=task_id,
                draft=draft,
                body=body,
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
        except ValueError as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise HTTPException(422, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/draft/natural-language-edit",
        dependencies=[Depends(require_token)],
        response_model=NaturalLanguageDraftEditResponse,
    )
    async def natural_language_application_draft_edit(
        application_id: str,
        body: NaturalLanguageDraftEditRequest,
    ) -> dict[str, Any]:
        task_id = str(uuid4())
        await services.harness.start_task(
            task_id,
            kind="draft_patch_preview",
            owner_id=application_id,
            resource_id=application_id,
            metadata={
                "origin": "natural_language_edit",
                "preview_only": body.preview_only,
                "instruction_digest": _workflow_edit_instruction_digest(body.instruction),
                "node_count": len(body.node_ids),
                "edge_count": len(body.edge_ids),
            },
        )
        try:
            if body.preview_only and (body.preview_task_id or body.expected_preview_digest):
                raise ValueError(
                    "preview_task_id and expected_preview_digest are apply-only fields"
                )
            if not body.preview_only and (
                not body.preview_task_id or not body.expected_preview_digest
            ):
                raise ValueError(
                    "applying a natural-language edit requires both preview_task_id "
                    "and expected_preview_digest from a reviewed preview"
                )
            draft = await services.workflow_store.get_draft(application_id)
            if not body.preview_only:
                (
                    response,
                    preview_digest,
                    stored_preview_source,
                ) = await _load_workflow_edit_stored_plan(
                    services,
                    application_id=application_id,
                    body=body,
                )
                preview_source: Literal["deterministic", "model", "stored_preview"] = (
                    "stored_preview"
                )
            else:
                if int(draft["revision"]) != body.expected_revision:
                    raise RevisionConflict(
                        "workflow edit revision conflict: "
                        f"expected {body.expected_revision}, current {draft['revision']}"
                    )
                if draft["content_hash"] != body.expected_content_hash:
                    raise RevisionConflict(
                        "workflow edit content hash no longer matches the current draft"
                    )
                response, planned_source = await _plan_workflow_edit(
                    services,
                    task_id=task_id,
                    draft=draft,
                    body=body,
                )
                preview_source = planned_source
                preview_digest = _workflow_edit_plan_digest(
                    application_id=application_id,
                    instruction=body.instruction,
                    revision=body.expected_revision,
                    content_hash=body.expected_content_hash,
                    node_ids=response.node_ids,
                    edge_ids=response.edge_ids,
                    operations=response.operations,
                )

            if body.expected_preview_digest and body.expected_preview_digest != preview_digest:
                raise RevisionConflict(
                    "workflow edit preview digest no longer matches the reviewed plan"
                )

            applied = False
            if response.supported and not body.preview_only:
                await services.applications.apply_operations_atomically(
                    application_id,
                    expected_revision=body.expected_revision,
                    expected_content_hash=body.expected_content_hash,
                    operations=response.operations,
                    idempotency_key=body.idempotency_key,
                    change_context_operation="natural_language_edit",
                )
                applied = True

            current = await services.workflow_store.get_draft(application_id)
            plan_metadata = _workflow_edit_stored_plan(
                application_id=application_id,
                body=body,
                response=response,
                preview_source=(
                    planned_source if preview_source != "stored_preview" else stored_preview_source
                ),
                preview_digest=preview_digest,
            )
            await services.harness.finish_task(
                task_id,
                status="succeeded" if response.supported else "failed",
                metadata={
                    "intent": response.intent,
                    "preview_source": preview_source,
                    "operation_count": len(response.operations),
                    "applied": applied,
                    "natural_language_edit_plan": plan_metadata,
                },
                error="" if response.supported else response.message,
            )
            draft_payload = _workflow_edit_draft_payload(current)
            return {
                "task_id": task_id,
                **response.model_dump(mode="json"),
                "applied": applied,
                "expected_revision": body.expected_revision,
                "expected_content_hash": body.expected_content_hash,
                "preview_source": preview_source,
                "preview_digest": preview_digest,
                "draft": draft_payload,
                "evidence": draft_payload["evidence"],
            }
        except RevisionConflict as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            await services.harness.finish_task(task_id, status="failed", error=str(error))
            raise HTTPException(422, str(error)) from error

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
        if not services.builders.has(body.builder):
            raise HTTPException(
                400,
                f"unknown builder: {body.builder} (available: {', '.join(services.builders.names())})",
            )
        try:
            await services.workflow_store.get_application(application_id)
            await services.workflow_store.get_draft(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        build_id = str(uuid4())
        await services.workflow_store.create_build(
            build_id,
            application_id,
            body.requirement,
            body.auto_publish,
            body.max_turns,
            body.max_repair_cycles,
            body.max_elapsed_seconds,
            body.planning_mode,
            builder=body.builder,
            coordinator_model=body.coordinator_model,
            teammate_models=body.teammate_models,
            thinking_enabled=body.thinking_enabled,
            effort=body.effort,
            turn_max_output_tokens=body.turn_max_output_tokens,
        )
        await asyncio.to_thread(
            services.build_transcripts.append,
            build_id,
            owner_record(text=body.requirement, draft_revision=0),
        )
        services.builders.get(body.builder).start(build_id)
        return {
            "build_id": build_id,
            "application_id": application_id,
            "status": "queued",
            "builder": body.builder,
            "max_elapsed_seconds": body.max_elapsed_seconds,
            "deadline": deadline_summary(body.max_elapsed_seconds),
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

    @app.get("/api/v1/builds/{build_id}/transcript", dependencies=[Depends(require_token)])
    async def get_build_transcript(
        build_id: str,
        after_turn: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        """Return the Builder's own model turns: reasoning, tool arguments, tool results."""

        records = await asyncio.to_thread(
            services.build_transcripts.read,
            build_id,
            after_turn=after_turn,
            limit=limit,
        )
        summary = await asyncio.to_thread(services.build_transcripts.summary, build_id)
        return {"build_id": build_id, "summary": summary, "records": records}

    # ── 运行级返修：业主一键"让莉莉丝自己查"，流水账证据自动随单 ──

    @app.post(
        "/api/v1/applications/{application_id}/runs/{run_id}/repair",
        dependencies=[Depends(require_token)],
    )
    async def request_run_repair(
        application_id: str, run_id: str, body: RunRepairRequest
    ) -> dict[str, Any]:
        try:
            run = await services.workflow_store.get_run(run_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        if str(run.get("application_id")) != application_id:
            raise HTTPException(404, "这个运行不属于该应用")
        builds = await services.workflow_store.list_builds(application_id)
        if not builds:
            raise HTTPException(409, "该应用没有可返修的构建")
        build = builds[0]
        if build["status"] in {"queued", "building"}:
            raise HTTPException(409, "正在搭建中，等这轮结束再让它查")
        events = await services.storage.list_events(run_id, 0)
        state = run.get("state")
        merged: dict[str, Any] = {}
        for value in ((state.outputs if hasattr(state, "outputs") else {}) or {}).values():
            if isinstance(value, dict):
                merged.update(value)
        ledger, suspicions = _summarize_run_ledger(events, merged)
        complaint = f"业主备注：{body.note}\n\n" if body.note.strip() else ""
        # 证据必须自带身份：这份账本对应哪次输入。缺了它，业主投诉"正常日
        # 金额不对"而账本恰好来自空数据日时，两份证据自相矛盾，修复会被
        # 带进"把 0 合理化"的沟里（ERP 盲测返修#3/#4 实案）。
        run_inputs = (
            getattr(state, "inputs", None)
            if not isinstance(state, dict)
            else state.get("inputs")
        ) or {}
        inputs_line = json.dumps(services.harness.redact_payload(run_inputs) if hasattr(services.harness, "redact_payload") else run_inputs, ensure_ascii=False, default=str)[:600]
        # 强制复现：平台自动用报障运行的原始输入发起一次新鲜运行。
        # 盲测教训：历史里的错误结论会压过任何指令（"上次查过是空数据"），
        # 只有新鲜账本能击穿旧信念——所以不指望她自觉重查，直接把复现
        # 运行递到她手里。
        replay_line = ""
        try:
            replay = await services.workflow_runtime.create_run(
                application_id,
                WorkflowRunRequest(inputs=dict(run_inputs), use_draft=False),
                origin="repair_replay",
            )
            replay_line = (
                f"平台已用完全相同的输入自动发起复现运行 {replay['run_id']}。\n"
                "先 run_inspect 这个复现运行的新鲜账本，以它为准定位——"
                "不要沿用你在早前轮次里得出的任何结论。\n"
            )
        except Exception:
            replay_line = ""
        message = (
            f"业主对运行 {run_id} 的结果不满意，要求你自查并修复。\n"
            f"这次运行的输入参数：{inputs_line}\n"
            + replay_line
            + "\n"
            + complaint
            + ("平台自动体检发现：" + "；".join(suspicions) + "\n\n" if suspicions else "")
            + "这次运行的执行流水账摘要：\n" + ledger + "\n\n"
            + "要求：先用 run_inspect 核对这次运行的完整证据，找到根因再动手修；"
            + "修好后必须自测（含空结果/异常输入的用例）确认问题消失，再重新发布。"
            + "不许只调提示词碰运气，不许拿格式示例充当结果。"
        )
        services.builders.for_build(build).queue_resume_message(build["id"], message[:8_000])
        await asyncio.to_thread(
            services.build_transcripts.append,
            build["id"],
            owner_record(text=f"[对运行 {run_id[:8]} 不满意，发起自查] {body.note}".strip(), draft_revision=build["team_state"].revision),
        )
        await services.workflow_store.update_build(build["id"], status="queued", error="")
        services.builders.for_build(build).start(build["id"])
        return {
            "application_id": application_id,
            "build_id": build["id"],
            "status": "queued",
            "suspicions": suspicions,
        }

    @app.post(
        "/api/v1/applications/{application_id}/acceptance/repair",
        dependencies=[Depends(require_token)],
    )
    async def repair_from_acceptance(application_id: str) -> dict[str, Any]:
        """把最近一次监理验收的失败项组装成改单，交给构建方返修。

        焊接两个既有闭环：监考（机械验收）→ 返修（自查修复）。客户点一下
        "按验收单返修"，失败用例的期望 vs 实际差异就是证据，不需要任何人
        写技术描述。
        """

        report = acceptance_pm.load_report(services.settings.data_dir, application_id)
        if not report:
            raise HTTPException(404, "还没有验收记录——先在监理页跑一次验收")
        if report.get("accepted"):
            raise HTTPException(409, "最近一次验收是通过的，没有需要返修的失败项")
        builds = await services.workflow_store.list_builds(application_id)
        if not builds:
            raise HTTPException(409, "该应用没有可返修的构建")
        build = builds[0]
        if build["status"] in {"queued", "building"}:
            raise HTTPException(409, "正在搭建中，等这轮结束再返修")

        lines: list[str] = []
        for missing in report.get("architecture_missing") or []:
            lines.append(f"- 结构审查不过：缺少环节类型 {missing}")
        for missing in report.get("lineage_missing") or []:
            lines.append(f"- 执行审计不过：{missing} 没有真实参与产出")
        for case in report.get("cases") or []:
            if case.get("passed"):
                continue
            lines.append(f"- 用例「{case.get('name')}」不过（运行 {case.get('run_status')}）：")
            for check in case.get("checks") or []:
                if not check.get("passed"):
                    lines.append(
                        f"    · {check.get('check')} —— 实际：{str(check.get('actual'))[:160]}"
                    )
        message = (
            "独立验收没有通过，以下是验收单上的全部失败项（期望 vs 实际），"
            "逐项修复后重新发布：\n\n" + "\n".join(lines[:60])
            + "\n\n要求：先用 run_inspect 查失败用例对应运行的账本定位根因；"
            "修复后自测确认每一项都消失，再重新发布。不许削弱验收口径来凑绿。"
        )
        services.builders.for_build(build).queue_resume_message(build["id"], message[:8_000])
        await asyncio.to_thread(
            services.build_transcripts.append,
            build["id"],
            owner_record(
                text=f"[验收不通过，按验收单发起返修] 共 {len(lines)} 项失败",
                draft_revision=build["team_state"].revision,
            ),
        )
        await services.workflow_store.update_build(build["id"], status="queued", error="")
        services.builders.for_build(build).start(build["id"])
        return {
            "application_id": application_id,
            "build_id": build["id"],
            "status": "queued",
            "failure_items": len(lines),
        }

    @app.get(
        "/api/v1/applications/{application_id}/runs/{run_id}/health",
        dependencies=[Depends(require_token)],
    )
    async def run_health(application_id: str, run_id: str) -> dict[str, Any]:
        """零模型体检：给试运行页出"结果可疑"横幅用。"""

        run = await services.workflow_store.get_run(run_id)
        if str(run.get("application_id")) != application_id:
            raise HTTPException(404, "这个运行不属于该应用")
        events = await services.storage.list_events(run_id, 0)
        state = run.get("state")
        merged: dict[str, Any] = {}
        for value in ((state.outputs if hasattr(state, "outputs") else {}) or {}).values():
            if isinstance(value, dict):
                merged.update(value)
        _, suspicions = _summarize_run_ledger(events, merged)
        return {"run_id": run_id, "suspicions": suspicions}

    # ── 使用者通道：一应用一码，链接即交付（永不出示总钥匙） ──

    async def _require_use_access(application_id: str, code: str) -> None:
        if not await services.workflow_store.verify_access_code(application_id, code):
            raise HTTPException(403, "访问码不对或已更换——找给你链接的人要新链接")

    @app.post(
        "/api/v1/applications/{application_id}/access-code",
        dependencies=[Depends(require_token)],
    )
    async def rotate_application_access_code(application_id: str) -> dict[str, Any]:
        try:
            code = await services.workflow_store.rotate_access_code(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        return {
            "application_id": application_id,
            "code": code,
            "use_path": f"/use/{application_id}?code={code}",
        }

    @app.get(
        "/api/v1/applications/{application_id}/access-code",
        dependencies=[Depends(require_token)],
    )
    async def get_application_access_code(application_id: str) -> dict[str, Any]:
        """取现行码（没有才生成）——复制多视图链接时不作废已发出的链接。"""

        try:
            code = await services.workflow_store.ensure_access_code(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        return {
            "application_id": application_id,
            "code": code,
            "use_path": f"/use/{application_id}?code={code}",
        }

    # ── 业主通道：一应用一业主码，免登录会话面（答问/插话/返修），永不出示总钥匙 ──

    async def _require_owner_access(application_id: str, code: str) -> None:
        if not await services.workflow_store.verify_owner_code(application_id, code):
            raise HTTPException(403, "业主码不对或已更换——联系服务方获取新链接")

    @app.get(
        "/api/v1/applications/{application_id}/owner-code",
        dependencies=[Depends(require_token)],
    )
    async def get_application_owner_code(application_id: str) -> dict[str, Any]:
        try:
            code = await services.workflow_store.ensure_owner_code(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        return {
            "application_id": application_id,
            "code": code,
            "owner_path": f"/owner/{application_id}?code={code}",
        }

    @app.post(
        "/api/v1/applications/{application_id}/owner-code",
        dependencies=[Depends(require_token)],
    )
    async def rotate_application_owner_code(application_id: str) -> dict[str, Any]:
        try:
            code = await services.workflow_store.rotate_owner_code(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        return {
            "application_id": application_id,
            "code": code,
            "owner_path": f"/owner/{application_id}?code={code}",
        }

    @app.get("/api/v1/owner/{application_id}/state")
    async def owner_state(application_id: str, code: str) -> dict[str, Any]:
        """业主会话面的一站式状态：应用名、最新构建、待答问题、验收摘要。"""

        await _require_owner_access(application_id, code)
        try:
            application = await services.workflow_store.get_application(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        builds = await services.workflow_store.list_builds(application_id)
        latest = builds[0] if builds else None
        acceptance = acceptance_pm.load_report(services.settings.data_dir, application_id)
        return {
            "application": {"id": application_id, "name": application["name"]},
            "build": None if latest is None else {
                "id": latest["id"],
                "status": latest["status"],
                "pending_question": latest["team_state"].pending_question
                if latest["status"] == "needs_attention" else None,
                "updated_at": latest["updated_at"],
            },
            "acceptance": None if not acceptance else {
                "accepted": bool(acceptance.get("accepted")),
                "passed_cases": acceptance.get("passed_cases"),
                # 报告里没有 total_cases 这个键（另两处都是 len(cases)）——
                # 取它恒为 None，业主页在"部分失败"这条最需要看清的路径上
                # 恒显「1 / ? 通过」。
                "total_cases": len(acceptance.get("cases") or []),
            },
            "published_version": application.get("active_version"),
        }

    @app.get("/api/v1/owner/{application_id}/transcript")
    async def owner_transcript(
        application_id: str,
        code: str,
        after_turn: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        await _require_owner_access(application_id, code)
        builds = await services.workflow_store.list_builds(application_id)
        if not builds:
            return {"build_id": None, "records": [], "summary": {"available": False}}
        build_id = builds[0]["id"]
        records = await asyncio.to_thread(
            services.build_transcripts.read, build_id, after_turn=after_turn, limit=500,
        )
        summary = await asyncio.to_thread(services.build_transcripts.summary, build_id)
        return {"build_id": build_id, "records": records, "summary": summary}

    @app.post("/api/v1/owner/{application_id}/message")
    async def owner_message(application_id: str, body: OwnerMessageRequest) -> dict[str, Any]:
        """业主发话：构建进行中→实时插话；已停→作为新指示续跑。与会话页同语义。"""

        await _require_owner_access(application_id, body.code)
        builds = await services.workflow_store.list_builds(application_id)
        if not builds:
            raise HTTPException(409, "还没有开始搭建——请联系服务方发起构建")
        build = builds[0]
        engine = services.builders.for_build(build)
        if build["status"] in {"queued", "building"}:
            try:
                engine.post_live_message(build["id"], body.message)
            except RuntimeError as error:
                raise HTTPException(409, str(error)) from error
            delivered = "live"
        else:
            engine.queue_resume_message(build["id"], body.message)
            await services.workflow_store.update_build(build["id"], status="queued", error="")
            engine.start(build["id"])
            delivered = "resume"
        await asyncio.to_thread(
            services.build_transcripts.append,
            build["id"],
            owner_record(text=body.message, draft_revision=build["team_state"].revision),
        )
        return {"build_id": build["id"], "delivered": delivered}

    @app.post("/api/v1/owner/{application_id}/repair")
    async def owner_repair(application_id: str, body: OwnerRepairRequest) -> dict[str, Any]:
        """业主一键"按验收单返修"——复用监理返修闭环，业主码即授权。"""

        await _require_owner_access(application_id, body.code)
        return await repair_from_acceptance(application_id)

    # ── 业主侧：工作区文件与工作流定义导出 ──

    @app.get(
        "/api/v1/applications/{application_id}/workspace/files",
        dependencies=[Depends(require_token)],
    )
    async def list_workspace_files(application_id: str) -> list[dict[str, Any]]:
        """列应用工作区里的全部文件（含子目录），工作区还没建就给空列表。"""

        from datetime import datetime, timezone

        def _collect() -> list[dict[str, Any]]:
            try:
                root = services.sandboxes.resolve_workspace(application_id)
            except Exception:
                return []
            items: list[dict[str, Any]] = []
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                stat = path.stat()
                items.append({
                    "path": str(path.relative_to(root)),
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                })
            items.sort(key=lambda item: item["path"])
            return items[:2000]

        return await asyncio.to_thread(_collect)

    @app.get(
        "/api/v1/applications/{application_id}/workspace/files/{file_path:path}",
        dependencies=[Depends(require_token)],
    )
    async def download_workspace_file(application_id: str, file_path: str) -> Any:
        from fastapi.responses import FileResponse

        try:
            root = await asyncio.to_thread(
                services.sandboxes.resolve_workspace, application_id
            )
        except Exception:
            raise HTTPException(404, "文件不存在")
        target = (root / file_path).resolve()
        if root.resolve() not in target.parents and target != root.resolve():
            raise HTTPException(404, "文件不存在")
        if not target.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(target, filename=target.name)

    @app.get(
        "/api/v1/applications/{application_id}/delivery-package",
        dependencies=[Depends(require_token)],
    )
    async def download_delivery_package(
        application_id: str, profile: str = "customer"
    ) -> Any:
        """一键交付包（zip）。

        customer（默认）：客户交互包——README（使用链接/界面清单/验收状态）
        + 工作流定义 JSON + 工作区产出文件。
        expert：专家材料包——只含工作区产出（答卷/脚本/图表），无任何平台
        痕迹，适合把成果作为"一份工程师的答卷"递给外部评审。
        两种切面都排除 data/ 顶层目录（甲方自己的原始数据不用还给甲方）。
        """

        import io
        import zipfile

        from fastapi.responses import Response

        if profile not in ("customer", "expert"):
            raise HTTPException(422, "profile 只能是 customer 或 expert")
        try:
            application = await services.workflow_store.get_application(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
            # 工作区产出（两种切面都有）
            # "目录还不存在"是常态（工作流没跑过），"路径越界/读不了"是真错误——
            # 此前同一个 except 把两者抹平成 root=None，于是真错误会静默产出
            # 一个 22 字节的空 zip 交到用户手里。
            root = None
            try:
                root = await asyncio.to_thread(
                    services.sandboxes.resolve_workspace, application_id
                )
            except Exception as error:  # noqa: BLE001 - 下面按消息分流
                if "does not exist" not in str(error):
                    logger.warning("交付包读不了工作区 %s：%s", application_id, error)
                    raise HTTPException(
                        503, f"工作区不可读，交付包未生成：{error}") from error
            skipped: list[str] = []
            if root is not None:
                for path in sorted(root.rglob("*")):
                    try:
                        if not path.is_file():
                            continue
                        rel = path.relative_to(root)
                        if rel.parts and rel.parts[0] == "data":
                            continue
                        if path.stat().st_size > 50_000_000:
                            skipped.append(f"{rel}（超过 50MB）")
                            continue
                        bundle.write(path, f"workspace/{rel}")
                    except OSError as error:
                        # 单个文件读不了不该毁掉整包，但要在包里留痕
                        skipped.append(f"{path.name}（{error}）")
            if skipped:
                bundle.writestr("workspace/_跳过的文件.txt",
                                "以下文件没有打进包里：\n" + "\n".join(skipped))

            if profile == "customer":
                code = await services.workflow_store.ensure_access_code(application_id)
                snapshot = None
                try:
                    definition = await customer_runtime_definition(application_id)
                    snapshot = definition.get("snapshot")
                except KeyError:
                    definition = None
                views = auto_view_tabs(snapshot) if snapshot else []
                stored = await services.workflow_store.list_views(application_id)
                report = acceptance_pm.load_report(
                    services.settings.data_dir, application_id
                )
                lines = [
                    f"# {application.get('name', '交付包')}",
                    "",
                    f"**交付需求**：{str(application.get('requirement') or '')[:500]}",
                    "",
                    "## 怎么使用",
                    "",
                    f"打开使用链接（含访问码，请妥善保管）：`/use/{application_id}?code={code}`",
                    "",
                    "界面（页面顶部标签可切换）：",
                ]
                seen_views = {tab["view_id"] or "default": tab["name"] for tab in views}
                for item in stored:
                    seen_views.setdefault(item["view_id"], item["name"])
                lines += [f"- {name}" for name in seen_views.values()]
                if report:
                    lines += [
                        "",
                        "## 验收状态",
                        "",
                        f"独立验收：{'✅ 已通过' if report.get('accepted') else '整改中'}"
                        f"（{report.get('passed_cases')}/{len(report.get('cases') or [])} 用例 · {report.get('stamp')}）",
                    ]
                lines += [
                    "",
                    "## 包内目录",
                    "",
                    "- `workspace/`：本次交付的全部产出文件（报告、脚本、图表等）",
                    "- `workflow-definition.json`：工作流完整定义（备份/迁移用）",
                ]
                bundle.writestr("README.md", "\n".join(lines))
                export = await export_application_workflow(application_id)
                bundle.writestr("workflow-definition.json", bytes(export.body))

        buffer.seek(0)
        suffix = "customer" if profile == "customer" else "expert"
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="delivery-{suffix}-{application_id[:8]}.zip"'
                )
            },
        )

    @app.get(
        "/api/v1/applications/{application_id}/export",
        dependencies=[Depends(require_token)],
    )
    async def export_application_workflow(application_id: str) -> Any:
        """导出工作流定义 JSON：有发布版导发布版，否则导当前草稿。"""

        from fastapi.responses import JSONResponse

        try:
            application = await services.workflow_store.get_application(application_id)
            active_version = application.get("active_version")
            if active_version is not None:
                published = await services.workflow_store.get_version(
                    application_id, int(active_version)
                )
                workflow = published["snapshot"].model_dump(mode="json")
                source = "published"
                version: int | None = int(published["version"])
            else:
                draft = await services.workflow_store.get_draft(application_id)
                workflow = draft["snapshot"].model_dump(mode="json")
                source = "draft"
                version = None
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        payload = {
            "application": {
                "id": str(application.get("id") or application_id),
                "name": application.get("name"),
                "description": application.get("description"),
                "requirement": application.get("requirement"),
                "mode": application.get("mode"),
                "delivery_mode": application.get("delivery_mode"),
                "active_version": active_version,
                "created_at": application.get("created_at"),
                "updated_at": application.get("updated_at"),
                "source": source,
                "version": version,
            },
            "workflow": workflow,
            "views": await services.workflow_store.list_views(application_id),
        }
        return JSONResponse(
            content=payload,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="workflow-{application_id}.json"'
                )
            },
        )

    # ── 界面方案：标注环节显隐，同一工作流生成不同使用界面 ──

    @app.get(
        "/api/v1/applications/{application_id}/views",
        dependencies=[Depends(require_token)],
    )
    async def list_application_views(application_id: str) -> dict[str, Any]:
        try:
            definition = await customer_runtime_definition(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        snapshot = definition.get("snapshot") or {}
        workflow = snapshot.get("workflow") or {}
        nodes = [
            {
                "id": str(node.get("id") or ""),
                "title": str(node.get("title") or node.get("id") or ""),
                "type": str(node.get("type") or ""),
            }
            for node in (workflow.get("nodes") or [])
        ]
        # 自动界面全量配置（含各自的隐藏集合），编辑器把存储值覆盖在上面展示。
        auto_views = []
        for tab in auto_view_tabs(snapshot):
            storage_id = tab["view_id"] or "default"
            synthesized = synthesize_auto_view(snapshot, tab["view_id"]) or {
                "hidden_nodes": default_hidden_nodes(snapshot)
            }
            auto_views.append({
                "storage_id": storage_id,
                "view_id": tab["view_id"],
                "name": tab["name"],
                "layout": tab["layout"],
                "hidden_nodes": synthesized["hidden_nodes"],
            })
        return {
            "application_id": application_id,
            "nodes": nodes,
            "default_hidden_nodes": default_hidden_nodes(snapshot),
            "auto_views": auto_views,
            "views": await services.workflow_store.list_views(application_id),
        }

    @app.put(
        "/api/v1/applications/{application_id}/views/{view_id}",
        dependencies=[Depends(require_token)],
    )
    async def upsert_application_view(
        application_id: str, view_id: str, body: ViewUpsertRequest
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", view_id):
            raise HTTPException(422, "视图标识只能用小写字母、数字、中横线或下划线（40 字以内）")
        try:
            return await services.workflow_store.upsert_view(
                application_id,
                view_id,
                name=body.name,
                layout=body.layout,
                hidden_nodes=body.hidden_nodes,
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.delete(
        "/api/v1/applications/{application_id}/views/{view_id}",
        dependencies=[Depends(require_token)],
    )
    async def delete_application_view(application_id: str, view_id: str) -> dict[str, str]:
        await services.workflow_store.delete_view(application_id, view_id)
        return {"status": "deleted"}

    async def _resolve_use_view(
        application_id: str, view_id: str, snapshot: Any = None
    ) -> dict[str, Any] | None:
        """找界面方案：存储的优先（业主命名/调整过），其次自动合成（极简/对话），
        都没有 → None（管理界面，自动推导）。"""

        if view_id:
            stored = await services.workflow_store.get_view(application_id, view_id)
            if stored:
                return stored
            synthesized = synthesize_auto_view(snapshot, view_id)
            if synthesized:
                return synthesized
        return await services.workflow_store.get_view(application_id, "default")

    @app.get("/api/v1/use/{application_id}/definition")
    async def use_definition(
        application_id: str, code: str = "", view: str = ""
    ) -> dict[str, Any]:
        await _require_use_access(application_id, code)
        try:
            definition = await customer_runtime_definition(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        application = await services.workflow_store.get_application(application_id)
        definition["application_name"] = application.get("name", "")
        snapshot = definition.get("snapshot")
        definition["view"] = project_view_definition(
            snapshot, await _resolve_use_view(application_id, view, snapshot)
        )
        # WaaS：一个服务一个入口，界面在服务里切换。
        # 标签栏 = 自动生成的一组界面（管理/极简/对话）+ 业主命名的界面；
        # 业主用同名标识（default/auto-simple/auto-chat）可覆盖自动界面。
        stored_views = await services.workflow_store.list_views(application_id)
        stored_by_id = {item["view_id"]: item for item in stored_views}
        tabs: list[dict[str, Any]] = []
        auto_ids: set[str] = set()
        for auto_tab in auto_view_tabs(snapshot):
            storage_id = auto_tab["view_id"] or "default"
            auto_ids.add(storage_id)
            stored = stored_by_id.get(storage_id)
            tabs.append({
                "view_id": auto_tab["view_id"],
                "name": stored["name"] if stored else auto_tab["name"],
                "layout": resolve_view_layout(
                    snapshot, stored["layout"] if stored else auto_tab["layout"]
                ),
            })
        tabs.extend(
            {
                "view_id": item["view_id"],
                "name": item["name"],
                "layout": resolve_view_layout(snapshot, item["layout"]),
            }
            for item in stored_views
            if item["view_id"] not in auto_ids
        )
        definition["views"] = tabs
        report = acceptance_pm.load_report(services.settings.data_dir, application_id)
        if report:
            definition["acceptance"] = {
                "accepted": bool(report.get("accepted")),
                "stamp": report.get("stamp"),
                "passed_cases": report.get("passed_cases"),
                "total_cases": len(report.get("cases") or []),
            }
        return definition

    @app.post("/api/v1/use/{application_id}/runs", status_code=202)
    async def use_create_run(
        application_id: str, body: WorkflowRunRequest, code: str = ""
    ) -> dict[str, Any]:
        await _require_use_access(application_id, code)
        try:
            body.use_draft = False
            return await services.workflow_runtime.create_run(application_id, body)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, RuntimeError) as error:
            raise HTTPException(422, str(error)) from error

    async def _use_run(application_id: str, run_id: str) -> dict[str, Any]:
        try:
            run = await services.workflow_store.get_run(run_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        if str(run.get("application_id")) != application_id:
            raise HTTPException(404, "这个运行不属于该应用")
        return run

    @app.get("/api/v1/use/{application_id}/runs/{run_id}")
    async def use_get_run(
        application_id: str, run_id: str, code: str = "", view: str = ""
    ) -> dict[str, Any]:
        await _require_use_access(application_id, code)
        run = await _use_run(application_id, run_id)
        state = run.get("state")
        snapshot = (
            getattr(state, "snapshot", None)
            if not isinstance(state, dict)
            else state.get("snapshot")
        )
        return project_view_run(
            run, await _resolve_use_view(application_id, view, snapshot)
        )

    @app.post("/api/v1/use/{application_id}/runs/{run_id}/resume")
    async def use_resume_run(
        application_id: str, run_id: str, body: ResumeRunRequest,
        code: str = "", view: str = "",
    ) -> dict[str, Any]:
        await _require_use_access(application_id, code)
        await _use_run(application_id, run_id)
        try:
            # runtime.resume 返回的是命令回执 {"run_id":…, "status":"queued"}，
            # 不是运行记录——直接投影会产出 id=""、空快照、created_at=null，
            # 还少了 stages/view 两个键，业主页拿到的对象与 GET 不是同一形状。
            await services.workflow_runtime.resume(run_id, body.values)
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(422, str(error)) from error
        run = await _use_run(application_id, run_id)
        state = run.get("state")
        snapshot = (
            getattr(state, "snapshot", None)
            if not isinstance(state, dict)
            else state.get("snapshot")
        )
        return project_view_run(
            run, await _resolve_use_view(application_id, view, snapshot)
        )

    @app.post("/api/v1/use/{application_id}/parse-table")
    async def use_parse_table(
        application_id: str, body: UseTableRequest, code: str = ""
    ) -> dict[str, Any]:
        await _require_use_access(application_id, code)
        from .table_intake import TableIntakeError, parse_table

        try:
            data = base64.b64decode(body.content_base64) if body.content_base64 else None
            return parse_table(body.filename, data=data, text=body.text)
        except TableIntakeError as error:
            raise HTTPException(422, str(error)) from error

    def _use_artifact_dir(run_id: str) -> Path:
        # 运行结束后工作区边界已释放，这里做无状态解析：
        # 运行期的产物落在 workspace_root 下（沙盒边界），旧数据可能在进程 CWD。
        candidates = [
            Path(services.settings.workspace_root) / ".workflow-run-artifacts" / run_id,
            Path.cwd() / ".workflow-run-artifacts" / run_id,
        ]
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        raise FileNotFoundError(run_id)

    @app.get("/api/v1/scheduler/health", dependencies=[Depends(require_token)])
    async def scheduler_health() -> dict[str, Any]:
        """调度器还在不在：体检报「定时没按时开火」时，这是下一步要查的事实。"""
        scheduler = getattr(services, "scheduler", None)
        if scheduler is None or not hasattr(scheduler, "health"):
            return {"running": False, "alive": False, "detail": "本实例没有调度器"}
        return scheduler.health()

    @app.post("/api/v1/applications/{application_id}/archive",
              dependencies=[Depends(require_token)])
    async def archive_application(application_id: str, archived: bool = True) -> dict[str, Any]:
        """从列表里收起来（或拿回来）。数据一条不删。"""
        try:
            return await services.workflow_store.set_archived(application_id, archived)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/v1/applications-archived", dependencies=[Depends(require_token)])
    async def list_archived_applications() -> dict[str, Any]:
        """已收起来的。归档是可逆操作，得看得见才敢按。"""
        items = await services.workflow_store.list_archived()
        return {"total": len(items), "items": items}

    @app.get("/api/v1/applications-archivable", dependencies=[Depends(require_token)])
    async def list_archivable(
        days_idle: int = Query(default=7, ge=0, le=3650),
    ) -> dict[str, Any]:
        """建议可以收起来的：从没发布、从没成功跑过、且放了一阵子的草稿。"""
        items = await services.workflow_store.list_archivable(days_idle=days_idle)
        return {"days_idle": days_idle, "total": len(items), "items": items}

    @app.get("/api/v1/health-report", dependencies=[Depends(require_token)])
    async def workflow_health_report(
        days: int = Query(default=7, ge=1, le=90),
    ) -> dict[str, Any]:
        """工作流健康度：哪些已发布工作流悄悄坏了（全败/连败/定时没触发）。"""
        from .overview import build_health

        return await build_health(services, days=days)

    @app.get("/api/v1/runs/{run_id}/artifacts", dependencies=[Depends(require_token)])
    async def list_run_artifacts(run_id: str) -> list[dict[str, Any]]:
        """令牌通道的产物列表（guanjia 客户端用）；无产物目录返回空表。"""
        try:
            await services.workflow_store.get_run(run_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        try:
            folder = await asyncio.to_thread(_use_artifact_dir, run_id)
        except Exception:  # noqa: BLE001 - 没有产物目录是常态
            return []
        return [
            {"name": str(path.relative_to(folder)), "size": path.stat().st_size}
            for path in sorted(folder.rglob("*")) if path.is_file()
        ]

    @app.get("/api/v1/runs/{run_id}/artifacts/{artifact_path:path}",
             dependencies=[Depends(require_token)])
    async def download_run_artifact(run_id: str, artifact_path: str) -> Any:
        from fastapi.responses import FileResponse

        try:
            await services.workflow_store.get_run(run_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        try:
            folder = await asyncio.to_thread(_use_artifact_dir, run_id)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(404, "文件不存在") from error
        target = (folder / artifact_path).resolve()
        if folder.resolve() not in target.parents and target != folder.resolve():
            raise HTTPException(404, "文件不存在")
        if not target.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(target, filename=target.name)

    @app.get("/api/v1/use/{application_id}/runs/{run_id}/artifacts")
    async def use_list_artifacts(
        application_id: str, run_id: str, code: str = ""
    ) -> list[dict[str, Any]]:
        await _require_use_access(application_id, code)
        await _use_run(application_id, run_id)
        try:
            folder = await asyncio.to_thread(_use_artifact_dir, run_id)
        except Exception:
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                items.append({
                    "name": str(path.relative_to(folder)),
                    "size": path.stat().st_size,
                })
        return items

    @app.get("/api/v1/use/{application_id}/runs/{run_id}/artifacts/{artifact_path:path}")
    async def use_download_artifact(
        application_id: str, run_id: str, artifact_path: str, code: str = ""
    ) -> Any:
        from fastapi.responses import FileResponse

        await _require_use_access(application_id, code)
        await _use_run(application_id, run_id)
        folder = await asyncio.to_thread(_use_artifact_dir, run_id)
        target = (folder / artifact_path).resolve()
        if folder.resolve() not in target.parents and target != folder.resolve():
            raise HTTPException(404, "文件不存在")
        if not target.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(target, filename=target.name)

    # ── 监理（按需受邀的第二个智能体）：出卷 / 监考 / 解释 / 巡查 ──

    @app.post(
        "/api/v1/applications/{application_id}/acceptance/spec",
        dependencies=[Depends(require_token)],
    )
    async def create_acceptance_spec(
        application_id: str, body: AcceptanceInterviewRequest
    ) -> dict[str, Any]:
        try:
            application = await services.workflow_store.get_application(application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        try:
            spec = await acceptance_pm.generate_spec(services, application, body.examples)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(422, f"监理出卷失败：{error}") from error
        acceptance_pm.save_spec(services.settings.data_dir, application_id, spec)
        return spec.model_dump(mode="json")

    @app.get(
        "/api/v1/applications/{application_id}/acceptance/spec",
        dependencies=[Depends(require_token)],
    )
    async def get_acceptance_spec(application_id: str) -> dict[str, Any]:
        spec = acceptance_pm.load_spec(services.settings.data_dir, application_id)
        if spec is None:
            raise HTTPException(404, "还没有验收方案")
        return spec.model_dump(mode="json")

    @app.post(
        "/api/v1/applications/{application_id}/acceptance/run",
        dependencies=[Depends(require_token)],
    )
    async def run_application_acceptance(application_id: str) -> dict[str, Any]:
        try:
            report = await acceptance_pm.run_acceptance(services, application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        report["markdown"] = acceptance_pm.render_report_markdown(report)
        return report

    @app.get(
        "/api/v1/applications/{application_id}/acceptance/report",
        dependencies=[Depends(require_token)],
    )
    async def get_acceptance_report(application_id: str) -> dict[str, Any]:
        report = acceptance_pm.load_report(services.settings.data_dir, application_id)
        if report is None:
            raise HTTPException(404, "还没有验收单")
        report["markdown"] = acceptance_pm.render_report_markdown(report)
        return report

    @app.get("/api/v1/pm/lessons", dependencies=[Depends(require_token)])
    async def get_pm_lessons() -> dict[str, Any]:
        return {"lessons": acceptance_pm.load_lessons(services.settings.data_dir)}

    @app.post("/api/v1/pm/lessons", dependencies=[Depends(require_token)])
    async def add_pm_lesson(body: BuildMessageRequest) -> dict[str, Any]:
        try:
            lessons = acceptance_pm.append_lesson(services.settings.data_dir, body.message)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"lessons": lessons}

    @app.post(
        "/api/v1/applications/{application_id}/pm/explain",
        dependencies=[Depends(require_token)],
    )
    async def pm_explain(application_id: str, body: OwnerExplainRequest) -> dict[str, Any]:
        try:
            text = await acceptance_pm.explain_for_owner(
                services, application_id, body.question
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        return {"application_id": application_id, "explanation": text}

    @app.post(
        "/api/v1/applications/{application_id}/pm/review",
        dependencies=[Depends(require_token)],
    )
    async def pm_review(application_id: str) -> dict[str, Any]:
        try:
            notes = await acceptance_pm.review_progress(services, application_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        return {"application_id": application_id, "notes": notes}

    def _hash_password(password: str, salt: bytes | None = None) -> str:
        import hashlib as _h
        import os as _os
        salt = salt or _os.urandom(16)
        digest = _h.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return f"pbkdf2${salt.hex()}${digest.hex()}"

    def _verify_password(password: str, stored: str | None) -> bool:
        import hashlib as _h
        import hmac as _hmac
        try:
            _, salt_hex, digest_hex = (stored or "").split("$")
        except ValueError:
            return False
        digest = _h.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 200_000)
        return _hmac.compare_digest(digest.hex(), digest_hex)

    def _issue_token() -> tuple[str, str]:
        import hashlib as _h
        import secrets as _s
        token = f"lil_{_s.token_urlsafe(24)}"
        return token, _h.sha256(token.encode("utf-8")).hexdigest()

    @app.post("/api/v1/auth/register")
    async def auth_register(body: dict[str, Any]) -> dict[str, Any]:
        """注册 = 共享注册令牌 + 自己起用户名密码。首个注册者自动成为管理员。"""

        if str(body.get("register_token") or "") != settings.api_token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "注册令牌不正确")
        name = str(body.get("name") or "").strip()
        password = str(body.get("password") or "")
        if not (1 <= len(name) <= 40):
            raise HTTPException(400, "用户名需 1-40 字")
        if len(password) < 6:
            raise HTTPException(400, "密码至少 6 位")
        if await services.storage.user_by_name(name):
            raise HTTPException(409, "用户名已存在")
        role = "admin" if await services.storage.count_users() == 0 else "member"
        token, token_hash = _issue_token()
        user = await services.storage.create_user(
            name, token_hash, role, password_hash=_hash_password(password)
        )
        await services.storage.append_event("system", "user.registered", {"name": name, "role": role})
        return {"user": {k: user[k] for k in ("id", "name", "role", "status")}, "token": token}

    @app.post("/api/v1/auth/login")
    async def auth_login(body: dict[str, Any]) -> dict[str, Any]:
        """登录换会话令牌；每次登录轮换（旧令牌即刻失效）。"""

        name = str(body.get("name") or "").strip()
        user = await services.storage.user_by_name(name)
        if not user or user.get("status") != "active" or not _verify_password(
            str(body.get("password") or ""), user.get("password_hash")
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码不正确")
        token, token_hash = _issue_token()
        await services.storage.rotate_user_token(user["id"], token_hash)
        await services.storage.append_event("system", "user.logged_in", {"name": name})
        return {"user": {k: user[k] for k in ("id", "name", "role", "status")}, "token": token}

    @app.get("/api/v1/me", dependencies=[Depends(require_token)])
    async def whoami(request: Request) -> dict[str, Any]:
        return {"user": _current_user(request)}

    @app.post("/api/v1/users", dependencies=[Depends(require_token)])
    async def create_user(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        _require_admin(request)
        import hashlib
        import secrets
        name = str(body.get("name") or "").strip()
        if not (1 <= len(name) <= 40):
            raise HTTPException(400, "name required (1-40 chars)")
        role = "admin" if body.get("role") == "admin" else "member"
        token = f"lil_{secrets.token_urlsafe(24)}"
        try:
            user = await services.storage.create_user(
                name, hashlib.sha256(token.encode("utf-8")).hexdigest(), role
            )
        except Exception as error:
            raise HTTPException(409, f"user exists or invalid: {error}") from error
        await services.storage.append_event("system", "user.created", {"name": name, "role": role})
        # 令牌只在创建响应里出现一次，服务端只存哈希
        return {"user": {k: user[k] for k in ("id", "name", "role", "status")}, "token": token}

    @app.get("/api/v1/users", dependencies=[Depends(require_token)])
    async def list_users(request: Request) -> list[dict[str, Any]]:
        _require_admin(request)
        return await services.storage.list_users()

    @app.post("/api/v1/users/{user_id}/status", dependencies=[Depends(require_token)])
    async def set_user_status(request: Request, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        _require_admin(request)
        status_value = "disabled" if body.get("status") == "disabled" else "active"
        await services.storage.set_user_status(user_id, status_value)
        await services.storage.append_event("system", "user.status_changed", {"user_id": user_id, "status": status_value})
        return {"ok": True, "status": status_value}

    @app.get("/api/v1/overview", dependencies=[Depends(require_token)])
    async def platform_overview() -> dict[str, Any]:
        from .overview import build_overview

        return await build_overview(services)

    @app.post("/api/v1/assistant/agent", dependencies=[Depends(require_token)])
    async def assistant_agent_chat(request: Request, body: AssistantChatRequest) -> dict[str, Any]:
        """管家智能体：工具在服务端执行（列表/运行/生成/统筹），CLI 只做薄 REPL。"""

        from .assistant_agent import WorkflowConcierge

        concierge = WorkflowConcierge(services, settings)
        actions, text = await concierge.reply(body.messages, _current_user(request))
        return {"actions": actions, "text": text}

    @app.post("/api/v1/assistant/agent/stream", dependencies=[Depends(require_token)])
    async def assistant_agent_stream(request: Request, body: AssistantChatRequest):
        """管家 SSE 流：delta（文字）/ action（工具动作）/ final。CLI 实时渲染用。"""

        from fastapi.responses import StreamingResponse

        from .assistant_agent import WorkflowConcierge

        queue: asyncio.Queue = asyncio.Queue()
        concierge = WorkflowConcierge(services, settings)
        user = _current_user(request)

        async def emit(event: dict) -> None:
            await queue.put(event)

        async def worker() -> None:
            try:
                await concierge.reply(body.messages, user, emit=emit)
            except Exception as error:  # noqa: BLE001 - 错误也走流内呈现
                await queue.put({"type": "error", "text": str(error)[:300]})
            await queue.put(None)

        task = asyncio.create_task(worker())

        async def gen():
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                task.cancel()

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/v1/assistant/chat", dependencies=[Depends(require_token)])
    async def assistant_chat(body: AssistantChatRequest) -> dict[str, Any]:
        from .agent_core import collect_model_stream

        chat_messages = [
            ChatMessage(
                role="assistant" if item.get("role") == "assistant" else "user",
                content=[ContentBlock(type="text", text=str(item.get("text", ""))[:8_000])],
            )
            for item in body.messages
        ]
        stream = services.provider.stream(
            model=settings.deepseek_runtime_model,
            system=body.system or "你是简洁可靠的中文助手，不知道就说不知道。",
            messages=chat_messages,
            tools=[],
            max_output_tokens=2_048,
            thinking_enabled=False,
            effort="low",
            tool_choice=None,
        )
        response = await collect_model_stream(stream, model=settings.deepseek_runtime_model)
        text = " ".join(block.text or "" for block in response.blocks if block.type == "text").strip()
        return {
            "text": text,
            "model": settings.deepseek_runtime_model,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": response.usage.cache_read_input_tokens,
            },
        }

    @app.post("/api/v1/builds/{build_id}/messages", dependencies=[Depends(require_token)])
    async def post_build_message(build_id: str, body: BuildMessageRequest) -> dict[str, Any]:
        """Deliver an owner note into a running build; the Builder reads it at its next turn."""

        try:
            build = await services.workflow_store.get_build(build_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        if build["status"] not in {"queued", "building"}:
            raise HTTPException(409, f"build is {build['status']} — use /resume to continue it")
        try:
            services.builders.for_build(build).post_live_message(build_id, body.message)
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        await asyncio.to_thread(
            services.build_transcripts.append,
            build_id,
            owner_record(text=body.message, draft_revision=build["team_state"].revision),
        )
        return {"build_id": build_id, "status": build["status"], "delivered": True}

    @app.post("/api/v1/builds/{build_id}/resume", dependencies=[Depends(require_token)])
    async def resume_build(build_id: str, body: ResumeBuildRequest | None = None) -> dict[str, Any]:
        try:
            build = await services.workflow_store.get_build(build_id)
            if build["status"] not in {"needs_attention", "cancelled", "ready", "published"}:
                raise HTTPException(409, f"build cannot resume from {build['status']}")
            if body and body.message:
                services.builders.for_build(build).queue_resume_message(build_id, body.message)
                await asyncio.to_thread(
                    services.build_transcripts.append,
                    build_id,
                    owner_record(text=body.message, draft_revision=build["team_state"].revision),
                )
            await services.workflow_store.update_build(build_id, status="queued", error="")
            services.builders.for_build(build).start(build_id)
            return {"build_id": build_id, "status": "queued"}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/v1/builds/{build_id}/cancel", dependencies=[Depends(require_token)])
    async def cancel_build(build_id: str) -> dict[str, Any]:
        try:
            build = await services.workflow_store.get_build(build_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        try:
            services.builders.for_build(build).cancel(build_id)
            return {"build_id": build_id, "status": "cancelling"}
        except KeyError:
            # 没在跑不等于取消不了：停下来等业主、或进程重启后成了孤儿的构建，
            # 内存里都没有任务。cancel 的语义是「我不要这个了」，
            # 跟它此刻有没有在跑无关——否则这类构建永远没有出口。
            if build["status"] in TERMINAL_BUILD_STATUSES:
                return {"build_id": build_id, "status": build["status"],
                        "note": "这个构建早就结束了，无需取消"}
            await services.workflow_store.update_build(
                build_id, status="cancelled", error="")
            return {"build_id": build_id, "status": "cancelled"}

    @app.post(
        "/api/v1/applications/{application_id}/tests/run",
        dependencies=[Depends(require_token)],
    )
    async def run_application_tests(
        application_id: str,
        body: WorkflowTestSuiteRequest | None = None,
    ) -> dict[str, Any]:
        try:
            request = body or WorkflowTestSuiteRequest()
            return await services.workflow_runtime.run_test_suite(
                application_id,
                workspace_path=request.workspace_path,
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

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


    async def customer_runtime_definition(application_id: str) -> dict[str, Any]:
        application = await services.workflow_store.get_application(application_id)
        active_version = application.get("active_version")
        if active_version is not None:
            published = await services.workflow_store.get_version(
                application_id,
                int(active_version),
            )
            definition = {
                "application_id": application_id,
                "source": "published",
                "version": int(published["version"]),
                "draft_revision": None,
                "content_hash": published["content_hash"],
                "snapshot": published["snapshot"],
            }
        else:
            draft = await services.workflow_store.get_draft(application_id)
            definition = {
                "application_id": application_id,
                "source": "draft",
                "version": None,
                "draft_revision": int(draft["revision"]),
                "content_hash": draft["content_hash"],
                "snapshot": draft["snapshot"],
            }
        return project_runtime_definition(definition)

    @app.get(
        "/api/v1/customer-runtime/applications/{application_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_customer_runtime_application(application_id: str) -> dict[str, Any]:
        """Return the complete Customer Runtime read model without engineering data."""

        try:
            application = await services.workflow_store.get_application(application_id)
            definition = await customer_runtime_definition(application_id)
            runs = await services.workflow_store.list_runs(application_id, limit=1)
            latest_run = runs[0] if runs else None
            events = (
                await services.storage.list_events(str(latest_run["id"]))
                if latest_run is not None
                else []
            )
            return {
                "application": project_runtime_application(application),
                "definition": definition,
                "latest_run": (project_runtime_run(latest_run) if latest_run is not None else None),
                "latest_events": project_runtime_events(events),
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get(
        "/api/v1/customer-runtime/runs/{run_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_customer_runtime_run(run_id: str) -> dict[str, Any]:
        """Return a run projection that cannot expose raw model or developer events."""

        try:
            run = await services.workflow_store.get_run(run_id)
            events = await services.storage.list_events(run_id)
            return {
                "run": project_runtime_run(run),
                "events": project_runtime_events(events),
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
                run.setdefault("triggered_by", "")
            return runs
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/api/v1/applications/{application_id}/runs",
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    async def create_workflow_run(
        application_id: str, body: WorkflowRunRequest, request: Request
    ) -> dict[str, Any]:
        try:
            # 归因：多用户下所有运行长得一样，出了问题不知道是谁触发的
            who = str((getattr(request.state, "user", None) or {}).get("name") or "")
            return await services.workflow_runtime.create_run(
                application_id, body, triggered_by=who)
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
            run.setdefault("triggered_by", "")
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
        await services.storage.create_generation(
            generation_id, body.requirement, body.workspace_path
        )
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
            spec, resolved_version, version_status = await services.storage.get_agent(
                agent_id, version
            )
            return {
                "version": resolved_version,
                "status": version_status,
                "spec": spec.model_dump(mode="json"),
            }
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.post(
        "/v1/agents/{agent_id}/versions/{version}/publish", dependencies=[Depends(require_token)]
    )
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

    @app.post(
        "/v1/sessions/{session_id}/messages", status_code=202, dependencies=[Depends(require_token)]
    )
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

    @app.get("/api/v1/runs/{run_id}/events/list", dependencies=[Depends(require_token)])
    async def run_events_list(
        run_id: str,
        after: int = 0,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        """有界事件列表：给客户端画已结束运行的时间线（SSE 版对完结流不收尾）。"""
        try:
            await services.workflow_store.get_run(run_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        events = await services.storage.list_events(run_id, after)
        # 截尾部而不是头部：一次运行可能有上千条事件（模型逐 token 的 *.delta），
        # 取最前 limit 条的话 workflow.completed/failed 永远在窗口外，
        # 客户端画出来的时间线永远"没有结局"。
        truncated = len(events) > limit
        window = events[-limit:] if truncated else events
        return {"run_id": run_id, "total": len(events), "truncated": truncated,
                "events": [
                    {"id": e.id, "type": e.type, "at": e.created_at, "data": e.data}
                    for e in window
                ]}

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
                    print(
                        f"[auto-extract] Build {build_id[:8]}: merged into {sim.target_template} v{merged.meta.version} conf={merged.meta.confidence}"
                    )
            elif all(not t.name.startswith("build-") for t in services.templates.list()):
                # Register as new template if no obvious match
                tname = f"build-extracted-{build_id[:8]}"
                services.templates.register(
                    tname,
                    wf,
                    meta_overrides={
                        "title": f"Extracted: {requirement[:50]}",
                        "category": "task_management",
                        "tags": node_types[:5],
                        "author": "auto-extract",
                    },
                )
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
        return (
            FileResponse(p, media_type="image/png")
            if p.exists()
            else HTMLResponse("", status_code=404)
        )

    @app.get("/dingtalk-icon-512.png")
    async def dingtalk_icon_512():
        from fastapi.responses import FileResponse

        p = _pwa_dir / "dingtalk-icon-512.png"
        return (
            FileResponse(p, media_type="image/png")
            if p.exists()
            else HTMLResponse("", status_code=404)
        )

    @app.get("/manifest.json")
    async def dingtalk_manifest():
        from fastapi.responses import FileResponse

        p = _pwa_dir / "manifest.json"
        return (
            FileResponse(p, media_type="application/json")
            if p.exists()
            else HTMLResponse("", status_code=404)
        )

    @app.get("/sw.js")
    async def dingtalk_sw():
        from fastapi.responses import FileResponse

        p = _pwa_dir / "sw.js"
        return (
            FileResponse(p, media_type="application/javascript")
            if p.exists()
            else HTMLResponse("", status_code=404)
        )

    # ── Dashboard ────────────────────────────────────────────

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> str:
        p = _pwa_dir / "dashboard.html"
        return p.read_text(encoding="utf-8") if p.exists() else "<h1>Dashboard not found</h1>"

    # ── Debug ────────────────────────────────────────────────

    @app.get("/debug", response_class=HTMLResponse)
    async def debug_page() -> str:
        return DEBUG_HTML











    def validate_collaborative_development_user_token(supplied: str) -> bool:
        return hmac.compare_digest(supplied, settings.api_token)


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
