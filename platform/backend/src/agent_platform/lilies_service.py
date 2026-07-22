from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
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
from .collaboration_models import (
    sanitize_collaboration_payload,
    validate_collaboration_payload_safety,
)
from .lilies_config import LiliesSettings
from .lilies_identity import build_lilies_system_prompt
from .lilies_collaboration_client import LiliesCollaborationClient
from .lilies_collaboration_tools import register_lilies_collaboration_tools
from .lilies_models import (
    AssignmentNetworkPolicy,
    BuildAssignment,
    CollaborationScope,
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
from .lilies_storage import (
    LiliesAccessDeniedError,
    LiliesAuthenticationError,
    LiliesConflictError,
    LiliesNotFoundError,
    LiliesStorage,
)
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


class LiliesCollaborationDurabilityError(LiliesServiceError):
    """A remote collaboration mutation succeeded before local durability failed."""

    pass


_COLLABORATION_TOOL_PREFIX = "collaboration_"
_REDACTED_COLLABORATION_PAYLOAD = "[REDACTED]"
_REJECTED_COLLABORATION_INPUT = {
    "sensitive_payload": _REDACTED_COLLABORATION_PAYLOAD,
}


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


@dataclass(frozen=True, slots=True)
class _CollaborationWaitTarget:
    kind: str
    identifier: str

    @property
    def wait_id(self) -> str:
        return f"{self.kind}:{self.identifier}"


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
        self._registered_secret_values: dict[str, frozenset[str]] = {}
        self.active_turns: dict[str, asyncio.Task[None]] = {}
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.permission_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.cancellation_dispositions: dict[str, CancellationDisposition] = {}
        self.stopping = False
        self.recovery_summary: dict[str, int] = {}

    async def initialize(self) -> dict[str, int]:
        self.settings.prepare()
        self.recovery_summary = await self.storage.initialize()
        for session in await self.storage.list_sessions():
            assignment = session.get("assignment")
            if (
                not isinstance(assignment, dict)
                or assignment.get("collaboration") is None
            ):
                continue
            waiting_turn = next(
                (
                    turn
                    for turn in reversed(await self.storage.list_turns(session["id"]))
                    if turn["status"] in {"running", "waiting_collaboration"}
                    and (
                        turn["status"] == "waiting_collaboration"
                        or (
                            isinstance(turn.get("checkpoint", {}).get("pending"), dict)
                            and turn["checkpoint"]["pending"].get("kind")
                            in {
                                "collaboration_side_effect_pending",
                                "collaboration_result_recovered",
                            }
                        )
                    )
                ),
                None,
            )
            if waiting_turn is not None:
                self._start_turn_task(session["id"], waiting_turn["id"])
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

        try:
            credential = await self._validate_assignment_access(assignment)
            collaboration_credential = await self._validate_collaboration_access(
                assignment
            )
            if collaboration_credential is not None and hmac.compare_digest(
                str(credential.get("value", "")),
                str(collaboration_credential.get("value", "")),
            ):
                raise LiliesAccessDeniedError(
                    "platform and collaboration credentials must be distinct"
                )
        except LiliesAccessDeniedError as error:
            # Session tool resolution is a service boundary.  Keep daemon
            # storage/auth implementation details out of the agent loop while
            # preserving the actionable, already-redacted reason.
            raise LiliesServiceError(str(error)) from error
        allowed_operations = {
            action.value for action in assignment.constraints.allowed_actions
        }

        fingerprint = self._digest_json(
            {
                "assignment": assignment.model_dump(mode="json", exclude_none=True),
                "credential_ref": credential["credential_ref"],
                "credential_updated_at": credential.get("updated_at"),
                "collaboration_credential_ref": (
                    collaboration_credential["credential_ref"]
                    if collaboration_credential is not None
                    else None
                ),
                "collaboration_credential_updated_at": (
                    collaboration_credential.get("updated_at")
                    if collaboration_credential is not None
                    else None
                ),
            }
        )
        registered_secrets = {
            str(value)
            for value in (
                self.settings.deepseek_api_key,
                credential.get("value"),
                (
                    collaboration_credential.get("value")
                    if collaboration_credential is not None
                    else None
                ),
            )
            if isinstance(value, str) and value
        }
        self._registered_secret_values[session_id] = frozenset(registered_secrets)
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
        if assignment.collaboration is not None:
            if collaboration_credential is None:  # pragma: no cover - invariant guard
                raise LiliesServiceError("collaboration credential is unavailable")
            collaboration_client = LiliesCollaborationClient(
                base_url=str(assignment.platform.base_url),
                access_token=str(collaboration_credential["value"]),
                channel_id=assignment.collaboration.channel_id,
            )
            register_lilies_collaboration_tools(registry, collaboration_client)
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

    async def _validate_assignment_access(
        self,
        assignment: BuildAssignment,
        *,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        policy = assignment.constraints.network_policy
        platform_host = (assignment.platform.base_url.host or "").casefold()
        if policy is AssignmentNetworkPolicy.none:
            raise LiliesAccessDeniedError(
                "assignment network policy denies the platform connection"
            )
        if policy is AssignmentNetworkPolicy.allowlist and platform_host not in {
            host.casefold() for host in assignment.constraints.allowed_hosts
        }:
            raise LiliesAccessDeniedError(
                "platform host is absent from the assignment allowlist"
            )

        credential = await self.storage.get_credential(
            assignment.platform.credential_ref,
            assignment_id=str(assignment.assignment_id),
        )
        if client_id is not None and credential.get("client_id") != client_id:
            raise LiliesAccessDeniedError(
                "assignment credential belongs to another local client"
            )
        if credential.get("kind") != CredentialKind.platform_assignment.value:
            raise LiliesAccessDeniedError("assignment credential has the wrong kind")
        assignment_scopes = sorted(scope.value for scope in assignment.platform.scopes)
        credential_scopes = sorted(str(scope) for scope in credential.get("scopes", []))
        if credential_scopes != assignment_scopes:
            raise LiliesAccessDeniedError(
                "assignment credential scopes do not match; they must exactly match "
                "the assignment"
            )
        expires_at = credential.get("expires_at")
        if expires_at is None:
            raise LiliesAuthenticationError(
                "assignment credential must have a bounded expiry"
            )
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry.astimezone(timezone.utc) < assignment.constraints.deadline_at:
            raise LiliesAuthenticationError(
                "assignment credential expires before the assignment deadline"
            )

        allowed_operations = {
            action.value for action in assignment.constraints.allowed_actions
        }
        if "platform_contract_get" not in allowed_operations:
            raise LiliesAccessDeniedError(
                "assignment must allow platform_contract_get"
            )
        scope_set = set(assignment_scopes)
        for operation in allowed_operations:
            if str(operation_by_name(operation)["scope"]) not in scope_set:
                raise LiliesAccessDeniedError(
                    f"assignment action {operation} is not covered by its credential scopes"
                )
        return credential

    async def _validate_collaboration_access(
        self,
        assignment: BuildAssignment,
        *,
        client_id: str | None = None,
    ) -> dict[str, Any] | None:
        access = assignment.collaboration
        if access is None:
            return None
        if assignment.mode.value != "formal_experiment":
            raise LiliesAccessDeniedError(
                "collaboration access is restricted to formal experiments"
            )
        credential = await self.storage.get_credential(
            access.credential_ref,
            assignment_id=str(assignment.assignment_id),
        )
        if client_id is not None and credential.get("client_id") != client_id:
            raise LiliesAccessDeniedError(
                "collaboration credential belongs to another local client"
            )
        if credential.get("kind") != CredentialKind.collaboration_channel.value:
            raise LiliesAccessDeniedError("collaboration credential has the wrong kind")
        expected_scopes = sorted(scope.value for scope in CollaborationScope)
        actual_scopes = sorted(str(scope) for scope in credential.get("scopes", []))
        if actual_scopes != expected_scopes:
            raise LiliesAccessDeniedError(
                "collaboration credential scopes do not exactly match the assignment"
            )
        assignment_scopes = sorted(scope.value for scope in access.scopes)
        if assignment_scopes != expected_scopes:
            raise LiliesAccessDeniedError(
                "assignment collaboration scopes are incomplete"
            )
        expires_at = credential.get("expires_at")
        if expires_at is None:
            raise LiliesAuthenticationError(
                "collaboration credential must have a bounded expiry"
            )
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry.astimezone(timezone.utc) != access.expires_at:
            raise LiliesAuthenticationError(
                "collaboration credential expiry does not match the assignment"
            )
        if expiry.astimezone(timezone.utc) < assignment.constraints.deadline_at:
            raise LiliesAuthenticationError(
                "collaboration credential expires before the assignment deadline"
            )
        return credential

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

    async def submit_assignment(
        self,
        session_id: str,
        assignment: BuildAssignment,
        *,
        client_id: str,
    ) -> dict[str, Any]:
        """Validate, persist, and start a BuildAssignment exactly once."""

        projection = assignment.model_dump(mode="json", exclude_none=True)
        replay = await self.storage.find_assignment_receipt(
            session_id,
            projection,
            client_id=client_id,
        )
        if replay is not None:
            return replay
        if self.stopping:
            raise LiliesConflictError("daemon is stopping")

        async with self._session_lock(session_id):
            replay = await self.storage.find_assignment_receipt(
                session_id,
                projection,
                client_id=client_id,
            )
            if replay is not None:
                return replay
            session = await self.storage.get_session(session_id, client_id=client_id)
            config = dict(session.get("config") or {})
            if config.get("kind") != "platform":
                raise LiliesConflictError(
                    "BuildAssignment requires a platform session"
                )
            if session["status"] != "ready":
                raise LiliesConflictError(
                    f"session {session_id} cannot accept an assignment from {session['status']}"
                )
            if session.get("assignment_id") is not None:
                raise LiliesConflictError(
                    f"session {session_id} already has a BuildAssignment"
                )
            if assignment.constraints.deadline_at <= datetime.now(timezone.utc):
                raise LiliesConflictError("assignment deadline has already passed")
            credential = await self._validate_assignment_access(
                assignment,
                client_id=client_id,
            )
            collaboration_credential = await self._validate_collaboration_access(
                assignment,
                client_id=client_id,
            )
            if collaboration_credential is not None and hmac.compare_digest(
                str(credential.get("value", "")),
                str(collaboration_credential.get("value", "")),
            ):
                raise LiliesAccessDeniedError(
                    "platform and collaboration credentials must be distinct"
                )

            constraints = assignment.constraints
            config.update(
                {
                    "max_turns": constraints.max_turns,
                    "max_model_calls": constraints.max_turns,
                    "max_tool_calls": constraints.max_tool_calls,
                    "deadline_at": constraints.deadline_at.isoformat(),
                    "network_policy": constraints.network_policy.value,
                    "allowed_hosts": list(constraints.allowed_hosts),
                    "allowed_actions": [
                        action.value for action in constraints.allowed_actions
                    ],
                }
            )
            if constraints.max_budget_usd is not None:
                config["max_budget_usd"] = constraints.max_budget_usd

            assignment_id = str(assignment.assignment_id)
            start_message_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"lilies:assignment-message:{assignment_id}:{assignment.idempotency_key}",
                )
            )
            start_turn_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"lilies:assignment-turn:{assignment_id}:{assignment.idempotency_key}",
                )
            )
            assignment_json = json.dumps(
                projection,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            start_content = [
                {
                    "type": "text",
                    "text": (
                        "Accept this BuildAssignment 1.0 as the authoritative customer task. "
                        "Use only its scoped platform contract and preserve its acceptance "
                        f"constraints.\n{assignment_json}"
                    ),
                }
            ]
            receipt = await self.storage.accept_assignment(
                session_id,
                projection,
                session_config=config,
                start_message_id=start_message_id,
                start_message_content=start_content,
                start_turn_id=start_turn_id,
                turn_checkpoint={"metrics": TurnMetrics(Usage()).checkpoint()},
                client_id=client_id,
            )
            if not receipt["replayed"]:
                self._start_turn_task(session_id, receipt["turn_id"])
            return receipt

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
            if self._is_collaboration_tool(tool.name):
                try:
                    projected = self._sanitize_collaboration_projection(
                        session_id,
                        approved_update,
                    )
                except ValueError as error:
                    raise LiliesServiceError(
                        "collaboration permission input was rejected by daemon safety policy"
                    ) from error
                if not isinstance(projected, dict):
                    raise LiliesServiceError(
                        "collaboration permission input projection is invalid"
                    )
                approved_update = projected
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
            pending = turn.get("checkpoint", {}).get("pending", {})
            if (
                turn["status"] == "running"
                and isinstance(pending, dict)
                and pending.get("kind") == "collaboration_side_effect_pending"
            ):
                await self._recover_pending_collaboration_side_effect(
                    session_id, turn_id, pending, metrics
                )
            elif (
                turn["status"] == "running"
                and isinstance(pending, dict)
                and pending.get("kind") == "collaboration_result_recovered"
            ):
                await self._close_uncertain_tool_calls(session_id)
            if turn["status"] == "waiting_collaboration":
                pending = turn.get("checkpoint", {}).get("pending", {})
                recovered_remote_result = bool(
                    isinstance(pending, dict)
                    and pending.get("recovered_after_remote_commit")
                )
                await self._await_collaboration_updates(session_id, turn_id)
                if recovered_remote_result:
                    await self._close_uncertain_tool_calls(session_id)
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
        except LiliesCollaborationDurabilityError:
            # Keep the pre-call idempotent recovery intent live.  A daemon
            # restart (or an explicit recovery task) can safely replay it;
            # converting this into a model-visible tool failure would orphan
            # an already-committed remote mutation.
            return
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
                collaboration_active=(
                    isinstance(session.get("assignment"), dict)
                    and session["assignment"].get("collaboration") is not None
                ),
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
            durable_visible_blocks: list[dict[str, Any]] = []
            for block in visible_blocks:
                projection = block.model_dump(mode="json", exclude_none=True)
                if block.type == "tool_use" and self._is_collaboration_tool(block.name):
                    projection["input"] = self._observable_collaboration_projection(
                        session_id,
                        block.input or {},
                    )
                durable_visible_blocks.append(projection)
            await self.storage.add_message(
                session_id,
                "assistant",
                durable_visible_blocks,
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
            wait_target: _CollaborationWaitTarget | None = None
            for index, block in enumerate(tool_calls):
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
                candidate = self._collaboration_wait_target(
                    block.name or "",
                    block.input or {},
                    result,
                )
                if candidate is None:
                    await self._add_tool_result_message(
                        session_id,
                        turn_id,
                        result,
                        message_id=(
                            self._collaboration_result_message_id(
                                turn_id, block.id
                            )
                            if self._is_collaboration_mutation(block.name or "")
                            else None
                        ),
                    )
                    continue

                wait_target = candidate
                result_blocks = [result.model_dump(mode="json", exclude_none=True)]
                for skipped in tool_calls[index + 1 :]:
                    result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": skipped.id,
                            "content": (
                                "This tool call was not executed because the formal "
                                "collaboration task entered a durable waiting state."
                            ),
                            "is_error": True,
                        }
                    )
                current = await self.storage.get_session(session_id)
                cursor = int(current.get("last_pipeline_cursor", 0))
                await self.storage.begin_collaboration_wait(
                    turn_id,
                    wait_id=wait_target.wait_id,
                    pipeline_cursor=cursor,
                    checkpoint={
                        "metrics": metrics.checkpoint(),
                        "pending": {
                            "kind": "collaboration_wait",
                            "wait_id": wait_target.wait_id,
                            "pipeline_cursor": cursor,
                        },
                    },
                    tool_result_content=result_blocks,
                    tool_result_message_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"lilies:collaboration-wait-result:{turn_id}:{block.id}",
                        )
                    ),
                )
                break
            if wait_target is not None:
                await self._await_collaboration_updates(session_id, turn_id)
            session = await self.storage.get_session(session_id)

    @staticmethod
    def _collaboration_wait_target(
        tool_name: str,
        tool_input: dict[str, Any],
        result: ContentBlock,
    ) -> _CollaborationWaitTarget | None:
        if result.is_error or tool_name not in {
            "collaboration_report_submit",
            "collaboration_verification_claim",
        }:
            return None
        try:
            envelope = json.loads(str(result.content))
            data = envelope["data"]
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if tool_name == "collaboration_report_submit":
            if str(tool_input.get("operation", "submit")) not in {"submit", "revise"}:
                return None
            if str(data.get("status")) not in {
                "awaiting_user_review",
                "approved_for_codex",
                "routed_to_task_author",
                "environment_failed",
            }:
                return None
            report_id = data.get("report_id")
            return (
                _CollaborationWaitTarget("report", str(report_id))
                if report_id is not None
                else None
            )
        if str(data.get("status")) not in {
            "frozen",
            "ready_for_independent_verification",
            "awaiting_independent_verification",
        }:
            return None
        claim_id = data.get("claim_id")
        return (
            _CollaborationWaitTarget("claim", str(claim_id))
            if claim_id is not None
            else None
        )

    async def _await_collaboration_updates(
        self,
        session_id: str,
        turn_id: str,
    ) -> None:
        session = await self.storage.get_session(session_id)
        wait_id = str(session.get("waiting_collaboration_id") or "")
        kind, separator, identifier = wait_id.partition(":")
        if separator != ":" or kind not in {"report", "claim"} or not identifier:
            raise LiliesServiceError("durable collaboration wait binding is invalid")
        target = _CollaborationWaitTarget(kind, identifier)
        tools = await self.tool_registry_for_session(session_id, session=session)
        updates_tool = tools.get("collaboration_updates_read")
        client = getattr(updates_tool, "client", None)
        if not isinstance(client, LiliesCollaborationClient):
            raise LiliesServiceError("collaboration update client is unavailable")
        while True:
            session = await self.storage.get_session(session_id)
            local_cursor = int(session.get("last_pipeline_cursor", 0))
            deadline = self._collaboration_deadline(session)
            deadline_elapsed = (
                deadline is not None and datetime.now(timezone.utc) >= deadline
            )
            await self._ack_collaboration_cursor(client, local_cursor)
            result = await client.read_updates(limit=500)
            if not result.ok:
                if result.status_code in {401, 403, 404, 410}:
                    await self._record_terminal_collaboration_update(
                        session_id=session_id,
                        turn_id=turn_id,
                        wait_id=wait_id,
                        pipeline_cursor=local_cursor,
                        reason="channel_closed_or_credential_revoked",
                        failure_owner="environment",
                        text=(
                            "The formal collaboration channel is no longer readable. "
                            "Its credential was revoked, expired, or the channel was closed; "
                            "do not keep waiting or retry hidden endpoints."
                        ),
                    )
                    return
                if deadline_elapsed:
                    await self._record_terminal_collaboration_update(
                        session_id=session_id,
                        turn_id=turn_id,
                        wait_id=wait_id,
                        pipeline_cursor=local_cursor,
                        reason="channel_unavailable_before_assignment_deadline",
                        failure_owner="environment",
                        text=(
                            "The formal collaboration channel remained unavailable until "
                            "the assignment deadline. Treat this as environment evidence; "
                            "do not claim the blocked branch completed."
                        ),
                    )
                    return
                await asyncio.sleep(self._collaboration_retry_delay(deadline))
                continue
            raw_events = result.data.get("events", [])
            if not isinstance(raw_events, list):
                if deadline_elapsed:
                    await self._record_terminal_collaboration_update(
                        session_id=session_id,
                        turn_id=turn_id,
                        wait_id=wait_id,
                        pipeline_cursor=local_cursor,
                        reason="collaboration_response_deadline_elapsed",
                        failure_owner="collaboration_counterparty",
                        text=(
                            "The formal collaboration channel was reachable, but no valid "
                            "response arrived before the assignment deadline."
                        ),
                    )
                    return
                await asyncio.sleep(self._collaboration_retry_delay(deadline))
                continue
            fresh_events = [
                event
                for event in raw_events
                if isinstance(event, dict)
                and isinstance(event.get("seq"), int)
                and not isinstance(event.get("seq"), bool)
                and int(event["seq"]) > local_cursor
            ]
            if not fresh_events:
                if deadline_elapsed:
                    await self._record_terminal_collaboration_update(
                        session_id=session_id,
                        turn_id=turn_id,
                        wait_id=wait_id,
                        pipeline_cursor=local_cursor,
                        reason="collaboration_response_deadline_elapsed",
                        failure_owner="collaboration_counterparty",
                        text=(
                            "The formal collaboration channel was reachable, but no response "
                            "for the blocked object arrived before the assignment deadline."
                        ),
                    )
                    return
                await asyncio.sleep(self._collaboration_retry_delay(deadline))
                continue
            projected = self._observable_collaboration_projection(
                session_id,
                fresh_events,
            )
            if not isinstance(projected, list):
                raise LiliesServiceError("collaboration update projection is invalid")
            next_cursor = max(int(event["seq"]) for event in fresh_events)
            resumes = any(
                self._collaboration_event_resumes(target, event)
                for event in fresh_events
            )
            content = [
                {
                    "type": "text",
                    "text": (
                        "Durable collaboration updates for the current formal task:\n"
                        + json.dumps(
                            projected,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    ),
                }
            ]
            message_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"lilies:collaboration-update:{session_id}:{wait_id}:{next_cursor}",
                )
            )
            await self.storage.record_collaboration_update(
                turn_id,
                wait_id=wait_id,
                pipeline_cursor=next_cursor,
                message_id=message_id,
                content=content,
                resume=resumes,
            )
            await self._ack_collaboration_cursor(client, next_cursor)
            if resumes:
                return

    @staticmethod
    def _collaboration_deadline(session: dict[str, Any]) -> datetime | None:
        raw = (session.get("config") or {}).get("deadline_at")
        if not isinstance(raw, str):
            assignment = session.get("assignment")
            if isinstance(assignment, dict):
                constraints = assignment.get("constraints")
                if isinstance(constraints, dict):
                    raw = constraints.get("deadline_at")
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _collaboration_retry_delay(deadline: datetime | None) -> float:
        if deadline is None:
            return 0.25
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        return max(0.001, min(0.25, remaining))

    async def _record_terminal_collaboration_update(
        self,
        *,
        session_id: str,
        turn_id: str,
        wait_id: str,
        pipeline_cursor: int,
        reason: str,
        failure_owner: str,
        text: str,
    ) -> None:
        content = [
            {
                "type": "text",
                "text": text
                + "\n"
                + json.dumps(
                    {
                        "kind": "collaboration_terminal",
                        "reason": reason,
                        "failure_owner": failure_owner,
                        "wait_id": wait_id,
                        "pipeline_cursor": pipeline_cursor,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }
        ]
        message_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lilies:collaboration-terminal:{session_id}:{wait_id}:{reason}",
            )
        )
        await self.storage.record_collaboration_update(
            turn_id,
            wait_id=wait_id,
            pipeline_cursor=pipeline_cursor,
            message_id=message_id,
            content=content,
            resume=True,
        )

    @staticmethod
    def _collaboration_event_resumes(
        target: _CollaborationWaitTarget,
        event: dict[str, Any],
    ) -> bool:
        message_type = str(event.get("message_type", ""))
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        correlation_id = str(event.get("correlation_id", ""))
        if message_type == "control" and payload.get("kind") == "channel_closed":
            return True
        if correlation_id != target.identifier:
            return False
        if target.kind == "claim":
            return message_type in {"verification_result", "control"}
        if message_type in {
            "developer_response",
            "task_amendment",
            "environment_response",
            "control",
        }:
            return True
        return message_type == "approval" and payload.get("decision") != "approve"

    @staticmethod
    async def _ack_collaboration_cursor(
        client: LiliesCollaborationClient,
        through: int,
    ) -> bool:
        if through <= 0:
            return True
        state = await client.channel_state()
        if not state.ok:
            return False
        cursor = state.data.get("reader_cursor")
        if not isinstance(cursor, dict):
            return False
        ack_seq = cursor.get("ack_seq")
        revision = cursor.get("revision")
        reader_id = cursor.get("reader_id")
        if (
            isinstance(ack_seq, bool)
            or not isinstance(ack_seq, int)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or not isinstance(reader_id, str)
            or not reader_id
        ):
            return False
        if ack_seq >= through:
            return True
        result = await client.acknowledge(
            {
                "idempotency_key": (
                    f"collaboration.ack.{client.channel_id.hex}.{through}"
                ),
                "expected_cursor_revision": revision,
                "reader_role": "lilies",
                "reader_id": reader_id,
                "ack_seq": through,
            }
        )
        return result.ok

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
        collaboration_tool = self._is_collaboration_tool(tool_name)
        if collaboration_tool:
            try:
                projected_input = self._sanitize_collaboration_projection(
                    session_id, tool_input
                )
                if not isinstance(projected_input, dict):
                    raise ValueError("collaboration input projection is not an object")
                # The exact redacted projection is both the outbound HTTP input
                # and the durable crash-replay input.  Never validate one value
                # and then send the original secret-bearing object.
                tool_input = projected_input
            except ValueError:
                await self._checkpoint(
                    turn_id,
                    "tool_rejected",
                    metrics,
                    {
                        "tool_call_id": block.id,
                        "tool_name": tool_name,
                        **_REJECTED_COLLABORATION_INPUT,
                    },
                    side_effect_state="completed",
                )
                await self._emit(
                    session_id,
                    "tool.failed",
                    {
                        "turn_id": turn_id,
                        "tool_call_id": block.id,
                        "tool": tool_name,
                        "error": (
                            "collaboration tool input was rejected by daemon safety policy"
                        ),
                    },
                )
                return ContentBlock(
                    type="tool_result",
                    tool_use_id=block.id,
                    content=(
                        "collaboration tool input was rejected by daemon safety policy"
                    ),
                    is_error=True,
                )
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
            await self._require_refreshed_contract_for_reprobe(
                session_id,
                tool_name=tool_name,
                tool_input=validated,
            )
            execution_pending: dict[str, Any] = {
                "tool_call_id": block.id,
                "tool_name": tool_name,
                "tool_input_digest": self._digest_json(validated),
            }
            if collaboration_tool and tool.side_effecting:
                execution_pending.update(
                    {
                        "kind": "collaboration_side_effect_pending",
                        "tool_input": validated,
                    }
                )
            await self._checkpoint(
                turn_id,
                "tool_executing",
                metrics,
                execution_pending,
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
            if collaboration_tool:
                outcome = self._sanitize_collaboration_tool_outcome(
                    session_id,
                    outcome,
                )
            await self._persist_platform_contract_digest(
                session_id,
                tool_name=tool_name,
                outcome=outcome,
            )
            result = ContentBlock(
                type="tool_result",
                tool_use_id=block.id,
                content=self._model_tool_result_content(tool, outcome),
                is_error=outcome.is_error,
            )
            try:
                await self._checkpoint_completed_tool_result(
                    session_id=session_id,
                    turn_id=turn_id,
                    tool_name=tool_name,
                    tool_input=validated,
                    result=result,
                    metrics=metrics,
                )
            except Exception as error:
                if collaboration_tool and tool.side_effecting:
                    raise LiliesCollaborationDurabilityError(
                        "remote collaboration result awaits durable recovery"
                    ) from error
                raise
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
            return result
        except (asyncio.CancelledError, LiliesCollaborationDurabilityError):
            raise
        except Exception as error:
            safe_error = (
                "collaboration tool request failed"
                if collaboration_tool
                else self._safe_error(error)
            )
            await self._emit(
                session_id,
                "tool.failed",
                {
                    "turn_id": turn_id,
                    "tool_call_id": block.id,
                    "tool": tool_name,
                    "error": safe_error,
                },
            )
            return ContentBlock(
                type="tool_result",
                tool_use_id=block.id,
                content=safe_error,
                is_error=True,
            )

    async def _checkpoint_completed_tool_result(
        self,
        *,
        session_id: str,
        turn_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        result: ContentBlock,
        metrics: TurnMetrics,
    ) -> None:
        target = self._collaboration_wait_target(tool_name, tool_input, result)
        if target is None and not self._is_collaboration_mutation(tool_name):
            await self._checkpoint(
                turn_id,
                "tool_completed",
                metrics,
                {"tool_call_id": result.tool_use_id, "tool_name": tool_name},
                side_effect_state="completed",
            )
            return
        session = await self.storage.get_session(session_id)
        cursor = int(session.get("last_pipeline_cursor", 0))
        message_id = self._collaboration_result_message_id(
            turn_id, result.tool_use_id
        )
        await self._checkpoint(
            turn_id,
            "collaboration_remote_result",
            metrics,
            {
                "kind": "collaboration_remote_result",
                "tool_call_id": result.tool_use_id,
                "tool_name": tool_name,
                "wait_id": target.wait_id if target is not None else None,
                "pipeline_cursor": cursor,
                "tool_result_message_id": message_id,
                "tool_result_content": [
                    result.model_dump(mode="json", exclude_none=True)
                ],
            },
            side_effect_state="completed",
        )

    async def _recover_pending_collaboration_side_effect(
        self,
        session_id: str,
        turn_id: str,
        pending: dict[str, Any],
        metrics: TurnMetrics,
    ) -> None:
        tool_name = str(pending["tool_name"])
        tool_call_id = str(pending["tool_call_id"])
        raw_input = pending.get("tool_input")
        if not isinstance(raw_input, dict) or not self._is_collaboration_mutation(
            tool_name
        ):
            raise LiliesServiceError("collaboration recovery intent is invalid")
        tools = await self.tool_registry_for_session(session_id)
        tool = tools.get(tool_name)
        validated = (
            raw_input
            if tool.handles_input_validation
            else tool.input_model.model_validate(raw_input).model_dump(mode="json")
        )
        await self._require_refreshed_contract_for_reprobe(
            session_id,
            tool_name=tool_name,
            tool_input=validated,
        )
        outcome = await tool.execute(
            validated,
            LiliesToolContext(
                session_id=session_id,
                workspace=self._workspace_for(session_id),
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            ),
        )
        outcome = self._sanitize_collaboration_tool_outcome(session_id, outcome)
        result = ContentBlock(
            type="tool_result",
            tool_use_id=tool_call_id,
            content=self._model_tool_result_content(tool, outcome),
            is_error=outcome.is_error,
        )
        try:
            await self._checkpoint_completed_tool_result(
                session_id=session_id,
                turn_id=turn_id,
                tool_name=tool_name,
                tool_input=validated,
                result=result,
                metrics=metrics,
            )
        except Exception as error:
            raise LiliesCollaborationDurabilityError(
                "replayed collaboration result awaits durable recovery"
            ) from error
        target = self._collaboration_wait_target(tool_name, validated, result)
        if target is not None:
            session = await self.storage.get_session(session_id)
            cursor = int(session.get("last_pipeline_cursor", 0))
            await self.storage.begin_collaboration_wait(
                turn_id,
                wait_id=target.wait_id,
                pipeline_cursor=cursor,
                checkpoint={
                    "metrics": metrics.checkpoint(),
                    "pending": {
                        "kind": "collaboration_wait",
                        "wait_id": target.wait_id,
                        "pipeline_cursor": cursor,
                    },
                },
                tool_result_content=[
                    result.model_dump(mode="json", exclude_none=True)
                ],
                tool_result_message_id=self._collaboration_result_message_id(
                    turn_id, tool_call_id
                ),
            )
            await self._await_collaboration_updates(session_id, turn_id)
        else:
            await self._add_tool_result_message(
                session_id,
                turn_id,
                result,
                message_id=self._collaboration_result_message_id(
                    turn_id, tool_call_id
                ),
            )
        await self._close_uncertain_tool_calls(session_id)

    @staticmethod
    def _collaboration_result_message_id(
        turn_id: str,
        tool_call_id: str | None,
    ) -> str:
        return str(
            uuid5(
                NAMESPACE_URL,
                f"lilies:collaboration-wait-result:{turn_id}:{tool_call_id}",
            )
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
        config = dict(session.get("config") or {})
        config.update(
            {
                "platform_contract_fetch_revision": int(
                    config.get("platform_contract_fetch_revision", 0)
                )
                + 1,
                "platform_contract_fetch_digest": digest,
                "platform_contract_fetch_pipeline_cursor": int(
                    session.get("last_pipeline_cursor", 0)
                ),
                "platform_contract_fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self.storage.update_session_context(
            session_id,
            config=config,
            platform_contract_digest=digest,
        )

    async def _require_refreshed_contract_for_reprobe(
        self,
        session_id: str,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        """Reject a reprobe until the daemon has fetched the asserted contract.

        The collaboration response is model-visible, so its digest alone is not
        proof of a refresh.  ``platform_contract_digest`` changes only after a
        successful, protocol-validated ``platform_contract_get`` result (apart
        from the assignment's initial authoritative digest).
        """

        if (
            tool_name != "collaboration_report_submit"
            or str(tool_input.get("operation", "submit")) != "reprobe"
        ):
            return
        result = tool_input.get("result")
        asserted = result.get("contract_digest") if isinstance(result, dict) else None
        if not isinstance(asserted, str):
            return  # The public tool schema will reject the malformed request.
        session = await self.storage.get_session(session_id)
        current = session.get("platform_contract_digest")
        config = session.get("config") or {}
        fetched_digest = config.get("platform_contract_fetch_digest")
        fetched_revision = config.get("platform_contract_fetch_revision")
        fetched_cursor = config.get("platform_contract_fetch_pipeline_cursor")
        current_cursor = int(session.get("last_pipeline_cursor", 0))
        if (
            not isinstance(current, str)
            or not hmac.compare_digest(current, asserted)
            or not isinstance(fetched_digest, str)
            or not hmac.compare_digest(fetched_digest, asserted)
            or not isinstance(fetched_revision, int)
            or isinstance(fetched_revision, bool)
            or fetched_revision < 1
            or not isinstance(fetched_cursor, int)
            or isinstance(fetched_cursor, bool)
            or fetched_cursor < current_cursor
        ):
            raise LiliesConflictError(
                "refresh the platform contract after the developer response before "
                "submitting this reprobe"
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
        durable_tool_input = tool_input
        if self._is_collaboration_tool(tool.name):
            try:
                projected = self._sanitize_collaboration_projection(
                    session_id,
                    tool_input,
                )
            except ValueError as error:  # pragma: no cover - guarded by _execute_tool
                raise LiliesServiceError(
                    "collaboration tool input was rejected by daemon safety policy"
                ) from error
            if not isinstance(projected, dict):  # pragma: no cover - tool input invariant
                raise LiliesServiceError("collaboration tool input projection is invalid")
            durable_tool_input = projected
        input_digest = self._digest_json(durable_tool_input)
        await self._checkpoint(
            turn_id,
            "waiting_permission",
            metrics,
            {
                "tool_call_id": block.id,
                "tool_name": tool.name,
                "tool_input": durable_tool_input,
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
            tool_input=durable_tool_input,
            input_summary=(
                durable_tool_input
                if self._is_collaboration_tool(tool.name)
                else redact_sensitive_fields(tool_input)
            ),
        )
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.permission_waiters[permission["id"]] = future
        try:
            decision = await future
        finally:
            self.permission_waiters.pop(permission["id"], None)
        if decision["status"] != "allowed":
            raise PermissionError(decision.get("message") or f"permission denied for {tool.name}")
        return self._validated_approved_permission_input(
            tool,
            decision,
            session_id=session_id,
        )

    def _validated_approved_permission_input(
        self,
        tool: LiliesTool,
        permission: dict[str, Any],
        *,
        session_id: str | None = None,
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
        safe_approved_input = approved_input
        if self._is_collaboration_tool(tool.name):
            try:
                projected = self._sanitize_collaboration_projection(
                    session_id,
                    approved_input,
                )
            except ValueError as error:
                raise LiliesServiceError(
                    "collaboration permission input was rejected by daemon safety policy"
                ) from error
            if not isinstance(projected, dict):
                raise LiliesServiceError(
                    "collaboration permission input projection is invalid"
                )
            safe_approved_input = projected
        validated_model = tool.input_model.model_validate(safe_approved_input)
        if tool.handles_input_validation:
            # These HTTP adapters deliberately receive their original public
            # wire and perform their own bounded validation/error projection.
            # Binding was already verified against the durable approved input.
            return safe_approved_input
        validated = validated_model.model_dump(mode="json")
        if self._digest_json(validated) != self._digest_json(safe_approved_input):
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
        validated = self._validated_approved_permission_input(
            tool,
            permission,
            session_id=session_id,
        )
        await self._require_refreshed_contract_for_reprobe(
            session_id,
            tool_name=tool_name,
            tool_input=validated,
        )
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
        if self._is_collaboration_tool(tool_name):
            outcome = self._sanitize_collaboration_tool_outcome(
                session_id,
                outcome,
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
        if self._is_collaboration_tool(pending.get("tool_name")):
            checkpoint_turn = await self.storage.get_turn(turn_id)
            projected = self._observable_collaboration_projection(
                str(checkpoint_turn["session_id"]),
                pending,
            )
            if not isinstance(projected, dict):  # pragma: no cover - invariant guard
                projected = dict(_REJECTED_COLLABORATION_INPUT)
            pending = projected
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
        *,
        message_id: str | None = None,
    ) -> None:
        await self.storage.add_message(
            session_id,
            "tool",
            [result.model_dump(mode="json", exclude_none=True)],
            turn_id=turn_id,
            message_id=message_id,
        )

    async def _model_messages(self, session_id: str) -> list[ChatMessage]:
        result: list[ChatMessage] = []
        session = await self.storage.get_session(session_id)
        messages = await self.storage.list_recent_messages(session_id, limit=5000)
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
        messages = await self.storage.list_messages_for_compaction(session_id)
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
        invariants = self._compaction_invariants(session, messages)
        previous_summary = str(session.get("context_summary") or "")
        assignment_projection = self._compaction_assignment_projection(assignment)
        summary = "\n".join(
            [
                "Persistent deterministic context summary:",
                *([f"assignment: {assignment_projection}"] if assignment_projection else []),
                *(
                    [f"prior_summary: {previous_summary[-15_000:]}"]
                    if previous_summary
                    else []
                ),
                *fragments[-80:],
                "structured_invariants: "
                + json.dumps(
                    invariants,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ]
        )
        if len(summary) > 50_000:
            # Preserve the authoritative assignment and structured tail while
            # trimming only lossy conversational fragments from the middle.
            prefix = "\n".join(
                [
                    "Persistent deterministic context summary:",
                    *(
                        [f"assignment: {assignment_projection}"]
                        if assignment_projection
                        else []
                    ),
                ]
            )
            invariant_tail = "structured_invariants: " + json.dumps(
                invariants,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            middle_budget = max(0, 50_000 - len(prefix) - len(invariant_tail) - 2)
            middle = "\n".join(
                [
                    *(
                        [f"prior_summary: {previous_summary[-15_000:]}"]
                        if previous_summary
                        else []
                    ),
                    *fragments[-80:],
                ]
            )[-middle_budget:]
            summary = "\n".join([prefix, middle, invariant_tail])
        if isinstance(assignment, dict) and assignment.get("collaboration") is not None:
            projected_summary = self._observable_collaboration_projection(
                session_id,
                summary,
            )
            summary = (
                projected_summary
                if isinstance(projected_summary, str)
                else _REDACTED_COLLABORATION_PAYLOAD
            )
        events = await self.storage.list_events(session_id, after=0, limit=5000)
        cursor = events[-1]["seq"] if events else 0
        await self.storage.update_session_context(
            session_id,
            context_summary=summary,
            summary_through_event_seq=cursor,
        )

    @staticmethod
    def _bounded_compaction_value(
        value: Any,
        *,
        depth: int = 0,
    ) -> Any:
        if depth >= 4:
            return "[nested value omitted]"
        if isinstance(value, str):
            if len(value) <= 750:
                return value
            return value[:500] + "…" + value[-200:]
        if isinstance(value, dict):
            keys = sorted(value, key=str)[:30]
            projected = {
                str(key): LocalLiliesService._bounded_compaction_value(
                    value[key], depth=depth + 1
                )
                for key in keys
            }
            if len(value) > len(keys):
                projected["_omitted_field_count"] = len(value) - len(keys)
            return projected
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            projected = [
                LocalLiliesService._bounded_compaction_value(
                    item, depth=depth + 1
                )
                for item in items[-12:]
            ]
            if len(items) > len(projected):
                projected.insert(0, {"_omitted_item_count": len(items) - len(projected)})
            return projected
        if isinstance(value, (int, float, bool, type(None))):
            return value
        return str(value)[:750]

    @staticmethod
    def _compaction_assignment_projection(assignment: Any) -> str:
        if not isinstance(assignment, dict):
            return ""
        projected = LocalLiliesService._bounded_compaction_value(assignment)
        if not isinstance(projected, dict):  # pragma: no cover - invariant guard
            return ""
        requirement = assignment.get("requirement")
        if isinstance(requirement, str):
            projected["requirement_sha256"] = hashlib.sha256(
                requirement.encode("utf-8")
            ).hexdigest()
        business_context = assignment.get("business_context")
        if isinstance(business_context, dict):
            business_goal = business_context.get("business_goal")
            if isinstance(business_goal, str):
                projected_business_context = projected.get("business_context")
                if not isinstance(projected_business_context, dict):
                    projected_business_context = {}
                    projected["business_context"] = projected_business_context
                # This field is a locked compaction invariant (max 10k), not
                # ordinary conversational detail.  Preserve the decisive
                # middle as well as a full-value integrity digest.
                projected_business_context["business_goal"] = business_goal
                projected_business_context["business_goal_sha256"] = (
                    hashlib.sha256(business_goal.encode("utf-8")).hexdigest()
                )
        projected["assignment_sha256"] = hashlib.sha256(
            json.dumps(
                assignment,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return json.dumps(
            projected,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _compaction_value_digest(value: Any) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _compaction_compact_digest(value: Any) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        encoded = base64.urlsafe_b64encode(
            hashlib.sha256(canonical.encode("utf-8")).digest()
        ).rstrip(b"=")
        return "sha256-b64:" + encoded.decode("ascii")

    @staticmethod
    def _compaction_complex_summary(name: str, value: Any) -> Any:
        if not isinstance(value, list):
            return LocalLiliesService._bounded_compaction_value(value)
        summary: dict[str, Any] = {
            "count": len(value),
            "digest": LocalLiliesService._compaction_value_digest(value),
        }
        if name == "attempted_routes":
            # Keep one compact, stable row for every attempted route.  The
            # detailed sample below remains useful to a model, while this
            # index prevents older attempts from disappearing at the
            # one-hundred-attempt contract ceiling.
            summary["index_schema"] = [
                "attempt_id",
                "route",
                "outcome",
                "evidence_count",
                "semantic_digest",
            ]
            summary["index"] = [
                [
                    str(item.get("attempt_id", ordinal))[:36],
                    str(item.get("route", ""))[:24],
                    str(item.get("outcome", ""))[:24],
                    (
                        len(item.get("evidence_refs", []))
                        if isinstance(item.get("evidence_refs"), list)
                        else int(item.get("evidence_ref") is not None)
                    ),
                    LocalLiliesService._compaction_compact_digest(item),
                ]
                if isinstance(item, dict)
                else [
                    str(ordinal),
                    "",
                    "",
                    0,
                    LocalLiliesService._compaction_compact_digest(item),
                ]
                for ordinal, item in enumerate(value)
            ]
        sample: list[Any] = []
        for item in value[-12:]:
            if not isinstance(item, dict):
                sample.append(LocalLiliesService._bounded_compaction_value(item))
                continue
            scalar_keys = {
                "attempt_id",
                "route",
                "input_digest",
                "outcome",
                "attempted_at",
                "evidence_id",
                "check_id",
                "kind",
                "digest",
                "label",
                "order",
                "action",
                "expected",
                "actual",
                "test_id",
                "command",
                "exit_code",
                "summary",
                "status",
                "name",
            }
            projected = {
                key: (
                    str(item[key])[:500]
                    if isinstance(item[key], str)
                    else item[key]
                )
                for key in scalar_keys
                if key in item and item[key] is not None
            }
            evidence = item.get("evidence_refs")
            if evidence is None and item.get("evidence_ref") is not None:
                evidence = [item["evidence_ref"]]
            if isinstance(evidence, list):
                projected["evidence_count"] = len(evidence)
                projected["evidence_digest"] = (
                    LocalLiliesService._compaction_value_digest(evidence)
                )
                projected["evidence_ids"] = [
                    str(ref.get("evidence_id"))[:200]
                    for ref in evidence
                    if isinstance(ref, dict) and ref.get("evidence_id") is not None
                ][:12]
            sample.append(projected)
        if sample:
            summary["sample"] = sample
        return summary

    @staticmethod
    def _compaction_claim_state(claim: dict[str, Any]) -> dict[str, Any]:
        complex_fields = {
            "test_run_ids",
            "business_run_ids",
            "artifact_refs",
            "host_receipt_refs",
            "resolved_report_ids",
            "remaining_limits",
            "differences",
            "evidence_refs",
        }
        exact_small_identifier_lists = {
            "test_run_ids",
            "business_run_ids",
            "resolved_report_ids",
            "remaining_limits",
        }
        return {
            key: (
                list(value)
                if key in exact_small_identifier_lists
                and isinstance(value, list)
                and len(value) <= 12
                else LocalLiliesService._compaction_complex_summary(key, value)
                if key in complex_fields
                else LocalLiliesService._bounded_compaction_value(value)
            )
            for key, value in claim.items()
        }

    @staticmethod
    def _compaction_report_state(report: dict[str, Any]) -> dict[str, Any]:
        complex_fields = {
            "manuals_checked",
            "attempted_routes",
            "reproduction",
            "independent_work",
            "workaround_considered",
            "secret_redactions",
            "evidence_refs",
            "generic_capability_changes",
            "tests_run",
            "browser_or_live_evidence",
            "reprobe_steps",
            "health_checks",
            "differences",
        }
        projected = {
            key: (
                LocalLiliesService._compaction_complex_summary(key, value)
                if key in complex_fields
                else value
                if key == "original_goal" and isinstance(value, str)
                else LocalLiliesService._bounded_compaction_value(value)
            )
            for key, value in report.items()
        }
        serialized = json.dumps(
            projected, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        if len(serialized) > 18_000:
            for key in complex_fields:
                summary = projected.get(key)
                if not isinstance(summary, dict) or "sample" not in summary:
                    continue
                keep = 4 if key == "attempted_routes" else 2
                summary["sample"] = summary["sample"][-keep:]
        return projected

    @staticmethod
    def _minimal_compaction_report_state(report: dict[str, Any]) -> dict[str, Any]:
        """Reduce detail without removing an unresolved report or its invariants."""

        scalar_fields = (
            "report_id",
            "category",
            "status",
            "phase",
            "severity",
            "route",
            "revision",
            "original_goal",
            "requirement_digest",
            "platform_contract_digest",
            "new_contract_digest",
            "causal_parent_id",
            "source_message_id",
            "summary",
        )
        projected: dict[str, Any] = {
            key: report[key] for key in scalar_fields if key in report
        }
        for key in (
            "manuals_checked",
            "attempted_routes",
            "reproduction",
            "independent_work",
            "workaround_considered",
            "secret_redactions",
            "evidence_refs",
            "generic_capability_changes",
            "tests_run",
            "browser_or_live_evidence",
            "reprobe_steps",
            "health_checks",
            "differences",
        ):
            summary = report.get(key)
            if not isinstance(summary, dict):
                continue
            compact = {
                name: summary[name]
                for name in ("count", "digest")
                if name in summary
            }
            if key == "attempted_routes":
                for name in ("index_schema", "index"):
                    if name in summary:
                        compact[name] = summary[name]
                if summary.get("sample"):
                    compact["sample"] = summary["sample"][-1:]
            projected[key] = compact
        projected["report_state_digest"] = (
            LocalLiliesService._compaction_value_digest(report)
        )
        projected["_summary_only"] = True
        return projected

    @staticmethod
    def _minimal_compaction_claim_state(claim: dict[str, Any]) -> dict[str, Any]:
        scalar_fields = (
            "claim_id",
            "status",
            "claim_revision",
            "application_id",
            "draft_revision",
            "content_hash",
            "published_version",
            "verdict",
            "oracle_digest",
            "invalidation_reason",
        )
        projected: dict[str, Any] = {
            key: claim[key] for key in scalar_fields if key in claim
        }
        for key in ("test_run_ids", "business_run_ids", "differences", "evidence_refs"):
            summary = claim.get(key)
            if isinstance(summary, dict):
                projected[key] = {
                    name: summary[name]
                    for name in ("count", "digest")
                    if name in summary
                }
        projected["claim_state_digest"] = (
            LocalLiliesService._compaction_value_digest(claim)
        )
        projected["_summary_only"] = True
        return projected

    @staticmethod
    def _compaction_invariants(
        session: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        reports: dict[str, dict[str, Any]] = {}
        claims: dict[str, dict[str, Any]] = {}
        report_seen: dict[str, int] = {}
        claim_seen: dict[str, int] = {}
        workflow_state: list[dict[str, Any]] = []
        decisions: list[str] = []
        seen_counter = 0
        report_fields = {
            "report_id",
            "category",
            "status",
            "phase",
            "severity",
            "route",
            "revision",
            "original_goal",
            "summary",
            "platform_contract_digest",
            "manuals_checked",
            "expected",
            "actual",
            "attempted_routes",
            "reproduction",
            "missing_contract",
            "independent_work",
            "evidence_refs",
            "blocking_scope",
            "workaround_considered",
            "workaround_loss",
            "requested_outcome",
            "confidence",
            "secret_redactions",
            "outcome",
            "implementation_summary",
            "generic_capability_changes",
            "tests_run",
            "commit_sha",
            "browser_or_live_evidence",
            "reprobe_steps",
            "new_contract_digest",
            "known_limits",
            "changes",
            "reason",
            "prior_task_revision",
            "previous_task_revision",
            "new_task_revision",
            "previous_requirement_digest",
            "new_requirement_digest",
            "environment_digest",
            "health_checks",
            "requirement_digest",
            "source_message_id",
            "correlation_id",
            "causal_parent_id",
            "differences",
        }
        claim_fields = {
            "claim_id",
            "status",
            "claim_revision",
            "verification_id",
            "verdict",
            "oracle_digest",
            "application_id",
            "draft_revision",
            "content_hash",
            "published_version",
            "test_run_ids",
            "business_run_ids",
            "artifact_refs",
            "host_receipt_refs",
            "resolved_report_ids",
            "remaining_limits",
            "differences",
            "evidence_refs",
            "reason",
            "invalidation_reason",
            "invalidated_at",
            "message_id",
            "correlation_id",
            "causal_parent_id",
        }
        claim_complex_fields = {
            "test_run_ids",
            "business_run_ids",
            "artifact_refs",
            "host_receipt_refs",
            "resolved_report_ids",
            "remaining_limits",
            "differences",
            "evidence_refs",
        }
        workflow_fields = {
            "application_id",
            "draft_id",
            "draft_revision",
            "content_hash",
            "run_id",
            "test_run_ids",
            "business_run_ids",
            "evidence_refs",
            "remaining_limits",
            "contract_digest",
            "new_contract_digest",
            "generic_capability_changes",
            "tests_run",
            "commit_sha",
            "browser_or_live_evidence",
            "reprobe_steps",
            "known_limits",
            "changes",
            "reason",
            "prior_task_revision",
            "previous_task_revision",
            "new_task_revision",
            "previous_requirement_digest",
            "new_requirement_digest",
            "environment_digest",
            "health_checks",
            "verdict",
            "findings",
            "remaining_gaps",
            "differences",
        }
        workflow_anchor_fields = {
            "application_id",
            "draft_id",
            "draft_revision",
            "content_hash",
            "run_id",
            "test_run_ids",
            "business_run_ids",
            "contract_digest",
            "new_contract_digest",
            "commit_sha",
            "verdict",
            "findings",
            "remaining_gaps",
            "new_task_revision",
            "environment_digest",
        }
        report_message_types = {
            "report",
            "evidence",
            "approval",
            "developer_response",
            "task_amendment",
            "environment_response",
            "lilies_reprobe_result",
        }

        def parse_embedded_json(value: str) -> Any | None:
            stripped = value.strip()
            candidates = [stripped]
            marker = "Durable collaboration updates for the current formal task:\n"
            if marker in value:
                candidates.insert(0, value.split(marker, 1)[1].strip())
            for candidate in candidates:
                if candidate[:1] not in {"{", "["}:
                    continue
                try:
                    return json.loads(candidate)
                except (TypeError, ValueError):
                    continue
            return None

        def inspect_value(value: Any, inherited_report_id: str | None = None) -> None:
            nonlocal seen_counter
            if isinstance(value, dict):
                seen_counter += 1
                message_type = str(value.get("message_type", ""))
                payload_schema = str(value.get("payload_schema", ""))
                payload = value.get("payload")
                typed_payload = payload if isinstance(payload, dict) else value
                report_id = value.get("report_id")
                if report_id is None and message_type in report_message_types:
                    report_id = value.get("correlation_id")
                if (
                    report_id is None
                    and payload_schema == "collaboration.lilies_reprobe_result.v1"
                ):
                    report_id = value.get("correlation_id")
                if report_id is None:
                    report_id = inherited_report_id
                if report_id is not None:
                    report_key = str(report_id)
                    projection = {
                        key: value[key]
                        for key in report_fields
                        if key in value and value[key] is not None
                    }
                    projection.setdefault("report_id", report_key)
                    reports.setdefault(report_key, {}).update(
                        {
                            key: (
                                item
                                if key == "original_goal"
                                or key
                                in {
                                    "manuals_checked",
                                    "attempted_routes",
                                    "reproduction",
                                    "independent_work",
                                    "workaround_considered",
                                    "secret_redactions",
                                    "evidence_refs",
                                    "generic_capability_changes",
                                    "tests_run",
                                    "browser_or_live_evidence",
                                    "reprobe_steps",
                                    "health_checks",
                                    "differences",
                                }
                                else LocalLiliesService._bounded_compaction_value(item)
                            )
                            for key, item in projection.items()
                        }
                    )
                    report_seen[report_key] = seen_counter
                claim_id = value.get("claim_id")
                if claim_id is None and message_type == "verification_result":
                    claim_id = value.get("correlation_id")
                if (
                    claim_id is None
                    and isinstance(payload, dict)
                    and payload.get("kind") == "claim_invalidated"
                ):
                    claim_id = payload.get("claim_id")
                if claim_id is not None:
                    claim_key = str(claim_id)
                    projection = {
                        key: value[key]
                        for key in claim_fields
                        if key in value and value[key] is not None
                    }
                    projection.update(
                        {
                            key: typed_payload[key]
                            for key in claim_fields
                            if key in typed_payload
                            and typed_payload[key] is not None
                            and key != "claim_id"
                        }
                    )
                    projection.setdefault("claim_id", claim_key)
                    current_claim = claims.setdefault(claim_key, {})
                    for key, item in projection.items():
                        if key == "claim_revision":
                            prior = current_claim.get(key)
                            if (
                                isinstance(item, int)
                                and not isinstance(item, bool)
                                and isinstance(prior, int)
                                and not isinstance(prior, bool)
                            ):
                                current_claim[key] = max(prior, item)
                            else:
                                current_claim[key] = item
                        elif (
                            key == "status"
                            and current_claim.get("status")
                            in {
                                "invalidated",
                                "independently_verified",
                                "verification_failed",
                            }
                            and item == "frozen"
                        ):
                            continue
                        elif key in claim_complex_fields:
                            current_claim[key] = item
                        else:
                            current_claim[key] = (
                                LocalLiliesService._bounded_compaction_value(item)
                            )
                    claim_seen[claim_key] = seen_counter

                if report_id is not None:
                    current_report = reports[str(report_id)]
                    resulting_revision = typed_payload.get("resulting_report_revision")
                    if not isinstance(resulting_revision, int) or isinstance(
                        resulting_revision, bool
                    ):
                        prior_revision = typed_payload.get("report_revision")
                        resulting_revision = (
                            prior_revision + 1
                            if isinstance(prior_revision, int)
                            and not isinstance(prior_revision, bool)
                            else None
                        )
                    derived_status: str | None = None
                    decision_value = typed_payload.get("decision")
                    if message_type == "approval" or decision_value in {
                        "approve",
                        "reject",
                        "needs_more_evidence",
                    }:
                        derived_status = {
                            "approve": "approved_for_codex",
                            "reject": "rejected",
                            "needs_more_evidence": "needs_more_evidence",
                        }.get(str(decision_value))
                    elif message_type == "developer_response":
                        derived_status = (
                            "ready_for_lilies_verification"
                            if typed_payload.get("outcome") == "implemented"
                            else "evidence_collecting"
                        )
                    elif message_type == "task_amendment":
                        derived_status = (
                            "task_package_amended"
                            if typed_payload.get("outcome") == "amended"
                            else "rejected_with_evidence"
                        )
                    elif message_type == "environment_response":
                        derived_status = (
                            "environment_restored"
                            if typed_payload.get("outcome") == "restored"
                            else "unresolved"
                        )
                    elif payload_schema == "collaboration.lilies_reprobe_result.v1":
                        verified = typed_payload.get("outcome") == "lilies_verified"
                        category = current_report.get("category")
                        if category in {
                            "platform_capability_gap",
                            "platform_defect_suspected",
                        }:
                            derived_status = (
                                "lilies_verified" if verified else "verification_failed"
                            )
                        elif category == "task_spec_gap":
                            derived_status = (
                                "lilies_rechecks" if verified else "routed_to_task_author"
                            )
                        elif category == "environment_gap":
                            derived_status = (
                                "lilies_health_checks" if verified else "environment_failed"
                            )
                    if derived_status is not None:
                        current_report["status"] = derived_status
                    if isinstance(resulting_revision, int):
                        current_report["revision"] = resulting_revision

                if claim_id is not None and message_type == "verification_result":
                    current_claim = claims[str(claim_id)]
                    verdict = typed_payload.get("verdict")
                    if verdict in {"independently_verified", "verification_failed"}:
                        current_claim["status"] = verdict
                    prior_claim_revision = typed_payload.get("claim_revision")
                    if isinstance(prior_claim_revision, int) and not isinstance(
                        prior_claim_revision, bool
                    ):
                        current_claim["claim_revision"] = prior_claim_revision + 1
                if (
                    claim_id is not None
                    and message_type == "control"
                    and typed_payload.get("kind") == "claim_invalidated"
                ):
                    current_claim = claims[str(claim_id)]
                    current_claim["status"] = "invalidated"
                    invalidation_reason = typed_payload.get(
                        "invalidation_reason", typed_payload.get("reason")
                    )
                    if invalidation_reason is not None:
                        current_claim["invalidation_reason"] = (
                            LocalLiliesService._bounded_compaction_value(
                                invalidation_reason
                            )
                        )
                    prior_claim_revision = current_claim.get("claim_revision")
                    if isinstance(prior_claim_revision, int) and not isinstance(
                        prior_claim_revision, bool
                    ):
                        current_claim["claim_revision"] = prior_claim_revision + 1
                workflow_projection = {
                    key: LocalLiliesService._bounded_compaction_value(value[key])
                    for key in workflow_fields
                    if key in value and value[key] is not None
                }
                if (
                    workflow_projection
                    and any(key in value for key in workflow_anchor_fields)
                    and workflow_projection not in workflow_state
                ):
                    workflow_state.append(workflow_projection)
                decision = value.get("decision")
                if decision in {"approve", "reject", "needs_more_evidence", "denied"}:
                    decisions.append(
                        json.dumps(
                            LocalLiliesService._bounded_compaction_value(
                                {
                                    "decision": decision,
                                    "report_id": report_id,
                                    "reason": value.get("reason"),
                                    "message_id": value.get("message_id"),
                                }
                            ),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )
                for key, child in value.items():
                    inspect_value(
                        child,
                        (
                            str(report_id)
                            if key == "payload" and report_id is not None
                            else None
                        ),
                    )
            elif isinstance(value, list):
                for child in value:
                    inspect_value(child, inherited_report_id)
            elif isinstance(value, str):
                parsed = parse_embedded_json(value)
                if parsed is not None:
                    inspect_value(parsed, inherited_report_id)

        for item in messages:
            for block in item.get("content", []):
                inspect_value(block)
                if block.get("type") == "text" and item.get("role") == "user":
                    text = str(block.get("text") or "")
                    folded = text.casefold()
                    if any(
                        marker in folded
                        for marker in (
                            "approve",
                            "approved",
                            "reject",
                            "denied",
                            "permission",
                            "批准",
                            "拒绝",
                            "授权",
                        )
                    ):
                        decisions.append(
                            LocalLiliesService._bounded_compaction_value(text)
                        )

        ordered_reports = sorted(reports, key=lambda key: report_seen.get(key, 0))
        ordered_claims = sorted(claims, key=lambda key: claim_seen.get(key, 0))
        terminal_reports = {"independently_verified", "withdrawn", "rejected"}
        active_reports = [
            LocalLiliesService._compaction_report_state(reports[key])
            for key in ordered_reports
            if str(reports[key].get("status", "")) not in terminal_reports
        ]
        indexed_report_ids = [
            report_id
            for report_id in ordered_reports
            if str(reports[report_id].get("status", "")) not in terminal_reports
        ]
        report_categories = sorted(
            {
                str(reports[report_id].get("category", ""))
                for report_id in indexed_report_ids
            }
        )
        report_statuses = sorted(
            {
                str(reports[report_id].get("status", ""))
                for report_id in indexed_report_ids
            }
        )

        def compact_uuid(value: Any) -> Any:
            if value is None:
                return None
            try:
                return UUID(str(value)).hex
            except (TypeError, ValueError):
                return str(value)

        def compact_attempted_routes(report: dict[str, Any]) -> list[list[Any]]:
            attempts = report.get("attempted_routes")
            if not isinstance(attempts, list):
                return []
            compact: list[list[Any]] = []
            for ordinal, attempt in enumerate(attempts):
                if not isinstance(attempt, dict):
                    compact.append(
                        [
                            str(ordinal),
                            "",
                            "",
                            [],
                            0,
                            LocalLiliesService._compaction_compact_digest(attempt),
                        ]
                    )
                    continue
                evidence = attempt.get("evidence_refs")
                evidence = evidence if isinstance(evidence, list) else []
                evidence_ids = [
                    str(ref.get("evidence_id"))
                    for ref in evidence
                    if isinstance(ref, dict) and ref.get("evidence_id") is not None
                ]
                if len(evidence_ids) > 12:
                    evidence_ids = evidence_ids[:6] + evidence_ids[-6:]

                def bounded_semantic_text(value: Any) -> str:
                    text = str(value or "")
                    return text if len(text) <= 120 else text[:80] + "…" + text[-32:]

                compact.append(
                    [
                        str(attempt.get("attempt_id", ordinal))[:36],
                        bounded_semantic_text(attempt.get("route")),
                        bounded_semantic_text(attempt.get("outcome")),
                        evidence_ids,
                        len(evidence),
                        (
                            LocalLiliesService._compaction_compact_digest(evidence)
                            if len(evidence) > len(evidence_ids)
                            else None
                        ),
                    ]
                )
            return compact

        report_attempts = {
            report_id: compact_attempted_routes(reports[report_id])
            for report_id in indexed_report_ids
        }
        report_index_schema = [
            "report_id_uuid_hex",
            "category_code",
            "status_code",
            "revision",
            "causal_parent_uuid_hex",
            "original_goal_or_digest",
            "original_goal_is_digest",
            "attempted_routes",
        ]
        report_attempt_schema = [
            "attempt_id",
            "route",
            "outcome",
            "evidence_ids_sample",
            "evidence_count",
            "evidence_digest",
        ]
        report_index = [
            [
                compact_uuid(reports[report_id].get("report_id", report_id)),
                report_categories.index(str(reports[report_id].get("category", ""))),
                report_statuses.index(str(reports[report_id].get("status", ""))),
                reports[report_id].get("revision"),
                compact_uuid(reports[report_id].get("causal_parent_id")),
                (
                    LocalLiliesService._compaction_compact_digest(
                        reports[report_id].get("original_goal")
                    )
                    if reports[report_id].get("original_goal") is not None
                    else None
                ),
                reports[report_id].get("original_goal") is not None,
                None,
            ]
            for report_id in indexed_report_ids
        ]
        assignment = session.get("assignment")
        assignment_constraints = (
            assignment.get("constraints") if isinstance(assignment, dict) else None
        )
        result = {
            "platform_contract_digest": session.get("platform_contract_digest"),
            "no_substitute_validation": (
                assignment_constraints.get("no_substitute_validation")
                if isinstance(assignment_constraints, dict)
                else None
            ),
            "waiting_collaboration_id": session.get("waiting_collaboration_id"),
            "last_platform_cursor": int(session.get("last_platform_cursor", 0)),
            "last_pipeline_cursor": int(session.get("last_pipeline_cursor", 0)),
            "reports": active_reports,
            "report_index_schema": report_index_schema,
            "report_category_codes": report_categories,
            "report_status_codes": report_statuses,
            "report_attempt_schema": report_attempt_schema,
            "report_index": report_index,
            "report_index_digest": LocalLiliesService._compaction_value_digest(
                report_index
            ),
            "report_count": len(reports),
            "report_index_omitted": 0,
            "report_detail_omitted": 0,
            "claims": [
                LocalLiliesService._compaction_claim_state(claims[key])
                for key in ordered_claims
            ],
            "claim_count": len(claims),
            "workflow_state": workflow_state,
            "user_decisions": decisions,
        }
        state_digest = hashlib.sha256(
            json.dumps(
                {
                    "reports": reports,
                    "claims": claims,
                    "workflow_state": workflow_state,
                    "user_decisions": decisions,
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        result["state_digest"] = f"sha256:{state_digest}"
        if len(
            json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        ) > 30_000 and result["report_index"] and result["reports"]:
            without_duplicate_index = dict(result)
            without_duplicate_index["report_index"] = []
            without_duplicate_index["report_index_schema"] = []
            without_duplicate_index["report_index_digest"] = None
            if len(
                json.dumps(
                    without_duplicate_index,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            ) <= 30_000:
                result["report_index_omitted"] += len(result["report_index"])
                result["report_index"] = []
                result["report_index_schema"] = []
                result["report_index_digest"] = None
        while len(
            json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        ) > 30_000:
            report_reductions: list[tuple[int, dict[str, Any], int]] = []
            for index, report in enumerate(result["reports"]):
                if report.get("_summary_only"):
                    continue
                minimal = LocalLiliesService._minimal_compaction_report_state(report)
                saving = len(
                    json.dumps(report, ensure_ascii=False, separators=(",", ":"))
                ) - len(
                    json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
                )
                if saving > 0:
                    report_reductions.append((index, minimal, saving))
            if report_reductions:
                index, minimal, _ = max(
                    report_reductions, key=lambda item: item[2]
                )
                result["reports"][index] = minimal
                continue

            claim_reductions: list[tuple[int, dict[str, Any], int]] = []
            for index, claim in enumerate(result["claims"]):
                if claim.get("_summary_only"):
                    continue
                minimal = LocalLiliesService._minimal_compaction_claim_state(claim)
                saving = len(
                    json.dumps(claim, ensure_ascii=False, separators=(",", ":"))
                ) - len(
                    json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
                )
                if saving > 0:
                    claim_reductions.append((index, minimal, saving))
            if claim_reductions:
                index, minimal, _ = max(
                    claim_reductions, key=lambda item: item[2]
                )
                result["claims"][index] = minimal
                continue

            if result["reports"]:
                # The all-report index retains identity, status, causal parent,
                # exact goal and an attempt/evidence digest.  Evict only the
                # duplicate detailed copy when the global bound requires it.
                evicted = result["reports"].pop(0)
                evicted_id = str(evicted.get("report_id", ""))
                for row in result["report_index"]:
                    if str(row[0]) != str(compact_uuid(evicted_id)):
                        continue
                    if evicted.get("original_goal") is not None:
                        row[5] = evicted["original_goal"]
                        row[6] = False
                    row[7] = report_attempts.get(evicted_id, [])
                    break
                result["report_detail_omitted"] += 1
                continue
            break
        return result

    async def _close_uncertain_tool_calls(self, session_id: str) -> None:
        messages = await self.storage.list_messages_for_compaction(session_id)
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

    @staticmethod
    def _is_collaboration_tool(tool_name: object) -> bool:
        return isinstance(tool_name, str) and tool_name.startswith(
            _COLLABORATION_TOOL_PREFIX
        )

    @staticmethod
    def _is_collaboration_mutation(tool_name: str) -> bool:
        return tool_name in {
            "collaboration_report_submit",
            "collaboration_verification_claim",
        }

    def _redact_registered_secret_values(
        self,
        session_id: str | None,
        value: Any,
    ) -> Any:
        """Remove daemon-known credentials before a collaboration projection.

        The shared collaboration sanitizer recognises credential fields and
        credential formats.  This extra in-memory registry also catches an
        exact provisioned bearer if a model copies it into an innocently named
        field.  Credential values remain registered only in process memory.
        """

        if isinstance(value, dict):
            return {
                str(self._redact_registered_secret_values(session_id, key)): (
                    self._redact_registered_secret_values(session_id, item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [
                self._redact_registered_secret_values(session_id, item)
                for item in value
            ]
        if not isinstance(value, str):
            return value
        redacted = value
        for secret in sorted(
            self._registered_secret_values.get(session_id or "", ()),
            key=len,
            reverse=True,
        ):
            if secret in redacted:
                redacted = redacted.replace(secret, _REDACTED_COLLABORATION_PAYLOAD)
        return redacted

    def _sanitize_collaboration_projection(
        self,
        session_id: str | None,
        value: Any,
    ) -> Any:
        registered_redacted = self._redact_registered_secret_values(
            session_id,
            value,
        )
        sanitized = sanitize_collaboration_payload(registered_redacted)
        return validate_collaboration_payload_safety(sanitized)

    def _observable_collaboration_projection(
        self,
        session_id: str | None,
        value: Any,
    ) -> Any:
        """Build a fail-closed projection for durable and UI-visible surfaces."""

        try:
            return self._sanitize_collaboration_projection(session_id, value)
        except ValueError:
            if isinstance(value, dict):
                metadata = {
                    key: item
                    for key, item in value.items()
                    if key
                    in {
                        "turn_id",
                        "tool_call_id",
                        "tool",
                        "tool_name",
                        "is_error",
                        "resumed_after_permission",
                    }
                    and isinstance(item, (str, int, float, bool, type(None)))
                }
                metadata.update(_REJECTED_COLLABORATION_INPUT)
                return metadata
            return _REDACTED_COLLABORATION_PAYLOAD

    def _sanitize_collaboration_tool_outcome(
        self,
        session_id: str,
        outcome: LiliesToolResult,
    ) -> LiliesToolResult:
        """Prevent an HTTP/error echo from reintroducing sensitive material."""

        try:
            decoded = json.loads(outcome.content)
        except (TypeError, ValueError):
            decoded = None
        try:
            if decoded is None:
                projected = self._sanitize_collaboration_projection(
                    session_id,
                    outcome.content,
                )
                content = (
                    projected
                    if isinstance(projected, str)
                    else json.dumps(
                        projected,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            else:
                projected = self._sanitize_collaboration_projection(
                    session_id,
                    decoded,
                )
                content = json.dumps(
                    projected,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
        except ValueError:
            content = json.dumps(
                {
                    "ok": False,
                    "status_code": 422,
                    "error": {
                        "code": "unsafe_collaboration_result",
                        "message": "collaboration result was rejected by daemon safety policy",
                        "retryable": False,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return LiliesToolResult(content, is_error=True)
        return LiliesToolResult(content, is_error=outcome.is_error)

    async def _emit(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        tool_name = data.get("tool", data.get("tool_name"))
        if self._is_collaboration_tool(tool_name):
            projected = self._observable_collaboration_projection(session_id, data)
            if not isinstance(projected, dict):  # pragma: no cover - invariant guard
                projected = dict(_REJECTED_COLLABORATION_INPUT)
            data = projected
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
