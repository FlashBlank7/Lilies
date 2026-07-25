"""Role-scoped tools for explicit collaborative-development assignments.

This module is intentionally platform-neutral.  It consumes only the frozen
``WorkspaceGrant`` issued for one development role and never imports workflow,
Builder, application, formal-task, or verifier services.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .collaborative_development_models import AgentRole, SideEffect, WorkspaceGrant


class DevelopmentToolError(RuntimeError):
    """A development tool could not safely complete."""


class DevelopmentToolDenied(DevelopmentToolError):
    """The frozen role grant does not authorize the requested operation."""


class DevelopmentToolUsageReplay(DevelopmentToolDenied):
    """A durable usage id was already consumed and must not execute twice."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        frozen=True,
    )


class DevelopmentToolName(str, Enum):
    workspace_search = "workspace_search"
    workspace_read = "workspace_read"
    workspace_write = "workspace_write"
    workspace_patch = "workspace_patch"
    process_run = "process_run"
    git_status = "git_status"
    git_diff = "git_diff"


class DevelopmentToolUsageMeter(Protocol):
    """The trusted, assignment-owned usage ledger used by bounded tools.

    ``reserve_development_tool_usage`` returns ``True`` only for the process
    that atomically acquired the right to execute the operation.  Returning
    ``False`` means the stable usage id was already reserved; callers must not
    repeat the filesystem or process side effect.
    """

    async def reserve_development_tool_usage(
        self,
        *,
        assignment_id: UUID,
        actor_role: AgentRole,
        usage_id: str,
        tool_name: str,
        request_digest: str,
        command_argv: tuple[str, ...] | None,
        command_cwd: str | None,
    ) -> bool: ...

    async def complete_development_tool_usage(
        self,
        *,
        assignment_id: UUID,
        actor_role: AgentRole,
        usage_id: str,
        request_digest: str,
        response_digest: str,
        output_digest: str | None,
    ) -> None: ...


