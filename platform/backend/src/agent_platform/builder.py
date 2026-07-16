from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from .applications import ApplicationService
from .blocks import BlockRegistry
from .models import ChatMessage, ContentBlock, ToolDefinition
from .providers import ModelProvider
from .runtime import AgentRuntime
from .storage import Storage
from .models import AgentSpec
from .workflow_models import (
    BuildTask,
    BuildTeamState,
    DraftOperation,
    EdgeSpec,
    NodeSpec,
    TeammateState,
    WorkflowTestCase,
)
from .workflow_runtime import WorkflowRuntime
from .workflow_storage import WorkflowStorage
from .tools import ToolRegistry


BUILDER_SYSTEM_PROMPT = """You coordinate a persistent team that builds production-ready agent workflows.

## Decision Tree (follow in order — do NOT skip steps)

### Step 1: MATCH — always check templates first
  Call template_suggestions FIRST, before any catalog search.
  - confidence >= 0.7: call template_adapt(name, requirement) to expand + get repair hints
  - confidence >= 0.5: call template_expand, then use draft_update_node to customize
  - confidence < 0.5: proceed to Step 2

### Step 2: BUILD — prefer topology over prompt complexity

  Harness nodes (if_else, task_dispatcher, permission_gate) are deterministic and
  testable. LLM nodes are powerful but non-deterministic. Move decisions into
  Harness nodes whenever possible:
  - Route with if_else/question_classifier, not "figure it out" in a prompt.
  - Sort dependencies with task_dispatcher, not "the LLM will determine the order."
  - Guard tools with permission_gate, expensive calls with budget_gate.

  CORE = [start, end, llm, if_else, loop, template_transform]
  - First compose with ONLY core blocks. Search space is 6^d, not 41^d.
  - Add ONE non-core block at a time, re-validate after each.
  - Never use more than 3 non-core blocks without justification.

### Step 3: TEST — run tests and use diagnostics for targeted repair
  - test_run returns precise diagnostics: which assertion failed, what node, expected vs actual.
  - Read the _repair_instruction field. It tells you EXACTLY which node to fix and how.
  - Use draft_update_node to fix only the broken node. Do NOT rebuild from scratch.
  - After each fix, validate (draft_validate) then test_run again.

### Step 4: PUBLISH — only when all mandatory tests pass

## Block Construction Rules
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
- Add mandatory tests with required_node_types to gate architecture visibility.
- If a test fails, test_run returns _repair_instruction with precise diagnostics:
    WHICH assertion failed → WHICH node produced wrong output → WHAT to fix.
  Read _repair_instruction carefully. Use draft_update_node to fix ONLY the broken node.
  Do NOT weaken assertions or rebuild from scratch — targeted repair only.
- Each draft mutation returns _validation + _hint. Check them after every operation.
- **Architecture review gate**: draft_validate before test_run. Fix errors first.
- **Block vs Tool**: WebSearch/Bash/Read are Tools, not Blocks. Use 'tool' block + tool_name.
- **structural_only**: for LLM workflows, set structural_only=true on tests.
- Publish only after all mandatory tests pass for the exact content hash.
"""


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
        self.on_build_complete = on_build_complete
        self.template_store = template_store
        self.active: dict[str, asyncio.Task[None]] = {}
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

    async def _run(self, build_id: str) -> None:
        build = await self.workflow_store.get_build(build_id)
        state: BuildTeamState = build["team_state"]
        await self.workflow_store.update_build(build_id, status="building", team_state=state)
        await self._emit(build_id, "build.started", {
            "application_id": build["application_id"], "requirement": build["requirement"]
        })
        if state.coordinator_messages:
            messages = [ChatMessage.model_validate(item) for item in state.coordinator_messages]
            messages.append(ChatMessage(role="user", content=[ContentBlock(
                type="text",
                text="Resume the same build from its persisted draft and team state. Inspect current status, resolve remaining failures, and complete the original acceptance criteria.",
            )]))
        else:
            messages = [ChatMessage(role="user", content=[ContentBlock(
                type="text",
                text=(
                    f"Build and verify this application:\n\n{build['requirement']}\n\n"
                    f"Application id: {build['application_id']}. Auto publish: {build['auto_publish']}."
                ),
            )])]
        try:
            await self._agent_loop(
                build_id,
                build["application_id"],
                state,
                messages,
                max_turns=int(build["max_turns"]),
                max_repair_cycles=int(build["max_repair_cycles"]),
                auto_publish=bool(build["auto_publish"]),
                teammate=None,
            )
            if state.published_version is not None:
                status = "published"
            else:
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
            # Collect build quality metadata for evolution signal
            draft = await self.workflow_store.get_draft(build["application_id"])
            build_metadata = {
                "node_count": len(draft["snapshot"].workflow.nodes),
                "edge_count": len(draft["snapshot"].workflow.edges),
                "test_count": len(draft["snapshot"].tests),
                "repair_cycles_used": state.repair_cycles,
                "node_types": sorted({n.type for n in draft["snapshot"].workflow.nodes}),
            }
            await self._emit(build_id, "build.completed", {
                "status": status,
                "published_version": state.published_version,
                "build_metadata": build_metadata,
            })
            # Recommendation flywheel: close the loop — update template success_rate
            if state.expanded_from_template and self.template_store:
                build_success = status == "published"
                self.template_store.record_usage(
                    state.expanded_from_template, success=build_success,
                )
            # Meta-cognition: try to evolve templates from the ACTUAL built workflow
            if self.on_build_complete and (status == "published" or status == "ready"):
                asyncio.create_task(self.on_build_complete(build_id))
        except asyncio.CancelledError:
            await self.workflow_store.update_build(build_id, status="cancelled", team_state=state)
            await self._emit(build_id, "build.cancelled", {})
            raise
        except Exception as error:
            await self.workflow_store.update_build(
                build_id, status="needs_attention", team_state=state, error=str(error)
            )
            await self._emit(build_id, "build.needs_attention", {
                "error": str(error), "error_type": type(error).__name__
            })

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
    ) -> str:
        final = ""
        tools = self._definitions(allow_team=teammate is None)
        for turn in range(1, max_turns + 1):
            stream = self.provider.stream(
                model=self.generator_model,
                system=BUILDER_SYSTEM_PROMPT + (
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
                # ── JSON parse error handler (P0 fix) ──
                if getattr(call, "input_parse_error", None):
                    content = (
                        f"JSON_PARSE_ERROR: {call.input_parse_error}\n\n"
                        f"Your tool call for '{call.name}' had malformed JSON. "
                        f"Please retry with correct JSON. Common causes:\n"
                        f"1. Missing comma between object fields\n"
                        f"2. Unescaped quotes inside string values\n"
                        f"3. Trailing comma in object/array\n"
                        f"4. String value not properly quoted\n"
                    )
                    is_error = True
                    results.append(ContentBlock(
                        type="tool_result", tool_use_id=call.id,
                        content=content, is_error=True,
                    ))
                    await self._emit(build_id, "build.operation", {
                        "actor": teammate or "coordinator",
                        "tool": call.name,
                        "input": {"error": "json_parse_error"},
                        "success": False,
                        "result": content[:500],
                    })
                    continue

                try:
                    value = await self._execute(
                        build_id,
                        application_id,
                        state,
                        call.name or "",
                        call.input or {},
                        max_repair_cycles=max_repair_cycles,
                        auto_publish=auto_publish,
                    )
                    content = json.dumps(value, ensure_ascii=False, default=str)
                    is_error = False
                except Exception as error:
                    content = f"{type(error).__name__}: {error}"
                    is_error = True
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
            if state.published_version is not None:
                break
            await self._emit(build_id, "build.turn.completed", {
                "actor": teammate or "coordinator", "turn": turn, "draft_revision": state.revision
            })
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
    ) -> Any:
        if tool == "catalog_search":
            query = str(data.get("query", "")).casefold()
            CORE_BLOCKS = {"start", "end", "llm", "if_else", "loop", "template_transform"}
            definitions = [
                item for item in self.blocks.list()
                if not query or query in f"{item.type} {item.title} {item.description} {item.category}".casefold()
            ]
            # ── Core-block priority (Habel-Plump-Lafont) ──
            definitions.sort(key=lambda d: (0 if d.type in CORE_BLOCKS else 1, d.type))
            results: list[dict[str, Any]] = [
                {"type": item.type, "title": item.title, "description": item.description,
                 "category": item.category, "core": item.type in CORE_BLOCKS}
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
            # Call the API endpoint internally — simple relevance scoring
            scored: list[tuple[float, dict[str, Any]]] = []
            query = requirement.casefold()
            templates = self.template_store.list() if self.template_store else []
            for meta in templates:
                searchable = f"{meta.name} {meta.title} {' '.join(meta.tags)}".casefold()
                tag_matches = sum(1 for tag in meta.tags if tag.casefold() in query)
                name_match = 1.0 if any(w in searchable for w in query.split() if len(w) > 3) else 0.0
                score = meta.confidence * (0.5 * tag_matches + 0.5 * name_match)
                if score > 0.1:
                    scored.append((score, meta))
            scored.sort(key=lambda x: x[0], reverse=True)
            # Bump usage_count for top matches (feedback: Builder selected this template)
            for s, meta in scored[:3]:
                if hasattr(meta, "usage_count"):
                    meta.usage_count += 1  # recommendation flywheel: template was chosen
            return [{"name": m.name, "title": m.title, "confidence": m.confidence,
                     "relevance": round(s, 3), "tags": m.tags}
                    for s, m in scored[:5]]
        if tool == "template_list":
            if self.template_store:
                return [
                    {"name": m.name, "title": m.title, "description": m.description,
                     "category": m.category, "tags": m.tags}
                    for m in self.template_store.list()
                ]
            # Fallback for when template_store is not configured
            return [
                {"name": name, "description": "Workflow subgraph template."}
                for name in self.blocks.template_names()
            ]
        if tool == "template_expand":
            template_name = str(data["name"])
            # Track which template was expanded for the recommendation flywheel
            state.expanded_from_template = template_name
            prefix = str(data.get("prefix") or template_name)
            position = data.get("position") if isinstance(data.get("position"), dict) else {}
            x_pos = float(position.get("x", 0))
            y_pos = float(position.get("y", 0))
            if self.template_store:
                workflow = self.template_store.expand_into_workflow(
                    template_name, prefix=prefix, x=x_pos, y=y_pos,
                )
            else:
                # Fallback to legacy BlockRegistry path
                workflow = self.blocks.expand_template(
                    template_name, prefix=prefix, x=x_pos, y=y_pos,
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
            return {
                "template": template_name,
                "revision": revision,
                "nodes": [node.id for node in workflow.nodes],
                "edges": [edge.id for edge in workflow.edges],
            }
        if tool == "template_adapt":
            # ── Insight 2: Cross-granularity learning ──
            # Expand a template then compute minimal edits needed to match requirement.
            # This is the "middle path" between full-expand and from-scratch.
            template_name = str(data["name"])
            requirement = str(data.get("requirement", ""))
            state.expanded_from_template = template_name
            prefix = str(data.get("prefix") or f"{template_name}_adapted")
            position = data.get("position") if isinstance(data.get("position"), dict) else {}
            x_pos = float(position.get("x", 0))
            y_pos = float(position.get("y", 0))

            if self.template_store:
                wf = self.template_store.expand_into_workflow(
                    template_name, prefix=prefix, x=x_pos, y=y_pos,
                )
            else:
                wf = self.blocks.expand_template(
                    template_name, prefix=prefix, x=x_pos, y=y_pos,
                )

            # Compute graph edit distance vs the requirement — identify what needs changing
            from .workflow_quality import suggest_minimal_repair

            # Get the original template workflow (un-prefixed) for comparison
            if self.template_store:
                orig = self.template_store.expand_into_workflow(
                    template_name, prefix="__orig__", x=0, y=0,
                )
            else:
                orig = self.blocks.expand_template(template_name, prefix="__orig__", x=0, y=0)

            repair_hints = suggest_minimal_repair(orig, wf)

            # Add all nodes/edges to draft
            draft = await self.workflow_store.get_draft(application_id)
            revision = int(draft["revision"])
            for node in wf.nodes:
                result = await self.applications.apply_operation(
                    application_id,
                    DraftOperation(
                        expected_revision=revision,
                        idempotency_key=f"{build_id}:template_adapt:{template_name}:{node.id}",
                        op="add_node",
                        data={"node": node.model_dump(mode="json")},
                    ),
                )
                revision = int(result["revision"])
            for edge in wf.edges:
                result = await self.applications.apply_operation(
                    application_id,
                    DraftOperation(
                        expected_revision=revision,
                        idempotency_key=f"{build_id}:template_adapt:{template_name}:{edge.id}",
                        op="add_edge",
                        data={"edge": edge.model_dump(mode="json")},
                    ),
                )
                revision = int(result["revision"])
            state.revision = revision
            return {
                "template": template_name,
                "mode": "adapt",
                "revision": revision,
                "nodes": [node.id for node in wf.nodes],
                "edges": [edge.id for edge in wf.edges],
                "repair_hints": repair_hints,
                "instruction": (
                    f"Template '{template_name}' expanded with {len(wf.nodes)} nodes. "
                    f"Requirement: {requirement[:200]}. "
                    f"Now use draft_update_node to adapt specific nodes to match the requirement. "
                    f"Repair hints: {'; '.join(repair_hints[:5])}"
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
            draft = await self.workflow_store.get_draft(application_id)
            if tool == "draft_add_node":
                node = NodeSpec.model_validate(data["node"])
                # ── Block-type validation with helpful error ──
                try:
                    definition = self.blocks.get(node.type)
                except KeyError:
                    known = [b.type for b in self.blocks.list()]
                    known_tools = list(self.core_tools.names())
                    similar_blocks = [b for b in known if node.type.casefold() in b.casefold() or b.casefold() in node.type.casefold()]
                    similar_tools = [t for t in known_tools if node.type.casefold() in t.casefold() or t.casefold() in node.type.casefold()]
                    hints = []
                    if similar_blocks:
                        hints.append(f"Did you mean one of these blocks: {similar_blocks[:5]}?")
                    if similar_tools:
                        hints.append(f"'{node.type}' is a Tool, not a Block. To use it, add a 'tool' block with tool_name='{node.type}'.")
                    if not hints:
                        hints.append(f"Available blocks: {sorted(known)[:20]}")
                    raise RuntimeError(
                        f"unknown block type: {node.type}. {' '.join(hints)}"
                    ) from None
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
                op, payload = "add_test", {
                    "test": WorkflowTestCase.model_validate(data["test"]).model_dump(mode="json")
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
            state.revision = result["revision"]
            # ── Post-operation auto-validation (P0 fix) ──
            # After any draft mutation, immediately validate so Builder
            # sees errors before the next operation — prevents building on
            # a broken foundation.
            try:
                v = await self.applications.validate_draft(application_id)
                result["_validation"] = {
                    "valid": v["valid"],
                    "errors": v.get("errors", []),
                    "warnings": v.get("warnings", []),
                }
                if not v["valid"]:
                    result["_hint"] = (
                        "Draft has structural errors. Fix these BEFORE your next operation."
                        " Use draft_update_node / draft_connect / draft_remove_edge to repair."
                        " Errors: " + "; ".join(v["errors"][:3])
                    )
            except Exception:
                pass  # validation is advisory, never block
            return result
        if tool == "draft_validate":
            return await self.applications.validate_draft(application_id)
        if tool == "test_run":
            if state.repair_cycles >= max_repair_cycles:
                raise RuntimeError(f"maximum repair cycles reached ({max_repair_cycles})")
            # ── Pre-test gate: validate draft first ──
            validation = await self.applications.validate_draft(application_id)
            if not validation["valid"]:
                state.repair_cycles += 1
                return {
                    "passed": False,
                    "validation": validation,
                    "tests": [],
                    "error": "Draft validation failed BEFORE running tests. Fix these issues first: " + "; ".join(validation["errors"]),
                }
            report = await self.runtime.run_test_suite(application_id)
            if not report["passed"]:
                state.repair_cycles += 1
                # ── Attach precise diagnostics (Harness-layer) ──
                diag = report.get("diagnostics", "")
                if diag:
                    report["_repair_instruction"] = (
                        "Tests failed. Below is a precise diagnostic of WHAT failed and WHERE. "
                        "Use draft_update_node to fix the specific nodes mentioned. "
                        "Do NOT rebuild from scratch — only fix the nodes with errors.\n\n"
                        + diag
                    )
            return report
        if tool == "draft_publish":
            if not auto_publish and not data.get("explicit", False):
                return {"status": "ready", "message": "auto publish is disabled"}
            published = await self.workflow_store.publish(application_id)
            state.published_version = published["version"]
            await self._emit(build_id, "build.published", published)
            return published
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
            )
            teammate.messages = [message.model_dump(mode="json") for message in messages]
            teammate.status = "idle"
            await self._emit(build_id, "team.teammate.idle", {"name": name, "result": result[:5000]})
            return {"name": name, "status": "idle", "result": result}
        if tool == "send_message":
            name = str(data["name"])
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
            )
            teammate.messages = [message.model_dump(mode="json") for message in messages]
            teammate.mailbox.clear()
            teammate.status = "idle"
            return {"name": name, "status": "idle", "result": result}
        raise KeyError(f"unknown builder tool: {tool}")

    def _definitions(self, *, allow_team: bool) -> list[ToolDefinition]:
        object_schema = {"type": "object", "additionalProperties": True}
        definitions = [
            # ── Step 1: MATCH (template-first strategy) ──
            ToolDefinition(name="template_suggestions", description="🔍 STEP 1 — Search template marketplace. ALWAYS call this FIRST before any catalog search. Returns matching templates with confidence scores.", input_schema={"type": "object", "properties": {"requirement": {"type": "string", "description": "Natural language requirement to match against templates"}}, "required": ["requirement"]}),
            ToolDefinition(name="template_list", description="List all available templates with categories and descriptions.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="template_adapt", description="🎯 PREFERRED — Expand template + get graph-edit-distance repair hints. Use when confidence >= 0.5. Gives you the minimal edits needed to adapt the template to your requirement.", input_schema={"type": "object", "properties": {"name": {"type": "string"}, "requirement": {"type": "string"}, "prefix": {"type": "string"}, "position": {"type": "object", "additionalProperties": True}}, "required": ["name", "requirement"]}),
            ToolDefinition(name="template_expand", description="Expand a template as-is into the draft (use template_adapt if you need customization hints).", input_schema={"type": "object", "properties": {"name": {"type": "string"}, "prefix": {"type": "string"}, "position": {"type": "object", "additionalProperties": True}}, "required": ["name"]}),
            # ── Step 2: BUILD (core blocks first) ──
            ToolDefinition(name="catalog_search", description="Search available workflow bricks. Results are sorted: core blocks first, then others.", input_schema={"type": "object", "properties": {"query": {"type": "string"}}}),
            ToolDefinition(name="catalog_get", description="Read the exact schema and ports for one brick.", input_schema={"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]}),
            ToolDefinition(name="manual_search", description="Search block manuals before selecting agent architecture bricks.", input_schema={"type": "object", "properties": {"query": {"type": "string"}, "block_kind": {"enum": ["business_workflow", "agent_architecture", "legacy_compatibility"]}}}),
            ToolDefinition(name="manual_get", description="Read one block manual, including when to use it, examples, anti-patterns, and Claude architecture mapping.", input_schema={"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]}),
            ToolDefinition(name="architecture_blueprint", description="Read the Claude-like runtime blueprint made from explicit composable bricks.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="draft_inspect", description="Inspect the current shared draft and revision.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="draft_add_node", description="Add exactly one configured node to the draft.", input_schema={"type": "object", "properties": {"node": NodeSpec.model_json_schema()}, "required": ["node"]}),
            ToolDefinition(name="draft_update_node", description="Patch exactly one node; config patches merge by default.", input_schema={"type": "object", "properties": {"node_id": {"type": "string"}, "changes": object_schema, "merge_config": {"type": "boolean"}}, "required": ["node_id", "changes"]}),
            ToolDefinition(name="draft_remove_node", description="Remove one node and its incident edges.", input_schema={"type": "object", "properties": {"node_id": {"type": "string"}}, "required": ["node_id"]}),
            ToolDefinition(name="draft_connect", description="Connect two existing node ports with one edge.", input_schema={"type": "object", "properties": {"edge": EdgeSpec.model_json_schema()}, "required": ["edge"]}),
            ToolDefinition(name="draft_remove_edge", description="Remove one edge.", input_schema={"type": "object", "properties": {"edge_id": {"type": "string"}}, "required": ["edge_id"]}),
            ToolDefinition(name="draft_upsert_agent", description="Create or update one inline Claude Agent definition.", input_schema={"type": "object", "properties": {"agent": AgentSpec.model_json_schema()}, "required": ["agent"]}),
            ToolDefinition(name="draft_validate", description="Run graph, schema, port, agent-binding, and test-presence validation.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="test_add", description="Add one traceable workflow acceptance test. Include required_node_types and required_tool_nodes for visible architecture gates.", input_schema={"type": "object", "properties": {"test": WorkflowTestCase.model_json_schema()}, "required": ["test"]}),
            ToolDefinition(name="test_remove", description="Remove an incorrect test, never to hide a real failure.", input_schema={"type": "object", "properties": {"test_id": {"type": "string"}}, "required": ["test_id"]}),
            ToolDefinition(name="test_run", description="Run all mandatory tests against the exact current draft using real providers and tools.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="draft_publish", description="Publish an immutable version; fails unless current hash passed all mandatory tests.", input_schema={"type": "object", "properties": {"explicit": {"type": "boolean"}}}),
            ToolDefinition(name="task", description="Create/list/update shared requirement tasks with owners and dependencies.", input_schema={"type": "object", "properties": {"action": {"enum": ["create", "list", "update"]}, "id": {"type": "integer"}, "subject": {"type": "string"}, "description": {"type": "string"}, "status": {"enum": ["pending", "in_progress", "completed", "blocked"]}, "owner": {"type": "string"}, "blocked_by": {"type": "array", "items": {"type": "integer"}}, "acceptance": {"type": "array", "items": {"type": "string"}}}, "required": ["action"]}),
        ]
        if allow_team:
            definitions.extend([
                ToolDefinition(name="spawn_teammate", description="Create an isolated persistent teammate for a bounded task.", input_schema={"type": "object", "properties": {"name": {"type": "string"}, "task": {"type": "string"}, "max_turns": {"type": "integer"}}, "required": ["name", "task"]}),
                ToolDefinition(name="send_message", description="Wake an existing teammate with a follow-up message while retaining its context.", input_schema={"type": "object", "properties": {"name": {"type": "string"}, "message": {"type": "string"}, "max_turns": {"type": "integer"}}, "required": ["name", "message"]}),
            ])
        return definitions

    async def _emit(self, stream_id: str, kind: str, data: dict[str, Any]) -> None:
        await self.storage.append_event(stream_id, kind, data)

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

    def _consume(self, build_id: str, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        self.active.pop(build_id, None)
