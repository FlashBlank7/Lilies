#!/usr/bin/env python3
"""Warn when a turn stops while the current mandatory stage remains open."""

from __future__ import annotations

import json
from pathlib import Path

from evolution_hook_common import active_stage_state, read_hook_input, repository_root


def stop_warning(root: Path) -> str:
    state = active_stage_state(root)
    invalid_newer = state.get("invalid_newer_reports", "none")
    if (
        state["stage_report"] != "none"
        and state.get("validation_status") == "valid"
        and state["closure_verdict"] == "pass"
        and invalid_newer == "none"
    ):
        return ""
    if state["stage_report"] == "none":
        return (
            "No valid v2 stage report is available"
            f"; invalid candidate reports={invalid_newer}. Do not claim stage or campaign completion, "
            "archive, or advance a version until the report and contract validators pass."
        )
    return (
        f"Stage {state['stage_report']} remains open with closure={state['closure_verdict']} "
        f"and current task={state['current_task_id']}; invalid newer reports={invalid_newer}. "
        "Do not claim stage or campaign completion. "
        "Persist intermediate evidence, resume actionable implementation, and run the closure validators "
        "before archive or version advancement. A stage-local external evidence ceiling is not a campaign "
        "blocker: cap the claim, record evidence debt, and continue authorized report work. An explicit user "
        "pause may stop execution without changing the stage status."
    )


def main() -> None:
    hook_input = read_hook_input()
    root = repository_root(Path(hook_input.get("cwd", Path.cwd())))
    warning = stop_warning(root)
    if warning:
        print(json.dumps({"continue": True, "systemMessage": warning}, ensure_ascii=False))


if __name__ == "__main__":
    main()
