from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import importlib.metadata
import json
import os
import platform
import secrets
import shutil
import socket
import stat
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .collaboration_models import ClaimStatus, EvidenceKind, VerificationClaim
from .formal_verification_contracts import (
    ArchivedEvidenceIndex,
    OracleContract,
)
from .formal_source_provenance import (
    DEVELOPER_TRUST_ROOT_PATHS,
    FormalSourceProvenanceError,
    approved_developer_response_bindings,
    verify_source_provenance_archive_offline,
)
from .forbidden_assistance_scanner import (
    ForbiddenAssistanceScanRecord,
    scan_forbidden_assistance,
    validate_scan_digest,
)
from .lilies_models import (
    AllowedAction,
    ApplicationTarget,
    ArtifactRef,
    AssignmentConstraints,
    AssignmentMode,
    AssignmentNetworkPolicy,
    BuildAssignment,
    BusinessContext,
    CollaborationAccess,
    DeliverableSpec,
    Digest,
    OpaqueReference,
    PlatformAccess,
    PlatformScope,
    ProhibitedAction,
    TaskPackageRef,
)
from .workflow_models import ApplicationSnapshot


MAX_CONTROL_FILE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 128 * 1024 * 1024
VERIFICATION_POLICY_MANIFEST_FILE = "verification-policy.json"
WORKSPACE_POLICY_FILE = ".lilies-workspace-policy.json"
WORKSPACE_MANIFEST_FILE = ".lilies-mount-manifest.json"
_IMMUTABLE_TOP_LEVEL_FILES = frozenset(
    {
        "task.yaml",
        "requirement.md",
        "environment.lock",
        "allowed-actions.json",
        "budget.json",
    }
)
_IMMUTABLE_TOP_LEVEL_DIRECTORIES = frozenset({"fixtures", "protected"})
_GENERATED_TOP_LEVEL = frozenset({"archive-manifest.json", "runs"})
_ARCHIVE_LOCK_FILE = ".archive-index.lock"
_FORMAL_COLLABORATION_ARCHIVE_COLLECTIONS = (
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
_MANDATORY_PROHIBITIONS = frozenset(
    {
        "read_platform_source",
        "read_platform_database",
        "read_protected",
        "modify_task_package",
        "install_unknown_adapter",
    }
)
_FORBIDDEN_WORKSPACE_SEGMENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "protected",
        "oracle",
        "expected-state",
        "platform-data",
        "platform_data",
    }
)
_FORBIDDEN_WORKSPACE_IDENTITIES = frozenset(
    item.casefold() for item in _FORBIDDEN_WORKSPACE_SEGMENTS
)
_DEVELOPER_SOURCE_EXCLUDED_IDENTITIES = _FORBIDDEN_WORKSPACE_IDENTITIES | frozenset(
    {
        ".venv",
        ".idea",
        ".vscode",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".next",
        "node_modules",
        "dist",
        "data",
        "workspaces",
    }
)
_DEVELOPER_SOURCE_SECRET_IDENTITIES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "service-account.json",
        "service_account.json",
    }
)
_DEVELOPER_SOURCE_SECRET_SUFFIXES = (
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
)
MAX_DEVELOPER_SOURCE_FILES = 20_000
MAX_DEVELOPER_SOURCE_BYTES = 256 * 1024 * 1024
_VERIFICATION_BUNDLE_SOURCE_PATHS = frozenset(
    {
        "agent_platform/__init__.py",
        "agent_platform/capability_contracts.py",
        "agent_platform/collaboration_models.py",
        "agent_platform/formal_source_provenance.py",
        "agent_platform/formal_verification_contracts.py",
        "agent_platform/forbidden_assistance_scanner.py",
        "agent_platform/independent_verifier.py",
        "agent_platform/independent_verifier_broker.py",
        "agent_platform/lilies_models.py",
        "agent_platform/models.py",
        "agent_platform/stable_verification.py",
        "agent_platform/stable_verification_cli.py",
        "agent_platform/stable_verification_coordinator.py",
        "agent_platform/task_packages.py",
        "agent_platform/workflow_models.py",
    }
)
_VERIFICATION_RUNTIME_DISTRIBUTIONS = (
    "PyYAML",
    "annotated-types",
    "pydantic",
    "pydantic-core",
    "typing-extensions",
    "typing-inspection",
)
_ACTION_PLATFORM_SCOPES: dict[AllowedAction, PlatformScope] = {
    AllowedAction.platform_contract_get: PlatformScope.catalog_read,
    AllowedAction.platform_block_search: PlatformScope.catalog_read,
    AllowedAction.platform_block_get: PlatformScope.catalog_read,
    AllowedAction.platform_tool_catalog: PlatformScope.catalog_read,
    AllowedAction.platform_application_create: PlatformScope.application_write,
    AllowedAction.platform_application_get: PlatformScope.application_write,
    AllowedAction.platform_draft_inspect: PlatformScope.draft_write,
    AllowedAction.platform_draft_apply: PlatformScope.draft_write,
    AllowedAction.platform_tests_run: PlatformScope.test_execute,
    AllowedAction.platform_run_start: PlatformScope.run_execute,
    AllowedAction.platform_run_get: PlatformScope.run_execute,
    AllowedAction.platform_run_resume: PlatformScope.run_execute,
    AllowedAction.platform_run_cancel: PlatformScope.run_execute,
    AllowedAction.platform_trace_get: PlatformScope.trace_read,
    AllowedAction.platform_artifact_read: PlatformScope.artifact_read,
    AllowedAction.platform_publish: PlatformScope.application_publish,
}


