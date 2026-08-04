from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .formal_workspace import (
    FormalWorkspaceRejected,
    validate_public_formal_workspace,
)
from .formal_source_provenance import (
    DEVELOPER_SOURCE_MANIFEST_FILE,
    DeveloperSourceProjectionManifest,
)
from .lilies_models import (
    ApplicationTarget,
    ApplicationTargetMode,
    BuildAssignment,
    CollaborationAccess,
    Digest,
    IdempotencyKey,
    OpaqueReference,
    PlatformAccess,
    PlatformScope,
)
from .task_packages import (
    WORKSPACE_MANIFEST_FILE,
    WORKSPACE_POLICY_FILE,
    AllowedActionsPolicy,
    TaskPackageManager,
    TaskPackageSecurityError,
    WorkspaceMountManifest,
    WorkspaceRole,
    formal_platform_scopes,
)


_FORBIDDEN_PUBLIC_SEGMENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "oracle",
        "protected",
        "expected-state",
        "platform-data",
        "platform_data",
    }
)
_FORBIDDEN_PUBLIC_IDENTITIES = frozenset(item.casefold() for item in _FORBIDDEN_PUBLIC_SEGMENTS)
_DEVELOPER_WORKER_RUNTIME_ROOTS = (
    PurePosixPath("work/.developer-worker-home"),
    PurePosixPath("work/.developer-worker-tmp"),
)
_PublicPath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4_096),
]


class FormalAssignmentBrokerError(RuntimeError):
    """Base error for a rejected platform-owned formal preparation."""


class FormalAssignmentBrokerConflict(FormalAssignmentBrokerError):
    """An idempotency or formal-run identity was reused with other input."""


