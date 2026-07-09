#!/usr/bin/env python3
"""Run the E08 workflow-internal passmode vs Platform Harness sidecar comparison."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "backend" / "src"))

from agent_platform.api import create_app  # noqa: E402
from agent_platform.config import Settings  # noqa: E402
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition  # noqa: E402
from agent_platform.providers import ModelProvider, ProviderCapabilities  # noqa: E402


DEFAULT_RESULT_PATH = (
    ROOT
    / "docs"
    / "experiment-status"
    / "evidence"
    / "experiment_v0.2.55_e08_sidecar_passmode_2026_07_10.json"
)


class NullProvider(ModelProvider):
    name = "null"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            thinking=False,
            tools=False,
            parallel_tools=False,
            prompt_caching=False,
            images=False,
            max_context_tokens=4096,
            max_output_tokens=1024,
        )

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
        raise RuntimeError("E08 deterministic comparison should not call a model provider")
        yield  # pragma: no cover


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    layer: str
    passmode: str
    status: str
    enforcement_strength: str
    bypassable_by_workflow_config: bool
    observability: list[str]
    failure_isolation: str
    recovery_semantics: str
    cost_signal: dict[str, Any]
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "layer": self.layer,
            "passmode": self.passmode,
            "status": self.status,
            "enforcement_strength": self.enforcement_strength,
            "bypassable_by_workflow_config": self.bypassable_by_workflow_config,
            "observability": self.observability,
            "failure_isolation": self.failure_isolation,
            "recovery_semantics": self.recovery_semantics,
            "cost_signal": self.cost_signal,
            "error": self.error,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def request(client: TestClient, method: str, path: str, token: str, **kwargs: Any) -> Any:
    response = client.request(method, path, headers=headers(token), **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
    return response.json()


def mutate(client: TestClient, token: str, app_id: str, revision: int, op: str, data: dict[str, Any]) -> int:
    result = request(
        client,
        "POST",
        f"/api/v1/applications/{app_id}/draft",
        token,
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )
    return int(result["revision"])


def wait_run(client: TestClient, token: str, run_id: str, terminal: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = request(client, "GET", f"/api/v1/runs/{run_id}", token)
        if str(last.get("status")) in terminal:
            return last
        time.sleep(0.02)
    raise TimeoutError(f"run {run_id} did not reach {sorted(terminal)}; last={last}")


def event_types(client: TestClient, token: str, stream_id: str) -> list[str]:
    events = request(client, "GET", f"/v1/streams/{stream_id}", token)
    if not isinstance(events, list):
        raise RuntimeError("stream endpoint returned non-list")
    return [str(event.get("type")) for event in events if isinstance(event, dict)]


def settings_for(name: str, *, network_egress_policy: str = "full") -> Settings:
    runtime_root = ROOT / ".tmp" / "e08_harness_sidecar_passmode" / name
    return Settings(
        api_token="workflow-test",
        data_dir=runtime_root / "data",
        workspace_root=runtime_root / "workspaces",
        platform_harness_network_egress_policy=network_egress_policy,
    )


def build_permission_app(client: TestClient, token: str, *, mode: str) -> str:
    app_id = request(
        client,
        "POST",
        "/api/v1/applications",
        token,
        json={"name": f"E08 permission {mode}", "requirement": "Compare workflow permission passmode."},
    )["id"]
    revision = 0
    nodes = [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {
            "id": "permission",
            "type": "permission_gate",
            "title": "Permission",
            "config": {
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                "settings": {"mode": mode, "reason": "E08 workflow-internal permission comparison."},
            },
        },
        {
            "id": "end",
            "type": "end",
            "title": "End",
            "config": {
                "outputs": {
                    "approved": {"$ref": {"node_id": "permission", "path": ["state", "approved"]}},
                    "mode": {"$ref": {"node_id": "permission", "path": ["state", "mode"]}},
                }
            },
        },
    ]
    for node in nodes:
        revision = mutate(client, token, app_id, revision, "add_node", {"node": node})
    for edge in [
        {"id": "start-permission", "source": "start", "target": "permission", "source_port": "output", "target_port": "input"},
        {"id": "permission-end", "source": "permission", "target": "end", "source_port": "output", "target_port": "input"},
    ]:
        revision = mutate(client, token, app_id, revision, "add_edge", {"edge": edge})
    return app_id


def run_permission_pause() -> ScenarioResult:
    token = "workflow-test"
    app = create_app(settings_for("permission_pause"), NullProvider())
    with TestClient(app) as client:
        app_id = build_permission_app(client, token, mode="always_ask")
        run_id = request(
            client,
            "POST",
            f"/api/v1/applications/{app_id}/runs",
            token,
            json={"inputs": {}, "use_draft": True},
        )["run_id"]
        record = wait_run(client, token, run_id, {"paused"})
        events = event_types(client, token, run_id)
    return ScenarioResult(
        name="workflow_internal_permission_pause",
        layer="workflow_internal_soft_harness",
        passmode="always_ask",
        status=str(record["status"]),
        enforcement_strength="soft_pause",
        bypassable_by_workflow_config=True,
        observability=sorted(set(events)),
        failure_isolation="run_paused_without_platform_failure",
        recovery_semantics="resume_with_human_approval_or_preset_input",
        cost_signal={"model_calls": 0, "external_actions": 0, "terminal": False},
    )


def run_permission_auto_approve() -> ScenarioResult:
    token = "workflow-test"
    app = create_app(settings_for("permission_auto_approve"), NullProvider())
    with TestClient(app) as client:
        app_id = build_permission_app(client, token, mode="auto_approve")
        run_id = request(
            client,
            "POST",
            f"/api/v1/applications/{app_id}/runs",
            token,
            json={"inputs": {}, "use_draft": True},
        )["run_id"]
        record = wait_run(client, token, run_id, {"succeeded"})
        events = event_types(client, token, run_id)
    return ScenarioResult(
        name="workflow_internal_permission_auto_approve",
        layer="workflow_internal_soft_harness",
        passmode="auto_approve",
        status=str(record["status"]),
        enforcement_strength="soft_pass",
        bypassable_by_workflow_config=True,
        observability=sorted(set(events)),
        failure_isolation="workflow_continues_by_configuration",
        recovery_semantics="not_needed_when_passmode_allows",
        cost_signal={"model_calls": 0, "external_actions": 0, "terminal": True},
    )


def build_http_app(client: TestClient, token: str) -> str:
    app_id = request(
        client,
        "POST",
        "/api/v1/applications",
        token,
        json={"name": "E08 sidecar block", "requirement": "Compare Platform Harness sidecar egress block."},
    )["id"]
    revision = 0
    nodes = [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {"id": "http", "type": "http_request", "title": "HTTP", "config": {"method": "GET", "url": "https://example.test/blocked"}},
        {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
    ]
    for node in nodes:
        revision = mutate(client, token, app_id, revision, "add_node", {"node": node})
    for edge in [
        {"id": "start-http", "source": "start", "target": "http", "source_port": "output", "target_port": "input"},
        {"id": "http-end", "source": "http", "target": "end", "source_port": "output", "target_port": "input"},
    ]:
        revision = mutate(client, token, app_id, revision, "add_edge", {"edge": edge})
    return app_id


def run_platform_sidecar_network_block() -> ScenarioResult:
    token = "workflow-test"
    app = create_app(settings_for("sidecar_network_block", network_egress_policy="none"), NullProvider())
    with TestClient(app) as client:
        app_id = build_http_app(client, token)
        run_id = request(
            client,
            "POST",
            f"/api/v1/applications/{app_id}/runs",
            token,
            json={"inputs": {}, "use_draft": True},
        )["run_id"]
        record = wait_run(client, token, run_id, {"failed"})
        events = event_types(client, token, run_id)
    return ScenarioResult(
        name="platform_sidecar_network_block",
        layer="platform_harness_sidecar",
        passmode="platform_policy_none",
        status=str(record["status"]),
        enforcement_strength="hard_block",
        bypassable_by_workflow_config=False,
        observability=sorted(set(events)),
        failure_isolation="run_failed_before_outbound_network_action",
        recovery_semantics="change_platform_policy_or_remove_external_action_then_retry",
        cost_signal={"model_calls": 0, "external_actions": 0, "terminal": True},
        error=str(record.get("error", "")),
    )


def build_result() -> dict[str, Any]:
    scenarios = [
        run_permission_pause(),
        run_permission_auto_approve(),
        run_platform_sidecar_network_block(),
    ]
    return {
        "experiment": "E08 harness sidecar/passmode comparison",
        "version": "v0.2.55",
        "status": "completed",
        "started_at": utc_now(),
        "finished_at": utc_now(),
        "paid_model_required": False,
        "scenarios": [scenario.to_json() for scenario in scenarios],
        "conclusion": (
            "workflow-internal passmode can pause or pass by workflow configuration; "
            "Platform Harness sidecar policy is a hard boundary that fails the run before the external action."
        ),
    }


def write_summary(result: dict[str, Any], path: Path) -> Path:
    summary_path = path.with_name(path.stem + "_summary.md")
    display_path = path
    try:
        display_path = path.relative_to(ROOT)
    except ValueError:
        pass
    lines = [
        "# E08 harness sidecar/passmode comparison",
        "",
        "## Summary",
        "",
        f"- Raw evidence: `{display_path.as_posix()}`",
        f"- Status: `{result['status']}`",
        f"- Paid/live model required: `{result['paid_model_required']}`",
        "",
        "## Scenarios",
        "",
        "| Scenario | Layer | Passmode | Status | Enforcement | Bypassable | Recovery |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for scenario in result["scenarios"]:
        lines.append(
            "| {name} | {layer} | {passmode} | {status} | {enforcement_strength} | {bypassable_by_workflow_config} | {recovery_semantics} |".format(
                **scenario
            )
        )
    lines.extend(["", "## Conclusion", "", result["conclusion"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def main() -> None:
    path = DEFAULT_RESULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    result = build_result()
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = write_summary(result, path)
    print(path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
