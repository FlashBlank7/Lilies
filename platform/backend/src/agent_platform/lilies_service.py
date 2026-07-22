from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError

from .agent_core import (
    INVALID_TOOL_INPUT_JSON_KEY,
    add_usage,
    collect_model_stream,
    redact_sensitive_fields,
)
from .lilies_config import LiliesSettings
from .lilies_identity import build_lilies_system_prompt
from .lilies_models import (
    AssignmentNetworkPolicy,
    BuildAssignment,
    CredentialKind,
    PermissionDecisionRequest,
    SessionCancelRequest,
    SessionCreateRequest,
    SessionMessageRequest,
    SessionResumeRequest,
)
from .lilies_platform_client import LiliesPlatformClient
from .lilies_platform_contract import operation_by_name
from .lilies_platform_tools import build_lilies_platform_registry
from .lilies_storage import LiliesConflictError, LiliesNotFoundError, LiliesStorage
from .lilies_tools import (
    LiliesTool,
    LiliesToolContext,
    LiliesToolRegistry,
    LiliesToolResult,
    build_lilies_core_registry,
)
from .models import ChatMessage, ContentBlock, ModelResponse, Usage
from .providers import ModelProvider, ProviderError
from .providers.deepseek import DeepSeekProvider


class LiliesServiceError(RuntimeError):
    pass


class LiliesBudgetExceeded(LiliesServiceError):
    pass


class LiliesDeadlineExceeded(LiliesServiceError):
    pass


@dataclass(slots=True)
class TurnMetrics:
    usage: Usage
    model_calls: int = 0
    tool_calls: int = 0

    @classmethod
    def from_checkpoint(cls, checkpoint: dict[str, Any]) -> TurnMetrics:
        raw = checkpoint.get("metrics", {})
        return cls(
            usage=Usage.model_validate(raw.get("usage", {})),
            model_calls=max(0, int(raw.get("model_calls", 0))),
            tool_calls=max(0, int(raw.get("tool_calls", 0))),
        )

    def checkpoint(self) -> dict[str, Any]:
        return {
            "usage": self.usage.model_dump(mode="json"),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
        }


@dataclass(frozen=True, slots=True)
class CumulativeMetrics:
    token_count: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    model_calls: int = 0


@dataclass(frozen=True, slots=True)
class CancellationDisposition:
    turn_status: str
    session_status: str
    reason: str
    preserve_waiting_state: bool = False


@dataclass(frozen=True, slots=True)
class LocalLiliesCore:
    """Minimal standalone core assembled without importing workflow product services."""

    storage: LiliesStorage
    provider: ModelProvider
    tools: LiliesToolRegistry
    service: LocalLiliesService


@dataclass(frozen=True, slots=True)
class _AssignmentToolBinding:
    fingerprint: str
    registry: LiliesToolRegistry


