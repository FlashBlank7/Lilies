from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent_platform.collaborative_development_models import (
    AgentRole,
    SideEffect,
    WorkspaceGrant,
    utc_now,
)
from agent_platform.collaborative_development_provider import (
    BoundDevelopmentProviderCapability,
    normalized_provider_endpoint_host,
)
from agent_platform.collaborative_development_dispatcher import canonical_digest


def _review_grant(tmp_path: Path) -> WorkspaceGrant:
    return WorkspaceGrant(
        workspace_id=uuid4(),
        agent_role=AgentRole.lilies,
        workspace_root=str(tmp_path / "review-snapshot"),
        baseline_commit="a" * 40,
        allowed_paths=("src", "tests"),
        allowed_argv=(("/usr/bin/python3", "check.py"),),
        allowed_hosts=("api.deepseek.com",),
        allowed_side_effects=(
            SideEffect.process_execute,
            SideEffect.network_access,
        ),
        secret_refs=("deepseek-runtime-credential",),
        created_at=utc_now(),
    )


def test_provider_capability_binds_exact_dispatch_grant_without_secret_value(
    tmp_path: Path,
) -> None:
    assignment_id = uuid4()
    grant = _review_grant(tmp_path)
    capability = BoundDevelopmentProviderCapability.bind(
        assignment_id=assignment_id,
        expected_role=AgentRole.lilies,
        grant=grant,
        provider="deepseek",
        model="deepseek-chat",
        endpoint_url="https://API.DeepSeek.com:443/v1",
        secret_ref="deepseek-runtime-credential",
    )

    capability.require_grant(grant)
    evidence = capability.public_evidence()
    assert evidence["dispatch_grant_digest"] == canonical_digest(grant)
    assert evidence["endpoint_hosts"] == ["api.deepseek.com"]
    assert evidence["capability_digest"] == canonical_digest(capability)
    assert "api_key" not in evidence
    assert "sk-test-never-persist" not in json.dumps(evidence, sort_keys=True)

    authorization = capability.cost_authorization_payload(
        provider_request_id="request-0001",
        worst_case_cost_usd=1,
    )
    receipt = capability.cost_receipt_payload(
        provider_request_id="request-0001",
        cost_usd=0,
        input_tokens=123,
        output_tokens=45,
    )
    for payload in (authorization, receipt):
        assert payload["provider_capability_digest"] == (
            capability.capability_digest
        )
        assert payload["dispatch_grant_digest"] == canonical_digest(grant)
        assert payload["agent_role"] == "lilies"
        assert payload["workspace_id"] == str(grant.workspace_id)
        assert payload["provider_hosts"] == ["api.deepseek.com"]
        assert payload["secret_refs"] == ["deepseek-runtime-credential"]
        assert "sk-test-never-persist" not in json.dumps(payload, sort_keys=True)
    assert authorization["evidence_kind"] == "provider_cost_authorization"
    assert receipt["evidence_kind"] == "provider_cost_receipt"
    assert (
        authorization["provider_request_binding_digest"]
        == receipt["provider_request_binding_digest"]
    )


@pytest.mark.parametrize(
    ("update", "message"),
    [
        (
            {"allowed_hosts": ("other.deepseek.com",)},
            "declared provider hosts",
        ),
        (
            {"secret_refs": ("other-runtime-credential",)},
            "declared secret references",
        ),
        (
            {
                "allowed_side_effects": (
                    SideEffect.process_execute,
                    SideEffect.network_access,
                    SideEffect.workspace_write,
                )
            },
            "side effects",
        ),
    ],
)
def test_provider_capability_rejects_host_ref_or_side_effect_expansion(
    tmp_path: Path,
    update: dict,
    message: str,
) -> None:
    grant = _review_grant(tmp_path).model_copy(update=update)
    with pytest.raises(ValueError, match=message):
        BoundDevelopmentProviderCapability.bind(
            assignment_id=uuid4(),
            expected_role=AgentRole.lilies,
            grant=grant,
            provider="deepseek",
            model="deepseek-chat",
            endpoint_url="https://api.deepseek.com/v1",
            secret_ref="deepseek-runtime-credential",
        )


