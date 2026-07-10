#!/usr/bin/env python3
"""Generate v0.2.110 E08 complete handler catalog evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.110_e08_complete_handler_catalog"


class FakeScheduler:
    async def trigger_now(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"run_id": "evidence-run", "status": "queued"}


class FakeWorkflowRuntime:
    async def create_run(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"run_id": "evidence-workflow-run", "status": "queued", "version": 1, "draft_revision": None}

    async def run_test_suite(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"passed": True, "summary": {"total": 1, "failed": 0, "mandatory_failed": 0}, "tests": []}


class FakeWorkflowStore:
    async def get_draft(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"snapshot": None, "revision": 1, "content_hash": "fake"}


class FakeDraftPatcher:
    def preview(self, *_: Any, **__: Any) -> Any:
        class Response:
            supported = True

            def model_dump(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
                return {"supported": True, "intent": "rename_node", "message": "ok", "operations": [], "warnings": []}

        return Response()


class FakeBenchmark:
    def evaluate(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("benchmark handler is not exercised in this catalog fake")

    def evaluate_suite(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("benchmark handler is not exercised in this catalog fake")


class FakeHarness:
    async def record_usage(self, *_: Any, **__: Any) -> None:
        return None


class FakeServices:
    scheduler = FakeScheduler()
    workflow_runtime = FakeWorkflowRuntime()
    workflow_store = FakeWorkflowStore()
    draft_patcher = FakeDraftPatcher()
    benchmark = FakeBenchmark()
    harness = FakeHarness()


def verify_contract() -> dict[str, Any]:
    backend_src = ROOT / "platform" / "backend" / "src"
    if str(backend_src) not in sys.path:
        sys.path.insert(0, str(backend_src))

    from agent_platform.platform_harness import PlatformHarness  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.storage import Storage  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.worker_runner import (  # pylint: disable=import-error,import-outside-toplevel
        PLATFORM_WORKER_TASK_KINDS,
        PlatformHarnessWorkerRunner,
        build_platform_worker_handlers,
        platform_worker_handler_catalog,
    )

    async def scenario() -> dict[str, Any]:
        data_dir = ROOT / ".tmp" / "v02_110_e08_complete_handler_catalog"
        if data_dir.exists():
            shutil.rmtree(data_dir)
        storage = Storage(data_dir)
        await storage.initialize()
        handlers = build_platform_worker_handlers(FakeServices())
        catalog = platform_worker_handler_catalog(handlers)
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        await harness.start_task(
            "handler-catalog-builder-build-1",
            kind="builder_build",
            owner_id="owner-a",
            resource_id="build-a",
            worker_id="producer",
            lease_seconds=60,
        )
        await harness.release_task_lease(
            "handler-catalog-builder-build-1",
            worker_id="producer",
            next_status="queued",
        )
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=handlers,
        )
        results = await runner.run_once(limit=5)
        finished = await harness.get_task("handler-catalog-benchmark-1")
        entries = {entry["kind"]: entry for entry in catalog["entries"]}
        completed_slices = [
            "docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md",
            "docs/workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract_summary.md",
            "docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md",
            "docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md",
            "docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md",
        ]
        checks = {
            "catalog_covers_all_task_kinds": set(handlers) == set(PLATFORM_WORKER_TASK_KINDS)
            and catalog["catalog_complete"] is True,
            "catalog_registry_complete": catalog["registered_catalog_complete"] is True
            and catalog["missing_required_kinds"] == []
            and catalog["unregistered_required_kinds"] == [],
            "scheduler_manual_trigger_implemented": entries["scheduler_manual_trigger"]["status"] == "implemented"
            and entries["scheduler_manual_trigger"]["executable"] is True,
            "scheduler_trigger_implemented_by_v02_114": entries["scheduler_trigger"]["status"] == "implemented"
            and entries["scheduler_trigger"]["executable"] is True,
            "workflow_run_implemented_by_v02_116": entries["workflow_run"]["status"] == "implemented"
            and entries["workflow_run"]["executable"] is True,
            "test_suite_implemented_by_v02_118": entries["test_suite"]["status"] == "implemented"
            and entries["test_suite"]["executable"] is True,
            "draft_patch_preview_implemented_by_v02_120": entries["draft_patch_preview"]["status"] == "implemented"
            and entries["draft_patch_preview"]["executable"] is True,
            "benchmark_implemented_by_v02_122": entries["benchmark"]["status"] == "implemented"
            and entries["benchmark"]["executable"] is True,
            "unimplemented_kinds_are_deterministic_unavailable": all(
                entries[kind]["status"] == "unavailable"
                and entries[kind]["handler_registered"] is True
                and entries[kind]["operator_action"]
                for kind in set(PLATFORM_WORKER_TASK_KINDS) - {
                    "workflow_run",
                    "test_suite",
                    "scheduler_trigger",
                    "scheduler_manual_trigger",
                    "draft_patch_preview",
                    "benchmark",
                }
            ),
            "unavailable_handler_fails_task_deterministically": len(results) == 1
            and results[0].status == "failed"
            and "worker handler unavailable: builder_build" in results[0].error
            and finished.status == "failed"
            and "worker handler unavailable: builder_build" in finished.error,
            "coverage_exposed_without_full_execution_claim": catalog["full_execution_coverage"] is False
            and catalog["not_full_sidecar_completion"] is True,
        }
        return {
            "catalog": catalog,
            "checks": checks,
            "completed_slices": completed_slices,
            "unavailable_failure": {
                "task_id": results[0].task_id,
                "status": results[0].status,
                "error": results[0].error,
                "finished_status": finished.status,
            },
        }

    details = asyncio.run(scenario())
    return {
        "version": "v0.2.110",
        "evidence_id": "e08_complete_handler_catalog",
        "source_stage_report": "docs/stage-reports/v0.2.109_e08_remaining_sidecar_slice_reselection.md",
        "status": "completed" if all(details["checks"].values()) else "needs_attention",
        "checks": details["checks"],
        "catalog_summary": {
            key: details["catalog"][key]
            for key in (
                "required_count",
                "cataloged_count",
                "implemented_count",
                "unavailable_count",
                "catalog_complete",
                "registered_catalog_complete",
                "full_execution_coverage",
                "deterministic_gap_failure",
                "not_full_sidecar_completion",
            )
        },
        "catalog_entries": details["catalog"]["entries"],
        "unavailable_failure": details["unavailable_failure"],
        "completed_slices_preserved": details["completed_slices"],
        "implementation_paths": [
            "platform/backend/src/agent_platform/worker_runner.py",
            "platform/backend/src/agent_platform/api.py",
            "tests/test_v02_110_e08_complete_handler_catalog.py",
        ],
        "invariants": {
            "e08_full_sidecar_completion_claimed": False,
            "distributed_heartbeat_registry_implemented": False,
            "external_kms_provider_integrated": False,
            "workingon_is_not_task_source": True,
        },
        "next_boundary": (
            "This closes complete handler catalog coverage and deterministic gap failure only; real handlers for "
            "non-scheduler task kinds, production worker supervision, and external KMS provider integration remain open."
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
        "# v0.2.110 E08 complete handler catalog",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Catalog complete: `{result['catalog_summary']['catalog_complete']}`",
        f"- Registered catalog complete: `{result['catalog_summary']['registered_catalog_complete']}`",
        f"- Full execution coverage: `{result['catalog_summary']['full_execution_coverage']}`",
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
    lines.extend(["", "## Catalog Entries", "", "| Kind | Status | Executable |", "| --- | --- | --- |"])
    for entry in result["catalog_entries"]:
        lines.append(f"| `{entry['kind']}` | `{entry['status']}` | `{entry['executable']}` |")
    lines.extend(["", "## Completed Slices Preserved", ""])
    for path in result["completed_slices_preserved"]:
        lines.append(f"- `{path}`")
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
