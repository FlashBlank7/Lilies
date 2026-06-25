from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from .config import Settings
from .models import (
    AgentSpec,
    ChatMessage,
    ContentBlock,
    GenerationRequest,
    PermissionMode,
    ToolDefinition,
)
from .prompts import AGENT_GENERATOR_PROMPT, build_generation_request
from .providers import ModelProvider
from .runtime import AgentRuntime
from .sandbox import SandboxManager
from .storage import Storage
from .tools import ToolRegistry


class AgentFactory:
    def __init__(
        self,
        *,
        settings: Settings,
        storage: Storage,
        provider: ModelProvider,
        runtime: AgentRuntime,
        tools: ToolRegistry,
        sandboxes: SandboxManager,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.provider = provider
        self.runtime = runtime
        self.tools = tools
        self.sandboxes = sandboxes

    async def generate(self, generation_id: str, request: GenerationRequest) -> None:
        await self.storage.update_generation(generation_id, status="generating")
        await self._emit(generation_id, "generation.started", {"requirement": request.requirement})
        try:
            spec = await self._generate_spec(generation_id, request.requirement)
            if request.validation_prompt:
                spec.validation.prompt = request.validation_prompt
            version = await self.storage.save_agent_version(spec, "draft")
            await self.storage.update_generation(
                generation_id, status="validating", agent_id=spec.id, agent_version=version
            )
            await self._emit(generation_id, "generation.spec.created", {
                "agent_id": spec.id,
                "version": version,
                "spec": spec.model_dump(mode="json"),
            })
            await self._validate(generation_id, spec, version, request.workspace_path)
            if request.auto_publish:
                await self.storage.publish_agent(spec.id, version)
                status = "published"
                await self._emit(generation_id, "generation.published", {
                    "agent_id": spec.id, "version": version
                })
            else:
                status = "draft"
            await self.storage.update_generation(generation_id, status=status)
            await self._emit(generation_id, "generation.completed", {
                "agent_id": spec.id, "version": version, "status": status
            })
        except Exception as error:
            await self.storage.update_generation(generation_id, status="failed", error=str(error))
            await self._emit(generation_id, "generation.failed", {
                "error": str(error), "error_type": type(error).__name__
            })

    async def _generate_spec(self, generation_id: str, requirement: str) -> AgentSpec:
        schema = AgentSpec.model_json_schema()
        definition = ToolDefinition(
            name="create_agent_spec",
            description="Create the complete platform-native agent specification.",
            input_schema=schema,
        )
        messages = [ChatMessage(role="user", content=[ContentBlock(
            type="text", text=build_generation_request(requirement, self.tools.names())
        )])]
        validation_error = ""
        for attempt in range(3):
            if validation_error:
                messages.append(ChatMessage(role="user", content=[ContentBlock(
                    type="text",
                    text=f"The previous AgentSpec was invalid. Correct every issue and call create_agent_spec again:\n{validation_error}",
                )]))
            stream = self.provider.stream(
                model=self.settings.deepseek_generator_model,
                system=AGENT_GENERATOR_PROMPT,
                messages=messages,
                tools=[definition],
                max_output_tokens=8_192,
                thinking_enabled=True,
                effort="high",
                # DeepSeek thinking mode rejects forced tool_choice. The generator
                # prompt requires this call and the repair loop enforces it.
                tool_choice={"type": "auto"},
                user_id=generation_id,
            )
            response = await self.runtime._collect_stream(
                generation_id, stream, "generation.model", self.settings.deepseek_generator_model
            )
            messages.append(ChatMessage(role="assistant", content=response.blocks))
            call = next(
                (block for block in response.blocks if block.type == "tool_use" and block.name == "create_agent_spec"),
                None,
            )
            if not call:
                validation_error = "create_agent_spec was not called"
                continue
            try:
                raw = dict(call.input or {})
                raw["id"] = str(uuid4())
                profile = raw.setdefault("provider_profile", {})
                profile["provider"] = "deepseek"
                profile.setdefault("model", self.settings.deepseek_runtime_model)
                spec = AgentSpec.model_validate(raw)
                self._validate_spec_capabilities(spec)
                return spec
            except (ValidationError, ValueError) as error:
                validation_error = str(error)
                await self._emit(generation_id, "generation.spec.repair", {
                    "attempt": attempt + 1, "validation_error": validation_error
                })
        raise RuntimeError(f"could not produce a valid AgentSpec: {validation_error}")

    def _validate_spec_capabilities(self, spec: AgentSpec) -> None:
        unknown = set(spec.tools) - set(self.tools.names())
        if unknown:
            raise ValueError(f"unknown tools: {sorted(unknown)}")
        if not spec.tools:
            raise ValueError("generated agent must select at least one tool")
        if spec.provider_profile.provider != "deepseek":
            raise ValueError("only the deepseek provider is currently installed")
        if spec.permission_mode in {PermissionMode.bypass, PermissionMode.plan}:
            spec.permission_mode = PermissionMode.default

    async def _validate(
        self, generation_id: str, spec: AgentSpec, version: int, workspace_path: str | None
    ) -> None:
        if workspace_path is None:
            workspace_path = f"generated/{generation_id}"
            workspace = self.sandboxes.resolve_workspace(workspace_path, create=True)
            readme = workspace / "README.md"
            if not readme.exists():
                readme.write_text(
                    f"# Validation workspace\n\nGenerated for agent requirement: {spec.description}\n",
                    encoding="utf-8",
                )
        await self._emit(generation_id, "generation.validation.started", {
            "workspace_path": workspace_path, "prompt": spec.validation.prompt
        })
        validation_spec = spec.model_copy(deep=True)
        validation_spec.permission_mode = PermissionMode.bypass
        session = await self.runtime.create_session(
            validation_spec, version, workspace_path, session_id=f"validation-{generation_id}"
        )
        try:
            answer = await self.runtime.run_turn_and_wait(session, spec.validation.prompt)
            if not answer.strip():
                raise RuntimeError("validation agent returned no final response")
            command_results: list[dict[str, Any]] = []
            for command in spec.validation.commands:
                result = await session.sandbox.run(["bash", "-lc", command], timeout=300)
                command_results.append({
                    "command": command,
                    "exit_code": result.exit_code,
                    "stdout": result.stdout[:5000],
                    "stderr": result.stderr[:5000],
                })
                if result.exit_code != 0:
                    raise RuntimeError(
                        f"validation command failed ({result.exit_code}): {command}\n{result.stderr[:1000]}"
                    )
            await self._emit(generation_id, "generation.validation.completed", {
                "session_id": session.id,
                "answer": answer[:20_000],
                "commands": command_results,
            })
        finally:
            await self.sandboxes.remove(session.id)

    async def _emit(self, generation_id: str, kind: str, data: dict[str, Any]) -> None:
        await self.storage.append_event(generation_id, kind, data)
