from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from .applications import ApplicationService
from .blocks import BlockRegistry
from .capability_contracts import (
    AcceptanceEvidenceTarget,
    CapabilityCarrierDecision,
    CapabilityCoverage,
    CarrierStatus,
    CarrierType,
    CoverageOwner,
    CoverageStatus,
    EnvironmentAvailability,
    ExternalContract,
    evaluate_capability_contract,
)
from .models import ChatMessage, ContentBlock, ToolDefinition
from .providers import ModelProvider, ProviderError
from .runtime import AgentRuntime, INVALID_TOOL_INPUT_JSON_KEY
from .storage import Storage
from .models import AgentSpec
from .platform_harness import PlatformHarness
from .template_strategy import (
    ALLOWED_REUSE_DEPTHS,
    build_suggestion_payload,
    policy_default_execution_contract,
    recommended_action_for_depth,
    resolve_effective_reuse_depth,
    score_template_matches,
    suggestion_default_metadata,
)
from .workflow_models import (
    BuildPlan,
    BuildTask,
    BuildTeamState,
    DraftOperation,
    EdgeSpec,
    NodeSpec,
    TestAssertion,
    TestFrameSpec,
    TeammateState,
    WorkflowTestCase,
)
from .workflow_runtime import WorkflowRuntime
from .workflow_storage import WorkflowStorage
from .tools import ToolRegistry


from .meta_cognition import DecisionTracker


BUILDER_SYSTEM_PROMPT = """You coordinate a persistent team that builds production-ready agent workflows.

You do not generate source code or a whole workflow JSON document. You and your teammates can only build by
using the supplied block-catalog and incremental draft tools. Every requirement must map to a task, one or more
nodes, and a mandatory test. Inspect manuals and schemas before configuring unfamiliar blocks.

Core rules:
- Start by inspecting the draft and catalog, then create requirement tasks.
- If a Capability Build Contract is attached, call capability_contract with action="get" before planning or mutation.
  Treat its F/G/X ids, E0-E5 envelope, external availability, evidence plan, and claim scope as authoritative.
- Map every required capability id into BuildPlan modules. Choose its carrier before adding blocks; one capability
  does not automatically mean one atomic block. Bind actual node/module/runtime/platform/external references with
  capability_contract action="bind" only after those references exist in the resource_inventory returned by
  action="get". A made-up string with a plausible prefix is not implementation evidence.
- An unavailable external contract is a scoped evidence gap, not a workflow graph defect. Preserve
  blocked_by_environment and the contract claim ceiling; never claim live or production success for it.
- A build with a Capability Build Contract cannot complete until capability_contract action="validate" with
  require_bound=true passes and the BuildPlan covers every required capability.
- For complex or multi-module requirements, call build_plan with action="set" before mutating the draft.
  The build plan should name modules, expected blocks, reuse_depth, complexity, risks, and how each module
  will be tested. Keep the plan updated as modules are built and tested.
- **Before building a workflow from scratch**, call template_suggestions with the
  requirement text and intended reuse_depth to check if a matching template already exists. If a template
  with confidence >= 0.7 matches the requirement, prefer expanding it via
  template_expand instead of building from scratch. This saves time and reuses
  proven patterns.
- Unless the requirement or an experiment explicitly asks for a fixed reuse depth, prefer
  template_suggestions with reuse_depth="adaptive" as the default suggestion mode.
  If it returns effective_reuse_depth and policy_reason, update the BuildPlan to that concrete depth
  before mutating the draft.
- If template_suggestions returns reuse_depth_source="policy_default", treat that result exactly as a
  resolved adaptive policy decision: immediately set or update BuildPlan.reuse_depth to
  effective_reuse_depth, preserve that the source was policy-defaulted in the plan strategy or evidence,
  and perform the returned recommended_action before more broad search.
- For agent architecture bricks, call manual_search or manual_get first, then add one brick at a time.
- Use architecture_blueprint when reconstructing a Claude-like agent loop from explicit bricks.
- Use template_list and template_expand when a known Codex-like workspace-agent or legacy Claude-like subgraph template fits; the expanded graph is
  editable and must still be validated, tested, and repaired incrementally.
- After template_expand, read the returned validation, node_types, and template_contract. Preserve
  template_contract.min_blocks_required unless you deliberately replace that capability with another visible
  block and then update tests to match the current draft.
- Use spawn_teammate for bounded independent design or verification work. Roles are dynamic, not predefined.
- Add and configure one node or edge per mutation tool call. Never assume an operation succeeded.
- Prefer explicit workflow bricks and agent architecture bricks over hiding behavior inside one Claude Agent. Use Tool bricks for registered
  tools such as WebSearch, HTTP Request for simple HTTP calls, Question Classifier/If/Else for routing,
  Variable Aggregator for joins, and Template/Answer/End for final formatting.
- Claude Agent bricks are legacy compatibility wrappers for old drafts. Do not use them as the default shape
  for new Claude-like agents; compose Context, Model Turn, Tool, Permission, Skill/MCP, Subagent, Mailbox,
  Budget, Checkpoint, and Event bricks instead.
- Values can reference prior output with {"$ref":{"node_id":"<id>","path":["field"]}}.
  Use node_id "$inputs" to reference raw workflow inputs.
- **Template Transform Node Syntax**: Template variables use double-brace Jinja syntax: {{ variable_name }}.
  You must declare each variable in the config.variables map as an object key mapped to a $ref that
  resolves to the actual value. Example correct config for template_transform:
    {"template": "Category: {{ category }}. Answer: {{ answer }}",
     "variables": {
       "category": {"$ref": {"node_id": "classifier", "path": ["branch"]}},
       "answer": {"$ref": {"node_id": "llm", "path": ["text"]}}
     }}
  NEVER use Python str.format() placeholders like {0} or {1} — they will render literally.
  ALWAYS use {{ name }} syntax where the name matches a key declared in variables.
- If you declare Start inputs, at least one downstream business-critical node must actually use them
  via "$inputs" or the Start node output. Search queries, prompts, HTTP params, and Agent tasks must
  incorporate user-provided inputs instead of ignoring them behind hard-coded text.
- For mutually exclusive branch outputs consumed by Variable Aggregator, set "optional": true inside the
  reference so a skipped branch resolves to null instead of failing.
- A valid graph has exactly one start, at least one end/answer, no implicit cycles, and no unreachable nodes.
- Add mandatory tests that demonstrate the user's actual acceptance criteria. Run them with test_run.
- Each test should include a readable frame with category, purpose, reviewer_guidance, reference, and failure_target.
  The frame should explain where the test sits in the acceptance framework, for example outline adherence,
  tool evidence, safety, or human review.
- Tests for generated workflows must set required_node_types for the visible architecture and required_tool_nodes
  when a concrete Tool brick is required, e.g. WebSearch. This prevents a single opaque Agent node from passing.
- When a requirement depends on external tools, tests must set required_tools, minimum_tool_calls, and
  require_cited_tool_urls so a model cannot pass by inventing plausible output without tool evidence.
- If a test fails, inspect events and modify blocks; do not weaken the assertion merely to make it green.
- Treat draft_validate warnings about disconnected inputs as issues to repair before publishing.
- Publish only after draft_validate and all mandatory tests pass for the exact current content hash.
- Do not claim completion before draft_publish returns a version (unless auto-publish is disabled).
"""


TEAMMATE_MIN_REMAINING_SECONDS = 90.0
TEAMMATE_REPAIR_BUDGET_EXHAUSTED_REASON = "repair_budget_exhausted"


class BuildDeadlineExceeded(RuntimeError):
    def __init__(self, max_elapsed_seconds: float, elapsed_seconds: float) -> None:
        super().__init__(
            f"builder build timed out after {max_elapsed_seconds:g}s "
            f"(elapsed {elapsed_seconds:.3f}s)"
        )
        self.max_elapsed_seconds = max_elapsed_seconds
        self.elapsed_seconds = elapsed_seconds


