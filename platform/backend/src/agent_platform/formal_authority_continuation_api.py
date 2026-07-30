from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .connector_sdk import ConnectorConflict
from .external_builder_bootstrap import (
    ExternalBuilderBootstrapReceipt,
    ExternalBuilderBootstrapRequest,
    bootstrap_external_builder_async,
)
from .platform_blackbox_auth import (
    PlatformBlackboxAuthError,
    TaskCredentialGrant,
    TaskCredentialRecord,
)


_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_COLLABORATION_CREDENTIAL_REF_PATTERN = re.compile(
    r"^collaboration_([0-9a-f]{32})$"
)
_LOGGER = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ContinueFormalAuthorityRequest(_StrictModel):
    """Owner-authorized, exact-policy continuation for an expired assignment."""

    schema_version: Literal["1.0"] = "1.0"
    session_id: UUID
    channel_id: UUID
    previous_platform_credential_ref: str = Field(min_length=20, max_length=160)
    previous_collaboration_credential_ref: str = Field(min_length=20, max_length=160)
    new_platform_credential_id: UUID
    new_platform_access_token: SecretStr = Field(
        min_length=48,
        max_length=512,
        json_schema_extra={"writeOnly": True, "format": "password"},
    )
    new_collaboration_credential_id: UUID
    new_collaboration_access_token: SecretStr = Field(
        min_length=48,
        max_length=512,
        json_schema_extra={"writeOnly": True, "format": "password"},
    )
    ttl_seconds: int = Field(ge=60, le=10_800)
    idempotency_key: str = Field(min_length=16, max_length=128)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("idempotency_key")
    @classmethod
    def idempotency_key_is_safe(cls, value: str) -> str:
        if _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError("idempotency_key contains unsupported characters")
        return value


class ContinuedCredential(_StrictModel):
    credential_id: UUID
    credential_ref: str
    previous_credential_ref: str
    expires_at: datetime


