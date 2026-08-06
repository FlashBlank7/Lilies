from __future__ import annotations

import asyncio
import json
import re
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
    ApplicationSnapshot,
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
- Carrier mapping is many-to-one. For a cohesive E0/E1 text transformation, prefer one structured Model Turn shared
  by related capabilities instead of a serial LLM call for every capability. Split model calls only when different
  tools, permissions, branches, state boundaries, or independently editable behavior require it.
- Capabilities with the same resource_hint are an explicit instruction to reuse the same node, module, or runtime
  service. Runtime node events and a structured step_log provide traceability; traceability alone never justifies
  one model call per capability or a separate Event Recorder node. Contract validation rejects different
  implementation_refs for a shared resource hint and rejects extra Model Turn nodes when one shared Model Turn
  carries all functional capabilities.
- Runtime node events satisfy an internal traceability guarantee, but they do not replace a customer-visible
  output explicitly requested by the source requirement. If the customer asks to output or return a structured
  step log, expose step_log (or an equivalent trace field) from the terminal node and add a structural assertion
  for that field.
- For a workflow that generates customer-facing replies or recommendations, constrain every model instruction:
  do not invent completed actions or guarantees, and do not suggest hazardous or loss-amplifying DIY remedies;
  direct the customer to a safe official next step when uncertain. A customer-support workflow only provides
  communication and official process guidance: never instruct the customer to repair, disassemble, glue, medicate,
  alter, or otherwise self-remediate a product, body, account, or asset. If use may be unsafe, advise stopping use
  and contacting official support or a qualified professional. Its mandatory acceptance test must include
  at least one scenario-specific not_contains assertion against an unsafe remedy, fabricated completion claim,
  or unsupported guarantee in the reply or next-step field.
- An unavailable external contract is a scoped evidence gap, not a workflow graph defect. Preserve
  blocked_by_environment and the contract claim ceiling; never claim live or production success for it.
- A build with a Capability Build Contract cannot complete until capability_contract action="validate" with
  require_bound=true passes and the BuildPlan covers every required capability.
- For complex or multi-module requirements, call build_plan with action="set" before mutating the draft.
  The build plan should name modules, expected blocks, reuse_depth, complexity, risks, and how each module
  will be tested. Keep the plan updated as modules are built and tested.
- **Before building a workflow from scratch**, call template_suggestions with the requirement text and intended
  reuse_depth. A name, keyword score, usage count, or confidence value is not implementation evidence. When a
  Capability Build Contract exists, only a suggestion with eligible_for_reuse=true may satisfy a reusable-module
  carrier. Preserve its exact module:<id>@<version> reference in the BuildPlan and carrier binding.
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
- Use template_list and template_expand when a known module or legacy subgraph fits. Legacy or draft templates may
  be expanded for editing, but cannot be bound as verified reusable-module evidence. The expanded graph must still
  be validated, tested, and repaired incrementally.
- After template_expand, read the returned validation, node_types, and template_contract. Preserve
  template_contract.min_blocks_required unless you deliberately replace that capability with another visible
  block and then update tests to match the current draft.
- Use spawn_teammate for bounded independent design or verification work. Roles are dynamic, not predefined.
- Add and configure one node or edge per mutation tool call. Never assume an operation succeeded.
- Batch independent inspection, planning, task, node, edge, and test tool calls in one model response when
  their inputs do not depend on one another. The platform persists every result separately.
- Treat the turn and deadline budget shown in each turn as a delivery constraint. Establish a valid runnable
  draft early, reserve the final third for validation, tests, repair, and publication, and do not spend repeated
  turns only inspecting or narrating.
- Keep the shared task ledger truthful: move active work to in_progress and mark verified work completed.
- Once the draft is valid and has a mandatory acceptance test, preserve that deliverable. Add and connect a
  replacement path before removing the old path. Never dismantle a working graph or delete its acceptance tests
  as a debugging experiment.
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
  For structured JSON returned by a Model Turn, prefer ["structured", "<field>"] or
  ["output", "<field>"]. A top-level ["<field>"] alias is also supported.
  Every template variable must reference a field the upstream block explicitly produces. Do not invent
  timestamp, trace, or metadata references that are absent from the upstream output contract.
  NEVER use Python str.format() placeholders like {0} or {1} — they will render literally.
  ALWAYS use {{ name }} syntax where the name matches a key declared in variables.
- draft_update_node merges nested config by default. To remove an obsolete nested key, call it with
  merge_config=false and provide the complete valid replacement config; omitting a key from a merge does not
  delete it.
- If you declare Start inputs, at least one downstream business-critical node must actually use them
  via "$inputs" or the Start node output. Search queries, prompts, HTTP params, and Agent tasks must
  incorporate user-provided inputs instead of ignoring them behind hard-coded text.
- For mutually exclusive branch outputs consumed by Variable Aggregator, set "optional": true inside the
  reference so a skipped branch resolves to null instead of failing.
- A valid graph has exactly one start, at least one end/answer, no implicit cycles, and no unreachable nodes.
- Add mandatory tests that demonstrate the user's actual acceptance criteria. Run them with test_run.
- Close and validate every required Capability Build Contract carrier before test_run. Acceptance is the final
  executable proof, not a checkpoint followed by contract mutations.
- Each test should include a readable frame with category, purpose, reviewer_guidance, reference, and failure_target.
  The frame should explain where the test sits in the acceptance framework, for example outline adherence,
  tool evidence, safety, or human review.
