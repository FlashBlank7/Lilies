from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .collaboration_models import (
    ApprovalDecision,
    CollaborationMessageEnvelope,
    DeveloperResponse,
)
from .formal_verification_contracts import ArchivedEvidenceIndex
from .formal_source_provenance import (
    FormalSourceProvenanceManifest,
    approved_developer_response_bindings,
)
from .lilies_models import BuildAssignment, Digest, OpaqueReference


SCANNER_VERSION = "t01f-generic-1"
InputKind = Literal[
    "bridge",
    "collaboration",
    "workflow",
    "blackbox_auth",
    "artifact_inventory",
    "evidence_index",
    "source_provenance",
    "source_semantic",
]
_GENERIC_POLICY = {
    "schema_version": "1.0",
    "scanner_version": SCANNER_VERSION,
    "rules": [
        "assignment_binding_complete",
        "real_host_only",
        "complete_attempt_denominator",
        "blackbox_tool_correlation",
        "assignment_start_draft_baseline",
        "append_only_draft_mutation_provenance",
        "meaningful_lilies_authored_draft_chain",
        "no_post_terminal_draft_mutation",
        "complete_business_evidence_inventory",
        "authorized_developer_response",
        "developer_source_object_provenance",
        "developer_source_semantic_provenance",
        "no_developer_authored_final_graph",
    ],
}
_SCANNER_IMPLEMENTATION_CONTRACT = {
    "schema_version": "1.0",
    "scanner_version": SCANNER_VERSION,
    "input_kinds": list(InputKind.__args__),  # type: ignore[attr-defined]
    "algorithm": [
        "validate_export_counts_and_watermarks",
        "bind_assignment_session_application",
        "require_real_host",
        "reconstruct_all_formal_workflow_attempts",
        "match_blackbox_requests_to_exact_tool_lifecycle",
        "reject_unknown_duplicate_or_unaccounted_daemon_tool_lifecycles",
        "reconstruct_assignment_baseline_and_every_draft_mutation",
        "reject_unattributed_noop_discontinuous_or_post_terminal_mutations",
        "bind_business_evidence_inventory",
        "verify_approved_developer_source_objects",
        "rederive_and_evaluate_changed_source_blob_semantics",
        "treat_binary_patch_as_display_only_and_git_blobs_as_authoritative",
    ],
}
_SCANNER_POLICIES = {
    SCANNER_VERSION: _GENERIC_POLICY,
}
_SCANNER_IMPLEMENTATION_CONTRACTS = {
    SCANNER_VERSION: _SCANNER_IMPLEMENTATION_CONTRACT,
}
_APPLICATION_SCOPED_OPERATIONS = {
    "platform_application_get",
    "platform_draft_inspect",
    "platform_draft_apply",
    "platform_tests_run",
    "platform_run_start",
    "platform_run_get",
    "platform_run_resume",
    "platform_run_cancel",
    "platform_trace_get",
    "platform_artifact_read",
    "platform_publish",
}
_BLACKBOX_OPERATIONS = {
    "platform_contract_get",
    "platform_block_search",
    "platform_block_get",
    "platform_tool_catalog",
    "platform_application_create",
    *_APPLICATION_SCOPED_OPERATIONS,
}
_NON_BLACKBOX_FORMAL_TOOLS = {
    "local_time",
    "workspace_list",
    "workspace_read",
    "workspace_write",
    "workspace_patch",
    "collaboration_report_submit",
    "collaboration_updates_read",
    "collaboration_verification_claim",
    "collaboration_formal_run_archive",
}
_FORMAL_TOOL_ALLOWLIST = _BLACKBOX_OPERATIONS | _NON_BLACKBOX_FORMAL_TOOLS


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


def _digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_json(value)
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def generic_policy_digest(
    scanner_version: str = SCANNER_VERSION,
) -> str:
    policy = _SCANNER_POLICIES.get(scanner_version)
    if policy is None:
        raise ValueError(f"unknown forbidden-assistance scanner: {scanner_version}")
    return _digest(policy)


def scanner_process_digest(
    scanner_version: str = SCANNER_VERSION,
) -> str:
    components = _SCANNER_EVALUATOR_COMPONENTS.get(scanner_version)
    if components is None:
        raise ValueError(f"unknown forbidden-assistance scanner: {scanner_version}")
    source_components = []
    for component in components:
        source = inspect.getsource(component).replace("\r\n", "\n")
        component_record: dict[str, Any] = {
            "qualified_name": f"{component.__module__}.{component.__qualname__}",
            "source_digest": _digest(source.encode("utf-8")),
        }
        if isinstance(component, type) and issubclass(component, BaseModel):
            component_record["model_schema"] = component.model_json_schema()
        source_components.append(component_record)
    return _digest(
        {
            "registry_schema_version": "1.0",
            "scanner_version": scanner_version,
            "implementation_contract": _SCANNER_IMPLEMENTATION_CONTRACTS[
                scanner_version
            ],
            "policy": _SCANNER_POLICIES[scanner_version],
            "runtime_contract": _SCANNER_RUNTIME_CONTRACTS[scanner_version],
            "source_components": source_components,
            "record_schema": ForbiddenAssistanceScanRecord.model_json_schema(),
        }
    )


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


def _safe_semantic_source_path(value: str) -> str:
    if (
        "\x00" in value
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ValueError("source semantic path is not normalized")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ValueError("source semantic path escapes its source root")
    return path.as_posix()


class SourceSemanticProjectPolicy(_FrozenModel):
    """Task-package source-project identity relevant to forbidden assistance."""

    name: str = Field(min_length=1, max_length=160)
    repository_url: str = Field(min_length=1, max_length=2_048)


class SourceSemanticPolicyBinding(_FrozenModel):
    """Canonical public task-package fields used to derive semantic markers."""

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    public_summary_digest: Digest
    source_projects: list[SourceSemanticProjectPolicy] = Field(
        min_length=1,
        max_length=20,
    )
    fixture_paths: list[str] = Field(max_length=10_000)
    fixture_identifiers: list[str] = Field(max_length=10_000)
    policy_digest: Digest

    @field_validator("fixture_paths")
    @classmethod
    def fixture_paths_are_safe(cls, value: list[str]) -> list[str]:
        normalized = [_safe_semantic_source_path(item) for item in value]
        if normalized != sorted(set(normalized)):
            raise ValueError("source semantic fixture paths must be sorted and unique")
        return normalized

    @field_validator("fixture_identifiers")
    @classmethod
    def fixture_identifiers_are_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value), key=str.casefold):
            raise ValueError(
                "source semantic fixture identifiers must be sorted and unique"
            )
        return value

    @model_validator(mode="after")
    def digest_matches(self) -> "SourceSemanticPolicyBinding":
        projects = [
            (item.name.casefold(), item.repository_url.casefold())
            for item in self.source_projects
        ]
        if projects != sorted(set(projects)):
            raise ValueError(
                "source semantic source projects must be sorted and unique"
            )
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"policy_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.policy_digest):
            raise ValueError("source semantic task policy digest changed")
        return self


