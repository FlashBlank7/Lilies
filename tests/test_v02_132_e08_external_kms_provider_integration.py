from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.platform_harness import (
    SECRET_ENVELOPE_V2_PREFIX,
    SECRET_ENVELOPE_V3_PREFIX,
    PlatformHarness,
    PlatformHarnessViolation,
)
from agent_platform.secret_kms import LocalSecretKMSProvider
from agent_platform.storage import Storage


def run(coro):
    return asyncio.run(coro)


def initialized_storage(path: Path) -> Storage:
    storage = Storage(path)
    run(storage.initialize())
    return storage


def provider(*, key_id: str = "kms-2026q3", key: str = "wrapping-key") -> LocalSecretKMSProvider:
    return LocalSecretKMSProvider(
        provider_id="local-external-kms",
        primary_key_id=key_id,
        wrapping_keys={key_id: key},
    )


def test_v02_132_new_secrets_use_provider_backed_v3_envelopes(tmp_path: Path) -> None:
    storage = initialized_storage(tmp_path / "storage")
    harness = PlatformHarness(storage=storage, secret_kms_provider=provider())

    public = run(
        harness.save_secret(
            owner_id="owner-a",
            name="api_token",
            value="sk-v3-provider-secret",
            description="provider-backed secret",
        )
    )
    raw = run(storage.get_platform_secret(owner_id="owner-a", name="api_token"))
    controls = harness.policy_controls()["secret_storage"]

    assert public["storage_mode"] == "encrypted_v3:local-external-kms:kms-2026q3"
    assert public["kms_provider_id"] == "local-external-kms"
    assert public["key_id"] == "kms-2026q3"
    assert public["encrypted"] is True
    assert "value" not in public
    assert raw["value"].startswith(SECRET_ENVELOPE_V3_PREFIX)
    assert "sk-v3-provider-secret" not in raw["value"]
    assert controls["new_secret_mode"] == "encrypted_v3:local-external-kms:kms-2026q3"
    assert controls["kms_provider_configured"] is True
    assert controls["external_kms_provider_integration"] is True
    assert controls["kms_provider"]["provider_type"] == "local"
    assert run(harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "api_token"})) == (
        "sk-v3-provider-secret"
    )


def test_v02_132_v3_envelope_requires_provider_and_key(tmp_path: Path) -> None:
    storage = initialized_storage(tmp_path / "storage")
    writer = PlatformHarness(storage=storage, secret_kms_provider=provider(key_id="kms-old", key="old-wrap"))
    run(writer.save_secret(owner_id="owner-a", name="rotated", value="sk-old-provider", description=""))

    no_provider = PlatformHarness(storage=storage)
    with pytest.raises(PlatformHarnessViolation, match="KMS provider is not configured"):
        run(no_provider.inject_secret_references(owner_id="owner-a", payload={"$secret": "rotated"}))

    missing_key = PlatformHarness(storage=storage, secret_kms_provider=provider(key_id="kms-new", key="new-wrap"))
    with pytest.raises(PlatformHarnessViolation, match="kms-old"):
        run(missing_key.inject_secret_references(owner_id="owner-a", payload={"$secret": "rotated"}))

    rotated = PlatformHarness(
        storage=storage,
        secret_kms_provider=LocalSecretKMSProvider(
            provider_id="local-external-kms",
            primary_key_id="kms-new",
            wrapping_keys={"kms-new": "new-wrap", "kms-old": "old-wrap"},
        ),
    )
    assert run(rotated.inject_secret_references(owner_id="owner-a", payload={"$secret": "rotated"})) == (
        "sk-old-provider"
    )


def test_v02_132_provider_mode_preserves_v2_read_compatibility(tmp_path: Path) -> None:
    storage = initialized_storage(tmp_path / "storage")
    old_harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="local-envelope-key",
        secret_envelope_key_id="local-v2",
    )
    run(old_harness.save_secret(owner_id="owner-a", name="v2_token", value="sk-v2", description=""))
    raw = run(storage.get_platform_secret(owner_id="owner-a", name="v2_token"))
    assert raw["value"].startswith(SECRET_ENVELOPE_V2_PREFIX)

    provider_harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="local-envelope-key",
        secret_envelope_key_id="local-v2",
        secret_kms_provider=provider(),
    )
    listed = {item["name"]: item for item in run(provider_harness.list_secrets(owner_id="owner-a"))}

    assert listed["v2_token"]["storage_mode"] == "encrypted_v2:local-v2"
    assert run(provider_harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "v2_token"})) == (
        "sk-v2"
    )


def test_v02_132_api_settings_wire_provider_backed_secret_storage(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        api_token="test-token",
        platform_harness_secret_kms_provider="local",
        platform_harness_secret_kms_provider_id="local-external-kms",
        platform_harness_secret_kms_key_id="kms-api",
        platform_harness_secret_kms_key="api-wrap-key",
    )
    settings.prepare()
    app = create_app(settings)
    headers = {"Authorization": "Bearer test-token"}

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/secrets",
            headers=headers,
            json={
                "owner_id": "owner-a",
                "name": "api_token",
                "value": "sk-api-provider",
                "description": "api provider secret",
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["storage_mode"] == "encrypted_v3:local-external-kms:kms-api"
        assert body["kms_provider_id"] == "local-external-kms"
        assert body["key_id"] == "kms-api"
        assert "value" not in body
