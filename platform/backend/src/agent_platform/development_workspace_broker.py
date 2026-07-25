"""Provision isolated Git workspaces for collaborative development assignments.

The broker is deliberately platform neutral: it knows about a source Git
repository and role-scoped workspace grants, not workflow applications,
Builder sessions, task packages, or hidden verification state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .collaborative_development_models import (
    AgentRole,
    DevelopmentResult,
    SideEffect,
    WorkspaceGrant,
    utc_now,
)


_WORKSPACE_NAMESPACE = UUID("fd2b3891-36d4-4f5d-900b-185968af8108")
_REVIEW_SNAPSHOT_NAMESPACE = UUID("82054dd8-b9d3-41fd-9b84-44edbf880050")


class DevelopmentWorkspaceError(RuntimeError):
    """A safe workspace-provisioning failure."""


class DevelopmentWorkspaceSpec(BaseModel):
    """The exact authority to materialize for one agent role."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_role: AgentRole
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    allowed_argv: tuple[tuple[str, ...], ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    allowed_side_effects: tuple[SideEffect, ...] = ()
    secret_refs: tuple[str, ...] = ()


class PreparedDevelopmentWorkspaces(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    source_repository: str
    baseline_commit: str
    grants: tuple[WorkspaceGrant, WorkspaceGrant]
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manifest_path: str

    @model_validator(mode="after")
    def contains_both_roles(self) -> PreparedDevelopmentWorkspaces:
        if {grant.agent_role for grant in self.grants} != {
            AgentRole.lilies,
            AgentRole.codex,
        }:
            raise ValueError("prepared workspaces must contain Lilies and Codex grants")
        return self


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_digest(payload: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(payload)).hexdigest()}"


class DevelopmentReviewSnapshotReceipt(BaseModel):
    """Tamper-evident binding for one Codex result reviewed by Lilies.

    The receipt proves only that an independent review snapshot was
    materialized.  It deliberately contains no operation that can merge,
    deploy, or otherwise promote the result into the user's source repository.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    receipt_id: UUID
    review_snapshot_id: UUID
    assignment_id: UUID
    result_id: UUID
    work_item_id: UUID
    lease_id: UUID
    source_role: Literal[AgentRole.codex] = AgentRole.codex
    reviewer_role: Literal[AgentRole.lilies] = AgentRole.lilies
    source_workspace_id: UUID
    reviewer_workspace_id: UUID
    baseline_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    diff_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_repository_state_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    changed_paths: tuple[str, ...] = ()
    review_workspace_root: str = Field(min_length=1, max_length=4_096)
    promotion_state: Literal["review_snapshot_only"] = "review_snapshot_only"
    source_repository_unchanged: Literal[True] = True
    created_at: datetime
    receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("changed_paths")
    @classmethod
    def changed_paths_are_normalized(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("changed paths must not contain duplicates")
        if tuple(sorted(value)) != value:
            raise ValueError("changed paths must be sorted")
        for item in value:
            path = PurePosixPath(item)
            if (
                not item
                or "\x00" in item
                or "\\" in item
                or path.is_absolute()
                or ".." in path.parts
                or str(path) != item
            ):
                raise ValueError("changed paths must be normalized relative POSIX paths")
        return value

    @field_validator("review_workspace_root")
    @classmethod
    def review_root_is_absolute_and_normalized(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or str(path) != value.rstrip("/") or value == "/":
            raise ValueError("review workspace root must be normalized and absolute")
        return value

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        if (
            value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset().total_seconds() != 0
        ):
            raise ValueError("created_at must use UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def receipt_digest_matches_content(self) -> DevelopmentReviewSnapshotReceipt:
        unsigned = self.model_dump(mode="json", exclude={"receipt_digest"})
        expected = _canonical_digest(unsigned)
        if not hmac.compare_digest(self.receipt_digest, expected):
            raise ValueError("review snapshot receipt digest does not match content")
        return self

    @classmethod
    def issue(cls, **payload: object) -> DevelopmentReviewSnapshotReceipt:
        provisional = cls.model_construct(
            **payload,
            receipt_digest="sha256:" + ("0" * 64),
        )
        unsigned = provisional.model_dump(mode="json", exclude={"receipt_digest"})
        return cls.model_validate(
            {
                **unsigned,
                "receipt_digest": _canonical_digest(unsigned),
            }
        )


class DevelopmentWorkspaceBroker:
    """Create two independent, content-addressed clones from one Git baseline."""

    def __init__(self, state_root: Path) -> None:
        resolved = state_root.expanduser().resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("development workspace state root cannot be a filesystem root")
        self.state_root = resolved

    @staticmethod
    def _run_git(
        *arguments: str,
        cwd: Path | None = None,
        timeout_seconds: float = 60.0,
    ) -> str:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                },
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DevelopmentWorkspaceError("Git workspace operation failed") from error
        if result.returncode != 0:
            raise DevelopmentWorkspaceError(
                f"Git workspace operation failed with exit code {result.returncode}"
            )
        return result.stdout.strip()

    @staticmethod
    def _run_git_bytes(
        *arguments: str,
        cwd: Path | None = None,
        timeout_seconds: float = 60.0,
    ) -> bytes:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", ""),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                },
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DevelopmentWorkspaceError("Git workspace operation failed") from error
        if result.returncode != 0:
            raise DevelopmentWorkspaceError(
                f"Git workspace operation failed with exit code {result.returncode}"
            )
        return result.stdout

    @staticmethod
    def _safe_relative_path(raw: bytes) -> str:
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DevelopmentWorkspaceError(
                "development workspace contains a non-UTF-8 path"
            ) from error
        path = PurePosixPath(value)
        if (
            not value
            or "\x00" in value
            or "\\" in value
            or path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
            or str(path) != value
        ):
            raise DevelopmentWorkspaceError("development workspace contains an unsafe path")
        return value

    @classmethod
    def _nul_paths(cls, payload: bytes) -> set[str]:
        if not payload:
            return set()
        if not payload.endswith(b"\x00"):
            raise DevelopmentWorkspaceError("Git returned an invalid path list")
        return {cls._safe_relative_path(raw) for raw in payload[:-1].split(b"\x00")}

    @staticmethod
    def _verify_workspace_root(workspace: Path) -> Path:
        if workspace.is_symlink() or not workspace.is_dir():
            raise DevelopmentWorkspaceError("development workspace must be a non-symlink directory")
        try:
            resolved = workspace.resolve(strict=True)
        except OSError as error:
            raise DevelopmentWorkspaceError(
                "development workspace is not safely resolvable"
            ) from error
        if resolved != workspace:
            raise DevelopmentWorkspaceError("development workspace root must not traverse symlinks")
        git_metadata = workspace / ".git"
        if git_metadata.is_symlink() or not git_metadata.is_dir():
            raise DevelopmentWorkspaceError("development workspace requires private Git metadata")
        return resolved

    @classmethod
    def _worktree_paths(cls, workspace: Path) -> set[str]:
        """Return every regular worktree file, rejecting link-based escapes."""

        paths: set[str] = set()
        for current, directories, files in os.walk(
            workspace,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in sorted(directories):
                if current_path == workspace and name == ".git":
                    continue
                candidate = current_path / name
                try:
                    metadata = candidate.stat(follow_symlinks=False)
                except OSError as error:
                    raise DevelopmentWorkspaceError(
                        "development workspace directory is not safely inspectable"
                    ) from error
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise DevelopmentWorkspaceError(
                        "development workspace must not contain symlink directories"
                    )
                relative = candidate.relative_to(workspace).as_posix()
                cls._safe_relative_path(relative.encode("utf-8"))
                safe_directories.append(name)
            directories[:] = safe_directories

            for name in sorted(files):
                candidate = current_path / name
                relative = candidate.relative_to(workspace).as_posix()
                cls._safe_relative_path(relative.encode("utf-8"))
                try:
                    metadata = candidate.stat(follow_symlinks=False)
                except OSError as error:
                    raise DevelopmentWorkspaceError(
                        "development workspace file is not safely inspectable"
                    ) from error
                if stat.S_ISLNK(metadata.st_mode):
                    raise DevelopmentWorkspaceError(
                        "development workspace must not contain symlink files"
                    )
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise DevelopmentWorkspaceError(
                        "development workspace files must be single-link regular files"
                    )
                paths.add(relative)
        return paths

    @staticmethod
    def _safe_file_fingerprint(path: Path) -> tuple[str, int, str]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise DevelopmentWorkspaceError(
                "development workspace file is not safely readable"
            ) from error
        digest = hashlib.sha256()
        size = 0
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise DevelopmentWorkspaceError(
                    "development workspace files must be single-link regular files"
                )
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
        finally:
            os.close(descriptor)
        return f"sha256:{digest.hexdigest()}", size, mode

    @classmethod
    def _workspace_changes(
        cls,
        *,
        workspace: Path,
        baseline_commit: str,
    ) -> tuple[dict[str, object], ...]:
        workspace = cls._verify_workspace_root(workspace)
        resolved_baseline = cls._run_git(
            "-C",
            str(workspace),
            "rev-parse",
            "--verify",
            f"{baseline_commit}^{{commit}}",
        )
        if resolved_baseline != baseline_commit:
            raise DevelopmentWorkspaceError("workspace does not contain the frozen baseline commit")
        baseline_paths = cls._nul_paths(
            cls._run_git_bytes(
                "-C",
                str(workspace),
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                baseline_commit,
            )
        )
        current_paths = cls._worktree_paths(workspace)
        tracked_changes = cls._nul_paths(
            cls._run_git_bytes(
                "-C",
                str(workspace),
                "diff",
                "--name-only",
                "-z",
                "--no-ext-diff",
                "--no-textconv",
                baseline_commit,
                "--",
            )
        )
        changed_paths = sorted(
            tracked_changes | (baseline_paths - current_paths) | (current_paths - baseline_paths)
        )
        changes: list[dict[str, object]] = []
        for relative in changed_paths:
            if relative not in current_paths:
                changes.append({"operation": "delete", "path": relative})
                continue
            content_digest, size, mode = cls._safe_file_fingerprint(
                workspace.joinpath(*PurePosixPath(relative).parts)
            )
            changes.append(
                {
                    "content_digest": content_digest,
                    "mode": mode,
                    "operation": "upsert",
                    "path": relative,
                    "size": size,
                }
            )
        return tuple(changes)

    @staticmethod
    def _diff_digest(
        baseline_commit: str,
        changes: tuple[dict[str, object], ...],
    ) -> str:
        return _canonical_digest(
            {
                "schema_version": "1.0",
                "baseline_commit": baseline_commit,
                "changes": changes,
            }
        )

    @classmethod
    def calculate_diff_digest(
        cls,
        *,
        workspace_root: Path,
        baseline_commit: str,
    ) -> str:
        """Calculate the canonical review diff digest for a result."""

        changes = cls._workspace_changes(
            workspace=workspace_root,
            baseline_commit=baseline_commit,
        )
        return cls._diff_digest(baseline_commit, changes)

    @classmethod
    def resolve_baseline(cls, source_repository: Path, revision: str) -> str:
        source = source_repository.expanduser().resolve()
        if not (source / ".git").exists():
            raise DevelopmentWorkspaceError("source repository is not a Git work tree")
        resolved = cls._run_git(
            "-C",
            str(source),
            "rev-parse",
            "--verify",
            f"{revision}^{{commit}}",
        )
        if not resolved or len(resolved) != 40:
            raise DevelopmentWorkspaceError("baseline did not resolve to a full Git commit")
        return resolved

    @staticmethod
    def _workspace_id(assignment_id: UUID, role: AgentRole) -> UUID:
        return uuid5(_WORKSPACE_NAMESPACE, f"{assignment_id}:{role.value}")

    @staticmethod
    def _manifest_bytes(payload: dict[str, object]) -> bytes:
        return _canonical_bytes(payload)

    @staticmethod
    def _safe_assignment_root(state_root: Path, assignment_id: UUID) -> Path:
        target = (state_root / str(assignment_id)).resolve()
        if target.parent != state_root:
            raise DevelopmentWorkspaceError("assignment workspace escaped the state root")
        return target

    @staticmethod
    def _verify_existing_clone(workspace: Path, baseline_commit: str) -> None:
        if workspace.is_symlink() or not (workspace / ".git").exists():
            raise DevelopmentWorkspaceError(
                "existing development workspace is not a safe Git clone"
            )
        actual = DevelopmentWorkspaceBroker._run_git(
            "-C",
            str(workspace),
            "rev-parse",
            "HEAD",
        )
        if actual != baseline_commit:
            raise DevelopmentWorkspaceError(
                "existing development workspace has a different baseline"
            )

    @staticmethod
    def _grant_for(
        prepared: PreparedDevelopmentWorkspaces,
        role: AgentRole,
    ) -> WorkspaceGrant:
        matches = [grant for grant in prepared.grants if grant.agent_role == role]
        if len(matches) != 1:
            raise DevelopmentWorkspaceError(
                "prepared workspace authority does not contain exactly one role grant"
            )
        return matches[0]

    def _verify_prepared_manifest(
        self,
        prepared: PreparedDevelopmentWorkspaces,
    ) -> Path:
        assignment_root = self._safe_assignment_root(
            self.state_root,
            prepared.assignment_id,
        )
        manifest_path = assignment_root / "workspace-manifest.json"
        if Path(prepared.manifest_path) != manifest_path:
            raise DevelopmentWorkspaceError(
                "prepared workspace manifest path is outside the assignment"
            )
        try:
            metadata = manifest_path.stat(follow_symlinks=False)
            encoded = manifest_path.read_bytes()
        except OSError as error:
            raise DevelopmentWorkspaceError("prepared workspace manifest is unavailable") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise DevelopmentWorkspaceError(
                "prepared workspace manifest is not a single-link regular file"
            )
        actual_digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        if not hmac.compare_digest(actual_digest, prepared.manifest_digest):
            raise DevelopmentWorkspaceError(
                "prepared workspace manifest digest does not match its bytes"
            )
        expected_manifest = {
            "schema_version": "1.0",
            "assignment_id": str(prepared.assignment_id),
            "source_repository": prepared.source_repository,
            "baseline_commit": prepared.baseline_commit,
            "grants": [
                grant.model_dump(mode="json", exclude_none=True) for grant in prepared.grants
            ],
        }
        if encoded != self._manifest_bytes(expected_manifest):
            raise DevelopmentWorkspaceError(
                "prepared workspace authority differs from its frozen manifest"
            )
        source = Path(prepared.source_repository)
        if not source.is_absolute() or source.is_symlink() or not source.is_dir():
            raise DevelopmentWorkspaceError(
                "prepared source repository is not a safe absolute directory"
            )
        if assignment_root == source or assignment_root in source.parents:
            raise DevelopmentWorkspaceError(
                "review broker state must not contain the source repository"
            )
        if source in assignment_root.parents:
            raise DevelopmentWorkspaceError(
                "review broker state must not be written inside the source repository"
            )
        return assignment_root

    @classmethod
    def _source_repository_state_digest(cls, source_repository: Path) -> str:
        head = cls._run_git(
            "-C",
            str(source_repository),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        )
        status = cls._run_git_bytes(
            "-C",
            str(source_repository),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        return _canonical_digest(
            {
                "head": head,
                "status_digest": (f"sha256:{hashlib.sha256(status).hexdigest()}"),
            }
        )

    @staticmethod
    def _path_is_granted(relative: str, allowed_paths: tuple[str, ...]) -> bool:
        return any(
            relative == prefix or relative.startswith(f"{prefix}/") for prefix in allowed_paths
        )

    @classmethod
    def _verify_result_baseline(
        cls,
        *,
        workspace: Path,
        baseline_commit: str,
        result: DevelopmentResult,
    ) -> None:
        if result.baseline_commit != baseline_commit:
            raise DevelopmentWorkspaceError("development result does not bind the frozen baseline")
        head = cls._run_git(
            "-C",
            str(workspace),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        )
        if result.commit_sha is None:
            if head != baseline_commit:
                raise DevelopmentWorkspaceError(
                    "uncommitted result workspace HEAD moved away from the baseline"
                )
            return
        result_commit = cls._run_git(
            "-C",
            str(workspace),
            "rev-parse",
            "--verify",
            f"{result.commit_sha}^{{commit}}",
        )
        if result_commit != head:
            raise DevelopmentWorkspaceError(
                "development result commit does not match source workspace HEAD"
            )
        merge_base = cls._run_git(
            "-C",
            str(workspace),
            "merge-base",
            baseline_commit,
            result_commit,
        )
        if merge_base != baseline_commit:
            raise DevelopmentWorkspaceError(
                "development result commit is not based on the frozen baseline"
            )

    @staticmethod
    def _ensure_safe_parent(root: Path, target: Path) -> None:
        try:
            relative = target.relative_to(root)
        except ValueError as error:
            raise DevelopmentWorkspaceError("review snapshot target escaped its root") from error
        cursor = root
        for part in relative.parts:
            cursor /= part
            if cursor.exists() or cursor.is_symlink():
                metadata = cursor.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise DevelopmentWorkspaceError(
                        "review snapshot parent must be a non-symlink directory"
                    )
            else:
                cursor.mkdir(mode=0o700)

    @classmethod
    def _copy_single_link_file(
        cls,
        *,
        source: Path,
        target: Path,
        mode: str,
    ) -> None:
        source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        temporary = target.parent / f".lilies-review-{uuid4().hex}"
        source_descriptor: int | None = None
        target_descriptor: int | None = None
        try:
            source_descriptor = os.open(source, source_flags)
            source_metadata = os.fstat(source_descriptor)
            if not stat.S_ISREG(source_metadata.st_mode) or source_metadata.st_nlink != 1:
                raise DevelopmentWorkspaceError("review source must be a single-link regular file")
            target_descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            while True:
                chunk = os.read(source_descriptor, 64 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(target_descriptor, view)
                    view = view[written:]
            os.fsync(target_descriptor)
            os.fchmod(target_descriptor, 0o755 if mode == "100755" else 0o644)
            os.close(target_descriptor)
            target_descriptor = None
            os.replace(temporary, target)
        except OSError as error:
            raise DevelopmentWorkspaceError(
                "review snapshot file could not be copied safely"
            ) from error
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)
            if target_descriptor is not None:
                os.close(target_descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def _apply_changes_to_snapshot(
        cls,
        *,
        source_workspace: Path,
        snapshot: Path,
        changes: tuple[dict[str, object], ...],
    ) -> None:
        for change in changes:
            relative = str(change["path"])
            parts = PurePosixPath(relative).parts
            target = snapshot.joinpath(*parts)
            cls._ensure_safe_parent(snapshot, target.parent)
            if change["operation"] == "delete":
                if target.exists() or target.is_symlink():
                    metadata = target.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        raise DevelopmentWorkspaceError(
                            "review snapshot cannot delete a directory as a file"
                        )
                    target.unlink()
                continue
            source = source_workspace.joinpath(*parts)
            cls._copy_single_link_file(
                source=source,
                target=target,
                mode=str(change["mode"]),
            )

    @staticmethod
    def _snapshot_digest(baseline_commit: str, diff_digest: str) -> str:
        return _canonical_digest(
            {
                "schema_version": "1.0",
                "baseline_commit": baseline_commit,
                "diff_digest": diff_digest,
            }
        )

    def prepare(
        self,
        *,
        source_repository: Path,
        assignment_id: UUID,
        baseline_revision: str,
        specs: tuple[DevelopmentWorkspaceSpec, DevelopmentWorkspaceSpec],
    ) -> PreparedDevelopmentWorkspaces:
        """Materialize or verify both role workspaces idempotently."""

        if {spec.agent_role for spec in specs} != {AgentRole.lilies, AgentRole.codex}:
            raise DevelopmentWorkspaceError(
                "workspace specifications must contain Lilies and Codex exactly once"
            )
        source = source_repository.expanduser().resolve()
        baseline_commit = self.resolve_baseline(source, baseline_revision)
        assignment_root = self._safe_assignment_root(self.state_root, assignment_id)
        assignment_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest_path = assignment_root / "workspace-manifest.json"
        persisted_created_at: dict[AgentRole, object] = {}
        if manifest_path.exists():
            try:
                persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                persisted_created_at = {
                    AgentRole(str(item["agent_role"])): item["created_at"]
                    for item in persisted_manifest.get("grants", [])
                }
            except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
                raise DevelopmentWorkspaceError("existing workspace manifest is invalid") from error

        grants: list[WorkspaceGrant] = []
        for spec in sorted(specs, key=lambda item: item.agent_role.value):
            workspace = assignment_root / spec.agent_role.value
            if workspace.exists():
                self._verify_existing_clone(workspace, baseline_commit)
            else:
                self._run_git(
                    "clone",
                    "--local",
                    "--no-hardlinks",
                    "--no-checkout",
                    str(source),
                    str(workspace),
                )
                self._run_git(
                    "-C",
                    str(workspace),
                    "checkout",
                    "--detach",
                    baseline_commit,
                )
            grants.append(
                WorkspaceGrant(
                    workspace_id=self._workspace_id(assignment_id, spec.agent_role),
                    agent_role=spec.agent_role,
                    workspace_root=str(workspace.resolve()),
                    baseline_commit=baseline_commit,
                    allowed_paths=spec.allowed_paths,
                    allowed_argv=spec.allowed_argv,
                    allowed_hosts=spec.allowed_hosts,
                    allowed_side_effects=spec.allowed_side_effects,
                    secret_refs=spec.secret_refs,
                    created_at=persisted_created_at.get(spec.agent_role, utc_now()),
                )
            )

        manifest = {
            "schema_version": "1.0",
            "assignment_id": str(assignment_id),
            "source_repository": str(source),
            "baseline_commit": baseline_commit,
            "grants": [grant.model_dump(mode="json", exclude_none=True) for grant in grants],
        }
        encoded = self._manifest_bytes(manifest)
        digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        if manifest_path.exists():
            try:
                existing = manifest_path.read_bytes()
            except OSError as error:
                raise DevelopmentWorkspaceError(
                    "existing workspace manifest is not readable"
                ) from error
            if existing != encoded:
                raise DevelopmentWorkspaceError(
                    "existing workspace manifest conflicts with requested authority"
                )
        else:
            descriptor = os.open(
                manifest_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())

        ordered_grants = tuple(grants)
        return PreparedDevelopmentWorkspaces(
            assignment_id=assignment_id,
            source_repository=str(source),
            baseline_commit=baseline_commit,
            grants=ordered_grants,
            manifest_digest=digest,
            manifest_path=str(manifest_path),
        )

    def load_prepared(
        self,
        assignment_id: UUID,
    ) -> PreparedDevelopmentWorkspaces:
        """Load and revalidate a previously prepared assignment manifest."""

        assignment_root = self._safe_assignment_root(self.state_root, assignment_id)
        manifest_path = assignment_root / "workspace-manifest.json"
        try:
            metadata = manifest_path.stat(follow_symlinks=False)
            encoded = manifest_path.read_bytes()
        except OSError as error:
            raise DevelopmentWorkspaceError(
                "prepared workspace manifest is unavailable"
            ) from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise DevelopmentWorkspaceError(
                "prepared workspace manifest is not a single-link regular file"
            )
        try:
            payload = json.loads(encoded)
        except (UnicodeError, ValueError) as error:
            raise DevelopmentWorkspaceError(
                "prepared workspace manifest is invalid"
            ) from error
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "assignment_id",
            "source_repository",
            "baseline_commit",
            "grants",
        }:
            raise DevelopmentWorkspaceError(
                "prepared workspace manifest has an unexpected schema"
            )
        if payload.get("schema_version") != "1.0" or payload.get(
            "assignment_id"
        ) != str(assignment_id):
            raise DevelopmentWorkspaceError(
                "prepared workspace manifest has a different assignment binding"
            )
        try:
            grants = tuple(
                WorkspaceGrant.model_validate(item)
                for item in payload["grants"]
            )
            prepared = PreparedDevelopmentWorkspaces(
                assignment_id=assignment_id,
                source_repository=str(payload["source_repository"]),
                baseline_commit=str(payload["baseline_commit"]),
                grants=grants,
                manifest_digest=(
                    f"sha256:{hashlib.sha256(encoded).hexdigest()}"
                ),
                manifest_path=str(manifest_path),
            )
        except (TypeError, ValueError) as error:
            raise DevelopmentWorkspaceError(
                "prepared workspace manifest is invalid"
            ) from error
        self._verify_prepared_manifest(prepared)
        return prepared

    def revise_prepared_grant(
        self,
        *,
        prepared: PreparedDevelopmentWorkspaces,
        expected_manifest_digest: str,
        replacement_grant: WorkspaceGrant,
    ) -> PreparedDevelopmentWorkspaces:
        """CAS-revise one approved role grant in the frozen manifest.

        Workspace identity, role, baseline, creation time, source repository,
        and the other role's authority remain frozen.  Replaying the same
        revision returns the already-published manifest; any competing
        revision fails closed.
        """

        if not hmac.compare_digest(
            prepared.manifest_digest,
            expected_manifest_digest,
        ):
            raise DevelopmentWorkspaceError(
                "expected manifest digest does not match the prepared receipt"
            )
        assignment_root = self._safe_assignment_root(
            self.state_root,
            prepared.assignment_id,
        )
        manifest_path = assignment_root / "workspace-manifest.json"
        if Path(prepared.manifest_path) != manifest_path:
            raise DevelopmentWorkspaceError(
                "prepared workspace manifest path is outside the assignment"
            )
        lock_path = assignment_root / ".workspace-manifest.lock"
        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            os.fchmod(lock_descriptor, 0o600)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            current = self.load_prepared(prepared.assignment_id)
            if (
                current.source_repository != prepared.source_repository
                or current.baseline_commit != prepared.baseline_commit
            ):
                raise DevelopmentWorkspaceError(
                    "prepared workspace source or baseline changed"
                )
            old_grant = self._grant_for(
                prepared,
                replacement_grant.agent_role,
            )
            stable_identity = (
                replacement_grant.workspace_id == old_grant.workspace_id,
                replacement_grant.agent_role == old_grant.agent_role,
                replacement_grant.workspace_root == old_grant.workspace_root,
                replacement_grant.baseline_commit == old_grant.baseline_commit,
                replacement_grant.created_at == old_grant.created_at,
            )
            if not all(stable_identity):
                raise DevelopmentWorkspaceError(
                    "grant revision cannot change workspace identity or baseline"
                )
            other_roles = {
                grant.agent_role: grant
                for grant in prepared.grants
                if grant.agent_role != replacement_grant.agent_role
            }
            current_other_roles = {
                grant.agent_role: grant
                for grant in current.grants
                if grant.agent_role != replacement_grant.agent_role
            }
            if current_other_roles != other_roles:
                raise DevelopmentWorkspaceError(
                    "another role's frozen workspace authority changed"
                )
            current_role_grant = self._grant_for(
                current,
                replacement_grant.agent_role,
            )
            if current_role_grant == replacement_grant:
                return current
            if replacement_grant.grant_revision != old_grant.grant_revision + 1:
                raise DevelopmentWorkspaceError(
                    "replacement grant revision must advance exactly once"
                )
            if (
                not hmac.compare_digest(
                    current.manifest_digest,
                    expected_manifest_digest,
                )
                or current != prepared
            ):
                raise DevelopmentWorkspaceError(
                    "workspace manifest compare-and-set failed"
                )

            revised_grants = tuple(
                replacement_grant
                if grant.agent_role == replacement_grant.agent_role
                else grant
                for grant in current.grants
            )
            manifest = {
                "schema_version": "1.0",
                "assignment_id": str(current.assignment_id),
                "source_repository": current.source_repository,
                "baseline_commit": current.baseline_commit,
                "grants": [
                    grant.model_dump(mode="json", exclude_none=True)
                    for grant in revised_grants
                ],
            }
            encoded = self._manifest_bytes(manifest)
            temporary = assignment_root / (
                f".workspace-manifest-{uuid4().hex}.tmp"
            )
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, manifest_path)
                manifest_path.chmod(0o600)
                directory_descriptor = os.open(assignment_root, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            finally:
                temporary.unlink(missing_ok=True)
            revised = PreparedDevelopmentWorkspaces(
                assignment_id=current.assignment_id,
                source_repository=current.source_repository,
                baseline_commit=current.baseline_commit,
                grants=revised_grants,
                manifest_digest=(
                    f"sha256:{hashlib.sha256(encoded).hexdigest()}"
                ),
                manifest_path=str(manifest_path),
            )
            self._verify_prepared_manifest(revised)
            return revised
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

    @staticmethod
    def _ensure_private_state_directory(path: Path) -> None:
        if path.exists() or path.is_symlink():
            metadata = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise DevelopmentWorkspaceError(
                    "review snapshot state path must be a non-symlink directory"
                )
            return
        path.mkdir(mode=0o700)

    @classmethod
    def _load_review_snapshot_receipt(
        cls,
        receipt_path: Path,
    ) -> DevelopmentReviewSnapshotReceipt:
        try:
            metadata = receipt_path.stat(follow_symlinks=False)
            encoded = receipt_path.read_bytes()
        except OSError as error:
            raise DevelopmentWorkspaceError("review snapshot receipt is unavailable") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise DevelopmentWorkspaceError(
                "review snapshot receipt must be a single-link regular file"
            )
        try:
            return DevelopmentReviewSnapshotReceipt.model_validate_json(encoded)
        except ValueError as error:
            raise DevelopmentWorkspaceError("review snapshot receipt is invalid") from error

    @classmethod
    def _verify_review_snapshot_receipt(
        cls,
        *,
        receipt: DevelopmentReviewSnapshotReceipt,
        prepared: PreparedDevelopmentWorkspaces,
        result: DevelopmentResult,
        source_grant: WorkspaceGrant,
        reviewer_grant: WorkspaceGrant,
        snapshot: Path,
        result_digest: str,
        changes: tuple[dict[str, object], ...],
    ) -> None:
        changed_paths = tuple(str(change["path"]) for change in changes)
        expected_snapshot_id = uuid5(
            _REVIEW_SNAPSHOT_NAMESPACE,
            f"{prepared.assignment_id}:{result.result_id}:snapshot",
        )
        expected_receipt_id = uuid5(
            _REVIEW_SNAPSHOT_NAMESPACE,
            f"{prepared.assignment_id}:{result.result_id}:receipt",
        )
        expected_diff_digest = cls._diff_digest(prepared.baseline_commit, changes)
        expected_snapshot_digest = cls._snapshot_digest(
            prepared.baseline_commit,
            expected_diff_digest,
        )
        bindings = (
            receipt.receipt_id == expected_receipt_id,
            receipt.review_snapshot_id == expected_snapshot_id,
            receipt.assignment_id == prepared.assignment_id,
            receipt.result_id == result.result_id,
            receipt.work_item_id == result.work_item_id,
            receipt.lease_id == result.lease_id,
            receipt.source_workspace_id == source_grant.workspace_id,
            receipt.reviewer_workspace_id == reviewer_grant.workspace_id,
            receipt.baseline_commit == prepared.baseline_commit,
            receipt.source_manifest_digest == prepared.manifest_digest,
            receipt.result_digest == result_digest,
            receipt.diff_digest == expected_diff_digest,
            receipt.snapshot_digest == expected_snapshot_digest,
            receipt.changed_paths == changed_paths,
            receipt.review_workspace_root == str(snapshot),
        )
        if not all(bindings):
            raise DevelopmentWorkspaceError(
                "review snapshot receipt differs from its frozen result binding"
            )
        snapshot_changes = cls._workspace_changes(
            workspace=snapshot,
            baseline_commit=prepared.baseline_commit,
        )
        if snapshot_changes != changes:
            raise DevelopmentWorkspaceError(
                "review snapshot content differs from its result receipt"
            )

    def materialize_review_snapshot(
        self,
        *,
        prepared: PreparedDevelopmentWorkspaces,
        result: DevelopmentResult,
    ) -> DevelopmentReviewSnapshotReceipt:
        """Materialize one result into a fresh, independent Lilies workspace.

        This is a review-only promotion boundary.  The user's source repository,
        the Codex workspace, and the original Lilies workspace are never
        modified.  A rework result receives a different result ID and therefore
        a new snapshot rather than mutating prior review evidence.
        """

        if result.agent_role != AgentRole.codex:
            raise DevelopmentWorkspaceError(
                "only a Codex development result can enter Lilies review"
            )
        if result.assignment_id != prepared.assignment_id:
            raise DevelopmentWorkspaceError("development result belongs to a different assignment")
        assignment_root = self._verify_prepared_manifest(prepared)
        source_repository = Path(prepared.source_repository)
        source_state_before = self._source_repository_state_digest(source_repository)
        source_grant = self._grant_for(prepared, AgentRole.codex)
        reviewer_grant = self._grant_for(prepared, AgentRole.lilies)
        source_workspace = Path(source_grant.workspace_root)
        reviewer_workspace = Path(reviewer_grant.workspace_root)
        if source_workspace != assignment_root / AgentRole.codex.value:
            raise DevelopmentWorkspaceError("Codex workspace is outside the prepared assignment")
        if reviewer_workspace != assignment_root / AgentRole.lilies.value:
            raise DevelopmentWorkspaceError("Lilies workspace is outside the prepared assignment")
        self._verify_workspace_root(source_workspace)
        self._verify_workspace_root(reviewer_workspace)
        if source_grant.baseline_commit != prepared.baseline_commit or (
            reviewer_grant.baseline_commit != prepared.baseline_commit
        ):
            raise DevelopmentWorkspaceError(
                "role grants do not share the frozen assignment baseline"
            )
        reviewer_head = self._run_git(
            "-C",
            str(reviewer_workspace),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        )
        if reviewer_head != prepared.baseline_commit:
            raise DevelopmentWorkspaceError(
                "Lilies base workspace moved away from the frozen baseline"
            )
        self._verify_result_baseline(
            workspace=source_workspace,
            baseline_commit=prepared.baseline_commit,
            result=result,
        )
        changes = self._workspace_changes(
            workspace=source_workspace,
            baseline_commit=prepared.baseline_commit,
        )
        changed_paths = tuple(str(change["path"]) for change in changes)
        for relative in changed_paths:
            if not self._path_is_granted(relative, source_grant.allowed_paths):
                raise DevelopmentWorkspaceError(
                    "Codex result changed a path outside its frozen grant"
                )
            if not self._path_is_granted(relative, reviewer_grant.allowed_paths):
                raise DevelopmentWorkspaceError(
                    "Codex result changed a path outside the Lilies review grant"
                )
        diff_digest = self._diff_digest(prepared.baseline_commit, changes)
        if not hmac.compare_digest(result.diff_digest, diff_digest):
            raise DevelopmentWorkspaceError(
                "development result diff digest does not match its source workspace"
            )
        result_digest = _canonical_digest(result.model_dump(mode="json", exclude_none=True))

        snapshots_root = assignment_root / "review-snapshots"
        receipts_root = assignment_root / "review-receipts"
        self._ensure_private_state_directory(snapshots_root)
        self._ensure_private_state_directory(receipts_root)
        snapshot = snapshots_root / str(result.result_id)
        receipt_path = receipts_root / f"{result.result_id}.json"
        if (
            snapshot.exists()
            or snapshot.is_symlink()
            or receipt_path.exists()
            or receipt_path.is_symlink()
        ):
            if not snapshot.is_dir() or snapshot.is_symlink() or not receipt_path.is_file():
                raise DevelopmentWorkspaceError(
                    "review snapshot replay state is incomplete or unsafe"
                )
            receipt = self._load_review_snapshot_receipt(receipt_path)
            self._verify_review_snapshot_receipt(
                receipt=receipt,
                prepared=prepared,
                result=result,
                source_grant=source_grant,
                reviewer_grant=reviewer_grant,
                snapshot=snapshot,
                result_digest=result_digest,
                changes=changes,
            )
            source_state_after = self._source_repository_state_digest(source_repository)
            if not hmac.compare_digest(source_state_before, source_state_after):
                raise DevelopmentWorkspaceError(
                    "source repository changed while replaying review materialization"
                )
            return receipt

        temporary = snapshots_root / (f".{result.result_id}.materializing-{uuid4().hex}")
        try:
            self._run_git(
                "clone",
                "--local",
                "--no-hardlinks",
                "--no-checkout",
                str(source_repository),
                str(temporary),
            )
            self._run_git(
                "-C",
                str(temporary),
                "checkout",
                "--detach",
                prepared.baseline_commit,
            )
            temporary.chmod(0o700)
            self._apply_changes_to_snapshot(
                source_workspace=source_workspace,
                snapshot=temporary,
                changes=changes,
            )
            materialized_changes = self._workspace_changes(
                workspace=temporary,
                baseline_commit=prepared.baseline_commit,
            )
            if materialized_changes != changes:
                raise DevelopmentWorkspaceError(
                    "materialized Lilies review snapshot differs from the Codex result"
                )
            source_changes_after = self._workspace_changes(
                workspace=source_workspace,
                baseline_commit=prepared.baseline_commit,
            )
            if source_changes_after != changes:
                raise DevelopmentWorkspaceError(
                    "Codex workspace changed during review materialization"
                )
            os.rename(temporary, snapshot)
        except OSError as error:
            raise DevelopmentWorkspaceError(
                "review snapshot could not be published atomically"
            ) from error
        finally:
            if temporary.exists() and not temporary.is_symlink():
                shutil.rmtree(temporary, ignore_errors=True)

        source_state_after = self._source_repository_state_digest(source_repository)
        if not hmac.compare_digest(source_state_before, source_state_after):
            raise DevelopmentWorkspaceError(
                "source repository changed during review materialization"
            )
        snapshot_digest = self._snapshot_digest(
            prepared.baseline_commit,
            diff_digest,
        )
        receipt = DevelopmentReviewSnapshotReceipt.issue(
            receipt_id=uuid5(
                _REVIEW_SNAPSHOT_NAMESPACE,
                f"{prepared.assignment_id}:{result.result_id}:receipt",
            ),
            review_snapshot_id=uuid5(
                _REVIEW_SNAPSHOT_NAMESPACE,
                f"{prepared.assignment_id}:{result.result_id}:snapshot",
            ),
            assignment_id=prepared.assignment_id,
            result_id=result.result_id,
            work_item_id=result.work_item_id,
            lease_id=result.lease_id,
            source_role=AgentRole.codex,
            reviewer_role=AgentRole.lilies,
            source_workspace_id=source_grant.workspace_id,
            reviewer_workspace_id=reviewer_grant.workspace_id,
            baseline_commit=prepared.baseline_commit,
            source_manifest_digest=prepared.manifest_digest,
            result_digest=result_digest,
            diff_digest=diff_digest,
            snapshot_digest=snapshot_digest,
            source_repository_state_digest=source_state_before,
            changed_paths=changed_paths,
            review_workspace_root=str(snapshot),
            promotion_state="review_snapshot_only",
            source_repository_unchanged=True,
            created_at=utc_now(),
        )
        encoded_receipt = _canonical_bytes(receipt.model_dump(mode="json"))
        try:
            descriptor = os.open(
                receipt_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded_receipt)
                handle.flush()
                os.fsync(handle.fileno())
            receipt_path.chmod(0o400)
        except OSError as error:
            raise DevelopmentWorkspaceError(
                "review snapshot receipt could not be persisted"
            ) from error
        return receipt


__all__ = [
    "DevelopmentReviewSnapshotReceipt",
    "DevelopmentWorkspaceBroker",
    "DevelopmentWorkspaceError",
    "DevelopmentWorkspaceSpec",
    "PreparedDevelopmentWorkspaces",
]
