from __future__ import annotations

from uuid import uuid4

import pytest

from agent_platform.collaborative_development_auth import (
    DevelopmentAuthenticationError,
    DevelopmentCredentialIssuer,
)
from agent_platform.collaborative_development_models import AgentRole


def test_role_credentials_are_assignment_scoped_and_tamper_evident() -> None:
    issuer = DevelopmentCredentialIssuer("k" * 32)
    first = uuid4()
    second = uuid4()

    lilies = issuer.authenticate(issuer.issue(first, AgentRole.lilies))
    codex = issuer.authenticate(issuer.issue(second, AgentRole.codex))

    assert lilies.assignment_id == first
    assert lilies.agent_role == AgentRole.lilies
    assert codex.assignment_id == second
    assert codex.agent_role == AgentRole.codex

    token = issuer.issue(first, AgentRole.codex)
    with pytest.raises(DevelopmentAuthenticationError):
        issuer.authenticate(f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}")


def test_short_signing_key_and_unstructured_tokens_are_rejected() -> None:
    with pytest.raises(ValueError):
        DevelopmentCredentialIssuer("short")
    issuer = DevelopmentCredentialIssuer("k" * 32)
    with pytest.raises(DevelopmentAuthenticationError):
        issuer.authenticate("ordinary-platform-token")
