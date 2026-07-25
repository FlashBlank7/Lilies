"""Record exact pytest outcomes for mandatory qualification commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest


_RESULT_PATH_ENV = "LILIES_QUALIFICATION_PYTEST_RESULT"
_collected_node_ids: set[str] = set()
_outcomes: dict[str, str] = {}


def pytest_collection_finish(session: pytest.Session) -> None:
    _collected_node_ids.update(item.nodeid for item in session.items)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    node_id = report.nodeid
    was_xfail = getattr(report, "wasxfail", None)
    if was_xfail:
        _outcomes[node_id] = "xfailed" if report.skipped else "xpassed"
        return
    if report.skipped:
        _outcomes[node_id] = "skipped"
        return
    if report.failed:
        _outcomes[node_id] = "failed" if report.when == "call" else "errors"
        return
    if report.when == "call" and report.passed:
        _outcomes[node_id] = "passed"


def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: int | pytest.ExitCode,
) -> None:
    raw_path = os.environ.get(_RESULT_PATH_ENV)
    if not raw_path:
        return
    path = Path(raw_path).expanduser().resolve()
    counts = {
        status: sum(outcome == status for outcome in _outcomes.values())
        for status in (
            "passed",
            "failed",
            "errors",
            "skipped",
            "xfailed",
            "xpassed",
        )
    }
    unresolved = _collected_node_ids - set(_outcomes)
    counts["errors"] += len(unresolved)
    payload: dict[str, Any] = {
        "collected": len(_collected_node_ids),
        **counts,
        "pytest_exit_status": int(exitstatus),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


__all__ = [
    "pytest_collection_finish",
    "pytest_runtest_logreport",
    "pytest_sessionfinish",
]
