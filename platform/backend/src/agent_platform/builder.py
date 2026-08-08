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
from .build_transcript import (
    BuildTranscriptStore,
    event_record,
    tool_call_record,
    turn_record,
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
- The complete block catalog (one line per block) is already in the build request. Inspect the draft, then go
  straight to catalog_get/manual_get for the few blocks you will actually use — broad catalog_search sweeps
  waste turns you need for building.
- You build for a human owner who often cannot state everything upfront. When information you genuinely need
  is missing and cannot be responsibly inferred (target output fields, matching tolerances, external system
  access, sample data), call ask_owner ONCE with a single batched, concrete list of questions instead of
  guessing. Never ask about details you can decide yourself; at most two ask_owner pauses per build.
- 会话流里你的每一句话都会实时展示给中文业主：过程叙述一律用简体中文、一句话说清你正在做什么
  （例如"正在把搜索结果接进分析环节"），不要英文叙述，不要技术黑话。思考过程可以自由，但说出口的
  话必须是业主能读的。
- The owner is a business person, not an engineer, and everything you say to them (ask_owner questions,
  delivery notes) is customer-facing: plain business language only. Never mention block names, node types,
  configuration fields, index or schema names, or any internal platform design — that is confidential
  platform IP and meaningless to the customer. Infrastructure, configuration, and tooling problems are
  YOURS to diagnose and solve; the owner can only answer business questions (what fields, what rules,
  what data, what counts as correct).
- The owner may send new instructions at any moment; they arrive as user messages marked as owner
  instructions. Fold them in immediately as top priority — they override your earlier assumptions.
- End every build with a delivery note written in the owner's language (中文需求 → 中文说明): what the
  workflow does, what inputs it needs, what it outputs, key assumptions, and what it explicitly cannot do
  (for example no real email/ERP connection — placeholder steps). Make it the final text of your last turn.
- When the requirement fixes named output fields, terminate with an end node exposing exactly those fields;
  use answer only for conversational replies with no declared output schema.
- Prefer one structured Model Turn shared by related steps instead of a serial LLM call per step. Split model
  calls only when different tools, permissions, branches, state boundaries, or independently editable behavior
  require it.
- Deterministic work must run on deterministic blocks, never inside an LLM prompt: record set matching and
  reconciliation → record_match (batch mode: sources list); business arithmetic (forecast averages, coverage,
  thresholds, MOQ floors) → variable_assigner in $formula mode — one readable infix line like
  "when(stock < avg(sales[-4:]) * lead, max(ceil(avg(sales[-4:]) * (lead + 2) - stock), moq), 0)" with vars
  bound to references; constrained replenishment/ordering → replenishment_planner. An LLM node is for
  judgment, language, and unstructured extraction only — a model doing arithmetic or matching is an audit
  finding, not a solution.
- Answering from reference documents — especially with audience or permission differences — must go through
  knowledge blocks (knowledge_index_sync to ingest, knowledge_retrieval with access filters to query). Pasting
  document text into a prompt is prohibited when the requirement names documents, manuals, or access levels.
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
- For complex or multi-module requirements, call build_plan with action="set" before mutating the draft.
  The build plan should name modules, expected blocks, reuse_depth, complexity, risks, and how each module
  will be tested. Keep the plan updated as modules are built and tested.
- **Before building a workflow from scratch**, call template_suggestions with the requirement text and intended
  reuse_depth. A name, keyword score, usage count, or confidence value is not implementation evidence.
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
- Every Start input must carry a Chinese business label AND an example value the end user can imitate
  (e.g. {"name": "bank_lines", "label": "银行流水", "type": "array", "example": [{"日期": "2026-08-01",
  "单号": "PO-1", "金额": 1200}]}). Ordinary users see only labels and examples — an unlabeled input
  is an unusable input.
- For mutually exclusive branch outputs consumed by Variable Aggregator, set "optional": true inside the
  reference so a skipped branch resolves to null instead of failing.
- A valid graph has exactly one start, at least one end/answer, no implicit cycles, and no unreachable nodes.
- Add mandatory tests that demonstrate the user's actual acceptance criteria. Run them with test_run.
  Acceptance is the final executable proof, not a checkpoint followed by later mutations.
- Any workflow that depends on external data (search, HTTP, collection, retrieval) MUST include an
  empty-result test case, and the workflow itself must handle emptiness honestly: expose an empty list
  plus a plain-language note suggesting what the user can change — NEVER let a structured output get
  filled with format examples or invented content when upstream returned nothing. Shape-valid garbage
  is still garbage.
- Each test should include a readable frame with category, purpose, reviewer_guidance, reference, and failure_target.
  The frame should explain where the test sits in the acceptance framework, for example outline adherence,
  tool evidence, safety, or human review.
- Tests for generated workflows must set required_node_types for the visible architecture and required_tool_nodes
  when a concrete Tool brick is required, e.g. WebSearch. This prevents a single opaque Agent node from passing.
- When a requirement depends on external tools, tests must set required_tools, minimum_tool_calls, and
  require_cited_tool_urls so a model cannot pass by inventing plausible output without tool evidence.
- test_add is an atomic add-or-replace operation: calling it with an existing test id replaces that test in one
  revision. Repair a failed test with the same id; do not delete it first or temporarily reduce acceptance coverage.
- If a test fails, work like a careful engineer: call run_inspect with the failing test's run_id and read the
  execution ledger (which nodes actually ran, which one failed, with what error) BEFORE editing anything.
  Fix the diagnosed cause; never blind-retry, never switch approach just because one attempt failed twice.
  Then inspect frame.failure_target and repair the implementation before changing the test. Once acceptance is first executed, its assertion semantics are frozen: do not remove,
  replace, or weaken assertions merely to make the build green.
- Once test_run passes all mandatory tests, the delivery is frozen. Do not inspect more catalogs, change nodes,
  edges, agents, templates, or tests, or delegate more work. Finish task and plan bookkeeping; after a passing
  test the platform auto-publishes when auto_publish is enabled, then write your delivery note.
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
        transcripts: BuildTranscriptStore | None = None,
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
        self.transcripts = transcripts
        self.active: dict[str, asyncio.Task[Any]] = {}
        self._trackers: dict[str, DecisionTracker] = {}  # build_id → tracker
        self._resume_messages: dict[str, str] = {}  # build_id → pending user note
        # build_id → owner notes sent while the build is running; drained at the
        # next coordinator turn. In-process only: builds started by this API
        # process run in this process (worker claiming is opt-in via /workers).
        self._live_messages: dict[str, list[str]] = {}

    def queue_resume_message(self, build_id: str, message: str) -> None:
        """Attach a user instruction that the resumed loop will read first."""

        text = message.strip()
        if text:
            self._resume_messages[build_id] = text[:8_000]

    def _internal_terms_in(self, text: str) -> list[str]:
        """Platform-internal vocabulary that must never reach a customer."""

        lowered = text.lower()
        hits = [
            block.type
            for block in self.blocks.list()
            if ("_" in block.type or len(block.type) >= 8) and block.type in lowered
        ]
        for term in (
            "$ref", "$inputs", "$run", "node_id", "draft_", "test_run",
            "config", "index_name", "source_id", "event_id", "top_k",
            "workflow json", "schema", "端口", "积木", "节点配置",
        ):
            if term in lowered:
                hits.append(term)
        return sorted(set(hits))

    def post_live_message(self, build_id: str, message: str) -> None:
        """Deliver an owner note into a running build's next coordinator turn."""

        text = message.strip()
        if not text:
            return
        inbox = self._live_messages.setdefault(build_id, [])
        if len(inbox) >= 20:
            raise RuntimeError("live message inbox is full — wait for the Builder to catch up")
        inbox.append(text[:8_000])

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
        if (
            build["status"] == "needs_attention"
            and state.pending_question
            and build_id not in self._resume_messages
        ):
            # Paused waiting for the owner. Lease reconciliation and other
            # recovery paths must not self-revive this build — only the resume
            # API (which queues the owner's reply and resets status) may.
            await self.harness.finish_task(build_id, status="paused")
            return {
                "build_id": build_id,
                "application_id": build["application_id"],
                "status": "needs_attention",
                "skipped": "waiting_owner",
            }
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
        resume_note = self._resume_messages.pop(build_id, None)
        answered_question = None
        if resume_note and state.pending_question:
            answered_question = state.pending_question
        if resume_note:
            state.pending_question = None
            # An owner instruction reopens delivery: the next publish must mint
            # a new version instead of short-circuiting on the existing one.
            state.published_version = None
        if state.coordinator_messages:
            messages = [ChatMessage.model_validate(item) for item in state.coordinator_messages]
            if answered_question:
                priority_frame = (
                    "You paused this build to ask the owner:\n\n"
                    f"{answered_question}\n\n"
                    f"The owner replied:\n\n{resume_note}\n\n"
                    "Treat the reply as the top priority for this continuation.\n\n"
                )
            elif resume_note:
                priority_frame = (
                    "The owner sent this instruction — treat it as the top priority for this "
                    f"continuation:\n\n{resume_note}\n\n"
                )
            else:
                priority_frame = ""
            messages.append(ChatMessage(role="user", content=[ContentBlock(
                type="text",
                text=(
                    priority_frame
                    + "Resume the same build from its persisted draft and team state. Inspect current status, "
                    "resolve remaining failures, and complete the original acceptance criteria."
                    + contract_context
                ),
            )]))
        else:
            messages = [ChatMessage(role="user", content=[ContentBlock(
                type="text",
                text=(
                    f"Build and verify this application:\n\n{build['requirement']}\n\n"
                    f"Application id: {build['application_id']}. Auto publish: {build['auto_publish']}.\n\n"
                    + self._catalog_overview()
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
            if state.pending_question:
                # The Builder paused to ask the owner. Skip validation/publish —
                # the draft is intentionally unfinished until the owner replies,
                # and the harness task pauses so resume can revive it.
                if manage_harness_task:
                    await self.harness.finish_task(build_id, status="paused")
                await self._emit(build_id, "build.waiting_owner", {
                    "question": state.pending_question,
                })
                self._record_event(
                    build_id, "waiting_owner",
                    "莉莉丝暂停了搭建，等你回复上面的问题后继续",
                )
                await self.workflow_store.update_build(
                    build_id, status="needs_attention", team_state=state, error=""
                )
                return {
                    "build_id": build_id,
                    "application_id": build["application_id"],
                    "status": "needs_attention",
                    "pending_question": state.pending_question,
                }
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
                    published = await self.workflow_store.publish(
                        build["application_id"], acknowledge_warnings=True
                    )
                    state.published_version = published["version"]
                    await self._emit(build_id, "build.published", published)
                    self._record_event(
                        build_id, "published",
                        f"工作流已发布为正式版 v{published['version']}，现在可以试运行了",
                    )
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
            self._record_event(build_id, "cancelled", "这次搭建被取消了")
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
            self._record_event(
                build_id, "needs_attention",
                "搭建中途遇到问题停下来了，可以在下方留言让莉莉丝继续",
            )
            await self.workflow_store.update_build(
                build_id, status="needs_attention", team_state=state, error=str(error)
            )
            if not manage_harness_task:
                raise





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
            if teammate is None:
                live_notes = self._live_messages.pop(build_id, None)
                if live_notes:
                    messages.append(ChatMessage(role="user", content=[ContentBlock(
                        type="text",
                        text=(
                            "The owner sent new instructions while you were building — fold them "
                            "into the current plan as top priority before continuing:\n\n"
                            + "\n\n".join(live_notes)
                        ),
                    )]))
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
                self._record_turn(build_id, turn, teammate, response, [], state)
                final = "".join(block.text or "" for block in response.blocks if block.type == "text")
                if teammate is None:
                    state.coordinator_messages = [
                        message.model_dump(mode="json") for message in messages
                    ]
                break
            results: list[ContentBlock] = []
            turn_tool_records: list[dict[str, Any]] = []
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
                turn_tool_records.append(tool_call_record(
                    name=call.name or "",
                    arguments=call.input or {},
                    result=content,
                    is_error=is_error,
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
            self._record_turn(build_id, turn, teammate, response, turn_tool_records, state)
            await self.workflow_store.update_build(build_id, team_state=state)
            if teammate is None and state.pending_question:
                final = state.pending_question
                break
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
        if tool == "ask_owner":
            question = str(data.get("question", "")).strip()
            if not question:
                raise RuntimeError("ask_owner requires a non-empty question")
            leaked = self._internal_terms_in(question)
            if leaked:
                raise RuntimeError(
                    "ask_owner rejected: the question exposes platform internals "
                    f"({', '.join(leaked[:5])}). The owner is a business person — they "
                    "cannot see or configure any of this, and internal design must never "
                    "be shown to customers. Solve infrastructure and configuration "
                    "problems yourself; ask only for business facts the owner actually "
                    "knows (field lists, sample data, business rules, acceptance "
                    "criteria), in plain business language."
                )
            state.pending_question = question[:4_000]
            return {
                "status": "question_delivered",
                "note": (
                    "The build pauses after this turn so the owner can reply. "
                    "End your turn now with a short status message; do not call more tools."
                ),
            }
        if tool == "run_inspect":
            run_id = str(data.get("run_id") or "").strip()
            if not run_id:
                raise RuntimeError("run_inspect requires run_id (test_run failures include one per test)")
            events = await self.storage.list_events(run_id, 0)
            trail: list[dict[str, Any]] = []
            for event in events:
                if event.type not in (
                    "node.started", "node.completed", "node.failed",
                    "workflow.failed", "workflow.completed",
                ):
                    continue
                payload = event.data or {}
                item: dict[str, Any] = {"event": event.type}
                for key in ("node_id", "type"):
                    if payload.get(key):
                        item[key] = payload[key]
                if event.type == "node.completed" and isinstance(payload.get("outputs"), dict):
                    item["output_keys"] = sorted(payload["outputs"].keys())[:12]
                if payload.get("error"):
                    item["error"] = str(payload["error"])[:400]
                trail.append(item)
            return {
                "run_id": run_id,
                "executed_node_types": sorted({
                    str(item.get("type")) for item in trail if item.get("type")
                }),
                "events": trail[:120],
                "note": "This ledger is what actually executed. Diagnose from it before editing.",
            }
        if tool == "catalog_search":
            query = str(data.get("query", "")).casefold()
            normalized_query = " ".join(query.split())
            if normalized_query in state.catalog_queries:
                return {
                    "note": (
                        "This exact query already ran in this build; its results are unchanged. "
                        "The complete catalog is in your first message — use catalog_get or "
                        "manual_get for a specific block instead of searching again."
                    ),
                    "matching_types": sorted(
                        item.type
                        for item in self.blocks.list()
                        if not normalized_query
                        or normalized_query
                        in f"{item.type} {item.title} {item.description} {item.category}".casefold()
                    ),
                }
            state.catalog_queries.append(normalized_query)
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
            definition = self.blocks.get(name)
            # The full definition includes the manual fields, so reading it
            # satisfies the read-the-manual-first requirement.
            self._remember_manual_lookup(state, definition.type)
            return definition.model_dump(mode="json")
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
                    payload.update({
                        "module_ref": record.module_ref,
                        "module_status": record.state.status,
                        "content_hash": record.state.content_hash,
                        "evidence_record_ids": record.state.evidence_record_ids,
                    })
                suggestion_payloads.append(payload)
            result = {
                "reuse_depth": reuse_depth,
                "effective_reuse_depth": effective_reuse_depth,
                "recommended_action": recommended_action_for_depth(effective_reuse_depth),
                "policy_reason": policy_reason,
                **default_metadata,
                "templates": suggestion_payloads,
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
            if (
                state.repair_cycles >= max_repair_cycles
                and state.last_failed_test_revision == state.revision
            ):
                # Block spinning on the same failing revision, but never dead-end
                # the build: changing the draft (new revision) re-opens test_run.
                raise RuntimeError(
                    f"maximum repair cycles reached ({max_repair_cycles}) for this "
                    "revision — change the draft before running tests again"
                )
            report = await self.runtime.run_test_suite(application_id)
            if not report["passed"]:
                if state.last_failed_test_revision != state.revision:
                    state.repair_cycles += 1
                    state.last_failed_test_revision = state.revision
            else:
                state.last_failed_test_revision = None
                if auto_publish:
                    published = await self.workflow_store.publish(
                        application_id, acknowledge_warnings=True
                    )
                    state.published_version = published["version"]
                    report["publication"] = published
                    report["next"] = (
                        "Published. End your next turn with the delivery note in the "
                        "owner's language: what it does, required inputs, outputs, key "
                        "assumptions, and anything it cannot do."
                    )
                    await self._emit(build_id, "build.published", published)
                    self._record_event(
                        build_id, "published",
                        f"工作流已发布为正式版 v{published['version']}，现在可以试运行了",
                    )
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
            # An explicit Builder publish after green tests acknowledges
            # remaining warnings; they stay recorded in the publish decision.
            published = await self.workflow_store.publish(
                application_id, acknowledge_warnings=True
            )
            state.published_version = published["version"]
            await self._emit(build_id, "build.published", published)
            self._record_event(
                build_id, "published",
                f"工作流已发布为正式版 v{published['version']}，现在可以试运行了",
            )
            return {
                **published,
                "next": (
                    "Published. End your next turn with the delivery note in the "
                    "owner's language: what it does, required inputs, outputs, key "
                    "assumptions, and anything it cannot do."
                ),
            }
        if tool == "build_plan":
            action = str(data["action"])
            if action == "set":
                plan = BuildPlan.model_validate(data["plan"])
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
            ToolDefinition(name="template_suggestions", description="Search reusable modules and legacy templates before building from scratch.", input_schema={"type": "object", "properties": {"requirement": {"type": "string", "description": "Natural language requirement to match against templates"}, "reuse_depth": {"enum": ["none", "shallow", "deep", "adaptive"], "description": "How aggressively to reuse templates."}}, "required": ["requirement"]}),
            ToolDefinition(name="template_list", description="List exact module versions, verification state, capability coverage, and legacy templates.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="template_expand", description="Expand one exact reusable-module version or legacy template into the editable draft.", input_schema={"type": "object", "properties": {"name": {"type": "string"}, "version": {"type": "integer", "minimum": 1}, "prefix": {"type": "string"}, "position": {"type": "object", "additionalProperties": True}}, "required": ["name"]}),
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
            ToolDefinition(name="run_inspect", description="Read the execution ledger of one run (node-level started/completed/failed events with errors). Call it with the run_id from a failing test before editing anything — diagnose from evidence, not guesses.", input_schema={"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}),
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
                ToolDefinition(name="ask_owner", description="Pause the build and ask the owner one batched set of blocking questions. Use only when required information cannot be responsibly inferred; the build stops until the owner replies.", input_schema={"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}),
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

    def _catalog_overview(self) -> str:
        """One compact line per block so the Builder never has to search blind."""

        by_category: dict[str, list[str]] = {}
        for item in self.blocks.list():
            by_category.setdefault(item.category, []).append(f"{item.type} — {item.title}")
        lines = ["Complete block catalog (type — title). Call catalog_get or manual_get for schemas:"]
        for category in sorted(by_category):
            lines.append(f"[{category}] " + "; ".join(sorted(by_category[category])))
        core = sorted(self.core_tools.names())
        if core:
            lines.append("[core tools] " + "; ".join(core))
        return "\n".join(lines)

    def _record_event(self, build_id: str, event: str, text: str) -> None:
        """Milestones (发布/等待/取消/故障) go into the transcript as system badges.

        Turn text is easy to skim past; the owner must never wonder "so did it
        publish or not". Never raises into the build loop.
        """

        if self.transcripts is None:
            return
        self.transcripts.append(build_id, event_record(text=text, event=event))

    def _record_turn(
        self,
        build_id: str,
        turn: int,
        teammate: str | None,
        response: Any,
        tool_records: list[dict[str, Any]],
        state: BuildTeamState,
    ) -> None:
        """Persist one model turn so a stalled build can be diagnosed later."""

        if self.transcripts is None:
            return
        self.transcripts.append(
            build_id,
            turn_record(
                turn=turn,
                actor=teammate or "coordinator",
                model=self.generator_model,
                blocks=response.blocks,
                tool_calls=tool_records,
                stop_reason=response.stop_reason,
                usage=response.usage,
                draft_revision=state.revision,
            ),
        )

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
