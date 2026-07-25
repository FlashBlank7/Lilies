"""Assignment-scoped credentials for the reusable development API."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from uuid import UUID

from .collaborative_development_models import AgentRole


_TOKEN_PATTERN = re.compile(
    r"^cd1\.([0-9a-f]{32})\.(lilies|codex)\.([A-Za-z0-9_-]{43})$"
)


class DevelopmentAuthenticationError(RuntimeError):
    """A deliberately non-specific credential rejection."""


@dataclass(frozen=True, slots=True)
class DevelopmentPrincipal:
    actor_role: str
    actor_id: str
    assignment_id: UUID | None = None

    @property
    def agent_role(self) -> AgentRole | None:
        try:
            return AgentRole(self.actor_role)
        except ValueError:
            return None


class DevelopmentCredentialIssuer:
    """Issue deterministic, revocable-by-assignment, role-scoped bearers."""

    def __init__(self, signing_key: str) -> None:
        encoded = signing_key.encode("utf-8")
        if len(encoded) < 32:
            raise ValueError("development signing key must contain at least 32 bytes")
        self._signing_key = encoded

    def _signature(self, unsigned: str) -> str:
        digest = hmac.new(
            self._signing_key,
            unsigned.encode("ascii"),
            hashlib.sha256,
        ).digest()
        import base64

        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def issue(self, assignment_id: UUID, role: AgentRole) -> str:
        unsigned = f"cd1.{assignment_id.hex}.{role.value}"
        return f"{unsigned}.{self._signature(unsigned)}"

    def authenticate(self, token: str) -> DevelopmentPrincipal:
        match = _TOKEN_PATTERN.fullmatch(token.strip())
        if match is None:
            raise DevelopmentAuthenticationError("development credential is invalid")
        assignment_hex, role_value, supplied_signature = match.groups()
        unsigned = f"cd1.{assignment_hex}.{role_value}"
        if not hmac.compare_digest(
            self._signature(unsigned),
            supplied_signature,
        ):
            raise DevelopmentAuthenticationError("development credential is invalid")
        assignment_id = UUID(hex=assignment_hex)
        role = AgentRole(role_value)
        return DevelopmentPrincipal(
            actor_role=role.value,
            actor_id=f"{role.value}-agent",
            assignment_id=assignment_id,
        )


__all__ = [
    "DevelopmentAuthenticationError",
    "DevelopmentCredentialIssuer",
    "DevelopmentPrincipal",
]