@pytest.mark.parametrize(
    "update",
    [
        {"grant_revision": 2},
        {"allowed_paths": ("src",)},
        {"baseline_commit": "c" * 40},
        {"allowed_argv": ()},
    ],
)
def test_provider_capability_rejects_post_binding_grant_change(
    tmp_path: Path,
    update: dict,
) -> None:
    grant = _review_grant(tmp_path)
    capability = BoundDevelopmentProviderCapability.bind(
        assignment_id=uuid4(),
        expected_role=AgentRole.lilies,
        grant=grant,
        provider="deepseek",
        model="deepseek-chat",
        endpoint_url="https://api.deepseek.com/v1",
        secret_ref="deepseek-runtime-credential",
    )
    changed = grant.model_copy(update=update)

    with pytest.raises(ValueError, match="exact dispatch grant"):
        capability.require_grant(changed)


def test_codex_subscription_capability_binds_all_transport_authority(
    tmp_path: Path,
) -> None:
    grant = WorkspaceGrant(
        workspace_id=uuid4(),
        agent_role=AgentRole.codex,
        workspace_root=str(tmp_path / "codex-workspace"),
        baseline_commit="b" * 40,
        allowed_paths=("src", "tests"),
        allowed_argv=(("/usr/local/bin/codex", "exec", "-"),),
        allowed_hosts=(
            "api.openai.com",
            "auth.openai.com",
            "chatgpt.com",
        ),
        allowed_side_effects=(
            SideEffect.workspace_write,
            SideEffect.process_execute,
            SideEffect.network_access,
        ),
        secret_refs=("codex-cli-session",),
        created_at=utc_now(),
    )
    capability = BoundDevelopmentProviderCapability.bind_exact(
        assignment_id=uuid4(),
        expected_role=AgentRole.codex,
        grant=grant,
        provider="openai-codex-cli",
        model="gpt-5.6-terra",
        expected_hosts=grant.allowed_hosts,
        expected_secret_refs=("codex-cli-session",),
        expected_side_effects=grant.allowed_side_effects,
        credential_identity="codex-cli-subscription",
    )

    capability.require_grant(grant)
    evidence = capability.public_evidence()
    assert evidence["dispatch_grant_digest"] == canonical_digest(grant)
    assert evidence["endpoint_hosts"] == list(grant.allowed_hosts)
    assert evidence["secret_refs"] == ["codex-cli-session"]
    assert evidence["credential_identity"] == "codex-cli-subscription"
    receipt = capability.cost_receipt_payload(
        provider_request_id="codex-request-0001",
        cost_usd=0,
        input_tokens=321,
        output_tokens=54,
    )
    assert receipt["provider_capability_digest"] == (
        capability.capability_digest
    )
    assert receipt["provider_hosts"] == list(grant.allowed_hosts)
    authorization = capability.cost_authorization_payload(
        provider_request_id="codex-request-0001",
        worst_case_cost_usd=1,
    )
    assert (
        authorization["provider_request_binding_digest"]
        == receipt["provider_request_binding_digest"]
    )


def test_single_endpoint_shortcut_does_not_fallback_for_codex(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="reserved for deepseek"):
        BoundDevelopmentProviderCapability.bind(
            assignment_id=uuid4(),
            expected_role=AgentRole.lilies,
            grant=_review_grant(tmp_path),
            provider="openai-codex-cli",
            model="gpt-5.6-terra",
            endpoint_url="https://api.openai.com/v1",
            secret_ref="deepseek-runtime-credential",
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("allowed_hosts", ("api.openai.com", "chatgpt.com")),
        ("secret_refs", ("another-codex-session",)),
        (
            "allowed_side_effects",
            (
                SideEffect.process_execute,
                SideEffect.network_access,
            ),
        ),
    ],
)
def test_codex_exact_binding_rejects_each_transport_authority_mismatch(
    tmp_path: Path,
    field: str,
    replacement: tuple,
) -> None:
    grant = WorkspaceGrant(
        workspace_id=uuid4(),
        agent_role=AgentRole.codex,
        workspace_root=str(tmp_path / "codex-workspace"),
        baseline_commit="b" * 40,
        allowed_paths=("src", "tests"),
        allowed_argv=(("/usr/local/bin/codex", "exec", "-"),),
        allowed_hosts=(
            "api.openai.com",
            "auth.openai.com",
            "chatgpt.com",
        ),
        allowed_side_effects=(
            SideEffect.workspace_write,
            SideEffect.process_execute,
            SideEffect.network_access,
        ),
        secret_refs=("codex-cli-session",),
        created_at=utc_now(),
    )
    expected = {
        "expected_hosts": grant.allowed_hosts,
        "expected_secret_refs": grant.secret_refs,
        "expected_side_effects": grant.allowed_side_effects,
    }
    expected[
        {
            "allowed_hosts": "expected_hosts",
            "secret_refs": "expected_secret_refs",
            "allowed_side_effects": "expected_side_effects",
        }[field]
    ] = replacement

    with pytest.raises(ValueError, match="provider capability"):
        BoundDevelopmentProviderCapability.bind_exact(
            assignment_id=uuid4(),
            expected_role=AgentRole.codex,
            grant=grant,
            provider="openai-codex-cli",
            model="gpt-5.6-terra",
            credential_identity="codex-cli-subscription",
            **expected,
        )