class FormalAssignmentProviderError(FormalAssignmentBrokerError):
    """A trusted platform provider returned authority outside the request."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class PrepareFormalAssignmentRequest(_FrozenModel):
    """The complete caller-controlled surface of the formal broker.

    Business requirements, actions, budgets, fixtures, targets, platform
    scopes, credentials, and collaboration channels intentionally cannot be
    supplied here. They come from the frozen task package or platform-owned
    providers.
    """

    task_id: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    revision: int = Field(ge=1)
    assignment_id: UUID
    application_id: UUID
    build_id: UUID
    session_id: UUID
    connection_id: UUID
    environment_instance_id: OpaqueReference
    idempotency_key: IdempotencyKey


class FormalPublicWorkspace(_FrozenModel):
    path: _PublicPath
    manifest_digest: Digest
    policy_digest: Digest

    @field_validator("path")
    @classmethod
    def path_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("formal public workspace path must be absolute")
        return value


class FormalDeveloperWorkspace(_FrozenModel):
    """Private Codex workspace binding for one formal assignment.

    This receipt is deliberately resolved through the authenticated developer
    lease path.  It is not embedded in ``PreparedFormalAssignment`` and is
    therefore never sent to the Lilies daemon.
    """

    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    task_id: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    task_revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    workspace: FormalPublicWorkspace
    source_manifest_digest: Digest | None = None
    baseline_commit_sha: str | None = Field(
        default=None,
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    )
    baseline_tree_sha: str | None = Field(
        default=None,
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    )
    branch_ref: str | None = Field(default=None, min_length=6, max_length=1_024)
    allowed_new_prefixes: tuple[str, ...] = ()
    allowed_new_files: tuple[str, ...] = ()


class FormalAssignmentPublicDigests(_FrozenModel):
    public_summary_digest: Digest
    environment_ready_digest: Digest
    environment_lock_digest: Digest
    allowed_actions_digest: Digest
    budget_digest: Digest


class PreparedFormalAssignment(_FrozenModel):
    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    run_id: OpaqueReference
    assignment: BuildAssignment
    workspace: FormalPublicWorkspace
    digests: FormalAssignmentPublicDigests
    bundle_digest: Digest

    @model_validator(mode="after")
    def bundle_digest_matches(self) -> PreparedFormalAssignment:
        expected = _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"bundle_digest"},
                )
            )
        )
        if not hmac.compare_digest(expected, self.bundle_digest):
            raise ValueError("formal assignment bundle digest does not match")
        return self


PlatformAccessProvider = Callable[
    [
        PrepareFormalAssignmentRequest,
        tuple[PlatformScope, ...],
        AllowedActionsPolicy,
    ],
    PlatformAccess | Mapping[str, Any],
]
CollaborationAccessProvider = Callable[
    [PrepareFormalAssignmentRequest, datetime],
    CollaborationAccess | Mapping[str, Any],
]
DeveloperProjectionProvider = Callable[..., DeveloperSourceProjectionManifest]


class _PreparedRecord(_FrozenModel):
    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    request_digest: Digest
    request: PrepareFormalAssignmentRequest
    prepared: PreparedFormalAssignment
    developer_source_manifest_digest: Digest | None = None


class _IdentityRecord(_FrozenModel):
    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    identity_kind: str = Field(pattern=r"^(assignment|build|session)$")
    identity: UUID
    assignment_id: UUID
    request_digest: Digest


class _IdempotencyRecord(_FrozenModel):
    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    idempotency_key: IdempotencyKey
    assignment_id: UUID
    request_digest: Digest


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


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _request_digest(request: PrepareFormalAssignmentRequest) -> str:
    return _digest(_canonical_json(request))


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("formal broker registry contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> Any:
    raise ValueError(f"formal broker registry contains non-finite JSON: {value}")


def _is_within(candidate: Path, parent: Path) -> bool:
    return candidate == parent or candidate.is_relative_to(parent)


def _contains_forbidden_identity(value: str) -> bool:
    return any(
        part.casefold() in _FORBIDDEN_PUBLIC_IDENTITIES for part in PurePosixPath(value).parts
    )


def _prepare_private_root(path: Path, *, label: str) -> Path:
    lexical = Path(path)
    if lexical.exists() and lexical.is_symlink():
        raise TaskPackageSecurityError(f"{label} cannot be a symlink")
    lexical.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = lexical.resolve(strict=True)
    if not resolved.is_dir():
        raise TaskPackageSecurityError(f"{label} must be a directory")
    os.chmod(resolved, 0o700)
    return resolved


def _read_regular_json(path: Path) -> Any:
    if path.is_symlink():
        raise TaskPackageSecurityError("formal broker registry files cannot be symlinks")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TaskPackageSecurityError(
            "formal broker registry file is not safely readable"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o400
        ):
            raise TaskPackageSecurityError("formal broker registry requires private regular files")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            raise TaskPackageSecurityError("formal broker registry file is too large")
    finally:
        os.close(descriptor)
    try:
        return json.loads(
            payload,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise TaskPackageSecurityError("formal broker registry file is not valid JSON") from error


def _regular_file_digest(path: Path) -> str:
    """Hash one no-follow, single-link regular file without trusting its path."""

    if path.is_symlink():
        raise TaskPackageSecurityError("formal workspace files cannot be symlinks")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TaskPackageSecurityError("formal workspace file is not safely readable") from error
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise TaskPackageSecurityError("formal workspace requires single-link regular files")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        final = os.fstat(descriptor)
        if (
            final.st_dev != metadata.st_dev
            or final.st_ino != metadata.st_ino
            or final.st_size != metadata.st_size
            or final.st_mtime_ns != metadata.st_mtime_ns
            or final.st_nlink != 1
        ):
            raise TaskPackageSecurityError("formal workspace file changed while it was verified")
    finally:
        os.close(descriptor)
    return f"sha256:{digest.hexdigest()}"


def _read_workspace_payload(path: Path, *, limit: int = 32 * 1024 * 1024) -> bytes:
    if path.is_symlink():
        raise TaskPackageSecurityError("formal workspace files cannot be symlinks")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TaskPackageSecurityError(
            "formal workspace file is not safely readable"
        ) from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise TaskPackageSecurityError(
                "formal workspace requires bounded single-link regular files"
            )
        payload = b""
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1)):
            payload += chunk
            if len(payload) > limit:
                raise TaskPackageSecurityError("formal workspace file is too large")
        after = os.fstat(descriptor)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_nlink != 1
        ):
            raise TaskPackageSecurityError(
                "formal workspace file changed while it was verified"
            )
        return payload
    finally:
        os.close(descriptor)


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not hmac.compare_digest(
            _canonical_json(_read_regular_json(path)),
            payload,
        ):
            raise FormalAssignmentBrokerConflict(
                "formal broker registry identity already has other content"
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
            if not hmac.compare_digest(
                _canonical_json(_read_regular_json(path)),
                payload,
            ):
                raise FormalAssignmentBrokerConflict(
                    "formal broker registry identity raced with other content"
                )
        else:
            os.chmod(path, 0o400)
    finally:
        temporary.unlink(missing_ok=True)


class FormalAssignmentBroker:
    """Platform-owned gate from a sealed package to a public daemon bundle."""

    def __init__(
        self,
        *,
        task_state_root: Path,
        broker_state_root: Path,
        public_workspace_root: Path,
        platform_access_provider: PlatformAccessProvider,
        collaboration_access_provider: CollaborationAccessProvider,
        environment_secret_resolver: Callable[[str], bytes],
        developer_source_root: Path | None = None,
        developer_workspace_root: Path | None = None,
        developer_projection_provider: DeveloperProjectionProvider | None = None,
        supplemental_public_materials: Mapping[str, Path] | None = None,
    ) -> None:
        lexical_task_root = Path(task_state_root)
        if lexical_task_root.exists() and lexical_task_root.is_symlink():
            raise TaskPackageSecurityError("task package state root cannot be a symlink")
        self.__manager = TaskPackageManager(
            lexical_task_root,
            environment_secret_resolver=environment_secret_resolver,
        )
        self.__broker_root = _prepare_private_root(
            Path(broker_state_root),
            label="formal broker state root",
        )
        self.__workspace_root = _prepare_private_root(
            Path(public_workspace_root),
            label="formal public workspace root",
        )
        if (developer_source_root is None) != (developer_workspace_root is None):
            raise ValueError("developer source and workspace roots must be configured together")
        self.__developer_source_root: Path | None = None
        self.__developer_workspace_root: Path | None = None
        self.__developer_projection_provider = developer_projection_provider
        if developer_source_root is not None and developer_workspace_root is not None:
            source_lexical = Path(developer_source_root)
            if source_lexical.is_symlink() or not source_lexical.is_dir():
                raise TaskPackageSecurityError(
                    "formal developer source root must be a real directory"
                )
            self.__developer_source_root = source_lexical.resolve(strict=True)
            self.__developer_workspace_root = _prepare_private_root(
                Path(developer_workspace_root),
                label="formal developer workspace root",
            )
        if developer_projection_provider is not None and developer_source_root is None:
            raise ValueError(
                "developer projection provider requires configured developer workspaces"
            )
        sealed_root = self.__manager.state_root
        isolated_roots = [
            sealed_root,
            self.__broker_root,
            self.__workspace_root,
        ]
        if self.__developer_workspace_root is not None:
            isolated_roots.append(self.__developer_workspace_root)
        if any(
            _is_within(left, right) or _is_within(right, left)
            for index, left in enumerate(isolated_roots)
            for right in isolated_roots[index + 1 :]
        ):
            raise TaskPackageSecurityError(
                "sealed task state, broker state, and formal workspaces must be isolated"
            )
        if self.__developer_source_root is not None and any(
            _is_within(root, self.__developer_source_root)
            or _is_within(self.__developer_source_root, root)
            for root in isolated_roots
        ):
            raise TaskPackageSecurityError(
                "formal developer source must be isolated from task and runtime state"
            )
        self.__records_root = self.__broker_root / "records"
        self.__identity_root = self.__broker_root / "identities"
        self.__idempotency_root = self.__broker_root / "idempotency"
        for path in (
            self.__records_root,
            self.__identity_root,
            self.__idempotency_root,
        ):
            if path.exists() and path.is_symlink():
                raise TaskPackageSecurityError(
                    "formal broker registry directory cannot be a symlink"
                )
            path.mkdir(mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)
        self.__lock_path = self.__broker_root / ".prepare.lock"
        self.__platform_access_provider = platform_access_provider
        self.__collaboration_access_provider = collaboration_access_provider
        self.__supplemental_public_materials = (
            dict(supplemental_public_materials)
            if supplemental_public_materials is not None
            else None
        )

    @contextmanager
    def _prepare_lock(self) -> Any:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.__lock_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _record_path(self, assignment_id: UUID) -> Path:
        return self.__records_root / f"{assignment_id}.json"

    def _identity_path(self, kind: str, identity: UUID) -> Path:
        return self.__identity_root / kind / f"{identity}.json"

    def _idempotency_path(self, key: str) -> Path:
        return self.__idempotency_root / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def _load_record(self, assignment_id: UUID) -> _PreparedRecord:
        path = self._record_path(assignment_id)
        if not path.is_file() or path.is_symlink():
            raise TaskPackageSecurityError(
                "formal broker registry points to a missing prepared record"
            )
        return _PreparedRecord.model_validate(_read_regular_json(path))

    def _validate_replay(
        self,
        request: PrepareFormalAssignmentRequest,
        expected_request_digest: str,
        record: _PreparedRecord,
        *,
        require_live_environment_ready: bool = True,
    ) -> PreparedFormalAssignment:
        if (
            not hmac.compare_digest(record.request_digest, expected_request_digest)
            or record.request != request
        ):
            raise FormalAssignmentBrokerConflict(
                "formal assignment identity was reused with other input"
            )
        prepared = record.prepared
        if prepared.assignment.assignment_id != request.assignment_id:
            raise TaskPackageSecurityError(
                "formal broker prepared record has another assignment identity"
            )
        expected_workspace = (self.__workspace_root / str(request.assignment_id)).resolve(
            strict=False
        )
        if Path(prepared.workspace.path) != expected_workspace:
            raise TaskPackageSecurityError(
                "formal broker prepared record points outside its public workspace"
            )
        if require_live_environment_ready:
            self.__manager.authorize_formal_assignment(prepared.assignment)
        else:
            self.__manager._authorize_formal_assignment(  # noqa: SLF001
                prepared.assignment,
                at=prepared.assignment.created_at,
            )
        self._assert_public_workspace(
            Path(prepared.workspace.path),
            prepared.assignment,
        )
        if self.__developer_workspace_root is not None:
            self._assert_developer_workspace(
                self.__developer_workspace_root / str(request.assignment_id),
                prepared,
                expected_source_manifest_digest=record.developer_source_manifest_digest,
            )
        return prepared

    def _resolve_replay(
        self,
        request: PrepareFormalAssignmentRequest,
        expected_request_digest: str,
    ) -> PreparedFormalAssignment | None:
        key_path = self._idempotency_path(request.idempotency_key)
        if key_path.exists() or key_path.is_symlink():
            key_record = _IdempotencyRecord.model_validate(_read_regular_json(key_path))
            if (
                key_record.idempotency_key != request.idempotency_key
                or not hmac.compare_digest(
                    key_record.request_digest,
                    expected_request_digest,
                )
                or key_record.assignment_id != request.assignment_id
            ):
                raise FormalAssignmentBrokerConflict(
                    "formal broker idempotency key was reused with other input"
                )
            prepared = self._validate_replay(
                request,
                expected_request_digest,
                self._load_record(key_record.assignment_id),
            )
            self._persist_identities(request, expected_request_digest)
            self._persist_idempotency(request, expected_request_digest)
            return prepared
        assignment_path = self._record_path(request.assignment_id)
        if assignment_path.exists() or assignment_path.is_symlink():
            prepared = self._validate_replay(
                request,
                expected_request_digest,
                self._load_record(request.assignment_id),
            )
            self._persist_identities(request, expected_request_digest)
            self._persist_idempotency(request, expected_request_digest)
            return prepared
        for kind, identity in (
            ("build", request.build_id),
            ("session", request.session_id),
        ):
            path = self._identity_path(kind, identity)
            if path.exists() or path.is_symlink():
                identity_record = _IdentityRecord.model_validate(_read_regular_json(path))
                if (
                    identity_record.identity != identity
                    or identity_record.assignment_id != request.assignment_id
                    or not hmac.compare_digest(
                        identity_record.request_digest,
                        expected_request_digest,
                    )
                ):
                    raise FormalAssignmentBrokerConflict(
                        f"formal {kind} identity was reused with other input"
                    )
                raise TaskPackageSecurityError(
                    f"formal {kind} registry has no prepared assignment record"
                )
        return None

    def _persist_idempotency(
        self,
        request: PrepareFormalAssignmentRequest,
        request_digest: str,
    ) -> None:
        record = _IdempotencyRecord(
            idempotency_key=request.idempotency_key,
            assignment_id=request.assignment_id,
            request_digest=request_digest,
        )
        _write_immutable(
            self._idempotency_path(request.idempotency_key),
            _canonical_json(record),
        )

    def _persist_identities(
        self,
        request: PrepareFormalAssignmentRequest,
        request_digest: str,
    ) -> None:
        for kind, identity in (
            ("assignment", request.assignment_id),
            ("build", request.build_id),
            ("session", request.session_id),
        ):
            identity_record = _IdentityRecord(
                identity_kind=kind,
                identity=identity,
                assignment_id=request.assignment_id,
                request_digest=request_digest,
            )
            _write_immutable(
                self._identity_path(kind, identity),
                _canonical_json(identity_record),
            )

    def _persist_prepared(
        self,
        request: PrepareFormalAssignmentRequest,
        request_digest: str,
        prepared: PreparedFormalAssignment,
        *,
        developer_source_manifest_digest: str | None = None,
    ) -> None:
        record = _PreparedRecord(
            request_digest=request_digest,
            request=request,
            prepared=prepared,
            developer_source_manifest_digest=developer_source_manifest_digest,
        )
        _write_immutable(
            self._record_path(request.assignment_id),
            _canonical_json(record),
        )
        self._persist_identities(request, request_digest)
        self._persist_idempotency(request, request_digest)

    def resolve_prepared_assignment(
        self,
        *,
        assignment_id: UUID,
        session_id: UUID,
    ) -> PreparedFormalAssignment:
        """Resolve one persisted broker receipt by its exact daemon session."""

        with self._prepare_lock():
            record = self._load_record(assignment_id)
            if (
                record.request.assignment_id != assignment_id
                or record.request.session_id != session_id
            ):
                raise FormalAssignmentBrokerConflict(
                    "formal assignment is not bound to the supplied session"
                )
            return self._validate_replay(
                record.request,
                _request_digest(record.request),
                record,
            )

    def resolve_developer_workspace(
        self,
        *,
        assignment_id: UUID,
        session_id: UUID,
    ) -> FormalDeveloperWorkspace:
        """Resolve the private Codex snapshot for one exact formal session."""

        if self.__developer_workspace_root is None:
            raise FormalAssignmentProviderError("formal developer workspace is not configured")
        with self._prepare_lock():
            record = self._load_record(assignment_id)
            if (
                record.request.assignment_id != assignment_id
                or record.request.session_id != session_id
            ):
                raise FormalAssignmentBrokerConflict(
                    "formal developer workspace is not bound to the supplied session"
                )
            prepared = self._validate_replay(
                record.request,
                _request_digest(record.request),
                record,
                require_live_environment_ready=False,
            )
            return self._assert_developer_workspace(
                self.__developer_workspace_root / str(assignment_id),
                prepared,
                expected_source_manifest_digest=record.developer_source_manifest_digest,
            )

    def _assert_developer_workspace(
        self,
        workspace: Path,
        prepared: PreparedFormalAssignment,
        *,
        expected_source_manifest_digest: str | None = None,
    ) -> FormalDeveloperWorkspace:
        if self.__developer_source_root is None or self.__developer_workspace_root is None:
            raise FormalAssignmentProviderError("formal developer workspace is not configured")
        expected = (
            self.__developer_workspace_root / str(prepared.assignment.assignment_id)
        ).resolve(strict=False)
        if workspace.is_symlink() or not workspace.is_dir():
            raise TaskPackageSecurityError("formal developer workspace must be a real directory")
        if workspace.resolve(strict=True) != expected:
            raise TaskPackageSecurityError(
                "formal developer workspace points outside its private root"
            )
        task_ref = prepared.assignment.task_package
        if task_ref is None:
            raise TaskPackageSecurityError("formal developer workspace has no task-package binding")
        package = self.__manager.load_frozen(
            task_ref.task_id,
            task_ref.revision,
            expected_public_digest=task_ref.public_summary_digest,
        )
        manifest, manifest_digest, policy_digest = self.__manager.require_workspace_manifest(
            package,
            workspace / WORKSPACE_MANIFEST_FILE,
            role=WorkspaceRole.developer,
            run_id=prepared.run_id,
            assignment_id=prepared.assignment.assignment_id,
        )
        platform_entries = [
            entry
            for entry in manifest.entries
            if entry.logical_source.startswith("platform-source:")
        ]
        if not platform_entries:
            raise TaskPackageSecurityError(
                "formal developer workspace has no filtered platform source"
            )
        projection: DeveloperSourceProjectionManifest | None = None
        source_manifest_path = (
            workspace / "source" / DEVELOPER_SOURCE_MANIFEST_FILE
        )
        if expected_source_manifest_digest is not None:
            try:
                source_manifest_payload = _read_workspace_payload(
                    source_manifest_path,
                    limit=32 * 1024 * 1024,
                )
                projection = DeveloperSourceProjectionManifest.model_validate_json(
                    source_manifest_payload
                )
            except Exception as error:
                raise TaskPackageSecurityError(
                    "formal developer source manifest is unavailable or invalid"
                ) from error
            if (
                not hmac.compare_digest(
                    source_manifest_payload,
                    _canonical_json(projection),
                )
                or not hmac.compare_digest(
                    projection.manifest_digest,
                    expected_source_manifest_digest,
                )
                or projection.task_id != task_ref.task_id
                or projection.task_revision != task_ref.revision
                or projection.run_id != prepared.run_id
                or projection.assignment_id != prepared.assignment.assignment_id
            ):
                raise TaskPackageSecurityError(
                    "formal developer source manifest differs from its broker binding"
                )
            if stat.S_IMODE(
                source_manifest_path.stat(follow_symlinks=False).st_mode
            ) != 0o400:
                raise TaskPackageSecurityError(
                    "formal developer source manifest must remain read-only"
                )
            source_entry_by_path = {
                entry.logical_source.removeprefix("platform-source:"): entry
                for entry in platform_entries
            }
            manifest_entry = source_entry_by_path.get(
                DEVELOPER_SOURCE_MANIFEST_FILE
            )
            if (
                manifest_entry is None
                or not hmac.compare_digest(
                    manifest_entry.digest,
                    _digest(source_manifest_payload),
                )
            ):
                raise TaskPackageSecurityError(
                    "workspace manifest does not bind the source projection manifest"
                )
            projected_entries = {
                entry.path: entry for entry in projection.entries
            }
            declared_projection = {
                path: entry
                for path, entry in source_entry_by_path.items()
                if path != DEVELOPER_SOURCE_MANIFEST_FILE
            }
            if set(declared_projection) != set(projected_entries):
                raise TaskPackageSecurityError(
                    "workspace projection differs from the trusted Git manifest"
                )
            for path, projected in projected_entries.items():
                declared = declared_projection[path]
                if (
                    declared.target_path != f"source/{path}"
                    or declared.read_only
                    or declared.size_bytes != projected.size_bytes
                    or not hmac.compare_digest(declared.digest, projected.digest)
                ):
                    raise TaskPackageSecurityError(
                        "workspace projection entry differs from its Git blob"
                    )
        declared_paths = {entry.target_path: entry for entry in manifest.entries}
        declared_paths[WORKSPACE_MANIFEST_FILE] = None
        declared_paths[WORKSPACE_POLICY_FILE] = None
        for entry in manifest.entries:
            parts = PurePosixPath(entry.target_path).parts
            if any(part.casefold() in _FORBIDDEN_PUBLIC_IDENTITIES for part in parts):
                raise TaskPackageSecurityError(
                    "formal developer workspace contains a protected path"
                )
            if entry.logical_source.startswith("platform-source:"):
                source_path = entry.logical_source.removeprefix("platform-source:")
                if (
                    entry.read_only
                    or not entry.target_path.startswith("source/")
                    or _contains_forbidden_identity(source_path)
                ):
                    raise TaskPackageSecurityError(
                        "formal developer source entry violates its filtered projection"
                    )
                continue
            if (
                not entry.logical_source.startswith("task-package:")
                or not entry.read_only
                or not entry.target_path.startswith("task/")
            ):
                raise TaskPackageSecurityError(
                    "formal developer workspace has an unknown source authority"
                )
            target = workspace / entry.target_path
            if stat.S_IMODE(
                target.stat(follow_symlinks=False).st_mode
            ) != 0o400 or not hmac.compare_digest(
                _regular_file_digest(target),
                entry.digest,
            ):
                raise TaskPackageSecurityError(
                    "formal developer task input changed after materialization"
                )
        for path in workspace.rglob("*"):
            relative = path.relative_to(workspace).as_posix()
            relative_path = PurePosixPath(relative)
            if any(
                relative_path == runtime_root
                or runtime_root in relative_path.parents
                for runtime_root in _DEVELOPER_WORKER_RUNTIME_ROOTS
            ):
                continue
            relative_parts = relative_path.parts
            if any(part.casefold() in _FORBIDDEN_PUBLIC_IDENTITIES for part in relative_parts):
                raise TaskPackageSecurityError(
                    "formal developer workspace contains a reserved tree segment"
                )
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise TaskPackageSecurityError("formal developer workspace cannot contain links")
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise TaskPackageSecurityError(
                    "formal developer workspace requires single-link regular files"
                )
            if relative not in declared_paths and not relative.startswith(("source/", "work/")):
                raise TaskPackageSecurityError(
                    "formal developer workspace contains an undeclared authority file"
                )
        return FormalDeveloperWorkspace(
            task_id=task_ref.task_id,
            task_revision=task_ref.revision,
            run_id=prepared.run_id,
            assignment_id=prepared.assignment.assignment_id,
            workspace=FormalPublicWorkspace(
                path=str(workspace.resolve(strict=True)),
                manifest_digest=manifest_digest,
                policy_digest=policy_digest,
            ),
            source_manifest_digest=(
                projection.manifest_digest if projection is not None else None
            ),
            baseline_commit_sha=(
                projection.baseline_commit_sha if projection is not None else None
            ),
            baseline_tree_sha=(
                projection.baseline_tree_sha if projection is not None else None
            ),
            branch_ref=projection.branch_ref if projection is not None else None,
            allowed_new_prefixes=(
                tuple(projection.allowed_new_prefixes)
                if projection is not None
                else ()
            ),
            allowed_new_files=(
                tuple(projection.allowed_new_files)
                if projection is not None
                else ()
            ),
        )

    @staticmethod
    def _assert_public_workspace(
        workspace: Path,
        assignment: BuildAssignment,
    ) -> WorkspaceMountManifest:
        if workspace.is_symlink() or not workspace.is_dir():
            raise TaskPackageSecurityError("formal public workspace must be a real directory")
        task_ref = assignment.task_package
        if task_ref is None:
            raise TaskPackageSecurityError(
                "formal assignment is missing its public workspace binding"
            )
        try:
            validate_public_formal_workspace(assignment, workspace)
        except FormalWorkspaceRejected as error:
            raise TaskPackageSecurityError(
                "formal public workspace failed its independent public gate"
            ) from error
        manifest_path = workspace / WORKSPACE_MANIFEST_FILE
        manifest = WorkspaceMountManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.role is not WorkspaceRole.lilies:
            raise TaskPackageSecurityError(
                "formal broker may only return a Lilies public workspace"
            )
        for entry in manifest.entries:
            parts = PurePosixPath(entry.target_path).parts
            if any(part.casefold() in _FORBIDDEN_PUBLIC_IDENTITIES for part in parts):
                raise TaskPackageSecurityError("formal public workspace contains a protected path")
            if entry.logical_source.casefold().startswith(
                ("task-package:protected/", "task-package:oracle/")
            ):
                raise TaskPackageSecurityError(
                    "formal public workspace contains a protected source"
                )
        for path in workspace.rglob("*"):
            relative_parts = path.relative_to(workspace).parts
            if any(part.casefold() in _FORBIDDEN_PUBLIC_IDENTITIES for part in relative_parts):
                raise TaskPackageSecurityError(
                    "formal public workspace contains a reserved tree segment"
                )
        return manifest

    def prepare(
        self,
        request: PrepareFormalAssignmentRequest | Mapping[str, Any],
    ) -> PreparedFormalAssignment:
        parsed = PrepareFormalAssignmentRequest.model_validate(request)
        parsed_digest = _request_digest(parsed)
        with self._prepare_lock():
            replay = self._resolve_replay(parsed, parsed_digest)
            if replay is not None:
                return replay

            package = self.__manager.load_frozen(
                parsed.task_id,
                parsed.revision,
            )
            required_scopes = tuple(
                formal_platform_scopes(
                    package.allowed_actions.platform_actions,
                )
            )
            platform = PlatformAccess.model_validate(
                self.__platform_access_provider(
                    parsed,
                    required_scopes,
                    package.allowed_actions,
                )
            )
            if tuple(platform.scopes) != required_scopes or platform.application_ids != [
                parsed.application_id
            ]:
                raise FormalAssignmentProviderError(
                    "platform provider must issue the exact action scopes "
                    "for only the existing target application"
                )

            run_id = TypeAdapter(OpaqueReference).validate_python(f"formal-run:{parsed.build_id}")
            ready_path, ready = self.__manager.run_environment_preflight(
                package,
                run_id=run_id,
                assignment_id=parsed.assignment_id,
                environment_instance_id=parsed.environment_instance_id,
                ttl_seconds=max(
                    60,
                    package.budget.assignment_wall_clock_seconds,
                ),
            )
            _, ready_digest = self.__manager.require_environment_ready(
                package,
                ready_path,
                run_id=run_id,
                assignment_id=parsed.assignment_id,
            )
            deadline_at = ready.finished_at + timedelta(
                seconds=package.budget.assignment_wall_clock_seconds
            )
            collaboration = CollaborationAccess.model_validate(
                self.__collaboration_access_provider(parsed, deadline_at)
            )
            if collaboration.expires_at != deadline_at:
                raise FormalAssignmentProviderError(
                    "collaboration provider must bind authority to the exact "
                    "formal assignment deadline"
                )
            if collaboration.credential_ref == platform.credential_ref:
                raise FormalAssignmentProviderError(
                    "platform and collaboration credentials must be distinct"
                )

            workspace = self.__workspace_root / str(parsed.assignment_id)
            if workspace.exists() or workspace.is_symlink():
                if workspace.is_symlink():
                    raise TaskPackageSecurityError("formal public workspace cannot be a symlink")
                self.__manager.require_workspace_manifest(
                    package,
                    workspace / WORKSPACE_MANIFEST_FILE,
                    role=WorkspaceRole.lilies,
                    run_id=run_id,
                    assignment_id=parsed.assignment_id,
                    environment_ready_digest=ready_digest,
                    environment_instance_id=ready.environment_instance_id,
                )
            else:
                self.__manager.materialize_task_workspace(
                    package,
                    workspace,
                    role=WorkspaceRole.lilies,
                    run_id=run_id,
                    assignment_id=parsed.assignment_id,
                    environment_ready_path=ready_path,
                    supplemental_public_materials=(
                        self.__supplemental_public_materials
                    ),
                )
            _, workspace_digest, policy_digest = self.__manager.require_workspace_manifest(
                package,
                workspace / WORKSPACE_MANIFEST_FILE,
                role=WorkspaceRole.lilies,
                run_id=run_id,
                assignment_id=parsed.assignment_id,
                environment_ready_digest=ready_digest,
                environment_instance_id=ready.environment_instance_id,
            )
            assignment = self.__manager.build_formal_assignment(
                package,
                ready_path=ready_path,
                workspace_manifest_path=workspace / WORKSPACE_MANIFEST_FILE,
                run_id=run_id,
                assignment_id=parsed.assignment_id,
                idempotency_key=parsed.idempotency_key,
                target=ApplicationTarget(
                    mode=ApplicationTargetMode.existing,
                    application_id=parsed.application_id,
                ),
                platform=platform,
                collaboration=collaboration,
                created_at=ready.finished_at,
            )
            self.__manager.authorize_formal_assignment(assignment)
            self._assert_public_workspace(workspace, assignment)
            task_ref = assignment.task_package
            assert task_ref is not None
            developer_source_manifest_digest: str | None = None
            if (
                self.__developer_source_root is not None
                and self.__developer_workspace_root is not None
            ):
                developer_workspace = self.__developer_workspace_root / str(parsed.assignment_id)
                if developer_workspace.exists() or developer_workspace.is_symlink():
                    if developer_workspace.is_symlink():
                        raise TaskPackageSecurityError(
                            "formal developer workspace cannot be a symlink"
                        )
                    if self.__developer_projection_provider is not None:
                        try:
                            workspace_projection = DeveloperSourceProjectionManifest.model_validate_json(
                                _read_workspace_payload(
                                    developer_workspace
                                    / "source"
                                    / DEVELOPER_SOURCE_MANIFEST_FILE
                                )
                            )
                            with tempfile.TemporaryDirectory(
                                prefix=".developer-projection-replay-",
                                dir=self.__broker_root,
                            ) as projection_directory:
                                projection = self.__developer_projection_provider(
                                    task_id=task_ref.task_id,
                                    task_revision=task_ref.revision,
                                    run_id=run_id,
                                    assignment_id=parsed.assignment_id,
                                    channel_id=collaboration.channel_id,
                                    captured_at=ready.finished_at,
                                    destination=Path(projection_directory),
                                )
                        except Exception as error:
                            raise TaskPackageSecurityError(
                                "existing formal developer workspace has no trusted projection"
                            ) from error
                        if workspace_projection != projection:
                            raise TaskPackageSecurityError(
                                "existing formal developer workspace changed after projection"
                            )
                        developer_source_manifest_digest = projection.manifest_digest
                else:
                    source_root = self.__developer_source_root
                    if self.__developer_projection_provider is not None:
                        with tempfile.TemporaryDirectory(
                            prefix=".developer-projection-",
                            dir=self.__broker_root,
                        ) as projection_directory:
                            projection = self.__developer_projection_provider(
                                task_id=task_ref.task_id,
                                task_revision=task_ref.revision,
                                run_id=run_id,
                                assignment_id=parsed.assignment_id,
                                channel_id=collaboration.channel_id,
                                captured_at=ready.finished_at,
                                destination=Path(projection_directory),
                            )
                            developer_source_manifest_digest = (
                                projection.manifest_digest
                            )
                            self.__manager.materialize_task_workspace(
                                package,
                                developer_workspace,
                                role=WorkspaceRole.developer,
                                run_id=run_id,
                                assignment_id=parsed.assignment_id,
                                developer_source_root=Path(projection_directory),
                            )
                        os.chmod(
                            developer_workspace
                            / "source"
                            / DEVELOPER_SOURCE_MANIFEST_FILE,
                            0o400,
                        )
                    else:
                        self.__manager.materialize_task_workspace(
                            package,
                            developer_workspace,
                            role=WorkspaceRole.developer,
                            run_id=run_id,
                            assignment_id=parsed.assignment_id,
                            developer_source_root=source_root,
                        )
            public_payload = {
                "schema_version": "1.0",
                "run_id": run_id,
                "assignment": assignment.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
                "workspace": {
                    "path": str(workspace.resolve(strict=True)),
                    "manifest_digest": workspace_digest,
                    "policy_digest": policy_digest,
                },
                "digests": {
                    "public_summary_digest": task_ref.public_summary_digest,
                    "environment_ready_digest": task_ref.environment_ready_digest,
                    "environment_lock_digest": task_ref.environment_lock_digest,
                    "allowed_actions_digest": task_ref.allowed_actions_digest,
                    "budget_digest": task_ref.budget_digest,
                },
            }
            prepared = PreparedFormalAssignment(
                **public_payload,
                bundle_digest=_digest(_canonical_json(public_payload)),
            )
            if self.__developer_workspace_root is not None:
                self._assert_developer_workspace(
                    self.__developer_workspace_root / str(parsed.assignment_id),
                    prepared,
                    expected_source_manifest_digest=developer_source_manifest_digest,
                )
            self._persist_prepared(
                parsed,
                parsed_digest,
                prepared,
                developer_source_manifest_digest=developer_source_manifest_digest,
            )
            return prepared
