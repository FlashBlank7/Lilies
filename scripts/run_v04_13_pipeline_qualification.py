#!/usr/bin/env python3
"""Run fixed PIPE-Q01 through PIPE-Q28 and emit one evidence-bound bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agent_platform.collaboration_qualification import (  # noqa: E402
    FAULT_INJECTION_LANES,
    PIPELINE_QUALIFICATION_CASES,
    PIPELINE_QUALIFICATION_COMMANDS,
    QualificationCommandResult,
    QualificationPytestOutcomes,
    QualificationSurfaceResult,
    build_fault_injection_qualification,
    build_pipeline_qualification_bundle,
    qualification_source_revision,
)


FAULT_EVIDENCE_ENV = "LILIES_V04_13_FAULT_EVIDENCE_DIR"
PYTEST_RESULT_ENV = "LILIES_QUALIFICATION_PYTEST_RESULT"


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def source_revision() -> str:
    return qualification_source_revision(ROOT)


def _run_command(
    *,
    command_id: str,
    case_ids: tuple[str, ...],
    argv: tuple[str, ...],
    timeout_seconds: float,
    environment: Mapping[str, str],
    pytest_result_path: Path,
) -> QualificationCommandResult:
    started = time.perf_counter()
    child_environment = os.environ.copy()
    child_environment.update(environment)
    child_environment[PYTEST_RESULT_ENV] = str(pytest_result_path)
    child_environment["PYTEST_ADDOPTS"] = (
        "-p agent_platform.qualification_pytest_guard"
    )
    pytest_outcomes: QualificationPytestOutcomes | None = None
    try:
        completed = subprocess.run(
            list(argv),
            cwd=ROOT,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            env=child_environment,
        )
        output = completed.stdout + completed.stderr
        exit_code: int | None = completed.returncode
        try:
            outcome_bytes = pytest_result_path.read_bytes()
            outcome_payload = json.loads(outcome_bytes)
            pytest_outcomes = QualificationPytestOutcomes.model_validate(
                {
                    key: outcome_payload[key]
                    for key in (
                        "collected",
                        "passed",
                        "failed",
                        "errors",
                        "skipped",
                        "xfailed",
                        "xpassed",
                    )
                }
            )
            output += b"\nqualification pytest outcomes\n" + outcome_bytes
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            output += b"\nqualification pytest outcome record missing or invalid\n"
        clean_pytest = (
            pytest_outcomes is not None
            and pytest_outcomes.passed == pytest_outcomes.collected
            and not any(
                (
                    pytest_outcomes.failed,
                    pytest_outcomes.errors,
                    pytest_outcomes.skipped,
                    pytest_outcomes.xfailed,
                    pytest_outcomes.xpassed,
                )
            )
        )
        status = (
            "passed"
            if completed.returncode == 0 and clean_pytest
            else "failed"
        )
        if completed.returncode == 0 and not clean_pytest:
            exit_code = 86
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, bytes) else b""
        stderr = error.stderr if isinstance(error.stderr, bytes) else b""
        output = stdout + stderr + b"\nqualification command timed out\n"
        exit_code = 124
        status = "failed"
    except OSError as error:
        output = f"{type(error).__name__}: {error}".encode()
        exit_code = 127
        status = "failed"
    return QualificationCommandResult(
        command_id=command_id,
        case_ids=list(case_ids),
        argv=list(argv),
        status=status,
        exit_code=exit_code,
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        output_digest=_digest_bytes(output),
        pytest_outcomes=pytest_outcomes,
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _surface(path: Path | None) -> QualificationSurfaceResult | None:
    if path is None:
        return None
    return QualificationSurfaceResult.model_validate(_read_json(path))


def _required_evidence(path: Path | None, *, kind: str) -> dict[str, Any]:
    if path is None:
        raise ValueError(f"--{kind.replace('_', '-')} evidence is required")
    payload = _read_json(path)
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        raise ValueError(f"{path} is not {kind} evidence")
    return payload


def _load_fault_evidence(directory: Path) -> Any:
    records: list[dict[str, Any]] = []
    for lane in FAULT_INJECTION_LANES:
        path = directory / f"{lane.lane}.jsonl"
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing actual {lane.lane} fault evidence")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) != 100:
            raise ValueError(
                f"{lane.lane} recorded {len(lines)} iterations instead of 100"
            )
        for line in lines:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"{lane.lane} fault record is not an object")
            records.append(payload)
    return build_fault_injection_qualification(records)


def _list_catalog() -> dict[str, Any]:
    return {
        "cases": [
            {
                "case_id": item.case_id,
                "scenario": item.scenario,
                "required_result": item.required_result,
                "mandatory": item.mandatory,
                "surface_group": item.surface_group,
                "command_ids": list(item.command_ids),
            }
            for item in PIPELINE_QUALIFICATION_CASES
        ],
        "commands": [
            {
                "command_id": item.command_id,
                "case_ids": list(item.case_ids),
                "argv": list(item.argv),
            }
            for item in PIPELINE_QUALIFICATION_COMMANDS
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic v0.4.13 PIPE-Q01-Q28 qualification.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON destination. Without it, the bundle is printed.",
    )
    parser.add_argument(
        "--source-revision",
        help=(
            "Explicit revision; defaults to the stable managed "
            "source/config content digest."
        ),
    )
    parser.add_argument("--api-result", type=Path, help="Formal API surface evidence.")
    parser.add_argument(
        "--browser-result",
        type=Path,
        help="Formal browser surface evidence.",
    )
    parser.add_argument(
        "--development-api-result",
        type=Path,
        help="Standalone collaboration API/CLI surface evidence.",
    )
    parser.add_argument(
        "--development-browser-result",
        type=Path,
        help="Optional collaboration adapter browser evidence.",
    )
    parser.add_argument(
        "--reusable-evidence",
        type=Path,
        help="Digest-bound reusable collaborative-development evidence.",
    )
    parser.add_argument(
        "--live-handoff",
        type=Path,
        help="Digest-bound bounded live Lilies-Codex handoff evidence.",
    )
    parser.add_argument(
        "--dispatch-history",
        type=Path,
        help="Digest-bound durable autonomous-dispatch history evidence.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=900,
        help="Per-command timeout (default: 900 seconds).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the immutable Q01-Q28 catalog without running commands.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")
    if args.list:
        print(json.dumps(_list_catalog(), ensure_ascii=False, indent=2))
        return 0
    actual_source_revision = source_revision()
    if actual_source_revision == "unavailable":
        print(
            "qualification source revision is unavailable",
            file=sys.stderr,
        )
        return 2
    if (
        args.source_revision is not None
        and args.source_revision != actual_source_revision
    ):
        print(
            "--source-revision does not match the current bound source tree",
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="lilies-v0413-fault-") as raw_directory:
        fault_directory = Path(raw_directory).resolve()
        fault_directory.chmod(0o700)
        results: list[QualificationCommandResult] = []
        for spec in PIPELINE_QUALIFICATION_COMMANDS:
            print(
                f"[qualification] {spec.command_id}: {' '.join(spec.argv)}",
                file=sys.stderr,
                flush=True,
            )
            result = _run_command(
                command_id=spec.command_id,
                case_ids=spec.case_ids,
                argv=spec.argv,
                timeout_seconds=args.timeout_seconds,
                environment={FAULT_EVIDENCE_ENV: str(fault_directory)},
                pytest_result_path=(
                    fault_directory / f"{spec.command_id}-pytest.json"
                ),
            )
            results.append(result)
            print(
                f"[qualification] {spec.command_id}: {result.status} "
                f"({result.duration_ms:.3f} ms, {result.output_digest})",
                file=sys.stderr,
                flush=True,
            )

        try:
            if source_revision() != actual_source_revision:
                raise ValueError(
                    "qualification source changed while commands were running"
                )
            fault_injection = _load_fault_evidence(fault_directory)
            extra_evidence = [
                _required_evidence(
                    args.reusable_evidence,
                    kind="reusable_collaborative_development",
                ),
                _required_evidence(
                    args.live_handoff,
                    kind="bounded_live_lilies_codex_handoff",
                ),
                _required_evidence(
                    args.dispatch_history,
                    kind="durable_autonomous_dispatch_history",
                ),
            ]
            bundle = build_pipeline_qualification_bundle(
                results,
                source_revision=actual_source_revision,
                api_result=_surface(args.api_result),
                browser_result=_surface(args.browser_result),
                development_api_result=_surface(args.development_api_result),
                development_browser_result=_surface(
                    args.development_browser_result
                ),
                fault_injection=fault_injection,
                extra_evidence=extra_evidence,
            )
            if source_revision() != actual_source_revision:
                raise ValueError(
                    "qualification source changed before bundle finalization"
                )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"qualification bundle rejected: {error}", file=sys.stderr)
            return 2

    rendered = json.dumps(
        bundle.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        destination = args.output.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{uuid4().hex}.tmp"
        )
        temporary.write_text(rendered, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        print(
            json.dumps(
                {
                    "status": bundle.status,
                    "output": str(destination),
                    "bundle_digest": bundle.bundle_digest,
                    "summary": bundle.summary.model_dump(mode="json"),
                },
                ensure_ascii=False,
            )
        )
    return 0 if bundle.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