class LocalLiliesService:
    """Standalone durable Lilies loop, independent from all workflow platform services."""

    def __init__(
        self,
        settings: LiliesSettings,
        *,
        storage: LiliesStorage | None = None,
        provider: ModelProvider | None = None,
        tools: LiliesToolRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage or LiliesStorage(settings.data_dir)
        self.provider = provider or DeepSeekProvider(
            settings.deepseek_api_key,
            settings.deepseek_base_url,
            settings.model_timeout_seconds,
        )
        self.tools = tools or build_lilies_core_registry()
        self.assignment_tool_bindings: dict[str, _AssignmentToolBinding] = {}
        self.active_turns: dict[str, asyncio.Task[None]] = {}
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.permission_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.cancellation_dispositions: dict[str, CancellationDisposition] = {}
        self.stopping = False
        self.recovery_summary: dict[str, int] = {}

    async def initialize(self) -> dict[str, int]:
        self.settings.prepare()
        self.recovery_summary = await self.storage.initialize()
        return self.recovery_summary

    async def tool_registry_for_session(
        self,
        session_id: str,
        *,
        session: dict[str, Any] | None = None,
    ) -> LiliesToolRegistry:
        """Resolve the least-privilege tool surface for one durable session.

        Ordinary sessions retain only the local registry supplied at service
        construction.  A platform assignment resolves its bearer from the
        daemon's private credential store and constructs HTTP adapters without
        importing any platform workflow service or database implementation.
        """

        current = session or await self.storage.get_session(session_id)
        raw_assignment = current.get("assignment")
        if raw_assignment is None:
            return self.tools
        try:
            assignment = BuildAssignment.model_validate(raw_assignment)
        except ValidationError as error:
            raise LiliesServiceError("persisted BuildAssignment is invalid") from error
        assignment_id = str(assignment.assignment_id)
        if current.get("assignment_id") != assignment_id:
            raise LiliesServiceError("session and BuildAssignment identifiers do not match")
        stored_digest = current.get("platform_contract_digest")
        effective_digest = stored_digest or assignment.platform.contract_digest
        if not isinstance(effective_digest, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", effective_digest
        ) is None:
            raise LiliesServiceError("session platform contract digest is invalid")

        policy = assignment.constraints.network_policy
        platform_host = (assignment.platform.base_url.host or "").casefold()
        if policy is AssignmentNetworkPolicy.none:
            raise LiliesServiceError("assignment network policy denies the platform connection")
        if policy is AssignmentNetworkPolicy.allowlist and platform_host not in {
            host.casefold() for host in assignment.constraints.allowed_hosts
        }:
            raise LiliesServiceError("platform host is absent from the assignment allowlist")

        credential = await self.storage.get_credential(
            assignment.platform.credential_ref,
            assignment_id=assignment_id,
        )
        if credential.get("kind") != CredentialKind.platform_assignment.value:
            raise LiliesServiceError("assignment credential has the wrong kind")
        assignment_scopes = {scope.value for scope in assignment.platform.scopes}
        credential_scopes = {str(scope) for scope in credential.get("scopes", [])}
        if credential_scopes != assignment_scopes:
            raise LiliesServiceError("assignment credential scopes do not match the assignment")

        allowed_operations = {
            action.value for action in assignment.constraints.allowed_actions
        }
        if "platform_contract_get" not in allowed_operations:
            raise LiliesServiceError("assignment must allow platform_contract_get")
        for operation in allowed_operations:
            if str(operation_by_name(operation)["scope"]) not in assignment_scopes:
                raise LiliesServiceError(
                    f"assignment action {operation} is not covered by its credential scopes"
                )

        fingerprint = self._digest_json(
            {
                "assignment": assignment.model_dump(mode="json", exclude_none=True),
                "credential_ref": credential["credential_ref"],
                "credential_updated_at": credential.get("updated_at"),
            }
        )
        cached = self.assignment_tool_bindings.get(session_id)
        if cached is not None and cached.fingerprint == fingerprint:
            return cached.registry
        client = LiliesPlatformClient(
            base_url=str(assignment.platform.base_url),
            access_token=str(credential["value"]),
            assignment_id=assignment.assignment_id,
            session_id=session_id,
            contract_digest=effective_digest,
            require_contract_fetch=True,
        )
        registry = build_lilies_platform_registry(
            client,
            include_core_tools=False,
            allowed_operations=allowed_operations,
        )
        self.assignment_tool_bindings[session_id] = _AssignmentToolBinding(
            fingerprint=fingerprint,
            registry=registry,
        )
        if stored_digest is None:
            await self.storage.update_session_context(
                session_id,
                platform_contract_digest=assignment.platform.contract_digest,
            )
        return registry

    async def shutdown(self, *, reason: str = "daemon_shutdown") -> None:
        self.stopping = True
        tasks = list(self.active_turns.items())
        for session_id, task in tasks:
            self.cancellation_dispositions[session_id] = CancellationDisposition(
                turn_status="interrupted",
                session_status="interrupted",
                reason=reason,
                preserve_waiting_state=True,
            )
            task.cancel()
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)

    async def create_session(
        self,
        request: SessionCreateRequest,
        *,
        client_id: str,
    ) -> dict[str, Any]:
        session_id = str(
            uuid5(NAMESPACE_URL, f"lilies:session:{client_id}:{request.idempotency_key}")
        )
        request_fingerprint = self._digest_json(request.model_dump(mode="json"))
        config = {
            "kind": request.kind.value,
            "title": request.title,
            "create_request_digest": request_fingerprint,
            "max_turns": self.settings.default_max_turns,
            "max_model_calls": self.settings.default_max_turns,
            "max_tool_calls": self.settings.default_max_tool_calls,
            "max_tokens": self.settings.context_window * self.settings.default_max_turns,
            "max_budget_usd": self.settings.default_max_budget_usd,
            "deadline_seconds": self.settings.default_deadline_seconds,
            "workspace": str(self._workspace_for(session_id)),
        }
        try:
            session = await self.storage.create_session(
                session_id=session_id,
                agent_version=self.settings.agent_version,
                model_profile=self.settings.model,
                system_identity_version=self.settings.system_identity_version,
                config=config,
                profile={"model": self.settings.model},
                client_id=client_id,
            )
        except LiliesConflictError:
            session = await self.storage.get_session(session_id, client_id=client_id)
            if session["config"].get("create_request_digest") != request_fingerprint:
                raise LiliesConflictError(
                    "session idempotency key was reused with a different payload"
                ) from None
        self._workspace_for(session_id).mkdir(parents=True, exist_ok=True, mode=0o700)
        return session

    async def submit_message(
        self,
        session_id: str,
        request: SessionMessageRequest,
        *,
        client_id: str,
    ) -> dict[str, Any]:
        if self.stopping:
            raise LiliesConflictError("daemon is stopping")
        async with self._session_lock(session_id):
            session = await self.storage.get_session(session_id, client_id=client_id)
            message_id = str(request.message_id)
            existing = next(
                (
                    turn
                    for turn in await self.storage.list_turns(session_id)
                    if turn["idempotency_key"] == request.idempotency_key
                ),
                None,
            )
            if existing is not None:
                if existing["request_id"] != message_id:
                    raise LiliesConflictError(
                        "message idempotency key was reused with a different payload"
                    )
                persisted = next(
                    (
                        item
                        for item in await self.storage.list_messages(
                            session_id, client_id=client_id
                        )
                        if item["id"] == message_id
                    ),
                    None,
                )
                expected_content = [{"type": "text", "text": request.content}]
                if persisted is None or persisted["content"] != expected_content:
                    raise LiliesConflictError(
                        "message idempotency key was reused with a different payload"
                    )
                return {**existing, "replayed": True}
            if session["status"] not in {"ready", "error"}:
                raise LiliesConflictError(
                    f"session {session_id} cannot accept a message from {session['status']}"
                )
            content = [{"type": "text", "text": request.content}]
            try:
                message = await self.storage.add_message(
                    session_id,
                    "user",
                    content,
                    message_id=message_id,
                )
            except LiliesConflictError:
                matches = [
                    item
                    for item in await self.storage.list_messages(
                        session_id, client_id=client_id
                    )
                    if item["id"] == message_id
                ]
                if not matches or matches[0]["content"] != content:
                    raise LiliesConflictError(
                        "message_id was reused with different content"
                    ) from None
                message = matches[0]
            turn = await self.storage.create_turn(
                session_id,
                request_id=message_id,
                idempotency_key=request.idempotency_key,
                input_message_id=message["id"],
                checkpoint={"metrics": TurnMetrics(Usage()).checkpoint()},
            )
            if not turn.get("replayed"):
                self._start_turn_task(session_id, turn["id"])
            return turn

    async def resume_session(
        self,
        session_id: str,
        request: SessionResumeRequest,
        *,
        client_id: str,
    ) -> dict[str, Any]:
        async with self._session_lock(session_id):
            return await self._resume_session_locked(
                session_id,
                request,
                client_id=client_id,
            )

    async def _resume_session_locked(
        self,
        session_id: str,
        request: SessionResumeRequest,
        *,
        client_id: str,
    ) -> dict[str, Any]:
        session = await self.storage.get_session(session_id, client_id=client_id)
        expected = request.expected_status.value
        if session["status"] != expected:
            raise LiliesConflictError(
                f"session {session_id} expected {expected}, found {session['status']}"
            )
        if expected in {"waiting_permission", "waiting_collaboration"}:
            await self.storage.append_event(
                session_id,
                "session.resume_requested",
                {"status": expected, "reason": request.reason},
            )
            return {
                "id": None,
                "session_id": session_id,
                "status": expected,
                "replayed": False,
                "waiting": True,
            }
        await self._close_uncertain_tool_calls(session_id)
        message_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lilies:resume-message:{session_id}:{request.idempotency_key}",
            )
        )
        content = [
            {
                "type": "text",
                "text": (
                    "Resume this session after an explicit user request. Inspect the persisted "
                    "evidence before acting and never replay an uncertain side effect."
                    + (f" Reason: {request.reason}" if request.reason else "")
                ),
            }
        ]
        try:
            message = await self.storage.add_message(
                session_id,
                "user",
                content,
                message_id=message_id,
            )
        except LiliesConflictError:
            messages = await self.storage.list_messages(session_id, client_id=client_id)
            message = next(item for item in messages if item["id"] == message_id)
        turn_kwargs = {
            "request_id": f"resume:{request.idempotency_key}",
            "idempotency_key": request.idempotency_key,
            "input_message_id": message["id"],
            "checkpoint": {"metrics": TurnMetrics(Usage()).checkpoint()},
        }
        if expected == "interrupted":
            turn = await self.storage.create_resume_turn(session_id, **turn_kwargs)
        else:
            turn = await self.storage.create_turn(session_id, phase="resume", **turn_kwargs)
        if not turn.get("replayed"):
            self._start_turn_task(session_id, turn["id"])
        return turn

    async def cancel_session(
        self,
        session_id: str,
        request: SessionCancelRequest,
        *,
        client_id: str,
    ) -> dict[str, Any]:
        session = await self.storage.get_session(session_id, client_id=client_id)
        if session["status"] == "cancelled":
            return session
        task = self.active_turns.get(session_id)
        self.cancellation_dispositions[session_id] = CancellationDisposition(
            turn_status="cancelled",
            session_status="cancelled",
            reason=request.reason,
        )
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return await self.storage.get_session(session_id, client_id=client_id)
        active_turn = next(
            (
                turn
                for turn in reversed(await self.storage.list_turns(session_id))
                if turn["status"]
                in {"running", "waiting_permission", "waiting_collaboration"}
            ),
            None,
        )
        if active_turn is not None:
            result = await self.storage.cancel_active_turn(
                active_turn["id"],
                reason=request.reason,
                session_status="cancelled",
            )
            return result["session"]
        return await self.storage.transition_session(
            session_id,
            "cancelled",
            reason=request.reason,
            expected_status=session["status"],
        )

    async def resolve_permission(
        self,
        session_id: str,
        request_id: str,
        request: PermissionDecisionRequest,
        *,
        client_id: str,
    ) -> dict[str, Any]:
        permission = await self.storage.get_permission_request(request_id)
        if permission["session_id"] != session_id:
            raise LiliesNotFoundError(f"permission request not found: {request_id}")
        if permission["input_digest"] != request.expected_input_digest:
            raise LiliesConflictError("permission input digest changed")
        approved_update = request.updated_input
        if approved_update is not None:
            tools = await self.tool_registry_for_session(session_id)
            tool = tools.get(permission["tool_name"])
            approved_update = tool.input_model.model_validate(approved_update).model_dump(
                mode="json"
            )
        resolved = await self.storage.resolve_permission_request(
            session_id,
            request_id,
            request.behavior,
            client_id=client_id,
            idempotency_key=request.idempotency_key,
            expected_input_digest=request.expected_input_digest,
            updated_input=approved_update,
            message=request.message,
        )
        waiter = self.permission_waiters.get(request_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(resolved)
        elif resolved["status"] in {"allowed", "denied"}:
            turn = await self.storage.get_turn(resolved["turn_id"])
            if turn["status"] == "running" and session_id not in self.active_turns:
                self._start_turn_task(
                    session_id,
                    turn["id"],
                    resume_permission=resolved,
                )
        return resolved

    async def request_stop(self, *, reason: str) -> int:
        self.stopping = True
        active_turns: list[dict[str, Any]] = []
        for session in await self.storage.list_sessions():
            if session["status"] not in {
                "running",
                "waiting_permission",
                "waiting_collaboration",
            }:
                continue
            active_turns.extend(
                turn
                for turn in await self.storage.list_turns(session["id"])
                if turn["status"]
                in {"running", "waiting_permission", "waiting_collaboration"}
            )
        active = len(active_turns)
        await self.storage.append_security_event(
            "daemon.stop_requested",
            {"reason": reason, "active_turns": active},
        )
        tasks: list[asyncio.Task[None]] = []
        for session_id, task in list(self.active_turns.items()):
            if task.done():
                continue
            self.cancellation_dispositions[session_id] = CancellationDisposition(
                turn_status="cancelled",
                session_status="interrupted",
                reason="daemon_stop",
                preserve_waiting_state=False,
            )
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A restored collaboration wait may have no in-memory task. The durable
        # sweep gives explicit stop the same persisted result for every active turn.
        for snapshot in active_turns:
            current = await self.storage.get_turn(snapshot["id"])
            if current["status"] not in {
                "running",
                "waiting_permission",
                "waiting_collaboration",
            }:
                continue
            metrics = TurnMetrics.from_checkpoint(current["checkpoint"])
            await self.storage.cancel_active_turn(
                current["id"],
                reason="daemon_stop",
                session_status="interrupted",
                token_count=self._token_total(metrics.usage),
                cost_usd=metrics.usage.cost_usd,
                tool_count=metrics.tool_calls,
                model_call_count=metrics.model_calls,
            )
        return active

    async def status(self) -> dict[str, Any]:
        clients = await self.storage.list_clients()
        sessions = await self.storage.list_sessions()
        active_clients = [item for item in clients if self._client_is_active(item)]
        return {
            "provider": self.provider.name,
            "model": self.settings.model,
            "paired_client_count": len(active_clients),
            "platform_paired": any(
                item.get("name") == "platform" for item in active_clients
            ),
            "active_session_count": len(
                [
                    item
                    for item in sessions
                    if item["status"]
                    in {"running", "waiting_permission", "waiting_collaboration"}
                ]
            ),
            "active_assignment_count": len(
                [
                    item
                    for item in sessions
                    if item.get("assignment_id")
                    and item["status"]
                    in {
                        "ready",
                        "running",
                        "waiting_permission",
                        "waiting_collaboration",
                        "interrupted",
                        "error",
                    }
                ]
            ),
            "stopping": self.stopping,
        }

    def _start_turn_task(
        self,
        session_id: str,
        turn_id: str,
        *,
        resume_permission: dict[str, Any] | None = None,
    ) -> None:
        existing = self.active_turns.get(session_id)
        if existing and not existing.done():
            raise LiliesConflictError("session already has an in-process active turn")
        task = asyncio.create_task(
            self._run_turn(session_id, turn_id, resume_permission=resume_permission),
            name=f"lilies-turn:{turn_id}",
        )
        self.active_turns[session_id] = task
        task.add_done_callback(lambda completed: self._consume_turn_task(session_id, completed))

    async def _run_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        resume_permission: dict[str, Any] | None = None,
    ) -> None:
        turn = await self.storage.get_turn(turn_id)
        metrics = TurnMetrics.from_checkpoint(turn["checkpoint"])
        try:
            if resume_permission is not None:
                session = await self.storage.get_session(session_id)
                self._enforce_limits(
                    session["config"],
                    self._turn_deadline(session["config"], turn),
                    self._cumulative_metrics(session),
                    metrics,
                )
                result = await self._complete_resolved_permission(
                    session_id,
                    turn,
                    resume_permission,
                    metrics,
                )
                await self._add_tool_result_message(session_id, turn_id, result)
            await self._run_model_loop(session_id, turn_id, metrics)
            await self.storage.finish_turn(
                turn_id,
                "completed",
                token_count=self._token_total(metrics.usage),
                cost_usd=metrics.usage.cost_usd,
                tool_count=metrics.tool_calls,
                model_call_count=metrics.model_calls,
            )
        except asyncio.CancelledError:
            disposition = self.cancellation_dispositions.pop(
                session_id,
                CancellationDisposition(
                    turn_status="interrupted",
                    session_status="interrupted",
                    reason="task_cancelled",
                ),
            )
            current = await self.storage.get_turn(turn_id)
            if not (
                disposition.preserve_waiting_state
                and current["status"] in {"waiting_permission", "waiting_collaboration"}
            ) and current["status"] in {
                "running",
                "waiting_permission",
                "waiting_collaboration",
            }:
                if disposition.turn_status == "cancelled":
                    await self.storage.cancel_active_turn(
                        turn_id,
                        reason=disposition.reason,
                        session_status=disposition.session_status,
                        token_count=self._token_total(metrics.usage),
                        cost_usd=metrics.usage.cost_usd,
                        tool_count=metrics.tool_calls,
                        model_call_count=metrics.model_calls,
                    )
                else:
                    await self.storage.finish_turn(
                        turn_id,
                        disposition.turn_status,
                        session_status=disposition.session_status,
                        token_count=self._token_total(metrics.usage),
                        cost_usd=metrics.usage.cost_usd,
                        tool_count=metrics.tool_calls,
                        model_call_count=metrics.model_calls,
                        interruption_reason=disposition.reason,
                    )
            raise
        except Exception as error:
            current = await self.storage.get_turn(turn_id)
            if current["status"] in {
                "running",
                "waiting_permission",
                "waiting_collaboration",
            }:
                await self.storage.finish_turn(
                    turn_id,
                    "error",
                    token_count=self._token_total(metrics.usage),
                    cost_usd=metrics.usage.cost_usd,
                    tool_count=metrics.tool_calls,
                    model_call_count=metrics.model_calls,
                    error=self._safe_error(error),
                )

    async def _run_model_loop(
        self,
        session_id: str,
        turn_id: str,
        metrics: TurnMetrics,
    ) -> None:
        session = await self.storage.get_session(session_id)
        config = session["config"]
        baseline = self._cumulative_metrics(session)
        turn = await self.storage.get_turn(turn_id)
        deadline = self._turn_deadline(config, turn)
        while True:
            self._enforce_limits(
                config,
                deadline,
                baseline,
                metrics,
                before_model=True,
            )
            await self._compact_if_needed(session_id, session)
            session = await self.storage.get_session(session_id)
            tools = await self.tool_registry_for_session(session_id, session=session)
            messages = await self._model_messages(session_id)
            system = build_lilies_system_prompt(
                workspace=str(self._workspace_for(session_id)),
                tool_names=tools.names(),
                context_summary=session.get("context_summary") or None,
            )
            response = await self._request_model(
                session_id,
                system=system,
                messages=messages,
                turn_id=turn_id,
                deadline=deadline,
                baseline=baseline,
                metrics=metrics,
                max_model_calls=self._max_model_calls(config),
                tools=tools,
            )
            visible_blocks = [block for block in response.blocks if block.type != "thinking"]
            await self.storage.add_message(
                session_id,
                "assistant",
                [block.model_dump(mode="json", exclude_none=True) for block in visible_blocks],
                turn_id=turn_id,
            )
            add_usage(metrics.usage, response.usage)
            tool_calls = [block for block in visible_blocks if block.type == "tool_use"]
            await self._checkpoint(
                turn_id,
                "model_completed",
                metrics,
                {"stop_reason": response.stop_reason, "tool_count": len(tool_calls)},
            )
            self._enforce_limits(config, deadline, baseline, metrics)
            if not tool_calls:
                return
            for block in tool_calls:
                result = await self._execute_tool(
                    session_id,
                    turn_id,
                    block,
                    metrics,
                    config,
                    deadline,
                    baseline,
                    tools,
                )
                await self._add_tool_result_message(session_id, turn_id, result)
            session = await self.storage.get_session(session_id)

    async def _request_model(
        self,
        session_id: str,
        *,
        system: str,
        messages: list[ChatMessage],
        turn_id: str,
        deadline: datetime,
        baseline: CumulativeMetrics,
        metrics: TurnMetrics,
        max_model_calls: int,
        tools: LiliesToolRegistry,
    ) -> ModelResponse:
        last_error: ProviderError | None = None
        for attempt in range(1, 4):
            if baseline.model_calls + metrics.model_calls >= max_model_calls:
                raise LiliesBudgetExceeded(
                    f"maximum model turns exceeded ({max_model_calls})"
                )
            if datetime.now(timezone.utc) >= deadline:
                raise LiliesDeadlineExceeded("session deadline exceeded")
            metrics.model_calls += 1
            await self._checkpoint(
                turn_id,
                "model_requested",
                metrics,
                {"attempt": attempt},
            )
            try:
                stream = self.provider.stream(
                    model=self.settings.model,
                    system=system,
                    messages=messages,
                    tools=tools.definitions(),
                    max_output_tokens=self.settings.max_output_tokens,
                    thinking_enabled=True,
                    effort="high",
                    user_id=session_id,
                )
                remaining_seconds = max(
                    0.001,
                    (deadline - datetime.now(timezone.utc)).total_seconds(),
                )
                return await collect_model_stream(
                    stream,
                    emit=lambda kind, data: self._emit(session_id, kind, data),
                    event_prefix="model",
                    model=self.settings.model,
                    timeout_seconds=min(
                        self.settings.model_timeout_seconds,
                        remaining_seconds,
                    ),
                    expose_thinking=False,
                    price_estimates_usd_per_million={
                        self.settings.model: {
                            "input_tokens": self.settings.model_price_input_usd_per_million,
                            "output_tokens": self.settings.model_price_output_usd_per_million,
                        }
                    },
                )
            except ProviderError as error:
                last_error = error
                if datetime.now(timezone.utc) >= deadline:
                    raise LiliesDeadlineExceeded("session deadline exceeded") from error
                if not error.retryable or attempt == 3:
                    raise
                delay = 2 ** (attempt - 1)
                await self._emit(
                    session_id,
                    "model.retry",
                    {"attempt": attempt, "delay_seconds": delay},
                )
                await asyncio.sleep(delay)
        raise last_error or LiliesServiceError("model request failed")

    async def _execute_tool(
        self,
        session_id: str,
        turn_id: str,
        block: ContentBlock,
        metrics: TurnMetrics,
        config: dict[str, Any],
        deadline: datetime,
        baseline: CumulativeMetrics,
        tools: LiliesToolRegistry,
    ) -> ContentBlock:
        metrics.tool_calls += 1
        self._enforce_limits(config, deadline, baseline, metrics)
        tool_name = block.name or ""
        tool_input = block.input or {}
        await self._checkpoint(
            turn_id,
            "tool_requested",
            metrics,
            {"tool_call_id": block.id, "tool_name": tool_name},
        )
        try:
            tool = tools.get(tool_name)
            invalid = tool_input.get(INVALID_TOOL_INPUT_JSON_KEY)
            if invalid is not None and not tool.handles_input_validation:
                raise LiliesServiceError(f"invalid tool input JSON for {tool_name}")
            validated = (
                tool_input
                if tool.handles_input_validation
                else tool.input_model.model_validate(tool_input).model_dump(mode="json")
            )
            requires_permission = (
                tool.requires_permission
                if tool.requires_permission is not None
                else tool.dangerous or tool.mutating
            )
            if requires_permission:
                validated = await self._wait_for_permission(
                    session_id,
                    turn_id,
                    block,
                    tool,
                    validated,
                    metrics,
                )
                self._enforce_limits(config, deadline, baseline, metrics)
            await self._checkpoint(
                turn_id,
                "tool_executing",
                metrics,
                {
                    "tool_call_id": block.id,
                    "tool_name": tool_name,
                    "tool_input_digest": self._digest_json(validated),
                },
                side_effect_state="executing" if tool.side_effecting else "read_only",
            )
            await self._emit(
                session_id,
                "tool.started",
                {
                    "turn_id": turn_id,
                    "tool_call_id": block.id,
                    "tool": tool_name,
                    "input": redact_sensitive_fields(validated),
                },
            )
            outcome = await tool.execute(
                validated,
                LiliesToolContext(
                    session_id=session_id,
                    workspace=self._workspace_for(session_id),
                    turn_id=turn_id,
                    tool_call_id=block.id,
                ),
            )
            await self._persist_platform_contract_digest(
                session_id,
                tool_name=tool_name,
                outcome=outcome,
            )
            await self._checkpoint(
                turn_id,
                "tool_completed",
                metrics,
                {"tool_call_id": block.id, "tool_name": tool_name},
                side_effect_state="completed",
            )
            await self._emit(
                session_id,
                "tool.completed" if not outcome.is_error else "tool.failed",
                {
                    "turn_id": turn_id,
                    "tool_call_id": block.id,
                    "tool": tool_name,
                    "is_error": outcome.is_error,
                    "content": outcome.content[:20_000],
                },
            )
            return ContentBlock(
                type="tool_result",
                tool_use_id=block.id,
                content=self._model_tool_result_content(tool, outcome),
                is_error=outcome.is_error,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._emit(
                session_id,
                "tool.failed",
                {
                    "turn_id": turn_id,
                    "tool_call_id": block.id,
                    "tool": tool_name,
                    "error": self._safe_error(error),
                },
            )
            return ContentBlock(
                type="tool_result",
                tool_use_id=block.id,
                content=self._safe_error(error),
                is_error=True,
            )

    async def _persist_platform_contract_digest(
        self,
        session_id: str,
        *,
        tool_name: str,
        outcome: LiliesToolResult,
    ) -> None:
        if tool_name != "platform_contract_get" or outcome.is_error:
            return
        try:
            payload = json.loads(outcome.content)
            digest = payload["data"]["contract_digest"]
        except (KeyError, TypeError, ValueError):
            return
        if not isinstance(digest, str):
            return
        session = await self.storage.get_session(session_id)
        if session.get("platform_contract_digest") == digest:
            return
        await self.storage.update_session_context(
            session_id,
            platform_contract_digest=digest,
        )

    async def _wait_for_permission(
        self,
        session_id: str,
        turn_id: str,
        block: ContentBlock,
        tool: LiliesTool,
        tool_input: dict[str, Any],
        metrics: TurnMetrics,
    ) -> dict[str, Any]:
        input_digest = self._digest_json(tool_input)
        await self._checkpoint(
            turn_id,
            "waiting_permission",
            metrics,
            {
                "tool_call_id": block.id,
                "tool_name": tool.name,
                "tool_input": tool_input,
                "tool_input_digest": input_digest,
            },
            side_effect_state="awaiting_permission",
        )
        permission = await self.storage.create_permission_request(
            session_id,
            turn_id,
            tool.name,
            input_digest,
            tool_call_id=block.id or "unknown",
            tool_input=tool_input,
            input_summary=redact_sensitive_fields(tool_input),
        )
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.permission_waiters[permission["id"]] = future
        try:
            decision = await future
        finally:
            self.permission_waiters.pop(permission["id"], None)
        if decision["status"] != "allowed":
            raise PermissionError(decision.get("message") or f"permission denied for {tool.name}")
        return self._validated_approved_permission_input(tool, decision)

    def _validated_approved_permission_input(
        self,
        tool: LiliesTool,
        permission: dict[str, Any],
    ) -> dict[str, Any]:
        original_input = permission.get("tool_input")
        original_digest = permission.get("original_input_digest", permission.get("input_digest"))
        if (
            not isinstance(original_input, dict)
            or not isinstance(original_digest, str)
            or self._digest_json(original_input) != original_digest
        ):
            raise LiliesConflictError("durable permission original input digest mismatch")
        approved_input = permission.get("decision_input")
        approved_digest = permission.get(
            "approved_input_digest", permission.get("decision_input_digest")
        )
        if not isinstance(approved_input, dict) or not isinstance(approved_digest, str):
            raise LiliesServiceError("durable permission is missing its approved tool input")
        if self._digest_json(approved_input) != approved_digest:
            raise LiliesConflictError("durable permission approved input digest mismatch")
        validated = tool.input_model.model_validate(approved_input).model_dump(mode="json")
        if self._digest_json(validated) != approved_digest:
            raise LiliesConflictError("approved tool input changed during normalization")
        return validated

    async def _complete_resolved_permission(
        self,
        session_id: str,
        turn: dict[str, Any],
        permission: dict[str, Any],
        metrics: TurnMetrics,
    ) -> ContentBlock:
        checkpoint = turn["checkpoint"]
        pending = checkpoint.get("pending", checkpoint)
        tool_call_id = permission.get("tool_call_id") or pending.get("tool_call_id")
        tool_name = permission["tool_name"]
        if permission["status"] != "allowed":
            return ContentBlock(
                type="tool_result",
                tool_use_id=tool_call_id,
                content=permission.get("message") or f"permission denied for {tool_name}",
                is_error=True,
            )
        tools = await self.tool_registry_for_session(session_id)
        tool = tools.get(tool_name)
        validated = self._validated_approved_permission_input(tool, permission)
        await self._checkpoint(
            turn["id"],
            "tool_executing",
            metrics,
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "tool_input_digest": self._digest_json(validated),
            },
            side_effect_state="executing" if tool.side_effecting else "read_only",
        )
        await self._emit(
            session_id,
            "tool.started",
            {
                "turn_id": turn["id"],
                "tool_call_id": tool_call_id,
                "tool": tool_name,
                "input": redact_sensitive_fields(validated),
                "resumed_after_permission": True,
            },
        )
        outcome = await tool.execute(
            validated,
            LiliesToolContext(
                session_id=session_id,
                workspace=self._workspace_for(session_id),
                turn_id=turn["id"],
                tool_call_id=tool_call_id,
            ),
        )
        await self._emit(
            session_id,
            "tool.completed" if not outcome.is_error else "tool.failed",
            {
                "turn_id": turn["id"],
                "tool_call_id": tool_call_id,
                "tool": tool_name,
                "is_error": outcome.is_error,
                "content": outcome.content[:20_000],
                "resumed_after_permission": True,
            },
        )
        await self._checkpoint(
            turn["id"],
            "tool_completed",
            metrics,
            {"tool_call_id": tool_call_id, "tool_name": tool_name},
            side_effect_state="completed",
        )
        return ContentBlock(
            type="tool_result",
            tool_use_id=tool_call_id,
            content=self._model_tool_result_content(tool, outcome),
            is_error=outcome.is_error,
        )

    @staticmethod
    def _model_tool_result_content(
        tool: LiliesTool,
        outcome: LiliesToolResult,
    ) -> str:
        if tool.preserve_result_integrity:
            # Platform HTTP tools produce an already-bounded atomic JSON
            # envelope.  Never apply a character slice that could invalidate it.
            return outcome.content
        return outcome.content[: tool.max_result_chars]

    async def _checkpoint(
        self,
        turn_id: str,
        phase: str,
        metrics: TurnMetrics,
        pending: dict[str, Any],
        *,
        side_effect_state: str | None = None,
    ) -> None:
        await self.storage.update_turn_checkpoint(
            turn_id,
            phase=phase,
            checkpoint={"metrics": metrics.checkpoint(), "pending": pending},
            side_effect_state=side_effect_state,
        )

    async def _add_tool_result_message(
        self,
        session_id: str,
        turn_id: str,
        result: ContentBlock,
    ) -> None:
        await self.storage.add_message(
            session_id,
            "tool",
            [result.model_dump(mode="json", exclude_none=True)],
            turn_id=turn_id,
        )

    async def _model_messages(self, session_id: str) -> list[ChatMessage]:
        result: list[ChatMessage] = []
        session = await self.storage.get_session(session_id)
        messages = await self.storage.list_messages(session_id, limit=5000)
        if session.get("context_summary"):
            messages = messages[-8:]
        for item in messages:
            if item["role"] == "system":
                continue
            role = "assistant" if item["role"] == "assistant" else "user"
            blocks = [ContentBlock.model_validate(block) for block in item["content"]]
            blocks = [block for block in blocks if block.type != "thinking"]
            if blocks:
                result.append(ChatMessage(role=role, content=blocks))
        return result

    async def _compact_if_needed(
        self,
        session_id: str,
        session: dict[str, Any],
    ) -> None:
        messages = await self.storage.list_messages(session_id, limit=5000)
        estimate = sum(len(json.dumps(item["content"], ensure_ascii=False)) for item in messages) // 4
        threshold = int((self.settings.context_window - self.settings.max_output_tokens) * 0.8)
        if estimate < threshold or len(messages) < 12:
            return
        old = messages[:-8]
        fragments: list[str] = []
        for item in old[-80:]:
            texts: list[str] = []
            for block in item["content"]:
                if block.get("type") == "text" and block.get("text"):
                    texts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    texts.append(f"tool request {block.get('name')} {block.get('id')}")
                elif block.get("type") == "tool_result":
                    texts.append(
                        f"tool result {block.get('tool_use_id')}: {str(block.get('content'))[:1000]}"
                    )
            if texts:
                fragments.append(f"{item['role']}: {' '.join(texts)}")
        assignment = session.get("assignment")
        summary = "\n".join(
            [
                "Persistent deterministic context summary:",
                *([f"assignment: {json.dumps(assignment, ensure_ascii=False)}"] if assignment else []),
                *fragments,
            ]
        )[-50_000:]
        events = await self.storage.list_events(session_id, after=0, limit=5000)
        cursor = events[-1]["seq"] if events else 0
        await self.storage.update_session_context(
            session_id,
            context_summary=summary,
            summary_through_event_seq=cursor,
        )

    async def _close_uncertain_tool_calls(self, session_id: str) -> None:
        messages = await self.storage.list_messages(session_id, limit=5000)
        requested: list[str] = []
        resolved: set[str] = set()
        for item in messages:
            for block in item["content"]:
                if block.get("type") == "tool_use" and block.get("id"):
                    requested.append(str(block["id"]))
                elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                    resolved.add(str(block["tool_use_id"]))
        missing = [tool_call_id for tool_call_id in requested if tool_call_id not in resolved]
        for tool_call_id in missing:
            await self.storage.add_message(
                session_id,
                "tool",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": (
                            "The daemon was interrupted before a durable result was recorded. "
                            "The side effect is uncertain and was not replayed automatically."
                        ),
                        "is_error": True,
                    }
                ],
            )

    @staticmethod
    def _cumulative_metrics(session: dict[str, Any]) -> CumulativeMetrics:
        """Read the authoritative usage already atomically settled by storage."""

        return CumulativeMetrics(
            token_count=int(session.get("token_count", 0)),
            cost_usd=float(session.get("cost_usd", 0.0)),
            tool_calls=int(session.get("tool_count", 0)),
            model_calls=int(session.get("model_call_count", 0)),
        )

    def _turn_deadline(
        self,
        config: dict[str, Any],
        turn: dict[str, Any],
    ) -> datetime:
        configured = config.get("deadline_at")
        if configured is not None:
            deadline = datetime.fromisoformat(str(configured).replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return deadline.astimezone(timezone.utc)
        created_at = datetime.fromisoformat(str(turn["created_at"]).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at.astimezone(timezone.utc) + timedelta(
            seconds=int(
                config.get(
                    "deadline_seconds",
                    self.settings.default_deadline_seconds,
                )
            )
        )

    def _enforce_limits(
        self,
        config: dict[str, Any],
        deadline: datetime,
        baseline: CumulativeMetrics,
        metrics: TurnMetrics,
        *,
        before_model: bool = False,
    ) -> None:
        if datetime.now(timezone.utc) >= deadline:
            raise LiliesDeadlineExceeded("session deadline exceeded")
        max_model_calls = self._max_model_calls(config)
        total_model_calls = baseline.model_calls + metrics.model_calls
        if before_model and total_model_calls >= max_model_calls:
            raise LiliesBudgetExceeded(
                f"maximum model turns exceeded ({max_model_calls}); cumulative model calls"
            )
        max_tools = int(config.get("max_tool_calls", self.settings.default_max_tool_calls))
        if baseline.tool_calls + metrics.tool_calls > max_tools:
            raise LiliesBudgetExceeded(f"maximum tool calls exceeded ({max_tools})")
        max_tokens = int(
            config.get(
                "max_tokens",
                self.settings.context_window * self.settings.default_max_turns,
            )
        )
        total_tokens = baseline.token_count + self._token_total(metrics.usage)
        if total_tokens > max_tokens or (before_model and total_tokens >= max_tokens):
            raise LiliesBudgetExceeded(f"maximum token budget exceeded ({max_tokens})")
        max_cost = float(
            config.get("max_budget_usd", self.settings.default_max_budget_usd)
        )
        total_cost = baseline.cost_usd + metrics.usage.cost_usd
        if total_cost > max_cost or (before_model and total_cost >= max_cost):
            raise LiliesBudgetExceeded(f"model budget exceeded ({max_cost:.2f} USD)")

    @staticmethod
    def _client_is_active(client: dict[str, Any]) -> bool:
        if client.get("revoked_at") is not None:
            return False
        expires_at = client.get("expires_at")
        if expires_at is None:
            return True
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry.astimezone(timezone.utc) > datetime.now(timezone.utc)

    def _max_model_calls(self, config: dict[str, Any]) -> int:
        return min(
            int(config.get("max_model_calls", self.settings.default_max_turns)),
            int(config.get("max_turns", self.settings.default_max_turns)),
        )

    async def _emit(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        await self.storage.append_event(session_id, kind, data)

    def _workspace_for(self, session_id: str) -> Path:
        try:
            UUID(session_id)
        except ValueError as error:
            raise ValueError("invalid session id") from error
        root = self.settings.resolved_workspace_root.resolve()
        workspace = (root / session_id).resolve()
        if root not in workspace.parents:
            raise ValueError("session workspace escapes workspace root")
        return workspace

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self.session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self.session_locks[session_id] = lock
        return lock

    @staticmethod
    def _digest_json(value: Any) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    @staticmethod
    def _token_total(usage: Usage) -> int:
        return usage.input_tokens + usage.output_tokens

    @staticmethod
    def _safe_error(error: Exception) -> str:
        if isinstance(error, ValidationError):
            return "tool input failed schema validation"
        if isinstance(error, (LiliesBudgetExceeded, LiliesDeadlineExceeded)):
            # These messages are constructed locally from numeric contract limits.
            return f"{type(error).__name__}: {error}"[:2000]
        value = str(error)
        for marker in ("authorization", "cookie", "password", "secret", "token"):
            if marker in value.casefold():
                return f"{type(error).__name__}: sensitive error detail redacted"
        return f"{type(error).__name__}: {value}"[:2000]

    def _consume_turn_task(self, session_id: str, task: asyncio.Task[None]) -> None:
        if self.active_turns.get(session_id) is task:
            self.active_turns.pop(session_id, None)
        if not task.cancelled():
            task.exception()


def build_local_lilies_core(
    settings: LiliesSettings,
    *,
    storage: LiliesStorage | None = None,
    provider: ModelProvider | None = None,
    tools: LiliesToolRegistry | None = None,
) -> LocalLiliesCore:
    """Assemble exactly the local provider, store, tools and durable loop."""

    selected_storage = storage or LiliesStorage(settings.data_dir)
    selected_tools = tools or build_lilies_core_registry()
    service = LocalLiliesService(
        settings,
        storage=selected_storage,
        provider=provider,
        tools=selected_tools,
    )
    return LocalLiliesCore(
        storage=selected_storage,
        provider=service.provider,
        tools=selected_tools,
        service=service,
    )
