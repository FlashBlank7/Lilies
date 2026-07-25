"""Optional per-iteration recorder used by the v0.4.13 fault qualification.

The fault tests remain normal tests when the environment variable is absent.
The qualification runner supplies a private temporary directory and requires
exactly one digest-bound record for every operation it actually executes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from .collaboration_qualification import canonical_digest


_EVIDENCE_DIRECTORY_ENV = "LILIES_V04_13_FAULT_EVIDENCE_DIR"
FaultLane = Literal["reconnect", "idempotency", "lease", "concurrency"]


def record_fault_iteration(
    *,
    lane: FaultLane,
    iteration: int,
    command_id: str,
    command: Sequence[str],
    counters: Mapping[str, int],
    output: Mapping[str, Any],
) -> None:
    raw_directory = os.environ.get(_EVIDENCE_DIRECTORY_ENV)
    if raw_directory is None:
        return
    if not 1 <= iteration <= 100:
        raise ValueError("fault evidence iteration must be between 1 and 100")
    directory = Path(raw_directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_digest = canonical_digest(
        {
            "lane": lane,
            "iteration": iteration,
            "output": dict(output),
        }
    )
    payload = {
        "lane": lane,
        "iteration": iteration,
        "status": "passed",
        "counters": dict(counters),
        "command_id": command_id,
        "command": list(command),
        "output_digest": output_digest,
    }
    record = {
        **payload,
        "record_digest": canonical_digest(payload),
    }
    destination = directory / f"{lane}.jsonl"
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


__all__ = ["record_fault_iteration"]
