#!/usr/bin/env python3
"""Inject the durable Lilies evolution state on startup or resume."""

from __future__ import annotations

import json
from pathlib import Path

from evolution_hook_common import active_stage_state, read_hook_input, repository_root


def context_message(root: Path) -> str:
    state = active_stage_state(root)
    return (
        "Lilies report-application campaign is active. Its highest objective is: "
        f"{state['campaign_objective']} Read "
        "docs/evolution-control/PROGRAM_CHARTER.md and the current Stage Contract before planning. "
        f"Active stage: {state['stage_report']}; current mandatory task: {state['current_task_id']}; "
        f"contract: {state['contract_status']}; closure: {state['closure_verdict']}; "
        f"validation: {state.get('validation_status', 'unknown')}; "
        f"invalid newer reports: {state.get('invalid_newer_reports', 'none')}. "
        "The stage report is the only next-task sequencing authority beneath the campaign objective. "
        "Workingon is intermediate evidence only. Resume actionable implementation; external evidence "
        "unavailability limits claims and creates evidence debt, but cannot block unrelated report work."
    )


def main() -> None:
    hook_input = read_hook_input()
    root = repository_root(Path(hook_input.get("cwd", Path.cwd())))
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context_message(root),
                }
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