class _ArchivedConnectorAssignmentWriteReceipt(BaseModel):
    """Lightweight replay-only schema kept inside the frozen verifier closure."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_id: str
    connector_id: str
    connector_version: int
    tenant_id: str
    profile_id: str
    operation_id: str
    operation_kind: Literal["read", "write", "compensate"]
    idempotency_key: str
    payload_hash: str
    status: Literal[
        "executing",
        "dry_run",
        "succeeded",
        "failed",
        "compensated",
    ]
    side_effect_state: Literal["none", "applied", "unknown", "compensated"]
    authorization_ref_digest: str | None
    adapter_called: bool
    created_at: str
    updated_at: str


class _ArchivedConnectorAssignmentBudgetReceipt(BaseModel):
    """Strict read-only mirror of the runtime connector budget receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: str
    policy_digest: str
    allowed_network_hosts: list[str]
    allowed_compensation_operations: list[str]
    max_write_count: int
    max_payload_bytes: int
    write_count: int
    writes: list[_ArchivedConnectorAssignmentWriteReceipt]
    receipt_digest: str

    @model_validator(mode="after")
    def verify_frozen_receipt(
        self,
    ) -> _ArchivedConnectorAssignmentBudgetReceipt:
        identities = [
            (
                item.connector_id,
                item.connector_version,
                item.tenant_id,
                item.operation_id,
                item.idempotency_key,
                item.execution_id,
            )
            for item in self.writes
        ]
        if identities != sorted(identities):
            raise ValueError(
                "connector assignment writes are not in stable execution "
                "identity order"
            )
        if len(identities) != len(set(identities)):
            raise ValueError(
                "connector assignment receipt contains duplicate writes"
            )
        execution_ids = [item.execution_id for item in self.writes]
        if len(execution_ids) != len(set(execution_ids)):
            raise ValueError(
                "connector assignment receipt contains duplicate execution ids"
            )
        if self.write_count != len(self.writes):
            raise ValueError(
                "connector assignment write count does not match durable "
                "reservations"
            )
        policy_document = {
            "allowed_network_hosts": self.allowed_network_hosts,
            "allowed_compensation_operations": (
                self.allowed_compensation_operations
            ),
            "max_write_count": self.max_write_count,
            "max_payload_bytes": self.max_payload_bytes,
        }
        expected_policy_digest = hashlib.sha256(
            json.dumps(
                policy_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(
            self.policy_digest,
            expected_policy_digest,
        ):
            raise ValueError(
                "connector assignment policy digest does not match receipt"
            )
        unsigned = self.model_dump(mode="json", exclude={"receipt_digest"})
        expected_receipt_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(
            self.receipt_digest,
            expected_receipt_digest,
        ):
            raise ValueError(
                "connector assignment receipt digest does not match content"
            )
        return self

TaskId = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
RelativePath = Annotated[str, StringConstraints(min_length=1, max_length=1_000)]


class TaskPackageError(RuntimeError):
    """Base error for a rejected package, preflight, mount, or archive."""


class TaskPackageSecurityError(TaskPackageError):
    """A filesystem or secret boundary was violated."""


class TaskPackageConflict(TaskPackageError):
    """An immutable identity was reused with different content."""


class TaskPackageNotReady(TaskPackageError):
    """The exact frozen environment is not ready for an assignment."""


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("datetime must use UTC")
    return value.astimezone(timezone.utc)


def _canonical_json(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _digest_file(path: Path, *, limit: int = MAX_ARCHIVE_FILE_BYTES) -> tuple[str, int]:
    descriptor = _open_regular_file(path)
    digest = hashlib.sha256()
    total = 0
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise TaskPackageSecurityError(f"file exceeds the {limit}-byte limit")
            digest.update(chunk)
        final = os.fstat(descriptor)
        if final.st_nlink != 1:
            raise TaskPackageSecurityError("hard-linked files are forbidden")
    finally:
        os.close(descriptor)
    return f"sha256:{digest.hexdigest()}", total


def _open_regular_file(path: Path) -> int:
    if path.is_symlink():
        raise TaskPackageSecurityError("symlink files are forbidden")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TaskPackageSecurityError("file is not safely readable") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise TaskPackageSecurityError("only regular files are allowed")
    if metadata.st_nlink != 1:
        os.close(descriptor)
        raise TaskPackageSecurityError("hard-linked files are forbidden")
    return descriptor


def _read_bytes(path: Path, *, limit: int = MAX_CONTROL_FILE_BYTES) -> bytes:
    descriptor = _open_regular_file(path)
    try:
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1)):
            total += len(chunk)
            if total > limit:
                raise TaskPackageSecurityError(f"file exceeds the {limit}-byte limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> Any:
    try:
        return _strict_json_loads(_read_bytes(path))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise TaskPackageError(f"{path.name} is not valid JSON") from error


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _strict_json_loads(payload: bytes | str) -> Any:
    return json.loads(
        payload,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_json_constant,
    )


def _walk_json_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            strings.append(str(key))
            strings.extend(_walk_json_strings(item))
    elif isinstance(value, list):
        for item in value:
            strings.extend(_walk_json_strings(item))
    elif isinstance(value, str):
        strings.append(value)
    return strings


def _walk_json_string_values(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            strings.extend(_walk_json_string_values(item))
    elif isinstance(value, list):
        for item in value:
            strings.extend(_walk_json_string_values(item))
    elif isinstance(value, str):
        strings.append(value)
    return strings


def _decoded_text_variants(value: str) -> list[str]:
    """Decode bounded scalar encodings used to disguise protected text."""

    variants = [value]
    encoded = value.strip()
    if not 4 <= len(encoded) <= MAX_CONTROL_FILE_BYTES:
        return variants
    candidates: list[bytes] = []
    if len(encoded) % 2 == 0 and all(character in "0123456789abcdefABCDEF" for character in encoded):
        try:
            candidates.append(bytes.fromhex(encoded))
        except ValueError:
            pass
    if all(character.isalnum() or character in "+/_-=" for character in encoded):
        padded = encoded + ("=" * (-len(encoded) % 4))
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                candidates.append(decoder(padded))
            except (ValueError, TypeError):
                pass
    for candidate in candidates:
        if not candidate or len(candidate) > MAX_CONTROL_FILE_BYTES:
            continue
        try:
            decoded = candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if decoded not in variants:
            variants.append(decoded)
    return variants


def _decoded_payload_strings(payload: bytes) -> list[str]:
    strings: list[str] = []
    try:
        strings.append(payload.decode("utf-8"))
    except UnicodeDecodeError:
        return strings
    decoded: Any | None = None
    try:
        decoded = _strict_json_loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        records: list[Any] = []
        for line in payload.splitlines():
            if not line or not line.strip():
                records = []
                break
            try:
                records.append(_strict_json_loads(line))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                records = []
                break
        if records:
            decoded = records
    if decoded is not None:
        strings.extend(_walk_json_strings(decoded))
    expanded: list[str] = []
    for value in strings:
        for variant in _decoded_text_variants(value):
            if variant not in expanded:
                expanded.append(variant)
    return expanded


def formal_platform_scopes(
    actions: Sequence[AllowedAction],
) -> list[PlatformScope]:
    """Project package actions to the only platform scopes they require."""

    required = {_ACTION_PLATFORM_SCOPES[action] for action in actions}
    return [scope for scope in PlatformScope if scope in required]


class _StrictYamlLoader(yaml.SafeLoader):
    pass


def _reject_duplicate_yaml_keys(
    loader: _StrictYamlLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key == "<<":
            raise TaskPackageError("YAML merge keys are forbidden")
        if key in result:
            raise TaskPackageError(f"duplicate YAML key is forbidden: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _reject_duplicate_yaml_keys,
)


def parse_task_package_yaml(raw: bytes) -> Any:
    """Parse the deliberately small, alias-free formal task YAML subset."""

    if len(raw) > MAX_CONTROL_FILE_BYTES:
        raise TaskPackageSecurityError(f"file exceeds the {MAX_CONTROL_FILE_BYTES}-byte limit")
    if b"&" in raw or b"*" in raw:
        # Formal control files deliberately use a small, explicit YAML subset.
        raise TaskPackageError("YAML anchors and aliases are forbidden")
    try:
        return yaml.load(raw.decode("utf-8"), Loader=_StrictYamlLoader)
    except TaskPackageError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise TaskPackageError("task control file is not valid safe YAML") from error


def _read_yaml(path: Path) -> Any:
    return parse_task_package_yaml(_read_bytes(path))


def _normalize_relative_path(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise ValueError("path must be a POSIX relative path")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("path must use NFC Unicode normalization")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise ValueError("path must be relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path cannot contain empty, dot, or parent segments")
    return path.as_posix()


def _unique_paths(values: Sequence[str]) -> list[str]:
    normalized = [_normalize_relative_path(value) for value in values]
    identities = [unicodedata.normalize("NFC", item).casefold() for item in normalized]
    if len(identities) != len(set(identities)):
        raise ValueError("paths must be unique after case and Unicode normalization")
    return normalized


def _contains_forbidden_workspace_segment(value: str) -> bool:
    return any(
        part.casefold() in _FORBIDDEN_WORKSPACE_IDENTITIES for part in PurePosixPath(value).parts
    )


def _resolved_child(root: Path, relative: str) -> Path:
    normalized = _normalize_relative_path(relative)
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*PurePosixPath(normalized).parts)
    resolved = candidate.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise TaskPackageSecurityError("path escapes its declared root")
    return candidate


class Cohort(str, Enum):
    enterprise = "enterprise"
    individual = "individual"


class ValidationMode(str, Enum):
    real_host = "real_host"
    protocol_mock = "protocol_mock"


class ArchiveStatus(str, Enum):
    succeeded = "succeeded"
    failed = "failed"
    partial = "partial"
    environment_failed = "environment_failed"
    task_author_failed = "task_author_failed"
    cancelled = "cancelled"
    invalid = "invalid"


class WorkspaceRole(str, Enum):
    lilies = "lilies"
    developer = "developer"
    verifier = "verifier"


class SourceProjectLock(StrictFrozenModel):
    name: str = Field(min_length=1, max_length=160)
    repository_url: AnyHttpUrl
    release: str = Field(min_length=1, max_length=160)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    image_digest: Digest
    license: str = Field(min_length=1, max_length=160)

    @field_validator("repository_url")
    @classmethod
    def repository_has_no_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("repository URL cannot carry credentials, query, or fragment")
        return value


class DeliverableContract(StrictFrozenModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=2_000)
    media_type: str = Field(min_length=1, max_length=200)


class TaskPackageSpec(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    cohort: Cohort
    customer_role: str = Field(min_length=1, max_length=2_000)
    business_goal: str = Field(min_length=1, max_length=10_000)
    source_projects: list[SourceProjectLock] = Field(min_length=1, max_length=20)
    requirement_file: Literal["requirement.md"]
    environment_lock_digest: Digest
    fixture_manifest_digest: Digest
    allowed_actions_file: Literal["allowed-actions.json"]
    budget_file: Literal["budget.json"]
    deliverables: list[DeliverableContract] = Field(min_length=1, max_length=100)
    acceptance_summary: str = Field(min_length=1, max_length=20_000)
    no_substitute_validation: Literal[True]
    collaboration_enabled: Literal[True]
    author: str = Field(min_length=1, max_length=160)
    created_at: datetime
    parent_revision: int | None = Field(default=None, ge=1)
    amendment_reason: str | None = Field(default=None, min_length=1, max_length=5_000)

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def revision_chain_is_explicit(self) -> TaskPackageSpec:
        if self.revision == 1:
            if self.parent_revision is not None or self.amendment_reason is not None:
                raise ValueError("revision one cannot have a parent or amendment reason")
        elif self.parent_revision != self.revision - 1 or self.amendment_reason is None:
            raise ValueError(
                "later revisions require the immediately preceding parent and a reason"
            )
        return self


class FileDigestEntry(StrictFrozenModel):
    path: RelativePath
    digest: Digest
    size_bytes: int = Field(ge=0, le=MAX_ARCHIVE_FILE_BYTES)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return _normalize_relative_path(value)


class FixtureManifest(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    files: list[FileDigestEntry] = Field(min_length=1, max_length=10_000)

    @field_validator("files")
    @classmethod
    def file_paths_are_unique(cls, value: list[FileDigestEntry]) -> list[FileDigestEntry]:
        _unique_paths([entry.path for entry in value])
        if any(
            not PurePosixPath(entry.path).parts
            or PurePosixPath(entry.path).parts[0] != "public-inputs"
            or "protected" in PurePosixPath(entry.path).parts
            for entry in value
        ):
            raise ValueError("fixture files must live under public-inputs")
        return value


class NamedDigest(StrictFrozenModel):
    name: str = Field(min_length=1, max_length=200)
    digest: Digest


class PortLock(StrictFrozenModel):
    service: str = Field(min_length=1, max_length=160)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def host_is_not_a_url(cls, value: str) -> str:
        if any(marker in value for marker in ("://", "/", "@", " ")):
            raise ValueError("host must be a hostname or IP, not a URL")
        return value


class HealthCheckSpec(StrictFrozenModel):
    check_id: OpaqueReference
    kind: Literal["http", "tcp"]
    url: AnyHttpUrl | None = None
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    expected_status: int | None = Field(default=None, ge=100, le=599)
    expected_body_digest: Digest | None = None
    timeout_seconds: float = Field(gt=0, le=60, allow_inf_nan=False)
    mandatory: Literal[True] = True

    @model_validator(mode="after")
    def endpoint_matches_kind(self) -> HealthCheckSpec:
        if self.kind == "http":
            if self.url is None or self.host is not None or self.port is not None:
                raise ValueError("HTTP checks require only url")
            parsed = urlsplit(str(self.url))
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("HTTP health URL must not carry credentials or query")
            if self.expected_status is None:
                raise ValueError("HTTP checks require expected_status")
        elif (
            self.url is not None
            or self.host is None
            or self.port is None
            or self.expected_status is not None
            or self.expected_body_digest is not None
        ):
            raise ValueError("TCP checks require only host and port")
        return self


class FaultInjectionLock(StrictFrozenModel):
    name: str = Field(min_length=1, max_length=200)
    activation_command_digest: Digest
    recovery_command_digest: Digest


class EnvironmentLock(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    source_projects: list[SourceProjectLock] = Field(min_length=1, max_length=20)
    compose_digest: Digest
    ports: list[PortLock] = Field(min_length=1, max_length=100)
    network_name: str = Field(min_length=1, max_length=160)
    volumes: list[str] = Field(min_length=1, max_length=100)
    initialization_commands: list[NamedDigest] = Field(min_length=1, max_length=100)
    seed_commands: list[NamedDigest] = Field(min_length=1, max_length=100)
    health_checks: list[HealthCheckSpec] = Field(min_length=1, max_length=100)
    secret_refs: list[OpaqueReference] = Field(default_factory=list, max_length=100)
    attestation_secret_ref: OpaqueReference
    python_version: str = Field(min_length=1, max_length=80)
    node_version: str = Field(min_length=1, max_length=80)
    docker_version: str = Field(min_length=1, max_length=80)
    fixture_files: list[FileDigestEntry] = Field(min_length=1, max_length=10_000)
    fault_injections: list[FaultInjectionLock] = Field(default_factory=list, max_length=100)
    provenance: Literal["real_host"]

    @field_validator(
        "ports",
        "initialization_commands",
        "seed_commands",
        "health_checks",
        "secret_refs",
        "fixture_files",
        "fault_injections",
    )
    @classmethod
    def entries_are_unique(cls, value: list[Any]) -> list[Any]:
        identities = [
            getattr(
                item,
                "check_id",
                getattr(
                    item,
                    "name",
                    getattr(item, "path", getattr(item, "service", item)),
                ),
            )
            for item in value
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("environment lock entries must be unique")
        return value

    @model_validator(mode="after")
    def health_checks_are_locked_to_declared_ports(self) -> EnvironmentLock:
        declared = {(item.host.casefold(), item.port) for item in self.ports}
        for check in self.health_checks:
            if check.kind == "http":
                assert check.url is not None
                parsed = urlsplit(str(check.url))
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                endpoint = (str(parsed.hostname).casefold(), port)
            else:
                assert check.host is not None and check.port is not None
                endpoint = (check.host.casefold(), check.port)
            if endpoint not in declared:
                raise ValueError("health check endpoint is absent from the locked port inventory")
        if not any(check.kind == "http" for check in self.health_checks):
            raise ValueError("real-host readiness requires an authenticated HTTP identity check")
        if self.attestation_secret_ref not in self.secret_refs:
            raise ValueError("environment attestation secret must be a declared secret reference")
        return self

    @field_validator("secret_refs")
    @classmethod
    def secrets_are_indirect_references(cls, value: list[str]) -> list[str]:
        if any(
            not item.startswith("secret:")
            or len(item) <= len("secret:")
            or any(character.isspace() for character in item)
            or "=" in item
            for item in value
        ):
            raise ValueError("environment secrets must use opaque secret: references")
        return value


class AllowedActionsPolicy(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    readable_host_objects: list[str] = Field(default_factory=list, max_length=500)
    writable_host_operations: list[str] = Field(default_factory=list, max_length=500)
    platform_actions: list[AllowedAction] = Field(min_length=1, max_length=100)
    network_hosts: list[str] = Field(default_factory=list, max_length=100)
    model_access: bool
    file_access: bool
    connector_access: bool
    permission_required_actions: list[str] = Field(default_factory=list, max_length=500)
    max_write_count: int = Field(ge=0, le=1_000_000)
    max_payload_bytes: int = Field(ge=1, le=100 * 1024 * 1024)
    compensation_actions: list[str] = Field(default_factory=list, max_length=500)
    prohibited_actions: list[str] = Field(min_length=5, max_length=100)
    validation_mode: Literal["real_host"]

    @field_validator(
        "readable_host_objects",
        "writable_host_operations",
        "network_hosts",
        "platform_actions",
        "permission_required_actions",
        "compensation_actions",
        "prohibited_actions",
    )
    @classmethod
    def values_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("allowed-actions values must be unique")
        return value

    @field_validator("prohibited_actions")
    @classmethod
    def mandatory_prohibitions_are_present(cls, value: list[str]) -> list[str]:
        missing = _MANDATORY_PROHIBITIONS - set(value)
        if missing:
            raise ValueError(f"mandatory prohibitions missing: {sorted(missing)}")
        return value

    @model_validator(mode="after")
    def host_policy_is_coherent(self) -> AllowedActionsPolicy:
        if not set(self.permission_required_actions).issubset(
            self.writable_host_operations
        ):
            raise ValueError(
                "permission-required actions must be writable host operations"
            )
        if (
            not self.connector_access
            and (
                self.readable_host_objects
                or self.writable_host_operations
                or self.network_hosts
                or self.permission_required_actions
                or self.compensation_actions
            )
        ):
            raise ValueError(
                "host object and operation policy requires connector_access"
            )
        if self.connector_access and not (
            self.readable_host_objects
            or self.writable_host_operations
            or self.compensation_actions
        ):
            raise ValueError(
                "connector_access requires an explicit host object or operation policy"
            )
        return self


class BudgetSpec(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    max_build_repair_turns: int = Field(ge=5, le=200)
    max_model_cost_usd: float = Field(gt=0, le=100_000, allow_inf_nan=False)
    assignment_wall_clock_seconds: int = Field(ge=1, le=7 * 24 * 60 * 60)
    max_platform_tool_calls: int = Field(ge=1, le=1_000)
    max_report_evidence_rounds: int = Field(ge=1, le=100)
    stable_hidden_runs: int = Field(ge=1, le=100)


class VerificationRuntimeDependency(StrictFrozenModel):
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=200)
    installed_file_count: int = Field(ge=1, le=100_000)
    installed_files_digest: Digest


class VerificationPolicyBundleManifest(StrictFrozenModel):
    """Content-addressed verifier/scanner/replay source and runtime policy."""

    schema_version: Literal["1.0"]
    entrypoint: Literal["agent_platform.independent_verifier"]
    python_implementation: str = Field(min_length=1, max_length=120)
    python_version: str = Field(min_length=1, max_length=120)
    python_executable_digest: Digest
    python_executable_size_bytes: int = Field(ge=1, le=MAX_ARCHIVE_FILE_BYTES)
    runtime_dependencies: list[VerificationRuntimeDependency] = Field(
        min_length=1,
        max_length=100,
    )
    protected_source_paths: list[RelativePath] = Field(
        min_length=1,
        max_length=200,
    )
    sources: list[FileDigestEntry] = Field(min_length=1, max_length=200)
    verification_process_digest: Digest

    @field_validator("runtime_dependencies")
    @classmethod
    def dependencies_are_canonical(
        cls,
        value: list[VerificationRuntimeDependency],
    ) -> list[VerificationRuntimeDependency]:
        identities = [item.name for item in value]
        if identities != sorted(set(identities)):
            raise ValueError(
                "verification runtime dependencies must be sorted and unique"
            )
        return value

    @field_validator("protected_source_paths")
    @classmethod
    def protected_paths_are_canonical(cls, value: list[str]) -> list[str]:
        normalized = [_normalize_relative_path(item) for item in value]
        if normalized != sorted(set(normalized)):
            raise ValueError(
                "verification protected source paths must be sorted and unique"
            )
        if normalized != sorted(DEVELOPER_TRUST_ROOT_PATHS):
            raise ValueError(
                "verification policy does not bind the complete developer trust root"
            )
        executable_trust_root = {
            f"platform/backend/src/{path}"
            for path in _VERIFICATION_BUNDLE_SOURCE_PATHS
        }
        if not executable_trust_root <= set(normalized):
            raise ValueError(
                "verification executable source is not protected from promotion"
            )
        return normalized

    @field_validator("sources")
    @classmethod
    def sources_are_canonical(
        cls,
        value: list[FileDigestEntry],
    ) -> list[FileDigestEntry]:
        paths = [item.path for item in value]
        if paths != sorted(_VERIFICATION_BUNDLE_SOURCE_PATHS):
            raise ValueError(
                "verification policy does not bind the exact executable source closure"
            )
        return value

    @model_validator(mode="after")
    def process_digest_matches_manifest(
        self,
    ) -> VerificationPolicyBundleManifest:
        expected = _digest_bytes(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"verification_process_digest"},
                    exclude_none=True,
                )
            )
        )
        if not hmac.compare_digest(expected, self.verification_process_digest):
            raise ValueError(
                "verification policy process digest does not match its manifest"
            )
        return self


class FrozenPackageRecord(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    public_summary_digest: Digest
    sealed_package_digest: Digest
    environment_lock_digest: Digest
    fixture_manifest_digest: Digest
    allowed_actions_digest: Digest
    budget_digest: Digest
    verification_process_digest: Digest
    immutable_files: list[FileDigestEntry] = Field(min_length=7, max_length=100_000)
    frozen_at: datetime

    @field_validator("frozen_at")
    @classmethod
    def frozen_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


@dataclass(frozen=True, slots=True)
class FrozenTaskPackage:
    root: Path
    record: FrozenPackageRecord
    task: TaskPackageSpec
    environment: EnvironmentLock
    fixtures: FixtureManifest
    allowed_actions: AllowedActionsPolicy
    budget: BudgetSpec


class HealthCheckResult(StrictFrozenModel):
    check_id: OpaqueReference
    kind: Literal["http", "tcp"]
    passed: bool
    observed_status: str = Field(min_length=1, max_length=500)
    duration_ms: int = Field(ge=0)
    evidence_digest: Digest
    attestation_challenge_digest: Digest | None = None
    identity_authenticated: bool
    checked_at: datetime
    provenance: Literal["real_host"]

    @field_validator("checked_at")
    @classmethod
    def checked_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def challenge_binding_matches_probe_kind(self) -> HealthCheckResult:
        if (self.kind == "http") != (self.attestation_challenge_digest is not None):
            raise ValueError("only HTTP health checks carry an attestation challenge")
        return self


class EnvironmentReady(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    public_summary_digest: Digest
    sealed_package_digest: Digest
    environment_lock_digest: Digest
    fixture_manifest_digest: Digest
    allowed_actions_digest: Digest
    budget_digest: Digest
    environment_instance_id: OpaqueReference
    started_at: datetime
    finished_at: datetime
    expires_at: datetime
    checks: list[HealthCheckResult] = Field(min_length=1, max_length=100)
    ready: Literal[True]
    provenance: Literal["real_host"]

    @field_validator("started_at", "finished_at", "expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def ready_window_and_checks_are_valid(self) -> EnvironmentReady:
        if not self.started_at <= self.finished_at < self.expires_at:
            raise ValueError("environment readiness timestamps are inconsistent")
        if not all(check.passed for check in self.checks):
            raise ValueError("environment-ready cannot contain a failed check")
        if len({check.check_id for check in self.checks}) != len(self.checks):
            raise ValueError("environment-ready checks must be unique")
        return self


class EnvironmentReadyRecord(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    ready_digest: Digest
    issued_at: datetime

    @field_validator("issued_at")
    @classmethod
    def issued_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class FormalAssignmentRecord(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    assignment_digest: Digest
    issued_at: datetime

    @field_validator("issued_at")
    @classmethod
    def issued_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class WorkspaceMountEntry(StrictFrozenModel):
    logical_source: str = Field(min_length=1, max_length=200)
    target_path: RelativePath
    digest: Digest
    size_bytes: int = Field(ge=0, le=MAX_ARCHIVE_FILE_BYTES)
    read_only: bool

    @field_validator("target_path")
    @classmethod
    def target_path_is_safe(cls, value: str) -> str:
        return _normalize_relative_path(value)


class WorkspaceMountManifest(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    role: WorkspaceRole
    run_id: OpaqueReference
    assignment_id: UUID
    public_summary_digest: Digest
    sealed_package_digest: Digest | None = None
    environment_ready_digest: Digest | None = None
    environment_instance_id: OpaqueReference | None = None
    archive_manifest_digest: Digest | None = None
    entries: list[WorkspaceMountEntry] = Field(min_length=1, max_length=100_000)
    denied_segments: list[str] = Field(min_length=1, max_length=100)
    writable_prefixes: list[RelativePath] = Field(default_factory=list, max_length=20)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("entries")
    @classmethod
    def mount_paths_are_unique(cls, value: list[WorkspaceMountEntry]) -> list[WorkspaceMountEntry]:
        _unique_paths([entry.target_path for entry in value])
        return value

    @field_validator("writable_prefixes")
    @classmethod
    def writable_prefixes_are_safe(cls, value: list[str]) -> list[str]:
        return _unique_paths(value)

    @model_validator(mode="after")
    def role_bindings_are_complete(self) -> WorkspaceMountManifest:
        if self.role is WorkspaceRole.lilies:
            if (
                self.sealed_package_digest is not None
                or self.environment_ready_digest is None
                or self.environment_instance_id is None
                or self.archive_manifest_digest is not None
            ):
                raise ValueError("Lilies workspace requires exact environment readiness only")
        elif self.role is WorkspaceRole.developer:
            if (
                self.sealed_package_digest is not None
                or self.environment_ready_digest is not None
                or self.environment_instance_id is not None
                or self.archive_manifest_digest is not None
            ):
                raise ValueError(
                    "developer workspace cannot carry environment or archive authority"
                )
        elif (
            self.sealed_package_digest is None
            or self.environment_ready_digest is not None
            or self.environment_instance_id is not None
            or self.archive_manifest_digest is None
        ):
            raise ValueError(
                "verifier workspace requires sealed package and frozen archive digests"
            )
        return self


class WorkspaceMountRecord(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    role: WorkspaceRole
    run_id: OpaqueReference
    assignment_id: UUID
    manifest_digest: Digest
    policy_digest: Digest
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class ArchiveClaimBinding(StrictFrozenModel):
    claim_id: UUID
    assignment_id: UUID
    application_id: UUID
    draft_revision: int = Field(ge=0)
    content_hash: Digest
    published_version: int | None = Field(default=None, ge=1)
    test_run_ids: list[OpaqueReference] = Field(min_length=1, max_length=500)
    business_run_ids: list[OpaqueReference] = Field(min_length=1, max_length=500)
    artifact_digests: list[Digest] = Field(default_factory=list, max_length=500)
    host_receipt_digests: list[Digest] = Field(default_factory=list, max_length=500)
    resolved_report_ids: list[UUID] = Field(default_factory=list, max_length=500)
    remaining_limits: list[str] = Field(default_factory=list, max_length=100)

    @field_validator(
        "test_run_ids",
        "business_run_ids",
        "artifact_digests",
        "host_receipt_digests",
        "resolved_report_ids",
        "remaining_limits",
    )
    @classmethod
    def binding_values_are_unique(cls, value: list[Any]) -> list[Any]:
        if len(value) != len(set(value)):
            raise ValueError("claim binding values must be unique")
        return value


class ArchivedMessageRecord(StrictFrozenModel):
    schema_version: Literal["1.0"]
    seq: int = Field(strict=True, ge=1)
    message_id: OpaqueReference
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    session_id: UUID
    kind: Literal[
        "assignment.accepted",
        "user.message",
        "lilies.message",
        "tool.call",
        "tool.result",
        "context.summary",
        "daemon.event",
    ]
    payload: dict[str, Any]
    payload_digest: Digest

    @model_validator(mode="after")
    def payload_digest_matches(self) -> ArchivedMessageRecord:
        expected = _digest_bytes(_canonical_json(self.payload))
        if not hmac.compare_digest(expected, self.payload_digest):
            raise ValueError("archived message payload digest does not match")
        return self


class ArchivedPlatformOutcome(StrictFrozenModel):
    application_id: UUID
    draft_revision: int = Field(ge=0)
    content_hash: Digest
    published_version: int | None = Field(default=None, ge=1)
    test_run_ids: list[OpaqueReference] = Field(min_length=1, max_length=500)
    business_run_ids: list[OpaqueReference] = Field(min_length=1, max_length=500)
    artifact_digests: list[Digest] = Field(default_factory=list, max_length=500)
    host_receipt_digests: list[Digest] = Field(default_factory=list, max_length=500)

    @field_validator(
        "test_run_ids",
        "business_run_ids",
        "artifact_digests",
        "host_receipt_digests",
    )
    @classmethod
    def outcome_values_are_unique(cls, value: list[Any]) -> list[Any]:
        if len(value) != len(set(value)):
            raise ValueError("platform outcome values must be unique")
        return value


class ArchivedPlatformEventRecord(StrictFrozenModel):
    schema_version: Literal["1.0"]
    seq: int = Field(strict=True, ge=1)
    event_id: OpaqueReference
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    application_id: UUID
    kind: Literal["run.started", "formal_run.snapshot"]
    payload: dict[str, Any]
    payload_digest: Digest
    outcome: ArchivedPlatformOutcome | None = None

    @model_validator(mode="after")
    def event_shape_is_exact(self) -> ArchivedPlatformEventRecord:
        expected = _digest_bytes(_canonical_json(self.payload))
        if not hmac.compare_digest(expected, self.payload_digest):
            raise ValueError("archived platform-event payload digest does not match")
        if self.kind == "formal_run.snapshot":
            if self.outcome is None or self.outcome.application_id != self.application_id:
                raise ValueError("formal run snapshot requires its exact application outcome")
        elif self.outcome is not None:
            raise ValueError("only a formal run snapshot may carry an outcome")
        return self


class ArchivedCollaborationRecord(StrictFrozenModel):
    schema_version: Literal["1.0"]
    seq: int = Field(strict=True, ge=1)
    event_id: OpaqueReference
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    channel_id: UUID
    kind: Literal["message", "report.resolved", "claim.prepared"]
    message_id: UUID | None = None
    report_id: UUID | None = None
    claim_binding: ArchiveClaimBinding | None = None
    payload: dict[str, Any]
    payload_digest: Digest

    @model_validator(mode="after")
    def collaboration_shape_is_exact(self) -> ArchivedCollaborationRecord:
        expected = _digest_bytes(_canonical_json(self.payload))
        if not hmac.compare_digest(expected, self.payload_digest):
            raise ValueError("archived collaboration payload digest does not match")
        if self.kind == "message":
            if (
                self.message_id is None
                or self.report_id is not None
                or self.claim_binding is not None
            ):
                raise ValueError("message requires only the collaboration message identity")
        elif self.kind == "claim.prepared":
            if (
                self.claim_binding is None
                or self.message_id is not None
                or self.report_id is not None
            ):
                raise ValueError("claim.prepared requires only the frozen claim binding")
        elif (
            self.report_id is None
            or self.message_id is not None
            or self.claim_binding is not None
        ):
            raise ValueError("report.resolved requires only the resolved report identity")
        return self


class ArchivedRunResult(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    application_id: UUID
    archive_status: ArchiveStatus
    validation_mode: ValidationMode
    business_status: OpaqueReference
    business_run_ids: list[OpaqueReference] = Field(min_length=1, max_length=500)
    artifact_digests: list[Digest] = Field(default_factory=list, max_length=500)
    host_receipt_digests: list[Digest] = Field(default_factory=list, max_length=500)
    remaining_limits: list[str] = Field(default_factory=list, max_length=100)
    summary: str = Field(min_length=1, max_length=20_000)

    @field_validator(
        "business_run_ids",
        "artifact_digests",
        "host_receipt_digests",
        "remaining_limits",
    )
    @classmethod
    def result_values_are_unique(cls, value: list[Any]) -> list[Any]:
        if len(value) != len(set(value)):
            raise ValueError("archived result values must be unique")
        return value


class ArchivedFormalReservation(StrictFrozenModel):
    """Terminal identity for a formal run never delivered to the daemon."""

    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    application_id: UUID
    build_id: UUID
    session_id: UUID
    connection_id: UUID
    channel_id: UUID
    environment_instance_id: OpaqueReference
    idempotency_key: str = Field(min_length=16, max_length=128)
    request_digest: Digest
    request_payload_digest: Digest
    preparation_state: Literal[
        "request_reserved",
        "manager_prepared",
    ]
    manager_prepared_assignment_digest: Digest | None
    daemon_assignment_delivery: Literal["not_started"]
    daemon_session_creation_started_at: None
    daemon_status: None
    relay_cursor: Literal[0]
    ack_cursor: Literal[0]
    daemon_event_count: Literal[0]
    credential_ref: None
    collaboration_credential_ref: None
    formal_workspace_receipt_json: None
    phase: Literal["cancelled", "error"]
    status: Literal["cancelled", "failed"]
    desired_state: Literal["active", "cancelled"]
    terminal_events_drained_at: datetime
    last_error_code: str | None = Field(default=None, max_length=500)
    last_error_message: str | None = Field(default=None, max_length=2_000)
    preflight_evidence: list[FileDigestEntry] = Field(
        default_factory=list,
        max_length=10_000,
    )
    environment_ready_digest: Digest | None = None
    workspace_mount_digest: Digest | None = None

    @field_validator("terminal_events_drained_at")
    @classmethod
    def terminal_events_drained_at_is_utc(
        cls,
        value: datetime,
    ) -> datetime:
        return _require_utc(value)

    @field_validator("preflight_evidence")
    @classmethod
    def preflight_evidence_is_exact(
        cls,
        value: list[FileDigestEntry],
    ) -> list[FileDigestEntry]:
        paths = [item.path for item in value]
        digests = [item.digest for item in value]
        if len(paths) != len(set(paths)) or len(digests) != len(
            set(digests)
        ):
            raise ValueError("reserved formal preflight evidence must be unique")
        if any(
            not path.startswith("environment-preflight/")
            for path in paths
        ):
            raise ValueError("reserved formal preflight evidence has another archive prefix")
        return value

    @model_validator(mode="after")
    def reserved_identity_is_consistent(self) -> ArchivedFormalReservation:
        if self.run_id != f"formal-run:{self.build_id}":
            raise ValueError("reserved formal run ID differs from its build identity")
        terminal_state = (
            self.phase,
            self.status,
            self.desired_state,
        )
        if terminal_state not in {
            ("error", "failed", "active"),
            ("cancelled", "cancelled", "cancelled"),
        }:
            raise ValueError(
                "reserved formal terminal state is not an exact error or "
                "cancellation binding"
            )
        if (
            self.preparation_state == "manager_prepared"
        ) != (self.manager_prepared_assignment_digest is not None):
            raise ValueError(
                "reserved formal preparation state does not match its "
                "manager-prepared assignment digest"
            )
        if self.workspace_mount_digest is not None and self.environment_ready_digest is None:
            raise ValueError("reserved formal workspace has no readiness binding")
        return self


class ArchivedPreassignmentScanRecord(StrictFrozenModel):
    """Honest scanner result before any assignment reached the daemon."""

    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    session_id: UUID
    application_id: UUID
    channel_id: UUID
    scanner_applicable: Literal[False]
    verdict: Literal["inconclusive"]
    reason: Literal[
        "build_assignment_not_issued",
        "assignment_not_delivered_to_daemon",
    ]
    input_bindings: list[FileDigestEntry] = Field(
        min_length=7,
        max_length=20_000,
    )
    scan_digest: Digest
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("input_bindings")
    @classmethod
    def input_bindings_are_exact(
        cls,
        value: list[FileDigestEntry],
    ) -> list[FileDigestEntry]:
        paths = [item.path for item in value]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("pre-assignment scan inputs must be unique and sorted")
        return value

    @model_validator(mode="after")
    def scan_digest_matches(self) -> ArchivedPreassignmentScanRecord:
        expected = _digest_bytes(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"scan_digest"},
                    exclude_none=True,
                )
            )
        )
        if not hmac.compare_digest(expected, self.scan_digest):
            raise ValueError("pre-assignment scanner digest changed")
        return self


class RunArchiveManifest(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    source_status: ArchiveStatus
    status: ArchiveStatus
    validation_mode: ValidationMode
    public_summary_digest: Digest
    sealed_package_digest: Digest
    verification_process_digest: Digest
    environment_ready_digest: Digest | None = None
    workspace_mount_digest: Digest | None = None
    claim_binding: ArchiveClaimBinding | None = None
    request_digest: Digest
    files: list[FileDigestEntry] = Field(min_length=1, max_length=100_000)
    security_findings: list[str] = Field(default_factory=list, max_length=1_000)
    forbidden_assistance_findings: list[str] = Field(
        default_factory=list,
        max_length=1_000,
    )
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("files")
    @classmethod
    def archive_paths_are_unique(cls, value: list[FileDigestEntry]) -> list[FileDigestEntry]:
        _unique_paths([entry.path for entry in value])
        paths = [entry.path for entry in value]
        if paths != sorted(paths):
            raise ValueError("archive file entries must be sorted")
        return value

    @model_validator(mode="after")
    def successful_archive_is_claimable(self) -> RunArchiveManifest:
        if self.status == ArchiveStatus.succeeded:
            if (
                self.source_status is not ArchiveStatus.succeeded
                or self.validation_mode is not ValidationMode.real_host
                or self.environment_ready_digest is None
                or self.workspace_mount_digest is None
                or self.claim_binding is None
                or self.security_findings
                or self.forbidden_assistance_findings
            ):
                raise ValueError(
                    "successful archives require real health, mount, claim, and clean scans"
                )
        elif self.status is ArchiveStatus.invalid:
            if (
                self.source_status is ArchiveStatus.invalid
                or not (self.security_findings or self.forbidden_assistance_findings)
            ):
                raise ValueError(
                    "invalid archives require a non-invalid source status and a finding"
                )
        elif self.status is not self.source_status:
            raise ValueError("archive status changed without an invalidating finding")
        return self


class ArchiveIndexEntry(StrictFrozenModel):
    run_id: OpaqueReference
    status: ArchiveStatus
    manifest_digest: Digest
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class ArchiveIndex(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    sealed_package_digest: Digest
    runs: list[ArchiveIndexEntry] = Field(default_factory=list, max_length=100_000)

    @field_validator("runs")
    @classmethod
    def run_entries_are_append_ordered(
        cls, value: list[ArchiveIndexEntry]
    ) -> list[ArchiveIndexEntry]:
        run_ids = [entry.run_id for entry in value]
        digests = [entry.manifest_digest for entry in value]
        if len(run_ids) != len(set(run_ids)) or len(digests) != len(set(digests)):
            raise ValueError("archive index run IDs and digests must be unique")
        if any(
            later.created_at < earlier.created_at
            for earlier, later in zip(value, value[1:], strict=False)
        ):
            raise ValueError("archive index entries must preserve append order")
        return value


class PreflightFailureEvidence(StrictFrozenModel):
    schema_version: Literal["1.0"]
    task_id: TaskId
    revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    environment_instance_id: OpaqueReference
    attempt: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime
    checks: list[HealthCheckResult]
    ready: Literal[False]
    failure: str = Field(min_length=1, max_length=2_000)


def _default_health_probe(
    spec: HealthCheckSpec,
    *,
    attestation_challenge: str,
    attestation_secret: bytes,
) -> HealthCheckResult:
    started = time.monotonic()
    checked_at = datetime.now(timezone.utc)
    passed = False
    identity_authenticated = False
    observed = "probe_failed"
    evidence = b"probe_failed"
    try:
        if spec.kind == "http":
            assert spec.url is not None

            class _NoRedirect(HTTPRedirectHandler):
                def redirect_request(
                    self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
                ) -> None:
                    return None

            opener = build_opener(_NoRedirect)
            request = Request(
                str(spec.url),
                method="GET",
                headers={
                    "X-Lilies-Attestation-Challenge": attestation_challenge,
                },
            )
            with opener.open(request, timeout=spec.timeout_seconds) as response:
                body = response.read(MAX_CONTROL_FILE_BYTES + 1)
                if len(body) > MAX_CONTROL_FILE_BYTES:
                    raise TaskPackageSecurityError("health response exceeded evidence limit")
                status_code = int(response.status)
                body_digest = _digest_bytes(body)
                supplied_attestation = response.headers.get(
                    "X-Lilies-Environment-Attestation",
                    "",
                )
                expected_attestation = (
                    "sha256:"
                    + hmac.new(
                        attestation_secret,
                        attestation_challenge.encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()
                )
                identity_authenticated = hmac.compare_digest(
                    supplied_attestation,
                    expected_attestation,
                )
                passed = (
                    identity_authenticated
                    and status_code == spec.expected_status
                    and (
                        spec.expected_body_digest is None
                        or hmac.compare_digest(body_digest, spec.expected_body_digest)
                    )
                )
                observed = f"http:{status_code}:{body_digest}"
                evidence = _canonical_json(
                    {
                        "status": status_code,
                        "body_digest": body_digest,
                        "identity_attestation_digest": _digest_bytes(
                            supplied_attestation.encode("utf-8")
                        ),
                    }
                )
        else:
            assert spec.host is not None and spec.port is not None
            with socket.create_connection(
                (spec.host, spec.port),
                timeout=spec.timeout_seconds,
            ):
                passed = True
                observed = "tcp:connected"
                evidence = observed.encode()
    except HTTPError as error:
        observed = f"http:{error.code}"
        evidence = observed.encode()
    except (OSError, TimeoutError, URLError, TaskPackageError) as error:
        observed = f"{type(error).__name__}:probe_failed"
        evidence = observed.encode()
    duration_ms = max(0, round((time.monotonic() - started) * 1_000))
    return HealthCheckResult(
        check_id=spec.check_id,
        kind=spec.kind,
        passed=passed,
        observed_status=observed,
        duration_ms=duration_ms,
        evidence_digest=_digest_bytes(evidence),
        attestation_challenge_digest=(
            _digest_bytes(attestation_challenge.encode("utf-8"))
            if spec.kind == "http"
            else None
        ),
        identity_authenticated=identity_authenticated,
        checked_at=checked_at,
        provenance="real_host",
    )


def _iter_tree_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise TaskPackageSecurityError("root cannot be a symlink")
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            child = current_path / name
            if child.is_symlink():
                raise TaskPackageSecurityError("symlink directories are forbidden")
        for name in names:
            child = current_path / name
            metadata = child.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise TaskPackageSecurityError("only regular files are allowed")
            if metadata.st_nlink != 1:
                raise TaskPackageSecurityError("hard-linked files are forbidden")
            files.append(child)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _iter_developer_source_files(root: Path) -> list[Path]:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise TaskPackageSecurityError("developer source root must be a real directory")
    resolved_root = root.resolve(strict=True)
    root_device = resolved_root.stat(follow_symlinks=False).st_dev
    files: list[Path] = []
    total_bytes = 0
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        resolved_current = current_path.resolve(strict=True)
        if (
            resolved_current != resolved_root
            and (
                resolved_root not in resolved_current.parents
                or os.path.ismount(current_path)
                or current_path.stat(follow_symlinks=False).st_dev != root_device
            )
        ):
            raise TaskPackageSecurityError(
                "developer source cannot cross a filesystem mount boundary"
            )
        allowed_directories: list[str] = []
        for name in directories:
            child = current_path / name
            if name.casefold() in _DEVELOPER_SOURCE_EXCLUDED_IDENTITIES:
                continue
            if child.is_symlink():
                raise TaskPackageSecurityError(
                    "developer source cannot contain symlink directories"
                )
            metadata = child.stat(follow_symlinks=False)
            if os.path.ismount(child) or metadata.st_dev != root_device:
                raise TaskPackageSecurityError(
                    "developer source cannot cross a filesystem mount boundary"
                )
            allowed_directories.append(name)
        directories[:] = allowed_directories
        for name in names:
            identity = name.casefold()
            if (
                identity in _DEVELOPER_SOURCE_EXCLUDED_IDENTITIES
                or identity in _DEVELOPER_SOURCE_SECRET_IDENTITIES
                or identity == ".env"
                or identity.startswith(".env.")
                or identity.endswith(_DEVELOPER_SOURCE_SECRET_SUFFIXES)
            ):
                continue
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            if _contains_forbidden_workspace_segment(relative):
                continue
            metadata = child.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise TaskPackageSecurityError("developer source may contain only regular files")
            if (
                metadata.st_nlink != 1
                or metadata.st_dev != root_device
                or os.path.ismount(child)
            ):
                raise TaskPackageSecurityError("developer source cannot contain hard-linked files")
            total_bytes += metadata.st_size
            if len(files) >= MAX_DEVELOPER_SOURCE_FILES or total_bytes > MAX_DEVELOPER_SOURCE_BYTES:
                raise TaskPackageSecurityError(
                    "developer source snapshot exceeds its bounded inventory"
                )
            files.append(child)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _copy_regular(source: Path, destination: Path) -> FileDigestEntry:
    source_descriptor = _open_regular_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        destination_descriptor = os.open(destination, flags, 0o600)
    except BaseException:
        os.close(source_descriptor)
        raise
    digest = hashlib.sha256()
    total = 0
    try:
        while chunk := os.read(source_descriptor, 1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE_FILE_BYTES:
                raise TaskPackageSecurityError("copied file exceeds the archive limit")
            digest.update(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_descriptor, remaining)
                if written <= 0:
                    raise TaskPackageSecurityError(
                        "archive copy stopped before all bytes were written"
                    )
                remaining = remaining[written:]
        os.fsync(destination_descriptor)
        source_final = os.fstat(source_descriptor)
        if source_final.st_nlink != 1:
            raise TaskPackageSecurityError("source changed into a hard link during copy")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)
    return FileDigestEntry(
        path=destination.name,
        digest=f"sha256:{digest.hexdigest()}",
        size_bytes=total,
    )


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _read_archive_jsonl(
    path: Path,
    *,
    record_model: type[BaseModel],
) -> list[BaseModel]:
    payload = _read_bytes(path, limit=MAX_ARCHIVE_FILE_BYTES)
    lines = payload.splitlines()
    if not lines:
        raise TaskPackageConflict(f"archive JSONL cannot be empty: {path.name}")
    records: list[BaseModel] = []
    record_ids: set[str] = set()
    for expected_seq, line in enumerate(lines, start=1):
        if not line or not line.strip():
            raise TaskPackageConflict(f"archive JSONL contains a blank line: {path.name}")
        try:
            raw = _strict_json_loads(line)
            if not isinstance(raw, dict):
                raise ValueError("JSONL record must be an object")
            raw_seq = raw.get("seq")
            if type(raw_seq) is not int or raw_seq != expected_seq:
                raise TaskPackageConflict(f"archive event sequence is not contiguous: {path.name}")
            record = record_model.model_validate(raw)
        except TaskPackageConflict:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise TaskPackageConflict(
                f"archive JSONL has an invalid strict record: {path.name}"
            ) from error
        record_id = str(getattr(record, "message_id", None) or getattr(record, "event_id", None))
        if record_id in record_ids:
            raise TaskPackageConflict(f"archive JSONL record identity is duplicated: {path.name}")
        record_ids.add(record_id)
        records.append(record)
    return records


class TaskPackageManager:
    """Freeze formal task inputs and append immutable run archives.

    The frozen registry is outside each revision directory. Recomputing a
    package-local manifest therefore cannot authorize an altered old revision.
    """

    def __init__(
        self,
        state_root: Path,
        *,
        read_only: bool = False,
        environment_secret_resolver: Callable[[str], bytes] | None = None,
    ) -> None:
        lexical_root = Path(state_root)
        if lexical_root.is_symlink():
            raise TaskPackageSecurityError("task package state root cannot be a symlink")
        self.state_root = lexical_root.resolve()
        self.packages_root = self.state_root / "packages"
        self.registry_root = self.state_root / "registry"
        self.preflight_root = self.state_root / "preflight"
        self.locks_root = self.state_root / "locks"
        self.verification_bundles_root = (
            self.state_root / "verification-policy-bundles"
        )
        self._environment_secret_resolver = environment_secret_resolver
        paths = (
            self.state_root,
            self.packages_root,
            self.registry_root,
            self.preflight_root,
            self.locks_root,
            self.verification_bundles_root,
        )
        if read_only:
            if any(not path.is_dir() or path.is_symlink() for path in paths):
                raise TaskPackageSecurityError("read-only task package state is incomplete")
        else:
            for path in paths:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _revision_root(self, task_id: str, revision: int) -> Path:
        if not task_id or "/" in task_id or "\\" in task_id or ".." in task_id:
            raise TaskPackageSecurityError("unsafe task identity")
        return self.packages_root / task_id / str(revision)

    def _registry_path(self, task_id: str, revision: int) -> Path:
        return self.registry_root / task_id / f"{revision}.json"

    def has_frozen_revision(self, task_id: str, revision: int) -> bool:
        """Return whether the trusted registry contains this exact revision."""

        if revision < 1:
            return False
        self._revision_root(task_id, revision)
        path = self._registry_path(task_id, revision)
        return path.is_file() and not path.is_symlink()

    def _ready_path(self, task_id: str, revision: int, run_id: str) -> Path:
        normalized_run_id = TypeAdapter(OpaqueReference).validate_python(run_id)
        return (
            self.preflight_root
            / task_id
            / str(revision)
            / normalized_run_id
            / "environment-ready.json"
        )

    def _ready_registry_path(
        self,
        task_id: str,
        revision: int,
        run_id: str,
    ) -> Path:
        normalized_run_id = TypeAdapter(OpaqueReference).validate_python(run_id)
        return (
            self.registry_root / "readiness" / task_id / str(revision) / f"{normalized_run_id}.json"
        )

    def _archive_lock_path(self, task_id: str, revision: int) -> Path:
        return self.locks_root / "archives" / task_id / f"{revision}.lock"

    def _workspace_registry_path(self, manifest_digest: str) -> Path:
        digest = TypeAdapter(Digest).validate_python(manifest_digest)
        return self.registry_root / "workspaces" / f"{digest.removeprefix('sha256:')}.json"

    def _formal_assignment_registry_path(self, assignment_id: UUID) -> Path:
        return self.registry_root / "formal-assignments" / f"{assignment_id}.json"

    def _verification_bundle_root(self, process_digest: str) -> Path:
        digest = TypeAdapter(Digest).validate_python(process_digest)
        return (
            self.verification_bundles_root
            / digest.removeprefix("sha256:")
        )

    @staticmethod
    def _verification_source_root() -> Path:
        source_root = Path(__file__).resolve().parent.parent
        package_root = source_root / "agent_platform"
        if (
            source_root.is_symlink()
            or package_root.is_symlink()
            or not package_root.is_dir()
        ):
            raise TaskPackageSecurityError(
                "verification source root is unavailable"
            )
        return source_root

    @staticmethod
    def _verification_runtime_dependencies(
    ) -> list[VerificationRuntimeDependency]:
        dependencies: list[VerificationRuntimeDependency] = []
        for name in sorted(_VERIFICATION_RUNTIME_DISTRIBUTIONS):
            try:
                distribution = importlib.metadata.distribution(name)
            except importlib.metadata.PackageNotFoundError as error:
                raise TaskPackageSecurityError(
                    "verification runtime dependency is unavailable"
                ) from error
            files = distribution.files
            if files is None:
                raise TaskPackageSecurityError(
                    "verification dependency has no installed file inventory"
                )
            normalized_paths = [str(path).replace("\\", "/") for path in files]
            if (
                normalized_paths != sorted(set(normalized_paths))
                or any(
                    not path
                    or path.startswith("/")
                    or "\x00" in path
                    or any(
                        part in {"", ".", ".."}
                        for part in PurePosixPath(path).parts
                    )
                    for path in normalized_paths
                )
                or len(
                    [
                        path
                        for path in normalized_paths
                        if path.endswith(".dist-info/RECORD")
                    ]
                )
                != 1
            ):
                raise TaskPackageSecurityError(
                    "verification dependency RECORD inventory is not canonical"
                )
            inventory: list[dict[str, Any]] = []
            for relative in normalized_paths:
                installed = Path(
                    distribution.locate_file(PurePosixPath(relative))
                )
                digest, size = _digest_file(
                    installed,
                    limit=MAX_ARCHIVE_FILE_BYTES,
                )
                inventory.append(
                    {
                        "path": relative,
                        "digest": digest,
                        "size_bytes": size,
                    }
                )
            dependencies.append(
                VerificationRuntimeDependency(
                    name=name,
                    version=distribution.version,
                    installed_file_count=len(inventory),
                    installed_files_digest=_digest_bytes(
                        _canonical_json(
                            {
                                "schema_version": "1.0",
                                "distribution": name,
                                "files": inventory,
                            }
                        )
                    ),
                )
            )
        return dependencies

    @staticmethod
    def _verification_python_executable() -> FileDigestEntry:
        executable = Path(sys.executable).resolve(strict=True)
        digest, size = _digest_file(
            executable,
            limit=MAX_ARCHIVE_FILE_BYTES,
        )
        return FileDigestEntry(
            path="python-executable",
            digest=digest,
            size_bytes=size,
        )

    def _verification_source_entries(self) -> list[FileDigestEntry]:
        source_root = self._verification_source_root()
        entries: list[FileDigestEntry] = []
        for relative in sorted(_VERIFICATION_BUNDLE_SOURCE_PATHS):
            source = _resolved_child(source_root, relative)
            digest, size = _digest_file(
                source,
                limit=MAX_ARCHIVE_FILE_BYTES,
            )
            entries.append(
                FileDigestEntry(
                    path=relative,
                    digest=digest,
                    size_bytes=size,
                )
            )
        return entries

    def freeze_verification_policy_bundle(
        self,
    ) -> VerificationPolicyBundleManifest:
        """Freeze the executable verifier policy before issuing an assignment."""

        entries = self._verification_source_entries()
        python_executable = self._verification_python_executable()
        manifest_payload = {
            "schema_version": "1.0",
            "entrypoint": "agent_platform.independent_verifier",
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "python_executable_digest": python_executable.digest,
            "python_executable_size_bytes": python_executable.size_bytes,
            "runtime_dependencies": [
                item.model_dump(mode="json")
                for item in self._verification_runtime_dependencies()
            ],
            "protected_source_paths": sorted(DEVELOPER_TRUST_ROOT_PATHS),
            "sources": [
                item.model_dump(mode="json")
                for item in entries
            ],
        }
        manifest = VerificationPolicyBundleManifest(
            **manifest_payload,
            verification_process_digest=_digest_bytes(
                _canonical_json(manifest_payload)
            ),
        )
        target = self._verification_bundle_root(
            manifest.verification_process_digest
        )
        if target.exists() or target.is_symlink():
            _root, existing = self.load_verification_policy_bundle(
                manifest.verification_process_digest
            )
            if existing != manifest:
                raise TaskPackageConflict(
                    "verification process digest is reused by another policy"
                )
            return existing

        temporary = Path(
            tempfile.mkdtemp(
                prefix=".verification-policy-",
                dir=self.verification_bundles_root,
            )
        )
        try:
            source_root = self._verification_source_root()
            for entry in entries:
                copied = _copy_regular(
                    _resolved_child(source_root, entry.path),
                    _resolved_child(temporary / "src", entry.path),
                )
                if (
                    not hmac.compare_digest(copied.digest, entry.digest)
                    or copied.size_bytes != entry.size_bytes
                ):
                    raise TaskPackageConflict(
                        "verification source changed while its policy was frozen"
                    )
            _atomic_write(
                temporary / VERIFICATION_POLICY_MANIFEST_FILE,
                _canonical_json(manifest),
                mode=0o400,
            )
            for path in _iter_tree_files(temporary):
                os.chmod(path, 0o400)
            try:
                os.rename(temporary, target)
            except OSError:
                if not target.exists() or target.is_symlink():
                    raise
                _root, existing = self.load_verification_policy_bundle(
                    manifest.verification_process_digest
                )
                if existing != manifest:
                    raise TaskPackageConflict(
                        "verification process digest raced with another policy"
                    )
            else:
                for directory in sorted(
                    (path for path in target.rglob("*") if path.is_dir()),
                    reverse=True,
                ):
                    os.chmod(directory, 0o500)
                os.chmod(target, 0o500)
            return manifest
        finally:
            if temporary.exists():
                os.chmod(temporary, 0o700)
                for directory in (
                    path
                    for path in temporary.rglob("*")
                    if path.is_dir()
                ):
                    os.chmod(directory, 0o700)
                shutil.rmtree(temporary)

    def load_verification_policy_bundle(
        self,
        process_digest: str,
    ) -> tuple[Path, VerificationPolicyBundleManifest]:
        """Validate and resolve one retained content-addressed verifier bundle."""

        target = self._verification_bundle_root(process_digest)
        if (
            target.is_symlink()
            or not target.is_dir()
            or stat.S_IMODE(
                target.stat(follow_symlinks=False).st_mode
            )
            != 0o500
        ):
            raise TaskPackageConflict(
                "verification policy bundle root changed"
            )
        manifest_path = target / VERIFICATION_POLICY_MANIFEST_FILE
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or stat.S_IMODE(
                manifest_path.stat(follow_symlinks=False).st_mode
            )
            != 0o400
        ):
            raise TaskPackageConflict(
                "verification policy manifest changed"
            )
        payload = _read_bytes(manifest_path, limit=MAX_CONTROL_FILE_BYTES)
        try:
            manifest = VerificationPolicyBundleManifest.model_validate_json(
                payload
            )
        except ValueError as error:
            raise TaskPackageConflict(
                "verification policy manifest is invalid"
            ) from error
        if (
            not hmac.compare_digest(payload, _canonical_json(manifest))
            or not hmac.compare_digest(
                manifest.verification_process_digest,
                process_digest,
            )
            or target.name
            != process_digest.removeprefix("sha256:")
        ):
            raise TaskPackageConflict(
                "verification policy manifest identity changed"
            )
        if (
            manifest.python_implementation
            != platform.python_implementation()
            or manifest.python_version != platform.python_version()
            or manifest.python_executable_digest
            != self._verification_python_executable().digest
            or manifest.python_executable_size_bytes
            != self._verification_python_executable().size_bytes
            or manifest.runtime_dependencies
            != self._verification_runtime_dependencies()
        ):
            raise TaskPackageConflict(
                "verification runtime differs from the frozen policy"
            )
        source_root = target / "src"
        declared = {entry.path: entry for entry in manifest.sources}
        expected_bundle_files = {
            VERIFICATION_POLICY_MANIFEST_FILE,
            *(f"src/{path}" for path in declared),
        }
        actual_bundle_files = {
            path.relative_to(target).as_posix()
            for path in _iter_tree_files(target)
        }
        if actual_bundle_files != expected_bundle_files:
            raise TaskPackageConflict(
                "verification policy bundle file inventory changed"
            )
        actual: set[str] = set()
        for path in _iter_tree_files(source_root):
            relative = path.relative_to(source_root).as_posix()
            actual.add(relative)
            entry = declared.get(relative)
            if entry is None:
                raise TaskPackageConflict(
                    "verification policy bundle contains an undeclared source"
                )
            if stat.S_IMODE(
                path.stat(follow_symlinks=False).st_mode
            ) != 0o400:
                raise TaskPackageConflict(
                    "verification policy source permissions changed"
                )
            digest, size = _digest_file(
                path,
                limit=MAX_ARCHIVE_FILE_BYTES,
            )
            if (
                not hmac.compare_digest(digest, entry.digest)
                or size != entry.size_bytes
            ):
                raise TaskPackageConflict(
                    f"verification policy source changed: {relative}"
                )
        if actual != set(declared):
            raise TaskPackageConflict(
                "verification policy bundle is missing source files"
            )
        for directory in (
            path for path in target.rglob("*") if path.is_dir()
        ):
            if (
                directory.is_symlink()
                or stat.S_IMODE(
                    directory.stat(follow_symlinks=False).st_mode
                )
                != 0o500
            ):
                raise TaskPackageConflict(
                    "verification policy directory permissions changed"
                )
        return source_root, manifest

    def _load_source(
        self, source_root: Path
    ) -> tuple[
        TaskPackageSpec,
        EnvironmentLock,
        FixtureManifest,
        AllowedActionsPolicy,
        BudgetSpec,
        list[FileDigestEntry],
    ]:
        source_root = Path(source_root).resolve()
        if not source_root.is_dir():
            raise TaskPackageError("task package source is not a directory")
        names = {item.name for item in source_root.iterdir()}
        unknown = names - (_IMMUTABLE_TOP_LEVEL_FILES | _IMMUTABLE_TOP_LEVEL_DIRECTORIES)
        if unknown:
            raise TaskPackageError(f"unknown package root entries: {sorted(unknown)}")
        missing = _IMMUTABLE_TOP_LEVEL_FILES - names
        if missing:
            raise TaskPackageError(f"missing package files: {sorted(missing)}")
        fixtures_root = source_root / "fixtures"
        protected_root = source_root / "protected"
        if not fixtures_root.is_dir() or not protected_root.is_dir():
            raise TaskPackageError("fixtures and protected directories are required")
        oracle_path = protected_root / "oracle" / "oracle.json"
        if not oracle_path.is_file() or oracle_path.is_symlink():
            raise TaskPackageError(
                "protected/oracle/oracle.json is required for independent verification"
            )
        task = TaskPackageSpec.model_validate(_read_yaml(source_root / "task.yaml"))
        environment = EnvironmentLock.model_validate(_read_yaml(source_root / "environment.lock"))
        fixtures = FixtureManifest.model_validate(_read_json(fixtures_root / "manifest.json"))
        allowed = AllowedActionsPolicy.model_validate(
            _read_json(source_root / "allowed-actions.json")
        )
        budget = BudgetSpec.model_validate(_read_json(source_root / "budget.json"))
        identities = {
            (item.task_id, item.revision) for item in (task, environment, fixtures, allowed, budget)
        }
        if identities != {(task.task_id, task.revision)}:
            raise TaskPackageError("package control files disagree on task revision")
        try:
            oracle = OracleContract.model_validate(_read_json(oracle_path))
        except ValueError as error:
            raise TaskPackageError(
                "protected oracle identity or checks are invalid"
            ) from error
        if (
            oracle.task_id != task.task_id
            or oracle.revision != task.revision
            or oracle.validation_mode != "real_host"
        ):
            raise TaskPackageError("protected oracle identity or checks are invalid")
        requirement = _read_bytes(source_root / "requirement.md")
        if not requirement.strip():
            raise TaskPackageError("requirement.md cannot be empty")
        all_files = _iter_tree_files(source_root)
        immutable_files: list[FileDigestEntry] = []
        seen_identities: set[str] = set()
        for path in all_files:
            relative = path.relative_to(source_root).as_posix()
            if PurePosixPath(relative).parts[0] in _GENERATED_TOP_LEVEL:
                continue
            normalized = _normalize_relative_path(relative)
            identity = unicodedata.normalize("NFC", normalized).casefold()
            if identity in seen_identities:
                raise TaskPackageSecurityError("package paths collide after normalization")
            seen_identities.add(identity)
            if not normalized.startswith("protected/") and _contains_forbidden_workspace_segment(
                normalized
            ):
                raise TaskPackageSecurityError(
                    f"public task path uses a reserved segment: {normalized}"
                )
            digest, size = _digest_file(path)
            immutable_files.append(FileDigestEntry(path=normalized, digest=digest, size_bytes=size))
        fixture_actual = {
            item.path.removeprefix("fixtures/"): item
            for item in immutable_files
            if item.path.startswith("fixtures/public-inputs/")
        }
        fixture_declared = {item.path: item for item in fixtures.files}
        if set(fixture_actual) != set(fixture_declared):
            raise TaskPackageError("fixture manifest does not exactly cover public inputs")
        for path, declared in fixture_declared.items():
            actual = fixture_actual[path]
            if (
                not hmac.compare_digest(actual.digest, declared.digest)
                or actual.size_bytes != declared.size_bytes
            ):
                raise TaskPackageError(f"fixture digest or size drift: {path}")
        environment_fixture = {item.path: item for item in environment.fixture_files}
        if environment_fixture != fixture_declared:
            raise TaskPackageError("environment lock fixture inventory does not match")
        if task.source_projects != environment.source_projects:
            raise TaskPackageError("task and environment lock source projects do not match exactly")
        marker_file = protected_root / "leak-markers.json"
        leak_markers = [str(protected_root.resolve())]
        if marker_file.exists():
            marker_value = _read_json(marker_file)
            if not isinstance(marker_value, dict) or not isinstance(
                marker_value.get("markers"), list
            ):
                raise TaskPackageError("protected leak-markers.json has invalid schema")
            for marker in marker_value["markers"]:
                if not isinstance(marker, str) or len(marker) < 8:
                    raise TaskPackageError("oracle leak markers must be strings of length >= 8")
                leak_markers.append(marker)
        protected_payloads = [
            _read_bytes(path, limit=MAX_ARCHIVE_FILE_BYTES)
            for path in all_files
            if path.relative_to(source_root).as_posix().startswith("protected/")
        ]
        protected_exact = {_digest_bytes(payload) for payload in protected_payloads}
        protected_canonical_json: set[str] = set()
        for payload in protected_payloads:
            try:
                protected_canonical_json.add(
                    _digest_bytes(_canonical_json(_strict_json_loads(payload)))
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
                TypeError,
            ):
                continue
        for entry in immutable_files:
            if entry.path.startswith("protected/"):
                continue
            payload = _read_bytes(
                _resolved_child(source_root, entry.path),
                limit=MAX_ARCHIVE_FILE_BYTES,
            )
            canonical_digest: str | None = None
            try:
                canonical_digest = _digest_bytes(_canonical_json(_strict_json_loads(payload)))
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
                TypeError,
            ):
                pass
            if (
                _digest_bytes(payload) in protected_exact
                or (canonical_digest is not None and canonical_digest in protected_canonical_json)
                or self._looks_like_oracle_contract(payload)
                or self._contains_protected_payload(payload, protected_payloads)
                or any(
                    unicodedata.normalize("NFKC", marker) in unicodedata.normalize("NFKC", decoded)
                    for decoded in _decoded_payload_strings(payload)
                    for marker in leak_markers
                )
            ):
                raise TaskPackageSecurityError(
                    f"public task file contains protected oracle material: {entry.path}"
                )
        environment_digest, _ = _digest_file(source_root / "environment.lock")
        fixture_digest, _ = _digest_file(fixtures_root / "manifest.json")
        if not hmac.compare_digest(task.environment_lock_digest, environment_digest):
            raise TaskPackageError("task environment lock digest does not match")
        if not hmac.compare_digest(task.fixture_manifest_digest, fixture_digest):
            raise TaskPackageError("task fixture manifest digest does not match")
        return task, environment, fixtures, allowed, budget, immutable_files

    @staticmethod
    def _aggregate_digest(
        entries: Sequence[FileDigestEntry],
        *,
        include_protected: bool,
    ) -> str:
        selected = [
            entry.model_dump(mode="json")
            for entry in entries
            if include_protected or not entry.path.startswith("protected/")
        ]
        return _digest_bytes(_canonical_json({"schema_version": "1.0", "entries": selected}))

    def freeze_revision(self, source_root: Path) -> FrozenTaskPackage:
        (
            task,
            environment,
            fixtures,
            allowed,
            budget,
            entries,
        ) = self._load_source(source_root)
        public_digest = self._aggregate_digest(entries, include_protected=False)
        sealed_digest = self._aggregate_digest(entries, include_protected=True)
        allowed_digest, _ = _digest_file(Path(source_root) / "allowed-actions.json")
        budget_digest, _ = _digest_file(Path(source_root) / "budget.json")
        verification_policy = self.freeze_verification_policy_bundle()
        record = FrozenPackageRecord(
            schema_version="1.0",
            task_id=task.task_id,
            revision=task.revision,
            public_summary_digest=public_digest,
            sealed_package_digest=sealed_digest,
            environment_lock_digest=task.environment_lock_digest,
            fixture_manifest_digest=task.fixture_manifest_digest,
            allowed_actions_digest=allowed_digest,
            budget_digest=budget_digest,
            verification_process_digest=(
                verification_policy.verification_process_digest
            ),
            immutable_files=entries,
            frozen_at=datetime.now(timezone.utc),
        )
        registry_path = self._registry_path(task.task_id, task.revision)
        target = self._revision_root(task.task_id, task.revision)
        if registry_path.exists():
            existing = FrozenPackageRecord.model_validate(_read_json(registry_path))
            if not hmac.compare_digest(
                existing.sealed_package_digest, record.sealed_package_digest
            ):
                raise TaskPackageConflict(
                    "an immutable task revision already exists with another digest"
                )
            return self.load_frozen(task.task_id, task.revision)
        if task.revision > 1:
            parent = self.load_frozen(task.task_id, task.revision - 1)
            if hmac.compare_digest(
                parent.record.sealed_package_digest,
                record.sealed_package_digest,
            ):
                raise TaskPackageConflict("a package amendment must change frozen content")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(prefix=f".{task.revision}.", dir=target.parent))
        try:
            source = Path(source_root).resolve()
            for entry in entries:
                copied = _copy_regular(
                    _resolved_child(source, entry.path),
                    _resolved_child(temporary, entry.path),
                )
                if (
                    not hmac.compare_digest(copied.digest, entry.digest)
                    or copied.size_bytes != entry.size_bytes
                ):
                    raise TaskPackageSecurityError("source changed while freezing")
            (temporary / "runs").mkdir(mode=0o700)
            index = ArchiveIndex(
                schema_version="1.0",
                task_id=task.task_id,
                revision=task.revision,
                sealed_package_digest=sealed_digest,
                runs=[],
            )
            _atomic_write(
                temporary / "archive-manifest.json",
                _canonical_json(index),
            )
            for entry in entries:
                os.chmod(_resolved_child(temporary, entry.path), 0o400)
            for directory in sorted(
                (path for path in temporary.rglob("*") if path.is_dir()),
                reverse=True,
            ):
                if directory.name != "runs":
                    os.chmod(directory, 0o500)
            if target.exists():
                raise TaskPackageConflict("task revision directory already exists")
            os.replace(temporary, target)
            registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(registry_path, flags, 0o400)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_canonical_json(record))
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return self.load_frozen(task.task_id, task.revision)

    def load_frozen(
        self,
        task_id: str,
        revision: int,
        *,
        expected_public_digest: str | None = None,
        expected_sealed_digest: str | None = None,
    ) -> FrozenTaskPackage:
        registry_path = self._registry_path(task_id, revision)
        if not registry_path.exists():
            raise TaskPackageError("frozen task revision is not registered")
        record = FrozenPackageRecord.model_validate(_read_json(registry_path))
        if record.task_id != task_id or record.revision != revision:
            raise TaskPackageSecurityError("registry identity mismatch")
        if expected_public_digest is not None and not hmac.compare_digest(
            record.public_summary_digest, expected_public_digest
        ):
            raise TaskPackageConflict("public task digest mismatch")
        if expected_sealed_digest is not None and not hmac.compare_digest(
            record.sealed_package_digest, expected_sealed_digest
        ):
            raise TaskPackageConflict("sealed task digest mismatch")
        root = self._revision_root(task_id, revision)
        actual_entries: list[FileDigestEntry] = []
        for entry in record.immutable_files:
            frozen_path = _resolved_child(root, entry.path)
            if stat.S_IMODE(frozen_path.lstat().st_mode) != 0o400:
                raise TaskPackageConflict(
                    f"frozen task file drift: permissions changed for {entry.path}"
                )
            digest, size = _digest_file(frozen_path)
            if not hmac.compare_digest(digest, entry.digest) or size != entry.size_bytes:
                raise TaskPackageConflict(f"frozen task file drift: {entry.path}")
            actual_entries.append(FileDigestEntry(path=entry.path, digest=digest, size_bytes=size))
        known = {entry.path for entry in record.immutable_files}
        for path in _iter_tree_files(root):
            relative = path.relative_to(root).as_posix()
            if relative == "archive-manifest.json" or relative.startswith("runs/"):
                continue
            if relative not in known:
                raise TaskPackageConflict(f"unexpected frozen task file: {relative}")
        public = self._aggregate_digest(actual_entries, include_protected=False)
        sealed = self._aggregate_digest(actual_entries, include_protected=True)
        if not hmac.compare_digest(public, record.public_summary_digest) or not hmac.compare_digest(
            sealed, record.sealed_package_digest
        ):
            raise TaskPackageConflict("frozen task aggregate digest drift")
        self.load_verification_policy_bundle(
            record.verification_process_digest
        )
        task = TaskPackageSpec.model_validate(_read_yaml(root / "task.yaml"))
        environment = EnvironmentLock.model_validate(_read_yaml(root / "environment.lock"))
        fixtures = FixtureManifest.model_validate(_read_json(root / "fixtures/manifest.json"))
        allowed = AllowedActionsPolicy.model_validate(_read_json(root / "allowed-actions.json"))
        budget = BudgetSpec.model_validate(_read_json(root / "budget.json"))
        return FrozenTaskPackage(
            root=root,
            record=record,
            task=task,
            environment=environment,
            fixtures=fixtures,
            allowed_actions=allowed,
            budget=budget,
        )

    def run_environment_preflight(
        self,
        package: FrozenTaskPackage,
        *,
        run_id: str,
        assignment_id: UUID,
        environment_instance_id: str,
        ttl_seconds: int = 900,
    ) -> tuple[Path, EnvironmentReady]:
        package = self.load_frozen(
            package.task.task_id,
            package.task.revision,
            expected_sealed_digest=package.record.sealed_package_digest,
        )
        if not 60 <= ttl_seconds <= 24 * 60 * 60:
            raise ValueError("environment readiness TTL must be 60 seconds to 24 hours")
        run_id = TypeAdapter(OpaqueReference).validate_python(run_id)
        target = self._ready_path(
            package.task.task_id,
            package.task.revision,
            run_id,
        )
        preflight_dir = target.parent
        preflight_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            existing, _ = self.require_environment_ready(
                package,
                target,
                run_id=run_id,
                assignment_id=assignment_id,
            )
            if existing.environment_instance_id != environment_instance_id:
                raise TaskPackageConflict(
                    "environment-ready identity already binds another instance"
                )
            return target, existing
        failure_paths = sorted(
            preflight_dir.glob("environment-preflight*.json"),
            key=lambda path: path.name,
        )
        for failure_path in failure_paths:
            failure = PreflightFailureEvidence.model_validate(_read_json(failure_path))
            if (
                failure.task_id != package.task.task_id
                or failure.revision != package.task.revision
                or failure.run_id != run_id
                or failure.assignment_id != assignment_id
                or failure.environment_instance_id != environment_instance_id
            ):
                raise TaskPackageConflict(
                    "environment preflight retry changed its frozen run binding"
                )
        attempt = len(failure_paths) + 1
        failure_target = preflight_dir / (
            "environment-preflight.json"
            if attempt == 1
            else f"environment-preflight-attempt-{attempt:04d}.json"
        )
        started_at = datetime.now(timezone.utc)

        def reject_preflight(
            failure_message: str,
            *,
            checks: list[HealthCheckResult] | None = None,
        ) -> None:
            failure = PreflightFailureEvidence(
                schema_version="1.0",
                task_id=package.task.task_id,
                revision=package.task.revision,
                run_id=run_id,
                assignment_id=assignment_id,
                environment_instance_id=environment_instance_id,
                attempt=attempt,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                checks=list(checks or []),
                ready=False,
                failure=failure_message,
            )
            _atomic_write(
                failure_target,
                _canonical_json(failure),
                mode=0o400,
            )

        resolver = self._environment_secret_resolver
        if resolver is None:
            reject_preflight("trusted environment attestation secret resolver is unavailable")
            raise TaskPackageNotReady(
                "formal environment preflight has no trusted attestation secret resolver"
            )
        try:
            attestation_secret = resolver(package.environment.attestation_secret_ref)
        except Exception as error:
            reject_preflight("trusted environment attestation secret is unavailable")
            raise TaskPackageNotReady(
                "formal environment attestation secret is unavailable"
            ) from error
        if not isinstance(attestation_secret, bytes) or len(attestation_secret) < 32:
            reject_preflight("trusted environment attestation secret is invalid")
            raise TaskPackageNotReady("formal environment attestation secret is invalid")
        results: list[HealthCheckResult] = []
        for spec in package.environment.health_checks:
            attestation_challenge = _digest_bytes(
                _canonical_json(
                    {
                        "schema_version": "1.0",
                        "task_id": package.task.task_id,
                        "revision": package.task.revision,
                        "run_id": run_id,
                        "assignment_id": str(assignment_id),
                        "environment_instance_id": environment_instance_id,
                        "environment_lock_digest": package.record.environment_lock_digest,
                        "check_id": spec.check_id,
                        "attempt": attempt,
                        "nonce": secrets.token_hex(32),
                    }
                )
            )
            results.append(
                _default_health_probe(
                    spec,
                    attestation_challenge=attestation_challenge,
                    attestation_secret=attestation_secret,
                )
            )
        expected_ids = [spec.check_id for spec in package.environment.health_checks]
        actual_ids = [result.check_id for result in results]
        finished_at = datetime.now(timezone.utc)
        if (
            actual_ids != expected_ids
            or any(not result.passed or result.provenance != "real_host" for result in results)
            or not any(result.identity_authenticated for result in results)
        ):
            reject_preflight(
                "one or more mandatory real-host health checks failed",
                checks=results,
            )
            raise TaskPackageNotReady(
                "formal environment preflight failed; no environment-ready was issued"
            )
        ready = EnvironmentReady(
            schema_version="1.0",
            task_id=package.task.task_id,
            revision=package.task.revision,
            run_id=run_id,
            assignment_id=assignment_id,
            public_summary_digest=package.record.public_summary_digest,
            sealed_package_digest=package.record.sealed_package_digest,
            environment_lock_digest=package.record.environment_lock_digest,
            fixture_manifest_digest=package.record.fixture_manifest_digest,
            allowed_actions_digest=package.record.allowed_actions_digest,
            budget_digest=package.record.budget_digest,
            environment_instance_id=environment_instance_id,
            started_at=started_at,
            finished_at=finished_at,
            expires_at=finished_at + timedelta(seconds=ttl_seconds),
            checks=results,
            ready=True,
            provenance="real_host",
        )
        payload = _canonical_json(ready)
        payload_digest = _digest_bytes(payload)
        if target.exists():
            if not hmac.compare_digest(_read_bytes(target), payload):
                raise TaskPackageConflict("environment-ready identity already has another result")
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(target, flags, 0o400)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        record = EnvironmentReadyRecord(
            schema_version="1.0",
            task_id=package.task.task_id,
            revision=package.task.revision,
            run_id=run_id,
            assignment_id=assignment_id,
            ready_digest=payload_digest,
            issued_at=finished_at,
        )
        registry_path = self._ready_registry_path(
            package.task.task_id,
            package.task.revision,
            run_id,
        )
        registry_payload = _canonical_json(record)
        registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if registry_path.exists():
            if not hmac.compare_digest(_read_bytes(registry_path), registry_payload):
                raise TaskPackageConflict(
                    "environment-ready registry identity already has another result"
                )
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(registry_path, flags, 0o400)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(registry_payload)
                handle.flush()
                os.fsync(handle.fileno())
        return target, ready

    def require_environment_ready(
        self,
        package: FrozenTaskPackage,
        ready_path: Path,
        *,
        run_id: str,
        assignment_id: UUID,
        at: datetime | None = None,
    ) -> tuple[EnvironmentReady, str]:
        package = self.load_frozen(
            package.task.task_id,
            package.task.revision,
            expected_sealed_digest=package.record.sealed_package_digest,
        )
        canonical_path = self._ready_path(
            package.task.task_id,
            package.task.revision,
            run_id,
        )
        try:
            supplied_path = Path(ready_path).resolve(strict=True)
            expected_path = canonical_path.resolve(strict=True)
        except OSError as error:
            raise TaskPackageNotReady("environment-ready does not bind this exact run") from error
        if supplied_path != expected_path:
            raise TaskPackageNotReady(
                "environment-ready is not the canonical manager-issued record"
            )
        payload = _read_bytes(expected_path)
        ready_digest = _digest_bytes(payload)
        registry = EnvironmentReadyRecord.model_validate(
            _read_json(
                self._ready_registry_path(
                    package.task.task_id,
                    package.task.revision,
                    run_id,
                )
            )
        )
        if (
            registry.task_id != package.task.task_id
            or registry.revision != package.task.revision
            or registry.run_id != run_id
            or registry.assignment_id != assignment_id
            or not hmac.compare_digest(registry.ready_digest, ready_digest)
        ):
            raise TaskPackageNotReady("environment-ready registry does not bind this exact run")
        ready = EnvironmentReady.model_validate_json(payload)
        expected = {
            "task_id": package.task.task_id,
            "revision": package.task.revision,
            "run_id": run_id,
            "assignment_id": str(assignment_id),
            "public_summary_digest": package.record.public_summary_digest,
            "sealed_package_digest": package.record.sealed_package_digest,
            "environment_lock_digest": package.record.environment_lock_digest,
            "fixture_manifest_digest": package.record.fixture_manifest_digest,
            "allowed_actions_digest": package.record.allowed_actions_digest,
            "budget_digest": package.record.budget_digest,
        }
        actual = ready.model_dump(mode="json")
        if any(actual[key] != value for key, value in expected.items()):
            raise TaskPackageNotReady("environment-ready does not bind this exact run")
        check_ids = [item.check_id for item in ready.checks]
        expected_ids = [item.check_id for item in package.environment.health_checks]
        if check_ids != expected_ids:
            raise TaskPackageNotReady("environment-ready does not cover exact health checks")
        now = at or datetime.now(timezone.utc)
        _require_utc(now)
        if ready.finished_at > now + timedelta(seconds=5) or now >= ready.expires_at:
            raise TaskPackageNotReady("environment-ready is stale or from the future")
        return ready, ready_digest

    def _project_formal_assignment(
        self,
        package: FrozenTaskPackage,
        ready: EnvironmentReady,
        *,
        ready_digest: str,
        workspace_mount_digest: str,
        workspace_policy_digest: str,
        run_id: str,
        assignment_id: UUID,
        idempotency_key: str,
        target: ApplicationTarget,
        platform: PlatformAccess,
        collaboration: CollaborationAccess,
        created_at: datetime,
    ) -> BuildAssignment:
        fixture_refs = [
            ArtifactRef(
                artifact_id=(
                    "fixture:" + hashlib.sha256(entry.path.encode("utf-8")).hexdigest()[:32]
                ),
                digest=entry.digest,
                media_type="application/octet-stream",
                display_name=PurePosixPath(entry.path).name,
            )
            for entry in package.fixtures.files
        ]
        network_policy = (
            AssignmentNetworkPolicy.allowlist
            if package.allowed_actions.network_hosts
            else AssignmentNetworkPolicy.none
        )
        expected_platform_scopes = formal_platform_scopes(package.allowed_actions.platform_actions)
        deadline_at = created_at + timedelta(seconds=package.budget.assignment_wall_clock_seconds)
        if platform.scopes != expected_platform_scopes:
            raise TaskPackageSecurityError(
                "formal platform scopes must be the exact package action projection"
            )
        if (
            platform.credential_ref == collaboration.credential_ref
            or collaboration.expires_at <= created_at
            or collaboration.expires_at != deadline_at
        ):
            raise TaskPackageSecurityError(
                "formal collaboration authority is not bounded to the assignment"
            )
        requirement = _read_bytes(package.root / package.task.requirement_file).decode("utf-8")
        return BuildAssignment(
            schema_version="1.0",
            assignment_id=assignment_id,
            idempotency_key=idempotency_key,
            mode=AssignmentMode.formal_experiment,
            requirement=requirement,
            business_context=BusinessContext(
                customer_roles=[package.task.customer_role],
                business_goal=package.task.business_goal,
                inputs=[entry.path for entry in package.fixtures.files],
                outputs=[item.name for item in package.task.deliverables],
                constraints=[package.task.acceptance_summary],
            ),
            task_package=TaskPackageRef(
                task_id=package.task.task_id,
                revision=package.task.revision,
                public_summary_digest=package.record.public_summary_digest,
                run_id=run_id,
                environment_ready_digest=ready_digest,
                environment_lock_digest=package.record.environment_lock_digest,
                allowed_actions_digest=package.record.allowed_actions_digest,
                budget_digest=package.record.budget_digest,
                environment_instance_id=ready.environment_instance_id,
                workspace_mount_digest=workspace_mount_digest,
                workspace_policy_digest=workspace_policy_digest,
            ),
            target=target,
            platform=platform,
            constraints=AssignmentConstraints(
                deadline_at=deadline_at,
                max_turns=package.budget.max_build_repair_turns,
                max_budget_usd=package.budget.max_model_cost_usd,
                max_tool_calls=package.budget.max_platform_tool_calls,
                network_policy=network_policy,
                allowed_hosts=package.allowed_actions.network_hosts,
                allowed_actions=package.allowed_actions.platform_actions,
                prohibited_actions=[
                    ProhibitedAction(action)
                    for action in package.allowed_actions.prohibited_actions
                ],
                no_substitute_validation=True,
                readable_host_objects=package.allowed_actions.readable_host_objects,
                writable_host_operations=(package.allowed_actions.writable_host_operations),
                model_access=package.allowed_actions.model_access,
                file_access=package.allowed_actions.file_access,
                connector_access=package.allowed_actions.connector_access,
                permission_required_actions=(package.allowed_actions.permission_required_actions),
                max_write_count=package.allowed_actions.max_write_count,
                max_payload_bytes=package.allowed_actions.max_payload_bytes,
                compensation_actions=package.allowed_actions.compensation_actions,
                max_report_evidence_rounds=(
                    package.budget.max_report_evidence_rounds
                ),
                stable_hidden_runs=package.budget.stable_hidden_runs,
            ),
            fixture_refs=fixture_refs,
            deliverables=[
                DeliverableSpec(
                    name=item.name,
                    description=item.description,
                    media_type=item.media_type,
                )
                for item in package.task.deliverables
            ],
            collaboration=collaboration,
            created_at=created_at,
        )

    def build_formal_assignment(
        self,
        package: FrozenTaskPackage,
        *,
        ready_path: Path,
        workspace_manifest_path: Path,
        run_id: str,
        assignment_id: UUID,
        idempotency_key: str,
        target: ApplicationTarget,
        platform: PlatformAccess,
        collaboration: CollaborationAccess,
        created_at: datetime | None = None,
    ) -> BuildAssignment:
        """Build the only trusted formal-assignment projection.

        Callers provide platform/channel capabilities, but cannot widen the
        package's network, action, budget, fixture, or validation policy.
        """

        package = self.load_frozen(
            package.task.task_id,
            package.task.revision,
            expected_sealed_digest=package.record.sealed_package_digest,
        )
        validation_now = datetime.now(timezone.utc)
        created = created_at or validation_now
        _require_utc(created)
        ready, ready_digest = self.require_environment_ready(
            package,
            ready_path,
            run_id=run_id,
            assignment_id=assignment_id,
            at=validation_now,
        )
        if ready.finished_at > created or created > validation_now + timedelta(seconds=5):
            raise TaskPackageNotReady(
                "formal assignment timestamp is outside its trusted preflight window"
            )
        _, workspace_mount_digest, workspace_policy_digest = self.require_workspace_manifest(
            package,
            workspace_manifest_path,
            role=WorkspaceRole.lilies,
            run_id=run_id,
            assignment_id=assignment_id,
            environment_ready_digest=ready_digest,
            environment_instance_id=ready.environment_instance_id,
        )
        assignment = self._project_formal_assignment(
            package,
            ready,
            ready_digest=ready_digest,
            workspace_mount_digest=workspace_mount_digest,
            workspace_policy_digest=workspace_policy_digest,
            run_id=run_id,
            assignment_id=assignment_id,
            idempotency_key=idempotency_key,
            target=target,
            platform=platform,
            collaboration=collaboration,
            created_at=created,
        )
        record = FormalAssignmentRecord(
            schema_version="1.0",
            task_id=package.task.task_id,
            revision=package.task.revision,
            run_id=run_id,
            assignment_id=assignment_id,
            assignment_digest=_digest_bytes(_canonical_json(assignment)),
            issued_at=created,
        )
        registry_path = self._formal_assignment_registry_path(assignment_id)
        registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        record_payload = _canonical_json(record)
        if registry_path.exists():
            if not hmac.compare_digest(
                _read_bytes(registry_path),
                record_payload,
            ):
                raise TaskPackageConflict(
                    "formal assignment identity already has another projection"
                )
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(registry_path, flags, 0o400)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(record_payload)
                handle.flush()
                os.fsync(handle.fileno())
        return assignment

    def authorize_formal_assignment(
        self,
        assignment: BuildAssignment,
    ) -> tuple[FrozenTaskPackage, EnvironmentReady, str]:
        """Resolve a formal assignment back to the exact manager-issued input.

        The public daemon may parse a ``BuildAssignment``, but only this reverse
        projection proves that its package, readiness, fixtures, actions, budget,
        deadline, and collaboration fields came from the frozen package manager.
        """

        return self._authorize_formal_assignment(assignment, at=None)

    def _authorize_formal_assignment(
        self,
        assignment: BuildAssignment,
        *,
        at: datetime | None,
    ) -> tuple[FrozenTaskPackage, EnvironmentReady, str]:
        """Authorize live input or replay it at its immutable creation time."""

        if assignment.mode is not AssignmentMode.formal_experiment:
            raise TaskPackageConflict(
                "only formal experiment assignments use the frozen package gate"
            )
        task_ref = assignment.task_package
        if (
            task_ref is None
            or task_ref.run_id is None
            or task_ref.environment_ready_digest is None
            or task_ref.workspace_mount_digest is None
            or task_ref.workspace_policy_digest is None
            or assignment.collaboration is None
        ):
            raise TaskPackageConflict("formal assignment is missing its frozen run binding")
        assignment_digest = _digest_bytes(_canonical_json(assignment))
        try:
            record = FormalAssignmentRecord.model_validate(
                _read_json(self._formal_assignment_registry_path(assignment.assignment_id))
            )
        except (OSError, ValueError, TaskPackageError) as error:
            raise TaskPackageConflict("formal assignment has no trusted manager record") from error
        if (
            record.task_id != task_ref.task_id
            or record.revision != task_ref.revision
            or record.run_id != task_ref.run_id
            or record.assignment_id != assignment.assignment_id
            or record.issued_at != assignment.created_at
            or not hmac.compare_digest(
                record.assignment_digest,
                assignment_digest,
            )
        ):
            raise TaskPackageConflict(
                "formal assignment is not the exact manager-issued projection"
            )
        package = self.load_frozen(
            task_ref.task_id,
            task_ref.revision,
            expected_public_digest=task_ref.public_summary_digest,
        )
        ready_path = self._ready_path(
            task_ref.task_id,
            task_ref.revision,
            task_ref.run_id,
        )
        ready, ready_digest = self.require_environment_ready(
            package,
            ready_path,
            run_id=task_ref.run_id,
            assignment_id=assignment.assignment_id,
            at=at,
        )
        if not hmac.compare_digest(
            ready_digest,
            task_ref.environment_ready_digest,
        ):
            raise TaskPackageConflict("formal assignment readiness digest does not match")
        workspace_record = WorkspaceMountRecord.model_validate(
            _read_json(self._workspace_registry_path(task_ref.workspace_mount_digest))
        )
        if (
            workspace_record.task_id != task_ref.task_id
            or workspace_record.revision != task_ref.revision
            or workspace_record.role is not WorkspaceRole.lilies
            or workspace_record.run_id != task_ref.run_id
            or workspace_record.assignment_id != assignment.assignment_id
            or not hmac.compare_digest(
                workspace_record.manifest_digest,
                task_ref.workspace_mount_digest,
            )
            or not hmac.compare_digest(
                workspace_record.policy_digest,
                task_ref.workspace_policy_digest,
            )
        ):
            raise TaskPackageConflict("formal assignment workspace binding is not manager-issued")
        expected = self._project_formal_assignment(
            package,
            ready,
            ready_digest=ready_digest,
            workspace_mount_digest=task_ref.workspace_mount_digest,
            workspace_policy_digest=task_ref.workspace_policy_digest,
            run_id=task_ref.run_id,
            assignment_id=assignment.assignment_id,
            idempotency_key=assignment.idempotency_key,
            target=assignment.target,
            platform=assignment.platform,
            collaboration=assignment.collaboration,
            created_at=assignment.created_at,
        )
        if not hmac.compare_digest(
            _canonical_json(expected),
            _canonical_json(assignment),
        ):
            raise TaskPackageConflict(
                "formal assignment differs from the trusted package projection"
            )
        return package, ready, ready_digest

    def materialize_task_workspace(
        self,
        package: FrozenTaskPackage,
        destination: Path,
        *,
        role: WorkspaceRole,
        run_id: str,
        assignment_id: UUID,
        environment_ready_path: Path | None = None,
        run_archive: Path | None = None,
        developer_source_root: Path | None = None,
    ) -> WorkspaceMountManifest:
        package = self.load_frozen(
            package.task.task_id,
            package.task.revision,
            expected_sealed_digest=package.record.sealed_package_digest,
        )
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise TaskPackageConflict("workspace destination must not exist")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
        entries: list[WorkspaceMountEntry] = []
        try:
            ready_digest: str | None = None
            environment_instance_id: str | None = None
            archive_digest: str | None = None
            manifest_created_at = package.record.frozen_at
            if role is WorkspaceRole.lilies:
                if (
                    environment_ready_path is None
                    or run_archive is not None
                    or developer_source_root is not None
                ):
                    raise TaskPackageError(
                        "Lilies workspace requires environment-ready and no archive"
                    )
                ready, ready_digest = self.require_environment_ready(
                    package,
                    environment_ready_path,
                    run_id=run_id,
                    assignment_id=assignment_id,
                )
                manifest_created_at = ready.finished_at
                environment_instance_id = ready.environment_instance_id
            elif role is WorkspaceRole.developer:
                if environment_ready_path is not None or run_archive is not None:
                    raise TaskPackageError(
                        "developer workspace cannot receive ready or archive authority"
                    )
            else:
                if (
                    environment_ready_path is not None
                    or run_archive is None
                    or developer_source_root is not None
                ):
                    raise TaskPackageError("verifier workspace requires only a sealed run archive")
            for entry in package.record.immutable_files:
                protected = entry.path.startswith("protected/")
                if role in {WorkspaceRole.lilies, WorkspaceRole.developer} and protected:
                    continue
                target_relative = (
                    f"task/{entry.path}" if role is WorkspaceRole.developer else entry.path
                )
                copied = _copy_regular(
                    _resolved_child(package.root, entry.path),
                    _resolved_child(temporary, target_relative),
                )
                if (
                    not hmac.compare_digest(copied.digest, entry.digest)
                    or copied.size_bytes != entry.size_bytes
                ):
                    raise TaskPackageConflict("frozen task changed while materializing a workspace")
                target = _resolved_child(temporary, target_relative)
                os.chmod(target, 0o400)
                entries.append(
                    WorkspaceMountEntry(
                        logical_source=f"task-package:{entry.path}",
                        target_path=target_relative,
                        digest=copied.digest,
                        size_bytes=copied.size_bytes,
                        read_only=True,
                    )
                )
            if role is WorkspaceRole.developer and developer_source_root is not None:
                source_lexical = Path(developer_source_root)
                source_files = _iter_developer_source_files(source_lexical)
                source_root = source_lexical.resolve(strict=True)
                if not source_files:
                    raise TaskPackageError("developer source snapshot cannot be empty")
                seen_source_paths: set[str] = set()
                for source_file in source_files:
                    relative = _normalize_relative_path(
                        source_file.relative_to(source_root).as_posix()
                    )
                    identity = unicodedata.normalize("NFC", relative).casefold()
                    if identity in seen_source_paths:
                        raise TaskPackageSecurityError(
                            "developer source paths collide after normalization"
                        )
                    seen_source_paths.add(identity)
                    target_relative = f"source/{relative}"
                    copied = _copy_regular(
                        source_file,
                        _resolved_child(temporary, target_relative),
                    )
                    os.chmod(
                        _resolved_child(temporary, target_relative),
                        0o600,
                    )
                    entries.append(
                        WorkspaceMountEntry(
                            logical_source=f"platform-source:{relative}",
                            target_path=target_relative,
                            digest=copied.digest,
                            size_bytes=copied.size_bytes,
                            read_only=False,
                        )
                    )
            if role is WorkspaceRole.verifier:
                assert run_archive is not None
                replay = self.replay_archive(run_archive)
                if (
                    replay.run_id != run_id
                    or replay.claim_binding is None
                    or replay.claim_binding.assignment_id != assignment_id
                ):
                    raise TaskPackageError(
                        "verifier workspace archive binding does not match the run"
                    )
                manifest_created_at = replay.created_at
                archive_manifest = Path(run_archive) / "archive-manifest.json"
                archive_digest = _digest_bytes(_read_bytes(archive_manifest))
                registered_root, _ = self.find_archive_by_digest(
                    package.task.task_id,
                    package.task.revision,
                    archive_digest,
                )
                if registered_root.resolve() != Path(run_archive).resolve():
                    raise TaskPackageError("verifier workspace archive is not the registered run")
                for entry in replay.files:
                    target_relative = f"archive/{entry.path}"
                    copied = _copy_regular(
                        _resolved_child(Path(run_archive), entry.path),
                        _resolved_child(temporary, target_relative),
                    )
                    os.chmod(_resolved_child(temporary, target_relative), 0o400)
                    entries.append(
                        WorkspaceMountEntry(
                            logical_source=f"run-archive:{entry.path}",
                            target_path=target_relative,
                            digest=copied.digest,
                            size_bytes=copied.size_bytes,
                            read_only=True,
                        )
                    )
                digest, size = _digest_file(archive_manifest)
                target_relative = "archive/archive-manifest.json"
                _copy_regular(
                    archive_manifest,
                    _resolved_child(temporary, target_relative),
                )
                os.chmod(_resolved_child(temporary, target_relative), 0o400)
                entries.append(
                    WorkspaceMountEntry(
                        logical_source="run-archive:archive-manifest.json",
                        target_path=target_relative,
                        digest=digest,
                        size_bytes=size,
                        read_only=True,
                    )
                )
                writable_prefixes = ["result"]
            elif role is WorkspaceRole.developer:
                writable_prefixes = ["source", "work"]
            else:
                writable_prefixes = ["work", "artifacts"]
            for prefix in writable_prefixes:
                _resolved_child(temporary, prefix).mkdir(parents=True, exist_ok=True, mode=0o700)
            manifest = WorkspaceMountManifest(
                schema_version="1.0",
                task_id=package.task.task_id,
                revision=package.task.revision,
                role=role,
                run_id=run_id,
                assignment_id=assignment_id,
                public_summary_digest=package.record.public_summary_digest,
                sealed_package_digest=(
                    package.record.sealed_package_digest
                    if role is WorkspaceRole.verifier
                    else None
                ),
                environment_ready_digest=ready_digest,
                environment_instance_id=environment_instance_id,
                archive_manifest_digest=archive_digest,
                entries=sorted(entries, key=lambda item: item.target_path),
                denied_segments=sorted(_FORBIDDEN_WORKSPACE_SEGMENTS),
                writable_prefixes=writable_prefixes,
                created_at=manifest_created_at,
            )
            _atomic_write(
                temporary / WORKSPACE_MANIFEST_FILE,
                _canonical_json(manifest),
                mode=0o400,
            )
            policy = {
                "schema_version": "1.0",
                "denied_segments": sorted(
                    _FORBIDDEN_WORKSPACE_SEGMENTS | {WORKSPACE_POLICY_FILE, WORKSPACE_MANIFEST_FILE}
                ),
                "writable_prefixes": writable_prefixes,
            }
            policy_payload = _canonical_json(policy)
            _atomic_write(
                temporary / WORKSPACE_POLICY_FILE,
                policy_payload,
                mode=0o400,
            )
            os.replace(temporary, destination)
            for directory in sorted(
                (path for path in destination.rglob("*") if path.is_dir()),
                reverse=True,
            ):
                relative = directory.relative_to(destination).as_posix()
                writable = any(
                    relative == prefix or relative.startswith(f"{prefix}/")
                    for prefix in writable_prefixes
                )
                os.chmod(directory, 0o700 if writable else 0o500)
            os.chmod(destination, 0o500)
            manifest_payload = _canonical_json(manifest)
            manifest_digest = _digest_bytes(manifest_payload)
            record = WorkspaceMountRecord(
                schema_version="1.0",
                task_id=package.task.task_id,
                revision=package.task.revision,
                role=role,
                run_id=run_id,
                assignment_id=assignment_id,
                manifest_digest=manifest_digest,
                policy_digest=_digest_bytes(policy_payload),
                created_at=manifest.created_at,
            )
            registry_path = self._workspace_registry_path(manifest_digest)
            registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            record_payload = _canonical_json(record)
            if registry_path.exists():
                if not hmac.compare_digest(
                    _read_bytes(registry_path),
                    record_payload,
                ):
                    raise TaskPackageConflict(
                        "workspace manifest digest has another registry binding"
                    )
            else:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(registry_path, flags, 0o400)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(record_payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            return manifest
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def require_workspace_manifest(
        self,
        package: FrozenTaskPackage,
        manifest_path: Path,
        *,
        role: WorkspaceRole,
        run_id: str,
        assignment_id: UUID,
        environment_ready_digest: str | None = None,
        environment_instance_id: str | None = None,
    ) -> tuple[WorkspaceMountManifest, str, str]:
        package = self.load_frozen(
            package.task.task_id,
            package.task.revision,
            expected_sealed_digest=package.record.sealed_package_digest,
        )
        payload = _read_bytes(Path(manifest_path))
        manifest_digest = _digest_bytes(payload)
        manifest = WorkspaceMountManifest.model_validate_json(payload)
        policy_path = Path(manifest_path).parent / WORKSPACE_POLICY_FILE
        policy_payload = _read_bytes(policy_path)
        policy_digest = _digest_bytes(policy_payload)
        try:
            policy = json.loads(policy_payload)
        except json.JSONDecodeError as error:
            raise TaskPackageSecurityError("workspace policy is not valid JSON") from error
        expected_policy = {
            "schema_version": "1.0",
            "denied_segments": sorted(
                _FORBIDDEN_WORKSPACE_SEGMENTS | {WORKSPACE_POLICY_FILE, WORKSPACE_MANIFEST_FILE}
            ),
            "writable_prefixes": manifest.writable_prefixes,
        }
        if policy != expected_policy:
            raise TaskPackageSecurityError("workspace policy differs from the manager projection")
        record = WorkspaceMountRecord.model_validate(
            _read_json(self._workspace_registry_path(manifest_digest))
        )
        expected = {
            "task_id": package.task.task_id,
            "revision": package.task.revision,
            "role": role,
            "run_id": run_id,
            "assignment_id": assignment_id,
            "public_summary_digest": package.record.public_summary_digest,
            "sealed_package_digest": (
                package.record.sealed_package_digest
                if role is WorkspaceRole.verifier
                else None
            ),
        }
        if any(getattr(manifest, key) != value for key, value in expected.items()):
            raise TaskPackageSecurityError("workspace manifest does not bind the exact formal run")
        if (
            record.task_id != package.task.task_id
            or record.revision != package.task.revision
            or record.role is not role
            or record.run_id != run_id
            or record.assignment_id != assignment_id
            or not hmac.compare_digest(record.manifest_digest, manifest_digest)
            or not hmac.compare_digest(record.policy_digest, policy_digest)
        ):
            raise TaskPackageSecurityError("workspace manifest is not manager-issued for this run")
        if environment_ready_digest is not None and not hmac.compare_digest(
            str(manifest.environment_ready_digest),
            environment_ready_digest,
        ):
            raise TaskPackageSecurityError(
                "workspace manifest has another environment-ready binding"
            )
        if (
            environment_instance_id is not None
            and manifest.environment_instance_id != environment_instance_id
        ):
            raise TaskPackageSecurityError(
                "workspace manifest has another environment instance binding"
            )
        return manifest, manifest_digest, policy_digest

    @staticmethod
    def _validated_archived_preflight_failures(
        run_root: Path,
        declared_files: Mapping[str, FileDigestEntry],
        *,
        task_id: str,
        revision: int,
        run_id: str,
        assignment_id: UUID,
        environment_instance_id: str,
    ) -> list[tuple[FileDigestEntry, PreflightFailureEvidence]]:
        """Replay failure attempts by their signed payload order, never their names."""

        validated: list[
            tuple[FileDigestEntry, PreflightFailureEvidence]
        ] = []
        seen_attempts: set[int] = set()
        seen_digests: set[str] = set()
        for entry in declared_files.values():
            if not entry.path.startswith("environment-preflight/"):
                continue
            parts = PurePosixPath(entry.path).parts
            if len(parts) != 2 or parts[0] != "environment-preflight":
                raise TaskPackageConflict(
                    "archived preflight evidence path is invalid"
                )
            payload = _read_bytes(
                run_root / entry.path,
                limit=MAX_ARCHIVE_FILE_BYTES,
            )
            if (
                len(payload) != entry.size_bytes
                or not hmac.compare_digest(
                    _digest_bytes(payload),
                    entry.digest,
                )
            ):
                raise TaskPackageConflict(
                    "archived preflight evidence digest changed"
                )
            try:
                failure = PreflightFailureEvidence.model_validate_json(
                    payload
                )
            except ValueError as error:
                raise TaskPackageConflict(
                    "archived preflight evidence is invalid"
                ) from error
            if (
                failure.task_id != task_id
                or failure.revision != revision
                or failure.run_id != run_id
                or failure.assignment_id != assignment_id
                or failure.environment_instance_id
                != environment_instance_id
            ):
                raise TaskPackageConflict(
                    "archived preflight evidence changed its formal binding"
                )
            if (
                failure.attempt in seen_attempts
                or entry.digest in seen_digests
            ):
                raise TaskPackageConflict(
                    "archived preflight evidence is not uniquely bound"
                )
            seen_attempts.add(failure.attempt)
            seen_digests.add(entry.digest)
            validated.append((entry, failure))
        validated.sort(key=lambda item: item[1].attempt)
        attempts = [failure.attempt for _, failure in validated]
        if attempts != list(range(1, len(attempts) + 1)):
            raise TaskPackageConflict(
                "archived preflight evidence has an incomplete attempt sequence"
            )
        if any(
            current.finished_at > following.started_at
            for (_, current), (_, following) in zip(
                validated,
                validated[1:],
                strict=False,
            )
        ):
            raise TaskPackageConflict(
                "archived preflight evidence reordered its attempt timeline"
            )
        return validated

    @staticmethod
    def _validate_connector_budget_semantics(
        run_root: Path,
        *,
        assignment_id: UUID,
        allowed_network_hosts: Sequence[str],
        allowed_compensation_operations: Sequence[str],
        max_write_count: int,
        max_payload_bytes: int,
        allow_missing: bool,
    ) -> _ArchivedConnectorAssignmentBudgetReceipt | None:
        primary = run_root / "connector-budget.json"
        scanner_copy = run_root / "scanner-inputs/connector-budget.json"
        try:
            primary_bytes = _read_bytes(
                primary,
                limit=MAX_CONTROL_FILE_BYTES,
            )
            scanner_bytes = _read_bytes(
                scanner_copy,
                limit=MAX_CONTROL_FILE_BYTES,
            )
        except (OSError, TaskPackageError) as error:
            raise TaskPackageConflict(
                "connector side-effect budget receipt is missing"
            ) from error
        if not hmac.compare_digest(primary_bytes, scanner_bytes):
            raise TaskPackageConflict(
                "connector side-effect receipt differs from its scanner copy"
            )
        try:
            document = _strict_json_loads(primary_bytes)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise TaskPackageConflict(
                "connector side-effect budget receipt is invalid"
            ) from error
        expected_hosts = sorted(
            {
                str(host).casefold().rstrip(".")
                for host in allowed_network_hosts
            }
        )
        if isinstance(document, Mapping) and document.get("complete") is False:
            expected_missing = {
                "schema_version": "1.0",
                "complete": False,
                "missing_reason": (
                    "no_workflow_runs_before_assignment_execution"
                ),
                "assignment_id": str(assignment_id),
                "allowed_network_hosts": expected_hosts,
                "allowed_compensation_operations": sorted(
                    set(allowed_compensation_operations)
                ),
                "max_write_count": max_write_count,
                "max_payload_bytes": max_payload_bytes,
                "write_count": 0,
                "writes": [],
            }
            if not allow_missing or dict(document) != expected_missing:
                raise TaskPackageConflict(
                    "connector side-effect denominator is missing outside "
                    "the pre-assignment boundary"
                )
            return None
        try:
            receipt = _ArchivedConnectorAssignmentBudgetReceipt.model_validate(
                document
            )
        except ValueError as error:
            raise TaskPackageConflict(
                "connector side-effect receipt digest or writes are invalid"
            ) from error
        if (
            receipt.assignment_id != str(assignment_id)
            or receipt.allowed_network_hosts != expected_hosts
            or receipt.allowed_compensation_operations
            != sorted(set(allowed_compensation_operations))
            or receipt.max_write_count != max_write_count
            or receipt.max_payload_bytes != max_payload_bytes
            or receipt.write_count > receipt.max_write_count
            or receipt.write_count != len(receipt.writes)
        ):
            raise TaskPackageConflict(
                "connector side-effect receipt changed its frozen policy"
            )
        return receipt

    def _validate_archive_semantics(
        self,
        run_root: Path,
        manifest: RunArchiveManifest,
    ) -> None:
        """Validate replayable records and cross-bind every formal result source."""

        declared_paths = {entry.path for entry in manifest.files}
        has_assignment = "assignment.json" in declared_paths
        has_reservation = "reserved-assignment.json" in declared_paths
        if has_assignment and has_reservation:
            raise TaskPackageConflict(
                "formal archive cannot contain both an assignment and a reservation"
            )
        if has_reservation:
            self._validate_reserved_archive_semantics(run_root, manifest)
            return

        record_specs: tuple[
            tuple[str, type[BaseModel]],
            ...,
        ] = (
            ("messages.jsonl", ArchivedMessageRecord),
            ("platform-events.jsonl", ArchivedPlatformEventRecord),
            ("collaboration.jsonl", ArchivedCollaborationRecord),
        )
        parsed: dict[str, list[BaseModel]] = {}
        for relative, model in record_specs:
            path = run_root / relative
            if not path.exists():
                continue
            if (
                path.stat(follow_symlinks=False).st_size == 0
                and manifest.claim_binding is None
                and any(entry.path == "assignment.json" for entry in manifest.files)
            ):
                parsed[relative] = []
                continue
            records = _read_archive_jsonl(path, record_model=model)
            for record in records:
                if (
                    record.task_id != manifest.task_id
                    or record.revision != manifest.revision
                    or record.run_id != manifest.run_id
                ):
                    raise TaskPackageConflict(f"archive record belongs to another run: {relative}")
            parsed[relative] = records

        binding = manifest.claim_binding
        if binding is None and "assignment.json" not in declared_paths:
            return
        missing = {
            "assignment.json",
            "messages.jsonl",
            "platform-events.jsonl",
            "collaboration.jsonl",
            "evidence-index.json",
            "forbidden-assistance-scan.json",
            "scanner-inputs/bridge.json",
            "scanner-inputs/collaboration.json",
            "scanner-inputs/workflow.json",
            "scanner-inputs/blackbox-auth.json",
            "connector-budget.json",
            "scanner-inputs/connector-budget.json",
            "scanner-inputs/artifact-inventory.json",
            "scanner-inputs/source-semantic.json",
            "source-provenance/manifest.json",
            "result.json",
        } - declared_paths
        if missing:
            raise TaskPackageConflict(
                f"formal archive is missing typed evidence: {sorted(missing)}"
            )

        try:
            assignment = BuildAssignment.model_validate(
                _read_json(run_root / "assignment.json")
            )
            self._authorize_formal_assignment(
                assignment,
                at=assignment.created_at,
            )
        except (TaskPackageError, ValueError) as error:
            raise TaskPackageConflict(
                "archived assignment is not a manager-authorized formal assignment"
            ) from error
        task_ref = assignment.task_package
        if (
            assignment.mode is not AssignmentMode.formal_experiment
            or task_ref is None
            or task_ref.task_id != manifest.task_id
            or task_ref.revision != manifest.revision
            or task_ref.run_id != manifest.run_id
            or assignment.collaboration is None
        ):
            raise TaskPackageConflict("archived assignment does not bind the exact formal run")
        frozen_package = self.load_frozen(
            manifest.task_id,
            manifest.revision,
            expected_public_digest=task_ref.public_summary_digest,
            expected_sealed_digest=manifest.sealed_package_digest,
        )
        try:
            connector_workflow_export = _read_json(
                run_root / "scanner-inputs/workflow.json"
            )
            connector_workflow_runs = connector_workflow_export.get("runs")
            if not isinstance(connector_workflow_runs, list):
                raise ValueError("workflow runs are not a list")
        except (TaskPackageError, ValueError) as error:
            raise TaskPackageConflict(
                "connector side-effect workflow denominator is invalid"
            ) from error
        self._validate_connector_budget_semantics(
            run_root,
            assignment_id=assignment.assignment_id,
            allowed_network_hosts=assignment.constraints.allowed_hosts,
            allowed_compensation_operations=(
                assignment.constraints.compensation_actions
            ),
            max_write_count=(
                assignment.constraints.max_write_count
                if assignment.constraints.max_write_count is not None
                else frozen_package.allowed_actions.max_write_count
            ),
            max_payload_bytes=(
                assignment.constraints.max_payload_bytes
                if assignment.constraints.max_payload_bytes is not None
                else frozen_package.allowed_actions.max_payload_bytes
            ),
            allow_missing=(
                binding is None and not connector_workflow_runs
            ),
        )
        try:
            result = ArchivedRunResult.model_validate(
                _read_json(run_root / "result.json")
            )
        except (TaskPackageError, ValueError) as error:
            raise TaskPackageConflict(
                "archived result does not follow the formal result schema"
            ) from error
        expected_assignment_id = (
            binding.assignment_id if binding is not None else assignment.assignment_id
        )
        expected_application_id = (
            binding.application_id
            if binding is not None
            else result.application_id
        )
        if (
            assignment.assignment_id != expected_assignment_id
            or (
                assignment.target.application_id is not None
                and assignment.target.application_id != expected_application_id
            )
            or result.task_id != manifest.task_id
            or result.revision != manifest.revision
            or result.run_id != manifest.run_id
            or result.assignment_id != expected_assignment_id
            or result.application_id != expected_application_id
            or result.archive_status is not manifest.source_status
            or result.validation_mode is not manifest.validation_mode
        ):
            raise TaskPackageConflict("archived result changed its formal run binding")

        try:
            evidence_index = ArchivedEvidenceIndex.model_validate(
                _read_json(run_root / "evidence-index.json")
            )
        except (TaskPackageError, ValueError) as error:
            raise TaskPackageConflict(
                "archive evidence index is unavailable or invalid"
            ) from error
        if (
            evidence_index.task_id != manifest.task_id
            or evidence_index.revision != manifest.revision
            or evidence_index.run_id != manifest.run_id
            or evidence_index.assignment_id != expected_assignment_id
            or evidence_index.application_id != expected_application_id
        ):
            raise TaskPackageConflict(
                "archive evidence index belongs to another formal run"
            )
        declared_files = {entry.path: entry for entry in manifest.files}
        preflight_failures = self._validated_archived_preflight_failures(
            run_root,
            declared_files,
            task_id=manifest.task_id,
            revision=manifest.revision,
            run_id=manifest.run_id,
            assignment_id=expected_assignment_id,
            environment_instance_id=str(
                task_ref.environment_instance_id
            ),
        )
        ready_entry = declared_files.get("environment-ready.json")
        try:
            ready = EnvironmentReady.model_validate(
                _read_json(run_root / "environment-ready.json")
            )
        except (TaskPackageError, ValueError) as error:
            raise TaskPackageConflict(
                "formal archive readiness evidence is unavailable or invalid"
            ) from error
        if (
            ready_entry is None
            or manifest.environment_ready_digest is None
            or task_ref.environment_ready_digest is None
            or not hmac.compare_digest(
                ready_entry.digest,
                manifest.environment_ready_digest,
            )
            or not hmac.compare_digest(
                task_ref.environment_ready_digest,
                manifest.environment_ready_digest,
            )
            or ready.task_id != manifest.task_id
            or ready.revision != manifest.revision
            or ready.run_id != manifest.run_id
            or ready.assignment_id != expected_assignment_id
            or ready.environment_instance_id
            != task_ref.environment_instance_id
            or ready.public_summary_digest
            != frozen_package.record.public_summary_digest
            or ready.sealed_package_digest
            != frozen_package.record.sealed_package_digest
            or ready.environment_lock_digest
            != frozen_package.record.environment_lock_digest
            or ready.fixture_manifest_digest
            != frozen_package.record.fixture_manifest_digest
            or ready.allowed_actions_digest
            != frozen_package.record.allowed_actions_digest
            or ready.budget_digest != frozen_package.record.budget_digest
            or (
                preflight_failures
                and preflight_failures[-1][1].finished_at
                > ready.started_at
            )
        ):
            raise TaskPackageConflict(
                "formal archive readiness changed its preflight binding"
            )
        evidence_paths = {
            path
            for path in declared_files
            if PurePosixPath(path).parts[0] in {"artifacts", "host-receipts"}
        }
        indexed_paths = {entry.archive_path for entry in evidence_index.entries}
        if indexed_paths != evidence_paths:
            raise TaskPackageConflict(
                "archive evidence index does not exactly cover frozen business evidence"
            )
        indexed_artifact_digests: list[str] = []
        indexed_receipt_digests: list[str] = []
        for evidence in evidence_index.entries:
            declared = declared_files.get(evidence.archive_path)
            if (
                declared is None
                or not hmac.compare_digest(declared.digest, evidence.digest)
                or declared.size_bytes != evidence.size_bytes
                or evidence.run_id not in result.business_run_ids
            ):
                raise TaskPackageConflict(
                    "archive evidence index does not bind trusted business-run bytes"
                )
            if evidence.kind == "artifact":
                indexed_artifact_digests.append(evidence.digest)
            else:
                indexed_receipt_digests.append(evidence.digest)
        expected_artifact_digests = (
            binding.artifact_digests
            if binding is not None
            else result.artifact_digests
        )
        expected_receipt_digests = (
            binding.host_receipt_digests
            if binding is not None
            else result.host_receipt_digests
        )
        if (
            sorted(indexed_artifact_digests) != sorted(expected_artifact_digests)
            or sorted(indexed_receipt_digests)
            != sorted(expected_receipt_digests)
        ):
            raise TaskPackageConflict(
                "archive evidence index does not bind the frozen result digests"
            )
        try:
            assistance_scan = ForbiddenAssistanceScanRecord.model_validate(
                _read_json(run_root / "forbidden-assistance-scan.json")
            )
            validate_scan_digest(assistance_scan)
        except (TaskPackageError, ValueError) as error:
            raise TaskPackageConflict(
                "forbidden-assistance scan is unavailable or invalid"
            ) from error
        expected_assistance_findings = sorted(
            f"{item.rule_id}:{item.source_ref}"
            for item in assistance_scan.findings
        )
        if (
            assistance_scan.task_id != manifest.task_id
            or assistance_scan.revision != manifest.revision
            or assistance_scan.run_id != manifest.run_id
            or assistance_scan.assignment_id != expected_assignment_id
            or assistance_scan.channel_id != assignment.collaboration.channel_id
            or (
                binding is not None
                and assistance_scan.channel_id
                not in {
                    item.channel_id
                    for item in parsed.get("collaboration.jsonl", [])
                    if isinstance(item, ArchivedCollaborationRecord)
                }
            )
            or expected_assistance_findings
            != sorted(manifest.forbidden_assistance_findings)
            or (
                manifest.status is ArchiveStatus.succeeded
                and assistance_scan.verdict != "pass"
            )
        ):
            raise TaskPackageConflict(
                "forbidden-assistance scan does not bind the archive outcome"
            )
        source_provenance_export = _read_json(
            run_root / "source-provenance/manifest.json"
        )
        source_archive_files: dict[str, bytes] = {}
        if not (
            isinstance(source_provenance_export, Mapping)
            and source_provenance_export.get("complete") is False
        ):
            source_archive_files = {
                path: _read_bytes(
                    run_root / path,
                    limit=MAX_ARCHIVE_FILE_BYTES,
                )
                for path in declared_files
                if path == "source-provenance/manifest.json"
                or path.startswith("source-provenance/")
            }
            collaboration_messages = [
                item.payload
                for item in parsed.get("collaboration.jsonl", [])
                if isinstance(item, ArchivedCollaborationRecord)
                and item.kind == "message"
            ]
            try:
                expected_source_bindings = approved_developer_response_bindings(
                    collaboration_messages,
                    channel_id=assignment.collaboration.channel_id,
                )
                source_manifest = verify_source_provenance_archive_offline(
                    archive_files=source_archive_files,
                    expected_assignment_id=expected_assignment_id,
                    expected_bindings=expected_source_bindings,
                    expected_manifest_digest=str(
                        source_provenance_export.get("manifest_digest") or ""
                    ),
                )
            except (FormalSourceProvenanceError, ValueError, TypeError) as error:
                raise TaskPackageConflict(
                    "source provenance archive cannot be independently replayed"
                ) from error
            if (
                source_manifest.task_id != manifest.task_id
                or source_manifest.task_revision != manifest.revision
                or source_manifest.run_id != manifest.run_id
                or source_manifest.assignment_id != expected_assignment_id
                or source_manifest.channel_id
                != assignment.collaboration.channel_id
            ):
                raise TaskPackageConflict(
                    "source provenance archive changed its formal run binding"
                )
        scan_bindings = {
            item.archive_path: item for item in assistance_scan.input_bindings
        }
        for relative, scan_binding in scan_bindings.items():
            declared = declared_files.get(relative)
            if declared is None:
                raise TaskPackageConflict(
                    "forbidden-assistance scan input is not in the archive manifest"
                )
            payload = _read_bytes(
                run_root / relative,
                limit=MAX_ARCHIVE_FILE_BYTES,
            )
            if (
                not hmac.compare_digest(_digest_bytes(payload), declared.digest)
                or not hmac.compare_digest(declared.digest, scan_binding.digest)
                or len(payload) != declared.size_bytes
            ):
                raise TaskPackageConflict(
                    "forbidden-assistance scan input digest changed"
                )

        try:
            recomputed_scan = scan_forbidden_assistance(
                scanner_version=assistance_scan.scanner_version,
                assignment=assignment,
                session_id=assistance_scan.session_id,
                channel_id=assistance_scan.channel_id,
                bridge_export=_read_json(
                    run_root / "scanner-inputs/bridge.json"
                ),
                collaboration_export=_read_json(
                    run_root / "scanner-inputs/collaboration.json"
                ),
                workflow_export=_read_json(
                    run_root / "scanner-inputs/workflow.json"
                ),
                blackbox_auth_export=_read_json(
                    run_root / "scanner-inputs/blackbox-auth.json"
                ),
                artifact_inventory_export=_read_json(
                    run_root / "scanner-inputs/artifact-inventory.json"
                ),
                source_provenance_export=_read_json(
                    run_root / "source-provenance/manifest.json"
                ),
                source_semantic_export=_read_json(
                    run_root / "scanner-inputs/source-semantic.json"
                ),
                source_semantic_task_package=frozen_package,
                source_semantic_files=source_archive_files,
                evidence_index=evidence_index,
                business_run_ids=(
                    binding.business_run_ids
                    if binding is not None
                    else result.business_run_ids
                ),
                validation_mode=manifest.validation_mode.value,
                created_at=assistance_scan.created_at,
            )
        except (TaskPackageError, ValueError) as error:
            raise TaskPackageConflict(
                "forbidden-assistance scan cannot be independently replayed"
            ) from error
        if not hmac.compare_digest(
            _canonical_json(recomputed_scan),
            _canonical_json(assistance_scan),
        ):
            raise TaskPackageConflict(
                "forbidden-assistance scan replay differs from the frozen record"
            )

        if binding is None:
            messages = [
                item
                for item in parsed.get("messages.jsonl", [])
                if isinstance(item, ArchivedMessageRecord)
            ]
            platform_events = [
                item
                for item in parsed.get("platform-events.jsonl", [])
                if isinstance(item, ArchivedPlatformEventRecord)
            ]
            collaboration = [
                item
                for item in parsed.get("collaboration.jsonl", [])
                if isinstance(item, ArchivedCollaborationRecord)
            ]
            if any(
                item.assignment_id != expected_assignment_id
                or item.session_id != assistance_scan.session_id
                for item in messages
            ):
                raise TaskPackageConflict(
                    "terminal archived messages crossed the formal assignment"
                )
            if any(
                item.assignment_id != expected_assignment_id
                or item.application_id != expected_application_id
                or item.outcome is not None
                for item in platform_events
            ):
                raise TaskPackageConflict(
                    "terminal platform events crossed the formal assignment"
                )
            if any(
                item.assignment_id != expected_assignment_id
                or item.channel_id != assignment.collaboration.channel_id
                or item.kind == "claim.prepared"
                for item in collaboration
            ):
                raise TaskPackageConflict(
                    "terminal collaboration records changed their no-claim binding"
                )
            workflow_export = _read_json(
                run_root / "scanner-inputs/workflow.json"
            )
            workflow_runs = (
                workflow_export.get("runs")
                if isinstance(workflow_export, Mapping)
                else None
            )
            if not isinstance(workflow_runs, list):
                raise TaskPackageConflict(
                    "terminal workflow attempt denominator is unavailable"
                )
            expected_run_ids: list[str] = []
            expected_run_payloads: dict[str, dict[str, Any]] = {}
            for run in workflow_runs:
                if not isinstance(run, Mapping):
                    raise TaskPackageConflict(
                        "terminal workflow attempt is not an object"
                    )
                run_id = str(run.get("id") or "")
                events = run.get("events")
                if not run_id or run_id in expected_run_payloads or not isinstance(events, list):
                    raise TaskPackageConflict(
                        "terminal workflow attempt denominator is invalid"
                    )
                try:
                    durable_events = [
                        {
                            "seq": int(event["seq"]),
                            "type": str(event["type"]),
                            "created_at": str(event["created_at"]),
                            "data": event["data"],
                            "data_digest": _digest_bytes(
                                _canonical_json(event["data"])
                            ),
                        }
                        for event in events
                    ]
                except (KeyError, TypeError, ValueError) as error:
                    raise TaskPackageConflict(
                        "terminal workflow event evidence is incomplete"
                    ) from error
                expected_run_ids.append(run_id)
                expected_run_payloads[run_id] = {
                    "platform_run_id": run_id,
                    "status": str(run.get("status") or ""),
                    "version": run.get("version"),
                    "draft_revision": run.get("draft_revision"),
                    "created_at": str(run.get("created_at") or ""),
                    "updated_at": str(run.get("updated_at") or ""),
                    "outputs": run.get("outputs"),
                    "error": run.get("error"),
                    "durable_events": durable_events,
                }
            archived_run_payloads: dict[str, dict[str, Any]] = {}
            for event in platform_events:
                platform_run_id = event.payload.get("platform_run_id")
                if platform_run_id is None:
                    continue
                run_id = str(platform_run_id)
                if run_id in archived_run_payloads:
                    raise TaskPackageConflict(
                        "terminal platform run denominator contains a duplicate"
                    )
                archived_run_payloads[run_id] = event.payload
            inventory_export = _read_json(
                run_root / "scanner-inputs/artifact-inventory.json"
            )
            inventory_records = (
                inventory_export.get("records")
                if isinstance(inventory_export, Mapping)
                else None
            )
            if not isinstance(inventory_records, list) or any(
                not isinstance(item, Mapping)
                for item in inventory_records
            ):
                raise TaskPackageConflict(
                    "terminal artifact inventory denominator is invalid"
                )
            inventory_run_ids = sorted(
                {
                    str(item.get("run_id"))
                    for item in inventory_records
                    if item.get("run_id")
                }
            )
            expected_business_run_ids = list(
                dict.fromkeys(
                    [
                        *expected_run_ids,
                        *inventory_run_ids,
                    ]
                )
            ) or [manifest.run_id]
            if (
                archived_run_payloads != expected_run_payloads
                or result.business_run_ids != expected_business_run_ids
            ):
                raise TaskPackageConflict(
                    "terminal archive does not preserve every workflow attempt"
                )
            return

        messages = [
            item
            for item in parsed.get("messages.jsonl", [])
            if isinstance(item, ArchivedMessageRecord)
        ]
        if (
            len([item for item in messages if item.kind == "assignment.accepted"]) != 1
            or len({item.session_id for item in messages}) != 1
            or any(item.assignment_id != binding.assignment_id for item in messages)
        ):
            raise TaskPackageConflict(
                "archived messages do not prove one formal assignment session"
            )

        platform_events = [
            item
            for item in parsed.get("platform-events.jsonl", [])
            if isinstance(item, ArchivedPlatformEventRecord)
        ]
        snapshots = [item for item in platform_events if item.kind == "formal_run.snapshot"]
        expected_outcome = ArchivedPlatformOutcome(
            application_id=binding.application_id,
            draft_revision=binding.draft_revision,
            content_hash=binding.content_hash,
            published_version=binding.published_version,
            test_run_ids=binding.test_run_ids,
            business_run_ids=binding.business_run_ids,
            artifact_digests=binding.artifact_digests,
            host_receipt_digests=binding.host_receipt_digests,
        )
        if (
            len(snapshots) != 1
            or snapshots[0].outcome != expected_outcome
            or any(
                item.assignment_id != binding.assignment_id
                or item.application_id != binding.application_id
                for item in platform_events
            )
        ):
            raise TaskPackageConflict(
                "platform event snapshot does not prove the frozen claim fields"
            )

        collaboration = [
            item
            for item in parsed.get("collaboration.jsonl", [])
            if isinstance(item, ArchivedCollaborationRecord)
        ]
        prepared = [item for item in collaboration if item.kind == "claim.prepared"]
        resolved_reports = sorted(
            (
                item.report_id
                for item in collaboration
                if item.kind == "report.resolved" and item.report_id is not None
            ),
            key=str,
        )
        if (
            len(prepared) != 1
            or prepared[0].claim_binding != binding
            or any(
                item.assignment_id != binding.assignment_id
                or item.channel_id != assignment.collaboration.channel_id
                for item in collaboration
            )
            or resolved_reports != sorted(binding.resolved_report_ids, key=str)
        ):
            raise TaskPackageConflict(
                "collaboration records do not prove the frozen claim preparation"
            )

        try:
            result = ArchivedRunResult.model_validate(_read_json(run_root / "result.json"))
        except ValueError as error:
            raise TaskPackageConflict(
                "archived result does not follow the formal result schema"
            ) from error
        if (
            result.task_id != manifest.task_id
            or result.revision != manifest.revision
            or result.run_id != manifest.run_id
            or result.assignment_id != binding.assignment_id
            or result.application_id != binding.application_id
            or result.archive_status is not manifest.source_status
            or result.validation_mode is not manifest.validation_mode
            or result.business_run_ids != binding.business_run_ids
            or result.artifact_digests != binding.artifact_digests
            or result.host_receipt_digests != binding.host_receipt_digests
            or result.remaining_limits != binding.remaining_limits
        ):
            raise TaskPackageConflict("archived result does not bind the exact claim outcome")

    def _validate_reserved_archive_semantics(
        self,
        run_root: Path,
        manifest: RunArchiveManifest,
    ) -> None:
        """Replay a formal terminal that never crossed into daemon execution."""

        if manifest.claim_binding is not None:
            raise TaskPackageConflict(
                "pre-assignment formal archive cannot carry a claim binding"
            )
        declared_files = {entry.path: entry for entry in manifest.files}
        required = {
            "reserved-assignment.json",
            "result.json",
            "evidence-index.json",
            "preassignment-scan.json",
            "scanner-inputs/bridge.json",
            "scanner-inputs/collaboration.json",
            "scanner-inputs/workflow.json",
            "scanner-inputs/blackbox-auth.json",
            "connector-budget.json",
            "scanner-inputs/connector-budget.json",
            "scanner-inputs/artifact-inventory.json",
            "source-provenance/manifest.json",
        }
        missing = required - set(declared_files)
        if missing:
            raise TaskPackageConflict(
                f"pre-assignment formal archive is missing evidence: {sorted(missing)}"
            )
        try:
            reservation = ArchivedFormalReservation.model_validate(
                _read_json(run_root / "reserved-assignment.json")
            )
            result = ArchivedRunResult.model_validate(
                _read_json(run_root / "result.json")
            )
            evidence_index = ArchivedEvidenceIndex.model_validate(
                _read_json(run_root / "evidence-index.json")
            )
            scan = ArchivedPreassignmentScanRecord.model_validate(
                _read_json(run_root / "preassignment-scan.json")
            )
        except (TaskPackageError, ValueError) as error:
            raise TaskPackageConflict(
                "pre-assignment formal archive has invalid typed evidence"
            ) from error
        common_identity = (
            reservation.task_id == manifest.task_id
            and reservation.revision == manifest.revision
            and reservation.run_id == manifest.run_id
            and result.task_id == manifest.task_id
            and result.revision == manifest.revision
            and result.run_id == manifest.run_id
            and result.assignment_id == reservation.assignment_id
            and result.application_id == reservation.application_id
            and result.archive_status is manifest.source_status
            and result.validation_mode is manifest.validation_mode
            and evidence_index.task_id == manifest.task_id
            and evidence_index.revision == manifest.revision
            and evidence_index.run_id == manifest.run_id
            and evidence_index.assignment_id == reservation.assignment_id
            and evidence_index.application_id == reservation.application_id
            and scan.task_id == manifest.task_id
            and scan.revision == manifest.revision
            and scan.run_id == manifest.run_id
            and scan.assignment_id == reservation.assignment_id
            and scan.session_id == reservation.session_id
            and scan.application_id == reservation.application_id
            and scan.channel_id == reservation.channel_id
        )
        if not common_identity:
            raise TaskPackageConflict(
                "pre-assignment formal archive changed its reserved run identity"
            )
        frozen_package = self.load_frozen(
            manifest.task_id,
            manifest.revision,
            expected_public_digest=manifest.public_summary_digest,
            expected_sealed_digest=manifest.sealed_package_digest,
        )
        self._validate_connector_budget_semantics(
            run_root,
            assignment_id=reservation.assignment_id,
            allowed_network_hosts=frozen_package.allowed_actions.network_hosts,
            allowed_compensation_operations=(
                frozen_package.allowed_actions.compensation_actions
            ),
            max_write_count=frozen_package.allowed_actions.max_write_count,
            max_payload_bytes=frozen_package.allowed_actions.max_payload_bytes,
            allow_missing=True,
        )
        expected_reason = (
            "assignment_not_delivered_to_daemon"
            if reservation.preparation_state == "manager_prepared"
            else "build_assignment_not_issued"
        )
        expected_finding = (
            "scanner_inconclusive:pre_daemon:"
            "assignment_not_delivered_to_daemon"
            if reservation.preparation_state == "manager_prepared"
            else (
                "scanner_inconclusive:preassignment:"
                "build_assignment_not_issued"
            )
        )
        if (
            scan.reason != expected_reason
            or manifest.status is not ArchiveStatus.invalid
            or sorted(manifest.forbidden_assistance_findings) != [expected_finding]
        ):
            raise TaskPackageConflict(
                "pre-assignment formal archive must remain explicitly inconclusive"
            )

        bridge_export = _read_json(run_root / "scanner-inputs/bridge.json")
        bridge_row = (
            bridge_export.get("assignment")
            if isinstance(bridge_export, Mapping)
            else None
        )
        bridge_events = (
            bridge_export.get("events")
            if isinstance(bridge_export, Mapping)
            else None
        )
        bridge_counts = (
            bridge_export.get("counts")
            if isinstance(bridge_export, Mapping)
            else None
        )
        bridge_watermark = (
            bridge_export.get("watermark")
            if isinstance(bridge_export, Mapping)
            else None
        )
        if not isinstance(bridge_row, Mapping):
            raise TaskPackageConflict(
                "pre-assignment bridge projection is unavailable"
            )
        try:
            request_payload = _strict_json_loads(
                str(bridge_row["request_json"]).encode("utf-8")
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise TaskPackageConflict(
                "pre-assignment formal request is unavailable"
            ) from error
        if not isinstance(request_payload, Mapping):
            raise TaskPackageConflict(
                "pre-assignment formal request is not an object"
            )
        request_payload_digest = _digest_bytes(_canonical_json(request_payload))
        request_digest = _digest_bytes(
            _canonical_json(
                {
                    "application_id": str(reservation.application_id),
                    "request": request_payload,
                }
            )
        )
        expected_request = {
            "idempotency_key": reservation.idempotency_key,
            "connection_id": str(reservation.connection_id),
            "task_id": reservation.task_id,
            "revision": reservation.revision,
            "environment_instance_id": reservation.environment_instance_id,
            "user_notified": True,
        }
        if (
            dict(request_payload) != expected_request
            or not hmac.compare_digest(
                reservation.request_payload_digest,
                request_payload_digest,
            )
            or not hmac.compare_digest(reservation.request_digest, request_digest)
            or not hmac.compare_digest(
                str(bridge_row.get("request_digest") or ""),
                request_digest,
            )
            or str(bridge_row.get("assignment_mode"))
            != AssignmentMode.formal_experiment.value
            or str(bridge_row.get("assignment_id")) != str(reservation.assignment_id)
            or str(bridge_row.get("application_id")) != str(reservation.application_id)
            or str(bridge_row.get("build_id")) != str(reservation.build_id)
            or str(bridge_row.get("session_id")) != str(reservation.session_id)
            or str(bridge_row.get("connection_id")) != str(reservation.connection_id)
            or str(bridge_row.get("phase")) != reservation.phase
            or str(bridge_row.get("status")) != reservation.status
            or str(bridge_row.get("desired_state")) != reservation.desired_state
            or str(bridge_row.get("terminal_events_drained_at"))
            != reservation.terminal_events_drained_at.isoformat()
            or bridge_row.get("daemon_session_creation_started_at")
            is not None
            or bridge_row.get("daemon_status") is not None
            or int(bridge_row.get("relay_cursor") or 0) != 0
            or int(bridge_row.get("ack_cursor") or 0) != 0
            or bridge_row.get("credential_ref") is not None
            or bridge_row.get("collaboration_credential_ref") is not None
            or bridge_row.get("formal_workspace_receipt_json") is not None
            or bridge_events != []
            or not isinstance(bridge_counts, Mapping)
            or bridge_counts.get("events") != 0
            or not isinstance(bridge_watermark, Mapping)
            or bridge_watermark.get("min_daemon_seq") is not None
            or bridge_watermark.get("max_daemon_seq") is not None
            or int(bridge_watermark.get("relay_cursor") or 0) != 0
            or int(bridge_watermark.get("ack_cursor") or 0) != 0
            or bridge_export.get("complete") is not True
        ):
            raise TaskPackageConflict(
                "pre-assignment archive differs from its durable reservation"
            )
        blackbox_auth = _read_json(
            run_root / "scanner-inputs/blackbox-auth.json"
        )
        blackbox_counts = (
            blackbox_auth.get("counts")
            if isinstance(blackbox_auth, Mapping)
            else None
        )
        blackbox_collections = (
            "credentials",
            "credential_applications",
            "requests",
            "audit",
            "security_events",
        )
        if (
            not isinstance(blackbox_auth, Mapping)
            or str(blackbox_auth.get("assignment_id"))
            != str(reservation.assignment_id)
            or str(blackbox_auth.get("session_id"))
            != str(reservation.session_id)
            or any(
                blackbox_auth.get(name) != []
                for name in blackbox_collections
            )
            or not isinstance(blackbox_counts, Mapping)
            or any(
                blackbox_counts.get(name) != 0
                for name in blackbox_collections
            )
        ):
            raise TaskPackageConflict(
                "pre-daemon archive contains credential or blackbox side effects"
            )

        prepared_assignment: BuildAssignment | None = None
        prepared_path = "manager-prepared-assignment.json"
        prepared_entry = declared_files.get(prepared_path)
        encoded_prepared = bridge_row.get("submission_json")
        if reservation.preparation_state == "manager_prepared":
            if (
                prepared_entry is None
                or reservation.manager_prepared_assignment_digest is None
                or not isinstance(encoded_prepared, str)
                or not encoded_prepared
            ):
                raise TaskPackageConflict(
                    "pre-daemon archive omitted its manager-prepared assignment"
                )
            prepared_payload = _read_bytes(
                run_root / prepared_path,
                limit=MAX_ARCHIVE_FILE_BYTES,
            )
            if (
                prepared_payload != encoded_prepared.encode("utf-8")
                or not hmac.compare_digest(
                    _digest_bytes(prepared_payload),
                    prepared_entry.digest,
                )
                or not hmac.compare_digest(
                    prepared_entry.digest,
                    reservation.manager_prepared_assignment_digest,
                )
                or len(prepared_payload) != prepared_entry.size_bytes
            ):
                raise TaskPackageConflict(
                    "manager-prepared assignment bytes changed"
                )
            try:
                prepared_assignment = BuildAssignment.model_validate_json(
                    prepared_payload
                )
                authorized_package, _, _ = (
                    self._authorize_formal_assignment(
                        prepared_assignment,
                        at=prepared_assignment.created_at,
                    )
                )
            except (TaskPackageError, ValueError) as error:
                raise TaskPackageConflict(
                    "manager-prepared assignment is not authorized"
                ) from error
            task_ref = prepared_assignment.task_package
            collaboration = prepared_assignment.collaboration
            if (
                authorized_package.task.task_id != manifest.task_id
                or authorized_package.task.revision != manifest.revision
                or task_ref is None
                or collaboration is None
                or prepared_assignment.mode
                is not AssignmentMode.formal_experiment
                or prepared_assignment.assignment_id
                != reservation.assignment_id
                or prepared_assignment.target.application_id
                != reservation.application_id
                or prepared_assignment.platform.application_ids
                != [reservation.application_id]
                or task_ref.task_id != reservation.task_id
                or task_ref.revision != reservation.revision
                or task_ref.run_id != reservation.run_id
                or task_ref.environment_instance_id
                != reservation.environment_instance_id
                or collaboration.channel_id != reservation.channel_id
                or prepared_assignment.created_at
                > reservation.terminal_events_drained_at
            ):
                raise TaskPackageConflict(
                    "manager-prepared assignment changed its reserved binding"
                )
        elif (
            prepared_entry is not None
            or reservation.manager_prepared_assignment_digest is not None
            or encoded_prepared is not None
        ):
            raise TaskPackageConflict(
                "request-only reservation contains manager-prepared assignment data"
            )
        collaboration_export = _read_json(
            run_root / "scanner-inputs/collaboration.json"
        )
        collaboration_channel = (
            collaboration_export.get("channel")
            if isinstance(collaboration_export, Mapping)
            else None
        )
        if not isinstance(collaboration_channel, Mapping):
            raise TaskPackageConflict(
                "pre-daemon collaboration projection is unavailable"
            )
        collaboration_complete = collaboration_export.get("complete")
        missing_collaboration_projection = (
            collaboration_complete is False
        )
        if missing_collaboration_projection:
            expected_projection_keys = {
                "schema_version",
                "complete",
                "missing_reason",
                "counts",
                "watermark",
                "channel",
                *_FORMAL_COLLABORATION_ARCHIVE_COLLECTIONS,
            }
            if (
                set(collaboration_export) != expected_projection_keys
                or collaboration_export.get("schema_version") != "1.0"
                or collaboration_export.get("missing_reason")
                != "collaboration_channel_not_created"
                or collaboration_export.get("counts")
                != {
                    name: 0
                    for name in _FORMAL_COLLABORATION_ARCHIVE_COLLECTIONS
                }
                or collaboration_export.get("watermark")
                != {
                    "min_message_seq": None,
                    "max_message_seq": None,
                    "next_seq": 1,
                    "max_report_evidence_rounds": None,
                    "report_evidence_rounds_used_total": 0,
                    "max_report_evidence_rounds_used": 0,
                    "budget_exhausted_reports": 0,
                }
                or any(
                    collaboration_export.get(name) != []
                    for name in _FORMAL_COLLABORATION_ARCHIVE_COLLECTIONS
                )
            ):
                raise TaskPackageConflict(
                    "pre-daemon missing collaboration projection changed"
                )
        if prepared_assignment is not None:
            common_collaboration_mismatch = (
                str(collaboration_channel.get("channel_id"))
                != str(reservation.channel_id)
                or str(collaboration_channel.get("assignment_id"))
                != str(reservation.assignment_id)
                or str(collaboration_channel.get("lilies_session_id"))
                != str(reservation.session_id)
            )
            complete_collaboration_mismatch = (
                collaboration_complete is True
                and (
                    collaboration_channel.get("task_id")
                    != reservation.task_id
                    or int(
                        collaboration_channel.get("task_revision") or 0
                    )
                    != reservation.revision
                    or collaboration_channel.get("application_ids")
                    != [str(reservation.application_id)]
                )
            )
            missing_collaboration_mismatch = (
                missing_collaboration_projection
                and dict(collaboration_channel)
                != {
                    "channel_id": str(reservation.channel_id),
                    "assignment_id": str(reservation.assignment_id),
                    "lilies_session_id": str(reservation.session_id),
                    "next_seq": 1,
                    "missing": True,
                }
            )
            if (
                common_collaboration_mismatch
                or collaboration_complete not in {True, False}
                or complete_collaboration_mismatch
                or missing_collaboration_mismatch
            ):
                raise TaskPackageConflict(
                    "manager-prepared collaboration projection changed"
                )
        elif (
            not missing_collaboration_projection
            or dict(collaboration_channel)
            != {
                "channel_id": str(reservation.channel_id),
                "task_id": reservation.task_id,
                "task_revision": reservation.revision,
                "assignment_id": str(reservation.assignment_id),
                "lilies_session_id": str(reservation.session_id),
                "application_ids": [str(reservation.application_id)],
                "next_seq": 1,
                "status": "closed",
            }
        ):
            raise TaskPackageConflict(
                "request-only collaboration absence projection changed"
            )

        validated_preflight = (
            self._validated_archived_preflight_failures(
                run_root,
                declared_files,
                task_id=reservation.task_id,
                revision=reservation.revision,
                run_id=reservation.run_id,
                assignment_id=reservation.assignment_id,
                environment_instance_id=(
                    reservation.environment_instance_id
                ),
            )
        )
        preflight_paths = {
            entry.path for entry, _ in validated_preflight
        }
        if reservation.preflight_evidence != [
            entry for entry, _ in validated_preflight
        ]:
            raise TaskPackageConflict(
                "pre-assignment archive does not exactly cover preflight attempts"
            )

        if (
            reservation.environment_ready_digest
            != manifest.environment_ready_digest
            or reservation.workspace_mount_digest
            != manifest.workspace_mount_digest
        ):
            raise TaskPackageConflict(
                "pre-assignment control-file digests changed"
            )
        ready: EnvironmentReady | None = None
        if manifest.environment_ready_digest is not None:
            try:
                ready = EnvironmentReady.model_validate(
                    _read_json(run_root / "environment-ready.json")
                )
            except ValueError as error:
                raise TaskPackageConflict(
                    "pre-assignment readiness evidence is invalid"
                ) from error
            if (
                ready.task_id != reservation.task_id
                or ready.revision != reservation.revision
                or ready.run_id != reservation.run_id
                or ready.assignment_id != reservation.assignment_id
                or ready.environment_instance_id
                != reservation.environment_instance_id
                or (
                    validated_preflight
                    and validated_preflight[-1][1].finished_at
                    > ready.started_at
                )
            ):
                raise TaskPackageConflict(
                    "pre-assignment readiness changed its reserved binding"
                )
        if manifest.workspace_mount_digest is not None:
            try:
                workspace = WorkspaceMountManifest.model_validate(
                    _read_json(run_root / "workspace-mount.json")
                )
            except ValueError as error:
                raise TaskPackageConflict(
                    "pre-assignment workspace evidence is invalid"
                ) from error
            if (
                ready is None
                or workspace.task_id != reservation.task_id
                or workspace.revision != reservation.revision
                or workspace.run_id != reservation.run_id
                or workspace.assignment_id != reservation.assignment_id
                or workspace.environment_ready_digest
                != reservation.environment_ready_digest
            ):
                raise TaskPackageConflict(
                    "pre-assignment workspace changed its reserved binding"
                )
        if prepared_assignment is not None:
            task_ref = prepared_assignment.task_package
            if (
                task_ref is None
                or task_ref.environment_ready_digest
                != reservation.environment_ready_digest
                or task_ref.workspace_mount_digest
                != reservation.workspace_mount_digest
            ):
                raise TaskPackageConflict(
                    "manager-prepared assignment changed its control evidence"
                )

        source = _read_json(run_root / "source-provenance/manifest.json")
        expected_source_missing_reason = (
            "assignment_not_delivered_to_daemon"
            if prepared_assignment is not None
            else "source_baseline_not_established"
        )
        if (
            not isinstance(source, Mapping)
            or source.get("complete") is not False
            or source.get("missing_reason")
            != expected_source_missing_reason
            or source.get("task_id") != reservation.task_id
            or source.get("task_revision") != reservation.revision
            or source.get("run_id") != reservation.run_id
            or str(source.get("assignment_id")) != str(reservation.assignment_id)
            or str(source.get("channel_id")) != str(reservation.channel_id)
        ):
            raise TaskPackageConflict(
                "pre-assignment source absence projection changed"
            )

        expected_scan_paths = {
            "evidence-index.json",
            "scanner-inputs/bridge.json",
            "scanner-inputs/collaboration.json",
            "scanner-inputs/workflow.json",
            "scanner-inputs/blackbox-auth.json",
            "connector-budget.json",
            "scanner-inputs/connector-budget.json",
            "scanner-inputs/artifact-inventory.json",
            "source-provenance/manifest.json",
            *preflight_paths,
        }
        if prepared_assignment is not None:
            expected_scan_paths.add("manager-prepared-assignment.json")
        if manifest.environment_ready_digest is not None:
            expected_scan_paths.add("environment-ready.json")
        if manifest.workspace_mount_digest is not None:
            expected_scan_paths.add("workspace-mount.json")
        scan_bindings = {entry.path: entry for entry in scan.input_bindings}
        if set(scan_bindings) != expected_scan_paths:
            raise TaskPackageConflict(
                "pre-assignment scanner does not cover every durable input"
            )
        for path, binding in scan_bindings.items():
            declared = declared_files.get(path)
            payload = _read_bytes(
                run_root / path,
                limit=MAX_ARCHIVE_FILE_BYTES,
            )
            if (
                declared is None
                or binding != declared
                or not hmac.compare_digest(
                    binding.digest,
                    _digest_bytes(payload),
                )
                or binding.size_bytes != len(payload)
            ):
                raise TaskPackageConflict(
                    "pre-assignment scanner input changed"
                )

        workflow = _read_json(run_root / "scanner-inputs/workflow.json")
        workflow_runs = (
            workflow.get("runs") if isinstance(workflow, Mapping) else None
        )
        if not isinstance(workflow_runs, list):
            raise TaskPackageConflict(
                "pre-assignment workflow denominator is unavailable"
            )
        workflow_run_ids: list[str] = []
        for item in workflow_runs:
            if not isinstance(item, Mapping):
                raise TaskPackageConflict(
                    "pre-assignment workflow denominator is invalid"
                )
            run_id = str(item.get("id") or "")
            state = item.get("state")
            if (
                not run_id
                or run_id in workflow_run_ids
                or not isinstance(state, Mapping)
                or str(state.get("assignment_id"))
                != str(reservation.assignment_id)
                or str(state.get("session_id")) != str(reservation.session_id)
            ):
                raise TaskPackageConflict(
                    "pre-assignment workflow run escaped its reservation"
                )
            workflow_run_ids.append(run_id)
        if result.business_run_ids != (
            workflow_run_ids or [reservation.run_id]
        ):
            raise TaskPackageConflict(
                "pre-assignment result omitted a workflow attempt"
            )

        evidence_paths = {
            path
            for path in declared_files
            if PurePosixPath(path).parts[0] in {"artifacts", "host-receipts"}
        }
        if {item.archive_path for item in evidence_index.entries} != evidence_paths:
            raise TaskPackageConflict(
                "pre-assignment evidence index does not cover archived bytes"
            )
        artifact_digests = sorted(
            item.digest
            for item in evidence_index.entries
            if item.kind == "artifact"
        )
        receipt_digests = sorted(
            item.digest
            for item in evidence_index.entries
            if item.kind == "host_receipt"
        )
        if (
            artifact_digests != sorted(result.artifact_digests)
            or receipt_digests != sorted(result.host_receipt_digests)
            or any(
                item.run_id not in result.business_run_ids
                for item in evidence_index.entries
            )
        ):
            raise TaskPackageConflict(
                "pre-assignment result changed its evidence denominator"
            )

    def _leak_markers(self, package: FrozenTaskPackage) -> list[str]:
        markers = [str((package.root / "protected").resolve())]
        marker_file = package.root / "protected" / "leak-markers.json"
        if marker_file.exists():
            value = _read_json(marker_file)
            if not isinstance(value, dict) or not isinstance(value.get("markers"), list):
                raise TaskPackageError("protected leak-markers.json has invalid schema")
            for marker in value["markers"]:
                if not isinstance(marker, str) or len(marker) < 8:
                    raise TaskPackageError("oracle leak markers must be strings of length >= 8")
                markers.append(marker)
        return markers

    def protected_leak_markers(
        self,
        package: FrozenTaskPackage,
    ) -> tuple[str, ...]:
        """Return protected canaries only for trusted archive/verifier guards."""

        package = self.load_frozen(
            package.task.task_id,
            package.task.revision,
            expected_sealed_digest=package.record.sealed_package_digest,
        )
        return tuple(self._leak_markers(package))

    def _protected_content_fingerprints(
        self,
        package: FrozenTaskPackage,
    ) -> tuple[set[str], set[str], tuple[bytes, ...], tuple[str, ...]]:
        exact: set[str] = set()
        canonical_json: set[str] = set()
        protected_payloads: list[bytes] = []
        protected_scalar_candidates: set[str] = set()
        public_text = ""
        for entry in package.record.immutable_files:
            if entry.path.startswith("protected/"):
                continue
            payload = _read_bytes(
                _resolved_child(package.root, entry.path),
                limit=MAX_ARCHIVE_FILE_BYTES,
            )
            try:
                public_text += "\n" + unicodedata.normalize(
                    "NFKC",
                    payload.decode("utf-8"),
                )
            except UnicodeDecodeError:
                continue
        for entry in package.record.immutable_files:
            if not entry.path.startswith("protected/"):
                continue
            payload = _read_bytes(
                _resolved_child(package.root, entry.path),
                limit=MAX_ARCHIVE_FILE_BYTES,
            )
            protected_payloads.append(payload)
            exact.add(_digest_bytes(payload))
            try:
                decoded = _strict_json_loads(payload)
                canonical_json.add(_digest_bytes(_canonical_json(decoded)))
                if entry.path == "protected/oracle/oracle.json":
                    oracle = OracleContract.model_validate(decoded)
                    for check in oracle.checks:
                        protected_scalar_candidates.update(
                            _walk_json_string_values(check.expected)
                        )
                else:
                    protected_scalar_candidates.update(
                        _walk_json_string_values(decoded)
                    )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                continue
        structural_values = {
            "1.0",
            "real_host",
            "file_exists",
            "file_sha256",
            "json_equals",
            "json_absent",
            "json_length",
            "completed",
            "succeeded",
            "failed",
            "cancelled",
            "invalid",
        }
        protected_scalars = tuple(
            sorted(
                candidate
                for candidate in protected_scalar_candidates
                if len(candidate.strip()) >= 6
                and candidate.strip().casefold() not in structural_values
                and unicodedata.normalize("NFKC", candidate) not in public_text
            )
        )
        return (
            exact,
            canonical_json,
            tuple(protected_payloads),
            protected_scalars,
        )

    @staticmethod
    def _contains_protected_payload(
        payload: bytes,
        protected_payloads: Sequence[bytes],
    ) -> bool:
        decoded_strings = _decoded_payload_strings(payload)
        for protected in protected_payloads:
            if not protected:
                continue
            if (
                protected in payload
                or base64.b64encode(protected) in payload
                or base64.urlsafe_b64encode(protected) in payload
                or protected.hex().encode("ascii") in payload
            ):
                return True
            try:
                protected_text = protected.decode("utf-8")
            except UnicodeDecodeError:
                continue
            normalized = unicodedata.normalize("NFKC", protected_text)
            if any(
                normalized in unicodedata.normalize("NFKC", decoded) for decoded in decoded_strings
            ):
                return True
        return False

    @staticmethod
    def _contains_protected_scalar(
        payload: bytes,
        protected_scalars: Sequence[str],
    ) -> bool:
        decoded_strings = [
            unicodedata.normalize("NFKC", value)
            for value in _decoded_payload_strings(payload)
        ]
        return any(
            unicodedata.normalize("NFKC", scalar) in decoded
            for scalar in protected_scalars
            for decoded in decoded_strings
        )

    @staticmethod
    def _looks_like_oracle_contract(payload: bytes) -> bool:
        try:
            value = _strict_json_loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return False
        return (
            isinstance(value, dict)
            and value.get("schema_version") == "1.0"
            and value.get("validation_mode") == "real_host"
            and isinstance(value.get("oracle_id"), str)
            and isinstance(value.get("task_id"), str)
            and isinstance(value.get("revision"), int)
            and isinstance(value.get("checks"), list)
        )

    def archive_run(
        self,
        package: FrozenTaskPackage,
        *,
        run_id: str,
        status: ArchiveStatus,
        validation_mode: ValidationMode,
        environment_ready_path: Path | None,
        workspace_manifest_path: Path | None,
        files: Mapping[str, bytes | Path],
        claim_binding: ArchiveClaimBinding | None,
        forbidden_assistance_findings: Sequence[str] = (),
    ) -> tuple[Path, RunArchiveManifest, str]:
        package = self.load_frozen(
            package.task.task_id,
            package.task.revision,
            expected_sealed_digest=package.record.sealed_package_digest,
        )
        run_id = TypeAdapter(OpaqueReference).validate_python(run_id)
        required = {
            "assignment.json",
            "draft.json",
            "messages.jsonl",
            "platform-events.jsonl",
            "collaboration.jsonl",
            "result.json",
        }
        if status is ArchiveStatus.succeeded and not required <= set(files):
            raise TaskPackageError(
                f"successful archive missing required files: {sorted(required - set(files))}"
            )
        if status is ArchiveStatus.succeeded and (
            environment_ready_path is None
            or workspace_manifest_path is None
            or claim_binding is None
            or validation_mode is not ValidationMode.real_host
        ):
            raise TaskPackageError(
                "a successful formal archive requires real health, workspace, and claim binding"
            )
        normalized_names = _unique_paths(list(files))
        if set(normalized_names) != set(files):
            raise TaskPackageSecurityError("archive file names are not canonical")
        reserved_inputs = {
            "archive-manifest.json",
            "environment-ready.json",
            "workspace-mount.json",
        }
        if any(
            name in reserved_inputs or PurePosixPath(name).parts[0] == "task"
            for name in normalized_names
        ):
            raise TaskPackageSecurityError("archive input collides with a manager-owned path")
        archive_assignment: BuildAssignment | None = None
        archive_reservation: ArchivedFormalReservation | None = None
        archive_validation_at: datetime | None = None
        if "assignment.json" in files:
            assignment_source = files["assignment.json"]
            try:
                assignment_payload = (
                    assignment_source
                    if isinstance(assignment_source, bytes)
                    else _read_bytes(Path(assignment_source))
                )
                archive_assignment = BuildAssignment.model_validate_json(
                    assignment_payload
                )
            except (OSError, ValueError, TaskPackageError) as error:
                raise TaskPackageSecurityError(
                    "archive assignment is not a trusted formal projection"
                ) from error
            task_ref = archive_assignment.task_package
            if (
                task_ref is None
                or task_ref.task_id != package.task.task_id
                or task_ref.revision != package.task.revision
                or task_ref.run_id != run_id
                or (
                    claim_binding is not None
                    and archive_assignment.assignment_id
                    != claim_binding.assignment_id
                )
                or (
                    claim_binding is not None
                    and archive_assignment.target.application_id is not None
                    and archive_assignment.target.application_id
                    != claim_binding.application_id
                )
            ):
                raise TaskPackageSecurityError(
                    "archive assignment changed its package or claim binding"
                )
            authorized_package, _, _ = self._authorize_formal_assignment(
                archive_assignment,
                at=archive_assignment.created_at,
            )
            if authorized_package.record != package.record:
                raise TaskPackageSecurityError(
                    "archive assignment belongs to another frozen package"
                )
            # Health readiness is evidence that gated assignment issuance. It is
            # replayed at that immutable timestamp, not falsely required to
            # remain live after a legitimate long-running assignment completes.
            archive_validation_at = archive_assignment.created_at
        if "reserved-assignment.json" in files:
            reservation_source = files["reserved-assignment.json"]
            try:
                reservation_payload = (
                    reservation_source
                    if isinstance(reservation_source, bytes)
                    else _read_bytes(Path(reservation_source))
                )
                archive_reservation = (
                    ArchivedFormalReservation.model_validate_json(
                        reservation_payload
                    )
                )
            except (OSError, ValueError, TaskPackageError) as error:
                raise TaskPackageSecurityError(
                    "formal reservation archive is invalid"
                ) from error
            if (
                archive_assignment is not None
                or claim_binding is not None
                or archive_reservation.task_id != package.task.task_id
                or archive_reservation.revision != package.task.revision
                or archive_reservation.run_id != run_id
            ):
                raise TaskPackageSecurityError(
                    "formal reservation archive changed its frozen identity"
                )
        request_files: list[dict[str, Any]] = []
        path_input_expectations: dict[str, tuple[str, int]] = {}
        for name in sorted(normalized_names):
            source = files[name]
            if isinstance(source, bytes):
                if len(source) > MAX_ARCHIVE_FILE_BYTES:
                    raise TaskPackageSecurityError("archive byte payload is too large")
                digest = _digest_bytes(source)
                size = len(source)
            else:
                digest, size = _digest_file(Path(source))
                path_input_expectations[name] = (digest, size)
            request_files.append({"path": name, "digest": digest, "size_bytes": size})
        ready_input: tuple[str, int] | None = None
        if environment_ready_path is not None:
            ready_input = _digest_file(Path(environment_ready_path))
            path_input_expectations["environment-ready.json"] = ready_input
        ready_input_digest = ready_input[0] if ready_input is not None else None
        mount_input: tuple[str, int] | None = None
        if workspace_manifest_path is not None:
            mount_input = _digest_file(Path(workspace_manifest_path))
            path_input_expectations["workspace-mount.json"] = mount_input
        mount_input_digest = mount_input[0] if mount_input is not None else None
        request_digest = _digest_bytes(
            _canonical_json(
                {
                    "schema_version": "1.0",
                    "task_id": package.task.task_id,
                    "revision": package.task.revision,
                    "run_id": run_id,
                    "status": status.value,
                    "validation_mode": validation_mode.value,
                    "environment_ready_digest": ready_input_digest,
                    "workspace_mount_digest": mount_input_digest,
                    "files": request_files,
                    "claim_binding": (
                        claim_binding.model_dump(mode="json") if claim_binding is not None else None
                    ),
                    "forbidden_assistance_findings": sorted(set(forbidden_assistance_findings)),
                }
            )
        )
        ready: EnvironmentReady | None = None
        ready_digest: str | None = None
        mount_digest: str | None = None
        if environment_ready_path is not None:
            control_assignment_id = (
                claim_binding.assignment_id
                if claim_binding is not None
                else archive_assignment.assignment_id
                if archive_assignment is not None
                else archive_reservation.assignment_id
                if archive_reservation is not None
                else None
            )
            if control_assignment_id is None:
                raise TaskPackageError(
                    "environment-ready archive requires a formal run binding"
                )
            if archive_validation_at is None:
                try:
                    archive_validation_at = EnvironmentReady.model_validate(
                        _read_json(Path(environment_ready_path))
                    ).finished_at
                except (OSError, ValueError, TaskPackageError) as error:
                    raise TaskPackageSecurityError(
                        "reserved readiness evidence is invalid"
                    ) from error
            ready, ready_digest = self.require_environment_ready(
                package,
                environment_ready_path,
                run_id=run_id,
                assignment_id=control_assignment_id,
                at=archive_validation_at,
            )
            if ready_input_digest is None or not hmac.compare_digest(
                ready_digest,
                ready_input_digest,
            ):
                raise TaskPackageConflict(
                    "environment-ready input changed while preparing the archive"
                )
        if workspace_manifest_path is not None:
            control_assignment_id = (
                claim_binding.assignment_id
                if claim_binding is not None
                else archive_assignment.assignment_id
                if archive_assignment is not None
                else archive_reservation.assignment_id
                if archive_reservation is not None
                else None
            )
            if control_assignment_id is None:
                raise TaskPackageError(
                    "workspace-bound archive requires a formal run binding"
                )
            _, mount_digest, _ = self.require_workspace_manifest(
                package,
                workspace_manifest_path,
                role=WorkspaceRole.lilies,
                run_id=run_id,
                assignment_id=control_assignment_id,
                environment_ready_digest=ready_digest,
                environment_instance_id=(
                    ready.environment_instance_id if ready is not None else None
                ),
            )
            if mount_input_digest is None or not hmac.compare_digest(
                mount_digest,
                mount_input_digest,
            ):
                raise TaskPackageConflict(
                    "workspace manifest input changed while preparing the archive"
                )
        if status is ArchiveStatus.succeeded and (
            ready_digest is None or mount_digest is None
        ):
            raise TaskPackageError(
                "a successful formal archive requires real health, workspace, and claim binding"
            )
        run_root = package.root / "runs" / _normalize_relative_path(run_id)
        if run_root.exists() or run_root.is_symlink():
            existing_manifest = run_root / "archive-manifest.json"
            if existing_manifest.exists():
                manifest = self.replay_archive(run_root)
                if not hmac.compare_digest(
                    manifest.request_digest,
                    request_digest,
                ):
                    raise TaskPackageConflict(
                        "run archive identity was reused with another request"
                    )
                manifest_digest = _digest_bytes(_read_bytes(existing_manifest))
                self._append_archive_index(package, manifest, manifest_digest)
                return run_root, manifest, manifest_digest
            raise TaskPackageConflict("run archive identity already exists but is incomplete")
        run_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(prefix=f".{run_root.name}.", dir=run_root.parent))
        entries: list[FileDigestEntry] = []
        security_findings: list[str] = []
        try:
            for entry in package.record.immutable_files:
                if entry.path.startswith("protected/"):
                    continue
                target_relative = f"task/{entry.path}"
                copied = _copy_regular(
                    _resolved_child(package.root, entry.path),
                    _resolved_child(temporary, target_relative),
                )
                if (
                    not hmac.compare_digest(copied.digest, entry.digest)
                    or copied.size_bytes != entry.size_bytes
                ):
                    raise TaskPackageConflict("frozen task changed while building the run archive")
                entries.append(
                    FileDigestEntry(
                        path=target_relative,
                        digest=copied.digest,
                        size_bytes=copied.size_bytes,
                    )
                )
            generated_files: dict[str, bytes | Path] = dict(files)
            if environment_ready_path is not None:
                generated_files["environment-ready.json"] = environment_ready_path
            if workspace_manifest_path is not None:
                generated_files["workspace-mount.json"] = workspace_manifest_path
            markers = self._leak_markers(package)
            (
                protected_exact,
                protected_canonical_json,
                protected_payloads,
                protected_scalars,
            ) = (
                self._protected_content_fingerprints(package)
            )
            for relative, source in generated_files.items():
                target = _resolved_child(temporary, relative)
                if isinstance(source, bytes):
                    if len(source) > MAX_ARCHIVE_FILE_BYTES:
                        raise TaskPackageSecurityError("archive byte payload is too large")
                    _atomic_write(target, source)
                    digest, size = _digest_file(target)
                else:
                    copied = _copy_regular(Path(source), target)
                    digest, size = copied.digest, copied.size_bytes
                    expected = path_input_expectations.get(relative)
                    if expected is None or (
                        not hmac.compare_digest(digest, expected[0])
                        or size != expected[1]
                    ):
                        raise TaskPackageConflict(
                            f"archive input changed while being sealed: {relative}"
                        )
                payload = _read_bytes(target, limit=MAX_ARCHIVE_FILE_BYTES)
                normalized_relative = _normalize_relative_path(relative)
                top_level = PurePosixPath(normalized_relative).parts[0]
                scalar_sensitive_surface = (
                    top_level not in {"artifacts", "host-receipts"}
                    and normalized_relative
                    not in {"result.json", "evidence-index.json"}
                )
                canonical_digest: str | None = None
                try:
                    canonical_digest = _digest_bytes(_canonical_json(_strict_json_loads(payload)))
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    TypeError,
                ):
                    pass
                if (
                    digest in protected_exact
                    or (
                        canonical_digest is not None
                        and canonical_digest in protected_canonical_json
                    )
                    or self._looks_like_oracle_contract(payload)
                    or self._contains_protected_payload(
                        payload,
                        protected_payloads,
                    )
                ):
                    security_findings.append(f"protected_oracle_content:{normalized_relative}")
                elif scalar_sensitive_surface and self._contains_protected_scalar(
                    payload,
                    protected_scalars,
                ):
                    security_findings.append(
                        f"protected_oracle_scalar:{normalized_relative}"
                    )
                elif any(
                    unicodedata.normalize("NFKC", marker) in unicodedata.normalize("NFKC", decoded)
                    for decoded in _decoded_payload_strings(payload)
                    for marker in markers
                ):
                    security_findings.append(f"protected_oracle_marker:{normalized_relative}")
                entries.append(FileDigestEntry(path=relative, digest=digest, size_bytes=size))
            if claim_binding is not None and status is ArchiveStatus.succeeded:
                archived_artifact_digests = sorted(
                    entry.digest
                    for entry in entries
                    if PurePosixPath(entry.path).parts[0] == "artifacts"
                )
                archived_receipt_digests = sorted(
                    entry.digest
                    for entry in entries
                    if PurePosixPath(entry.path).parts[0] == "host-receipts"
                )
                if archived_artifact_digests != sorted(claim_binding.artifact_digests):
                    raise TaskPackageConflict(
                        "archive artifacts do not match the frozen claim binding"
                    )
                if archived_receipt_digests != sorted(claim_binding.host_receipt_digests):
                    raise TaskPackageConflict(
                        "archive host receipts do not match the frozen claim binding"
                    )
            if claim_binding is not None and "draft.json" in generated_files:
                draft = _read_json(temporary / "draft.json")
                if not isinstance(draft, dict):
                    raise TaskPackageError("draft.json must be an object")
                if int(draft.get("revision", -1)) != claim_binding.draft_revision:
                    raise TaskPackageConflict("archive draft revision does not match claim")
                content_hash = str(draft.get("content_hash", ""))
                if not content_hash.startswith("sha256:"):
                    content_hash = f"sha256:{content_hash}"
                if not hmac.compare_digest(content_hash, claim_binding.content_hash):
                    raise TaskPackageConflict("archive draft content hash does not match claim")
                snapshot = ApplicationSnapshot.model_validate(draft.get("snapshot"))
                snapshot_hash = f"sha256:{snapshot.content_hash()}"
                if not hmac.compare_digest(snapshot_hash, claim_binding.content_hash):
                    raise TaskPackageConflict("archived draft bytes do not match content hash")
            if claim_binding is not None and "assignment.json" in generated_files:
                assignment_record = _read_json(temporary / "assignment.json")
                if (
                    not isinstance(assignment_record, dict)
                    or str(assignment_record.get("assignment_id"))
                    != str(claim_binding.assignment_id)
                    or assignment_record.get("mode") != "formal_experiment"
                ):
                    raise TaskPackageConflict(
                        "archived assignment does not match the formal claim binding"
                    )
            effective_status = (
                ArchiveStatus.invalid
                if security_findings or forbidden_assistance_findings
                else status
            )
            manifest = RunArchiveManifest(
                schema_version="1.0",
                task_id=package.task.task_id,
                revision=package.task.revision,
                run_id=run_id,
                source_status=status,
                status=effective_status,
                validation_mode=validation_mode,
                public_summary_digest=package.record.public_summary_digest,
                sealed_package_digest=package.record.sealed_package_digest,
                verification_process_digest=(
                    package.record.verification_process_digest
                ),
                environment_ready_digest=ready_digest,
                workspace_mount_digest=mount_digest,
                claim_binding=claim_binding,
                request_digest=request_digest,
                files=sorted(entries, key=lambda item: item.path),
                security_findings=sorted(set(security_findings)),
                forbidden_assistance_findings=sorted(set(forbidden_assistance_findings)),
                created_at=datetime.now(timezone.utc),
            )
            if (
                claim_binding is not None
                or "assignment.json" in generated_files
                or "reserved-assignment.json" in generated_files
            ):
                self._validate_archive_semantics(temporary, manifest)
            manifest_payload = _canonical_json(manifest)
            _atomic_write(
                temporary / "archive-manifest.json",
                manifest_payload,
                mode=0o400,
            )
            for entry in entries:
                os.chmod(_resolved_child(temporary, entry.path), 0o400)
            for directory in sorted(
                (path for path in temporary.rglob("*") if path.is_dir()),
                reverse=True,
            ):
                os.chmod(directory, 0o500)
            os.replace(temporary, run_root)
            os.chmod(run_root, 0o500)
            manifest_digest = _digest_bytes(manifest_payload)
            self._append_archive_index(package, manifest, manifest_digest)
            return run_root, manifest, manifest_digest
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def _append_archive_index(
        self,
        package: FrozenTaskPackage,
        manifest: RunArchiveManifest,
        manifest_digest: str,
    ) -> None:
        lock_path = self._archive_lock_path(
            package.task.task_id,
            package.task.revision,
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            index_path = package.root / "archive-manifest.json"
            index = ArchiveIndex.model_validate(_read_json(index_path))
            existing = {entry.run_id: entry for entry in index.runs}
            if manifest.run_id in existing:
                if not hmac.compare_digest(
                    existing[manifest.run_id].manifest_digest,
                    manifest_digest,
                ):
                    raise TaskPackageConflict("run index identity conflicts")
                return
            updated = index.model_copy(
                update={
                    "runs": [
                        *index.runs,
                        ArchiveIndexEntry(
                            run_id=manifest.run_id,
                            status=manifest.status,
                            manifest_digest=manifest_digest,
                            created_at=manifest.created_at,
                        ),
                    ]
                }
            )
            _atomic_write(index_path, _canonical_json(updated))
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

    def replay_archive(
        self,
        run_root: Path,
        *,
        expected_manifest_digest: str | None = None,
    ) -> RunArchiveManifest:
        lexical_root = Path(run_root)
        if lexical_root.is_symlink():
            raise TaskPackageConflict("archive root cannot be a symlink")
        run_root = lexical_root.resolve()
        if (
            not run_root.is_dir()
            or stat.S_IMODE(run_root.stat(follow_symlinks=False).st_mode) != 0o500
        ):
            raise TaskPackageConflict("archive root permissions changed")
        manifest_path = run_root / "archive-manifest.json"
        if (
            manifest_path.is_symlink()
            or stat.S_IMODE(manifest_path.stat(follow_symlinks=False).st_mode) != 0o400
        ):
            raise TaskPackageConflict("archive manifest permissions changed")
        payload = _read_bytes(manifest_path)
        digest = _digest_bytes(payload)
        if expected_manifest_digest is not None and not hmac.compare_digest(
            digest, expected_manifest_digest
        ):
            raise TaskPackageConflict("archive manifest digest changed")
        manifest = RunArchiveManifest.model_validate_json(payload)
        declared = {entry.path: entry for entry in manifest.files}
        control_bindings = (
            ("environment-ready.json", manifest.environment_ready_digest),
            ("workspace-mount.json", manifest.workspace_mount_digest),
        )
        for relative, expected_digest in control_bindings:
            entry = declared.get(relative)
            if (entry is None) != (expected_digest is None):
                raise TaskPackageConflict(
                    f"archive control-file binding changed: {relative}"
                )
            if (
                entry is not None
                and expected_digest is not None
                and not hmac.compare_digest(entry.digest, expected_digest)
            ):
                raise TaskPackageConflict(
                    f"archive control-file digest changed: {relative}"
                )
        actual: set[str] = set()
        for path in _iter_tree_files(run_root):
            relative = path.relative_to(run_root).as_posix()
            if relative == "archive-manifest.json":
                continue
            if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != 0o400:
                raise TaskPackageConflict(f"archive file permissions changed: {relative}")
            actual.add(relative)
            entry = declared.get(relative)
            if entry is None:
                raise TaskPackageConflict(f"unlisted archive file: {relative}")
            file_digest, size = _digest_file(path)
            if not hmac.compare_digest(file_digest, entry.digest) or size != entry.size_bytes:
                raise TaskPackageConflict(f"archive byte drift: {relative}")
        if actual != set(declared):
            raise TaskPackageConflict(f"archive files missing: {sorted(set(declared) - actual)}")
        for directory in (path for path in run_root.rglob("*") if path.is_dir()):
            if (
                directory.is_symlink()
                or stat.S_IMODE(directory.stat(follow_symlinks=False).st_mode) != 0o500
            ):
                raise TaskPackageConflict("archive directory permissions changed")
        self._validate_archive_semantics(run_root, manifest)
        return manifest

    def find_archive_by_digest(
        self,
        task_id: str,
        revision: int,
        manifest_digest: str,
    ) -> tuple[Path, RunArchiveManifest]:
        package = self.load_frozen(task_id, revision)
        index = ArchiveIndex.model_validate(_read_json(package.root / "archive-manifest.json"))
        if (
            index.task_id != task_id
            or index.revision != revision
            or not hmac.compare_digest(
                index.sealed_package_digest,
                package.record.sealed_package_digest,
            )
        ):
            raise TaskPackageConflict("archive index package binding changed")
        indexed = {entry.run_id: entry for entry in index.runs}
        actual: dict[str, tuple[Path, RunArchiveManifest, str]] = {}
        runs_root = package.root / "runs"
        for child in sorted(runs_root.iterdir(), key=lambda item: item.name):
            if child.is_symlink() or not child.is_dir():
                raise TaskPackageConflict("run archive root contains an unsafe entry")
            run_id = TypeAdapter(OpaqueReference).validate_python(child.name)
            if run_id != child.name:
                raise TaskPackageConflict("run archive identity is not canonical")
            manifest_path = child / "archive-manifest.json"
            digest = _digest_bytes(_read_bytes(manifest_path))
            manifest = self.replay_archive(
                child,
                expected_manifest_digest=digest,
            )
            if (
                manifest.task_id != task_id
                or manifest.revision != revision
                or manifest.run_id != run_id
                or not hmac.compare_digest(
                    manifest.sealed_package_digest,
                    package.record.sealed_package_digest,
                )
                or not hmac.compare_digest(
                    manifest.verification_process_digest,
                    package.record.verification_process_digest,
                )
            ):
                raise TaskPackageConflict("run archive manifest belongs to another package")
            actual[run_id] = (child, manifest, digest)
        if set(actual) != set(indexed):
            raise TaskPackageConflict("archive index does not account for every preserved run")
        for run_id, entry in indexed.items():
            _, manifest, digest = actual[run_id]
            if (
                manifest.status is not entry.status
                or manifest.created_at != entry.created_at
                or not hmac.compare_digest(digest, entry.manifest_digest)
            ):
                raise TaskPackageConflict("archive index entry changed")
        matches = [
            (root, manifest)
            for root, manifest, digest in actual.values()
            if hmac.compare_digest(digest, manifest_digest)
        ]
        if len(matches) != 1:
            raise TaskPackageError("archive manifest digest is not uniquely registered")
        return matches[0]

    def replay_registered_run(
        self,
        task_id: str,
        revision: int,
        run_id: str,
        *,
        expected_manifest_digest: str | None = None,
    ) -> RunArchiveManifest:
        run_id = TypeAdapter(OpaqueReference).validate_python(run_id)
        package = self.load_frozen(task_id, revision)
        index = ArchiveIndex.model_validate(_read_json(package.root / "archive-manifest.json"))
        matches = [entry for entry in index.runs if entry.run_id == run_id]
        if len(matches) != 1:
            raise TaskPackageError("run is not uniquely registered")
        if expected_manifest_digest is not None and not hmac.compare_digest(
            matches[0].manifest_digest,
            expected_manifest_digest,
        ):
            raise TaskPackageConflict("registered run digest does not match")
        root, manifest = self.find_archive_by_digest(
            task_id,
            revision,
            matches[0].manifest_digest,
        )
        if root.name != run_id or manifest.run_id != run_id:
            raise TaskPackageConflict("registered run identity changed")
        return manifest

    def validate_claim_binding(
        self,
        *,
        task_id: str,
        revision: int,
        claim: VerificationClaim,
    ) -> RunArchiveManifest:
        if (
            not isinstance(claim, VerificationClaim)
            or claim.schema_version != "1.1"
            or claim.status is not ClaimStatus.frozen
        ):
            raise TaskPackageConflict(
                "formal verification requires a frozen server-owned claim schema 1.1"
            )
        package_digest = getattr(claim, "task_package_digest", None)
        ready_digest = getattr(claim, "environment_ready_digest", None)
        archive_digest = getattr(claim, "archive_manifest_digest", None)
        process_digest = getattr(claim, "verification_process_digest", None)
        validation_mode = getattr(claim, "validation_mode", None)
        if not all(
            (package_digest, ready_digest, archive_digest, process_digest)
        ):
            raise TaskPackageConflict("claim is missing frozen package evidence")
        if validation_mode != ValidationMode.real_host.value:
            raise TaskPackageConflict("substitute validation cannot form a formal claim")
        package = self.load_frozen(
            task_id,
            revision,
            expected_public_digest=package_digest,
        )
        _, manifest = self.find_archive_by_digest(
            task_id,
            revision,
            archive_digest,
        )
        binding = manifest.claim_binding
        if (
            manifest.status is not ArchiveStatus.succeeded
            or manifest.validation_mode is not ValidationMode.real_host
            or manifest.security_findings
            or manifest.forbidden_assistance_findings
            or binding is None
            or not hmac.compare_digest(str(manifest.environment_ready_digest), str(ready_digest))
            or not hmac.compare_digest(
                manifest.sealed_package_digest,
                package.record.sealed_package_digest,
            )
            or not hmac.compare_digest(
                manifest.public_summary_digest,
                str(package_digest),
            )
            or not hmac.compare_digest(
                manifest.verification_process_digest,
                str(process_digest),
            )
            or not hmac.compare_digest(
                package.record.verification_process_digest,
                str(process_digest),
            )
        ):
            raise TaskPackageConflict("claim references a non-claimable run archive")
        self.load_verification_policy_bundle(str(process_digest))
        comparisons = {
            "claim_id": str(claim.claim_id) == str(binding.claim_id),
            "assignment_id": claim.assignment_id == binding.assignment_id,
            "application_id": str(claim.application_id) == str(binding.application_id),
            "draft_revision": claim.draft_revision == binding.draft_revision,
            "content_hash": hmac.compare_digest(claim.content_hash, binding.content_hash),
            "published_version": claim.published_version == binding.published_version,
            "test_run_ids": claim.test_run_ids == binding.test_run_ids,
            "business_run_ids": claim.business_run_ids == binding.business_run_ids,
            "artifact_digests": [item.digest for item in claim.artifact_refs]
            == binding.artifact_digests,
            "host_receipt_digests": [item.digest for item in claim.host_receipt_refs]
            == binding.host_receipt_digests,
            "resolved_report_ids": (claim.resolved_report_ids == binding.resolved_report_ids),
            "remaining_limits": claim.remaining_limits == binding.remaining_limits,
            "artifact_metadata": all(
                item.kind is EvidenceKind.artifact and item.captured_at <= claim.created_at
                for item in claim.artifact_refs
            ),
            "host_receipt_metadata": all(
                item.kind is EvidenceKind.host_receipt and item.captured_at <= claim.created_at
                for item in claim.host_receipt_refs
            ),
        }
        if not all(comparisons.values()):
            failed = sorted(key for key, ok in comparisons.items() if not ok)
            raise TaskPackageConflict(f"claim/archive binding mismatch: {failed}")
        return manifest