- Tests for generated workflows must set required_node_types for the visible architecture and required_tool_nodes
  when a concrete Tool brick is required, e.g. WebSearch. This prevents a single opaque Agent node from passing.
- When a requirement depends on external tools, tests must set required_tools, minimum_tool_calls, and
  require_cited_tool_urls so a model cannot pass by inventing plausible output without tool evidence.
- test_add is an atomic add-or-replace operation: calling it with an existing test id replaces that test in one
  revision. Repair a failed test with the same id; do not delete it first or temporarily reduce acceptance coverage.
- If a test fails, inspect its frame.failure_target and runtime events, then repair the implementation before
  changing the test. Once acceptance is first executed, its assertion semantics are frozen: do not remove,
  replace, or weaken assertions merely to make the build green.
- Once test_run passes all mandatory tests, the delivery is frozen. Do not inspect more catalogs, change nodes,
  edges, agents, templates, or tests, or delegate more work. Finish task and plan bookkeeping, close the Capability
  Build Contract before testing; after a passing test the platform auto-publishes when auto_publish is enabled.
- Treat draft_validate warnings about disconnected inputs as issues to repair before publishing.
- Publish only after draft_validate and all mandatory tests pass for the exact current content hash.
- Do not claim completion before draft_publish returns a version (unless auto-publish is disabled).
"""


TEAMMATE_MIN_REMAINING_SECONDS = 90.0
TEAMMATE_REPAIR_BUDGET_EXHAUSTED_REASON = "repair_budget_exhausted"
BUILDER_MAX_STALLED_PROGRESS_TURNS = 6
BUILDER_MAX_DISCOVERY_ONLY_TURNS = 10
BUILDER_TEAMMATE_MAX_TURNS = 8

_CUSTOMER_TRACE_OUTPUT_RE = re.compile(
    r"(?:输出|返回|展示|包含).{0,24}(?:结构化)?(?:步骤|执行|处理).{0,8}(?:日志|记录|轨迹|证据)"
    r"|\b(?:output|return|show|include|expose)\b.{0,40}"
    r"\b(?:structured\s+)?(?:step|execution|process)\s*(?:log|trace|evidence)\b",
    re.I,
)
_CUSTOMER_ADVICE_GUARD_RE = re.compile(
    r"(?:不得|不要|禁止|避免).{0,100}"
    r"(?:危险|伤害|扩大损失|自行(?:维修|修复|处置)|未经验证|虚构|承诺)"
    r"|\b(?:do not|never|avoid)\b.{0,120}"
    r"\b(?:hazardous|harmful|unsafe|diy|unverified|invent|fabricat|guarantee|promise)",
    re.I,
)
_CUSTOMER_SELF_REPAIR_GUARD_RE = re.compile(
    r"(?:不得|不要|禁止|绝不).{0,100}"
    r"(?:自行(?:维修|修复|拆解|拆机|粘合|处置)|使用(?:胶水|药物)|产品维修)"
    r"|\b(?:do not|never|must not)\b.{0,140}"
    r"\b(?:self[- ]?repair|disassembl|glue|adhesive|medicat|self[- ]?remed)",
    re.I,
)
_OUTPUT_SEMANTIC_GROUPS = {
    "urgency": ("urgency", "urgent", "emergency", "priority", "severity"),
    "issue_type": (
        "issue_type",
        "issue_category",
        "issue_kind",
        "problem_type",
        "problem_category",
        "complaint_type",
        "category",
    ),
    "reply": ("reply", "response"),
    "reason": ("reason", "reasoning", "rationale", "justification"),
    "next_step": ("next_step", "next_action", "follow_up", "recommended_action"),
    "trace": (
        "step_log",
        "steps",
        "trace",
        "trace_log",
        "process_log",
        "execution_log",
        "structured_step",
    ),
}


def _normalized_output_key(value: str) -> str:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "_", normalized.casefold()).strip("_")


def _output_assertion_key(value: str) -> str:
    normalized = _normalized_output_key(value)
    for group, aliases in _OUTPUT_SEMANTIC_GROUPS.items():
        if any(alias in normalized for alias in aliases):
            return group
    return normalized
BUILDER_TEAMMATE_FOLLOWUP_MAX_TURNS = 6

BUILDER_DISCOVERY_TOOLS = {
    "architecture_blueprint",
    "capability_contract",
    "catalog_get",
    "catalog_search",
    "draft_inspect",
    "manual_get",
    "manual_search",
    "template_list",
    "template_suggestions",
}
BUILDER_VERIFICATION_TOOLS = {"draft_validate", "test_run"}


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
        build_started_at = time.time()
        max_elapsed_seconds = self._coerce_max_elapsed_seconds(build.get("max_elapsed_seconds"))
        task_metadata: dict[str, Any] = {
            "max_turns": build["max_turns"],
            "max_repair_cycles": build["max_repair_cycles"],
            "auto_publish": build["auto_publish"],
            "application_id": build["application_id"],
            "workflow_id": build["application_id"],
            "model": self.generator_model,
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
            agent_loop = self._agent_loop(
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
            if max_elapsed_seconds is not None:
                try:
                    await self._await_with_wall_clock_deadline(
                        agent_loop,
                        max_elapsed_seconds=max_elapsed_seconds,
                        build_started_at=build_started_at,
                    )
                except BuildDeadlineExceeded as error:
                    await self._emit(build_id, "build.deadline.exceeded", {
                        "max_elapsed_seconds": max_elapsed_seconds,
                        "elapsed_seconds": round(error.elapsed_seconds, 3),
                    })
                    raise
            else:
                await agent_loop
            self._trackers[build_id] = tracker
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
            completed_progress = self._complete_verified_progress(state)
            if completed_progress:
                await self.workflow_store.update_build(build_id, team_state=state)
                await self._emit(build_id, "build.progress.completed", completed_progress)
            if manage_harness_task:
                await self.harness.finish_task(build_id, status="succeeded")
            await self._emit(build_id, "build.completed", {
                "status": status, "published_version": state.published_version
            })
            # A terminal build status is the public commit marker. Publish it only
            # after the task record and terminal event are durable.
            await self.workflow_store.update_build(build_id, status=status, team_state=state)
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
            if manage_harness_task:
                await self.harness.finish_task(build_id, status="cancelled")
            await self._emit(build_id, "build.cancelled", {})
            await self.workflow_store.update_build(build_id, status="cancelled", team_state=state)
            raise
        except Exception as error:
            failure_metadata = self._failure_metadata(error)
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
            await self.workflow_store.update_build(
                build_id, status="needs_attention", team_state=state, error=str(error)
            )
            if not manage_harness_task:
                raise

    def _capability_resource_inventory(self, snapshot: Any) -> dict[str, Any]:
        verified_modules = (
            self.template_store.verified_module_refs()
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
            "marketplace_templates": verified_modules,
            "verified_modules": verified_modules,
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
            | set(inventory["verified_modules"]),
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

    @staticmethod
    def _shared_carrier_binding_errors(
        snapshot: ApplicationSnapshot,
        contract: Any,
    ) -> list[str]:
        required_ids = {
            item.id for item in contract.capabilities if item.required
        }
        functional_ids = {
            item.id
            for item in contract.functional_capabilities
            if item.required
        }
        groups: dict[str, list[CapabilityCarrierDecision]] = {}
        for decision in contract.carrier_decisions:
            if (
                decision.capability_id in required_ids
                and decision.resource_hint.startswith("shared:")
            ):
                groups.setdefault(decision.resource_hint, []).append(decision)

        node_types = {
            node.id: node.type for node in snapshot.workflow.nodes
        }
        model_turn_ids = {
            node.id
            for node in snapshot.workflow.nodes
            if node.type == "model_turn"
        }
        shared_model_function_ids: set[str] = set()
        shared_model_refs: set[str] = set()
        errors: list[str] = []
        for resource_hint, decisions in groups.items():
            reference_sets = {
                tuple(sorted(decision.implementation_refs))
                for decision in decisions
            }
            if len(reference_sets) != 1:
                bindings = {
                    decision.capability_id: decision.implementation_refs
                    for decision in decisions
                }
                errors.append(
                    f"shared carrier {resource_hint} must reuse identical "
                    f"implementation_refs: {bindings}"
                )
                continue
            references = set(next(iter(reference_sets), ()))
            if resource_hint.startswith("shared:model_turn:"):
                shared_model_function_ids.update(
                    decision.capability_id
                    for decision in decisions
                    if decision.capability_id in functional_ids
                )
                shared_model_refs.update(references)
                if len(references) != 1:
                    errors.append(
                        f"shared Model Turn carrier {resource_hint} must bind exactly one "
                        f"workflow node, got {sorted(references)}"
                    )
                elif node_types.get(next(iter(references))) != "model_turn":
                    errors.append(
                        f"shared Model Turn carrier {resource_hint} must bind a model_turn node"
                    )

        if functional_ids and shared_model_function_ids == functional_ids:
            extra_model_turns = sorted(model_turn_ids - shared_model_refs)
            if extra_model_turns:
                errors.append(
                    "all functional capabilities use shared Model Turn carriers, but the "
                    f"workflow contains extra Model Turn nodes: {extra_model_turns}"
                )
        return errors


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
        draft = await self.workflow_store.get_draft(application_id)
        delivery_warnings = self._draft_delivery_errors(draft["snapshot"])
        errors = list(dict.fromkeys(validation["errors"]))
        return {
            "valid": not errors,
            "errors": errors,
            "warnings": list(dict.fromkeys([*validation["warnings"], *delivery_warnings])),
            "revision": validation["revision"],
            "test_count": validation["test_count"],
        }

    def _template_contract(
        self,
        template_name: str,
        source: str,
        version: int | None = None,
    ) -> dict[str, Any] | None:
        if source == "server_defined" and template_name == "codex_like_workspace_agent":
            return {
                "name": template_name,
                "title": "Codex-like Workspace Agent",
                "category": "workspace_agent",
                "expected_inputs": ["task", "workspace_path", "network_policy", "cancel_requested"],
                "expected_outputs": ["answer"],
                "min_blocks_required": 13,
                "evidence_level": "design_only",
                "module_status": "legacy_unverified",
                "claim_scope": (
                    "editable server template only; use the exact verified capability module "
                    "for implementation evidence"
                ),
            }
        if source != "marketplace" or not self.template_store:
            return None
        try:
            record = self.template_store.get_record(template_name, version)
        except KeyError:
            return None
        template = record.template
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
            "module_ref": record.module_ref,
            "module_status": record.state.status,
            "content_hash": record.state.content_hash,
            "capability_contract": (
                template.module_contract.model_dump(mode="json")
                if template.module_contract
                else None
            ),
            "evidence_record_ids": record.state.evidence_record_ids,
            "verification_errors": record.state.verification_errors,
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
        progress_fingerprint = self._durable_progress_fingerprint(state)
        stalled_progress_turns = 0
        discovery_only_turns = 0
        seen_progress_evidence: set[str] = set()
        for turn in range(1, max_turns + 1):
            teammate_stop_reason: str | None = None
            await self.harness.record_usage(
                build_id,
                "model_call",
                metadata={"actor": teammate or "coordinator", "turn": turn, "model": self.generator_model},
            )
            turn_budget_prompt = self._turn_budget_prompt(
                turn,
                max_turns,
                state,
                stalled_progress_turns=stalled_progress_turns,
                discovery_only_turns=discovery_only_turns,
                remaining_seconds=self._remaining_build_seconds(
                    build_started_at,
                    max_elapsed_seconds,
                ),
            )
            stream = self.provider.stream(
                model=self.generator_model,
                system=BUILDER_SYSTEM_PROMPT + self._planning_mode_prompt(state.planning_mode) + turn_budget_prompt + (
                    f"\nYou are teammate {teammate}. Complete your assigned bounded task and report evidence."
                    if teammate else "\nYou are the coordinator. Delegate when useful and synthesize results."
                ),
                messages=messages,
                tools=tools,
                max_output_tokens=8_192,
                thinking_enabled=True,
                effort="high",
                tool_choice={"type": "auto"},
                user_id=f"{build_id}-{teammate or 'coordinator'}",
            )
            response = await self.agent_runtime._collect_stream(
                build_id,
                stream,
                f"build.{teammate or 'coordinator'}.model",
                self.generator_model,
            )
            await self.harness.record_model_usage(
                build_id,
                response.usage,
                model=self.generator_model,
                provider=self.provider.provider_name_for(self.generator_model),
                metadata={
                    "application_id": application_id,
                    "workflow_id": application_id,
                    "actor": teammate or "coordinator",
                    "turn": turn,
                    "phase": "builder_team",
                },
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
            turn_discovery_progress = False
            turn_verification_progress = False
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
                    # Persist user-visible progress before emitting the operation event so
                    # a client reacting to that event can immediately read the new state.
                    await self.workflow_store.update_build(build_id, team_state=state)
                    content = json.dumps(value, ensure_ascii=False, default=str)
                    is_error = False
                    progress_kind = self._builder_evidence_progress_kind(
                        call.name or "",
                        call.input or {},
                        value,
                        seen_progress_evidence,
                    )
                    turn_discovery_progress = (
                        turn_discovery_progress or progress_kind == "discovery"
                    )
                    turn_verification_progress = (
                        turn_verification_progress or progress_kind == "verification"
                    )
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
                    "progress": self._team_progress(state),
                })
            messages.append(ChatMessage(role="user", content=results))
            if teammate is None:
                state.coordinator_messages = [
                    message.model_dump(mode="json") for message in messages
                ]
            await self.workflow_store.update_build(build_id, team_state=state)
            next_fingerprint = self._durable_progress_fingerprint(state)
            durable_progress = next_fingerprint != progress_fingerprint
            if durable_progress:
                progress_fingerprint = next_fingerprint
            if durable_progress or turn_verification_progress:
                stalled_progress_turns = 0
                discovery_only_turns = 0
            elif turn_discovery_progress:
                stalled_progress_turns = 0
                discovery_only_turns += 1
            else:
                stalled_progress_turns += 1
                discovery_only_turns += 1
            if stalled_progress_turns >= BUILDER_MAX_STALLED_PROGRESS_TURNS:
                await self._emit(build_id, "build.progress.stalled", {
                    "actor": teammate or "coordinator",
                    "turn": turn,
                    "stalled_turns": stalled_progress_turns,
                    "draft_revision": state.revision,
                })
                raise RuntimeError(
                    "builder progress stalled: no durable draft, plan, task, or repair progress for "
                    f"{stalled_progress_turns} consecutive turns"
                )
            if discovery_only_turns >= BUILDER_MAX_DISCOVERY_ONLY_TURNS:
                await self._emit(build_id, "build.progress.exploration_exhausted", {
                    "actor": teammate or "coordinator",
                    "turn": turn,
                    "discovery_only_turns": discovery_only_turns,
                    "draft_revision": state.revision,
                })
                raise RuntimeError(
                    "builder exploration budget exhausted: no task, plan, draft, test, or verification "
                    f"delivery progress for {discovery_only_turns} consecutive turns"
                )
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
                turn_completed["elapsed_seconds"] = round(time.time() - build_started_at, 3)
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
            templates = (
                [
                    record.template.meta
                    for record in self.template_store.list_records(all_versions=True)
                ]
                if self.template_store
                else []
            )
            scored = score_template_matches(requirement, templates)
            # Bump usage_count for top matches (feedback: Builder selected this template)
            for _, meta in scored[:3]:
                if hasattr(meta, "usage_count"):
                    meta.usage_count += 1  # recommendation flywheel: template was chosen
            top_meta = scored[0][1] if scored else None
            effective_reuse_depth, policy_reason = resolve_effective_reuse_depth(reuse_depth, top_meta)
            suggestion_payloads: list[dict[str, Any]] = []
            for score, meta in scored[:5]:
                payload = {
                    **build_suggestion_payload(
                        meta,
                        score,
                        reuse_depth,
                        default_metadata=default_metadata,
                    ),
                    "source": "marketplace",
                    "relevance": round(score, 3),
                }
                if self.template_store:
                    record = self.template_store.get_record(meta.name, meta.version)
                    compatibility = self.template_store.compatibility(
                        record,
                        state.capability_build_contract,
                    )
                    payload.update({
                        "module_ref": record.module_ref,
                        "module_status": record.state.status,
                        "content_hash": record.state.content_hash,
                        "compatibility": compatibility.model_dump(mode="json"),
                        "eligible_for_reuse": compatibility.eligible_for_reuse,
                        "evidence_record_ids": record.state.evidence_record_ids,
                    })
                    if (
                        state.capability_build_contract is not None
                        and not compatibility.eligible_for_reuse
                    ):
                        payload["recommended_action"] = "inspect_only"
                suggestion_payloads.append(payload)
            if state.capability_build_contract is not None:
                suggestion_payloads.sort(
                    key=lambda item: (
                        bool(item.get("eligible_for_reuse")),
                        float(item.get("relevance", 0.0)),
                    ),
                    reverse=True,
                )
            verified_match_available = any(
                bool(item.get("eligible_for_reuse"))
                for item in suggestion_payloads
            )
            result = {
                "reuse_depth": reuse_depth,
                "effective_reuse_depth": effective_reuse_depth,
                "recommended_action": (
                    recommended_action_for_depth(effective_reuse_depth)
                    if state.capability_build_contract is None or verified_match_available
                    else "build_from_scratch"
                ),
                "policy_reason": policy_reason,
                **default_metadata,
                "templates": suggestion_payloads,
            }
            if default_metadata.get("defaulted_by_policy"):
                result["execution_contract"] = policy_default_execution_contract(
                    effective_reuse_depth,
                    reuse_depth_source=str(default_metadata.get("reuse_depth_source") or "policy_default"),
                )
                if state.capability_build_contract is not None and not verified_match_available:
                    result["execution_contract"]["then"] = "build_from_scratch"
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
                for record in self.template_store.list_records(all_versions=True):
                    meta = record.template.meta
                    templates.append({
                        "name": meta.name,
                        "title": meta.title,
                        "source": "marketplace",
                        "description": meta.description,
                        "category": meta.category,
                        "tags": meta.tags,
                        "confidence": meta.confidence,
                        "recommended_action": "expand_template",
                        "version": record.state.version,
                        "module_ref": record.module_ref,
                        "module_status": record.state.status,
                        "verified_capability_carrier": record.state.status == "verified",
                        "capability_ids": (
                            record.template.module_contract.capability_ids
                            if record.template.module_contract
                            else []
                        ),
                        "known_boundaries": (
                            [
                                item.model_dump(mode="json")
                                for item in record.template.module_contract.known_boundaries
                            ]
                            if record.template.module_contract
                            else []
                        ),
                    })
            return templates
        if tool == "template_expand":
            self._enforce_planning_required(state, tool)
            template_name = str(data["name"])
            requested_version = (
                int(data["version"])
                if data.get("version") is not None
                else None
            )
            prefix = str(data.get("prefix") or template_name)
            position = data.get("position") if isinstance(data.get("position"), dict) else {}
            x = float(position.get("x", 0))
            y = float(position.get("y", 0))
            marketplace_names = set(self.template_store.names()) if self.template_store else set()
            if self.template_store and template_name in marketplace_names:
                source = "marketplace"
                workflow = self.template_store.expand_into_workflow(
                    template_name,
                    version=requested_version,
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
                "version": (
                    self.template_store.get_record(
                        template_name,
                        requested_version,
                    ).state.version
                    if source == "marketplace" and self.template_store
                    else None
                ),
                "module_ref": (
                    self.template_store.get_record(
                        template_name,
                        requested_version,
                    ).module_ref
                    if source == "marketplace" and self.template_store
                    else None
                ),
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
                "template_contract": self._template_contract(
                    template_name,
                    source,
                    requested_version,
                ),
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
                resource_hint = str(
                    data.get("resource_hint")
                    or (existing.resource_hint if existing else "")
                )
                if (
                    existing is not None
                    and existing.resource_hint.startswith("shared:")
                    and resource_hint != existing.resource_hint
                ):
                    raise ValueError(
                        "authoritative shared resource_hint cannot be changed during binding: "
                        f"{existing.resource_hint}"
                    )
                implementation_refs = [
                    str(item)
                    for item in data.get(
                        "implementation_refs",
                        existing.implementation_refs if existing else [],
                    )
                    if str(item).strip()
                ]
                node_refs = {
                    item.id for item in draft["snapshot"].workflow.nodes
                }
                implementation_refs = list(dict.fromkeys(
                    reference.removeprefix("node:")
                    if reference.startswith("node:")
                    and reference.removeprefix("node:") in node_refs
                    else reference
                    for reference in implementation_refs
                ))
                if status in {CarrierStatus.bound, CarrierStatus.blocked_by_environment} and not implementation_refs:
                    raise ValueError("bound carrier decisions require implementation_refs")
                decision = CapabilityCarrierDecision(
                    capability_id=capability_id,
                    carrier_type=carrier_type,
                    resource_hint=resource_hint,
                    rationale=str(
                        data.get("rationale")
                        or (existing.rationale if existing else "")
                    ),
                    status=status,
                    implementation_refs=implementation_refs,
                )
                if decision.resource_hint.startswith("shared:"):
                    peer_bindings = {
                        item.capability_id: item.implementation_refs
                        for item in contract.carrier_decisions
                        if item.capability_id != capability_id
                        and item.resource_hint == decision.resource_hint
                        and item.status == CarrierStatus.bound
                        and item.implementation_refs
                    }
                    mismatched_peers = {
                        peer_id: references
                        for peer_id, references in peer_bindings.items()
                        if set(references) != set(decision.implementation_refs)
                    }
                    if mismatched_peers:
                        raise ValueError(
                            f"shared carrier {decision.resource_hint} must reuse "
                            "implementation_refs already bound by peer capabilities: "
                            f"{mismatched_peers}"
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
            operation = DraftOperation(
                expected_revision=int(draft["revision"]),
                idempotency_key=f"{build_id}:{tool}:{uuid4()}",
                op=op,
                data=payload,
            )
            result = await self.applications.apply_operation(
                application_id,
                operation,
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
            return await self._draft_validation_summary(application_id)
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
                if auto_publish:
                    published = await self.workflow_store.publish(application_id)
                    state.published_version = published["version"]
                    report["publication"] = published
                    await self._emit(build_id, "build.published", published)
            return report
        if tool == "draft_publish":
            if state.published_version is not None:
                return {
                    "application_id": application_id,
                    "version": state.published_version,
                    "status": "already_published",
                }
            if not auto_publish and not data.get("explicit", False):
                return {"status": "ready", "message": "auto publish is disabled"}
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
                    for module in plan.modules:
                        if module.carrier_type != CarrierType.reusable_module:
                            continue
                        if not module.reusable_module_ref:
                            # The plan may describe a module that will be built from nodes.
                            # Exact references are mandatory only when selecting registry reuse.
                            continue
                        if not self.template_store:
                            raise RuntimeError("reusable module registry is unavailable")
                        try:
                            reusable = self.template_store.get_record_by_ref(
                                module.reusable_module_ref
                            )
                        except KeyError as error:
                            raise RuntimeError(str(error)) from error
                        if reusable.state.status != "verified":
                            raise RuntimeError(
                                f"BuildPlan reusable module is not verified: "
                                f"{module.reusable_module_ref}"
                            )
                        declared = set(
                            reusable.template.module_contract.capability_ids
                            if reusable.template.module_contract
                            else []
                        )
                        unsupported = sorted(set(module.capability_ids) - declared)
                        if unsupported:
                            raise RuntimeError(
                                f"BuildPlan module {module.id} assigns capabilities not "
                                f"declared by {module.reusable_module_ref}: {unsupported}"
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
                max_turns=min(
                    int(data.get("max_turns", BUILDER_TEAMMATE_MAX_TURNS)),
                    BUILDER_TEAMMATE_MAX_TURNS,
                ),
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
                max_turns=min(
                    int(data.get("max_turns", BUILDER_TEAMMATE_FOLLOWUP_MAX_TURNS)),
                    BUILDER_TEAMMATE_FOLLOWUP_MAX_TURNS,
                ),
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
            ToolDefinition(name="template_suggestions", description="Search reusable modules and legacy templates. With a Capability Build Contract, only eligible_for_reuse=true is verified carrier evidence.", input_schema={"type": "object", "properties": {"requirement": {"type": "string", "description": "Natural language requirement to match against templates"}, "reuse_depth": {"enum": ["none", "shallow", "deep", "adaptive"], "description": "How aggressively to reuse templates."}}, "required": ["requirement"]}),
            ToolDefinition(name="template_list", description="List exact module versions, verification state, capability coverage, and legacy templates.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="template_expand", description="Expand one exact reusable-module version or legacy template into the editable draft.", input_schema={"type": "object", "properties": {"name": {"type": "string"}, "version": {"type": "integer", "minimum": 1}, "prefix": {"type": "string"}, "position": {"type": "object", "additionalProperties": True}}, "required": ["name"]}),
            ToolDefinition(name="capability_contract", description="Inspect, bind, or validate the authoritative F/G/X Capability Build Contract. Bind only real node/module/runtime/platform/external references and use require_bound before completion.", input_schema={"type": "object", "properties": {"action": {"enum": ["get", "bind", "validate"]}, "capability_id": {"type": "string"}, "carrier_type": {"enum": [item.value for item in CarrierType]}, "resource_hint": {"type": "string"}, "rationale": {"type": "string"}, "status": {"enum": [item.value for item in CarrierStatus]}, "implementation_refs": {"type": "array", "items": {"type": "string"}}, "owner": {"enum": [item.value for item in CoverageOwner]}, "coverage_status": {"enum": [item.value for item in CoverageStatus]}, "surface": {"type": "string"}, "notes": {"type": "string"}, "require_bound": {"type": "boolean"}}, "required": ["action"]}),
            ToolDefinition(name="draft_inspect", description="Inspect the current shared draft and revision.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="draft_add_node", description="Add exactly one configured node to the draft.", input_schema={"type": "object", "properties": {"node": NodeSpec.model_json_schema()}, "required": ["node"]}),
            ToolDefinition(name="draft_update_node", description="Patch exactly one node; config patches merge by default.", input_schema={"type": "object", "properties": {"node_id": {"type": "string"}, "changes": object_schema, "merge_config": {"type": "boolean"}}, "required": ["node_id", "changes"]}),
            ToolDefinition(name="draft_remove_node", description="Remove one node and its incident edges.", input_schema={"type": "object", "properties": {"node_id": {"type": "string"}}, "required": ["node_id"]}),
            ToolDefinition(name="draft_connect", description="Connect two existing node ports with one edge.", input_schema={"type": "object", "properties": {"edge": EdgeSpec.model_json_schema()}, "required": ["edge"]}),
            ToolDefinition(name="draft_remove_edge", description="Remove one edge.", input_schema={"type": "object", "properties": {"edge_id": {"type": "string"}}, "required": ["edge_id"]}),
            ToolDefinition(name="draft_upsert_agent", description="Create or update one inline Claude Agent definition.", input_schema={"type": "object", "properties": {"agent": AgentSpec.model_json_schema()}, "required": ["agent"]}),
            ToolDefinition(name="draft_validate", description="Run graph, schema, port, agent-binding, and test-presence validation.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="test_add", description="Atomically add or replace one traceable workflow acceptance test. Reuse the same test id when repairing it; never delete first. Include a readable frame plus required_node_types and required_tool_nodes for visible architecture gates.", input_schema={"type": "object", "properties": {"test": WorkflowTestCase.model_json_schema()}, "required": ["test"]}),
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

    @staticmethod
    def _turn_budget_prompt(
        turn: int,
        max_turns: int,
        state: BuildTeamState,
        *,
        stalled_progress_turns: int,
        discovery_only_turns: int,
        remaining_seconds: float | None,
    ) -> str:
        remaining = max_turns - turn + 1
        final_third = (
            turn > (max_turns * 2) // 3
            or (remaining_seconds is not None and remaining_seconds < 180)
        )
        phase = "verification and delivery" if final_third else "construction"
        statuses = WorkflowBuilder._team_progress(state)["task_statuses"]
        time_budget = (
            f"; approximately {max(0, remaining_seconds):.0f}s remain"
            if remaining_seconds is not None
            else ""
        )
        delivery_directive = ""
        return (
            f"\n\nCurrent delivery budget: turn {turn}/{max_turns}; {remaining} turns remain"
            f"{time_budget}; "
            f"phase={phase}; draft_revision={state.revision}; repair_cycles={state.repair_cycles}; "
            f"task_statuses={json.dumps(statuses, sort_keys=True)}; "
            f"consecutive_turns_without_any_new_progress={stalled_progress_turns}; "
            f"consecutive_discovery_only_turns={discovery_only_turns}. "
            "Use tools now. Batch independent calls. Make durable draft, plan, task, or test progress on "
            "this turn. In the delivery phase, stop broad exploration and prioritize a valid draft, "
            f"mandatory tests, task status updates, and publication.{delivery_directive}"
        )

    @staticmethod
    def _builder_evidence_progress_kind(
        tool: str,
        tool_input: dict[str, Any],
        value: Any,
        seen: set[str],
    ) -> str | None:
        kind: str | None = None
        if tool in BUILDER_VERIFICATION_TOOLS:
            kind = "verification"
        elif tool in BUILDER_DISCOVERY_TOOLS:
            if tool in {"catalog_search", "manual_search"} and not value:
                return None
            kind = "discovery"
        if kind is None:
            return None
        signature = json.dumps(
            {"tool": tool, "input": tool_input, "value": value},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if signature in seen:
            return None
        seen.add(signature)
        return kind

    @staticmethod
    def _durable_progress_fingerprint(state: BuildTeamState) -> str:
        payload = {
            "revision": state.revision,
            "repair_cycles": state.repair_cycles,
            "published_version": state.published_version,
            "tasks": [task.model_dump(mode="json") for task in state.tasks],
            "build_plan": (
                state.build_plan.model_dump(mode="json")
                if state.build_plan is not None
                else None
            ),
            "manual_lookups": sorted(state.manual_lookups),
            "capability_closure": state.capability_closure,
            "teammates": {
                name: teammate.status
                for name, teammate in sorted(state.teammates.items())
            },
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    def _draft_delivery_errors(self, snapshot: ApplicationSnapshot) -> list[str]:
        errors = list(self.blocks.validate_workflow(snapshot.workflow))
        mandatory_tests = [test for test in snapshot.tests if test.mandatory]
        if not mandatory_tests:
            errors.append("at least one mandatory acceptance test is required")
            return errors
        node_types = {node.type for node in snapshot.workflow.nodes}
        tool_node_names = {
            str(node.config.get("tool_name"))
            for node in snapshot.workflow.nodes
            if node.type == "tool" and node.config.get("tool_name")
        }
        tool_node_names.update(
            str(node.config.get("settings", {}).get("tool_name"))
            for node in snapshot.workflow.nodes
            if node.type == "tool_executor"
            and node.config.get("settings", {}).get("tool_name")
        )
        for test in mandatory_tests:
            missing_types = sorted(set(test.required_node_types) - node_types)
            if missing_types:
                errors.append(f"test {test.id} missing required node types: {missing_types}")
            missing_tools = sorted(set(test.required_tool_nodes) - tool_node_names)
            if missing_tools:
                errors.append(f"test {test.id} missing required tool nodes: {missing_tools}")
        contract = snapshot.capability_build_contract
        if contract is None:
            return errors

        required_capabilities = {
            capability.id
            for capability in contract.capabilities
            if capability.required
        }
        tested_capabilities = {
            capability_id
            for test in mandatory_tests
            for capability_id in test.capability_ids
        }
        missing_capability_tests = sorted(
            required_capabilities - tested_capabilities
        )
        if missing_capability_tests:
            errors.append(
                "mandatory acceptance tests do not cover required contract capabilities: "
                f"{missing_capability_tests}"
            )

        assertions_by_capability: dict[str, set[str]] = {}
        for test in mandatory_tests:
            asserted_keys = {
                _output_assertion_key(path_part)
                for assertion in test.assertions
                for path_part in assertion.path
                if _normalized_output_key(path_part)
            }
            for capability_id in test.capability_ids:
                assertions_by_capability.setdefault(capability_id, set()).update(
                    asserted_keys
                )

        for capability in contract.functional_capabilities:
            if not capability.required:
                continue
            required_output_keys = {
                _output_assertion_key(output)
                for output in capability.outputs
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", output)
            }
            missing_output_keys = sorted(
                required_output_keys
                - assertions_by_capability.get(capability.id, set())
            )
            if missing_output_keys:
                errors.append(
                    f"mandatory acceptance for {capability.id} lacks output assertions: "
                    f"{missing_output_keys}"
                )

        customer_reply_capabilities = {
            capability.id
            for capability in contract.functional_capabilities
            if capability.required
            and any(
                _output_assertion_key(output) == "reply"
                for output in capability.outputs
            )
        }
        if customer_reply_capabilities:
            model_nodes = [
                node
                for node in snapshot.workflow.nodes
                if node.type in {"model_turn", "llm"}
            ]
            if model_nodes and any(
                (
                    not _CUSTOMER_ADVICE_GUARD_RE.search(
                        json.dumps(node.config, ensure_ascii=False)
                    )
                    or not _CUSTOMER_SELF_REPAIR_GUARD_RE.search(
                        json.dumps(node.config, ensure_ascii=False)
                    )
                )
                for node in model_nodes
            ):
                errors.append(
                    "customer-facing reply model instructions must forbid unsafe "
                    "DIY remedies including self-repair, disassembly, or glue; "
                    "they must also forbid fabricated actions and unsupported guarantees"
                )
            negative_reply_coverage = {
                capability_id
                for test in mandatory_tests
                for capability_id in test.capability_ids
                if capability_id in customer_reply_capabilities
                and any(
                    assertion.operator == "not_contains"
                    and any(
                        _output_assertion_key(path_part)
                        in {"reply", "next_step"}
                        for path_part in assertion.path
                    )
                    and isinstance(assertion.expected, str)
                    and bool(assertion.expected.strip())
                    for assertion in test.assertions
                )
            }
            missing_negative_coverage = sorted(
                customer_reply_capabilities - negative_reply_coverage
            )
            if missing_negative_coverage:
                errors.append(
                    "customer-facing reply capabilities require scenario-specific "
                    "not_contains safety assertions: "
                    f"{missing_negative_coverage}"
                )

        if _CUSTOMER_TRACE_OUTPUT_RE.search(contract.source_requirement):
            for guarantee in contract.runtime_guarantees:
                guarantee_text = " ".join([
                    guarantee.id,
                    guarantee.title,
                    guarantee.description,
                    *guarantee.acceptance,
                ])
                if (
                    guarantee.required
                    and guarantee.guarantee_type in {"audit", "observability"}
                    and _output_assertion_key(guarantee_text) == "trace"
                    and "trace" not in assertions_by_capability.get(
                        guarantee.id,
                        set(),
                    )
                ):
                    errors.append(
                        f"mandatory acceptance for {guarantee.id} must assert a "
                        "customer-visible structured step-log output"
                    )
        return errors







    @staticmethod
    def _complete_verified_progress(state: BuildTeamState) -> dict[str, Any]:
        completed_task_ids: list[int] = []
        for task in state.tasks:
            if task.status in {"pending", "in_progress"}:
                task.status = "completed"
                completed_task_ids.append(task.id)
        completed_module_ids: list[str] = []
        if state.build_plan is not None:
            for module in state.build_plan.modules:
                if module.status != "blocked" and module.status != "done":
                    module.status = "done"
                    completed_module_ids.append(module.id)
        if not completed_task_ids and not completed_module_ids:
            return {}
        return {
            "task_ids": completed_task_ids,
            "module_ids": completed_module_ids,
            "basis": "draft validation and mandatory acceptance suite passed",
        }

    async def _emit(self, stream_id: str, kind: str, data: dict[str, Any]) -> None:
        await self.storage.append_event(stream_id, kind, data)

    @staticmethod
    def _team_progress(state: BuildTeamState) -> dict[str, Any]:
        task_statuses: dict[str, int] = {}
        for task in state.tasks:
            task_statuses[task.status] = task_statuses.get(task.status, 0) + 1
        teammate_statuses: dict[str, int] = {}
        for teammate in state.teammates.values():
            teammate_statuses[teammate.status] = teammate_statuses.get(teammate.status, 0) + 1
        return {
            "task_count": len(state.tasks),
            "task_statuses": task_statuses,
            "teammate_count": len(state.teammates),
            "teammate_statuses": teammate_statuses,
            "repair_cycles": state.repair_cycles,
            "draft_revision": state.revision,
            "published_version": state.published_version,
        }

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
    async def _await_with_wall_clock_deadline(
        operation: Awaitable[Any],
        *,
        max_elapsed_seconds: float,
        build_started_at: float,
    ) -> Any:
        task = asyncio.ensure_future(operation)
        try:
            while not task.done():
                elapsed_seconds = time.time() - build_started_at
                remaining_seconds = max_elapsed_seconds - elapsed_seconds
                if remaining_seconds <= 0:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise BuildDeadlineExceeded(max_elapsed_seconds, elapsed_seconds)
                await asyncio.wait(
                    {task},
                    timeout=min(1.0, remaining_seconds),
                )
            try:
                return await task
            except TimeoutError as error:
                raise RuntimeError(
                    "builder operation timed out before the overall build deadline"
                ) from error
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    def _remaining_build_seconds(
        build_started_at: float | None,
        max_elapsed_seconds: float | None,
    ) -> float | None:
        if build_started_at is None or max_elapsed_seconds is None:
            return None
        return max_elapsed_seconds - (time.time() - build_started_at)

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
