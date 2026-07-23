from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .collaboration_models import (
    CollaborationChannel,
    SafePayloadModel,
    sanitize_collaboration_payload,
)


class _StrictProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StudioPermissionContext(SafePayloadModel):
    request_id: UUID
    tool_name: str = Field(min_length=1, max_length=160)
    input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    redacted_input: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending"] = "pending"


class StudioDeliverableContext(SafePayloadModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=2_000)
    media_type: str = Field(min_length=1, max_length=200)
    required: bool = True


class StudioTaskPackageContext(_StrictProjection):
    task_id: str = Field(min_length=1, max_length=120)
    revision: int = Field(ge=1)
    public_summary_digest: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


class StudioCompactionContext(SafePayloadModel):
    summary: str = Field(min_length=1, max_length=100_000)
    summary_through_event_seq: int = Field(ge=1)


class StudioObservableEvent(SafePayloadModel):
    seq: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=200)
    kind: Literal[
        "message",
        "tool",
        "permission",
        "context",
        "assignment",
        "session",
    ]
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(default="", max_length=10_000)
    status: str = Field(default="", max_length=120)
    actor: str = Field(default="", max_length=120)
    tool_name: str | None = Field(default=None, max_length=160)
    tool_call_id: str | None = Field(default=None, max_length=300)
    duration_ms: float | None = Field(default=None, ge=0)
    redacted_input: dict[str, Any] = Field(default_factory=dict)
    permission_request: StudioPermissionContext | None = None
    permission_request_id: str | None = Field(default=None, max_length=100)
    evidence_refs: list[str] = Field(default_factory=list, max_length=500)
    created_at: datetime


class StudioTraceEvent(_StrictProjection):
    seq: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=200)
    node_id: str = Field(default="", max_length=300)
    title: str = Field(default="", max_length=300)
    status: str = Field(default="", max_length=120)
    created_at: str = Field(default="", max_length=100)


class StudioRunContext(_StrictProjection):
    run_id: UUID
    status: str = Field(min_length=1, max_length=120)
    draft_revision: int | None = Field(default=None, ge=0)
    error: str = Field(default="", max_length=2_000)
    created_at: str = Field(default="", max_length=100)
    updated_at: str = Field(default="", max_length=100)
    trace: list[StudioTraceEvent] = Field(default_factory=list, max_length=100)


class StudioDraftContext(_StrictProjection):
    revision: int = Field(ge=0)
    content_hash: str = Field(min_length=1, max_length=200)
    tested_hash: str | None = Field(default=None, max_length=200)
    evidence_state: str = Field(default="missing", max_length=120)
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    test_count: int = Field(ge=0)
    tests_passed: int = Field(default=0, ge=0)
    tests_failed: int = Field(default=0, ge=0)


class StudioApplicationContext(_StrictProjection):
    application_id: UUID
    name: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=2_000)
    draft: StudioDraftContext
    runs: list[StudioRunContext] = Field(default_factory=list, max_length=20)


class StudioAssignmentContext(_StrictProjection):
    task_id: str = Field(min_length=1, max_length=120)
    task_revision: int = Field(ge=1)
    assignment_id: UUID
    application_id: UUID
    build_id: UUID
    session_id: UUID
    connection_id: UUID
    phase: str = Field(min_length=1, max_length=120)
    status: str = Field(min_length=1, max_length=120)
    desired_state: str = Field(min_length=1, max_length=120)
    daemon_status: str | None = Field(default=None, max_length=120)
    connection_status: str = Field(default="unknown", max_length=120)
    requirement: str = Field(min_length=1, max_length=30_000)
    business_context: dict[str, Any] = Field(default_factory=dict)
    task_package: StudioTaskPackageContext | None = None
    deliverables: list[StudioDeliverableContext] = Field(
        default_factory=list,
        max_length=100,
    )
    compaction: StudioCompactionContext | None = None
    contract_digest: str | None = Field(default=None, max_length=200)
    allowed_actions: list[str] = Field(default_factory=list, max_length=500)
    deadline_at: datetime | None = None
    max_turns: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_budget_usd: float | None = Field(default=None, gt=0)
    created_at: datetime
    updated_at: datetime


