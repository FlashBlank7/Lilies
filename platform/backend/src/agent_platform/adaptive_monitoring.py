from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
MONITOR_SNAPSHOT_PATH = (
    ROOT
    / "docs"
    / "experiment-status"
    / "evidence"
    / "monitor_v0.2.56_e05_adaptive_policy_2026_07_10.json"
)


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _normalize_case(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "family": str(case.get("family") or ""),
        "mode": str(case.get("mode") or ""),
        "build_status": str(case.get("build_status") or ""),
        "effective_depth": str(case.get("effective_depth") or ""),
        "reuse_depth_source": str(case.get("reuse_depth_source") or ""),
        "benchmark_passed": case.get("benchmark_passed"),
        "timeout_like": bool(case.get("timeout_like")),
        "available_overrides": list(case.get("available_overrides") or []),
        "source": str(case.get("source") or ""),
    }


def adaptive_monitoring_status(path: Path = MONITOR_SNAPSHOT_PATH) -> dict[str, Any]:
    if not path.exists():
        return {
            "status": "missing_evidence",
            "version": "v0.2.56",
            "source": relative(path),
            "generated_at": None,
            "critical_alert_count": 0,
            "warning_alert_count": 0,
            "override_options_visible": False,
            "available_overrides": [],
            "cases": [],
            "alerts": [],
            "conclusion": "Adaptive monitoring evidence is not available on this machine.",
        }
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    cases = [_normalize_case(case) for case in snapshot.get("cases", [])]
    critical_alerts = list(snapshot.get("critical_alerts") or [])
    alerts = list(snapshot.get("alerts") or [])
    policy_default_case = next((case for case in cases if case["mode"] == "policy_default"), None)
    available_overrides = policy_default_case["available_overrides"] if policy_default_case else []
    return {
        "status": "healthy" if not critical_alerts else "attention",
        "version": str(snapshot.get("version") or "v0.2.56"),
        "source": relative(path),
        "generated_at": snapshot.get("generated_at"),
        "critical_alert_count": len(critical_alerts),
        "warning_alert_count": len([alert for alert in alerts if alert.get("level") == "warning"]),
        "override_options_visible": bool(snapshot.get("override_options_visible")),
        "available_overrides": available_overrides,
        "cases": cases,
        "alerts": alerts,
        "conclusion": str(snapshot.get("conclusion") or ""),
    }
