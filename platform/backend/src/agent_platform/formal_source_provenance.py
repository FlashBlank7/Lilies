from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .collaboration_models import (
    ApprovalDecision,
    CollaborationMessageEnvelope,
    DeveloperOutcome,
    DeveloperResponse,
    MessageType,
    ReportDecision,
    SenderRole,
)
from .lilies_models import Digest, OpaqueReference


GitObjectId = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
]
SafeSourcePath = Annotated[str, StringConstraints(min_length=1, max_length=4_096)]

SOURCE_PROVENANCE_MANIFEST_PATH = "source-provenance/manifest.json"
DEVELOPER_SOURCE_MANIFEST_FILE = ".lilies-source-manifest.json"
MAX_GIT_STATUS_BYTES = 8 * 1024 * 1024
MAX_COMMIT_OBJECT_BYTES = 2 * 1024 * 1024
MAX_TREE_OBJECT_BYTES = 32 * 1024 * 1024
MAX_BLOB_OBJECT_BYTES = 32 * 1024 * 1024
MAX_BINARY_DIFF_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_PAYLOAD_BYTES = 256 * 1024 * 1024

_FORBIDDEN_SOURCE_SEGMENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".env",
        "__pycache__",
        "credentials",
        "expected-state",
        "oracle",
        "platform-data",
        "platform_data",
        "protected",
        "secrets",
    }
)
_FORBIDDEN_SOURCE_PREFIXES = (
    ".tmp",
    "data",
    "platform/backend/data",
    "platform/frontend/data",
)
_ALLOWED_FILE_MODES = frozenset({"100644", "100755"})
_DEVELOPER_PROJECTION_PREFIXES = ("platform/", "tests/", "scripts/")
_DEVELOPER_PROJECTION_FILES: frozenset[str] = frozenset()
_DEVELOPER_PROJECTION_DENIED_SEGMENTS = (
    _FORBIDDEN_SOURCE_SEGMENTS | {"data"}
)
# These files decide what a formal run may claim, what evidence is archived,
# and which bytes the independent verifier executes.  A developer can repair
# ordinary platform/runtime code, but the same promotion must never be able to
# rewrite its own evidence, replay, scanner, or verification authority.
DEVELOPER_TRUST_ROOT_PATHS = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "platform/backend/src/agent_platform/__init__.py",
        "platform/backend/src/agent_platform/api.py",
        "platform/backend/src/agent_platform/capability_contracts.py",
        "platform/backend/src/agent_platform/collaboration_api.py",
        "platform/backend/src/agent_platform/collaboration_models.py",
        "platform/backend/src/agent_platform/collaboration_service.py",
        "platform/backend/src/agent_platform/collaboration_storage.py",
        "platform/backend/src/agent_platform/config.py",
        "platform/backend/src/agent_platform/forbidden_assistance_scanner.py",
        "platform/backend/src/agent_platform/formal_assignment_broker.py",
        "platform/backend/src/agent_platform/formal_assignment_runtime.py",
        "platform/backend/src/agent_platform/formal_developer_worker_broker.py",
        "platform/backend/src/agent_platform/formal_run_archiver.py",
        "platform/backend/src/agent_platform/formal_source_provenance.py",
        "platform/backend/src/agent_platform/formal_verification_contracts.py",
        "platform/backend/src/agent_platform/formal_workspace.py",
        "platform/backend/src/agent_platform/independent_verifier.py",
        "platform/backend/src/agent_platform/independent_verifier_broker.py",
        "platform/backend/src/agent_platform/lilies_models.py",
        "platform/backend/src/agent_platform/local_lilies_bridge.py",
        "platform/backend/src/agent_platform/local_lilies_bridge_api.py",
        "platform/backend/src/agent_platform/models.py",
        "platform/backend/src/agent_platform/platform_blackbox_artifacts.py",
        "platform/backend/src/agent_platform/platform_blackbox_auth.py",
        "platform/backend/src/agent_platform/stable_verification.py",
        "platform/backend/src/agent_platform/stable_verification_cli.py",
        "platform/backend/src/agent_platform/stable_verification_coordinator.py",
        "platform/backend/src/agent_platform/task_packages.py",
        "platform/backend/src/agent_platform/workflow_models.py",
        "platform/backend/src/agent_platform/workflow_storage.py",
    }
)


@cache
def _process_identity_for_pid(process_id: int) -> UUID:
    """Return one stable token per OS process, including after ``fork``."""

    if process_id != os.getpid():
        raise FormalSourceProvenanceSecurityError(
            "process identity was requested for another OS process"
        )
    return uuid4()


class FormalSourceProvenanceError(RuntimeError):
    """A formal developer source claim is not reproducible from Git."""


class FormalSourceProvenanceConflict(FormalSourceProvenanceError):
    """A durable source-provenance identity was reused with other content."""


class FormalSourceProvenanceSecurityError(FormalSourceProvenanceError):
    """Source evidence crossed the formal developer security boundary."""


@dataclass(frozen=True)
class _KernelBootIdentity:
    digest: str
    started_at: datetime


def _current_kernel_boot_identity() -> _KernelBootIdentity:
    identity: bytes
    started_at: datetime
    if sys.platform == "darwin":
        class Timeval(ctypes.Structure):
            _fields_ = [
                ("tv_sec", ctypes.c_long),
                ("tv_usec", ctypes.c_int),
            ]

        boot_time = Timeval()
        size = ctypes.c_size_t(ctypes.sizeof(boot_time))
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            sysctlbyname = libc.sysctlbyname
        except AttributeError as error:
            raise FormalSourceProvenanceSecurityError(
                "kernel boot identity is unavailable"
            ) from error
        sysctlbyname.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        sysctlbyname.restype = ctypes.c_int
        if (
            sysctlbyname(
                b"kern.boottime",
                ctypes.byref(boot_time),
                ctypes.byref(size),
                None,
                0,
            )
            != 0
            or size.value != ctypes.sizeof(boot_time)
        ):
            raise FormalSourceProvenanceSecurityError(
                "kernel boot identity is unavailable"
            )
        identity = (
            f"darwin:{boot_time.tv_sec}:{boot_time.tv_usec}"
        ).encode("ascii")
        started_at = datetime.fromtimestamp(
            boot_time.tv_sec + boot_time.tv_usec / 1_000_000,
            timezone.utc,
        )
    elif sys.platform.startswith("linux"):
        try:
            boot_uuid = UUID(
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="ascii")
                .strip()
            )
            boot_seconds = next(
                int(line.split()[1])
                for line in Path("/proc/stat")
                .read_text(encoding="ascii")
                .splitlines()
                if line.startswith("btime ")
            )
        except (OSError, StopIteration, ValueError) as error:
            raise FormalSourceProvenanceSecurityError(
                "kernel boot identity is unavailable"
            ) from error
        identity = f"linux:{boot_uuid}".encode("ascii")
        started_at = datetime.fromtimestamp(boot_seconds, timezone.utc)
    else:
        raise FormalSourceProvenanceSecurityError(
            "kernel boot identity is unavailable on this platform"
        )
    return _KernelBootIdentity(
        digest=f"sha256:{hashlib.sha256(identity).hexdigest()}",
        started_at=started_at,
    )