class SourceSemanticArchiveFile(_FrozenModel):
    """One authoritative blob or display-only patch consumed by the evaluator."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["git_blob", "binary_patch"]
    archive_path: str = Field(min_length=1, max_length=4_096)
    payload_digest: Digest
    size_bytes: int = Field(ge=0, le=128 * 1024 * 1024)
    blob_oid: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40,64}$",
    )
    commit_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40,64}$",
    )
    authority: Literal[
        "authoritative_git_blob",
        "display_only_patch",
    ]

    @model_validator(mode="after")
    def role_matches_identity(self) -> "SourceSemanticArchiveFile":
        path = PurePosixPath(self.archive_path)
        if path.is_absolute() or any(
            part in {"", ".", ".."} for part in path.parts
        ):
            raise ValueError("source semantic archive file path is unsafe")
        normalized = path.as_posix()
        if self.kind == "git_blob":
            if (
                self.blob_oid is None
                or self.commit_sha is not None
                or self.authority != "authoritative_git_blob"
                or not normalized.startswith("source-provenance/objects/")
                or not normalized.endswith(f"{self.blob_oid}.blob")
            ):
                raise ValueError("source semantic blob binding is invalid")
        elif (
            self.commit_sha is None
            or self.blob_oid is not None
            or self.authority != "display_only_patch"
            or not normalized.startswith("source-provenance/patches/")
            or not normalized.endswith(f"-{self.commit_sha}.patch")
        ):
            raise ValueError("source semantic patch binding is invalid")
        return self


class SourceSemanticChange(_FrozenModel):
    """One exact Git path delta evaluated from its old/new blob endpoints."""

    schema_version: Literal["1.0"] = "1.0"
    commit_order: int = Field(ge=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    path: str = Field(min_length=1, max_length=4_096)
    change_kind: Literal["added", "deleted", "modified", "type_changed"]
    old_blob_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40,64}$",
    )
    new_blob_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40,64}$",
    )

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return _safe_semantic_source_path(value)

    @model_validator(mode="after")
    def endpoints_match_kind(self) -> "SourceSemanticChange":
        if self.change_kind == "added":
            valid = self.old_blob_sha is None and self.new_blob_sha is not None
        elif self.change_kind == "deleted":
            valid = self.old_blob_sha is not None and self.new_blob_sha is None
        else:
            valid = self.old_blob_sha is not None and self.new_blob_sha is not None
        if not valid:
            raise ValueError("source semantic change endpoints are invalid")
        return self


class SourceSemanticInput(_FrozenModel):
    """Archiveable, exactly reproducible input for raw source semantics."""

    schema_version: Literal["1.0"] = "1.0"
    complete: Literal[True] = True
    task_id: str = Field(min_length=3, max_length=160)
    task_revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    channel_id: UUID
    source_manifest_digest: Digest
    task_policy: SourceSemanticPolicyBinding
    patch_authority: Literal[
        "display_only_git_blob_tree_authoritative"
    ] = "display_only_git_blob_tree_authoritative"
    change_count: int = Field(ge=0, le=5_000)
    file_count: int = Field(ge=0, le=20_000)
    changes: list[SourceSemanticChange] = Field(max_length=5_000)
    files: list[SourceSemanticArchiveFile] = Field(max_length=20_000)
    input_digest: Digest

    @model_validator(mode="after")
    def exact_projection_and_digest_match(self) -> "SourceSemanticInput":
        if (
            self.task_id != self.task_policy.task_id
            or self.task_revision != self.task_policy.revision
            or self.change_count != len(self.changes)
            or self.file_count != len(self.files)
        ):
            raise ValueError("source semantic input changed its task or counts")
        change_keys = [
            (item.commit_order, item.path) for item in self.changes
        ]
        if change_keys != sorted(set(change_keys)):
            raise ValueError("source semantic changes must be sorted and unique")
        archive_paths = [item.archive_path for item in self.files]
        if archive_paths != sorted(set(archive_paths)):
            raise ValueError("source semantic files must be sorted and unique")
        blob_oids = {
            item.blob_oid
            for item in self.files
            if item.kind == "git_blob" and item.blob_oid is not None
        }
        expected_blob_oids = {
            oid
            for change in self.changes
            for oid in (change.old_blob_sha, change.new_blob_sha)
            if oid is not None
        }
        patch_commits = {
            item.commit_sha
            for item in self.files
            if item.kind == "binary_patch" and item.commit_sha is not None
        }
        expected_patch_commits = {item.commit_sha for item in self.changes}
        if (
            blob_oids != expected_blob_oids
            or patch_commits != expected_patch_commits
        ):
            raise ValueError(
                "source semantic files do not exactly cover Git endpoints"
            )
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"input_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.input_digest):
            raise ValueError("source semantic input digest changed")
        return self


class ForbiddenAssistanceInputBinding(_FrozenModel):
    kind: InputKind
    archive_path: str = Field(min_length=1, max_length=500)
    digest: Digest
    count: int = Field(ge=0)
    min_seq: int | None = Field(default=None, ge=1)
    max_seq: int | None = Field(default=None, ge=1)
    complete: bool

    @model_validator(mode="after")
    def sequence_bounds_match(self) -> "ForbiddenAssistanceInputBinding":
        if (self.min_seq is None) != (self.max_seq is None):
            raise ValueError("scanner input sequence bounds must be paired")
        if (
            self.min_seq is not None
            and self.max_seq is not None
            and self.min_seq > self.max_seq
        ):
            raise ValueError("scanner input sequence bounds are reversed")
        return self


class ForbiddenAssistanceFinding(_FrozenModel):
    rule_id: OpaqueReference
    outcome: Literal["violation", "inconclusive"]
    source_ref: str = Field(min_length=1, max_length=500)
    evidence_digest: Digest


class ForbiddenAssistanceScanRecord(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    scanner_version: str = Field(
        default=SCANNER_VERSION,
        min_length=3,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    scanner_process_digest: Digest
    task_id: str = Field(min_length=3, max_length=160)
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    session_id: UUID
    channel_id: UUID
    protected_policy_digest: Digest
    input_bindings: list[ForbiddenAssistanceInputBinding] = Field(
        min_length=len(InputKind.__args__),  # type: ignore[attr-defined]
        max_length=len(InputKind.__args__),  # type: ignore[attr-defined]
    )
    findings: list[ForbiddenAssistanceFinding] = Field(max_length=1_000)
    verdict: Literal["pass", "failed", "inconclusive"]
    scan_digest: Digest
    created_at: datetime

    @field_validator("scanner_version")
    @classmethod
    def scanner_version_is_registered(cls, value: str) -> str:
        if value not in _SCANNER_POLICIES:
            raise ValueError("forbidden-assistance scanner version is not registered")
        return value

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("scanner timestamp must use UTC")
        return value

    @field_validator("input_bindings")
    @classmethod
    def input_kinds_are_unique(
        cls,
        value: list[ForbiddenAssistanceInputBinding],
    ) -> list[ForbiddenAssistanceInputBinding]:
        kinds = [item.kind for item in value]
        expected = set(InputKind.__args__)  # type: ignore[attr-defined]
        if len(kinds) != len(set(kinds)) or set(kinds) != expected:
            raise ValueError("scanner input kinds must exactly cover the policy")
        return value

    @model_validator(mode="after")
    def verdict_matches_findings(self) -> "ForbiddenAssistanceScanRecord":
        inconclusive = any(
            item.outcome == "inconclusive" for item in self.findings
        )
        expected = (
            "inconclusive"
            if inconclusive
            else ("failed" if self.findings else "pass")
        )
        if self.verdict != expected:
            raise ValueError("scanner verdict does not match its findings")
        return self


def _strings_and_keys(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            result.append(str(key))
            result.extend(_strings_and_keys(item))
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in value:
            result.extend(_strings_and_keys(item))
    elif isinstance(value, str):
        result.append(value)
    return result


def _bridge_tool_lifecycles(
    bridge_export: Mapping[str, Any],
) -> tuple[
    dict[str, list[tuple[int, str, str, bool | None]]],
    list[Any],
]:
    found: dict[str, list[tuple[int, str, str, bool | None]]] = {}
    invalid: list[Any] = []
    for event in bridge_export.get("events", []):
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("event_type") or "")
        if event_type not in {"tool.started", "tool.completed", "tool.failed"}:
            continue
        encoded = event.get("data_json")
        if isinstance(encoded, Mapping):
            data = encoded
        else:
            try:
                data = json.loads(str(encoded or "{}"))
            except json.JSONDecodeError:
                invalid.append(event)
                continue
        if not isinstance(data, Mapping):
            invalid.append(event)
            continue
        tool_call_id = data.get("tool_call_id")
        tool = data.get("tool")
        seq = event.get("daemon_seq")
        if (
            not isinstance(tool_call_id, str)
            or not isinstance(tool, str)
            or not isinstance(seq, int)
        ):
            invalid.append(event)
            continue
        raw_is_error = data.get("is_error")
        is_error = raw_is_error if isinstance(raw_is_error, bool) else None
        found.setdefault(tool_call_id, []).append(
            (seq, event_type, tool, is_error)
        )
    for lifecycle in found.values():
        lifecycle.sort(key=lambda item: item[0])
    return found, invalid


def _input_binding(
    *,
    kind: InputKind,
    archive_path: str,
    value: Mapping[str, Any],
) -> ForbiddenAssistanceInputBinding:
    seqs: list[int] = []
    row_count = 0
    complete = value.get("complete") is True
    if kind == "bridge":
        rows = value.get("events", [])
        if isinstance(rows, list):
            seqs = [
                int(item["daemon_seq"])
                for item in rows
                if isinstance(item, Mapping)
                and isinstance(item.get("daemon_seq"), int)
            ]
            row_count = len(rows)
        counts = value.get("counts")
        watermark = value.get("watermark")
        assignment = value.get("assignment")
        relay_cursor = (
            int(watermark.get("relay_cursor", -1))
            if isinstance(watermark, Mapping)
            else -1
        )
        ack_cursor = (
            int(watermark.get("ack_cursor", -2))
            if isinstance(watermark, Mapping)
            else -2
        )
        complete = complete and (
            isinstance(rows, list)
            and len(seqs) == len(rows)
            and seqs == list(range(1, relay_cursor + 1))
            and isinstance(counts, Mapping)
            and counts.get("events") == len(rows)
            and isinstance(watermark, Mapping)
            and watermark.get("min_daemon_seq")
            == (seqs[0] if seqs else None)
            and watermark.get("max_daemon_seq")
            == (seqs[-1] if seqs else None)
            and ack_cursor == relay_cursor
            and isinstance(assignment, Mapping)
            and assignment.get("terminal_events_drained_at") is not None
        )
    elif kind == "collaboration":
        rows = value.get("audit", [])
        table_names = (
            "credentials",
            "messages",
            "reports",
            "report_revisions",
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
        counts = value.get("counts")
        tables_valid = isinstance(counts, Mapping)
        row_count = 0
        for name in table_names:
            table = value.get(name)
            tables_valid = (
                tables_valid
                and isinstance(table, list)
                and counts.get(name) == len(table)
            )
            if isinstance(table, list):
                row_count += len(table)
        messages = value.get("messages")
        message_seqs = [
            int(item["seq"])
            for item in messages
            if isinstance(item, Mapping) and isinstance(item.get("seq"), int)
        ] if isinstance(messages, list) else []
        watermark = value.get("watermark")
        channel = value.get("channel")
        next_seq = (
            int(watermark.get("next_seq", -1))
            if isinstance(watermark, Mapping)
            else -1
        )
        complete = complete and bool(
            tables_valid
            and isinstance(messages, list)
            and len(message_seqs) == len(messages)
            and message_seqs == list(range(1, next_seq))
            and isinstance(channel, Mapping)
            and int(channel.get("next_seq", -2)) == next_seq
            and isinstance(watermark, Mapping)
            and watermark.get("min_message_seq")
            == (message_seqs[0] if message_seqs else None)
            and watermark.get("max_message_seq")
            == (message_seqs[-1] if message_seqs else None)
        )
    elif kind == "workflow":
        rows = value.get("runs", [])
        provenance = value.get("formal_draft_provenance")
        baselines = (
            provenance.get("baselines", [])
            if isinstance(provenance, Mapping)
            else []
        )
        mutations = (
            provenance.get("mutations", [])
            if isinstance(provenance, Mapping)
            else []
        )
        counts = value.get("counts")
        run_event_counts = value.get("run_event_counts")
        actual_run_events = 0
        run_events_valid = isinstance(run_event_counts, Mapping)
        if isinstance(rows, list):
            row_count = len(rows)
            for run in rows:
                events = run.get("events") if isinstance(run, Mapping) else None
                run_id = str(run.get("id")) if isinstance(run, Mapping) else ""
                if not isinstance(events, list):
                    run_events_valid = False
                    continue
                event_seqs = [
                    int(event["seq"])
                    for event in events
                    if isinstance(event, Mapping)
                    and isinstance(event.get("seq"), int)
                ]
                run_events_valid = (
                    run_events_valid
                    and len(event_seqs) == len(events)
                    and (
                        not event_seqs
                        or event_seqs == list(range(1, event_seqs[-1] + 1))
                    )
                    and run_event_counts.get(run_id) == len(events)
                )
                actual_run_events += len(events)
        complete = complete and bool(
            isinstance(rows, list)
            and isinstance(baselines, list)
            and isinstance(mutations, list)
            and isinstance(counts, Mapping)
            and counts.get("runs") == len(rows)
            and counts.get("run_events") == actual_run_events
            and counts.get("formal_draft_baselines") == len(baselines)
            and counts.get("formal_draft_mutations") == len(mutations)
            and run_events_valid
        )
    elif kind == "blackbox_auth":
        rows = value.get("audit", [])
        audit_seqs = [
            int(item["seq"])
            for item in rows
            if isinstance(item, Mapping) and isinstance(item.get("seq"), int)
        ]
        seqs = audit_seqs
        security_rows = value.get("security_events", [])
        security_seqs = [
            int(item["seq"])
            for item in security_rows
            if isinstance(item, Mapping) and isinstance(item.get("seq"), int)
        ] if isinstance(security_rows, list) else []
        table_names = (
            "credentials",
            "credential_applications",
            "requests",
            "audit",
            "security_events",
        )
        counts = value.get("counts")
        tables_valid = isinstance(counts, Mapping)
        row_count = 0
        for name in table_names:
            table = value.get(name)
            tables_valid = (
                tables_valid
                and isinstance(table, list)
                and counts.get(name) == len(table)
            )
            if isinstance(table, list):
                row_count += len(table)
        complete = complete and bool(
            tables_valid
            and isinstance(rows, list)
            and len(audit_seqs) == len(rows)
            and audit_seqs == sorted(set(audit_seqs))
            and isinstance(security_rows, list)
            and len(security_seqs) == len(security_rows)
            and security_seqs == sorted(set(security_seqs))
            and value.get("audit_min_seq")
            == (audit_seqs[0] if audit_seqs else None)
            and value.get("audit_max_seq")
            == (audit_seqs[-1] if audit_seqs else None)
            and value.get("security_min_seq")
            == (security_seqs[0] if security_seqs else None)
            and value.get("security_max_seq")
            == (security_seqs[-1] if security_seqs else None)
        )
    elif kind == "artifact_inventory":
        rows = value.get("records", [])
        row_count = len(rows) if isinstance(rows, list) else 0
        complete = complete and (
            isinstance(rows, list) and value.get("count") == len(rows)
        )
    elif kind == "evidence_index":
        rows = value.get("entries", [])
        row_count = len(rows) if isinstance(rows, list) else 0
        complete = complete and (
            isinstance(rows, list) and value.get("entry_count") == len(rows)
        )
    elif kind == "source_provenance":
        rows = value.get("approved_commits", [])
        row_count = len(rows) if isinstance(rows, list) else 0
        manifest_digest = value.get("manifest_digest")
        payload = dict(value)
        payload.pop("manifest_digest", None)
        complete = (
            isinstance(rows, list)
            and isinstance(manifest_digest, str)
            and hmac.compare_digest(_digest(payload), manifest_digest)
        )
    else:
        rows = value.get("changes", [])
        row_count = len(rows) if isinstance(rows, list) else 0
        try:
            semantic_input = SourceSemanticInput.model_validate(value)
        except (TypeError, ValueError):
            complete = False
        else:
            complete = bool(
                semantic_input.complete
                and semantic_input.change_count == len(semantic_input.changes)
                and semantic_input.file_count == len(semantic_input.files)
            )
    if not isinstance(rows, list):
        complete = False
    return ForbiddenAssistanceInputBinding(
        kind=kind,
        archive_path=archive_path,
        digest=_digest(value),
        count=row_count,
        min_seq=min(seqs) if seqs else None,
        max_seq=max(seqs) if seqs else None,
        complete=complete,
    )


def _finding(
    rule_id: str,
    outcome: Literal["violation", "inconclusive"],
    source_ref: str,
    evidence: Any,
) -> ForbiddenAssistanceFinding:
    return ForbiddenAssistanceFinding(
        rule_id=rule_id,
        outcome=outcome,
        source_ref=source_ref,
        evidence_digest=_digest(evidence),
    )


def _as_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return value
    raise ValueError(f"{label} must be a mapping or Pydantic model")


def _fixture_identifier_tokens(value: Any) -> set[str]:
    identifiers: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _semantic_normalize(str(key))
            if normalized:
                identifiers.add(normalized)
            identifiers.update(_fixture_identifier_tokens(item))
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in value:
            identifiers.update(_fixture_identifier_tokens(item))
    return identifiers


def _frozen_fixture_identifiers(
    package_root: Path,
    fixtures: Mapping[str, Any],
) -> list[str]:
    """Extract only public field names from the manager-frozen fixture bytes."""

    fixtures_root = (package_root / "fixtures").resolve()
    identifiers: set[str] = set()
    consumed = 0
    for raw_entry in fixtures.get("files", []):
        entry = _as_mapping(
            raw_entry,
            label="source semantic fixture file",
        )
        relative = _safe_semantic_source_path(str(entry.get("path") or ""))
        candidate = fixtures_root.joinpath(*PurePosixPath(relative).parts)
        resolved = candidate.resolve()
        if (
            not resolved.is_relative_to(fixtures_root)
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            raise ValueError("source semantic fixture file escaped its frozen root")
        size = candidate.stat(follow_symlinks=False).st_size
        consumed += size
        if size > 2 * 1024 * 1024 or consumed > 16 * 1024 * 1024:
            continue
        payload = candidate.read_bytes()
        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            identifiers.update(_fixture_identifier_tokens(json.loads(decoded)))
        except (json.JSONDecodeError, ValueError):
            first_line = decoded.splitlines()[0] if decoded.splitlines() else ""
            for token in re.split(r"[,;\t]", first_line):
                normalized = _semantic_normalize(token)
                if normalized and re.fullmatch(
                    r"[a-z_][a-z0-9_]{1,127}",
                    normalized,
                ):
                    identifiers.add(normalized)
    return sorted(identifiers, key=str.casefold)


def _source_semantic_policy(
    task_package: Any,
) -> SourceSemanticPolicyBinding:
    """Project the frozen public task policy without importing task_packages.

    ``task_packages`` imports this module, so this deliberately accepts either
    a mapping or a FrozenTaskPackage-like object and uses structural access.
    """

    if isinstance(task_package, (BaseModel, Mapping)):
        root = _as_mapping(task_package, label="source semantic task package")
    else:
        task_value = getattr(task_package, "task", None)
        fixtures_value = getattr(task_package, "fixtures", None)
        record_value = getattr(task_package, "record", None)
        fixture_mapping = _as_mapping(
            fixtures_value,
            label="frozen fixture manifest",
        )
        package_root = Path(getattr(task_package, "root"))
        root = {
            "task": _as_mapping(task_value, label="frozen task spec"),
            "fixtures": fixture_mapping,
            "fixture_identifiers": _frozen_fixture_identifiers(
                package_root,
                fixture_mapping,
            ),
            "public_summary_digest": getattr(
                record_value,
                "public_summary_digest",
                None,
            ),
        }
    task_value = root.get("task", root)
    task = _as_mapping(task_value, label="source semantic task spec")
    fixtures_value = root.get("fixtures", {})
    fixtures = (
        _as_mapping(fixtures_value, label="source semantic fixtures")
        if fixtures_value
        else {}
    )
    raw_projects = task.get("source_projects")
    if not isinstance(raw_projects, Sequence) or isinstance(
        raw_projects,
        (str, bytes, bytearray),
    ):
        raise ValueError("source semantic task policy has no source projects")
    projects: list[SourceSemanticProjectPolicy] = []
    for raw_project in raw_projects:
        project = _as_mapping(
            raw_project,
            label="source semantic source project",
        )
        projects.append(
            SourceSemanticProjectPolicy(
                name=str(project.get("name") or ""),
                repository_url=str(project.get("repository_url") or ""),
            )
        )
    projects.sort(
        key=lambda item: (
            item.name.casefold(),
            item.repository_url.casefold(),
        )
    )

    raw_fixture_paths = root.get("fixture_paths")
    if raw_fixture_paths is None:
        raw_fixture_files = fixtures.get("files", [])
        raw_fixture_paths = [
            str(
                _as_mapping(item, label="source semantic fixture file").get(
                    "path"
                )
                or ""
            )
            for item in raw_fixture_files
        ]
    if not isinstance(raw_fixture_paths, Sequence) or isinstance(
        raw_fixture_paths,
        (str, bytes, bytearray),
    ):
        raise ValueError("source semantic fixture paths are invalid")
    fixture_paths = sorted(
        {
            _safe_semantic_source_path(str(path))
            for path in raw_fixture_paths
        }
    )
    raw_fixture_identifiers = root.get("fixture_identifiers", [])
    if not isinstance(raw_fixture_identifiers, Sequence) or isinstance(
        raw_fixture_identifiers,
        (str, bytes, bytearray),
    ):
        raise ValueError("source semantic fixture identifiers are invalid")
    fixture_identifiers = sorted(
        {
            str(value).strip().casefold()
            for value in raw_fixture_identifiers
            if str(value).strip()
        },
        key=str.casefold,
    )
    public_summary_digest = str(
        root.get("public_summary_digest")
        or task.get("public_summary_digest")
        or _digest(task)
    )
    payload = {
        "schema_version": "1.0",
        "task_id": str(task.get("task_id") or ""),
        "revision": int(task.get("revision", 0)),
        "public_summary_digest": public_summary_digest,
        "source_projects": [
            project.model_dump(mode="json") for project in projects
        ],
        "fixture_paths": fixture_paths,
        "fixture_identifiers": fixture_identifiers,
    }
    return SourceSemanticPolicyBinding(
        **payload,
        policy_digest=_digest(payload),
    )


def _source_semantic_file(
    *,
    kind: Literal["git_blob", "binary_patch"],
    archive_path: str,
    payload_digest: str,
    size_bytes: int,
    source_files: Mapping[str, bytes],
    blob_oid: str | None = None,
    commit_sha: str | None = None,
) -> SourceSemanticArchiveFile:
    payload = source_files.get(archive_path)
    if (
        not isinstance(payload, bytes)
        or len(payload) != size_bytes
        or not hmac.compare_digest(_digest(payload), payload_digest)
    ):
        raise ValueError(
            f"source semantic raw file differs from provenance: {archive_path}"
        )
    return SourceSemanticArchiveFile(
        kind=kind,
        archive_path=archive_path,
        payload_digest=payload_digest,
        size_bytes=size_bytes,
        blob_oid=blob_oid,
        commit_sha=commit_sha,
        authority=(
            "authoritative_git_blob"
            if kind == "git_blob"
            else "display_only_patch"
        ),
    )


def derive_source_semantic_input(
    *,
    task_package: Any,
    source_manifest: FormalSourceProvenanceManifest | Mapping[str, Any],
    source_files: Mapping[str, bytes],
) -> SourceSemanticInput:
    """Derive the canonical semantic input from frozen policy and raw Git bytes.

    Patches are bound for archive integrity but never treated as semantic
    authority. Old/new Git blob endpoints, already tied to the archived trees by
    the source-provenance verifier, are the only content authority.
    """

    manifest = (
        source_manifest
        if isinstance(source_manifest, FormalSourceProvenanceManifest)
        else FormalSourceProvenanceManifest.model_validate(source_manifest)
    )
    policy = _source_semantic_policy(task_package)
    if (
        manifest.task_id != policy.task_id
        or manifest.task_revision != policy.revision
    ):
        raise ValueError("source semantic policy belongs to another task revision")

    changes: list[SourceSemanticChange] = []
    files_by_path: dict[str, SourceSemanticArchiveFile] = {}
    for commit in manifest.approved_commits:
        descriptors = {item.oid: item for item in commit.blob_objects}
        for change in commit.changes:
            changes.append(
                SourceSemanticChange(
                    commit_order=commit.order,
                    commit_sha=commit.commit_sha,
                    path=change.path,
                    change_kind=change.change_kind,
                    old_blob_sha=change.old_blob_sha,
                    new_blob_sha=change.new_blob_sha,
                )
            )
            for oid in (change.old_blob_sha, change.new_blob_sha):
                if oid is None:
                    continue
                descriptor = descriptors.get(oid)
                if descriptor is None:
                    raise ValueError(
                        "source semantic change omits a Git blob descriptor"
                    )
                bound = _source_semantic_file(
                    kind="git_blob",
                    archive_path=descriptor.archive_path,
                    payload_digest=descriptor.payload_digest,
                    size_bytes=descriptor.size_bytes,
                    blob_oid=descriptor.oid,
                    source_files=source_files,
                )
                prior = files_by_path.get(bound.archive_path)
                if prior is not None and prior != bound:
                    raise ValueError(
                        "source semantic raw blob identity has conflicting evidence"
                    )
                files_by_path[bound.archive_path] = bound
        patch = commit.binary_diff
        patch_bound = _source_semantic_file(
            kind="binary_patch",
            archive_path=patch.archive_path,
            payload_digest=patch.payload_digest,
            size_bytes=patch.size_bytes,
            commit_sha=commit.commit_sha,
            source_files=source_files,
        )
        prior_patch = files_by_path.get(patch_bound.archive_path)
        if prior_patch is not None and prior_patch != patch_bound:
            raise ValueError(
                "source semantic patch identity has conflicting evidence"
            )
        files_by_path[patch_bound.archive_path] = patch_bound

    changes.sort(key=lambda item: (item.commit_order, item.path))
    files = [files_by_path[path] for path in sorted(files_by_path)]
    payload = {
        "schema_version": "1.0",
        "complete": True,
        "task_id": manifest.task_id,
        "task_revision": manifest.task_revision,
        "run_id": manifest.run_id,
        "assignment_id": manifest.assignment_id,
        "channel_id": manifest.channel_id,
        "source_manifest_digest": manifest.manifest_digest,
        "task_policy": policy.model_dump(mode="json"),
        "patch_authority": "display_only_git_blob_tree_authoritative",
        "change_count": len(changes),
        "file_count": len(files),
        "changes": [
            item.model_dump(mode="json", exclude_none=True)
            for item in changes
        ],
        "files": [
            item.model_dump(mode="json", exclude_none=True)
            for item in files
        ],
    }
    return SourceSemanticInput(
        **payload,
        input_digest=_digest(payload),
    )


_SEMANTIC_MARKER_STOPLIST = frozenset(
    {
        "api",
        "backend",
        "client",
        "data",
        "fixture",
        "fixtures",
        "frontend",
        "input",
        "platform",
        "project",
        "public",
        "public_inputs",
        "server",
        "source",
        "test",
        "tests",
    }
)
_SOURCE_IMPLEMENTATION_MARKERS = frozenset(
    {
        "adapter",
        "adapters",
        "field_map",
        "field_mapper",
        "field_mapping",
        "manifest",
        "mapper",
        "mapping",
        "mappings",
        "schema",
        "schemas",
    }
)
_TEXT_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".conf",
        ".cpp",
        ".css",
        ".csv",
        ".go",
        ".graphql",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".md",
        ".mjs",
        ".py",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


def _semantic_normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


def _repository_stem(repository_url: str) -> str:
    parsed = urlsplit(repository_url)
    path = parsed.path
    if not path and ":" in repository_url:
        path = repository_url.rsplit(":", 1)[-1]
    stem = PurePosixPath(path.rstrip("/")).name
    return stem[:-4] if stem.casefold().endswith(".git") else stem


def _semantic_markers(
    policy: SourceSemanticPolicyBinding,
) -> dict[str, set[str]]:
    task_markers = {_semantic_normalize(policy.task_id)}
    project_markers: set[str] = set()
    for project in policy.source_projects:
        for raw in (project.name, _repository_stem(project.repository_url)):
            normalized = _semantic_normalize(raw)
            if normalized:
                project_markers.add(normalized)
            project_markers.update(
                token
                for token in normalized.split("_")
                if len(token) >= 5
                and token not in _SEMANTIC_MARKER_STOPLIST
            )
    fixture_markers = {
        _semantic_normalize(value)
        for value in policy.fixture_identifiers
        if _semantic_normalize(value)
    }
    for path in policy.fixture_paths:
        name = PurePosixPath(path).name
        stem = PurePosixPath(name).stem
        for raw in (name, stem):
            normalized = _semantic_normalize(raw)
            if len(normalized.replace("_", "")) >= 6:
                fixture_markers.add(normalized)
    return {
        "task_id": {
            value for value in task_markers if value
        },
        "source_project": {
            value
            for value in project_markers
            if value not in _SEMANTIC_MARKER_STOPLIST
        },
        "fixture": {
            value
            for value in fixture_markers
            if value not in _SEMANTIC_MARKER_STOPLIST
        },
    }


def _contains_semantic_marker(normalized: str, marker: str) -> bool:
    if not marker:
        return False
    bounded = f"_{normalized}_"
    if f"_{marker}_" in bounded:
        return True
    compact_marker = marker.replace("_", "")
    compact_value = normalized.replace("_", "")
    return len(compact_marker) >= 6 and compact_marker in compact_value


def _added_source_text(old: str, new: str) -> str:
    matcher = SequenceMatcher(
        a=old.splitlines(),
        b=new.splitlines(),
        autojunk=False,
    )
    additions: list[str] = []
    new_lines = new.splitlines()
    for tag, _old_start, _old_end, new_start, new_end in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            additions.extend(new_lines[new_start:new_end])
    return "\n".join(additions)


def _explicit_final_graph(text: str) -> bool:
    folded = unicodedata.normalize("NFKC", text).casefold()
    normalized = _semantic_normalize(folded)
    if any(
        _contains_semantic_marker(normalized, marker)
        for marker in ("final_graph", "final_workflow", "prebuilt_workflow")
    ):
        return True
    quoted_nodes = re.search(r"""[\"']nodes[\"']\s*:""", folded)
    quoted_edges = re.search(r"""[\"']edges[\"']\s*:""", folded)
    if quoted_nodes and quoted_edges:
        return True
    workflow_spec = re.search(r"\bworkflow_?spec\s*\(", folded)
    assigned_nodes = re.search(r"\bnodes\s*=", folded)
    assigned_edges = re.search(r"\bedges\s*=", folded)
    return bool(workflow_spec and assigned_nodes and assigned_edges)


