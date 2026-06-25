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
        self.applications = applications
        self.blocks = blocks
        self.provider = provider
        self.agent_runtime = agent_runtime
        self.tools = tools
        self.sandboxes = sandboxes
        self.runtime_model = runtime_model
        self.active_tasks: dict[str, asyncio.Task[None]] = {}

    async def create_run(self, application_id: str, request: WorkflowRunRequest) -> dict[str, Any]:
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
        self._start(state)
        return {"run_id": run_id, "status": "queued", "version": version, "draft_revision": draft_revision}

    async def resume(self, run_id: str, values: dict[str, Any]) -> dict[str, Any]:
        record = await self.workflow_store.get_run(run_id)
        if record["status"] != "paused":
            raise RuntimeError(f"workflow run is not paused: {record['status']}")
        state: WorkflowRunState = record["state"]
        state.resumed_values = values
        await self.workflow_store.update_run(run_id, status="queued", state=state)
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
        except HumanInputPause:
            await self._emit(state.run_id, "workflow.paused", {"node_id": state.waiting_node_id})
            # Persist paused as the final awaited action. Observers cannot see a
            # resumable state while this task is still emitting and be raced by
            # process shutdown cancellation.
            await self.workflow_store.update_run(state.run_id, status="paused", state=state)
        except asyncio.CancelledError:
            await self.workflow_store.update_run(state.run_id, status="cancelled", state=state)
            await self._emit(state.run_id, "workflow.cancelled", {})
            raise
        except Exception as error:
            await self.workflow_store.update_run(
                state.run_id, status="failed", state=state, error=str(error)
            )
            await self._emit(state.run_id, "workflow.failed", {
                "error": str(error), "error_type": type(error).__name__
            })

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
                output = {"error": str(error), "branch": "error"}
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
            return await self._execute_tool(config, snapshot, context, workspace_path, run_id, scoped_id)
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
            return await self._http(config, context)
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
            compacted = text if len(text) <= max_chars else text[: max_chars - 80] + "\n...[compacted]"
            result = {
                "summary": compacted,
                "dropped_chars": max(0, len(text) - len(compacted)),
                "preserved_facts": settings.get("preserved_facts", []),
            }
            await self._emit(run_id, "context.compaction.completed", {"node_id": scoped_id, **result})
            return {"output": result, "state": {"mechanism": node.type}}

        if node.type == "model_turn":
            prompt = str(settings.get("prompt", value))
            text, usage = await self._model_text(
                run_id,
                str(settings.get("model") or self.runtime_model),
                str(settings.get("system") or "You are a precise coding agent runtime block."),
                prompt,
                scoped_id,
            )
            return {"output": {"text": text, "usage": usage.model_dump(mode="json")}, "text": text, "state": {"mechanism": node.type}}

        if node.type == "tool_executor":
            tool_name = settings.get("tool_name")
            if not tool_name:
                raise ValueError("tool_executor.settings.tool_name is required")
            tool_input = settings.get("tool_input", value if isinstance(value, dict) else {"input": value})
            effective_workspace = str(
                settings.get("workspace_path")
                or (
                    value.get("workspace")
                    if isinstance(value, dict) and value.get("workspace")
                    else workspace_path
                )
            )
            result = await self._execute_tool(
                ToolConfig(tool_name=str(tool_name), input=tool_input),
                snapshot,
                context,
                effective_workspace,
                run_id,
                scoped_id,
            )
            return {"output": result["output"], "state": {"mechanism": node.type, "tool_name": tool_name}}

        if node.type == "permission_gate":
            preset = context["inputs"].get("__permissions__", {}) if isinstance(context["inputs"].get("__permissions__"), dict) else {}
            approved = bool(settings.get("auto_approve")) or bool(preset.get(node.id))
            if state and state.waiting_node_id == node.id and state.resumed_values is not None:
                approved = state.resumed_values.get("behavior") == "allow" or bool(state.resumed_values.get("approved"))
            if not approved:
                if not state:
                    raise RuntimeError("permission_gate requires persisted top-level runs when approval is not preset")
                state.waiting_node_id = node.id
                await self._emit(run_id, "permission.requested", {
                    "node_id": scoped_id,
                    "reason": settings.get("reason", "Sensitive action requires approval."),
                    "input": self._redact(value),
                })
                raise HumanInputPause()
            await self._emit(run_id, "permission.resolved", {"node_id": scoped_id, "behavior": "allow"})
            return {"output": value, "state": {"mechanism": node.type, "approved": True}}

        if node.type == "tool_result_normalizer":
            normalized = value
            if isinstance(value, str):
                try:
                    normalized = json.loads(value)
                except json.JSONDecodeError:
                    normalized = {"text": value}
            return {"output": normalized, "state": {"mechanism": node.type}}

        if node.type == "budget_gate":
            max_cost = settings.get("max_cost_usd")
            spent = float(settings.get("spent_cost_usd", 0))
            allowed = max_cost is None or spent <= float(max_cost)
            return {"output": value, "state": {"mechanism": node.type, "allowed": allowed, "spent_cost_usd": spent, "max_cost_usd": max_cost}}

        if node.type == "round_limit":
            current_round = int(settings.get("current_round", 0))
            max_rounds = int(settings.get("max_rounds", 30))
            return {"output": value, "state": {"mechanism": node.type, "allowed": current_round < max_rounds, "current_round": current_round, "max_rounds": max_rounds}}

        if node.type == "event_recorder":
            event = {"node_id": scoped_id, "label": settings.get("label", node.title), "payload": self._redact(value)}
            await self._emit(run_id, "agent_architecture.event", event)
            return {"output": value, "state": {"mechanism": node.type, "recorded": True}}

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

        state_payload = {"mechanism": node.type, **settings}
        if node.type == "retry_error_classifier":
            error_text = str(settings.get("error") or value or "")
            retryable = any(token in error_text.casefold() for token in ("timeout", "rate", "temporary", "retry"))
            state_payload.update({"class": "retryable" if retryable else "fatal", "retryable": retryable})
        elif node.type == "stop_continue_controller":
            reason = str(settings.get("stop_reason", ""))
            state_payload.update({"continue": reason in {"tool_use", "max_tokens", ""}, "stop_reason": reason})
        elif node.type == "sandbox_boundary":
            state_payload.update({
                "workspace": settings.get("workspace", workspace_path),
                "network_policy": settings.get("network_policy", "full"),
            })
            return {
                "output": {
                    "input": value,
                    "workspace": state_payload["workspace"],
                    "network_policy": state_payload["network_policy"],
                },
                "state": state_payload,
            }
        elif node.type == "dependency_gate":
            dependencies = settings.get("dependencies", [])
            completed = set(settings.get("completed", []))
            state_payload.update({"open_dependencies": [item for item in dependencies if item not in completed]})
        elif node.type == "mailbox_wait_wake":
            preset = context["inputs"].get("__mailbox__", {}) if isinstance(context["inputs"].get("__mailbox__"), dict) else {}
            messages = settings.get("messages") or preset.get(node.id) or []
            if state and state.waiting_node_id == node.id and state.resumed_values is not None:
                resumed_messages = state.resumed_values.get("messages", state.resumed_values)
                messages = resumed_messages if isinstance(resumed_messages, list) else [resumed_messages]
            if not messages:
                if not state:
                    raise RuntimeError("mailbox_wait_wake requires persisted top-level runs when no message is preset")
                state.waiting_node_id = node.id
                await self._emit(run_id, "mailbox.waiting", {
                    "node_id": scoped_id,
                    "input": self._redact(value),
                })
                raise HumanInputPause()
            await self._emit(run_id, "mailbox.woke", {"node_id": scoped_id, "messages": self._redact(messages)})
            state_payload.update({"awake": True, "messages": messages})
        elif node.type == "checkpoint_resume":
            state_payload.update({"checkpoint": settings.get("checkpoint_id", f"{run_id}:{scoped_id}")})
        elif node.type == "cancellation_point":
            state_payload.update({"cancelled": bool(settings.get("cancelled", False))})
        return {"output": value if value is not None else state_payload, "state": state_payload}

    @staticmethod
    def _incoming_value(node: NodeSpec, context: dict[str, Any]) -> Any:
        if not context["nodes"]:
            return None
        return next(reversed(context["nodes"].values()))

    async def _model_text(
        self, run_id: str, model: str, system: str, prompt: str, node_id: str
    ) -> tuple[str, Usage]:
        stream = self.provider.stream(
            model=model,
            system=system,
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text=prompt)])],
            tools=[],
            max_output_tokens=8_192,
            thinking_enabled=True,
            effort="high",
            user_id=run_id,
        )
        response = await self.agent_runtime._collect_stream(
            run_id, stream, f"node.{node_id}.model", model
        )
        text = "".join(block.text or "" for block in response.blocks if block.type == "text")
        return text, response.usage

    async def _execute_tool(
        self,
        config: ToolConfig,
        snapshot: ApplicationSnapshot,
        context: dict[str, Any],
        workspace_path: str,
        run_id: str,
        node_id: str,
    ) -> dict[str, Any]:
        if config.tool_name.startswith("workflow:"):
            application_id = config.tool_name.split(":", 1)[1]
            nested = await self.create_run(
                application_id,
                WorkflowRunRequest(inputs=self._resolve(config.input, context), workspace_path=workspace_path),
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
            await self._emit(run_id, f"node.{node_id}.tool.started", {
                "tool": config.tool_name, "input": self._redact(resolved_input)
            })
            result = await tool.execute(
                resolved_input,
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

    async def _http(self, config: HTTPConfig, context: dict[str, Any]) -> dict[str, Any]:
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
        headers = {key: str(self._resolve(value, context)) for key, value in config.headers.items()}
        query = {key: self._resolve(value, context) for key, value in config.query.items()}
        body = self._resolve(config.body, context)
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
            return {"passed": False, "validation": validation, "tests": []}
        draft = await self.workflow_store.get_draft(application_id)
        snapshot: ApplicationSnapshot = draft["snapshot"]
        results: list[dict[str, Any]] = []
        for test in snapshot.tests:
            created = await self.create_run(
                application_id,
                WorkflowRunRequest(inputs=test.inputs, use_draft=True, workspace_path="."),
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
            passed = (
                record["status"] == "succeeded"
                and all(item["passed"] for item in assertions)
                and required_node_types_passed
                and required_tool_nodes_passed
                and required_tools_passed
                and minimum_calls_passed
                and citation_passed
            )
            results.append({
                "test_id": test.id,
                "name": test.name,
                "mandatory": test.mandatory,
                "passed": passed,
                "run_id": run_id,
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
        report = {"passed": passed, "validation": validation, "tests": results}
        if passed:
            await self.workflow_store.mark_tested(
                application_id, draft["revision"], draft["content_hash"], report
            )
        await self._emit(application_id, "tests.completed", report)
        return report

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
            return expected in actual
        if operator == "not_contains":
            return expected not in actual
        if operator == "type":
            names = {"string": str, "number": (int, float), "boolean": bool, "object": dict, "array": list}
            return isinstance(actual, names[str(expected)])
        return False

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
                key: "***" if any(token in key.casefold() for token in ("key", "secret", "token", "password")) else WorkflowRuntime._redact(item)
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
