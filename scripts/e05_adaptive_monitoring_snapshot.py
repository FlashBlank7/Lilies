#!/usr/bin/env python3
"""Build a deterministic monitoring snapshot for adaptive Template policy evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = (
    ROOT
    / "docs"
    / "experiment-status"
    / "evidence"
    / "monitor_v0.2.56_e05_adaptive_policy_2026_07_10.json"
)
SOURCES = [
    {
        "family": "data_analyzer",
        "mode": "adaptive_explicit",
        "path": ROOT / "docs/experiment-status/evidence/experiment_v0.2.49_e05_data_analyzer_adaptive_live_2026_07_10.json",
        "arm": "adaptive",
    },
    {
        "family": "code_review",
        "mode": "adaptive_explicit",
        "path": ROOT / "docs/experiment-status/evidence/experiment_v0.2.51_e05_code_review_adaptive_live_2026_07_10.json",
        "arm": "adaptive",
    },
    {
        "family": "data_analyzer",
        "mode": "policy_default",
        "path": ROOT / "docs/experiment-status/evidence/experiment_v0.2.54_e05_data_analyzer_policy_default_reliability_2026_07_10.json",
        "arm": "policy_default",
    },
]


@dataclass(frozen=True)
class MonitorCase:
    family: str
    mode: str
    source: str
    arm: str
    build_status: str
    elapsed_seconds: float | None
    reuse_depth_source: str
    requested_depth: str
    effective_depth: str
    recommended_action: str
    benchmark_passed: bool | None
    timeout_like: bool
    available_overrides: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "mode": self.mode,
            "source": self.source,
            "arm": self.arm,
            "build_status": self.build_status,
            "elapsed_seconds": self.elapsed_seconds,
            "reuse_depth_source": self.reuse_depth_source,
            "requested_depth": self.requested_depth,
            "effective_depth": self.effective_depth,
            "recommended_action": self.recommended_action,
            "benchmark_passed": self.benchmark_passed,
            "timeout_like": self.timeout_like,
            "available_overrides": self.available_overrides,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def load_arm(path: Path, depth: str) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for arm in data.get("arms", []):
        if arm.get("depth") == depth:
            return arm
    raise KeyError(f"arm {depth!r} not found in {path}")


def strategy_for(arm: dict[str, Any]) -> dict[str, Any]:
    strategy = arm.get("policy_default_resolution") or arm.get("adaptive_resolution") or arm.get("resolved_template_strategy")
    return strategy if isinstance(strategy, dict) else {}


def build_case(source: dict[str, Any]) -> MonitorCase:
    path = Path(source["path"])
    arm = load_arm(path, str(source["arm"]))
    strategy = strategy_for(arm)
    benchmark = arm.get("benchmark_outcome") if isinstance(arm.get("benchmark_outcome"), dict) else {}
    failure = arm.get("failure_summary") if isinstance(arm.get("failure_summary"), dict) else {}
    reuse_depth_source = str(strategy.get("reuse_depth_source") or "")
    if not reuse_depth_source and source["mode"] == "adaptive_explicit":
        reuse_depth_source = "explicit"
    return MonitorCase(
        family=str(source["family"]),
        mode=str(source["mode"]),
        source=relative(path),
        arm=str(source["arm"]),
        build_status=str(arm.get("build_status") or ""),
        elapsed_seconds=arm.get("elapsed_seconds"),
        reuse_depth_source=reuse_depth_source,
        requested_depth=str(strategy.get("reuse_depth") or ""),
        effective_depth=str(strategy.get("effective_reuse_depth") or ""),
        recommended_action=str(strategy.get("recommended_action") or ""),
        benchmark_passed=benchmark.get("case_passed"),
        timeout_like=bool(failure.get("timeout_like")),
        available_overrides=list(strategy.get("available_overrides") or []),
    )


def alerts_for(cases: list[MonitorCase]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    for case in cases:
        if case.timeout_like:
            alerts.append({"level": "critical", "family": case.family, "mode": case.mode, "reason": "timeout_like_failure"})
        if case.mode == "policy_default" and case.build_status != "published":
            alerts.append({"level": "critical", "family": case.family, "mode": case.mode, "reason": "policy_default_not_published"})
        if case.benchmark_passed is False:
            alerts.append({"level": "warning", "family": case.family, "mode": case.mode, "reason": "benchmark_case_failed"})
        if case.mode == "policy_default" and sorted(case.available_overrides) != ["adaptive", "deep", "none", "shallow"]:
            alerts.append({"level": "warning", "family": case.family, "mode": case.mode, "reason": "override_options_missing"})
    covered = {(case.family, case.mode) for case in cases}
    for expected in {("data_analyzer", "adaptive_explicit"), ("code_review", "adaptive_explicit"), ("data_analyzer", "policy_default")} - covered:
        alerts.append({"level": "warning", "family": expected[0], "mode": expected[1], "reason": "coverage_missing"})
    return alerts


def build_snapshot() -> dict[str, Any]:
    cases = [build_case(source) for source in SOURCES]
    alerts = alerts_for(cases)
    return {
        "monitor": "E05 adaptive policy monitoring snapshot",
        "version": "v0.2.56",
        "status": "completed",
        "generated_at": utc_now(),
        "cases": [case.to_json() for case in cases],
        "alerts": alerts,
        "critical_alerts": [alert for alert in alerts if alert["level"] == "critical"],
        "override_options_visible": any(
            case.mode == "policy_default"
            and sorted(case.available_overrides) == ["adaptive", "deep", "none", "shallow"]
            for case in cases
        ),
        "conclusion": (
            "Current monitored evidence has no critical adaptive/default-path alert; "
            "fixed-depth overrides remain visible and should stay as rollback controls."
        ),
    }


def write_summary(snapshot: dict[str, Any], path: Path) -> Path:
    summary_path = path.with_name(path.stem + "_summary.md")
    lines = [
        "# E05 adaptive policy monitoring snapshot",
        "",
        "## Summary",
        "",
        f"- Raw evidence: `{relative(path)}`",
        f"- Status: `{snapshot['status']}`",
        f"- Critical alerts: `{len(snapshot['critical_alerts'])}`",
        f"- Override options visible: `{snapshot['override_options_visible']}`",
        "",
        "## Cases",
        "",
        "| Family | Mode | Build | Effective | Source | Benchmark | Timeout |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in snapshot["cases"]:
        lines.append(
            "| {family} | {mode} | {build_status} | {effective_depth} | {reuse_depth_source} | {benchmark_passed} | {timeout_like} |".format(
                **case
            )
        )
    lines.extend(["", "## Conclusion", "", snapshot["conclusion"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = build_snapshot()
    OUTPUT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = write_summary(snapshot, OUTPUT_PATH)
    print(OUTPUT_PATH)
    print(summary_path)
    print(snapshot["status"])


if __name__ == "__main__":
    main()
