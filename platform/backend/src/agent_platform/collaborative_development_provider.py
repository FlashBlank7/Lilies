"""Provider authority bound to one collaborative-development dispatch grant.

The capability deliberately contains only public authority metadata.  A
credential value stays in the trusted control plane and may be injected into a
provider transport only after this binding validates the exact role grant.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .collaborative_development_models import AgentRole, SideEffect, WorkspaceGrant


_MAX_PROVIDER_COST_USD = 1_000_000
_DEEPSEEK_PROVIDER = "deepseek"


def _is_control(character: str) -> bool:
    return ord(character) < 32 or ord(character) == 127


def _canonical_digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _normalized_hostname(host: str) -> str:
    candidate = host.casefold()
    if not candidate or candidate.endswith(".") or "%" in candidate:
        raise ValueError("provider hosts must use canonical host names")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        try:
            candidate.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("provider hosts must use ASCII host names") from error
        if len(candidate) > 253:
            raise ValueError("provider host name is too long")
        labels = candidate.split(".")
        if any(
            not label
            or len(label) > 63
            or label[0] == "-"
            or label[-1] == "-"
            or any(
                not (character.isascii() and character.isalnum())
                and character != "-"
                for character in label
            )
            for label in labels
        ):
            raise ValueError("provider hosts must use canonical host names")
        return candidate
    return address.compressed


def _render_host_authority(host: str, port: int | None) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    if port is None:
        return rendered_host
    return f"{rendered_host}:{port}"


def _normalized_host_authority(authority: str) -> str:
    candidate = authority.strip().casefold()
    if (
        not candidate
        or "://" in candidate
        or any(
            character in candidate
            for character in ("/", "\\", "*", "@", "?", "#")
        )
        or any(character.isspace() or _is_control(character) for character in candidate)
    ):
        raise ValueError("provider hosts must be exact host authorities")

    port: int | None = None
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing < 0:
            raise ValueError("provider hosts must be exact host authorities")
        host = candidate[1:closing]
        remainder = candidate[closing + 1 :]
        if remainder:
            if not remainder.startswith(":") or not remainder[1:].isdigit():
                raise ValueError("provider hosts must be exact host authorities")
            port = int(remainder[1:])
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ValueError(
                "provider hosts must use canonical host names"
            ) from error
        if address.version != 6:
            raise ValueError("brackets are reserved for IPv6 provider hosts")
        normalized_host = address.compressed
    else:
        if candidate.count(":") > 1:
            raise ValueError("IPv6 provider hosts must use brackets")
        if ":" in candidate:
            host, port_text = candidate.rsplit(":", 1)
            if not port_text.isdigit():
                raise ValueError("provider hosts must use a numeric port")
            port = int(port_text)
        else:
            host = candidate
        normalized_host = _normalized_hostname(host)

    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("provider host port must be between 1 and 65535")
    return _render_host_authority(normalized_host, port)


def normalized_provider_endpoint_host(endpoint_url: str) -> str:
    """Return the exact host authority used by a HTTPS provider endpoint."""

    if (
        endpoint_url != endpoint_url.strip()
        or "\\" in endpoint_url
        or any(_is_control(character) for character in endpoint_url)
    ):
        raise ValueError("provider endpoint must be a credential-free HTTPS URL")
    parsed = urlparse(endpoint_url)
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("provider endpoint must be a credential-free HTTPS URL")
    host = _normalized_hostname(parsed.hostname)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("provider endpoint has an invalid port") from error
    if parsed.netloc.endswith(":"):
        raise ValueError("provider endpoint has an invalid port")
    if port is None or port == 443:
        port = None
    return _render_host_authority(host, port)


def _normalized_exact_hosts(hosts: tuple[str, ...]) -> tuple[str, ...]:
    normalized = [_normalized_host_authority(host) for host in hosts]
    if not normalized or len(normalized) != len(set(normalized)):
        raise ValueError("provider hosts must be non-empty and unique")
    return tuple(normalized)


def _validated_provider_request_id(provider_request_id: str) -> str:
    if (
        not isinstance(provider_request_id, str)
        or provider_request_id != provider_request_id.strip()
        or not 1 <= len(provider_request_id) <= 500
        or any(
            character in provider_request_id
            for character in ("\x00", "\n", "\r")
        )
        or any(_is_control(character) for character in provider_request_id)
    ):
        raise ValueError("provider request id must be an exact non-empty identifier")
    return provider_request_id


def _validated_cost(value: float, *, label: str, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized > _MAX_PROVIDER_COST_USD
        or (normalized <= 0 if positive else normalized < 0)
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(
            f"{label} must be a finite {qualifier} amount within the provider cap"
        )
    if normalized == 0:
        return 0.0
    return normalized


def _validated_token_count(value: int, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


class BoundDevelopmentProviderCapability(BaseModel):
    """Non-secret provider authority for one exact dispatch grant."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    agent_role: AgentRole
    workspace_id: UUID
    dispatch_grant_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provider: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    endpoint_hosts: tuple[str, ...] = Field(min_length=1)
    secret_refs: tuple[str, ...] = Field(min_length=1)
    allowed_side_effects: tuple[SideEffect, ...] = Field(min_length=1)
    credential_identity: str = Field(min_length=1, max_length=500)

    @field_validator("provider")
    @classmethod
    def provider_is_canonical(cls, value: str) -> str:
        normalized = value.casefold()
        if any(
            character.isspace() or _is_control(character)
            for character in normalized
        ):
            raise ValueError("provider must be a canonical identifier")
        return normalized

    @field_validator("model", "credential_identity")
    @classmethod
    def public_identifiers_are_safe(cls, value: str) -> str:
        if any(_is_control(character) for character in value):
            raise ValueError("public provider identifiers cannot contain control characters")
        return value

    @field_validator("secret_refs")
    @classmethod
    def secret_refs_are_exact(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            not item
            or item != item.strip()
            or any(character.isspace() or _is_control(character) for character in item)
            for item in value
        ):
            raise ValueError(
                "provider secret references must be exact, non-empty, and unique"
            )
        return value

    @field_validator("endpoint_hosts")
    @classmethod
    def endpoint_hosts_are_canonical(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _normalized_exact_hosts(value)

    @field_validator("allowed_side_effects")
    @classmethod
    def side_effects_are_unique(
        cls,
        value: tuple[SideEffect, ...],
    ) -> tuple[SideEffect, ...]:
        if len(value) != len(set(value)):
            raise ValueError("provider side effects must be unique")
        return value

    @model_validator(mode="after")
    def network_authority_is_explicit(
        self,
    ) -> BoundDevelopmentProviderCapability:
        if SideEffect.network_access not in self.allowed_side_effects:
            raise ValueError(
                "provider capability requires the network_access side effect"
            )
        return self

    @classmethod
    def bind(
        cls,
        *,
        assignment_id: UUID,
        expected_role: AgentRole,
        grant: WorkspaceGrant,
        provider: str,
        model: str,
        endpoint_url: str,
        secret_ref: str,
        credential_identity: str | None = None,
    ) -> BoundDevelopmentProviderCapability:
        """Bind only a narrowly scoped snapshot grant.

        Provider access is valid only when the grant carries exactly one
        normalized endpoint host, exactly the declared secret reference, and
        only process/network side effects.  The credential value is neither
        accepted nor retained by this model.
        """

        normalized_provider = provider.strip().casefold()
        if normalized_provider != _DEEPSEEK_PROVIDER:
            raise ValueError(
                "single-endpoint provider binding is reserved for deepseek; "
                "other transports must use bind_exact"
            )
        return cls.bind_exact(
            assignment_id=assignment_id,
            expected_role=expected_role,
            grant=grant,
            provider=normalized_provider,
            model=model,
            expected_hosts=(
                normalized_provider_endpoint_host(endpoint_url),
            ),
            expected_secret_refs=(secret_ref,),
            expected_side_effects=(
                SideEffect.process_execute,
                SideEffect.network_access,
            ),
            credential_identity=(
                secret_ref if credential_identity is None else credential_identity
            ),
        )

    @classmethod
    def bind_exact(
        cls,
        *,
        assignment_id: UUID,
        expected_role: AgentRole,
        grant: WorkspaceGrant,
        provider: str,
        model: str,
        expected_hosts: tuple[str, ...],
        expected_secret_refs: tuple[str, ...],
        expected_side_effects: tuple[SideEffect, ...],
        credential_identity: str,
    ) -> BoundDevelopmentProviderCapability:
        """Bind a transport whose complete authority is already declared."""

        normalized_hosts = _normalized_exact_hosts(expected_hosts)
        normalized_provider = provider.strip().casefold()
        if grant.agent_role != expected_role:
            raise ValueError("provider capability role differs from the dispatch grant")
        if grant.allowed_hosts != normalized_hosts:
            raise ValueError(
                "provider capability requires exactly its declared provider hosts"
            )
        if grant.secret_refs != expected_secret_refs:
            raise ValueError(
                "provider capability requires exactly its declared secret references"
            )
        if grant.allowed_side_effects != expected_side_effects:
            raise ValueError(
                "provider capability side effects differ from the exact dispatch grant"
            )
        return cls(
            assignment_id=assignment_id,
            agent_role=expected_role,
            workspace_id=grant.workspace_id,
            dispatch_grant_digest=_canonical_digest(grant),
            provider=normalized_provider,
            model=model,
            endpoint_hosts=normalized_hosts,
            secret_refs=expected_secret_refs,
            allowed_side_effects=expected_side_effects,
            credential_identity=credential_identity,
        )

    @property
    def capability_digest(self) -> str:
        return _canonical_digest(self)

    def require_grant(self, grant: WorkspaceGrant) -> None:
        """Fail closed if any dispatch authority changed after binding."""

        if (
            grant.agent_role != self.agent_role
            or grant.workspace_id != self.workspace_id
            or _canonical_digest(grant) != self.dispatch_grant_digest
            or grant.allowed_hosts != self.endpoint_hosts
            or grant.secret_refs != self.secret_refs
            or grant.allowed_side_effects != self.allowed_side_effects
        ):
            raise ValueError(
                "provider capability no longer matches the exact dispatch grant"
            )

    def public_evidence(self) -> dict[str, Any]:
        """Return validator-safe authority evidence without a credential value."""

        return {
            **self.model_dump(mode="json"),
            "capability_digest": self.capability_digest,
        }

    def _provider_request_binding(
        self,
        *,
        provider_request_id: str,
    ) -> dict[str, Any]:
        request_id = _validated_provider_request_id(provider_request_id)
        binding = {
            "schema_version": self.schema_version,
            "assignment_id": str(self.assignment_id),
            "agent_role": self.agent_role.value,
            "workspace_id": str(self.workspace_id),
            "provider": self.provider,
            "provider_request_id": request_id,
            "model": self.model,
            "provider_capability_digest": self.capability_digest,
            "dispatch_grant_digest": self.dispatch_grant_digest,
            "provider_hosts": list(self.endpoint_hosts),
            "secret_refs": list(self.secret_refs),
            "provider_side_effects": [
                effect.value for effect in self.allowed_side_effects
            ],
            "credential_identity": self.credential_identity,
        }
        return {
            **binding,
            "provider_request_binding_digest": _canonical_digest(binding),
        }

    def cost_authorization_payload(
        self,
        *,
        provider_request_id: str,
        worst_case_cost_usd: float,
    ) -> dict[str, Any]:
        binding = self._provider_request_binding(
            provider_request_id=provider_request_id,
        )
        return {
            **binding,
            "evidence_kind": "provider_cost_authorization",
            "worst_case_cost_usd": _validated_cost(
                worst_case_cost_usd,
                label="worst-case provider cost",
                positive=True,
            ),
        }

    def cost_receipt_payload(
        self,
        *,
        provider_request_id: str,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
    ) -> dict[str, Any]:
        binding = self._provider_request_binding(
            provider_request_id=provider_request_id,
        )
        return {
            **binding,
            "evidence_kind": "provider_cost_receipt",
            "cost_usd": _validated_cost(
                cost_usd,
                label="provider cost",
                positive=False,
            ),
            "input_tokens": _validated_token_count(
                input_tokens,
                label="input tokens",
            ),
            "output_tokens": _validated_token_count(
                output_tokens,
                label="output tokens",
            ),
        }


__all__ = [
    "BoundDevelopmentProviderCapability",
    "normalized_provider_endpoint_host",
]
