from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .collaboration_models import (
    ActorId,
    CollaborationChannel,
    CollaborationReport,
    DeveloperLease,
    DeveloperWorkspaceBinding,
)
from .lilies_models import Digest, IdempotencyKey, OpaqueReference
from .task_packages import (
    WORKSPACE_MANIFEST_FILE,
    WORKSPACE_POLICY_FILE,
    WorkspaceMountManifest,
    WorkspaceRole,
)


class FormalDeveloperWorkerError(RuntimeError):
    """A formal developer process could not run inside its trusted boundary."""


class FormalDeveloperWorkerConflict(FormalDeveloperWorkerError):
    """A durable worker identity was reused with different input."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


_WorkerArgument = Annotated[
    str,
    StringConstraints(min_length=1, max_length=32_768),
]


class DeveloperWorkerRunRequest(_FrozenModel):
    """Caller-controlled arguments for the platform-owned developer runtime.

    The executable, environment, filesystem grants, and sandbox profile are
    deliberately absent.  They are selected by the trusted broker.
    """

    idempotency_key: IdempotencyKey
    lease_id: UUID
    lease_owner_id: ActorId
    expected_report_revision: int = Field(ge=1)
    response_id: UUID
    arguments: list[_WorkerArgument] = Field(min_length=1, max_length=256)
    timeout_seconds: int = Field(default=600, ge=1, le=900)

    @field_validator("arguments")
    @classmethod
    def arguments_are_process_safe(cls, value: list[str]) -> list[str]:
        if any("\x00" in argument for argument in value):
            raise ValueError("developer worker arguments cannot contain NUL")
        if sum(len(argument.encode("utf-8")) for argument in value) > 256 * 1024:
            raise ValueError("developer worker arguments exceed the bounded request size")
        return value


class DeveloperWorkerReceipt(_FrozenModel):
    """Digest-bound evidence emitted only by the platform-owned OS broker."""

    schema_version: Literal["1.0"] = "1.0"
    receipt_id: UUID
    assignment_id: UUID
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    lease_id: UUID
    lease_owner_id: ActorId
    response_id: UUID
    task_id: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    task_revision: int = Field(ge=1)
    run_id: OpaqueReference
    workspace_manifest_digest: Digest
    workspace_policy_digest: Digest
    source_manifest_digest: Digest | None = None
    request_digest: Digest
    runtime_executable_digest: Digest
    runtime_boundary_digest: Digest
    arguments_digest: Digest
    environment_digest: Digest
    sandbox_profile_digest: Digest
    workspace_before_digest: Digest
    workspace_after_digest: Digest
    writable_prefixes: list[Literal["source", "work"]] = Field(
        min_length=2,
        max_length=2,
    )
    sandboxed: Literal[True] = True
    network_access: Literal["denied"] = "denied"
    inherited_environment: Literal["none"] = "none"
    started_at: datetime
    finished_at: datetime
    worker_pid: int = Field(ge=1)
    exit_code: int | None = None
    timed_out: bool
    stdout_digest: Digest
    stdout_bytes: int = Field(ge=0)
    stderr_digest: Digest
    stderr_bytes: int = Field(ge=0)
    boundary_intact: bool
    receipt_digest: Digest

    @field_validator("started_at", "finished_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("developer worker timestamps require UTC")
        if value.utcoffset().total_seconds() != 0:
            raise ValueError("developer worker timestamps must use UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def receipt_is_consistent_and_digest_bound(self) -> DeveloperWorkerReceipt:
        if self.finished_at < self.started_at:
            raise ValueError("developer worker finish precedes its start")
        if self.timed_out != (self.exit_code is None):
            raise ValueError("developer worker timeout and exit status disagree")
        if self.writable_prefixes != ["source", "work"]:
            raise ValueError("developer worker writable boundary changed")
        expected = _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"receipt_digest"},
                )
            )
        )
        if not hmac.compare_digest(expected, self.receipt_digest):
            raise ValueError("developer worker receipt digest does not match")
        return self

    @property
    def successful(self) -> bool:
        return not self.timed_out and self.exit_code == 0 and self.boundary_intact


_REQUIRED_DENIED_SEGMENTS = frozenset(
    {
        ".git",
        "protected",
        "oracle",
        "platform-data",
        "platform_data",
    }
)
_FORBIDDEN_ENVIRONMENT_IDENTITIES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "developer_token",
        "lilies_collaboration_developer_token",
        "lilies_platform_token",
        "password",
        "platform_token",
        "secret",
        "token",
    }
)
_MAX_WORKSPACE_FILES = 100_000
_MAX_WORKSPACE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
_WORKER_RUNTIME_RELATIVE_ROOTS = (
    PurePosixPath("work/.developer-worker-home"),
    PurePosixPath("work/.developer-worker-tmp"),
)


def _canonical_json(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc).isoformat()
        return normalized.replace("+00:00", "Z")
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _digest_file(path: Path, *, limit: int = 512 * 1024 * 1024) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FormalDeveloperWorkerError(
            "developer worker runtime file is not safely readable"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise FormalDeveloperWorkerError(
                "developer worker runtime file is not a single-link regular file"
            )
        if metadata.st_size > limit:
            raise FormalDeveloperWorkerError(
                "developer worker runtime file exceeds its trust limit"
            )
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise FormalDeveloperWorkerError(
                    "developer worker runtime file exceeds its trust limit"
                )
            digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    finally:
        os.close(descriptor)


def _read_regular(path: Path, *, limit: int = 16 * 1024 * 1024) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FormalDeveloperWorkerError(
            "developer worker boundary file is not safely readable"
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_mode & 0o022:
            raise FormalDeveloperWorkerError("developer worker boundary file has unsafe metadata")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1)):
            total += len(chunk)
            if total > limit:
                raise FormalDeveloperWorkerError("developer worker boundary file exceeds its limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise FormalDeveloperWorkerError(
                "developer worker boundary file changed while being read"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o400) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _sandbox_rule(path: Path) -> str:
    resolved = path.resolve(strict=True)
    operation = "subpath" if resolved.is_dir() else "literal"
    return f"({operation} {json.dumps(str(resolved))})"


def _sandbox_profile(
    *,
    read_roots: Iterable[Path],
    executable_roots: Iterable[Path],
    writable_roots: Iterable[Path],
) -> str:
    read_rules = list(dict.fromkeys(_sandbox_rule(path) for path in read_roots))
    executable_rules = list(dict.fromkeys(_sandbox_rule(path) for path in executable_roots))
    writable_rules = list(dict.fromkeys(_sandbox_rule(path) for path in writable_roots))
    lines = [
        "(version 1)",
        '(import "system.sb")',
        "(deny default)",
        "(allow process-fork)",
        "(allow process-info* (target self))",
    ]
    lines.extend(f"(allow process-exec {rule})" for rule in executable_rules)
    lines.extend(f"(allow file-read* {rule})" for rule in read_rules)
    lines.extend(f"(allow file-read-metadata {rule})" for rule in read_rules)
    lines.extend(f"(allow file-map-executable {rule})" for rule in read_rules)
    lines.extend(("(allow sysctl-read)",))
    lines.extend(f"(allow file-write* {rule})" for rule in writable_rules)
    lines.append("(deny network*)")
    return "\n".join(lines)


def _safe_environment(
    *,
    workspace: Path,
    executable_roots: Sequence[Path],
) -> dict[str, str]:
    work = workspace / "work"
    home = work / ".developer-worker-home"
    temporary = work / ".developer-worker-tmp"
    for path in (home, temporary):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    path_entries = list(
        dict.fromkeys(
            str(path if path.is_dir() else path.parent)
            for raw in executable_roots
            if (path := Path(raw).resolve(strict=True))
        )
    )
    environment = {
        "HOME": str(home.resolve()),
        "LANG": "C",
        "LC_ALL": "C",
        "NO_COLOR": "1",
        "PATH": os.pathsep.join(path_entries),
        "TMPDIR": str(temporary.resolve()),
    }
    for key, value in environment.items():
        identity = key.casefold()
        if any(marker in identity for marker in _FORBIDDEN_ENVIRONMENT_IDENTITIES):
            raise FormalDeveloperWorkerError(
                "developer worker environment contains a credential-shaped key"
            )
        if "\x00" in value:
            raise FormalDeveloperWorkerError(
                "developer worker environment contains an invalid value"
            )
    return environment


def _workspace_tree_digest(
    root: Path,
    *,
    excluded_relative_roots: Sequence[PurePosixPath] = (),
) -> str:
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        relative_path = PurePosixPath(relative)
        if any(
            relative_path == excluded or excluded in relative_path.parents
            for excluded in excluded_relative_roots
        ):
            continue
        parts = relative_path.parts
        if any(part.casefold() in _REQUIRED_DENIED_SEGMENTS for part in parts):
            raise FormalDeveloperWorkerError("developer worker workspace contains a forbidden path")
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise FormalDeveloperWorkerError(
                "developer worker workspace cannot contain symbolic links"
            )
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(
                _canonical_json(
                    {
                        "kind": "directory",
                        "mode": stat.S_IMODE(metadata.st_mode),
                        "path": relative,
                    }
                )
            )
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise FormalDeveloperWorkerError(
                "developer worker workspace requires single-link regular files"
            )
        files += 1
        total_bytes += metadata.st_size
        if files > _MAX_WORKSPACE_FILES or total_bytes > _MAX_WORKSPACE_BYTES:
            raise FormalDeveloperWorkerError(
                "developer worker workspace exceeds its digest boundary"
            )
        digest.update(
            _canonical_json(
                {
                    "kind": "file",
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "path": relative,
                    "size": metadata.st_size,
                }
            )
        )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
        finally:
            os.close(descriptor)
    return f"sha256:{digest.hexdigest()}"


def _validate_workspace(
    binding: DeveloperWorkspaceBinding,
) -> tuple[Path, WorkspaceMountManifest, list[Path]]:
    workspace = Path(binding.path)
    if workspace.is_symlink() or not workspace.is_dir():
        raise FormalDeveloperWorkerError("developer worker workspace is not a real directory")
    workspace = workspace.resolve(strict=True)
    if stat.S_IMODE(workspace.stat(follow_symlinks=False).st_mode) != 0o500:
        raise FormalDeveloperWorkerError("developer worker workspace root is not read-only")
    manifest_payload = _read_regular(workspace / WORKSPACE_MANIFEST_FILE)
    policy_payload = _read_regular(workspace / WORKSPACE_POLICY_FILE)
    if not hmac.compare_digest(_digest(manifest_payload), binding.manifest_digest):
        raise FormalDeveloperWorkerError("developer worker workspace manifest changed")
    if not hmac.compare_digest(_digest(policy_payload), binding.policy_digest):
        raise FormalDeveloperWorkerError("developer worker workspace policy changed")
    try:
        manifest = WorkspaceMountManifest.model_validate_json(manifest_payload)
        policy = json.loads(policy_payload)
    except Exception as error:
        raise FormalDeveloperWorkerError(
            "developer worker workspace controls are invalid"
        ) from error
    if (
        manifest.role is not WorkspaceRole.developer
        or manifest.task_id != binding.task_id
        or manifest.revision != binding.task_revision
        or manifest.run_id != binding.run_id
        or manifest.assignment_id != binding.assignment_id
        or manifest.writable_prefixes != ["source", "work"]
    ):
        raise FormalDeveloperWorkerError(
            "developer worker workspace controls have another formal binding"
        )
    if not isinstance(policy, dict):
        raise FormalDeveloperWorkerError("developer worker workspace policy is not fail closed")
    denied = {
        str(item).casefold() for item in policy.get("denied_segments", []) if isinstance(item, str)
    }
    if (
        policy.get("schema_version") != "1.0"
        or policy.get("writable_prefixes") != ["source", "work"]
        or not _REQUIRED_DENIED_SEGMENTS.issubset(denied)
    ):
        raise FormalDeveloperWorkerError("developer worker workspace policy is not fail closed")
    writable = [workspace / "source", workspace / "work"]
    if any(
        path.is_symlink()
        or not path.is_dir()
        or stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != 0o700
        or workspace not in path.resolve(strict=True).parents
        for path in writable
    ):
        raise FormalDeveloperWorkerError("developer worker writable prefix is unsafe")
    return workspace, manifest, writable


def _bounded_capture(value: bytes | None) -> tuple[bytes, bool]:
    payload = value or b""
    return payload[:_MAX_CAPTURE_BYTES], len(payload) <= _MAX_CAPTURE_BYTES


class FormalDeveloperWorkerBroker:
    """Launch one fixed developer runtime through a deny-by-default OS sandbox."""

    def __init__(
        self,
        *,
        state_root: Path,
        runtime_executable: Path,
        runtime_read_roots: Iterable[Path] = (),
        runtime_executable_roots: Iterable[Path] = (),
    ) -> None:
        raw_state_root = Path(state_root)
        if raw_state_root.is_symlink():
            raise FormalDeveloperWorkerError(
                "developer worker state root cannot be a symbolic link"
            )
        self._state_root = raw_state_root.resolve(strict=False)
        self._state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._state_root.is_symlink() or not self._state_root.is_dir():
            raise FormalDeveloperWorkerError(
                "developer worker state root is not a private directory"
            )
        os.chmod(self._state_root, 0o700)
        self._receipts_root = self._state_root / "receipts"
        self._runs_root = self._state_root / "runs"
        for path in (self._receipts_root, self._runs_root):
            if path.is_symlink():
                raise FormalDeveloperWorkerError(
                    "developer worker state directory cannot be a symbolic link"
                )
            path.mkdir(mode=0o700, exist_ok=True)
            if not path.is_dir():
                raise FormalDeveloperWorkerError("developer worker state directory is invalid")
            os.chmod(path, 0o700)
        self._lock_path = self._state_root / ".worker.lock"
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_descriptor = os.open(self._lock_path, lock_flags, 0o600)
        except OSError as error:
            raise FormalDeveloperWorkerError("developer worker lock file is unsafe") from error
        try:
            lock_metadata = os.fstat(lock_descriptor)
            if not stat.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_nlink != 1:
                raise FormalDeveloperWorkerError("developer worker lock file is unsafe")
        finally:
            os.close(lock_descriptor)
        os.chmod(self._lock_path, 0o600)
        executable = Path(runtime_executable).resolve(strict=True)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FormalDeveloperWorkerError("developer worker runtime is not an executable file")
        self._runtime_executable = executable
        self._runtime_executable_digest = _digest_file(executable)
        system_runtime_roots = (
            Path("/System/Library"),
            Path("/usr/lib"),
            Path("/private/var/db/dyld"),
            Path("/private/etc/ssl"),
            Path("/etc/ssl"),
            Path("/dev/null"),
            Path("/dev/urandom"),
        )
        self._runtime_read_roots = tuple(
            dict.fromkeys(
                path.resolve(strict=True)
                for raw in (
                    executable,
                    *runtime_read_roots,
                    *runtime_executable_roots,
                    *system_runtime_roots,
                )
                if (path := Path(raw)).exists()
            )
        )
        self._runtime_executable_roots = tuple(
            dict.fromkeys(
                path.resolve(strict=True)
                for raw in (executable, *runtime_executable_roots)
                if (path := Path(raw)).exists()
            )
        )
        if any(
            root == self._state_root
            or root in self._state_root.parents
            or self._state_root in root.parents
            for root in self._runtime_read_roots
        ):
            raise FormalDeveloperWorkerError(
                "developer worker receipt state overlaps its runtime read boundary"
            )
        self._runtime_boundary_digest = _digest(
            _canonical_json(
                {
                    "read_roots": [str(path) for path in self._runtime_read_roots],
                    "executable_roots": [str(path) for path in self._runtime_executable_roots],
                }
            )
        )

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._lock_path, flags)
        except OSError as error:
            raise FormalDeveloperWorkerError("developer worker lock file is unavailable") from error
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise FormalDeveloperWorkerError("developer worker lock file changed")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _receipt_id(
        self,
        *,
        channel: CollaborationChannel,
        report: CollaborationReport,
        request: DeveloperWorkerRunRequest,
    ) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            "lilies:formal-developer-worker:"
            f"{channel.assignment_id}:{report.report_id}:"
            f"{request.lease_id}:{request.lease_owner_id}:"
            f"{request.idempotency_key}",
        )

    def _receipt_path(self, receipt_id: UUID) -> Path:
        return self._receipts_root / f"{receipt_id}.json"

    def _run_path(self, receipt_id: UUID) -> Path:
        return self._runs_root / f"{receipt_id}.json"

    @staticmethod
    def _sandbox_executable() -> Path:
        sandbox = shutil.which("sandbox-exec")
        if sys.platform != "darwin" or sandbox is None:
            raise FormalDeveloperWorkerError(
                "macOS sandbox-exec is required for the formal developer worker"
            )
        sandbox_path = Path(sandbox).resolve(strict=True)
        if sandbox_path != Path("/usr/bin/sandbox-exec"):
            raise FormalDeveloperWorkerError(
                "formal developer worker resolved an untrusted sandbox executable"
            )
        return sandbox_path

    def _request_digest(
        self,
        *,
        channel: CollaborationChannel,
        report: CollaborationReport,
        lease: DeveloperLease,
        workspace: DeveloperWorkspaceBinding,
        request: DeveloperWorkerRunRequest,
    ) -> str:
        return _digest(
            _canonical_json(
                {
                    "channel_id": str(channel.channel_id),
                    "assignment_id": str(channel.assignment_id),
                    "report_id": str(report.report_id),
                    "report_revision": report.revision,
                    "lease_id": str(lease.lease_id),
                    "lease_owner_id": lease.owner_id,
                    "workspace": workspace.model_dump(
                        mode="json",
                        exclude={"path"},
                        exclude_none=False,
                    ),
                    "request": request.model_dump(
                        mode="json",
                        exclude_none=False,
                    ),
                    "runtime_executable_digest": self._runtime_executable_digest,
                    "runtime_boundary_digest": self._runtime_boundary_digest,
                }
            )
        )

    def _load_receipt(self, receipt_id: UUID) -> DeveloperWorkerReceipt:
        path = self._receipt_path(receipt_id)
        try:
            payload = _read_regular(path)
            if payload != _canonical_json(json.loads(payload)):
                raise ValueError("receipt is not canonical")
            return DeveloperWorkerReceipt.model_validate_json(payload)
        except Exception as error:
            raise FormalDeveloperWorkerError(
                "developer worker receipt is unavailable or untrusted"
            ) from error

    def run(
        self,
        *,
        channel: CollaborationChannel,
        report: CollaborationReport,
        lease: DeveloperLease,
        workspace: DeveloperWorkspaceBinding,
        request: DeveloperWorkerRunRequest | Mapping[str, Any],
    ) -> DeveloperWorkerReceipt:
        parsed = DeveloperWorkerRunRequest.model_validate(request)
        if (
            channel.assignment_id != workspace.assignment_id
            or channel.channel_id != report.channel_id
            or channel.task_id != workspace.task_id
            or channel.task_revision != workspace.task_revision
            or report.report_id != lease.report_id
            or report.revision != lease.report_revision
            or report.revision != parsed.expected_report_revision
            or lease.lease_id != parsed.lease_id
            or lease.owner_id != parsed.lease_owner_id
        ):
            raise FormalDeveloperWorkerError(
                "developer worker request differs from its channel or lease"
            )
        workspace_path, _manifest, writable_roots = _validate_workspace(workspace)
        sandbox_path = self._sandbox_executable()
        if (
            self._state_root == workspace_path
            or self._state_root in workspace_path.parents
            or workspace_path in self._state_root.parents
        ):
            raise FormalDeveloperWorkerError(
                "developer worker receipt state overlaps its readable workspace"
            )
        receipt_id = self._receipt_id(
            channel=channel,
            report=report,
            request=parsed,
        )
        request_digest = self._request_digest(
            channel=channel,
            report=report,
            lease=lease,
            workspace=workspace,
            request=parsed,
        )
        with self._exclusive_lock():
            receipt_path = self._receipt_path(receipt_id)
            if receipt_path.exists():
                receipt = self._load_receipt(receipt_id)
                if not hmac.compare_digest(receipt.request_digest, request_digest):
                    raise FormalDeveloperWorkerConflict(
                        "developer worker idempotency key was reused with another request"
                    )
                return receipt
            run_path = self._run_path(receipt_id)
            if run_path.exists():
                try:
                    run_record = json.loads(_read_regular(run_path))
                except Exception as error:
                    raise FormalDeveloperWorkerError(
                        "developer worker durable run intent is invalid"
                    ) from error
                if not hmac.compare_digest(
                    str(run_record.get("request_digest", "")),
                    request_digest,
                ):
                    raise FormalDeveloperWorkerConflict(
                        "developer worker idempotency key was reused with another request"
                    )
                raise FormalDeveloperWorkerConflict(
                    "developer worker has an indeterminate prior execution"
                )
            _atomic_write(
                run_path,
                _canonical_json(
                    {
                        "schema_version": "1.0",
                        "receipt_id": str(receipt_id),
                        "request_digest": request_digest,
                        "state": "running",
                    }
                ),
            )
            return self._execute(
                channel=channel,
                report=report,
                lease=lease,
                workspace=workspace,
                workspace_path=workspace_path,
                writable_roots=writable_roots,
                request=parsed,
                request_digest=request_digest,
                receipt_id=receipt_id,
                run_path=run_path,
                sandbox_path=sandbox_path,
            )

    def _execute(
        self,
        *,
        channel: CollaborationChannel,
        report: CollaborationReport,
        lease: DeveloperLease,
        workspace: DeveloperWorkspaceBinding,
        workspace_path: Path,
        writable_roots: Sequence[Path],
        request: DeveloperWorkerRunRequest,
        request_digest: str,
        receipt_id: UUID,
        run_path: Path,
        sandbox_path: Path,
    ) -> DeveloperWorkerReceipt:
        environment = _safe_environment(
            workspace=workspace_path,
            executable_roots=self._runtime_executable_roots,
        )
        before_digest = _workspace_tree_digest(
            workspace_path,
            excluded_relative_roots=_WORKER_RUNTIME_RELATIVE_ROOTS,
        )
        read_roots = (
            workspace_path,
            *self._runtime_read_roots,
        )
        profile = _sandbox_profile(
            read_roots=read_roots,
            executable_roots=(
                *self._runtime_executable_roots,
                workspace_path,
            ),
            writable_roots=writable_roots,
        )
        arguments_digest = _digest(_canonical_json(request.arguments))
        environment_digest = _digest(_canonical_json(environment))
        profile_digest = _digest(profile.encode("utf-8"))
        command = [
            str(sandbox_path),
            "-p",
            profile,
            str(self._runtime_executable),
            *request.arguments,
        ]
        started_at = datetime.now(timezone.utc)
        process = subprocess.Popen(
            command,
            cwd=workspace_path,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        timed_out = False
        exit_code: int | None
        try:
            stdout, stderr = process.communicate(timeout=request.timeout_seconds)
            exit_code = process.returncode
        except subprocess.TimeoutExpired as error:
            timed_out = True
            process.kill()
            tail_stdout, tail_stderr = process.communicate()
            stdout = (error.output or b"") + (tail_stdout or b"")
            stderr = (error.stderr or b"") + (tail_stderr or b"")
            exit_code = None
        finished_at = datetime.now(timezone.utc)
        stdout, stdout_complete = _bounded_capture(stdout)
        stderr, stderr_complete = _bounded_capture(stderr)
        boundary_intact = stdout_complete and stderr_complete
        try:
            after_digest = _workspace_tree_digest(
                workspace_path,
                excluded_relative_roots=_WORKER_RUNTIME_RELATIVE_ROOTS,
            )
        except FormalDeveloperWorkerError:
            boundary_intact = False
            after_digest = _digest(
                _canonical_json(
                    {
                        "boundary": "invalid",
                        "receipt_id": str(receipt_id),
                    }
                )
            )
        receipt_payload = {
            "schema_version": "1.0",
            "receipt_id": receipt_id,
            "assignment_id": channel.assignment_id,
            "channel_id": channel.channel_id,
            "report_id": report.report_id,
            "report_revision": report.revision,
            "lease_id": lease.lease_id,
            "lease_owner_id": lease.owner_id,
            "response_id": request.response_id,
            "task_id": workspace.task_id,
            "task_revision": workspace.task_revision,
            "run_id": workspace.run_id,
            "workspace_manifest_digest": workspace.manifest_digest,
            "workspace_policy_digest": workspace.policy_digest,
            "source_manifest_digest": workspace.source_manifest_digest,
            "request_digest": request_digest,
            "runtime_executable_digest": self._runtime_executable_digest,
            "runtime_boundary_digest": self._runtime_boundary_digest,
            "arguments_digest": arguments_digest,
            "environment_digest": environment_digest,
            "sandbox_profile_digest": profile_digest,
            "workspace_before_digest": before_digest,
            "workspace_after_digest": after_digest,
            "writable_prefixes": ["source", "work"],
            "sandboxed": True,
            "network_access": "denied",
            "inherited_environment": "none",
            "started_at": started_at,
            "finished_at": finished_at,
            "worker_pid": process.pid,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout_digest": _digest(stdout),
            "stdout_bytes": len(stdout),
            "stderr_digest": _digest(stderr),
            "stderr_bytes": len(stderr),
            "boundary_intact": boundary_intact,
        }
        receipt = DeveloperWorkerReceipt.model_validate(
            {
                **receipt_payload,
                "receipt_digest": _digest(_canonical_json(receipt_payload)),
            }
        )
        _atomic_write(
            self._receipt_path(receipt_id),
            _canonical_json(receipt),
        )
        _atomic_write(
            run_path,
            _canonical_json(
                {
                    "schema_version": "1.0",
                    "receipt_id": str(receipt_id),
                    "request_digest": request_digest,
                    "receipt_digest": receipt.receipt_digest,
                    "state": "completed",
                }
            ),
        )
        return receipt

    def validate_receipt(
        self,
        *,
        channel: CollaborationChannel,
        report: CollaborationReport,
        lease: DeveloperLease,
        workspace: DeveloperWorkspaceBinding,
        response_id: UUID,
        receipt_id: UUID,
        receipt_digest: str,
        require_success: bool,
    ) -> bool:
        try:
            workspace_path, _manifest, _writable = _validate_workspace(workspace)
            receipt = self._load_receipt(receipt_id)
            bindings_match = (
                receipt.assignment_id == channel.assignment_id
                and receipt.channel_id == channel.channel_id
                and receipt.report_id == report.report_id
                and receipt.report_revision == report.revision
                and receipt.lease_id == lease.lease_id
                and receipt.lease_owner_id == lease.owner_id
                and receipt.response_id == response_id
                and receipt.task_id == workspace.task_id
                and receipt.task_revision == workspace.task_revision
                and receipt.run_id == workspace.run_id
                and hmac.compare_digest(
                    receipt.workspace_manifest_digest,
                    workspace.manifest_digest,
                )
                and hmac.compare_digest(
                    receipt.workspace_policy_digest,
                    workspace.policy_digest,
                )
                and hmac.compare_digest(
                    str(receipt.source_manifest_digest),
                    str(workspace.source_manifest_digest),
                )
                and hmac.compare_digest(receipt.receipt_digest, receipt_digest)
                and hmac.compare_digest(
                    receipt.runtime_executable_digest,
                    self._runtime_executable_digest,
                )
                and hmac.compare_digest(
                    receipt.runtime_boundary_digest,
                    self._runtime_boundary_digest,
                )
                and hmac.compare_digest(
                    receipt.workspace_after_digest,
                    _workspace_tree_digest(
                        workspace_path,
                        excluded_relative_roots=(
                            _WORKER_RUNTIME_RELATIVE_ROOTS
                        ),
                    ),
                )
                and receipt.boundary_intact
            )
            return bindings_match and (receipt.successful or not require_success)
        except (FormalDeveloperWorkerError, OSError, ValueError):
            return False