class StudioCollaborationContext(_StrictProjection):
    schema_version: Literal["1.0"] = "1.0"
    assignment: StudioAssignmentContext | None = None
    observable_events: list[StudioObservableEvent] = Field(
        default_factory=list,
        max_length=2_000,
    )
    applications: list[StudioApplicationContext] = Field(
        default_factory=list,
        max_length=500,
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return {}
    return _mapping(decoded)


def _safe_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    sanitized = sanitize_collaboration_payload(text)
    return str(sanitized)[:limit]


def _without_private_observation_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if (
                normalized
                in {
                    "chain_of_thought",
                    "private_reason",
                    "private_reasoning",
                    "raw_blocks",
                    "reasoning_tokens",
                    "signature",
                    "thinking",
                    "thinking_blocks",
                }
                or "private_reason" in normalized
                or "raw_block" in normalized
                or "thinking" in normalized
            ):
                continue
            projected[str(key)] = _without_private_observation_fields(item)
        return projected
    if isinstance(value, (list, tuple)):
        return [_without_private_observation_fields(item) for item in value]
    return value


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    sanitized = sanitize_collaboration_payload(
        _without_private_observation_fields(value)
    )
    if not isinstance(sanitized, dict):
        return {}
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded) <= 8_000:
        return sanitized
    return {
        "summary": "redacted input is too large for the monitor",
        "digest_only": True,
    }


def _message_text(data: Mapping[str, Any]) -> tuple[str, str]:
    role = str(data.get("role") or "agent")
    fragments: list[str] = []
    for block in data.get("content", []):
        if not isinstance(block, Mapping) or str(block.get("type")) != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            fragments.append(text)
    content = "".join(fragments).strip()
    if role == "user" and "BuildAssignment" in content:
        return role, "正式任务要求已注入；公开需求见任务上下文。"
    return role, _safe_text(content, limit=10_000)


def _tool_result_summary(data: Mapping[str, Any], *, failed: bool) -> tuple[str, list[str]]:
    raw = data.get("content")
    decoded = _json_mapping(raw)
    error = _mapping(decoded.get("error"))
    evidence = [
        str(item)
        for item in decoded.get("evidence_refs", [])
        if isinstance(item, str)
    ][:500]
    if error:
        message = error.get("message") or error.get("code") or "tool failed"
        return _safe_text(message, limit=2_000), evidence
    operation = decoded.get("operation") or data.get("tool") or "tool"
    status = "failed" if failed or decoded.get("ok") is False else "completed"
    return f"{operation} {status}", evidence


