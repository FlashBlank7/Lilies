#!/usr/bin/env python3
"""Run and persist the reproducible v0.4.12 release gate."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "evidence" / "v0.4.12" / "release-gate.json"
NODE_BIN = Path.home() / ".nvm" / "versions" / "node" / "v24.15.0" / "bin"


COMMANDS = [
    (
        "focused_backend",
        ".venv/bin/python -m pytest -q tests/test_v04_12_openapi_connector_generation.py tests/test_v04_10_connector_embedding.py",
        ROOT,
    ),
    ("full_backend", ".venv/bin/python -m pytest -q", ROOT),
    (
        "ruff_changed_files",
        ".venv/bin/python -m ruff check platform/backend/src/agent_platform/openapi_connector.py platform/backend/src/agent_platform/connector_sdk.py platform/backend/src/agent_platform/api.py scripts/run_v04_12_openapi_generalization.py scripts/run_v04_12_openapi_live_contract.py scripts/validate_v04_12_openapi_evidence.py scripts/run_v04_12_release_gate.py tests/test_v04_12_openapi_connector_generation.py",
        ROOT,
    ),
    ("frontend_typecheck", "npm run lint", ROOT / "platform" / "frontend"),
    ("frontend_production_build", "npm run build", ROOT / "platform" / "frontend"),
    (
        "evidence_denominator",
        ".venv/bin/python scripts/validate_v04_12_openapi_evidence.py",
        ROOT,
    ),
    (
        "stage_report_template",
        ".venv/bin/python scripts/validate_stage_report_template.py docs/stage-reports/v0.4.12_openapi_connector_generation_loop.md",
        ROOT,
    ),
    (
        "evolution_control",
        ".venv/bin/python scripts/validate_evolution_control.py",
        ROOT,
    ),
]


def run_command(name: str, command: str, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        shell=True,
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    return {
        "name": name,
        "command": command,
        "cwd": str(cwd.relative_to(ROOT) or "."),
        "status": "passed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "output_tail": output[-4000:],
    }


def live_contract_gate() -> dict[str, Any]:
    path = ROOT / "docs" / "evidence" / "v0.4.12" / "inventree_live_contract.json"
    history_path = (
        ROOT / "docs" / "evidence" / "v0.4.12" / "inventree_live_contract_attempt_1_failure.json"
    )
    side_effect_path = ROOT / "docs" / "evidence" / "v0.4.12" / "inventree_live_side_effect.json"
    aggregate_path = (
        ROOT / "docs" / "evidence" / "v0.4.12" / "openapi_generalization_aggregate.json"
    )
    envelope_path = ROOT / "docs" / "evidence" / "v0.4.12" / "response_envelope_contract.json"
    try:
        raw = path.read_bytes()
        history_raw = history_path.read_bytes()
        side_effect_raw = side_effect_path.read_bytes()
        aggregate_raw = aggregate_path.read_bytes()
        envelope_raw = envelope_path.read_bytes()
        evidence = json.loads(raw)
        history = json.loads(history_raw)
        side_effect = json.loads(side_effect_raw)
        aggregate = json.loads(aggregate_raw)
        envelope = json.loads(envelope_raw)
        run = evidence["contract_run"]
        methods = set(evidence["selected_operations"].values())
        write_operations = {
            operation_id
            for operation_id, method in evidence["selected_operations"].items()
            if method != "GET"
        }
        write_result = next(
            item
            for item in run["results"]
            if item["case"]["kind"] == "positive"
            and item["case"]["operation_id"] in write_operations
        )
        observed_cases: dict[str, str] = {}
        for artifact in (history, evidence):
            for result in artifact["contract_run"]["results"]:
                case_id = result["case"]["id"]
                previous = observed_cases.get(case_id)
                if previous is not None and previous != result["status"]:
                    raise ValueError(f"conflicting historical status for {case_id}")
                observed_cases[case_id] = result["status"]
        statuses = (
            "passed",
            "failed",
            "skipped",
            "unsupported",
            "blocked_by_environment",
        )
        observed_counts = {
            status: sum(value == status for value in observed_cases.values()) for status in statuses
        }
        history_results = history["contract_run"]["results"]
        history_counts = {
            status: sum(item["status"] == status for item in history_results) for status in statuses
        }
        current_counts = {
            status: sum(item["status"] == status for item in run["results"]) for status in statuses
        }
        history_summary_consistent = (
            history["contract_run"]["status"] == "failed"
            and history_counts["failed"] > 0
            and all(
                history["contract_run"][status] == count for status, count in history_counts.items()
            )
        )
        current_summary_consistent = (
            run["status"] == "passed"
            and current_counts["passed"] == len(run["results"])
            and all(run[status] == count for status, count in current_counts.items())
        )
        denominator = aggregate["contract_case_denominator"]
        executed_input = write_result["executed_input_evidence"]["body_preview"]["body"]
        envelope_results = {item["expected_result"] for item in envelope["cases"]}
        historical_failures = [item for item in history_results if item["status"] == "failed"]
        retained_failure = aggregate["host_results"]["inventree"]["retained_failure"]
        retained_failure_consistent = (
            len(historical_failures) == 1
            and retained_failure["artifact"] == history_path.name
            and retained_failure["case_id"] == historical_failures[0]["case"]["id"]
            and retained_failure["operation_id"] == historical_failures[0]["case"]["operation_id"]
            and retained_failure["actual"] == historical_failures[0]["actual"]
            and aggregate["host_results"]["inventree"]["historical_failed_cases"]
            == len(historical_failures)
        )
        attempt_history = aggregate["execution_attempt_history"]
        attempt_history_consistent = (
            attempt_history["run_count"] == 2
            and attempt_history["result_records"] == len(history_results) + len(run["results"])
            and attempt_history["passed_records"]
            == history_counts["passed"] + current_counts["passed"]
            and attempt_history["failed_records"]
            == history_counts["failed"] + current_counts["failed"]
            and attempt_history["artifacts"] == [history_path.name, path.name]
        )
        passed = (
            run["status"] == "passed"
            and run["failed"] == 0
            and "GET" in methods
            and bool(methods - {"GET"})
            and side_effect["status"] == "passed"
            and side_effect["operation_id"] in write_operations
            and side_effect["actual"]["pk"] == write_result["response_evidence"]["identity"]["pk"]
            and all(
                side_effect["expected"][key] == executed_input[key] for key in ("company", "name")
            )
            and observed_counts["failed"] >= 1
            and history_summary_consistent
            and current_summary_consistent
            and retained_failure_consistent
            and attempt_history_consistent
            and denominator["passed"] == observed_counts["passed"]
            and denominator["failed"] == observed_counts["failed"]
            and denominator["not_run"] == denominator["total_generated"] - len(observed_cases)
            and aggregate["human_and_model_cost"]["human_rescue_count"] >= 1
            and envelope_results == {"passed", "failed"}
            and all(item["raw_body"] for item in envelope["cases"])
        )
        return {
            "path": str(path.relative_to(ROOT)),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "history_path": str(history_path.relative_to(ROOT)),
            "history_sha256": hashlib.sha256(history_raw).hexdigest(),
            "side_effect_path": str(side_effect_path.relative_to(ROOT)),
            "side_effect_sha256": hashlib.sha256(side_effect_raw).hexdigest(),
            "aggregate_path": str(aggregate_path.relative_to(ROOT)),
            "aggregate_sha256": hashlib.sha256(aggregate_raw).hexdigest(),
            "response_envelope_path": str(envelope_path.relative_to(ROOT)),
            "response_envelope_sha256": hashlib.sha256(envelope_raw).hexdigest(),
            "status": "passed" if passed else "failed",
            "contract_status": run["status"],
            "side_effect_status": side_effect["status"],
            "selected_methods": sorted(methods),
            "passed_cases": run["passed"],
            "failed_cases": run["failed"],
            "retained_unique_case_counts": observed_counts,
            "retained_unique_case_total": len(observed_cases),
            "historical_failure_retained": observed_counts["failed"] >= 1,
            "historical_summary_consistent": history_summary_consistent,
            "current_summary_consistent": current_summary_consistent,
            "retained_failure_consistent": retained_failure_consistent,
            "attempt_history_consistent": attempt_history_consistent,
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {
            "path": str(path.relative_to(ROOT)),
            "status": "failed",
            "error": str(error),
        }


def main() -> int:
    env = os.environ.copy()
    env["PATH"] = f"{NODE_BIN}:{env.get('PATH', '')}"
    results = [run_command(name, command, cwd, env) for name, command, cwd in COMMANDS]
    live_gate = live_contract_gate()
    payload = {
        "schema_version": "v0.4.12-release-gate-2",
        "status": (
            "passed"
            if all(item["status"] == "passed" for item in results)
            and live_gate["status"] == "passed"
            else "failed"
        ),
        "commands": results,
        "required_live_contract": live_gate,
        "browser_evidence": "browser/browser-evidence.json",
        "browser_execution_command": "node scripts/v04_12_openapi_connector_browser.mjs --api http://127.0.0.1:18120 --web http://127.0.0.1:13120 --token v0412-browser --output docs/evidence/v0.4.12/browser",
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "output": str(OUTPUT)}, ensure_ascii=False))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
