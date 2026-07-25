#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agent_platform.token_monitoring import (  # noqa: E402
    collect_token_monitor_snapshot,
    snapshot_delta,
)
def _number(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "0"
    return f"{int(value):,}"


def _money(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "$0.000000"
    return f"${float(value):.6f}"


def _verdict(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _model_egress_enabled() -> bool:
    configured: dict[str, str] = {}
    dotenv = ROOT / ".env"
    if dotenv.is_file():
        for raw_line in dotenv.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in {
                "MODEL_EGRESS_ENABLED",
                "LILIES_MODEL_EGRESS_ENABLED",
            }:
                configured[key.strip()] = value.strip().strip("'\"")
    for key in ("MODEL_EGRESS_ENABLED", "LILIES_MODEL_EGRESS_ENABLED"):
        if key in os.environ:
            configured[key] = os.environ[key]
    return any(
        value.strip().lower() in {"1", "true", "yes", "on"}
        for value in configured.values()
    )


def _print_snapshot(
    snapshot: Mapping[str, Any],
    *,
    delta: Mapping[str, Any] | None,
) -> None:
    safety = snapshot["safety"]
    usage = snapshot["usage"]
    totals = usage["totals"]
    print(f"Lilies token monitor  {snapshot['generated_at']}")
    print(
        "Safety: "
        f"processes={safety['model_capable_processes_active']} "
        f"startup-auto={safety['startup_auto_consumer_count']} "
        f"egress={_verdict(safety['model_egress_enabled'])} "
        f"evidence-complete={_verdict(safety['evidence_complete'])} "
        f"safe-now={_verdict(safety['safe_now'])} "
        f"safe-on-start={_verdict(safety['safe_on_platform_or_daemon_start'])}"
    )
    print(
        "Usage: "
        f"input={_number(totals['input_tokens'])} "
        f"output={_number(totals['output_tokens'])} "
        f"cache={_number(totals['cached_input_tokens'])} "
        f"reasoning-reported={_number(totals['reasoning_tokens'])} "
        f"unattributed={_number(totals['unattributed_tokens'])} "
        f"total={_number(totals['tokens'])} "
        f"calls={_number(totals['model_calls'])} "
        f"records={_number(totals['usage_records'])} "
        f"unknown-usage-calls={_number(totals['unknown_usage_model_calls'])} "
        f"cost={_money(totals['cost_usd'])}"
    )
    if delta is not None:
        print(
            "Delta: "
            f"tokens={_number(delta['tokens'])} "
            f"rate={float(delta['tokens_per_minute']):,.1f}/min "
            f"calls={_number(delta['model_calls'])} "
            f"unknown={_number(delta['unknown_usage_model_calls'])} "
            f"reconciled-unknown="
            f"{_number(delta['reconciled_unknown_usage_model_calls'])} "
            f"cost={_money(delta['cost_usd'])}"
        )
    print("By stage:")
    for row in usage["by_stage"]:
        print(
            f"  {row['name']:<42} "
            f"{_number(row['tokens']):>14} tokens  "
            f"{_number(row['model_calls']):>6} calls  "
            f"{_number(row['unknown_usage_model_calls']):>4} unknown  "
            f"{_money(row['cost_usd']):>12}"
        )
    if not usage["by_stage"]:
        print("  no recorded model usage")
    if snapshot["processes"]:
        print("Model-capable processes:")
        for process in snapshot["processes"]:
            print(
                f"  pid={process['pid']} kind={process['kind']} elapsed={process['elapsed']}"
            )
    auto = safety["startup_auto_consumers"]
    if any(auto.values()):
        print("Startup auto-consumers:")
        for name, count in auto.items():
            if count:
                print(f"  {name}={count}")
    if safety["missing_required_sources"]:
        print(
            "Missing required ledgers: "
            + ", ".join(safety["missing_required_sources"])
        )
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read platform, local Lilies, and collaborative-development ledgers without "
            "calling a model or provider."
        )
    )
    parser.add_argument(
        "--platform-db",
        type=Path,
        default=ROOT / "data" / "agent_platform.db",
    )
    parser.add_argument(
        "--lilies-db",
        type=Path,
        default=Path("~/.lilies/lilies.db").expanduser(),
    )
    parser.add_argument(
        "--bridge-db",
        type=Path,
        default=ROOT / "data" / "local-lilies-bridge.db",
    )
    parser.add_argument(
        "--development-db",
        type=Path,
        default=ROOT / "data" / "collaborative-development.db",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        help=(
            "Use an EXP-LILIES state root: platform-data and lilies-data databases "
            "override the default paths."
        ),
    )
    parser.add_argument(
        "--watch",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Refresh continuously; zero prints one snapshot.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON lines.")
    parser.add_argument(
        "--fail-on-risk",
        action="store_true",
        help="Return exit 3 when a model-capable process or startup auto-consumer exists.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.watch < 0:
        raise SystemExit("--watch must be zero or positive")
    if args.state_root is not None:
        state_root = args.state_root.expanduser().resolve()
        args.platform_db = state_root / "platform-data" / "agent_platform.db"
        args.bridge_db = state_root / "platform-data" / "local-lilies-bridge.db"
        args.development_db = (
            state_root / "platform-data" / "collaborative-development.db"
        )
        args.lilies_db = state_root / "lilies-data" / "lilies.db"

    previous: dict[str, Any] | None = None
    previous_at = time.monotonic()
    model_egress_enabled = _model_egress_enabled()
    while True:
        current_at = time.monotonic()
        snapshot = collect_token_monitor_snapshot(
            platform_db=args.platform_db,
            lilies_db=args.lilies_db,
            bridge_db=args.bridge_db,
            development_db=args.development_db,
            model_egress_enabled=model_egress_enabled,
        )
        delta = (
            snapshot_delta(previous, snapshot, elapsed_seconds=current_at - previous_at)
            if previous is not None
            else None
        )
        if args.json:
            print(
                json.dumps(
                    {**snapshot, "delta": delta},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
        else:
            _print_snapshot(snapshot, delta=delta)
        risk = (
            snapshot["safety"]["safe_now"] is not True
            or snapshot["safety"]["safe_on_platform_or_daemon_start"] is not True
        )
        if args.watch == 0:
            return 3 if args.fail_on_risk and risk else 0
        previous = snapshot
        previous_at = current_at
        try:
            time.sleep(max(0.25, args.watch))
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