_CODE_GENERATION_BOOT = _current_kernel_boot_identity()
_CODE_GENERATION_MONOTONIC_NS = time.monotonic_ns()
_CODE_GENERATION_LOADED_AT = datetime.now(timezone.utc)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing an inode at ``destination``."""

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    result: int
    try:
        if sys.platform == "darwin":
            renamex_np = libc.renamex_np
            renamex_np.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renamex_np.restype = ctypes.c_int
            # Darwin sys/stdio.h: RENAME_EXCL.
            result = renamex_np(source_bytes, destination_bytes, 0x00000004)
        elif sys.platform.startswith("linux"):
            renameat2 = libc.renameat2
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            # Linux fs.h: RENAME_NOREPLACE.
            result = renameat2(
                -100,
                source_bytes,
                -100,
                destination_bytes,
                0x00000001,
            )
        else:
            raise FormalSourceProvenanceSecurityError(
                "atomic no-replace rename is unavailable on this platform"
            )
    except AttributeError as error:
        raise FormalSourceProvenanceSecurityError(
            "atomic no-replace rename is unavailable on this platform"
        ) from error
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination,
        )
    if error_number in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
        raise FormalSourceProvenanceSecurityError(
            "kernel rejected atomic no-replace rename support"
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        source,
        destination,
    )


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
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
    ).encode("utf-8")


def _digest(payload: bytes | BaseModel | Mapping[str, Any]) -> str:
    encoded = payload if isinstance(payload, bytes) else _canonical_json(payload)
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("source provenance timestamps must use UTC")
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("source provenance JSON contains a duplicate key")
        value[key] = item
    return value


def _strict_json(payload: bytes) -> Any:
    try:
        return json.loads(
            payload,
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise FormalSourceProvenanceSecurityError(
            "source provenance JSON is not canonical valid JSON"
        ) from error


def _contains_dotenv_segment(value: str) -> bool:
    return any(
        part == ".env" or part.startswith(".env.")
        for part in (segment.casefold() for segment in PurePosixPath(value).parts)
    )


def _safe_repository_path(value: str) -> str:
    if (
        "\x00" in value
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("developer source path is not a safe normalized path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ValueError("developer source path escapes the repository")
    return path.as_posix()


def _safe_source_path(value: str) -> str:
    normalized = _safe_repository_path(value)
    path = PurePosixPath(normalized)
    folded_parts = tuple(part.casefold() for part in path.parts)
    if any(part in _FORBIDDEN_SOURCE_SEGMENTS for part in folded_parts):
        raise ValueError("developer source path enters reserved runtime or protected state")
    folded = normalized.casefold()
    if any(
        folded == prefix or folded.startswith(f"{prefix}/")
        for prefix in _FORBIDDEN_SOURCE_PREFIXES
    ):
        raise ValueError("developer source path enters reserved runtime or protected state")
    return normalized


def _safe_archive_path(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise ValueError("source archive path must be POSIX relative")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ValueError("source archive path escapes the archive")
    normalized = path.as_posix()
    if not normalized.startswith("source-provenance/"):
        raise ValueError("source archive payload is outside source-provenance")
    return normalized


class GitWorktreeState(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    object_format: Literal["sha1", "sha256"]
    head_commit_sha: GitObjectId
    head_tree_sha: GitObjectId
    status_digest: Digest
    tracked_change_count: int = Field(ge=0)
    untracked_file_count: int = Field(ge=0)
    conflicted_path_count: int = Field(ge=0)
    clean: bool

    @model_validator(mode="after")
    def clean_matches_counts(self) -> GitWorktreeState:
        expected_length = 40 if self.object_format == "sha1" else 64
        if len(self.head_commit_sha) != expected_length or len(self.head_tree_sha) != expected_length:
            raise ValueError("Git object IDs do not match the repository object format")
        expected_clean = (
            self.tracked_change_count == 0
            and self.untracked_file_count == 0
            and self.conflicted_path_count == 0
        )
        if self.clean != expected_clean:
            raise ValueError("Git clean state does not match its status counters")
        if self.clean and not hmac.compare_digest(
            self.status_digest,
            _digest(b""),
        ):
            raise ValueError("clean Git source state has a non-empty status digest")
        return self


class FormalSourceBaseline(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=3, max_length=160)
    task_revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    channel_id: UUID
    source_state: GitWorktreeState
    captured_at: datetime
    baseline_digest: Digest

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def digest_and_clean_state_match(self) -> FormalSourceBaseline:
        if not self.source_state.clean:
            raise ValueError("formal source baseline must be clean")
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"baseline_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.baseline_digest):
            raise ValueError("formal source baseline digest does not match")
        return self


class DeveloperSourceProjectionEntry(_FrozenModel):
    """One repository-relative regular blob disclosed to the developer."""

    schema_version: Literal["1.0"] = "1.0"
    path: SafeSourcePath
    mode: Literal["100644", "100755"]
    blob_sha: GitObjectId
    digest: Digest
    size_bytes: int = Field(ge=0, le=MAX_BLOB_OBJECT_BYTES)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return _safe_source_path(value)


class DeveloperSourceProjectionManifest(_FrozenModel):
    """Immutable Git-object projection used to create a no-``.git`` workspace."""

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=3, max_length=160)
    task_revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    channel_id: UUID
    object_format: Literal["sha1", "sha256"]
    branch_ref: str = Field(
        min_length=6,
        max_length=1_024,
        pattern=r"^refs/heads/[A-Za-z0-9._/-]+$",
    )
    baseline_commit_sha: GitObjectId
    baseline_tree_sha: GitObjectId
    entries: list[DeveloperSourceProjectionEntry] = Field(
        min_length=1,
        max_length=200_000,
    )
    allowed_new_prefixes: list[SafeSourcePath] = Field(max_length=100)
    allowed_new_files: list[SafeSourcePath] = Field(max_length=100)
    captured_at: datetime
    manifest_digest: Digest

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("allowed_new_prefixes", "allowed_new_files")
    @classmethod
    def allowed_paths_are_safe(cls, value: list[str]) -> list[str]:
        normalized = [_safe_source_path(item.rstrip("/")) for item in value]
        if normalized != sorted(set(normalized)):
            raise ValueError("developer source allowlist must be sorted and unique")
        return normalized

    @model_validator(mode="after")
    def projection_is_exact(self) -> DeveloperSourceProjectionManifest:
        expected_length = 40 if self.object_format == "sha1" else 64
        if (
            len(self.baseline_commit_sha) != expected_length
            or len(self.baseline_tree_sha) != expected_length
        ):
            raise ValueError("projection Git object IDs use another object format")
        paths = [entry.path for entry in self.entries]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("developer source projection entries must be sorted and unique")
        if any(not _is_projected_source_path(path) for path in paths):
            raise ValueError(
                "developer source projection contains a protected trust-root path"
            )
        if DEVELOPER_SOURCE_MANIFEST_FILE in paths:
            raise ValueError("developer source manifest cannot project itself")
        if (
            self.allowed_new_prefixes
            != [prefix.rstrip("/") for prefix in sorted(_DEVELOPER_PROJECTION_PREFIXES)]
            or self.allowed_new_files != sorted(_DEVELOPER_PROJECTION_FILES)
        ):
            raise ValueError("developer source projection allowlist is not canonical")
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"manifest_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.manifest_digest):
            raise ValueError("developer source projection manifest digest does not match")
        return self

    def permits_new_path(self, path: str) -> bool:
        normalized = _safe_source_path(path)
        if (
            normalized in DEVELOPER_TRUST_ROOT_PATHS
            or _contains_dotenv_segment(normalized)
        ):
            return False
        return normalized in self.allowed_new_files or any(
            normalized.startswith(f"{prefix.rstrip('/')}/")
            for prefix in self.allowed_new_prefixes
        )


class DeveloperSourcePromotionBlob(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    path: SafeSourcePath
    mode: Literal["100644", "100755"]
    blob_sha: GitObjectId
    payload_digest: Digest
    size_bytes: int = Field(ge=0, le=MAX_BLOB_OBJECT_BYTES)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return _safe_source_path(value)


class DeveloperSourcePromotionIntent(_FrozenModel):
    """Frozen workspace delta written before any Git ref or worktree mutation."""

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    lease_id: UUID
    lease_owner_id: str = Field(min_length=1, max_length=512)
    response_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=512)
    workspace_manifest_digest: Digest
    source_manifest_digest: Digest
    branch_ref: str = Field(min_length=6, max_length=1_024)
    parent_commit_sha: GitObjectId
    parent_tree_sha: GitObjectId
    changes: list[GitPathChange] = Field(min_length=1, max_length=5_000)
    target_blobs: list[DeveloperSourcePromotionBlob] = Field(max_length=5_000)
    created_at: datetime
    intent_digest: Digest

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def intent_is_exact(self) -> DeveloperSourcePromotionIntent:
        paths = [change.path for change in self.changes]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("developer source promotion paths must be sorted and unique")
        target_paths = [blob.path for blob in self.target_blobs]
        expected_targets = [
            change.path for change in self.changes if change.new_blob_sha is not None
        ]
        if target_paths != expected_targets:
            raise ValueError("promotion target blobs do not exactly cover target files")
        for change, blob in zip(
            (item for item in self.changes if item.new_blob_sha is not None),
            self.target_blobs,
            strict=True,
        ):
            if change.new_blob_sha != blob.blob_sha or change.new_mode != blob.mode:
                raise ValueError("promotion target blob differs from its Git change")
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"intent_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.intent_digest):
            raise ValueError("developer source promotion intent digest does not match")
        return self


class DeveloperSourcePromotionReceipt(_FrozenModel):
    """Trusted proof that the frozen delta became a single-parent active commit."""

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    lease_id: UUID
    response_id: UUID
    workspace_manifest_digest: Digest
    source_manifest_digest: Digest
    intent_digest: Digest
    branch_ref: str = Field(min_length=6, max_length=1_024)
    parent_commit_sha: GitObjectId
    parent_tree_sha: GitObjectId
    commit_sha: GitObjectId
    tree_sha: GitObjectId
    changed_paths: list[SafeSourcePath] = Field(min_length=1, max_length=5_000)
    object_state: Literal["object_created"]
    activation_state: Literal["activated"]
    reload_status: Literal["not_required", "restart_required", "confirmed"]
    object_created_at: datetime
    activated_at: datetime
    process_instance_id: UUID
    receipt_digest: Digest

    @field_validator("object_created_at", "activated_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("changed_paths")
    @classmethod
    def paths_are_safe(cls, value: list[str]) -> list[str]:
        normalized = [_safe_source_path(item) for item in value]
        if normalized != sorted(set(normalized)):
            raise ValueError("promotion receipt paths must be sorted and unique")
        return normalized

    @model_validator(mode="after")
    def receipt_is_exact(self) -> DeveloperSourcePromotionReceipt:
        if self.activated_at < self.object_created_at:
            raise ValueError("promotion activation predates object creation")
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"receipt_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.receipt_digest):
            raise ValueError("developer source promotion receipt digest does not match")
        return self


class DeveloperSourceActivationFence(_FrozenModel):
    """Trusted code-generation boundary captured only after activation."""

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    response_id: UUID
    receipt_digest: Digest
    intent_digest: Digest
    branch_ref: str = Field(min_length=6, max_length=1_024)
    commit_sha: GitObjectId
    tree_sha: GitObjectId
    activation_process_instance_id: UUID
    activation_boot_id: Digest
    activation_boot_started_at: datetime
    activation_monotonic_ns: int = Field(ge=1)
    activated_at: datetime
    fence_digest: Digest

    @field_validator("activation_boot_started_at", "activated_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def fence_is_exact(self) -> DeveloperSourceActivationFence:
        if self.activated_at < self.activation_boot_started_at:
            raise ValueError("activation fence predates its kernel boot")
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"fence_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.fence_digest):
            raise ValueError("developer activation fence digest does not match")
        return self


class DeveloperSourcePromotionAdoption(_FrozenModel):
    """Audited authorization to use an activated receipt under a new lease."""

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    lease_id: UUID
    lease_owner_id: str = Field(min_length=1, max_length=512)
    response_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=512)
    receipt_digest: Digest
    workspace_manifest_digest: Digest
    source_manifest_digest: Digest
    adopted_at: datetime
    adoption_digest: Digest

    @field_validator("adopted_at")
    @classmethod
    def adopted_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def adoption_is_exact(self) -> DeveloperSourcePromotionAdoption:
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"adoption_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.adoption_digest):
            raise ValueError("developer source promotion adoption digest does not match")
        return self


class DeveloperSourceReloadConfirmation(_FrozenModel):
    """Audited proof that another process reloaded the exact active promotion."""

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    response_id: UUID
    receipt_digest: Digest
    intent_digest: Digest
    branch_ref: str = Field(min_length=6, max_length=1_024)
    hidden_ref: str = Field(min_length=6, max_length=2_048)
    commit_sha: GitObjectId
    tree_sha: GitObjectId
    changed_paths: list[SafeSourcePath] = Field(min_length=1, max_length=5_000)
    status: Literal["confirmed"]
    activation_process_instance_id: UUID
    confirming_process_instance_id: UUID
    activation_fence_digest: Digest
    activation_boot_id: Digest
    activation_boot_started_at: datetime
    activation_monotonic_ns: int = Field(ge=1)
    process_generation_boot_id: Digest
    process_generation_boot_started_at: datetime
    process_generation_monotonic_ns: int = Field(ge=1)
    process_generation_loaded_at: datetime
    confirmed_at: datetime
    confirmation_digest: Digest

    @field_validator(
        "activation_boot_started_at",
        "process_generation_boot_started_at",
        "process_generation_loaded_at",
        "confirmed_at",
    )
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("changed_paths")
    @classmethod
    def paths_are_safe(cls, value: list[str]) -> list[str]:
        normalized = [_safe_source_path(item) for item in value]
        if normalized != sorted(set(normalized)):
            raise ValueError("reload confirmation paths must be sorted and unique")
        return normalized

    @model_validator(mode="after")
    def confirmation_is_exact(self) -> DeveloperSourceReloadConfirmation:
        if (
            self.activation_process_instance_id
            == self.confirming_process_instance_id
        ):
            raise ValueError("developer code reload requires another process instance")
        if self.confirmed_at < self.process_generation_loaded_at:
            raise ValueError("reload confirmation predates its code generation")
        if (
            self.process_generation_loaded_at
            < self.process_generation_boot_started_at
        ):
            raise ValueError("code generation predates its kernel boot")
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"confirmation_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.confirmation_digest):
            raise ValueError("developer reload confirmation digest does not match")
        return self


def _code_generation_follows_activation(
    *,
    fence: DeveloperSourceActivationFence,
    process_boot_id: str,
    process_boot_started_at: datetime,
    process_monotonic_ns: int,
) -> bool:
    if process_boot_id == fence.activation_boot_id:
        return (
            process_boot_started_at == fence.activation_boot_started_at
            and process_monotonic_ns > fence.activation_monotonic_ns
        )
    return process_boot_started_at > fence.activated_at


class _DeveloperSourcePromotionAbort(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    response_id: UUID
    intent_digest: Digest
    branch_ref: str = Field(min_length=6, max_length=1_024)
    parent_commit_sha: GitObjectId
    status: Literal["aborted_after_rollback"]
    aborted_at: datetime
    abort_digest: Digest

    @field_validator("aborted_at")
    @classmethod
    def aborted_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def abort_is_exact(self) -> _DeveloperSourcePromotionAbort:
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"abort_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.abort_digest):
            raise ValueError("developer promotion abort digest does not match")
        return self


class _DeveloperSourceObjectReceipt(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    response_id: UUID
    intent_digest: Digest
    parent_commit_sha: GitObjectId
    commit_sha: GitObjectId
    tree_sha: GitObjectId
    hidden_ref: str = Field(min_length=6, max_length=2_048)
    created_at: datetime
    receipt_digest: Digest

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def digest_matches(self) -> _DeveloperSourceObjectReceipt:
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"receipt_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.receipt_digest):
            raise ValueError("Git object creation receipt digest does not match")
        return self


class ApprovedDeveloperResponseBinding(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    channel_id: UUID
    report_id: UUID
    approval_id: UUID
    approval_message_id: UUID
    approval_message_seq: int = Field(ge=1)
    approval_authority: Literal["user", "task_auto_forward"]
    approval_payload_digest: Digest
    approved_report_revision: int = Field(ge=1)
    response_id: UUID
    response_message_id: UUID
    response_message_seq: int = Field(ge=1)
    response_report_revision: int = Field(ge=1)
    response_payload_digest: Digest
    commit_sha: GitObjectId

    @model_validator(mode="after")
    def response_follows_approval(self) -> ApprovedDeveloperResponseBinding:
        if self.response_message_seq <= self.approval_message_seq:
            raise ValueError("DeveloperResponse must follow its approval")
        if self.response_report_revision < self.approved_report_revision:
            raise ValueError("DeveloperResponse predates its approved report revision")
        return self


class GitPathChange(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    path: SafeSourcePath
    change_kind: Literal["added", "deleted", "modified", "type_changed"]
    old_mode: Literal["100644", "100755"] | None = None
    new_mode: Literal["100644", "100755"] | None = None
    old_blob_sha: GitObjectId | None = None
    new_blob_sha: GitObjectId | None = None

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return _safe_source_path(value)

    @model_validator(mode="after")
    def endpoints_match_change_kind(self) -> GitPathChange:
        if self.change_kind == "added":
            if (
                self.old_mode is not None
                or self.old_blob_sha is not None
                or self.new_mode is None
                or self.new_blob_sha is None
            ):
                raise ValueError("added source change has invalid Git endpoints")
        elif self.change_kind == "deleted":
            if (
                self.old_mode is None
                or self.old_blob_sha is None
                or self.new_mode is not None
                or self.new_blob_sha is not None
            ):
                raise ValueError("deleted source change has invalid Git endpoints")
        elif (
            self.old_mode is None
            or self.old_blob_sha is None
            or self.new_mode is None
            or self.new_blob_sha is None
        ):
            raise ValueError("modified source change requires both Git endpoints")
        return self


DeveloperSourcePromotionIntent.model_rebuild()


class ArchivedGitObject(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    object_type: Literal["commit", "tree", "blob"]
    oid: GitObjectId
    archive_path: str = Field(min_length=1, max_length=4_096)
    payload_digest: Digest
    size_bytes: int = Field(ge=0, le=MAX_BLOB_OBJECT_BYTES)

    @field_validator("archive_path")
    @classmethod
    def archive_path_is_safe(cls, value: str) -> str:
        return _safe_archive_path(value)

    @model_validator(mode="after")
    def path_matches_object(self) -> ArchivedGitObject:
        suffixes = {
            "commit": ".commit",
            "tree": ".tree",
            "blob": ".blob",
        }
        prefixes = {
            "commit": "source-provenance/commits/",
            "tree": "source-provenance/trees/",
            "blob": "source-provenance/objects/",
        }
        suffix = suffixes[self.object_type]
        expected_prefix = prefixes[self.object_type]
        if not self.archive_path.startswith(expected_prefix) or not self.archive_path.endswith(
            suffix
        ):
            raise ValueError("Git object archive path does not match its type")
        name = PurePosixPath(self.archive_path).name
        if self.object_type in {"tree", "blob"} and name != f"{self.oid}{suffix}":
            raise ValueError("Git tree/blob archive path does not match its object ID")
        if self.object_type == "commit" and not name.endswith(
            f"-{self.oid}{suffix}"
        ):
            raise ValueError("Git commit archive path does not match its object ID")
        return self


class ArchivedBinaryDiff(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    archive_path: str = Field(min_length=1, max_length=4_096)
    payload_digest: Digest
    size_bytes: int = Field(ge=1, le=MAX_BINARY_DIFF_BYTES)

    @field_validator("archive_path")
    @classmethod
    def archive_path_is_safe(cls, value: str) -> str:
        normalized = _safe_archive_path(value)
        if not normalized.startswith("source-provenance/patches/") or not normalized.endswith(
            ".patch"
        ):
            raise ValueError("binary diff archive path is not canonical")
        return normalized


class DeveloperCommitProvenance(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    order: int = Field(ge=1)
    binding: ApprovedDeveloperResponseBinding
    commit_sha: GitObjectId
    tree_sha: GitObjectId
    parent_commit_sha: GitObjectId
    commit_object: ArchivedGitObject
    changed_paths: list[SafeSourcePath] = Field(min_length=1, max_length=5_000)
    changes: list[GitPathChange] = Field(min_length=1, max_length=5_000)
    blob_objects: list[ArchivedGitObject] = Field(min_length=1, max_length=10_000)
    binary_diff: ArchivedBinaryDiff
    provenance_digest: Digest

    @field_validator("changed_paths")
    @classmethod
    def changed_paths_are_safe(cls, value: list[str]) -> list[str]:
        normalized = [_safe_source_path(item) for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("developer commit changed paths must be unique")
        return normalized

    @model_validator(mode="after")
    def evidence_is_exactly_bound(self) -> DeveloperCommitProvenance:
        if self.commit_sha != self.binding.commit_sha:
            raise ValueError("developer commit differs from its DeveloperResponse")
        if (
            self.commit_object.object_type != "commit"
            or self.commit_object.oid != self.commit_sha
        ):
            raise ValueError("developer commit object evidence is not bound")
        if self.changed_paths != [change.path for change in self.changes]:
            raise ValueError("developer commit path summary differs from raw changes")
        blob_by_oid: dict[str, ArchivedGitObject] = {}
        for blob in self.blob_objects:
            if blob.object_type != "blob":
                raise ValueError("developer blob evidence contains a non-blob object")
            existing = blob_by_oid.get(blob.oid)
            if existing is not None and existing != blob:
                raise ValueError("developer blob object has conflicting evidence")
            blob_by_oid[blob.oid] = blob
        expected_blobs = {
            oid
            for change in self.changes
            for oid in (change.old_blob_sha, change.new_blob_sha)
            if oid is not None
        }
        if set(blob_by_oid) != expected_blobs:
            raise ValueError("developer blob archive does not exactly cover changed blobs")
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"provenance_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.provenance_digest):
            raise ValueError("developer commit provenance digest does not match")
        return self


class FormalSourceProvenanceManifest(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=3, max_length=160)
    task_revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    channel_id: UUID
    baseline: FormalSourceBaseline
    baseline_commit_object: ArchivedGitObject
    tree_objects: list[ArchivedGitObject] = Field(min_length=1, max_length=200_000)
    approved_commits: list[DeveloperCommitProvenance] = Field(max_length=1_000)
    developer_projection: DeveloperSourceProjectionManifest | None = None
    projection_blob_objects: list[ArchivedGitObject] | None = Field(
        default=None,
        max_length=200_000,
    )
    promotion_intents: list[DeveloperSourcePromotionIntent] | None = Field(
        default=None,
        max_length=1_000,
    )
    promotion_receipts: list[DeveloperSourcePromotionReceipt] | None = Field(
        default=None,
        max_length=1_000,
    )
    activation_fences: list[DeveloperSourceActivationFence] | None = Field(
        default=None,
        max_length=1_000,
    )
    promotion_adoptions: list[DeveloperSourcePromotionAdoption] | None = Field(
        default=None,
        max_length=10_000,
    )
    reload_confirmations: list[DeveloperSourceReloadConfirmation] | None = Field(
        default=None,
        max_length=10_000,
    )
    final_source_state: GitWorktreeState
    finalized_at: datetime
    manifest_digest: Digest

    @field_validator("finalized_at")
    @classmethod
    def finalized_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def manifest_is_a_complete_linear_history(self) -> FormalSourceProvenanceManifest:
        if (
            self.task_id != self.baseline.task_id
            or self.task_revision != self.baseline.task_revision
            or self.run_id != self.baseline.run_id
            or self.assignment_id != self.baseline.assignment_id
            or self.channel_id != self.baseline.channel_id
        ):
            raise ValueError("source provenance manifest differs from its baseline")
        if self.finalized_at < self.baseline.captured_at:
            raise ValueError("source provenance finalization predates its baseline")
        if not self.final_source_state.clean:
            raise ValueError("final developer source state must be clean")
        if (
            self.final_source_state.object_format
            != self.baseline.source_state.object_format
        ):
            raise ValueError("Git object format changed during the formal assignment")
        if (
            self.baseline_commit_object.object_type != "commit"
            or self.baseline_commit_object.oid
            != self.baseline.source_state.head_commit_sha
        ):
            raise ValueError("baseline commit object does not match the source baseline")
        tree_by_oid: dict[str, ArchivedGitObject] = {}
        for tree in self.tree_objects:
            if tree.object_type != "tree":
                raise ValueError("source tree inventory contains a non-tree object")
            if tree.oid in tree_by_oid:
                raise ValueError("source tree inventory contains a duplicate object")
            tree_by_oid[tree.oid] = tree
        required_roots = {
            self.baseline.source_state.head_tree_sha,
            *(commit.tree_sha for commit in self.approved_commits),
        }
        if not required_roots <= set(tree_by_oid):
            raise ValueError("source tree inventory omits a commit root tree")
        prior = self.baseline.source_state.head_commit_sha
        response_ids: set[UUID] = set()
        response_messages: set[UUID] = set()
        approval_ids: set[UUID] = set()
        commit_ids: set[str] = set()
        paths: dict[str, tuple[str, int]] = {
            self.baseline_commit_object.archive_path: (
                self.baseline_commit_object.payload_digest,
                self.baseline_commit_object.size_bytes,
            )
        }
        for tree in self.tree_objects:
            identity = (tree.payload_digest, tree.size_bytes)
            existing = paths.get(tree.archive_path)
            if existing is not None and existing != identity:
                raise ValueError("source tree archive path has conflicting payloads")
            paths[tree.archive_path] = identity
        for expected_order, commit in enumerate(self.approved_commits, start=1):
            if commit.order != expected_order or commit.parent_commit_sha != prior:
                raise ValueError("approved developer commits are not one exact linear history")
            if commit.binding.channel_id != self.channel_id:
                raise ValueError("developer commit is bound to another collaboration channel")
            if (
                commit.binding.response_id in response_ids
                or commit.binding.response_message_id in response_messages
                or commit.binding.approval_id in approval_ids
                or commit.commit_sha in commit_ids
            ):
                raise ValueError("developer source provenance reuses an approval or response")
            response_ids.add(commit.binding.response_id)
            response_messages.add(commit.binding.response_message_id)
            approval_ids.add(commit.binding.approval_id)
            commit_ids.add(commit.commit_sha)
            prior = commit.commit_sha
            for item in (
                commit.commit_object,
                *commit.blob_objects,
            ):
                identity = (item.payload_digest, item.size_bytes)
                existing = paths.get(item.archive_path)
                if existing is not None and existing != identity:
                    raise ValueError("source archive path has conflicting payloads")
                paths[item.archive_path] = identity
            diff_identity = (
                commit.binary_diff.payload_digest,
                commit.binary_diff.size_bytes,
            )
            existing = paths.get(commit.binary_diff.archive_path)
            if existing is not None and existing != diff_identity:
                raise ValueError("source archive path has conflicting payloads")
            paths[commit.binary_diff.archive_path] = diff_identity
        if self.final_source_state.head_commit_sha != prior:
            raise ValueError("final source HEAD differs from the approved commit history")
        if self.approved_commits:
            expected_tree = self.approved_commits[-1].tree_sha
        else:
            expected_tree = self.baseline.source_state.head_tree_sha
        if self.final_source_state.head_tree_sha != expected_tree:
            raise ValueError("final source tree differs from the approved commit history")
        trusted_workspace_fields = (
            self.developer_projection,
            self.projection_blob_objects,
            self.promotion_intents,
            self.promotion_receipts,
            self.activation_fences,
            self.promotion_adoptions,
            self.reload_confirmations,
        )
        if any(item is None for item in trusted_workspace_fields) and any(
            item is not None for item in trusted_workspace_fields
        ):
            raise ValueError(
                "trusted workspace provenance requires projection, intents, and receipts together"
            )
        if self.developer_projection is not None:
            projection = self.developer_projection
            projection_blobs = self.projection_blob_objects or []
            intents = self.promotion_intents or []
            receipts = self.promotion_receipts or []
            fences = self.activation_fences or []
            adoptions = self.promotion_adoptions or []
            confirmations = self.reload_confirmations or []
            if (
                projection.task_id != self.task_id
                or projection.task_revision != self.task_revision
                or projection.run_id != self.run_id
                or projection.assignment_id != self.assignment_id
                or projection.channel_id != self.channel_id
                or projection.baseline_commit_sha
                != self.baseline.source_state.head_commit_sha
                or projection.baseline_tree_sha
                != self.baseline.source_state.head_tree_sha
                or len(intents) != len(self.approved_commits)
                or len(receipts) != len(self.approved_commits)
                or len(fences) != len(self.approved_commits)
            ):
                raise ValueError(
                    "trusted workspace projection differs from the approved source history"
                )
            projection_blob_by_oid = {
                blob.oid: blob for blob in projection_blobs
            }
            if (
                len(projection_blob_by_oid) != len(projection_blobs)
                or set(projection_blob_by_oid)
                != {entry.blob_sha for entry in projection.entries}
            ):
                raise ValueError(
                    "projection blob archive does not exactly cover the baseline projection"
                )
            for blob in projection_blobs:
                if blob.object_type != "blob":
                    raise ValueError(
                        "projection blob evidence contains a non-blob object"
                    )
                identity = (blob.payload_digest, blob.size_bytes)
                existing = paths.get(blob.archive_path)
                if existing is not None and existing != identity:
                    raise ValueError(
                        "projection blob archive path has conflicting payloads"
                    )
                paths[blob.archive_path] = identity
            for entry in projection.entries:
                blob = projection_blob_by_oid[entry.blob_sha]
                if (
                    blob.payload_digest != entry.digest
                    or blob.size_bytes != entry.size_bytes
                ):
                    raise ValueError(
                        "projection entry differs from its archived Git blob"
                    )
            known_paths = {entry.path for entry in projection.entries}
            for intent, receipt, fence, commit in zip(
                intents,
                receipts,
                fences,
                self.approved_commits,
                strict=True,
            ):
                if (
                    intent.assignment_id != self.assignment_id
                    or intent.channel_id != self.channel_id
                    or intent.report_id != receipt.report_id
                    or intent.report_revision != receipt.report_revision
                    or intent.lease_id != receipt.lease_id
                    or intent.response_id != receipt.response_id
                    or intent.workspace_manifest_digest
                    != receipt.workspace_manifest_digest
                    or intent.source_manifest_digest
                    != receipt.source_manifest_digest
                    or intent.intent_digest != receipt.intent_digest
                    or intent.branch_ref != receipt.branch_ref
                    or intent.parent_commit_sha != receipt.parent_commit_sha
                    or intent.parent_tree_sha != receipt.parent_tree_sha
                    or intent.changes != commit.changes
                    or receipt.assignment_id != self.assignment_id
                    or receipt.channel_id != self.channel_id
                    or receipt.report_id != commit.binding.report_id
                    or receipt.response_id != commit.binding.response_id
                    or receipt.commit_sha != commit.commit_sha
                    or receipt.tree_sha != commit.tree_sha
                    or receipt.parent_commit_sha != commit.parent_commit_sha
                    or receipt.changed_paths != commit.changed_paths
                    or receipt.source_manifest_digest != projection.manifest_digest
                    or fence.assignment_id != self.assignment_id
                    or fence.channel_id != self.channel_id
                    or fence.report_id != receipt.report_id
                    or fence.report_revision != receipt.report_revision
                    or fence.response_id != receipt.response_id
                    or fence.receipt_digest != receipt.receipt_digest
                    or fence.intent_digest != receipt.intent_digest
                    or fence.branch_ref != receipt.branch_ref
                    or fence.commit_sha != receipt.commit_sha
                    or fence.tree_sha != receipt.tree_sha
                    or fence.activation_process_instance_id
                    != receipt.process_instance_id
                    or fence.activated_at != receipt.activated_at
                ):
                    raise ValueError(
                        "promotion receipt differs from its approved developer commit"
                    )
                archived_blobs = {
                    blob.oid: blob for blob in commit.blob_objects
                }
                for target in intent.target_blobs:
                    archived = archived_blobs.get(target.blob_sha)
                    if (
                        archived is None
                        or archived.payload_digest != target.payload_digest
                        or archived.size_bytes != target.size_bytes
                    ):
                        raise ValueError(
                            "promotion workspace blob differs from archived Git bytes"
                        )
                for change in commit.changes:
                    if change.change_kind == "added":
                        if (
                            change.path in known_paths
                            or not projection.permits_new_path(change.path)
                        ):
                            raise ValueError(
                                "promoted source addition crosses its allowed-new boundary"
                            )
                        known_paths.add(change.path)
                    elif change.path not in known_paths:
                        raise ValueError(
                            "promoted source change was outside its projected history"
                        )
                    if change.change_kind == "deleted":
                        known_paths.remove(change.path)
            receipt_by_digest = {
                receipt.receipt_digest: receipt for receipt in receipts
            }
            fence_by_receipt = {
                fence.receipt_digest: fence for fence in fences
            }
            if len(receipt_by_digest) != len(receipts):
                raise ValueError("promotion receipt digest is reused")
            if set(fence_by_receipt) != set(receipt_by_digest):
                raise ValueError("activation fences do not exactly cover promotions")
            adoption_identities: set[tuple[UUID, int, UUID]] = set()
            for adoption in adoptions:
                receipt = receipt_by_digest.get(adoption.receipt_digest)
                identity = (
                    adoption.response_id,
                    adoption.report_revision,
                    adoption.lease_id,
                )
                if (
                    receipt is None
                    or identity in adoption_identities
                    or adoption.assignment_id != self.assignment_id
                    or adoption.channel_id != self.channel_id
                    or adoption.report_id != receipt.report_id
                    or adoption.report_revision < receipt.report_revision
                    or adoption.response_id != receipt.response_id
                    or adoption.workspace_manifest_digest
                    != receipt.workspace_manifest_digest
                    or adoption.source_manifest_digest
                    != receipt.source_manifest_digest
                ):
                    raise ValueError(
                        "promotion adoption differs from its activated receipt"
                    )
                adoption_identities.add(identity)
            confirmation_identities: set[tuple[str, UUID]] = set()
            confirmed_receipts: set[str] = set()
            for confirmation in confirmations:
                receipt = receipt_by_digest.get(confirmation.receipt_digest)
                fence = fence_by_receipt.get(confirmation.receipt_digest)
                expected_hidden_ref = (
                    f"refs/lilies/formal/{self.assignment_id}/"
                    f"{confirmation.response_id}"
                )
                identity = (
                    confirmation.receipt_digest,
                    confirmation.confirming_process_instance_id,
                )
                if (
                    receipt is None
                    or fence is None
                    or receipt.reload_status == "not_required"
                    or identity in confirmation_identities
                    or confirmation.assignment_id != self.assignment_id
                    or confirmation.channel_id != self.channel_id
                    or confirmation.report_id != receipt.report_id
                    or confirmation.report_revision != receipt.report_revision
                    or confirmation.response_id != receipt.response_id
                    or confirmation.intent_digest != receipt.intent_digest
                    or confirmation.branch_ref != receipt.branch_ref
                    or confirmation.hidden_ref != expected_hidden_ref
                    or confirmation.commit_sha != receipt.commit_sha
                    or confirmation.tree_sha != receipt.tree_sha
                    or confirmation.changed_paths != receipt.changed_paths
                    or confirmation.activation_process_instance_id
                    != receipt.process_instance_id
                    or confirmation.activation_fence_digest
                    != fence.fence_digest
                    or confirmation.activation_boot_id
                    != fence.activation_boot_id
                    or confirmation.activation_boot_started_at
                    != fence.activation_boot_started_at
                    or confirmation.activation_monotonic_ns
                    != fence.activation_monotonic_ns
                    or not _code_generation_follows_activation(
                        fence=fence,
                        process_boot_id=confirmation.process_generation_boot_id,
                        process_boot_started_at=(
                            confirmation.process_generation_boot_started_at
                        ),
                        process_monotonic_ns=(
                            confirmation.process_generation_monotonic_ns
                        ),
                    )
                ):
                    raise ValueError(
                        "reload confirmation differs from its activated receipt"
                    )
                confirmation_identities.add(identity)
                confirmed_receipts.add(confirmation.receipt_digest)
            expected_confirmations = {
                receipt.receipt_digest
                for receipt in receipts
                if receipt.reload_status != "not_required"
            }
            if confirmed_receipts != expected_confirmations:
                raise ValueError(
                    "reload confirmations do not exactly cover restart-required promotions"
                )
            for commit, receipt in zip(
                self.approved_commits,
                receipts,
                strict=True,
            ):
                revision = commit.binding.response_report_revision
                if revision == receipt.report_revision:
                    continue
                if not any(
                    adoption.receipt_digest == receipt.receipt_digest
                    and adoption.report_id == commit.binding.report_id
                    and adoption.report_revision == revision
                    for adoption in adoptions
                ):
                    raise ValueError(
                        "approved response revision lacks a promotion adoption"
                    )
        expected = _digest(
            self.model_dump(
                mode="json",
                exclude={"manifest_digest"},
                exclude_none=True,
            )
        )
        if not hmac.compare_digest(expected, self.manifest_digest):
            raise ValueError("source provenance manifest digest does not match")
        return self


@dataclass(frozen=True)
class FormalSourceProvenanceArchive:
    manifest: FormalSourceProvenanceManifest
    files: Mapping[str, bytes]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


ContentGuard = Callable[[str, bytes], bool | None]


class _GitRepository:
    def __init__(self, root: Path) -> None:
        lexical = Path(root)
        if lexical.is_symlink() or not lexical.is_dir():
            raise FormalSourceProvenanceSecurityError(
                "formal source root must be a real Git repository directory"
            )
        self.root = lexical.resolve(strict=True)
        discovered = self.run(
            ["rev-parse", "--show-toplevel"],
            limit=16 * 1024,
        )
        try:
            top_level = Path(discovered.decode("utf-8").strip()).resolve(strict=True)
        except (UnicodeDecodeError, OSError) as error:
            raise FormalSourceProvenanceSecurityError(
                "Git repository root is not a safe local path"
            ) from error
        if top_level != self.root:
            raise FormalSourceProvenanceSecurityError(
                "formal source provenance requires the complete Git worktree root"
            )
        object_format = self.run(
            ["rev-parse", "--show-object-format"],
            limit=64,
        ).decode("ascii").strip()
        if object_format not in {"sha1", "sha256"}:
            raise FormalSourceProvenanceSecurityError(
                "unsupported Git object format"
            )
        self.object_format: Literal["sha1", "sha256"] = object_format  # type: ignore[assignment]
        self.oid_length = 40 if object_format == "sha1" else 64

    def run(
        self,
        arguments: Sequence[str],
        *,
        limit: int,
        timeout: float = 30,
        input_payload: bytes | None = None,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> bytes:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C",
            }
        )
        if environment_overrides is not None:
            environment.update(environment_overrides)
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.root), *arguments],
                check=False,
                capture_output=True,
                env=environment,
                timeout=timeout,
                input=input_payload,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise FormalSourceProvenanceError(
                "Git source provenance command failed"
            ) from error
        if completed.returncode != 0:
            raise FormalSourceProvenanceError(
                "Git source provenance command rejected the repository state"
            )
        if len(completed.stdout) > limit:
            raise FormalSourceProvenanceSecurityError(
                "Git source provenance payload exceeds its archive limit"
            )
        return completed.stdout

    def symbolic_head(self) -> str:
        try:
            value = self.run(
                ["symbolic-ref", "-q", "HEAD"],
                limit=4 * 1024,
            ).decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise FormalSourceProvenanceSecurityError(
                "Git symbolic HEAD is not UTF-8"
            ) from error
        if (
            not value.startswith("refs/heads/")
            or "\\" in value
            or ".." in PurePosixPath(value).parts
        ):
            raise FormalSourceProvenanceConflict(
                "formal source promotion requires an attached local branch"
            )
        return value

    def git_path(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
            raise FormalSourceProvenanceSecurityError("unsafe Git administrative path")
        try:
            raw = self.run(
                ["rev-parse", "--git-path", name],
                limit=16 * 1024,
            ).decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise FormalSourceProvenanceSecurityError(
                "Git administrative path is not UTF-8"
            ) from error
        lexical = Path(raw)
        if not lexical.is_absolute():
            lexical = self.root / lexical
        candidate = lexical.resolve(strict=False)
        parent = candidate.parent.resolve(strict=True)
        if parent.is_symlink() or not parent.is_dir() or candidate.parent.resolve() != parent:
            raise FormalSourceProvenanceSecurityError(
                "Git administrative path has an unsafe parent"
            )
        return candidate

    def oid(self, revision: str, object_type: Literal["commit", "tree"]) -> str:
        value = self.run(
            ["rev-parse", "--verify", f"{revision}^{{{object_type}}}"],
            limit=256,
        ).decode("ascii").strip()
        if (
            len(value) != self.oid_length
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise FormalSourceProvenanceSecurityError(
                "Git returned an invalid object ID"
            )
        return value

    def object_payload(
        self,
        oid: str,
        object_type: Literal["commit", "tree", "blob"],
    ) -> bytes:
        if len(oid) != self.oid_length or any(
            character not in "0123456789abcdef" for character in oid
        ):
            raise FormalSourceProvenanceSecurityError(
                "source provenance references an invalid Git object ID"
            )
        actual_type = self.run(["cat-file", "-t", oid], limit=64).decode("ascii").strip()
        if actual_type != object_type:
            raise FormalSourceProvenanceSecurityError(
                "source provenance Git object has an unexpected type"
            )
        limit = {
            "commit": MAX_COMMIT_OBJECT_BYTES,
            "tree": MAX_TREE_OBJECT_BYTES,
            "blob": MAX_BLOB_OBJECT_BYTES,
        }[object_type]
        return self.run(["cat-file", object_type, oid], limit=limit)

    def object_oid(
        self,
        object_type: Literal["commit", "tree", "blob"],
        payload: bytes,
    ) -> str:
        header = f"{object_type} {len(payload)}\0".encode("ascii")
        return hashlib.new(self.object_format, header + payload).hexdigest()

    def state(self) -> GitWorktreeState:
        first_head = self.oid("HEAD", "commit")
        first_tree = self.oid(first_head, "tree")
        first_status = self.run(
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
            limit=MAX_GIT_STATUS_BYTES,
        )
        second_head = self.oid("HEAD", "commit")
        second_tree = self.oid(second_head, "tree")
        second_status = self.run(
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
            limit=MAX_GIT_STATUS_BYTES,
        )
        if (
            first_head != second_head
            or first_tree != second_tree
            or not hmac.compare_digest(first_status, second_status)
        ):
            raise FormalSourceProvenanceConflict(
                "developer Git source changed while its state was captured"
            )
        tracked, untracked, conflicted = _status_counts(first_status)
        return GitWorktreeState(
            object_format=self.object_format,
            head_commit_sha=first_head,
            head_tree_sha=first_tree,
            status_digest=_digest(first_status),
            tracked_change_count=tracked,
            untracked_file_count=untracked,
            conflicted_path_count=conflicted,
            clean=not first_status,
        )


def _status_counts(payload: bytes) -> tuple[int, int, int]:
    tokens = payload.split(b"\0")
    tracked = 0
    untracked = 0
    conflicted = 0
    index = 0
    conflicts = {b"DD", b"AU", b"UD", b"UA", b"DU", b"AA", b"UU"}
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise FormalSourceProvenanceSecurityError(
                "Git status returned an invalid record"
            )
        code = record[:2]
        if code == b"??":
            untracked += 1
        else:
            tracked += 1
        if code in conflicts:
            conflicted += 1
        if b"R" in code or b"C" in code:
            if index >= len(tokens) or not tokens[index]:
                raise FormalSourceProvenanceSecurityError(
                    "Git status rename record is incomplete"
                )
            index += 1
    return tracked, untracked, conflicted


def capture_git_source_state(repository_root: Path) -> GitWorktreeState:
    """Capture HEAD/tree and a path-free digest/count summary of dirty state."""

    return _GitRepository(repository_root).state()


def approved_developer_response_bindings(
    messages: Sequence[CollaborationMessageEnvelope | Mapping[str, Any]],
    *,
    channel_id: UUID,
) -> list[ApprovedDeveloperResponseBinding]:
    """Derive the only commits authorized by durable approval/response messages."""

    parsed = [
        message
        if isinstance(message, CollaborationMessageEnvelope)
        else CollaborationMessageEnvelope.model_validate(message)
        for message in messages
    ]
    parsed.sort(key=lambda message: message.seq)
    if len({message.seq for message in parsed}) != len(parsed) or len(
        {message.message_id for message in parsed}
    ) != len(parsed):
        raise FormalSourceProvenanceConflict(
            "collaboration export contains duplicate message identities"
        )
    approvals: dict[
        UUID,
        tuple[CollaborationMessageEnvelope, ApprovalDecision],
    ] = {}
    bindings: list[ApprovedDeveloperResponseBinding] = []
    for message in parsed:
        if message.channel_id != channel_id:
            continue
        if message.message_type is MessageType.approval:
            approval = ApprovalDecision.model_validate(message.payload)
            if (
                approval.channel_id != channel_id
                or approval.report_id != message.correlation_id
                or approval.actor_id != message.sender_id
            ):
                raise FormalSourceProvenanceConflict(
                    "approval message does not match its report binding"
                )
            if approval.decision is ReportDecision.approve:
                if message.sender_role not in {SenderRole.user, SenderRole.platform}:
                    raise FormalSourceProvenanceConflict(
                        "approval message has no user-authorized authority"
                    )
                approvals[approval.report_id] = (message, approval)
            else:
                approvals.pop(approval.report_id, None)
            continue
        if message.message_type is not MessageType.developer_response:
            continue
        response = DeveloperResponse.model_validate(message.payload)
        if (
            response.channel_id != channel_id
            or response.report_id != message.correlation_id
        ):
            raise FormalSourceProvenanceConflict(
                "DeveloperResponse message does not match its report binding"
            )
        approved = approvals.pop(response.report_id, None)
        if response.outcome is not DeveloperOutcome.implemented:
            continue
        if approved is None:
            raise FormalSourceProvenanceConflict(
                "implemented DeveloperResponse has no active user approval"
            )
        approval_message, approval = approved
        if response.commit_sha is None:  # pragma: no cover - model invariant
            raise FormalSourceProvenanceConflict(
                "implemented DeveloperResponse has no source commit"
            )
        authority: Literal["user", "task_auto_forward"]
        if approval_message.sender_role is SenderRole.user:
            authority = "user"
        else:
            if (
                approval.actor_id != "platform-auto-forward"
                or approval.reason != "user-confirmed task-local auto-forward"
            ):
                raise FormalSourceProvenanceConflict(
                    "platform approval is not the task-local user-authorized route"
                )
            authority = "task_auto_forward"
        bindings.append(
            ApprovedDeveloperResponseBinding(
                channel_id=channel_id,
                report_id=response.report_id,
                approval_id=approval.approval_id,
                approval_message_id=approval_message.message_id,
                approval_message_seq=approval_message.seq,
                approval_authority=authority,
                approval_payload_digest=_digest(approval),
                approved_report_revision=approval.resulting_report_revision,
                response_id=response.response_id,
                response_message_id=message.message_id,
                response_message_seq=message.seq,
                response_report_revision=response.report_revision,
                response_payload_digest=_digest(response),
                commit_sha=response.commit_sha,
            )
        )
    if len({binding.commit_sha for binding in bindings}) != len(bindings):
        raise FormalSourceProvenanceConflict(
            "one source commit cannot satisfy multiple DeveloperResponses"
        )
    return bindings


def _parse_commit_headers(payload: bytes) -> tuple[str, list[str]]:
    header, separator, _message = payload.partition(b"\n\n")
    if not separator:
        raise FormalSourceProvenanceSecurityError(
            "Git commit object has no header terminator"
        )
    tree: str | None = None
    parents: list[str] = []
    for line in header.splitlines():
        if line.startswith(b"tree "):
            if tree is not None:
                raise FormalSourceProvenanceSecurityError(
                    "Git commit object has multiple tree headers"
                )
            try:
                tree = line[5:].decode("ascii")
            except UnicodeDecodeError as error:
                raise FormalSourceProvenanceSecurityError(
                    "Git commit tree header is invalid"
                ) from error
        elif line.startswith(b"parent "):
            try:
                parents.append(line[7:].decode("ascii"))
            except UnicodeDecodeError as error:
                raise FormalSourceProvenanceSecurityError(
                    "Git commit parent header is invalid"
                ) from error
    if tree is None:
        raise FormalSourceProvenanceSecurityError(
            "Git commit object has no tree header"
        )
    return tree, parents


@dataclass(frozen=True)
class _RawTreeEntry:
    mode: str
    name: str
    oid: str
    object_type: Literal["tree", "blob", "commit"]


def _object_oid(
    object_format: Literal["sha1", "sha256"],
    object_type: Literal["commit", "tree", "blob"],
    payload: bytes,
) -> str:
    header = f"{object_type} {len(payload)}\0".encode("ascii")
    return hashlib.new(object_format, header + payload).hexdigest()


def _parse_raw_tree(
    payload: bytes,
    *,
    object_format: Literal["sha1", "sha256"],
) -> list[_RawTreeEntry]:
    oid_bytes = 20 if object_format == "sha1" else 32
    entries: list[_RawTreeEntry] = []
    offset = 0
    while offset < len(payload):
        space = payload.find(b" ", offset)
        terminator = payload.find(b"\0", space + 1)
        if space <= offset or terminator < 0:
            raise FormalSourceProvenanceSecurityError(
                "archived Git tree contains an incomplete entry"
            )
        oid_start = terminator + 1
        oid_end = oid_start + oid_bytes
        if oid_end > len(payload):
            raise FormalSourceProvenanceSecurityError(
                "archived Git tree contains a truncated object ID"
            )
        try:
            mode = payload[offset:space].decode("ascii")
            name = payload[space + 1 : terminator].decode("utf-8")
        except UnicodeDecodeError as error:
            raise FormalSourceProvenanceSecurityError(
                "archived Git tree contains an unsafe entry encoding"
            ) from error
        if (
            not name
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
            or unicodedata.normalize("NFC", name) != name
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise FormalSourceProvenanceSecurityError(
                "archived Git tree contains an unsafe entry name"
            )
        normalized_mode = "40000" if mode == "040000" else mode
        if normalized_mode == "40000":
            object_type: Literal["tree", "blob", "commit"] = "tree"
        elif normalized_mode == "160000":
            object_type = "commit"
        elif normalized_mode in {"100644", "100755", "120000"}:
            object_type = "blob"
        else:
            raise FormalSourceProvenanceSecurityError(
                "archived Git tree contains an unsupported entry mode"
            )
        entries.append(
            _RawTreeEntry(
                mode=normalized_mode,
                name=name,
                oid=payload[oid_start:oid_end].hex(),
                object_type=object_type,
            )
        )
        offset = oid_end
    if len({entry.name for entry in entries}) != len(entries):
        raise FormalSourceProvenanceSecurityError(
            "archived Git tree contains duplicate entry names"
        )
    return entries


def _is_projected_source_path(path: str) -> bool:
    normalized = _safe_source_path(path)
    if (
        normalized in DEVELOPER_TRUST_ROOT_PATHS
        or _contains_dotenv_segment(normalized)
    ):
        return False
    if any(
        part.casefold() in _DEVELOPER_PROJECTION_DENIED_SEGMENTS
        for part in PurePosixPath(normalized).parts
    ):
        return False
    return normalized in _DEVELOPER_PROJECTION_FILES or any(
        normalized.startswith(prefix)
        for prefix in _DEVELOPER_PROJECTION_PREFIXES
    )


def _projected_tree_entries(
    repository: _GitRepository,
    commit_sha: str,
) -> list[DeveloperSourceProjectionEntry]:
    """Read the explicit developer projection directly from immutable Git blobs."""

    payload = repository.run(
        ["ls-tree", "-r", "-z", "--full-tree", commit_sha],
        limit=MAX_ARCHIVE_PAYLOAD_BYTES,
    )
    entries: list[DeveloperSourceProjectionEntry] = []
    for record in payload.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            raise FormalSourceProvenanceSecurityError(
                "Git projection contains an invalid tree entry"
            )
        try:
            mode = fields[0].decode("ascii")
            object_type = fields[1].decode("ascii")
            oid = fields[2].decode("ascii")
            path = raw_path.decode("utf-8")
            normalized = _safe_source_path(path)
        except UnicodeDecodeError as error:
            raise FormalSourceProvenanceSecurityError(
                "Git projection contains an unsafe repository path"
            ) from error
        except ValueError:
            # Protected/reserved paths outside the explicit source projection
            # are intentionally undisclosed, not a reason to copy or inspect
            # their blobs.
            continue
        if not _is_projected_source_path(normalized):
            continue
        if (
            object_type != "blob"
            or mode not in _ALLOWED_FILE_MODES
            or len(oid) != repository.oid_length
            or any(character not in "0123456789abcdef" for character in oid)
        ):
            raise FormalSourceProvenanceSecurityError(
                "developer projection contains a symlink, submodule, or special file"
            )
        blob = repository.object_payload(oid, "blob")
        if repository.object_oid("blob", blob) != oid:
            raise FormalSourceProvenanceSecurityError(
                "developer projection blob does not hash to its Git identity"
            )
        entries.append(
            DeveloperSourceProjectionEntry(
                path=normalized,
                mode=mode,
                blob_sha=oid,
                digest=_digest(blob),
                size_bytes=len(blob),
            )
        )
    entries.sort(key=lambda item: item.path)
    if not entries:
        raise FormalSourceProvenanceConflict(
            "developer source projection is empty"
        )
    if len({entry.path.casefold() for entry in entries}) != len(entries):
        raise FormalSourceProvenanceSecurityError(
            "developer source projection paths collide after normalization"
        )
    return entries


def _read_workspace_file(
    path: Path,
    *,
    limit: int = MAX_BLOB_OBJECT_BYTES,
    allow_activation_link: bool = False,
) -> tuple[bytes, os.stat_result]:
    if path.is_symlink():
        raise FormalSourceProvenanceSecurityError(
            "developer workspace cannot contain symlink source files"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FormalSourceProvenanceSecurityError(
            "developer workspace file is not safely readable"
        ) from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or (
                before.st_nlink not in {1, 2}
                if allow_activation_link
                else before.st_nlink != 1
            )
            or before.st_size > limit
        ):
            raise FormalSourceProvenanceSecurityError(
                "developer workspace requires bounded single-link regular files"
            )
        payload = b""
        while chunk := os.read(descriptor, 1024 * 1024):
            payload += chunk
            if len(payload) > limit:
                raise FormalSourceProvenanceSecurityError(
                    "developer workspace file exceeds the source size limit"
                )
        after = os.fstat(descriptor)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or (
                after.st_nlink not in {1, 2}
                if allow_activation_link
                else after.st_nlink != 1
            )
        ):
            raise FormalSourceProvenanceConflict(
                "developer workspace changed while its delta was frozen"
            )
        return payload, after
    finally:
        os.close(descriptor)


def _safe_materialize_file(root: Path, relative_path: str, payload: bytes) -> None:
    normalized = _safe_source_path(relative_path)
    target = root.joinpath(*PurePosixPath(normalized).parts)
    if not target.resolve(strict=False).is_relative_to(root.resolve(strict=True)):
        raise FormalSourceProvenanceSecurityError(
            "developer projection target escapes its workspace"
        )
    cursor = root
    for segment in PurePosixPath(normalized).parts[:-1]:
        cursor = cursor / segment
        if cursor.exists() and (cursor.is_symlink() or not cursor.is_dir()):
            raise FormalSourceProvenanceSecurityError(
                "developer projection target has an unsafe ancestor"
            )
        cursor.mkdir(mode=0o700, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FormalSourceProvenanceConflict(
            "developer projection destination is not empty"
        )
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _tree_object_closure(
    repository: _GitRepository,
    *,
    root_tree_oids: Sequence[str],
    content_guard: ContentGuard | None,
) -> tuple[list[ArchivedGitObject], dict[str, bytes]]:
    descriptors: dict[str, ArchivedGitObject] = {}
    files: dict[str, bytes] = {}
    pending: list[tuple[str, str]] = [
        (root, "")
        for root in reversed(list(dict.fromkeys(root_tree_oids)))
    ]
    visited_contexts: set[tuple[str, str]] = set()
    while pending:
        tree_oid, prefix = pending.pop()
        context = (tree_oid, prefix)
        if context in visited_contexts:
            continue
        visited_contexts.add(context)
        tree_payload = repository.object_payload(tree_oid, "tree")
        if repository.object_oid("tree", tree_payload) != tree_oid:
            raise FormalSourceProvenanceSecurityError(
                "Git tree object does not hash to its declared identity"
            )
        tree_path = f"source-provenance/trees/{tree_oid}.tree"
        _guard_payload(
            content_guard,
            label=tree_path,
            payload=tree_payload,
        )
        descriptor = ArchivedGitObject(
            object_type="tree",
            oid=tree_oid,
            archive_path=tree_path,
            payload_digest=_digest(tree_payload),
            size_bytes=len(tree_payload),
        )
        existing = descriptors.get(tree_oid)
        if existing is not None and existing != descriptor:
            raise FormalSourceProvenanceSecurityError(
                "Git tree object has conflicting source evidence"
            )
        descriptors[tree_oid] = descriptor
        files[tree_path] = tree_payload
        for entry in _parse_raw_tree(
            tree_payload,
            object_format=repository.object_format,
        ):
            full_path = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                normalized = _safe_repository_path(full_path)
            except ValueError as error:
                raise FormalSourceProvenanceSecurityError(
                    "Git source tree contains an unsafe repository path"
                ) from error
            if entry.object_type == "tree":
                pending.append((entry.oid, normalized))
    return (
        [descriptors[oid] for oid in sorted(descriptors)],
        files,
    )


def _baseline_commit_evidence(
    repository: _GitRepository,
    *,
    commit_sha: str,
    content_guard: ContentGuard | None,
) -> tuple[ArchivedGitObject, bytes]:
    payload = repository.object_payload(commit_sha, "commit")
    if repository.object_oid("commit", payload) != commit_sha:
        raise FormalSourceProvenanceSecurityError(
            "Git baseline commit does not hash to its declared identity"
        )
    archive_path = (
        f"source-provenance/commits/baseline-{commit_sha}.commit"
    )
    _guard_payload(content_guard, label=archive_path, payload=payload)
    return (
        ArchivedGitObject(
            object_type="commit",
            oid=commit_sha,
            archive_path=archive_path,
            payload_digest=_digest(payload),
            size_bytes=len(payload),
        ),
        payload,
    )


def _parse_raw_changes(
    payload: bytes,
    *,
    oid_length: int,
) -> list[GitPathChange]:
    tokens = payload.split(b"\0")
    changes: list[GitPathChange] = []
    index = 0
    kinds = {
        b"A": "added",
        b"D": "deleted",
        b"M": "modified",
        b"T": "type_changed",
    }
    while index < len(tokens):
        metadata = tokens[index]
        index += 1
        if not metadata:
            continue
        if index >= len(tokens) or not tokens[index]:
            raise FormalSourceProvenanceSecurityError(
                "Git raw diff contains an incomplete path record"
            )
        path_bytes = tokens[index]
        index += 1
        fields = metadata.split(b" ")
        if len(fields) != 5 or not fields[0].startswith(b":"):
            raise FormalSourceProvenanceSecurityError(
                "Git raw diff contains invalid object metadata"
            )
        old_mode = fields[0][1:].decode("ascii")
        new_mode = fields[1].decode("ascii")
        old_oid = fields[2].decode("ascii")
        new_oid = fields[3].decode("ascii")
        status_code = fields[4]
        kind = kinds.get(status_code)
        if kind is None:
            raise FormalSourceProvenanceSecurityError(
                "developer source commit uses an unsupported Git change kind"
            )
        zero = "0" * oid_length
        normalized_old_mode = None if old_mode == "000000" else old_mode
        normalized_new_mode = None if new_mode == "000000" else new_mode
        if (
            normalized_old_mode is not None
            and normalized_old_mode not in _ALLOWED_FILE_MODES
        ) or (
            normalized_new_mode is not None
            and normalized_new_mode not in _ALLOWED_FILE_MODES
        ):
            raise FormalSourceProvenanceSecurityError(
                "developer source commit introduces a non-regular source object"
            )
        try:
            path = _safe_source_path(path_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise FormalSourceProvenanceSecurityError(
                "developer source commit touches a reserved or unsafe path"
            ) from error
        changes.append(
            GitPathChange(
                path=path,
                change_kind=kind,  # type: ignore[arg-type]
                old_mode=normalized_old_mode,
                new_mode=normalized_new_mode,
                old_blob_sha=None if old_oid == zero else old_oid,
                new_blob_sha=None if new_oid == zero else new_oid,
            )
        )
    if not changes:
        raise FormalSourceProvenanceConflict(
            "implemented DeveloperResponse commit has no source changes"
        )
    if len({change.path for change in changes}) != len(changes):
        raise FormalSourceProvenanceSecurityError(
            "Git raw diff repeats a changed source path"
        )
    return changes


def _guard_payload(
    guard: ContentGuard | None,
    *,
    label: str,
    payload: bytes,
) -> None:
    if guard is None:
        return
    try:
        accepted = guard(label, payload)
    except Exception as error:
        raise FormalSourceProvenanceSecurityError(
            "source payload content guard could not establish isolation"
        ) from error
    if accepted is False:
        raise FormalSourceProvenanceSecurityError(
            "source payload content guard rejected protected or runtime content"
        )


def _commit_provenance(
    repository: _GitRepository,
    *,
    order: int,
    parent_commit_sha: str,
    binding: ApprovedDeveloperResponseBinding,
    content_guard: ContentGuard | None,
) -> tuple[DeveloperCommitProvenance, dict[str, bytes]]:
    commit_sha = repository.oid(binding.commit_sha, "commit")
    if commit_sha != binding.commit_sha:
        raise FormalSourceProvenanceConflict(
            "DeveloperResponse must declare the complete Git commit ID"
        )
    commit_payload = repository.object_payload(commit_sha, "commit")
    if repository.object_oid("commit", commit_payload) != commit_sha:
        raise FormalSourceProvenanceSecurityError(
            "Git commit object does not hash to its declared identity"
        )
    tree_sha, parents = _parse_commit_headers(commit_payload)
    if parents != [parent_commit_sha]:
        raise FormalSourceProvenanceConflict(
            "developer source history contains an undeclared or merge commit"
        )
    if repository.oid(commit_sha, "tree") != tree_sha:
        raise FormalSourceProvenanceSecurityError(
            "developer commit tree differs from its Git object"
        )
    raw_changes = repository.run(
        [
            "diff-tree",
            "-r",
            "--raw",
            "-z",
            "--no-commit-id",
            "--no-renames",
            "--full-index",
            parent_commit_sha,
            commit_sha,
            "--",
        ],
        limit=MAX_BINARY_DIFF_BYTES,
    )
    changes = _parse_raw_changes(raw_changes, oid_length=repository.oid_length)
    patch = repository.run(
        [
            "diff",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            parent_commit_sha,
            commit_sha,
            "--",
        ],
        limit=MAX_BINARY_DIFF_BYTES,
    )
    if not patch:
        raise FormalSourceProvenanceConflict(
            "implemented DeveloperResponse commit has no binary diff"
        )
    commit_path = (
        f"source-provenance/commits/{order:04d}-{commit_sha}.commit"
    )
    patch_path = (
        f"source-provenance/patches/{order:04d}-{commit_sha}.patch"
    )
    _guard_payload(content_guard, label=commit_path, payload=commit_payload)
    _guard_payload(content_guard, label=patch_path, payload=patch)
    files: dict[str, bytes] = {
        commit_path: commit_payload,
        patch_path: patch,
    }
    blob_objects: list[ArchivedGitObject] = []
    blob_ids = sorted(
        {
            oid
            for change in changes
            for oid in (change.old_blob_sha, change.new_blob_sha)
            if oid is not None
        }
    )
    for blob_oid in blob_ids:
        blob_payload = repository.object_payload(blob_oid, "blob")
        if repository.object_oid("blob", blob_payload) != blob_oid:
            raise FormalSourceProvenanceSecurityError(
                "Git blob object does not hash to its declared identity"
            )
        blob_path = f"source-provenance/objects/{blob_oid}.blob"
        _guard_payload(content_guard, label=blob_path, payload=blob_payload)
        files.setdefault(blob_path, blob_payload)
        blob_objects.append(
            ArchivedGitObject(
                object_type="blob",
                oid=blob_oid,
                archive_path=blob_path,
                payload_digest=_digest(blob_payload),
                size_bytes=len(blob_payload),
            )
        )
    commit_object = ArchivedGitObject(
        object_type="commit",
        oid=commit_sha,
        archive_path=commit_path,
        payload_digest=_digest(commit_payload),
        size_bytes=len(commit_payload),
    )
    binary_diff = ArchivedBinaryDiff(
        archive_path=patch_path,
        payload_digest=_digest(patch),
        size_bytes=len(patch),
    )
    payload = {
        "schema_version": "1.0",
        "order": order,
        "binding": binding.model_dump(mode="json"),
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "parent_commit_sha": parent_commit_sha,
        "commit_object": commit_object.model_dump(mode="json"),
        "changed_paths": [change.path for change in changes],
        "changes": [
            change.model_dump(mode="json", exclude_none=True)
            for change in changes
        ],
        "blob_objects": [
            blob.model_dump(mode="json") for blob in blob_objects
        ],
        "binary_diff": binary_diff.model_dump(mode="json"),
    }
    provenance = DeveloperCommitProvenance(
        **payload,
        provenance_digest=_digest(payload),
    )
    return provenance, files


def _private_root(path: Path) -> Path:
    lexical = Path(path)
    if lexical.exists() and lexical.is_symlink():
        raise FormalSourceProvenanceSecurityError(
            "source provenance state root cannot be a symlink"
        )
    lexical.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = lexical.resolve(strict=True)
    if not root.is_dir():
        raise FormalSourceProvenanceSecurityError(
            "source provenance state root must be a directory"
        )
    os.chmod(root, 0o700)
    return root


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise FormalSourceProvenanceSecurityError(
                "source provenance state contains a non-regular file"
            )
        existing = _read_private(path, limit=max(len(payload), 2 * 1024 * 1024))
        if not hmac.compare_digest(existing, payload):
            raise FormalSourceProvenanceConflict(
                "source provenance identity already has other content"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o400)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = _read_private(path, limit=max(len(payload), 2 * 1024 * 1024))
            if not hmac.compare_digest(existing, payload):
                raise FormalSourceProvenanceConflict(
                    "source provenance identity raced with other content"
                )
        else:
            os.chmod(path, 0o400)
    finally:
        temporary.unlink(missing_ok=True)


def _read_private(path: Path, *, limit: int) -> bytes:
    if path.is_symlink():
        raise FormalSourceProvenanceSecurityError(
            "source provenance state file cannot be a symlink"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FormalSourceProvenanceSecurityError(
            "source provenance state file is not safely readable"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o400
        ):
            raise FormalSourceProvenanceSecurityError(
                "source provenance state requires immutable private files"
            )
        payload = b""
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1)):
            payload += chunk
            if len(payload) > limit:
                raise FormalSourceProvenanceSecurityError(
                    "source provenance state file exceeds its size limit"
                )
        return payload
    finally:
        os.close(descriptor)


class FormalSourceProvenanceCoordinator:
    """Freeze and archive the exact Git commits authorized during one formal run."""

    def __init__(
        self,
        *,
        repository_root: Path,
        state_root: Path,
        content_guard: ContentGuard | None = None,
    ) -> None:
        self._repository = _GitRepository(repository_root)
        self._state_root = _private_root(state_root)
        self._assignments_root = self._state_root / "assignments"
        self._assignments_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self._assignments_root, 0o700)
        self._lock_path = self._state_root / ".source-provenance.lock"
        self._content_guard = content_guard
        self._process_instance_id = _process_identity_for_pid(os.getpid())

    def _assignment_root(self, assignment_id: UUID) -> Path:
        return self._assignments_root / str(assignment_id)

    def _baseline_path(self, assignment_id: UUID) -> Path:
        return self._assignment_root(assignment_id) / "baseline.json"

    def _projection_path(self, assignment_id: UUID) -> Path:
        return self._assignment_root(assignment_id) / "developer-projection.json"

    def _record_path(self, assignment_id: UUID, response_id: UUID) -> Path:
        return (
            self._assignment_root(assignment_id)
            / "records"
            / f"{response_id}.json"
        )

    def _promotion_root(self, assignment_id: UUID, response_id: UUID) -> Path:
        return (
            self._assignment_root(assignment_id)
            / "promotions"
            / str(response_id)
        )

    def _promotion_intent_path(self, assignment_id: UUID, response_id: UUID) -> Path:
        return self._promotion_root(assignment_id, response_id) / "intent.json"

    def _promotion_object_path(self, assignment_id: UUID, response_id: UUID) -> Path:
        return self._promotion_root(assignment_id, response_id) / "object-created.json"

    def _promotion_receipt_path(self, assignment_id: UUID, response_id: UUID) -> Path:
        return self._promotion_root(assignment_id, response_id) / "activated.json"

    def _promotion_activation_fence_path(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> Path:
        return (
            self._promotion_root(assignment_id, response_id)
            / "activation-fence.json"
        )

    def _promotion_reload_root(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> Path:
        return (
            self._promotion_root(assignment_id, response_id)
            / "reload-confirmations"
        )

    def _promotion_reload_path(
        self,
        assignment_id: UUID,
        response_id: UUID,
        process_instance_id: UUID,
    ) -> Path:
        return (
            self._promotion_reload_root(assignment_id, response_id)
            / f"{process_instance_id}.json"
        )

    def _promotion_adoption_path(
        self,
        assignment_id: UUID,
        response_id: UUID,
        report_revision: int,
        lease_id: UUID,
    ) -> Path:
        return (
            self._promotion_root(assignment_id, response_id)
            / "adoptions"
            / f"{report_revision:010d}-{lease_id}.json"
        )

    def _promotion_abort_path(self, assignment_id: UUID, response_id: UUID) -> Path:
        return self._promotion_root(assignment_id, response_id) / "aborted.json"

    def _activation_index_before_path(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> Path:
        return self._promotion_root(assignment_id, response_id) / "index-before.bin"

    def _activation_index_after_path(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> Path:
        return self._promotion_root(assignment_id, response_id) / "index-after.bin"

    def _activation_recovery_path(
        self,
        assignment_id: UUID,
        response_id: UUID,
        phase: str,
    ) -> Path:
        if not re.fullmatch(r"[a-z0-9_-]{1,80}", phase):
            raise FormalSourceProvenanceSecurityError(
                "activation recovery phase is invalid"
            )
        return (
            self._promotion_root(assignment_id, response_id)
            / "recovery"
            / f"{phase}.json"
        )

    def _promotion_blob_path(
        self,
        assignment_id: UUID,
        response_id: UUID,
        blob: DeveloperSourcePromotionBlob,
    ) -> Path:
        return (
            self._promotion_root(assignment_id, response_id)
            / "workspace-blobs"
            / f"{blob.blob_sha}.blob"
        )

    def _payload_path(self, assignment_id: UUID, archive_path: str) -> Path:
        normalized = _safe_archive_path(archive_path)
        return self._assignment_root(assignment_id).joinpath(
            *PurePosixPath(normalized).parts
        )

    def _read_payload_descriptor(
        self,
        assignment_id: UUID,
        descriptor: ArchivedGitObject | ArchivedBinaryDiff,
    ) -> bytes:
        if isinstance(descriptor, ArchivedBinaryDiff):
            limit = MAX_BINARY_DIFF_BYTES
        elif descriptor.object_type == "commit":
            limit = MAX_COMMIT_OBJECT_BYTES
        elif descriptor.object_type == "tree":
            limit = MAX_TREE_OBJECT_BYTES
        else:
            limit = MAX_BLOB_OBJECT_BYTES
        payload = _read_private(
            self._payload_path(assignment_id, descriptor.archive_path),
            limit=limit,
        )
        if (
            len(payload) != descriptor.size_bytes
            or not hmac.compare_digest(
                _digest(payload),
                descriptor.payload_digest,
            )
        ):
            raise FormalSourceProvenanceSecurityError(
                "source archive payload differs from its immutable record"
            )
        _guard_payload(
            self._content_guard,
            label=descriptor.archive_path,
            payload=payload,
        )
        return payload

    def _baseline_objects(
        self,
        state: GitWorktreeState,
    ) -> tuple[ArchivedGitObject, list[ArchivedGitObject], dict[str, bytes]]:
        commit, commit_payload = _baseline_commit_evidence(
            self._repository,
            commit_sha=state.head_commit_sha,
            content_guard=self._content_guard,
        )
        tree_sha, _parents = _parse_commit_headers(commit_payload)
        if tree_sha != state.head_tree_sha:
            raise FormalSourceProvenanceSecurityError(
                "baseline commit object differs from its frozen tree"
            )
        trees, tree_files = _tree_object_closure(
            self._repository,
            root_tree_oids=[state.head_tree_sha],
            content_guard=self._content_guard,
        )
        return commit, trees, {
            commit.archive_path: commit_payload,
            **tree_files,
        }

    def _lock(self) -> Any:
        class _LockContext:
            def __init__(self, path: Path) -> None:
                self.path = path
                self.descriptor: int | None = None

            def __enter__(self) -> None:
                self.descriptor = os.open(
                    self.path,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                fcntl.flock(self.descriptor, fcntl.LOCK_EX)

            def __exit__(self, *_args: Any) -> None:
                assert self.descriptor is not None
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
                os.close(self.descriptor)

        return _LockContext(self._lock_path)

    def _baseline_from_projection(
        self,
        projection: DeveloperSourceProjectionManifest,
    ) -> FormalSourceBaseline:
        state = GitWorktreeState(
            object_format=projection.object_format,
            head_commit_sha=projection.baseline_commit_sha,
            head_tree_sha=projection.baseline_tree_sha,
            status_digest=_digest(b""),
            tracked_change_count=0,
            untracked_file_count=0,
            conflicted_path_count=0,
            clean=True,
        )
        payload = {
            "schema_version": "1.0",
            "task_id": projection.task_id,
            "task_revision": projection.task_revision,
            "run_id": projection.run_id,
            "assignment_id": str(projection.assignment_id),
            "channel_id": str(projection.channel_id),
            "source_state": state.model_dump(mode="json"),
            "captured_at": projection.captured_at.isoformat().replace("+00:00", "Z"),
        }
        return FormalSourceBaseline(
            **payload,
            baseline_digest=_digest(payload),
        )

    def _materialize_projection(
        self,
        projection: DeveloperSourceProjectionManifest,
        destination: Path,
    ) -> None:
        lexical = Path(destination)
        if lexical.exists():
            if lexical.is_symlink() or not lexical.is_dir() or any(lexical.iterdir()):
                raise FormalSourceProvenanceConflict(
                    "developer projection destination must be an empty real directory"
                )
        else:
            lexical.mkdir(parents=True, mode=0o700)
        root = lexical.resolve(strict=True)
        os.chmod(root, 0o700)
        for entry in projection.entries:
            blob = self._repository.object_payload(entry.blob_sha, "blob")
            if (
                len(blob) != entry.size_bytes
                or not hmac.compare_digest(_digest(blob), entry.digest)
                or self._repository.object_oid("blob", blob) != entry.blob_sha
            ):
                raise FormalSourceProvenanceSecurityError(
                    "frozen developer projection differs from its Git blob"
                )
            _safe_materialize_file(root, entry.path, blob)
        _safe_materialize_file(
            root,
            DEVELOPER_SOURCE_MANIFEST_FILE,
            _canonical_json(projection),
        )
        os.chmod(root / DEVELOPER_SOURCE_MANIFEST_FILE, 0o400)

    def _persist_projection_blobs(
        self,
        projection: DeveloperSourceProjectionManifest,
    ) -> tuple[list[ArchivedGitObject], dict[str, bytes]]:
        """Persist every disclosed baseline blob as independent offline evidence."""

        descriptors: dict[str, ArchivedGitObject] = {}
        files: dict[str, bytes] = {}
        for entry in projection.entries:
            payload = self._repository.object_payload(entry.blob_sha, "blob")
            if (
                len(payload) != entry.size_bytes
                or not hmac.compare_digest(_digest(payload), entry.digest)
                or self._repository.object_oid("blob", payload) != entry.blob_sha
            ):
                raise FormalSourceProvenanceSecurityError(
                    "developer projection entry differs from its Git blob"
                )
            archive_path = f"source-provenance/objects/{entry.blob_sha}.blob"
            descriptor = ArchivedGitObject(
                object_type="blob",
                oid=entry.blob_sha,
                archive_path=archive_path,
                payload_digest=entry.digest,
                size_bytes=entry.size_bytes,
            )
            existing = descriptors.get(entry.blob_sha)
            if existing is not None and existing != descriptor:
                raise FormalSourceProvenanceSecurityError(
                    "developer projection reuses a Git blob with conflicting bytes"
                )
            descriptors[entry.blob_sha] = descriptor
            files[archive_path] = payload
        ordered = [descriptors[oid] for oid in sorted(descriptors)]
        for descriptor in ordered:
            payload = files[descriptor.archive_path]
            _write_immutable(
                self._payload_path(
                    projection.assignment_id,
                    descriptor.archive_path,
                ),
                payload,
            )
        return ordered, files

    def freeze_workspace_projection(
        self,
        *,
        task_id: str,
        task_revision: int,
        run_id: str,
        assignment_id: UUID,
        channel_id: UUID,
        captured_at: datetime,
        destination: Path,
    ) -> DeveloperSourceProjectionManifest:
        """Freeze and materialize the explicit HEAD-blob developer projection.

        This deliberately ignores unrelated tracked, staged, and untracked
        worktree changes: the developer receives only bytes addressed by the
        clean baseline commit/tree recorded in the manifest.
        """

        with self._lock():
            path = self._projection_path(assignment_id)
            if path.exists() or path.is_symlink():
                payload = _read_private(path, limit=MAX_ARCHIVE_PAYLOAD_BYTES)
                projection = DeveloperSourceProjectionManifest.model_validate(
                    _strict_json(payload)
                )
                if not hmac.compare_digest(payload, _canonical_json(projection)):
                    raise FormalSourceProvenanceSecurityError(
                        "developer projection manifest is not canonical"
                    )
                expected = (
                    task_id,
                    task_revision,
                    run_id,
                    assignment_id,
                    channel_id,
                )
                actual = (
                    projection.task_id,
                    projection.task_revision,
                    projection.run_id,
                    projection.assignment_id,
                    projection.channel_id,
                )
                if actual != expected:
                    raise FormalSourceProvenanceConflict(
                        "developer projection identity was reused by another task"
                    )
                baseline = self._baseline_from_projection(projection)
                _commit, _trees, object_files = self._baseline_objects(
                    baseline.source_state
                )
                for archive_path, object_payload in object_files.items():
                    _write_immutable(
                        self._payload_path(assignment_id, archive_path),
                        object_payload,
                    )
                _write_immutable(
                    self._baseline_path(assignment_id),
                    _canonical_json(baseline),
                )
                self._persist_projection_blobs(projection)
                self._materialize_projection(projection, destination)
                return projection

            first_branch = self._repository.symbolic_head()
            first_commit = self._repository.oid("HEAD", "commit")
            first_tree = self._repository.oid(first_commit, "tree")
            entries = _projected_tree_entries(self._repository, first_commit)
            second_branch = self._repository.symbolic_head()
            second_commit = self._repository.oid("HEAD", "commit")
            second_tree = self._repository.oid(second_commit, "tree")
            if (
                first_branch != second_branch
                or first_commit != second_commit
                or first_tree != second_tree
            ):
                raise FormalSourceProvenanceConflict(
                    "Git baseline moved while the developer projection was frozen"
                )
            payload = {
                "schema_version": "1.0",
                "task_id": task_id,
                "task_revision": task_revision,
                "run_id": run_id,
                "assignment_id": str(assignment_id),
                "channel_id": str(channel_id),
                "object_format": self._repository.object_format,
                "branch_ref": first_branch,
                "baseline_commit_sha": first_commit,
                "baseline_tree_sha": first_tree,
                "entries": [
                    entry.model_dump(mode="json") for entry in entries
                ],
                "allowed_new_prefixes": [
                    prefix.rstrip("/")
                    for prefix in sorted(_DEVELOPER_PROJECTION_PREFIXES)
                ],
                "allowed_new_files": sorted(_DEVELOPER_PROJECTION_FILES),
                "captured_at": _utc(captured_at).isoformat().replace("+00:00", "Z"),
            }
            projection = DeveloperSourceProjectionManifest(
                **payload,
                manifest_digest=_digest(payload),
            )
            baseline = self._baseline_from_projection(projection)
            _commit, _trees, object_files = self._baseline_objects(
                baseline.source_state
            )
            for archive_path, object_payload in object_files.items():
                _write_immutable(
                    self._payload_path(assignment_id, archive_path),
                    object_payload,
                )
            # The projection is the crash-recovery authority for recreating a
            # missing baseline file, so it is persisted first.
            _write_immutable(path, _canonical_json(projection))
            _write_immutable(
                self._baseline_path(assignment_id),
                _canonical_json(baseline),
            )
            self._persist_projection_blobs(projection)
            self._materialize_projection(projection, destination)
            return projection

    def load_workspace_projection(
        self,
        assignment_id: UUID,
    ) -> DeveloperSourceProjectionManifest:
        path = self._projection_path(assignment_id)
        if not path.is_file() or path.is_symlink():
            raise FormalSourceProvenanceConflict(
                "formal assignment has no developer source projection"
            )
        payload = _read_private(path, limit=MAX_ARCHIVE_PAYLOAD_BYTES)
        projection = DeveloperSourceProjectionManifest.model_validate(
            _strict_json(payload)
        )
        if not hmac.compare_digest(payload, _canonical_json(projection)):
            raise FormalSourceProvenanceSecurityError(
                "developer projection manifest is not canonical"
            )
        return projection

    def _load_promotion_intent(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> DeveloperSourcePromotionIntent:
        path = self._promotion_intent_path(assignment_id, response_id)
        payload = _read_private(path, limit=MAX_ARCHIVE_PAYLOAD_BYTES)
        intent = DeveloperSourcePromotionIntent.model_validate(_strict_json(payload))
        if not hmac.compare_digest(payload, _canonical_json(intent)):
            raise FormalSourceProvenanceSecurityError(
                "developer promotion intent is not canonical"
            )
        return intent

    def _load_object_receipt(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> _DeveloperSourceObjectReceipt:
        path = self._promotion_object_path(assignment_id, response_id)
        payload = _read_private(path, limit=2 * 1024 * 1024)
        receipt = _DeveloperSourceObjectReceipt.model_validate(_strict_json(payload))
        if not hmac.compare_digest(payload, _canonical_json(receipt)):
            raise FormalSourceProvenanceSecurityError(
                "developer Git object receipt is not canonical"
            )
        return receipt

    def _load_promotion_receipt(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> DeveloperSourcePromotionReceipt:
        path = self._promotion_receipt_path(assignment_id, response_id)
        payload = _read_private(path, limit=2 * 1024 * 1024)
        receipt = DeveloperSourcePromotionReceipt.model_validate(_strict_json(payload))
        if not hmac.compare_digest(payload, _canonical_json(receipt)):
            raise FormalSourceProvenanceSecurityError(
                "developer promotion receipt is not canonical"
            )
        return receipt

    def _load_activation_fence(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> DeveloperSourceActivationFence:
        path = self._promotion_activation_fence_path(
            assignment_id,
            response_id,
        )
        payload = _read_private(path, limit=2 * 1024 * 1024)
        fence = DeveloperSourceActivationFence.model_validate(
            _strict_json(payload)
        )
        if (
            fence.assignment_id != assignment_id
            or fence.response_id != response_id
            or not hmac.compare_digest(payload, _canonical_json(fence))
        ):
            raise FormalSourceProvenanceSecurityError(
                "developer activation fence identity or encoding changed"
            )
        return fence

    def _load_reload_confirmation(
        self,
        assignment_id: UUID,
        response_id: UUID,
        process_instance_id: UUID,
    ) -> DeveloperSourceReloadConfirmation:
        path = self._promotion_reload_path(
            assignment_id,
            response_id,
            process_instance_id,
        )
        payload = _read_private(path, limit=2 * 1024 * 1024)
        confirmation = DeveloperSourceReloadConfirmation.model_validate(
            _strict_json(payload)
        )
        if (
            confirmation.assignment_id != assignment_id
            or confirmation.response_id != response_id
            or confirmation.confirming_process_instance_id
            != process_instance_id
            or not hmac.compare_digest(payload, _canonical_json(confirmation))
        ):
            raise FormalSourceProvenanceSecurityError(
                "developer reload confirmation identity or encoding changed"
            )
        return confirmation

    def _load_reload_confirmations(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> list[DeveloperSourceReloadConfirmation]:
        root = self._promotion_reload_root(assignment_id, response_id)
        if not root.exists() and not root.is_symlink():
            return []
        if root.is_symlink() or not root.is_dir():
            raise FormalSourceProvenanceSecurityError(
                "developer reload confirmation registry is not a real directory"
            )
        confirmations: list[DeveloperSourceReloadConfirmation] = []
        for path in root.iterdir():
            if (
                path.name.startswith(".")
                or path.suffix != ".json"
                or path.is_symlink()
                or not path.is_file()
            ):
                raise FormalSourceProvenanceSecurityError(
                    "developer reload confirmation registry contains an unknown entry"
                )
            try:
                process_instance_id = UUID(path.stem)
            except ValueError as error:
                raise FormalSourceProvenanceSecurityError(
                    "developer reload confirmation has an invalid process identity"
                ) from error
            if path != self._promotion_reload_path(
                assignment_id,
                response_id,
                process_instance_id,
            ):
                raise FormalSourceProvenanceSecurityError(
                    "developer reload confirmation filename is not canonical"
                )
            confirmations.append(
                self._load_reload_confirmation(
                    assignment_id,
                    response_id,
                    process_instance_id,
                )
            )
        confirmations.sort(
            key=lambda item: (
                item.confirmed_at,
                str(item.confirming_process_instance_id),
            )
        )
        return confirmations

    def _load_promotion_abort(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> _DeveloperSourcePromotionAbort:
        path = self._promotion_abort_path(assignment_id, response_id)
        payload = _read_private(path, limit=2 * 1024 * 1024)
        abort = _DeveloperSourcePromotionAbort.model_validate(
            _strict_json(payload)
        )
        if (
            abort.assignment_id != assignment_id
            or abort.response_id != response_id
            or not hmac.compare_digest(payload, _canonical_json(abort))
        ):
            raise FormalSourceProvenanceSecurityError(
                "developer promotion abort is not canonical"
            )
        return abort

    def _load_promotion_adoptions(
        self,
        assignment_id: UUID,
        response_id: UUID,
    ) -> list[DeveloperSourcePromotionAdoption]:
        root = self._promotion_root(assignment_id, response_id) / "adoptions"
        if not root.exists():
            return []
        if root.is_symlink() or not root.is_dir():
            raise FormalSourceProvenanceSecurityError(
                "promotion adoption registry is not a real directory"
            )
        adoptions: list[DeveloperSourcePromotionAdoption] = []
        for path in root.iterdir():
            if (
                path.name.startswith(".")
                or path.suffix != ".json"
                or path.is_symlink()
                or not path.is_file()
            ):
                raise FormalSourceProvenanceSecurityError(
                    "promotion adoption registry contains an unknown entry"
                )
            payload = _read_private(path, limit=2 * 1024 * 1024)
            adoption = DeveloperSourcePromotionAdoption.model_validate(
                _strict_json(payload)
            )
            if (
                adoption.assignment_id != assignment_id
                or adoption.response_id != response_id
                or not hmac.compare_digest(payload, _canonical_json(adoption))
                or path
                != self._promotion_adoption_path(
                    assignment_id,
                    response_id,
                    adoption.report_revision,
                    adoption.lease_id,
                )
            ):
                raise FormalSourceProvenanceSecurityError(
                    "promotion adoption identity or encoding changed"
                )
            adoptions.append(adoption)
        adoptions.sort(key=lambda item: (item.report_revision, str(item.lease_id)))
        return adoptions

    def _authorized_promotion_revision(
        self,
        *,
        receipt: DeveloperSourcePromotionReceipt,
        report_id: UUID,
        report_revision: int,
    ) -> bool:
        if receipt.report_id != report_id:
            return False
        if receipt.report_revision == report_revision:
            return True
        return any(
            adoption.report_id == report_id
            and adoption.report_revision == report_revision
            and adoption.receipt_digest == receipt.receipt_digest
            and adoption.channel_id == receipt.channel_id
            for adoption in self._load_promotion_adoptions(
                receipt.assignment_id,
                receipt.response_id,
            )
        )

    def _require_activation_fence(
        self,
        *,
        receipt: DeveloperSourcePromotionReceipt,
        intent: DeveloperSourcePromotionIntent,
        object_receipt: _DeveloperSourceObjectReceipt,
    ) -> DeveloperSourceActivationFence:
        fence = self._load_activation_fence(
            receipt.assignment_id,
            receipt.response_id,
        )
        if (
            fence.channel_id != receipt.channel_id
            or fence.report_id != receipt.report_id
            or fence.report_revision != receipt.report_revision
            or fence.receipt_digest != receipt.receipt_digest
            or fence.intent_digest != intent.intent_digest
            or fence.branch_ref != receipt.branch_ref
            or fence.commit_sha != object_receipt.commit_sha
            or fence.tree_sha != object_receipt.tree_sha
            or fence.activation_process_instance_id
            != receipt.process_instance_id
            or fence.activated_at != receipt.activated_at
        ):
            raise FormalSourceProvenanceSecurityError(
                "developer activation fence differs from its receipt"
            )
        return fence

    def _require_active_promotion(
        self,
        *,
        projection: DeveloperSourceProjectionManifest,
        intent: DeveloperSourcePromotionIntent,
        receipt: DeveloperSourcePromotionReceipt,
        receipts: Sequence[DeveloperSourcePromotionReceipt],
    ) -> _DeveloperSourceObjectReceipt:
        """Require the exact tail commit, refs, worktree, and index endpoints."""

        object_receipt = self._load_object_receipt(
            receipt.assignment_id,
            receipt.response_id,
        )
        expected_hidden_ref = (
            f"refs/lilies/formal/{receipt.assignment_id}/{receipt.response_id}"
        )
        if (
            not receipts
            or receipts[-1] != receipt
            or intent.assignment_id != receipt.assignment_id
            or intent.channel_id != receipt.channel_id
            or intent.report_id != receipt.report_id
            or intent.report_revision != receipt.report_revision
            or intent.lease_id != receipt.lease_id
            or intent.response_id != receipt.response_id
            or intent.workspace_manifest_digest
            != receipt.workspace_manifest_digest
            or intent.source_manifest_digest != receipt.source_manifest_digest
            or intent.intent_digest != receipt.intent_digest
            or intent.branch_ref != receipt.branch_ref
            or intent.parent_commit_sha != receipt.parent_commit_sha
            or intent.parent_tree_sha != receipt.parent_tree_sha
            or [change.path for change in intent.changes] != receipt.changed_paths
            or receipt.assignment_id != projection.assignment_id
            or receipt.channel_id != projection.channel_id
            or receipt.source_manifest_digest != projection.manifest_digest
            or object_receipt.assignment_id != receipt.assignment_id
            or object_receipt.response_id != receipt.response_id
            or object_receipt.intent_digest != receipt.intent_digest
            or object_receipt.parent_commit_sha != receipt.parent_commit_sha
            or object_receipt.commit_sha != receipt.commit_sha
            or object_receipt.tree_sha != receipt.tree_sha
            or object_receipt.hidden_ref != expected_hidden_ref
            or self._repository.symbolic_head() != receipt.branch_ref
            or self._repository.oid(receipt.branch_ref, "commit")
            != receipt.commit_sha
            or self._repository.oid("HEAD", "commit") != receipt.commit_sha
            or self._repository.oid(receipt.commit_sha, "tree") != receipt.tree_sha
            or self._repository.oid(object_receipt.hidden_ref, "commit")
            != receipt.commit_sha
        ):
            raise FormalSourceProvenanceConflict(
                "developer promotion is not the exact active chain tail"
            )
        target_entries = {
            entry.path: (entry.mode, entry.blob_sha)
            for entry in _projected_tree_entries(
                self._repository,
                receipt.commit_sha,
            )
        }
        for path in receipt.changed_paths:
            target = target_entries.get(path)
            if (
                self._worktree_endpoint(path) != target
                or self._index_endpoint(path) != target
            ):
                raise FormalSourceProvenanceConflict(
                    "an activated source path differs from its commit endpoint"
                )
        self._require_activation_fence(
            receipt=receipt,
            intent=intent,
            object_receipt=object_receipt,
        )
        return object_receipt

    def _require_or_create_reload_confirmation(
        self,
        *,
        receipt: DeveloperSourcePromotionReceipt,
        intent: DeveloperSourcePromotionIntent,
        object_receipt: _DeveloperSourceObjectReceipt,
    ) -> DeveloperSourceReloadConfirmation | None:
        if receipt.reload_status == "not_required":
            return None
        fence = self._require_activation_fence(
            receipt=receipt,
            intent=intent,
            object_receipt=object_receipt,
        )
        path = self._promotion_reload_path(
            receipt.assignment_id,
            receipt.response_id,
            self._process_instance_id,
        )
        confirmations = self._load_reload_confirmations(
            receipt.assignment_id,
            receipt.response_id,
        )
        confirmation = next(
            (
                item
                for item in confirmations
                if item.confirming_process_instance_id
                == self._process_instance_id
            ),
            None,
        )
        if confirmation is None:
            if receipt.process_instance_id == self._process_instance_id:
                raise FormalSourceProvenanceConflict(
                    "activated developer code has not been loaded by a new process"
                )
            if not _code_generation_follows_activation(
                fence=fence,
                process_boot_id=_CODE_GENERATION_BOOT.digest,
                process_boot_started_at=_CODE_GENERATION_BOOT.started_at,
                process_monotonic_ns=_CODE_GENERATION_MONOTONIC_NS,
            ):
                raise FormalSourceProvenanceConflict(
                    "serving code generation predates developer activation"
                )
            payload = {
                "schema_version": "1.0",
                "assignment_id": str(receipt.assignment_id),
                "channel_id": str(receipt.channel_id),
                "report_id": str(receipt.report_id),
                "report_revision": receipt.report_revision,
                "response_id": str(receipt.response_id),
                "receipt_digest": receipt.receipt_digest,
                "intent_digest": receipt.intent_digest,
                "branch_ref": receipt.branch_ref,
                "hidden_ref": object_receipt.hidden_ref,
                "commit_sha": receipt.commit_sha,
                "tree_sha": receipt.tree_sha,
                "changed_paths": receipt.changed_paths,
                "status": "confirmed",
                "activation_process_instance_id": str(
                    receipt.process_instance_id
                ),
                "confirming_process_instance_id": str(
                    self._process_instance_id
                ),
                "activation_fence_digest": fence.fence_digest,
                "activation_boot_id": fence.activation_boot_id,
                "activation_boot_started_at": (
                    fence.activation_boot_started_at.isoformat()
                    .replace("+00:00", "Z")
                ),
                "activation_monotonic_ns": fence.activation_monotonic_ns,
                "process_generation_boot_id": _CODE_GENERATION_BOOT.digest,
                "process_generation_boot_started_at": (
                    _CODE_GENERATION_BOOT.started_at.isoformat()
                    .replace("+00:00", "Z")
                ),
                "process_generation_monotonic_ns": (
                    _CODE_GENERATION_MONOTONIC_NS
                ),
                "process_generation_loaded_at": (
                    _CODE_GENERATION_LOADED_AT.isoformat()
                    .replace("+00:00", "Z")
                ),
                "confirmed_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            confirmation = DeveloperSourceReloadConfirmation(
                **payload,
                confirmation_digest=_digest(payload),
            )
            _write_immutable(path, _canonical_json(confirmation))
        if (
            confirmation.assignment_id != receipt.assignment_id
            or confirmation.channel_id != receipt.channel_id
            or confirmation.report_id != receipt.report_id
            or confirmation.report_revision != receipt.report_revision
            or confirmation.response_id != receipt.response_id
            or confirmation.receipt_digest != receipt.receipt_digest
            or confirmation.intent_digest != intent.intent_digest
            or confirmation.branch_ref != receipt.branch_ref
            or confirmation.hidden_ref != object_receipt.hidden_ref
            or confirmation.commit_sha != receipt.commit_sha
            or confirmation.tree_sha != receipt.tree_sha
            or confirmation.changed_paths != receipt.changed_paths
            or confirmation.activation_process_instance_id
            != receipt.process_instance_id
            or confirmation.confirming_process_instance_id
            != self._process_instance_id
            or confirmation.activation_fence_digest != fence.fence_digest
            or confirmation.activation_boot_id != fence.activation_boot_id
            or confirmation.activation_boot_started_at
            != fence.activation_boot_started_at
            or confirmation.activation_monotonic_ns
            != fence.activation_monotonic_ns
            or confirmation.process_generation_boot_id
            != _CODE_GENERATION_BOOT.digest
            or confirmation.process_generation_boot_started_at
            != _CODE_GENERATION_BOOT.started_at
            or confirmation.process_generation_monotonic_ns
            != _CODE_GENERATION_MONOTONIC_NS
            or confirmation.process_generation_loaded_at
            != _CODE_GENERATION_LOADED_AT
            or not _code_generation_follows_activation(
                fence=fence,
                process_boot_id=confirmation.process_generation_boot_id,
                process_boot_started_at=(
                    confirmation.process_generation_boot_started_at
                ),
                process_monotonic_ns=(
                    confirmation.process_generation_monotonic_ns
                ),
            )
        ):
            raise FormalSourceProvenanceSecurityError(
                "developer code reload confirmation differs from its activation"
            )
        return confirmation

    def _validate_activation_recovery_records(
        self,
        intent: DeveloperSourcePromotionIntent,
    ) -> None:
        root = (
            self._promotion_root(intent.assignment_id, intent.response_id)
            / "recovery"
        )
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            raise FormalSourceProvenanceSecurityError(
                "activation recovery registry is not a real directory"
            )
        for path in root.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise FormalSourceProvenanceSecurityError(
                    "activation recovery registry contains an unknown entry"
                )
            payload = _read_private(path, limit=2 * 1024 * 1024)
            recovery = _strict_json(payload)
            identity = hashlib.sha256(payload).hexdigest()[:20]
            if (
                not isinstance(recovery, Mapping)
                or recovery.get("schema_version") != "1.0"
                or recovery.get("assignment_id") != str(intent.assignment_id)
                or recovery.get("response_id") != str(intent.response_id)
                or recovery.get("intent_digest") != intent.intent_digest
                or recovery.get("phase") != "activation_failed"
                or path.name != f"activation_failed_{identity}.json"
                or not hmac.compare_digest(payload, _canonical_json(recovery))
            ):
                raise FormalSourceProvenanceSecurityError(
                    "activation recovery record is invalid"
                )

    def _abort_safe_pending_promotions(
        self,
        *,
        assignment_id: UUID,
        excluding_response_id: UUID,
    ) -> None:
        """Seal only pending intents proven to be fully back at their parent."""

        root = self._assignment_root(assignment_id) / "promotions"
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            raise FormalSourceProvenanceSecurityError(
                "developer promotion registry is not a real directory"
            )
        for child in root.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_symlink() or not child.is_dir():
                raise FormalSourceProvenanceSecurityError(
                    "developer promotion registry contains an unknown entry"
                )
            try:
                response_id = UUID(child.name)
            except ValueError as error:
                raise FormalSourceProvenanceSecurityError(
                    "developer promotion registry contains an invalid response identity"
                ) from error
            if response_id == excluding_response_id:
                continue
            intent_path = self._promotion_intent_path(assignment_id, response_id)
            receipt_path = self._promotion_receipt_path(assignment_id, response_id)
            abort_path = self._promotion_abort_path(assignment_id, response_id)
            if (
                receipt_path.exists()
                or receipt_path.is_symlink()
                or abort_path.exists()
                or abort_path.is_symlink()
                or (not intent_path.exists() and not intent_path.is_symlink())
            ):
                continue
            intent = self._load_promotion_intent(assignment_id, response_id)
            self._validate_activation_recovery_records(intent)
            if (
                intent.assignment_id != assignment_id
                or self._repository.symbolic_head() != intent.branch_ref
                or self._repository.oid(intent.branch_ref, "commit")
                != intent.parent_commit_sha
                or self._repository.oid("HEAD", "commit")
                != intent.parent_commit_sha
                or self._repository.oid(intent.parent_commit_sha, "tree")
                != intent.parent_tree_sha
            ):
                continue
            backups: dict[str, Path] = {}
            safe = True
            for change in intent.changes:
                backup = self._worktree_backup_path(intent, change.path)
                parent = (
                    None
                    if change.old_blob_sha is None
                    else (str(change.old_mode), change.old_blob_sha)
                )
                if (
                    self._worktree_endpoint(
                        change.path,
                        allow_activation_link=backup.exists() and parent is not None,
                    )
                    != parent
                    or self._index_endpoint(change.path) != parent
                ):
                    safe = False
                    break
                if backup.exists() or backup.is_symlink():
                    if (
                        parent is None
                        or backup.is_symlink()
                        or self._file_endpoint(
                            backup,
                            allow_activation_link=True,
                        )
                        != parent
                    ):
                        safe = False
                        break
                    backups[change.path] = backup
            if not safe:
                continue
            payload = {
                "schema_version": "1.0",
                "assignment_id": str(assignment_id),
                "response_id": str(response_id),
                "intent_digest": intent.intent_digest,
                "branch_ref": intent.branch_ref,
                "parent_commit_sha": intent.parent_commit_sha,
                "status": "aborted_after_rollback",
                "aborted_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            abort = _DeveloperSourcePromotionAbort(
                **payload,
                abort_digest=_digest(payload),
            )
            _write_immutable(abort_path, _canonical_json(abort))
            self._cleanup_worktree_backups(backups)

    def _load_promotion_receipts(
        self,
        assignment_id: UUID,
    ) -> list[DeveloperSourcePromotionReceipt]:
        root = self._assignment_root(assignment_id) / "promotions"
        if not root.exists():
            return []
        if root.is_symlink() or not root.is_dir():
            raise FormalSourceProvenanceSecurityError(
                "developer promotion registry is not a real directory"
            )
        receipts: list[DeveloperSourcePromotionReceipt] = []
        pending: list[UUID] = []
        for child in root.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_symlink() or not child.is_dir():
                raise FormalSourceProvenanceSecurityError(
                    "developer promotion registry contains an unknown entry"
                )
            try:
                response_id = UUID(child.name)
            except ValueError as error:
                raise FormalSourceProvenanceSecurityError(
                    "developer promotion registry contains an invalid response identity"
                ) from error
            receipt_path = self._promotion_receipt_path(assignment_id, response_id)
            intent_path = self._promotion_intent_path(assignment_id, response_id)
            aborted_path = self._promotion_abort_path(assignment_id, response_id)
            if aborted_path.exists() or aborted_path.is_symlink():
                self._load_promotion_abort(assignment_id, response_id)
                continue
            if receipt_path.exists() or receipt_path.is_symlink():
                receipts.append(
                    self._load_promotion_receipt(assignment_id, response_id)
                )
            elif intent_path.exists() or intent_path.is_symlink():
                pending.append(response_id)
        if pending:
            raise FormalSourceProvenanceConflict(
                "another developer source promotion is not yet activated"
            )
        projection = self.load_workspace_projection(assignment_id)
        ordered: list[DeveloperSourcePromotionReceipt] = []
        parent = projection.baseline_commit_sha
        remaining = list(receipts)
        while remaining:
            matches = [item for item in remaining if item.parent_commit_sha == parent]
            if len(matches) != 1:
                raise FormalSourceProvenanceConflict(
                    "developer promotion receipts are not one exact linear chain"
                )
            selected = matches[0]
            ordered.append(selected)
            remaining.remove(selected)
            parent = selected.commit_sha
        return ordered

    def _workspace_delta(
        self,
        *,
        projection: DeveloperSourceProjectionManifest,
        workspace: Path,
        parent_commit_sha: str,
        workspace_manifest_digest: str,
        source_manifest_digest: str,
        require_changes: bool = True,
    ) -> tuple[list[GitPathChange], list[DeveloperSourcePromotionBlob], dict[str, bytes]]:
        lexical = Path(workspace)
        if lexical.is_symlink() or not lexical.is_dir():
            raise FormalSourceProvenanceSecurityError(
                "developer promotion workspace must be a real directory"
            )
        root = lexical.resolve(strict=True)
        mount_payload, _mount_stat = _read_workspace_file(
            root / ".lilies-mount-manifest.json"
        )
        if not hmac.compare_digest(_digest(mount_payload), workspace_manifest_digest):
            raise FormalSourceProvenanceConflict(
                "developer workspace mount manifest differs from its lease binding"
            )
        source_root = root / "source"
        if source_root.is_symlink() or not source_root.is_dir():
            raise FormalSourceProvenanceSecurityError(
                "developer workspace source projection is unavailable"
            )
        manifest_path = source_root / DEVELOPER_SOURCE_MANIFEST_FILE
        manifest_payload, manifest_stat = _read_workspace_file(manifest_path)
        try:
            workspace_projection = DeveloperSourceProjectionManifest.model_validate_json(
                manifest_payload
            )
        except Exception as error:
            raise FormalSourceProvenanceSecurityError(
                "developer source manifest is invalid"
            ) from error
        if (
            stat.S_IMODE(manifest_stat.st_mode) != 0o400
            or not hmac.compare_digest(
                manifest_payload,
                _canonical_json(workspace_projection),
            )
            or workspace_projection != projection
            or not hmac.compare_digest(
                projection.manifest_digest,
                source_manifest_digest,
            )
        ):
            raise FormalSourceProvenanceConflict(
                "developer source manifest was replaced or tampered"
            )

        workspace_files: dict[str, tuple[bytes, os.stat_result]] = {}
        for candidate in source_root.rglob("*"):
            relative = candidate.relative_to(source_root).as_posix()
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise FormalSourceProvenanceSecurityError(
                    "developer source projection cannot contain links"
                )
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise FormalSourceProvenanceSecurityError(
                    "developer source projection requires regular single-link files"
                )
            if relative == DEVELOPER_SOURCE_MANIFEST_FILE:
                continue
            try:
                normalized = _safe_source_path(relative)
            except ValueError as error:
                raise FormalSourceProvenanceSecurityError(
                    "developer source workspace contains an unsafe path"
                ) from error
            if not _is_projected_source_path(normalized):
                raise FormalSourceProvenanceSecurityError(
                    "developer source workspace contains a file outside its projection"
                )
            if normalized.casefold() in {
                item.casefold() for item in workspace_files
            }:
                raise FormalSourceProvenanceSecurityError(
                    "developer source workspace paths collide after normalization"
                )
            workspace_files[normalized] = _read_workspace_file(candidate)

        parent_entries = {
            entry.path: entry
            for entry in _projected_tree_entries(
                self._repository,
                parent_commit_sha,
            )
        }
        changes: list[GitPathChange] = []
        target_blobs: list[DeveloperSourcePromotionBlob] = []
        target_payloads: dict[str, bytes] = {}
        for path in sorted(set(parent_entries) | set(workspace_files)):
            parent = parent_entries.get(path)
            current = workspace_files.get(path)
            if parent is None and not projection.permits_new_path(path):
                raise FormalSourceProvenanceSecurityError(
                    "developer workspace adds a path outside its allowed-new boundary"
                )
            if current is None:
                assert parent is not None
                changes.append(
                    GitPathChange(
                        path=path,
                        change_kind="deleted",
                        old_mode=parent.mode,
                        old_blob_sha=parent.blob_sha,
                    )
                )
                continue
            payload, metadata = current
            mode: Literal["100644", "100755"]
            if parent is not None:
                # Materialization intentionally uses 0600. Existing Git mode is
                # therefore preserved; executable additions must opt in.
                mode = parent.mode
            else:
                mode = "100755" if metadata.st_mode & 0o111 else "100644"
            blob_sha = self._repository.object_oid("blob", payload)
            if (
                parent is not None
                and parent.mode == mode
                and parent.blob_sha == blob_sha
            ):
                continue
            change_kind: Literal["added", "modified"] = (
                "added" if parent is None else "modified"
            )
            changes.append(
                GitPathChange(
                    path=path,
                    change_kind=change_kind,
                    old_mode=parent.mode if parent is not None else None,
                    new_mode=mode,
                    old_blob_sha=parent.blob_sha if parent is not None else None,
                    new_blob_sha=blob_sha,
                )
            )
            descriptor = DeveloperSourcePromotionBlob(
                path=path,
                mode=mode,
                blob_sha=blob_sha,
                payload_digest=_digest(payload),
                size_bytes=len(payload),
            )
            target_blobs.append(descriptor)
            target_payloads[path] = payload
        if not changes and require_changes:
            raise FormalSourceProvenanceConflict(
                "developer workspace has no promotable source delta"
            )
        return changes, target_blobs, target_payloads

    def _freeze_promotion_intent(
        self,
        *,
        projection: DeveloperSourceProjectionManifest,
        workspace: Path,
        channel_id: UUID,
        report_id: UUID,
        report_revision: int,
        lease_id: UUID,
        lease_owner_id: str,
        response_id: UUID,
        idempotency_key: str,
        workspace_manifest_digest: str,
        source_manifest_digest: str,
        created_at: datetime,
    ) -> DeveloperSourcePromotionIntent:
        intent_path = self._promotion_intent_path(
            projection.assignment_id,
            response_id,
        )
        if intent_path.exists() or intent_path.is_symlink():
            intent = self._load_promotion_intent(
                projection.assignment_id,
                response_id,
            )
            expected = (
                projection.assignment_id,
                channel_id,
                report_id,
                report_revision,
                lease_id,
                lease_owner_id,
                response_id,
                idempotency_key,
                workspace_manifest_digest,
                source_manifest_digest,
            )
            actual = (
                intent.assignment_id,
                intent.channel_id,
                intent.report_id,
                intent.report_revision,
                intent.lease_id,
                intent.lease_owner_id,
                intent.response_id,
                intent.idempotency_key,
                intent.workspace_manifest_digest,
                intent.source_manifest_digest,
            )
            if actual != expected:
                raise FormalSourceProvenanceConflict(
                    "developer promotion identity was replayed with other input"
                )
            return intent

        self._abort_safe_pending_promotions(
            assignment_id=projection.assignment_id,
            excluding_response_id=response_id,
        )
        receipts = self._load_promotion_receipts(projection.assignment_id)
        parent_commit = (
            receipts[-1].commit_sha if receipts else projection.baseline_commit_sha
        )
        parent_tree = (
            receipts[-1].tree_sha if receipts else projection.baseline_tree_sha
        )
        if self._repository.oid(parent_commit, "tree") != parent_tree:
            raise FormalSourceProvenanceSecurityError(
                "developer promotion parent tree differs from its trusted receipt"
            )
        changes, target_blobs, target_payloads = self._workspace_delta(
            projection=projection,
            workspace=workspace,
            parent_commit_sha=parent_commit,
            workspace_manifest_digest=workspace_manifest_digest,
            source_manifest_digest=source_manifest_digest,
        )
        payload = {
            "schema_version": "1.0",
            "assignment_id": str(projection.assignment_id),
            "channel_id": str(channel_id),
            "report_id": str(report_id),
            "report_revision": report_revision,
            "lease_id": str(lease_id),
            "lease_owner_id": lease_owner_id,
            "response_id": str(response_id),
            "idempotency_key": idempotency_key,
            "workspace_manifest_digest": workspace_manifest_digest,
            "source_manifest_digest": source_manifest_digest,
            "branch_ref": projection.branch_ref,
            "parent_commit_sha": parent_commit,
            "parent_tree_sha": parent_tree,
            "changes": [
                change.model_dump(mode="json", exclude_none=True)
                for change in changes
            ],
            "target_blobs": [
                blob.model_dump(mode="json") for blob in target_blobs
            ],
            "created_at": _utc(created_at).isoformat().replace("+00:00", "Z"),
        }
        intent = DeveloperSourcePromotionIntent(
            **payload,
            intent_digest=_digest(payload),
        )
        for blob in target_blobs:
            target_payload = target_payloads[blob.path]
            if (
                len(target_payload) != blob.size_bytes
                or not hmac.compare_digest(
                    _digest(target_payload),
                    blob.payload_digest,
                )
            ):
                raise FormalSourceProvenanceConflict(
                    "developer workspace changed before its delta was persisted"
                )
            _write_immutable(
                self._promotion_blob_path(
                    projection.assignment_id,
                    response_id,
                    blob,
                ),
                target_payload,
            )
        _write_immutable(intent_path, _canonical_json(intent))
        return intent

    def _create_promotion_commit(
        self,
        intent: DeveloperSourcePromotionIntent,
    ) -> _DeveloperSourceObjectReceipt:
        path = self._promotion_object_path(intent.assignment_id, intent.response_id)
        if path.exists() or path.is_symlink():
            receipt = self._load_object_receipt(
                intent.assignment_id,
                intent.response_id,
            )
            if receipt.intent_digest != intent.intent_digest:
                raise FormalSourceProvenanceConflict(
                    "Git object receipt belongs to another promotion intent"
                )
            return receipt

        if self._repository.oid(intent.parent_commit_sha, "tree") != intent.parent_tree_sha:
            raise FormalSourceProvenanceConflict(
                "developer promotion parent commit/tree binding changed"
            )
        for blob in intent.target_blobs:
            payload = _read_private(
                self._promotion_blob_path(
                    intent.assignment_id,
                    intent.response_id,
                    blob,
                ),
                limit=MAX_BLOB_OBJECT_BYTES,
            )
            if (
                len(payload) != blob.size_bytes
                or not hmac.compare_digest(_digest(payload), blob.payload_digest)
                or self._repository.object_oid("blob", payload) != blob.blob_sha
            ):
                raise FormalSourceProvenanceSecurityError(
                    "frozen developer workspace blob differs from its promotion intent"
                )
            written = self._repository.run(
                ["hash-object", "-w", "--stdin"],
                limit=256,
                input_payload=payload,
                environment_overrides={"GIT_OPTIONAL_LOCKS": "1"},
            ).decode("ascii").strip()
            if written != blob.blob_sha:
                raise FormalSourceProvenanceSecurityError(
                    "Git wrote another object than the frozen workspace blob"
                )

        descriptor, index_name = tempfile.mkstemp(
            prefix=".lilies-promotion-index-",
            dir=self._state_root,
        )
        os.close(descriptor)
        index_path = Path(index_name)
        index_path.unlink()
        environment = {
            "GIT_INDEX_FILE": str(index_path),
            "GIT_OPTIONAL_LOCKS": "1",
        }
        try:
            self._repository.run(
                ["read-tree", intent.parent_commit_sha],
                limit=1024,
                environment_overrides=environment,
            )
            zero = "0" * self._repository.oid_length
            index_payload = b"".join(
                (
                    (
                        f"0 {zero}\t{change.path}\0"
                        if change.new_blob_sha is None
                        else (
                            f"{change.new_mode} {change.new_blob_sha}"
                            f"\t{change.path}\0"
                        )
                    ).encode("utf-8")
                )
                for change in intent.changes
            )
            self._repository.run(
                ["update-index", "-z", "--index-info"],
                limit=1024,
                input_payload=index_payload,
                environment_overrides=environment,
            )
            tree_sha = self._repository.run(
                ["write-tree"],
                limit=256,
                environment_overrides=environment,
            ).decode("ascii").strip()
        finally:
            index_path.unlink(missing_ok=True)
        if (
            len(tree_sha) != self._repository.oid_length
            or tree_sha == intent.parent_tree_sha
        ):
            raise FormalSourceProvenanceConflict(
                "developer promotion did not create a distinct valid Git tree"
            )
        git_date = intent.created_at.strftime("%Y-%m-%dT%H:%M:%S%z")
        commit_environment = {
            "GIT_AUTHOR_NAME": "Lilies Formal Promotion",
            "GIT_AUTHOR_EMAIL": "formal-promotion@lilies.local",
            "GIT_AUTHOR_DATE": git_date,
            "GIT_COMMITTER_NAME": "Lilies Formal Promotion",
            "GIT_COMMITTER_EMAIL": "formal-promotion@lilies.local",
            "GIT_COMMITTER_DATE": git_date,
            "GIT_OPTIONAL_LOCKS": "1",
        }
        message = (
            "Lilies formal developer promotion\n\n"
            f"Assignment: {intent.assignment_id}\n"
            f"Response: {intent.response_id}\n"
            f"Intent: {intent.intent_digest}\n"
        ).encode("utf-8")
        commit_sha = self._repository.run(
            ["commit-tree", tree_sha, "-p", intent.parent_commit_sha],
            limit=256,
            input_payload=message,
            environment_overrides=commit_environment,
        ).decode("ascii").strip()
        commit_payload = self._repository.object_payload(commit_sha, "commit")
        parsed_tree, parents = _parse_commit_headers(commit_payload)
        if parsed_tree != tree_sha or parents != [intent.parent_commit_sha]:
            raise FormalSourceProvenanceSecurityError(
                "promotion commit is not the exact frozen single-parent tree"
            )
        raw_changes = self._repository.run(
            [
                "diff-tree",
                "-r",
                "--raw",
                "-z",
                "--no-commit-id",
                "--no-renames",
                "--full-index",
                intent.parent_commit_sha,
                commit_sha,
                "--",
            ],
            limit=MAX_BINARY_DIFF_BYTES,
        )
        if _parse_raw_changes(
            raw_changes,
            oid_length=self._repository.oid_length,
        ) != intent.changes:
            raise FormalSourceProvenanceSecurityError(
                "promotion commit contains an extra or missing workspace delta"
            )
        hidden_ref = (
            f"refs/lilies/formal/{intent.assignment_id}/{intent.response_id}"
        )
        try:
            existing_ref = self._repository.oid(hidden_ref, "commit")
        except FormalSourceProvenanceError:
            existing_ref = None
        if existing_ref is None:
            self._repository.run(
                [
                    "update-ref",
                    hidden_ref,
                    commit_sha,
                    "0" * self._repository.oid_length,
                ],
                limit=1024,
                environment_overrides={"GIT_OPTIONAL_LOCKS": "1"},
            )
        elif existing_ref != commit_sha:
            raise FormalSourceProvenanceConflict(
                "promotion retention ref already names another commit"
            )
        receipt_payload = {
            "schema_version": "1.0",
            "assignment_id": str(intent.assignment_id),
            "response_id": str(intent.response_id),
            "intent_digest": intent.intent_digest,
            "parent_commit_sha": intent.parent_commit_sha,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "hidden_ref": hidden_ref,
            "created_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        receipt = _DeveloperSourceObjectReceipt(
            **receipt_payload,
            receipt_digest=_digest(receipt_payload),
        )
        _write_immutable(path, _canonical_json(receipt))
        return receipt

    def _worktree_endpoint(
        self,
        path: str,
        *,
        allow_activation_link: bool = False,
    ) -> tuple[str, str] | None:
        normalized = _safe_source_path(path)
        candidate = self._repository.root.joinpath(
            *PurePosixPath(normalized).parts
        )
        cursor = self._repository.root
        for segment in PurePosixPath(normalized).parts[:-1]:
            cursor = cursor / segment
            if cursor.exists() and (cursor.is_symlink() or not cursor.is_dir()):
                raise FormalSourceProvenanceSecurityError(
                    "promotion worktree path has an unsafe ancestor"
                )
        return self._file_endpoint(
            candidate,
            allow_activation_link=allow_activation_link,
        )

    def _file_endpoint(
        self,
        candidate: Path,
        *,
        allow_activation_link: bool = False,
    ) -> tuple[str, str] | None:
        if not candidate.exists() and not candidate.is_symlink():
            return None
        payload, metadata = _read_workspace_file(
            candidate,
            allow_activation_link=allow_activation_link,
        )
        mode = "100755" if metadata.st_mode & 0o111 else "100644"
        return mode, self._repository.object_oid("blob", payload)

    def _worktree_backup_path(
        self,
        intent: DeveloperSourcePromotionIntent,
        path: str,
    ) -> Path:
        normalized = _safe_source_path(path)
        candidate = self._repository.root.joinpath(
            *PurePosixPath(normalized).parts
        )
        identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
        return candidate.parent / (
            f".lilies-activation-{intent.response_id.hex[:12]}-{identity}.bak"
        )

    def _prepare_worktree_backups(
        self,
        *,
        intent: DeveloperSourcePromotionIntent,
        parent_entries: Mapping[str, tuple[str, str]],
        target_entries: Mapping[str, tuple[str, str]],
        allow_missing_target_backup: bool = False,
    ) -> dict[str, Path]:
        backups: dict[str, Path] = {}
        for change in intent.changes:
            parent = parent_entries.get(change.path)
            target = target_entries.get(change.path)
            backup = self._worktree_backup_path(intent, change.path)
            current = self._worktree_endpoint(
                change.path,
                allow_activation_link=backup.exists() and parent is not None,
            )
            if current not in {parent, target}:
                raise FormalSourceProvenanceConflict(
                    "promotion affected path changed before backup"
                )
            if parent is None:
                continue
            backups[change.path] = backup
            if backup.exists() or backup.is_symlink():
                if (
                    backup.is_symlink()
                    or self._file_endpoint(
                        backup,
                        allow_activation_link=True,
                    )
                    != parent
                ):
                    raise FormalSourceProvenanceConflict(
                        "activation backup contains concurrent user bytes"
                    )
                continue
            if current != parent:
                if allow_missing_target_backup and current == target:
                    continue
                raise FormalSourceProvenanceConflict(
                    "crash recovery is missing an affected-path backup"
                )
            candidate = self._repository.root.joinpath(
                *PurePosixPath(change.path).parts
            )
            try:
                os.link(candidate, backup, follow_symlinks=False)
            except (FileExistsError, OSError) as error:
                raise FormalSourceProvenanceConflict(
                    "affected-path activation backup could not be created"
                ) from error
            if (
                self._worktree_endpoint(
                    change.path,
                    allow_activation_link=True,
                )
                != parent
                or self._file_endpoint(
                    backup,
                    allow_activation_link=True,
                )
                != parent
            ):
                raise FormalSourceProvenanceConflict(
                    "affected path changed while its activation backup was created"
                )
        return backups

    def _apply_worktree_cas(
        self,
        *,
        intent: DeveloperSourcePromotionIntent,
        change: GitPathChange,
        parent: tuple[str, str] | None,
        target: tuple[str, str] | None,
        backup: Path | None,
    ) -> None:
        current = self._worktree_endpoint(
            change.path,
            allow_activation_link=parent is not None,
        )
        if current == target:
            return
        if current != parent:
            raise FormalSourceProvenanceConflict(
                "affected worktree path lost its compare-and-swap"
            )
        if parent is not None and (
            backup is None
            or self._file_endpoint(
                backup,
                allow_activation_link=True,
            )
            != parent
        ):
            raise FormalSourceProvenanceConflict(
                "affected worktree backup changed before activation"
            )
        # Recheck immediately before the path mutation. A hard-linked backup
        # observes writes through an already-open descriptor to the old inode.
        if (
            self._worktree_endpoint(
                change.path,
                allow_activation_link=parent is not None,
            )
            != parent
        ):
            raise FormalSourceProvenanceConflict(
                "affected worktree path changed immediately before activation"
            )
        self._write_worktree_endpoint(
            intent=intent,
            path=change.path,
            expected=parent,
            endpoint=target,
        )
        if parent is not None and self._file_endpoint(
            backup,
            allow_activation_link=True,
        ) != parent:
            # The hard-linked backup preserves writes through an already-open
            # descriptor. Never replace the live candidate with it: rollback
            # will either restore a still-trusted parent or leave every
            # third-party inode untouched.
            raise FormalSourceProvenanceConflict(
                "concurrent user bytes appeared during worktree activation"
            )
        if self._worktree_endpoint(change.path) != target:
            raise FormalSourceProvenanceConflict(
                "affected worktree path did not reach its target endpoint"
            )

    def _rollback_worktree(
        self,
        *,
        intent: DeveloperSourcePromotionIntent,
        parent_entries: Mapping[str, tuple[str, str]],
        target_entries: Mapping[str, tuple[str, str]],
        backups: Mapping[str, Path],
    ) -> dict[str, str]:
        outcome: dict[str, str] = {}
        for change in reversed(intent.changes):
            parent = parent_entries.get(change.path)
            target = target_entries.get(change.path)
            backup = backups.get(change.path)
            current = self._worktree_endpoint(
                change.path,
                allow_activation_link=(
                    parent is not None
                    and backup is not None
                    and backup.exists()
                ),
            )
            if current == parent:
                outcome[change.path] = "already_parent"
                continue
            if current != target:
                outcome[change.path] = "preserved_third_party"
                continue
            if parent is not None and (
                backup is None
                or self._file_endpoint(
                    backup,
                    allow_activation_link=True,
                )
                != parent
            ):
                outcome[change.path] = "backup_unavailable"
                continue
            try:
                self._write_worktree_endpoint(
                    intent=intent,
                    path=change.path,
                    expected=target,
                    endpoint=parent,
                )
            except FormalSourceProvenanceError:
                outcome[change.path] = "preserved_third_party"
                continue
            outcome[change.path] = (
                "rolled_back"
                if (
                    self._worktree_endpoint(change.path) == parent
                    and (
                        parent is None
                        or (
                            backup is not None
                            and self._file_endpoint(
                                backup,
                                allow_activation_link=True,
                            )
                            == parent
                        )
                    )
                )
                else "preserved_third_party"
            )
        return outcome

    def _cleanup_worktree_backups(self, backups: Mapping[str, Path]) -> None:
        for backup in backups.values():
            backup.unlink(missing_ok=True)

    def _index_endpoint(
        self,
        path: str,
    ) -> tuple[str, str] | None:
        payload = self._repository.run(
            ["ls-files", "--stage", "-z", "--", path],
            limit=64 * 1024,
        )
        records = [item for item in payload.split(b"\0") if item]
        if not records:
            return None
        if len(records) != 1:
            raise FormalSourceProvenanceConflict(
                "promotion path has conflicted or duplicate index entries"
            )
        metadata, separator, raw_path = records[0].partition(b"\t")
        fields = metadata.split(b" ")
        try:
            decoded_path = raw_path.decode("utf-8")
            mode = fields[0].decode("ascii")
            oid = fields[1].decode("ascii")
            stage = fields[2].decode("ascii")
        except (UnicodeDecodeError, IndexError) as error:
            raise FormalSourceProvenanceSecurityError(
                "Git index returned an invalid source entry"
            ) from error
        if (
            not separator
            or decoded_path != path
            or stage != "0"
            or mode not in _ALLOWED_FILE_MODES
            or len(oid) != self._repository.oid_length
        ):
            raise FormalSourceProvenanceConflict(
                "promotion path has an unsupported index state"
            )
        return mode, oid

    def _prepare_index_transaction(
        self,
        *,
        intent: DeveloperSourcePromotionIntent,
        target_entries: Mapping[str, tuple[str, str]],
    ) -> tuple[Path, bytes, bytes]:
        index_path = self._repository.git_path("index")
        before_path = self._activation_index_before_path(
            intent.assignment_id,
            intent.response_id,
        )
        after_path = self._activation_index_after_path(
            intent.assignment_id,
            intent.response_id,
        )
        if before_path.exists() or before_path.is_symlink():
            before = _read_private(before_path, limit=MAX_GIT_STATUS_BYTES)
        else:
            before, _metadata = _read_workspace_file(
                index_path,
                limit=MAX_GIT_STATUS_BYTES,
            )
            _write_immutable(before_path, before)
        if after_path.exists() or after_path.is_symlink():
            after = _read_private(after_path, limit=MAX_GIT_STATUS_BYTES)
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".lilies-index-proposal-",
                dir=self._state_root,
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                    handle.write(before)
                    handle.flush()
                    os.fsync(handle.fileno())
                zero = "0" * self._repository.oid_length
                index_payload = b"".join(
                    (
                        (
                            f"0 {zero}\t{change.path}\0"
                            if target_entries.get(change.path) is None
                            else (
                                f"{target_entries[change.path][0]} "
                                f"{target_entries[change.path][1]}"
                                f"\t{change.path}\0"
                            )
                        ).encode("utf-8")
                    )
                    for change in intent.changes
                )
                self._repository.run(
                    ["update-index", "-z", "--index-info"],
                    limit=1024,
                    input_payload=index_payload,
                    environment_overrides={
                        "GIT_INDEX_FILE": str(temporary),
                        "GIT_OPTIONAL_LOCKS": "1",
                    },
                )
                after, _metadata = _read_workspace_file(
                    temporary,
                    limit=MAX_GIT_STATUS_BYTES,
                )
            finally:
                os.close(descriptor)
                temporary.unlink(missing_ok=True)
            _write_immutable(after_path, after)
        current, _metadata = _read_workspace_file(
            index_path,
            limit=MAX_GIT_STATUS_BYTES,
        )
        if current not in {before, after}:
            raise FormalSourceProvenanceConflict(
                "Git index changed outside the activation transaction"
            )
        return index_path, before, after

    def _replace_index_cas(
        self,
        *,
        index_path: Path,
        expected: bytes,
        target: bytes,
    ) -> bool:
        current, _metadata = _read_workspace_file(
            index_path,
            limit=MAX_GIT_STATUS_BYTES,
        )
        if hmac.compare_digest(current, target):
            return True
        if not hmac.compare_digest(current, expected):
            return False
        lock_path = index_path.with_name(f"{index_path.name}.lock")
        try:
            descriptor = os.open(
                lock_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            return False
        installed = False
        try:
            current_after_lock, _metadata = _read_workspace_file(
                index_path,
                limit=MAX_GIT_STATUS_BYTES,
            )
            if not hmac.compare_digest(current_after_lock, expected):
                return False
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(target)
                handle.flush()
                os.fsync(handle.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(lock_path, index_path)
            installed = True
            return True
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not installed:
                lock_path.unlink(missing_ok=True)

    def _move_worktree_candidate_to_displacement(
        self,
        candidate: Path,
        displacement: Path,
    ) -> None:
        """Atomically capture the live inode in a same-directory quarantine."""

        _rename_noreplace(candidate, displacement)

    def _restore_displacement_without_overwrite(
        self,
        *,
        displacement: Path,
        candidate: Path,
    ) -> bool:
        """Restore a captured inode only while the live path remains absent."""

        try:
            os.link(displacement, candidate, follow_symlinks=False)
        except FileExistsError:
            return False
        try:
            candidate_stat = candidate.lstat()
            displaced_stat = displacement.lstat()
        except OSError:
            return False
        if (
            candidate_stat.st_dev != displaced_stat.st_dev
            or candidate_stat.st_ino != displaced_stat.st_ino
        ):
            return False
        # Keep the quarantine hard link on the rejected path. If another
        # process removes or replaces the restored live name immediately
        # afterward, these third-party bytes still have a durable inode.
        return True

    def _write_worktree_endpoint(
        self,
        *,
        intent: DeveloperSourcePromotionIntent,
        path: str,
        expected: tuple[str, str] | None,
        endpoint: tuple[str, str] | None,
    ) -> None:
        """Install one endpoint without ever overwriting the live candidate.

        Existing candidates are first moved into a same-directory quarantine.
        The inode actually moved is then hashed and compared with ``expected``.
        Target installation uses an O_EXCL hard link, so a third-party inode
        appearing in either race window is preserved rather than replaced.
        """

        normalized = _safe_source_path(path)
        candidate = self._repository.root.joinpath(
            *PurePosixPath(normalized).parts
        )
        cursor = self._repository.root
        for segment in PurePosixPath(normalized).parts[:-1]:
            cursor = cursor / segment
            if cursor.exists():
                if cursor.is_symlink() or not cursor.is_dir():
                    raise FormalSourceProvenanceSecurityError(
                        "promotion target has an unsafe ancestor"
                    )
            else:
                cursor.mkdir(mode=0o755)
        target_descriptor = -1
        temporary: Path | None = None
        displacement: Path | None = None
        displacement_captured = False
        try:
            if endpoint is not None:
                mode, oid = endpoint
                payload = self._repository.object_payload(oid, "blob")
                target_descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{candidate.name}.lilies-target-",
                    dir=candidate.parent,
                )
                temporary = Path(temporary_name)
                os.fchmod(
                    target_descriptor,
                    0o755 if mode == "100755" else 0o644,
                )
                with os.fdopen(
                    target_descriptor,
                    "wb",
                    closefd=False,
                ) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self._file_endpoint(temporary) != endpoint:
                    raise FormalSourceProvenanceSecurityError(
                        "promotion target staging file differs from its Git blob"
                    )
            if expected is not None:
                displacement = candidate.with_name(
                    f".{candidate.name}.lilies-displaced-"
                    f"{intent.response_id.hex[:12]}-{uuid4().hex}"
                )
                if (
                    self._worktree_endpoint(
                        path,
                        allow_activation_link=True,
                    )
                    != expected
                ):
                    raise FormalSourceProvenanceConflict(
                        "affected worktree path changed at its mutation boundary"
                    )
                try:
                    self._move_worktree_candidate_to_displacement(
                        candidate,
                        displacement,
                    )
                except FileExistsError as error:
                    raise FormalSourceProvenanceConflict(
                        "worktree displacement destination appeared concurrently"
                    ) from error
                displacement_captured = True
                displaced_endpoint = self._file_endpoint(
                    displacement,
                    allow_activation_link=True,
                )
                if displaced_endpoint != expected:
                    self._restore_displacement_without_overwrite(
                        displacement=displacement,
                        candidate=candidate,
                    )
                    raise FormalSourceProvenanceConflict(
                        "worktree mutation captured a concurrent third-party inode"
                    )
            elif candidate.exists() or candidate.is_symlink():
                raise FormalSourceProvenanceConflict(
                    "promotion target appeared at its creation boundary"
                )

            if endpoint is not None:
                assert temporary is not None
                try:
                    os.link(temporary, candidate)
                except FileExistsError as error:
                    raise FormalSourceProvenanceConflict(
                        "promotion target appeared while activation was running"
                    ) from error
                temporary.unlink()
                if self._worktree_endpoint(path) != endpoint:
                    raise FormalSourceProvenanceConflict(
                        "promotion target changed after exclusive installation"
                    )
            elif candidate.exists() or candidate.is_symlink():
                raise FormalSourceProvenanceConflict(
                    "promotion deletion raced with a third-party inode"
                )

            if displacement is not None:
                if (
                    self._file_endpoint(
                        displacement,
                        allow_activation_link=True,
                    )
                    != expected
                ):
                    raise FormalSourceProvenanceConflict(
                        "captured worktree inode changed during activation"
                    )
                displacement.unlink()
                displacement = None
        finally:
            if target_descriptor >= 0:
                os.close(target_descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if displacement is not None and displacement_captured:
                displaced_endpoint = self._file_endpoint(
                    displacement,
                    allow_activation_link=True,
                )
                if displaced_endpoint == expected:
                    displacement.unlink(missing_ok=True)

    def _activate_promotion(
        self,
        *,
        projection: DeveloperSourceProjectionManifest,
        intent: DeveloperSourcePromotionIntent,
        object_receipt: _DeveloperSourceObjectReceipt,
    ) -> DeveloperSourcePromotionReceipt:
        path = self._promotion_receipt_path(intent.assignment_id, intent.response_id)
        if path.exists() or path.is_symlink():
            receipt = self._load_promotion_receipt(
                intent.assignment_id,
                intent.response_id,
            )
            if (
                receipt.intent_digest != intent.intent_digest
                or receipt.commit_sha != object_receipt.commit_sha
            ):
                raise FormalSourceProvenanceConflict(
                    "activation receipt belongs to another promotion"
                )
            object_receipt = self._load_object_receipt(
                intent.assignment_id,
                intent.response_id,
            )
            self._require_activation_fence(
                receipt=receipt,
                intent=intent,
                object_receipt=object_receipt,
            )
            return receipt
        if projection.branch_ref != intent.branch_ref:
            raise FormalSourceProvenanceConflict(
                "developer promotion branch differs from its frozen projection"
            )
        if self._repository.symbolic_head() != intent.branch_ref:
            raise FormalSourceProvenanceConflict(
                "developer promotion cannot activate on another checked-out branch"
            )
        branch_commit = self._repository.oid(intent.branch_ref, "commit")
        if branch_commit not in {
            intent.parent_commit_sha,
            object_receipt.commit_sha,
        }:
            raise FormalSourceProvenanceConflict(
                "developer promotion parent no longer matches the active branch"
            )
        if self._repository.oid(object_receipt.hidden_ref, "commit") != object_receipt.commit_sha:
            raise FormalSourceProvenanceSecurityError(
                "developer promotion retention ref changed before activation"
            )

        parent_entries = {
            entry.path: (entry.mode, entry.blob_sha)
            for entry in _projected_tree_entries(
                self._repository,
                intent.parent_commit_sha,
            )
        }
        target_entries = dict(parent_entries)
        for change in intent.changes:
            expected_parent = parent_entries.get(change.path)
            declared_parent = (
                None
                if change.old_blob_sha is None
                else (str(change.old_mode), change.old_blob_sha)
            )
            if expected_parent != declared_parent:
                raise FormalSourceProvenanceSecurityError(
                    "promotion intent old endpoint differs from its parent tree"
                )
            if change.new_blob_sha is None:
                target_entries.pop(change.path, None)
            else:
                target_entries[change.path] = (
                    str(change.new_mode),
                    change.new_blob_sha,
                )

        for change in intent.changes:
            allowed = {
                parent_entries.get(change.path),
                target_entries.get(change.path),
            }
            worktree_endpoint = self._worktree_endpoint(change.path)
            index_endpoint = self._index_endpoint(change.path)
            if worktree_endpoint not in allowed:
                raise FormalSourceProvenanceConflict(
                    "promotion would overwrite a user worktree change"
                )
            if index_endpoint not in allowed:
                raise FormalSourceProvenanceConflict(
                    "promotion would overwrite a user staged change"
                )

        index_path, index_before, index_after = self._prepare_index_transaction(
            intent=intent,
            target_entries=target_entries,
        )
        backups: dict[str, Path] = {}
        activation_error: Exception | None = None
        try:
            backups = self._prepare_worktree_backups(
                intent=intent,
                parent_entries=parent_entries,
                target_entries=target_entries,
                allow_missing_target_backup=(
                    branch_commit == object_receipt.commit_sha
                ),
            )
            # Crash replay accepts a mixture of exact parent and exact target
            # endpoints, but no third state. Each write repeats the endpoint
            # CAS immediately before mutation.
            for change in intent.changes:
                self._apply_worktree_cas(
                    intent=intent,
                    change=change,
                    parent=parent_entries.get(change.path),
                    target=target_entries.get(change.path),
                    backup=backups.get(change.path),
                )
            if not self._replace_index_cas(
                index_path=index_path,
                expected=index_before,
                target=index_after,
            ):
                raise FormalSourceProvenanceConflict(
                    "promotion index lost its compare-and-swap"
                )
            for change in intent.changes:
                if (
                    self._worktree_endpoint(change.path)
                    != target_entries.get(change.path)
                    or self._index_endpoint(change.path)
                    != target_entries.get(change.path)
                ):
                    raise FormalSourceProvenanceConflict(
                        "promotion endpoint changed before branch activation"
                    )

            branch_commit = self._repository.oid(intent.branch_ref, "commit")
            if branch_commit == intent.parent_commit_sha:
                self._repository.run(
                    [
                        "update-ref",
                        intent.branch_ref,
                        object_receipt.commit_sha,
                        intent.parent_commit_sha,
                    ],
                    limit=1024,
                    environment_overrides={"GIT_OPTIONAL_LOCKS": "1"},
                )
            elif branch_commit != object_receipt.commit_sha:
                raise FormalSourceProvenanceConflict(
                    "developer promotion lost its branch compare-and-swap"
                )
        except Exception as error:
            activation_error = error

        if activation_error is not None:
            branch_after_error = self._repository.oid(intent.branch_ref, "commit")
            if branch_after_error == object_receipt.commit_sha and all(
                self._worktree_endpoint(change.path)
                == target_entries.get(change.path)
                and self._index_endpoint(change.path)
                == target_entries.get(change.path)
                for change in intent.changes
            ):
                # The ref CAS completed before a process interruption surfaced.
                activation_error = None
            else:
                index_rolled_back = self._replace_index_cas(
                    index_path=index_path,
                    expected=index_after,
                    target=index_before,
                )
                worktree_rollback = self._rollback_worktree(
                    intent=intent,
                    parent_entries=parent_entries,
                    target_entries=target_entries,
                    backups=backups,
                )
                recovery = {
                    "schema_version": "1.0",
                    "assignment_id": str(intent.assignment_id),
                    "response_id": str(intent.response_id),
                    "intent_digest": intent.intent_digest,
                    "phase": "activation_failed",
                    "error_type": type(activation_error).__name__,
                    "branch_commit_sha": branch_after_error,
                    "index_rolled_back": index_rolled_back,
                    "worktree_rollback": worktree_rollback,
                }
                recovery_identity = hashlib.sha256(
                    _canonical_json(recovery)
                ).hexdigest()[:20]
                _write_immutable(
                    self._activation_recovery_path(
                        intent.assignment_id,
                        intent.response_id,
                        f"activation_failed_{recovery_identity}",
                    ),
                    _canonical_json(recovery),
                )
                if index_rolled_back and all(
                    outcome in {"rolled_back", "already_parent"}
                    for outcome in worktree_rollback.values()
                ):
                    self._cleanup_worktree_backups(backups)
                raise activation_error
        if (
            self._repository.oid(intent.branch_ref, "commit")
            != object_receipt.commit_sha
            or self._repository.oid("HEAD", "commit")
            != object_receipt.commit_sha
        ):
            raise FormalSourceProvenanceConflict(
                "developer promotion commit is not active on HEAD"
            )
        self._cleanup_worktree_backups(backups)
        runtime_reload_required = any(
            change.path == "pyproject.toml"
            or change.path == "uv.lock"
            or change.path.startswith(
                ("platform/backend/", "platform/frontend/")
            )
            for change in intent.changes
        )
        fence_path = self._promotion_activation_fence_path(
            intent.assignment_id,
            intent.response_id,
        )
        existing_fence = (
            self._load_activation_fence(
                intent.assignment_id,
                intent.response_id,
            )
            if fence_path.exists() or fence_path.is_symlink()
            else None
        )
        activated_at = (
            existing_fence.activated_at
            if existing_fence is not None
            else datetime.now(timezone.utc)
        )
        activation_process_instance_id = (
            existing_fence.activation_process_instance_id
            if existing_fence is not None
            else self._process_instance_id
        )
        receipt_payload = {
            "schema_version": "1.0",
            "assignment_id": str(intent.assignment_id),
            "channel_id": str(intent.channel_id),
            "report_id": str(intent.report_id),
            "report_revision": intent.report_revision,
            "lease_id": str(intent.lease_id),
            "response_id": str(intent.response_id),
            "workspace_manifest_digest": intent.workspace_manifest_digest,
            "source_manifest_digest": intent.source_manifest_digest,
            "intent_digest": intent.intent_digest,
            "branch_ref": intent.branch_ref,
            "parent_commit_sha": intent.parent_commit_sha,
            "parent_tree_sha": intent.parent_tree_sha,
            "commit_sha": object_receipt.commit_sha,
            "tree_sha": object_receipt.tree_sha,
            "changed_paths": [change.path for change in intent.changes],
            "object_state": "object_created",
            "activation_state": "activated",
            "reload_status": (
                "restart_required" if runtime_reload_required else "not_required"
            ),
            "object_created_at": object_receipt.created_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "activated_at": activated_at.isoformat().replace("+00:00", "Z"),
            "process_instance_id": str(activation_process_instance_id),
        }
        receipt = DeveloperSourcePromotionReceipt(
            **receipt_payload,
            receipt_digest=_digest(receipt_payload),
        )
        if existing_fence is not None:
            if (
                existing_fence.channel_id != intent.channel_id
                or existing_fence.report_id != intent.report_id
                or existing_fence.report_revision != intent.report_revision
                or existing_fence.receipt_digest != receipt.receipt_digest
                or existing_fence.intent_digest != intent.intent_digest
                or existing_fence.branch_ref != intent.branch_ref
                or existing_fence.commit_sha != object_receipt.commit_sha
                or existing_fence.tree_sha != object_receipt.tree_sha
            ):
                raise FormalSourceProvenanceSecurityError(
                    "pending activation fence differs from active source"
                )
        else:
            activation_monotonic_ns = time.monotonic_ns()
            fence_payload = {
                "schema_version": "1.0",
                "assignment_id": str(intent.assignment_id),
                "channel_id": str(intent.channel_id),
                "report_id": str(intent.report_id),
                "report_revision": intent.report_revision,
                "response_id": str(intent.response_id),
                "receipt_digest": receipt.receipt_digest,
                "intent_digest": intent.intent_digest,
                "branch_ref": intent.branch_ref,
                "commit_sha": object_receipt.commit_sha,
                "tree_sha": object_receipt.tree_sha,
                "activation_process_instance_id": str(
                    activation_process_instance_id
                ),
                "activation_boot_id": _CODE_GENERATION_BOOT.digest,
                "activation_boot_started_at": (
                    _CODE_GENERATION_BOOT.started_at.isoformat()
                    .replace("+00:00", "Z")
                ),
                "activation_monotonic_ns": activation_monotonic_ns,
                "activated_at": (
                    activated_at.isoformat().replace("+00:00", "Z")
                ),
            }
            fence = DeveloperSourceActivationFence(
                **fence_payload,
                fence_digest=_digest(fence_payload),
            )
            _write_immutable(fence_path, _canonical_json(fence))
        _write_immutable(path, _canonical_json(receipt))
        return receipt

    def _adopt_promotion(
        self,
        *,
        projection: DeveloperSourceProjectionManifest,
        receipt: DeveloperSourcePromotionReceipt,
        report_id: UUID,
        report_revision: int,
        lease_id: UUID,
        lease_owner_id: str,
        idempotency_key: str,
        workspace: Path,
        workspace_manifest_digest: str,
        source_manifest_digest: str,
        adopted_at: datetime,
    ) -> DeveloperSourcePromotionAdoption:
        if (
            receipt.report_id != report_id
            or report_revision < receipt.report_revision
            or receipt.workspace_manifest_digest != workspace_manifest_digest
            or receipt.source_manifest_digest != source_manifest_digest
            or projection.manifest_digest != source_manifest_digest
        ):
            raise FormalSourceProvenanceConflict(
                "promotion adoption differs from its activated receipt"
            )
        path = self._promotion_adoption_path(
            receipt.assignment_id,
            receipt.response_id,
            report_revision,
            lease_id,
        )
        if path.exists() or path.is_symlink():
            for existing in self._load_promotion_adoptions(
                receipt.assignment_id,
                receipt.response_id,
            ):
                if (
                    existing.report_revision == report_revision
                    and existing.lease_id == lease_id
                ):
                    expected = (
                        receipt.channel_id,
                        report_id,
                        lease_owner_id,
                        idempotency_key,
                        receipt.receipt_digest,
                        workspace_manifest_digest,
                        source_manifest_digest,
                    )
                    actual = (
                        existing.channel_id,
                        existing.report_id,
                        existing.lease_owner_id,
                        existing.idempotency_key,
                        existing.receipt_digest,
                        existing.workspace_manifest_digest,
                        existing.source_manifest_digest,
                    )
                    if actual != expected:
                        raise FormalSourceProvenanceConflict(
                            "promotion adoption was replayed with other authority"
                        )
                    return existing
            raise FormalSourceProvenanceSecurityError(
                "promotion adoption path has no matching authority"
            )
        receipts = self._load_promotion_receipts(receipt.assignment_id)
        intent = self._load_promotion_intent(
            receipt.assignment_id,
            receipt.response_id,
        )
        self._require_active_promotion(
            projection=projection,
            intent=intent,
            receipt=receipt,
            receipts=receipts,
        )
        changes, _blobs, _payloads = self._workspace_delta(
            projection=projection,
            workspace=workspace,
            parent_commit_sha=receipt.commit_sha,
            workspace_manifest_digest=workspace_manifest_digest,
            source_manifest_digest=source_manifest_digest,
            require_changes=False,
        )
        if changes:
            raise FormalSourceProvenanceConflict(
                "promotion adoption requires the unchanged activated workspace"
            )
        final_entries = {
            entry.path: (entry.mode, entry.blob_sha)
            for entry in _projected_tree_entries(
                self._repository,
                receipt.commit_sha,
            )
        }
        if any(
            self._worktree_endpoint(path) != final_entries.get(path)
            or self._index_endpoint(path) != final_entries.get(path)
            for path in receipt.changed_paths
        ):
            raise FormalSourceProvenanceConflict(
                "promotion adoption found affected-path drift"
            )
        payload = {
            "schema_version": "1.0",
            "assignment_id": str(receipt.assignment_id),
            "channel_id": str(receipt.channel_id),
            "report_id": str(report_id),
            "report_revision": report_revision,
            "lease_id": str(lease_id),
            "lease_owner_id": lease_owner_id,
            "response_id": str(receipt.response_id),
            "idempotency_key": idempotency_key,
            "receipt_digest": receipt.receipt_digest,
            "workspace_manifest_digest": workspace_manifest_digest,
            "source_manifest_digest": source_manifest_digest,
            "adopted_at": _utc(adopted_at).isoformat().replace("+00:00", "Z"),
        }
        adoption = DeveloperSourcePromotionAdoption(
            **payload,
            adoption_digest=_digest(payload),
        )
        _write_immutable(path, _canonical_json(adoption))
        return adoption

    def promote_workspace_delta(
        self,
        *,
        assignment_id: UUID,
        channel_id: UUID,
        report_id: UUID,
        report_revision: int,
        lease_id: UUID,
        lease_owner_id: str,
        response_id: UUID,
        idempotency_key: str,
        workspace: Path,
        workspace_manifest_digest: str,
        source_manifest_digest: str,
        created_at: datetime,
    ) -> DeveloperSourcePromotionReceipt:
        """Create and safely activate only the exact no-``.git`` workspace delta."""

        with self._lock():
            projection = self.load_workspace_projection(assignment_id)
            if (
                projection.assignment_id != assignment_id
                or projection.channel_id != channel_id
                or not hmac.compare_digest(
                    projection.manifest_digest,
                    source_manifest_digest,
                )
            ):
                raise FormalSourceProvenanceConflict(
                    "developer promotion differs from its source projection"
                )
            receipt_path = self._promotion_receipt_path(
                assignment_id,
                response_id,
            )
            if receipt_path.exists() or receipt_path.is_symlink():
                intent = self._load_promotion_intent(assignment_id, response_id)
                receipt = self._load_promotion_receipt(assignment_id, response_id)
                immutable_expected = (
                    channel_id,
                    report_id,
                    workspace_manifest_digest,
                    source_manifest_digest,
                )
                immutable_actual = (
                    intent.channel_id,
                    intent.report_id,
                    intent.workspace_manifest_digest,
                    intent.source_manifest_digest,
                )
                if (
                    immutable_actual != immutable_expected
                    or receipt.intent_digest != intent.intent_digest
                ):
                    raise FormalSourceProvenanceConflict(
                        "developer promotion replay differs from its frozen request"
                    )
                original_authority = (
                    intent.report_revision,
                    intent.lease_id,
                    intent.lease_owner_id,
                    intent.idempotency_key,
                )
                requested_authority = (
                    report_revision,
                    lease_id,
                    lease_owner_id,
                    idempotency_key,
                )
                if requested_authority != original_authority:
                    self._adopt_promotion(
                        projection=projection,
                        receipt=receipt,
                        report_id=report_id,
                        report_revision=report_revision,
                        lease_id=lease_id,
                        lease_owner_id=lease_owner_id,
                        idempotency_key=idempotency_key,
                        workspace=workspace,
                        workspace_manifest_digest=workspace_manifest_digest,
                        source_manifest_digest=source_manifest_digest,
                        adopted_at=created_at,
                    )
                receipts = self._load_promotion_receipts(assignment_id)
                object_receipt = self._require_active_promotion(
                    projection=projection,
                    intent=intent,
                    receipt=receipt,
                    receipts=receipts,
                )
                reload_path = self._promotion_reload_path(
                    assignment_id,
                    response_id,
                    self._process_instance_id,
                )
                if (
                    receipt.reload_status == "restart_required"
                    and (
                        receipt.process_instance_id != self._process_instance_id
                        or reload_path.exists()
                        or reload_path.is_symlink()
                    )
                ):
                    self._require_or_create_reload_confirmation(
                        receipt=receipt,
                        intent=intent,
                        object_receipt=object_receipt,
                    )
                return receipt
            intent = self._freeze_promotion_intent(
                projection=projection,
                workspace=workspace,
                channel_id=channel_id,
                report_id=report_id,
                report_revision=report_revision,
                lease_id=lease_id,
                lease_owner_id=lease_owner_id,
                response_id=response_id,
                idempotency_key=idempotency_key,
                workspace_manifest_digest=workspace_manifest_digest,
                source_manifest_digest=source_manifest_digest,
                created_at=created_at,
            )
            object_receipt = self._create_promotion_commit(intent)
            return self._activate_promotion(
                projection=projection,
                intent=intent,
                object_receipt=object_receipt,
            )

    def promoted_response_is_effective(
        self,
        *,
        assignment_id: UUID,
        channel_id: UUID,
        report_id: UUID,
        report_revision: int,
        response_id: UUID,
        commit_sha: str,
    ) -> bool:
        """Resolve only an activated assignment receipt, never an arbitrary commit."""

        with self._lock():
            try:
                projection = self.load_workspace_projection(assignment_id)
                receipt = self._load_promotion_receipt(
                    assignment_id,
                    response_id,
                )
                intent = self._load_promotion_intent(
                    assignment_id,
                    response_id,
                )
                receipts = self._load_promotion_receipts(assignment_id)
                if (
                    projection.assignment_id != assignment_id
                    or projection.channel_id != channel_id
                    or receipt.channel_id != channel_id
                    or receipt.report_id != report_id
                    or not self._authorized_promotion_revision(
                        receipt=receipt,
                        report_id=report_id,
                        report_revision=report_revision,
                    )
                    or receipt.commit_sha != commit_sha
                ):
                    return False
                object_receipt = self._require_active_promotion(
                    projection=projection,
                    intent=intent,
                    receipt=receipt,
                    receipts=receipts,
                )
                self._require_or_create_reload_confirmation(
                    receipt=receipt,
                    intent=intent,
                    object_receipt=object_receipt,
                )
                return True
            except Exception:
                return False

    def freeze_baseline(
        self,
        *,
        task_id: str,
        task_revision: int,
        run_id: str,
        assignment_id: UUID,
        channel_id: UUID,
        captured_at: datetime,
    ) -> FormalSourceBaseline:
        """Persist the clean repository baseline before Codex receives a lease."""

        with self._lock():
            path = self._baseline_path(assignment_id)
            if path.exists() or path.is_symlink():
                baseline = FormalSourceBaseline.model_validate(
                    _strict_json(_read_private(path, limit=2 * 1024 * 1024))
                )
                expected = (
                    task_id,
                    task_revision,
                    run_id,
                    assignment_id,
                    channel_id,
                )
                actual = (
                    baseline.task_id,
                    baseline.task_revision,
                    baseline.run_id,
                    baseline.assignment_id,
                    baseline.channel_id,
                )
                if actual != expected:
                    raise FormalSourceProvenanceConflict(
                        "formal assignment source baseline was reused by another task"
                    )
                commit, trees, expected_files = self._baseline_objects(
                    baseline.source_state
                )
                for descriptor in (commit, *trees):
                    persisted = self._read_payload_descriptor(
                        assignment_id,
                        descriptor,
                    )
                    if not hmac.compare_digest(
                        persisted,
                        expected_files[descriptor.archive_path],
                    ):
                        raise FormalSourceProvenanceSecurityError(
                            "frozen baseline object differs from Git"
                        )
                return baseline
            state = self._repository.state()
            if not state.clean:
                raise FormalSourceProvenanceConflict(
                    "formal assignment cannot start from dirty or untracked developer source"
                )
            payload = {
                "schema_version": "1.0",
                "task_id": task_id,
                "task_revision": task_revision,
                "run_id": run_id,
                "assignment_id": str(assignment_id),
                "channel_id": str(channel_id),
                "source_state": state.model_dump(mode="json"),
                "captured_at": _utc(captured_at).isoformat().replace(
                    "+00:00",
                    "Z",
                ),
            }
            baseline = FormalSourceBaseline(
                **payload,
                baseline_digest=_digest(payload),
            )
            _commit, _trees, object_files = self._baseline_objects(state)
            for archive_path, object_payload in object_files.items():
                _write_immutable(
                    self._payload_path(assignment_id, archive_path),
                    object_payload,
                )
            _write_immutable(path, _canonical_json(baseline))
            return baseline

    def _load_baseline(self, assignment_id: UUID) -> FormalSourceBaseline:
        path = self._baseline_path(assignment_id)
        if not path.is_file() or path.is_symlink():
            raise FormalSourceProvenanceConflict(
                "formal assignment has no frozen source baseline"
            )
        payload = _read_private(path, limit=2 * 1024 * 1024)
        baseline = FormalSourceBaseline.model_validate(_strict_json(payload))
        if not hmac.compare_digest(payload, _canonical_json(baseline)):
            raise FormalSourceProvenanceSecurityError(
                "formal source baseline is not canonical"
            )
        return baseline

    def _load_records(
        self,
        assignment_id: UUID,
    ) -> list[DeveloperCommitProvenance]:
        records_root = self._assignment_root(assignment_id) / "records"
        if not records_root.exists():
            return []
        if records_root.is_symlink() or not records_root.is_dir():
            raise FormalSourceProvenanceSecurityError(
                "source provenance record root is not a real directory"
            )
        records: list[DeveloperCommitProvenance] = []
        for path in records_root.iterdir():
            if path.name.startswith("."):
                continue
            if path.suffix != ".json" or not path.is_file() or path.is_symlink():
                raise FormalSourceProvenanceSecurityError(
                    "source provenance record root contains an unknown entry"
                )
            payload = _read_private(path, limit=8 * 1024 * 1024)
            record = DeveloperCommitProvenance.model_validate(_strict_json(payload))
            if (
                path.stem != str(record.binding.response_id)
                or not hmac.compare_digest(payload, _canonical_json(record))
            ):
                raise FormalSourceProvenanceSecurityError(
                    "source provenance record identity or encoding changed"
                )
            records.append(record)
        records.sort(key=lambda record: record.order)
        if [record.order for record in records] != list(range(1, len(records) + 1)):
            raise FormalSourceProvenanceConflict(
                "source provenance record sequence is incomplete"
            )
        return records

    def _read_record_files(
        self,
        assignment_id: UUID,
        record: DeveloperCommitProvenance,
    ) -> dict[str, bytes]:
        descriptors: list[
            ArchivedGitObject | ArchivedBinaryDiff
        ] = [
            record.commit_object,
            *record.blob_objects,
            record.binary_diff,
        ]
        files: dict[str, bytes] = {}
        for descriptor in descriptors:
            existing = files.get(descriptor.archive_path)
            if existing is not None:
                if (
                    not hmac.compare_digest(
                        _digest(existing),
                        descriptor.payload_digest,
                    )
                    or len(existing) != descriptor.size_bytes
                ):
                    raise FormalSourceProvenanceSecurityError(
                        "duplicate source archive object has conflicting bytes"
                    )
                continue
            limit = (
                MAX_BINARY_DIFF_BYTES
                if isinstance(descriptor, ArchivedBinaryDiff)
                else (
                    MAX_COMMIT_OBJECT_BYTES
                    if descriptor.object_type == "commit"
                    else MAX_BLOB_OBJECT_BYTES
                )
            )
            payload = _read_private(
                self._payload_path(assignment_id, descriptor.archive_path),
                limit=limit,
            )
            if (
                len(payload) != descriptor.size_bytes
                or not hmac.compare_digest(
                    _digest(payload),
                    descriptor.payload_digest,
                )
            ):
                raise FormalSourceProvenanceSecurityError(
                    "source archive payload differs from its immutable record"
                )
            _guard_payload(
                self._content_guard,
                label=descriptor.archive_path,
                payload=payload,
            )
            files[descriptor.archive_path] = payload
        return files

    def record_approved_response(
        self,
        *,
        assignment_id: UUID,
        binding: ApprovedDeveloperResponseBinding,
    ) -> DeveloperCommitProvenance:
        """Attest one approved response while its commit is the clean HEAD."""

        with self._lock():
            baseline = self._load_baseline(assignment_id)
            if binding.channel_id != baseline.channel_id:
                raise FormalSourceProvenanceConflict(
                    "DeveloperResponse is bound to another formal channel"
                )
            records = self._load_records(assignment_id)
            for existing in records:
                if existing.binding.response_id == binding.response_id:
                    if existing.binding != binding:
                        raise FormalSourceProvenanceConflict(
                            "DeveloperResponse source identity was replayed with other content"
                        )
                    self._read_record_files(assignment_id, existing)
                    trees, tree_files = _tree_object_closure(
                        self._repository,
                        root_tree_oids=[existing.tree_sha],
                        content_guard=self._content_guard,
                    )
                    for tree in trees:
                        if not hmac.compare_digest(
                            self._read_payload_descriptor(
                                assignment_id,
                                tree,
                            ),
                            tree_files[tree.archive_path],
                        ):
                            raise FormalSourceProvenanceSecurityError(
                                "frozen developer tree differs from Git"
                            )
                    return existing
            if records and (
                binding.response_message_seq
                <= records[-1].binding.response_message_seq
            ):
                raise FormalSourceProvenanceConflict(
                    "DeveloperResponse source commits are not in message order"
                )
            previous = (
                records[-1].commit_sha
                if records
                else baseline.source_state.head_commit_sha
            )
            before = self._repository.state()
            if not before.clean or before.head_commit_sha != binding.commit_sha:
                raise FormalSourceProvenanceConflict(
                    "approved DeveloperResponse is not the clean developer source HEAD"
                )
            provenance, files = _commit_provenance(
                self._repository,
                order=len(records) + 1,
                parent_commit_sha=previous,
                binding=binding,
                content_guard=self._content_guard,
            )
            _trees, tree_files = _tree_object_closure(
                self._repository,
                root_tree_oids=[provenance.tree_sha],
                content_guard=self._content_guard,
            )
            files.update(tree_files)
            after = self._repository.state()
            if before != after:
                raise FormalSourceProvenanceConflict(
                    "developer source changed while its commit was archived"
                )
            assignment_root = self._assignment_root(assignment_id)
            assignment_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(assignment_root, 0o700)
            for archive_path, payload in files.items():
                _write_immutable(
                    self._payload_path(assignment_id, archive_path),
                    payload,
                )
            _write_immutable(
                self._record_path(assignment_id, binding.response_id),
                _canonical_json(provenance),
            )
            return provenance

    def record_promoted_response(
        self,
        *,
        assignment_id: UUID,
        binding: ApprovedDeveloperResponseBinding,
    ) -> DeveloperCommitProvenance:
        """Archive only the effective promotion bound to this exact response."""

        with self._lock():
            baseline = self._load_baseline(assignment_id)
            projection = self.load_workspace_projection(assignment_id)
            if (
                binding.channel_id != baseline.channel_id
                or projection.channel_id != binding.channel_id
            ):
                raise FormalSourceProvenanceConflict(
                    "DeveloperResponse is bound to another formal source projection"
                )
            receipt = self._load_promotion_receipt(
                assignment_id,
                binding.response_id,
            )
            intent = self._load_promotion_intent(
                assignment_id,
                binding.response_id,
            )
            if (
                projection.assignment_id != assignment_id
                or receipt.assignment_id != assignment_id
                or receipt.channel_id != binding.channel_id
                or binding.report_id != receipt.report_id
                or binding.response_id != receipt.response_id
                or receipt.commit_sha != binding.commit_sha
                or intent.assignment_id != assignment_id
                or intent.channel_id != binding.channel_id
                or intent.report_id != binding.report_id
                or intent.report_revision != receipt.report_revision
                or intent.lease_id != receipt.lease_id
                or intent.response_id != binding.response_id
                or intent.workspace_manifest_digest
                != receipt.workspace_manifest_digest
                or intent.source_manifest_digest
                != receipt.source_manifest_digest
                or intent.branch_ref != receipt.branch_ref
                or intent.parent_commit_sha != receipt.parent_commit_sha
                or intent.parent_tree_sha != receipt.parent_tree_sha
                or [change.path for change in intent.changes]
                != receipt.changed_paths
                or receipt.intent_digest != intent.intent_digest
                or receipt.source_manifest_digest != projection.manifest_digest
                or not self._authorized_promotion_revision(
                    receipt=receipt,
                    report_id=binding.report_id,
                    report_revision=binding.response_report_revision,
                )
            ):
                raise FormalSourceProvenanceConflict(
                    "DeveloperResponse commit differs from its trusted promotion"
                )
            records = self._load_records(assignment_id)
            for existing in records:
                if existing.binding.response_id == binding.response_id:
                    if existing.binding != binding:
                        raise FormalSourceProvenanceConflict(
                            "DeveloperResponse source identity was replayed with other content"
                        )
                    self._read_record_files(assignment_id, existing)
                    return existing
            receipts = self._load_promotion_receipts(assignment_id)
            object_receipt = self._require_active_promotion(
                projection=projection,
                intent=intent,
                receipt=receipt,
                receipts=receipts,
            )
            self._require_or_create_reload_confirmation(
                receipt=receipt,
                intent=intent,
                object_receipt=object_receipt,
            )
            if records and (
                binding.response_message_seq
                <= records[-1].binding.response_message_seq
            ):
                raise FormalSourceProvenanceConflict(
                    "DeveloperResponse source commits are not in message order"
                )
            previous = (
                records[-1].commit_sha
                if records
                else baseline.source_state.head_commit_sha
            )
            if receipt.parent_commit_sha != previous:
                raise FormalSourceProvenanceConflict(
                    "promoted DeveloperResponse is not the next approved commit"
                )
            provenance, files = _commit_provenance(
                self._repository,
                order=len(records) + 1,
                parent_commit_sha=previous,
                binding=binding,
                content_guard=self._content_guard,
            )
            if (
                provenance.tree_sha != receipt.tree_sha
                or provenance.changes != intent.changes
                or provenance.changed_paths != receipt.changed_paths
            ):
                raise FormalSourceProvenanceSecurityError(
                    "archived Git commit differs from the frozen workspace delta"
                )
            _trees, tree_files = _tree_object_closure(
                self._repository,
                root_tree_oids=[provenance.tree_sha],
                content_guard=self._content_guard,
            )
            files.update(tree_files)
            assignment_root = self._assignment_root(assignment_id)
            assignment_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(assignment_root, 0o700)
            for archive_path, payload in files.items():
                _write_immutable(
                    self._payload_path(assignment_id, archive_path),
                    payload,
                )
            _write_immutable(
                self._record_path(assignment_id, binding.response_id),
                _canonical_json(provenance),
            )
            return provenance

    def finalize_archive(
        self,
        *,
        assignment_id: UUID,
        expected_bindings: Sequence[ApprovedDeveloperResponseBinding],
        finalized_at: datetime,
    ) -> FormalSourceProvenanceArchive:
        """Build deterministic archive files only for the exact approved responses."""

        with self._lock():
            baseline = self._load_baseline(assignment_id)
            records = self._load_records(assignment_id)
            expected = list(expected_bindings)
            if [record.binding for record in records] != expected:
                raise FormalSourceProvenanceConflict(
                    "source archive does not exactly match approved DeveloperResponses"
                )
            files: dict[str, bytes] = {}
            previous = baseline.source_state.head_commit_sha
            for record in records:
                if record.parent_commit_sha != previous:
                    raise FormalSourceProvenanceConflict(
                        "source archive commit sequence is not linear"
                    )
                reconstructed, repository_files = _commit_provenance(
                    self._repository,
                    order=record.order,
                    parent_commit_sha=previous,
                    binding=record.binding,
                    content_guard=self._content_guard,
                )
                if reconstructed != record:
                    raise FormalSourceProvenanceSecurityError(
                        "durable source provenance differs from Git"
                    )
                persisted_files = self._read_record_files(assignment_id, record)
                if persisted_files != repository_files:
                    raise FormalSourceProvenanceSecurityError(
                        "durable source payloads differ from Git objects"
                    )
                for path, payload in persisted_files.items():
                    existing = files.get(path)
                    if existing is not None and not hmac.compare_digest(
                        existing,
                        payload,
                    ):
                        raise FormalSourceProvenanceSecurityError(
                            "source archive object path contains conflicting bytes"
                        )
                    files[path] = payload
                previous = record.commit_sha
            baseline_commit, _baseline_trees, baseline_files = self._baseline_objects(
                baseline.source_state
            )
            tree_objects, tree_files = _tree_object_closure(
                self._repository,
                root_tree_oids=[
                    baseline.source_state.head_tree_sha,
                    *(record.tree_sha for record in records),
                ],
                content_guard=self._content_guard,
            )
            required_object_files = {
                baseline_commit.archive_path: baseline_files[
                    baseline_commit.archive_path
                ],
                **tree_files,
            }
            for descriptor in (baseline_commit, *tree_objects):
                persisted = self._read_payload_descriptor(
                    assignment_id,
                    descriptor,
                )
                expected_payload = required_object_files[descriptor.archive_path]
                if not hmac.compare_digest(persisted, expected_payload):
                    raise FormalSourceProvenanceSecurityError(
                        "durable source tree evidence differs from Git"
                    )
                files[descriptor.archive_path] = persisted
            projection_path = self._projection_path(assignment_id)
            projection: DeveloperSourceProjectionManifest | None = None
            projection_blob_objects: list[ArchivedGitObject] | None = None
            promotion_intents: list[DeveloperSourcePromotionIntent] | None = None
            promotion_receipts: list[DeveloperSourcePromotionReceipt] | None = None
            activation_fences: list[DeveloperSourceActivationFence] | None = None
            promotion_adoptions: list[DeveloperSourcePromotionAdoption] | None = None
            reload_confirmations: list[DeveloperSourceReloadConfirmation] | None = None
            if projection_path.exists() or projection_path.is_symlink():
                projection = self.load_workspace_projection(assignment_id)
                projection_blob_objects, projection_blob_files = (
                    self._persist_projection_blobs(projection)
                )
                for descriptor in projection_blob_objects:
                    persisted = self._read_payload_descriptor(
                        assignment_id,
                        descriptor,
                    )
                    expected_payload = projection_blob_files[
                        descriptor.archive_path
                    ]
                    if not hmac.compare_digest(persisted, expected_payload):
                        raise FormalSourceProvenanceSecurityError(
                            "durable projection blob differs from Git"
                        )
                    existing = files.get(descriptor.archive_path)
                    if existing is not None and not hmac.compare_digest(
                        existing,
                        persisted,
                    ):
                        raise FormalSourceProvenanceSecurityError(
                            "projection blob conflicts with approved commit evidence"
                        )
                    files[descriptor.archive_path] = persisted
                promotion_receipts = self._load_promotion_receipts(assignment_id)
                promotion_intents = [
                    self._load_promotion_intent(
                        assignment_id,
                        receipt.response_id,
                    )
                    for receipt in promotion_receipts
                ]
                activation_fences = [
                    self._load_activation_fence(
                        assignment_id,
                        receipt.response_id,
                    )
                    for receipt in promotion_receipts
                ]
                promotion_adoptions = [
                    adoption
                    for receipt in promotion_receipts
                    for adoption in self._load_promotion_adoptions(
                        assignment_id,
                        receipt.response_id,
                    )
                ]
                reload_confirmations = []
                for receipt in promotion_receipts:
                    reload_root = self._promotion_reload_root(
                        assignment_id,
                        receipt.response_id,
                    )
                    if receipt.reload_status != "not_required":
                        receipt_confirmations = self._load_reload_confirmations(
                            assignment_id,
                            receipt.response_id,
                        )
                        if not receipt_confirmations:
                            raise FormalSourceProvenanceConflict(
                                "restart-required promotion has no reload confirmation"
                            )
                        reload_confirmations.extend(receipt_confirmations)
                    elif reload_root.exists() or reload_root.is_symlink():
                        raise FormalSourceProvenanceSecurityError(
                            "non-runtime promotion has an undeclared reload confirmation"
                        )
                if [
                    (receipt.response_id, receipt.commit_sha)
                    for receipt in promotion_receipts
                ] != [
                    (record.binding.response_id, record.commit_sha)
                    for record in records
                ]:
                    raise FormalSourceProvenanceConflict(
                        "source archive does not exactly match trusted promotions"
                    )
                if (
                    self._repository.symbolic_head() != projection.branch_ref
                    or self._repository.oid(projection.branch_ref, "commit") != previous
                    or self._repository.oid("HEAD", "commit") != previous
                ):
                    raise FormalSourceProvenanceConflict(
                        "activated source branch differs from approved promotion history"
                    )
                final_entries = {
                    entry.path: (entry.mode, entry.blob_sha)
                    for entry in _projected_tree_entries(
                        self._repository,
                        previous,
                    )
                }
                for path in {
                    item
                    for record in records
                    for item in record.changed_paths
                }:
                    endpoint = final_entries.get(path)
                    if (
                        self._worktree_endpoint(path) != endpoint
                        or self._index_endpoint(path) != endpoint
                    ):
                        raise FormalSourceProvenanceConflict(
                            "an activated source path drifted after its promotion"
                        )
                current = GitWorktreeState(
                    object_format=self._repository.object_format,
                    head_commit_sha=previous,
                    head_tree_sha=self._repository.oid(previous, "tree"),
                    status_digest=_digest(b""),
                    tracked_change_count=0,
                    untracked_file_count=0,
                    conflicted_path_count=0,
                    clean=True,
                )
            else:
                current = self._repository.state()
                if not current.clean or current.head_commit_sha != previous:
                    raise FormalSourceProvenanceConflict(
                        "developer source has dirty, untracked, or undeclared final drift"
                    )
                if self._repository.oid(previous, "tree") != current.head_tree_sha:
                    raise FormalSourceProvenanceConflict(
                        "developer source final tree differs from approved history"
                    )
            _require_exact_revision_range(
                self._repository,
                baseline=baseline.source_state.head_commit_sha,
                final=previous,
                expected=[record.commit_sha for record in records],
            )
            if sum(len(payload) for payload in files.values()) > MAX_ARCHIVE_PAYLOAD_BYTES:
                raise FormalSourceProvenanceSecurityError(
                    "source provenance archive exceeds its aggregate size limit"
                )
            payload = {
                "schema_version": "1.0",
                "task_id": baseline.task_id,
                "task_revision": baseline.task_revision,
                "run_id": baseline.run_id,
                "assignment_id": str(baseline.assignment_id),
                "channel_id": str(baseline.channel_id),
                "baseline": baseline.model_dump(mode="json"),
                "baseline_commit_object": baseline_commit.model_dump(mode="json"),
                "tree_objects": [
                    tree.model_dump(mode="json") for tree in tree_objects
                ],
                "approved_commits": [
                    record.model_dump(mode="json", exclude_none=True)
                    for record in records
                ],
                "final_source_state": current.model_dump(mode="json"),
                "finalized_at": _utc(finalized_at).isoformat().replace(
                    "+00:00",
                    "Z",
                ),
            }
            if (
                projection is not None
                and projection_blob_objects is not None
                and promotion_intents is not None
                and promotion_receipts is not None
                and activation_fences is not None
                and promotion_adoptions is not None
                and reload_confirmations is not None
            ):
                payload["developer_projection"] = projection.model_dump(
                    mode="json"
                )
                payload["projection_blob_objects"] = [
                    blob.model_dump(mode="json")
                    for blob in projection_blob_objects
                ]
                payload["promotion_intents"] = [
                    intent.model_dump(mode="json")
                    for intent in promotion_intents
                ]
                payload["promotion_receipts"] = [
                    receipt.model_dump(mode="json")
                    for receipt in promotion_receipts
                ]
                payload["activation_fences"] = [
                    fence.model_dump(mode="json")
                    for fence in activation_fences
                ]
                payload["promotion_adoptions"] = [
                    adoption.model_dump(mode="json")
                    for adoption in promotion_adoptions
                ]
                payload["reload_confirmations"] = [
                    confirmation.model_dump(mode="json")
                    for confirmation in reload_confirmations
                ]
            manifest = FormalSourceProvenanceManifest(
                **payload,
                manifest_digest=_digest(payload),
            )
            files[SOURCE_PROVENANCE_MANIFEST_PATH] = _canonical_json(manifest)
            return FormalSourceProvenanceArchive(
                manifest=manifest,
                files=files,
            )


def _require_exact_revision_range(
    repository: _GitRepository,
    *,
    baseline: str,
    final: str,
    expected: Sequence[str],
) -> None:
    if baseline == final:
        actual: list[str] = []
    else:
        payload = repository.run(
            ["rev-list", "--reverse", f"{baseline}..{final}", "--"],
            limit=4 * 1024 * 1024,
        )
        try:
            actual = [
                line
                for line in payload.decode("ascii").splitlines()
                if line
            ]
        except UnicodeDecodeError as error:
            raise FormalSourceProvenanceSecurityError(
                "Git revision range contains an invalid object ID"
            ) from error
    if actual != list(expected):
        raise FormalSourceProvenanceConflict(
            "developer source history contains an undeclared commit"
        )


def _source_manifest_from_archive(
    *,
    archive_files: Mapping[str, bytes],
    expected_assignment_id: UUID,
    expected_bindings: Sequence[ApprovedDeveloperResponseBinding],
    expected_manifest_digest: str | None,
) -> FormalSourceProvenanceManifest:
    manifest_payload = archive_files.get(SOURCE_PROVENANCE_MANIFEST_PATH)
    if manifest_payload is None:
        raise FormalSourceProvenanceConflict(
            "source provenance archive has no manifest"
        )
    manifest = FormalSourceProvenanceManifest.model_validate(
        _strict_json(manifest_payload)
    )
    if not hmac.compare_digest(manifest_payload, _canonical_json(manifest)):
        raise FormalSourceProvenanceSecurityError(
            "source provenance manifest is not canonical"
        )
    if expected_manifest_digest is not None and not hmac.compare_digest(
        manifest.manifest_digest,
        expected_manifest_digest,
    ):
        raise FormalSourceProvenanceConflict(
            "source provenance manifest differs from its external binding"
        )
    if (
        manifest.assignment_id != expected_assignment_id
        or [commit.binding for commit in manifest.approved_commits]
        != list(expected_bindings)
    ):
        raise FormalSourceProvenanceConflict(
            "source provenance archive is not bound to the approved formal responses"
        )
    return manifest


def _source_archive_descriptors(
    manifest: FormalSourceProvenanceManifest,
) -> dict[str, ArchivedGitObject | ArchivedBinaryDiff]:
    descriptors: dict[str, ArchivedGitObject | ArchivedBinaryDiff] = {}
    all_descriptors: list[ArchivedGitObject | ArchivedBinaryDiff] = [
        manifest.baseline_commit_object,
        *manifest.tree_objects,
        *(manifest.projection_blob_objects or []),
    ]
    for commit in manifest.approved_commits:
        all_descriptors.extend(
            (
                commit.commit_object,
                *commit.blob_objects,
                commit.binary_diff,
            )
        )
    for descriptor in all_descriptors:
        existing = descriptors.get(descriptor.archive_path)
        if existing is not None and existing != descriptor:
            raise FormalSourceProvenanceSecurityError(
                "source provenance archive path has conflicting descriptors"
            )
        descriptors[descriptor.archive_path] = descriptor
    return descriptors


def _offline_tree_snapshots(
    *,
    tree_entries: Mapping[str, Sequence[_RawTreeEntry]],
    root_tree_oids: Sequence[str],
) -> tuple[dict[str, dict[str, tuple[str, str, str]]], set[str]]:
    snapshots: dict[str, dict[str, tuple[str, str, str]]] = {}
    reachable: set[str] = set()
    for root_oid in dict.fromkeys(root_tree_oids):
        flattened: dict[str, tuple[str, str, str]] = {}
        pending: list[tuple[str, str, tuple[str, ...]]] = [
            (root_oid, "", ())
        ]
        while pending:
            tree_oid, prefix, ancestors = pending.pop()
            if tree_oid in ancestors:
                raise FormalSourceProvenanceSecurityError(
                    "archived Git tree graph contains a cycle"
                )
            entries = tree_entries.get(tree_oid)
            if entries is None:
                raise FormalSourceProvenanceSecurityError(
                    "source provenance archive omits a reachable Git tree"
                )
            reachable.add(tree_oid)
            next_ancestors = (*ancestors, tree_oid)
            for entry in reversed(entries):
                full_path = f"{prefix}/{entry.name}" if prefix else entry.name
                try:
                    normalized = _safe_repository_path(full_path)
                except ValueError as error:
                    raise FormalSourceProvenanceSecurityError(
                        "archived Git tree contains an unsafe repository path"
                    ) from error
                if entry.object_type == "tree":
                    pending.append((entry.oid, normalized, next_ancestors))
                    continue
                if normalized in flattened:
                    raise FormalSourceProvenanceSecurityError(
                        "archived Git tree resolves duplicate source paths"
                    )
                flattened[normalized] = (
                    entry.mode,
                    entry.oid,
                    entry.object_type,
                )
        snapshots[root_oid] = flattened
    return snapshots, reachable


def _offline_expected_changes(
    *,
    parent: Mapping[str, tuple[str, str, str]],
    current: Mapping[str, tuple[str, str, str]],
) -> dict[str, GitPathChange]:
    changes: dict[str, GitPathChange] = {}
    for path in sorted(set(parent) | set(current)):
        old = parent.get(path)
        new = current.get(path)
        if old == new:
            continue
        for endpoint in (old, new):
            if endpoint is None:
                continue
            mode, _oid, object_type = endpoint
            if object_type != "blob" or mode not in _ALLOWED_FILE_MODES:
                raise FormalSourceProvenanceSecurityError(
                    "approved source tree changes a non-regular source object"
                )
        if old is None:
            assert new is not None
            changes[path] = GitPathChange(
                path=path,
                change_kind="added",
                new_mode=new[0],
                new_blob_sha=new[1],
            )
        elif new is None:
            changes[path] = GitPathChange(
                path=path,
                change_kind="deleted",
                old_mode=old[0],
                old_blob_sha=old[1],
            )
        else:
            changes[path] = GitPathChange(
                path=path,
                change_kind="modified",
                old_mode=old[0],
                old_blob_sha=old[1],
                new_mode=new[0],
                new_blob_sha=new[1],
            )
    return changes


def verify_source_provenance_archive_offline(
    *,
    archive_files: Mapping[str, bytes],
    expected_assignment_id: UUID,
    expected_bindings: Sequence[ApprovedDeveloperResponseBinding],
    expected_manifest_digest: str | None = None,
    content_guard: ContentGuard | None = None,
) -> FormalSourceProvenanceManifest:
    """Verify the complete source archive without a checkout or Git metadata."""

    manifest = _source_manifest_from_archive(
        archive_files=archive_files,
        expected_assignment_id=expected_assignment_id,
        expected_bindings=expected_bindings,
        expected_manifest_digest=expected_manifest_digest,
    )
    descriptors = _source_archive_descriptors(manifest)
    expected_paths = {
        SOURCE_PROVENANCE_MANIFEST_PATH,
        *descriptors,
    }
    if set(archive_files) != expected_paths:
        raise FormalSourceProvenanceSecurityError(
            "source provenance archive contains missing or undeclared payloads"
        )
    object_format = manifest.baseline.source_state.object_format
    object_payloads: dict[tuple[str, str], bytes] = {}
    for path, descriptor in descriptors.items():
        payload = archive_files[path]
        if (
            len(payload) != descriptor.size_bytes
            or not hmac.compare_digest(
                _digest(payload),
                descriptor.payload_digest,
            )
        ):
            raise FormalSourceProvenanceSecurityError(
                "source provenance payload differs from its descriptor"
            )
        _guard_payload(content_guard, label=path, payload=payload)
        if isinstance(descriptor, ArchivedBinaryDiff):
            if not payload:
                raise FormalSourceProvenanceSecurityError(
                    "source provenance binary diff is empty"
                )
            continue
        actual_oid = _object_oid(
            object_format,
            descriptor.object_type,
            payload,
        )
        if not hmac.compare_digest(actual_oid, descriptor.oid):
            raise FormalSourceProvenanceSecurityError(
                "archived Git object does not hash to its declared identity"
            )
        key = (descriptor.object_type, descriptor.oid)
        existing = object_payloads.get(key)
        if existing is not None and not hmac.compare_digest(existing, payload):
            raise FormalSourceProvenanceSecurityError(
                "archived Git object identity has conflicting bytes"
            )
        object_payloads[key] = payload

    baseline_payload = object_payloads.get(
        ("commit", manifest.baseline.source_state.head_commit_sha)
    )
    if baseline_payload is None:
        raise FormalSourceProvenanceSecurityError(
            "source provenance archive omits the baseline commit object"
        )
    baseline_tree, _baseline_parents = _parse_commit_headers(baseline_payload)
    if baseline_tree != manifest.baseline.source_state.head_tree_sha:
        raise FormalSourceProvenanceSecurityError(
            "archived baseline commit points to another source tree"
        )
    previous_commit = manifest.baseline.source_state.head_commit_sha
    previous_tree = manifest.baseline.source_state.head_tree_sha
    for record in manifest.approved_commits:
        commit_payload = object_payloads.get(("commit", record.commit_sha))
        if commit_payload is None:
            raise FormalSourceProvenanceSecurityError(
                "source provenance archive omits an approved commit object"
            )
        tree_oid, parents = _parse_commit_headers(commit_payload)
        if tree_oid != record.tree_sha or parents != [previous_commit]:
            raise FormalSourceProvenanceSecurityError(
                "approved commit object differs from its tree or direct parent"
            )
        if record.parent_commit_sha != previous_commit:
            raise FormalSourceProvenanceConflict(
                "approved source commit chain contains an undeclared parent"
            )
        previous_commit = record.commit_sha
        previous_tree = record.tree_sha
    if (
        previous_commit != manifest.final_source_state.head_commit_sha
        or previous_tree != manifest.final_source_state.head_tree_sha
    ):
        raise FormalSourceProvenanceConflict(
            "offline source commit chain differs from the final source state"
        )

    tree_entries: dict[str, list[_RawTreeEntry]] = {}
    for descriptor in manifest.tree_objects:
        payload = object_payloads.get(("tree", descriptor.oid))
        if payload is None:
            raise FormalSourceProvenanceSecurityError(
                "source provenance tree descriptor has no object payload"
            )
        tree_entries[descriptor.oid] = _parse_raw_tree(
            payload,
            object_format=object_format,
        )
    root_trees = [
        manifest.baseline.source_state.head_tree_sha,
        *(record.tree_sha for record in manifest.approved_commits),
    ]
    snapshots, reachable_trees = _offline_tree_snapshots(
        tree_entries=tree_entries,
        root_tree_oids=root_trees,
    )
    if reachable_trees != set(tree_entries):
        raise FormalSourceProvenanceSecurityError(
            "source provenance archive contains an unreachable Git tree object"
        )

    if manifest.developer_projection is not None:
        projection = manifest.developer_projection
        baseline_snapshot = snapshots[
            manifest.baseline.source_state.head_tree_sha
        ]
        if any(
            _is_projected_source_path(path)
            and (
                object_type != "blob"
                or mode not in _ALLOWED_FILE_MODES
            )
            for path, (mode, _oid, object_type) in baseline_snapshot.items()
        ):
            raise FormalSourceProvenanceSecurityError(
                "baseline projection contains a special Git object"
            )
        expected_projection = {
            path: (mode, oid)
            for path, (mode, oid, object_type) in baseline_snapshot.items()
            if _is_projected_source_path(path)
            and object_type == "blob"
            and mode in _ALLOWED_FILE_MODES
        }
        actual_projection = {
            entry.path: (entry.mode, entry.blob_sha)
            for entry in projection.entries
        }
        if actual_projection != expected_projection:
            raise FormalSourceProvenanceSecurityError(
                "developer projection differs from the filtered baseline tree"
            )
        projection_descriptors = {
            descriptor.oid: descriptor
            for descriptor in (manifest.projection_blob_objects or [])
        }
        for entry in projection.entries:
            descriptor = projection_descriptors.get(entry.blob_sha)
            payload = object_payloads.get(("blob", entry.blob_sha))
            if (
                descriptor is None
                or payload is None
                or descriptor.payload_digest != entry.digest
                or descriptor.size_bytes != entry.size_bytes
                or len(payload) != entry.size_bytes
                or not hmac.compare_digest(_digest(payload), entry.digest)
            ):
                raise FormalSourceProvenanceSecurityError(
                    "developer projection blob is not independently replayable"
                )

    parent_tree = manifest.baseline.source_state.head_tree_sha
    for record in manifest.approved_commits:
        expected_changes = _offline_expected_changes(
            parent=snapshots[parent_tree],
            current=snapshots[record.tree_sha],
        )
        declared_changes = {change.path: change for change in record.changes}
        if (
            set(expected_changes) != set(record.changed_paths)
            or declared_changes != expected_changes
        ):
            raise FormalSourceProvenanceSecurityError(
                "approved commit changed paths or blob endpoints differ from its trees"
            )
        for blob in record.blob_objects:
            if ("blob", blob.oid) not in object_payloads:
                raise FormalSourceProvenanceSecurityError(
                    "approved source endpoint omits its raw blob object"
                )
        parent_tree = record.tree_sha
    return manifest


def verify_source_provenance_archive(
    *,
    repository_root: Path,
    archive_files: Mapping[str, bytes],
    expected_assignment_id: UUID,
    expected_bindings: Sequence[ApprovedDeveloperResponseBinding],
    require_current_checkout: bool,
    content_guard: ContentGuard | None = None,
) -> FormalSourceProvenanceManifest:
    """Read-only verification against archived bytes and real Git objects."""

    repository = _GitRepository(repository_root)
    manifest = verify_source_provenance_archive_offline(
        archive_files=archive_files,
        expected_assignment_id=expected_assignment_id,
        expected_bindings=expected_bindings,
        content_guard=content_guard,
    )
    previous = manifest.baseline.source_state.head_commit_sha
    if repository.oid(previous, "commit") != previous or repository.oid(
        previous, "tree"
    ) != manifest.baseline.source_state.head_tree_sha:
        raise FormalSourceProvenanceConflict(
            "source provenance baseline no longer resolves to its Git tree"
        )
    for record in manifest.approved_commits:
        reconstructed, files = _commit_provenance(
            repository,
            order=record.order,
            parent_commit_sha=previous,
            binding=record.binding,
            content_guard=content_guard,
        )
        if reconstructed != record:
            raise FormalSourceProvenanceSecurityError(
                "archived developer commit metadata differs from Git"
            )
        for path, expected_payload in files.items():
            actual_payload = archive_files.get(path)
            if actual_payload is None or not hmac.compare_digest(
                actual_payload,
                expected_payload,
            ):
                raise FormalSourceProvenanceSecurityError(
                    "source provenance payload is missing or differs from Git"
                )
            _guard_payload(content_guard, label=path, payload=actual_payload)
        previous = record.commit_sha
    _require_exact_revision_range(
        repository,
        baseline=manifest.baseline.source_state.head_commit_sha,
        final=previous,
        expected=[record.commit_sha for record in manifest.approved_commits],
    )
    if require_current_checkout:
        current = repository.state()
        if current != manifest.final_source_state:
            raise FormalSourceProvenanceConflict(
                "current developer source differs from archived final state"
            )
    return manifest
