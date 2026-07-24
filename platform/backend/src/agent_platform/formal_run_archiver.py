from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .collaboration_models import (
    ChannelStatus,
    CollaborationChannel,
    CollaborationMessageEnvelope,
    CollaborationReport,
    EvidenceKind,
    EvidenceRef,
    ReportCategory,
    ReportStatus,
    VerificationClaimPayload,
    frozen_claim_context_digest,
)
from .collaboration_storage import CollaborationNotFound
from .connector_sdk import (
    ConnectorAssignmentBudgetReceipt,
    ConnectorService,
)
from .formal_verification_contracts import (
    ArchivedEvidenceIndex,
    ArchivedEvidenceIndexEntry,
    OracleEvidenceSelector,
)
from .formal_source_provenance import (
    FormalSourceProvenanceCoordinator,
    approved_developer_response_bindings,
)
from .forbidden_assistance_scanner import (
    derive_source_semantic_input,
    scan_forbidden_assistance,
)
from .lilies_models import AssignmentMode, BuildAssignment, Digest, OpaqueReference
from .platform_blackbox_auth import PlatformBlackboxAuthStore
from .platform_blackbox_artifacts import (
    ArtifactBinding,
    ArtifactReadRequest,
    ArtifactRecord,
    PlatformBlackboxArtifactStore,
)
from .lilies_platform_contract import MAX_ARTIFACT_CHUNK_BYTES
from .task_packages import (
    WORKSPACE_MANIFEST_FILE,
    ArchiveClaimBinding,
    ArchiveStatus,
    ArchivedCollaborationRecord,
    ArchivedFormalReservation,
    ArchivedMessageRecord,
    ArchivedPreassignmentScanRecord,
    ArchivedPlatformEventRecord,
    ArchivedPlatformOutcome,
    ArchivedRunResult,
    EnvironmentReady,
    FileDigestEntry,
    PreflightFailureEvidence,
    RunArchiveManifest,
    TaskPackageConflict,
    TaskPackageManager,
    ValidationMode,
    WorkspaceRole,
)
from .workflow_models import ApplicationSnapshot


class FormalRunArchiveError(RuntimeError):
    """A durable formal run could not be projected into a trusted archive."""


