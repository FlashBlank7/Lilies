#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agent_platform.token_monitoring import (  # noqa: E402
    TokenMonitorReadError,
    collect_token_monitor_snapshot,
    compact_token_monitor_snapshot,
    discover_model_capable_processes,
    snapshot_delta,
)
from agent_platform.local_lilies_client import LocalLiliesHttpClient  # noqa: E402
from agent_platform.local_lilies_discovery import discover_local_lilies  # noqa: E402


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
    return any(value.strip().lower() in {"1", "true", "yes", "on"} for value in configured.values())


def _listener_matches_process(
    pid: int,
    base_url: str,
    *,
    lsof_path: Path = Path("/usr/sbin/lsof"),
) -> bool:
    try:
        parsed = urlsplit(base_url)
        host = str(ipaddress.ip_address(parsed.hostname or ""))
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "http"
        or port is None
        or not ipaddress.ip_address(host).is_loopback
        or "%" in (parsed.hostname or "")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        return False
    if not lsof_path.is_file():
        return False
    try:
        result = subprocess.run(
            [
                str(lsof_path),
                "-nP",
                "-a",
                "-p",
                str(pid),
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-Fpn",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return False
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 65_536:
        return False
    observed_pid: int | None = None
    listeners: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            try:
                observed_pid = int(line[1:])
            except ValueError:
                return False
        elif line.startswith("n"):
            endpoint = line[1:]
            if endpoint.startswith("["):
                closing = endpoint.find("]")
                if closing <= 1 or not endpoint[closing + 1 :].startswith(":"):
                    return False
                raw_host = endpoint[1:closing]
                raw_port = endpoint[closing + 2 :]
            else:
                raw_host, separator, raw_port = endpoint.rpartition(":")
                if not separator:
                    return False
            try:
                listeners.append((str(ipaddress.ip_address(raw_host)), int(raw_port)))
            except ValueError:
                return False
    return observed_pid == pid and listeners == [(host, port)]


def _standalone_daemon_attestations(
    processes: Sequence[Mapping[str, Any]],
    *,
    discovery_path: Path,
) -> dict[int, bool]:
    daemon_pids = {
        process.get("pid")
        for process in processes
        if process.get("kind") == "local_lilies_daemon"
        and process.get("distribution") == "standalone"
        and isinstance(process.get("pid"), int)
        and not isinstance(process.get("pid"), bool)
    }
    if not daemon_pids:
        return {}
    try:
        discovery = asyncio.run(discover_local_lilies(discovery_path, LocalLiliesHttpClient()))
    except (OSError, RuntimeError):
        return {}
    pid = discovery.get("pid")
    egress_enabled = discovery.get("model_egress_enabled")
    base_url = discovery.get("base_url")
    if (
        discovery.get("status") != "available"
        or pid not in daemon_pids
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(egress_enabled, bool)
        or not isinstance(base_url, str)
        or not _listener_matches_process(pid, base_url)
    ):
        return {}
    return {pid: egress_enabled}


def _path_overlaps_standalone_state(
    source_path: Path,
    *,
    discovery_path: Path,
) -> bool:
    source = Path(os.path.abspath(source_path.expanduser()))
    standalone_state = Path(os.path.abspath(discovery_path.expanduser())).parent
    try:
        source.relative_to(standalone_state)
    except ValueError:
        pass
    else:
        return True
    try:
        resolved_state = standalone_state.resolve(strict=False)
        if source.resolve(strict=False).is_relative_to(resolved_state):
            return True
        inferred_daemon_db = standalone_state / "lilies.db"
        if source.exists() and inferred_daemon_db.exists():
            return os.path.samefile(source, inferred_daemon_db)
        return False
    except (OSError, RuntimeError):
        # The explicit legacy path remains acceptable only when it can be
        # proven separate from the standalone daemon's private state.
        return True


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
        f"unblocked={safety['unblocked_model_processes_active']} "
        f"unknown={safety['unknown_model_processes_active']} "
        f"startup-auto={safety['startup_auto_consumer_count']} "
        f"monitor-default-egress={_verdict(safety['model_egress_enabled'])} "
        f"external-spend-disabled="
        f"{_verdict(safety['external_codex_spend_disabled'])} "
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
                f"  pid={process['pid']} kind={process['kind']} "
                f"status={process['safety_status']} elapsed={process['elapsed']}"
            )
    auto = safety["startup_auto_consumers"]
    if any(auto.values()):
        print("Startup auto-consumers:")
        for name, count in auto.items():
            if count:
                print(f"  {name}={count}")
    if safety["missing_required_sources"]:
        print("Missing required ledgers: " + ", ".join(safety["missing_required_sources"]))
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read platform-owned ledgers and inspect standalone Lilies only through "
            "its bounded public loopback API, without calling a model or provider."
        )
    )
    parser.add_argument(
        "--platform-db",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--platform-owned-legacy-lilies-db",
        dest="platform_owned_legacy_lilies_db",
        type=Path,
        default=None,
        help=(
            "Optional legacy embedded-Lilies ledger owned by this platform/state root. "
            "Never point this at standalone Lilies private state."
        ),
    )
    parser.add_argument(
        "--standalone-discovery-record",
        type=Path,
        default=Path("~/.lilies/daemon.json").expanduser(),
        help="Same-user public standalone daemon discovery record.",
    )
    parser.add_argument(
        "--bridge-db",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--development-db",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        help=(
            "Use an EXP-LILIES state root for platform-owned data. A legacy embedded-Lilies "
            "ledger is never inferred and must still be named explicitly."
        ),
    )
    parser.add_argument(
        "--watch",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Refresh continuously; zero prints one snapshot.",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Emit full JSON lines.")
    output.add_argument(
        "--summary-json",
        action="store_true",
        help=(
            "Emit compact JSON lines with safety, aggregate usage, processes, "
            "and source activity counts."
        ),
    )
    parser.add_argument(
        "--fail-on-risk",
        action="store_true",
        help="Return exit 3 when a model-capable process or startup auto-consumer exists.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.watch < 0:
        raise SystemExit("--watch must be zero or positive")
    if args.state_root is not None:
        state_root = args.state_root.expanduser().resolve()
        args.platform_db = args.platform_db or (state_root / "platform-data" / "agent_platform.db")
        args.bridge_db = args.bridge_db or (state_root / "platform-data" / "local-lilies-bridge.db")
        args.development_db = args.development_db or (
            state_root / "platform-data" / "collaborative-development.db"
        )

    args.platform_db = args.platform_db or (ROOT / "data" / "agent_platform.db")
    args.bridge_db = args.bridge_db or (ROOT / "data" / "local-lilies-bridge.db")
    args.development_db = args.development_db or (ROOT / "data" / "collaborative-development.db")
    sqlite_sources = {
        "--platform-db": args.platform_db,
        "--bridge-db": args.bridge_db,
        "--development-db": args.development_db,
        "--platform-owned-legacy-lilies-db": args.platform_owned_legacy_lilies_db,
    }
    for option, source_path in sqlite_sources.items():
        if source_path is not None and _path_overlaps_standalone_state(
            source_path,
            discovery_path=args.standalone_discovery_record,
        ):
            parser.error(
                f"{option} must not point into or alias the standalone Lilies "
                "state directory derived from --standalone-discovery-record"
            )

    previous: dict[str, Any] | None = None
    previous_at = time.monotonic()
    model_egress_enabled = _model_egress_enabled()
    while True:
        current_at = time.monotonic()
        external_codex_spend_disabled = (
            None
            if args.state_root is None
            else (args.state_root.expanduser().resolve() / "EXTERNAL_CODEX_SPEND_DISABLED").exists()
        )
        process_inspection_complete = True
        try:
            processes = discover_model_capable_processes()
        except TokenMonitorReadError:
            processes = []
            process_inspection_complete = False
        process_egress_attestations = _standalone_daemon_attestations(
            processes,
            discovery_path=args.standalone_discovery_record,
        )
        required_sources = [
            "platform",
            "bridge",
            "collaborative_development",
            "standalone_lilies",
        ]
        if args.platform_owned_legacy_lilies_db is not None:
            required_sources.append("platform_owned_legacy_lilies")
        # A platform runtime may inject the paired global observability bracket.
        # This standalone CLI intentionally never reads or mints a daemon bearer,
        # so absent observability remains evidence-incomplete instead of being
        # reported as zero, even while the daemon is stopped.
        snapshot = collect_token_monitor_snapshot(
            platform_db=args.platform_db,
            bridge_db=args.bridge_db,
            development_db=args.development_db,
            platform_owned_legacy_lilies_db=args.platform_owned_legacy_lilies_db,
            standalone_usage_snapshot=None,
            standalone_observability_snapshot=None,
            required_sources=required_sources,
            model_egress_enabled=model_egress_enabled,
            external_codex_spend_disabled=external_codex_spend_disabled,
            process_egress_attestations=process_egress_attestations,
            process_records=processes,
            process_inspection_complete=process_inspection_complete,
        )
        delta = (
            snapshot_delta(previous, snapshot, elapsed_seconds=current_at - previous_at)
            if previous is not None
            else None
        )
        if args.summary_json:
            print(
                json.dumps(
                    compact_token_monitor_snapshot(snapshot, delta=delta),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
        elif args.json:
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
