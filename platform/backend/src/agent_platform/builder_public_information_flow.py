from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .customer_runtime_projection import project_public_value


_DIGEST_PREFIX = "sha256:"
_BLOCKED_EVENT_MARKERS = (
    "chain_of_thought",
    "collaboration",
    "developer",
    "hidden",
    "model.delta",
    "model.text",
    "oracle",
    "private",
    "prompt",
    "protected",
    "raw",
    "reasoning",
    "secret",
    "signature",
    "thinking",
)
_BLOCKED_TEXT_MARKERS = (
    "expected_answer",
    "hidden-oracle",
    "hidden_oracle",
    "oracle://",
    "protected://",
)
_TRACE_PREFIXES = (
    "budget.",
    "cancellation.",
    "checkpoint.",
    "context.compaction.",
    "contract.",
    "human_input.",
    "loop.",
    "node.",
    "permission.",
    "round_limit.",
    "workflow.",
)
_TRACE_DETAIL_KEYS = frozenset(
    {
        "attempt",
        "behavior",
        "branch",
        "code",
        "duration_ms",
        "error_type",
        "iteration",
        "level",
        "max_iterations",
        "mode",
        "node_id",
        "status",
        "tool",
        "tool_name",
        "type",
    }
)
_AUDIT_DETAIL_KEYS = frozenset(
    {
        "created_application_id",
        "response_digest",
        "status_code",
    }
)


class _FrozenProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BuilderPublicInformationFlowEvent(_FrozenProjection):
    event_id: str = Field(min_length=1, max_length=500)
    source: Literal[
        "blackbox_audit",
        "application",
        "draft",
        "tests",
        "run",
        "trace",
    ]
    kind: Literal[
        "authorization",
        "application",
        "draft",
        "tests",
        "run",
        "trace",
    ]
    event_type: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=1_000)
    application_id: str = Field(min_length=1, max_length=200)
    run_id: str | None = Field(default=None, min_length=1, max_length=200)
    sequence: int | None = Field(default=None, ge=1)
    operation: str | None = Field(default=None, min_length=1, max_length=200)
    created_at: str | None = Field(default=None, min_length=1, max_length=100)
    details: dict[str, Any] = Field(default_factory=dict)


class BuilderPublicInformationFlowCursor(_FrozenProjection):
    audit_after: int = Field(default=0, ge=0)
    trace_after_by_run: dict[str, int] = Field(default_factory=dict)


class BuilderPublicInformationFlowProjection(_FrozenProjection):
    schema_version: Literal["1.0"] = "1.0"
    application_id: str = Field(min_length=1, max_length=200)
    assignment_id: str | None = Field(default=None, min_length=1, max_length=200)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    redacted: Literal[True] = True
    events: list[BuilderPublicInformationFlowEvent] = Field(
        default_factory=list,
        max_length=10_000,
    )
    cursor: BuilderPublicInformationFlowCursor


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _enum_value(item) for key, item in value.items()}


