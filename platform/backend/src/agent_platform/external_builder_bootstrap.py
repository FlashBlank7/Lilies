from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .formal_assignment_broker import (
    PrepareFormalAssignmentRequest,
    PreparedFormalAssignment,
)
from .lilies_models import AssignmentMode, BuildAssignment, OpaqueReference
from .platform_blackbox_auth import (
    IssuedTaskCredential,
    PlatformBlackboxOperation,
    PlatformBlackboxScope,
    TaskCredentialGrant,
)


class ExternalBuilderBootstrapError(RuntimeError):
    """A trusted external Builder bootstrap failed closed."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class ExternalBuilderBootstrapRequest(_FrozenModel):
    """Host-owned inputs required to prepare one external Builder attempt."""

    schema_version: Literal["1.0"] = "1.0"
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
    idempotency_key: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    builder_actor: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    handoff_path: Path

    @field_validator("handoff_path")
    @classmethod
    def handoff_path_is_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("external Builder handoff_path must be absolute")
        return value

    def formal_request(self) -> PrepareFormalAssignmentRequest:
        return PrepareFormalAssignmentRequest(
            task_id=self.task_id,
            revision=self.revision,
            assignment_id=self.assignment_id,
            application_id=self.application_id,
            build_id=self.build_id,
            session_id=self.session_id,
            connection_id=self.connection_id,
            environment_instance_id=self.environment_instance_id,
            idempotency_key=self.idempotency_key,
        )


class ExternalBuilderBootstrapReceipt(_FrozenModel):
    """Secret-free owner observation for an external Builder handoff."""

    schema_version: Literal["1.0"] = "1.0"
    builder_actor: str
    task_id: str
    revision: int
    run_id: OpaqueReference
    assignment_id: UUID
    application_id: UUID
    build_id: UUID
    session_id: UUID
    connection_id: UUID
    environment_instance_id: OpaqueReference
    channel_id: UUID
    task_credential_ref: OpaqueReference
    collaboration_credential_ref: OpaqueReference
    contract_digest: str
    assignment_bundle_digest: str
    workspace_manifest_digest: str
    workspace_policy_digest: str
    expires_at: datetime
    handoff_path: Path
    handoff_digest: str
    formal_archive_supported: Literal[True] = True


class ExternalBuilderBootstrapService:
    """Prepare one API-native external Builder and its trusted archive lifecycle.

    ``services`` is intentionally structural.  The production caller passes
    ``app.state.services``; tests can provide narrow fakes.  The Builder never
    impersonates a local daemon: its separate durable lifecycle only binds the
    broker-prepared assignment, public request audit, formal draft provenance,
    collaboration channel, archive, and independent verifier.
    """

    def __init__(
        self,
        *,
        services: Any,
        task_token_factory: Callable[[UUID], str] | None = None,
    ) -> None:
        self._services = services
        self._task_token_factory = task_token_factory or _new_task_token

    async def bootstrap_async(
        self,
        request: ExternalBuilderBootstrapRequest,
    ) -> ExternalBuilderBootstrapReceipt:
        _preflight_handoff_target(
            request.handoff_path,
            allow_exact_replay=True,
        )
        broker = _formal_broker(self._services)
        auth_store = _auth_store(self._services)
        bridge_store = _external_builder_store(self._services)
        workflow_store = _workflow_store(self._services)

        prepared = PreparedFormalAssignment.model_validate(
            await broker.prepare_async(request.formal_request())
        )
        assignment = prepared.assignment
        _assert_prepared_binding(request=request, prepared=prepared)

        grant = _task_credential_grant(
            assignment=assignment,
            session_id=request.session_id,
        )
        credential_id = _task_credential_id(request.assignment_id)
        prepared_task_token = self._task_token_factory(credential_id)
        issued = IssuedTaskCredential.model_validate(
            await auth_store.issue_credential(
                grant,
                idempotency_key=f"credential.issue.{request.assignment_id.hex}",
                prepared_access_token=SecretStr(prepared_task_token),
                credential_id=credential_id,
            )
        )
        _assert_issued_binding(
            issued=issued,
            grant=grant,
            credential_id=credential_id,
            prepared_access_token=prepared_task_token,
        )

        collaboration_access = assignment.collaboration
        collaboration_activated = False
        reservation_created = False
        try:
            if collaboration_access is None:
                raise ExternalBuilderBootstrapError(
                    "formal assignment omitted collaboration authority"
                )
            collaboration_secret = await broker.collaboration_credential_secret(
                assignment,
                request.session_id,
            )
            collaboration_activated = True
            if not isinstance(collaboration_secret, SecretStr):
                raise ExternalBuilderBootstrapError(
                    "formal collaboration provider returned a non-secret credential"
                )
            collaboration_token = collaboration_secret.get_secret_value()
            if (
                not collaboration_token
                or secrets.compare_digest(
                    collaboration_token,
                    prepared_task_token,
                )
            ):
                raise ExternalBuilderBootstrapError(
                    "formal collaboration credential is empty or aliases task authority"
                )

            safe_request = request.model_dump(
                mode="json",
                exclude={"handoff_path"},
            )
            request_json = _canonical_json_bytes(safe_request).decode("utf-8")
            request_digest = (
                f"sha256:{hashlib.sha256(request_json.encode('utf-8')).hexdigest()}"
            )
            await workflow_store.begin_formal_draft_provenance(
                assignment_id=str(request.assignment_id),
                session_id=str(request.session_id),
                application_id=str(request.application_id),
            )
            await bridge_store.reserve_external_builder_assignment(
                assignment=assignment,
                session_id=request.session_id,
                connection_id=request.connection_id,
                request_json=request_json,
                request_digest=request_digest,
                credential_ref=issued.credential.credential_ref,
                collaboration_credential_ref=collaboration_access.credential_ref,
                task_token_secret_ref=issued.credential.credential_ref,
                builder_actor=request.builder_actor,
            )
            reservation_created = True

            handoff = _handoff_payload(
                request=request,
                prepared=prepared,
                issued=issued,
                collaboration_token=collaboration_token,
            )
            handoff_digest = await asyncio.to_thread(
                _write_private_json_once,
                request.handoff_path,
                handoff,
            )
        except Exception as error:
            if reservation_created:
                # The exact reservation makes a lost handoff write safely
                # recoverable with the same request and prepared token.
                raise
            cleanup_errors: list[Exception] = []
            close_collaboration = getattr(
                broker,
                "close_collaboration_authority",
                None,
            )
            if collaboration_activated:
                if not callable(close_collaboration):
                    cleanup_errors.append(
                        ExternalBuilderBootstrapError(
                            "formal collaboration cleanup is unavailable"
                        )
                    )
                else:
                    try:
                        await close_collaboration(
                            assignment,
                            request.session_id,
                        )
                    except Exception as cleanup_error:
                        cleanup_errors.append(cleanup_error)
            revoke_credential = getattr(auth_store, "revoke_credential", None)
            if not callable(revoke_credential):
                cleanup_errors.append(
                    ExternalBuilderBootstrapError(
                        "platform credential cleanup is unavailable"
                    )
                )
            else:
                try:
                    await revoke_credential(
                        issued.credential.credential_ref,
                        reason="external Builder bootstrap failed before handoff",
                    )
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise ExternalBuilderBootstrapError(
                    "external Builder bootstrap failed and authority cleanup "
                    "did not complete"
                ) from error
            raise
        return ExternalBuilderBootstrapReceipt(
            builder_actor=request.builder_actor,
            task_id=request.task_id,
            revision=request.revision,
            run_id=prepared.run_id,
            assignment_id=request.assignment_id,
            application_id=request.application_id,
            build_id=request.build_id,
            session_id=request.session_id,
            connection_id=request.connection_id,
            environment_instance_id=request.environment_instance_id,
            channel_id=collaboration_access.channel_id,
            task_credential_ref=issued.credential.credential_ref,
            collaboration_credential_ref=collaboration_access.credential_ref,
            contract_digest=assignment.platform.contract_digest,
            assignment_bundle_digest=prepared.bundle_digest,
            workspace_manifest_digest=prepared.workspace.manifest_digest,
            workspace_policy_digest=prepared.workspace.policy_digest,
            expires_at=assignment.constraints.deadline_at,
            handoff_path=request.handoff_path,
            handoff_digest=handoff_digest,
        )


async def bootstrap_external_builder_async(
    *,
    services: Any,
    request: ExternalBuilderBootstrapRequest,
    task_token_factory: Callable[[UUID], str] | None = None,
) -> ExternalBuilderBootstrapReceipt:
    """Host bootstrap callable for an app created in the same process."""

    return await ExternalBuilderBootstrapService(
        services=services,
        task_token_factory=task_token_factory,
    ).bootstrap_async(request)


def _formal_broker(services: Any) -> Any:
    bridge = getattr(services, "local_lilies_bridge", None)
    broker = getattr(bridge, "formal_assignment_broker", None)
    if broker is None or not callable(getattr(broker, "prepare_async", None)):
        raise ExternalBuilderBootstrapError(
            "formal assignment broker is unavailable"
        )
    if not callable(getattr(broker, "collaboration_credential_secret", None)):
        raise ExternalBuilderBootstrapError(
            "formal collaboration credential provider is unavailable"
        )
    return broker


def _auth_store(services: Any) -> Any:
    auth_store = getattr(services, "platform_blackbox_auth", None)
    if auth_store is None or not callable(getattr(auth_store, "issue_credential", None)):
        raise ExternalBuilderBootstrapError(
            "platform task credential issuer is unavailable"
        )
    return auth_store


def _external_builder_store(services: Any) -> Any:
    bridge = getattr(services, "local_lilies_bridge", None)
    store = getattr(bridge, "store", None)
    if store is None or not callable(
        getattr(store, "reserve_external_builder_assignment", None)
    ):
        raise ExternalBuilderBootstrapError(
            "external Builder lifecycle store is unavailable"
        )
    return store


def _workflow_store(services: Any) -> Any:
    store = getattr(services, "workflow_store", None)
    if store is None or not callable(
        getattr(store, "begin_formal_draft_provenance", None)
    ):
        raise ExternalBuilderBootstrapError(
            "formal workflow provenance store is unavailable"
        )
    return store


def _task_credential_id(assignment_id: UUID) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"lilies:platform-task-credential:{assignment_id}",
    )


def _new_task_token(credential_id: UUID) -> str:
    return f"lpt_{credential_id.hex}_{secrets.token_urlsafe(32)}"


def _assert_prepared_binding(
    *,
    request: ExternalBuilderBootstrapRequest,
    prepared: PreparedFormalAssignment,
) -> None:
    assignment = prepared.assignment
    task_ref = assignment.task_package
    constraints = assignment.constraints
    forbidden_workspace_segments = {
        ".git",
        ".hg",
        ".svn",
        "oracle",
        "protected",
        "expected-state",
        "expected_state",
        "platform-data",
        "platform_data",
    }
    workspace_parts = {
        part.casefold() for part in Path(prepared.workspace.path).parts
    }
    expected_credential_ref = (
        f"platform-task-credential:{_task_credential_id(request.assignment_id)}"
    )
    if (
        assignment.mode is not AssignmentMode.formal_experiment
        or assignment.assignment_id != request.assignment_id
        or assignment.idempotency_key != request.idempotency_key
        or task_ref is None
        or task_ref.task_id != request.task_id
        or task_ref.revision != request.revision
        or task_ref.run_id != prepared.run_id
        or task_ref.environment_instance_id != request.environment_instance_id
        or assignment.target.application_id != request.application_id
        or assignment.platform.application_ids != [request.application_id]
        or assignment.platform.credential_ref != expected_credential_ref
        or assignment.collaboration is None
        or assignment.collaboration.expires_at != constraints.deadline_at
        or bool(workspace_parts & forbidden_workspace_segments)
    ):
        raise ExternalBuilderBootstrapError(
            "prepared formal assignment escaped its requested identity binding"
        )
    required_policy = (
        task_ref.allowed_actions_digest,
        task_ref.budget_digest,
        constraints.model_access,
        constraints.file_access,
        constraints.connector_access,
        constraints.max_write_count,
        constraints.max_payload_bytes,
        constraints.max_report_evidence_rounds,
        constraints.stable_hidden_runs,
    )
    if any(value is None for value in required_policy):
        raise ExternalBuilderBootstrapError(
            "prepared formal assignment policy is incomplete"
        )


def _task_credential_grant(
    *,
    assignment: BuildAssignment,
    session_id: UUID,
) -> TaskCredentialGrant:
    task_ref = assignment.task_package
    constraints = assignment.constraints
    if task_ref is None:
        raise ExternalBuilderBootstrapError(
            "formal assignment omitted its task package"
        )
    try:
        return TaskCredentialGrant(
            assignment_id=assignment.assignment_id,
            session_id=session_id,
            scopes=[
                PlatformBlackboxScope(scope.value)
                for scope in assignment.platform.scopes
            ],
            application_ids=list(assignment.platform.application_ids),
            allowed_operations=[
                PlatformBlackboxOperation(action.value)
                for action in constraints.allowed_actions
            ],
            allowed_actions_digest=task_ref.allowed_actions_digest,
            budget_digest=task_ref.budget_digest,
            allowed_network_hosts=list(constraints.allowed_hosts),
            model_access=constraints.model_access,
            file_access=constraints.file_access,
            connector_access=constraints.connector_access,
            readable_host_objects=list(constraints.readable_host_objects),
            writable_host_operations=list(constraints.writable_host_operations),
            permission_required_actions=list(
                constraints.permission_required_actions
            ),
            max_write_count=constraints.max_write_count,
            max_payload_bytes=constraints.max_payload_bytes,
            compensation_actions=list(constraints.compensation_actions),
            max_report_evidence_rounds=constraints.max_report_evidence_rounds,
            stable_hidden_runs=constraints.stable_hidden_runs,
            expires_at=constraints.deadline_at,
        )
    except (TypeError, ValueError) as error:
        raise ExternalBuilderBootstrapError(
            "prepared formal assignment cannot produce an exact task credential policy"
        ) from error


def _assert_issued_binding(
    *,
    issued: IssuedTaskCredential,
    grant: TaskCredentialGrant,
    credential_id: UUID,
    prepared_access_token: str,
) -> None:
    credential = issued.credential
    expected_policy = grant.model_dump(
        mode="python",
        exclude={"schema_version"},
    )
    if (
        credential.credential_id != credential_id
        or credential.credential_ref != f"platform-task-credential:{credential_id}"
        or credential.revoked_at is not None
        or issued.access_token.get_secret_value() != prepared_access_token
        or any(
            not _issued_policy_value_matches(
                field_name,
                expected,
                getattr(credential, field_name),
            )
            for field_name, expected in expected_policy.items()
        )
    ):
        raise ExternalBuilderBootstrapError(
            "issued task credential differs from the frozen assignment policy"
        )


def _issued_policy_value_matches(
    field_name: str,
    expected: Any,
    actual: Any,
) -> bool:
    unordered_fields = {
        "scopes",
        "application_ids",
        "allowed_operations",
        "allowed_network_hosts",
        "readable_host_objects",
        "writable_host_operations",
        "permission_required_actions",
        "compensation_actions",
    }
    if field_name not in unordered_fields:
        return actual == expected

    def normalized(value: Any) -> str:
        resolved = getattr(value, "value", value)
        text = str(resolved)
        return text.casefold() if field_name == "allowed_network_hosts" else text

    return sorted(normalized(item) for item in actual) == sorted(
        normalized(item) for item in expected
    )


def _handoff_payload(
    *,
    request: ExternalBuilderBootstrapRequest,
    prepared: PreparedFormalAssignment,
    issued: IssuedTaskCredential,
    collaboration_token: str,
) -> dict[str, Any]:
    assignment = prepared.assignment
    collaboration_access = assignment.collaboration
    if collaboration_access is None:
        raise ExternalBuilderBootstrapError(
            "formal assignment omitted collaboration authority"
        )
    return {
        "schema_version": "1.0",
        "builder_actor": request.builder_actor,
        "formal_archive_supported": True,
        "task": {
            "task_id": request.task_id,
            "revision": request.revision,
            "run_id": prepared.run_id,
        },
        "assignment": {
            "assignment_id": str(request.assignment_id),
            "application_id": str(request.application_id),
            "build_id": str(request.build_id),
            "session_id": str(request.session_id),
            "connection_id": str(request.connection_id),
            "environment_instance_id": request.environment_instance_id,
            "bundle_digest": prepared.bundle_digest,
        },
        "workspace": {
            "path": prepared.workspace.path,
            "manifest_digest": prepared.workspace.manifest_digest,
            "policy_digest": prepared.workspace.policy_digest,
        },
        "platform": {
            "base_url": str(assignment.platform.base_url).rstrip("/"),
            "contract_url": assignment.platform.contract_url,
            "contract_digest": assignment.platform.contract_digest,
            "credential_ref": issued.credential.credential_ref,
            "access_token": issued.access_token.get_secret_value(),
            "expires_at": assignment.constraints.deadline_at.isoformat(),
        },
        "collaboration": {
            "base_url": str(assignment.platform.base_url).rstrip("/"),
            "channel_id": str(collaboration_access.channel_id),
            "credential_ref": collaboration_access.credential_ref,
            "access_token": collaboration_token,
            "expires_at": collaboration_access.expires_at.isoformat(),
        },
    }


def _preflight_handoff_target(
    path: Path,
    *,
    allow_exact_replay: bool = False,
) -> None:
    if not path.is_absolute():
        raise ExternalBuilderBootstrapError(
            "external Builder handoff path must be absolute"
        )
    parent = path.parent
    if parent.is_symlink():
        raise ExternalBuilderBootstrapError(
            "external Builder handoff parent must not be a symlink"
        )
    if parent.exists():
        try:
            if parent.resolve(strict=True) != parent:
                raise ExternalBuilderBootstrapError(
                    "external Builder handoff parent must not traverse a symlink"
                )
        except OSError as error:
            raise ExternalBuilderBootstrapError(
                "external Builder handoff parent cannot be resolved safely"
            ) from error
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise ExternalBuilderBootstrapError(
            "external Builder handoff path must not be a symlink"
        )
    if (
        allow_exact_replay
        and stat.S_ISREG(mode)
        and stat.S_IMODE(mode) == 0o600
    ):
        return
    raise ExternalBuilderBootstrapError(
        "external Builder handoff path already exists"
    )


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ExternalBuilderBootstrapError(
            "external Builder handoff is not canonical JSON"
        ) from error


def _write_private_json_once(path: Path, value: Any) -> str:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if parent.is_symlink() or parent.resolve(strict=True) != parent:
            raise ExternalBuilderBootstrapError(
                "external Builder handoff parent must not traverse a symlink"
            )
    except OSError as error:
        raise ExternalBuilderBootstrapError(
            "external Builder handoff parent cannot be resolved safely"
        ) from error

    payload = _canonical_json_bytes(value)
    try:
        existing_mode = path.lstat().st_mode
    except FileNotFoundError:
        existing_mode = None
    if existing_mode is not None:
        if (
            stat.S_ISLNK(existing_mode)
            or not stat.S_ISREG(existing_mode)
            or stat.S_IMODE(existing_mode) != 0o600
        ):
            raise ExternalBuilderBootstrapError(
                "external Builder handoff replay target is unsafe"
            )
        try:
            existing = path.read_bytes()
        except OSError as error:
            raise ExternalBuilderBootstrapError(
                "external Builder handoff replay target cannot be read"
            ) from error
        if not hmac.compare_digest(existing, payload):
            raise ExternalBuilderBootstrapError(
                "external Builder handoff conflicts with the existing authority"
            )
        return f"sha256:{hashlib.sha256(existing).hexdigest()}"

    temporary = parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            try:
                concurrent_mode = path.lstat().st_mode
                concurrent = path.read_bytes()
            except OSError as read_error:
                raise ExternalBuilderBootstrapError(
                    "external Builder handoff path was concurrently created"
                ) from read_error
            if (
                stat.S_ISLNK(concurrent_mode)
                or not stat.S_ISREG(concurrent_mode)
                or stat.S_IMODE(concurrent_mode) != 0o600
                or not hmac.compare_digest(concurrent, payload)
            ):
                raise ExternalBuilderBootstrapError(
                    "external Builder handoff path was concurrently created"
                ) from error
            return f"sha256:{hashlib.sha256(concurrent).hexdigest()}"
        os.chmod(path, 0o600, follow_symlinks=False)
        if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != 0o600:
            raise ExternalBuilderBootstrapError(
                "external Builder handoff did not retain mode 0600"
            )
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except ExternalBuilderBootstrapError:
        raise
    except OSError as error:
        raise ExternalBuilderBootstrapError(
            "external Builder handoff could not be written safely"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
