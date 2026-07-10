from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
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
HISTORY_PATH = Path("monitoring") / "adaptive_template_policy_history.jsonl"


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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def refresh_history_path(data_dir: Path) -> Path:
    return data_dir / HISTORY_PATH


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


def _refresh_record(status: dict[str, Any], trigger: str) -> dict[str, Any]:
    return {
        "refreshed_at": utc_now(),
        "trigger": trigger,
        "status": status["status"],
        "critical_alert_count": status["critical_alert_count"],
        "warning_alert_count": status["warning_alert_count"],
        "override_options_visible": status["override_options_visible"],
        "source": status["source"],
        "source_generated_at": status.get("generated_at"),
    }


def adaptive_monitoring_history(data_dir: Path, limit: int = 10) -> list[dict[str, Any]]:
    path = refresh_history_path(data_dir)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records[-limit:][::-1]


def adaptive_monitoring_status_with_history(data_dir: Path, limit: int = 10) -> dict[str, Any]:
    status = adaptive_monitoring_status()
    history = adaptive_monitoring_history(data_dir, limit=limit)
    return {
        **status,
        "last_refresh": history[0] if history else None,
        "history": history,
        "history_count": len(history),
        "history_path": refresh_history_path(data_dir).as_posix(),
    }


def record_adaptive_monitoring_refresh(data_dir: Path, trigger: str = "manual") -> dict[str, Any]:
    status = adaptive_monitoring_status()
    record = _refresh_record(status, trigger=trigger)
    path = refresh_history_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return adaptive_monitoring_status_with_history(data_dir)


def adaptive_monitoring_schedule_status(
    data_dir: Path,
    interval_seconds: float,
    *,
    running: bool = False,
) -> dict[str, Any]:
    history = adaptive_monitoring_history(data_dir, limit=1)
    return {
        "enabled": interval_seconds > 0,
        "interval_seconds": interval_seconds,
        "running": running,
        "trigger": "scheduled" if interval_seconds > 0 else "disabled",
        "history_path": refresh_history_path(data_dir).as_posix(),
        "last_refresh": history[0] if history else None,
    }


async def adaptive_monitoring_refresh_loop(data_dir: Path, interval_seconds: float) -> None:
    if interval_seconds <= 0:
        return
    while True:
        await asyncio.sleep(interval_seconds)
        record_adaptive_monitoring_refresh(data_dir, trigger="scheduled")