class ContinueFormalAuthorityResponse(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    session_id: UUID
    channel_id: UUID
    application_ids: list[UUID]
    platform: ContinuedCredential
    collaboration: ContinuedCredential
    policy_digest: str
    continuation_digest: str
    reason: str


class RotateFormalAuthorityRequest(_StrictModel):
    """Start one fresh Builder attempt while preserving the frozen task policy."""

    schema_version: Literal["1.0"] = "1.0"
    session_id: UUID
    channel_id: UUID
    previous_platform_credential_ref: str = Field(min_length=20, max_length=160)
    previous_collaboration_credential_ref: str = Field(min_length=20, max_length=160)
    application_id: UUID
    task_id: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    revision: int = Field(ge=1)
    environment_instance_id: str = Field(
        min_length=3,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    retire_predecessor_channel: Literal[True]
    rotation_id: UUID
    idempotency_key: str = Field(min_length=16, max_length=128)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("idempotency_key")
    @classmethod
    def idempotency_key_is_safe(cls, value: str) -> str:
        if _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError("idempotency_key contains unsupported characters")
        return value


class RotateFormalAuthorityResponse(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    predecessor_assignment_id: UUID
    predecessor_session_id: UUID
    predecessor_channel_id: UUID
    rotation_id: UUID
    bootstrap: ExternalBuilderBootstrapReceipt
    rotation_digest: str
    reason: str


class RebindFormalConnectorCredentialRequest(_StrictModel):
    """Owner-only, secret-free connector credential reference rotation."""

    schema_version: Literal["1.0"] = "1.0"
    connector_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    connector_version: int = Field(ge=1)
    tenant_id: str = Field(min_length=1, max_length=200)
    application_id: UUID
    expected_binding_revision: int = Field(ge=1)
    current_secret_ref: str = Field(
        min_length=12,
        max_length=512,
        pattern=r"^secret://[^/]+/[^/]+$",
    )
    replacement_secret_ref: str = Field(
        min_length=12,
        max_length=512,
        pattern=r"^secret://[^/]+/[^/]+$",
    )
    reason: str = Field(min_length=3, max_length=500)


class RebindFormalConnectorCredentialResponse(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    connector_id: str
    connector_version: int
    tenant_id: str
    application_id: UUID
    previous_secret_ref: str
    secret_ref: str
    previous_binding_revision: int
    binding_revision: int
    changed: bool
    rebind_digest: str
    reason: str


def _split_platform_secret_ref(secret_ref: str) -> tuple[str, str]:
    prefix = "secret://"
    if not secret_ref.startswith(prefix):
        raise ValueError("connector binding credential is not a PlatformHarness secret")
    owner_id, separator, name = secret_ref.removeprefix(prefix).partition("/")
    if not separator or not owner_id or not name or "/" in name:
        raise ValueError("connector binding credential secret reference is invalid")
    return owner_id, name


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _operation_key(kind: str, assignment_id: UUID, supplied: str) -> str:
    semantic = _digest(
        {
            "kind": kind,
            "assignment_id": str(assignment_id),
            "idempotency_key": supplied,
        }
    ).removeprefix("sha256:")
    return f"authority.continue.{kind}.{semantic[:48]}"


def _task_token_matches_id(token: str, credential_id: UUID) -> bool:
    prefix = f"lpt_{credential_id.hex}_"
    return token.startswith(prefix) and len(token) >= len(prefix) + 32


def _collaboration_credential_id(credential_ref: str) -> UUID:
    match = _COLLABORATION_CREDENTIAL_REF_PATTERN.fullmatch(credential_ref)
    if match is None:
        raise ValueError("previous collaboration credential reference is invalid")
    return UUID(hex=match.group(1))


def _task_policy(record: TaskCredentialRecord) -> dict[str, Any]:
    return {
        "assignment_id": str(record.assignment_id),
        "session_id": str(record.session_id),
        "scopes": sorted(scope.value for scope in record.scopes),
        "application_ids": sorted(str(item) for item in record.application_ids),
        "allowed_operations": sorted(item.value for item in record.allowed_operations),
        "allowed_actions_digest": record.allowed_actions_digest,
        "budget_digest": record.budget_digest,
        "allowed_network_hosts": sorted(record.allowed_network_hosts),
        "model_access": record.model_access,
        "file_access": record.file_access,
        "connector_access": record.connector_access,
        "readable_host_objects": sorted(record.readable_host_objects),
        "writable_host_operations": sorted(record.writable_host_operations),
        "permission_required_actions": sorted(record.permission_required_actions),
        "max_write_count": record.max_write_count,
        "max_payload_bytes": record.max_payload_bytes,
        "compensation_actions": sorted(record.compensation_actions),
        "max_report_evidence_rounds": record.max_report_evidence_rounds,
        "stable_hidden_runs": record.stable_hidden_runs,
    }


def _clone_grant(
    record: TaskCredentialRecord,
    *,
    expires_at: datetime,
) -> TaskCredentialGrant:
    return TaskCredentialGrant(
        assignment_id=record.assignment_id,
        session_id=record.session_id,
        scopes=list(record.scopes),
        application_ids=list(record.application_ids),
        allowed_operations=list(record.allowed_operations),
        allowed_actions_digest=record.allowed_actions_digest,
        budget_digest=record.budget_digest,
        allowed_network_hosts=list(record.allowed_network_hosts),
        model_access=record.model_access,
        file_access=record.file_access,
        connector_access=record.connector_access,
        readable_host_objects=list(record.readable_host_objects),
        writable_host_operations=list(record.writable_host_operations),
        permission_required_actions=list(record.permission_required_actions),
        max_write_count=record.max_write_count,
        max_payload_bytes=record.max_payload_bytes,
        compensation_actions=list(record.compensation_actions),
        max_report_evidence_rounds=record.max_report_evidence_rounds,
        stable_hidden_runs=record.stable_hidden_runs,
        expires_at=expires_at,
    )


def _rotation_identifiers(
    *,
    predecessor_assignment_id: UUID,
    rotation_id: UUID,
) -> dict[str, UUID]:
    namespace = (
        "lilies:formal-authority-rotation:"
        f"{predecessor_assignment_id}:{rotation_id}"
    )
    return {
        label: uuid5(NAMESPACE_URL, f"{namespace}:{label}")
        for label in ("assignment_id", "build_id", "session_id", "connection_id")
    }


def _rotation_bootstrap_idempotency_key(
    *,
    owner_idempotency_key: str,
    rotation_id: UUID,
) -> str:
    semantic = _digest(
        {
            "kind": "formal_authority_rotation_bootstrap",
            "owner_idempotency_key": owner_idempotency_key,
            "rotation_id": str(rotation_id),
        }
    ).removeprefix("sha256:")
    return f"authority.rotate.{semantic}"


def _rotation_task_token_factory(
    *,
    signing_key: str,
    rotation_id: UUID,
) -> Any:
    key = signing_key.encode("utf-8")
    if len(key) < 32:
        raise ValueError("formal authority rotation signing key is unavailable")

    def create(credential_id: UUID) -> str:
        digest = hmac.new(
            key,
            (
                "lilies:formal-authority-rotation-task-token:v1:"
                f"{rotation_id}:{credential_id}"
            ).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        suffix = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return f"lpt_{credential_id.hex}_{suffix}"

    return create


def _rotation_handoff_path(
    handoff_root: Path,
    *,
    assignment_id: UUID,
) -> Path:
    if not handoff_root.is_absolute() or handoff_root.is_symlink():
        raise ValueError("formal authority rotation handoff root is unsafe")
    handoff_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    handoff_root.chmod(0o700)
    return handoff_root / f"{assignment_id}.json"


def _require_expired_task_credential(
    record: TaskCredentialRecord,
    *,
    assignment_id: UUID,
    session_id: UUID,
    now: datetime,
    allow_retired: bool = False,
    allow_active_retirement: bool = False,
) -> None:
    if record.assignment_id != assignment_id or record.session_id != session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "formal_authority_not_found",
                "message": "formal assignment authority was not found",
            },
        )
    if record.revoked_at is not None and not allow_retired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "revoked_authority_cannot_continue",
                "message": "revoked formal authority cannot be continued",
            },
        )
    if record.expires_at > now and not allow_active_retirement:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "authority_not_expired",
                "message": "only expired formal authority can be continued",
            },
        )