def _unique_tuple[T](values: tuple[T, ...], *, label: str) -> tuple[T, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")
    return values


def _normalized_relative_path(value: str, *, allow_root: bool = False) -> str:
    if "\x00" in value or "\\" in value:
        raise ValueError("path must be a normalized POSIX workspace path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must stay inside the granted workspace")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        if allow_root:
            return "."
        raise ValueError("path must identify an entry below the workspace root")
    return normalized


def _validated_argv(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        raise ValueError("argv must not be empty")
    if len(value) > 256:
        raise ValueError("argv exceeds the 256-element limit")
    if sum(len(item.encode("utf-8")) for item in value) > 256 * 1024:
        raise ValueError("argv exceeds the bounded request size")
    if any(not item or "\x00" in item or "\r" in item or "\n" in item for item in value):
        raise ValueError("argv elements must be non-empty and free of control separators")
    return value


def _validated_usage_id(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 240
        or "\x00" in candidate
        or "\r" in candidate
        or "\n" in candidate
    ):
        raise ValueError("usage_id must be a non-empty bounded opaque identifier")
    return candidate


class _MeteredToolRequest(_StrictFrozenModel):
    """Common request metadata hidden from the authority grant.

    The caller, not an agent-generated payload, is responsible for binding this
    value to a stable model tool-use id or another durable invocation id.
    Unmetered low-level tests may omit it; metered production tools reject its
    absence before touching workspace contents or starting a process.
    """

    usage_id: str | None = Field(default=None, min_length=1, max_length=240)

    @field_validator("usage_id")
    @classmethod
    def usage_id_is_bounded(cls, value: str | None) -> str | None:
        return _validated_usage_id(value)


class DevelopmentToolAuthority(_StrictFrozenModel):
    """The exact role grant and local safety limits used by one tool service."""

    schema_version: Literal["1.0"] = "1.0"
    actor_role: AgentRole
    workspace_grant: WorkspaceGrant
    enabled_tools: tuple[DevelopmentToolName, ...] = Field(min_length=1)
    max_timeout_seconds: float = Field(default=120.0, gt=0, le=900, allow_inf_nan=False)
    max_output_bytes: int = Field(default=256_000, ge=1, le=4_000_000)
    autonomous_handoff: bool = False

    @field_validator("enabled_tools")
    @classmethod
    def enabled_tools_are_unique(
        cls,
        value: tuple[DevelopmentToolName, ...],
    ) -> tuple[DevelopmentToolName, ...]:
        return _unique_tuple(value, label="enabled tools")

    @model_validator(mode="after")
    def role_and_side_effects_match(self) -> DevelopmentToolAuthority:
        if self.workspace_grant.agent_role != self.actor_role:
            raise ValueError("tool actor role must match the frozen workspace grant")
        enabled = set(self.enabled_tools)
        effects = set(self.workspace_grant.allowed_side_effects)
        if enabled.intersection(
            {
                DevelopmentToolName.workspace_write,
                DevelopmentToolName.workspace_patch,
            }
        ) and SideEffect.workspace_write not in effects:
            raise ValueError("workspace mutation tools require workspace_write authority")
        if (
            DevelopmentToolName.process_run in enabled
            and SideEffect.process_execute not in effects
        ):
            raise ValueError("process_run requires process_execute authority")
        if (
            DevelopmentToolName.process_run in enabled
            and not self.workspace_grant.allowed_argv
        ):
            raise ValueError("process_run requires an explicit non-empty argv allowlist")
        return self


class AutonomousHandoffAuthorityRequest(_StrictFrozenModel):
    """A same-role projection used by an autonomous child or resumed worker."""

    enabled_tools: tuple[DevelopmentToolName, ...] = Field(min_length=1)
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    allowed_argv: tuple[tuple[str, ...], ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    allowed_side_effects: tuple[SideEffect, ...] = ()
    secret_refs: tuple[str, ...] = ()
    max_timeout_seconds: float = Field(gt=0, le=900, allow_inf_nan=False)
    max_output_bytes: int = Field(ge=1, le=4_000_000)

    @field_validator("enabled_tools", "allowed_paths", "allowed_argv")
    @classmethod
    def required_values_are_unique(cls, value: tuple) -> tuple:
        return _unique_tuple(value, label="handoff values")

    @field_validator("allowed_hosts", "allowed_side_effects", "secret_refs")
    @classmethod
    def optional_values_are_unique(cls, value: tuple) -> tuple:
        return _unique_tuple(value, label="handoff values")

    @field_validator("allowed_paths")
    @classmethod
    def paths_are_normalized(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_normalized_relative_path(item) for item in value)

    @field_validator("allowed_argv")
    @classmethod
    def argv_vectors_are_valid(
        cls,
        value: tuple[tuple[str, ...], ...],
    ) -> tuple[tuple[str, ...], ...]:
        return tuple(_validated_argv(item) for item in value)


def derive_autonomous_handoff_authority(
    parent: DevelopmentToolAuthority,
    request: AutonomousHandoffAuthorityRequest | None = None,
) -> DevelopmentToolAuthority:
    """Return the identical authority, or an explicitly narrower projection.

    A handoff to a different role must use that role's independently frozen
    assignment grant.  This function deliberately cannot change roles,
    workspace identity, baseline, or grant revision.
    """

    if request is None:
        return parent.model_copy(update={"autonomous_handoff": True})

    grant = parent.workspace_grant
    if not set(request.enabled_tools).issubset(parent.enabled_tools):
        raise DevelopmentToolDenied("autonomous handoff cannot add tools")
    if not set(request.allowed_paths).issubset(grant.allowed_paths):
        raise DevelopmentToolDenied("autonomous handoff cannot add workspace paths")
    if not set(request.allowed_argv).issubset(grant.allowed_argv):
        raise DevelopmentToolDenied("autonomous handoff cannot add argv")
    if not set(request.allowed_hosts).issubset(grant.allowed_hosts):
        raise DevelopmentToolDenied("autonomous handoff cannot add network hosts")
    if not set(request.allowed_side_effects).issubset(grant.allowed_side_effects):
        raise DevelopmentToolDenied("autonomous handoff cannot add side effects")
    if not set(request.secret_refs).issubset(grant.secret_refs):
        raise DevelopmentToolDenied("autonomous handoff cannot add secret references")
    if request.max_timeout_seconds > parent.max_timeout_seconds:
        raise DevelopmentToolDenied("autonomous handoff cannot increase timeout")
    if request.max_output_bytes > parent.max_output_bytes:
        raise DevelopmentToolDenied("autonomous handoff cannot increase output limit")

    projected_grant = WorkspaceGrant(
        workspace_id=grant.workspace_id,
        agent_role=grant.agent_role,
        workspace_root=grant.workspace_root,
        baseline_commit=grant.baseline_commit,
        grant_revision=grant.grant_revision,
        allowed_paths=request.allowed_paths,
        allowed_argv=request.allowed_argv,
        allowed_hosts=request.allowed_hosts,
        allowed_side_effects=request.allowed_side_effects,
        secret_refs=request.secret_refs,
        created_at=grant.created_at,
    )
    return DevelopmentToolAuthority(
        actor_role=parent.actor_role,
        workspace_grant=projected_grant,
        enabled_tools=request.enabled_tools,
        max_timeout_seconds=request.max_timeout_seconds,
        max_output_bytes=request.max_output_bytes,
        autonomous_handoff=True,
    )


class WorkspaceSearchRequest(_MeteredToolRequest):
    path: str
    file_pattern: str = Field(default="*", min_length=1, max_length=1_000)
    text: str | None = Field(default=None, min_length=1, max_length=20_000)
    case_sensitive: bool = False
    max_results: int = Field(default=200, ge=1, le=5_000)
    max_file_bytes: int = Field(default=1_000_000, ge=1, le=4_000_000)

    @field_validator("path")
    @classmethod
    def path_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class WorkspaceReadRequest(_MeteredToolRequest):
    path: str
    offset_lines: int = Field(default=0, ge=0)
    max_lines: int = Field(default=2_000, ge=1, le=20_000)
    max_bytes: int = Field(default=1_000_000, ge=1, le=4_000_000)

    @field_validator("path")
    @classmethod
    def path_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class WorkspaceWriteRequest(_MeteredToolRequest):
    path: str
    content: str = Field(max_length=1_000_000)

    @field_validator("path")
    @classmethod
    def path_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class WorkspacePatchRequest(_MeteredToolRequest):
    path: str
    old_string: str = Field(min_length=1, max_length=500_000)
    new_string: str = Field(max_length=500_000)
    replace_all: bool = False

    @field_validator("path")
    @classmethod
    def path_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class ProcessRunRequest(_MeteredToolRequest):
    argv: tuple[str, ...] = Field(min_length=1, max_length=256)
    cwd: str
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        le=900,
        allow_inf_nan=False,
    )
    max_output_bytes: int | None = Field(default=None, ge=1, le=4_000_000)
    stdin: str | None = Field(default=None, max_length=1_000_000)

    @field_validator("argv")
    @classmethod
    def argv_is_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_argv(value)

    @field_validator("cwd")
    @classmethod
    def cwd_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class GitStatusRequest(_MeteredToolRequest):
    cwd: str
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        le=120,
        allow_inf_nan=False,
    )
    max_output_bytes: int | None = Field(default=None, ge=1, le=1_000_000)

    @field_validator("cwd")
    @classmethod
    def cwd_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)


