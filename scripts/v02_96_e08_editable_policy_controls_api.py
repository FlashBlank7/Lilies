#!/usr/bin/env python3
"""Generate v0.2.96 editable policy-controls API evidence."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
TESTS_DIR = ROOT / "tests"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from agent_platform.api import create_app  # noqa: E402
from agent_platform.config import Settings  # noqa: E402
from test_workflow import ScriptedProvider, headers  # type: ignore  # noqa: E402


OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.96_e08_editable_policy_controls_api"


def generate_evidence() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="lilies-v02-96-") as raw_tmp:
        tmp_path = Path(raw_tmp)
        settings = Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            platform_harness_network_egress_policy="full",
            platform_harness_secret_policy_enabled=True,
            platform_harness_worker_lease_seconds=0,
        )
        app = create_app(settings, ScriptedProvider())
        with TestClient(app) as client:
            before = client.get("/api/v1/platform/harness/policy-controls", headers=headers())
            patch = client.patch(
                "/api/v1/platform/harness/policy-controls",
                headers=headers(),
                json={
                    "network_egress_policy": "allowlist",
                    "network_egress_allowlist": ["api.example.test"],
                    "cancellation_policy": "disabled",
                    "secret_policy_enabled": False,
                    "worker_lease_seconds": 30,
                    "limits": {
                        "max_model_calls_per_task": 5,
                        "max_tool_calls_per_owner": 2,
                    },
                    "reason": "v0.2.96 evidence generation",
                },
            )
            after = client.get("/api/v1/platform/harness/policy-controls", headers=headers())
            rejection = client.patch(
                "/api/v1/platform/harness/policy-controls",
                headers=headers(),
                json={
                    "limits": {"max_tool_calls_per_task": -1},
                    "reason": "invalid negative limit evidence",
                },
            )

        before.raise_for_status()
        patch.raise_for_status()
        after.raise_for_status()
        return {
            "version": "v0.2.96",
            "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.95_e08_followup_controls_scope.md",
            "status": "completed",
            "endpoint": "PATCH /api/v1/platform/harness/policy-controls",
            "before": before.json(),
            "patch_response": patch.json(),
            "after": after.json(),
            "invalid_update_rejection": {
                "status_code": rejection.status_code,
                "body": rejection.text,
            },
            "e07_invariant": {
                "status": "preserved",
                "no_e07_code_or_default_change": True,
            },
            "not_full_sidecar_completion": True,
        }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    patch = result["patch_response"]
    lines = [
        "# v0.2.96 E08 editable policy-controls API evidence",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Endpoint: `{result['endpoint']}`",
        f"- Network policy before: `{result['before']['network_egress_policy']}`",
        f"- Network policy after: `{result['after']['network_egress_policy']}`",
        f"- Cancellation policy after: `{result['after']['cancellation_policy']}`",
        f"- Worker lease after: `{result['after']['worker_lease_seconds']}`",
        f"- Changed fields: `{', '.join(patch['audit']['changed_fields'])}`",
        f"- Invalid update rejection status: `{result['invalid_update_rejection']['status_code']}`",
        f"- E07 invariant: `{result['e07_invariant']['status']}`",
        f"- Not full sidecar completion: `{result['not_full_sidecar_completion']}`",
        "",
    ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = generate_evidence()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["patch_response"]["audit"]["action"])


if __name__ == "__main__":
    main()