def _observable_event(raw: Any) -> StudioObservableEvent | None:
    event_type = str(getattr(raw, "event_type", ""))
    if not event_type or any(
        marker in event_type.casefold()
        for marker in ("thinking", "signature", "private_reason")
    ):
        return None
    seq = int(getattr(raw, "daemon_seq", 0))
    created_at = getattr(raw, "received_at", None)
    if seq < 1 or not isinstance(created_at, datetime):
        return None
    data = _mapping(getattr(raw, "data", {}))

    if event_type == "message.created":
        role, summary = _message_text(data)
        if not summary or role == "tool":
            return None
        return StudioObservableEvent(
            seq=seq,
            event_type=event_type,
            kind="message",
            title="莉莉丝消息" if role == "assistant" else "任务输入",
            summary=summary,
            status="delivered",
            actor=role,
            created_at=created_at,
        )
    if event_type in {"tool.requested", "tool.started", "tool.completed", "tool.failed"}:
        tool = str(data.get("tool") or "unknown tool")[:160]
        failed = event_type == "tool.failed"
        if event_type in {"tool.completed", "tool.failed"}:
            summary, evidence = _tool_result_summary(data, failed=failed)
        else:
            summary = "调用已开始" if event_type == "tool.started" else "准备调用"
            evidence = []
        return StudioObservableEvent(
            seq=seq,
            event_type=event_type,
            kind="tool",
            title=tool,
            summary=summary,
            status="failed" if failed else event_type.rsplit(".", 1)[-1],
            actor="lilies",
            tool_name=tool,
            tool_call_id=(
                str(data.get("tool_call_id"))[:300]
                if data.get("tool_call_id")
                else None
            ),
            redacted_input=_safe_mapping(data.get("input")),
            evidence_refs=evidence,
            created_at=created_at,
        )
    if event_type in {"permission.requested", "permission_required"}:
        request = _mapping(data.get("permission_request")) or data
        try:
            permission = StudioPermissionContext(
                request_id=request.get("request_id"),
                tool_name=request.get("tool_name") or request.get("tool"),
                input_digest=request.get("input_digest"),
                redacted_input=_safe_mapping(
                    request.get("redacted_input") or request.get("input_summary")
                ),
            )
        except ValueError:
            return None
        return StudioObservableEvent(
            seq=seq,
            event_type=event_type,
            kind="permission",
            title="运行权限请求",
            summary=f"{permission.tool_name} 等待用户允许一次或拒绝",
            status="pending",
            actor="lilies",
            tool_name=permission.tool_name,
            redacted_input=permission.redacted_input,
            permission_request=permission,
            created_at=created_at,
        )
    if event_type in {"permission.resolved", "permission.denied"}:
        request_id = str(data.get("request_id") or "")
        behavior = str(
            data.get("behavior")
            or ("deny" if event_type == "permission.denied" else "resolved")
        )
        return StudioObservableEvent(
            seq=seq,
            event_type=event_type,
            kind="permission",
            title="运行权限已处理",
            summary=(
                f"权限请求 {request_id[:8]} 已{('允许一次' if behavior == 'allow' else '拒绝')}"
                if request_id
                else "运行权限请求已处理"
            ),
            status=behavior,
            actor="user",
            tool_name=(
                str(data.get("tool_name") or data.get("tool"))[:160]
                if data.get("tool_name") or data.get("tool")
                else None
            ),
            permission_request_id=request_id[:100] if request_id else None,
            created_at=created_at,
        )
    if event_type.startswith("context.compaction"):
        return StudioObservableEvent(
            seq=seq,
            event_type=event_type,
            kind="context",
            title="上下文压缩",
            summary=_safe_text(
                data.get("summary") or "上下文已压缩并保留事件覆盖范围。",
                limit=10_000,
            ),
            status=event_type.rsplit(".", 1)[-1],
            actor="lilies",
            created_at=created_at,
        )
    if event_type.startswith("assignment."):
        return StudioObservableEvent(
            seq=seq,
            event_type=event_type,
            kind="assignment",
            title="Assignment 状态",
            summary=_safe_text(
                data.get("reason") or event_type.removeprefix("assignment."),
                limit=2_000,
            ),
            status=event_type.rsplit(".", 1)[-1],
            actor="platform",
            created_at=created_at,
        )
    if event_type.startswith("session.") or event_type.startswith("turn."):
        status = data.get("to_status") or data.get("status") or event_type.rsplit(".", 1)[-1]
        return StudioObservableEvent(
            seq=seq,
            event_type=event_type,
            kind="session",
            title="会话状态",
            summary=_safe_text(data.get("reason") or event_type, limit=2_000),
            status=str(status)[:120],
            actor="lilies",
            created_at=created_at,
        )
    return None