class GitDiffRequest(_MeteredToolRequest):
    cwd: str
    paths: tuple[str, ...] = ()
    cached: bool = False
    context_lines: int = Field(default=3, ge=0, le=20)
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        le=120,
        allow_inf_nan=False,
    )
    max_output_bytes: int | None = Field(default=None, ge=1, le=2_000_000)

    @field_validator("cwd")
    @classmethod
    def cwd_is_normalized(cls, value: str) -> str:
        return _normalized_relative_path(value)

    @field_validator("paths")
    @classmethod
    def paths_are_normalized(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique_tuple(value, label="git diff paths")
        return tuple(_normalized_relative_path(item) for item in value)


class SearchMatch(_StrictFrozenModel):
    path: str
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    excerpt: str | None = None


class WorkspaceSearchResult(_StrictFrozenModel):
    matches: tuple[SearchMatch, ...]
    truncated: bool
    skipped_binary_or_large: int = Field(ge=0)


class WorkspaceReadResult(_StrictFrozenModel):
    path: str
    content: str
    first_line: int = Field(ge=1)
    returned_lines: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    truncated: bool


class WorkspaceMutationResult(_StrictFrozenModel):
    path: str
    bytes_written: int = Field(ge=0)
    replacements: int = Field(default=0, ge=0)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ProcessRunResult(_StrictFrozenModel):
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    stdout_truncated: bool
    stderr_truncated: bool
    output_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    inherited_environment: Literal["none"] = "none"
    network_access: Literal["not_granted"] = "not_granted"
    shell_used: Literal[False] = False

    @model_validator(mode="after")
    def timeout_matches_exit_code(self) -> ProcessRunResult:
        if self.timed_out != (self.exit_code is None):
            raise ValueError("timed_out and exit_code disagree")
        return self


_FORBIDDEN_EXECUTABLES = frozenset(
    {
        "ash",
        "bash",
        "cmd",
        "curl",
        "dash",
        "docker",
        "doas",
        "env",
        "fish",
        "ftp",
        "git",
        "kubectl",
        "nc",
        "ncat",
        "netcat",
        "podman",
        "powershell",
        "pwsh",
        "scp",
        "sftp",
        "sh",
        "socat",
        "ssh",
        "sudo",
        "telnet",
        "wget",
        "zsh",
    }
)
_EVAL_FLAGS = frozenset({"-c", "-e", "--eval"})
_NETWORK_SCHEMES = ("http://", "https://", "ftp://", "ssh://", "git://")
_RESERVED_FILE_SEGMENTS = frozenset({".git"})
_SAFE_PATH = os.defpath
_MACOS_SANDBOX = "/usr/bin/sandbox-exec"


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _sanitized_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LILIES_NETWORK_ACCESS": "denied",
        "PATH": _SAFE_PATH,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "TZ": "UTC",
    }


