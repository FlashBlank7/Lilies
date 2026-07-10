from __future__ import annotations

import asyncio
import hashlib
import hmac
from pathlib import Path

import pytest

from agent_platform.platform_harness import (
    SECRET_ENVELOPE_PREFIX,
    SECRET_ENVELOPE_ITERATIONS,
    SECRET_ENVELOPE_V2_PREFIX,
    PlatformHarness,
    PlatformHarnessViolation,
)
from agent_platform.storage import Storage


def run(coro):
    return asyncio.run(coro)


def initialized_storage(path: Path) -> Storage:
    storage = Storage(path)
    run(storage.initialize())
    return storage


def build_v1_envelope(harness: PlatformHarness, *, value: str, key: str) -> str:
    salt = b"0123456789abcdef"
    nonce = b"abcdef9876543210"
    enc_key, mac_key = harness._derive_secret_envelope_keys(key, salt)
    plaintext = value.encode("utf-8")
    ciphertext = harness._xor_bytes(plaintext, harness._keystream(enc_key, nonce, len(plaintext)))
    envelope = {
        "algorithm": "hmac-sha256-xor-stream",
        "ciphertext": harness._b64(ciphertext),
        "iterations": SECRET_ENVELOPE_ITERATIONS,
        "kdf": "pbkdf2-hmac-sha256",
        "nonce": harness._b64(nonce),
        "salt": harness._b64(salt),
        "version": 1,
    }
    mac_input = harness._stable_json(envelope).encode("utf-8")
    envelope["tag"] = harness._b64(hmac.new(mac_key, mac_input, hashlib.sha256).digest())
    return SECRET_ENVELOPE_PREFIX + harness._b64(harness._stable_json(envelope).encode("utf-8"))


def test_v02_108_new_secrets_use_v2_keyed_envelopes(tmp_path: Path) -> None:
    storage = initialized_storage(tmp_path / "storage")
    harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="current-secret-key",
        secret_envelope_key_id="kms-local-2026q3",
    )

    public = run(
        harness.save_secret(
            owner_id="owner-a",
            name="api_token",
            value="sk-v2-secret",
            description="v2 secret",
        )
    )
    raw = run(storage.get_platform_secret(owner_id="owner-a", name="api_token"))

    assert public["storage_mode"] == "encrypted_v2:kms-local-2026q3"
    assert public["key_id"] == "kms-local-2026q3"
    assert public["encrypted"] is True
    assert "value" not in public
    assert raw["value"].startswith(SECRET_ENVELOPE_V2_PREFIX)
    assert "sk-v2-secret" not in raw["value"]
    assert run(
        harness.inject_secret_references(
            owner_id="owner-a",
            payload={"Authorization": {"$secret": "api_token", "prefix": "Bearer "}},
        )
    ) == {"Authorization": "Bearer sk-v2-secret"}


def test_v02_108_rotated_v2_envelope_reads_with_previous_key(tmp_path: Path) -> None:
    storage = initialized_storage(tmp_path / "storage")
    old_harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="old-secret-key",
        secret_envelope_key_id="kms-old",
    )
    run(old_harness.save_secret(owner_id="owner-a", name="rotated", value="sk-old", description=""))

    rotated_harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="new-secret-key",
        secret_envelope_key_id="kms-new",
        secret_envelope_previous_keys={"kms-old": "old-secret-key"},
    )

    listed = run(rotated_harness.list_secrets(owner_id="owner-a"))
    assert listed[0]["storage_mode"] == "encrypted_v2:kms-old"
    assert run(
        rotated_harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "rotated"})
    ) == "sk-old"

    run(rotated_harness.save_secret(owner_id="owner-a", name="new", value="sk-new", description=""))
    new_raw = run(storage.get_platform_secret(owner_id="owner-a", name="new"))
    assert new_raw["value"].startswith(SECRET_ENVELOPE_V2_PREFIX)
    assert run(rotated_harness.list_secrets(owner_id="owner-a"))[0]["storage_mode"] == "encrypted_v2:kms-new"


def test_v02_108_rotated_v2_envelope_rejects_missing_previous_key(tmp_path: Path) -> None:
    storage = initialized_storage(tmp_path / "storage")
    old_harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="old-secret-key",
        secret_envelope_key_id="kms-old",
    )
    run(old_harness.save_secret(owner_id="owner-a", name="rotated", value="sk-old", description=""))

    rotated_harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="new-secret-key",
        secret_envelope_key_id="kms-new",
    )

    with pytest.raises(PlatformHarnessViolation, match="kms-old"):
        run(rotated_harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "rotated"}))


def test_v02_108_v1_envelope_and_plaintext_rows_remain_readable(tmp_path: Path) -> None:
    storage = initialized_storage(tmp_path / "storage")
    harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="legacy-secret-key",
        secret_envelope_key_id="kms-current",
    )
    v1_value = build_v1_envelope(harness, value="sk-v1", key="legacy-secret-key")
    run(
        storage.save_platform_secret(
            owner_id="owner-a",
            name="v1_token",
            value=v1_value,
            description="legacy v1 row",
        )
    )
    run(
        storage.save_platform_secret(
            owner_id="owner-a",
            name="plain_token",
            value="sk-plain",
            description="legacy plaintext row",
        )
    )

    listed = {item["name"]: item for item in run(harness.list_secrets(owner_id="owner-a"))}
    assert listed["v1_token"]["storage_mode"] == "encrypted_v1"
    assert listed["plain_token"]["storage_mode"] == "legacy_plaintext"
    assert run(harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "v1_token"})) == "sk-v1"
    assert run(harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "plain_token"})) == "sk-plain"


def test_v02_108_policy_controls_report_rotation_contract(tmp_path: Path) -> None:
    harness = PlatformHarness(
        storage=initialized_storage(tmp_path / "storage"),
        secret_envelope_key="current-secret-key",
        secret_envelope_key_id="kms-current",
        secret_envelope_previous_keys={"kms-old": "old-secret-key"},
    )

    secret_storage = harness.policy_controls()["secret_storage"]
    assert secret_storage == {
        "new_secret_mode": "encrypted_v2:kms-current",
        "envelope_configured": True,
        "current_key_id": "kms-current",
        "keyring_size": 2,
        "rotation_aware": True,
        "kms_provider_configured": False,
        "kms_provider": {
            "provider_id": "",
            "provider_type": "",
            "configured": False,
            "primary_key_id": "",
            "keyring_size": 0,
            "rotation_aware": False,
            "wrap_supported": False,
            "unwrap_supported": False,
        },
        "external_kms_provider_integration": False,
        "legacy_v1_read_supported": True,
        "legacy_plaintext_read_supported": True,
    }
