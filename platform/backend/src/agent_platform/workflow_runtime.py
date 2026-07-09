from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass
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
    Condition,
    EndConfig,
    HTTPConfig,
    HumanInputConfig,
    IfElseConfig,
    IterationConfig,
    LLMConfig,
    LoopConfig,
    ParameterExtractorConfig,
    ScheduleTriggerConfig,
    StartConfig,
    TemplateConfig,
    ToolConfig,
    VariableAggregatorConfig,
    VariableAssignerConfig,
)
from .models import AgentSpec, ChatMessage, ContentBlock, PermissionMode, Usage
from .platform_harness import PlatformHarness
from .providers import ModelProvider
from .runtime import AgentRuntime
from .sandbox import SandboxManager
from .storage import Storage
from .tools import ToolContext, ToolRegistry
from .workflow_models import (
    ApplicationSnapshot,
    ErrorStrategy,
    NodeSpec,
    WorkflowRunRequest,
    WorkflowRunState,
    WorkflowSpec,
)
from .workflow_storage import WorkflowStorage


class HumanInputPause(RuntimeError):
    pass


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
        self.active_tasks: dict[str, asyncio.Task[None]] = {}

    async def create_run(
        self,
        application_id: str,
        request: WorkflowRunRequest,
        *,
        parent_task_id: str | None = None,
        origin: str = "api",
    ) -> dict[str, Any]:
        if request.use_draft:
            draft = await self.workflow_store.get_draft(application_id)
            snapshot, version, draft_revision = draft["snapshot"], None, int(draft["revision"])
        else:
            published = await self.workflow_store.get_version(application_id, request.version)
            snapshot, version, draft_revision = published["snapshot"], int(published["version"]), None
        errors = self.blocks.validate_workflow(snapshot.workflow)
        if errors:
            raise ValueError("invalid workflow: " + "; ".join(errors))
        self.sandboxes.resolve_workspace(request.workspace_path)
        run_id = str(uuid4())
        state = WorkflowRunState(
            run_id=run_id,
            application_id=application_id,
            snapshot=snapshot,
            inputs=request.inputs,
            workspace_path=request.workspace_path,
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
                "workspace_path": request.workspace_path,
            },
        )
        self._start(state)
        return {"run_id": run_id, "status": "queued", "version": version, "draft_revision": draft_revision}

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
                else:
                    output = {"error": str(error), "branch": "error"}

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
            except Exception:
                if attempt == attempts:
                    raise
                await self._emit(run_id, "node.retry", {"node_id": scoped_id, "attempt": attempt + 1})
                await asyncio.sleep(node.retry.delay_seconds)
        raise RuntimeError("unreachable")

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
        context = {"inputs": inputs, "nodes": outputs}
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
        if isinstance(config, LLMConfig):
            prompt = str(self._resolve(config.prompt, context))
            text, usage = await self._model_text(
                run_id, config.model or self.runtime_model, config.system, prompt, scoped_id
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
            session = await self.agent_runtime.create_session(agent, version, workspace_path)
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
            return {"output": {key: self._resolve(value, context) for key, value in config.assignments.items()}}
        if isinstance(config, VariableAggregatorConfig):
            values = [self._resolve(value, context) for value in config.variables]
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
            return await self._http(config, context, owner_id=state.application_id if state else "")
        if isinstance(config, IterationConfig):
            items = self._resolve(config.items, context)
            if not isinstance(items, list):
                raise TypeError("iteration items must resolve to an array")
            semaphore = asyncio.Semaphore(config.parallelism)

            async def one(index: int, item: Any) -> Any:
                async with semaphore:
                    nested_inputs = {**inputs, config.item_name: item, "index": index}
                    nested = await self._run_graph(
                        snapshot,
                        config.workflow,
                        nested_inputs,
                        workspace_path,
                        run_id,
                        prefix=f"{scoped_id}[{index}].",
                    )
                    value: Any = nested.get(config.output_node_id)
                    for key in config.output_path:
                        value = value[key]
                    return value

            return {"items": await asyncio.gather(*(one(index, item) for index, item in enumerate(items)))}
        if isinstance(config, LoopConfig):
            variables = {key: self._resolve(value, context) for key, value in config.variables.items()}
            nested: dict[str, dict[str, Any]] = {}
            for index in range(config.max_iterations):
                nested = await self._run_graph(
                    snapshot,
                    config.workflow,
                    {**inputs, **variables, "iteration": index},
                    workspace_path,
                    run_id,
                    prefix=f"{scoped_id}[{index}].",
                )
                loop_context = {"inputs": {**inputs, **variables}, "nodes": nested}
                condition = config.break_condition.model_copy(
                    update={"value": self._resolve(config.break_value, loop_context)}
                )
                if self._evaluate(condition, loop_context):
                    return {"output": nested.get(config.output_node_id, {}), "iterations": index + 1}
                variables["previous"] = nested.get(config.output_node_id, {})
            raise RuntimeError(f"loop did not meet break condition after {config.max_iterations} iterations")
        if isinstance(config, HumanInputConfig):
            preset = inputs.get("__human__", {}).get(node.id) if isinstance(inputs.get("__human__"), dict) else None
            if preset is not None:
                return {"output": preset, **preset}
            if state and state.waiting_node_id == node.id and state.resumed_values is not None:
                values = state.resumed_values
                for field in config.fields:
                    if field.required and values.get(field.name) is None:
                        raise ValueError(f"missing required human input: {field.name}")
                return {"output": values, **values}
            if not state:
                raise RuntimeError("human input is only supported in persisted top-level runs")
            state.waiting_node_id = node.id
            await self._emit(run_id, "human_input.required", {
                "node_id": node.id,
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
            prompt = str(settings.get("prompt", json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value))
            tool_names = [str(t) for t in settings.get("tools", [])]
            system = str(settings.get("system") or "You are a precise coding agent runtime block.")
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
            return {
                "output": {"text": text, "usage": usage.model_dump(mode="json"), "tool_use_blocks": [], "stop_reason": None},
                "text": text,
                "state": {"mechanism": node.type},
            }

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
            effective_workspace = str(
                settings.get("workspace_path")
                or (
                    value.get("workspace")
                    if isinstance(value, dict) and value.get("workspace")
                    else workspace_path
                )
            )
            result = await self._execute_tool(
                ToolConfig(tool_name=str(tool_name), input=tool_input_value),
                snapshot,
                context,
                effective_workspace,
                run_id,
                scoped_id,
                owner_id=state.application_id if state else "",
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
            declared_workspace = str(settings.get("workspace", workspace_path))
            network_policy = str(settings.get("network_policy", "none"))
            effective_policy = network_policy if network_policy in {"none", "full", "allowlist"} else "none"
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
                max_turns=max_turns,
                max_budget_usd=float(max_budget_usd) if max_budget_usd is not None else None,
                allow_subagents=False,
            )
            version = await self.storage.save_agent_version(subagent, "workflow-subagent")
            session_id = f"{run_id}-{scoped_id}-subagent"
            session = await self.agent_runtime.create_session(
                subagent,
                version,
                str(settings.get("workspace_path") or workspace_path),
                session_id=session_id,
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

    async def _execute_tool(
        self,
        config: ToolConfig,
        snapshot: ApplicationSnapshot,
        context: dict[str, Any],
        workspace_path: str,
        run_id: str,
        node_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        if config.tool_name.startswith("workflow:"):
            application_id = config.tool_name.split(":", 1)[1]
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
            )
            await self.active_tasks[nested["run_id"]]
            record = await self.workflow_store.get_run(nested["run_id"])
            if record["status"] != "succeeded":
                raise RuntimeError(
                    f"nested workflow {application_id} ended with {record['status']}: {record.get('error') or ''}"
                )
            return {"output": record["outputs"], "run_id": nested["run_id"]}
        tool = self.tools.get(config.tool_name)
        agent = AgentSpec(
            name=f"Workflow tool {config.tool_name}",
            description="Executes one tool from a validated workflow.",
            system_prompt="Execute the configured workflow tool exactly and return its result.",
            tools=[config.tool_name],
            permission_mode=PermissionMode.bypass,
        )
        session_id = f"workflow-{run_id}-{node_id}"
        sandbox = None
        if config.tool_name != "WebSearch":
            sandbox = await self.sandboxes.get_or_create(session_id, workspace_path, agent.network_policy, [])

        async def no_subagent(_: str, __: str | None) -> str:
            raise RuntimeError("subagents are not available inside a single Tool block")

        try:
            resolved_input = self._resolve(config.input, context)
            self._enforce_tool_network_policy(config.tool_name, resolved_input, agent)
            self.harness.enforce_secret_policy(
                surface=f"workflow_tool:{config.tool_name}",
                payload=resolved_input,
            )
            injected_input = await self.harness.inject_secret_references(
                owner_id=owner_id,
                payload=resolved_input,
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
        self, tool_name: str, tool_input: dict[str, Any], agent: AgentSpec
    ) -> None:
        if tool_name == "WebSearch":
            self.harness.enforce_network_egress_policy(
                surface="workflow_tool:WebSearch",
                hostname="news.google.com",
            )
            return
        if tool_name != "MCP":
            return
        server_name = str(tool_input.get("server", ""))
        server = next((item for item in agent.mcp_servers if item.name == server_name), None)
        if not server or server.transport != "http" or not server.url:
            return
        parsed = urlparse(server.url)
        if parsed.hostname:
            self.harness.enforce_network_egress_policy(
                surface=f"workflow_tool:MCP:{server.name}",
                hostname=parsed.hostname,
            )

    async def _http(self, config: HTTPConfig, context: dict[str, Any], *, owner_id: str) -> dict[str, Any]:
        url = str(self._resolve(config.url, context))
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("HTTP block requires an http or https URL")
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
        )
        headers = {key: str(value) for key, value in injected_headers.items()}
        query = await self.harness.inject_secret_references(
            owner_id=owner_id,
            payload=query,
        )
        body = await self.harness.inject_secret_references(
            owner_id=owner_id,
            payload=body,
        )
        async with httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=True) as client:
            response = await client.request(
                config.method, url, headers=headers, params=query, json=body if body is not None else None
            )
        content_type = response.headers.get("content-type", "")
        value: Any = response.json() if "json" in content_type else response.text
        if response.is_error:
            raise RuntimeError(f"HTTP {response.status_code}: {str(value)[:1000]}")
        return {"output": value, "status": response.status_code, "headers": dict(response.headers)}

    async def run_test_suite(self, application_id: str) -> dict[str, Any]:
        validation = await self.applications.validate_draft(application_id)
        if not validation["valid"]:
            return {
                "passed": False,
                "validation": validation,
                "summary": {"total": 0, "passed": 0, "failed": 0, "mandatory_failed": 0, "frames": []},
                "tests": [],
            }
        draft = await self.workflow_store.get_draft(application_id)
        snapshot: ApplicationSnapshot = draft["snapshot"]
        test_task_id = f"test-suite:{uuid4()}"
        await self.harness.start_task(
            test_task_id,
            kind="test_suite",
            owner_id=application_id,
            resource_id=application_id,
            metadata={"draft_revision": draft["revision"], "content_hash": draft["content_hash"]},
        )
        results: list[dict[str, Any]] = []
        for test in snapshot.tests:
            created = await self.create_run(
                application_id,
                WorkflowRunRequest(inputs=test.inputs, use_draft=True, workspace_path="."),
                parent_task_id=test_task_id,
                origin="test_suite",
            )
            run_id = created["run_id"]
            task = self.active_tasks[run_id]
            await task
            record = await self.workflow_store.get_run(run_id)
            workflow_events = await self.storage.list_events(run_id)
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
                    actual: Any = record["outputs"]
                    for key in assertion.path:
                        actual = actual[key]
                    passed = self._assert(actual, assertion.operator, assertion.expected)
                    assertions.append({"passed": passed, "actual": actual, **assertion.model_dump(mode="json")})
                except Exception as error:
                    assertions.append({"passed": False, "error": str(error), **assertion.model_dump(mode="json")})
            failed_checks: list[str] = []
            if record["status"] != "succeeded":
                failed_checks.append(f"run status is {record['status']}")
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
                "feedback_hints": test.feedback_hints,
            }
            results.append({
                "test_id": test.id,
                "name": test.name,
                "mandatory": test.mandatory,
                "passed": passed,
                "run_id": run_id,
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
        if passed:
            await self.workflow_store.mark_tested(
                application_id, draft["revision"], draft["content_hash"], report
            )
        await self.harness.finish_task(test_task_id, status="succeeded" if passed else "failed")
        await self._emit(application_id, "tests.completed", report)
        return report

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
            return True
        return outputs[edge.source].get("branch") == edge.branch

    @staticmethod
    def _terminal_outputs(workflow: WorkflowSpec, outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        terminal = [node for node in workflow.nodes if node.type in {"end", "answer"} and node.id in outputs]
        if len(terminal) == 1:
            return outputs[terminal[0].id]
        return {node.id: outputs[node.id] for node in terminal}

    @classmethod
    def _resolve(cls, value: Any, context: dict[str, Any]) -> Any:
        if isinstance(value, dict) and set(value) == {"$ref"}:
            reference = value["$ref"]
            if reference.get("node_id") == "$inputs":
                current: Any = context["inputs"]
            else:
                if reference["node_id"] not in context["nodes"] and reference.get("optional"):
                    return None
                current = context["nodes"][reference["node_id"]]
            for key in reference.get("path", []):
                current = current[int(key)] if isinstance(current, list) else current[key]
            return current
        if isinstance(value, dict):
            return {key: cls._resolve(item, context) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._resolve(item, context) for item in value]
        return value

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
    def _json_from_text(text: str) -> Any:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.I)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            match = re.search(r"(\{.*\}|\[.*\])", stripped, re.S)
            if not match:
                raise ValueError("model did not return valid JSON")
            return json.loads(match.group(1))

    @staticmethod
    def _json_type(value: str) -> str:
        return {"file": "object", "file_list": "array", "any": "string"}.get(value, value)

    @staticmethod
    def _assert(actual: Any, operator: str, expected: Any) -> bool:
        if operator == "exists":
            return actual is not None
        if operator == "equals":
            return actual == expected
        if operator == "contains":
            return expected in actual if actual is not None else False
        if operator == "not_contains":
            return expected not in actual if actual is not None else True
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
