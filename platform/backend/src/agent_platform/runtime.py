from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from .config import Settings
from .models import (
    AgentSpec,
    ChatMessage,
    ContentBlock,
    ModelResponse,
    StreamEvent,
    Usage,
)
from .permissions import PermissionBroker
from .platform_harness import PlatformHarness
from .prompts import build_system_prompt
from .providers import ModelProvider, ProviderError
from .sandbox import SandboxManager, SandboxSession
from .storage import Storage
from .tools import ToolContext, ToolRegistry


INVALID_TOOL_INPUT_JSON_KEY = "_invalid_tool_input_json"


@dataclass(slots=True)
class SessionRuntime:
    id: str
    agent: AgentSpec
    agent_version: int
    workspace_path: str
    sandbox: SandboxSession
    messages: list[ChatMessage] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_task: asyncio.Task[None] | None = None


class AgentRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        storage: Storage,
        provider: ModelProvider,
        tools: ToolRegistry,
        sandboxes: SandboxManager,
        permissions: PermissionBroker,
        harness: PlatformHarness,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.provider = provider
        self.tools = tools
        self.sandboxes = sandboxes
        self.permissions = permissions
        self.harness = harness
        self.sessions: dict[str, SessionRuntime] = {}
        self.event_relays: dict[str, Callable[[str, dict[str, Any]], Awaitable[None]]] = {}

    def register_event_relay(
        self, session_id: str, relay: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        self.event_relays[session_id] = relay

    def unregister_event_relay(self, session_id: str) -> None:
        self.event_relays.pop(session_id, None)

    async def create_session(
        self, agent: AgentSpec, version: int, workspace_path: str, *, session_id: str | None = None
    ) -> SessionRuntime:
        session_id = session_id or str(uuid4())
        sandbox = await self.sandboxes.get_or_create(
            session_id,
            workspace_path,
            agent.network_policy,
            agent.network_allowlist,
        )
        session = SessionRuntime(
            id=session_id,
            agent=agent,
            agent_version=version,
            workspace_path=workspace_path,
            sandbox=sandbox,
        )
        self.sessions[session_id] = session
        await self.storage.create_session(session_id, agent.id, version, workspace_path)
        await self.emit(session_id, "session.started", {
            "session_id": session_id,
            "agent_id": agent.id,
            "agent_version": version,
            "workspace_path": workspace_path,
        })
        return session

    async def get_session(self, session_id: str) -> SessionRuntime:
        if session_id in self.sessions:
            return self.sessions[session_id]
        record = await self.storage.get_session(session_id)
        agent, version, _ = await self.storage.get_agent(record["agent_id"], record["agent_version"])
        sandbox = await self.sandboxes.get_or_create(
            session_id,
            record["workspace_path"],
            agent.network_policy,
            agent.network_allowlist,
        )
        session = SessionRuntime(
            id=session_id,
            agent=agent,
            agent_version=version,
            workspace_path=record["workspace_path"],
            sandbox=sandbox,
            messages=record["messages"],
            usage=Usage.model_validate(record["usage"] or {}),
        )
        self.sessions[session_id] = session
        return session

    async def start_turn(self, session_id: str, content: str) -> str:
        session = await self.get_session(session_id)
        if session.active_task and not session.active_task.done():
            raise RuntimeError("session already has an active turn")
        turn_id = str(uuid4())
        session.active_task = asyncio.create_task(self._run_turn(session, turn_id, content))
        session.active_task.add_done_callback(self._consume_background_exception)
        return turn_id

    async def run_turn_and_wait(self, session: SessionRuntime, content: str) -> str:
        turn_id = str(uuid4())
        await self._run_turn(session, turn_id, content, propagate=True)
        for message in reversed(session.messages):
            if message.role == "assistant":
                return "".join(block.text or "" for block in message.content if block.type == "text")
        return ""

    async def _run_turn(
        self, session: SessionRuntime, turn_id: str, content: str, *, propagate: bool = False
    ) -> None:
        async with session.lock:
            await self.storage.update_session(session.id, status="running")
            await self.emit(session.id, "turn.started", {"turn_id": turn_id})
            try:
                if session.agent.initial_prompt and not session.messages:
                    content = f"{session.agent.initial_prompt}\n\n{content}"
                session.messages.append(
                    ChatMessage(role="user", content=[ContentBlock(type="text", text=content)])
                )
                await self._run_loop(session, turn_id=turn_id, depth=0)
                await self.storage.update_session(
                    session.id,
                    status="ready",
                    messages=session.messages,
                    usage=session.usage.model_dump(mode="json"),
                )
                await self.emit(session.id, "turn.completed", {
                    "turn_id": turn_id,
                    "usage": session.usage.model_dump(mode="json"),
                })
            except asyncio.CancelledError:
                await self.storage.update_session(session.id, status="ready", messages=session.messages)
                await self.emit(session.id, "turn.cancelled", {"turn_id": turn_id})
                raise
            except Exception as error:
                await self.storage.update_session(session.id, status="error", messages=session.messages)
                await self.emit(session.id, "turn.failed", {
                    "turn_id": turn_id,
                    "error": str(error),
                    "error_type": type(error).__name__,
                })
                if propagate:
                    raise

    async def _run_loop(self, session: SessionRuntime, *, turn_id: str, depth: int) -> None:
        definitions = self.tools.definitions_for(session.agent)
        system = build_system_prompt(
            session.agent, session.sandbox.workspace, [item.name for item in definitions]
        )
        for iteration in range(1, session.agent.max_turns + 1):
            await self._compact_if_needed(session, system)
            response = await self._request_model(
                session,
                system=system,
                tools=definitions,
                event_prefix="model",
            )
            session.messages.append(ChatMessage(role="assistant", content=response.blocks))
            self._add_usage(session.usage, response.usage)
            if session.agent.max_budget_usd and session.usage.cost_usd > session.agent.max_budget_usd:
                raise RuntimeError("agent budget exceeded")

            tool_calls = [block for block in response.blocks if block.type == "tool_use"]
            if not tool_calls:
                return
            await self.emit(session.id, "agent.iteration", {
                "turn_id": turn_id, "iteration": iteration, "tool_count": len(tool_calls)
            })
            semaphore = asyncio.Semaphore(self.settings.max_parallel_tools)

            async def execute(block: ContentBlock) -> ContentBlock:
                async with semaphore:
                    return await self._execute_tool(session, block, depth)

            results = await asyncio.gather(*(execute(block) for block in tool_calls))
            session.messages.append(ChatMessage(role="user", content=results))
            await self.storage.update_session(
                session.id, messages=session.messages, usage=session.usage.model_dump(mode="json")
            )
        raise RuntimeError(f"maximum turns exceeded ({session.agent.max_turns})")

    async def _request_model(
        self,
        session: SessionRuntime,
        *,
        system: str,
        tools: list[Any],
        event_prefix: str,
        messages: list[ChatMessage] | None = None,
        tool_choice: dict[str, str] | None = None,
        model: str | None = None,
    ) -> ModelResponse:
        last_error: ProviderError | None = None
        for attempt in range(3):
            try:
                stream = self.provider.stream(
                    model=model or session.agent.provider_profile.model,
                    system=system,
                    messages=messages or session.messages,
                    tools=tools,
                    max_output_tokens=session.agent.provider_profile.max_output_tokens,
                    thinking_enabled=session.agent.provider_profile.thinking.enabled,
                    effort=session.agent.provider_profile.thinking.effort,
                    tool_choice=tool_choice,
                    user_id=session.id,
                )
                selected_model = model or session.agent.provider_profile.model
                return await self._collect_stream(session.id, stream, event_prefix, selected_model)
            except ProviderError as error:
                last_error = error
                if not error.retryable or attempt == 2:
                    raise
                delay = 2**attempt
                await self.emit(session.id, "model.retry", {"attempt": attempt + 1, "delay": delay})
                await asyncio.sleep(delay)
        raise last_error or RuntimeError("model request failed")

    async def _collect_stream(
        self,
        stream_id: str,
        stream: Any,
        event_prefix: str,
        model: str = "",
        timeout_seconds: float | None = None,
    ) -> ModelResponse:
        timeout = self.settings.deepseek_timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            if timeout > 0:
                async with asyncio.timeout(timeout):
                    return await self._collect_stream_unbounded(stream_id, stream, event_prefix, model)
            return await self._collect_stream_unbounded(stream_id, stream, event_prefix, model)
        except asyncio.TimeoutError as error:
            await self.emit(stream_id, f"{event_prefix}.timeout", {
                "model": model,
                "timeout_seconds": timeout,
            })
            raise ProviderError(
                f"model stream timed out after {timeout:g}s",
                retryable=True,
            ) from error
        except ProviderError as error:
            await self.emit(stream_id, f"{event_prefix}.failed", {
                "model": model,
                "error": str(error),
                "error_type": type(error).__name__,
                "retryable": error.retryable,
                "status_code": error.status_code,
            })
            raise

    async def _collect_stream_unbounded(
        self, stream_id: str, stream: Any, event_prefix: str, model: str = ""
    ) -> ModelResponse:
        blocks: dict[int, ContentBlock] = {}
        input_json: dict[int, str] = {}
        usage = Usage()
        stop_reason: str | None = None
        async for event in stream:
            assert isinstance(event, StreamEvent)
            data = event.data
            if event.type == "message_start":
                raw_usage = data.get("message", {}).get("usage", {})
                usage.input_tokens = raw_usage.get("input_tokens", 0)
                usage.cache_read_input_tokens = raw_usage.get("cache_read_input_tokens", 0)
                usage.cache_creation_input_tokens = raw_usage.get("cache_creation_input_tokens", 0)
            elif event.type == "content_block_start":
                index = int(data.get("index", len(blocks)))
                raw = data.get("content_block", {})
                block_type = raw.get("type")
                if block_type == "text":
                    blocks[index] = ContentBlock(type="text", text=raw.get("text", ""))
                elif block_type == "thinking":
                    blocks[index] = ContentBlock(type="thinking", thinking=raw.get("thinking", ""))
                elif block_type == "tool_use":
                    blocks[index] = ContentBlock(
                        type="tool_use", id=raw.get("id"), name=raw.get("name"), input=raw.get("input", {})
                    )
                    input_json[index] = ""
                    await self.emit(stream_id, "tool.requested", {
                        "tool_use_id": raw.get("id"), "tool": raw.get("name")
                    })
            elif event.type == "content_block_delta":
                index = int(data.get("index", 0))
                delta = data.get("delta", {})
                block = blocks.get(index)
                if delta.get("type") == "text_delta" and block:
                    value = delta.get("text", "")
                    block.text = (block.text or "") + value
                    await self.emit(stream_id, f"{event_prefix}.text.delta", {"text": value})
                elif delta.get("type") == "thinking_delta" and block:
                    value = delta.get("thinking", "")
                    block.thinking = (block.thinking or "") + value
                    await self.emit(stream_id, f"{event_prefix}.thinking.delta", {"thinking": value})
                elif delta.get("type") == "signature_delta" and block:
                    block.signature = (block.signature or "") + delta.get("signature", "")
                elif delta.get("type") == "input_json_delta":
                    input_json[index] = input_json.get(index, "") + delta.get("partial_json", "")
            elif event.type == "content_block_stop":
                index = int(data.get("index", 0))
                block = blocks.get(index)
                if block and block.type == "tool_use" and input_json.get(index):
                    try:
                        block.input = json.loads(input_json[index])
                    except json.JSONDecodeError as error:
                        raw = input_json[index]
                        block.input = {
                            INVALID_TOOL_INPUT_JSON_KEY: {
                                "error": str(error),
                                "raw_preview": raw[:2_000],
                                "raw_length": len(raw),
                            }
                        }
                        await self.emit(stream_id, "tool.input_json.invalid", {
                            "tool": block.name,
                            "error": str(error),
                            "raw_length": len(raw),
                            "raw_preview": raw[:500],
                        })
            elif event.type == "message_delta":
                stop_reason = data.get("delta", {}).get("stop_reason", stop_reason)
                usage.output_tokens = data.get("usage", {}).get("output_tokens", usage.output_tokens)
            elif event.type == "error":
                raise ProviderError(str(data.get("error", data)))
        response = ModelResponse(
            blocks=[blocks[index] for index in sorted(blocks)],
            stop_reason=stop_reason,
            usage=usage,
        )
        self._price_usage(response.usage, model)
        return response

    async def _execute_tool(
        self, session: SessionRuntime, block: ContentBlock, depth: int
    ) -> ContentBlock:
        tool_name = block.name or ""
        tool_input = block.input or {}
        try:
            invalid_json = tool_input.get(INVALID_TOOL_INPUT_JSON_KEY)
            if invalid_json is not None:
                error = (
                    invalid_json.get("error", "unknown parse error")
                    if isinstance(invalid_json, dict)
                    else "unknown parse error"
                )
                raise RuntimeError(
                    f"invalid tool input JSON for {tool_name}: {error}. "
                    "Re-emit this tool call with valid JSON arguments."
                )
            tool = self.tools.get(tool_name)
            allowed = {definition.name for definition in self.tools.definitions_for(session.agent)}
            if tool_name not in allowed:
                raise PermissionError(f"tool is not enabled: {tool_name}")
            self._enforce_tool_network_policy(
                session.agent,
                tool_name,
                tool_input,
                sandboxed_stdio=True,
            )
            self.harness.enforce_secret_policy(
                surface=f"agent_tool:{tool_name}",
                payload=tool_input,
            )
            tool_input = await self.permissions.request(
                session_id=session.id,
                mode=session.agent.permission_mode,
                tool_name=tool_name,
                tool_input=tool_input,
                dangerous=tool.dangerous,
                mutating=tool.mutating,
                emit=lambda kind, data: self.emit(session.id, kind, data),
            )
            self.harness.enforce_secret_policy(
                surface=f"agent_tool:{tool_name}",
                payload=tool_input,
            )
            tool_input = await self.harness.inject_secret_references(
                owner_id=session.agent.id,
                payload=tool_input,
            )
            await self.emit(session.id, "tool.started", {
                "tool_use_id": block.id, "tool": tool_name, "input": self._redact(tool_input)
            })

            async def spawn(task: str, role: str | None) -> str:
                if depth >= self.settings.max_subagent_depth:
                    raise RuntimeError("maximum subagent depth reached")
                return await self._run_subagent(session, task, role, depth + 1)

            result = await tool.execute(
                tool_input,
                ToolContext(
                    session_id=session.id,
                    agent=session.agent,
                    sandbox=session.sandbox,
                    emit=lambda kind, data: self.emit(session.id, kind, data),
                    spawn_subagent=spawn,
                ),
            )
            await self.emit(session.id, "tool.completed" if not result.is_error else "tool.failed", {
                "tool_use_id": block.id,
                "tool": tool_name,
                "content": result.content[:20_000],
            })
            return ContentBlock(
                type="tool_result", tool_use_id=block.id, content=result.content, is_error=result.is_error
            )
        except Exception as error:
            await self.emit(session.id, "tool.failed", {
                "tool_use_id": block.id, "tool": tool_name, "error": str(error)
            })
            return ContentBlock(
                type="tool_result", tool_use_id=block.id, content=str(error), is_error=True
            )

    def _enforce_tool_network_policy(
        self,
        agent: AgentSpec,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        sandboxed_stdio: bool = False,
    ) -> None:
        if tool_name == "WebSearch":
            self.harness.enforce_network_egress_policy(
                surface="agent_tool:WebSearch",
                hostname="news.google.com",
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
                surface="agent_tool:MCP",
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
                surface=f"agent_tool:MCP:{server.name}",
                hostname=parsed.hostname,
            )

    async def _run_subagent(
        self, parent: SessionRuntime, task: str, role: str | None, depth: int
    ) -> str:
        subagent_id = str(uuid4())
        await self.emit(parent.id, "agent.spawned", {
            "agent_id": subagent_id, "role": role, "depth": depth, "task": task
        })
        spec = parent.agent.model_copy(deep=True)
        spec.name = role or f"{parent.agent.name} subagent"
        spec.max_turns = min(parent.agent.max_turns, 12)
        spec.allow_subagents = depth < self.settings.max_subagent_depth
        child = await self.create_session(
            spec,
            parent.agent_version,
            parent.workspace_path,
            session_id=subagent_id,
        )
        child.messages.append(ChatMessage(role="user", content=[ContentBlock(type="text", text=task)]))
        try:
            # The child gets its own SessionRuntime, sandbox container, permission scope,
            # messages, usage counter, lock and event stream. Only the workspace contents
            # are shared, matching Claude Code's forked-agent context boundary.
            await self._run_loop(child, turn_id=str(uuid4()), depth=depth)
            await self.storage.update_session(
                child.id,
                status="ready",
                messages=child.messages,
                usage=child.usage.model_dump(mode="json"),
            )
            final = ""
            for message in reversed(child.messages):
                if message.role == "assistant":
                    final = "".join(
                        block.text or "" for block in message.content if block.type == "text"
                    )
                    if final:
                        break
            await self.emit(parent.id, "agent.completed", {
                "agent_id": subagent_id,
                "depth": depth,
                "result": final[:20_000],
                "usage": child.usage.model_dump(mode="json"),
            })
            return final or "Subagent stopped without a final response."
        finally:
            await self.sandboxes.remove(subagent_id)

    async def _compact_if_needed(self, session: SessionRuntime, system: str) -> None:
        estimate = (len(system) + sum(len(message.model_dump_json()) for message in session.messages)) // 4
        threshold = int(
            (session.agent.provider_profile.context_window - session.agent.provider_profile.max_output_tokens)
            * 0.85
        )
        if estimate < threshold or len(session.messages) < 10:
            return
        await self.emit(session.id, "context.compaction.started", {
            "estimated_tokens": estimate, "threshold": threshold
        })
        cut = max(1, len(session.messages) - 6)
        if (
            cut > 0
            and session.messages[cut].role == "user"
            and any(block.type == "tool_result" for block in session.messages[cut].content)
        ):
            cut -= 1
        old, recent = session.messages[:cut], session.messages[cut:]
        summary_request = ChatMessage(role="user", content=[ContentBlock(
            type="text",
            text="Summarize this conversation for another agent. Preserve decisions, files changed, commands, errors, pending work, and exact technical facts.\n\n"
            + "\n".join(message.model_dump_json() for message in old),
        )])
        response = await self._request_model(
            session,
            system="You create loss-minimizing agent context summaries.",
            tools=[],
            event_prefix="compaction",
            messages=[summary_request],
        )
        summary = "".join(block.text or "" for block in response.blocks if block.type == "text")
        session.messages = [
            ChatMessage(role="user", content=[ContentBlock(type="text", text=f"<context_summary>\n{summary}\n</context_summary>")]),
            *recent,
        ]
        await self.emit(session.id, "context.compaction.completed", {
            "removed_messages": len(old), "summary_chars": len(summary)
        })

    async def emit(self, stream_id: str, kind: str, data: dict[str, Any]) -> None:
        await self.storage.append_event(stream_id, kind, data)
        relay = self.event_relays.get(stream_id)
        if relay:
            await relay(kind, data)

    def cancel(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session or not session.active_task or session.active_task.done():
            raise KeyError("active turn not found")
        session.active_task.cancel()

    @staticmethod
    def _redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "***" if any(
                    token in key.casefold()
                    for token in ("key", "secret", "token", "password", "authorization", "cookie", "credential")
                ) else AgentRuntime._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [AgentRuntime._redact(item) for item in value]
        return value

    @staticmethod
    def _consume_background_exception(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _add_usage(total: Usage, current: Usage) -> None:
        total.input_tokens += current.input_tokens
        total.output_tokens += current.output_tokens
        total.cache_read_input_tokens += current.cache_read_input_tokens
        total.cache_creation_input_tokens += current.cache_creation_input_tokens
        total.cost_usd += current.cost_usd

    @staticmethod
    def _price_usage(usage: Usage, model: str) -> None:
        # Current defaults; observability only. Provider billing remains authoritative.
        if "pro" in model:
            input_rate, output_rate = 0.435, 0.87
        else:
            input_rate, output_rate = 0.14, 0.28
        usage.cost_usd = (
            usage.input_tokens * input_rate + usage.output_tokens * output_rate
        ) / 1_000_000