class FormalRunArchiveUnavailable(FormalRunArchiveError):
    """A trusted archive projection may succeed after transient state settles."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class FormalRunArchivePreparationRequest(_FrozenModel):
    """Caller-selected identities whose evidence must already exist in platform stores."""

    schema_version: Literal["1.0"] = "1.0"
    expected_channel_revision: int = Field(ge=1)
    claim_id: UUID
    test_run_ids: list[OpaqueReference] = Field(min_length=1, max_length=500)
    business_run_ids: list[OpaqueReference] = Field(min_length=1, max_length=500)
    artifact_ids: list[UUID] = Field(default_factory=list, max_length=500)
    host_receipt_ids: list[UUID] = Field(default_factory=list, max_length=500)
    remaining_limits: list[str] = Field(default_factory=list, max_length=100)
    summary: str = Field(min_length=1, max_length=20_000)
    idempotency_key: str = Field(min_length=16, max_length=200)

    @field_validator(
        "test_run_ids",
        "business_run_ids",
        "artifact_ids",
        "host_receipt_ids",
        "remaining_limits",
    )
    @classmethod
    def values_are_unique(cls, value: list[Any]) -> list[Any]:
        if len(value) != len(set(value)):
            raise ValueError("formal archive request values must be unique")
        return value


class FormalRunArchivePreparationResult(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    channel_id: UUID
    public_summary_digest: Digest
    environment_ready_digest: Digest
    workspace_mount_digest: Digest
    archive_manifest_digest: Digest
    claim_binding: ArchiveClaimBinding
    artifact_refs: list[EvidenceRef] = Field(default_factory=list, max_length=500)
    host_receipt_refs: list[EvidenceRef] = Field(default_factory=list, max_length=500)
    verification_claim: VerificationClaimPayload


class FormalRunArchiveIntentReceipt(_FrozenModel):
    """Durable receipt returned while the daemon-owned turn is still running.

    This is deliberately not a completion result.  The bridge consumes the
    frozen intent only after it has authenticated the daemon's terminal state
    and drained the complete event tail.
    """

    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    channel_id: UUID
    claim_id: UUID
    intent_digest: Digest
    state: Literal["awaiting_daemon_completion", "verification_pending"]
    accepted_at: datetime
    replayed: bool = False


class FormalTerminalArchiveResult(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    status: ArchiveStatus
    archive_manifest_digest: Digest


class FormalRunArchiveInvalid(FormalRunArchiveError):
    """A success attempt was durably sealed as an invalid archive."""

    def __init__(
        self,
        message: str,
        *,
        result: FormalTerminalArchiveResult,
    ) -> None:
        super().__init__(message)
        self.result = result


class _ReservedFormalRequest(_FrozenModel):
    idempotency_key: str = Field(min_length=16, max_length=128)
    connection_id: UUID
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    environment_instance_id: OpaqueReference
    user_notified: Literal[True]


_RESOLVED_REPORT_STATUSES: dict[ReportCategory, frozenset[ReportStatus]] = {
    ReportCategory.task_spec_gap: frozenset(
        {
            ReportStatus.lilies_rechecks,
            ReportStatus.independently_verified,
        }
    ),
    ReportCategory.environment_gap: frozenset(
        {
            ReportStatus.lilies_health_checks,
            ReportStatus.independently_verified,
        }
    ),
    ReportCategory.platform_capability_gap: frozenset(
        {
            ReportStatus.lilies_verified,
            ReportStatus.independently_verified,
            ReportStatus.rejected,
            ReportStatus.withdrawn,
        }
    ),
    ReportCategory.platform_defect_suspected: frozenset(
        {
            ReportStatus.lilies_verified,
            ReportStatus.independently_verified,
            ReportStatus.rejected,
            ReportStatus.withdrawn,
        }
    ),
}

_COLLABORATION_EXPORT_TABLES = (
    "credentials",
    "messages",
    "reports",
    "report_revisions",
    "report_evidence_budgets",
    "approvals",
    "reader_cursors",
    "reader_ack_receipts",
    "developer_leases",
    "lease_operations",
    "developer_responses",
    "task_amendments",
    "environment_responses",
    "reprobes",
    "claims",
    "verifications",
    "audit",
    "outbox",
    "channel_operations",
    "operation_receipts",
)


def _canonical_json(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def _digest(value: bytes | BaseModel | Mapping[str, Any]) -> str:
    payload = value if isinstance(value, bytes) else _canonical_json(value)
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _jsonl(records: Sequence[BaseModel]) -> bytes:
    return b"".join(_canonical_json(record) + b"\n" for record in records)


def _record_id(prefix: str, *parts: Any) -> str:
    source = ":".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(source).hexdigest()[:40]}"


def _content_hash(value: Any) -> str:
    normalized = str(value)
    return normalized if normalized.startswith("sha256:") else f"sha256:{normalized}"


class FormalRunArchiveCoordinator:
    """Platform-owned exporter from durable run stores into append-only task archives."""

    def __init__(
        self,
        *,
        task_state_root: Path,
        public_workspace_root: Path,
        bridge_store: Any,
        collaboration_store: Any,
        workflow_storage: Any,
        artifact_store: PlatformBlackboxArtifactStore,
        auth_store: PlatformBlackboxAuthStore | None = None,
        connector_service: ConnectorService | None = None,
        source_provenance: FormalSourceProvenanceCoordinator | None = None,
    ) -> None:
        self._task_state_root = Path(task_state_root).resolve()
        self._public_workspace_root = Path(public_workspace_root).resolve()
        self._manager = TaskPackageManager(self._task_state_root)
        self._bridge_store = bridge_store
        self._collaboration_store = collaboration_store
        self._workflow = workflow_storage
        self._artifact_store = artifact_store
        self._auth_store = auth_store
        self._connector_service = connector_service
        self._source_provenance = source_provenance
        self._locks: dict[UUID, asyncio.Lock] = {}

    def _lock(self, assignment_id: UUID) -> asyncio.Lock:
        lock = self._locks.get(assignment_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[assignment_id] = lock
        return lock

    @staticmethod
    def _assignment_from_row(row: Mapping[str, Any]) -> BuildAssignment:
        if str(row.get("assignment_mode")) != AssignmentMode.formal_experiment.value:
            raise FormalRunArchiveError("run archive requires a formal assignment")
        encoded = row.get("submission_json")
        if not isinstance(encoded, str) or not encoded:
            raise FormalRunArchiveError(
                "formal assignment has no durable manager-prepared projection"
            )
        try:
            assignment = BuildAssignment.model_validate_json(encoded)
        except ValueError as error:
            raise FormalRunArchiveError("durable formal assignment is invalid") from error
        task = assignment.task_package
        if (
            assignment.mode is not AssignmentMode.formal_experiment
            or task is None
            or assignment.collaboration is None
            or str(assignment.assignment_id) != str(row.get("assignment_id"))
            or str(assignment.target.application_id) != str(row.get("application_id"))
            or task.run_id != f"formal-run:{row.get('build_id')}"
        ):
            raise FormalRunArchiveError("durable formal assignment identity changed")
        return assignment

    @staticmethod
    def _missing_collaboration_export(
        assignment: BuildAssignment,
        session_id: UUID,
    ) -> dict[str, Any]:
        """Freeze an absent channel explicitly while retaining every other store."""

        access = assignment.collaboration
        if access is None:  # pragma: no cover - guarded by formal assignment validation
            raise FormalRunArchiveError("formal assignment has no collaboration binding")
        return {
            "schema_version": "1.0",
            "complete": False,
            "missing_reason": "collaboration_channel_not_created",
            "counts": {name: 0 for name in _COLLABORATION_EXPORT_TABLES},
            "watermark": {
                "min_message_seq": None,
                "max_message_seq": None,
                "next_seq": 1,
                "max_report_evidence_rounds": None,
                "report_evidence_rounds_used_total": 0,
                "max_report_evidence_rounds_used": 0,
                "budget_exhausted_reports": 0,
            },
            "channel": {
                "channel_id": str(access.channel_id),
                "assignment_id": str(assignment.assignment_id),
                "lilies_session_id": str(session_id),
                "next_seq": 1,
                "missing": True,
            },
            **{name: [] for name in _COLLABORATION_EXPORT_TABLES},
        }

    @staticmethod
    def _reserved_channel_id(
        *,
        task_id: str,
        revision: int,
        assignment_id: UUID,
    ) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"lilies:collaboration:{task_id}:{revision}:{assignment_id}",
        )

    @staticmethod
    def _missing_reserved_collaboration_export(
        *,
        channel_id: UUID,
        assignment_id: UUID,
        session_id: UUID,
        task_id: str,
        revision: int,
        application_id: UUID,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "complete": False,
            "missing_reason": "collaboration_channel_not_created",
            "counts": {name: 0 for name in _COLLABORATION_EXPORT_TABLES},
            "watermark": {
                "min_message_seq": None,
                "max_message_seq": None,
                "next_seq": 1,
                "max_report_evidence_rounds": None,
                "report_evidence_rounds_used_total": 0,
                "max_report_evidence_rounds_used": 0,
                "budget_exhausted_reports": 0,
            },
            "channel": {
                "channel_id": str(channel_id),
                "task_id": task_id,
                "task_revision": revision,
                "assignment_id": str(assignment_id),
                "lilies_session_id": str(session_id),
                "application_ids": [str(application_id)],
                "next_seq": 1,
                "status": "closed",
            },
            **{name: [] for name in _COLLABORATION_EXPORT_TABLES},
        }

    def _reserved_preflight_evidence(
        self,
        *,
        task_id: str,
        revision: int,
        run_id: str,
        assignment_id: UUID,
        environment_instance_id: str,
    ) -> tuple[dict[str, bytes], list[FileDigestEntry]]:
        preflight_root = (
            self._task_state_root
            / "preflight"
            / task_id
            / str(revision)
            / run_id
        )
        if not preflight_root.exists():
            return {}, []
        if preflight_root.is_symlink() or not preflight_root.is_dir():
            raise FormalRunArchiveError(
                "formal preflight evidence root is unsafe"
            )
        paths = list(preflight_root.glob("environment-preflight*.json"))
        validated: list[
            tuple[
                PreflightFailureEvidence,
                str,
                bytes,
                FileDigestEntry,
            ]
        ] = []
        seen_paths: set[str] = set()
        seen_attempts: set[int] = set()
        seen_digests: set[str] = set()
        for path in paths:
            if (
                path.is_symlink()
                or not path.is_file()
                or (
                    path.name != "environment-preflight.json"
                    and not (
                        path.name.startswith("environment-preflight-attempt-")
                        and path.name.endswith(".json")
                        and path.name[
                            len("environment-preflight-attempt-") : -len(".json")
                        ].isdigit()
                    )
                )
            ):
                raise FormalRunArchiveError(
                    "formal preflight evidence contains an unsafe entry"
                )
            payload = path.read_bytes()
            try:
                failure = PreflightFailureEvidence.model_validate_json(payload)
            except ValueError as error:
                raise FormalRunArchiveError(
                    "formal preflight failure evidence is invalid"
                ) from error
            if (
                failure.task_id != task_id
                or failure.revision != revision
                or failure.run_id != run_id
                or failure.assignment_id != assignment_id
                or failure.environment_instance_id != environment_instance_id
            ):
                raise FormalRunArchiveError(
                    "formal preflight failure evidence changed its binding"
                )
            archive_path = f"environment-preflight/{path.name}"
            digest = _digest(payload)
            if (
                archive_path in seen_paths
                or failure.attempt in seen_attempts
                or digest in seen_digests
            ):
                raise FormalRunArchiveError(
                    "formal preflight failure evidence is not uniquely bound"
                )
            entry = FileDigestEntry(
                path=archive_path,
                digest=digest,
                size_bytes=len(payload),
            )
            seen_paths.add(archive_path)
            seen_attempts.add(failure.attempt)
            seen_digests.add(digest)
            validated.append((failure, archive_path, payload, entry))
        validated.sort(key=lambda item: item[0].attempt)
        attempts = [item[0].attempt for item in validated]
        if attempts != list(range(1, len(attempts) + 1)):
            raise FormalRunArchiveError(
                "formal preflight failure evidence has an incomplete attempt sequence"
            )
        files = {
            archive_path: payload
            for _, archive_path, payload, _ in validated
        }
        entries = [entry for _, _, _, entry in validated]
        return files, entries

    @staticmethod
    def _assert_channel(
        assignment: BuildAssignment,
        session_id: UUID,
        export: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> tuple[CollaborationChannel, list[CollaborationMessageEnvelope], list[CollaborationReport]]:
        channel = CollaborationChannel.model_validate(export.get("channel"))
        access = assignment.collaboration
        task = assignment.task_package
        if access is None or task is None:
            raise FormalRunArchiveError("formal assignment has no collaboration binding")
        if (
            channel.channel_id != access.channel_id
            or channel.assignment_id != assignment.assignment_id
            or channel.lilies_session_id != session_id
            or channel.task_id != task.task_id
            or channel.task_revision != task.revision
            or channel.application_ids != assignment.platform.application_ids
            or (expected_revision is not None and channel.revision != expected_revision)
        ):
            raise FormalRunArchiveError("collaboration export changed its formal binding")
        messages = [
            CollaborationMessageEnvelope.model_validate(item) for item in export.get("messages", [])
        ]
        reports = [CollaborationReport.model_validate(item) for item in export.get("reports", [])]
        if any(message.channel_id != channel.channel_id for message in messages) or any(
            report.channel_id != channel.channel_id for report in reports
        ):
            raise FormalRunArchiveError("collaboration export crossed channel boundaries")
        return channel, messages, reports

    @staticmethod
    def _bridge_messages(
        *,
        assignment: BuildAssignment,
        session_id: UUID,
        events: Sequence[Mapping[str, Any]],
    ) -> list[ArchivedMessageRecord]:
        task = assignment.task_package
        if task is None:
            raise FormalRunArchiveError("formal assignment has no task package")
        records: list[ArchivedMessageRecord] = []
        for seq, event in enumerate(events, start=1):
            event_type = str(event.get("event_type") or "")
            try:
                data = json.loads(str(event.get("data_json") or "{}"))
            except json.JSONDecodeError as error:
                raise FormalRunArchiveError("bridge event payload is not valid JSON") from error
            if not isinstance(data, dict):
                raise FormalRunArchiveError("bridge event payload is not an object")
            if event_type == "assignment.accepted":
                kind = "assignment.accepted"
            elif event_type == "message.created":
                role = str(data.get("role") or "")
                kind = {
                    "user": "user.message",
                    "assistant": "lilies.message",
                    "tool": "tool.result",
                }.get(role, "daemon.event")
            elif event_type in {"tool.requested", "tool.started"}:
                kind = "tool.call"
            elif event_type in {"tool.completed", "tool.failed"}:
                kind = "tool.result"
            elif event_type == "context.summary":
                kind = "context.summary"
            else:
                kind = "daemon.event"
            payload = {
                "event_type": event_type,
                "data": data,
                "received_at": str(event.get("received_at") or ""),
            }
            records.append(
                ArchivedMessageRecord(
                    schema_version="1.0",
                    seq=seq,
                    message_id=_record_id(
                        "bridge-message",
                        assignment.assignment_id,
                        event.get("daemon_seq"),
                    ),
                    task_id=task.task_id,
                    revision=task.revision,
                    run_id=task.run_id,
                    assignment_id=assignment.assignment_id,
                    session_id=session_id,
                    kind=kind,
                    payload=payload,
                    payload_digest=_digest(payload),
                )
            )
        return records

    @staticmethod
    def _collaboration_records(
        *,
        assignment: BuildAssignment,
        channel: CollaborationChannel,
        messages: Sequence[CollaborationMessageEnvelope],
        reports: Sequence[CollaborationReport],
        binding: ArchiveClaimBinding | None,
    ) -> list[ArchivedCollaborationRecord]:
        task = assignment.task_package
        if task is None:
            raise FormalRunArchiveError("formal assignment has no task package")
        records: list[ArchivedCollaborationRecord] = []
        for message in messages:
            payload = message.model_dump(mode="json", exclude_none=True)
            records.append(
                ArchivedCollaborationRecord(
                    schema_version="1.0",
                    seq=len(records) + 1,
                    event_id=_record_id("collaboration-message", message.message_id),
                    task_id=task.task_id,
                    revision=task.revision,
                    run_id=task.run_id,
                    assignment_id=assignment.assignment_id,
                    channel_id=channel.channel_id,
                    kind="message",
                    message_id=message.message_id,
                    payload=payload,
                    payload_digest=_digest(payload),
                )
            )
        for report in reports:
            payload = {
                "category": report.category.value,
                "route": report.route.value,
                "status": report.status.value,
                "revision": report.revision,
                "updated_at": report.updated_at.isoformat(),
            }
            records.append(
                ArchivedCollaborationRecord(
                    schema_version="1.0",
                    seq=len(records) + 1,
                    event_id=_record_id("collaboration-report", report.report_id),
                    task_id=task.task_id,
                    revision=task.revision,
                    run_id=task.run_id,
                    assignment_id=assignment.assignment_id,
                    channel_id=channel.channel_id,
                    kind="report.resolved",
                    report_id=report.report_id,
                    payload=payload,
                    payload_digest=_digest(payload),
                )
            )
        if binding is not None:
            payload = {
                "source": "platform_owned_formal_run_archiver",
                "channel_revision": channel.revision,
                "message_count": len(messages),
                "report_count": len(reports),
            }
            records.append(
                ArchivedCollaborationRecord(
                    schema_version="1.0",
                    seq=len(records) + 1,
                    event_id=_record_id("collaboration-claim", binding.claim_id),
                    task_id=task.task_id,
                    revision=task.revision,
                    run_id=task.run_id,
                    assignment_id=assignment.assignment_id,
                    channel_id=channel.channel_id,
                    kind="claim.prepared",
                    claim_binding=binding,
                    payload=payload,
                    payload_digest=_digest(payload),
                )
            )
        return records

    @staticmethod
    def _reported_test_run_ids(
        *,
        draft: Mapping[str, Any],
        content_hash: str,
    ) -> set[str]:
        """Read the current acceptance-run identities from the durable test report."""

        tested_hash = draft.get("tested_hash")
        report = draft.get("validation_report")
        if (
            not isinstance(tested_hash, str)
            or not hmac.compare_digest(_content_hash(tested_hash), content_hash)
            or not isinstance(report, Mapping)
            or report.get("passed") is not True
        ):
            raise FormalRunArchiveError(
                "formal archive requires a successful current platform test report"
            )
        validation = report.get("validation")
        if (
            not isinstance(validation, Mapping)
            or validation.get("valid") is not True
            or not isinstance(validation.get("content_hash"), str)
            or not hmac.compare_digest(
                _content_hash(validation["content_hash"]),
                content_hash,
            )
        ):
            raise FormalRunArchiveError(
                "formal test report does not bind the current frozen content"
            )
        tests = report.get("tests")
        if not isinstance(tests, list) or not tests:
            raise FormalRunArchiveError("formal test report has no durable acceptance runs")
        run_ids: set[str] = set()
        for item in tests:
            if (
                not isinstance(item, Mapping)
                or item.get("passed") is not True
                or str(item.get("run_status") or "") != "succeeded"
                or not str(item.get("run_id") or "")
            ):
                raise FormalRunArchiveError(
                    "formal test report contains an unsuccessful acceptance run"
                )
            run_id = str(item["run_id"])
            if run_id in run_ids:
                raise FormalRunArchiveError("formal test report contains duplicate run identities")
            run_ids.add(run_id)
        return run_ids

    @staticmethod
    def _blackbox_run_operation_ids(
        *,
        blackbox_auth_export: Mapping[str, Any],
        assignment: BuildAssignment,
        session_id: UUID,
    ) -> tuple[set[str], set[str]]:
        """Bind every workflow run to the public operation that created it."""

        requests = blackbox_auth_export.get("requests")
        if (
            blackbox_auth_export.get("complete") is not True
            or not isinstance(requests, list)
        ):
            raise FormalRunArchiveError(
                "formal blackbox request denominator is incomplete"
            )
        application_id = str(assignment.target.application_id)
        test_run_ids: set[str] = set()
        business_run_ids: set[str] = set()
        for request in requests:
            if not isinstance(request, Mapping):
                raise FormalRunArchiveError(
                    "formal blackbox request denominator is invalid"
                )
            operation = str(request.get("operation") or "")
            if operation not in {
                "platform_tests_run",
                "platform_run_start",
            }:
                continue
            if (
                str(request.get("assignment_id"))
                != str(assignment.assignment_id)
                or str(request.get("session_id")) != str(session_id)
                or str(request.get("application_id")) != application_id
            ):
                raise FormalRunArchiveError(
                    "formal run operation escaped its assignment binding"
                )
            status_code = request.get("status_code")
            if (
                request.get("state") != "completed"
                or not isinstance(status_code, int)
            ):
                raise FormalRunArchiveError(
                    "formal run operation has no terminal response"
                )
            if status_code >= 400:
                continue
            response = request.get("response")
            if not isinstance(response, Mapping):
                raise FormalRunArchiveError(
                    "formal run operation response is unavailable"
                )
            response_operation = response.get("operation")
            data = response.get("data")
            if (
                response.get("ok") is not True
                or response_operation != operation
                or not isinstance(data, Mapping)
            ):
                raise FormalRunArchiveError(
                    "formal run operation response changed its public envelope"
                )
            if operation == "platform_tests_run":
                tests = data.get("tests")
                if not isinstance(tests, list):
                    raise FormalRunArchiveError(
                        "formal test operation response has no test denominator"
                    )
                for item in tests:
                    if not isinstance(item, Mapping):
                        raise FormalRunArchiveError(
                            "formal test operation response is invalid"
                        )
                    run_id = str(item.get("run_id") or "")
                    if not run_id:
                        # Draft-validation failures legitimately have no run.
                        continue
                    if run_id in test_run_ids:
                        raise FormalRunArchiveError(
                            "formal test operation reused a workflow run identity"
                        )
                    test_run_ids.add(run_id)
            else:
                run_id = str(data.get("run_id") or "")
                if not run_id or run_id in business_run_ids:
                    raise FormalRunArchiveError(
                        "formal business operation has an invalid run identity"
                    )
                business_run_ids.add(run_id)
        overlap = test_run_ids & business_run_ids
        if overlap:
            raise FormalRunArchiveError(
                "formal workflow run was attributed to both test and business operations"
            )
        return test_run_ids, business_run_ids

    @staticmethod
    def _assert_blackbox_credential_policy(
        *,
        blackbox_auth_export: Mapping[str, Any],
        assignment: BuildAssignment,
        session_id: UUID,
    ) -> None:
        """Require the durable server credential to match the frozen task policy."""

        task_ref = assignment.task_package
        credentials = blackbox_auth_export.get("credentials")
        applications = blackbox_auth_export.get("credential_applications")
        if (
            task_ref is None
            or not isinstance(credentials, list)
            or len(credentials) != 1
            or not isinstance(credentials[0], Mapping)
            or not isinstance(applications, list)
        ):
            raise FormalRunArchiveError(
                "formal blackbox credential policy is incomplete"
            )
        credential = credentials[0]
        constraints = assignment.constraints
        expected = {
            "credential_ref": assignment.platform.credential_ref,
            "assignment_id": str(assignment.assignment_id),
            "session_id": str(session_id),
            "scopes": sorted(scope.value for scope in assignment.platform.scopes),
            "allowed_operations": sorted(
                action.value for action in constraints.allowed_actions
            ),
            "allowed_actions_digest": task_ref.allowed_actions_digest,
            "budget_digest": task_ref.budget_digest,
            "allowed_network_hosts": sorted(
                host.casefold() for host in constraints.allowed_hosts
            ),
            "model_access": constraints.model_access,
            "file_access": constraints.file_access,
            "connector_access": constraints.connector_access,
            "readable_host_objects": sorted(constraints.readable_host_objects),
            "writable_host_operations": sorted(
                constraints.writable_host_operations
            ),
            "permission_required_actions": sorted(
                constraints.permission_required_actions
            ),
            "max_write_count": constraints.max_write_count,
            "max_payload_bytes": constraints.max_payload_bytes,
            "compensation_actions": sorted(constraints.compensation_actions),
            "max_report_evidence_rounds": (
                constraints.max_report_evidence_rounds
            ),
            "stable_hidden_runs": constraints.stable_hidden_runs,
        }
        if any(credential.get(key) != value for key, value in expected.items()):
            raise FormalRunArchiveError(
                "formal blackbox credential differs from the frozen task policy"
            )
        credential_id = str(credential.get("id") or "")
        if not credential_id or not any(
            isinstance(item, Mapping)
            and str(item.get("credential_id")) == credential_id
            and str(item.get("application_id"))
            == str(assignment.target.application_id)
            for item in applications
        ):
            raise FormalRunArchiveError(
                "formal blackbox credential application binding is incomplete"
            )
        requests = blackbox_auth_export.get("requests")
        if not isinstance(requests, list) or any(
            not isinstance(request, Mapping) for request in requests
        ):
            raise FormalRunArchiveError(
                "formal blackbox request denominator is incomplete"
            )

    @staticmethod
    def _assert_connector_budget_policy(
        *,
        receipt: ConnectorAssignmentBudgetReceipt,
        assignment: BuildAssignment,
    ) -> None:
        constraints = assignment.constraints
        expected_hosts = sorted(
            {
                host.casefold().rstrip(".")
                for host in constraints.allowed_hosts
            }
        )
        if (
            str(receipt.assignment_id) != str(assignment.assignment_id)
            or receipt.allowed_network_hosts != expected_hosts
            or receipt.allowed_compensation_operations
            != sorted(set(constraints.compensation_actions))
            or receipt.max_write_count != constraints.max_write_count
            or receipt.max_payload_bytes != constraints.max_payload_bytes
            or receipt.write_count > receipt.max_write_count
            or receipt.write_count != len(receipt.writes)
        ):
            raise FormalRunArchiveError(
                "connector side-effect receipt differs from the frozen task policy"
            )

    async def _export_connector_budget(
        self,
        assignment: BuildAssignment,
    ) -> dict[str, Any]:
        constraints = assignment.constraints
        if (
            self._connector_service is None
            or constraints.max_write_count is None
            or constraints.max_payload_bytes is None
        ):
            raise FormalRunArchiveError(
                "formal archive has no connector side-effect budget source"
            )
        try:
            await self._connector_service.freeze_assignment_budget(
                assignment_id=str(assignment.assignment_id),
                allowed_network_hosts=list(constraints.allowed_hosts),
                allowed_compensation_operations=list(
                    constraints.compensation_actions
                ),
                max_write_count=constraints.max_write_count,
                max_payload_bytes=constraints.max_payload_bytes,
            )
            receipt = await self._connector_service.export_assignment_budget(
                str(assignment.assignment_id)
            )
            receipt = ConnectorAssignmentBudgetReceipt.model_validate(receipt)
        except Exception as error:
            raise FormalRunArchiveError(
                "formal connector side-effect receipt is unavailable or invalid"
            ) from error
        self._assert_connector_budget_policy(
            receipt=receipt,
            assignment=assignment,
        )
        return receipt.model_dump(mode="json")

    @staticmethod
    def _missing_connector_budget(
        *,
        assignment_id: UUID,
        allowed_network_hosts: Sequence[str],
        allowed_compensation_operations: Sequence[str],
        max_write_count: int,
        max_payload_bytes: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "complete": False,
            "missing_reason": "no_workflow_runs_before_assignment_execution",
            "assignment_id": str(assignment_id),
            "allowed_network_hosts": sorted(
                {
                    str(host).casefold().rstrip(".")
                    for host in allowed_network_hosts
                }
            ),
            "allowed_compensation_operations": sorted(
                set(allowed_compensation_operations)
            ),
            "max_write_count": max_write_count,
            "max_payload_bytes": max_payload_bytes,
            "write_count": 0,
            "writes": [],
        }

    @staticmethod
    def _classified_runs(
        *,
        run_rows: Sequence[Mapping[str, Any]],
        reported_test_run_ids: set[str],
        blackbox_test_run_ids: set[str],
        blackbox_business_run_ids: set[str],
        assignment: BuildAssignment,
        session_id: UUID,
        draft_revision: int,
        content_hash: str,
        published_version: int | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Derive complete test/business sets from platform-owned run metadata."""

        test_rows: list[dict[str, Any]] = []
        business_rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        application_id = str(assignment.target.application_id)
        for source in run_rows:
            row = dict(source)
            run_id = str(row.get("id") or "")
            if not run_id or run_id in seen_ids:
                raise FormalRunArchiveError(
                    "formal platform run export has duplicate or missing identities"
                )
            seen_ids.add(run_id)
            state = row.get("state")
            if not isinstance(state, Mapping):
                raise FormalRunArchiveError("formal platform run state is unavailable")
            if (
                str(row.get("application_id")) != application_id
                or str(state.get("assignment_id")) != str(assignment.assignment_id)
                or str(state.get("session_id")) != str(session_id)
            ):
                raise FormalRunArchiveError(
                    "formal platform run escaped its durable assignment binding"
                )
            snapshot = ApplicationSnapshot.model_validate(state.get("snapshot"))
            snapshot_hash = f"sha256:{snapshot.content_hash()}"
            persisted_draft_revision = row.get("draft_revision")
            persisted_version = row.get("version")
            current_draft_run = (
                persisted_draft_revision is not None
                and int(persisted_draft_revision) == draft_revision
                and persisted_version is None
            )
            published_run = (
                published_version is not None
                and persisted_draft_revision is None
                and persisted_version is not None
                and int(persisted_version) == published_version
            )
            if not (current_draft_run or published_run):
                # Historical runs for another frozen revision/version are not
                # evidence for the current claim.
                continue
            if not hmac.compare_digest(snapshot_hash, content_hash):
                raise FormalRunArchiveError(
                    "formal platform run metadata does not match its frozen content"
                )
            if run_id in blackbox_test_run_ids:
                test_rows.append(row)
            elif run_id in blackbox_business_run_ids:
                business_rows.append(row)
            else:
                raise FormalRunArchiveError(
                    "formal platform run has no public operation provenance"
                )
        classified_test_ids = {str(item["id"]) for item in test_rows}
        if not reported_test_run_ids <= classified_test_ids:
            raise FormalRunArchiveError(
                "formal test report references a missing or ineligible platform run"
            )
        if any(
            str(item.get("status")) != "succeeded"
            for item in business_rows
        ):
            raise FormalRunArchiveError(
                "successful formal archive requires every current business run "
                "to have succeeded"
            )
        return test_rows, business_rows

    async def _artifact_bytes(
        self,
        record: ArtifactRecord,
        *,
        assignment: BuildAssignment,
        session_id: UUID,
    ) -> bytes:
        binding = ArtifactBinding(
            assignment_id=assignment.assignment_id,
            session_id=session_id,
            application_id=UUID(str(assignment.target.application_id)),
            run_id=record.run_id,
        )
        return await self._artifact_bytes_for_binding(
            record,
            binding=binding,
        )

    async def _artifact_bytes_for_binding(
        self,
        record: ArtifactRecord,
        *,
        binding: ArtifactBinding,
    ) -> bytes:
        offset = 0
        chunks: list[bytes] = []
        while True:
            result = await self._artifact_store.read_artifact(
                ArtifactReadRequest(
                    artifact_id=record.artifact_id,
                    binding=binding,
                    offset_bytes=offset,
                    max_bytes=MAX_ARTIFACT_CHUNK_BYTES,
                ),
                artifact_root=record.root_path,
            )
            if result.encoding == "utf8":
                chunk = result.content.encode("utf-8")
            else:
                chunk = base64.b64decode(result.content, validate=True)
            chunks.append(chunk)
            if result.complete:
                break
            if result.next_offset_bytes is None or result.next_offset_bytes <= offset:
                raise FormalRunArchiveError("artifact reader did not advance")
            offset = result.next_offset_bytes
        payload = b"".join(chunks)
        if len(payload) != record.size_bytes or not hmac.compare_digest(
            _digest(payload),
            record.sha256,
        ):
            raise FormalRunArchiveError("artifact export changed registered bytes")
        return payload

    async def _reserved_evidence_files(
        self,
        *,
        inventory_records: Sequence[Mapping[str, Any]],
        assignment_id: UUID,
        session_id: UUID,
        application_id: UUID,
        allowed_run_ids: set[str],
    ) -> tuple[
        dict[str, bytes],
        list[EvidenceRef],
        list[EvidenceRef],
        list[ArchivedEvidenceIndexEntry],
    ]:
        files: dict[str, bytes] = {}
        artifact_refs: list[EvidenceRef] = []
        receipt_refs: list[EvidenceRef] = []
        index_entries: list[ArchivedEvidenceIndexEntry] = []
        seen: set[UUID] = set()
        for item in inventory_records:
            try:
                artifact_id = UUID(str(item["artifact_id"]))
                kind = EvidenceKind(str(item["evidence_kind"]))
            except (KeyError, TypeError, ValueError) as error:
                raise FormalRunArchiveError(
                    "reserved formal artifact inventory is invalid"
                ) from error
            if artifact_id in seen:
                raise FormalRunArchiveError(
                    "reserved formal artifact inventory contains a duplicate"
                )
            seen.add(artifact_id)
            try:
                record = await self._artifact_store.get_artifact(artifact_id)
            except Exception as error:
                raise FormalRunArchiveError(
                    "reserved formal evidence bytes are unavailable"
                ) from error
            provenance = record.provenance
            if (
                record.assignment_id != assignment_id
                or record.session_id != session_id
                or record.application_id != application_id
                or record.run_id not in allowed_run_ids
                or record.evidence_kind != kind.value
                or provenance.evidence_kind != kind.value
                or provenance.assignment_id != assignment_id
                or provenance.session_id != session_id
                or provenance.application_id != application_id
                or provenance.run_id != record.run_id
            ):
                raise FormalRunArchiveError(
                    "reserved formal evidence has no matching provenance"
                )
            if kind is EvidenceKind.host_receipt and (
                provenance.source != "platform_host_write"
                or provenance.receipt_id is None
                or provenance.operation is None
            ):
                raise FormalRunArchiveError(
                    "reserved host receipt has no durable host-write provenance"
                )
            payload = await self._artifact_bytes_for_binding(
                record,
                binding=ArtifactBinding(
                    assignment_id=assignment_id,
                    session_id=session_id,
                    application_id=application_id,
                    run_id=record.run_id,
                ),
            )
            prefix = (
                "artifacts"
                if kind is EvidenceKind.artifact
                else "host-receipts"
            )
            archive_path = f"{prefix}/{record.artifact_id}.bin"
            files[archive_path] = payload
            ref = EvidenceRef(
                evidence_id=f"artifact:{record.artifact_id}",
                kind=kind,
                digest=record.sha256,
                media_type=record.media_type,
                label=record.relative_path,
                captured_at=record.created_at,
            )
            if kind is EvidenceKind.artifact:
                artifact_refs.append(ref)
            else:
                receipt_refs.append(ref)
            selector = OracleEvidenceSelector(
                kind=kind.value,
                label=record.relative_path,
                operation=(
                    provenance.operation
                    if kind is EvidenceKind.host_receipt
                    else None
                ),
            )
            index_entries.append(
                ArchivedEvidenceIndexEntry(
                    schema_version="1.0",
                    evidence_key=selector.evidence_key,
                    kind=kind.value,
                    label=record.relative_path,
                    operation=selector.operation,
                    provenance_source=provenance.source,
                    run_id=record.run_id,
                    archive_path=archive_path,
                    digest=record.sha256,
                    size_bytes=record.size_bytes,
                    media_type=record.media_type,
                )
            )
        return files, artifact_refs, receipt_refs, index_entries

    async def _evidence_files(
        self,
        *,
        artifact_ids: Sequence[UUID],
        kind: EvidenceKind,
        assignment: BuildAssignment,
        session_id: UUID,
        allowed_run_ids: set[str],
    ) -> tuple[
        dict[str, bytes],
        list[EvidenceRef],
        list[ArchivedEvidenceIndexEntry],
    ]:
        prefix = "artifacts" if kind is EvidenceKind.artifact else "host-receipts"
        files: dict[str, bytes] = {}
        refs: list[EvidenceRef] = []
        index_entries: list[ArchivedEvidenceIndexEntry] = []
        for artifact_id in artifact_ids:
            try:
                record = await self._artifact_store.get_artifact(artifact_id)
            except Exception as error:
                raise FormalRunArchiveError("registered formal evidence was not found") from error
            provenance = record.provenance
            if (
                record.assignment_id != assignment.assignment_id
                or record.session_id != session_id
                or record.application_id != assignment.target.application_id
                or record.run_id not in allowed_run_ids
                or record.evidence_kind != kind.value
                or provenance.evidence_kind != kind.value
                or provenance.assignment_id != assignment.assignment_id
                or provenance.session_id != session_id
                or provenance.application_id != assignment.target.application_id
                or provenance.run_id != record.run_id
            ):
                raise FormalRunArchiveError(
                    "registered formal evidence has no matching platform provenance"
                )
            if kind is EvidenceKind.host_receipt and (
                provenance.source != "platform_host_write"
                or provenance.receipt_id is None
                or provenance.operation is None
            ):
                raise FormalRunArchiveError(
                    "host receipt was not emitted by a durable platform host write"
                )
            payload = await self._artifact_bytes(
                record,
                assignment=assignment,
                session_id=session_id,
            )
            archive_path = f"{prefix}/{record.artifact_id}.bin"
            files[archive_path] = payload
            refs.append(
                EvidenceRef(
                    evidence_id=f"artifact:{record.artifact_id}",
                    kind=kind,
                    digest=record.sha256,
                    media_type=record.media_type,
                    label=record.relative_path,
                    captured_at=record.created_at,
                )
            )
            selector = OracleEvidenceSelector(
                kind=kind.value,
                label=record.relative_path,
                operation=(provenance.operation if kind is EvidenceKind.host_receipt else None),
            )
            index_entries.append(
                ArchivedEvidenceIndexEntry(
                    schema_version="1.0",
                    evidence_key=selector.evidence_key,
                    kind=kind.value,
                    label=record.relative_path,
                    operation=selector.operation,
                    provenance_source=provenance.source,
                    run_id=record.run_id,
                    archive_path=archive_path,
                    digest=record.sha256,
                    size_bytes=record.size_bytes,
                    media_type=record.media_type,
                )
            )
        return files, refs, index_entries

    def _platform_records(
        self,
        *,
        assignment: BuildAssignment,
        run_rows: Sequence[Mapping[str, Any]],
        outcome: ArchivedPlatformOutcome | None,
    ) -> list[ArchivedPlatformEventRecord]:
        task = assignment.task_package
        if task is None:
            raise FormalRunArchiveError("formal assignment has no task package")
        records: list[ArchivedPlatformEventRecord] = []
        summaries: list[dict[str, Any]] = []
        for row in run_rows:
            durable_events = row.get("events")
            if not isinstance(durable_events, list):
                raise FormalRunArchiveError("formal platform run events are unavailable")
            payload = {
                "platform_run_id": str(row["id"]),
                "status": str(row["status"]),
                "version": row.get("version"),
                "draft_revision": row.get("draft_revision"),
                "created_at": str(row.get("created_at") or ""),
                "updated_at": str(row.get("updated_at") or ""),
                "outputs": row.get("outputs"),
                "error": row.get("error"),
                "durable_events": [
                    {
                        "seq": int(event["seq"]),
                        "type": str(event["type"]),
                        "created_at": str(event["created_at"]),
                        "data": event["data"],
                        "data_digest": _digest(event["data"]),
                    }
                    for event in durable_events
                ],
            }
            summaries.append(payload)
            records.append(
                ArchivedPlatformEventRecord(
                    schema_version="1.0",
                    seq=len(records) + 1,
                    event_id=_record_id("platform-run", row["id"]),
                    task_id=task.task_id,
                    revision=task.revision,
                    run_id=task.run_id,
                    assignment_id=assignment.assignment_id,
                    application_id=UUID(str(assignment.target.application_id)),
                    kind="run.started",
                    payload=payload,
                    payload_digest=_digest(payload),
                )
            )
        if outcome is not None:
            snapshot_payload = {
                "source": "workflow_storage",
                "platform_runs": summaries,
            }
            records.append(
                ArchivedPlatformEventRecord(
                    schema_version="1.0",
                    seq=len(records) + 1,
                    event_id=_record_id("platform-snapshot", task.run_id),
                    task_id=task.task_id,
                    revision=task.revision,
                    run_id=task.run_id,
                    assignment_id=assignment.assignment_id,
                    application_id=outcome.application_id,
                    kind="formal_run.snapshot",
                    payload=snapshot_payload,
                    payload_digest=_digest(snapshot_payload),
                    outcome=outcome,
                )
            )
        return records

    async def prepare_success_archive(
        self,
        *,
        channel_id: UUID,
        request: FormalRunArchivePreparationRequest,
    ) -> FormalRunArchivePreparationResult:
        export = await self._collaboration_store.export_channel(channel_id)
        exported_channel = CollaborationChannel.model_validate(export.get("channel"))
        bridge_export = await self._bridge_store.export_assignment(exported_channel.assignment_id)
        row = bridge_export["assignment"]
        assignment = self._assignment_from_row(row)
        async with self._lock(assignment.assignment_id):
            if (
                str(row.get("phase")) != "completed"
                or str(row.get("status"))
                not in {
                    "completed",
                    "verification_pending",
                }
                or row.get("terminal_events_drained_at") is None
                or int(row.get("relay_cursor") or 0) != int(row.get("ack_cursor") or 0)
            ):
                raise FormalRunArchiveError(
                    "successful formal archive requires a completed, drained durable assignment"
                )
            if set(request.test_run_ids) & set(request.business_run_ids):
                raise FormalRunArchiveError("test and business run identities must be disjoint")
            if set(request.artifact_ids) & set(request.host_receipt_ids):
                raise FormalRunArchiveError("artifact and host-receipt identities must be disjoint")
            session_id = UUID(str(row["session_id"]))
            channel, collaboration_messages, reports = self._assert_channel(
                assignment,
                session_id,
                export,
                expected_revision=request.expected_channel_revision,
            )
            if channel.status is not ChannelStatus.active:
                raise FormalRunArchiveError("successful archive requires an active claim channel")
            if any(
                report.status not in _RESOLVED_REPORT_STATUSES[report.category]
                for report in reports
            ):
                raise FormalRunArchiveError(
                    "successful archive cannot account for an unresolved report"
                )
            try:
                workflow_export = await self._workflow.export_formal_run_snapshot(
                    str(assignment.target.application_id),
                    assignment_id=str(assignment.assignment_id),
                    session_id=str(session_id),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise FormalRunArchiveUnavailable(
                    "formal archive workflow snapshot is unavailable"
                ) from error
            if self._auth_store is None:
                raise FormalRunArchiveError(
                    "formal forbidden-assistance scanner has no blackbox audit source"
                )
            try:
                blackbox_auth_export = (
                    await self._auth_store.export_assignment_snapshot(
                        assignment_id=assignment.assignment_id,
                        session_id=session_id,
                    )
                )
            except Exception as error:
                raise FormalRunArchiveUnavailable(
                    "formal blackbox request snapshot is unavailable"
                ) from error
            self._assert_blackbox_credential_policy(
                blackbox_auth_export=blackbox_auth_export,
                assignment=assignment,
                session_id=session_id,
            )
            connector_budget_export = await self._export_connector_budget(
                assignment
            )
            (
                blackbox_test_run_ids,
                blackbox_business_run_ids,
            ) = self._blackbox_run_operation_ids(
                blackbox_auth_export=blackbox_auth_export,
                assignment=assignment,
                session_id=session_id,
            )
            draft = workflow_export["draft"]
            application = workflow_export["application"]
            snapshot = ApplicationSnapshot.model_validate(draft.get("snapshot"))
            draft_revision = int(draft["revision"])
            content_hash = _content_hash(draft["content_hash"])
            if not hmac.compare_digest(
                content_hash,
                f"sha256:{snapshot.content_hash()}",
            ):
                raise FormalRunArchiveError("current draft content hash is inconsistent")
            published_version: int | None = None
            active_version = application.get("active_version")
            if active_version is not None:
                version = workflow_export.get("published_version")
                if not isinstance(version, Mapping):
                    raise FormalRunArchiveError("published version projection is unavailable")
                if hmac.compare_digest(
                    _content_hash(version["content_hash"]),
                    content_hash,
                ):
                    published_version = int(active_version)
            reported_test_run_ids = self._reported_test_run_ids(
                draft=draft,
                content_hash=content_hash,
            )
            test_run_rows, business_run_rows = self._classified_runs(
                run_rows=workflow_export["runs"],
                reported_test_run_ids=reported_test_run_ids,
                blackbox_test_run_ids=blackbox_test_run_ids,
                blackbox_business_run_ids=blackbox_business_run_ids,
                assignment=assignment,
                session_id=session_id,
                draft_revision=draft_revision,
                content_hash=content_hash,
                published_version=published_version,
            )
            test_run_ids = [str(item["id"]) for item in test_run_rows]
            business_run_ids = [str(item["id"]) for item in business_run_rows]
            if set(request.test_run_ids) != set(test_run_ids) or set(
                request.business_run_ids
            ) != set(business_run_ids):
                raise FormalRunArchiveError(
                    "formal archive run sets do not match the complete platform-owned "
                    "test and business run sets"
                )
            allowed_run_ids = {*test_run_ids, *business_run_ids}
            artifact_files, artifact_refs, artifact_index_entries = await self._evidence_files(
                artifact_ids=request.artifact_ids,
                kind=EvidenceKind.artifact,
                assignment=assignment,
                session_id=session_id,
                allowed_run_ids=allowed_run_ids,
            )
            receipt_files, receipt_refs, receipt_index_entries = await self._evidence_files(
                artifact_ids=request.host_receipt_ids,
                kind=EvidenceKind.host_receipt,
                assignment=assignment,
                session_id=session_id,
                allowed_run_ids=allowed_run_ids,
            )
            task = assignment.task_package
            if task is None:  # pragma: no cover - guarded by _assignment_from_row
                raise FormalRunArchiveError("formal assignment has no task package")
            package = self._manager.load_frozen(
                task.task_id,
                task.revision,
                expected_public_digest=task.public_summary_digest,
            )
            try:
                evidence_entries = [
                    *artifact_index_entries,
                    *receipt_index_entries,
                ]
                evidence_index = ArchivedEvidenceIndex(
                    schema_version="1.0",
                    task_id=task.task_id,
                    revision=task.revision,
                    run_id=task.run_id,
                    assignment_id=assignment.assignment_id,
                    application_id=UUID(str(assignment.target.application_id)),
                    entry_count=len(evidence_entries),
                    entries=evidence_entries,
                )
            except ValueError as error:
                raise FormalRunArchiveError(
                    "successful archive requires uniquely indexed business evidence"
                ) from error
            resolved_report_ids = [report.report_id for report in reports]
            binding = ArchiveClaimBinding(
                claim_id=request.claim_id,
                assignment_id=assignment.assignment_id,
                application_id=UUID(str(assignment.target.application_id)),
                draft_revision=draft_revision,
                content_hash=content_hash,
                published_version=published_version,
                test_run_ids=test_run_ids,
                business_run_ids=business_run_ids,
                artifact_digests=[item.digest for item in artifact_refs],
                host_receipt_digests=[item.digest for item in receipt_refs],
                resolved_report_ids=resolved_report_ids,
                remaining_limits=request.remaining_limits,
            )
            messages = self._bridge_messages(
                assignment=assignment,
                session_id=session_id,
                events=bridge_export["events"],
            )
            if len([item for item in messages if item.kind == "assignment.accepted"]) != 1:
                raise FormalRunArchiveError(
                    "successful archive requires one durable assignment acceptance"
                )
            outcome = ArchivedPlatformOutcome(
                application_id=binding.application_id,
                draft_revision=binding.draft_revision,
                content_hash=binding.content_hash,
                published_version=binding.published_version,
                test_run_ids=binding.test_run_ids,
                business_run_ids=binding.business_run_ids,
                artifact_digests=binding.artifact_digests,
                host_receipt_digests=binding.host_receipt_digests,
            )
            platform_records = self._platform_records(
                assignment=assignment,
                # Preserve the full assignment/session denominator, including
                # failed, cancelled, and historical-draft runs.  The successful
                # claim sets above are a platform-derived subset, never an
                # archive filter controlled by the caller.
                run_rows=workflow_export["runs"],
                outcome=outcome,
            )
            collaboration_records = self._collaboration_records(
                assignment=assignment,
                channel=channel,
                messages=collaboration_messages,
                reports=reports,
                binding=binding,
            )
            scan_created_at = datetime.fromisoformat(
                str(row["terminal_events_drained_at"]).replace(
                    "Z",
                    "+00:00",
                )
            ).astimezone(timezone.utc)
            if self._source_provenance is None:
                raise FormalRunArchiveError(
                    "formal archive has no developer source provenance boundary"
                )
            try:
                expected_source_bindings = approved_developer_response_bindings(
                    collaboration_messages,
                    channel_id=channel.channel_id,
                )
                source_archive = await asyncio.to_thread(
                    self._source_provenance.finalize_archive,
                    assignment_id=assignment.assignment_id,
                    expected_bindings=expected_source_bindings,
                    finalized_at=scan_created_at,
                )
            except Exception as error:
                raise FormalRunArchiveError(
                    "formal developer source provenance was rejected"
                ) from error
            source_provenance_export = source_archive.manifest.model_dump(
                mode="json",
                exclude_none=True,
            )
            try:
                source_semantic_input = derive_source_semantic_input(
                    task_package=package,
                    source_manifest=source_archive.manifest,
                    source_files=source_archive.files,
                )
            except Exception as error:
                raise FormalRunArchiveError(
                    "formal source semantic input could not be derived"
                ) from error
            source_semantic_export = source_semantic_input.model_dump(
                mode="json",
                exclude_none=True,
            )
            try:
                artifact_inventory_export = (
                    await self._artifact_store.export_assignment_inventory(
                        assignment_id=assignment.assignment_id,
                        session_id=session_id,
                        application_id=UUID(
                            str(assignment.target.application_id)
                        ),
                    )
                )
                assistance_scan = scan_forbidden_assistance(
                    assignment=assignment,
                    session_id=session_id,
                    channel_id=channel.channel_id,
                    bridge_export=bridge_export,
                    collaboration_export=export,
                    workflow_export=workflow_export,
                    blackbox_auth_export=blackbox_auth_export,
                    artifact_inventory_export=artifact_inventory_export,
                    source_provenance_export=source_provenance_export,
                    source_semantic_export=source_semantic_export,
                    source_semantic_task_package=package,
                    source_semantic_files=source_archive.files,
                    evidence_index=evidence_index,
                    business_run_ids=binding.business_run_ids,
                    validation_mode=ValidationMode.real_host.value,
                    created_at=scan_created_at,
                )
            except Exception as error:
                raise FormalRunArchiveUnavailable(
                    "forbidden-assistance scan inputs are unavailable"
                ) from error
            result = ArchivedRunResult(
                schema_version="1.0",
                task_id=task.task_id,
                revision=task.revision,
                run_id=task.run_id,
                assignment_id=assignment.assignment_id,
                application_id=binding.application_id,
                archive_status=ArchiveStatus.succeeded,
                validation_mode=ValidationMode.real_host,
                business_status="succeeded",
                business_run_ids=binding.business_run_ids,
                artifact_digests=binding.artifact_digests,
                host_receipt_digests=binding.host_receipt_digests,
                remaining_limits=binding.remaining_limits,
                summary=request.summary,
            )
            files: dict[str, bytes | Path] = {
                "assignment.json": _canonical_json(assignment),
                "draft.json": _canonical_json(
                    {
                        "revision": draft_revision,
                        "content_hash": content_hash,
                        "snapshot": snapshot.model_dump(mode="json", exclude_none=True),
                    }
                ),
                "messages.jsonl": _jsonl(messages),
                "platform-events.jsonl": _jsonl(platform_records),
                "collaboration.jsonl": _jsonl(collaboration_records),
                "evidence-index.json": _canonical_json(evidence_index),
                "forbidden-assistance-scan.json": _canonical_json(
                    assistance_scan
                ),
                "scanner-inputs/bridge.json": _canonical_json(
                    bridge_export
                ),
                "scanner-inputs/collaboration.json": _canonical_json(export),
                "scanner-inputs/workflow.json": _canonical_json(
                    workflow_export
                ),
                "scanner-inputs/blackbox-auth.json": _canonical_json(
                    blackbox_auth_export
                ),
                "connector-budget.json": _canonical_json(
                    connector_budget_export
                ),
                "scanner-inputs/connector-budget.json": _canonical_json(
                    connector_budget_export
                ),
                "scanner-inputs/artifact-inventory.json": _canonical_json(
                    artifact_inventory_export
                ),
                "scanner-inputs/source-semantic.json": _canonical_json(
                    source_semantic_input
                ),
                "result.json": _canonical_json(result),
                **source_archive.files,
                **artifact_files,
                **receipt_files,
            }
            latest_bridge = await self._bridge_store.export_assignment(assignment.assignment_id)
            latest_collaboration = await self._collaboration_store.export_channel(
                channel.channel_id
            )
            latest_workflow = await self._workflow.export_formal_run_snapshot(
                str(assignment.target.application_id),
                assignment_id=str(assignment.assignment_id),
                session_id=str(session_id),
            )
            latest_blackbox_auth = (
                await self._auth_store.export_assignment_snapshot(
                    assignment_id=assignment.assignment_id,
                    session_id=session_id,
                )
            )
            latest_connector_budget = await self._export_connector_budget(
                assignment
            )
            latest_artifact_inventory = (
                await self._artifact_store.export_assignment_inventory(
                    assignment_id=assignment.assignment_id,
                    session_id=session_id,
                    application_id=UUID(str(assignment.target.application_id)),
                )
            )
            try:
                latest_source_archive = await asyncio.to_thread(
                    self._source_provenance.finalize_archive,
                    assignment_id=assignment.assignment_id,
                    expected_bindings=expected_source_bindings,
                    finalized_at=scan_created_at,
                )
            except Exception as error:
                raise FormalRunArchiveUnavailable(
                    "formal developer source changed during archive export"
                ) from error
            if (
                not hmac.compare_digest(
                    _digest(bridge_export),
                    _digest(latest_bridge),
                )
                or not hmac.compare_digest(
                    _digest(export),
                    _digest(latest_collaboration),
                )
                or not hmac.compare_digest(
                    _digest(workflow_export),
                    _digest(latest_workflow),
                )
                or not hmac.compare_digest(
                    _digest(blackbox_auth_export),
                    _digest(latest_blackbox_auth),
                )
                or not hmac.compare_digest(
                    _digest(connector_budget_export),
                    _digest(latest_connector_budget),
                )
                or not hmac.compare_digest(
                    _digest(artifact_inventory_export),
                    _digest(latest_artifact_inventory),
                )
                or latest_source_archive.manifest != source_archive.manifest
                or dict(latest_source_archive.files) != dict(source_archive.files)
            ):
                raise FormalRunArchiveUnavailable(
                    "formal durable stores changed during archive export"
                )
            preflight_files, _ = self._reserved_preflight_evidence(
                task_id=task.task_id,
                revision=task.revision,
                run_id=task.run_id,
                assignment_id=assignment.assignment_id,
                environment_instance_id=task.environment_instance_id,
            )
            files.update(preflight_files)
            ready_path = (
                self._task_state_root
                / "preflight"
                / task.task_id
                / str(task.revision)
                / task.run_id
                / "environment-ready.json"
            )
            workspace_manifest_path = (
                self._public_workspace_root
                / str(assignment.assignment_id)
                / WORKSPACE_MANIFEST_FILE
            )
            try:
                _, manifest, manifest_digest = await asyncio.to_thread(
                    self._manager.archive_run,
                    package,
                    run_id=task.run_id,
                    status=ArchiveStatus.succeeded,
                    validation_mode=ValidationMode.real_host,
                    environment_ready_path=ready_path,
                    workspace_manifest_path=workspace_manifest_path,
                    files=files,
                    claim_binding=binding,
                    forbidden_assistance_findings=[
                        f"{item.rule_id}:{item.source_ref}"
                        for item in assistance_scan.findings
                    ],
                )
            except (OSError, TimeoutError) as error:
                raise FormalRunArchiveUnavailable(
                    "platform-owned success archive is temporarily unavailable"
                ) from error
            except Exception as error:
                raise FormalRunArchiveError(
                    "platform-owned success archive was rejected"
                ) from error
            if manifest.status is ArchiveStatus.invalid:
                raise FormalRunArchiveInvalid(
                    "platform-owned success archive was marked invalid",
                    result=FormalTerminalArchiveResult(
                        task_id=task.task_id,
                        revision=task.revision,
                        run_id=task.run_id,
                        assignment_id=assignment.assignment_id,
                        status=ArchiveStatus.invalid,
                        archive_manifest_digest=manifest_digest,
                    ),
                )
            if (
                manifest.status is not ArchiveStatus.succeeded
                or manifest.claim_binding != binding
            ):
                raise FormalRunArchiveError("platform-owned success archive was marked invalid")
            claim_payload: dict[str, Any] = {
                "schema_version": "1.1",
                "claim_id": str(binding.claim_id),
                "application_id": str(binding.application_id),
                "draft_revision": binding.draft_revision,
                "content_hash": binding.content_hash,
                "published_version": binding.published_version,
                "test_run_ids": binding.test_run_ids,
                "business_run_ids": binding.business_run_ids,
                "artifact_refs": [item.model_dump(mode="json") for item in artifact_refs],
                "host_receipt_refs": [item.model_dump(mode="json") for item in receipt_refs],
                "resolved_report_ids": binding.resolved_report_ids,
                "remaining_limits": binding.remaining_limits,
                "task_package_digest": manifest.public_summary_digest,
                "environment_ready_digest": str(manifest.environment_ready_digest),
                "archive_manifest_digest": manifest_digest,
                "verification_process_digest": (
                    manifest.verification_process_digest
                ),
                "validation_mode": "real_host",
                "claim": "ready_for_independent_verification",
            }
            claim_payload["frozen_context_digest"] = frozen_claim_context_digest(claim_payload)
            verification_claim = VerificationClaimPayload.model_validate(claim_payload)
            return FormalRunArchivePreparationResult(
                task_id=task.task_id,
                revision=task.revision,
                run_id=task.run_id,
                assignment_id=assignment.assignment_id,
                channel_id=channel.channel_id,
                public_summary_digest=manifest.public_summary_digest,
                environment_ready_digest=str(manifest.environment_ready_digest),
                workspace_mount_digest=str(manifest.workspace_mount_digest),
                archive_manifest_digest=manifest_digest,
                claim_binding=binding,
                artifact_refs=artifact_refs,
                host_receipt_refs=receipt_refs,
                verification_claim=verification_claim,
            )

    async def _archive_reserved_terminal(
        self,
        *,
        row: Mapping[str, Any],
        bridge_export: Mapping[str, Any],
        request: _ReservedFormalRequest,
        status: ArchiveStatus,
    ) -> FormalTerminalArchiveResult:
        assignment_id = UUID(str(row["assignment_id"]))
        application_id = UUID(str(row["application_id"]))
        build_id = UUID(str(row["build_id"]))
        session_id = UUID(str(row["session_id"]))
        run_id = f"formal-run:{build_id}"
        events = bridge_export.get("events")
        counts = bridge_export.get("counts")
        watermark = bridge_export.get("watermark")
        if (
            row.get("daemon_session_creation_started_at") is not None
            or row.get("daemon_status") is not None
            or int(row.get("relay_cursor") or 0) != 0
            or int(row.get("ack_cursor") or 0) != 0
            or row.get("credential_ref") is not None
            or row.get("collaboration_credential_ref") is not None
            or row.get("formal_workspace_receipt_json") is not None
            or events != []
            or not isinstance(counts, Mapping)
            or counts.get("events") != 0
            or not isinstance(watermark, Mapping)
            or watermark.get("min_daemon_seq") is not None
            or watermark.get("max_daemon_seq") is not None
            or int(watermark.get("relay_cursor") or 0) != 0
            or int(watermark.get("ack_cursor") or 0) != 0
            or bridge_export.get("complete") is not True
        ):
            raise FormalRunArchiveError(
                "reserved terminal archive crossed the pre-daemon boundary"
            )
        prepared_assignment: BuildAssignment | None = None
        prepared_assignment_payload: bytes | None = None
        prepared_assignment_digest: str | None = None
        encoded_assignment = row.get("submission_json")
        if encoded_assignment is not None:
            if not isinstance(encoded_assignment, str) or not encoded_assignment:
                raise FormalRunArchiveError(
                    "manager-prepared assignment projection is invalid"
                )
            prepared_assignment_payload = encoded_assignment.encode("utf-8")
            prepared_assignment_digest = _digest(
                prepared_assignment_payload
            )
            prepared_assignment = self._assignment_from_row(row)
            task = prepared_assignment.task_package
            access = prepared_assignment.collaboration
            if (
                task is None
                or access is None
                or task.task_id != request.task_id
                or task.revision != request.revision
                or task.run_id != run_id
            ):
                raise FormalRunArchiveError(
                    "manager-prepared assignment differs from its reservation"
                )
            preparation_state = "manager_prepared"
            scan_reason = "assignment_not_delivered_to_daemon"
            channel_id = access.channel_id
        else:
            preparation_state = "request_reserved"
            scan_reason = "build_assignment_not_issued"
            channel_id = self._reserved_channel_id(
                task_id=request.task_id,
                revision=request.revision,
                assignment_id=assignment_id,
            )
        package = self._manager.load_frozen(
            request.task_id,
            request.revision,
            expected_public_digest=(
                prepared_assignment.task_package.public_summary_digest
                if prepared_assignment is not None
                and prepared_assignment.task_package is not None
                else None
            ),
        )
        try:
            workflow_export = await self._workflow.export_formal_run_snapshot(
                str(application_id),
                assignment_id=str(assignment_id),
                session_id=str(session_id),
            )
        except (OSError, TimeoutError) as error:
            raise FormalRunArchiveUnavailable(
                "reserved terminal workflow export is temporarily unavailable"
            ) from error
        except Exception as error:
            raise FormalRunArchiveError(
                "reserved terminal workflow export was rejected"
            ) from error
        try:
            collaboration_export = await self._collaboration_store.export_channel(
                channel_id
            )
        except CollaborationNotFound:
            if prepared_assignment is not None:
                collaboration_export = self._missing_collaboration_export(
                    prepared_assignment,
                    session_id,
                )
            else:
                collaboration_export = self._missing_reserved_collaboration_export(
                    channel_id=channel_id,
                    assignment_id=assignment_id,
                    session_id=session_id,
                    task_id=request.task_id,
                    revision=request.revision,
                    application_id=application_id,
                )
        except (OSError, TimeoutError) as error:
            raise FormalRunArchiveUnavailable(
                "reserved terminal collaboration export is temporarily unavailable"
            ) from error
        except Exception as error:
            raise FormalRunArchiveError(
                "reserved terminal collaboration export was rejected"
            ) from error
        if (
            prepared_assignment is not None
            and collaboration_export.get("complete") is True
        ):
            try:
                self._assert_channel(
                    prepared_assignment,
                    session_id,
                    collaboration_export,
                )
            except Exception as error:
                raise FormalRunArchiveError(
                    "manager-prepared assignment collaboration changed"
                ) from error
        if self._auth_store is None:
            raise FormalRunArchiveError(
                "reserved terminal archive has no blackbox audit source"
            )
        try:
            blackbox_auth_export = await self._auth_store.export_assignment_snapshot(
                assignment_id=assignment_id,
                session_id=session_id,
            )
            artifact_inventory_export = (
                await self._artifact_store.export_assignment_inventory(
                    assignment_id=assignment_id,
                    session_id=session_id,
                    application_id=application_id,
                )
            )
        except (OSError, TimeoutError) as error:
            raise FormalRunArchiveUnavailable(
                "reserved terminal scanner inputs are temporarily unavailable"
            ) from error
        except Exception as error:
            raise FormalRunArchiveError(
                "reserved terminal scanner inputs were rejected"
            ) from error
        auth_collections = (
            "credentials",
            "credential_applications",
            "requests",
            "audit",
            "security_events",
        )
        auth_counts = blackbox_auth_export.get("counts")
        if (
            str(blackbox_auth_export.get("assignment_id"))
            != str(assignment_id)
            or str(blackbox_auth_export.get("session_id"))
            != str(session_id)
            or any(
                blackbox_auth_export.get(name) != []
                for name in auth_collections
            )
            or not isinstance(auth_counts, Mapping)
            or any(auth_counts.get(name) != 0 for name in auth_collections)
        ):
            raise FormalRunArchiveError(
                "reserved terminal archive has credential or blackbox side effects"
            )

        run_rows = workflow_export.get("runs")
        inventory_records = artifact_inventory_export.get("records")
        if not isinstance(run_rows, list) or not isinstance(
            inventory_records,
            list,
        ):
            raise FormalRunArchiveError(
                "reserved terminal evidence denominator is unavailable"
            )
        workflow_run_ids = [str(item.get("id") or "") for item in run_rows]
        if any(not item for item in workflow_run_ids) or len(
            workflow_run_ids
        ) != len(set(workflow_run_ids)):
            raise FormalRunArchiveError(
                "reserved terminal workflow denominator is invalid"
            )
        inventory_run_ids = [
            str(item.get("run_id") or "")
            for item in inventory_records
            if isinstance(item, Mapping) and item.get("run_id")
        ]
        business_run_ids = list(
            dict.fromkeys([*workflow_run_ids, *inventory_run_ids])
        ) or [run_id]
        evidence_files, artifact_refs, receipt_refs, evidence_entries = (
            await self._reserved_evidence_files(
                inventory_records=[
                    item
                    for item in inventory_records
                    if isinstance(item, Mapping)
                ],
                assignment_id=assignment_id,
                session_id=session_id,
                application_id=application_id,
                allowed_run_ids=set(business_run_ids),
            )
        )
        if len(inventory_records) != len(
            [
                item
                for item in inventory_records
                if isinstance(item, Mapping)
            ]
        ):
            raise FormalRunArchiveError(
                "reserved terminal artifact inventory is invalid"
            )
        evidence_index = ArchivedEvidenceIndex(
            schema_version="1.0",
            task_id=request.task_id,
            revision=request.revision,
            run_id=run_id,
            assignment_id=assignment_id,
            application_id=application_id,
            entry_count=len(evidence_entries),
            entries=evidence_entries,
        )
        preflight_files, preflight_entries = (
            self._reserved_preflight_evidence(
                task_id=request.task_id,
                revision=request.revision,
                run_id=run_id,
                assignment_id=assignment_id,
                environment_instance_id=request.environment_instance_id,
            )
        )
        ready_candidate = (
            self._task_state_root
            / "preflight"
            / request.task_id
            / str(request.revision)
            / run_id
            / "environment-ready.json"
        )
        ready_path: Path | None = None
        ready_digest: str | None = None
        ready: EnvironmentReady | None = None
        if ready_candidate.exists():
            try:
                ready = EnvironmentReady.model_validate_json(
                    ready_candidate.read_bytes()
                )
                _, ready_digest = self._manager.require_environment_ready(
                    package,
                    ready_candidate,
                    run_id=run_id,
                    assignment_id=assignment_id,
                    at=ready.finished_at,
                )
            except Exception as error:
                raise FormalRunArchiveError(
                    "reserved terminal readiness evidence is invalid"
                ) from error
            ready_path = ready_candidate
        workspace_candidate = (
            self._public_workspace_root
            / str(assignment_id)
            / WORKSPACE_MANIFEST_FILE
        )
        workspace_path: Path | None = None
        workspace_digest: str | None = None
        if workspace_candidate.exists():
            if ready is None or ready_digest is None:
                raise FormalRunArchiveError(
                    "reserved terminal workspace has no readiness evidence"
                )
            try:
                _, workspace_digest, _ = (
                    self._manager.require_workspace_manifest(
                        package,
                        workspace_candidate,
                        role=WorkspaceRole.lilies,
                        run_id=run_id,
                        assignment_id=assignment_id,
                        environment_ready_digest=ready_digest,
                        environment_instance_id=ready.environment_instance_id,
                    )
                )
            except Exception as error:
                raise FormalRunArchiveError(
                    "reserved terminal workspace evidence is invalid"
                ) from error
            workspace_path = workspace_candidate
        if prepared_assignment is not None:
            task = prepared_assignment.task_package
            if (
                task is None
                or task.environment_instance_id
                != request.environment_instance_id
                or task.environment_ready_digest != ready_digest
                or task.workspace_mount_digest != workspace_digest
            ):
                raise FormalRunArchiveError(
                    "manager-prepared assignment changed its control evidence"
                )

        source_provenance_export: dict[str, Any] = {
            "schema_version": "1.0",
            "complete": False,
            "missing_reason": (
                scan_reason
                if prepared_assignment is not None
                else "source_baseline_not_established"
            ),
            "task_id": request.task_id,
            "task_revision": request.revision,
            "run_id": run_id,
            "assignment_id": str(assignment_id),
            "channel_id": str(channel_id),
            "approved_commits": [],
        }
        connector_budget_export = self._missing_connector_budget(
            assignment_id=assignment_id,
            allowed_network_hosts=package.allowed_actions.network_hosts,
            allowed_compensation_operations=(
                package.allowed_actions.compensation_actions
            ),
            max_write_count=package.allowed_actions.max_write_count,
            max_payload_bytes=package.allowed_actions.max_payload_bytes,
        )
        scanner_inputs: dict[str, bytes] = {
            "evidence-index.json": _canonical_json(evidence_index),
            "scanner-inputs/bridge.json": _canonical_json(bridge_export),
            "scanner-inputs/collaboration.json": _canonical_json(
                collaboration_export
            ),
            "scanner-inputs/workflow.json": _canonical_json(workflow_export),
            "scanner-inputs/blackbox-auth.json": _canonical_json(
                blackbox_auth_export
            ),
            "connector-budget.json": _canonical_json(
                connector_budget_export
            ),
            "scanner-inputs/connector-budget.json": _canonical_json(
                connector_budget_export
            ),
            "scanner-inputs/artifact-inventory.json": _canonical_json(
                artifact_inventory_export
            ),
            "source-provenance/manifest.json": _canonical_json(
                source_provenance_export
            ),
            **preflight_files,
        }
        if prepared_assignment_payload is not None:
            scanner_inputs[
                "manager-prepared-assignment.json"
            ] = prepared_assignment_payload
        if ready_path is not None:
            scanner_inputs["environment-ready.json"] = ready_path.read_bytes()
        if workspace_path is not None:
            scanner_inputs["workspace-mount.json"] = workspace_path.read_bytes()
        input_bindings = sorted(
            (
                FileDigestEntry(
                    path=path,
                    digest=_digest(payload),
                    size_bytes=len(payload),
                )
                for path, payload in scanner_inputs.items()
            ),
            key=lambda item: item.path,
        )
        scan_created_at = datetime.fromisoformat(
            str(row["terminal_events_drained_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        scan_payload = {
            "schema_version": "1.0",
            "task_id": request.task_id,
            "revision": request.revision,
            "run_id": run_id,
            "assignment_id": str(assignment_id),
            "session_id": str(session_id),
            "application_id": str(application_id),
            "channel_id": str(channel_id),
            "scanner_applicable": False,
            "verdict": "inconclusive",
            "reason": scan_reason,
            "input_bindings": [
                item.model_dump(mode="json")
                for item in input_bindings
            ],
            "created_at": scan_created_at.isoformat().replace("+00:00", "Z"),
        }
        preassignment_scan = ArchivedPreassignmentScanRecord.model_validate(
            {
                **scan_payload,
                "scan_digest": _digest(scan_payload),
            }
        )
        request_payload = request.model_dump(mode="json")
        reservation = ArchivedFormalReservation(
            schema_version="1.0",
            task_id=request.task_id,
            revision=request.revision,
            run_id=run_id,
            assignment_id=assignment_id,
            application_id=application_id,
            build_id=build_id,
            session_id=session_id,
            connection_id=request.connection_id,
            channel_id=channel_id,
            environment_instance_id=request.environment_instance_id,
            idempotency_key=request.idempotency_key,
            request_digest=_digest(
                {
                    "application_id": str(application_id),
                    "request": request_payload,
                }
            ),
            request_payload_digest=_digest(request_payload),
            preparation_state=preparation_state,
            manager_prepared_assignment_digest=(
                prepared_assignment_digest
            ),
            daemon_assignment_delivery="not_started",
            daemon_session_creation_started_at=None,
            daemon_status=None,
            relay_cursor=0,
            ack_cursor=0,
            daemon_event_count=0,
            credential_ref=None,
            collaboration_credential_ref=None,
            formal_workspace_receipt_json=None,
            phase=str(row["phase"]),
            status=str(row["status"]),
            desired_state=str(row["desired_state"]),
            terminal_events_drained_at=scan_created_at,
            last_error_code=(
                str(row["last_error_code"])
                if row.get("last_error_code") is not None
                else None
            ),
            last_error_message=(
                str(row["last_error_message"])
                if row.get("last_error_message") is not None
                else None
            ),
            preflight_evidence=preflight_entries,
            environment_ready_digest=ready_digest,
            workspace_mount_digest=workspace_digest,
        )
        result = ArchivedRunResult(
            schema_version="1.0",
            task_id=request.task_id,
            revision=request.revision,
            run_id=run_id,
            assignment_id=assignment_id,
            application_id=application_id,
            archive_status=status,
            validation_mode=ValidationMode.real_host,
            business_status=(
                "environment_failed"
                if status is ArchiveStatus.environment_failed
                else "assignment_cancelled"
                if status is ArchiveStatus.cancelled
                else "assignment_failed"
            ),
            business_run_ids=business_run_ids,
            artifact_digests=[item.digest for item in artifact_refs],
            host_receipt_digests=[item.digest for item in receipt_refs],
            remaining_limits=[
                str(
                    row.get("last_error_code")
                    or scan_reason
                )
            ],
            summary=(
                str(row.get("last_error_message") or "")
                or (
                    "Manager-prepared formal assignment was never delivered "
                    "to the daemon."
                    if prepared_assignment is not None
                    else (
                        "Formal reservation ended before BuildAssignment "
                        "issuance."
                    )
                )
            ),
        )
        files: dict[str, bytes | Path] = {
            "reserved-assignment.json": _canonical_json(
                reservation.model_dump(
                    mode="json",
                    exclude_none=False,
                )
            ),
            "result.json": _canonical_json(result),
            "preassignment-scan.json": _canonical_json(preassignment_scan),
            **{
                path: payload
                for path, payload in scanner_inputs.items()
                if path
                not in {
                    "environment-ready.json",
                    "workspace-mount.json",
                }
            },
            **evidence_files,
        }
        finding = (
            "scanner_inconclusive:pre_daemon:"
            "assignment_not_delivered_to_daemon"
            if prepared_assignment is not None
            else (
                "scanner_inconclusive:preassignment:"
                "build_assignment_not_issued"
            )
        )
        try:
            _, _, manifest_digest = await asyncio.to_thread(
                self._manager.archive_run,
                package,
                run_id=run_id,
                status=status,
                validation_mode=ValidationMode.real_host,
                environment_ready_path=ready_path,
                workspace_manifest_path=workspace_path,
                files=files,
                claim_binding=None,
                forbidden_assistance_findings=[finding],
            )
        except (OSError, TimeoutError) as error:
            raise FormalRunArchiveUnavailable(
                "reserved terminal archive is temporarily unavailable"
            ) from error
        except Exception as error:
            raise FormalRunArchiveError(
                "reserved terminal archive was rejected"
            ) from error
        return FormalTerminalArchiveResult(
            task_id=request.task_id,
            revision=request.revision,
            run_id=run_id,
            assignment_id=assignment_id,
            status=status,
            archive_manifest_digest=manifest_digest,
        )

    async def archive_terminal_assignment(
        self,
        assignment_id: UUID,
    ) -> FormalTerminalArchiveResult | None:
        async with self._lock(assignment_id):
            try:
                bridge_export = await self._bridge_store.export_assignment(assignment_id)
            except (OSError, TimeoutError) as error:
                raise FormalRunArchiveUnavailable(
                    "terminal bridge export is temporarily unavailable"
                ) from error
            row = bridge_export["assignment"]
            if str(row.get("assignment_mode")) != AssignmentMode.formal_experiment.value:
                return None
            phase = str(row.get("phase"))
            if phase not in {"cancelled", "error"}:
                return None
            if row.get("terminal_events_drained_at") is None or int(
                row.get("relay_cursor") or 0
            ) != int(row.get("ack_cursor") or 0):
                raise FormalRunArchiveError(
                    "terminal formal archive requires a sealed daemon event stream"
                )
            try:
                reserved_request = _ReservedFormalRequest.model_validate_json(
                    str(row["request_json"])
                )
            except (KeyError, ValueError, TypeError) as error:
                raise FormalRunArchiveError(
                    "terminal formal reservation request is invalid"
                ) from error
            expected_run_id = f"formal-run:{row['build_id']}"
            status = (
                ArchiveStatus.cancelled
                if phase == "cancelled"
                else ArchiveStatus.invalid
                if str(row.get("status")) == "invalid"
                else ArchiveStatus.environment_failed
                if str(row.get("status")) == "environment_failed"
                else ArchiveStatus.failed
            )
            persisted_result = row.get("formal_terminal_archive_result_json")
            persisted_digest = row.get("formal_terminal_archive_manifest_digest")
            if row.get("formal_terminal_archive_completed_at") is not None:
                try:
                    result = FormalTerminalArchiveResult.model_validate_json(
                        str(persisted_result)
                    )
                    if (
                        result.assignment_id != assignment_id
                        or result.task_id != reserved_request.task_id
                        or result.revision != reserved_request.revision
                        or result.run_id != expected_run_id
                        or result.status is not status
                        or not hmac.compare_digest(
                            result.archive_manifest_digest,
                            str(persisted_digest),
                        )
                    ):
                        raise ValueError("terminal archive checkpoint changed its binding")
                    replayed = self.replay(result)
                    if (
                        result.status is ArchiveStatus.invalid
                        and (
                            replayed.status is not ArchiveStatus.invalid
                            or replayed.source_status is not ArchiveStatus.succeeded
                        )
                    ) or (
                        result.status is not ArchiveStatus.invalid
                        and replayed.source_status is not result.status
                    ):
                        raise ValueError("terminal archive source status changed")
                except (TypeError, ValueError, TaskPackageConflict) as error:
                    raise FormalRunArchiveError(
                        "terminal archive checkpoint is invalid"
                    ) from error
                return result
            if row.get("daemon_session_creation_started_at") is None:
                return await self._archive_reserved_terminal(
                    row=row,
                    bridge_export=bridge_export,
                    request=reserved_request,
                    status=status,
                )
            assignment = self._assignment_from_row(row)
            task = assignment.task_package
            if task is None:  # pragma: no cover - guarded by _assignment_from_row
                raise FormalRunArchiveError("formal assignment has no task package")
            if (
                task.task_id != reserved_request.task_id
                or task.revision != reserved_request.revision
                or task.run_id != expected_run_id
            ):
                raise FormalRunArchiveError(
                    "submitted formal assignment differs from its reservation"
                )
            package = self._manager.load_frozen(
                task.task_id,
                task.revision,
                expected_public_digest=task.public_summary_digest,
            )
            application_id = UUID(str(row["application_id"]))
            if status is ArchiveStatus.invalid:
                run_root = package.root / "runs" / task.run_id
                manifest_path = run_root / "archive-manifest.json"
                try:
                    manifest_payload = manifest_path.read_bytes()
                    manifest_digest = _digest(manifest_payload)
                    manifest = self._manager.replay_archive(
                        run_root,
                        expected_manifest_digest=manifest_digest,
                    )
                except (OSError, ValueError, TaskPackageConflict) as error:
                    raise FormalRunArchiveError(
                        "sealed invalid success archive is unavailable"
                    ) from error
                binding = manifest.claim_binding
                if (
                    manifest.task_id != task.task_id
                    or manifest.revision != task.revision
                    or manifest.run_id != task.run_id
                    or manifest.status is not ArchiveStatus.invalid
                    or manifest.source_status is not ArchiveStatus.succeeded
                    or binding is None
                    or binding.assignment_id != assignment.assignment_id
                    or binding.application_id != application_id
                ):
                    raise FormalRunArchiveError(
                        "sealed invalid success archive changed its binding"
                    )
                return FormalTerminalArchiveResult(
                    task_id=task.task_id,
                    revision=task.revision,
                    run_id=task.run_id,
                    assignment_id=assignment.assignment_id,
                    status=ArchiveStatus.invalid,
                    archive_manifest_digest=manifest_digest,
                )
            session_id = UUID(str(row["session_id"]))
            messages = self._bridge_messages(
                assignment=assignment,
                session_id=session_id,
                events=bridge_export["events"],
            )
            try:
                workflow_export = await self._workflow.export_formal_run_snapshot(
                    str(application_id),
                    assignment_id=str(assignment.assignment_id),
                    session_id=str(session_id),
                )
            except (OSError, TimeoutError) as error:
                raise FormalRunArchiveUnavailable(
                    "terminal workflow export is temporarily unavailable"
                ) from error
            except Exception as error:
                raise FormalRunArchiveError(
                    "terminal workflow export was rejected"
                ) from error
            draft = workflow_export.get("draft")
            if not isinstance(draft, Mapping):
                raise FormalRunArchiveError("terminal workflow draft projection is unavailable")
            snapshot = ApplicationSnapshot.model_validate(draft.get("snapshot"))
            try:
                collaboration_export = await self._collaboration_store.export_channel(
                    assignment.collaboration.channel_id
                )
            except CollaborationNotFound:
                collaboration_export = self._missing_collaboration_export(
                    assignment,
                    session_id,
                )
                collaboration_messages = []
                collaboration_records: list[ArchivedCollaborationRecord] = []
            except (OSError, TimeoutError) as error:
                raise FormalRunArchiveUnavailable(
                    "terminal collaboration export is temporarily unavailable"
                ) from error
            except Exception as error:
                raise FormalRunArchiveError(
                    "terminal collaboration export was rejected"
                ) from error
            else:
                try:
                    channel, collaboration_messages, reports = self._assert_channel(
                        assignment,
                        session_id,
                        collaboration_export,
                    )
                except (TypeError, ValueError) as error:
                    raise FormalRunArchiveError(
                        "terminal collaboration export changed its binding"
                    ) from error
                resolved_reports = [
                    report
                    for report in reports
                    if report.status in _RESOLVED_REPORT_STATUSES[report.category]
                ]
                collaboration_records = self._collaboration_records(
                    assignment=assignment,
                    channel=channel,
                    messages=collaboration_messages,
                    reports=resolved_reports,
                    binding=None,
                )
            if self._auth_store is None:
                raise FormalRunArchiveError(
                    "terminal forbidden-assistance scanner has no blackbox audit source"
                )
            try:
                blackbox_auth_export = await self._auth_store.export_assignment_snapshot(
                    assignment_id=assignment.assignment_id,
                    session_id=session_id,
                )
                artifact_inventory_export = (
                    await self._artifact_store.export_assignment_inventory(
                        assignment_id=assignment.assignment_id,
                        session_id=session_id,
                        application_id=application_id,
                    )
                )
            except (OSError, TimeoutError) as error:
                raise FormalRunArchiveUnavailable(
                    "terminal scanner store export is temporarily unavailable"
                ) from error
            except Exception as error:
                raise FormalRunArchiveError(
                    "terminal scanner store export was rejected"
                ) from error

            run_rows = workflow_export.get("runs")
            if not isinstance(run_rows, list):
                raise FormalRunArchiveError("terminal workflow run denominator is unavailable")
            try:
                self._assert_blackbox_credential_policy(
                    assignment=assignment,
                    session_id=session_id,
                    blackbox_auth_export=blackbox_auth_export,
                )
            except (TypeError, ValueError) as error:
                raise FormalRunArchiveError(
                    "terminal blackbox credential audit changed its binding"
                ) from error
            connector_budget_export = (
                await self._export_connector_budget(assignment)
                if run_rows
                else self._missing_connector_budget(
                    assignment_id=assignment.assignment_id,
                    allowed_network_hosts=assignment.constraints.allowed_hosts,
                    allowed_compensation_operations=(
                        assignment.constraints.compensation_actions
                    ),
                    max_write_count=(
                        assignment.constraints.max_write_count
                        if assignment.constraints.max_write_count is not None
                        else package.allowed_actions.max_write_count
                    ),
                    max_payload_bytes=(
                        assignment.constraints.max_payload_bytes
                        if assignment.constraints.max_payload_bytes is not None
                        else package.allowed_actions.max_payload_bytes
                    ),
                )
            )
            workflow_run_ids = [str(item.get("id") or "") for item in run_rows]
            if any(not value for value in workflow_run_ids) or len(workflow_run_ids) != len(
                set(workflow_run_ids)
            ):
                raise FormalRunArchiveError(
                    "terminal workflow run denominator has invalid identities"
                )
            inventory_records = artifact_inventory_export.get("records")
            if not isinstance(inventory_records, list):
                raise FormalRunArchiveError("terminal artifact inventory is invalid")
            inventory_run_ids = {
                str(item.get("run_id") or "")
                for item in inventory_records
                if isinstance(item, Mapping) and item.get("run_id")
            }
            all_evidence_run_ids = {*workflow_run_ids, *inventory_run_ids}
            artifact_ids: list[UUID] = []
            receipt_ids: list[UUID] = []
            try:
                for item in inventory_records:
                    if not isinstance(item, Mapping):
                        raise ValueError("artifact inventory record is not an object")
                    evidence_id = UUID(str(item["artifact_id"]))
                    if str(item.get("evidence_kind")) == EvidenceKind.artifact.value:
                        artifact_ids.append(evidence_id)
                    elif str(item.get("evidence_kind")) == EvidenceKind.host_receipt.value:
                        receipt_ids.append(evidence_id)
                    else:
                        raise ValueError("artifact inventory kind is unsupported")
                artifact_files, artifact_refs, artifact_index_entries = (
                    await self._evidence_files(
                        artifact_ids=artifact_ids,
                        kind=EvidenceKind.artifact,
                        assignment=assignment,
                        session_id=session_id,
                        allowed_run_ids=all_evidence_run_ids,
                    )
                )
                receipt_files, receipt_refs, receipt_index_entries = (
                    await self._evidence_files(
                        artifact_ids=receipt_ids,
                        kind=EvidenceKind.host_receipt,
                        assignment=assignment,
                        session_id=session_id,
                        allowed_run_ids=all_evidence_run_ids,
                    )
                )
                evidence_entries = [
                    *artifact_index_entries,
                    *receipt_index_entries,
                ]
                evidence_index = ArchivedEvidenceIndex(
                    schema_version="1.0",
                    task_id=task.task_id,
                    revision=task.revision,
                    run_id=task.run_id,
                    assignment_id=assignment.assignment_id,
                    application_id=application_id,
                    entry_count=len(evidence_entries),
                    entries=evidence_entries,
                )
            except (OSError, TimeoutError) as error:
                raise FormalRunArchiveUnavailable(
                    "terminal evidence bytes are temporarily unavailable"
                ) from error
            except Exception as error:
                raise FormalRunArchiveError(
                    "terminal evidence inventory was rejected"
                ) from error

            scan_created_at = datetime.fromisoformat(
                str(row["terminal_events_drained_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if self._source_provenance is None:
                raise FormalRunArchiveError(
                    "submitted terminal archive has no source provenance boundary"
                )
            try:
                expected_source_bindings = approved_developer_response_bindings(
                    collaboration_messages,
                    channel_id=assignment.collaboration.channel_id,
                )
                source_archive = await asyncio.to_thread(
                    self._source_provenance.finalize_archive,
                    assignment_id=assignment.assignment_id,
                    expected_bindings=expected_source_bindings,
                    finalized_at=scan_created_at,
                )
            except Exception as error:
                raise FormalRunArchiveError(
                    "submitted terminal source provenance was rejected"
                ) from error
            source_provenance_export = source_archive.manifest.model_dump(
                mode="json",
                exclude_none=True,
            )
            try:
                source_semantic_input = derive_source_semantic_input(
                    task_package=package,
                    source_manifest=source_archive.manifest,
                    source_files=source_archive.files,
                )
            except Exception as error:
                raise FormalRunArchiveError(
                    "terminal source semantic input could not be derived"
                ) from error
            source_semantic_export = source_semantic_input.model_dump(
                mode="json",
                exclude_none=True,
            )
            business_run_ids = list(
                dict.fromkeys(
                    [
                        *workflow_run_ids,
                        *sorted(inventory_run_ids),
                    ]
                )
            ) or [task.run_id]
            try:
                assistance_scan = scan_forbidden_assistance(
                    assignment=assignment,
                    session_id=session_id,
                    channel_id=assignment.collaboration.channel_id,
                    bridge_export=bridge_export,
                    collaboration_export=collaboration_export,
                    workflow_export=workflow_export,
                    blackbox_auth_export=blackbox_auth_export,
                    artifact_inventory_export=artifact_inventory_export,
                    source_provenance_export=source_provenance_export,
                    source_semantic_export=source_semantic_export,
                    source_semantic_task_package=package,
                    source_semantic_files=source_archive.files,
                    evidence_index=evidence_index,
                    business_run_ids=business_run_ids,
                    validation_mode=ValidationMode.real_host.value,
                    created_at=scan_created_at,
                )
            except Exception as error:
                raise FormalRunArchiveError(
                    "terminal forbidden-assistance scan rejected durable inputs"
                ) from error

            platform_records = self._platform_records(
                assignment=assignment,
                run_rows=run_rows,
                outcome=None,
            )
            failure_payload = {
                "phase": phase,
                "status": str(row.get("status") or ""),
                "last_error_code": row.get("last_error_code"),
            }
            platform_records.append(
                ArchivedPlatformEventRecord(
                    schema_version="1.0",
                    seq=len(platform_records) + 1,
                    event_id=_record_id("platform-terminal", assignment.assignment_id),
                    task_id=task.task_id,
                    revision=task.revision,
                    run_id=task.run_id,
                    assignment_id=assignment.assignment_id,
                    application_id=application_id,
                    kind="run.started",
                    payload=failure_payload,
                    payload_digest=_digest(failure_payload),
                )
            )
            result = ArchivedRunResult(
                schema_version="1.0",
                task_id=task.task_id,
                revision=task.revision,
                run_id=task.run_id,
                assignment_id=assignment.assignment_id,
                application_id=application_id,
                archive_status=status,
                validation_mode=ValidationMode.real_host,
                business_status={
                    ArchiveStatus.cancelled: "assignment_cancelled",
                    ArchiveStatus.environment_failed: "environment_failed",
                    ArchiveStatus.failed: "assignment_failed",
                }[status],
                business_run_ids=business_run_ids,
                artifact_digests=[item.digest for item in artifact_refs],
                host_receipt_digests=[item.digest for item in receipt_refs],
                remaining_limits=[str(row.get("last_error_code") or f"assignment_{phase}")],
                summary=(
                    str(row.get("last_error_message") or "")
                    or f"Formal assignment ended with {phase}."
                ),
            )
            files: dict[str, bytes | Path] = {
                "assignment.json": _canonical_json(assignment),
                "bridge-assignment.json": _canonical_json(
                    {
                        "assignment_id": str(assignment.assignment_id),
                        "application_id": str(application_id),
                        "build_id": str(row["build_id"]),
                        "session_id": str(session_id),
                        "phase": phase,
                        "status": str(row.get("status") or ""),
                        "daemon_status": row.get("daemon_status"),
                        "last_error_code": row.get("last_error_code"),
                        "last_error_message": row.get("last_error_message"),
                    }
                ),
                "draft.json": _canonical_json(
                    {
                        "revision": int(draft["revision"]),
                        "content_hash": _content_hash(draft["content_hash"]),
                        "snapshot": snapshot.model_dump(mode="json", exclude_none=True),
                    }
                ),
                "messages.jsonl": _jsonl(messages),
                "platform-events.jsonl": _jsonl(platform_records),
                "collaboration.jsonl": _jsonl(collaboration_records),
                "evidence-index.json": _canonical_json(evidence_index),
                "forbidden-assistance-scan.json": _canonical_json(assistance_scan),
                "scanner-inputs/bridge.json": _canonical_json(bridge_export),
                "scanner-inputs/collaboration.json": _canonical_json(
                    collaboration_export
                ),
                "scanner-inputs/workflow.json": _canonical_json(workflow_export),
                "scanner-inputs/blackbox-auth.json": _canonical_json(
                    blackbox_auth_export
                ),
                "connector-budget.json": _canonical_json(
                    connector_budget_export
                ),
                "scanner-inputs/connector-budget.json": _canonical_json(
                    connector_budget_export
                ),
                "scanner-inputs/artifact-inventory.json": _canonical_json(
                    artifact_inventory_export
                ),
                "scanner-inputs/source-semantic.json": _canonical_json(
                    source_semantic_input
                ),
                "source-provenance/manifest.json": _canonical_json(
                    source_provenance_export
                ),
                "result.json": _canonical_json(result),
                **source_archive.files,
                **artifact_files,
                **receipt_files,
            }

            try:
                latest_bridge = await self._bridge_store.export_assignment(
                    assignment.assignment_id
                )
                try:
                    latest_collaboration = await self._collaboration_store.export_channel(
                        assignment.collaboration.channel_id
                    )
                except CollaborationNotFound:
                    latest_collaboration = self._missing_collaboration_export(
                        assignment,
                        session_id,
                    )
                latest_workflow = await self._workflow.export_formal_run_snapshot(
                    str(application_id),
                    assignment_id=str(assignment.assignment_id),
                    session_id=str(session_id),
                )
                latest_auth = await self._auth_store.export_assignment_snapshot(
                    assignment_id=assignment.assignment_id,
                    session_id=session_id,
                )
                latest_connector_budget = (
                    await self._export_connector_budget(assignment)
                    if run_rows
                    else self._missing_connector_budget(
                        assignment_id=assignment.assignment_id,
                        allowed_network_hosts=(
                            assignment.constraints.allowed_hosts
                        ),
                        allowed_compensation_operations=(
                            assignment.constraints.compensation_actions
                        ),
                        max_write_count=(
                            assignment.constraints.max_write_count
                            if assignment.constraints.max_write_count
                            is not None
                            else package.allowed_actions.max_write_count
                        ),
                        max_payload_bytes=(
                            assignment.constraints.max_payload_bytes
                            if assignment.constraints.max_payload_bytes
                            is not None
                            else package.allowed_actions.max_payload_bytes
                        ),
                    )
                )
                latest_inventory = (
                    await self._artifact_store.export_assignment_inventory(
                        assignment_id=assignment.assignment_id,
                        session_id=session_id,
                        application_id=application_id,
                    )
                )
                latest_source_archive = await asyncio.to_thread(
                    self._source_provenance.finalize_archive,
                    assignment_id=assignment.assignment_id,
                    expected_bindings=expected_source_bindings,
                    finalized_at=scan_created_at,
                )
            except (OSError, TimeoutError) as error:
                raise FormalRunArchiveUnavailable(
                    "terminal durable stores cannot be rechecked"
                ) from error
            except Exception as error:
                raise FormalRunArchiveError(
                    "terminal durable store recheck was rejected"
                ) from error
            if any(
                not hmac.compare_digest(_digest(before), _digest(after))
                for before, after in (
                    (bridge_export, latest_bridge),
                    (collaboration_export, latest_collaboration),
                    (workflow_export, latest_workflow),
                    (blackbox_auth_export, latest_auth),
                    (connector_budget_export, latest_connector_budget),
                    (artifact_inventory_export, latest_inventory),
                )
            ) or (
                latest_source_archive.manifest != source_archive.manifest
                or dict(latest_source_archive.files)
                != dict(source_archive.files)
            ):
                raise FormalRunArchiveUnavailable(
                    "terminal durable stores changed during archive export"
                )
            preflight_files, _ = self._reserved_preflight_evidence(
                task_id=task.task_id,
                revision=task.revision,
                run_id=task.run_id,
                assignment_id=assignment.assignment_id,
                environment_instance_id=task.environment_instance_id,
            )
            files.update(preflight_files)
            ready_path = (
                self._task_state_root
                / "preflight"
                / task.task_id
                / str(task.revision)
                / task.run_id
                / "environment-ready.json"
            )
            workspace_manifest_path = (
                self._public_workspace_root
                / str(assignment.assignment_id)
                / WORKSPACE_MANIFEST_FILE
            )
            try:
                _, manifest, manifest_digest = await asyncio.to_thread(
                    self._manager.archive_run,
                    package,
                    run_id=task.run_id,
                    status=status,
                    validation_mode=ValidationMode.real_host,
                    environment_ready_path=ready_path,
                    workspace_manifest_path=workspace_manifest_path,
                    files=files,
                    claim_binding=None,
                    forbidden_assistance_findings=[
                        f"{item.rule_id}:{item.source_ref}"
                        for item in assistance_scan.findings
                    ],
                )
            except (OSError, TimeoutError) as error:
                raise FormalRunArchiveUnavailable(
                    "platform-owned terminal archive is temporarily unavailable"
                ) from error
            except Exception as error:
                raise FormalRunArchiveError(
                    "platform-owned terminal archive was rejected: "
                    f"{type(error).__name__}: {error}"
                ) from error
            return FormalTerminalArchiveResult(
                task_id=task.task_id,
                revision=task.revision,
                run_id=task.run_id,
                assignment_id=assignment.assignment_id,
                status=status,
                archive_manifest_digest=manifest_digest,
            )

    def replay(
        self,
        result: FormalRunArchivePreparationResult | FormalTerminalArchiveResult,
    ) -> RunArchiveManifest:
        package = self._manager.load_frozen(result.task_id, result.revision)
        return self._manager.replay_archive(
            package.root / "runs" / result.run_id,
            expected_manifest_digest=result.archive_manifest_digest,
        )