def _require_expired_collaboration_credential(
    *,
    export: dict[str, Any],
    credential_id: UUID,
    assignment_id: UUID,
    session_id: UUID,
    now: datetime,
    allow_retired: bool = False,
    allow_active_retirement: bool = False,
) -> dict[str, Any]:
    channel = dict(export.get("channel") or {})
    channel_status = str(channel.get("status"))
    if (
        str(channel.get("assignment_id")) != str(assignment_id)
        or str(channel.get("lilies_session_id")) != str(session_id)
        or channel_status
        not in (
            {"active", "disconnected", "closed"}
            if allow_retired
            else {"active", "disconnected"}
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "formal_collaboration_not_found",
                "message": "formal collaboration authority was not found",
            },
        )
    matches = [
        dict(item)
        for item in export.get("credentials", [])
        if str(item.get("credential_id")) == str(credential_id)
    ]
    if len(matches) != 1:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "formal_collaboration_not_found",
                "message": "formal collaboration authority was not found",
            },
        )
    record = matches[0]
    if (
        str(record.get("assignment_id")) != str(assignment_id)
        or str(record.get("lilies_session_id")) != str(session_id)
        or str(record.get("role")) != "lilies"
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "formal_collaboration_not_found",
                "message": "formal collaboration authority was not found",
            },
        )
    if record.get("revoked_at") is not None and not (
        allow_retired and channel_status == "closed"
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "revoked_authority_cannot_continue",
                "message": "revoked formal collaboration authority cannot be continued",
            },
        )
    expires_at = datetime.fromisoformat(str(record["expires_at"]))
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        expires_at.astimezone(timezone.utc) > now
        and record.get("revoked_at") is None
        and not allow_active_retirement
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "authority_not_expired",
                "message": "only expired formal collaboration authority can be continued",
            },
        )
    return record


