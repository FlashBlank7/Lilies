#!/usr/bin/env python3
"""Generate deterministic E08 cancellation and budget behavior evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.platform_harness import PlatformHarness, PlatformHarnessViolation
from agent_platform.storage import Storage


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "e08_cancellation_budget_behavior_v0.2.68"


class FakeActiveTask:
    def __init__(self) -> None:
        self.cancel_called = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancel_called = True


class NullProvider(ModelProvider):
    name = "null"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(False, False, False, False, False, 0, 0)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("v0.2.68 deterministic evidence does not call a model provider")
        yield


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


def cancellation_api_probe(root: Path) -> dict[str, Any]:
    settings = Settings(
        api_token="workflow-test",
        data_dir=root / "api-data",
        workspace_root=root / "api-workspaces",
    )
    app = create_app(settings, NullProvider())
    fake_task = FakeActiveTask()
    run_id = "e08-cancel-active-run"
    with TestClient(app) as client:
        app.state.services.workflow_runtime.active_tasks[run_id] = fake_task
        response = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers())
    return {
        "run_id": run_id,
        "status_code": response.status_code,
        "response": response.json(),
        "cancel_called": fake_task.cancel_called,
    }


async def budget_probe(root: Path) -> dict[str, Any]:
    storage = Storage(root / "budget-data")
    await storage.initialize()
    harness = PlatformHarness(storage=storage, max_model_calls_per_task=1)
    task_id = "e08-budget-task"
    await harness.start_task(task_id, kind="workflow_run", owner_id="e08", resource_id=task_id)
    await harness.record_usage(task_id, "model_call")
    violation = ""
    try:
        await harness.record_usage(task_id, "model_call")
    except PlatformHarnessViolation as error:
        violation = str(error)
    record = await harness.get_task(task_id)
    return {
        "task_id": task_id,
        "violation": violation,
        "status": record.status,
        "error": record.error,
        "usage_counts": record.usage_counts,
    }


def build_evidence(root: Path = ROOT / ".tmp" / "v02_68_e08_behavior") -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    return {
        "version": "v0.2.68",
        "cancellation_api": cancellation_api_probe(root),
        "budget_record": asyncio.run(budget_probe(root)),
        "not_full_sidecar_completion": True,
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(evidence: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    cancellation = evidence["cancellation_api"]
    budget = evidence["budget_record"]
    lines = [
        "# E08 cancellation/budget behavior v0.2.68",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Cancellation API status: `{cancellation['status_code']}`",
        f"- Cancellation called: `{cancellation['cancel_called']}`",
        f"- Budget task status: `{budget['status']}`",
        f"- Budget violation: `{budget['violation']}`",
        f"- Not full sidecar completion: `{evidence['not_full_sidecar_completion']}`",
        "",
    ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    evidence = build_evidence()
    json_path, summary_path = write_outputs(evidence, args.output_dir)
    print(json_path)
    print(summary_path)


if __name__ == "__main__":
    main()