class WorkflowBuilder:
    def __init__(
        self,
        *,
        storage: Storage,
        workflow_store: WorkflowStorage,
        applications: ApplicationService,
        blocks: BlockRegistry,
        runtime: WorkflowRuntime,
        provider: ModelProvider,
        agent_runtime: AgentRuntime,
        generator_model: str,
        core_tools: ToolRegistry,
        harness: PlatformHarness,
        on_build_complete: Callable[[str], Awaitable[None]] | None = None,
        template_store: Any | None = None,
    ) -> None:
        self.storage = storage
        self.workflow_store = workflow_store
        self.applications = applications
        self.blocks = blocks
        self.runtime = runtime
        self.provider = provider
        self.agent_runtime = agent_runtime
        self.generator_model = generator_model
        self.core_tools = core_tools
        self.harness = harness
        self.on_build_complete = on_build_complete
        self.template_store = template_store
        self.active: dict[str, asyncio.Task[Any]] = {}
        self._trackers: dict[str, DecisionTracker] = {}  # build_id → tracker

    def start(self, build_id: str) -> None:
        if build_id in self.active and not self.active[build_id].done():
            raise RuntimeError("build is already running")
        task = asyncio.create_task(self._run(build_id))
        self.active[build_id] = task
        task.add_done_callback(lambda item: self._consume(build_id, item))

    def cancel(self, build_id: str) -> None:
        task = self.active.get(build_id)
        if not task or task.done():
            raise KeyError("active build not found")
        task.cancel()

    async def run_claimed_build(self, build_id: str) -> dict[str, Any]:
        if build_id in self.active and not self.active[build_id].done():
            raise RuntimeError("build is already running")
        task = asyncio.current_task()
        if task is not None:
            self.active[build_id] = task
        try:
            return await self._run(build_id, manage_harness_task=False)
        finally:
            if task is None or self.active.get(build_id) is task:
                self.active.pop(build_id, None)

    async def _run(self, build_id: str, *, manage_harness_task: bool = True) -> dict[str, Any]:
        build = await self.workflow_store.get_build(build_id)
        state: BuildTeamState = build["team_state"]
        build_started_at = time.monotonic()
        max_elapsed_seconds = self._coerce_max_elapsed_seconds(build.get("max_elapsed_seconds"))
        task_metadata: dict[str, Any] = {
            "max_turns": build["max_turns"],
            "max_repair_cycles": build["max_repair_cycles"],
            "auto_publish": build["auto_publish"],
        }
        if max_elapsed_seconds is not None:
            task_metadata["max_elapsed_seconds"] = max_elapsed_seconds
        if manage_harness_task:
            await self.harness.start_task(
                build_id,
                kind="builder_build",
                owner_id=build["application_id"],
                resource_id=build_id,
                metadata=task_metadata,
            )
        await self.workflow_store.update_build(build_id, status="building", team_state=state)
        await self._emit(build_id, "build.started", {
            "application_id": build["application_id"], "requirement": build["requirement"]
        })
        if max_elapsed_seconds is not None:
            await self._emit(build_id, "build.deadline.configured", {
                "max_elapsed_seconds": max_elapsed_seconds,
            })
        contract_context = ""
        if state.capability_build_contract is not None:
            contract_context = (
                "\n\nAuthoritative Capability Build Contract:\n"
                + state.capability_build_contract.model_dump_json(indent=2)
                + "\nDo not replace this contract with a generic summary."
            )
        if state.coordinator_messages:
            messages = [ChatMessage.model_validate(item) for item in state.coordinator_messages]
            messages.append(ChatMessage(role="user", content=[ContentBlock(
                type="text",
                text=(
                    "Resume the same build from its persisted draft and team state. Inspect current status, "
                    "resolve remaining failures, and complete the original acceptance criteria."
                    + contract_context
                ),
            )]))
        else:
            messages = [ChatMessage(role="user", content=[ContentBlock(
                type="text",
                text=(
                    f"Build and verify this application:\n\n{build['requirement']}\n\n"
                    f"Application id: {build['application_id']}. Auto publish: {build['auto_publish']}."
                    + contract_context
                ),
            )])]
        # Create a DecisionTracker to record the Builder's choices
        tracker = DecisionTracker(f"Build-{build_id[:8]}")
        try:
            try:
                if max_elapsed_seconds is not None:
                    async with asyncio.timeout(max_elapsed_seconds):
                        await self._agent_loop(
                            build_id,
                            build["application_id"],
                            state,
                            messages,
                            max_turns=int(build["max_turns"]),
                            max_repair_cycles=int(build["max_repair_cycles"]),
                            auto_publish=bool(build["auto_publish"]),
                            teammate=None,
                            tracker=tracker,
                            build_started_at=build_started_at,
                            max_elapsed_seconds=max_elapsed_seconds,
                        )
                else:
                    await self._agent_loop(
                        build_id,
                        build["application_id"],
                        state,
                        messages,
                        max_turns=int(build["max_turns"]),
                        max_repair_cycles=int(build["max_repair_cycles"]),
                        auto_publish=bool(build["auto_publish"]),
                        teammate=None,
                        tracker=tracker,
                    )
            except asyncio.TimeoutError as error:
                elapsed_seconds = time.monotonic() - build_started_at
                await self._emit(build_id, "build.deadline.exceeded", {
                    "max_elapsed_seconds": max_elapsed_seconds,
                    "elapsed_seconds": round(elapsed_seconds, 3),
                })
                raise BuildDeadlineExceeded(
                    max_elapsed_seconds or 0,
                    elapsed_seconds,
                ) from error
            self._trackers[build_id] = tracker
            await self._validate_capability_contract_completion(
                build["application_id"],
                state,
            )
            if state.published_version is not None:
                status = "published"
            else:
                await self._ensure_mandatory_smoke_test(build_id, build["application_id"], state)
                validation = await self.applications.validate_draft(build["application_id"])
                if not validation["valid"]:
                    raise RuntimeError("builder stopped with invalid draft: " + "; ".join(validation["errors"]))
                report = await self.runtime.run_test_suite(build["application_id"])
                if not report["passed"]:
                    raise RuntimeError("builder stopped before mandatory tests passed")
                if build["auto_publish"]:
                    published = await self.workflow_store.publish(build["application_id"])
                    state.published_version = published["version"]
                    await self._emit(build_id, "build.published", published)
                    status = "published"
                else:
                    status = "ready"
            await self.workflow_store.update_build(build_id, status=status, team_state=state)
            if manage_harness_task:
                await self.harness.finish_task(build_id, status="succeeded")
            await self._emit(build_id, "build.completed", {
                "status": status, "published_version": state.published_version
            })
            # Meta-cognition: try to extract a reusable template from this build
            if self.on_build_complete and (status == "published" or status == "ready"):
                asyncio.create_task(self.on_build_complete(build_id))
            return {
                "build_id": build_id,
                "application_id": build["application_id"],
                "status": status,
                "published_version": state.published_version,
            }
        except asyncio.CancelledError:
            await self.workflow_store.update_build(build_id, status="cancelled", team_state=state)
            if manage_harness_task:
                await self.harness.finish_task(build_id, status="cancelled")
            await self._emit(build_id, "build.cancelled", {})
            raise
        except Exception as error:
            failure_metadata = self._failure_metadata(error)
            await self.workflow_store.update_build(
                build_id, status="needs_attention", team_state=state, error=str(error)
            )
            if manage_harness_task:
                await self.harness.finish_task(
                    build_id,
                    status="failed",
                    error=str(error),
                    metadata=failure_metadata,
                )
            await self._emit(build_id, "build.needs_attention", {
                "error": str(error),
                "error_type": type(error).__name__,
                **failure_metadata,
            })
            if not manage_harness_task:
                raise

    def _capability_resource_inventory(self, snapshot: Any) -> dict[str, Any]:
        marketplace_templates = (
            [f"template:marketplace:{name}" for name in self.template_store.names()]
            if self.template_store
            else []
        )
        return {
            "workflow_nodes": [
                {
                    "reference": node.id,
                    "node_type": node.type,
                    "title": node.title,
                }
                for node in snapshot.workflow.nodes
            ],
            "server_templates": [
                f"template:server_defined:{name}"
                for name in self.blocks.template_names()
            ],
            "marketplace_templates": marketplace_templates,
            "runtime_services": [
                "runtime:workflow_runtime",
                "runtime:agent_runtime",
                "runtime:metadata_storage",
                "runtime:checkpoint_store",
            ],
            "platform_controls": [
                "platform:application_service",
                "platform:block_registry",
                "platform:platform_harness",
            ],
            "external_resources": [
                "external:model_provider",
                "external:tool_registry",
            ],
        }

    def _invalid_capability_references(
        self,
        snapshot: Any,
        contract: Any,
        decision: CapabilityCarrierDecision,
    ) -> list[str]:
        inventory = self._capability_resource_inventory(snapshot)
        node_refs = {item["reference"] for item in inventory["workflow_nodes"]}
        allowed_by_type = {
            CarrierType.atomic_block: node_refs,
            CarrierType.reusable_module: node_refs
            | set(inventory["server_templates"])
            | set(inventory["marketplace_templates"]),
            CarrierType.runtime_service: node_refs | set(inventory["runtime_services"]),
            CarrierType.platform_control: node_refs | set(inventory["platform_controls"]),
            CarrierType.connector_external_contract: node_refs
            | set(inventory["external_resources"]),
        }
        capability = next(
            (item for item in contract.capabilities if item.id == decision.capability_id),
            None,
        )
        allowed = set(allowed_by_type[decision.carrier_type])
        if (
            isinstance(capability, ExternalContract)
            and capability.availability == EnvironmentAvailability.unavailable
            and decision.status == CarrierStatus.blocked_by_environment
        ):
            allowed.add(f"contract:{capability.id}")
        return sorted(
            reference
            for reference in decision.implementation_refs
            if reference not in allowed
        )

    async def _validate_capability_contract_completion(
        self,
        application_id: str,
        state: BuildTeamState,
    ) -> dict[str, Any] | None:
        draft = await self.workflow_store.get_draft(application_id)
        contract = draft["snapshot"].capability_build_contract
        if contract is None:
            state.capability_build_contract = None
            state.capability_closure = None
            return None
        closure = evaluate_capability_contract(contract, require_bound_carriers=True)
        if not closure.valid:
            raise RuntimeError(
                "capability contract is not ready for build completion: "
                + "; ".join(closure.blocking_errors)
            )
        if state.build_plan is None:
            raise RuntimeError("Capability Build Contract requires a BuildPlan before completion")
        if state.build_plan.capability_contract_id != contract.contract_id:
            raise RuntimeError(
                "BuildPlan capability_contract_id does not match the application contract"
            )
        known = {item.id for item in contract.capabilities}
        required = {item.id for item in contract.capabilities if item.required}
        covered = {
            capability_id
            for module in state.build_plan.modules
            for capability_id in module.capability_ids
        }
        unknown = sorted(covered - known)
        missing = sorted(required - covered)
        if unknown:
            raise RuntimeError(f"BuildPlan references unknown capability ids: {unknown}")
        if missing:
            raise RuntimeError(f"BuildPlan does not cover required capability ids: {missing}")

        invalid_refs: list[dict[str, Any]] = []
        for decision in contract.carrier_decisions:
            if decision.capability_id not in required:
                continue
            invalid = self._invalid_capability_references(
                draft["snapshot"],
                contract,
                decision,
            )
            if invalid:
                invalid_refs.append({
                    "capability_id": decision.capability_id,
                    "references": invalid,
                })
        if invalid_refs:
            raise RuntimeError(
                "carrier bindings reference resources that are not present in the draft or registered inventory: "
                f"{invalid_refs}"
            )
        state.capability_build_contract = contract
        state.capability_closure = closure.model_dump(mode="json")
        return state.capability_closure

    async def _ensure_mandatory_smoke_test(
        self, build_id: str, application_id: str, state: BuildTeamState
    ) -> None:
        draft = await self.workflow_store.get_draft(application_id)
        snapshot = draft["snapshot"]
        if any(test.mandatory for test in snapshot.tests) or not snapshot.workflow.nodes:
            return

        inputs = self._smoke_inputs(snapshot.workflow.nodes)
        node_types = sorted({node.type for node in snapshot.workflow.nodes})
        tool_nodes = sorted({
            str(node.config.get("tool_name"))
            for node in snapshot.workflow.nodes
            if node.type == "tool" and node.config.get("tool_name")
        } | {
            str(node.config.get("settings", {}).get("tool_name"))
            for node in snapshot.workflow.nodes
            if node.type == "tool_executor" and node.config.get("settings", {}).get("tool_name")
        })
        test = WorkflowTestCase(
            id="auto_smoke_acceptance",
            name="Auto smoke acceptance",
            requirement="Builder preflight generated this mandatory smoke test because no mandatory test was provided.",
            frame=TestFrameSpec(
                title="Auto smoke acceptance",
                category="structure",
                purpose="Verify the generated BlockFlow can execute end to end before it is marked ready.",
                reviewer_guidance=(
                    "Replace this generated smoke test with task-specific acceptance tests in the next repair pass "
                    "if stronger content or tool evidence checks are needed."
                ),
                reference="Builder preflight test gate",
                failure_target="workflow graph, start inputs, or final output blocks",
            ),
            inputs=inputs,
            assertions=[TestAssertion(path=[], operator="exists", structural=True)],
            required_node_types=node_types,
            required_tool_nodes=tool_nodes,
            mandatory=True,
            structural_only=True,
            feedback_hints=[
                "The Builder did not create a task-specific mandatory test.",
                "Inspect whether the workflow executes end to end before adding stronger assertions.",
            ],
            capability_ids=(
                [
                    item.id
                    for item in snapshot.capability_build_contract.capabilities
                    if item.required
                ]
                if snapshot.capability_build_contract is not None
                else []
            ),
            evidence_target=(
                AcceptanceEvidenceTarget(
                    level=snapshot.capability_build_contract.evidence_plan[0].target_level,
                    environment=snapshot.capability_build_contract.evidence_plan[0].environment,
                    expected_status=snapshot.capability_build_contract.evidence_plan[0].expected_status,
                    claim_scope=snapshot.capability_build_contract.evidence_plan[0].claim_scope,
                )
                if snapshot.capability_build_contract is not None
                and snapshot.capability_build_contract.evidence_plan
                else None
            ),
        )
        result = await self.applications.apply_operation(
            application_id,
            DraftOperation(
                expected_revision=int(draft["revision"]),
                idempotency_key=f"{build_id}:auto_smoke_acceptance",
                op="add_test",
                data={"test": test.model_dump(mode="json")},
            ),
        )
        state.revision = result["revision"]
        await self._emit(build_id, "build.preflight_test_added", {
            "test_id": test.id,
            "revision": state.revision,
            "reason": "missing mandatory acceptance test",
        })

    @staticmethod
    def _smoke_inputs(nodes: list[NodeSpec]) -> dict[str, Any]:
        inputs: dict[str, Any] = {}
        for node in nodes:
            if node.type != "start":
                continue
            for field in node.config.get("inputs", []):
                if not isinstance(field, dict) or not field.get("name"):
                    continue
                name = str(field["name"])
                field_type = str(field.get("type", "string"))
                if field_type in {"integer", "number"}:
                    inputs[name] = 1
                elif field_type == "boolean":
                    inputs[name] = True
                elif field_type == "array":
                    inputs[name] = ["test"]
                elif field_type == "object":
                    inputs[name] = {"value": "test"}
                else:
                    inputs[name] = "test"
        return inputs

    @staticmethod
    def _validate_test_requirements_available(test: WorkflowTestCase, snapshot: Any) -> None:
        node_types = sorted({node.type for node in snapshot.workflow.nodes})
        tool_node_names = sorted({
            str(node.config.get("tool_name"))
            for node in snapshot.workflow.nodes
            if node.type == "tool" and node.config.get("tool_name")
        } | {
            str(node.config.get("settings", {}).get("tool_name"))
            for node in snapshot.workflow.nodes
            if node.type == "tool_executor" and node.config.get("settings", {}).get("tool_name")
        })
        missing_node_types = sorted(set(test.required_node_types) - set(node_types))
        missing_tool_nodes = sorted(set(test.required_tool_nodes) - set(tool_node_names))
        messages: list[str] = []
        if missing_node_types:
            messages.append(
                "test required unavailable node types: "
                f"{missing_node_types}; available node types: {node_types}. "
                "Update required_node_types to match the current draft, or add the missing block before adding the test."
            )
        if missing_tool_nodes:
            messages.append(
                "test required unavailable tool nodes: "
                f"{missing_tool_nodes}; available tool nodes: {tool_node_names}. "
                "Update required_tool_nodes to match tool nodes already present in the draft, or add the missing tool node first."
            )
        if messages:
            raise RuntimeError(" ".join(messages))

    @staticmethod
    def _validate_node_removal_keeps_test_requirements(node_id: str, snapshot: Any) -> None:
        node = next((item for item in snapshot.workflow.nodes if item.id == node_id), None)
        if node is None:
            return
        remaining_node_types = [item.type for item in snapshot.workflow.nodes if item.id != node_id]
        blocked_tests: list[str] = []
        for test in snapshot.tests:
            if not test.mandatory or node.type not in test.required_node_types:
                continue
            if node.type not in remaining_node_types:
                blocked_tests.append(test.id)
        if blocked_tests:
            raise RuntimeError(
                f"removing node {node_id!r} would break mandatory test required_node_types "
                f"for node type {node.type!r}: {blocked_tests}. "
                "Update or remove the affected tests first, or add a replacement node with the same type."
            )

    async def _draft_validation_summary(self, application_id: str) -> dict[str, Any]:
        validation = await self.applications.validate_draft(application_id)
        return {
            "valid": validation["valid"],
            "errors": validation["errors"],
            "warnings": validation["warnings"],
            "revision": validation["revision"],
            "test_count": validation["test_count"],
        }

    def _template_contract(self, template_name: str, source: str) -> dict[str, Any] | None:
        if source == "server_defined" and template_name == "codex_like_workspace_agent":
            return {
                "name": template_name,
                "title": "Codex-like Workspace Agent",
                "category": "workspace_agent",
                "expected_inputs": ["task", "workspace_path", "network_policy", "cancel_requested"],
                "expected_outputs": ["answer"],
                "min_blocks_required": 13,
                "evidence_level": "component_verified",
                "claim_scope": "deterministic local workspace; live and production environments not implied",
            }
        if source != "marketplace" or not self.template_store:
            return None
        try:
            template = self.template_store.get(template_name)
        except KeyError:
            return None
        meta = template.meta
        return {
            "name": meta.name,
            "title": meta.title,
            "category": meta.category,
            "expected_inputs": meta.expected_inputs,
            "expected_outputs": meta.expected_outputs,
            "min_blocks_required": meta.min_blocks_required,
            "confidence": meta.confidence,
            "tags": meta.tags,
        }

    async def _agent_loop(
        self,
        build_id: str,
        application_id: str,
        state: BuildTeamState,
        messages: list[ChatMessage],
        *,
        max_turns: int,
        max_repair_cycles: int,
        auto_publish: bool,
        teammate: str | None,
        tracker: DecisionTracker | None = None,
        build_started_at: float | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> str:
        final = ""
        tools = self._definitions(
            allow_team=teammate is None,
            planning_mode=state.planning_mode,
        )
        for turn in range(1, max_turns + 1):
            teammate_stop_reason: str | None = None
            await self.harness.record_usage(
                build_id,
                "model_call",
                metadata={"actor": teammate or "coordinator", "turn": turn, "model": self.generator_model},
            )
            stream = self.provider.stream(
                model=self.generator_model,
                system=BUILDER_SYSTEM_PROMPT + self._planning_mode_prompt(state.planning_mode) + (
                    f"\nYou are teammate {teammate}. Complete your assigned bounded task and report evidence."
                    if teammate else "\nYou are the coordinator. Delegate when useful and synthesize results."
                ),
                messages=messages,
                tools=tools,
                max_output_tokens=16_384,
                thinking_enabled=True,
                effort="xhigh",
                tool_choice={"type": "auto"},
                user_id=f"{build_id}-{teammate or 'coordinator'}",
            )
            response = await self.agent_runtime._collect_stream(
                build_id,
                stream,
                f"build.{teammate or 'coordinator'}.model",
                self.generator_model,
            )
            messages.append(ChatMessage(role="assistant", content=response.blocks))
            calls = [block for block in response.blocks if block.type == "tool_use"]
            if not calls:
                final = "".join(block.text or "" for block in response.blocks if block.type == "text")
                if teammate is None:
                    state.coordinator_messages = [
                        message.model_dump(mode="json") for message in messages
                    ]
                break
            results: list[ContentBlock] = []
            for call in calls:
                try:
                    await self.harness.record_usage(
                        build_id,
                        "tool_call",
                        metadata={"actor": teammate or "coordinator", "tool": call.name or ""},
                    )
                    invalid_json = (call.input or {}).get(INVALID_TOOL_INPUT_JSON_KEY)
                    if invalid_json is not None:
                        error = (
                            invalid_json.get("error", "unknown parse error")
                            if isinstance(invalid_json, dict)
                            else "unknown parse error"
                        )
                        raise RuntimeError(
                            f"invalid tool input JSON for {call.name or ''}: {error}. "
                            "Re-emit this tool call with valid JSON arguments."
                        )
                    value = await self._execute(
                        build_id,
                        application_id,
                        state,
                        call.name or "",
                        call.input or {},
                        max_repair_cycles=max_repair_cycles,
                        auto_publish=auto_publish,
                        tracker=tracker,
                        build_started_at=build_started_at,
                        max_elapsed_seconds=max_elapsed_seconds,
                    )
                    content = json.dumps(value, ensure_ascii=False, default=str)
                    is_error = False
                except Exception as error:
                    content = f"{type(error).__name__}: {error}"
                    is_error = True
                    if (
                        teammate is not None
                        and (call.name or "") == "test_run"
                        and self._is_repair_budget_exhausted_message(str(error))
                    ):
                        teammate_stop_reason = TEAMMATE_REPAIR_BUDGET_EXHAUSTED_REASON
                results.append(ContentBlock(
                    type="tool_result", tool_use_id=call.id, content=content, is_error=is_error
                ))
                await self._emit(build_id, "build.operation", {
                    "actor": teammate or "coordinator",
                    "tool": call.name,
                    "input": self._redact(call.input or {}),
                    "success": not is_error,
                    "result": content[:10_000],
                })
            messages.append(ChatMessage(role="user", content=results))
            if teammate is None:
                state.coordinator_messages = [
                    message.model_dump(mode="json") for message in messages
                ]
            await self.workflow_store.update_build(build_id, team_state=state)
            if teammate is not None and teammate_stop_reason is not None:
                final = (
                    "Stopped teammate work after test_run exhausted the repair budget at the current draft "
                    "revision. Return the current findings to the coordinator instead of continuing "
                    "long-tail debugging in this branch."
                )
                await self._emit(build_id, "team.teammate.stopped", {
                    "name": teammate,
                    "reason": teammate_stop_reason,
                    "draft_revision": state.revision,
                })
                break
            if state.published_version is not None:
                break
            turn_completed = {
                "actor": teammate or "coordinator",
                "turn": turn,
                "draft_revision": state.revision,
            }
            if build_started_at is not None:
                turn_completed["elapsed_seconds"] = round(time.monotonic() - build_started_at, 3)
            if max_elapsed_seconds is not None:
                turn_completed["max_elapsed_seconds"] = max_elapsed_seconds
            await self._emit(build_id, "build.turn.completed", turn_completed)
        return final

    async def _execute(
        self,
        build_id: str,
        application_id: str,
        state: BuildTeamState,
        tool: str,
        data: dict[str, Any],
        *,
        max_repair_cycles: int,
        auto_publish: bool,
        tracker: DecisionTracker | None = None,
        build_started_at: float | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> Any:
        if state.planning_mode == "disabled" and tool == "build_plan":
            raise RuntimeError("build_plan is disabled for this build planning_mode")
        if tool == "catalog_search":
            query = str(data.get("query", "")).casefold()
            definitions = [
                item for item in self.blocks.list()
                if not query or query in f"{item.type} {item.title} {item.description} {item.category}".casefold()
            ]
            results: list[dict[str, Any]] = [
                {"type": item.type, "title": item.title, "description": item.description, "category": item.category}
                for item in definitions
            ]
            for application in await self.workflow_store.list_applications():
                if application["active_version"] is None:
                    continue
                searchable = f"{application['name']} {application['description']} workflow tool".casefold()
                if query and query not in searchable:
                    continue
                results.append({
                    "resource_type": "workflow_tool",
                    "name": f"workflow:{application['id']}",
                    "title": application["name"],
                    "description": application["description"],
                    "version": application["active_version"],
                })
            for name in self.core_tools.names():
                definition = self.core_tools.get(name).definition()
                searchable = f"{name} {definition.description} core tool".casefold()
                if query and query not in searchable:
                    continue
                results.append({
                    "resource_type": "core_tool",
                    "name": name,
                    "description": definition.description,
                })
            return results
        if tool == "catalog_get":
            name = str(data["type"])
            if name in self.core_tools.names():
                return self.core_tools.get(name).definition().model_dump(mode="json")
            for candidate in self.core_tools.names():
                if candidate.casefold() == name.casefold():
                    definition = self.core_tools.get(candidate).definition().model_dump(mode="json")
                    definition["canonical_name"] = candidate
                    return definition
            return self.blocks.get(name).model_dump(mode="json")
        if tool == "manual_search":
            query = str(data.get("query", ""))
            block_kind = data.get("block_kind")
            manuals = self.blocks.manuals(query=query, block_kind=str(block_kind) if block_kind else None)
            for manual in manuals:
                self._remember_manual_lookup(state, str(manual["type"]))
            return manuals
        if tool == "manual_get":
            block_type = str(data["type"])
            manual = self.blocks.manual(block_type)
            self._remember_manual_lookup(state, block_type)
            return manual
        if tool == "architecture_blueprint":
            blueprint = self.blocks.claude_architecture_blueprint()
            for group in blueprint["groups"].values():
                for manual in group:
                    self._remember_manual_lookup(state, str(manual["type"]))
            return blueprint
        if tool == "template_suggestions":
            requirement = str(data.get("requirement", ""))
            reuse_depth, default_metadata = suggestion_default_metadata(
                data.get("reuse_depth"),
                build_plan_reuse_depth=state.build_plan.reuse_depth if state.build_plan else None,
                runtime_policy_reuse_depth=(state.runtime_builder_policy or {}).get("reuse_depth"),
                runtime_policy_version=(
                    (state.complexity_router or {}).get("policy_version")
                    if state.complexity_router
                    else None
                ),
            )
            if reuse_depth not in ALLOWED_REUSE_DEPTHS:
                allowed = ", ".join(sorted(ALLOWED_REUSE_DEPTHS))
                raise RuntimeError(f"reuse_depth must be one of: {allowed}")
            if reuse_depth == "none":
                return {
                    "reuse_depth": reuse_depth,
                    "effective_reuse_depth": "none",
                    "recommended_action": "build_from_scratch",
                    "policy_reason": "explicit:none",
                    **default_metadata,
                    "templates": [],
                }
            templates = self.template_store.list() if self.template_store else []
            scored = score_template_matches(requirement, templates)
            # Bump usage_count for top matches (feedback: Builder selected this template)
            for _, meta in scored[:3]:
                if hasattr(meta, "usage_count"):
                    meta.usage_count += 1  # recommendation flywheel: template was chosen
            top_meta = scored[0][1] if scored else None
            effective_reuse_depth, policy_reason = resolve_effective_reuse_depth(reuse_depth, top_meta)
            result = {
                "reuse_depth": reuse_depth,
                "effective_reuse_depth": effective_reuse_depth,
                "recommended_action": recommended_action_for_depth(effective_reuse_depth),
                "policy_reason": policy_reason,
                **default_metadata,
                "templates": [
                    {
                        **build_suggestion_payload(
                            m,
                            s,
                            reuse_depth,
                            default_metadata=default_metadata,
                        ),
                        "source": "marketplace",
                        "relevance": round(s, 3),
                    }
                    for s, m in scored[:5]
                ],
            }
            if default_metadata.get("defaulted_by_policy"):
                result["execution_contract"] = policy_default_execution_contract(
                    effective_reuse_depth,
                    reuse_depth_source=str(default_metadata.get("reuse_depth_source") or "policy_default"),
                )
            return result
        if tool == "template_list":
            templates = [
                {
                    "name": name,
                    "title": name,
                    "source": "server_defined",
                    "description": (
                        "Editable Codex-like plan-act-observe workspace agent with structured tool feedback."
                        if name == "codex_like_workspace_agent"
                        else "Editable legacy Claude-like coding agent architecture subgraph."
                    ),
                }
                for name in self.blocks.template_names()
            ]
            if self.template_store:
                for meta in self.template_store.list():
                    templates.append({
                        "name": meta.name,
                        "title": meta.title,
                        "source": "marketplace",
                        "description": meta.description,
                        "category": meta.category,
                        "tags": meta.tags,
                        "confidence": meta.confidence,
                        "recommended_action": "expand_template",
                    })
            return templates
        if tool == "template_expand":
            self._enforce_planning_required(state, tool)
            template_name = str(data["name"])
            prefix = str(data.get("prefix") or template_name)
            position = data.get("position") if isinstance(data.get("position"), dict) else {}
            x = float(position.get("x", 0))
            y = float(position.get("y", 0))
            marketplace_names = set(self.template_store.names()) if self.template_store else set()
            if self.template_store and template_name in marketplace_names:
                source = "marketplace"
                workflow = self.template_store.expand_into_workflow(
                    template_name,
                    prefix=prefix,
                    x=x,
                    y=y,
                )
            else:
                source = "server_defined"
                workflow = self.blocks.expand_template(
                    template_name,
                    prefix=prefix,
                    x=x,
                    y=y,
                )
            draft = await self.workflow_store.get_draft(application_id)
            revision = int(draft["revision"])
            for node in workflow.nodes:
                definition = self.blocks.get(node.type)
                if definition.block_kind == "agent_architecture":
                    self._remember_manual_lookup(state, node.type)
                result = await self.applications.apply_operation(
                    application_id,
                    DraftOperation(
                        expected_revision=revision,
                        idempotency_key=f"{build_id}:template_expand:{template_name}:{node.id}",
                        op="add_node",
                        data={"node": node.model_dump(mode="json")},
                    ),
                )
                revision = int(result["revision"])
            for edge in workflow.edges:
                result = await self.applications.apply_operation(
                    application_id,
                    DraftOperation(
                        expected_revision=revision,
                        idempotency_key=f"{build_id}:template_expand:{template_name}:{edge.id}",
                        op="add_edge",
                        data={"edge": edge.model_dump(mode="json")},
                    ),
                )
                revision = int(result["revision"])
            state.revision = revision
            validation_errors = self.blocks.validate_workflow(workflow)
            return {
                "template": template_name,
                "source": source,
                "revision": revision,
                "nodes": [node.id for node in workflow.nodes],
                "edges": [edge.id for edge in workflow.edges],
                "node_types": sorted({node.type for node in workflow.nodes}),
                "edge_count": len(workflow.edges),
                "validation": {
                    "valid": not validation_errors,
                    "errors": validation_errors,
                },
                "draft_validation": await self._draft_validation_summary(application_id),
                "template_contract": self._template_contract(template_name, source),
            }
        if tool == "capability_contract":
            action = str(data["action"])
            draft = await self.workflow_store.get_draft(application_id)
            contract = draft["snapshot"].capability_build_contract
            if contract is None:
                raise RuntimeError("application has no Capability Build Contract")
            if action == "get":
                closure = evaluate_capability_contract(contract)
                state.capability_build_contract = contract
                state.capability_closure = closure.model_dump(mode="json")
                return {
                    "contract": contract.model_dump(mode="json"),
                    "closure": state.capability_closure,
                    "routing": state.capability_routing,
                    "resource_inventory": self._capability_resource_inventory(
                        draft["snapshot"]
                    ),
                }
            if action == "validate":
                if bool(data.get("require_bound", False)):
                    closure = await self._validate_capability_contract_completion(
                        application_id,
                        state,
                    )
                    return {"valid": True, "closure": closure}
                closure = evaluate_capability_contract(contract)
                return closure.model_dump(mode="json")
            if action == "bind":
                self._enforce_planning_required(state, tool)
                capability_id = str(data["capability_id"])
                if capability_id not in {item.id for item in contract.capabilities}:
                    raise KeyError(f"unknown capability id: {capability_id}")
                existing = next(
                    (
                        item
                        for item in contract.carrier_decisions
                        if item.capability_id == capability_id
                    ),
                    None,
                )
                carrier_type = CarrierType(
                    data.get("carrier_type")
                    or (existing.carrier_type if existing else "")
                )
                status = CarrierStatus(
                    data.get("status") or CarrierStatus.bound.value
                )
                implementation_refs = [
                    str(item)
                    for item in data.get(
                        "implementation_refs",
                        existing.implementation_refs if existing else [],
                    )
                    if str(item).strip()
                ]
                if status in {CarrierStatus.bound, CarrierStatus.blocked_by_environment} and not implementation_refs:
                    raise ValueError("bound carrier decisions require implementation_refs")
                decision = CapabilityCarrierDecision(
                    capability_id=capability_id,
                    carrier_type=carrier_type,
                    resource_hint=str(
                        data.get("resource_hint")
                        or (existing.resource_hint if existing else "")
                    ),
                    rationale=str(
                        data.get("rationale")
                        or (existing.rationale if existing else "")
                    ),
                    status=status,
                    implementation_refs=implementation_refs,
                )
                invalid_references = self._invalid_capability_references(
                    draft["snapshot"],
                    contract,
                    decision,
                )
                if invalid_references:
                    raise ValueError(
                        "carrier references are not present in the draft or registered inventory: "
                        f"{invalid_references}"
                    )
                updated = contract.model_copy(deep=True)
                updated.carrier_decisions = [
                    decision if item.capability_id == capability_id else item
                    for item in updated.carrier_decisions
                ]
                if existing is None:
                    updated.carrier_decisions.append(decision)
                if data.get("owner"):
                    owner = CoverageOwner(str(data["owner"]))
                    coverage_status = CoverageStatus(
                        str(data.get("coverage_status") or CoverageStatus.available.value)
                    )
                    coverage = CapabilityCoverage(
                        capability_id=capability_id,
                        owner=owner,
                        status=coverage_status,
                        surface=str(data.get("surface") or implementation_refs[0]),
                        notes=str(data.get("notes") or ""),
                    )
                    replaced = False
                    coverage_items: list[CapabilityCoverage] = []
                    for item in updated.platform_coverage:
                        if item.capability_id == capability_id:
                            if not replaced:
                                coverage_items.append(coverage)
                                replaced = True
                        else:
                            coverage_items.append(item)
                    if not replaced:
                        coverage_items.append(coverage)
                    updated.platform_coverage = coverage_items
                closure = evaluate_capability_contract(updated)
                if not closure.valid:
                    raise RuntimeError(
                        "carrier update makes the capability contract invalid: "
                        + "; ".join(closure.blocking_errors)
                    )
                result = await self.applications.apply_operation(
                    application_id,
                    DraftOperation(
                        expected_revision=int(draft["revision"]),
                        idempotency_key=f"{build_id}:capability_contract:{capability_id}:{uuid4()}",
                        op="set_capability_build_contract",
                        data={"contract": updated.model_dump(mode="json")},
                    ),
                )
                state.revision = int(result["revision"])
                state.capability_build_contract = updated
                state.capability_closure = closure.model_dump(mode="json")
                return {
                    **result,
                    "capability_id": capability_id,
                    "carrier": decision.model_dump(mode="json"),
                    "closure": state.capability_closure,
                }
            raise ValueError(f"unknown capability_contract action: {action}")
        if tool == "draft_inspect":
            draft = await self.workflow_store.get_draft(application_id)
            state.revision = int(draft["revision"])
            return {
                "revision": draft["revision"],
                "content_hash": draft["content_hash"],
                "snapshot": draft["snapshot"].model_dump(mode="json"),
            }
        if tool in {
            "draft_add_node", "draft_update_node", "draft_remove_node", "draft_connect",
            "draft_remove_edge", "draft_upsert_agent", "test_add", "test_remove",
        }:
            self._enforce_planning_required(state, tool)
            draft = await self.workflow_store.get_draft(application_id)
            if tool == "draft_add_node":
                node = NodeSpec.model_validate(data["node"])
                definition = self.blocks.get(node.type)
                if definition.block_kind == "agent_architecture" and node.type not in state.manual_lookups:
                    raise RuntimeError(f"manual lookup required before using agent architecture block: {node.type}")
                op, payload = "add_node", {
                    "node": node.model_dump(mode="json")
                }
            elif tool == "draft_update_node":
                op, payload = "update_node", {
                    "node_id": data["node_id"],
                    "changes": data["changes"],
                    "merge_config": data.get("merge_config", True),
                }
            elif tool == "draft_remove_node":
                self._validate_node_removal_keeps_test_requirements(str(data["node_id"]), draft["snapshot"])
                op, payload = "remove_node", {"node_id": data["node_id"]}
            elif tool == "draft_connect":
                op, payload = "add_edge", {
                    "edge": EdgeSpec.model_validate(data["edge"]).model_dump(mode="json")
                }
            elif tool == "draft_remove_edge":
                op, payload = "remove_edge", {"edge_id": data["edge_id"]}
            elif tool == "draft_upsert_agent":
                op, payload = "upsert_agent", {
                    "agent": AgentSpec.model_validate(data["agent"]).model_dump(mode="json")
                }
            elif tool == "test_add":
                test = WorkflowTestCase.model_validate(data["test"])
                self._validate_test_requirements_available(test, draft["snapshot"])
                contract = draft["snapshot"].capability_build_contract
                if contract is not None:
                    known_capabilities = {item.id for item in contract.capabilities}
                    unknown_capabilities = sorted(
                        set(test.capability_ids) - known_capabilities
                    )
                    if unknown_capabilities:
                        raise RuntimeError(
                            "test references unknown capability ids: "
                            f"{unknown_capabilities}"
                        )
                    if not test.capability_ids:
                        raise RuntimeError(
                            "tests for a Capability Build Contract must declare capability_ids"
                        )
                    if test.evidence_target is None:
                        raise RuntimeError(
                            "tests for a Capability Build Contract must declare evidence_target"
                        )
                op, payload = "add_test", {
                    "test": test.model_dump(mode="json")
                }
            else:
                op, payload = "remove_test", {"test_id": data["test_id"]}
            result = await self.applications.apply_operation(
                application_id,
                DraftOperation(
                    expected_revision=int(draft["revision"]),
                    idempotency_key=f"{build_id}:{tool}:{uuid4()}",
                    op=op,
                    data=payload,
                ),
            )
            # Record design decisions for meta-cognition
            if tracker and tool in ("draft_add_node", "draft_connect", "draft_publish", "template_expand"):
                decision_label = {
                    "draft_add_node": f"Add node: {data.get('node', {}).get('type', '?')}",
                    "draft_connect": f"Connect: {data.get('edge', {}).get('source', '?')}→{data.get('edge', {}).get('target', '?')}",
                    "draft_publish": "Publish workflow",
                    "template_expand": f"Expand template: {data.get('name', '?')}",
                }.get(tool, tool)
                tracker._current = tracker.ask(decision_label, f"Build {build_id[:8]}")
                tracker.answer("proceed", f"Revision {result['revision']}", f"{tool} succeeded")
            state.revision = result["revision"]
            if tool in {"draft_update_node", "draft_remove_node", "draft_connect", "draft_remove_edge", "test_add"}:
                result["validation"] = await self._draft_validation_summary(application_id)
            return result
        if tool == "draft_validate":
            return await self.applications.validate_draft(application_id)
        if tool == "test_run":
            if state.repair_cycles > max_repair_cycles or (
                state.repair_cycles == max_repair_cycles
                and state.last_failed_test_revision == state.revision
            ):
                raise RuntimeError(f"maximum repair cycles reached ({max_repair_cycles})")
            report = await self.runtime.run_test_suite(application_id)
            if not report["passed"]:
                if state.last_failed_test_revision != state.revision:
                    state.repair_cycles += 1
                    state.last_failed_test_revision = state.revision
            else:
                state.last_failed_test_revision = None
            return report
        if tool == "draft_publish":
            if not auto_publish and not data.get("explicit", False):
                return {"status": "ready", "message": "auto publish is disabled"}
            await self._validate_capability_contract_completion(application_id, state)
            published = await self.workflow_store.publish(application_id)
            state.published_version = published["version"]
            await self._emit(build_id, "build.published", published)
            return published
        if tool == "build_plan":
            action = str(data["action"])
            if action == "set":
                plan = BuildPlan.model_validate(data["plan"])
                if state.capability_build_contract is not None:
                    contract = state.capability_build_contract
                    if plan.capability_contract_id != contract.contract_id:
                        raise RuntimeError(
                            "BuildPlan must reference the authoritative capability contract id"
                        )
                    known = {item.id for item in contract.capabilities}
                    planned = {
                        capability_id
                        for module in plan.modules
                        for capability_id in module.capability_ids
                    }
                    unknown = sorted(planned - known)
                    if unknown:
                        raise RuntimeError(
                            f"BuildPlan references unknown capability ids: {unknown}"
                        )
                state.build_plan = plan
                return state.build_plan.model_dump(mode="json")
            if action == "get":
                return state.build_plan.model_dump(mode="json") if state.build_plan else None
            if action == "update_module":
                if state.build_plan is None:
                    raise RuntimeError("build plan has not been set")
                module_id = str(data["module_id"])
                module = next(
                    (item for item in state.build_plan.modules if item.id == module_id),
                    None,
                )
                if module is None:
                    raise KeyError(f"unknown build plan module: {module_id}")
                changes = data.get("changes", {})
                updated = module.model_copy(update=changes)
                state.build_plan.modules = [
                    updated if item.id == module_id else item
                    for item in state.build_plan.modules
                ]
                return updated.model_dump(mode="json")
            raise ValueError(f"unknown build_plan action: {action}")
        if tool == "task":
            action = data["action"]
            if action == "create":
                task = BuildTask(
                    id=max([item.id for item in state.tasks] or [0]) + 1,
                    subject=data["subject"],
                    description=data.get("description", ""),
                    owner=data.get("owner"),
                    blocked_by=data.get("blocked_by", []),
                    acceptance=data.get("acceptance", []),
                )
                state.tasks.append(task)
            elif action == "update":
                task = next(item for item in state.tasks if item.id == int(data["id"]))
                for key in ("status", "owner", "subject", "description"):
                    if key in data:
                        setattr(task, key, data[key])
            return [item.model_dump(mode="json") for item in state.tasks]
        if tool == "spawn_teammate":
            name = str(data["name"])
            if name in state.teammates:
                raise ValueError(f"teammate already exists: {name}")
            blocked_reason = self._teammate_guard_reason(
                state,
                max_repair_cycles=max_repair_cycles,
                build_started_at=build_started_at,
                max_elapsed_seconds=max_elapsed_seconds,
            )
            if blocked_reason is not None:
                await self._emit(build_id, "team.teammate.blocked", {
                    "name": name,
                    "reason": blocked_reason,
                    "draft_revision": state.revision,
                })
                raise RuntimeError(blocked_reason)
            teammate = TeammateState(name=name, purpose=str(data["task"]))
            state.teammates[name] = teammate
            await self._emit(build_id, "team.teammate.spawned", teammate.model_dump(mode="json"))
            messages = [ChatMessage(role="user", content=[ContentBlock(type="text", text=str(data["task"]))])]
            result = await self._agent_loop(
                build_id,
                application_id,
                state,
                messages,
                max_turns=min(int(data.get("max_turns", 12)), 20),
                max_repair_cycles=max_repair_cycles,
                auto_publish=auto_publish,
                teammate=name,
                build_started_at=build_started_at,
                max_elapsed_seconds=max_elapsed_seconds,
            )
            teammate.messages = [message.model_dump(mode="json") for message in messages]
            teammate.status = "idle"
            await self._emit(build_id, "team.teammate.idle", {"name": name, "result": result[:5000]})
            return {"name": name, "status": "idle", "result": result}
        if tool == "send_message":
            name = str(data["name"])
            blocked_reason = self._teammate_guard_reason(
                state,
                max_repair_cycles=max_repair_cycles,
                build_started_at=build_started_at,
                max_elapsed_seconds=max_elapsed_seconds,
            )
            if blocked_reason is not None:
                await self._emit(build_id, "team.teammate.blocked", {
                    "name": name,
                    "reason": blocked_reason,
                    "draft_revision": state.revision,
                })
                raise RuntimeError(blocked_reason)
            teammate = state.teammates[name]
            teammate.mailbox.append(str(data["message"]))
            teammate.status = "working"
            messages = [ChatMessage.model_validate(item) for item in teammate.messages]
            messages.append(ChatMessage(role="user", content=[ContentBlock(type="text", text=str(data["message"]))]))
            result = await self._agent_loop(
                build_id,
                application_id,
                state,
                messages,
                max_turns=min(int(data.get("max_turns", 8)), 12),
                max_repair_cycles=max_repair_cycles,
                auto_publish=auto_publish,
                teammate=name,
                build_started_at=build_started_at,
                max_elapsed_seconds=max_elapsed_seconds,
            )
            teammate.messages = [message.model_dump(mode="json") for message in messages]
            teammate.mailbox.clear()
            teammate.status = "idle"
            return {"name": name, "status": "idle", "result": result}
        raise KeyError(f"unknown builder tool: {tool}")

    def _definitions(
        self,
        *,
        allow_team: bool,
        planning_mode: str = "auto",
    ) -> list[ToolDefinition]:
        object_schema = {"type": "object", "additionalProperties": True}
        definitions = [
            ToolDefinition(name="catalog_search", description="Search available workflow bricks.", input_schema={"type": "object", "properties": {"query": {"type": "string"}}}),
            ToolDefinition(name="catalog_get", description="Read the exact schema and ports for one brick.", input_schema={"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]}),
            ToolDefinition(name="manual_search", description="Search block manuals before selecting agent architecture bricks.", input_schema={"type": "object", "properties": {"query": {"type": "string"}, "block_kind": {"enum": ["business_workflow", "agent_architecture", "legacy_compatibility"]}}}),
            ToolDefinition(name="manual_get", description="Read one block manual, including when to use it, examples, anti-patterns, and Claude architecture mapping.", input_schema={"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]}),
            ToolDefinition(name="architecture_blueprint", description="Read the Claude-like runtime blueprint made from explicit composable bricks.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="template_suggestions", description="Search template marketplace for matching templates. Use BEFORE building from scratch.", input_schema={"type": "object", "properties": {"requirement": {"type": "string", "description": "Natural language requirement to match against templates"}, "reuse_depth": {"enum": ["none", "shallow", "deep", "adaptive"], "description": "How aggressively to reuse templates."}}, "required": ["requirement"]}),
            ToolDefinition(name="template_list", description="List expandable server-defined and marketplace workflow templates.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="template_expand", description="Expand one server-defined or marketplace workflow template into the draft.", input_schema={"type": "object", "properties": {"name": {"type": "string"}, "prefix": {"type": "string"}, "position": {"type": "object", "additionalProperties": True}}, "required": ["name"]}),
            ToolDefinition(name="capability_contract", description="Inspect, bind, or validate the authoritative F/G/X Capability Build Contract. Bind only real node/module/runtime/platform/external references and use require_bound before completion.", input_schema={"type": "object", "properties": {"action": {"enum": ["get", "bind", "validate"]}, "capability_id": {"type": "string"}, "carrier_type": {"enum": [item.value for item in CarrierType]}, "resource_hint": {"type": "string"}, "rationale": {"type": "string"}, "status": {"enum": [item.value for item in CarrierStatus]}, "implementation_refs": {"type": "array", "items": {"type": "string"}}, "owner": {"enum": [item.value for item in CoverageOwner]}, "coverage_status": {"enum": [item.value for item in CoverageStatus]}, "surface": {"type": "string"}, "notes": {"type": "string"}, "require_bound": {"type": "boolean"}}, "required": ["action"]}),
            ToolDefinition(name="draft_inspect", description="Inspect the current shared draft and revision.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="draft_add_node", description="Add exactly one configured node to the draft.", input_schema={"type": "object", "properties": {"node": NodeSpec.model_json_schema()}, "required": ["node"]}),
            ToolDefinition(name="draft_update_node", description="Patch exactly one node; config patches merge by default.", input_schema={"type": "object", "properties": {"node_id": {"type": "string"}, "changes": object_schema, "merge_config": {"type": "boolean"}}, "required": ["node_id", "changes"]}),
            ToolDefinition(name="draft_remove_node", description="Remove one node and its incident edges.", input_schema={"type": "object", "properties": {"node_id": {"type": "string"}}, "required": ["node_id"]}),
            ToolDefinition(name="draft_connect", description="Connect two existing node ports with one edge.", input_schema={"type": "object", "properties": {"edge": EdgeSpec.model_json_schema()}, "required": ["edge"]}),
            ToolDefinition(name="draft_remove_edge", description="Remove one edge.", input_schema={"type": "object", "properties": {"edge_id": {"type": "string"}}, "required": ["edge_id"]}),
            ToolDefinition(name="draft_upsert_agent", description="Create or update one inline Claude Agent definition.", input_schema={"type": "object", "properties": {"agent": AgentSpec.model_json_schema()}, "required": ["agent"]}),
            ToolDefinition(name="draft_validate", description="Run graph, schema, port, agent-binding, and test-presence validation.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="test_add", description="Add one traceable workflow acceptance test. Include a readable frame plus required_node_types and required_tool_nodes for visible architecture gates.", input_schema={"type": "object", "properties": {"test": WorkflowTestCase.model_json_schema()}, "required": ["test"]}),
            ToolDefinition(name="test_remove", description="Remove an incorrect test, never to hide a real failure.", input_schema={"type": "object", "properties": {"test_id": {"type": "string"}}, "required": ["test_id"]}),
            ToolDefinition(name="test_run", description="Run all mandatory tests against the exact current draft using real providers and tools.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="draft_publish", description="Publish an immutable version; fails unless current hash passed all mandatory tests.", input_schema={"type": "object", "properties": {"explicit": {"type": "boolean"}}}),
            ToolDefinition(name="task", description="Create/list/update shared requirement tasks with owners and dependencies.", input_schema={"type": "object", "properties": {"action": {"enum": ["create", "list", "update"]}, "id": {"type": "integer"}, "subject": {"type": "string"}, "description": {"type": "string"}, "status": {"enum": ["pending", "in_progress", "completed", "blocked"]}, "owner": {"type": "string"}, "blocked_by": {"type": "array", "items": {"type": "integer"}}, "acceptance": {"type": "array", "items": {"type": "string"}}}, "required": ["action"]}),
        ]
        if planning_mode != "disabled":
            definitions.append(
                ToolDefinition(name="build_plan", description="Create, inspect, or update a module-level BuildPlan before building complex BlockFlows.", input_schema={"type": "object", "properties": {"action": {"enum": ["set", "get", "update_module"]}, "plan": BuildPlan.model_json_schema(), "module_id": {"type": "string"}, "changes": object_schema}, "required": ["action"]})
            )
        if allow_team:
            definitions.extend([
                ToolDefinition(name="spawn_teammate", description="Create an isolated persistent teammate for a bounded task.", input_schema={"type": "object", "properties": {"name": {"type": "string"}, "task": {"type": "string"}, "max_turns": {"type": "integer"}}, "required": ["name", "task"]}),
                ToolDefinition(name="send_message", description="Wake an existing teammate with a follow-up message while retaining its context.", input_schema={"type": "object", "properties": {"name": {"type": "string"}, "message": {"type": "string"}, "max_turns": {"type": "integer"}}, "required": ["name", "message"]}),
            ])
        return definitions

    @staticmethod
    def _planning_mode_prompt(planning_mode: str) -> str:
        if planning_mode == "required":
            return (
                "\nPlanning mode is REQUIRED for this build: call build_plan with action=\"set\" "
                "before any draft, template, or test mutation. Keep the plan updated as evidence changes."
            )
        if planning_mode == "disabled":
            return (
                "\nPlanning mode is DISABLED for this build: do not call build_plan. "
                "Build incrementally node by node using draft and test tools only."
            )
        return ""

    @staticmethod
    def _enforce_planning_required(state: BuildTeamState, tool: str) -> None:
        if state.planning_mode == "required" and state.build_plan is None:
            raise RuntimeError(
                f"build_plan required before {tool} when planning_mode=required"
            )

    async def _emit(self, stream_id: str, kind: str, data: dict[str, Any]) -> None:
        await self.storage.append_event(stream_id, kind, data)

    @staticmethod
    def _coerce_max_elapsed_seconds(value: Any) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return seconds if seconds > 0 else None

    @staticmethod
    def _remaining_build_seconds(
        build_started_at: float | None,
        max_elapsed_seconds: float | None,
    ) -> float | None:
        if build_started_at is None or max_elapsed_seconds is None:
            return None
        return max_elapsed_seconds - (time.monotonic() - build_started_at)

    @staticmethod
    def _is_repair_budget_exhausted_message(message: str) -> bool:
        return "maximum repair cycles reached" in message.casefold()

    @classmethod
    def _teammate_guard_reason(
        cls,
        state: BuildTeamState,
        *,
        max_repair_cycles: int,
        build_started_at: float | None,
        max_elapsed_seconds: float | None,
    ) -> str | None:
        if (
            state.last_failed_test_revision == state.revision
            and state.repair_cycles >= max_repair_cycles
        ):
            return (
                "teammate work blocked: repair budget exhausted at the current draft revision; "
                "the coordinator must mutate the draft before delegating more test-driven debugging"
            )
        remaining_seconds = cls._remaining_build_seconds(build_started_at, max_elapsed_seconds)
        if remaining_seconds is not None and remaining_seconds < TEAMMATE_MIN_REMAINING_SECONDS:
            return (
                "teammate work blocked: remaining build deadline "
                f"{remaining_seconds:.3f}s is below the minimum teammate budget "
                f"{TEAMMATE_MIN_REMAINING_SECONDS:g}s"
            )
        return None

    @staticmethod
    def _failure_metadata(error: Exception) -> dict[str, Any]:
        message = str(error)
        timeout_like = "timeout" in message.casefold() or "timed out" in message.casefold()
        if isinstance(error, BuildDeadlineExceeded):
            failure = {
                "type": "build_timeout",
                "error_type": type(error).__name__,
                "retryable": True,
                "status_code": None,
                "timeout_like": True,
                "max_elapsed_seconds": error.max_elapsed_seconds,
                "elapsed_seconds": round(error.elapsed_seconds, 3),
            }
        elif isinstance(error, ProviderError):
            failure = {
                "type": "model_provider",
                "error_type": type(error).__name__,
                "retryable": error.retryable,
                "status_code": error.status_code,
                "timeout_like": timeout_like,
            }
        else:
            failure = {
                "type": "runtime",
                "error_type": type(error).__name__,
                "retryable": False,
                "status_code": None,
                "timeout_like": timeout_like,
            }
        return {"failure": failure}

    @staticmethod
    def _redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: "***" if any(word in key.casefold() for word in ("secret", "token", "password", "api_key")) else WorkflowBuilder._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [WorkflowBuilder._redact(item) for item in value]
        return value

    @staticmethod
    def _remember_manual_lookup(state: BuildTeamState, block_type: str) -> None:
        if block_type not in state.manual_lookups:
            state.manual_lookups.append(block_type)

    def _consume(self, build_id: str, task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()
        self.active.pop(build_id, None)