def install_formal_authority_continuation_api(
    app: FastAPI,
    services: Any,
    *,
    require_user_token: Any,
    handoff_root: Path,
    token_derivation_key: str,
) -> None:
    @app.put(
        "/api/v1/platform/formal-environment/connector-bindings/secret-ref",
        response_model=RebindFormalConnectorCredentialResponse,
        dependencies=[Depends(require_user_token)],
        tags=["formal-environment"],
        include_in_schema=False,
    )
    async def rebind_formal_connector_credential(
        body: RebindFormalConnectorCredentialRequest,
    ) -> RebindFormalConnectorCredentialResponse:
        bindings = await services.connectors.list_bindings(
            body.connector_id,
            tenant_id=body.tenant_id,
            application_id=str(body.application_id),
        )
        matches = [
            binding
            for binding in bindings
            if binding.connector_version == body.connector_version
            and str(body.application_id) in binding.application_ids
        ]
        if len(matches) != 1:
            raise HTTPException(
                status_code=(
                    status.HTTP_404_NOT_FOUND
                    if not matches
                    else status.HTTP_409_CONFLICT
                ),
                detail={
                    "code": "connector_binding_not_exact",
                    "message": "formal connector binding did not resolve uniquely",
                },
            )
        binding = matches[0]
        if binding.revision != body.expected_binding_revision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "connector_binding_revision_conflict",
                    "message": "formal connector binding changed before credential rebind",
                },
            )
        if binding.secret_ref != body.current_secret_ref:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "connector_binding_secret_conflict",
                    "message": "formal connector binding references another credential",
                },
            )
        current_owner, _current_name = _split_platform_secret_ref(
            body.current_secret_ref
        )
        replacement_owner, replacement_name = _split_platform_secret_ref(
            body.replacement_secret_ref
        )
        if current_owner != replacement_owner:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "connector_binding_secret_owner_mismatch",
                    "message": "credential rebind cannot cross PlatformHarness owners",
                },
            )
        available_secrets = await services.harness.list_secrets(
            owner_id=replacement_owner
        )
        if not any(
            item.get("name") == replacement_name
            and item.get("encrypted") is True
            for item in available_secrets
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "replacement_connector_secret_not_found",
                    "message": "replacement connector credential is unavailable",
                },
            )
        changed = body.current_secret_ref != body.replacement_secret_ref
        saved = binding
        if changed:
            try:
                saved = await services.connectors.upsert_binding(
                    binding.model_copy(
                        update={"secret_ref": body.replacement_secret_ref}
                    ),
                    expected_revision=body.expected_binding_revision,
                )
            except ConnectorConflict as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "connector_binding_rebind_conflict",
                        "message": "formal connector credential rebind conflicted",
                    },
                ) from error
            except (KeyError, ValueError) as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "code": "connector_binding_rebind_invalid",
                        "message": "formal connector credential rebind was invalid",
                    },
                ) from error
        semantic = {
            "connector_id": saved.connector_id,
            "connector_version": saved.connector_version,
            "tenant_id": saved.tenant_id,
            "application_id": str(body.application_id),
            "previous_secret_ref": body.current_secret_ref,
            "secret_ref": saved.secret_ref,
            "previous_binding_revision": body.expected_binding_revision,
            "binding_revision": saved.revision,
            "changed": changed,
            "reason": body.reason,
        }
        return RebindFormalConnectorCredentialResponse(
            **semantic,
            rebind_digest=_digest(semantic),
        )

    @app.post(
        "/api/v1/formal-assignments/{assignment_id}/authority/continue",
        response_model=ContinueFormalAuthorityResponse,
        dependencies=[Depends(require_user_token)],
        tags=["formal-authority"],
        include_in_schema=False,
    )
    async def continue_formal_assignment_authority(
        assignment_id: UUID,
        body: ContinueFormalAuthorityRequest,
    ) -> ContinueFormalAuthorityResponse:
        now = _utc_now()
        platform_token = body.new_platform_access_token.get_secret_value()
        collaboration_token = body.new_collaboration_access_token.get_secret_value()
        if not _task_token_matches_id(
            platform_token,
            body.new_platform_credential_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "prepared_task_token_invalid",
                    "message": "prepared task token is not bound to its credential id",
                },
            )
        if (
            len(collaboration_token) < 48
            or hmac.compare_digest(platform_token, collaboration_token)
            or body.new_platform_credential_id
            == body.new_collaboration_credential_id
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "prepared_credentials_invalid",
                    "message": "continued credentials must be distinct and well formed",
                },
            )

        try:
            previous_platform = (
                await services.platform_blackbox_auth.get_credential(
                    body.previous_platform_credential_ref
                )
            )
        except PlatformBlackboxAuthError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "formal_authority_not_found",
                    "message": "formal assignment authority was not found",
                },
            ) from error
        _require_expired_task_credential(
            previous_platform,
            assignment_id=assignment_id,
            session_id=body.session_id,
            now=now,
        )
        if not previous_platform.application_ids:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "formal_application_binding_missing",
                    "message": "formal authority has no application binding",
                },
            )

        previous_collaboration_id = _collaboration_credential_id(
            body.previous_collaboration_credential_ref
        )
        export = await services.collaboration.store.export_channel(body.channel_id)
        previous_collaboration = _require_expired_collaboration_credential(
            export=export,
            credential_id=previous_collaboration_id,
            assignment_id=assignment_id,
            session_id=body.session_id,
            now=now,
        )

        expires_at = now + timedelta(seconds=body.ttl_seconds)
        issued_platform = await services.platform_blackbox_auth.issue_credential(
            _clone_grant(previous_platform, expires_at=expires_at),
            idempotency_key=_operation_key(
                "platform",
                assignment_id,
                body.idempotency_key,
            ),
            prepared_access_token=body.new_platform_access_token,
            credential_id=body.new_platform_credential_id,
        )
        if _task_policy(issued_platform.credential) != _task_policy(
            previous_platform
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "authority_policy_drift",
                    "message": "continued platform authority changed the frozen policy",
                },
            )

        register_secret = getattr(
            services.collaboration.store,
            "register_secret_value",
            None,
        )
        if callable(register_secret):
            register_secret(collaboration_token)
        collaboration_record = await services.collaboration.store.provision_credential(
            {
                "credential_id": str(body.new_collaboration_credential_id),
                "channel_id": str(body.channel_id),
                "assignment_id": str(assignment_id),
                "lilies_session_id": str(body.session_id),
                "role": "lilies",
                "scopes": sorted(str(item) for item in previous_collaboration["scopes"]),
                "expires_at": expires_at.isoformat(),
                "idempotency_key": _operation_key(
                    "collaboration",
                    assignment_id,
                    body.idempotency_key,
                ),
            },
            collaboration_token,
        )

        policy_digest = _digest(
            {
                "platform": _task_policy(previous_platform),
                "collaboration": {
                    "assignment_id": str(previous_collaboration["assignment_id"]),
                    "lilies_session_id": str(
                        previous_collaboration["lilies_session_id"]
                    ),
                    "channel_id": str(previous_collaboration["channel_id"]),
                    "role": str(previous_collaboration["role"]),
                    "scopes": sorted(previous_collaboration["scopes"]),
                },
            }
        )
        response_semantic = {
            "assignment_id": str(assignment_id),
            "session_id": str(body.session_id),
            "channel_id": str(body.channel_id),
            "application_ids": sorted(
                str(item) for item in previous_platform.application_ids
            ),
            "previous_platform_credential_ref": (
                body.previous_platform_credential_ref
            ),
            "platform_credential_ref": (
                issued_platform.credential.credential_ref
            ),
            "previous_collaboration_credential_ref": (
                body.previous_collaboration_credential_ref
            ),
            "collaboration_credential_ref": (
                f"collaboration_{body.new_collaboration_credential_id.hex}"
            ),
            "expires_at": expires_at.isoformat(),
            "policy_digest": policy_digest,
            "reason": body.reason,
        }
        return ContinueFormalAuthorityResponse(
            assignment_id=assignment_id,
            session_id=body.session_id,
            channel_id=body.channel_id,
            application_ids=list(previous_platform.application_ids),
            platform=ContinuedCredential(
                credential_id=issued_platform.credential.credential_id,
                credential_ref=issued_platform.credential.credential_ref,
                previous_credential_ref=body.previous_platform_credential_ref,
                expires_at=issued_platform.credential.expires_at,
            ),
            collaboration=ContinuedCredential(
                credential_id=body.new_collaboration_credential_id,
                credential_ref=(
                    f"collaboration_{body.new_collaboration_credential_id.hex}"
                ),
                previous_credential_ref=(
                    body.previous_collaboration_credential_ref
                ),
                expires_at=datetime.fromisoformat(
                    str(collaboration_record["expires_at"])
                ),
            ),
            policy_digest=policy_digest,
            continuation_digest=_digest(response_semantic),
            reason=body.reason,
        )

    @app.post(
        "/api/v1/formal-assignments/{assignment_id}/authority/rotate",
        response_model=RotateFormalAuthorityResponse,
        dependencies=[Depends(require_user_token)],
        tags=["formal-authority"],
        include_in_schema=False,
    )
    async def rotate_formal_assignment_authority(
        assignment_id: UUID,
        body: RotateFormalAuthorityRequest,
    ) -> RotateFormalAuthorityResponse:
        now = _utc_now()
        try:
            previous_platform = (
                await services.platform_blackbox_auth.get_credential(
                    body.previous_platform_credential_ref
                )
            )
        except PlatformBlackboxAuthError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "formal_authority_not_found",
                    "message": "formal assignment authority was not found",
                },
            ) from error
        _require_expired_task_credential(
            previous_platform,
            assignment_id=assignment_id,
            session_id=body.session_id,
            now=now,
            allow_retired=True,
            allow_active_retirement=True,
        )
        if body.application_id not in previous_platform.application_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "formal_application_binding_missing",
                    "message": "formal authority has no matching application binding",
                },
            )

        previous_collaboration_id = _collaboration_credential_id(
            body.previous_collaboration_credential_ref
        )
        export = await services.collaboration.store.export_channel(body.channel_id)
        _require_expired_collaboration_credential(
            export=export,
            credential_id=previous_collaboration_id,
            assignment_id=assignment_id,
            session_id=body.session_id,
            now=now,
            allow_retired=True,
            allow_active_retirement=True,
        )
        predecessor_channel = dict(export.get("channel") or {})
        if str(predecessor_channel.get("status")) != "closed":
            try:
                await services.collaboration.close_formal_assignment_channel(
                    assignment_mode="formal_experiment",
                    task_id=body.task_id,
                    task_revision=body.revision,
                    assignment_id=assignment_id,
                    lilies_session_id=body.session_id,
                    application_ids=[body.application_id],
                )
            except Exception as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "predecessor_authority_retirement_failed",
                        "message": (
                            "predecessor collaboration authority could not be "
                            "retired exactly"
                        ),
                    },
                ) from error
        try:
            await (
                services.local_lilies_bridge.store
                .retire_external_builder_assignment(
                    assignment_id,
                    session_id=body.session_id,
                    application_id=body.application_id,
                    credential_ref=body.previous_platform_credential_ref,
                    collaboration_credential_ref=(
                        body.previous_collaboration_credential_ref
                    ),
                    reason=body.reason,
                )
            )
            await services.platform_blackbox_auth.revoke_credential(
                body.previous_platform_credential_ref,
                reason=(
                    "formal authority retired before successor rotation: "
                    f"{body.reason}"
                )[:1_000],
            )
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "predecessor_authority_retirement_failed",
                    "message": (
                        "predecessor Builder authority could not be retired "
                        "exactly"
                    ),
                },
            ) from error

        identifiers = _rotation_identifiers(
            predecessor_assignment_id=assignment_id,
            rotation_id=body.rotation_id,
        )
        handoff_path = _rotation_handoff_path(
            handoff_root,
            assignment_id=identifiers["assignment_id"],
        )
        request = ExternalBuilderBootstrapRequest(
            task_id=body.task_id,
            revision=body.revision,
            assignment_id=identifiers["assignment_id"],
            application_id=body.application_id,
            build_id=identifiers["build_id"],
            session_id=identifiers["session_id"],
            connection_id=identifiers["connection_id"],
            environment_instance_id=body.environment_instance_id,
            idempotency_key=_rotation_bootstrap_idempotency_key(
                owner_idempotency_key=body.idempotency_key,
                rotation_id=body.rotation_id,
            ),
            builder_actor="codex",
            handoff_path=handoff_path,
        )
        try:
            receipt = await bootstrap_external_builder_async(
                services=services,
                request=request,
                task_token_factory=_rotation_task_token_factory(
                    signing_key=token_derivation_key,
                    rotation_id=body.rotation_id,
                ),
            )
        except (ValueError, RuntimeError) as error:
            _LOGGER.exception(
                "fresh formal Builder authority preparation failed",
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "formal_authority_rotation_failed",
                    "message": "fresh formal Builder authority could not be prepared",
                },
            ) from error

        semantic = {
            "predecessor_assignment_id": str(assignment_id),
            "predecessor_session_id": str(body.session_id),
            "predecessor_channel_id": str(body.channel_id),
            "rotation_id": str(body.rotation_id),
            "new_assignment_id": str(receipt.assignment_id),
            "new_session_id": str(receipt.session_id),
            "new_channel_id": str(receipt.channel_id),
            "application_id": str(receipt.application_id),
            "task_id": receipt.task_id,
            "revision": receipt.revision,
            "handoff_digest": receipt.handoff_digest,
            "reason": body.reason,
        }
        return RotateFormalAuthorityResponse(
            predecessor_assignment_id=assignment_id,
            predecessor_session_id=body.session_id,
            predecessor_channel_id=body.channel_id,
            rotation_id=body.rotation_id,
            bootstrap=receipt,
            rotation_digest=_digest(semantic),
            reason=body.reason,
        )
