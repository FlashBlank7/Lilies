from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
from collections import defaultdict, deque
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .applications import ApplicationService
from .blocks import (
    AgentArchitectureConfig,
    AnswerConfig,
    BlockRegistry,
    ClaudeAgentConfig,
    ClassifierConfig,
    CollectionDigestConfig,
    ConnectorActionConfig,
    Condition,
    DeployedModelInferenceConfig,
    DeployedForecastConfig,
    EndConfig,
    EventSubscriptionTriggerConfig,
    HTTPConfig,
    HumanInputConfig,
    IfElseConfig,
    IterationConfig,
    JsonSchemaValidateConfig,
    LLMConfig,
    LoopConfig,
    ModelDriftMonitorConfig,
    ParameterExtractorConfig,
    RecordCollectionNormalizeConfig,
    RecordDeduplicateConfig,
    RecordMatchConfig,
    ReplenishmentPlannerConfig,
    RegexExtractConfig,
    ScheduleTriggerConfig,
    StartConfig,
    TemplateConfig,
    ToolConfig,
    TypedJsonArtifactConfig,
    TypedWorkbookConfig,
    VariableAggregatorConfig,
    VariableAssignerConfig,
    WebCollectionConfig,
)
from .connector_sdk import ConnectorExecutionRequest, ConnectorService
from .event_automation import (
    DurableEventTimerConfig,
    DurableEventTimerRequest,
    EventAutomationService,
)
from .execution_policy import ExecutionPolicySnapshot
from .knowledge_rag import (
    GroundedAnswerConfig,
    KnowledgeIndexService,
    KnowledgeIndexSyncConfig,
    KnowledgeRetrievalConfig,
    KnowledgeRetrieveRequest,
    KnowledgeSyncRequest,
    grounded_answer,
)
from .models import AgentSpec, ChatMessage, ContentBlock, PermissionMode, Usage
from .governed_memory import GovernedMemoryPermission, GovernedMemorySurface, GovernedMemoryViolation
from .platform_harness import PlatformHarness
from .providers import ModelProvider
from .record_pipeline import (
    deduplicate_records,
    extract_regex_fields,
    match_record,
    match_records,
    normalize_record_collection,
    validate_json_value,
    write_typed_json_artifact,
)
from .runtime import AgentRuntime
from .sandbox import SandboxManager
from .storage import Storage
from .tabular_models import (
    ModelObservation,
    TabularDriftRequest,
    TabularInferenceRequest,
    TabularModelService,
)
from .forecast_models import (
    ForecastInferenceRequest,
    ForecastModelService,
    ForecastSeries,
)
from .replenishment import ReplenishmentPlanRequest, solve_replenishment
from .tools import ToolContext, ToolRegistry
from .typed_workbook import write_typed_workbook_artifact
from .workflow_models import (
    ApplicationSnapshot,
    ErrorStrategy,
    NodeSpec,
    WorkflowRunRequest,
    WorkflowRunState,
    WorkflowSpec,
    WorkflowTestCase,
)
from .workflow_storage import WorkflowStorage
from .web_collection import ControlledWebCollector


class HumanInputPause(RuntimeError):
    pass


class WorkflowReferenceResolutionError(ValueError):
    """A required workflow value reference could not be resolved."""


class WorkflowWorkspaceBoundaryViolation(ValueError):
    """A trusted execution policy rejected a workflow-selected workspace."""


class NestedWorkflowScopeDenied(ValueError):
    """A workflow attempted to call an application outside its run policy."""


class NestedWorkflowCycleDenied(ValueError):
    """A nested workflow call would create an application cycle."""


class NestedWorkflowDepthExceeded(ValueError):
    """A nested workflow call exceeded the persisted application depth."""


class WorkflowRuntimeToolScopeDenied(ValueError):
    """A workflow attempted to execute a runtime tool outside its run policy."""


class WorkflowRuntimeNetworkScopeDenied(ValueError):
    """A workflow attempted network access outside its run policy."""


class WorkflowRuntimeSecretScopeDenied(ValueError):
    """A workflow attempted to resolve a secret outside its run policy."""


class WorkflowRuntimeModelScopeDenied(ValueError):
    """A workflow attempted model execution while model access was disabled."""


class WorkflowRuntimeConnectorScopeDenied(ValueError):
    """A workflow attempted a connector operation outside its task policy."""


class WorkflowRuntimePermissionScopeDenied(ValueError):
    """A connector operation lacked its task-required authorization receipt."""


class WorkflowRuntimeWriteLimitExceeded(ValueError):
    """A workflow exceeded its frozen host write count."""


class WorkflowRuntimePayloadLimitExceeded(ValueError):
    """A workflow connector payload exceeded its frozen byte limit."""


BLACKBOX_RUNTIME_TOOL_ALLOWLIST = frozenset({"Edit", "Glob", "Grep", "Read", "Write"})
BLACKBOX_RUNTIME_NETWORK_ALLOWLIST: frozenset[str] = frozenset()
MAX_NESTED_WORKFLOW_DEPTH = 16


TEST_SUITE_MAX_CONCURRENCY = 4


