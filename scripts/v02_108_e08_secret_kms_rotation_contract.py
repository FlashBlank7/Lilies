#!/usr/bin/env python3
"""Generate v0.2.108 E08 secret KMS/rotation contract evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.108_e08_secret_kms_rotation_contract"


def verify_contract() -> dict[str, Any]:
    backend_src = ROOT / "platform" / "backend" / "src"
    if str(backend_src) not in sys.path:
        sys.path.insert(0, str(backend_src))

    from agent_platform.platform_harness import (  # pylint: disable=import-error,import-outside-toplevel
        SECRET_ENVELOPE_ITERATIONS,
        SECRET_ENVELOPE_PREFIX,
        SECRET_ENVELOPE_V2_PREFIX,
        PlatformHarness,
        PlatformHarnessViolation,
    )
    from agent_platform.storage import Storage  # pylint: disable=import-error,import-outside-toplevel

    def run(coro):
        return asyncio.run(coro)

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

    data_dir = ROOT / ".tmp" / "v02_108_e08_secret_kms_rotation_contract"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    storage = Storage(data_dir)
    run(storage.initialize())

    old_harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="old-local-envelope-key",
        secret_envelope_key_id="kms-old",
    )
    new_public = run(
        old_harness.save_secret(
            owner_id="owner-a",
            name="rotated",
            value="sk-v02-108-rotated",
            description="rotated envelope evidence",
        )
    )
    rotated_raw = run(storage.get_platform_secret(owner_id="owner-a", name="rotated"))

    rotated_harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="new-local-envelope-key",
        secret_envelope_key_id="kms-new",
        secret_envelope_previous_keys={"kms-old": "old-local-envelope-key"},
    )
    rotated_value = run(rotated_harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "rotated"}))
    run(rotated_harness.save_secret(owner_id="owner-a", name="new", value="sk-v02-108-new", description=""))
    current_raw = run(storage.get_platform_secret(owner_id="owner-a", name="new"))
    current_public = run(rotated_harness.list_secrets(owner_id="owner-a"))[0]

    missing_key_blocked = False
    try:
        missing_harness = PlatformHarness(
            storage=storage,
            secret_envelope_key="new-local-envelope-key",
            secret_envelope_key_id="kms-new",
        )
        run(missing_harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "rotated"}))
    except PlatformHarnessViolation as error:
        missing_key_blocked = "kms-old" in str(error)

    v1_value = build_v1_envelope(rotated_harness, value="sk-v02-108-v1", key="new-local-envelope-key")
    run(
        storage.save_platform_secret(
            owner_id="owner-a",
            name="legacy_v1",
            value=v1_value,
            description="legacy v1 evidence",
        )
    )
    run(
        storage.save_platform_secret(
            owner_id="owner-a",
            name="legacy_plaintext",
            value="sk-v02-108-plain",
            description="legacy plaintext evidence",
        )
    )
    legacy_v1 = run(rotated_harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "legacy_v1"}))
    legacy_plaintext = run(
        rotated_harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "legacy_plaintext"})
    )
    legacy_public = {item["name"]: item for item in run(rotated_harness.list_secrets(owner_id="owner-a"))}
    policy_secret_storage = rotated_harness.policy_controls()["secret_storage"]

    leaked_tokens = [
        "sk-v02-108-rotated",
        "sk-v02-108-new",
        "sk-v02-108-v1",
        "sk-v02-108-plain",
    ]
    public_metadata = json.dumps([new_public, current_public, legacy_public], sort_keys=True)
    checks = {
        "new_secret_uses_v2_key_id": current_raw["value"].startswith(SECRET_ENVELOPE_V2_PREFIX)
        and current_public["storage_mode"] == "encrypted_v2:kms-new"
        and current_public["key_id"] == "kms-new",
        "rotated_v2_reads_with_previous_key": rotated_value == "sk-v02-108-rotated"
        and new_public["storage_mode"] == "encrypted_v2:kms-old",
        "missing_previous_key_blocks_old_v2": missing_key_blocked,
        "legacy_v1_read_supported": legacy_v1 == "sk-v02-108-v1"
        and legacy_public["legacy_v1"]["storage_mode"] == "encrypted_v1",
        "legacy_plaintext_read_supported": legacy_plaintext == "sk-v02-108-plain"
        and legacy_public["legacy_plaintext"]["storage_mode"] == "legacy_plaintext",
        "secret_material_redacted_from_public_metadata": all(token not in public_metadata for token in leaked_tokens),
        "secret_material_not_plaintext_at_rest_for_encrypted_rows": "sk-v02-108-rotated" not in rotated_raw["value"]
        and "sk-v02-108-new" not in current_raw["value"],
        "policy_controls_report_rotation_contract": policy_secret_storage
        == {
            "new_secret_mode": "encrypted_v2:kms-new",
            "envelope_configured": True,
            "current_key_id": "kms-new",
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
        },
    }
    return {
        "version": "v0.2.108",
        "evidence_id": "e08_secret_kms_rotation_contract",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.107_e08_remaining_sidecar_slice_reselection.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "checks": checks,
        "public_modes": {
            "rotated_old": new_public["storage_mode"],
            "current_new": current_public["storage_mode"],
            "legacy_v1": legacy_public["legacy_v1"]["storage_mode"],
            "legacy_plaintext": legacy_public["legacy_plaintext"]["storage_mode"],
        },
        "policy_secret_storage": policy_secret_storage,
        "existing_evidence_preserved": [
            "docs/stage-report-archives/v0.2.x/v0.2.15_platform_harness_secret_policy.md",
            "docs/stage-report-archives/v0.2.x/v0.2.25_platform_harness_secret_envelope.md",
        ],
        "implementation_paths": [
            "platform/backend/src/agent_platform/config.py",
            "platform/backend/src/agent_platform/api.py",
            "platform/backend/src/agent_platform/platform_harness.py",
            "tests/test_v02_108_e08_secret_kms_rotation_contract.py",
            "tests/test_workflow.py",
        ],
        "invariants": {
            "external_kms_integrated": False,
            "local_kms_like_contract_only": True,
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
        },
        "next_boundary": (
            "This closes the local KMS/rotation-grade envelope slice only; external KMS integration, "
            "complete handler catalog, distributed heartbeat registry, and full sidecar completion remain open."
        ),
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.108 E08 secret KMS/rotation contract",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- External KMS integrated: `{result['invariants']['external_kms_integrated']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        f"- Next boundary: {result['next_boundary']}",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    for name, value in result["checks"].items():
        lines.append(f"| `{name}` | `{value}` |")
    lines.extend(["", "## Public Modes", "", "| Secret class | Public storage mode |", "| --- | --- |"])
    for name, value in result["public_modes"].items():
        lines.append(f"| `{name}` | `{value}` |")
    lines.extend(["", "## Existing Evidence Preserved", ""])
    for path in result["existing_evidence_preserved"]:
        lines.append(f"- `{path}`")
    lines.extend(["", "## Implementation Paths", ""])
    for path in result["implementation_paths"]:
        lines.append(f"- `{path}`")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = verify_contract()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