def _looks_like_fixture_field_mapping(
    added_text: str,
    fixture_markers: set[str],
) -> bool:
    if len(fixture_markers) < 2:
        return False
    folded = unicodedata.normalize("NFKC", added_text).casefold()
    key_assignments = re.findall(
        r"""[\"']([a-z_][a-z0-9_-]*)[\"']\s*:""",
        folded,
    )
    indexed_reads = re.findall(
        r"""\[\s*[\"']([a-z_][a-z0-9_-]*)[\"']\s*\]""",
        folded,
    )
    assigned_identifiers = {
        _semantic_normalize(item)
        for item in (*key_assignments, *indexed_reads)
    }
    return len(assigned_identifiers & fixture_markers) >= 2 and bool(
        key_assignments
    )


def _evaluate_derived_source_semantics(
    source_input: SourceSemanticInput,
    source_files: Mapping[str, bytes],
) -> list[ForbiddenAssistanceFinding]:
    findings: list[ForbiddenAssistanceFinding] = []
    markers = _semantic_markers(source_input.task_policy)
    blobs = {
        item.blob_oid: source_files[item.archive_path]
        for item in source_input.files
        if item.kind == "git_blob" and item.blob_oid is not None
    }
    for change in source_input.changes:
        if change.new_blob_sha is None:
            continue
        source_ref = (
            f"source-change:{change.commit_order}:{change.path}"
        )
        new_payload = blobs[change.new_blob_sha]
        old_payload = (
            blobs[change.old_blob_sha]
            if change.old_blob_sha is not None
            else b""
        )
        try:
            new_text = new_payload.decode("utf-8")
            old_text = old_payload.decode("utf-8") if old_payload else ""
        except UnicodeDecodeError:
            path_text = _semantic_normalize(change.path)
            marker_categories = {
                category
                for category, values in markers.items()
                if any(
                    _contains_semantic_marker(path_text, marker)
                    for marker in values
                )
            }
            implementation = {
                marker
                for marker in _SOURCE_IMPLEMENTATION_MARKERS
                if _contains_semantic_marker(path_text, marker)
            }
            if marker_categories and implementation:
                findings.append(
                    _finding(
                        "developer_task_specific_source_assistance",
                        "violation",
                        source_ref,
                        {
                            "path": change.path,
                            "commit_sha": change.commit_sha,
                            "marker_categories": sorted(marker_categories),
                            "implementation_markers": sorted(implementation),
                            "new_blob_sha": change.new_blob_sha,
                        },
                    )
                )
            elif PurePosixPath(change.path).suffix.casefold() in _TEXT_SOURCE_SUFFIXES:
                findings.append(
                    _finding(
                        "developer_source_semantic_text_unreadable",
                        "inconclusive",
                        source_ref,
                        {
                            "path": change.path,
                            "commit_sha": change.commit_sha,
                            "new_blob_sha": change.new_blob_sha,
                        },
                    )
                )
            continue

        added = _added_source_text(old_text, new_text)
        semantic_surface = _semantic_normalize(
            f"{change.path}\n{added}"
        )
        marker_categories = {
            category
            for category, values in markers.items()
            if any(
                _contains_semantic_marker(semantic_surface, marker)
                for marker in values
            )
        }
        implementation = {
            marker
            for marker in _SOURCE_IMPLEMENTATION_MARKERS
            if _contains_semantic_marker(semantic_surface, marker)
        }
        fixture_markers = {
            marker
            for marker in markers["fixture"]
            if _contains_semantic_marker(semantic_surface, marker)
        }
        task_specific_source = bool(
            marker_categories & {"task_id", "source_project"}
        )
        structural_field_mapping = _looks_like_fixture_field_mapping(
            added,
            fixture_markers,
        )
        if (
            task_specific_source
            or (marker_categories and implementation)
            or structural_field_mapping
        ):
            findings.append(
                _finding(
                    "developer_task_specific_source_assistance",
                    "violation",
                    source_ref,
                    {
                        "path": change.path,
                        "commit_sha": change.commit_sha,
                        "marker_categories": sorted(marker_categories),
                        "implementation_markers": sorted(implementation),
                        "fixture_markers": sorted(fixture_markers),
                        "structural_field_mapping": structural_field_mapping,
                        "old_blob_sha": change.old_blob_sha,
                        "new_blob_sha": change.new_blob_sha,
                    },
                )
            )
        if _explicit_final_graph(new_text) and not _explicit_final_graph(old_text):
            findings.append(
                _finding(
                    "developer_authored_final_workflow_source",
                    "violation",
                    source_ref,
                    {
                        "path": change.path,
                        "commit_sha": change.commit_sha,
                        "old_blob_sha": change.old_blob_sha,
                        "new_blob_sha": change.new_blob_sha,
                    },
                )
            )
    return findings


