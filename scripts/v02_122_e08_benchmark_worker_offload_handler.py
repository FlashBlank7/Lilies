#!/usr/bin/env python3
"""Generate v0.2.122 E08 benchmark worker offload evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.122_e08_benchmark_worker_offload_handler"


def _prepare_imports() -> None:
    backend_src = ROOT / "platform" / "backend" / "src"
    for path in (ROOT, backend_src):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def reference_workflow() -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {
                "id": "permission",
                "type": "permission_gate",
                "title": "Permission",
                "config": {
                    "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                    "settings": {"auto_approve": True},
                },
            },
            {
                "id": "end",
                "type": "end",
                "title": "End",
                "config": {"outputs": {"ok": {"$ref": {"node_id": "permission", "path": ["output"]}}}},
            },
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "permission", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "permission", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }


def missing_harness_workflow() -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }


def passing_case(name: str = "complete harness") -> dict[str, Any]:
    reference = reference_workflow()
    return {
        "name": name,
        "reference": reference,
        "candidate": reference,
        "required_harness_nodes": ["permission_gate"],
    }


def failing_suite() -> dict[str, Any]:
    reference = reference_workflow()
    return {
        "name": "worker benchmark suite",
        "minimum_score": 0.8,
        "minimum_pass_rate": 1.0,
        "cases": [
            passing_case(),
            {
                "name": "missing harness",
                "reference": reference,
                "candidate": missing_harness_workflow(),
                "required_harness_nodes": ["permission_gate"],
            },
        ],
    }


def verify_contract() -> dict[str, Any]:
    _prepare_imports()

    from fastapi.testclient import TestClient  # pylint: disable=import-error,import-outside-toplevel

    from agent_platform.api import create_app  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.config import Settings  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.worker_runner import (  # pylint: disable=import-error,import-outside-toplevel
        PlatformHarnessWorkerRunner,
        build_platform_worker_handlers,
        platform_worker_handler_catalog,
    )
    from tests.test_runtime import ScriptedProvider  # pylint: disable=import-error,import-outside-toplevel

    data_dir = ROOT / ".tmp" / "v02_122_e08_benchmark_worker_offload_handler"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    settings = Settings(
        api_token="workflow-test",
        data_dir=data_dir / "data",
        workspace_root=data_dir / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        services = client.app.state.services
        harness = services.harness
        handlers = build_platform_worker_handlers(services)
        catalog = platform_worker_handler_catalog(handlers)
        entries = {entry["kind"]: entry for entry in catalog["entries"]}

        async def queue_case_task() -> None:
            await harness.start_task(
                "evidence-benchmark-case-task",
                kind="benchmark",
                owner_id="builder-benchmark",
                resource_id="complete harness",
                metadata={"case_payload": passing_case()},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(
                "evidence-benchmark-case-task",
                worker_id="producer",
                next_status="queued",
            )

        async def queue_suite_task() -> None:
            await harness.start_task(
                "evidence-benchmark-suite-task",
                kind="benchmark",
                owner_id="builder-benchmark-suite",
                resource_id="worker benchmark suite",
                metadata={"suite_payload": failing_suite()},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(
                "evidence-benchmark-suite-task",
                worker_id="producer",
                next_status="queued",
            )

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="v02-122-worker",
            lease_seconds=60,
            handlers=handlers,
        )

        client.portal.call(queue_case_task)

        async def run_case_worker_once():
            return await runner.run_once(kind="benchmark", limit=5)

        case_results = client.portal.call(run_case_worker_once)
        case_task = client.portal.call(harness.get_task, "evidence-benchmark-case-task")
        case_result = case_task.metadata["worker_runner"]["result"]

        client.portal.call(queue_suite_task)

        async def run_suite_worker_once():
            return await runner.run_once(kind="benchmark", limit=5)

        suite_results = client.portal.call(run_suite_worker_once)
        suite_task = client.portal.call(harness.get_task, "evidence-benchmark-suite-task")
        suite_result = suite_task.metadata["worker_runner"]["result"]

        api_response = client.post(
            "/api/v1/builder-benchmark/evaluate",
            headers=headers(),
            json=passing_case("api complete harness"),
        )
        api_response.raise_for_status()
        api_body = api_response.json()
        history = client.get(
            "/api/v1/builder-benchmark/history?owner_id=builder-benchmark",
            headers=headers(),
        )
        history.raise_for_status()
        heartbeats = {
            row.worker_id: row.model_dump(mode="json")
            for row in client.portal.call(harness.list_worker_heartbeats)
        }

    remaining_unavailable = [
        entry["kind"]
        for entry in catalog["entries"]
        if entry["status"] == "unavailable"
    ]
    history_records = history.json()
    checks = {
        "benchmark_catalog_implemented": entries["benchmark"]["status"] == "implemented"
        and entries["benchmark"]["executable"] is True
        and entries["benchmark"]["implementation"] == "benchmark_handler",
        "worker_completed_passing_case": len(case_results) == 1
        and case_results[0].status == "succeeded"
        and case_task.status == "succeeded"
        and case_result["passed"] is True,
        "worker_failed_failing_suite_with_report": len(suite_results) == 1
        and suite_results[0].status == "failed"
        and suite_task.status == "failed"
        and suite_result["passed"] is False
        and suite_result["failed_cases"] == ["missing harness"],
        "suite_usage_recorded_on_worker_task": suite_task.usage_counts.get("node_execution") == 2,
        "api_benchmark_path_preserved": api_body["report"]["passed"] is True,
        "benchmark_history_preserved": any(item["id"] == api_body["task_id"] for item in history_records)
        and any(item["id"] == "evidence-benchmark-case-task" for item in history_records),
        "heartbeat_registry_preserved": heartbeats["v02-122-worker"]["status"] == "idle"
        and heartbeats["v02-122-worker"]["metadata"]["last_task_status"] == "failed",
        "remaining_catalog_gaps_closed": set(remaining_unavailable) == set(),
        "full_execution_coverage_without_full_sidecar_claim": catalog["full_execution_coverage"] is True
        and catalog["not_full_sidecar_completion"] is True,
    }
    return {
        "version": "v0.2.122",
        "evidence_id": "e08_benchmark_worker_offload_handler",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.121_e08_remaining_sidecar_slice_reselection.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "checks": checks,
        "catalog_summary": {
            key: catalog[key]
            for key in (
                "version",
                "required_count",
                "cataloged_count",
                "implemented_count",
                "unavailable_count",
                "catalog_complete",
                "registered_catalog_complete",
                "full_execution_coverage",
                "not_full_sidecar_completion",
            )
        },
        "benchmark_entry": entries["benchmark"],
        "remaining_unavailable": remaining_unavailable,
        "case_worker_task": case_task.model_dump(mode="json"),
        "suite_worker_task": suite_task.model_dump(mode="json"),
        "case_worker_result": case_result,
        "suite_worker_result": suite_result,
        "api_task_id": api_body["task_id"],
        "history_count": len(history_records),
        "heartbeats": heartbeats,
        "implementation_paths": [
            "platform/backend/src/agent_platform/worker_runner.py",
            "tests/test_v02_122_e08_benchmark_worker_offload_handler.py",
            "scripts/v02_122_e08_benchmark_worker_offload_handler.py",
        ],
        "invariants": {
            "api_benchmark_path_preserved": True,
            "builder_build_implemented": True,
            "process_supervision_implemented": False,
            "distributed_queue_implemented": False,
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
            "stage_report_is_next_task_source": True,
        },
        "next_boundary": (
            "This closes the benchmark worker offload handler only. Full Platform Harness sidecar completion "
            "still needs production worker supervision, distributed queue semantics, and external KMS "
            "provider integration."
        ),
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.122 E08 benchmark worker offload handler",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Benchmark status: `{result['benchmark_entry']['status']}`",
        f"- Catalog full execution coverage: `{result['catalog_summary']['full_execution_coverage']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        f"- Next boundary: {result['next_boundary']}",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    for name, value in result["checks"].items():
        lines.append(f"| `{name}` | `{value}` |")
    lines.extend(["", "## Worker Result", ""])
    lines.append(f"- Case worker task id: `{result['case_worker_task']['id']}`")
    lines.append(f"- Case worker task status: `{result['case_worker_task']['status']}`")
    lines.append(f"- Suite worker task id: `{result['suite_worker_task']['id']}`")
    lines.append(f"- Suite worker task status: `{result['suite_worker_task']['status']}`")
    lines.append(f"- Suite failed cases: `{result['suite_worker_result']['failed_cases']}`")
    lines.append(f"- API benchmark task id: `{result['api_task_id']}`")
    lines.extend(["", "## Remaining Unavailable Worker Kinds", ""])
    for kind in result["remaining_unavailable"]:
        lines.append(f"- `{kind}`")
    lines.extend(["", "## Implementation Paths", ""])
    for path in result["implementation_paths"]:
        lines.append(f"- `{path}`")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = verify_contract()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
