"""Platform-neutral worker for durable collaborative-development handoffs.

The worker intentionally knows nothing about workflow applications or Builder.
It reads only the reusable collaborative-development store, dispatches its
durable outbox, and records outcomes in a separate SQLite journal.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from io import BufferedRandom
from pathlib import Path
from pathlib import PurePosixPath
from threading import Event as ThreadEvent
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .collaborative_development_handler import RoleBoundDispatchContext
from .collaborative_development_dispatcher import (
    CollaborativeDevelopmentDispatchJournal,
    CollaborativeDevelopmentDispatcher,
    DevelopmentDispatchHandler,
    DispatchHistoryRecord,
    DispatchOutcome,
    DispatchOutcomeStatus,
    RequestedAuthority,
    canonical_digest,
)
from .collaborative_development_auth import DevelopmentPrincipal
from .collaborative_development_models import (
    AgentRole,
    AssignmentStatus,
    DevelopmentAssignment,
    DevelopmentAssignmentProjection,
    DevelopmentBudget,
    DevelopmentLease,
    DevelopmentResult,
    DevelopmentTaskRole,
    DevelopmentWorkItem,
    DispatchOutboxItem,
    LiliesReview,
    SideEffect,
    WorkspaceGrant,
)
from .collaborative_development_service import CollaborativeDevelopmentService
from .collaborative_development_storage import (
    CollaborativeDevelopmentBudgetExceeded,
    CollaborativeDevelopmentConflict,
    CollaborativeDevelopmentStore,
)
from .development_workspace_broker import (
    DevelopmentReviewSnapshotReceipt,
    DevelopmentWorkspaceBroker,
)


_MAX_ADAPTER_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_ADAPTER_INPUT_BYTES = 2 * 1024 * 1024
_MACOS_SANDBOX = Path("/usr/bin/sandbox-exec")


@dataclass(frozen=True)
class _RegularFileBinding:
    path: Path
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    content_digest: str


class ExternalAssignmentProjection(BaseModel):
    """Minimum assignment context visible to one untrusted role adapter."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: UUID
    goal: str
    software_id: str
    baseline_commit: str
    budget: DevelopmentBudget
    deadline: datetime


class ExternalRoleProjection(BaseModel):
    """One role's usable workspace surface, without hosts or secret refs."""

    model_config = ConfigDict(extra="forbid")

    agent_role: AgentRole
    task_roles: tuple[DevelopmentTaskRole, ...]
    workspace_id: UUID
    workspace_root: str
    baseline_commit: str
    grant_revision: int
    allowed_paths: tuple[str, ...]
    allowed_argv: tuple[tuple[str, ...], ...]
    allowed_side_effects: tuple[SideEffect, ...]


class ExternalDispatchEnvelope(BaseModel):
    """Strict message sent to one explicitly configured role adapter."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    outbox_id: UUID
    outbox_idempotency_key: str = Field(min_length=8, max_length=200)
    grant_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    destination_role: AgentRole
    outbox: DispatchOutboxItem
    assignment: ExternalAssignmentProjection
    work_item: DevelopmentWorkItem
    role: ExternalRoleProjection
    lease: DevelopmentLease | None = None
    source_result: DevelopmentResult | None = None
    review_snapshot: DevelopmentReviewSnapshotReceipt | None = None


class ExternalDispatchResponse(BaseModel):
    """Bound response; stale or cross-assignment adapter output is rejected."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    outbox_id: UUID
    outbox_idempotency_key: str = Field(min_length=8, max_length=200)
    grant_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    outcome: DispatchOutcome
    development_result: DevelopmentResult | None = None
    lilies_review: LiliesReview | None = None