class WorkflowHTTPError(RuntimeError):
    """HTTP response failure with enough structure for safe retry policy."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code


@dataclass(slots=True)
class NodeExecutionError(RuntimeError):
    node_id: str
    cause: Exception

    def __str__(self) -> str:
        return f"node {self.node_id} failed: {self.cause}"


class WorkflowRuntime:
    def __init__(
        self,
        *,
        storage: Storage,
        workflow_store: WorkflowStorage,
        harness: PlatformHarness,
        applications: ApplicationService,
        blocks: BlockRegistry,
        provider: ModelProvider,
        agent_runtime: AgentRuntime,
        tools: ToolRegistry,
        sandboxes: SandboxManager,
        runtime_model: str,
        governed_memory: GovernedMemorySurface | None = None,
        web_collector: ControlledWebCollector | None = None,
        connector_service: ConnectorService | None = None,
        tabular_models: TabularModelService | None = None,
        forecast_models: ForecastModelService | None = None,
        knowledge_indexes: KnowledgeIndexService | None = None,
        event_automation: EventAutomationService | None = None,
    ) -> None:
        self.storage = storage
        self.workflow_store = workflow_store
        self.harness = harness
        self.applications = applications
        self.blocks = blocks
        self.provider = provider
        self.agent_runtime = agent_runtime
        self.tools = tools
        self.sandboxes = sandboxes
        self.runtime_model = runtime_model
        self.governed_memory = governed_memory
        self.web_collector = web_collector
        self.connector_service = connector_service
        self.tabular_models = tabular_models
        self.forecast_models = forecast_models
        self.knowledge_indexes = knowledge_indexes
        self.event_automation = event_automation
        self.active_tasks: dict[str, asyncio.Task[None]] = {}
        self._workspace_boundaries: dict[str, Path] = {}
        self._nested_application_allowlists: dict[str, frozenset[str]] = {}
        self._runtime_tool_allowlists: dict[str, frozenset[str]] = {}
        self._network_host_allowlists: dict[str, frozenset[str]] = {}
        self._model_access_policies: dict[str, bool] = {}
        self._connector_operation_allowlists: dict[str, frozenset[str]] = {}
        self._writable_connector_operations: dict[str, frozenset[str]] = {}
        self._permission_connector_operations: dict[str, frozenset[str]] = {}
        self._compensation_connector_operations: dict[str, frozenset[str]] = {}

    async def create_run(
        self,
        application_id: str,
        request: WorkflowRunRequest,
        *,
        parent_task_id: str | None = None,
        origin: str = "api",
        workspace_boundary: str | None = None,
        allowed_nested_application_ids: Collection[str] | None = None,
        allowed_runtime_tools: Collection[str] | None = None,
        allowed_network_hosts: Collection[str] | None = None,
        model_access: bool | None = None,
        allowed_connector_operations: Collection[str] | None = None,
        writable_connector_operations: Collection[str] | None = None,
        permission_required_connector_operations: Collection[str] | None = None,
        compensation_connector_operations: Collection[str] | None = None,
        max_connector_write_count: int | None = None,
        max_connector_payload_bytes: int | None = None,
        governed_host_actions: bool = False,
        assignment_id: str | None = None,
        session_id: str | None = None,
        connector_descriptor_digests: dict[str, str] | None = None,
        task_credential_ref_digest: str | None = None,
        task_policy_digest: str | None = None,
        allowed_actions_digest: str | None = None,
        budget_digest: str | None = None,
        task_deadline_at: str | None = None,
        application_call_chain: Collection[str] | None = None,
        simulated_human_inputs: dict[str, dict[str, Any]] | None = None,
        allow_published_authority_rebind: bool = False,
    ) -> dict[str, Any]:
        ancestor_chain = [str(value) for value in (application_call_chain or ())]
        if application_id in ancestor_chain:
            raise NestedWorkflowCycleDenied(
                "nested workflow application cycle is outside the execution policy"
            )
        if len(ancestor_chain) >= MAX_NESTED_WORKFLOW_DEPTH:
            raise NestedWorkflowDepthExceeded(
                "nested workflow application depth exceeds the execution policy"
            )
        current_call_chain = [*ancestor_chain, application_id]
        published_policy: ExecutionPolicySnapshot | None = None
        effective_policy: ExecutionPolicySnapshot | None = None
        if request.use_draft:
            draft = await self.workflow_store.get_draft(application_id)
            snapshot, version, draft_revision = draft["snapshot"], None, int(draft["revision"])
        else:
            published = await self.workflow_store.get_version(application_id, request.version)
            snapshot, version, draft_revision = published["snapshot"], int(published["version"]), None
            raw_policy = (published.get("publication_decision") or {}).get(
                "execution_policy_snapshot"
            )
            if raw_policy is not None:
                published_policy = ExecutionPolicySnapshot.model_validate(raw_policy)
                effective_policy = published_policy.constrained_by(
                    workspace_boundary=workspace_boundary,
                    assignment_id=assignment_id,
                    session_id=session_id,
                    allowed_nested_application_ids=allowed_nested_application_ids,
                    allowed_runtime_tools=allowed_runtime_tools,
                    allowed_network_hosts=allowed_network_hosts,
                    model_access=model_access,
                    allowed_connector_operations=allowed_connector_operations,
                    writable_connector_operations=writable_connector_operations,
                    permission_required_connector_operations=(
                        permission_required_connector_operations
                    ),
                    compensation_connector_operations=(
                        compensation_connector_operations
                    ),
                    max_connector_write_count=max_connector_write_count,
                    max_connector_payload_bytes=max_connector_payload_bytes,
                    governed_host_actions=governed_host_actions,
                    allow_authority_rebind=allow_published_authority_rebind,
                )
                workspace_boundary = effective_policy.workspace_boundary
                allowed_nested_application_ids = (
                    effective_policy.allowed_nested_application_ids
                )
                allowed_runtime_tools = effective_policy.allowed_runtime_tools
                allowed_network_hosts = effective_policy.allowed_network_hosts
                model_access = effective_policy.model_access
                allowed_connector_operations = (
                    effective_policy.allowed_connector_operations
                )
                writable_connector_operations = (
                    effective_policy.writable_connector_operations
                )
                permission_required_connector_operations = (
                    effective_policy.permission_required_connector_operations
                )
                compensation_connector_operations = (
                    effective_policy.compensation_connector_operations
                )
                max_connector_write_count = (
                    effective_policy.max_connector_write_count
                )
                max_connector_payload_bytes = (
                    effective_policy.max_connector_payload_bytes
                )
                governed_host_actions = effective_policy.governed_host_actions
                assignment_id = str(effective_policy.assignment_id)
                session_id = str(effective_policy.session_id)
        restricted = any(
            value is not None
            for value in (
                workspace_boundary,
                allowed_nested_application_ids,
                allowed_runtime_tools,
                allowed_network_hosts,
                model_access,
                allowed_connector_operations,
                writable_connector_operations,
                permission_required_connector_operations,
                compensation_connector_operations,
                max_connector_write_count,
                max_connector_payload_bytes,
                assignment_id,
                session_id,
            )
        )
        if restricted:
            self._validate_restricted_inputs(
                request.inputs,
                allowed_reserved_keys=(
                    frozenset({"__event_automation"})
                    if origin == "event_automation"
                    else frozenset()
                ),
            )
        errors = self.blocks.validate_workflow(snapshot.workflow)
        if errors:
            raise ValueError("invalid workflow: " + "; ".join(errors))
        resolved_boundary = (
            self.sandboxes.resolve_workspace(workspace_boundary).resolve()
            if workspace_boundary is not None
            else None
        )
        resolved_workspace = (
            self._resolve_scoped_workspace(request.workspace_path, resolved_boundary)
            if resolved_boundary is not None
            else self.sandboxes.resolve_workspace(request.workspace_path)
        )
        nested_allowlist = (
            frozenset(str(value) for value in allowed_nested_application_ids)
            if allowed_nested_application_ids is not None
            else None
        )
        runtime_tool_allowlist = (
            frozenset(str(value) for value in allowed_runtime_tools)
            if allowed_runtime_tools is not None
            else None
        )
        network_host_allowlist = (
            frozenset(str(value).casefold() for value in allowed_network_hosts)
            if allowed_network_hosts is not None
            else None
        )
        connector_allowlist = (
            frozenset(str(value) for value in allowed_connector_operations)
            if allowed_connector_operations is not None
            else None
        )
        writable_connector_allowlist = (
            frozenset(str(value) for value in writable_connector_operations)
            if writable_connector_operations is not None
            else None
        )
        permission_connector_allowlist = (
            frozenset(
                str(value)
                for value in permission_required_connector_operations
            )
            if permission_required_connector_operations is not None
            else None
        )
        compensation_connector_allowlist = (
            frozenset(str(value) for value in compensation_connector_operations)
            if compensation_connector_operations is not None
            else None
        )
        if connector_allowlist is not None and any(
            policy is not None and not policy.issubset(connector_allowlist)
            for policy in (
                writable_connector_allowlist,
                permission_connector_allowlist,
                compensation_connector_allowlist,
            )
        ):
            raise WorkflowRuntimeConnectorScopeDenied(
                "connector sub-policies exceed the assigned connector operation policy"
            )
        self._validate_execution_policy(
            snapshot.workflow,
            workspace_boundary=resolved_boundary,
            allowed_nested_application_ids=nested_allowlist,
            allowed_runtime_tools=runtime_tool_allowlist,
            allowed_network_hosts=network_host_allowlist,
            model_access=model_access,
            allowed_connector_operations=connector_allowlist,
            governed_host_actions=governed_host_actions,
            agents=snapshot.agents,
        )
        if governed_host_actions:
            if (
                self.connector_service is None
                or assignment_id is None
                or network_host_allowlist is None
                or max_connector_write_count is None
                or max_connector_payload_bytes is None
            ):
                raise WorkflowRuntimeConnectorScopeDenied(
                    "governed host-action policy is incomplete"
                )
            await self.connector_service.freeze_assignment_budget(
                assignment_id=assignment_id,
                allowed_network_hosts=sorted(network_host_allowlist),
                allowed_compensation_operations=sorted(
                    compensation_connector_allowlist or ()
                ),
                max_write_count=max_connector_write_count,
                max_payload_bytes=max_connector_payload_bytes,
            )
        run_id = str(uuid4())
        inputs = await self._inputs_with_governed_memory(
            application_id=application_id,
            inputs=request.inputs,
            run_id=run_id,
        )
        if simulated_human_inputs:
            inputs["__human__"] = simulated_human_inputs
        state = WorkflowRunState(
            run_id=run_id,
            application_id=application_id,
            snapshot=snapshot,
            inputs=inputs,
            workspace_path=str(resolved_workspace),
            workspace_boundary=(
                str(resolved_boundary) if resolved_boundary is not None else None
            ),
            allowed_nested_application_ids=(
                sorted(nested_allowlist) if nested_allowlist is not None else None
            ),
            allowed_runtime_tools=(
                sorted(runtime_tool_allowlist) if runtime_tool_allowlist is not None else None
            ),
            allowed_network_hosts=(
                sorted(network_host_allowlist) if network_host_allowlist is not None else None
            ),
            model_access=model_access,
            allowed_connector_operations=(
                sorted(connector_allowlist)
                if connector_allowlist is not None
                else None
            ),
            writable_connector_operations=(
                sorted(writable_connector_allowlist)
                if writable_connector_allowlist is not None
                else None
            ),
            permission_required_connector_operations=(
                sorted(permission_connector_allowlist)
                if permission_connector_allowlist is not None
                else None
            ),
            compensation_connector_operations=(
                sorted(compensation_connector_allowlist)
                if compensation_connector_allowlist is not None
                else None
            ),
            max_connector_write_count=max_connector_write_count,
            max_connector_payload_bytes=max_connector_payload_bytes,
            governed_host_actions=governed_host_actions,
            connector_descriptor_digests=connector_descriptor_digests,
            task_credential_ref_digest=task_credential_ref_digest,
            task_policy_digest=task_policy_digest,
            allowed_actions_digest=allowed_actions_digest,
            budget_digest=budget_digest,
            task_deadline_at=task_deadline_at,
            published_execution_policy_digest=(
                published_policy.policy_digest
                if published_policy is not None
                else None
            ),
            execution_policy_digest=(
                effective_policy.policy_digest
                if effective_policy is not None
                else None
            ),
            assignment_id=assignment_id,
            session_id=session_id,
            application_call_chain=current_call_chain,
        )
        await self.workflow_store.create_run(
            state, version=version, draft_revision=draft_revision
        )
        await self.harness.start_task(
            run_id,
            kind="workflow_run",
            owner_id=application_id,
            resource_id=run_id,
            parent_task_id=parent_task_id,
            metadata={
                "origin": origin,
                "version": version,
                "draft_revision": draft_revision,
                "workspace_path": state.workspace_path,
                "application_id": application_id,
                "workflow_id": application_id,
                "model": self.runtime_model,
                "budget_limit_usd": self._workflow_budget_limit(snapshot),
            },
        )
        self._start(state)
        return {
            "run_id": run_id,
            "status": "queued",
            "version": version,
            "draft_revision": draft_revision,
            "published_execution_policy_digest": (
                state.published_execution_policy_digest
            ),
            "execution_policy_digest": state.execution_policy_digest,
        }

    def _resolve_scoped_workspace(self, requested: str, boundary: Path) -> Path:
        """Resolve a workflow-selected directory relative to its trusted boundary."""

        candidate = Path(requested)
        if not candidate.is_absolute():
            candidate = boundary / candidate
        resolved = candidate.resolve()
        if resolved != boundary and boundary not in resolved.parents:
            raise WorkflowWorkspaceBoundaryViolation(
                "workflow workspace is outside the task-owned execution boundary"
            )
        try:
            checked = self.sandboxes.resolve_workspace(str(resolved)).resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise WorkflowWorkspaceBoundaryViolation(
                "workflow workspace is unavailable inside the task-owned execution boundary"
            ) from error
        if checked != boundary and boundary not in checked.parents:
            raise WorkflowWorkspaceBoundaryViolation(
                "workflow workspace is outside the task-owned execution boundary"
            )
        return checked

    def _workspace_for_run(self, run_id: str, requested: str) -> str:
        boundary = self._workspace_boundaries.get(run_id)
        if boundary is None:
            return requested
        return str(self._resolve_scoped_workspace(requested, boundary))

    def _artifact_workspace_for_run(self, run_id: str, requested: str) -> Path:
        """Give every run a private artifact namespace inside its declared workspace."""

        workspace = Path(self._workspace_for_run(run_id, requested)).resolve(
            strict=True
        )
        artifact_runs = workspace / ".workflow-run-artifacts"
        if artifact_runs.is_symlink():
            raise ValueError("workflow artifact run directory cannot be a symbolic link")
        artifact_runs.mkdir(mode=0o700, exist_ok=True)
        if artifact_runs.resolve() != artifact_runs or artifact_runs.parent != workspace:
            raise ValueError("workflow artifact run directory escapes the workspace")
        run_workspace = artifact_runs / run_id
        if run_workspace.is_symlink():
            raise ValueError("workflow run artifact directory cannot be a symbolic link")
        run_workspace.mkdir(mode=0o700, exist_ok=True)
        if (
            run_workspace.resolve() != run_workspace
            or run_workspace.parent != artifact_runs
        ):
            raise ValueError("workflow run artifact directory escapes the workspace")
        return run_workspace

    def _validate_execution_policy(
        self,
        workflow: WorkflowSpec,
        *,
        workspace_boundary: Path | None,
        allowed_nested_application_ids: frozenset[str] | None,
        allowed_runtime_tools: frozenset[str] | None = None,
        allowed_network_hosts: frozenset[str] | None = None,
        model_access: bool | None = None,
        allowed_connector_operations: frozenset[str] | None = None,
        governed_host_actions: bool = False,
        agents: dict[str, AgentSpec] | None = None,
    ) -> None:
        """Reject statically declared boundary escapes before a run is persisted."""

        for node in workflow.nodes:
            if any(
                value is not None
                for value in (
                    workspace_boundary,
                    allowed_nested_application_ids,
                    allowed_runtime_tools,
                    allowed_network_hosts,
                    model_access,
                    allowed_connector_operations,
                )
            ):
                definition = self.blocks.get(node.type)
                if definition.block_kind == "legacy_compatibility" or not definition.available:
                    raise WorkflowRuntimeToolScopeDenied(
                        "workflow block is outside the public assigned run policy"
                    )
            if (
                allowed_runtime_tools is not None
                and self.harness.contains_secret_reference(node.config)
            ):
                raise WorkflowRuntimeSecretScopeDenied(
                    "secret references are outside the assigned run policy"
                )
            config = self.blocks.validate_node(node)
            if model_access is False and isinstance(
                config,
                (
                    LLMConfig,
                    ClassifierConfig,
                    ParameterExtractorConfig,
                    ClaudeAgentConfig,
                ),
            ):
                raise WorkflowRuntimeModelScopeDenied(
                    "model-backed workflow blocks are outside the assigned run policy"
                )
            if (
                model_access is False
                and isinstance(config, AgentArchitectureConfig)
                and node.type in {"model_turn", "subagent_spawn"}
            ):
                raise WorkflowRuntimeModelScopeDenied(
                    "model-backed workflow blocks are outside the assigned run policy"
                )
            if isinstance(config, ToolConfig):
                self._validate_nested_workflow_target(
                    config.tool_name,
                    allowed_nested_application_ids,
                )
                self._validate_runtime_tool_target(config.tool_name, allowed_runtime_tools)
            if isinstance(config, HTTPConfig) and governed_host_actions:
                raise WorkflowRuntimeNetworkScopeDenied(
                    "raw HTTP blocks cannot bypass assigned host actions"
                )
            if isinstance(config, HTTPConfig) and allowed_network_hosts is not None:
                self._validate_network_url(config.url, allowed_network_hosts)
            if isinstance(config, WebCollectionConfig) and allowed_network_hosts is not None:
                raise WorkflowRuntimeNetworkScopeDenied(
                    "network-backed workflow blocks are outside the assigned run policy"
                )
            if isinstance(config, ConnectorActionConfig):
                self._resolve_connector_operation(
                    config,
                    allowed_connector_operations,
                )
            if isinstance(config, ClaudeAgentConfig) and agents is not None:
                agent = agents.get(config.agent_id)
                if agent is not None:
                    self._validate_agent_execution_policy(
                        agent,
                        allowed_runtime_tools=allowed_runtime_tools,
                        allowed_network_hosts=allowed_network_hosts,
                    )
            if isinstance(config, AgentArchitectureConfig):
                settings = config.settings
                if allowed_runtime_tools is not None and node.type == "mcp_gateway":
                    raise WorkflowRuntimeToolScopeDenied(
                        "MCP process launch is outside the assigned run policy"
                    )
                workspace_key = {
                    "tool_executor": "workspace_path",
                    "sandbox_boundary": "workspace",
                    "subagent_spawn": "workspace_path",
                }.get(node.type)
                if workspace_boundary is not None and workspace_key is not None:
                    declared = settings.get(workspace_key)
                    if isinstance(declared, str):
                        self._resolve_scoped_workspace(declared, workspace_boundary)
                if node.type == "tool_executor":
                    tool_name = settings.get("tool_name")
                    if isinstance(tool_name, str):
                        self._validate_nested_workflow_target(
                            tool_name,
                            allowed_nested_application_ids,
                        )
                        self._validate_runtime_tool_target(
                            tool_name,
                            allowed_runtime_tools,
                        )
                declared_tools = settings.get("tools")
                if isinstance(declared_tools, list):
                    for tool_name in declared_tools:
                        if isinstance(tool_name, str):
                            self._validate_runtime_tool_target(
                                tool_name,
                                allowed_runtime_tools,
                            )
                if allowed_network_hosts is not None and node.type == "sandbox_boundary":
                    declared_policy = str(settings.get("network_policy", "none"))
                    if declared_policy != "none":
                        raise WorkflowRuntimeNetworkScopeDenied(
                            "sandbox network access is outside the assigned run policy"
                        )
            if isinstance(config, (IterationConfig, LoopConfig)):
                self._validate_execution_policy(
                    config.workflow,
                    workspace_boundary=workspace_boundary,
                    allowed_nested_application_ids=allowed_nested_application_ids,
                    allowed_runtime_tools=allowed_runtime_tools,
                    allowed_network_hosts=allowed_network_hosts,
                    model_access=model_access,
                    allowed_connector_operations=allowed_connector_operations,
                    governed_host_actions=governed_host_actions,
                    agents=agents,
                )

    @staticmethod
    def _validate_runtime_tool_target(
        tool_name: str,
        allowed_runtime_tools: frozenset[str] | None,
    ) -> None:
        if tool_name.startswith("workflow:") or allowed_runtime_tools is None:
            return
        if tool_name not in allowed_runtime_tools:
            raise WorkflowRuntimeToolScopeDenied(
                "runtime tool is outside the assigned run policy"
            )

    @staticmethod
    def _resolve_connector_operation(
        config: ConnectorActionConfig,
        allowed_operations: frozenset[str] | None,
    ) -> str:
        canonical = f"{config.connector_id}.{config.operation_id}"
        if allowed_operations is None:
            return canonical
        candidates = {
            config.operation_id,
            canonical,
            f"{config.connector_id}:{config.operation_id}",
        }
        matched = sorted(candidates.intersection(allowed_operations))
        if len(matched) != 1:
            raise WorkflowRuntimeConnectorScopeDenied(
                "connector operation is absent from or ambiguous in the assigned policy"
            )
        return matched[0]

    async def _issue_runtime_exact_connector_authorization(
        self,
        *,
        config: ConnectorActionConfig,
        state: WorkflowRunState | None,
        run_id: str,
        node_id: str,
        tenant_id: str,
        actor_id: str,
        profile_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> str:
        """Issue one exact run-bound authorization after reference resolution."""

        if self.connector_service is None or state is None:
            raise WorkflowRuntimePermissionScopeDenied(
                "runtime exact connector authorization policy is incomplete"
            )
        authorization_identity = {
            "node_id": node_id,
            "connector_id": config.connector_id,
            "connector_version": config.connector_version,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "profile_id": profile_id,
            "operation_id": config.operation_id,
            "payload": payload,
            "idempotency_key": idempotency_key,
        }
        try:
            authorization_cache_key = hashlib.sha256(
                json.dumps(
                    authorization_identity,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError) as error:
            raise ValueError(
                "runtime exact connector authorization identity must be canonical JSON"
            ) from error
        cached_authorization_id = state.runtime_connector_authorization_ids.get(
            authorization_cache_key
        )
        if cached_authorization_id is not None:
            return cached_authorization_id
        if not state.governed_host_actions:
            await self._emit(
                run_id,
                "permission.requested",
                {
                    "node_id": node_id,
                    "operation_id": config.operation_id,
                    "behavior": "runtime_exact",
                    "issuance_source": "owner",
                },
            )
            authorization = await self.connector_service.create_authorization(
                connector_id=config.connector_id,
                connector_version=config.connector_version,
                tenant_id=tenant_id,
                actor_id=actor_id,
                profile_id=profile_id,
                operation_id=config.operation_id,
                payload=payload,
                application_id=state.application_id,
                run_id=run_id,
                expires_in_seconds=300,
                max_uses=1,
                issuance_source="owner",
            )
            state.runtime_connector_authorization_ids[
                authorization_cache_key
            ] = authorization.id
            await self._emit(
                run_id,
                "permission.resolved",
                {
                    "node_id": node_id,
                    "operation_id": config.operation_id,
                    "behavior": "runtime_exact",
                    "issuance_source": "owner",
                    "outcome": "issued",
                },
            )
            return authorization.id
        if (
            not state.assignment_id
            or not state.session_id
            or state.allowed_network_hosts is None
            or state.compensation_connector_operations is None
            or state.max_connector_write_count is None
            or state.max_connector_payload_bytes is None
            or state.connector_descriptor_digests is None
            or not state.task_credential_ref_digest
            or not state.task_policy_digest
            or not state.allowed_actions_digest
            or not state.budget_digest
            or not state.task_deadline_at
        ):
            raise WorkflowRuntimePermissionScopeDenied(
                "runtime exact connector authorization policy is incomplete"
            )
        operation_key = f"{config.connector_id}.{config.operation_id}"
        descriptor_digest = state.connector_descriptor_digests.get(operation_key)
        if descriptor_digest is None:
            raise WorkflowRuntimePermissionScopeDenied(
                "runtime exact connector authorization descriptor is absent"
            )
        deadline = datetime.fromisoformat(
            state.task_deadline_at.replace("Z", "+00:00")
        )
        if deadline.tzinfo is None:
            raise WorkflowRuntimePermissionScopeDenied(
                "runtime exact connector authorization deadline is invalid"
            )
        remaining_seconds = int(
            (deadline - datetime.now(timezone.utc)).total_seconds()
        )
        if remaining_seconds < 1:
            raise WorkflowRuntimePermissionScopeDenied(
                "runtime exact connector authorization deadline expired"
            )
        budget = await self.connector_service.freeze_assignment_budget(
            assignment_id=state.assignment_id,
            allowed_network_hosts=state.allowed_network_hosts,
            allowed_compensation_operations=(
                state.compensation_connector_operations
            ),
            max_write_count=state.max_connector_write_count,
            max_payload_bytes=state.max_connector_payload_bytes,
        )
        if budget.write_count >= budget.max_write_count:
            raise WorkflowRuntimeWriteLimitExceeded(
                "connector write authorization budget is exhausted"
            )
        await self._emit(
            run_id,
            "permission.requested",
            {
                "node_id": node_id,
                "operation_id": config.operation_id,
                "behavior": "runtime_exact",
            },
        )
        authorization = await self.connector_service.create_authorization(
            connector_id=config.connector_id,
            connector_version=config.connector_version,
            tenant_id=tenant_id,
            actor_id=actor_id,
            profile_id=profile_id,
            operation_id=config.operation_id,
            payload=payload,
            assignment_id=state.assignment_id,
            session_id=state.session_id,
            application_id=state.application_id,
            run_id=run_id,
            expires_in_seconds=min(300, remaining_seconds),
            max_uses=1,
            issuance_source="task_policy",
            descriptor_digest=descriptor_digest,
            task_credential_ref_digest=state.task_credential_ref_digest,
            task_policy_digest=state.task_policy_digest,
            allowed_actions_digest=state.allowed_actions_digest,
            budget_digest=state.budget_digest,
            assignment_budget_policy_digest=f"sha256:{budget.policy_digest}",
            assignment_max_write_count=budget.max_write_count,
            assignment_max_payload_bytes=budget.max_payload_bytes,
            assignment_write_count_at_issue=budget.write_count,
            task_deadline_at=state.task_deadline_at,
        )
        state.runtime_connector_authorization_ids[
            authorization_cache_key
        ] = authorization.id
        await self._emit(
            run_id,
            "permission.resolved",
            {
                "node_id": node_id,
                "operation_id": config.operation_id,
                "behavior": "runtime_exact",
                "outcome": "issued",
            },
        )
        return authorization.id

    @staticmethod
    def _validate_network_url(
        raw_url: Any,
        allowed_network_hosts: frozenset[str],
    ) -> None:
        if not isinstance(raw_url, str):
            if not allowed_network_hosts:
                raise WorkflowRuntimeNetworkScopeDenied(
                    "dynamic network destinations are outside the assigned run policy"
                )
            return
        hostname = urlparse(raw_url).hostname
        if hostname is None or hostname.casefold() not in allowed_network_hosts:
            raise WorkflowRuntimeNetworkScopeDenied(
                "network destination is outside the assigned run policy"
            )

    def _validate_agent_execution_policy(
        self,
        agent: AgentSpec,
        *,
        allowed_runtime_tools: frozenset[str] | None,
        allowed_network_hosts: frozenset[str] | None,
    ) -> None:
        effective_agent = self._restricted_agent(agent, allowed_runtime_tools)
        actual_tools = {
            definition.name for definition in self.tools.definitions_for(effective_agent)
        }
        for tool_name in actual_tools:
            self._validate_runtime_tool_target(tool_name, allowed_runtime_tools)
        if allowed_runtime_tools is not None and agent.mcp_servers:
            raise WorkflowRuntimeToolScopeDenied(
                "agent MCP process launch is outside the assigned run policy"
            )
        if allowed_network_hosts is None:
            return
        policy = str(getattr(agent.network_policy, "value", agent.network_policy))
        if policy == "full":
            raise WorkflowRuntimeNetworkScopeDenied(
                "full agent network access is outside the assigned run policy"
            )
        declared_hosts = {str(host).casefold() for host in agent.network_allowlist}
        if not declared_hosts.issubset(allowed_network_hosts):
            raise WorkflowRuntimeNetworkScopeDenied(
                "agent network allowlist exceeds the assigned run policy"
            )

    def _restricted_agent(
        self,
        agent: AgentSpec,
        allowed_runtime_tools: frozenset[str] | None,
    ) -> AgentSpec:
        """Materialize an empty restricted tool list as genuinely tool-free.

        AgentRuntime preserves a legacy convention where an empty ``tools``
        list means "all registered tools".  Restricted workflow runs cannot
        inherit that convention, so subtract the complete registry from the
        effective agent while leaving unrestricted/legacy runs unchanged.
        """

        if allowed_runtime_tools is None or agent.tools:
            return agent
        return agent.model_copy(
            update={
                "disallowed_tools": sorted(
                    set(agent.disallowed_tools) | set(self.tools.names())
                )
            }
        )

    def validate_restricted_snapshot(
        self,
        snapshot: ApplicationSnapshot,
        *,
        workspace_boundary: str,
        allowed_nested_application_ids: Collection[str],
        allowed_runtime_tools: Collection[str],
        allowed_network_hosts: Collection[str],
        model_access: bool | None = None,
        allowed_connector_operations: Collection[str] | None = None,
        governed_host_actions: bool = False,
        for_publication: bool = False,
    ) -> None:
        """Apply the same static policy used by black-box runs without persisting a run."""

        for test in snapshot.tests:
            self._validate_restricted_inputs(test.inputs)
        boundary = self.sandboxes.resolve_workspace(workspace_boundary).resolve()
        self._validate_execution_policy(
            snapshot.workflow,
            workspace_boundary=boundary,
            allowed_nested_application_ids=frozenset(
                str(value) for value in allowed_nested_application_ids
            ),
            allowed_runtime_tools=frozenset(str(value) for value in allowed_runtime_tools),
            allowed_network_hosts=frozenset(
                str(value).casefold() for value in allowed_network_hosts
            ),
            model_access=model_access,
            allowed_connector_operations=(
                frozenset(str(value) for value in allowed_connector_operations)
                if allowed_connector_operations is not None
                else None
            ),
            governed_host_actions=governed_host_actions,
            agents=snapshot.agents,
        )
        if for_publication:
            self._validate_publication_policy(snapshot.workflow)

    def _validate_publication_policy(self, workflow: WorkflowSpec) -> None:
        """Keep boundaries without a complete immutable runtime contract closed.

        Task-scoped publication now persists workspace, tool, nested workflow,
        connector, model and budget authority.  Raw HTTP/web collection and
        scheduling still have destination/trigger semantics that are not fully
        represented by that snapshot and therefore remain unavailable.
        """

        for node in workflow.nodes:
            config = self.blocks.validate_node(node)
            if isinstance(config, (HTTPConfig, WebCollectionConfig)):
                raise WorkflowRuntimeNetworkScopeDenied(
                    "raw network workflow blocks are outside immutable published policy"
                )
            if isinstance(config, ScheduleTriggerConfig):
                raise WorkflowRuntimeToolScopeDenied(
                    "scheduled execution is outside immutable published policy"
                )
            if isinstance(config, (IterationConfig, LoopConfig)):
                self._validate_publication_policy(config.workflow)

    def _validate_restricted_inputs(
        self,
        inputs: dict[str, Any],
        *,
        allowed_reserved_keys: frozenset[str] = frozenset(),
    ) -> None:
        reserved = sorted(
            str(key)
            for key in inputs
            if str(key).startswith("__")
            and str(key) not in allowed_reserved_keys
        )
        if reserved:
            raise ValueError(f"reserved runtime input keys are not public: {reserved}")
        if self.harness.contains_secret_reference(inputs):
            raise WorkflowRuntimeSecretScopeDenied(
                "secret references are outside the assigned run policy"
            )

    @staticmethod
    def _validate_nested_workflow_target(
        tool_name: str,
        allowed_nested_application_ids: frozenset[str] | None,
    ) -> None:
        if not tool_name.startswith("workflow:") or allowed_nested_application_ids is None:
            return
        application_id = tool_name.split(":", 1)[1]
        if application_id not in allowed_nested_application_ids:
            raise NestedWorkflowScopeDenied(
                "nested workflow target is outside the assigned application scope"
            )

    async def _inputs_with_governed_memory(
        self,
        *,
        application_id: str,
        inputs: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        config = inputs.get("__governed_memory__")
        if not config:
            return dict(inputs)
        if not isinstance(config, dict):
            raise ValueError("__governed_memory__ must be an object")
        if config.get("enabled") is not True:
            return dict(inputs)
        if self.governed_memory is None:
            raise ValueError("governed memory surface is not configured")
        actor_id = str(config.get("actor_id") or "").strip()
        scope_id = str(config.get("scope_id") or "").strip()
        purpose = str(config.get("purpose") or "").strip()
        reason = str(config.get("reason") or "").strip()
        if not actor_id or not scope_id or not purpose or not reason:
            raise ValueError("__governed_memory__ requires actor_id, scope_id, purpose, and reason")
        limit = int(config.get("limit", 20))
        limit = max(1, min(limit, 100))
        permission = GovernedMemoryPermission(
            actor_id=actor_id,
            owner_id=application_id,
            scope_id=scope_id,
            purpose=purpose,
            allowed_operations=["read"],
            expires_at=config.get("permission_expires_at"),
        )
        try:
            listed = await self.governed_memory.list_active(
                owner_id=application_id,
                scope_id=scope_id,
                permission=permission,
                reason=reason,
                limit=limit,
            )
            audited_items = [
                await self.governed_memory.read(item.id, permission=permission, reason=reason)
                for item in listed
            ]
        except GovernedMemoryViolation as error:
            raise ValueError(f"governed memory retrieval rejected: {error}") from error
        enriched = dict(inputs)
        enriched["__governed_memory_context__"] = {
            "enabled": True,
            "owner_id": application_id,
            "scope_id": scope_id,
            "actor_id": actor_id,
            "purpose": purpose,
            "reason": reason,
            "retrieved_count": len(audited_items),
            "audit_stream_id": GovernedMemorySurface.audit_stream_id(application_id, scope_id),
            "items": [
                {
                    "id": item.id,
                    "content": item.content,
                    "source": item.source.model_dump(mode="json"),
                    "retention_class": item.retention_class,
                    "expires_at": item.expires_at,
                }
                for item in audited_items
            ],
        }
        await self._emit(
            run_id,
            "governed_memory.retrieved",
            {
                "owner_id": application_id,
                "scope_id": scope_id,
                "actor_id": actor_id,
                "retrieved_count": len(audited_items),
                "audit_stream_id": enriched["__governed_memory_context__"]["audit_stream_id"],
            },
        )
        return enriched

    async def resume(self, run_id: str, values: dict[str, Any]) -> dict[str, Any]:
        record = await self.workflow_store.get_run(run_id)
        if record["status"] != "paused":
            raise RuntimeError(f"workflow run is not paused: {record['status']}")
        state: WorkflowRunState = record["state"]
        state.resumed_values = values
        await self.workflow_store.update_run(run_id, status="queued", state=state)
        await self.harness.start_task(
            run_id,
            kind="workflow_run",
            owner_id=state.application_id,
            resource_id=run_id,
            metadata={"origin": "resume"},
        )
        await self._emit(run_id, "workflow.resumed", {"node_id": state.waiting_node_id})
        self._start(state)
        return {"run_id": run_id, "status": "queued"}

    def cancel(self, run_id: str) -> None:
        task = self.active_tasks.get(run_id)
        if not task or task.done():
            raise KeyError("active workflow run not found")
        task.cancel()

    def _start(self, state: WorkflowRunState) -> None:
        existing = self.active_tasks.get(state.run_id)
        if existing and not existing.done():
            raise RuntimeError("workflow run is already active")
        if state.workspace_boundary is not None:
            boundary = self.sandboxes.resolve_workspace(state.workspace_boundary).resolve()
            self._resolve_scoped_workspace(state.workspace_path, boundary)
            self._workspace_boundaries[state.run_id] = boundary
        if state.allowed_nested_application_ids is not None:
            self._nested_application_allowlists[state.run_id] = frozenset(
                state.allowed_nested_application_ids
            )
        if state.allowed_runtime_tools is not None:
            self._runtime_tool_allowlists[state.run_id] = frozenset(
                state.allowed_runtime_tools
            )
        if state.allowed_network_hosts is not None:
            self._network_host_allowlists[state.run_id] = frozenset(
                host.casefold() for host in state.allowed_network_hosts
            )
        if state.model_access is not None:
            self._model_access_policies[state.run_id] = state.model_access
        if state.allowed_connector_operations is not None:
            self._connector_operation_allowlists[state.run_id] = frozenset(
                state.allowed_connector_operations
            )
        if state.writable_connector_operations is not None:
            self._writable_connector_operations[state.run_id] = frozenset(
                state.writable_connector_operations
            )
        if state.permission_required_connector_operations is not None:
            self._permission_connector_operations[state.run_id] = frozenset(
                state.permission_required_connector_operations
            )
        if state.compensation_connector_operations is not None:
            self._compensation_connector_operations[state.run_id] = frozenset(
                state.compensation_connector_operations
            )
        task = asyncio.create_task(self._run(state))
        self.active_tasks[state.run_id] = task
        task.add_done_callback(lambda item: self._consume(state.run_id, item))

    async def _run(self, state: WorkflowRunState) -> None:
        await self.workflow_store.update_run(state.run_id, status="running", state=state)
        await self._emit(state.run_id, "workflow.started", {
            "application_id": state.application_id,
            "input": self._redact(state.inputs),
        })
        try:
            local_outputs = await self._run_graph(
                state.snapshot,
                state.snapshot.workflow,
                state.inputs,
                state.workspace_path,
                state.run_id,
                prefix="",
                top_state=state,
            )
            result = self._terminal_outputs(state.snapshot.workflow, local_outputs)
            await self.workflow_store.update_run(
                state.run_id, status="succeeded", state=state, outputs=result
            )
            await self._emit(state.run_id, "workflow.completed", {"outputs": result})
            await self.harness.finish_task(state.run_id, status="succeeded")
        except HumanInputPause:
            await self._emit(state.run_id, "workflow.paused", {"node_id": state.waiting_node_id})
            # Persist paused as the final awaited action. Observers cannot see a
            # resumable state while this task is still emitting and be raced by
            # process shutdown cancellation.
            await self.workflow_store.update_run(state.run_id, status="paused", state=state)
            await self.harness.finish_task(state.run_id, status="paused")
        except asyncio.CancelledError:
            await self.workflow_store.update_run(state.run_id, status="cancelled", state=state)
            await self._emit(state.run_id, "workflow.cancelled", {})
            await self.harness.finish_task(state.run_id, status="cancelled")
            raise
        except Exception as error:
            await self.workflow_store.update_run(
                state.run_id, status="failed", state=state, error=str(error)
            )
            await self._emit(state.run_id, "workflow.failed", {
                "error": str(error), "error_type": type(error).__name__
            })
            await self.harness.finish_task(state.run_id, status="failed", error=str(error))

    async def _run_graph(
        self,
        snapshot: ApplicationSnapshot,
        workflow: WorkflowSpec,
        inputs: dict[str, Any],
        workspace_path: str,
        run_id: str,
        *,
        prefix: str,
        top_state: WorkflowRunState | None = None,
    ) -> dict[str, dict[str, Any]]:
        node_map = {node.id: node for node in workflow.nodes}
        incoming: dict[str, list[Any]] = defaultdict(list)
        outgoing: dict[str, list[str]] = defaultdict(list)
        indegree = {node.id: 0 for node in workflow.nodes}
        for edge in workflow.edges:
            incoming[edge.target].append(edge)
            outgoing[edge.source].append(edge.target)
            indegree[edge.target] += 1
        queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
        order: list[str] = []
        while queue:
            current = queue.popleft()
            order.append(current)
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)

        outputs = top_state.outputs if top_state and not prefix else {}
        completed = set(top_state.completed if top_state and not prefix else [])
        skipped = set(top_state.skipped if top_state and not prefix else [])
        for node_id in order:
            node = node_map[node_id]
            scoped_id = f"{prefix}{node_id}"
            if node_id in completed or node_id in skipped:
                continue
            edges = incoming[node_id]
            if edges and not any(self._edge_active(edge, outputs, skipped) for edge in edges):
                skipped.add(node_id)
                if top_state and not prefix:
                    top_state.skipped = list(skipped)
                    await self.workflow_store.update_run(run_id, status="running", state=top_state)
                await self._emit(run_id, "node.skipped", {"node_id": scoped_id})
                continue
            if top_state and not prefix:
                await self.harness.record_usage(
                    run_id,
                    "node_execution",
                    metadata={"node_id": scoped_id, "type": node.type, "title": node.title},
                )
            await self._emit(run_id, "node.started", {"node_id": scoped_id, "type": node.type, "title": node.title})
            try:
                output = await self._execute_with_retry(
                    snapshot,
                    node,
                    inputs,
                    outputs,
                    workspace_path,
                    run_id,
                    scoped_id,
                    top_state,
                )
            except HumanInputPause:
                raise
            except Exception as error:
                await self._emit(run_id, "node.failed", {"node_id": scoped_id, "error": str(error)})
                if node.error_strategy == ErrorStrategy.fail:
                    raise NodeExecutionError(scoped_id, error) from error
                elif node.error_strategy == ErrorStrategy.degraded:
                    await self._emit(run_id, "node.degraded", {
                        "node_id": scoped_id, "error": str(error),
                        "degraded_value": self._redact(node.degraded_value),
                    })
                    output = {
                        "output": node.degraded_value,
                        "error": str(error),
                        "degraded": True,
                        "state": {"degraded": True, "error": str(error)},
                    }
                elif node.error_strategy == ErrorStrategy.retry_with_fallback:
                    output = {
                        "output": node.fallback_value,
                        "error": str(error),
                        "fallback_used": True,
                        "state": {"fallback": True, "error": str(error)},
                    }
                elif node.error_strategy == ErrorStrategy.error_branch:
                    output = {"error": str(error), "branch": "error"}
                else:
                    output = {"error": str(error)}

            # Contract validation (post-execution, non-blocking)
            if node.contract and node.contract.enforce:
                await self._validate_contract(node, output, scoped_id, run_id)

            outputs[node_id] = output
            completed.add(node_id)
            if top_state and not prefix:
                top_state.outputs = outputs
                top_state.completed = list(completed)
                top_state.waiting_node_id = None
                top_state.resumed_values = None
                await self.workflow_store.update_run(run_id, status="running", state=top_state)
            await self._emit(run_id, "node.completed", {"node_id": scoped_id, "outputs": self._redact(output)})
        return outputs

    async def _execute_with_retry(
        self,
        snapshot: ApplicationSnapshot,
        node: NodeSpec,
        inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        workspace_path: str,
        run_id: str,
        scoped_id: str,
        state: WorkflowRunState | None,
    ) -> dict[str, Any]:
        attempts = node.retry.max_attempts if node.retry.enabled else 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._execute_node(
                    snapshot, node, inputs, outputs, workspace_path, run_id, scoped_id, state
                )
            except HumanInputPause:
                raise
            except Exception as error:
                if attempt == attempts or not self._retryable_execution_error(error):
                    raise
                await self._emit(run_id, "node.retry", {"node_id": scoped_id, "attempt": attempt + 1})
                await asyncio.sleep(node.retry.delay_seconds)
        raise RuntimeError("unreachable")

    @staticmethod
    def _retryable_execution_error(error: Exception) -> bool:
        if isinstance(error, WorkflowHTTPError):
            return (
                error.status_code in {408, 429}
                or 500 <= error.status_code <= 599
            )
        return True

    async def _execute_node(
        self,
        snapshot: ApplicationSnapshot,
        node: NodeSpec,
        inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        workspace_path: str,
        run_id: str,
        scoped_id: str,
        state: WorkflowRunState | None,
    ) -> dict[str, Any]:
        config = self.blocks.validate_node(node)
        context = {"inputs": inputs, "nodes": outputs, "run": {"run_id": run_id}}
        if isinstance(config, StartConfig):
            result: dict[str, Any] = {}
            for field in config.inputs:
                value = inputs.get(field.name, field.default)
                if field.required and value is None:
                    raise ValueError(f"missing required input: {field.name}")
                result[field.name] = value
            return {"output": result, **result}
        if isinstance(config, ScheduleTriggerConfig):
            result = {**config.inputs, **inputs}
            return {"output": result, **result}
        if isinstance(config, EventSubscriptionTriggerConfig):
            result = {}
            for field in config.inputs:
                value = inputs.get(field.name, field.default)
                if field.required and value is None:
                    raise ValueError(f"missing required input: {field.name}")
                result[field.name] = value
            return {"output": result, **result}
        if isinstance(config, LLMConfig):
            prompt = str(self._resolve(config.prompt, context))
            system = config.system
            if config.structured_output is not None:
                schema = json.dumps(config.structured_output, ensure_ascii=False)
                system = (
                    f"{system.rstrip()}\n\n"
                    "Runtime output contract: return exactly one valid JSON value that matches "
                    f"this JSON Schema: {schema}\n"
                    "Do not return Markdown, prose, XML, comments, or a fenced code block. "
                    "This runtime instruction overrides any earlier output-format instruction."
                )
            text, usage = await self._model_text(
                run_id, config.model or self.runtime_model, system, prompt, scoped_id
            )
            result = {"text": text, "usage": usage.model_dump(mode="json")}
            if config.structured_output is not None:
                result["structured"] = self._json_from_text(text)
            return result
        if isinstance(config, ClaudeAgentConfig):
            agent = snapshot.agents.get(config.agent_id)
            if agent:
                version = await self.storage.save_agent_version(agent, "application")
            else:
                agent, version, _ = await self.storage.get_agent(config.agent_id, config.version)
            self._validate_agent_execution_policy(
                agent,
                allowed_runtime_tools=self._runtime_tool_allowlists.get(run_id),
                allowed_network_hosts=self._network_host_allowlists.get(run_id),
            )
            agent = self._restricted_agent(
                agent,
                self._runtime_tool_allowlists.get(run_id),
            )
            session = await self.agent_runtime.create_session(
                agent,
                version,
                workspace_path,
                parent_task_id=run_id,
                governance_owner_id=state.application_id if state else None,
                governance_application_id=state.application_id if state else None,
                allow_secret_references=run_id not in self._runtime_tool_allowlists,
            )
            task = str(self._resolve(config.task, context))
            await self._emit(run_id, "node.agent.session", {"node_id": scoped_id, "session_id": session.id})

            async def relay_agent_event(kind: str, data: dict[str, Any]) -> None:
                payload = {
                    "node_id": scoped_id,
                    "session_id": session.id,
                    **data,
                }
                if kind in {
                    "permission.requested",
                    "permission.resolved",
                    "tool.started",
                    "tool.completed",
                    "tool.failed",
                    "turn.completed",
                    "turn.failed",
                    "turn.cancelled",
                    "agent.iteration",
                    "context.compaction.started",
                    "context.compaction.completed",
                } or kind.startswith("model."):
                    await self._emit(run_id, f"node.agent.{kind}", payload)
                if kind in {"permission.requested", "permission.resolved"}:
                    await self._emit(run_id, kind, payload)
                    if state:
                        await self.workflow_store.update_run(run_id, status="running", state=state)

            self.agent_runtime.register_event_relay(session.id, relay_agent_event)
            try:
                text = await self.agent_runtime.run_turn_and_wait(session, task)
            finally:
                self.agent_runtime.unregister_event_relay(session.id)
            tool_calls = [
                block.name
                for message in session.messages
                for block in message.content
                if block.type == "tool_use" and block.name
            ]
            return {
                "text": text,
                "session_id": session.id,
                "tool_calls": tool_calls,
                "usage": session.usage.model_dump(mode="json"),
            }
        if isinstance(config, ToolConfig):
            return await self._execute_tool(
                config,
                snapshot,
                context,
                workspace_path,
                run_id,
                scoped_id,
                owner_id=state.application_id if state else "",
                state=state,
            )
        if isinstance(config, AgentArchitectureConfig):
            return await self._execute_agent_architecture_block(
                config, snapshot, node, context, workspace_path, run_id, scoped_id, state
            )
        if isinstance(config, IfElseConfig):
            for case in config.cases:
                values = [self._evaluate(condition, context) for condition in case.conditions]
                if (case.logical_operator == "and" and all(values)) or (case.logical_operator == "or" and any(values)):
                    return {"branch": case.id}
            return {"branch": config.default_branch}
        if isinstance(config, ClassifierConfig):
            value = str(self._resolve(config.input, context))
            prompt = (
                f"{config.instruction}\nClasses: {json.dumps(config.classes, ensure_ascii=False)}\n"
                f"Input: {value}\nReturn only one exact class name."
            )
            text, usage = await self._model_text(
                run_id, config.model or self.runtime_model, "You are a precise text router.", prompt, scoped_id
            )
            branch = next((item for item in config.classes if item.casefold() in text.casefold()), None)
            if branch is None:
                raise ValueError(f"classifier returned no known class: {text[:200]}")
            return {"branch": branch, "text": text, "usage": usage.model_dump(mode="json")}
        if isinstance(config, ParameterExtractorConfig):
            value = self._resolve(config.input, context)
            schema = {
                "type": "object",
                "properties": {field.name: {"type": self._json_type(field.type.value)} for field in config.fields},
                "required": [field.name for field in config.fields if field.required],
            }
            prompt = f"{config.instruction}\nSchema: {json.dumps(schema)}\nInput: {value}\nReturn JSON only."
            text, usage = await self._model_text(
                run_id, config.model or self.runtime_model, "Extract structured data exactly.", prompt, scoped_id
            )
            return {"structured": self._json_from_text(text), "usage": usage.model_dump(mode="json")}
        if isinstance(config, TemplateConfig):
            variables = {key: self._resolve(value, context) for key, value in config.variables.items()}
            return {"text": self._render(config.template, variables)}
        if isinstance(config, VariableAssignerConfig):
            return {
                "output": {
                    key: self._resolve_assignment(value, context)
                    for key, value in config.assignments.items()
                }
            }
        if isinstance(config, VariableAggregatorConfig):
            skipped_nodes = set(state.skipped if state else [])
            values = []
            for variable in config.variables:
                try:
                    values.append(self._resolve(variable, context))
                except (KeyError, IndexError, TypeError, ValueError):
                    if self._references_skipped_node(variable, skipped_nodes):
                        values.append(None)
                        continue
                    raise
            if config.mode == "array":
                value: Any = values
            elif config.mode == "merge":
                value = {}
                for item in values:
                    if isinstance(item, dict):
                        value.update(item)
            else:
                value = next((item for item in values if item is not None), None)
            return {"output": value}
        if isinstance(config, HTTPConfig):
            if run_id in self._network_host_allowlists:
                return await self._http(
                    config,
                    context,
                    owner_id=state.application_id if state else "",
                    run_id=run_id,
                )
            # Preserve the legacy override seam for unrestricted runs. Several
            # integrations replace ``_http`` with the original three-argument
            # callable; only policy-bound runs need the additional run key.
            return await self._http(
                config,
                context,
                owner_id=state.application_id if state else "",
            )
        if isinstance(config, DurableEventTimerConfig):
            if self.event_automation is None:
                raise RuntimeError("event automation service is not configured")
            raw_operation = str(self._resolve(config.operation, context))
            operation = {
                "on": "schedule",
                "open": "schedule",
                "off": "cancel",
                "closed": "cancel",
            }.get(raw_operation, raw_operation)
            request = DurableEventTimerRequest.model_validate(
                {
                    "operation": operation,
                    "timer_key": self._resolve(config.timer_key, context),
                    "subject_id": self._resolve(config.subject_id, context),
                    "event_id": self._resolve(config.event_id, context),
                    "occurred_at": self._resolve(config.occurred_at, context),
                    "hold_for_seconds": self._resolve(
                        config.hold_for_seconds,
                        context,
                    ),
                    "due_inputs": self._resolve(config.due_inputs, context),
                }
            )
            result = await self.event_automation.apply_timer(
                state.application_id if state else "",
                workspace_path,
                request,
            )
            return {"output": result, **result}
        if isinstance(config, WebCollectionConfig):
            if run_id in self._network_host_allowlists:
                raise WorkflowRuntimeNetworkScopeDenied(
                    "network-backed workflow blocks are outside the assigned run policy"
                )
            if self.web_collector is None:
                raise RuntimeError("controlled Web collection service is not configured")
            job_context = inputs.get("__job__", {})
            if not isinstance(job_context, dict):
                raise ValueError("__job__ input must be an object")
            result = await self.web_collector.collect(
                config=config,
                sources=self._resolve(config.sources, context),
                application_id=state.application_id if state else "",
                run_id=run_id,
                job_context=job_context,
            )
            return {"output": result, **result}
        if isinstance(config, CollectionDigestConfig):
            return ControlledWebCollector.render_digest(
                config,
                self._resolve(config.collection, context),
                self._resolve(config.topic, context),
            )
        if isinstance(config, DeployedModelInferenceConfig):
            if self.tabular_models is None:
                raise RuntimeError("tabular model service is not configured")
            resolved_units = (
                self._resolve(config.units, context) if config.units is not None else {}
            )
            result = await self.tabular_models.predict(
                config.deployment_name,
                TabularInferenceRequest(
                    features=self._resolve(config.features, context),
                    units=resolved_units or {},
                ),
            )
            return {"output": result, **result}
        if isinstance(config, ModelDriftMonitorConfig):
            if self.tabular_models is None:
                raise RuntimeError("tabular model service is not configured")
            raw_observations = self._resolve(config.observations, context)
            if not isinstance(raw_observations, list):
                raise ValueError("model drift observations must resolve to an array")
            result = await self.tabular_models.drift(
                config.deployment_name,
                TabularDriftRequest(
                    observations=[
                        ModelObservation.model_validate(item) for item in raw_observations
                    ],
                    warning_threshold=config.warning_threshold,
                    critical_threshold=config.critical_threshold,
                ),
            )
            return {"output": result, **result}
        if isinstance(config, DeployedForecastConfig):
            if self.forecast_models is None:
                raise RuntimeError("forecast model service is not configured")
            raw_series = self._resolve(config.series, context)
            if not isinstance(raw_series, list):
                raise ValueError("forecast series must resolve to an array")
            result = await self.forecast_models.predict(
                config.deployment_name,
                ForecastInferenceRequest(
                    series=[ForecastSeries.model_validate(item) for item in raw_series],
                    unit=str(self._resolve(config.unit, context)),
                    horizon=int(self._resolve(config.horizon, context)),
                ),
            )
            return {"output": result, **result}
        if isinstance(config, ReplenishmentPlannerConfig):
            result = solve_replenishment(
                ReplenishmentPlanRequest(
                    forecasts=self._resolve(config.forecasts, context),
                    items=self._resolve(config.items, context),
                    capacity=float(self._resolve(config.capacity, context)),
                    budget=float(self._resolve(config.budget, context)),
                    solver_version=config.solver_version,
                    max_candidates_per_item=config.max_candidates_per_item,
                    max_states=config.max_states,
                )
            )
            return {"output": result, **result}
        if isinstance(config, KnowledgeIndexSyncConfig):
            if self.knowledge_indexes is None:
                raise RuntimeError("knowledge index service is not configured")
            documents = self._resolve(config.documents, context)
            deleted_source_ids = self._resolve(config.deleted_source_ids, context)
            event_id = self._resolve(config.event_id, context)
            if config.replace:
                incoming = {
                    str(item.get("source_id") or item.get("title") or "")
                    for item in (documents if isinstance(documents, list) else [])
                    if isinstance(item, dict)
                }
                existing = await self.knowledge_indexes.list_source_ids(config.index_name)
                stale = sorted(set(existing) - incoming)
                combined = {str(item) for item in (deleted_source_ids or [])} | set(stale)
                deleted_source_ids = sorted(combined - incoming)
            result = await self.knowledge_indexes.sync(
                config.index_name,
                KnowledgeSyncRequest.model_validate(
                    {
                        "documents": documents,
                        "deleted_source_ids": deleted_source_ids,
                        "event_id": event_id,
                    }
                ),
            )
            return {"output": result, **result}
        if isinstance(config, KnowledgeRetrievalConfig):
            if self.knowledge_indexes is None:
                raise RuntimeError("knowledge index service is not configured")
            result = await self.knowledge_indexes.retrieve(
                config.index_name,
                KnowledgeRetrieveRequest.model_validate(
                    {
                        "query": self._resolve(config.query, context),
                        "principal_roles": self._resolve(config.principal_roles, context),
                        "top_k": config.top_k,
                        "minimum_score": config.minimum_score,
                    }
                ),
            )
            return {"output": result, **result}
        if isinstance(config, GroundedAnswerConfig):
            result = grounded_answer(
                query=str(self._resolve(config.query, context)),
                retrieval=self._resolve(config.retrieval, context),
                refusal_message=config.refusal_message,
            )
            return {"output": result, **result}
        if isinstance(config, JsonSchemaValidateConfig):
            serialized = config.model_dump(mode="python", by_alias=True)
            result = validate_json_value(
                self._resolve(serialized["value"], context),
                serialized["schema"],
                max_errors=config.max_errors,
            )
            return {"output": result, **result}
        if isinstance(config, RegexExtractConfig):
            result = extract_regex_fields(
                self._resolve(config.text, context),
                config.fields,
            )
            return {"output": result, **result}
        if isinstance(config, RecordCollectionNormalizeConfig):
            result = normalize_record_collection(
                self._resolve(config.value, context),
                config.record_paths,
                single_object_policy=config.single_object_policy,
                empty_policy=config.empty_policy,
            )
            return {"output": result, **result}
        if isinstance(config, RecordDeduplicateConfig):
            result = deduplicate_records(
                self._resolve(config.records, context),
                config.key_paths,
                missing_key_policy=config.missing_key_policy,
            )
            return {"output": result, **result}
        if isinstance(config, RecordMatchConfig):
            if config.sources is not None:
                result = match_records(
                    self._resolve(config.sources, context),
                    self._resolve(config.candidates, context),
                    conditions=config.conditions,
                    conflict_checks=config.conflict_checks,
                    min_score=config.min_score,
                    ambiguity_threshold=config.ambiguity_threshold,
                    result_limit=config.result_limit,
                    consume_candidates=config.consume_candidates,
                )
            else:
                result = match_record(
                    self._resolve(config.source, context),
                    self._resolve(config.candidates, context),
                    conditions=config.conditions,
                    conflict_checks=config.conflict_checks,
                    min_score=config.min_score,
                    ambiguity_threshold=config.ambiguity_threshold,
                    result_limit=config.result_limit,
                )
            return {"output": result, **result}
        if isinstance(config, TypedJsonArtifactConfig):
            serialized = config.model_dump(mode="python", by_alias=True)
            artifact_workspace = self._artifact_workspace_for_run(
                run_id,
                workspace_path,
            )
            artifact = write_typed_json_artifact(
                workspace=artifact_workspace,
                value=self._resolve(serialized["value"], context),
                filename=config.filename,
                lineage=self._resolve(serialized["lineage"], context),
                run_id=run_id,
                node_id=scoped_id,
                application_id=state.application_id if state else "",
            )
            artifact["relative_path"] = (
                f".workflow-run-artifacts/{run_id}/"
                f"{artifact['relative_path']}"
            )
            await self._emit(
                run_id,
                "artifact.created",
                {
                    "node_id": scoped_id,
                    "relative_path": artifact["relative_path"],
                    "media_type": artifact["media_type"],
                    "size_bytes": artifact["size_bytes"],
                    "sha256": artifact["sha256"],
                    "replayed": artifact["replayed"],
                },
            )
            return {"output": artifact, "artifact": artifact}
        if isinstance(config, TypedWorkbookConfig):
            serialized = config.model_dump(mode="python", by_alias=True)
            artifact_workspace = self._artifact_workspace_for_run(
                run_id,
                workspace_path,
            )
            artifact = write_typed_workbook_artifact(
                workspace=artifact_workspace,
                spec=self._resolve(serialized["spec"], context),
                filename=config.filename,
                formula_policy=config.formula_policy,
                lineage=self._resolve(serialized["lineage"], context),
                run_id=run_id,
                node_id=scoped_id,
                application_id=state.application_id if state else "",
            )
            artifact["relative_path"] = (
                f".workflow-run-artifacts/{run_id}/"
                f"{artifact['relative_path']}"
            )
            await self._emit(
                run_id,
                "artifact.created",
                {
                    "node_id": scoped_id,
                    "relative_path": artifact["relative_path"],
                    "media_type": artifact["media_type"],
                    "size_bytes": artifact["size_bytes"],
                    "sha256": artifact["sha256"],
                    "replayed": artifact["replayed"],
                },
            )
            return {"output": artifact, "artifact": artifact}
        if isinstance(config, ConnectorActionConfig):
            if self.connector_service is None:
                raise RuntimeError("Connector service is not configured")
            operation = self._resolve_connector_operation(
                config,
                self._connector_operation_allowlists.get(run_id),
            )
            connector_manifest = await self.connector_service.get_manifest(
                config.connector_id,
                config.connector_version,
            )
            connector_operation = connector_manifest.operation(
                config.operation_id
            )
            tenant_id = str(self._resolve(config.tenant_id, context))
            actor_id = str(self._resolve(config.actor_id, context))
            actor_roles = self._resolve(config.actor_roles, context)
            profile_id = str(self._resolve(config.profile_id, context))
            payload = self._resolve(config.payload, context)
            idempotency_key = str(self._resolve(config.idempotency_key, context))
            authorization_id = str(self._resolve(config.authorization_id, context) or "")
            authorization_mode = str(
                self._resolve(config.authorization_mode, context)
            )
            execution_mode = str(self._resolve(config.execution_mode, context))
            if not isinstance(actor_roles, list) or not all(
                isinstance(item, str) and item for item in actor_roles
            ):
                raise ValueError("connector actor_roles must resolve to a non-empty string array")
            if not isinstance(payload, dict):
                raise ValueError("connector payload must resolve to an object")
            if execution_mode not in {"dry_run", "execute"}:
                raise ValueError("connector execution_mode must be dry_run or execute")
            if authorization_mode not in {"explicit", "runtime_exact"}:
                raise ValueError(
                    "connector authorization_mode must be explicit or runtime_exact"
                )
            if authorization_id and authorization_mode == "runtime_exact":
                raise ValueError(
                    "connector runtime_exact authorization cannot also supply "
                    "authorization_id"
                )
            if run_id in self._connector_operation_allowlists:
                if state is None or state.max_connector_payload_bytes is None:
                    raise WorkflowRuntimePayloadLimitExceeded(
                        "connector payload limit is absent from the assigned run policy"
                    )
                try:
                    payload_bytes = len(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                    )
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        "connector payload must be finite canonical JSON"
                    ) from error
                if payload_bytes > state.max_connector_payload_bytes:
                    raise WorkflowRuntimePayloadLimitExceeded(
                        "connector payload exceeds the assigned byte limit"
                    )
                writable = self._writable_connector_operations.get(
                    run_id,
                    frozenset(),
                )
                compensations = self._compensation_connector_operations.get(
                    run_id,
                    frozenset(),
                )
                if execution_mode == "execute":
                    if (
                        connector_operation.kind == "read"
                        and operation in {*writable, *compensations}
                    ):
                        raise WorkflowRuntimeConnectorScopeDenied(
                            "connector read is assigned to a mutating operation lane"
                        )
                    if (
                        connector_operation.kind == "write"
                        and operation not in writable
                    ):
                        raise WorkflowRuntimeConnectorScopeDenied(
                            "connector write is outside the assigned host operation policy"
                        )
                    if (
                        connector_operation.kind == "compensate"
                        and operation not in compensations
                    ):
                        raise WorkflowRuntimeConnectorScopeDenied(
                            "connector compensation is outside the assigned host operation policy"
                        )
                if (
                    execution_mode == "execute"
                    and connector_operation.mutating
                    and not authorization_id
                ):
                    if authorization_mode == "runtime_exact":
                        authorization_id = (
                            await self._issue_runtime_exact_connector_authorization(
                                config=config,
                                state=state,
                                run_id=run_id,
                                node_id=scoped_id,
                                tenant_id=tenant_id,
                                actor_id=actor_id,
                                profile_id=profile_id,
                                payload=payload,
                                idempotency_key=idempotency_key,
                            )
                        )
                    elif operation in self._permission_connector_operations.get(
                        run_id,
                        frozenset(),
                    ):
                        raise WorkflowRuntimePermissionScopeDenied(
                            "connector write requires an authorization receipt"
                        )
                if (
                    execution_mode == "execute"
                    and connector_operation.mutating
                    and state is not None
                ):
                    limit = state.max_connector_write_count
                    if idempotency_key not in state.connector_write_keys:
                        if (
                            limit is None
                            or state.connector_write_count >= limit
                        ):
                            raise WorkflowRuntimeWriteLimitExceeded(
                                "connector write limit is exhausted"
                            )
                        state.connector_write_count += 1
                        state.connector_write_keys.append(idempotency_key)
            if (
                run_id not in self._connector_operation_allowlists
                and execution_mode == "execute"
                and connector_operation.mutating
                and not authorization_id
                and authorization_mode == "runtime_exact"
            ):
                authorization_id = (
                    await self._issue_runtime_exact_connector_authorization(
                        config=config,
                        state=state,
                        run_id=run_id,
                        node_id=scoped_id,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        profile_id=profile_id,
                        payload=payload,
                        idempotency_key=idempotency_key,
                    )
                )
            execution = await self.connector_service.execute(
                ConnectorExecutionRequest(
                    connector_id=config.connector_id,
                    connector_version=config.connector_version,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    actor_roles=actor_roles,
                    profile_id=profile_id,
                    operation_id=config.operation_id,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    authorization_id=authorization_id,
                    dry_run=execution_mode == "dry_run",
                    application_id=state.application_id if state else "",
                    run_id=run_id,
                    assignment_id=(
                        state.assignment_id
                        if state is not None
                        and state.governed_host_actions
                        and state.assignment_id is not None
                        else ""
                    ),
                    session_id=(
                        state.session_id
                        if state is not None
                        and state.governed_host_actions
                        and state.session_id is not None
                        else ""
                    ),
                    allowed_network_hosts=(
                        list(state.allowed_network_hosts)
                        if state is not None
                        and state.governed_host_actions
                        and state.allowed_network_hosts is not None
                        else None
                    ),
                    allowed_compensation_operations=(
                        list(state.compensation_connector_operations)
                        if state is not None
                        and state.governed_host_actions
                        and state.compensation_connector_operations is not None
                        else None
                    ),
                    permission_required=(
                        execution_mode == "execute"
                        and connector_operation.mutating
                        and operation
                        in self._permission_connector_operations.get(
                            run_id,
                            frozenset(),
                        )
                    ),
                    assignment_max_write_count=(
                        state.max_connector_write_count
                        if state is not None and state.governed_host_actions
                        else None
                    ),
                    assignment_max_payload_bytes=(
                        state.max_connector_payload_bytes
                        if state is not None and state.governed_host_actions
                        else None
                    ),
                )
            )
            receipt = execution.public_receipt()
            await self._emit(
                run_id,
                "connector.execution.completed",
                {
                    "node_id": scoped_id,
                    "execution_id": execution.id,
                    "operation_id": execution.operation_id,
                    "status": execution.status,
                    "replayed": execution.replayed,
                },
            )
            return {
                "output": receipt,
                "receipt": receipt,
                "response": execution.response,
            }
        if isinstance(config, IterationConfig):
            items = self._resolve(config.items, context)
            if not isinstance(items, list):
                raise TypeError("iteration items must resolve to an array")
            variables = {
                key: self._resolve(value, context)
                for key, value in config.variables.items()
            }
            semaphore = asyncio.Semaphore(config.parallelism)

            async def one(index: int, item: Any) -> Any:
                async with semaphore:
                    nested_inputs = {
                        **inputs,
                        **variables,
                        config.item_name: item,
                        "index": index,
                    }
                    nested = await self._run_graph(
                        snapshot,
                        config.workflow,
                        nested_inputs,
                        workspace_path,
                        run_id,
                        prefix=f"{scoped_id}[{index}].",
                        top_state=state,
                    )
                    value: Any = nested.get(config.output_node_id)
                    for key in config.output_path:
                        value = value[key]
                    return value

            return {"items": await asyncio.gather(*(one(index, item) for index, item in enumerate(items)))}
        if isinstance(config, LoopConfig):
            variables = {key: self._resolve(value, context) for key, value in config.variables.items()}
            loop_state = (
                self._resolve(config.initial_state, context)
                if config.initial_state is not None
                else variables.get(config.state_input_name, {})
            )
            feedback = variables.get(config.feedback_input_name)
            previous: Any = None
            nested: dict[str, dict[str, Any]] = {}
            for index in range(config.max_iterations):
                nested_inputs = {
                    **inputs,
                    **variables,
                    "iteration": index,
                    "previous": previous,
                    config.state_input_name: loop_state,
                    config.feedback_input_name: feedback,
                }
                await self._emit(run_id, "loop.iteration.started", {
                    "node_id": scoped_id,
                    "iteration": index + 1,
                    "state": self._redact(loop_state),
                    "feedback": self._redact(feedback),
                })
                nested = await self._run_graph(
                    snapshot,
                    config.workflow,
                    nested_inputs,
                    workspace_path,
                    run_id,
                    prefix=f"{scoped_id}[{index}].",
                )
                loop_context = {"inputs": nested_inputs, "nodes": nested}
                output = nested.get(config.output_node_id, {})
                next_state = (
                    self._resolve(config.state_update, loop_context)
                    if config.state_update is not None
                    else output.get("state", loop_state) if isinstance(output, dict) else loop_state
                )
                next_feedback = (
                    self._resolve(config.feedback_value, loop_context)
                    if config.feedback_value is not None
                    else output.get("feedback", feedback) if isinstance(output, dict) else feedback
                )
                break_value = self._resolve(config.break_value, loop_context)
                break_condition = config.break_condition.model_copy(update={"value": break_value})
                should_break = self._evaluate(break_condition, loop_context)
                cancel_value: Any = None
                should_cancel = False
                if config.cancel_condition is not None:
                    cancel_value = self._resolve(config.cancel_value, loop_context)
                    cancel_condition = config.cancel_condition.model_copy(update={"value": cancel_value})
                    should_cancel = self._evaluate(cancel_condition, loop_context)
                if config.checkpoint_each_iteration:
                    checkpoint_id = f"{scoped_id}:iteration:{index + 1}"
                    await self.storage.save_checkpoint(
                        run_id,
                        checkpoint_id,
                        {
                            "node_id": scoped_id,
                            "iteration": index + 1,
                            "variables": variables,
                            "output_node_id": config.output_node_id,
                            "output": output,
                            "state": next_state,
                            "feedback": next_feedback,
                            "break_value": break_value,
                            "cancel_value": cancel_value,
                        },
                    )
                    await self._emit(run_id, "loop.checkpoint.saved", {
                        "node_id": scoped_id,
                        "checkpoint_id": checkpoint_id,
                        "iteration": index + 1,
                    })
                stop_reason = "cancelled" if should_cancel else "break_condition" if should_break else "continue"
                await self._emit(run_id, "loop.iteration.completed", {
                    "node_id": scoped_id,
                    "iteration": index + 1,
                    "state": self._redact(next_state),
                    "feedback": self._redact(next_feedback),
                    "break_value": self._redact(break_value),
                    "cancel_value": self._redact(cancel_value),
                    "stop_reason": stop_reason,
                })
                result = {
                    "output": output,
                    "iterations": index + 1,
                    "state": next_state,
                    "feedback": next_feedback,
                    "stop_reason": stop_reason,
                    "cancelled": should_cancel,
                }
                if should_cancel or should_break:
                    return result
                previous = output
                loop_state = next_state
                feedback = next_feedback
                variables[config.state_input_name] = loop_state
                variables[config.feedback_input_name] = feedback
            raise RuntimeError(f"loop did not meet break condition after {config.max_iterations} iterations")
        if isinstance(config, HumanInputConfig):
            preset = inputs.get("__human__", {}).get(node.id) if isinstance(inputs.get("__human__"), dict) else None
            if preset is not None:
                return {"output": preset, **preset}
            if state and scoped_id in state.human_input_values:
                values = state.human_input_values[scoped_id]
                return {"output": values, **values}
            if (
                state
                and state.waiting_node_id in {node.id, scoped_id}
                and state.resumed_values is not None
            ):
                values = dict(state.resumed_values)
                for field in config.fields:
                    if field.required and values.get(field.name) is None:
                        raise ValueError(f"missing required human input: {field.name}")
                state.human_input_values[scoped_id] = values
                state.waiting_node_id = None
                state.resumed_values = None
                await self.workflow_store.update_run(run_id, status="running", state=state)
                return {"output": values, **values}
            if not state:
                raise RuntimeError("human input is only supported in persisted top-level runs")
            state.waiting_node_id = scoped_id
            await self._emit(run_id, "human_input.required", {
                "node_id": scoped_id,
                "block_node_id": node.id,
                "title": config.title,
                "description": config.description,
                "fields": [field.model_dump(mode="json") for field in config.fields],
            })
            raise HumanInputPause()
        if isinstance(config, EndConfig):
            return {key: self._resolve(value, context) for key, value in config.outputs.items()}
        if isinstance(config, AnswerConfig):
            return {"answer": self._resolve(config.answer, context)}
        raise RuntimeError(f"block executor missing: {node.type}")

    async def _execute_agent_architecture_block(
        self,
        config: AgentArchitectureConfig,
        snapshot: ApplicationSnapshot,
        node: NodeSpec,
        context: dict[str, Any],
        workspace_path: str,
        run_id: str,
        scoped_id: str,
        state: WorkflowRunState | None,
    ) -> dict[str, Any]:
        value = self._resolve(config.input, context) if config.input is not None else self._incoming_value(node, context)
        settings = self._resolve(config.settings, context)
        if not isinstance(settings, dict):
            raise TypeError("agent architecture block settings must resolve to an object")

        async def emit_harness_signal(
            signal_type: str, status: str, details: dict[str, Any] | None = None
        ) -> None:
            await self._emit(run_id, "harness.signal", {
                "node_id": scoped_id,
                "block_type": node.type,
                "signal_type": signal_type,
                "status": status,
                "details": self._redact(details or {}),
            })

        # ── Context group ───────────────────────────────────────────

        if node.type == "context_assembler":
            fragments = settings.get("fragments", [])
            if not isinstance(fragments, list):
                raise TypeError("context_assembler.settings.fragments must be an array")
            resolved = [self._resolve(item, context) for item in fragments]
            assembled = {
                "input": value,
                "fragments": resolved,
                "nodes": json.loads(json.dumps(context["nodes"], ensure_ascii=False, default=str)),
            }
            return {"output": assembled, "state": {"mechanism": node.type, "fragment_count": len(resolved)}}

        if node.type == "workspace_context_injector":
            files = settings.get("files", [])
            workspace_context = {
                "workspace_path": workspace_path,
                "files": files,
                "input": value,
                "scope": settings.get("scope", "current_workspace"),
            }
            return {"output": workspace_context, "state": {"mechanism": node.type}}

        if node.type == "conversation_memory":
            facts = settings.get("facts", [])
            messages = settings.get("messages", value if isinstance(value, list) else [])
            memory = {"facts": facts, "messages": messages, "latest": value}
            return {"output": memory, "state": {"mechanism": node.type, "fact_count": len(facts)}}

        if node.type == "context_compactor":
            await self._emit(run_id, "context.compaction.started", {"node_id": scoped_id})
            max_chars = int(settings.get("max_chars", 4000))
            text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
            if len(text) > max_chars:
                preserve = "\n".join(
                    f"{fact}" for fact in settings.get("preserved_facts", [])
                )
                header = f"<compacted>\nPreserved facts:\n{preserve}\n"
                body_start = max_chars - len(header) - 80
                compacted = header + text[:max(0, body_start)] + "\n...[compacted]"
            else:
                compacted = text
            result = {
                "summary": compacted,
                "dropped_chars": max(0, len(text) - len(compacted)),
                "preserved_facts": settings.get("preserved_facts", []),
            }
            await self._emit(run_id, "context.compaction.completed", {"node_id": scoped_id, **result})
            return {"output": result, "state": {"mechanism": node.type}}

        # ── Model loop group ────────────────────────────────────────

        if node.type == "model_turn":
            prompt = self._model_turn_prompt(
                settings.get("prompt"),
                value,
                context.get("inputs", {}),
            )
            tool_names = [str(t) for t in settings.get("tools", [])]
            system = str(settings.get("system") or settings.get("system_prompt") or "You are a precise coding agent runtime block.")
            model = str(settings.get("model") or self.runtime_model)

            if tool_names:
                result = await self._model_turn_with_tools(
                    run_id, model, system, prompt, scoped_id, tool_names,
                )
                return {
                    "output": result,
                    "text": result["text"],
                    "state": {"mechanism": node.type, "tool_count": len(tool_names)},
                }

            text, usage = await self._model_text(run_id, model, system, prompt, scoped_id)
            output: dict[str, Any] = {
                "text": text,
                "usage": usage.model_dump(mode="json"),
                "tool_use_blocks": [],
                "stop_reason": None,
            }
            structured: dict[str, Any] | None = None
            if "json" in system.casefold() or str(settings.get("output_format", "")).casefold() == "json":
                try:
                    parsed = self._json_from_text(text)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    structured = parsed
                    output.update(structured)
                    output["structured"] = structured
            result: dict[str, Any] = {
                "output": output,
                "text": text,
                "state": {"mechanism": node.type},
            }
            if structured is not None:
                result["structured"] = structured
                for key, item in structured.items():
                    if key not in result:
                        result[key] = item
            return result

        if node.type == "tool_call_router":
            tool_use_blocks: list[dict[str, Any]] = []
            if isinstance(value, dict):
                tool_use_blocks = value.get("tool_use_blocks", [])
                if not tool_use_blocks:
                    raw_text = value.get("text", "")
                    if isinstance(raw_text, str):
                        parsed = self._parse_tool_use_from_text(raw_text)
                        if parsed:
                            tool_use_blocks = parsed
            elif isinstance(value, str):
                parsed = self._parse_tool_use_from_text(value)
                if parsed:
                    tool_use_blocks = parsed
            if not tool_use_blocks:
                return {
                    "output": {"tool_calls": [], "no_tool_calls": True, "source": value},
                    "state": {"mechanism": node.type, "routed_count": 0},
                }
            routed = []
            for tb in tool_use_blocks:
                tool_name = tb.get("name", "")
                tool_input = tb.get("input", {})
                routed.append({
                    "tool_use_id": tb.get("id", ""),
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "routed": True,
                })
            await self._emit(run_id, "tool_call_router.routed", {
                "node_id": scoped_id,
                "count": len(routed),
                "tools": [r["tool_name"] for r in routed],
            })
            return {
                "output": {"tool_calls": routed, "no_tool_calls": False, "count": len(routed)},
                "state": {"mechanism": node.type, "routed_count": len(routed)},
            }

        if node.type == "stop_continue_controller":
            reason = str(settings.get("stop_reason", ""))
            if not reason and isinstance(value, dict):
                reason = str(value.get("stop_reason", ""))
            has_tool_calls = False
            if isinstance(value, dict):
                tool_blocks = value.get("tool_use_blocks", [])
                has_tool_calls = bool(tool_blocks)
                if not reason and not tool_blocks:
                    text = value.get("text", "")
                    if isinstance(text, str) and len(text.strip()) > 0:
                        reason = "end_turn"
            stop_reasons: dict[str, bool] = {
                "end_turn": reason == "end_turn",
                "tool_use": reason == "tool_use" or has_tool_calls,
                "max_tokens": reason == "max_tokens",
                "stop_sequence": reason == "stop_sequence",
                "refusal": reason == "refusal",
            }
            should_continue = (
                stop_reasons["tool_use"]
                or stop_reasons["max_tokens"]
                or (not reason and not stop_reasons["end_turn"])
            )
            return {
                "output": {
                    "input": value,
                    "stop_reason": reason or ("tool_use" if has_tool_calls else "end_turn"),
                    "continue": should_continue,
                    "stop_reasons": stop_reasons,
                },
                "state": {
                    "mechanism": node.type,
                    "continue": should_continue,
                    "stop_reason": reason,
                },
            }

        if node.type == "retry_error_classifier":
            error_text = str(settings.get("error") or value or "")
            if isinstance(value, dict) and value.get("error"):
                error_text = str(value["error"])
            elif isinstance(value, str) and not error_text:
                error_text = value
            classified = self._classify_error(error_text, settings)
            retryable = classified["retryable"]
            if retryable:
                delay = float(settings.get("retry_delay_seconds", 2 ** (classified.get("attempt", 1) - 1)))
                classified["retry_delay"] = min(delay, 60)
            await self._emit(run_id, "error.classified", {
                "node_id": scoped_id,
                "class": classified["class"],
                "retryable": retryable,
            })
            return {
                "output": {"error": error_text, **classified},
                "state": {"mechanism": node.type, **classified},
            }

        # ── Tools group ─────────────────────────────────────────────

        if node.type == "tool_executor":
            tool_name = settings.get("tool_name")
            if not tool_name:
                if isinstance(value, dict) and value.get("tool_calls"):
                    calls = value["tool_calls"]
                    if calls and isinstance(calls, list) and calls[0].get("tool_name"):
                        tool_name = str(calls[0]["tool_name"])
                        tool_input_value = calls[0].get("tool_input", {})
                    else:
                        raise ValueError("tool_executor.settings.tool_name is required and no routed tool calls found")
                else:
                    raise ValueError("tool_executor.settings.tool_name is required")
            else:
                tool_input_value = settings.get("tool_input", value if isinstance(value, dict) else {"input": value})
            requested_workspace = self._resolve(
                settings.get("workspace_path")
                or (
                    value.get("workspace")
                    if isinstance(value, dict) and value.get("workspace")
                    else workspace_path
                ),
                context,
            )
            effective_workspace = self._workspace_for_run(run_id, str(requested_workspace))
            result = await self._execute_tool(
                ToolConfig(tool_name=str(tool_name), input=tool_input_value),
                snapshot,
                context,
                effective_workspace,
                run_id,
                scoped_id,
                owner_id=state.application_id if state else "",
                state=state,
            )
            return {"output": result["output"], "state": {"mechanism": node.type, "tool_name": tool_name}}

        if node.type == "tool_result_normalizer":
            normalized = value
            if isinstance(value, str):
                try:
                    normalized = json.loads(value)
                except json.JSONDecodeError:
                    normalized = {"text": value}
            elif isinstance(value, dict):
                has_tool_output = value.get("output")
                if has_tool_output is not None:
                    inner = has_tool_output
                    if isinstance(inner, str):
                        try:
                            inner = json.loads(inner)
                        except json.JSONDecodeError:
                            inner = {"text": inner}
                    normalized = {
                        "tool_output": inner,
                        "normalized_at": scoped_id,
                        "source": value,
                    }
            return {"output": normalized, "state": {"mechanism": node.type}}

        if node.type == "permission_gate":
            preset = context["inputs"].get("__permissions__", {}) if isinstance(context["inputs"].get("__permissions__"), dict) else {}
            mode = str(settings.get("mode", "always_ask"))
            # Harness-inspired three-level permission system:
            #   always_ask  — pause and request approval before every sensitive step
            #   plan_first  — first show what will be done, then ask for approval once
            #   auto_approve — bypass approval entirely (for trusted workflows)
            approved = (
                mode == "auto_approve"
                or bool(preset.get(node.id))
                or bool(settings.get("auto_approve"))  # legacy compat
            )
            if state and state.waiting_node_id == node.id and state.resumed_values is not None:
                approved = state.resumed_values.get("behavior") == "allow" or bool(state.resumed_values.get("approved"))
            limit = int(settings.get("max_auto_per_hour", 0))
            if approved and limit > 0:
                # Track auto-approval count from context
                auto_count = context.get("_auto_approve_count", 0)
                if auto_count >= limit:
                    approved = False  # escalate to manual for safety
            # Emit plan event for plan_first mode regardless of approval
            if mode == "plan_first":
                await self._emit(run_id, "permission.plan", {
                    "node_id": scoped_id,
                    "reason": settings.get("reason", "Sensitive action requires review."),
                    "plan": self._redact(value),
                    "auto_approved": approved,
                })
            if not approved:
                if not state:
                    raise RuntimeError("permission_gate requires persisted top-level runs when approval is not preset")
                state.waiting_node_id = node.id
                await emit_harness_signal("permission", "waiting", {
                    "mode": mode,
                    "reason": settings.get("reason", "Sensitive action requires approval."),
                })
                await self._emit(run_id, "permission.requested", {
                    "node_id": scoped_id,
                    "reason": settings.get("reason", "Sensitive action requires approval."),
                    "mode": mode,
                    "input": self._redact(value),
                })
                raise HumanInputPause()
            await self._emit(run_id, "permission.resolved", {"node_id": scoped_id, "mode": mode, "behavior": "allow"})
            await emit_harness_signal("permission", "allowed", {"mode": mode})
            return {"output": value, "state": {"mechanism": node.type, "approved": True, "mode": mode}}

        if node.type == "sandbox_boundary":
            declared_workspace = self._workspace_for_run(
                run_id,
                str(self._resolve(settings.get("workspace", workspace_path), context)),
            )
            network_policy = str(settings.get("network_policy", "none"))
            effective_policy = network_policy if network_policy in {"none", "full", "allowlist"} else "none"
            if run_id in self._network_host_allowlists and effective_policy != "none":
                raise WorkflowRuntimeNetworkScopeDenied(
                    "sandbox network access is outside the assigned run policy"
                )
            self.sandboxes.resolve_workspace(declared_workspace)
            await emit_harness_signal("sandbox", "declared", {
                "workspace": declared_workspace,
                "network_policy": effective_policy,
            })
            await self._emit(run_id, "sandbox.boundary.declared", {
                "node_id": scoped_id,
                "workspace": declared_workspace,
                "network_policy": effective_policy,
            })
            return {
                "output": {
                    "input": value,
                    "workspace": declared_workspace,
                    "network_policy": effective_policy,
                },
                "state": {
                    "mechanism": node.type,
                    "workspace": declared_workspace,
                    "network_policy": effective_policy,
                },
            }

        # ── Skill / MCP group ───────────────────────────────────────

        if node.type == "skill_loader":
            skill_names = settings.get("skills", [])
            if isinstance(skill_names, str):
                skill_names = [s.strip() for s in skill_names.split(",") if s.strip()]
            loaded: list[dict[str, Any]] = []
            for name in skill_names:
                agent_skill = next(
                    (s for s in (snapshot.agents or {}).values() if s.name == name), None
                )
                if agent_skill is None:
                    loaded.append({"name": name, "status": "not_found", "instructions": ""})
                    continue
                skill_def = next(
                    (s for s in (agent_skill.skills or []) if s.name == name), None
                )
                loaded.append({
                    "name": name,
                    "status": "loaded",
                    "instructions": skill_def.instructions if skill_def else agent_skill.system_prompt,
                    "tools": agent_skill.tools or [],
                })
            await self._emit(run_id, "skill.loaded", {
                "node_id": scoped_id,
                "skills": [s["name"] for s in loaded],
            })
            return {
                "output": {"skills": loaded, "input": value},
                "state": {"mechanism": node.type, "loaded_count": len(loaded)},
            }

        if node.type == "mcp_gateway":
            if run_id in self._runtime_tool_allowlists:
                raise WorkflowRuntimeToolScopeDenied(
                    "MCP process launch is outside the assigned run policy"
                )
            servers = settings.get("servers", [])
            if isinstance(servers, dict):
                servers = [servers]
            discovered: list[dict[str, Any]] = []
            for server in servers:
                server_name = str(server.get("name", "unnamed"))
                try:
                    from .tools.mcp import MCPClient
                    client = MCPClient(
                        command=str(server.get("command", "")),
                        args=[str(a) for a in server.get("args", [])],
                        env={str(k): str(v) for k, v in server.get("env", {}).items()},
                    )
                    async with client:
                        cap = await client.list_tools()
                    discovered.append({
                        "name": server_name,
                        "status": "connected",
                        "tools": [t.get("name", "") for t in cap.get("tools", [])],
                        "raw_capabilities": cap,
                    })
                except Exception as exc:
                    discovered.append({
                        "name": server_name,
                        "status": "failed",
                        "error": str(exc),
                    })
            await self._emit(run_id, "mcp.gateway.discovered", {
                "node_id": scoped_id,
                "servers": [d["name"] for d in discovered],
            })
            return {
                "output": {"mcp_servers": discovered, "input": value},
                "state": {"mechanism": node.type, "server_count": len(discovered)},
            }

        if node.type == "capability_registry":
            tool_names = [str(t) for t in settings.get("tools", [])]
            skill_list = settings.get("skills", [])
            mcp_list = settings.get("mcp_servers", [])
            if isinstance(value, dict):
                if value.get("skills"):
                    for s in value["skills"]:
                        if isinstance(s, dict):
                            skill_list.append(s.get("name", ""))
                            tool_names.extend(s.get("tools", []))
                if value.get("mcp_servers"):
                    for s in value["mcp_servers"]:
                        if isinstance(s, dict) and s.get("status") == "connected":
                            mcp_list.append(s.get("name", ""))
                            tool_names.extend(s.get("tools", []))
            all_tools: list[str] = sorted(set(t for t in tool_names if t))
            all_skills: list[str] = sorted(set(s for s in skill_list if isinstance(s, str) and s))
            all_mcp: list[str] = sorted(set(s for s in mcp_list if isinstance(s, str) and s))
            registry = {
                "tools": all_tools,
                "skills": all_skills,
                "mcp_servers": all_mcp,
                "total_capabilities": len(all_tools) + len(all_skills),
            }
            await self._emit(run_id, "capability.registry.built", {
                "node_id": scoped_id,
                "tool_count": len(all_tools),
                "skill_count": len(all_skills),
            })
            return {
                "output": {"registry": registry, "input": value},
                "state": {"mechanism": node.type, **registry},
            }

        # ── Multi-agent group ───────────────────────────────────────

        if node.type == "subagent_spawn":
            task = str(settings.get("task") or value or "")
            if not task:
                raise ValueError("subagent_spawn.settings.task is required")
            tools = [str(item) for item in settings.get("tools", [])]
            for tool_name in tools:
                self._validate_runtime_tool_target(
                    tool_name,
                    self._runtime_tool_allowlists.get(run_id),
                )
            allowed_network_hosts = self._network_host_allowlists.get(run_id)
            budget = settings.get("budget", {}) if isinstance(settings.get("budget", {}), dict) else {}
            max_turns = int(budget.get("max_rounds", settings.get("max_turns", 4)))
            max_budget_usd = budget.get("max_cost_usd", settings.get("max_budget_usd"))
            subagent = AgentSpec(
                name=str(settings.get("name") or node.title or "Workflow subagent"),
                description=str(settings.get("description") or "Executes one bounded workflow subtask."),
                system_prompt=str(settings.get("system_prompt") or (
                    "You are a bounded subagent spawned by a workflow architecture block. "
                    "Use only the assigned context and enabled tools, report concise evidence, "
                    "and stop when the bounded task is complete."
                )),
                tools=tools,
                permission_mode=PermissionMode.bypass,
                network_policy=(
                    "allowlist"
                    if allowed_network_hosts
                    else "none"
                    if allowed_network_hosts is not None
                    else "full"
                ),
                network_allowlist=sorted(allowed_network_hosts or ()),
                max_turns=max_turns,
                max_budget_usd=float(max_budget_usd) if max_budget_usd is not None else None,
                allow_subagents=False,
            )
            subagent = self._restricted_agent(
                subagent,
                self._runtime_tool_allowlists.get(run_id),
            )
            version = await self.storage.save_agent_version(subagent, "workflow-subagent")
            session_id = f"{run_id}-{scoped_id}-subagent"
            session = await self.agent_runtime.create_session(
                subagent,
                version,
                self._workspace_for_run(
                    run_id,
                    str(
                        self._resolve(
                            settings.get("workspace_path") or workspace_path,
                            context,
                        )
                    ),
                ),
                session_id=session_id,
                governance_owner_id=state.application_id if state else None,
                governance_application_id=state.application_id if state else None,
                allow_secret_references=run_id not in self._runtime_tool_allowlists,
            )
            await self._emit(run_id, "subagent.started", {
                "node_id": scoped_id,
                "session_id": session.id,
                "tools": tools,
                "budget": {"max_turns": max_turns, "max_budget_usd": max_budget_usd},
                "task": task,
            })

            async def relay_subagent_event(kind: str, data: dict[str, Any]) -> None:
                await self._emit(run_id, "subagent.event", {
                    "node_id": scoped_id,
                    "session_id": session.id,
                    "event": kind,
                    "data": self._redact(data),
                })

            self.agent_runtime.register_event_relay(session.id, relay_subagent_event)
            try:
                result = await self.agent_runtime.run_turn_and_wait(session, task)
            finally:
                self.agent_runtime.unregister_event_relay(session.id)
            await self._emit(run_id, "subagent.completed", {
                "node_id": scoped_id,
                "session_id": session.id,
                "usage": session.usage.model_dump(mode="json"),
                "result": result[:20_000],
            })
            return {
                "output": result,
                "state": {
                    "mechanism": node.type,
                    "session_id": session.id,
                    "tools": tools,
                    "max_turns": max_turns,
                    "max_budget_usd": max_budget_usd,
                    "usage": session.usage.model_dump(mode="json"),
                },
            }

        if node.type == "task_dispatcher":
            tasks = settings.get("tasks", [])
            if isinstance(value, list) and not tasks:
                tasks_raw = value
                tasks = []
                for t in tasks_raw:
                    if isinstance(t, str):
                        tasks.append({"name": t, "dependencies": [], "owner": None})
                    elif isinstance(t, dict):
                        tasks.append(t)
            if not tasks:
                return {
                    "output": {"dispatch_plan": [], "message": "no tasks to dispatch"},
                    "state": {"mechanism": node.type, "dispatched": 0},
                }
            ordered = self._topological_task_sort(tasks)
            dispatched = []
            for idx, task in enumerate(ordered):
                dispatched.append({
                    "order": idx,
                    "name": task.get("name", task.get("subject", f"task-{idx}")),
                    "dependencies": task.get("dependencies", task.get("blocked_by", [])),
                    "owner": task.get("owner"),
                    "status": "ready" if idx == 0 else "waiting",
                })
            await self._emit(run_id, "task.dispatched", {
                "node_id": scoped_id,
                "total": len(dispatched),
            })
            return {
                "output": {"dispatch_plan": dispatched, "total": len(dispatched)},
                "state": {"mechanism": node.type, "dispatched": len(dispatched)},
            }

        if node.type == "mailbox_wait_wake":
            preset = context["inputs"].get("__mailbox__", {}) if isinstance(context["inputs"].get("__mailbox__"), dict) else {}
            expected = settings.get("expect_messages", settings.get("messages", []))
            if isinstance(expected, str):
                expected = [expected]
            found = preset.get(node.id) or settings.get("messages") or []
            if state and state.waiting_node_id == node.id and state.resumed_values is not None:
                resumed_messages = state.resumed_values.get("messages", state.resumed_values)
                found = resumed_messages if isinstance(resumed_messages, list) else [resumed_messages]
            if not found:
                if not state:
                    raise RuntimeError("mailbox_wait_wake requires persisted top-level runs when no message is preset")
                state.waiting_node_id = node.id
                await self._emit(run_id, "mailbox.waiting", {
                    "node_id": scoped_id,
                    "expected": expected,
                    "input": self._redact(value),
                })
                raise HumanInputPause()
            matched = [m for m in found if not expected or any(
                str(e).casefold() in str(m).casefold() for e in expected
            )]
            if not matched and expected:
                if not state:
                    raise RuntimeError("mailbox_wait_wake: expected messages not matched")
                state.waiting_node_id = node.id
                await self._emit(run_id, "mailbox.waiting", {
                    "node_id": scoped_id,
                    "expected": expected,
                    "received": self._redact(found),
                })
                raise HumanInputPause()
            final_messages = matched or found
            await self._emit(run_id, "mailbox.woke", {
                "node_id": scoped_id,
                "matched": len(matched) if matched else len(found),
            })
            return {
                "output": {"messages": final_messages, "awake": True, "input": value},
                "state": {
                    "mechanism": node.type,
                    "awake": True,
                    "messages": final_messages,
                    "message_count": len(final_messages),
                },
            }

        if node.type == "dependency_gate":
            dependencies = settings.get("dependencies", [])
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            completed_raw = settings.get("completed", [])
            if isinstance(settings.get("completed"), str):
                completed_raw = [completed_raw]
            completed = set(str(c) for c in completed_raw)
            if isinstance(value, dict):
                upstream_completed = value.get("completed", [])
                if isinstance(upstream_completed, list):
                    completed.update(str(c) for c in upstream_completed)
            blocked = [d for d in dependencies if str(d) not in completed]
            all_satisfied = len(blocked) == 0
            if not all_satisfied:
                await self._emit(run_id, "dependency.blocked", {
                    "node_id": scoped_id,
                    "blocked_by": blocked,
                    "completed": sorted(completed),
                })
            return {
                "output": {
                    "input": value,
                    "dependencies": dependencies,
                    "completed": sorted(completed),
                    "blocked": blocked,
                    "all_satisfied": all_satisfied,
                },
                "state": {
                    "mechanism": node.type,
                    "blocked": blocked,
                    "all_satisfied": all_satisfied,
                },
            }

        # ── Governance group ────────────────────────────────────────

        if node.type == "budget_gate":
            max_cost = settings.get("max_cost_usd")
            spent = float(settings.get("spent_cost_usd", 0))
            if isinstance(value, dict) and value.get("usage"):
                usage = value["usage"]
                if isinstance(usage, dict):
                    spent += float(usage.get("cost_usd", 0))
            allowed = max_cost is None or spent <= float(max_cost)
            if not allowed:
                await self._emit(run_id, "budget.exceeded", {
                    "node_id": scoped_id,
                    "spent": spent,
                    "max": max_cost,
                })
            await emit_harness_signal("budget", "allowed" if allowed else "blocked", {
                "spent_cost_usd": spent,
                "max_cost_usd": max_cost,
            })
            return {
                "output": {"input": value, "allowed": allowed, "spent_cost_usd": spent, "max_cost_usd": max_cost},
                "state": {"mechanism": node.type, "allowed": allowed, "spent_cost_usd": spent, "max_cost_usd": max_cost},
            }

        if node.type == "round_limit":
            current_round = int(settings.get("current_round", 0))
            max_rounds = int(settings.get("max_rounds", 30))
            allowed = current_round < max_rounds
            if not allowed:
                await self._emit(run_id, "round_limit.reached", {
                    "node_id": scoped_id,
                    "current": current_round,
                    "max": max_rounds,
                })
            await emit_harness_signal("round_limit", "allowed" if allowed else "blocked", {
                "current_round": current_round,
                "max_rounds": max_rounds,
            })
            return {
                "output": {"input": value, "allowed": allowed, "current_round": current_round, "max_rounds": max_rounds},
                "state": {"mechanism": node.type, "allowed": allowed, "current_round": current_round, "max_rounds": max_rounds},
            }

        if node.type == "soft_block":
            from .soft_block import get_discrete_block_type
            strategy = str(settings.get("strategy", "context_assemble"))
            discrete_type = get_discrete_block_type(strategy)
            if discrete_type is None:
                raise RuntimeError(f"soft_block: unknown strategy: {strategy}")

            # SoftBlock is a design-time macro: at runtime it delegates directly
            # to the equivalent discrete block. No runtime strategy selection.
            return await self._execute_agent_architecture_block(
                AgentArchitectureConfig(
                    input=config.input,
                    settings=settings,
                ),
                snapshot,
                NodeSpec(
                    id=node.id, type=discrete_type, title=node.title,
                    config={"input": config.input, "settings": settings},
                ),
                context,
                workspace_path,
                run_id,
                scoped_id,
                state,
            )

        if node.type == "hook_point":
            hook_name = str(settings.get("hook_name", node.title))
            direction = str(settings.get("direction", "before"))
            timeout_s = float(settings.get("timeout_seconds", 30))
            default_behavior = str(settings.get("default_behavior", "continue"))
            await emit_harness_signal("hook", "triggered", {
                "hook_name": hook_name,
                "direction": direction,
                "timeout_seconds": timeout_s,
                "default_behavior": default_behavior,
            })
            await self._emit(run_id, "hook.triggered", {
                "node_id": scoped_id,
                "hook_name": hook_name,
                "direction": direction,
                "payload": self._redact(value),
            })
            # External systems can listen to "hook.triggered" events via SSE
            # and respond through a resume-like mechanism. For now, hooks are
            # non-blocking and always continue.
            return {
                "output": value,
                "state": {
                    "mechanism": node.type,
                    "hook_name": hook_name,
                    "direction": direction,
                    "triggered": True,
                },
            }

        if node.type == "event_recorder":
            event = {"node_id": scoped_id, "label": settings.get("label", node.title), "payload": self._redact(value)}
            await emit_harness_signal("event", "recorded", {"label": event["label"]})
            await self._emit(run_id, "agent_architecture.event", event)
            return {"output": value, "state": {"mechanism": node.type, "recorded": True}}

        if node.type == "checkpoint_resume":
            checkpoint_id = str(settings.get("checkpoint_id", f"{run_id}:{scoped_id}"))
            checkpoint_data = {
                "checkpoint_id": checkpoint_id,
                "node_id": scoped_id,
                "run_id": run_id,
                "workspace_path": workspace_path,
                "completed_nodes": list(state.completed) if state else [],
                "outputs_snapshot": {
                    k: self._redact(v) for k, v in (state.outputs if state else {}).items()
                } if state else {},
                "value_snapshot": self._redact(value),
                "timestamp_utc": str(scoped_id),  # scoped_id carries run prefix
            }
            # Persist checkpoint data to storage for crash recovery
            await self.storage.save_checkpoint(run_id, checkpoint_id, checkpoint_data)
            await emit_harness_signal("checkpoint", "saved", {"checkpoint_id": checkpoint_id})
            await self._emit(run_id, "checkpoint.saved", {
                "node_id": scoped_id,
                "checkpoint_id": checkpoint_id,
                "completed_count": len(checkpoint_data["completed_nodes"]),
            })
            return {
                "output": {"input": value, "checkpoint": checkpoint_data},
                "state": {"mechanism": node.type, "checkpoint_id": checkpoint_id, "checkpoint_data": checkpoint_data},
            }

        if node.type == "cancellation_point":
            is_cancelled = bool(settings.get("cancelled", False))
            if state and state.run_id in self.active_tasks:
                task = self.active_tasks.get(state.run_id)
                if task and task.cancelled():
                    is_cancelled = True
            await self._emit(run_id, "cancellation.checked", {
                "node_id": scoped_id,
                "cancelled": is_cancelled,
            })
            await emit_harness_signal("cancellation", "cancelled" if is_cancelled else "clear", {
                "cancelled": is_cancelled,
            })
            return {
                "output": {"input": value, "cancelled": is_cancelled},
                "state": {"mechanism": node.type, "cancelled": is_cancelled},
            }

        raise RuntimeError(f"unknown agent architecture block: {node.type}")

    @staticmethod
    def _parse_tool_use_from_text(text: str) -> list[dict[str, Any]]:
        """Best-effort parse of tool-use intents from free-form model text.

        Detects ``<tool_call>``, ``<function_call>`` XML tags and
        ``tool_use`` JSON blocks that DeepSeek may embed inline.
        """
        results: list[dict[str, Any]] = []
        if not isinstance(text, str) or not text.strip():
            return results
        # XML-style: <tool_call>{"name": "Read", "input": {...}}</tool_call>
        xml_pattern = re.compile(
            r"<(?:tool_call|function_call|invoke)>\s*(.*?)\s*</(?:tool_call|function_call|invoke)>",
            re.DOTALL | re.IGNORECASE,
        )
        for match in xml_pattern.finditer(text):
            try:
                parsed = json.loads(match.group(1))
                results.append({
                    "name": parsed.get("name", parsed.get("tool", "")),
                    "input": parsed.get("input", parsed.get("arguments", parsed.get("args", {}))),
                })
            except json.JSONDecodeError:
                pass
        if results:
            return results
        # JSON code-fence: ```json {"tool": "Read", ...} ```
        json_fence = re.compile(r"```(?:json)?\s*\n?\s*(\{.*?\})\s*\n?\s*```", re.DOTALL | re.IGNORECASE)
        for match in json_fence.finditer(text):
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, dict) and (parsed.get("tool") or parsed.get("name")):
                    results.append({
                        "name": parsed.get("name", parsed.get("tool", "")),
                        "input": parsed.get("input", parsed.get("arguments", {})),
                    })
            except json.JSONDecodeError:
                pass
        if results:
            return results
        # Free JSON object with tool/name field
        brace_pattern = re.compile(r"\{[^{}]*\"(?:tool|name)\"[^{}]*\}", re.IGNORECASE)
        for match in brace_pattern.finditer(text):
            try:
                parsed = json.loads(match.group(0))
                results.append({
                    "name": parsed.get("name", parsed.get("tool", "")),
                    "input": parsed.get("input", parsed.get("arguments", {})),
                })
            except json.JSONDecodeError:
                pass
        return results

    @staticmethod
    def _classify_error(error_text: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Classify an error string into retryable / fatal categories."""
        error_lower = error_text.casefold()
        retryable_tokens = [
            "timeout", "timed out", "rate limit", "rate limited", "too many requests",
            "temporary", "transient", "retry", "connection", "network",
            "503", "502", "504", "429",
        ]
        permission_tokens = [
            "permission denied", "unauthorized", "forbidden", "access denied",
            "not allowed", "401", "403",
        ]
        tool_tokens = [
            "tool not found", "unknown tool", "tool error", "execution failed",
            "syntax error", "syntaxerror", "nameerror", "attributeerror", "modulenotfounderror",
            "command not found", "no such file", "traceback",
        ]
        fatal_tokens = [
            "api key", "authentication", "invalid request", "quota exceeded",
            "billing", "insufficient", "not available",
        ]
        retryable = any(t in error_lower for t in retryable_tokens)
        is_permission = any(t in error_lower for t in permission_tokens)
        is_tool = any(t in error_lower for t in tool_tokens)
        is_fatal = any(t in error_lower for t in fatal_tokens)
        if is_fatal:
            error_class = "fatal"
            retryable = False
        elif is_permission:
            error_class = "permission"
            retryable = False
        elif is_tool:
            error_class = "tool"
        elif retryable:
            error_class = "retryable"
        else:
            error_class = "unknown"
        return {
            "class": error_class,
            "retryable": retryable,
            "permission_error": is_permission,
            "tool_error": is_tool,
            "fatal": is_fatal,
        }

    @staticmethod
    def _topological_task_sort(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sort tasks by dependency order using Kahn's algorithm."""
        name_to_task: dict[str, dict[str, Any]] = {}
        for t in tasks:
            name = t.get("name", t.get("subject", str(id(t))))
            name_to_task[name] = t
        indegree: dict[str, int] = {n: 0 for n in name_to_task}
        outgoing: dict[str, list[str]] = {n: [] for n in name_to_task}
        for name, task in name_to_task.items():
            deps = task.get("dependencies", task.get("blocked_by", []))
            if isinstance(deps, str):
                deps = [deps]
            for dep in deps:
                dep_str = str(dep)
                if dep_str in name_to_task:
                    outgoing[dep_str].append(name)
                    indegree[name] += 1
        queue: deque[str] = deque(n for n, d in indegree.items() if d == 0)
        ordered: list[dict[str, Any]] = []
        while queue:
            current = queue.popleft()
            ordered.append(name_to_task[current])
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if len(ordered) < len(name_to_task):
            remaining = [n for n in name_to_task if n not in {t.get("name", t.get("subject", "")) for t in ordered}]
            ordered.extend(name_to_task[n] for n in remaining)
        return ordered

    @staticmethod
    def _incoming_value(node: NodeSpec, context: dict[str, Any]) -> Any:
        if not context["nodes"]:
            return None
        return next(reversed(context["nodes"].values()))

    async def _model_text(
        self, run_id: str, model: str, system: str, prompt: str, node_id: str
    ) -> tuple[str, Usage]:
        await self.harness.record_usage(
            run_id,
            "model_call",
            metadata={"node_id": node_id, "model": model, "mode": "text"},
        )
        stream = self.provider.stream(
            model=model,
            system=system,
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text=prompt)])],
            tools=[],
            max_output_tokens=8_192,
            thinking_enabled=True,
            effort="xhigh",
            user_id=run_id,
        )
        response = await self.agent_runtime._collect_stream(
            run_id, stream, f"node.{node_id}.model", model
        )
        await self.harness.record_model_usage(
            run_id,
            response.usage,
            model=model,
            provider=self.provider.provider_name_for(model),
            metadata={"node_id": node_id, "phase": "workflow_model_text"},
        )
        text = "".join(block.text or "" for block in response.blocks if block.type == "text")
        return text, response.usage

    async def _model_turn_with_tools(
        self,
        run_id: str,
        model: str,
        system: str,
        prompt: str,
        node_id: str,
        tool_names: list[str],
    ) -> dict[str, Any]:
        """Execute a model turn with optional tool definitions.

        Returns a dict with ``text``, ``tool_use_blocks``, and ``usage`` so
        downstream agent-architecture blocks can inspect and route tool calls.
        """
        from .models import ToolDefinition as TD
        await self.harness.record_usage(
            run_id,
            "model_call",
            metadata={"node_id": node_id, "model": model, "mode": "tool_turn"},
        )
        definitions: list[Any] = []
        for name in tool_names:
            self._validate_runtime_tool_target(
                name,
                self._runtime_tool_allowlists.get(run_id),
            )
            try:
                tool = self.tools.get(name)
                definitions.append(tool.definition())
            except KeyError:
                definitions.append(TD(
                    name=name,
                    description=f"Tool: {name}",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": True},
                ))
        stream = self.provider.stream(
            model=model,
            system=system,
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text=prompt)])],
            tools=definitions,
            max_output_tokens=8_192,
            thinking_enabled=True,
            effort="xhigh",
            tool_choice={"type": "auto"} if definitions else {"type": "none"},
            user_id=run_id,
        )
        response = await self.agent_runtime._collect_stream(
            run_id, stream, f"node.{node_id}.model", model
        )
        await self.harness.record_model_usage(
            run_id,
            response.usage,
            model=model,
            provider=self.provider.provider_name_for(model),
            metadata={"node_id": node_id, "phase": "workflow_model_tool_turn"},
        )
        text = "".join(block.text or "" for block in response.blocks if block.type == "text")
        thinking = "".join(
            block.thinking or "" for block in response.blocks if block.type == "thinking"
        )
        tool_use_blocks: list[dict[str, Any]] = []
        for block in response.blocks:
            if block.type == "tool_use" and block.name:
                tool_use_blocks.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input or {},
                })
        return {
            "text": text,
            "thinking": thinking,
            "tool_use_blocks": tool_use_blocks,
            "stop_reason": response.stop_reason,
            "usage": response.usage.model_dump(mode="json"),
            "raw_blocks": [block.model_dump(mode="json") for block in response.blocks],
        }

    @staticmethod
    def _workflow_budget_limit(snapshot: ApplicationSnapshot) -> float | None:
        limits: list[float] = []
        for node in snapshot.workflow.nodes:
            if node.type != "budget_gate":
                continue
            settings = node.config.get("settings", node.config)
            if not isinstance(settings, dict):
                continue
            raw = settings.get("max_cost_usd", settings.get("max_budget_usd"))
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
                limits.append(float(raw))
        return min(limits) if limits else None

    async def _execute_tool(
        self,
        config: ToolConfig,
        snapshot: ApplicationSnapshot,
        context: dict[str, Any],
        workspace_path: str,
        run_id: str,
        node_id: str,
        owner_id: str,
        state: WorkflowRunState | None,
    ) -> dict[str, Any]:
        if config.tool_name.startswith("workflow:"):
            application_id = config.tool_name.split(":", 1)[1]
            self._validate_nested_workflow_target(
                config.tool_name,
                self._nested_application_allowlists.get(run_id),
            )
            await self.harness.record_usage(
                run_id,
                "nested_workflow_call",
                metadata={"node_id": node_id, "application_id": application_id},
            )
            nested = await self.create_run(
                application_id,
                WorkflowRunRequest(inputs=self._resolve(config.input, context), workspace_path=workspace_path),
                parent_task_id=run_id,
                origin="nested_workflow_tool",
                workspace_boundary=(
                    str(self._workspace_boundaries[run_id])
                    if run_id in self._workspace_boundaries
                    else None
                ),
                allowed_nested_application_ids=self._nested_application_allowlists.get(run_id),
                allowed_runtime_tools=self._runtime_tool_allowlists.get(run_id),
                allowed_network_hosts=self._network_host_allowlists.get(run_id),
                model_access=self._model_access_policies.get(run_id),
                allowed_connector_operations=(
                    self._connector_operation_allowlists.get(run_id)
                ),
                writable_connector_operations=(
                    self._writable_connector_operations.get(run_id)
                ),
                permission_required_connector_operations=(
                    self._permission_connector_operations.get(run_id)
                ),
                compensation_connector_operations=(
                    self._compensation_connector_operations.get(run_id)
                ),
                max_connector_write_count=(
                    state.max_connector_write_count
                    if state is not None
                    and state.max_connector_write_count is not None
                    else None
                ),
                max_connector_payload_bytes=(
                    state.max_connector_payload_bytes
                    if state is not None
                    else None
                ),
                governed_host_actions=(
                    state.governed_host_actions if state is not None else False
                ),
                assignment_id=state.assignment_id if state is not None else None,
                session_id=state.session_id if state is not None else None,
                connector_descriptor_digests=(
                    state.connector_descriptor_digests
                    if state is not None
                    else None
                ),
                task_credential_ref_digest=(
                    state.task_credential_ref_digest
                    if state is not None
                    else None
                ),
                task_policy_digest=(
                    state.task_policy_digest if state is not None else None
                ),
                allowed_actions_digest=(
                    state.allowed_actions_digest if state is not None else None
                ),
                budget_digest=state.budget_digest if state is not None else None,
                task_deadline_at=(
                    state.task_deadline_at if state is not None else None
                ),
                application_call_chain=(
                    state.application_call_chain if state is not None else None
                ),
            )
            await self.active_tasks[nested["run_id"]]
            record = await self.workflow_store.get_run(nested["run_id"])
            if state is not None and state.max_connector_write_count is not None:
                nested_state = WorkflowRunState.model_validate(record["state"])
                state.connector_write_count += nested_state.connector_write_count
                if state.connector_write_count > state.max_connector_write_count:
                    raise WorkflowRuntimeWriteLimitExceeded(
                        "nested workflow exceeded the connector write limit"
                    )
            if record["status"] != "succeeded":
                raise RuntimeError(
                    f"nested workflow {application_id} ended with {record['status']}: {record.get('error') or ''}"
                )
            return {"output": record["outputs"], "run_id": nested["run_id"]}
        self._validate_runtime_tool_target(
            config.tool_name,
            self._runtime_tool_allowlists.get(run_id),
        )
        tool = self.tools.get(config.tool_name)
        allowed_network_hosts = self._network_host_allowlists.get(run_id)
        agent = AgentSpec(
            name=f"Workflow tool {config.tool_name}",
            description="Executes one tool from a validated workflow.",
            system_prompt="Execute the configured workflow tool exactly and return its result.",
            tools=[config.tool_name],
            permission_mode=PermissionMode.bypass,
            network_policy=(
                "allowlist"
                if allowed_network_hosts
                else "none"
                if allowed_network_hosts is not None
                else "full"
            ),
            network_allowlist=sorted(allowed_network_hosts or ()),
        )
        session_id = f"workflow-{run_id}-{node_id}"
        sandbox = None
        if config.tool_name != "WebSearch":
            sandbox = await self.sandboxes.get_or_create(session_id, workspace_path, agent.network_policy, [])

        async def no_subagent(_: str, __: str | None) -> str:
            raise RuntimeError("subagents are not available inside a single Tool block")

        try:
            resolved_input = self._resolve(config.input, context)
            self._enforce_tool_network_policy(
                config.tool_name,
                resolved_input,
                agent,
                sandboxed_stdio=sandbox is not None,
            )
            self.harness.enforce_secret_policy(
                surface=f"workflow_tool:{config.tool_name}",
                payload=resolved_input,
            )
            injected_input = await self.harness.inject_secret_references(
                owner_id=owner_id,
                payload=resolved_input,
                allow_secret_references=run_id not in self._runtime_tool_allowlists,
            )
            await self.harness.record_usage(
                run_id,
                "tool_call",
                metadata={"node_id": node_id, "tool": config.tool_name},
            )
            await self._emit(run_id, f"node.{node_id}.tool.started", {
                "tool": config.tool_name, "input": self._redact(injected_input)
            })
            result = await tool.execute(
                injected_input,
                ToolContext(
                    session_id=session_id,
                    agent=agent,
                    sandbox=sandbox,  # type: ignore[arg-type]
                    emit=lambda kind, data: self._emit(run_id, f"node.{node_id}.{kind}", data),
                    spawn_subagent=no_subagent,
                ),
            )
            if result.is_error:
                await self._emit(run_id, f"node.{node_id}.tool.failed", {
                    "tool": config.tool_name, "content": result.content
                })
                raise RuntimeError(result.content)
            await self._emit(run_id, f"node.{node_id}.tool.completed", {
                "tool": config.tool_name, "content": result.content
            })
            try:
                parsed = json.loads(result.content)
            except json.JSONDecodeError:
                parsed = result.content
            return {"output": parsed}
        except Exception as error:
            await self._emit(run_id, f"node.{node_id}.tool.failed", {
                "tool": config.tool_name, "error": str(error)
            })
            raise
        finally:
            if sandbox is not None:
                await self.sandboxes.remove(session_id)

    def _enforce_tool_network_policy(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        agent: AgentSpec,
        *,
        sandboxed_stdio: bool = False,
    ) -> None:
        if tool_name == "WebSearch":
            self.harness.enforce_network_egress_policy(
                surface="workflow_tool:WebSearch",
                hostname="news.google.com",
            )
            return
        if tool_name == "Program":
            tool = self.tools.get(tool_name)
            network_hosts_for = getattr(tool, "network_hosts_for", None)
            if callable(network_hosts_for):
                for hostname in network_hosts_for(str(tool_input.get("profile_id", ""))):
                    self.harness.enforce_network_egress_policy(
                        surface="workflow_tool:Program",
                        hostname=hostname,
                    )
            return
        if tool_name != "MCP":
            return
        server_name = str(tool_input.get("server", ""))
        server = next((item for item in agent.mcp_servers if item.name == server_name), None)
        if not server:
            return
        if server.transport == "stdio":
            self.harness.enforce_stdio_mcp_policy(
                surface="workflow_tool:MCP",
                server_name=server.name,
                agent_network_policy=agent.network_policy,
                sandbox_network_policy=agent.network_policy if sandboxed_stdio else None,
                declared_egress_hosts=server.egress_hosts,
                agent_network_allowlist=agent.network_allowlist,
            )
            return
        if not server.url:
            return
        parsed = urlparse(server.url)
        if parsed.hostname:
            self.harness.enforce_network_egress_policy(
                surface=f"workflow_tool:MCP:{server.name}",
                hostname=parsed.hostname,
            )

    async def _http(
        self,
        config: HTTPConfig,
        context: dict[str, Any],
        *,
        owner_id: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        url = str(self._resolve(config.url, context))
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("HTTP block requires an http or https URL")
        allowed_network_hosts = (
            self._network_host_allowlists.get(run_id) if run_id is not None else None
        )
        if (
            allowed_network_hosts is not None
            and parsed.hostname.casefold() not in allowed_network_hosts
        ):
            raise WorkflowRuntimeNetworkScopeDenied(
                "network destination is outside the assigned run policy"
            )
        try:
            address = ipaddress.ip_address(parsed.hostname)
            if address.is_link_local or address.is_multicast or address.is_unspecified:
                raise ValueError("HTTP block rejects link-local, multicast, and unspecified addresses")
        except ValueError as error:
            if "rejects" in str(error):
                raise
        self.harness.enforce_network_egress_policy(
            surface="http_request",
            hostname=parsed.hostname,
        )
        header_values = {key: self._resolve(value, context) for key, value in config.headers.items()}
        query = {key: self._resolve(value, context) for key, value in config.query.items()}
        body = self._resolve(config.body, context)
        self.harness.enforce_secret_policy(
            surface=f"http:{parsed.hostname}",
            payload={"headers": header_values, "query": query, "body": body},
        )
        injected_headers = await self.harness.inject_secret_references(
            owner_id=owner_id,
            payload=header_values,
            allow_secret_references=(
                run_id is None or run_id not in self._runtime_tool_allowlists
            ),
        )
        headers = {key: str(value) for key, value in injected_headers.items()}
        query = await self.harness.inject_secret_references(
            owner_id=owner_id,
            payload=query,
            allow_secret_references=(
                run_id is None or run_id not in self._runtime_tool_allowlists
            ),
        )
        body = await self.harness.inject_secret_references(
            owner_id=owner_id,
            payload=body,
            allow_secret_references=(
                run_id is None or run_id not in self._runtime_tool_allowlists
            ),
        )
        async with httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=True) as client:
            response = await client.request(
                config.method, url, headers=headers, params=query, json=body if body is not None else None
            )
        content_type = response.headers.get("content-type", "")
        value: Any = response.json() if "json" in content_type else response.text
        if response.is_error:
            raise WorkflowHTTPError(response.status_code, str(value)[:1000])
        return {"output": value, "status": response.status_code, "headers": dict(response.headers)}

    async def run_test_suite(
        self,
        application_id: str,
        *,
        harness_task_id: str | None = None,
        manage_harness_task: bool = True,
        origin: str = "test_suite",
        workspace_path: str = ".",
        workspace_boundary: str | None = None,
        allowed_nested_application_ids: Collection[str] | None = None,
        allowed_runtime_tools: Collection[str] | None = None,
        allowed_network_hosts: Collection[str] | None = None,
        model_access: bool | None = None,
        allowed_connector_operations: Collection[str] | None = None,
        writable_connector_operations: Collection[str] | None = None,
        permission_required_connector_operations: Collection[str] | None = None,
        compensation_connector_operations: Collection[str] | None = None,
        max_connector_write_count: int | None = None,
        max_connector_payload_bytes: int | None = None,
        governed_host_actions: bool = False,
        assignment_id: str | None = None,
        session_id: str | None = None,
        connector_descriptor_digests: dict[str, str] | None = None,
        task_credential_ref_digest: str | None = None,
        task_policy_digest: str | None = None,
        allowed_actions_digest: str | None = None,
        budget_digest: str | None = None,
        task_deadline_at: str | None = None,
    ) -> dict[str, Any]:
        draft = await self.workflow_store.get_draft(application_id)
        snapshot: ApplicationSnapshot = draft["snapshot"]
        nested_allowlist = (
            frozenset(str(value) for value in allowed_nested_application_ids)
            if allowed_nested_application_ids is not None
            else None
        )
        runtime_tool_allowlist = (
            frozenset(str(value) for value in allowed_runtime_tools)
            if allowed_runtime_tools is not None
            else None
        )
        network_host_allowlist = (
            frozenset(str(value).casefold() for value in allowed_network_hosts)
            if allowed_network_hosts is not None
            else None
        )
        connector_allowlist = (
            frozenset(str(value) for value in allowed_connector_operations)
            if allowed_connector_operations is not None
            else None
        )
        writable_connector_allowlist = (
            frozenset(str(value) for value in writable_connector_operations)
            if writable_connector_operations is not None
            else None
        )
        permission_connector_allowlist = (
            frozenset(
                str(value)
                for value in permission_required_connector_operations
            )
            if permission_required_connector_operations is not None
            else None
        )
        compensation_connector_allowlist = (
            frozenset(str(value) for value in compensation_connector_operations)
            if compensation_connector_operations is not None
            else None
        )
        if any(
            value is not None
            for value in (
                workspace_boundary,
                nested_allowlist,
                runtime_tool_allowlist,
                network_host_allowlist,
                model_access,
                connector_allowlist,
            )
        ):
            for test in snapshot.tests:
                self._validate_restricted_inputs(test.inputs)
        case_workspaces: list[Path | None] = [None for _ in snapshot.tests]
        suite_instance = f"test-suite-{uuid4().hex}"
        if workspace_boundary is not None:
            suite_boundary = self.sandboxes.resolve_workspace(workspace_boundary).resolve()
            suite_base = self._resolve_scoped_workspace(
                workspace_path,
                suite_boundary,
            )
        else:
            suite_base = self.sandboxes.resolve_workspace(
                workspace_path,
                create=True,
            ).resolve()
        suite_workspace = suite_base / suite_instance
        suite_workspace.mkdir(parents=False, exist_ok=False)
        if workspace_boundary is not None:
            # Validate once before draft validation and before any report, run,
            # version, or active-version side effect. This deliberately also
            # covers an empty test suite.
            self._validate_execution_policy(
                snapshot.workflow,
                workspace_boundary=suite_workspace,
                allowed_nested_application_ids=nested_allowlist,
                allowed_runtime_tools=runtime_tool_allowlist,
                allowed_network_hosts=network_host_allowlist,
                model_access=model_access,
                allowed_connector_operations=connector_allowlist,
                governed_host_actions=governed_host_actions,
                agents=snapshot.agents,
            )
        validation = await self.applications.validate_draft(application_id)
        if validation["valid"]:
            for index, test in enumerate(snapshot.tests):
                safe_test_id = re.sub(r"[^A-Za-z0-9_.-]", "-", str(test.id))[:48]
                case_workspace = suite_workspace / f"case-{index:03d}-{safe_test_id or 'test'}"
                case_workspace.mkdir(parents=False, exist_ok=False)
                self._stage_test_declared_workspaces(
                    snapshot.workflow,
                    suite_base,
                    case_workspace,
                )
                self._stage_test_workspace_tools(suite_base, case_workspace)
                self._validate_execution_policy(
                    snapshot.workflow,
                    workspace_boundary=case_workspace,
                    allowed_nested_application_ids=nested_allowlist,
                    allowed_runtime_tools=runtime_tool_allowlist,
                    allowed_network_hosts=network_host_allowlist,
                    model_access=model_access,
                    allowed_connector_operations=connector_allowlist,
                    agents=snapshot.agents,
                )
                case_workspaces[index] = case_workspace
        test_task_id = harness_task_id or f"test-suite:{uuid4()}"
        if manage_harness_task:
            await self.harness.start_task(
                test_task_id,
                kind="test_suite",
                owner_id=application_id,
                resource_id=application_id,
                metadata={
                    "draft_revision": draft["revision"],
                    "content_hash": draft["content_hash"],
                    "origin": origin,
                },
            )
        if not validation["valid"]:
            node_types = [node.type for node in snapshot.workflow.nodes]
            tool_node_names = [
                str(node.config.get("tool_name"))
                for node in snapshot.workflow.nodes
                if node.type == "tool" and node.config.get("tool_name")
            ]
            tool_node_names.extend(
                str(node.config.get("settings", {}).get("tool_name"))
                for node in snapshot.workflow.nodes
                if node.type == "tool_executor"
                and node.config.get("settings", {}).get("tool_name")
            )
            validation_errors = [str(item) for item in validation.get("errors", [])]
            global_errors = [item for item in validation_errors if not item.startswith("test ")]
            results: list[dict[str, Any]] = []
            for test in snapshot.tests:
                test_errors = [
                    item for item in validation_errors if item.startswith(f"test {test.id} ")
                ]
                failed_checks = [
                    "test was not run because draft validation failed",
                    *global_errors,
                    *test_errors,
                ]
                assertions = [
                    {
                        **assertion.model_dump(mode="json"),
                        "passed": False,
                        "error": "not run because draft validation failed",
                    }
                    for assertion in test.assertions
                ]
                missing_node_types = sorted(set(test.required_node_types) - set(node_types))
                missing_tool_nodes = sorted(
                    set(test.required_tool_nodes) - set(tool_node_names)
                )
                frame = (
                    test.frame.model_dump(mode="json")
                    if test.frame
                    else {
                        "id": test.id,
                        "title": test.name,
                        "category": "custom",
                        "purpose": test.requirement,
                        "reviewer_guidance": "",
                        "reference": "",
                        "failure_target": "",
                    }
                )
                readable_report = {
                    "title": frame.get("title") or test.name,
                    "category": frame.get("category", "custom"),
                    "purpose": frame.get("purpose") or test.requirement,
                    "status": "failed",
                    "mandatory": test.mandatory,
                    "reviewer_guidance": frame.get("reviewer_guidance", ""),
                    "reference": frame.get("reference", ""),
                    "failure_target": frame.get("failure_target", ""),
                    "failed_checks": failed_checks,
                    "failed_assertions": assertions,
                    "feedback_hints": test.feedback_hints,
                }
                results.append({
                    "test_id": test.id,
                    "name": test.name,
                    "mandatory": test.mandatory,
                    "passed": False,
                    "run_id": "",
                    "run_status": "not_run",
                    "run_error": "draft validation failed before execution",
                    "failure_code": "draft_validation_failed",
                    "failed_node": None,
                    "outputs": {},
                    "frame": frame,
                    "readable_report": readable_report,
                    "assertions": assertions,
                    "tool_evidence": {
                        "used_tools": [],
                        "required_tools": test.required_tools,
                        "required_tools_passed": not test.required_tools,
                        "required_node_types": test.required_node_types,
                        "node_types": node_types,
                        "required_node_types_passed": not missing_node_types,
                        "required_tool_nodes": test.required_tool_nodes,
                        "tool_node_names": tool_node_names,
                        "required_tool_nodes_passed": not missing_tool_nodes,
                        "minimum_tool_calls": test.minimum_tool_calls,
                        "minimum_calls_passed": test.minimum_tool_calls == 0,
                        "output_urls": [],
                        "cited_tool_urls": [],
                        "unverified_output_urls": [],
                        "citation_passed": not test.require_cited_tool_urls,
                    },
                })
            summary = {
                "total": len(results),
                "passed": 0,
                "failed": len(results),
                "mandatory_failed": sum(1 for item in results if item["mandatory"]),
                "frames": [
                    {
                        "test_id": item["test_id"],
                        "title": item["readable_report"]["title"],
                        "category": item["readable_report"]["category"],
                        "status": "failed",
                    }
                    for item in results
                ],
            }
            report = {
                "passed": False,
                "validation": validation,
                "summary": summary,
                "tests": results,
            }
            await self.workflow_store.record_test_report(
                application_id,
                draft["revision"],
                draft["content_hash"],
                report,
                passed=False,
            )
            if manage_harness_task:
                await self.harness.finish_task(test_task_id, status="failed")
            await self._emit(application_id, "tests.completed", report)
            return report
        semaphore = asyncio.Semaphore(TEST_SUITE_MAX_CONCURRENCY)

        async def execute_test(
            index: int,
            test: WorkflowTestCase,
        ) -> tuple[WorkflowTestCase, str]:
            async with semaphore:
                case_workspace = case_workspaces[index]
                created = await self.create_run(
                    application_id,
                    WorkflowRunRequest(
                        inputs=test.inputs,
                        use_draft=True,
                        workspace_path=(
                            str(case_workspace) if case_workspace is not None else workspace_path
                        ),
                    ),
                    parent_task_id=test_task_id,
                    origin=origin,
                    workspace_boundary=(
                        str(case_workspace) if case_workspace is not None else None
                    ),
                    allowed_nested_application_ids=nested_allowlist,
                    allowed_runtime_tools=runtime_tool_allowlist,
                    allowed_network_hosts=network_host_allowlist,
                    model_access=model_access,
                    allowed_connector_operations=connector_allowlist,
                    writable_connector_operations=writable_connector_allowlist,
                    permission_required_connector_operations=(
                        permission_connector_allowlist
                    ),
                    compensation_connector_operations=(
                        compensation_connector_allowlist
                    ),
                    max_connector_write_count=max_connector_write_count,
                    max_connector_payload_bytes=max_connector_payload_bytes,
                    governed_host_actions=governed_host_actions,
                    assignment_id=assignment_id,
                    session_id=session_id,
                    connector_descriptor_digests=connector_descriptor_digests,
                    task_credential_ref_digest=task_credential_ref_digest,
                    task_policy_digest=task_policy_digest,
                    allowed_actions_digest=allowed_actions_digest,
                    budget_digest=budget_digest,
                    task_deadline_at=task_deadline_at,
                    simulated_human_inputs=test.simulated_human_inputs,
                )
                run_id = created["run_id"]
                task = self.active_tasks[run_id]
                await task
                return test, run_id

        test_runs = await asyncio.gather(
            *(execute_test(index, test) for index, test in enumerate(snapshot.tests))
        )
        results: list[dict[str, Any]] = []
        for test, run_id in test_runs:
            record = await self.workflow_store.get_run(run_id)
            workflow_events = await self.storage.list_events(run_id)
            failed_event = next(
                (event for event in reversed(workflow_events) if event.type == "node.failed"),
                None,
            )
            failed_node_id = str(failed_event.data.get("node_id", "")) if failed_event else ""
            failed_node_spec = next(
                (node for node in snapshot.workflow.nodes if node.id == failed_node_id),
                None,
            )
            failed_node_output = "".join(
                str(event.data.get("text", ""))
                for event in workflow_events
                if failed_node_id
                and event.type == f"node.{failed_node_id}.model.text.delta"
            )
            failed_node = (
                {
                    "id": failed_node_id,
                    "title": failed_node_spec.title if failed_node_spec else failed_node_id,
                    "type": failed_node_spec.type if failed_node_spec else "",
                    "error": str(failed_event.data.get("error", "")),
                    "output_preview": failed_node_output[:4_000],
                }
                if failed_event
                else None
            )
            run_error = str(record.get("error") or "")
            failure_code = self._acceptance_failure_code(run_error)
            session_ids = [
                str(event.data["session_id"])
                for event in workflow_events
                if event.type == "node.agent.session" and event.data.get("session_id")
            ]
            tool_events = []
            for session_id in session_ids:
                tool_events.extend(
                    event
                    for event in await self.storage.list_events(session_id)
                    if event.type == "tool.completed"
                )
            workflow_tool_events = [
                event
                for event in workflow_events
                if event.type.endswith(".tool.completed") or event.type.endswith(".tool.failed")
            ]
            tool_events.extend(workflow_tool_events)
            used_tools = [str(event.data.get("tool", "")) for event in tool_events]
            node_types = [node.type for node in snapshot.workflow.nodes]
            tool_node_names = [
                str(node.config.get("tool_name"))
                for node in snapshot.workflow.nodes
                if node.type == "tool" and node.config.get("tool_name")
            ]
            tool_node_names.extend(
                str(node.config.get("settings", {}).get("tool_name"))
                for node in snapshot.workflow.nodes
                if node.type == "tool_executor" and node.config.get("settings", {}).get("tool_name")
            )
            required_node_types_passed = all(
                required in node_types for required in test.required_node_types
            )
            required_tool_nodes_passed = all(
                required in tool_node_names for required in test.required_tool_nodes
            )
            required_tools_passed = all(tool in used_tools for tool in test.required_tools)
            minimum_calls_passed = len(tool_events) >= test.minimum_tool_calls
            evidence_urls = set().union(*(
                self._extract_urls(str(event.data.get("content", ""))) for event in tool_events
            )) if tool_events else set()
            output_urls = self._extract_urls(record["outputs"])
            cited_urls = sorted(output_urls & evidence_urls)
            unverified_output_urls = sorted(output_urls - evidence_urls)
            citation_passed = (
                not test.require_cited_tool_urls
                or (bool(output_urls) and not unverified_output_urls)
            )
            assertions = []
            for assertion in test.assertions:
                try:
                    semantic_unwrap = False
                    try:
                        actual = self._resolve_assertion_path(
                            record["outputs"],
                            assertion.path,
                        )
                    except (KeyError, TypeError, IndexError):
                        actual = self._resolve_assertion_path(
                            self._semantic_acceptance_output(record["outputs"]),
                            assertion.path,
                        )
                        semantic_unwrap = True
                    assertion_result = {
                        "passed": self._assert(
                            actual,
                            assertion.operator,
                            assertion.expected,
                        ),
                        "actual": actual,
                        **assertion.model_dump(mode="json"),
                    }
                    if semantic_unwrap:
                        assertion_result["semantic_unwrap"] = True
                    assertions.append(assertion_result)
                except Exception as error:
                    path = ".".join(str(item) for item in assertion.path) or "<root>"
                    message = (
                        f"output path not found: {path}"
                        if isinstance(error, (KeyError, TypeError, IndexError))
                        else str(error)
                    )
                    assertions.append({
                        "passed": False,
                        "actual": None,
                        "error": message,
                        **assertion.model_dump(mode="json"),
                    })
            failed_checks: list[str] = []
            if record["status"] != "succeeded":
                failed_checks.append(f"run status is {record['status']}")
                if run_error:
                    failed_checks.append(f"workflow error: {run_error}")
            if not required_node_types_passed:
                missing = sorted(set(test.required_node_types) - set(node_types))
                failed_checks.append(f"missing required node types: {missing}")
            if not required_tool_nodes_passed:
                missing = sorted(set(test.required_tool_nodes) - set(tool_node_names))
                failed_checks.append(f"missing required tool nodes: {missing}")
            if not required_tools_passed:
                missing = sorted(set(test.required_tools) - set(used_tools))
                failed_checks.append(f"missing required tool evidence: {missing}")
            if not minimum_calls_passed:
                failed_checks.append(
                    f"tool calls below minimum: {len(tool_events)} < {test.minimum_tool_calls}"
                )
            if not citation_passed:
                failed_checks.append("output URLs are not fully backed by tool evidence")
            failed_assertions = [
                assertion for assertion in assertions if not assertion.get("passed")
            ]
            if failed_assertions:
                failed_checks.append(f"failed assertions: {len(failed_assertions)}")
            passed = (
                record["status"] == "succeeded"
                and all(item["passed"] for item in assertions)
                and required_node_types_passed
                and required_tool_nodes_passed
                and required_tools_passed
                and minimum_calls_passed
                and citation_passed
            )
            frame = (
                test.frame.model_dump(mode="json")
                if test.frame
                else {
                    "id": test.id,
                    "title": test.name,
                    "category": "custom",
                    "purpose": test.requirement,
                    "reviewer_guidance": "",
                    "reference": "",
                    "failure_target": "",
                }
            )
            readable_report = {
                "title": frame.get("title") or test.name,
                "category": frame.get("category", "custom"),
                "purpose": frame.get("purpose") or test.requirement,
                "status": "passed" if passed else "failed",
                "mandatory": test.mandatory,
                "reviewer_guidance": frame.get("reviewer_guidance", ""),
                "reference": frame.get("reference", ""),
                "failure_target": frame.get("failure_target", ""),
                "failed_checks": failed_checks,
                "failed_assertions": failed_assertions,
                "failure_code": failure_code,
                "feedback_hints": test.feedback_hints,
            }
            results.append({
                "test_id": test.id,
                "name": test.name,
                "mandatory": test.mandatory,
                "passed": passed,
                "run_id": run_id,
                "run_status": record["status"],
                "run_error": run_error,
                "failure_code": failure_code,
                "failed_node": failed_node,
                "outputs": record["outputs"],
                "frame": frame,
                "readable_report": readable_report,
                "assertions": assertions,
                "tool_evidence": {
                    "used_tools": used_tools,
                    "required_tools": test.required_tools,
                    "required_tools_passed": required_tools_passed,
                    "required_node_types": test.required_node_types,
                    "node_types": node_types,
                    "required_node_types_passed": required_node_types_passed,
                    "required_tool_nodes": test.required_tool_nodes,
                    "tool_node_names": tool_node_names,
                    "required_tool_nodes_passed": required_tool_nodes_passed,
                    "minimum_tool_calls": test.minimum_tool_calls,
                    "minimum_calls_passed": minimum_calls_passed,
                    "output_urls": sorted(output_urls),
                    "cited_tool_urls": cited_urls,
                    "unverified_output_urls": unverified_output_urls,
                    "citation_passed": citation_passed,
                },
            })
        passed = all(item["passed"] for item in results if item["mandatory"])
        summary = {
            "total": len(results),
            "passed": sum(1 for item in results if item["passed"]),
            "failed": sum(1 for item in results if not item["passed"]),
            "mandatory_failed": sum(
                1 for item in results if item["mandatory"] and not item["passed"]
            ),
            "frames": [
                {
                    "test_id": item["test_id"],
                    "title": item["readable_report"]["title"],
                    "category": item["readable_report"]["category"],
                    "status": item["readable_report"]["status"],
                }
                for item in results
            ],
        }
        report = {"passed": passed, "validation": validation, "summary": summary, "tests": results}
        await self.workflow_store.record_test_report(
            application_id,
            draft["revision"],
            draft["content_hash"],
            report,
            passed=passed,
        )
        if manage_harness_task:
            await self.harness.finish_task(test_task_id, status="succeeded" if passed else "failed")
        await self._emit(application_id, "tests.completed", report)
        return report

    @classmethod
    def _stage_test_declared_workspaces(
        cls,
        workflow: WorkflowSpec,
        suite_base: Path,
        case_workspace: Path,
    ) -> None:
        """Copy declared writable project roots into one isolated test case."""

        suite_base = suite_base.resolve(strict=True)
        case_workspace = case_workspace.resolve(strict=True)
        if case_workspace == suite_base or suite_base not in case_workspace.parents:
            raise WorkflowWorkspaceBoundaryViolation(
                "test case workspace must stay inside its suite workspace"
            )

        declared = cls._declared_test_workspace_paths(workflow)
        roots: list[Path] = []
        for value in declared:
            relative = cls._safe_test_workspace_relative(value)
            if any(root == relative or root in relative.parents for root in roots):
                continue
            roots = [root for root in roots if relative not in root.parents]
            roots.append(relative)
            roots.sort(key=lambda item: (len(item.parts), item.as_posix()))

        for relative in roots:
            source = suite_base.joinpath(*relative.parts)
            cursor = suite_base
            for part in relative.parts:
                cursor /= part
                if cursor.is_symlink():
                    raise WorkflowWorkspaceBoundaryViolation(
                        "declared test workspace path contains a symbolic link"
                    )
            try:
                resolved_source = source.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise WorkflowWorkspaceBoundaryViolation(
                    "declared test workspace is unavailable inside the suite workspace"
                ) from error
            if (
                resolved_source != suite_base
                and suite_base not in resolved_source.parents
            ):
                raise WorkflowWorkspaceBoundaryViolation(
                    "declared test workspace escapes the suite workspace"
                )
            if not resolved_source.is_dir():
                raise WorkflowWorkspaceBoundaryViolation(
                    "declared test workspace must be a directory"
                )
            cls._validate_test_workspace_snapshot_source(
                resolved_source,
                suite_base=suite_base,
                suite_workspace=case_workspace.parent,
            )
            destination = case_workspace.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                resolved_source,
                destination,
                copy_function=shutil.copy2,
                dirs_exist_ok=relative == Path("."),
                ignore=cls._test_workspace_copy_ignore(
                    suite_base=suite_base,
                    suite_workspace=case_workspace.parent,
                ),
            )

    @staticmethod
    def _safe_test_workspace_relative(value: str) -> Path:
        if not value or "\x00" in value or "\\" in value:
            raise WorkflowWorkspaceBoundaryViolation(
                "declared test workspace must be a relative POSIX path"
            )
        candidate = Path(value)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise WorkflowWorkspaceBoundaryViolation(
                "declared test workspace must stay relative to the suite workspace"
            )
        normalized = Path(*[part for part in candidate.parts if part != "."])
        if not normalized.parts:
            return Path(".")
        first = normalized.parts[0]
        if first == ".workflow-run-artifacts" or first.startswith("test-suite-"):
            raise WorkflowWorkspaceBoundaryViolation(
                "declared test workspace uses a reserved runtime path"
            )
        return normalized

    @classmethod
    def _declared_test_workspace_paths(cls, workflow: WorkflowSpec) -> list[str]:
        declared: list[str] = []
        pending = [workflow]
        while pending:
            current = pending.pop()
            for node in current.nodes:
                workspace_key = {
                    "tool_executor": "workspace_path",
                    "sandbox_boundary": "workspace",
                    "subagent_spawn": "workspace_path",
                }.get(node.type)
                settings = node.config.get("settings", {})
                if workspace_key is not None and isinstance(settings, dict):
                    value = settings.get(workspace_key)
                    if isinstance(value, str):
                        declared.append(value)
                nested = node.config.get("workflow")
                if isinstance(nested, dict):
                    pending.append(WorkflowSpec.model_validate(nested))
        return declared

    @staticmethod
    def _validate_test_workspace_snapshot_source(
        source: Path,
        *,
        suite_base: Path,
        suite_workspace: Path,
    ) -> None:
        for current, directory_names, file_names in os.walk(
            source,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            if current_path == suite_base:
                directory_names[:] = [
                    name
                    for name in directory_names
                    if name != suite_workspace.name
                    and not name.startswith("test-suite-")
                    and name != ".workflow-run-artifacts"
                ]
            for name in directory_names:
                candidate = current_path / name
                metadata = candidate.lstat()
                if candidate.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                    raise WorkflowWorkspaceBoundaryViolation(
                        "declared test workspace contains a symlink or special directory"
                    )
            for name in file_names:
                candidate = current_path / name
                metadata = candidate.lstat()
                if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                    raise WorkflowWorkspaceBoundaryViolation(
                        "declared test workspace contains a symlink or special file"
                    )

    @staticmethod
    def _test_workspace_copy_ignore(
        *,
        suite_base: Path,
        suite_workspace: Path,
    ) -> Callable[[str, list[str]], set[str]]:
        def ignore(current: str, names: list[str]) -> set[str]:
            if Path(current).resolve() != suite_base:
                return set()
            return {
                name
                for name in names
                if name == suite_workspace.name
                or name.startswith("test-suite-")
                or name == ".workflow-run-artifacts"
            }

        return ignore

    @staticmethod
    def _stage_test_workspace_tools(
        suite_base: Path,
        case_workspace: Path,
    ) -> None:
        """Snapshot program tools and writable local cache into an isolated test."""

        source = (suite_base / "tools").resolve()
        if not source.is_dir():
            return
        for candidate in source.rglob("*"):
            if not candidate.is_symlink():
                continue
            target = candidate.resolve()
            if target != source and source not in target.parents:
                raise WorkflowWorkspaceBoundaryViolation(
                    f"test tool symlink escapes workspace tools: {candidate}"
                )
        destination = case_workspace / "tools"
        if not destination.exists():
            try:
                shutil.copytree(
                    source,
                    destination,
                    symlinks=True,
                    copy_function=os.link,
                )
            except OSError:
                shutil.rmtree(destination, ignore_errors=True)
                shutil.copytree(source, destination, symlinks=True)
        cache_source = (suite_base / ".program-cache").resolve()
        if not cache_source.is_dir():
            return
        for candidate in cache_source.rglob("*"):
            if not candidate.is_symlink():
                continue
            target = candidate.resolve()
            if target != cache_source and cache_source not in target.parents:
                raise WorkflowWorkspaceBoundaryViolation(
                    f"test program cache symlink escapes workspace: {candidate}"
                )
        cache_destination = case_workspace / ".program-cache"
        if not cache_destination.exists():
            shutil.copytree(
                cache_source,
                cache_destination,
                symlinks=True,
            )

    async def _validate_contract(
        self, node: NodeSpec, output: dict[str, Any], scoped_id: str, run_id: str
    ) -> None:
        """Validate node output against its declared contract (non-fatal)."""
        contract = node.contract
        if not contract or not contract.outputs:
            return
        violations: list[str] = []
        for field, type_str in contract.outputs.items():
            actual = output.get(field)
            if actual is None and not contract.lenient:
                violations.append(f"missing required output: {field}")
                continue
            if actual is not None and not self._matches_type(actual, type_str):
                violations.append(
                    f"output {field} expected {type_str}, got {type(actual).__name__}"
                )
        if violations:
            level = "error" if not contract.lenient else "warning"
            await self._emit(run_id, f"contract.{level}", {
                "node_id": scoped_id,
                "contract": contract.model_dump(mode="json"),
                "violations": violations,
            })

    @staticmethod
    def _matches_type(value: Any, type_str: str) -> bool:
        type_map = {
            "string": str, "number": (int, float), "boolean": bool,
            "object": dict, "array": list, "any": object,
        }
        expected = type_map.get(type_str, object)
        return isinstance(value, expected)

    @staticmethod
    def _edge_active(edge: Any, outputs: dict[str, dict[str, Any]], skipped: set[str]) -> bool:
        if edge.source in skipped or edge.source not in outputs:
            return False
        if edge.branch is None:
            # A branchless edge is the implicit success path for blocks that
            # support ``error_strategy=error_branch``.  Once the runtime emits
            # the reserved error branch, only an explicitly labelled error
            # edge may continue; otherwise both success and failure paths run.
            return outputs[edge.source].get("branch") != "error"
        return outputs[edge.source].get("branch") == edge.branch

    @staticmethod
    def _terminal_outputs(workflow: WorkflowSpec, outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        terminal = [node for node in workflow.nodes if node.type in {"end", "answer"} and node.id in outputs]
        if len(terminal) == 1:
            return outputs[terminal[0].id]
        return {node.id: outputs[node.id] for node in terminal}

    @classmethod
    def _resolve(cls, value: Any, context: dict[str, Any]) -> Any:
        if (
            isinstance(value, dict)
            and "$ref" in value
            and set(value).issubset({"$ref", "optional"})
        ):
            reference = dict(value["$ref"])
            if value.get("optional"):
                reference["optional"] = True
            node_id = str(reference.get("node_id") or "")
            path = list(reference.get("path", []))
            traversed: list[Any] = []
            current: Any = None
            try:
                if node_id == "$inputs":
                    current: Any = context["inputs"]
                elif node_id == "$run":
                    # Builtin run metadata, e.g. a per-run unique event id:
                    # {"$ref": {"node_id": "$run", "path": ["run_id"]}}
                    current = context.get("run", {})
                else:
                    if node_id not in context["nodes"] and reference.get("optional"):
                        return None
                    current = context["nodes"][node_id]
                for key in path:
                    current = current[int(key)] if isinstance(current, list) else current[key]
                    traversed.append(key)
                return current
            except (KeyError, IndexError, TypeError, ValueError) as error:
                if reference.get("optional"):
                    return None
                failed_segment = (
                    path[len(traversed)] if len(traversed) < len(path) else None
                )
                container = type(current).__name__
                detail = (
                    f"workflow reference could not resolve node={node_id!r} "
                    f"path={path!r}; failed_segment={failed_segment!r}; "
                    f"container_type={container}"
                )
                if isinstance(current, list):
                    detail += f"; container_length={len(current)}"
                raise WorkflowReferenceResolutionError(detail) from error
        if isinstance(value, dict):
            return {key: cls._resolve(item, context) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._resolve(item, context) for item in value]
        return value

    @classmethod
    def _resolve_assignment(cls, value: Any, context: dict[str, Any]) -> Any:
        """Resolve bounded, deterministic expressions inside Variable Assigner only."""

        if not isinstance(value, dict) or len(value) != 1:
            return cls._resolve(value, context)
        operator, operand = next(iter(value.items()))
        if operator not in {
            "$add",
            "$subtract",
            "$equals",
            "$length",
            "$sum",
            "$count",
            "$concat",
            "$coalesce",
            "$json_encode",
        }:
            return cls._resolve(value, context)
        if operator in {"$add", "$subtract", "$equals", "$concat", "$coalesce"}:
            if not isinstance(operand, list):
                raise TypeError(f"{operator} requires an array")
            resolved = [cls._resolve_assignment(item, context) for item in operand]
            if operator == "$add":
                if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in resolved):
                    raise TypeError("$add values must be numbers")
                return sum(resolved)
            if operator == "$subtract":
                if (
                    len(resolved) != 2
                    or any(
                        isinstance(item, bool) or not isinstance(item, (int, float))
                        for item in resolved
                    )
                ):
                    raise TypeError("$subtract requires exactly two numbers")
                return resolved[0] - resolved[1]
            if operator == "$equals":
                if len(resolved) != 2:
                    raise TypeError("$equals requires exactly two values")
                return resolved[0] == resolved[1]
            if operator == "$concat":
                if any(not isinstance(item, str) for item in resolved):
                    raise TypeError("$concat values must be strings")
                return "".join(resolved)
            return next((item for item in resolved if item is not None), None)
        if operator == "$json_encode":
            resolved = cls._resolve_assignment(operand, context)
            try:
                encoded = json.dumps(
                    resolved,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            except (TypeError, ValueError) as error:
                raise TypeError("$json_encode value must be JSON serializable") from error
            if len(encoded.encode("utf-8")) > 1_000_000:
                raise ValueError("$json_encode output exceeds 1000000 bytes")
            return encoded
        if operator == "$length":
            resolved = cls._resolve_assignment(operand, context)
            if not isinstance(resolved, (list, dict, str)):
                raise TypeError("$length requires an array, object, or string")
            return len(resolved)
        if operator == "$sum":
            items, path, where = cls._assignment_collection_spec(operand, context)
            values = [
                cls._assignment_path(item, path)
                for item in items
                if cls._assignment_where(item, where)
            ]
            if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in values):
                raise TypeError("$sum selected values must be numbers")
            return sum(values)
        items, _, where = cls._assignment_collection_spec(operand, context)
        return sum(1 for item in items if cls._assignment_where(item, where))

    @classmethod
    def _assignment_collection_spec(
        cls,
        operand: Any,
        context: dict[str, Any],
    ) -> tuple[list[Any], list[str | int], dict[str, Any] | None]:
        if isinstance(operand, dict) and set(operand).issubset({"items", "path", "where"}):
            items = cls._resolve_assignment(operand.get("items"), context)
            path = list(operand.get("path", []))
            where = operand.get("where")
        else:
            items = cls._resolve_assignment(operand, context)
            path = []
            where = None
        if not isinstance(items, list):
            raise TypeError("collection expression requires an array")
        if len(items) > 100_000:
            raise ValueError("collection expression exceeds 100000 items")
        if len(path) > 32 or any(not isinstance(item, (str, int)) for item in path):
            raise ValueError("collection expression path is invalid")
        if where is not None and (
            not isinstance(where, dict)
            or set(where) != {"path", "equals"}
            or not isinstance(where["path"], list)
            or len(where["path"]) > 32
        ):
            raise ValueError("collection expression where must contain path and equals")
        return items, path, where

    @staticmethod
    def _assignment_path(value: Any, path: list[str | int]) -> Any:
        current = value
        for key in path:
            current = current[int(key)] if isinstance(current, list) else current[key]
        return current

    @classmethod
    def _assignment_where(cls, value: Any, where: dict[str, Any] | None) -> bool:
        if where is None:
            return True
        try:
            return cls._assignment_path(value, list(where["path"])) == where["equals"]
        except (KeyError, IndexError, TypeError, ValueError):
            return False

    @classmethod
    def _references_skipped_node(cls, value: Any, skipped_nodes: set[str]) -> bool:
        if not skipped_nodes:
            return False
        if (
            isinstance(value, dict)
            and "$ref" in value
            and set(value).issubset({"$ref", "optional"})
        ):
            return str(value["$ref"].get("node_id")) in skipped_nodes
        if isinstance(value, dict):
            return any(cls._references_skipped_node(item, skipped_nodes) for item in value.values())
        if isinstance(value, list):
            return any(cls._references_skipped_node(item, skipped_nodes) for item in value)
        return False

    @classmethod
    def _evaluate(cls, condition: Condition, context: dict[str, Any]) -> bool:
        value = cls._resolve(condition.value, context)
        expected = cls._resolve(condition.expected, context)
        operations = {
            "equals": lambda: value == expected,
            "not_equals": lambda: value != expected,
            "contains": lambda: expected in value,
            "not_contains": lambda: expected not in value,
            "gt": lambda: value > expected,
            "gte": lambda: value >= expected,
            "lt": lambda: value < expected,
            "lte": lambda: value <= expected,
            "exists": lambda: value is not None,
            "empty": lambda: value in (None, "", [], {}),
        }
        return bool(operations[condition.operator]())

    @staticmethod
    def _render(template: str, variables: dict[str, Any]) -> str:
        def replace(match: re.Match[str]) -> str:
            value: Any = variables
            for key in match.group(1).strip().split("."):
                value = value[key]
            return str(value)

        return re.sub(r"{{\s*([\w.]+)\s*}}", replace, template)

    @staticmethod
    def _model_turn_prompt(
        configured_prompt: Any,
        value: Any,
        workflow_inputs: dict[str, Any],
    ) -> str:
        def as_text(item: Any) -> str:
            if isinstance(item, str):
                return item
            return json.dumps(item, ensure_ascii=False, default=str)

        def has_content(item: Any) -> bool:
            if item is None:
                return False
            if isinstance(item, str):
                return bool(item.strip())
            if isinstance(item, dict):
                return any(has_content(nested) for nested in item.values())
            if isinstance(item, (list, tuple, set)):
                return any(has_content(nested) for nested in item)
            return True

        input_text = as_text(value)
        if configured_prompt is None:
            return input_text

        prompt = as_text(configured_prompt)
        if prompt.strip() == input_text.strip():
            return prompt

        variables: dict[str, Any] = {
            **workflow_inputs,
            "input": value,
            "value": value,
        }
        if isinstance(value, dict):
            variables.update(value)
        injected_input = False

        def replace(match: re.Match[str]) -> str:
            nonlocal injected_input
            current: Any = variables
            try:
                for key in match.group(1).strip().split("."):
                    current = current[int(key)] if isinstance(current, list) else current[key]
            except (KeyError, IndexError, TypeError, ValueError):
                return match.group(0)
            injected_input = True
            return as_text(current)

        rendered = re.sub(r"{{\s*([\w.]+)\s*}}", replace, prompt)
        if injected_input or not has_content(value) or input_text in rendered:
            return rendered
        return f"{rendered.rstrip()}\n\n<workflow_input>\n{input_text}\n</workflow_input>"

    @staticmethod
    def _json_from_text(text: str) -> Any:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.I)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as error:
            match = re.search(r"(\{.*\}|\[.*\])", stripped, re.S)
            if not match:
                raise ValueError("model did not return valid JSON") from error
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError as nested_error:
                raise ValueError("model did not return valid JSON") from nested_error

    @staticmethod
    def _acceptance_failure_code(run_error: str) -> str:
        normalized = run_error.casefold()
        if "valid json" in normalized or "json" in normalized and "parse" in normalized:
            return "structured_output_invalid"
        if "node " in normalized and " failed:" in normalized:
            return "node_execution_failed"
        if run_error:
            return "workflow_run_failed"
        return ""

    @classmethod
    def _semantic_acceptance_output(cls, outputs: Any) -> Any:
        value = outputs
        terminal_keys = {"answer", "result", "output", "text", "content"}
        for _ in range(4):
            if isinstance(value, dict) and len(value) == 1:
                key, nested = next(iter(value.items()))
                if key in terminal_keys:
                    value = nested
                    continue
            if isinstance(value, str):
                try:
                    parsed = cls._json_from_text(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    break
                if parsed is value:
                    break
                value = parsed
                continue
            break
        return value

    @staticmethod
    def _json_type(value: str) -> str:
        return {"file": "object", "file_list": "array", "any": "string"}.get(value, value)

    @staticmethod
    def _resolve_assertion_path(value: Any, path: list[str]) -> Any:
        actual = value
        for key in path:
            if isinstance(actual, list):
                is_canonical_index = key == "0" or (
                    key.isascii()
                    and key.isdigit()
                    and key[0] in "123456789"
                )
                if not is_canonical_index:
                    raise TypeError("array assertion path segment must be a canonical index")
                actual = actual[int(key)]
                continue
            actual = actual[key]
        return actual

    @staticmethod
    def _assert(actual: Any, operator: str, expected: Any) -> bool:
        if operator == "exists":
            return actual is not None
        if operator == "equals":
            return actual == expected
        if operator == "contains":
            return WorkflowRuntime._contains_value(actual, expected)
        if operator == "not_contains":
            return not WorkflowRuntime._contains_value(actual, expected)
        if operator == "type":
            names = {"string": str, "number": (int, float), "boolean": bool, "object": dict, "array": list}
            return isinstance(actual, names[str(expected)])
        if operator == "min_length":
            try:
                return len(actual) >= int(expected)
            except (TypeError, ValueError):
                return False
        if operator == "max_length":
            try:
                return len(actual) <= int(expected)
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def _contains_value(actual: Any, expected: Any) -> bool:
        if actual is None:
            return False
        try:
            if expected in actual:
                return True
        except TypeError:
            pass
        if isinstance(actual, str):
            return str(expected) in actual
        try:
            haystack = json.dumps(actual, ensure_ascii=False, default=str)
        except TypeError:
            haystack = str(actual)
        return str(expected) in haystack

    @staticmethod
    def _is_structural_assertion(operator: str) -> bool:
        """Return True if the operator only checks structure, not content."""
        return operator in {"exists", "type", "min_length", "max_length"}

    @staticmethod
    def _extract_urls(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set().union(*(WorkflowRuntime._extract_urls(item) for item in value.values())) if value else set()
        if isinstance(value, (list, tuple, set)):
            return set().union(*(WorkflowRuntime._extract_urls(item) for item in value)) if value else set()
        if not isinstance(value, str):
            return set()
        return {
            url.rstrip(".,;）)]}")
            for url in re.findall(r"https?://[^\s\"'<>]+", value)
        }

    @staticmethod
    def _redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "***" if any(
                    token in key.casefold()
                    for token in ("key", "secret", "token", "password", "authorization", "cookie", "credential")
                ) else WorkflowRuntime._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [WorkflowRuntime._redact(item) for item in value]
        return value

    async def _emit(self, stream_id: str, kind: str, data: dict[str, Any]) -> None:
        await self.storage.append_event(stream_id, kind, data)

    def _consume(self, run_id: str, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        self.active_tasks.pop(run_id, None)
        self._workspace_boundaries.pop(run_id, None)
        self._nested_application_allowlists.pop(run_id, None)
        self._runtime_tool_allowlists.pop(run_id, None)
        self._network_host_allowlists.pop(run_id, None)
        self._model_access_policies.pop(run_id, None)
        self._connector_operation_allowlists.pop(run_id, None)
        self._writable_connector_operations.pop(run_id, None)
        self._permission_connector_operations.pop(run_id, None)
        self._compensation_connector_operations.pop(run_id, None)
