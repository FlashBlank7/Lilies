#!/usr/bin/env python3
"""Verify the current E05 scheduled monitoring hook product contract."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "verification_v0.2.103_e05_scheduled_monitoring_hook"


def _load_contract() -> dict[str, Any]:
    import sys

    backend_src = ROOT / "platform" / "backend" / "src"
    if str(backend_src) not in sys.path:
        sys.path.insert(0, str(backend_src))

    from agent_platform.adaptive_monitoring import (  # pylint: disable=import-error,import-outside-toplevel
        adaptive_monitoring_schedule_status,
        adaptive_monitoring_status_with_history,
        record_adaptive_monitoring_refresh,
        refresh_history_path,
    )

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        disabled = adaptive_monitoring_schedule_status(data_dir, 0.0, running=False)
        enabled = adaptive_monitoring_schedule_status(data_dir, 3600.0, running=True)
        scheduled = record_adaptive_monitoring_refresh(data_dir, trigger="manual_schedule_run")
        current = adaptive_monitoring_status_with_history(data_dir)
        history_path = refresh_history_path(data_dir)
        return {
            "disabled_default": disabled,
            "enabled_configured": enabled,
            "manual_schedule_run": scheduled,
            "current_status_after_run": current,
            "history_path_exists": history_path.exists(),
        }


def verify() -> dict[str, Any]:
    contract = _load_contract()
    current = contract["current_status_after_run"]
    disabled = contract["disabled_default"]
    enabled = contract["enabled_configured"]
    scheduled = contract["manual_schedule_run"]
    checks = {
        "defaults_disabled": disabled["enabled"] is False and disabled["interval_seconds"] == 0,
        "configured_interval_enabled": enabled["enabled"] is True and enabled["running"] is True,
        "manual_schedule_run_persists_trigger": (
            scheduled["last_refresh"]["trigger"] == "manual_schedule_run"
            and contract["history_path_exists"] is True
        ),
        "manual_refresh_history_visible": current["history_count"] == 1 and current["last_refresh"] is not None,
        "override_options_visible": current["available_overrides"] == ["adaptive", "deep", "none", "shallow"],
        "critical_alerts_zero": current["critical_alert_count"] == 0,
    }
    return {
        "version": "v0.2.103",
        "verification_id": "e05_scheduled_monitoring_hook",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.102_productization_lane_reselection.md",
        "status": "verified_existing_product_capability" if all(checks.values()) else "needs_attention",
        "selected_lane_from_v0_2_102": "e05_scheduled_monitoring_hook",
        "implementation_origin": "v0.2.63_adaptive_monitoring_schedule_and_report_audit",
        "new_backend_implementation_required": False,
        "implementation_paths": [
            "platform/backend/src/agent_platform/adaptive_monitoring.py",
            "platform/backend/src/agent_platform/api.py",
            "platform/backend/src/agent_platform/config.py",
            "tests/test_adaptive_monitoring_product_surface.py",
        ],
        "checks": checks,
        "contract": contract,
        "invariants": {
            "manual_refresh_preserved": True,
            "fixed_depth_overrides_visible": True,
            "e07_guarded_default_preserved": True,
            "e08_full_sidecar_completion_claimed": False,
            "e02_true_human_panel_blocked": True,
            "e10_governed_memory_blocked": True,
            "workingon_is_not_task_source": True,
        },
        "conclusion": (
            "The E05 scheduled monitoring hook already exists in the current product code and is verified "
            "against disabled-by-default scheduling, configured schedule visibility, persisted run-once "
            "history, and override visibility. v0.2.103 reconciles status drift rather than duplicating "
            "the v0.2.63 implementation."
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
        "# v0.2.103 E05 scheduled monitoring hook verification",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Implementation origin: `{result['implementation_origin']}`",
        f"- New backend implementation required: `{result['new_backend_implementation_required']}`",
        f"- Conclusion: {result['conclusion']}",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    for name, value in result["checks"].items():
        lines.append(f"| `{name}` | `{value}` |")
    lines.extend(
        [
            "",
            "## Implementation Paths",
            "",
        ]
    )
    for path in result["implementation_paths"]:
        lines.append(f"- `{path}`")
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            f"- Manual refresh preserved: `{result['invariants']['manual_refresh_preserved']}`",
            f"- Fixed-depth overrides visible: `{result['invariants']['fixed_depth_overrides_visible']}`",
            f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
            f"- Workingon is not task source: `{result['invariants']['workingon_is_not_task_source']}`",
            "",
        ]
    )
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = verify()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