class DevelopmentWorkspaceTools:
    """Execute bounded development tools under one immutable role authority."""

    def __init__(
        self,
        authority: DevelopmentToolAuthority,
        *,
        assignment_id: UUID | str | None = None,
        usage_meter: DevelopmentToolUsageMeter | None = None,
        metering_required: bool = False,
    ) -> None:
        self.authority = authority
        if (assignment_id is None) != (usage_meter is None):
            raise ValueError(
                "assignment_id and usage_meter must be supplied together"
            )
        if metering_required and usage_meter is None:
            raise DevelopmentToolDenied(
                "trusted assignment usage metering is required"
            )
        self.assignment_id = (
            UUID(str(assignment_id)) if assignment_id is not None else None
        )
        self.usage_meter = usage_meter
        self.metering_required = metering_required
        lexical_root = Path(authority.workspace_grant.workspace_root)
        if lexical_root.is_symlink() or not lexical_root.is_dir():
            raise DevelopmentToolDenied(
                "development workspace root must be a non-symlink directory"
            )
        resolved_root = lexical_root.resolve(strict=True)
        if resolved_root != lexical_root:
            raise DevelopmentToolDenied(
                "development workspace root must not traverse symlinks"
            )
        self.root = resolved_root

    @staticmethod
    def _request_digest(
        tool: DevelopmentToolName,
        request: _MeteredToolRequest,
        *,
        workspace_id: UUID,
        grant_revision: int,
    ) -> str:
        payload = request.model_dump(mode="json", exclude={"usage_id"})
        canonical = json.dumps(
            {
                "tool": tool.value,
                "workspace_id": str(workspace_id),
                "grant_revision": grant_revision,
                "request": payload,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _digest(canonical)

    async def _reserve_usage(
        self,
        tool: DevelopmentToolName,
        request: _MeteredToolRequest,
        *,
        command_argv: tuple[str, ...] | None = None,
        command_cwd: str | None = None,
    ) -> str | None:
        meter = self.usage_meter
        assignment_id = self.assignment_id
        if meter is None or assignment_id is None:
            if self.metering_required:
                raise DevelopmentToolDenied(
                    "trusted assignment usage metering is required"
                )
            return None
        usage_id = _validated_usage_id(request.usage_id)
        if usage_id is None:
            raise DevelopmentToolDenied(
                "metered development tool calls require a stable usage_id"
            )
        request_digest = self._request_digest(
            tool,
            request,
            workspace_id=self.authority.workspace_grant.workspace_id,
            grant_revision=self.authority.workspace_grant.grant_revision,
        )
        acquired = await meter.reserve_development_tool_usage(
            assignment_id=assignment_id,
            actor_role=self.authority.actor_role,
            usage_id=usage_id,
            tool_name=tool.value,
            request_digest=request_digest,
            command_argv=command_argv,
            command_cwd=command_cwd,
        )
        if not acquired:
            raise DevelopmentToolUsageReplay(
                "usage_id was already reserved; the operation will not execute twice"
            )
        return request_digest

    async def _complete_usage(
        self,
        request: _MeteredToolRequest,
        *,
        request_digest: str | None,
        response: BaseModel,
        output_digest: str | None = None,
    ) -> None:
        if request_digest is None:
            return
        meter = self.usage_meter
        assignment_id = self.assignment_id
        usage_id = _validated_usage_id(request.usage_id)
        if meter is None or assignment_id is None or usage_id is None:
            raise DevelopmentToolDenied(
                "trusted usage reservation lost its assignment binding"
            )
        canonical = json.dumps(
            response.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        await meter.complete_development_tool_usage(
            assignment_id=assignment_id,
            actor_role=self.authority.actor_role,
            usage_id=usage_id,
            request_digest=request_digest,
            response_digest=_digest(canonical),
            output_digest=output_digest,
        )

    def _require_tool(self, tool: DevelopmentToolName) -> None:
        if tool not in self.authority.enabled_tools:
            raise DevelopmentToolDenied(
                f"{tool.value} is not enabled for {self.authority.actor_role.value}"
            )

    def _granted(self, relative: str) -> bool:
        return any(
            relative == prefix or relative.startswith(f"{prefix}/")
            for prefix in self.authority.workspace_grant.allowed_paths
        )

    def _resolve(
        self,
        requested: str,
        *,
        for_write: bool = False,
        allow_directory: bool = False,
    ) -> Path:
        try:
            relative = _normalized_relative_path(requested)
        except ValueError as error:
            raise DevelopmentToolDenied(str(error)) from error
        if any(
            part.casefold() in _RESERVED_FILE_SEGMENTS
            for part in PurePosixPath(relative).parts
        ):
            raise DevelopmentToolDenied("Git metadata is available only through read-only Git tools")
        if not self._granted(relative):
            raise DevelopmentToolDenied("path is outside the role's allowed_paths grant")

        candidate = self.root.joinpath(*PurePosixPath(relative).parts)
        cursor = self.root
        parts = PurePosixPath(relative).parts
        for index, part in enumerate(parts):
            cursor = cursor / part
            exists = cursor.exists() or cursor.is_symlink()
            if not exists:
                if for_write:
                    break
                raise DevelopmentToolError(f"workspace path does not exist: {relative}")
            metadata = cursor.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise DevelopmentToolDenied("workspace paths must not traverse symlinks")
            if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise DevelopmentToolError(f"workspace parent is not a directory: {relative}")

        resolved = candidate.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise DevelopmentToolDenied("workspace path escapes the granted root")
        if candidate.exists():
            metadata = candidate.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise DevelopmentToolDenied("workspace paths must not be symlinks")
            if allow_directory and not stat.S_ISDIR(metadata.st_mode):
                raise DevelopmentToolError(f"not a directory: {relative}")
            if not allow_directory and not for_write and not stat.S_ISREG(metadata.st_mode):
                raise DevelopmentToolError(f"not a regular file: {relative}")
        return candidate

    def _ensure_parent_directories(self, path: Path) -> None:
        relative_parent = path.parent.relative_to(self.root)
        cursor = self.root
        for part in relative_parent.parts:
            cursor = cursor / part
            if cursor.exists() or cursor.is_symlink():
                metadata = cursor.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise DevelopmentToolDenied(
                        "workspace write parent must be a non-symlink directory"
                    )
            else:
                cursor.mkdir(mode=0o700)

    @staticmethod
    def _safe_read_bytes(path: Path, *, limit: int) -> tuple[bytes, int]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise DevelopmentToolDenied("workspace file is not safely readable") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise DevelopmentToolDenied(
                    "workspace reads require a single-link regular file"
                )
            total = metadata.st_size
            payload = bytearray()
            while len(payload) <= limit:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            return bytes(payload[:limit]), total
        finally:
            os.close(descriptor)

    async def workspace_search(
        self,
        request: WorkspaceSearchRequest,
    ) -> WorkspaceSearchResult:
        self._require_tool(DevelopmentToolName.workspace_search)
        base = self._resolve(request.path, allow_directory=True)
        usage_digest = await self._reserve_usage(
            DevelopmentToolName.workspace_search,
            request,
        )

        def search() -> WorkspaceSearchResult:
            matches: list[SearchMatch] = []
            skipped = 0
            needle = request.text if request.case_sensitive else (request.text or "").casefold()
            truncated = False
            for current, directories, files in os.walk(base, followlinks=False):
                safe_directories: list[str] = []
                for name in sorted(directories):
                    path = Path(current) / name
                    if name.casefold() in _RESERVED_FILE_SEGMENTS or path.is_symlink():
                        continue
                    safe_directories.append(name)
                directories[:] = safe_directories
                for name in sorted(files):
                    path = Path(current) / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    relative = path.relative_to(self.root).as_posix()
                    if not fnmatch.fnmatch(name, request.file_pattern) and not fnmatch.fnmatch(
                        relative, request.file_pattern
                    ):
                        continue
                    if request.text is None:
                        matches.append(SearchMatch(path=relative))
                    else:
                        try:
                            payload, total = self._safe_read_bytes(
                                path,
                                limit=request.max_file_bytes,
                            )
                        except DevelopmentToolDenied:
                            skipped += 1
                            continue
                        if total > request.max_file_bytes or b"\x00" in payload:
                            skipped += 1
                            continue
                        text = payload.decode("utf-8", errors="replace")
                        for line_number, line in enumerate(text.splitlines(), start=1):
                            haystack = line if request.case_sensitive else line.casefold()
                            column = haystack.find(needle)
                            if column >= 0:
                                matches.append(
                                    SearchMatch(
                                        path=relative,
                                        line=line_number,
                                        column=column + 1,
                                        excerpt=line[:2_000],
                                    )
                                )
                                if len(matches) >= request.max_results:
                                    truncated = True
                                    break
                    if len(matches) >= request.max_results:
                        truncated = True
                        break
                if truncated:
                    break
            return WorkspaceSearchResult(
                matches=tuple(matches),
                truncated=truncated,
                skipped_binary_or_large=skipped,
            )

        result = await asyncio.to_thread(search)
        await self._complete_usage(
            request,
            request_digest=usage_digest,
            response=result,
        )
        return result

    async def workspace_read(self, request: WorkspaceReadRequest) -> WorkspaceReadResult:
        self._require_tool(DevelopmentToolName.workspace_read)
        path = self._resolve(request.path)
        usage_digest = await self._reserve_usage(
            DevelopmentToolName.workspace_read,
            request,
        )

        def read() -> WorkspaceReadResult:
            payload, total = self._safe_read_bytes(path, limit=request.max_bytes)
            text = payload.decode("utf-8", errors="replace")
            lines = text.splitlines()
            selected = lines[
                request.offset_lines : request.offset_lines + request.max_lines
            ]
            truncated = (
                total > request.max_bytes
                or request.offset_lines + len(selected) < len(lines)
            )
            return WorkspaceReadResult(
                path=request.path,
                content="\n".join(selected),
                first_line=request.offset_lines + 1,
                returned_lines=len(selected),
                total_bytes=total,
                truncated=truncated,
            )

        result = await asyncio.to_thread(read)
        await self._complete_usage(
            request,
            request_digest=usage_digest,
            response=result,
        )
        return result

    async def workspace_write(
        self,
        request: WorkspaceWriteRequest,
    ) -> WorkspaceMutationResult:
        self._require_tool(DevelopmentToolName.workspace_write)
        path = self._resolve(request.path, for_write=True)
        payload = request.content.encode("utf-8")
        usage_digest = await self._reserve_usage(
            DevelopmentToolName.workspace_write,
            request,
        )

        def write() -> WorkspaceMutationResult:
            self._ensure_parent_directories(path)
            if path.exists() or path.is_symlink():
                metadata = path.stat(follow_symlinks=False)
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                ):
                    raise DevelopmentToolDenied(
                        "workspace writes require a single-link regular target"
                    )
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.lilies-",
                dir=path.parent,
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
            return WorkspaceMutationResult(
                path=request.path,
                bytes_written=len(payload),
                content_digest=_digest(payload),
            )

        result = await asyncio.to_thread(write)
        await self._complete_usage(
            request,
            request_digest=usage_digest,
            response=result,
            output_digest=result.content_digest,
        )
        return result

    async def workspace_patch(
        self,
        request: WorkspacePatchRequest,
    ) -> WorkspaceMutationResult:
        self._require_tool(DevelopmentToolName.workspace_patch)
        path = self._resolve(request.path, for_write=True)
        usage_digest = await self._reserve_usage(
            DevelopmentToolName.workspace_patch,
            request,
        )

        def patch() -> WorkspaceMutationResult:
            payload, total = self._safe_read_bytes(path, limit=1_000_001)
            if total > 1_000_000:
                raise DevelopmentToolError("workspace patch target exceeds 1 MB")
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise DevelopmentToolError("workspace patch target is not UTF-8") from error
            count = text.count(request.old_string)
            if count == 0:
                raise DevelopmentToolError("old_string not found")
            if count > 1 and not request.replace_all:
                raise DevelopmentToolError(f"old_string has {count} matches")
            replacements = count if request.replace_all else 1
            updated = text.replace(
                request.old_string,
                request.new_string,
                -1 if request.replace_all else 1,
            )
            updated_payload = updated.encode("utf-8")
            self._ensure_parent_directories(path)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.lilies-",
                dir=path.parent,
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(updated_payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
            return WorkspaceMutationResult(
                path=request.path,
                bytes_written=len(updated_payload),
                replacements=replacements,
                content_digest=_digest(updated_payload),
            )

        result = await asyncio.to_thread(patch)
        await self._complete_usage(
            request,
            request_digest=usage_digest,
            response=result,
            output_digest=result.content_digest,
        )
        return result

    def _bounded_process_request(
        self,
        *,
        timeout_seconds: float | None,
        max_output_bytes: int | None,
    ) -> tuple[float, int]:
        effective_timeout = (
            self.authority.max_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        effective_output = (
            self.authority.max_output_bytes
            if max_output_bytes is None
            else max_output_bytes
        )
        if effective_timeout > self.authority.max_timeout_seconds:
            raise DevelopmentToolDenied("requested timeout exceeds the role authority")
        if effective_output > self.authority.max_output_bytes:
            raise DevelopmentToolDenied("requested output cap exceeds the role authority")
        return effective_timeout, effective_output

    @staticmethod
    def _deny_unsafe_argv(argv: tuple[str, ...]) -> None:
        executable = Path(argv[0]).name.casefold()
        if executable in _FORBIDDEN_EXECUTABLES:
            raise DevelopmentToolDenied(
                f"command is forbidden by the development tool policy: {executable}"
            )
        if any(item.casefold() in _EVAL_FLAGS for item in argv[1:]):
            raise DevelopmentToolDenied("inline interpreter evaluation is forbidden")
        if any(
            item.casefold().startswith(_NETWORK_SCHEMES)
            for item in argv[1:]
        ):
            raise DevelopmentToolDenied(
                "process_run does not implicitly grant network access"
            )

    def _resolve_workspace_executable(
        self,
        argv: tuple[str, ...],
        cwd: Path,
    ) -> tuple[str, ...]:
        executable = argv[0]
        if "/" not in executable:
            located = shutil.which(executable, path=_SAFE_PATH)
            if located is None:
                raise DevelopmentToolDenied(
                    "executable is unavailable in the sanitized PATH"
                )
            return (located, *argv[1:])
        candidate = Path(executable)
        if candidate.is_absolute():
            try:
                relative = candidate.relative_to(self.root).as_posix()
            except ValueError:
                if not candidate.exists():
                    raise DevelopmentToolDenied("allowed executable does not exist")
                return argv
        else:
            candidate = cwd / candidate
            try:
                relative = candidate.relative_to(self.root).as_posix()
            except ValueError as error:
                raise DevelopmentToolDenied(
                    "relative executable escapes the workspace"
                ) from error
        safe = self._resolve(relative)
        metadata = safe.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise DevelopmentToolDenied(
                "workspace executable must be a non-symlink regular file"
            )
        return (str(safe), *argv[1:])

    def _validate_process_arguments(self, argv: tuple[str, ...], cwd: Path) -> None:
        cwd_relative = cwd.relative_to(self.root)
        for argument in argv[1:]:
            candidate_text = argument
            if argument.startswith("-") and "=" in argument:
                candidate_text = argument.split("=", 1)[1]
            candidate_text = candidate_text.split("::", 1)[0]
            if not candidate_text:
                continue
            candidate_path = PurePosixPath(candidate_text)
            if ".." in candidate_path.parts:
                raise DevelopmentToolDenied(
                    "process arguments cannot escape the workspace"
                )
            if candidate_path.is_absolute():
                candidate = Path(candidate_text)
                try:
                    relative = candidate.relative_to(self.root).as_posix()
                except ValueError as error:
                    raise DevelopmentToolDenied(
                        "absolute process argument is outside the workspace"
                    ) from error
                self._resolve(
                    relative,
                    for_write=not candidate.exists(),
                    allow_directory=candidate.is_dir(),
                )
                continue
            candidate = cwd / candidate_path
            looks_like_path = (
                "/" in candidate_text or candidate.exists() or candidate.is_symlink()
            )
            if not looks_like_path:
                continue
            combined = (
                PurePosixPath(cwd_relative.as_posix()) / candidate_path
            ).as_posix()
            self._resolve(
                combined,
                for_write=not candidate.exists(),
                allow_directory=candidate.is_dir(),
            )

    def _validate_git_repository(
        self,
        cwd: Path,
    ) -> tuple[Path, Path, Path]:
        cursor = cwd
        marker: Path | None = None
        repository_root: Path | None = None
        while True:
            candidate = cursor / ".git"
            if candidate.exists() or candidate.is_symlink():
                marker = candidate
                repository_root = cursor
                break
            if cursor == self.root:
                break
            cursor = cursor.parent
            if cursor != self.root and self.root not in cursor.parents:
                break
        if marker is None:
            raise DevelopmentToolDenied(
                "Git tools require repository metadata inside the granted workspace"
            )
        assert repository_root is not None
        if marker.is_symlink():
            raise DevelopmentToolDenied("Git metadata must not be a symlink")
        metadata = marker.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            git_directory = marker
        elif stat.S_ISREG(metadata.st_mode):
            payload, total = self._safe_read_bytes(marker, limit=4_096)
            if total > 4_096:
                raise DevelopmentToolDenied("Git indirection file is too large")
            line = payload.decode("utf-8", errors="strict").strip()
            if not line.startswith("gitdir:"):
                raise DevelopmentToolDenied("Git metadata indirection is invalid")
            target = Path(line.removeprefix("gitdir:").strip())
            git_directory = target if target.is_absolute() else marker.parent / target
        else:
            raise DevelopmentToolDenied(
                "Git metadata is not a directory or indirection file"
            )
        resolved_git = git_directory.resolve(strict=True)
        if resolved_git != self.root and self.root not in resolved_git.parents:
            raise DevelopmentToolDenied("Git metadata escapes the granted workspace")
        if git_directory.is_symlink() or not resolved_git.is_dir():
            raise DevelopmentToolDenied("Git metadata must be a non-symlink directory")
        entries = 0
        for current, directories, files in os.walk(resolved_git, followlinks=False):
            for name in [*directories, *files]:
                entries += 1
                if entries > 100_000:
                    raise DevelopmentToolDenied(
                        "Git metadata exceeds the safety scan limit"
                    )
                if (Path(current) / name).is_symlink():
                    raise DevelopmentToolDenied("Git metadata must not contain symlinks")
        return repository_root, marker, resolved_git

    def _granted_git_pathspecs(self, repository_root: Path) -> tuple[str, ...]:
        """Project the workspace grant into paths relative to one Git root."""

        repository_relative = repository_root.relative_to(self.root)
        repository_parts = repository_relative.parts
        pathspecs: set[str] = set()
        for granted in self.authority.workspace_grant.allowed_paths:
            granted_path = PurePosixPath(granted)
            granted_parts = granted_path.parts
            if repository_parts == ():
                pathspecs.add(granted_path.as_posix())
            elif granted_parts[: len(repository_parts)] == repository_parts:
                relative_parts = granted_parts[len(repository_parts) :]
                pathspecs.add(
                    PurePosixPath(*relative_parts).as_posix()
                    if relative_parts
                    else "."
                )
            elif repository_parts[: len(granted_parts)] == granted_parts:
                # The entire nested repository is below an already granted
                # workspace directory.
                pathspecs.add(".")
        if not pathspecs:
            raise DevelopmentToolDenied(
                "Git repository does not intersect the granted workspace paths"
            )
        if "." in pathspecs:
            return (".",)
        return tuple(sorted(pathspecs))

    def _requested_git_pathspecs(
        self,
        *,
        request_cwd: str,
        requested_paths: tuple[str, ...],
        repository_root: Path,
    ) -> tuple[str, ...]:
        if not requested_paths:
            return self._granted_git_pathspecs(repository_root)
        resolved: list[str] = []
        for requested in requested_paths:
            combined = PurePosixPath(request_cwd) / PurePosixPath(requested)
            normalized = combined.as_posix()
            absolute = self._resolve(normalized, for_write=True)
            try:
                relative = absolute.relative_to(repository_root).as_posix()
            except ValueError as error:
                raise DevelopmentToolDenied(
                    "Git path is outside the selected repository"
                ) from error
            resolved.append(relative)
        return tuple(dict.fromkeys(resolved))

    async def _run_process(
        self,
        *,
        receipt_argv: tuple[str, ...],
        execution_argv: tuple[str, ...],
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
        stdin: str | None,
        read_entire_workspace: bool = False,
        additional_read_paths: tuple[Path, ...] = (),
    ) -> ProcessRunResult:
        sandboxed_argv = self._sandboxed_argv(
            execution_argv,
            read_entire_workspace=read_entire_workspace,
            additional_read_paths=additional_read_paths,
        )
        process = await asyncio.create_subprocess_exec(
            *sandboxed_argv,
            cwd=cwd,
            env=_sanitized_environment(),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def read_stream(
            stream: asyncio.StreamReader | None,
        ) -> tuple[bytes, int, bool]:
            if stream is None:
                return b"", 0, False
            captured = bytearray()
            total = 0
            while chunk := await stream.read(64 * 1024):
                total += len(chunk)
                remaining = max_output_bytes - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
            return bytes(captured), total, total > len(captured)

        stdout_task = asyncio.create_task(read_stream(process.stdout))
        stderr_task = asyncio.create_task(read_stream(process.stderr))
        if stdin is not None and process.stdin is not None:
            process.stdin.write(stdin.encode("utf-8"))
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            process.stdin.close()
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            await process.wait()
        stdout_payload, stdout_bytes, stdout_truncated = await stdout_task
        stderr_payload, stderr_bytes, stderr_truncated = await stderr_task
        if len(stdout_payload) + len(stderr_payload) > max_output_bytes:
            stdout_payload = stdout_payload[:max_output_bytes]
            remaining = max_output_bytes - len(stdout_payload)
            stderr_payload = stderr_payload[:remaining]
            stdout_truncated = stdout_truncated or stdout_bytes > len(stdout_payload)
            stderr_truncated = stderr_truncated or stderr_bytes > len(stderr_payload)
        digest_payload = (
            stdout_payload
            + b"\x00"
            + stderr_payload
            + b"\x00"
            + str(process.returncode).encode("ascii")
        )
        return ProcessRunResult(
            argv=receipt_argv,
            cwd=cwd.relative_to(self.root).as_posix(),
            exit_code=None if timed_out else process.returncode,
            timed_out=timed_out,
            stdout=stdout_payload.decode("utf-8", errors="replace"),
            stderr=stderr_payload.decode("utf-8", errors="replace"),
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            output_digest=_digest(digest_payload),
        )

    @staticmethod
    def _sandbox_literal(path: Path | str) -> str:
        return json.dumps(str(path), ensure_ascii=True)

    def _trusted_runtime_roots(
        self,
        execution_argv: tuple[str, ...],
    ) -> tuple[Path, ...]:
        lexical_executable = Path(execution_argv[0])
        executable = lexical_executable.resolve(strict=True)
        candidates = {
            Path("/System"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/Library/Apple"),
            Path("/Library/Developer/CommandLineTools"),
            Path("/private/var/select"),
            Path("/var/select"),
            lexical_executable,
            lexical_executable.parent,
            executable,
            executable.parent,
        }
        base_executable = getattr(sys, "_base_executable", None)
        if isinstance(base_executable, str) and base_executable:
            lexical_base = Path(base_executable)
            candidates.add(lexical_base)
            candidates.add(lexical_base.parent)
            try:
                resolved_base = lexical_base.resolve(strict=True)
            except OSError:
                pass
            else:
                candidates.add(resolved_base)
                candidates.add(resolved_base.parent)
        for prefix in (Path(sys.prefix), Path(sys.base_prefix)):
            try:
                resolved = prefix.resolve(strict=True)
            except OSError:
                continue
            candidates.add(resolved)
        for package_root in (Path("/opt/homebrew"), Path("/usr/local")):
            if executable == package_root or package_root in executable.parents:
                candidates.add(package_root)
        return tuple(sorted(candidates, key=lambda item: str(item)))

    def _sandboxed_argv(
        self,
        execution_argv: tuple[str, ...],
        *,
        read_entire_workspace: bool,
        additional_read_paths: tuple[Path, ...] = (),
    ) -> tuple[str, ...]:
        """Wrap an exact argv in a fail-closed macOS Seatbelt profile.

        Literal argv validation prevents shell injection, while Seatbelt
        constrains behavior inside an otherwise trusted executable.  Network
        and child-process creation remain denied; integrations requiring an
        authorized host must use a connector that can enforce that exact host.
        """

        sandbox = Path(_MACOS_SANDBOX)
        if not sandbox.is_file() or not os.access(sandbox, os.X_OK):
            raise DevelopmentToolDenied(
                "process_run requires an available operating-system sandbox"
            )

        read_paths: set[Path] = set(self._trusted_runtime_roots(execution_argv))
        if read_entire_workspace:
            read_paths.add(self.root)
        else:
            read_paths.update(
                self.root.joinpath(*PurePosixPath(path).parts)
                for path in self.authority.workspace_grant.allowed_paths
            )
        read_paths.update(additional_read_paths)

        write_paths: set[Path] = set()
        if (
            not read_entire_workspace
            and SideEffect.workspace_write
            in self.authority.workspace_grant.allowed_side_effects
        ):
            write_paths.update(
                self.root.joinpath(*PurePosixPath(path).parts)
                for path in self.authority.workspace_grant.allowed_paths
            )

        read_filters = "\n".join(
            f"  (subpath {self._sandbox_literal(path)})"
            for path in sorted(read_paths, key=lambda item: str(item))
        )
        write_filters = "\n".join(
            f"  (subpath {self._sandbox_literal(path)})"
            for path in sorted(write_paths, key=lambda item: str(item))
        )
        metadata_paths: set[Path] = {Path("/")}
        for path in read_paths | write_paths:
            metadata_paths.add(path)
            metadata_paths.update(path.parents)
        metadata_filters = "\n".join(
            f"  (literal {self._sandbox_literal(path)})"
            for path in sorted(metadata_paths, key=lambda item: str(item))
        )
        write_rule = (
            f"(allow file-write*\n{write_filters}\n  (literal \"/dev/null\"))"
            if write_filters
            else '(allow file-write* (literal "/dev/null"))'
        )
        profile = "\n".join(
            (
                "(version 1)",
                "(deny default)",
                "(allow process-exec)",
                "(allow process-info*)",
                "(allow signal (target self))",
                "(allow sysctl-read)",
                "(allow mach-lookup)",
                "(allow ipc-posix-shm-read*)",
                "(allow file-read-metadata",
                metadata_filters,
                ")",
                "(allow file-read*",
                read_filters,
                '  (literal "/")',
                f"  (literal {self._sandbox_literal(self.root)})",
                '  (literal "/dev/null")',
                '  (literal "/dev/urandom")',
                '  (literal "/private/var/db/timezone/zoneinfo/UTC"))',
                write_rule,
            )
        )
        return (str(sandbox), "-p", profile, "--", *execution_argv)

    async def process_run(self, request: ProcessRunRequest) -> ProcessRunResult:
        self._require_tool(DevelopmentToolName.process_run)
        timeout_seconds, max_output_bytes = self._bounded_process_request(
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
        )
        argv = tuple(request.argv)
        if argv not in self.authority.workspace_grant.allowed_argv:
            raise DevelopmentToolDenied("argv is not present in the exact command allowlist")
        self._deny_unsafe_argv(argv)
        cwd = self._resolve(request.cwd, allow_directory=True)
        self._validate_process_arguments(argv, cwd)
        execution_argv = self._resolve_workspace_executable(argv, cwd)
        usage_digest = await self._reserve_usage(
            DevelopmentToolName.process_run,
            request,
            command_argv=argv,
            command_cwd=request.cwd,
        )
        result = await self._run_process(
            receipt_argv=argv,
            execution_argv=execution_argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            stdin=request.stdin,
            read_entire_workspace=False,
        )
        await self._complete_usage(
            request,
            request_digest=usage_digest,
            response=result,
            output_digest=result.output_digest,
        )
        return result

    async def git_status(self, request: GitStatusRequest) -> ProcessRunResult:
        self._require_tool(DevelopmentToolName.git_status)
        timeout_seconds, max_output_bytes = self._bounded_process_request(
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
        )
        cwd = self._resolve(request.cwd, allow_directory=True)
        repository_root, marker, git_directory = self._validate_git_repository(
            cwd
        )
        pathspecs = self._granted_git_pathspecs(repository_root)
        git = shutil.which("git", path=_SAFE_PATH)
        if git is None:
            raise DevelopmentToolError("git is unavailable in the sanitized PATH")
        receipt_argv = (
            "git",
            "status",
            "--short",
            "--branch",
            "--untracked-files=all",
            "--",
            *pathspecs,
        )
        argv = (
            git,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(repository_root),
            "status",
            "--short",
            "--branch",
            "--untracked-files=all",
            "--",
            *pathspecs,
        )
        usage_digest = await self._reserve_usage(
            DevelopmentToolName.git_status,
            request,
            command_argv=receipt_argv,
            command_cwd=request.cwd,
        )
        result = await self._run_process(
            receipt_argv=receipt_argv,
            execution_argv=argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            stdin=None,
            read_entire_workspace=False,
            additional_read_paths=(marker, git_directory),
        )
        await self._complete_usage(
            request,
            request_digest=usage_digest,
            response=result,
            output_digest=result.output_digest,
        )
        return result

    async def git_diff(self, request: GitDiffRequest) -> ProcessRunResult:
        self._require_tool(DevelopmentToolName.git_diff)
        timeout_seconds, max_output_bytes = self._bounded_process_request(
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
        )
        cwd = self._resolve(request.cwd, allow_directory=True)
        repository_root, marker, git_directory = self._validate_git_repository(
            cwd
        )
        resolved_paths = self._requested_git_pathspecs(
            request_cwd=request.cwd,
            requested_paths=request.paths,
            repository_root=repository_root,
        )
        git = shutil.which("git", path=_SAFE_PATH)
        if git is None:
            raise DevelopmentToolError("git is unavailable in the sanitized PATH")
        argv = [
            git,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(repository_root),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            f"--unified={request.context_lines}",
        ]
        if request.cached:
            argv.append("--cached")
        argv.append("--")
        argv.extend(resolved_paths)
        receipt = ["git", "diff", f"--unified={request.context_lines}"]
        if request.cached:
            receipt.append("--cached")
        receipt.extend(["--", *resolved_paths])
        receipt_argv = tuple(receipt)
        usage_digest = await self._reserve_usage(
            DevelopmentToolName.git_diff,
            request,
            command_argv=receipt_argv,
            command_cwd=request.cwd,
        )
        result = await self._run_process(
            receipt_argv=receipt_argv,
            execution_argv=tuple(argv),
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            stdin=None,
            read_entire_workspace=False,
            additional_read_paths=(marker, git_directory),
        )
        await self._complete_usage(
            request,
            request_digest=usage_digest,
            response=result,
            output_digest=result.output_digest,
        )
        return result


__all__ = [
    "AutonomousHandoffAuthorityRequest",
    "DevelopmentToolAuthority",
    "DevelopmentToolDenied",
    "DevelopmentToolError",
    "DevelopmentToolName",
    "DevelopmentToolUsageMeter",
    "DevelopmentToolUsageReplay",
    "DevelopmentWorkspaceTools",
    "GitDiffRequest",
    "GitStatusRequest",
    "ProcessRunRequest",
    "ProcessRunResult",
    "SearchMatch",
    "WorkspaceMutationResult",
    "WorkspacePatchRequest",
    "WorkspaceReadRequest",
    "WorkspaceReadResult",
    "WorkspaceSearchRequest",
    "WorkspaceSearchResult",
    "WorkspaceWriteRequest",
    "derive_autonomous_handoff_authority",
]