def test_credential_identity_and_request_id_are_digest_bound(
    tmp_path: Path,
) -> None:
    grant = _review_grant(tmp_path)
    first = BoundDevelopmentProviderCapability.bind(
        assignment_id=uuid4(),
        expected_role=AgentRole.lilies,
        grant=grant,
        provider="deepseek",
        model="deepseek-chat",
        endpoint_url="https://api.deepseek.com/v1",
        secret_ref="deepseek-runtime-credential",
        credential_identity="deepseek-account-a",
    )
    second = BoundDevelopmentProviderCapability.bind(
        assignment_id=first.assignment_id,
        expected_role=AgentRole.lilies,
        grant=grant,
        provider="deepseek",
        model="deepseek-chat",
        endpoint_url="https://api.deepseek.com/v1",
        secret_ref="deepseek-runtime-credential",
        credential_identity="deepseek-account-b",
    )
    assert first.capability_digest != second.capability_digest

    first_request = first.cost_authorization_payload(
        provider_request_id="request-a",
        worst_case_cost_usd=1,
    )
    second_request = first.cost_authorization_payload(
        provider_request_id="request-b",
        worst_case_cost_usd=1,
    )
    assert (
        first_request["provider_request_binding_digest"]
        != second_request["provider_request_binding_digest"]
    )
    assert canonical_digest(first_request) != canonical_digest(second_request)


@pytest.mark.parametrize(
    ("method", "kwargs", "message"),
    [
        (
            "authorization",
            {"provider_request_id": "", "worst_case_cost_usd": 1},
            "request id",
        ),
        (
            "authorization",
            {"provider_request_id": "request", "worst_case_cost_usd": 0},
            "worst-case",
        ),
        (
            "receipt",
            {
                "provider_request_id": "request",
                "cost_usd": -0.01,
                "input_tokens": 1,
                "output_tokens": 1,
            },
            "provider cost",
        ),
        (
            "receipt",
            {
                "provider_request_id": "request",
                "cost_usd": 0,
                "input_tokens": True,
                "output_tokens": 1,
            },
            "input tokens",
        ),
    ],
)
def test_cost_evidence_rejects_ambiguous_or_invalid_values(
    tmp_path: Path,
    method: str,
    kwargs: dict,
    message: str,
) -> None:
    capability = BoundDevelopmentProviderCapability.bind(
        assignment_id=uuid4(),
        expected_role=AgentRole.lilies,
        grant=_review_grant(tmp_path),
        provider="deepseek",
        model="deepseek-chat",
        endpoint_url="https://api.deepseek.com/v1",
        secret_ref="deepseek-runtime-credential",
    )
    with pytest.raises(ValueError, match=message):
        if method == "authorization":
            capability.cost_authorization_payload(**kwargs)
        else:
            capability.cost_receipt_payload(**kwargs)


def test_provider_endpoint_normalization_is_exact_and_https_only() -> None:
    assert (
        normalized_provider_endpoint_host(
            "https://API.DeepSeek.com:8443/v1"
        )
        == "api.deepseek.com:8443"
    )
    with pytest.raises(ValueError, match="credential-free HTTPS"):
        normalized_provider_endpoint_host(
            "https://user:password@api.deepseek.com/v1"
        )
    with pytest.raises(ValueError, match="credential-free HTTPS"):
        normalized_provider_endpoint_host("http://api.deepseek.com/v1")
    with pytest.raises(ValueError, match="credential-free HTTPS"):
        normalized_provider_endpoint_host(
            "https://api.deepseek.com/v1?api_key=never-allow"
        )
    with pytest.raises(ValueError, match="canonical host"):
        normalized_provider_endpoint_host("https://api.deepseek.com./v1")
    assert (
        normalized_provider_endpoint_host("https://[2001:0db8::1]:8443/v1")
        == "[2001:db8::1]:8443"
    )