def _trace_event(raw: Any) -> StudioTraceEvent | None:
    event_type = str(getattr(raw, "type", ""))
    lowered = event_type.casefold()
    if (
        not event_type
        or any(
            marker in lowered
            for marker in (
                "thinking",
                "signature",
                "private_reason",
                "collaboration",
                "developer",
                "codex",
            )
        )
    ):
        return None
    if not (
        event_type.startswith("workflow.")
        or event_type.startswith("node.")
        or event_type.startswith("permission.")
    ):
        return None
    data = _mapping(getattr(raw, "data", {}))
    return StudioTraceEvent(
        seq=int(getattr(raw, "id", 0)),
        event_type=event_type,
        node_id=str(data.get("node_id") or "")[:300],
        title=str(data.get("title") or data.get("type") or "")[:300],
        status=event_type.rsplit(".", 1)[-1][:120],
        created_at=str(getattr(raw, "created_at", ""))[:100],
    )


async def _application_context(
    workflow_storage: Any,
    application_id: UUID,
) -> StudioApplicationContext | None:
    try:
        application = await workflow_storage.get_application(str(application_id))
        draft = await workflow_storage.get_draft(str(application_id))
    except (KeyError, RuntimeError, ValueError):
        return None
    snapshot = draft.get("snapshot")
    workflow = getattr(snapshot, "workflow", None)
    nodes = list(getattr(workflow, "nodes", []) or [])
    edges = list(getattr(workflow, "edges", []) or [])
    tests = list(getattr(snapshot, "tests", []) or [])
    evidence = _mapping(application.get("evidence"))
    validation = _mapping(evidence.get("last_validation_report"))
    summary = _mapping(validation.get("summary"))

    run_items: list[StudioRunContext] = []
    try:
        runs = await workflow_storage.list_runs(str(application_id), limit=20)
    except (KeyError, RuntimeError, ValueError):
        runs = []
    for run in runs:
        trace: list[StudioTraceEvent] = []
        storage = getattr(workflow_storage, "storage", None)
        if storage is not None and hasattr(storage, "list_events"):
            try:
                raw_trace = await storage.list_events(str(run["id"]), after=0)
            except (KeyError, RuntimeError, ValueError):
                raw_trace = []
            for item in raw_trace[-500:]:
                projected = _trace_event(item)
                if projected is not None:
                    trace.append(projected)
            trace = trace[-100:]
        run_items.append(
            StudioRunContext(
                run_id=run["id"],
                status=str(run.get("status") or "unknown"),
                draft_revision=run.get("draft_revision"),
                error=_safe_text(run.get("error"), limit=2_000),
                created_at=str(run.get("created_at") or ""),
                updated_at=str(run.get("updated_at") or ""),
                trace=trace,
            )
        )
    return StudioApplicationContext(
        application_id=application_id,
        name=str(application.get("name") or application_id),
        description=str(
            application.get("display_description")
            or application.get("description")
            or ""
        )[:2_000],
        draft=StudioDraftContext(
            revision=int(draft.get("revision", 0)),
            content_hash=str(draft.get("content_hash") or "missing"),
            tested_hash=draft.get("tested_hash"),
            evidence_state=str(evidence.get("state") or "missing"),
            node_count=len(nodes),
            edge_count=len(edges),
            test_count=len(tests),
            tests_passed=int(summary.get("passed") or 0),
            tests_failed=int(summary.get("failed") or 0),
        ),
        runs=run_items,
    )