def evaluate_source_semantic_input(
    *,
    task_package: Any,
    source_manifest: FormalSourceProvenanceManifest | Mapping[str, Any],
    source_files: Mapping[str, bytes],
    archived_input: SourceSemanticInput | Mapping[str, Any] | None = None,
) -> list[ForbiddenAssistanceFinding]:
    """Re-derive, exact-compare, then evaluate one online or replayed input."""

    try:
        derived = derive_source_semantic_input(
            task_package=task_package,
            source_manifest=source_manifest,
            source_files=source_files,
        )
        if archived_input is not None:
            persisted = (
                archived_input
                if isinstance(archived_input, SourceSemanticInput)
                else SourceSemanticInput.model_validate(archived_input)
            )
            if not hmac.compare_digest(
                _canonical_json(persisted),
                _canonical_json(derived),
            ):
                raise ValueError(
                    "archived source semantic input differs from re-derivation"
                )
    except Exception as error:
        return [
            _finding(
                "developer_source_semantic_input_unreplayable",
                "inconclusive",
                "scanner-inputs/source-semantic.json",
                {"error_type": type(error).__name__},
            )
        ]
    return _evaluate_derived_source_semantics(derived, source_files)


def _response_revision_and_hash(
    request: Mapping[str, Any],
) -> tuple[int | None, str | None]:
    response = request.get("response")
    if not isinstance(response, Mapping):
        return None, None
    candidates = [response]
    for key in ("draft", "result", "data"):
        nested = response.get(key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    for value in candidates:
        revision = value.get("revision")
        content_hash = value.get("content_hash")
        if isinstance(revision, int) and isinstance(content_hash, str):
            normalized = (
                content_hash
                if content_hash.startswith("sha256:")
                else f"sha256:{content_hash}"
            )
            return revision, normalized
    return None, None


def _normalized_digest(value: Any) -> str:
    raw = str(value or "")
    return raw if raw.startswith("sha256:") else f"sha256:{raw}"


def _utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed


def _scan_t01f_generic_1(
    *,
    assignment: BuildAssignment,
    session_id: UUID,
    channel_id: UUID,
    bridge_export: Mapping[str, Any],
    collaboration_export: Mapping[str, Any],
    workflow_export: Mapping[str, Any],
    blackbox_auth_export: Mapping[str, Any],
    artifact_inventory_export: Mapping[str, Any],
    source_provenance_export: Mapping[str, Any],
    source_semantic_export: Mapping[str, Any] | None = None,
    source_semantic_task_package: Any | None = None,
    source_semantic_files: Mapping[str, bytes] | None = None,
    evidence_index: ArchivedEvidenceIndex,
    business_run_ids: Sequence[str],
    validation_mode: str,
    created_at: datetime,
) -> ForbiddenAssistanceScanRecord:
    """Reconstruct generic authorship and denominator rules from durable stores."""

    task = assignment.task_package
    if task is None or assignment.collaboration is None:
        raise ValueError("forbidden-assistance scan requires a formal assignment")
    semantic_export: Mapping[str, Any] = (
        source_semantic_export
        if source_semantic_export is not None
        else {
            "schema_version": "1.0",
            "complete": False,
            "missing_reason": "source_semantic_input_not_archived",
        }
    )
    raw_inputs: dict[str, Mapping[str, Any]] = {
        "bridge": bridge_export,
        "collaboration": collaboration_export,
        "workflow": workflow_export,
        "blackbox_auth": blackbox_auth_export,
        "artifact_inventory": artifact_inventory_export,
        "source_provenance": source_provenance_export,
        "source_semantic": semantic_export,
        "evidence_index": evidence_index.model_dump(
            mode="json",
            exclude_none=True,
        ),
    }
    paths = {
        "bridge": "scanner-inputs/bridge.json",
        "collaboration": "scanner-inputs/collaboration.json",
        "workflow": "scanner-inputs/workflow.json",
        "blackbox_auth": "scanner-inputs/blackbox-auth.json",
        "artifact_inventory": "scanner-inputs/artifact-inventory.json",
        "source_provenance": "source-provenance/manifest.json",
        "source_semantic": "scanner-inputs/source-semantic.json",
        "evidence_index": "evidence-index.json",
    }
    bindings = [
        _input_binding(
            kind=kind,  # type: ignore[arg-type]
            archive_path=paths[kind],
            value=value,
        )
        for kind, value in raw_inputs.items()
    ]
    findings: list[ForbiddenAssistanceFinding] = []

    identity = {
        "assignment_id": str(assignment.assignment_id),
        "session_id": str(session_id),
        "application_id": str(
            assignment.target.application_id
            or evidence_index.application_id
        ),
    }
    bridge_row = bridge_export.get("assignment")
    channel = collaboration_export.get("channel")
    binding_checks = {
        "bridge_assignment": (
            isinstance(bridge_row, Mapping)
            and str(bridge_row.get("assignment_id"))
            == identity["assignment_id"]
            and str(bridge_row.get("session_id")) == identity["session_id"]
        ),
        "collaboration_channel": (
            isinstance(channel, Mapping)
            and str(channel.get("assignment_id"))
            == identity["assignment_id"]
            and str(channel.get("lilies_session_id"))
            == identity["session_id"]
            and str(channel.get("channel_id")) == str(channel_id)
        ),
        "blackbox_auth": (
            str(blackbox_auth_export.get("assignment_id"))
            == identity["assignment_id"]
            and str(blackbox_auth_export.get("session_id"))
            == identity["session_id"]
        ),
        "artifact_inventory": (
            str(artifact_inventory_export.get("assignment_id"))
            == identity["assignment_id"]
            and str(artifact_inventory_export.get("session_id"))
            == identity["session_id"]
            and str(artifact_inventory_export.get("application_id"))
            == identity["application_id"]
        ),
        "source_provenance": (
            str(source_provenance_export.get("assignment_id"))
            == identity["assignment_id"]
            and str(source_provenance_export.get("channel_id"))
            == str(channel_id)
            and str(source_provenance_export.get("task_id")) == task.task_id
            and int(source_provenance_export.get("task_revision", -1))
            == task.revision
            and str(source_provenance_export.get("run_id")) == task.run_id
        ),
        "source_semantic": (
            str(semantic_export.get("assignment_id"))
            == identity["assignment_id"]
            and str(semantic_export.get("channel_id")) == str(channel_id)
            and str(semantic_export.get("task_id")) == task.task_id
            and int(semantic_export.get("task_revision", -1))
            == task.revision
            and str(semantic_export.get("run_id")) == task.run_id
            and str(semantic_export.get("source_manifest_digest"))
            == str(source_provenance_export.get("manifest_digest"))
            and isinstance(semantic_export.get("task_policy"), Mapping)
            and str(
                semantic_export["task_policy"].get(
                    "public_summary_digest"
                )
            )
            == task.public_summary_digest
        ),
    }
    for source, passed in binding_checks.items():
        if not passed:
            findings.append(
                _finding(
                    "assignment_binding_incomplete",
                    "inconclusive",
                    source,
                    raw_inputs.get(source.split("_")[0], identity),
                )
            )

    if validation_mode != "real_host":
        findings.append(
            _finding(
                "real_host_required",
                "violation",
                "archive.validation_mode",
                validation_mode,
            )
        )
    for item in bindings:
        if not item.complete:
            findings.append(
                _finding(
                    "complete_store_export_required",
                    "inconclusive",
                    item.archive_path,
                    item.model_dump(mode="json"),
                )
            )

    workflow_runs = workflow_export.get("runs")
    if not isinstance(workflow_runs, list) or not workflow_runs:
        findings.append(
            _finding(
                "complete_attempt_denominator",
                "inconclusive",
                "scanner-inputs/workflow.json",
                workflow_runs,
            )
        )
        workflow_runs = []
    for run in workflow_runs:
        state = run.get("state") if isinstance(run, Mapping) else None
        events = run.get("events") if isinstance(run, Mapping) else None
        if (
            not isinstance(run, Mapping)
            or not isinstance(state, Mapping)
            or str(state.get("assignment_id")) != identity["assignment_id"]
            or str(state.get("session_id")) != identity["session_id"]
            or "outputs" not in run
            or "error" not in run
            or not isinstance(events, list)
            or any(
                not isinstance(event, Mapping) or "data" not in event
                for event in events
            )
        ):
            findings.append(
                _finding(
                    "complete_attempt_denominator",
                    "inconclusive",
                    f"workflow-run:{run.get('id') if isinstance(run, Mapping) else 'invalid'}",
                    run,
                )
            )

    bridge_tool_lifecycles, invalid_tool_events = _bridge_tool_lifecycles(
        bridge_export
    )
    for index, event in enumerate(invalid_tool_events):
        findings.append(
            _finding(
                "daemon_tool_lifecycle_event_invalid",
                "violation",
                f"bridge-tool-event:{index}",
                event,
            )
        )
    requests = blackbox_auth_export.get("requests")
    if not isinstance(requests, list):
        requests = []
    successful_draft_requests: dict[str, Mapping[str, Any]] = {}
    request_tool_call_ids: set[str] = set()
    blackbox_test_run_ids: set[str] = set()
    blackbox_business_run_ids: set[str] = set()
    for request in requests:
        if not isinstance(request, Mapping):
            findings.append(
                _finding(
                    "blackbox_request_invalid",
                    "inconclusive",
                    "scanner-inputs/blackbox-auth.json",
                    request,
                )
            )
            continue
        source_ref = f"blackbox-request:{request.get('request_id')}"
        request_operation = str(request.get("operation") or "")
        request_payload = request.get("payload")
        payload_digest = str(request.get("payload_digest") or "")
        if (
            not isinstance(request_payload, Mapping)
            or not hmac.compare_digest(_digest(request_payload), payload_digest)
        ):
            findings.append(
                _finding(
                    "blackbox_request_payload_unreplayable",
                    "inconclusive",
                    source_ref,
                    request,
                )
            )
        if (
            str(request.get("assignment_id")) != identity["assignment_id"]
            or str(request.get("session_id")) != identity["session_id"]
            or (
                request_operation in _APPLICATION_SCOPED_OPERATIONS
                and str(request.get("application_id")) != identity["application_id"]
            )
        ):
            findings.append(
                _finding(
                    "blackbox_request_cross_binding",
                    "violation",
                    source_ref,
                    request,
                )
            )
        if request.get("state") != "completed":
            findings.append(
                _finding(
                    "terminal_blackbox_request_incomplete",
                    "inconclusive",
                    source_ref,
                    request,
                )
            )
        tool_call_id = str(request.get("tool_call_id") or "")
        operation = request_operation
        lifecycle = bridge_tool_lifecycles.get(tool_call_id, [])
        if not tool_call_id or tool_call_id in request_tool_call_ids:
            findings.append(
                _finding(
                    "blackbox_tool_call_identity_reused",
                    "violation",
                    source_ref,
                    request,
                )
            )
        else:
            request_tool_call_ids.add(tool_call_id)
        started = [
            item
            for item in lifecycle
            if item[1] == "tool.started" and item[2] == operation
        ]
        terminal = [
            item
            for item in lifecycle
            if item[1] in {"tool.completed", "tool.failed"}
            and item[2] == operation
        ]
        status_code = request.get("status_code")
        successful = isinstance(status_code, int) and status_code < 400
        exact_lifecycle = (
            len(started) == 1
            and len(terminal) == 1
            and started[0][0] < terminal[0][0]
            and (
                (
                    successful
                    and terminal[0][1] == "tool.completed"
                    and terminal[0][3] is False
                )
                or (
                    not successful
                    and terminal[0][1] == "tool.failed"
                )
            )
        )
        if not exact_lifecycle:
            findings.append(
                _finding(
                    "blackbox_tool_lifecycle_unattributed",
                    "violation",
                    source_ref,
                    {
                        "request": request,
                        "lifecycle": lifecycle,
                    },
                )
            )
        if successful and operation in {
            "platform_tests_run",
            "platform_run_start",
        }:
            response = request.get("response")
            data = (
                response.get("data")
                if isinstance(response, Mapping)
                else None
            )
            operation_run_ids: list[str] = []
            if (
                not isinstance(response, Mapping)
                or response.get("ok") is not True
                or response.get("operation") != operation
                or not isinstance(data, Mapping)
            ):
                findings.append(
                    _finding(
                        "run_operation_response_unreplayable",
                        "inconclusive",
                        source_ref,
                        request,
                    )
                )
            elif operation == "platform_tests_run":
                tests = data.get("tests")
                if not isinstance(tests, list) or any(
                    not isinstance(item, Mapping)
                    for item in tests
                ):
                    findings.append(
                        _finding(
                            "run_operation_response_unreplayable",
                            "inconclusive",
                            source_ref,
                            request,
                        )
                    )
                else:
                    operation_run_ids = [
                        str(item.get("run_id") or "")
                        for item in tests
                        if str(item.get("run_id") or "")
                    ]
                    if (
                        len(operation_run_ids)
                        != len(set(operation_run_ids))
                        or any(
                            run_id in blackbox_test_run_ids
                            for run_id in operation_run_ids
                        )
                    ):
                        findings.append(
                            _finding(
                                "test_run_operation_identity_reused",
                                "violation",
                                source_ref,
                                operation_run_ids,
                            )
                        )
                    blackbox_test_run_ids.update(operation_run_ids)
            else:
                run_id = str(data.get("run_id") or "")
                if not run_id or run_id in blackbox_business_run_ids:
                    findings.append(
                        _finding(
                            "business_run_operation_identity_invalid",
                            "violation",
                            source_ref,
                            request,
                        )
                    )
                else:
                    operation_run_ids = [run_id]
                    blackbox_business_run_ids.add(run_id)
        if (
            request.get("operation") == "platform_draft_apply"
            and successful
        ):
            revision, content_hash = _response_revision_and_hash(request)
            if revision is not None and content_hash is not None:
                request_id = str(request.get("request_id") or "")
                if not request_id or request_id in successful_draft_requests:
                    findings.append(
                        _finding(
                            "draft_apply_request_identity_invalid",
                            "violation",
                            source_ref,
                            request,
                        )
                    )
                else:
                    successful_draft_requests[request_id] = request
            else:
                findings.append(
                    _finding(
                        "draft_apply_response_incomplete",
                        "inconclusive",
                        source_ref,
                        request,
                )
            )

    for tool_call_id, lifecycle in bridge_tool_lifecycles.items():
        tools = {item[2] for item in lifecycle}
        started = [item for item in lifecycle if item[1] == "tool.started"]
        terminal = [
            item
            for item in lifecycle
            if item[1] in {"tool.completed", "tool.failed"}
        ]
        exact_lifecycle = (
            len(lifecycle) == 2
            and len(started) == 1
            and len(terminal) == 1
            and started[0][0] < terminal[0][0]
            and len(tools) == 1
            and (
                (
                    terminal[0][1] == "tool.completed"
                    and terminal[0][3] is False
                )
                or terminal[0][1] == "tool.failed"
            )
        )
        if not exact_lifecycle:
            findings.append(
                _finding(
                    "daemon_tool_lifecycle_not_exactly_once",
                    "violation",
                    f"bridge-tool-call:{tool_call_id}",
                    lifecycle,
                )
            )
        tool = next(iter(tools), "")
        if tool not in _FORMAL_TOOL_ALLOWLIST:
            findings.append(
                _finding(
                    "daemon_tool_not_in_formal_allowlist",
                    "violation",
                    f"bridge-tool-call:{tool_call_id}",
                    lifecycle,
                )
            )
        if tool in _BLACKBOX_OPERATIONS and tool_call_id not in request_tool_call_ids:
            findings.append(
                _finding(
                    "blackbox_tool_lifecycle_has_no_authorization",
                    "violation",
                    f"bridge-tool-call:{tool_call_id}",
                    lifecycle,
                )
            )

    provenance = workflow_export.get("formal_draft_provenance")
    baselines = (
        provenance.get("baselines")
        if isinstance(provenance, Mapping)
        else None
    )
    mutations = (
        provenance.get("mutations")
        if isinstance(provenance, Mapping)
        else None
    )
    baseline: Mapping[str, Any] | None = None
    if not isinstance(baselines, list) or len(baselines) != 1:
        findings.append(
            _finding(
                "assignment_start_draft_baseline_missing",
                "inconclusive",
                "workflow.formal_draft_provenance.baselines",
                baselines,
            )
        )
    elif not isinstance(baselines[0], Mapping):
        findings.append(
            _finding(
                "assignment_start_draft_baseline_invalid",
                "inconclusive",
                "workflow.formal_draft_provenance.baselines[0]",
                baselines[0],
            )
        )
    else:
        baseline = baselines[0]
        if (
            str(baseline.get("assignment_id")) != identity["assignment_id"]
            or str(baseline.get("session_id")) != identity["session_id"]
            or str(baseline.get("application_id")) != identity["application_id"]
            or not isinstance(baseline.get("baseline_revision"), int)
            or _utc_datetime(baseline.get("started_at")) is None
        ):
            findings.append(
                _finding(
                    "assignment_start_draft_baseline_invalid",
                    "inconclusive",
                    "workflow.formal_draft_provenance.baselines[0]",
                    baseline,
                )
            )

    if not isinstance(mutations, list) or not mutations:
        findings.append(
            _finding(
                "lilies_draft_mutation_chain_missing",
                "violation",
                "workflow.formal_draft_provenance.mutations",
                mutations,
            )
        )
        mutations = []
    terminal_drained_at = (
        _utc_datetime(bridge_row.get("terminal_events_drained_at"))
        if isinstance(bridge_row, Mapping)
        else None
    )
    baseline_started_at = (
        _utc_datetime(baseline.get("started_at"))
        if baseline is not None
        else None
    )
    cursor: tuple[int, str] | None = None
    if baseline is not None and isinstance(baseline.get("baseline_revision"), int):
        cursor = (
            int(baseline["baseline_revision"]),
            _normalized_digest(baseline.get("baseline_content_hash")),
        )
    matched_request_ids: set[str] = set()
    previous_after_revision = -1
    for index, mutation in enumerate(mutations):
        source_ref = f"workflow-draft-mutation:{index}"
        if not isinstance(mutation, Mapping):
            findings.append(
                _finding(
                    "draft_mutation_record_invalid",
                    "inconclusive",
                    source_ref,
                    mutation,
                )
            )
            continue
        before_revision = mutation.get("before_revision")
        after_revision = mutation.get("after_revision")
        before_state = (
            int(before_revision),
            _normalized_digest(mutation.get("before_content_hash")),
        ) if isinstance(before_revision, int) else None
        after_state = (
            int(after_revision),
            _normalized_digest(mutation.get("after_content_hash")),
        ) if isinstance(after_revision, int) else None
        mutation_at = _utc_datetime(mutation.get("created_at"))
        request_id = str(mutation.get("request_id") or "")
        tool_call_id = str(mutation.get("tool_call_id") or "")
        request = successful_draft_requests.get(request_id)
        response_state = (
            _response_revision_and_hash(request)
            if request is not None
            else (None, None)
        )
        request_payload = (
            request.get("payload")
            if isinstance(request, Mapping)
            and isinstance(request.get("payload"), Mapping)
            else None
        )
        operation_payload = (
            {
                "application_id": request_payload.get("application_id"),
                "expected_revision": request_payload.get("expected_revision"),
                "op": request_payload.get("op"),
                "data": request_payload.get("data"),
            }
            if request_payload is not None
            else None
        )
        valid = (
            str(mutation.get("assignment_id")) == identity["assignment_id"]
            and str(mutation.get("session_id")) == identity["session_id"]
            and str(mutation.get("application_id")) == identity["application_id"]
            and mutation.get("actor_kind") == "lilies_blackbox"
            and cursor is not None
            and before_state == cursor
            and after_state is not None
            and after_state[0] == cursor[0] + 1
            and after_state[0] > previous_after_revision
            and mutation.get("content_changed") in {1, True}
            and before_state[1] != after_state[1]
            and request is not None
            and str(request.get("tool_call_id")) == tool_call_id
            and request_payload is not None
            and str(request_payload.get("application_id"))
            == identity["application_id"]
            and request_payload.get("expected_revision") == before_revision
            and str(request_payload.get("op")) == str(mutation.get("operation"))
            and isinstance(request_payload.get("data"), Mapping)
            and str(request.get("idempotency_key"))
            == str(mutation.get("idempotency_key"))
            and operation_payload is not None
            and hmac.compare_digest(
                _digest(operation_payload),
                str(mutation.get("operation_digest") or ""),
            )
            and str(request.get("payload_digest"))
            == str(mutation.get("request_payload_digest"))
            and response_state == after_state
            and mutation_at is not None
            and baseline_started_at is not None
            and mutation_at >= baseline_started_at
            and terminal_drained_at is not None
            and mutation_at <= terminal_drained_at
        )
        if not valid:
            findings.append(
                _finding(
                    "draft_mutation_chain_untrusted",
                    "violation",
                    source_ref,
                    {
                        "mutation": mutation,
                        "expected_before": cursor,
                        "request": request,
                        "terminal_events_drained_at": (
                            terminal_drained_at.isoformat()
                            if terminal_drained_at is not None
                            else None
                        ),
                    },
                )
            )
        if request is not None:
            matched_request_ids.add(request_id)
        if after_state is not None:
            cursor = after_state
            previous_after_revision = after_state[0]
    if set(successful_draft_requests) != matched_request_ids:
        findings.append(
            _finding(
                "draft_apply_mutation_denominator_mismatch",
                "violation",
                "blackbox-auth.draft-apply",
                {
                    "successful_request_ids": sorted(successful_draft_requests),
                    "mutation_request_ids": sorted(matched_request_ids),
                },
            )
        )

    draft = workflow_export.get("draft")
    if not isinstance(draft, Mapping):
        findings.append(
            _finding(
                "final_draft_provenance",
                "inconclusive",
                "scanner-inputs/workflow.json",
                draft,
            )
        )
    else:
        final_state = (
            int(draft.get("revision", -1)),
            _normalized_digest(draft.get("content_hash")),
        )
        if cursor is None or final_state != cursor:
            findings.append(
                _finding(
                    "final_draft_not_produced_by_lilies_blackbox",
                    "violation",
                    "workflow.draft",
                    draft,
                )
            )

    inventory_records = artifact_inventory_export.get("records")
    if not isinstance(inventory_records, list):
        inventory_records = []
    claimed_business_run_ids = {str(value) for value in business_run_ids}
    if not claimed_business_run_ids:
        findings.append(
            _finding(
                "business_run_denominator_missing",
                "inconclusive",
                "claim.business_run_ids",
                list(business_run_ids),
            )
        )
    if (
        not claimed_business_run_ids <= blackbox_business_run_ids
        or claimed_business_run_ids & blackbox_test_run_ids
    ):
        findings.append(
            _finding(
                "business_run_operation_provenance_invalid",
                "violation",
                "scanner-inputs/blackbox-auth.json",
                {
                    "claimed": sorted(claimed_business_run_ids),
                    "platform_run_start": sorted(
                        blackbox_business_run_ids
                    ),
                    "platform_tests_run": sorted(blackbox_test_run_ids),
                },
            )
        )
    indexed_business_run_ids = {
        str(entry.run_id) for entry in evidence_index.entries
    }
    workflow_run_ids = {
        str(run.get("id"))
        for run in workflow_runs
        if isinstance(run, Mapping)
    }
    if (
        not claimed_business_run_ids <= workflow_run_ids
        or not claimed_business_run_ids <= indexed_business_run_ids
        or not indexed_business_run_ids <= claimed_business_run_ids
    ):
        findings.append(
            _finding(
                "business_run_denominator_incomplete",
                "violation",
                "evidence-index.json",
                {
                    "claimed": sorted(claimed_business_run_ids),
                    "workflow": sorted(workflow_run_ids),
                    "indexed": sorted(indexed_business_run_ids),
                },
            )
        )
    inventory_business = {
        str(record.get("artifact_id")): record
        for record in inventory_records
        if isinstance(record, Mapping)
        and str(record.get("run_id")) in claimed_business_run_ids
    }
    indexed_artifact_ids = {
        entry.archive_path.rsplit("/", 1)[-1].removesuffix(".bin")
        for entry in evidence_index.entries
    }
    if set(inventory_business) != indexed_artifact_ids:
        findings.append(
            _finding(
                "complete_business_evidence_inventory",
                "violation",
                "evidence-index.json",
                {
                    "inventory_ids": sorted(inventory_business),
                    "indexed_ids": sorted(indexed_artifact_ids),
                },
            )
        )

    reports = {
        str(item.get("report_id")): item
        for item in collaboration_export.get("reports", [])
        if isinstance(item, Mapping)
    }
    approvals = {
        str(item.get("report_id"))
        for item in collaboration_export.get("approvals", [])
        if isinstance(item, Mapping)
        and str(item.get("decision") or item.get("status")).casefold()
        in {"approved", "approve"}
    }
    try:
        source_manifest = FormalSourceProvenanceManifest.model_validate(
            source_provenance_export
        )
        expected_source_bindings = approved_developer_response_bindings(
            collaboration_export.get("messages", []),
            channel_id=channel_id,
        )
    except Exception as error:
        findings.append(
            _finding(
                "developer_source_provenance_invalid",
                "inconclusive",
                "source-provenance/manifest.json",
                {
                    "source": source_provenance_export,
                    "error_type": type(error).__name__,
                },
            )
        )
        source_manifest = None
        expected_source_bindings = []
    if source_manifest is not None and (
        source_manifest.assignment_id != assignment.assignment_id
        or source_manifest.channel_id != channel_id
        or source_manifest.task_id != task.task_id
        or source_manifest.task_revision != task.revision
        or source_manifest.run_id != task.run_id
        or [commit.binding for commit in source_manifest.approved_commits]
        != expected_source_bindings
    ):
        findings.append(
            _finding(
                "developer_source_provenance_binding_mismatch",
                "violation",
                "source-provenance/manifest.json",
                {
                    "manifest": source_manifest,
                    "expected_bindings": expected_source_bindings,
                },
            )
        )
    findings.extend(
        evaluate_source_semantic_input(
            task_package=source_semantic_task_package,
            source_manifest=(
                source_manifest
                if source_manifest is not None
                else source_provenance_export
            ),
            source_files=source_semantic_files or {},
            archived_input=semantic_export,
        )
    )
    for response in collaboration_export.get("developer_responses", []):
        if not isinstance(response, Mapping):
            findings.append(
                _finding(
                    "developer_response_invalid",
                    "inconclusive",
                    "collaboration.developer_responses",
                    response,
                )
            )
            continue
        report_id = str(response.get("report_id") or "")
        source_ref = f"developer-response:{response.get('response_id')}"
        if report_id not in reports or report_id not in approvals:
            findings.append(
                _finding(
                    "developer_delta_not_user_approved",
                    "violation",
                    source_ref,
                    response,
                )
            )
        normalized = {
            value.casefold().replace("-", "_")
            for value in _strings_and_keys(response)
        }
        if (
            {"nodes", "edges"} <= normalized
            or "workflowspec" in normalized
            or "workflow_spec" in normalized
            or "final_graph" in normalized
        ):
            findings.append(
                _finding(
                    "developer_authored_final_workflow",
                    "violation",
                    source_ref,
                    response,
                )
            )

    unique_findings = {
        (
            item.rule_id,
            item.outcome,
            item.source_ref,
            item.evidence_digest,
        ): item
        for item in findings
    }
    findings = [
        unique_findings[key]
        for key in sorted(
            unique_findings,
            key=lambda value: tuple(str(item) for item in value),
        )
    ]
    verdict: Literal["pass", "failed", "inconclusive"]
    if any(item.outcome == "inconclusive" for item in findings):
        verdict = "inconclusive"
    elif findings:
        verdict = "failed"
    else:
        verdict = "pass"
    base = {
        "schema_version": "1.0",
        "scanner_version": SCANNER_VERSION,
        "scanner_process_digest": scanner_process_digest(),
        "task_id": task.task_id,
        "revision": task.revision,
        "run_id": task.run_id,
        "assignment_id": assignment.assignment_id,
        "session_id": session_id,
        "channel_id": channel_id,
        "protected_policy_digest": generic_policy_digest(),
        "input_bindings": bindings,
        "findings": findings,
        "verdict": verdict,
        "created_at": created_at,
    }
    provisional = ForbiddenAssistanceScanRecord(
        **base,
        scan_digest="sha256:" + "0" * 64,
    )
    digest_payload = provisional.model_dump(mode="json", exclude_none=True)
    digest_payload.pop("scan_digest")
    return provisional.model_copy(
        update={"scan_digest": _digest(digest_payload)}
    )


def validate_scan_digest(record: ForbiddenAssistanceScanRecord) -> None:
    payload = record.model_dump(mode="json", exclude_none=True)
    persisted = str(payload.pop("scan_digest"))
    if not hmac.compare_digest(_digest(payload), persisted):
        raise ValueError("forbidden-assistance scan digest changed")


# Every retained evaluator owns an immutable set of source components. A later
# scanner must be added under a new version instead of editing these functions
# in place; golden digest tests make accidental drift visible.
_SCANNER_RUNTIME_CONTRACTS: Mapping[str, Mapping[str, Any]] = {
    SCANNER_VERSION: {
        "application_scoped_operations": sorted(
            _APPLICATION_SCOPED_OPERATIONS
        ),
        "blackbox_operations": sorted(_BLACKBOX_OPERATIONS),
        "non_blackbox_formal_tools": sorted(_NON_BLACKBOX_FORMAL_TOOLS),
        "formal_tool_allowlist": sorted(_FORMAL_TOOL_ALLOWLIST),
        "semantic_marker_stoplist": sorted(_SEMANTIC_MARKER_STOPLIST),
        "source_implementation_markers": sorted(
            _SOURCE_IMPLEMENTATION_MARKERS
        ),
        "text_source_suffixes": sorted(_TEXT_SOURCE_SUFFIXES),
    }
}
_SCANNER_EVALUATOR_COMPONENTS: Mapping[str, tuple[Any, ...]] = {
    SCANNER_VERSION: (
        BuildAssignment,
        ArchivedEvidenceIndex,
        CollaborationMessageEnvelope,
        ApprovalDecision,
        DeveloperResponse,
        FormalSourceProvenanceManifest,
        approved_developer_response_bindings,
        _safe_semantic_source_path,
        SourceSemanticProjectPolicy,
        SourceSemanticPolicyBinding,
        SourceSemanticArchiveFile,
        SourceSemanticChange,
        SourceSemanticInput,
        ForbiddenAssistanceInputBinding,
        ForbiddenAssistanceFinding,
        ForbiddenAssistanceScanRecord,
        _strings_and_keys,
        _bridge_tool_lifecycles,
        _input_binding,
        _finding,
        _as_mapping,
        _fixture_identifier_tokens,
        _frozen_fixture_identifiers,
        _source_semantic_policy,
        _source_semantic_file,
        derive_source_semantic_input,
        _semantic_normalize,
        _repository_stem,
        _semantic_markers,
        _contains_semantic_marker,
        _added_source_text,
        _explicit_final_graph,
        _looks_like_fixture_field_mapping,
        _evaluate_derived_source_semantics,
        evaluate_source_semantic_input,
        _response_revision_and_hash,
        _normalized_digest,
        _utc_datetime,
        _scan_t01f_generic_1,
        validate_scan_digest,
    ),
}
_SCANNER_EVALUATORS = {
    SCANNER_VERSION: _scan_t01f_generic_1,
}


def registered_scanner_versions() -> tuple[str, ...]:
    versions = tuple(sorted(_SCANNER_EVALUATORS))
    if set(versions) != set(_SCANNER_POLICIES) or set(versions) != set(
        _SCANNER_EVALUATOR_COMPONENTS
    ) or set(versions) != set(_SCANNER_RUNTIME_CONTRACTS):
        raise RuntimeError("forbidden-assistance scanner registry is incomplete")
    return versions


def scan_forbidden_assistance(
    *,
    scanner_version: str = SCANNER_VERSION,
    **inputs: Any,
) -> ForbiddenAssistanceScanRecord:
    """Dispatch a scan through its retained immutable evaluator."""

    registered_scanner_versions()
    evaluator = _SCANNER_EVALUATORS.get(scanner_version)
    if evaluator is None:
        raise ValueError(
            f"forbidden-assistance scanner is unavailable: {scanner_version}"
        )
    return evaluator(**inputs)
