from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


_POLICY_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_POLICY_LIST_FIELDS = (
    "allowed_nested_application_ids",
    "allowed_runtime_tools",
    "allowed_network_hosts",
    "allowed_connector_operations",
    "writable_connector_operations",
    "permission_required_connector_operations",
    "compensation_connector_operations",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode()).hexdigest()}"


def _normalized_policy_data(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    for field in _POLICY_LIST_FIELDS:
        values = normalized.get(field, ())
        if not isinstance(values, (list, tuple, set, frozenset)):
            continue
        items = {
            (
                str(item).casefold().rstrip(".")
                if field == "allowed_network_hosts"
                else str(item)
            )
            for item in values
        }
        normalized[field] = sorted(item for item in items if item)
    boundary = normalized.get("workspace_boundary")
    if isinstance(boundary, str) and boundary:
        normalized["workspace_boundary"] = str(Path(boundary).resolve())
    return normalized


class ExecutionPolicyExpansionDenied(ValueError):
    """A caller attempted to run outside an immutable published policy."""


class ExecutionPolicySnapshot(BaseModel):
    """Immutable execution authority captured with a published workflow version."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    schema_version: Literal["1.0"] = "1.0"
    policy_digest: str = Field(pattern=_POLICY_DIGEST_PATTERN)
    workspace_boundary: str = Field(min_length=1, max_length=4_096)
    workspace_scope_digest: str = Field(pattern=_POLICY_DIGEST_PATTERN)
    assignment_id: UUID
    session_id: UUID
    allowed_nested_application_ids: tuple[str, ...] = Field(max_length=100)
    allowed_runtime_tools: tuple[str, ...] = Field(max_length=500)
    allowed_network_hosts: tuple[str, ...] = Field(max_length=100)
    model_access: bool
    allowed_connector_operations: tuple[str, ...] = Field(max_length=1_500)
    writable_connector_operations: tuple[str, ...] = Field(max_length=500)
    permission_required_connector_operations: tuple[str, ...] = Field(max_length=500)
    compensation_connector_operations: tuple[str, ...] = Field(max_length=500)
    max_connector_write_count: int = Field(ge=0, le=1_000_000)
    max_connector_payload_bytes: int = Field(ge=1, le=100 * 1024 * 1024)
    governed_host_actions: bool

    @model_validator(mode="before")
    @classmethod
    def normalize_and_verify_digest(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = _normalized_policy_data(value)
        boundary = normalized.get("workspace_boundary")
        if isinstance(boundary, str) and boundary:
            expected_scope = _digest({"workspace_boundary": boundary})
            if normalized.get("workspace_scope_digest") != expected_scope:
                raise ValueError("execution policy workspace scope digest mismatch")
        supplied = normalized.get("policy_digest")
        payload = {
            key: item
            for key, item in normalized.items()
            if key != "policy_digest"
        }
        if supplied != _digest(payload):
            raise ValueError("execution policy digest mismatch")
        return normalized

    @classmethod
    def build(
        cls,
        *,
        workspace_boundary: str,
        assignment_id: str | UUID,
        session_id: str | UUID,
        allowed_nested_application_ids: list[str] | tuple[str, ...] | set[str],
        allowed_runtime_tools: list[str] | tuple[str, ...] | set[str],
        allowed_network_hosts: list[str] | tuple[str, ...] | set[str],
        model_access: bool,
        allowed_connector_operations: list[str] | tuple[str, ...] | set[str],
        writable_connector_operations: list[str] | tuple[str, ...] | set[str],
        permission_required_connector_operations: (
            list[str] | tuple[str, ...] | set[str]
        ),
        compensation_connector_operations: list[str] | tuple[str, ...] | set[str],
        max_connector_write_count: int,
        max_connector_payload_bytes: int,
        governed_host_actions: bool,
    ) -> ExecutionPolicySnapshot:
        data = _normalized_policy_data(
            {
                "schema_version": "1.0",
                "workspace_boundary": workspace_boundary,
                "assignment_id": str(assignment_id),
                "session_id": str(session_id),
                "allowed_nested_application_ids": allowed_nested_application_ids,
                "allowed_runtime_tools": allowed_runtime_tools,
                "allowed_network_hosts": allowed_network_hosts,
                "model_access": model_access,
                "allowed_connector_operations": allowed_connector_operations,
                "writable_connector_operations": writable_connector_operations,
                "permission_required_connector_operations": (
                    permission_required_connector_operations
                ),
                "compensation_connector_operations": (
                    compensation_connector_operations
                ),
                "max_connector_write_count": max_connector_write_count,
                "max_connector_payload_bytes": max_connector_payload_bytes,
                "governed_host_actions": governed_host_actions,
            }
        )
        data["workspace_scope_digest"] = _digest(
            {"workspace_boundary": data["workspace_boundary"]}
        )
        data["policy_digest"] = _digest(data)
        return cls.model_validate(data)

    def public_projection(self) -> dict[str, Any]:
        """Expose the complete policy without leaking the host filesystem path."""

        projection = self.model_dump(mode="json", exclude={"workspace_boundary"})
        projection["workspace_scope"] = {
            "kind": "assignment_session",
            "digest": self.workspace_scope_digest,
        }
        projection.pop("workspace_scope_digest", None)
        return projection

    def constrained_by(
        self,
        *,
        workspace_boundary: str | None,
        assignment_id: str | None,
        session_id: str | None,
        allowed_nested_application_ids: list[str] | tuple[str, ...] | set[str] | None,
        allowed_runtime_tools: list[str] | tuple[str, ...] | set[str] | None,
        allowed_network_hosts: list[str] | tuple[str, ...] | set[str] | None,
        model_access: bool | None,
        allowed_connector_operations: list[str] | tuple[str, ...] | set[str] | None,
        writable_connector_operations: list[str] | tuple[str, ...] | set[str] | None,
        permission_required_connector_operations: (
            list[str] | tuple[str, ...] | set[str] | None
        ),
        compensation_connector_operations: (
            list[str] | tuple[str, ...] | set[str] | None
        ),
        max_connector_write_count: int | None,
        max_connector_payload_bytes: int | None,
        governed_host_actions: bool,
        allow_authority_rebind: bool = False,
    ) -> ExecutionPolicySnapshot:
        if (
            not allow_authority_rebind
            and assignment_id is not None
            and assignment_id != str(self.assignment_id)
        ):
            raise ExecutionPolicyExpansionDenied(
                "caller assignment differs from the published execution policy"
            )
        if (
            not allow_authority_rebind
            and session_id is not None
            and session_id != str(self.session_id)
        ):
            raise ExecutionPolicyExpansionDenied(
                "caller session differs from the published execution policy"
            )
        stored_boundary = Path(self.workspace_boundary).resolve()
        effective_boundary = stored_boundary
        if workspace_boundary is not None:
            caller_boundary = Path(workspace_boundary).resolve()
            if (
                not allow_authority_rebind
                and caller_boundary != stored_boundary
                and stored_boundary not in caller_boundary.parents
            ):
                raise ExecutionPolicyExpansionDenied(
                    "caller workspace exceeds the published execution policy"
                )
            effective_boundary = caller_boundary

        def narrowed(
            stored: tuple[str, ...],
            caller: list[str] | tuple[str, ...] | set[str] | None,
            *,
            casefold: bool = False,
        ) -> set[str]:
            stored_values = set(stored)
            if caller is None:
                return stored_values
            caller_values = {
                (
                    str(item).casefold().rstrip(".")
                    if casefold
                    else str(item)
                )
                for item in caller
            }
            return stored_values.intersection(caller_values)

        nested = narrowed(
            self.allowed_nested_application_ids,
            allowed_nested_application_ids,
        )
        runtime_tools = narrowed(
            self.allowed_runtime_tools,
            allowed_runtime_tools,
        )
        network_hosts = narrowed(
            self.allowed_network_hosts,
            allowed_network_hosts,
            casefold=True,
        )
        connector_operations = narrowed(
            self.allowed_connector_operations,
            allowed_connector_operations,
        )
        writable_operations = narrowed(
            self.writable_connector_operations,
            writable_connector_operations,
        ).intersection(connector_operations)
        compensation_operations = narrowed(
            self.compensation_connector_operations,
            compensation_connector_operations,
        ).intersection(connector_operations)
        caller_permissions = (
            set()
            if permission_required_connector_operations is None
            else {
                str(item)
                for item in permission_required_connector_operations
            }
        )
        permission_operations = (
            set(self.permission_required_connector_operations).union(
                caller_permissions
            )
        ).intersection(writable_operations)

        return self.build(
            workspace_boundary=str(effective_boundary),
            assignment_id=assignment_id or self.assignment_id,
            session_id=session_id or self.session_id,
            allowed_nested_application_ids=nested,
            allowed_runtime_tools=runtime_tools,
            allowed_network_hosts=network_hosts,
            model_access=(
                self.model_access
                if model_access is None
                else self.model_access and model_access
            ),
            allowed_connector_operations=connector_operations,
            writable_connector_operations=writable_operations,
            permission_required_connector_operations=permission_operations,
            compensation_connector_operations=compensation_operations,
            max_connector_write_count=(
                self.max_connector_write_count
                if max_connector_write_count is None
                else min(
                    self.max_connector_write_count,
                    max_connector_write_count,
                )
            ),
            max_connector_payload_bytes=(
                self.max_connector_payload_bytes
                if max_connector_payload_bytes is None
                else min(
                    self.max_connector_payload_bytes,
                    max_connector_payload_bytes,
                )
            ),
            governed_host_actions=(
                self.governed_host_actions or governed_host_actions
            ),
        )
