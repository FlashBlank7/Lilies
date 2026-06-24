from __future__ import annotations

import asyncio
import json
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

You do not generate source code or a whole workflow JSON document. You and your teammates can only build by
using the supplied block-catalog and incremental draft tools. Every requirement must map to a task, one or more
nodes, and a mandatory test. Inspect schemas before configuring unfamiliar blocks.

Core rules:
- Start by inspecting the draft and catalog, then create requirement tasks.
- Use spawn_teammate for bounded independent design or verification work. Roles are dynamic, not predefined.
- Add and configure one node or edge per mutation tool call. Never assume an operation succeeded.
- Values can reference prior output with {"$ref":{"node_id":"<id>","path":["field"]}}.
  Use node_id "$inputs" to reference raw workflow inputs.
- For mutually exclusive branch outputs consumed by Variable Aggregator, set "optional": true inside the
  reference so a skipped branch resolves to null instead of failing.
- A valid graph has exactly one start, at least one end/answer, no implicit cycles, and no unreachable nodes.
- Add mandatory tests that demonstrate the user's actual acceptance criteria. Run them with test_run.
- When a requirement depends on external tools, tests must set required_tools, minimum_tool_calls, and
  require_cited_tool_urls so a model cannot pass by inventing plausible output without tool evidence.
- If a test fails, inspect events and modify blocks; do not weaken the assertion merely to make it green.
- Publish only after draft_validate and all mandatory tests pass for the exact current content hash.
- Do not claim completion before draft_publish returns a version (unless auto-publish is disabled).
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
            await self._emit(build_id, "build.completed", {
                "status": status, "published_version": state.published_version
            })
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
                op, payload = "add_node", {
                    "node": NodeSpec.model_validate(data["node"]).model_dump(mode="json")
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
            return result
        if tool == "draft_validate":
            return await self.applications.validate_draft(application_id)
        if tool == "test_run":
            if state.repair_cycles >= max_repair_cycles:
                raise RuntimeError(f"maximum repair cycles reached ({max_repair_cycles})")
            report = await self.runtime.run_test_suite(application_id)
            if not report["passed"]:
                state.repair_cycles += 1
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
            ToolDefinition(name="catalog_search", description="Search available workflow bricks.", input_schema={"type": "object", "properties": {"query": {"type": "string"}}}),
            ToolDefinition(name="catalog_get", description="Read the exact schema and ports for one brick.", input_schema={"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]}),
            ToolDefinition(name="draft_inspect", description="Inspect the current shared draft and revision.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="draft_add_node", description="Add exactly one configured node to the draft.", input_schema={"type": "object", "properties": {"node": NodeSpec.model_json_schema()}, "required": ["node"]}),
            ToolDefinition(name="draft_update_node", description="Patch exactly one node; config patches merge by default.", input_schema={"type": "object", "properties": {"node_id": {"type": "string"}, "changes": object_schema, "merge_config": {"type": "boolean"}}, "required": ["node_id", "changes"]}),
            ToolDefinition(name="draft_remove_node", description="Remove one node and its incident edges.", input_schema={"type": "object", "properties": {"node_id": {"type": "string"}}, "required": ["node_id"]}),
            ToolDefinition(name="draft_connect", description="Connect two existing node ports with one edge.", input_schema={"type": "object", "properties": {"edge": EdgeSpec.model_json_schema()}, "required": ["edge"]}),
            ToolDefinition(name="draft_remove_edge", description="Remove one edge.", input_schema={"type": "object", "properties": {"edge_id": {"type": "string"}}, "required": ["edge_id"]}),
            ToolDefinition(name="draft_upsert_agent", description="Create or update one inline Claude Agent definition.", input_schema={"type": "object", "properties": {"agent": AgentSpec.model_json_schema()}, "required": ["agent"]}),
            ToolDefinition(name="draft_validate", description="Run graph, schema, port, agent-binding, and test-presence validation.", input_schema={"type": "object", "properties": {}}),
            ToolDefinition(name="test_add", description="Add one traceable workflow acceptance test.", input_schema={"type": "object", "properties": {"test": WorkflowTestCase.model_json_schema()}, "required": ["test"]}),
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

    def _consume(self, build_id: str, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        self.active.pop(build_id, None)