def _response(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    envelope = _mapping(value)
    data = envelope.get("data")
    if isinstance(data, Mapping):
        return _mapping(data), envelope
    return envelope, {}


def _safe_text(value: Any, *, limit: int) -> str:
    value = _enum_value(value)
    projected = project_public_value(value)
    if not isinstance(projected, str):
        return ""
    projected = projected.strip()
    lowered = projected.casefold()
    if any(marker in lowered for marker in _BLOCKED_TEXT_MARKERS):
        return ""
    return projected[:limit]


def _safe_identifier(value: Any, *, limit: int = 200) -> str:
    return _safe_text(value, limit=limit)


def _safe_digest(value: Any) -> str | None:
    text = _safe_identifier(value, limit=100)
    if (
        text.startswith(_DIGEST_PREFIX)
        and len(text) == len(_DIGEST_PREFIX) + 64
        and all(character in "0123456789abcdef" for character in text[7:])
    ):
        return text
    return None


def _safe_created_at(value: Any) -> str | None:
    text = _safe_identifier(value, limit=100)
    return text or None


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    value = _enum_value(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = _safe_text(value, limit=300)
    return text or None


def _safe_details(
    source: Mapping[str, Any],
    allowed: frozenset[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(allowed):
        if key not in source:
            continue
        value = _safe_scalar(source[key])
        if value is not None:
            result[key] = value
    return result


def _count(value: Any) -> int:
    return len(value) if isinstance(value, Sequence) and not isinstance(value, str) else 0


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _status(value: Any, *, default: str) -> str:
    return _safe_identifier(value, limit=120) or default


def _application_matches(data: Mapping[str, Any], application_id: str) -> bool:
    candidate = data.get("application_id") or data.get("id")
    return candidate is None or str(candidate) == application_id


def _audit_events(
    application_id: str,
    records: Sequence[Any],
) -> tuple[list[BuilderPublicInformationFlowEvent], int, str | None, str | None]:
    events: list[BuilderPublicInformationFlowEvent] = []
    audit_after = 0
    assignment_ids: set[str] = set()
    session_ids: set[str] = set()
    for raw in records:
        record = _mapping(raw)
        if str(record.get("application_id") or "") != application_id:
            continue
        seq = _int(record.get("seq"))
        if seq < 1:
            continue
        audit_after = max(audit_after, seq)
        operation = _safe_identifier(record.get("operation"))
        event_type = _safe_identifier(record.get("event_type"))
        lowered = f"{operation} {event_type}".casefold()
        if (
            not operation
            or not event_type
            or any(marker in lowered for marker in _BLOCKED_EVENT_MARKERS)
        ):
            continue
        event_identity = _safe_identifier(record.get("event_id"), limit=300)
        if not event_identity:
            event_identity = f"{seq}:{operation}:{event_type}"
        outcome = _status(record.get("outcome"), default="observed")
        details = _safe_details(_mapping(record.get("details")), _AUDIT_DETAIL_KEYS)
        contract_digest = _safe_digest(record.get("contract_digest"))
        payload_digest = _safe_digest(record.get("payload_digest"))
        if contract_digest is not None:
            details["contract_digest"] = contract_digest
        if payload_digest is not None:
            details["payload_digest"] = payload_digest
        reason_code = _safe_identifier(record.get("reason_code"), limit=100)
        if reason_code:
            details["reason_code"] = reason_code
        assignment_id = _safe_identifier(record.get("assignment_id"))
        session_id = _safe_identifier(record.get("session_id"))
        if assignment_id:
            assignment_ids.add(assignment_id)
        if session_id:
            session_ids.add(session_id)
        summary = f"{operation} {outcome}"
        if isinstance(details.get("status_code"), int):
            summary += f" (HTTP {details['status_code']})"
        events.append(
            BuilderPublicInformationFlowEvent(
                event_id=f"blackbox-audit:{event_identity}",
                source="blackbox_audit",
                kind="authorization",
                event_type=event_type,
                status=outcome,
                title=operation,
                summary=summary,
                application_id=application_id,
                sequence=seq,
                operation=operation,
                created_at=_safe_created_at(record.get("created_at")),
                details=details,
            )
        )
    return (
        events,
        audit_after,
        next(iter(assignment_ids)) if len(assignment_ids) == 1 else None,
        next(iter(session_ids)) if len(session_ids) == 1 else None,
    )


def _application_event(
    application_id: str,
    response: Any,
) -> BuilderPublicInformationFlowEvent | None:
    data, envelope = _response(response)
    if not data or not _application_matches(data, application_id):
        return None
    published_version = data.get("published_version")
    status = _status(data.get("status"), default="observed")
    details: dict[str, Any] = {}
    if isinstance(published_version, int):
        details["published_version"] = published_version
    evidence_state = _safe_identifier(_mapping(data.get("evidence")).get("state"))
    if evidence_state:
        details["evidence_state"] = evidence_state
    request_id = _safe_identifier(envelope.get("request_id"), limit=300)
    updated_at = _safe_created_at(data.get("updated_at"))
    identity = request_id or updated_at or status
    return BuilderPublicInformationFlowEvent(
        event_id=f"application:{application_id}:{identity}",
        source="application",
        kind="application",
        event_type="application.summary",
        status=status,
        title="Application summary",
        summary=f"Application is {status}",
        application_id=application_id,
        operation=_safe_identifier(envelope.get("operation")) or None,
        created_at=updated_at or _safe_created_at(data.get("created_at")),
        details=details,
    )


def _draft_event(
    application_id: str,
    response: Any,
) -> BuilderPublicInformationFlowEvent | None:
    data, envelope = _response(response)
    if not data or not _application_matches(data, application_id):
        return None
    snapshot = _mapping(data.get("snapshot"))
    workflow = _mapping(snapshot.get("workflow"))
    revision = _int(data.get("revision"))
    details: dict[str, Any] = {
        "revision": revision,
        "node_count": _count(workflow.get("nodes")),
        "edge_count": _count(workflow.get("edges")),
        "test_count": _count(snapshot.get("tests")),
    }
    content_hash = _safe_identifier(data.get("content_hash"), limit=200)
    tested_hash = _safe_identifier(data.get("tested_hash"), limit=200)
    if content_hash:
        details["content_hash"] = content_hash
    if tested_hash:
        details["tested_hash"] = tested_hash
    status = "tested" if tested_hash and tested_hash == content_hash else "draft"
    return BuilderPublicInformationFlowEvent(
        event_id=f"application:{application_id}:draft:{revision}:{content_hash or 'missing'}",
        source="draft",
        kind="draft",
        event_type="application.draft.summary",
        status=status,
        title="Draft summary",
        summary=(
            f"Draft revision {revision}: {details['node_count']} nodes, "
            f"{details['edge_count']} edges, {details['test_count']} tests"
        ),
        application_id=application_id,
        operation=_safe_identifier(envelope.get("operation")) or None,
        created_at=_safe_created_at(data.get("updated_at")),
        details=details,
    )


def _test_event(
    application_id: str,
    response: Any,
    index: int,
) -> BuilderPublicInformationFlowEvent | None:
    data, envelope = _response(response)
    if not data or not _application_matches(data, application_id):
        return None
    results = data.get("results")
    summary = _mapping(data.get("summary"))
    total = _count(results) or _int(summary.get("total"))
    passed_count = _int(summary.get("passed"))
    failed_count = _int(summary.get("failed"))
    passed = data.get("passed")
    if isinstance(passed, bool) and total and not passed_count and not failed_count:
        passed_count = total if passed else 0
        failed_count = 0 if passed else total
    status = "passed" if passed is True else "failed" if passed is False else "completed"
    request_id = _safe_identifier(envelope.get("request_id"), limit=300)
    identity = request_id or f"{index}:{total}:{passed_count}:{failed_count}"
    return BuilderPublicInformationFlowEvent(
        event_id=f"application:{application_id}:tests:{identity}",
        source="tests",
        kind="tests",
        event_type="application.tests.summary",
        status=status,
        title="Test summary",
        summary=f"Tests {status}: {passed_count} passed, {failed_count} failed",
        application_id=application_id,
        operation=_safe_identifier(envelope.get("operation")) or None,
        created_at=_safe_created_at(data.get("updated_at") or data.get("created_at")),
        details={
            "total": total,
            "passed": passed_count,
            "failed": failed_count,
        },
    )


def _run_event(
    application_id: str,
    response: Any,
) -> tuple[BuilderPublicInformationFlowEvent | None, str | None]:
    data, envelope = _response(response)
    if not data or not _application_matches(data, application_id):
        return None, None
    run_id = _safe_identifier(data.get("run_id") or data.get("id"))
    if not run_id:
        return None, None
    status = _status(data.get("status"), default="observed")
    details: dict[str, Any] = {
        "artifact_count": _count(data.get("artifacts")),
        "completed_node_count": _count(data.get("completed_node_ids")),
        "skipped_node_count": _count(data.get("skipped_node_ids")),
    }
    for key in ("draft_revision", "version"):
        if isinstance(data.get(key), int):
            details[key] = data[key]
    updated_at = _safe_created_at(data.get("updated_at"))
    return (
        BuilderPublicInformationFlowEvent(
            event_id=f"run:{run_id}:summary:{status}:{updated_at or 'current'}",
            source="run",
            kind="run",
            event_type="application.run.summary",
            status=status,
            title="Run summary",
            summary=f"Run {run_id} is {status}",
            application_id=application_id,
            run_id=run_id,
            operation=_safe_identifier(envelope.get("operation")) or None,
            created_at=updated_at or _safe_created_at(data.get("created_at")),
            details=details,
        ),
        run_id,
    )


def _trace_events(
    application_id: str,
    response: Any,
    known_run_ids: set[str],
) -> tuple[list[BuilderPublicInformationFlowEvent], str | None, int]:
    data, _ = _response(response)
    run_id = _safe_identifier(data.get("run_id"))
    if not run_id or run_id not in known_run_ids:
        return [], None, 0
    events: list[BuilderPublicInformationFlowEvent] = []
    trace_after = _int(data.get("next_after"))
    raw_events = data.get("events")
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, str):
        return [], run_id, trace_after
    for raw in raw_events:
        item = _mapping(raw)
        seq = _int(item.get("seq"))
        event_type = _safe_identifier(item.get("type") or item.get("event_type"))
        lowered = event_type.casefold()
        if (
            seq < 1
            or not event_type
            or not event_type.startswith(_TRACE_PREFIXES)
            or any(marker in lowered for marker in _BLOCKED_EVENT_MARKERS)
        ):
            continue
        details = _safe_details(_mapping(item.get("data")), _TRACE_DETAIL_KEYS)
        status = _status(
            details.get("status") or event_type.rsplit(".", 1)[-1],
            default="observed",
        )
        node_id = _safe_identifier(details.get("node_id"))
        summary = event_type if not node_id else f"{event_type} for node {node_id}"
        events.append(
            BuilderPublicInformationFlowEvent(
                event_id=f"run:{run_id}:trace:{seq}",
                source="trace",
                kind="trace",
                event_type=event_type,
                status=status,
                title="Workflow trace",
                summary=summary,
                application_id=application_id,
                run_id=run_id,
                sequence=seq,
                created_at=_safe_created_at(item.get("created_at")),
                details=details,
            )
        )
        trace_after = max(trace_after, seq)
    return events, run_id, trace_after


def _event_sort_key(event: BuilderPublicInformationFlowEvent) -> tuple[str, str]:
    return event.created_at or "9999-12-31T23:59:59Z", event.event_id


def project_builder_public_api_information_flow(
    *,
    application_id: str,
    audit_records: Sequence[Any] = (),
    application_response: Any = None,
    draft_response: Any = None,
    test_responses: Sequence[Any] = (),
    run_responses: Sequence[Any] = (),
    trace_responses: Sequence[Any] = (),
) -> BuilderPublicInformationFlowProjection:
    """Project public Builder API observations into safe, persistable events.

    This function is deliberately pure and read-only.  It consumes already
    authorized public API responses and blackbox audit records; it does not
    query storage, relay daemon events, or create collaboration messages.
    """

    application_id = _safe_identifier(application_id)
    if not application_id:
        raise ValueError("application_id is required")
    audit_events, audit_after, assignment_id, session_id = _audit_events(
        application_id,
        audit_records,
    )
    events = list(audit_events)
    application_event = _application_event(application_id, application_response)
    if application_event is not None:
        events.append(application_event)
    draft_event = _draft_event(application_id, draft_response)
    if draft_event is not None:
        events.append(draft_event)
    for index, response in enumerate(test_responses):
        event = _test_event(application_id, response, index)
        if event is not None:
            events.append(event)
    known_run_ids: set[str] = set()
    for response in run_responses:
        event, run_id = _run_event(application_id, response)
        if event is not None and run_id is not None:
            events.append(event)
            known_run_ids.add(run_id)
    trace_after_by_run: dict[str, int] = {}
    for response in trace_responses:
        projected, run_id, trace_after = _trace_events(
            application_id,
            response,
            known_run_ids,
        )
        events.extend(projected)
        if run_id is not None:
            trace_after_by_run[run_id] = max(
                trace_after_by_run.get(run_id, 0),
                trace_after,
            )
    unique = {event.event_id: event for event in events}
    return BuilderPublicInformationFlowProjection(
        application_id=application_id,
        assignment_id=assignment_id,
        session_id=session_id,
        events=sorted(unique.values(), key=_event_sort_key),
        cursor=BuilderPublicInformationFlowCursor(
            audit_after=audit_after,
            trace_after_by_run=dict(sorted(trace_after_by_run.items())),
        ),
    )
