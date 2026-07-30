#!/usr/bin/env python3
"""Append one read-only Lilies token/safety observation to a JSON time series.

The collector delegates discovery to ``monitor_lilies_tokens.py`` and never
calls a model or provider.  It preserves unknown and incomplete states instead
of turning missing ledgers into zero-usage claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MONITOR = ROOT / "scripts" / "monitor_lilies_tokens.py"


class TokenCaptureError(RuntimeError):
    """Raised when a bounded monitor observation cannot be persisted."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TokenCaptureError(f"cannot read token time series: {path}") from error
    if not isinstance(value, dict):
        raise TokenCaptureError("token time series root must be a JSON object")
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def capture(*, state_root: Path, label: str) -> dict[str, Any]:
    command = [
        sys.executable,
        str(MONITOR),
        "--state-root",
        str(state_root.resolve()),
        "--json",
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or len(lines) != 1:
        raise TokenCaptureError(
            "token monitor did not return one successful JSON observation"
        )
    try:
        snapshot = json.loads(lines[0])
    except ValueError as error:
        raise TokenCaptureError("token monitor returned invalid JSON") from error
    if not isinstance(snapshot, dict):
        raise TokenCaptureError("token monitor observation is not an object")
    safety = snapshot.get("safety")
    usage = snapshot.get("usage")
    if not isinstance(safety, dict) or not isinstance(usage, dict):
        raise TokenCaptureError("token monitor omitted safety or usage")
    observation = {
        "label": label,
        "captured_at": snapshot.get("generated_at"),
        "state_root": str(state_root.resolve()),
        "monitor": {
            "argv": [
                "scripts/monitor_lilies_tokens.py",
                "--state-root",
                str(state_root.resolve()),
                "--json",
            ],
            "exit_code": completed.returncode,
        },
        "snapshot": snapshot,
    }
    observation["observation_digest"] = _canonical_digest(observation)
    return observation


def append_observation(*, output: Path, observation: dict[str, Any]) -> dict[str, Any]:
    if output.exists():
        series = _read_object(output)
        if series.get("schema_version") != "1.0":
            raise TokenCaptureError("token time series schema version drifted")
        observations = series.get("observations")
        if not isinstance(observations, list):
            raise TokenCaptureError("token time series observations are invalid")
    else:
        series = {
            "schema_version": "1.0",
            "collector": "scripts/capture_lilies_token_timeseries.py",
            "claim_boundary": (
                "Zero known platform usage is not a machine-wide no-spend "
                "guarantee when required ledgers or PID-bound egress "
                "attestations are unavailable."
            ),
            "observations": [],
        }
        observations = series["observations"]
    observations.append(observation)
    series["observation_count"] = len(observations)
    series["latest_observation_digest"] = observation["observation_digest"]
    _atomic_json(output.resolve(), series)
    return series


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Append one provider-free Lilies token and background-work "
            "observation to a durable time series."
        )
    )
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.label.strip() or len(args.label) > 160:
        raise TokenCaptureError("label must contain 1..160 non-whitespace characters")
    observation = capture(
        state_root=args.state_root,
        label=args.label.strip(),
    )
    series = append_observation(output=args.output, observation=observation)
    safety = observation["snapshot"]["safety"]
    usage = observation["snapshot"]["usage"]["totals"]
    print(
        json.dumps(
            {
                "status": "captured",
                "output": str(args.output.resolve()),
                "observation_count": series["observation_count"],
                "captured_at": observation["captured_at"],
                "model_calls": usage.get("model_calls"),
                "tokens": usage.get("tokens"),
                "cost_usd": usage.get("cost_usd"),
                "background_consumption_observed": safety.get(
                    "background_consumption_observed"
                ),
                "safe_now": safety.get("safe_now"),
                "evidence_complete": safety.get("evidence_complete"),
                "missing_required_sources": safety.get(
                    "missing_required_sources"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TokenCaptureError as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, sort_keys=True))
        raise SystemExit(2) from error
