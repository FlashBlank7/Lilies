#!/usr/bin/env python3
"""Persist a resume checkpoint without creating next-stage authority."""

from __future__ import annotations

import json
from pathlib import Path

from evolution_hook_common import checkpoint_payload, read_hook_input, repository_root


def write_checkpoint(root: Path, hook_input: dict | None = None, destination: Path | None = None) -> Path:
    destination = destination or root / "docs/workingon/evolution_checkpoint.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = checkpoint_payload(root, hook_input)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def main() -> None:
    hook_input = read_hook_input()
    root = repository_root(Path(hook_input.get("cwd", Path.cwd())))
    destination = write_checkpoint(root, hook_input)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": hook_input.get("hook_event_name", "PreCompact"),
                    "additionalContext": (
                        f"Evolution checkpoint saved at {destination.relative_to(root)}. "
                        "It records the current task only and is not a next-stage task source."
                    ),
                }
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
