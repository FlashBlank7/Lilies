#!/usr/bin/env python3
"""Generate v0.2.132 E08 external KMS provider integration evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.132_e08_external_kms_provider_integration"


def _prepare_imports() -> None:
    backend_src = ROOT / "platform" / "backend" / "src"
    if str(backend_src) not in sys.path:
        sys.path.insert(0, str(backend_src))


def build_evidence() -> dict[str, Any]:
    _prepare_imports()

    from agent_platform.platform_harness import (  # pylint: disable=import-error,import-outside-toplevel
        SECRET_ENVELOPE_V2_PREFIX,
        SECRET_ENVELOPE_V3_PREFIX,
        PlatformHarness,
        PlatformHarnessViolation,
    )
    from agent_platform.secret_kms import LocalSecretKMSProvider  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.storage import Storage  # pylint: disable=import-error,import-outside-toplevel

    def run(coro):
        return asyncio.run(coro)

    data_dir = ROOT / ".tmp" / "v02_132_e08_external_kms_provider_integration"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    storage = Storage(data_dir)
    run(storage.initialize())

    provider = LocalSecretKMSProvider(
        provider_id="local-external-kms",
        primary_key_id="kms-2026q3",
        wrapping_keys={"kms-2026q3": "wrapping-key-2026q3"},
    )
    harness = PlatformHarness(storage=storage, secret_kms_provider=provider)
    public = run(
        harness.save_secret(
            owner_id="owner-a",
            name="provider_token",
            value="sk-v02-132-provider",
            description="provider-backed evidence",
        )
    )
    raw = run(storage.get_platform_secret(owner_id="owner-a", name="provider_token"))
    resolved = run(harness.inject_secret_references(owner_id="owner-a", payload={"$secret": "provider_token"}))
    controls = harness.policy_controls()["secret_storage"]

    no_provider_blocked = False
    try:
        run(PlatformHarness(storage=storage).inject_secret_references(owner_id="owner-a", payload={"$secret": "provider_token"}))
    except PlatformHarnessViolation as error:
        no_provider_blocked = "KMS provider is not configured" in str(error)

    missing_key_blocked = False
    try:
        wrong_provider = LocalSecretKMSProvider(
            provider_id="local-external-kms",
            primary_key_id="kms-new",
            wrapping_keys={"kms-new": "new-wrapping-key"},
        )
        run(
            PlatformHarness(storage=storage, secret_kms_provider=wrong_provider).inject_secret_references(
                owner_id="owner-a",
                payload={"$secret": "provider_token"},
            )
        )
    except PlatformHarnessViolation as error:
        missing_key_blocked = "kms-2026q3" in str(error)

    v2_writer = PlatformHarness(
        storage=storage,
        secret_envelope_key="local-envelope-key",
        secret_envelope_key_id="local-v2",
    )
    run(v2_writer.save_secret(owner_id="owner-a", name="v2_token", value="sk-v02-132-v2", description=""))
    v2_raw = run(storage.get_platform_secret(owner_id="owner-a", name="v2_token"))
    v2_reader = PlatformHarness(
        storage=storage,
        secret_envelope_key="local-envelope-key",
        secret_envelope_key_id="local-v2",
        secret_kms_provider=provider,
    )
    v2_resolved = run(v2_reader.inject_secret_references(owner_id="owner-a", payload={"$secret": "v2_token"}))

    public_json = json.dumps(public, sort_keys=True)
    checks = {
        "new_secret_uses_v3_provider_envelope": raw["value"].startswith(SECRET_ENVELOPE_V3_PREFIX)
        and public["storage_mode"] == "encrypted_v3:local-external-kms:kms-2026q3"
        and public["kms_provider_id"] == "local-external-kms"
        and public["key_id"] == "kms-2026q3",
        "provider_unwrap_resolves_secret": resolved == "sk-v02-132-provider",
        "missing_provider_blocks_v3_read": no_provider_blocked,
        "missing_provider_key_blocks_v3_read": missing_key_blocked,
        "v2_compatibility_preserved": v2_raw["value"].startswith(SECRET_ENVELOPE_V2_PREFIX)
        and v2_resolved == "sk-v02-132-v2",
        "secret_material_redacted_from_public_metadata": "sk-v02-132-provider" not in public_json
        and "value" not in public,
        "policy_controls_report_external_kms": controls["external_kms_provider_integration"] is True
        and controls["kms_provider_configured"] is True
        and controls["kms_provider"]["provider_id"] == "local-external-kms",
    }
    return {
        "version": "v0.2.132",
        "evidence_id": "e08_external_kms_provider_integration",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.131_e08_remaining_sidecar_architecture_reselection.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "checks": checks,
        "public_secret": public,
        "policy_secret_storage": controls,
        "implementation_paths": [
            "platform/backend/src/agent_platform/secret_kms.py",
            "platform/backend/src/agent_platform/platform_harness.py",
            "platform/backend/src/agent_platform/config.py",
            "platform/backend/src/agent_platform/api.py",
            "tests/test_v02_132_e08_external_kms_provider_integration.py",
        ],
        "boundaries": {
            "external_kms_provider_integration_claimed": True,
            "provider_type": "local",
            "cloud_provider_deployment_claimed": False,
            "e08_full_sidecar_completion_claimed": False,
        },
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
        "# v0.2.132 E08 external KMS provider integration",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Source stage report: `{result['source_stage_report']}`",
        f"- External KMS provider integration claimed: `{result['boundaries']['external_kms_provider_integration_claimed']}`",
        f"- Provider type: `{result['boundaries']['provider_type']}`",
        f"- Cloud provider deployment claimed: `{result['boundaries']['cloud_provider_deployment_claimed']}`",
        f"- E08 full sidecar completion claimed: `{result['boundaries']['e08_full_sidecar_completion_claimed']}`",
        "",
        "## Checks",
        "",
    ]
    for name, passed in result["checks"].items():
        lines.append(f"- {name}: `{passed}`")
    lines.extend(
        [
            "",
            "## Secret Storage",
            "",
            f"- New secret mode: `{result['policy_secret_storage']['new_secret_mode']}`",
            f"- KMS provider configured: `{result['policy_secret_storage']['kms_provider_configured']}`",
            f"- KMS provider id: `{result['policy_secret_storage']['kms_provider']['provider_id']}`",
        ]
    )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = build_evidence()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