async def build_collaboration_studio_context(
    *,
    channel: CollaborationChannel,
    bridge: Any,
    workflow_storage: Any,
) -> StudioCollaborationContext:
    """Build a strict user-visible projection without raw model/run payloads."""

    assignment_context: StudioAssignmentContext | None = None
    observable_events: list[StudioObservableEvent] = []
    try:
        assignment = await bridge.store.get_assignment(channel.assignment_id)
    except (KeyError, RuntimeError, ValueError):
        assignment = None
    if assignment is not None:
        request = _json_mapping(assignment.get("request_json"))
        submission = _json_mapping(assignment.get("submission_json"))
        authoritative = submission or request
        constraints = _mapping(authoritative.get("constraints"))
        platform = _mapping(authoritative.get("platform"))
        connection_status = "unknown"
        try:
            connection = await bridge.store.get_connection(assignment["connection_id"])
            connection_status = str(connection.get("status") or "unknown")
        except (KeyError, RuntimeError, ValueError):
            pass
        compaction: StudioCompactionContext | None = None
        try:
            session = await bridge.get_assignment_session(channel.assignment_id)
            if (
                session.context_summary
                and session.summary_through_event_seq > 0
            ):
                compaction = StudioCompactionContext(
                    summary=_safe_text(session.context_summary, limit=100_000),
                    summary_through_event_seq=session.summary_through_event_seq,
                )
        except (KeyError, RuntimeError, ValueError):
            pass
        task_package = _mapping(authoritative.get("task_package"))
        task_package_context = (
            StudioTaskPackageContext(
                task_id=task_package.get("task_id"),
                revision=task_package.get("revision"),
                public_summary_digest=task_package.get("public_summary_digest"),
            )
            if task_package
            else None
        )
        deliverables = [
            StudioDeliverableContext.model_validate(item)
            for item in authoritative.get("deliverables", [])
            if isinstance(item, Mapping)
        ][:100]
        assignment_context = StudioAssignmentContext(
            task_id=channel.task_id,
            task_revision=channel.task_revision,
            assignment_id=assignment["assignment_id"],
            application_id=assignment["application_id"],
            build_id=assignment["build_id"],
            session_id=assignment["session_id"],
            connection_id=assignment["connection_id"],
            phase=str(assignment.get("phase") or "unknown"),
            status=str(assignment.get("status") or "unknown"),
            desired_state=str(assignment.get("desired_state") or "unknown"),
            daemon_status=assignment.get("daemon_status"),
            connection_status=connection_status,
            requirement=str(
                authoritative.get("requirement")
                or request.get("requirement")
                or "Formal task requirement unavailable"
            ),
            business_context=_safe_mapping(
                authoritative.get("business_context")
                or request.get("business_context")
            ),
            task_package=task_package_context,
            deliverables=deliverables,
            compaction=compaction,
            contract_digest=platform.get("contract_digest"),
            allowed_actions=[
                str(item)[:200]
                for item in constraints.get("allowed_actions", [])
                if isinstance(item, str)
            ][:500],
            deadline_at=constraints.get("deadline_at"),
            max_turns=constraints.get("max_turns"),
            max_tool_calls=constraints.get("max_tool_calls"),
            max_budget_usd=constraints.get("max_budget_usd"),
            created_at=assignment["created_at"],
            updated_at=assignment["updated_at"],
        )
        try:
            raw_events = await bridge.list_events(channel.assignment_id, after=0)
        except (KeyError, RuntimeError, ValueError):
            raw_events = []
        tool_started_at: dict[str, datetime] = {}
        for raw in raw_events[-10_000:]:
            projected = _observable_event(raw)
            if projected is not None:
                if (
                    projected.kind == "tool"
                    and projected.tool_call_id
                    and projected.event_type == "tool.started"
                ):
                    tool_started_at[projected.tool_call_id] = projected.created_at
                elif (
                    projected.kind == "tool"
                    and projected.tool_call_id
                    and projected.event_type in {"tool.completed", "tool.failed"}
                    and projected.tool_call_id in tool_started_at
                ):
                    duration = (
                        projected.created_at
                        - tool_started_at[projected.tool_call_id]
                    ).total_seconds() * 1_000
                    projected = projected.model_copy(
                        update={"duration_ms": max(0.0, duration)}
                    )
                observable_events.append(projected)
        observable_events = observable_events[-2_000:]

    applications: list[StudioApplicationContext] = []
    for application_id in channel.application_ids:
        projected = await _application_context(workflow_storage, application_id)
        if projected is not None:
            applications.append(projected)
    return StudioCollaborationContext(
        assignment=assignment_context,
        observable_events=observable_events,
        applications=applications,
    )