class ExternalJsonArgvDispatchHandler:
    """Invoke an explicit argv adapter with one JSON request on stdin.

    No shell is involved and the worker environment is not inherited.  A
    fail-closed macOS Seatbelt profile narrows filesystem and process access to
    the frozen grant, denies network, and refuses to execute at all when that
    OS boundary is unavailable.  The adapter receives only its role projection,
    while the digest binds the complete frozen grant without exposing secret
    references or another role's authority.  Adapter transport failures
    produce a durable retry outcome rather than an invented result.
    """

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int = 300,
        retry_after_seconds: int = 30,
    ) -> None:
        if not argv:
            raise ValueError("external adapter argv cannot be empty")
        if not Path(argv[0]).is_absolute():
            raise ValueError("external adapter executable must be an absolute path")
        if len(argv) > 100 or any(
            not value or len(value) > 4_096 or "\x00" in value for value in argv
        ):
            raise ValueError("external adapter argv is invalid")
        if not 1 <= timeout_seconds <= 3_600:
            raise ValueError("adapter timeout must be between 1 and 3600 seconds")
        if not 1 <= retry_after_seconds <= 3_600:
            raise ValueError("adapter retry delay must be between 1 and 3600 seconds")
        self.argv = argv
        self.timeout_seconds = timeout_seconds
        self.retry_after_seconds = retry_after_seconds

    @staticmethod
    def _clean_environment() -> dict[str, str]:
        """Provide deterministic process basics without leaking worker secrets."""

        return {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }

    @staticmethod
    def _safe_regular_file(path: Path, *, label: str) -> _RegularFileBinding:
        try:
            lexical = path.lstat()
            resolved = path.resolve(strict=True)
            if path != resolved or stat.S_ISLNK(lexical.st_mode):
                raise ValueError(f"{label} must not traverse a symlink")
            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
        except OSError as error:
            raise ValueError(f"{label} is not safely readable") from error
        try:
            metadata = os.fstat(descriptor)
            if (
                metadata.st_dev != lexical.st_dev
                or metadata.st_ino != lexical.st_ino
            ):
                raise ValueError(f"{label} changed while it was opened")
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o022
            ):
                raise ValueError(
                    f"{label} must be a single-link non-writable regular file"
                )
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
        finally:
            os.close(descriptor)
        return _RegularFileBinding(
            path=resolved,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            changed_ns=metadata.st_ctime_ns,
            content_digest=f"sha256:{digest.hexdigest()}",
        )

    @classmethod
    def _validate_regular_file_bindings(
        cls,
        bindings: tuple[_RegularFileBinding, ...],
    ) -> None:
        for expected in bindings:
            observed = cls._safe_regular_file(
                expected.path,
                label="bound adapter file",
            )
            if observed != expected:
                raise ValueError("bound adapter file changed before or during execution")

    @staticmethod
    def _profile_rule(path: Path) -> str:
        operation = "subpath" if path.is_dir() else "literal"
        return f"({operation} {json.dumps(str(path), ensure_ascii=True)})"

    @classmethod
    def _runtime_read_roots(
        cls,
        *,
        executable: Path,
        entrypoints: tuple[Path, ...],
        input_path: Path,
    ) -> set[Path]:
        roots = {
            executable,
            executable.parent,
            executable.parent.parent,
            input_path,
        }
        for candidate in (
            Path("/System"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/Library/Apple"),
            Path("/Library/Developer/CommandLineTools"),
            Path("/private/var/db/dyld"),
            Path("/private/var/db/timezone"),
            Path("/dev/null"),
            Path("/dev/urandom"),
        ):
            if candidate.exists():
                roots.add(candidate.resolve())
        roots.update(entrypoints)
        return roots

    @staticmethod
    def _workspace_paths(grant: WorkspaceGrant) -> tuple[Path, set[Path]]:
        lexical_root = Path(grant.workspace_root)
        if lexical_root.is_symlink() or not lexical_root.is_dir():
            raise ValueError("adapter workspace must be a non-symlink directory")
        root = lexical_root.resolve(strict=True)
        if root != lexical_root:
            raise ValueError("adapter workspace must not traverse symlinks")
        granted: set[Path] = set()
        for raw in grant.allowed_paths:
            relative = PurePosixPath(raw)
            candidate = root.joinpath(*relative.parts)
            cursor = root
            for part in relative.parts:
                cursor = cursor / part
                if not cursor.exists():
                    break
                if cursor.is_symlink():
                    raise ValueError("adapter workspace grant traverses a symlink")
            resolved = candidate.resolve(strict=False)
            if resolved != root and root not in resolved.parents:
                raise ValueError("adapter workspace grant escapes its root")
            granted.add(candidate)
        return root, granted

    @classmethod
    def _sandbox_command(
        cls,
        *,
        argv: tuple[str, ...],
        grant: WorkspaceGrant,
        input_path: Path,
    ) -> tuple[tuple[str, ...], Path, tuple[_RegularFileBinding, ...]]:
        if not _MACOS_SANDBOX.is_file() or not os.access(_MACOS_SANDBOX, os.X_OK):
            raise ValueError("external role adapter requires the macOS OS sandbox")
        executable_binding = cls._safe_regular_file(
            Path(argv[0]),
            label="adapter executable",
        )
        entrypoint_bindings: tuple[_RegularFileBinding, ...] = ()
        if len(argv) > 1 and Path(argv[1]).is_absolute():
            entrypoint_bindings = (
                cls._safe_regular_file(
                    Path(argv[1]),
                    label="adapter entrypoint",
                ),
            )
        file_bindings = (executable_binding, *entrypoint_bindings)
        executable = executable_binding.path
        workspace_root, workspace_paths = cls._workspace_paths(grant)
        runtime_roots = cls._runtime_read_roots(
            executable=executable,
            entrypoints=tuple(
                binding.path for binding in entrypoint_bindings
            ),
            input_path=input_path,
        )
        read_roots = runtime_roots | workspace_paths
        write_roots = (
            workspace_paths if SideEffect.workspace_write in grant.allowed_side_effects else set()
        )
        metadata_roots = {Path("/"), workspace_root}
        for path in read_roots | write_roots:
            metadata_roots.add(path)
            metadata_roots.update(path.parents)
        profile = [
            "(version 1)",
            '(import "system.sb")',
            "(deny default)",
            "(allow process-info*)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow ipc-posix-shm-read*)",
        ]
        # The one adapter process is the sole metered command.  It may not
        # fork arbitrary child argv under executable-only Seatbelt authority.
        profile.append(f"(allow process-exec {cls._profile_rule(executable)})")
        profile.extend(
            f"(allow file-read-metadata (literal {json.dumps(str(path))}))"
            for path in sorted(metadata_roots, key=str)
        )
        profile.extend(
            f"(allow file-read* {cls._profile_rule(path)})" for path in sorted(read_roots, key=str)
        )
        profile.extend(
            f"(allow file-map-executable {cls._profile_rule(path)})"
            for path in sorted(runtime_roots, key=str)
        )
        profile.extend(
            f"(allow file-write* {cls._profile_rule(path)})"
            for path in sorted(write_roots, key=str)
        )
        profile.append('(allow file-write* (literal "/dev/null"))')
        profile.append("(deny network*)")
        command = (
            str(_MACOS_SANDBOX),
            "-p",
            "\n".join(profile),
            "--",
            str(executable),
            *argv[1:],
        )
        return command, workspace_root, file_bindings

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()

    @classmethod
    def _run_bounded(
        cls,
        *,
        command: tuple[str, ...],
        cwd: Path,
        request_stream: BufferedRandom,
        timeout_seconds: int,
        file_bindings: tuple[_RegularFileBinding, ...] = (),
        cancel_event: ThreadEvent | None = None,
    ) -> tuple[
        int | None,
        bytes,
        Literal["complete", "timeout", "overflow", "cancelled"],
    ]:
        cls._validate_regular_file_bindings(file_bindings)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=cls._clean_environment(),
            stdin=request_stream,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            cls._validate_regular_file_bindings(file_bindings)
        except ValueError:
            cls._kill_process_group(process)
            process.wait(timeout=2)
            raise
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        output = bytearray()
        deadline = time.monotonic() + timeout_seconds
        state: Literal["complete", "timeout", "overflow", "cancelled"] = "complete"
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    state = "cancelled"
                    cls._kill_process_group(process)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    state = "timeout"
                    cls._kill_process_group(process)
                    break
                events = selector.select(timeout=min(remaining, 0.1))
                if not events:
                    if process.poll() is not None:
                        chunk = os.read(process.stdout.fileno(), 64 * 1024)
                        if chunk:
                            if len(output) + len(chunk) > _MAX_ADAPTER_OUTPUT_BYTES:
                                remaining_capacity = _MAX_ADAPTER_OUTPUT_BYTES - len(output)
                                output.extend(chunk[:remaining_capacity])
                                state = "overflow"
                                cls._kill_process_group(process)
                                break
                            output.extend(chunk)
                            continue
                        break
                    continue
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                if not chunk:
                    break
                if len(output) + len(chunk) > _MAX_ADAPTER_OUTPUT_BYTES:
                    remaining_capacity = _MAX_ADAPTER_OUTPUT_BYTES - len(output)
                    output.extend(chunk[:remaining_capacity])
                    state = "overflow"
                    cls._kill_process_group(process)
                    break
                output.extend(chunk)
        finally:
            selector.close()
            process.stdout.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                cls._kill_process_group(process)
                process.wait(timeout=2)
        cls._validate_regular_file_bindings(file_bindings)
        return (
            process.returncode if state == "complete" else None,
            bytes(output),
            state,
        )

    def __call__(
        self,
        *,
        context: RoleBoundDispatchContext,
    ) -> DispatchOutcome:
        grant_digest = canonical_digest(context.workspace_grant)
        return self._failure_response(
            outbox=context.outbox,
            grant_digest=grant_digest,
            detail=("configured role adapter requires a trusted durable usage meter"),
        ).outcome

    def _failure_response(
        self,
        *,
        outbox: DispatchOutboxItem,
        grant_digest: str,
        detail: str,
    ) -> ExternalDispatchResponse:
        return ExternalDispatchResponse(
            outbox_id=outbox.outbox_id,
            outbox_idempotency_key=outbox.idempotency_key,
            grant_digest=grant_digest,
            outcome=DispatchOutcome(
                status=DispatchOutcomeStatus.retry,
                detail=detail,
                retry_after_seconds=self.retry_after_seconds,
            ),
        )

    @staticmethod
    def _reconciliation_response(
        *,
        outbox: DispatchOutboxItem,
        grant_digest: str,
        detail: str,
    ) -> ExternalDispatchResponse:
        return ExternalDispatchResponse(
            outbox_id=outbox.outbox_id,
            outbox_idempotency_key=outbox.idempotency_key,
            grant_digest=grant_digest,
            outcome=DispatchOutcome(
                status=DispatchOutcomeStatus.reconciliation_required,
                detail=detail,
            ),
        )

    def _authority_response(
        self,
        *,
        context: RoleBoundDispatchContext,
        grant_digest: str,
    ) -> ExternalDispatchResponse | None:
        grant = context.workspace_grant
        missing_argv = self.argv not in grant.allowed_argv
        missing_process = SideEffect.process_execute not in grant.allowed_side_effects
        if not missing_argv and not missing_process:
            return None
        request = RequestedAuthority(
            argv=(self.argv,) if missing_argv else (),
            side_effects=((SideEffect.process_execute,) if missing_process else ()),
            reason=(
                "The external role adapter requires this exact argv in the "
                "frozen role workspace; executable-only authority is not enough."
            ),
        )
        return ExternalDispatchResponse(
            outbox_id=context.outbox.outbox_id,
            outbox_idempotency_key=context.outbox.idempotency_key,
            grant_digest=grant_digest,
            outcome=DispatchOutcome(
                status=DispatchOutcomeStatus.authorization_required,
                detail=("configured role adapter exact argv is outside the frozen grant"),
                requested_authority=request,
            ),
        )

    @staticmethod
    def _budget_authority_response(
        *,
        context: RoleBoundDispatchContext,
        grant_digest: str,
        used_tool_calls: int,
        used_commands: int,
    ) -> ExternalDispatchResponse:
        budget = context.assignment.budget
        needs_tools = used_tool_calls + 1 > budget.max_tool_calls
        needs_commands = used_commands + 1 > budget.max_commands
        expanded_tools = (
            min(budget.max_tool_calls + 1, 10_000_000) if needs_tools else budget.max_tool_calls
        )
        expanded_commands = (
            min(budget.max_commands + 1, 1_000_000) if needs_commands else budget.max_commands
        )
        if (needs_tools and expanded_tools == budget.max_tool_calls) or (
            needs_commands and expanded_commands == budget.max_commands
        ):
            return ExternalJsonArgvDispatchHandler._reconciliation_response(
                outbox=context.outbox,
                grant_digest=grant_digest,
                detail=("configured role adapter budget is exhausted at the contract maximum"),
            )
        requested_budget = budget.model_copy(
            update={
                "max_tool_calls": expanded_tools,
                "max_commands": expanded_commands,
            }
        )
        return ExternalDispatchResponse(
            outbox_id=context.outbox.outbox_id,
            outbox_idempotency_key=context.outbox.idempotency_key,
            grant_digest=grant_digest,
            outcome=DispatchOutcome(
                status=DispatchOutcomeStatus.authorization_required,
                detail=("configured role adapter requires additional tool and command budget"),
                requested_authority=RequestedAuthority(
                    budget=requested_budget,
                    reason=(
                        "One exact external adapter invocation requires one "
                        "durably reserved tool call and command."
                    ),
                ),
            ),
        )

    async def invoke_autonomous(
        self,
        *,
        context: RoleBoundDispatchContext,
        usage_meter: CollaborativeDevelopmentStore,
    ) -> ExternalDispatchResponse:
        outbox = context.outbox
        grant = context.workspace_grant
        grant_digest = canonical_digest(grant)
        await usage_meter.require_development_tool_metering(context.assignment.assignment_id)
        authority_response = self._authority_response(
            context=context,
            grant_digest=grant_digest,
        )
        if authority_response is not None:
            return authority_response

        usage_id = f"external-adapter:{outbox.outbox_id}"
        request_digest = canonical_digest(
            {
                "tool": "process_run",
                "assignment_id": str(context.assignment.assignment_id),
                "workspace_id": str(grant.workspace_id),
                "grant_revision": grant.grant_revision,
                "outbox_id": str(outbox.outbox_id),
                "argv": list(self.argv),
                "cwd": ".",
            }
        )
        try:
            acquired = await usage_meter.reserve_development_tool_usage(
                assignment_id=context.assignment.assignment_id,
                actor_role=outbox.destination_role,
                usage_id=usage_id,
                tool_name="process_run",
                request_digest=request_digest,
                command_argv=self.argv,
                command_cwd=".",
            )
        except CollaborativeDevelopmentBudgetExceeded:
            usage = await usage_meter.list_development_tool_usage(context.assignment.assignment_id)
            return self._budget_authority_response(
                context=context,
                grant_digest=grant_digest,
                used_tool_calls=sum(record.tool_calls for record in usage),
                used_commands=sum(record.commands for record in usage),
            )
        except CollaborativeDevelopmentConflict:
            return self._reconciliation_response(
                outbox=outbox,
                grant_digest=grant_digest,
                detail=("configured role adapter invocation differs from its durable usage fence"),
            )
        if not acquired:
            return self._reconciliation_response(
                outbox=outbox,
                grant_digest=grant_digest,
                detail=(
                    "configured role adapter was not replayed because its "
                    "durable usage id already exists"
                ),
            )

        response = await asyncio.to_thread(
            self._invoke_once,
            context=context,
        )
        response_digest = canonical_digest(response)
        await usage_meter.complete_development_tool_usage(
            assignment_id=context.assignment.assignment_id,
            actor_role=outbox.destination_role,
            usage_id=usage_id,
            request_digest=request_digest,
            response_digest=response_digest,
            output_digest=canonical_digest(
                {
                    "adapter_response_digest": response_digest,
                    "status": response.outcome.status.value,
                }
            ),
        )
        return response

    def _invoke_once(
        self,
        *,
        context: RoleBoundDispatchContext,
    ) -> ExternalDispatchResponse:
        outbox = context.outbox
        assignment = context.assignment
        work_item = context.work_item
        grant = context.workspace_grant
        grant_digest = canonical_digest(grant)
        role_grant = assignment.agent_role
        envelope = ExternalDispatchEnvelope(
            outbox_id=outbox.outbox_id,
            outbox_idempotency_key=outbox.idempotency_key,
            grant_digest=grant_digest,
            destination_role=outbox.destination_role,
            outbox=outbox,
            assignment=ExternalAssignmentProjection(
                assignment_id=assignment.assignment_id,
                goal=assignment.goal,
                software_id=assignment.software_id,
                baseline_commit=assignment.baseline_commit,
                budget=assignment.budget,
                deadline=assignment.deadline,
            ),
            work_item=work_item,
            role=ExternalRoleProjection(
                agent_role=outbox.destination_role,
                task_roles=role_grant.task_roles,
                workspace_id=grant.workspace_id,
                workspace_root=grant.workspace_root,
                baseline_commit=grant.baseline_commit,
                grant_revision=grant.grant_revision,
                allowed_paths=grant.allowed_paths,
                allowed_argv=grant.allowed_argv,
                allowed_side_effects=tuple(
                    effect
                    for effect in grant.allowed_side_effects
                    if effect
                    not in {
                        SideEffect.network_access,
                        SideEffect.external_mutation,
                    }
                ),
            ),
            lease=context.lease,
            source_result=context.source_result,
            review_snapshot=context.review_snapshot,
        )
        encoded = (
            json.dumps(
                envelope.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > _MAX_ADAPTER_INPUT_BYTES:
            return self._failure_response(
                outbox=outbox,
                grant_digest=grant_digest,
                detail="configured role adapter input exceeded the safety limit",
            )
        descriptor, raw_input_path = tempfile.mkstemp(prefix="lilies-collab-adapter-")
        input_path = Path(raw_input_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w+b") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                handle.seek(0)
                command, cwd, file_bindings = self._sandbox_command(
                    argv=self.argv,
                    grant=grant,
                    input_path=input_path,
                )
                returncode, stdout, process_state = self._run_bounded(
                    command=command,
                    cwd=cwd,
                    request_stream=handle,
                    timeout_seconds=self.timeout_seconds,
                    file_bindings=file_bindings,
                    cancel_event=context.cancel_event,
                )
        except (OSError, ValueError, subprocess.SubprocessError):
            return self._failure_response(
                outbox=outbox,
                grant_digest=grant_digest,
                detail="configured role adapter was rejected by the OS boundary",
            )
        finally:
            input_path.unlink(missing_ok=True)
        if process_state == "cancelled":
            return self._failure_response(
                outbox=outbox,
                grant_digest=grant_digest,
                detail="configured role adapter was cancelled",
            )
        if process_state == "timeout":
            return self._failure_response(
                outbox=outbox,
                grant_digest=grant_digest,
                detail="configured role adapter exceeded its time limit",
            )
        if process_state == "overflow":
            return self._failure_response(
                outbox=outbox,
                grant_digest=grant_digest,
                detail="configured role adapter output exceeded the safety limit",
            )
        if returncode != 0:
            return self._failure_response(
                outbox=outbox,
                grant_digest=grant_digest,
                detail="configured role adapter did not complete successfully",
            )
        try:
            response = ExternalDispatchResponse.model_validate_json(stdout)
        except ValueError:
            return self._failure_response(
                outbox=outbox,
                grant_digest=grant_digest,
                detail="configured role adapter returned an invalid response",
            )
        if (
            response.outbox_id != outbox.outbox_id
            or response.outbox_idempotency_key != outbox.idempotency_key
            or response.grant_digest != grant_digest
        ):
            return self._failure_response(
                outbox=outbox,
                grant_digest=grant_digest,
                detail="configured role adapter response binding did not match",
            )
        return response


class AutonomousHandlerCompletion(BaseModel):
    """Strict lifecycle payload returned by an embedded role handler."""

    model_config = ConfigDict(extra="forbid")

    outcome: DispatchOutcome
    development_result: DevelopmentResult | None = None
    lilies_review: LiliesReview | None = None


class AutonomousDevelopmentLifecycleBridge:
    """Complete the fenced WorkItem lifecycle around one role handler.

    The bridge creates internal role principals directly; no assignment bearer
    is minted or exposed to the handler.  It verifies the assignment before
    and after every observable workspace operation and supplies a cancellation
    event while a handler is running.
    """

    def __init__(
        self,
        *,
        service: CollaborativeDevelopmentService,
        workspace_broker: DevelopmentWorkspaceBroker,
        lease_ttl_seconds: int = 300,
        cancellation_poll_seconds: float = 0.05,
    ) -> None:
        if not 1 <= lease_ttl_seconds <= 3_600:
            raise ValueError("lifecycle lease TTL must be between 1 and 3600 seconds")
        if not 0.01 <= cancellation_poll_seconds <= 1:
            raise ValueError("cancellation poll interval must be between 0.01 and 1 second")
        self.service = service
        self.workspace_broker = workspace_broker
        self.lease_ttl_seconds = lease_ttl_seconds
        self.cancellation_poll_seconds = cancellation_poll_seconds

    @staticmethod
    def _principal(assignment_id: UUID, role: AgentRole) -> DevelopmentPrincipal:
        return DevelopmentPrincipal(
            actor_role=role.value,
            actor_id=f"autonomous-worker-{role.value}",
            assignment_id=assignment_id,
        )

    async def _require_active(self, principal: DevelopmentPrincipal) -> None:
        await self.service.validate_principal(principal)
        assert principal.assignment_id is not None
        assignment = await self.service.store.get_assignment(principal.assignment_id)
        if assignment.status != AssignmentStatus.active:
            raise RuntimeError("collaborative development assignment is not active")

    async def _watch_assignment(
        self,
        *,
        principal: DevelopmentPrincipal,
        cancel_event: ThreadEvent,
        finished: asyncio.Event,
    ) -> None:
        while not finished.is_set():
            try:
                await self._require_active(principal)
            except Exception:
                cancel_event.set()
                return
            try:
                await asyncio.wait_for(
                    finished.wait(),
                    timeout=self.cancellation_poll_seconds,
                )
            except TimeoutError:
                continue

    async def _invoke_handler(
        self,
        handler: DevelopmentDispatchHandler,
        *,
        outbox: DispatchOutboxItem,
        assignment: DevelopmentAssignment,
        work_item: DevelopmentWorkItem,
        grant: WorkspaceGrant,
        lease: DevelopmentLease | None,
        source_result: DevelopmentResult | None,
        review_snapshot: DevelopmentReviewSnapshotReceipt | None,
        cancel_event: ThreadEvent,
    ) -> AutonomousHandlerCompletion:
        context = RoleBoundDispatchContext.from_assignment(
            outbox=outbox,
            assignment=assignment,
            work_item=work_item,
            lease=lease,
            source_result=source_result,
            review_snapshot=review_snapshot,
            cancel_event=cancel_event,
            workspace_grant=grant,
        )
        if isinstance(handler, ExternalJsonArgvDispatchHandler):
            response = await handler.invoke_autonomous(
                context=context,
                usage_meter=self.service.store,
            )
            return AutonomousHandlerCompletion(
                outcome=response.outcome,
                development_result=response.development_result,
                lilies_review=response.lilies_review,
            )
        if inspect.iscoroutinefunction(handler):
            raw = await handler(context=context)
        else:
            raw = await asyncio.to_thread(handler, context=context)
            if inspect.isawaitable(raw):
                raw = await raw
        if isinstance(raw, DispatchOutcome):
            return AutonomousHandlerCompletion(outcome=raw)
        return AutonomousHandlerCompletion.model_validate(raw)

    async def _invoke_with_cancellation(
        self,
        handler: DevelopmentDispatchHandler,
        *,
        principal: DevelopmentPrincipal,
        outbox: DispatchOutboxItem,
        assignment: DevelopmentAssignment,
        work_item: DevelopmentWorkItem,
        grant: WorkspaceGrant,
        lease: DevelopmentLease | None = None,
        source_result: DevelopmentResult | None = None,
        review_snapshot: DevelopmentReviewSnapshotReceipt | None = None,
    ) -> tuple[AutonomousHandlerCompletion, bool]:
        await self._require_active(principal)
        cancel_event = ThreadEvent()
        finished = asyncio.Event()
        watcher = asyncio.create_task(
            self._watch_assignment(
                principal=principal,
                cancel_event=cancel_event,
                finished=finished,
            )
        )
        try:
            completion = await self._invoke_handler(
                handler,
                outbox=outbox,
                assignment=assignment,
                work_item=work_item,
                grant=grant,
                lease=lease,
                source_result=source_result,
                review_snapshot=review_snapshot,
                cancel_event=cancel_event,
            )
        finally:
            finished.set()
            await watcher
        try:
            await self._require_active(principal)
        except Exception:
            cancel_event.set()
        return completion, cancel_event.is_set()

    async def _abort_execution(
        self,
        *,
        principal: DevelopmentPrincipal,
        lease: DevelopmentLease,
        work_item: DevelopmentWorkItem,
        outbox: DispatchOutboxItem,
        reason: str,
    ) -> None:
        try:
            await self._require_active(principal)
        except Exception:
            return
        await self.service.abort_work(
            principal=principal,
            lease_id=lease.lease_id,
            expected_work_item_revision=work_item.revision,
            reason=reason,
            idempotency_key=f"worker:{outbox.outbox_id}:abort",
        )

    async def _dispatch_work(
        self,
        handler: DevelopmentDispatchHandler,
        *,
        outbox: DispatchOutboxItem,
        assignment: DevelopmentAssignment,
        work_item: DevelopmentWorkItem,
        grant: WorkspaceGrant,
    ) -> DispatchOutcome:
        principal = self._principal(assignment.assignment_id, outbox.destination_role)
        await self._require_active(principal)
        lease = await self.service.acquire_lease(
            principal=principal,
            work_item_id=work_item.work_item_id,
            expected_revision=work_item.revision,
            ttl_seconds=self.lease_ttl_seconds,
            idempotency_key=f"worker:{outbox.outbox_id}:lease",
        )
        working = await self.service.start_work(
            principal=principal,
            lease_id=lease.lease_id,
            expected_work_item_revision=lease.work_item_revision,
            idempotency_key=f"worker:{outbox.outbox_id}:start",
        )
        try:
            completion, cancelled = await self._invoke_with_cancellation(
                handler,
                principal=principal,
                outbox=outbox,
                assignment=assignment,
                work_item=working,
                grant=grant,
                lease=lease,
            )
        except Exception:
            await self._abort_execution(
                principal=principal,
                lease=lease,
                work_item=working,
                outbox=outbox,
                reason="role handler failed before producing a lifecycle outcome",
            )
            return DispatchOutcome(
                status=DispatchOutcomeStatus.retry,
                detail="role handler failed before producing a lifecycle outcome",
                retry_after_seconds=30,
            )
        if cancelled:
            await self._abort_execution(
                principal=principal,
                lease=lease,
                work_item=working,
                outbox=outbox,
                reason="assignment stopped while the role handler was running",
            )
            return DispatchOutcome(
                status=DispatchOutcomeStatus.reconciliation_required,
                detail="assignment stopped before the autonomous result was submitted",
            )
        if completion.outcome.status != DispatchOutcomeStatus.delivered:
            await self._abort_execution(
                principal=principal,
                lease=lease,
                work_item=working,
                outbox=outbox,
                reason=completion.outcome.detail,
            )
            return completion.outcome
        result = completion.development_result
        if result is None or completion.lilies_review is not None:
            await self._abort_execution(
                principal=principal,
                lease=lease,
                work_item=working,
                outbox=outbox,
                reason="delivered work dispatch omitted its strict DevelopmentResult",
            )
            return DispatchOutcome(
                status=DispatchOutcomeStatus.retry,
                detail="delivered work dispatch omitted its strict DevelopmentResult",
                retry_after_seconds=30,
            )
        if (
            result.assignment_id != assignment.assignment_id
            or result.work_item_id != working.work_item_id
            or result.lease_id != lease.lease_id
            or result.agent_role != outbox.destination_role
            or result.baseline_commit != assignment.baseline_commit
        ):
            await self._abort_execution(
                principal=principal,
                lease=lease,
                work_item=working,
                outbox=outbox,
                reason="DevelopmentResult binding differed from the active lease",
            )
            return DispatchOutcome(
                status=DispatchOutcomeStatus.retry,
                detail="DevelopmentResult binding differed from the active lease",
                retry_after_seconds=30,
            )
        await self._require_active(principal)
        try:
            await self.service.submit_result(
                principal=principal,
                result=result,
                expected_work_item_revision=working.revision,
                idempotency_key=f"worker:{outbox.outbox_id}:result",
            )
        except Exception:
            await self._abort_execution(
                principal=principal,
                lease=lease,
                work_item=working,
                outbox=outbox,
                reason="DevelopmentResult was rejected by the fenced service",
            )
            return DispatchOutcome(
                status=DispatchOutcomeStatus.retry,
                detail="DevelopmentResult was rejected by the fenced service",
                retry_after_seconds=30,
            )
        try:
            await self._require_active(principal)
        except Exception:
            return DispatchOutcome(
                status=DispatchOutcomeStatus.reconciliation_required,
                detail="assignment stopped immediately after result submission",
            )
        return completion.outcome

    @staticmethod
    def _review_grant(
        original: WorkspaceGrant,
        receipt: DevelopmentReviewSnapshotReceipt,
    ) -> WorkspaceGrant:
        retained_effects = {
            SideEffect.process_execute,
            SideEffect.network_access,
        }
        side_effects = tuple(
            effect
            for effect in original.allowed_side_effects
            if effect in retained_effects
        )
        retains_network = SideEffect.network_access in side_effects
        return WorkspaceGrant(
            workspace_id=receipt.review_snapshot_id,
            agent_role=AgentRole.lilies,
            workspace_root=receipt.review_workspace_root,
            baseline_commit=original.baseline_commit,
            grant_revision=original.grant_revision,
            allowed_paths=original.allowed_paths,
            allowed_argv=original.allowed_argv,
            allowed_hosts=original.allowed_hosts if retains_network else (),
            allowed_side_effects=side_effects,
            secret_refs=original.secret_refs if retains_network else (),
            created_at=original.created_at,
        )

    async def _dispatch_review(
        self,
        handler: DevelopmentDispatchHandler,
        *,
        outbox: DispatchOutboxItem,
        assignment: DevelopmentAssignment,
        work_item: DevelopmentWorkItem,
        grant: WorkspaceGrant,
    ) -> DispatchOutcome:
        principal = self._principal(assignment.assignment_id, AgentRole.lilies)
        await self._require_active(principal)
        try:
            result_id = UUID(str(outbox.payload["result_id"]))
        except (KeyError, ValueError) as error:
            raise RuntimeError("Lilies review outbox has no valid result binding") from error
        result = await self.service.get_result(
            principal=principal,
            result_id=result_id,
        )
        prepared = await asyncio.to_thread(
            self.workspace_broker.load_prepared,
            assignment.assignment_id,
        )
        prepared_grants = {item.agent_role: item for item in prepared.grants}
        assignment_grants = {item.agent_role: item for item in assignment.workspace_grants}
        if (
            prepared.baseline_commit != assignment.baseline_commit
            or prepared_grants != assignment_grants
        ):
            raise RuntimeError("prepared review workspaces differ from assignment authority")
        await self._require_active(principal)
        receipt = await asyncio.to_thread(
            self.workspace_broker.materialize_review_snapshot,
            prepared=prepared,
            result=result,
        )
        try:
            await self._require_active(principal)
        except Exception:
            return DispatchOutcome(
                status=DispatchOutcomeStatus.reconciliation_required,
                detail="assignment stopped during review snapshot materialization",
            )
        review_grant = self._review_grant(grant, receipt)
        current = await self.service.store.get_work_item(work_item.work_item_id)
        try:
            completion, cancelled = await self._invoke_with_cancellation(
                handler,
                principal=principal,
                outbox=outbox,
                assignment=assignment,
                work_item=current,
                grant=review_grant,
                source_result=result,
                review_snapshot=receipt,
            )
        except Exception:
            return DispatchOutcome(
                status=DispatchOutcomeStatus.retry,
                detail="review handler failed before producing a lifecycle outcome",
                retry_after_seconds=30,
            )
        if cancelled:
            return DispatchOutcome(
                status=DispatchOutcomeStatus.reconciliation_required,
                detail="assignment stopped before the autonomous review was submitted",
            )
        if completion.outcome.status != DispatchOutcomeStatus.delivered:
            return completion.outcome
        review = completion.lilies_review
        if review is None or completion.development_result is not None:
            return DispatchOutcome(
                status=DispatchOutcomeStatus.retry,
                detail="delivered review dispatch omitted its strict LiliesReview",
                retry_after_seconds=30,
            )
        if (
            review.assignment_id != assignment.assignment_id
            or review.work_item_id != current.work_item_id
            or review.result_id != result.result_id
        ):
            return DispatchOutcome(
                status=DispatchOutcomeStatus.retry,
                detail="LiliesReview binding differed from the materialized result",
                retry_after_seconds=30,
            )
        await self._require_active(principal)
        try:
            await self.service.submit_review(
                principal=principal,
                review=review,
                expected_work_item_revision=current.revision,
                idempotency_key=f"worker:{outbox.outbox_id}:review",
            )
        except Exception:
            return DispatchOutcome(
                status=DispatchOutcomeStatus.retry,
                detail="LiliesReview was rejected by the fenced service",
                retry_after_seconds=30,
            )
        try:
            await self._require_active(principal)
        except Exception:
            return DispatchOutcome(
                status=DispatchOutcomeStatus.reconciliation_required,
                detail="assignment stopped immediately after review submission",
            )
        return completion.outcome

    async def dispatch(
        self,
        handler: DevelopmentDispatchHandler,
        *,
        context: RoleBoundDispatchContext,
    ) -> DispatchOutcome:
        outbox = context.outbox
        assignment = await self.service.store.get_assignment(context.assignment.assignment_id)
        projected = DevelopmentAssignmentProjection.from_assignment(
            assignment,
            outbox.destination_role,
        )
        work_item = await self.service.store.get_work_item(context.work_item.work_item_id)
        if projected != context.assignment or work_item != context.work_item:
            return DispatchOutcome(
                status=DispatchOutcomeStatus.reconciliation_required,
                detail=(
                    "trusted assignment or work-item state changed after the "
                    "role-bound dispatch projection was issued"
                ),
            )
        grant = projected.workspace_grant
        if outbox.kind == "work_dispatch":
            return await self._dispatch_work(
                handler,
                outbox=outbox,
                assignment=assignment,
                work_item=work_item,
                grant=grant,
            )
        if outbox.kind == "lilies_review":
            if outbox.destination_role != AgentRole.lilies:
                raise RuntimeError("review outbox must target Lilies")
            return await self._dispatch_review(
                handler,
                outbox=outbox,
                assignment=assignment,
                work_item=work_item,
                grant=grant,
            )
        raise RuntimeError("unsupported collaborative development outbox kind")

    def wrap(
        self,
        handler: DevelopmentDispatchHandler,
    ) -> DevelopmentDispatchHandler:
        async def wrapped(
            *,
            context: RoleBoundDispatchContext,
        ) -> DispatchOutcome:
            return await self.dispatch(
                handler,
                context=context,
            )

        return wrapped


class WorkerBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    dispatcher_id: str
    journal_path: str
    records: tuple[DispatchHistoryRecord, ...]


async def run_dispatch_worker(
    *,
    database_path: Path,
    journal_path: Path,
    handlers: Mapping[AgentRole, DevelopmentDispatchHandler],
    once: bool,
    poll_interval_seconds: float,
    limit: int,
    claim_ttl_seconds: int,
    dispatcher_id: str | None = None,
    stop_event: asyncio.Event | None = None,
    on_batch: Callable[[WorkerBatch], Any] | None = None,
    lifecycle_bridge: AutonomousDevelopmentLifecycleBridge | None = None,
) -> WorkerBatch:
    """Run one batch or continuously poll until ``stop_event`` is set."""

    if not 0.05 <= poll_interval_seconds <= 60:
        raise ValueError("worker poll interval must be between 0.05 and 60 seconds")
    if not 1 <= limit <= 5_000:
        raise ValueError("worker batch limit must be between 1 and 5000")
    if not once and stop_event is None:
        stop_event = asyncio.Event()

    resolved_journal = journal_path.expanduser().resolve()
    effective_dispatcher_id = dispatcher_id or f"worker-{uuid4().hex}"
    effective_handlers = (
        {role: lifecycle_bridge.wrap(handler) for role, handler in handlers.items()}
        if lifecycle_bridge is not None
        else handlers
    )
    dispatcher = CollaborativeDevelopmentDispatcher(
        store=CollaborativeDevelopmentStore(database_path.expanduser().resolve()),
        journal=CollaborativeDevelopmentDispatchJournal(resolved_journal),
        handlers=effective_handlers,
        dispatcher_id=effective_dispatcher_id,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    await dispatcher.initialize()

    while True:
        records = tuple(await dispatcher.dispatch_once(limit=limit))
        batch = WorkerBatch(
            dispatcher_id=effective_dispatcher_id,
            journal_path=str(resolved_journal),
            records=records,
        )
        if on_batch is not None and (records or once):
            on_batch(batch)
        if once:
            return batch
        assert stop_event is not None
        if stop_event.is_set():
            return batch
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=poll_interval_seconds,
            )
        except TimeoutError:
            continue


__all__ = [
    "AutonomousDevelopmentLifecycleBridge",
    "AutonomousHandlerCompletion",
    "ExternalDispatchEnvelope",
    "ExternalDispatchResponse",
    "ExternalJsonArgvDispatchHandler",
    "WorkerBatch",
    "run_dispatch_worker",
]
