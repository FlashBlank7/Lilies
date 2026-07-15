from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.blocks import LoopConfig, build_block_registry
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.sandbox import CommandResult


HEADERS = {"authorization": "Bearer workflow-test"}
SCENARIO_ID = "codex_like_workspace_agent"
WORKSPACE_SENTINEL = "LILIES_CODEX_FEEDBACK_SENTINEL"
ROOT = Path(__file__).resolve().parents[1]


class CodexFeedbackProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.loop_prompts: list[str] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

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
        prompt = "\n".join(
            block.text or ""
            for message in messages
            for block in message.content
        )
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 3}}})
        if "Plan the workspace task" in system:
            text = json.dumps({
                "goal": "Read workspace evidence and answer",
                "steps": ["read README.md", "use the evidence", "return the result"],
                "read_only_first": True,
                "likely_tools": ["Read"],
                "risks": ["workspace boundary"],
                "done_when": "the final answer cites the observed marker",
            })
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            })
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 12},
            })
            return

        self.loop_prompts.append(prompt)
        if WORKSPACE_SENTINEL not in prompt:
            tool_input = {"path": "README.md", "offset": 0, "limit": 200}
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "tool_use", "id": "read-workspace", "name": "Read", "input": {}},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 5},
            })
            return

        answer = f"Workspace evidence observed: {WORKSPACE_SENTINEL}. The feedback loop completed."
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "text_delta", "text": answer},
        })
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 8},
        })


class LocalReadSandbox:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.commands: list[list[str]] = []

    async def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        max_output: int = 200_000,
    ) -> CommandResult:
        self.commands.append(argv)
        if argv[:2] != ["python", "-c"] or "splitlines()" not in argv[2]:
            return CommandResult(stdout="", stderr=f"unsupported command: {argv}", exit_code=127)
        path = (self.workspace / argv[3]).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            return CommandResult(stdout="", stderr="path escapes workspace", exit_code=1)
        offset, limit = int(argv[4]), int(argv[5])
        lines = path.read_text(encoding="utf-8").splitlines()[offset: offset + limit]
        output = "\n".join(
            f"{index + 1:6d}\t{line}"
            for index, line in enumerate(lines, start=offset)
        )
        return CommandResult(stdout=output[:max_output], stderr="", exit_code=0)


class LocalReadSandboxes:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.sessions: dict[str, LocalReadSandbox] = {}

    def resolve_workspace(self, requested: str, *, create: bool = False) -> Path:
        path = Path(requested)
        candidate = path if path.is_absolute() else self.workspace_root / path
        resolved = candidate.resolve()
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise ValueError("workspace must stay under the configured root")
        if create:
            resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise ValueError(f"workspace does not exist: {resolved}")
        return resolved

    async def get_or_create(self, session_id: str, workspace_path: str, *_: Any) -> LocalReadSandbox:
        if session_id not in self.sessions:
            self.sessions[session_id] = LocalReadSandbox(self.resolve_workspace(workspace_path))
        return self.sessions[session_id]

    async def remove(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    async def close(self) -> None:
        self.sessions.clear()


def make_app(tmp_path: Path) -> tuple[Any, Settings, CodexFeedbackProvider]:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    (settings.workspace_root / "README.md").write_text(
        f"# Scenario fixture\n\n{WORKSPACE_SENTINEL}\n",
        encoding="utf-8",
    )
    provider = CodexFeedbackProvider()
    app = create_app(settings, provider)
    sandboxes = LocalReadSandboxes(settings.workspace_root)
    services = app.state.services
    services.sandboxes = sandboxes
    services.workflow_runtime.sandboxes = sandboxes
    services.runtime.sandboxes = sandboxes
    return app, settings, provider


def create_and_apply(client: TestClient) -> tuple[str, dict[str, Any]]:
    application = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={
            "name": "Codex feedback scenario",
            "requirement": "Build a workflow that can work like Codex inside a selected workspace.",
        },
    ).json()
    draft = client.get(
        f"/api/v1/applications/{application['id']}/draft",
        headers=HEADERS,
    ).json()
    applied = client.post(
        f"/api/v1/applications/{application['id']}/scenarios/{SCENARIO_ID}/apply",
        headers=HEADERS,
        json={
            "expected_revision": draft["revision"],
            "expected_content_hash": draft["content_hash"],
            "idempotency_key": "apply-codex-scenario",
        },
    )
    assert applied.status_code == 200, applied.text
    return application["id"], applied.json()


def wait_for_run(client: TestClient, run_id: str) -> dict[str, Any]:
    run: dict[str, Any] = {}
    for _ in range(200):
        run = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS).json()
        if run["status"] in {"succeeded", "failed", "cancelled", "paused"}:
            return run
        time.sleep(0.01)
    return run


def test_codex_scenario_catalog_and_atomic_apply(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    with TestClient(app) as client:
        catalog = client.get("/api/v1/scenarios", headers=HEADERS)
        assert catalog.status_code == 200, catalog.text
        summary = catalog.json()[0]
        assert summary["id"] == SCENARIO_ID
        assert summary["required_envelope"].startswith("E2")
        assert summary["evidence_profile"]["selected_level"] == "H2"
        assert summary["evidence_profile"]["status"] == "component_verified"
        assert "production reliability or SLO" in summary["evidence_profile"]["excluded_claims"]
        assert summary["acceptance_case_count"] == 3

        app_id, applied = create_and_apply(client)
        assert applied["operations_applied"] == 2
        assert applied["revision"] == 1
        assert applied["validation"]["valid"] is True, applied["validation"]
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=HEADERS).json()
        assert len(draft["snapshot"]["tests"]) == 3
        workflow = draft["snapshot"]["workflow"]
        node_types = {node["type"] for node in workflow["nodes"]}
        assert "claude_agent" not in node_types
        assert {
            "context_assembler",
            "model_turn",
            "permission_gate",
            "sandbox_boundary",
            "loop",
            "event_recorder",
            "answer",
        } <= node_types
        loop = next(node for node in workflow["nodes"] if node["type"] == "loop")
        assert loop["config"]["checkpoint_each_iteration"] is True
        assert loop["config"]["state_update"]["$ref"]["node_id"] == "loop_end"
        assert loop["config"]["feedback_value"]["$ref"]["node_id"] == "loop_end"
        assert loop["config"]["cancel_condition"]["expected"] is True
        nested_types = {node["type"] for node in loop["config"]["workflow"]["nodes"]}
        assert {"model_turn", "tool_call_router", "tool_executor", "tool_result_normalizer", "stop_continue_controller"} <= nested_types

        before_stale = draft["content_hash"]
        stale = client.post(
            f"/api/v1/applications/{app_id}/scenarios/{SCENARIO_ID}/apply",
            headers=HEADERS,
            json={
                "expected_revision": 0,
                "expected_content_hash": before_stale,
                "replace_existing": True,
                "idempotency_key": "stale-scenario-replace",
            },
        )
        assert stale.status_code == 409, stale.text
        after_stale = client.get(f"/api/v1/applications/{app_id}/draft", headers=HEADERS).json()
        assert after_stale["content_hash"] == before_stale
        assert after_stale["revision"] == draft["revision"]


def test_codex_feedback_loop_reads_then_feeds_tool_result_to_next_turn(tmp_path: Path) -> None:
    app, settings, provider = make_app(tmp_path)
    with TestClient(app) as client:
        app_id, _ = create_and_apply(client)
        created = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=HEADERS,
            json={
                "use_draft": True,
                "workspace_path": ".",
                "inputs": {
                    "task": "Read README.md and summarize the observed marker.",
                    "workspace_path": ".",
                    "network_policy": "none",
                    "cancel_requested": False,
                    "__permissions__": {"codex_permission": True},
                },
            },
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        run = wait_for_run(client, run_id)
        assert run["status"] == "succeeded", run
        assert WORKSPACE_SENTINEL in run["outputs"]["answer"]
        assert len(provider.loop_prompts) == 2
        assert WORKSPACE_SENTINEL not in provider.loop_prompts[0]
        assert WORKSPACE_SENTINEL in provider.loop_prompts[1]

        events = client.get(f"/v1/streams/{run_id}", headers=HEADERS).json()
        event_types = [event["type"] for event in events]
        assert "permission.plan" in event_types
        assert "permission.resolved" in event_types
        assert "sandbox.boundary.declared" in event_types
        assert event_types.count("loop.iteration.started") == 2
        assert event_types.count("loop.iteration.completed") == 2
        assert event_types.count("loop.checkpoint.saved") == 2
        assert any(event_type.endswith("tool.completed") for event_type in event_types)
        completed = [event["data"] for event in events if event["type"] == "loop.iteration.completed"]
        assert [item["stop_reason"] for item in completed] == ["continue", "break_condition"]

        db = sqlite3.connect(settings.data_dir / "agent_platform.db")
        try:
            checkpoints = db.execute(
                "SELECT checkpoint_id, data_json FROM checkpoints WHERE run_id=? ORDER BY checkpoint_id",
                (run_id,),
            ).fetchall()
        finally:
            db.close()
        assert [row[0] for row in checkpoints] == [
            "codex_loop:iteration:1",
            "codex_loop:iteration:2",
        ]
        first_checkpoint = json.loads(checkpoints[0][1])
        assert WORKSPACE_SENTINEL in json.dumps(first_checkpoint["feedback"], ensure_ascii=False)


def test_codex_loop_explicit_cancel_stops_after_iteration_boundary(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path)
    with TestClient(app) as client:
        app_id, _ = create_and_apply(client)
        created = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=HEADERS,
            json={
                "use_draft": True,
                "workspace_path": ".",
                "inputs": {
                    "task": "Read README.md, then stop at the explicit iteration boundary.",
                    "workspace_path": ".",
                    "network_policy": "none",
                    "cancel_requested": True,
                    "__permissions__": {"codex_permission": True},
                },
            },
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        run = wait_for_run(client, run_id)
        assert run["status"] == "succeeded", run
        events = client.get(f"/v1/streams/{run_id}", headers=HEADERS).json()
        completed = [event["data"] for event in events if event["type"] == "loop.iteration.completed"]
        assert len(completed) == 1
        assert completed[0]["stop_reason"] == "cancelled"


def test_loop_schema_keeps_legacy_contract_and_exposes_first_class_feedback_fields() -> None:
    registry = build_block_registry()
    legacy = registry.expand_template("claude_like_coding_agent", prefix="legacy")
    assert registry.validate_workflow(legacy) == []
    legacy_loop = next(node for node in legacy.nodes if node.type == "loop")
    config = LoopConfig.model_validate(legacy_loop.config)
    assert config.initial_state is None
    assert config.state_input_name == "loop_state"
    assert config.feedback_input_name == "tool_feedback"
    assert config.cancel_condition is None

    loop_definition = registry.get("loop").model_dump(mode="json")
    properties = loop_definition["config_schema"]["properties"]
    assert {
        "initial_state",
        "state_update",
        "feedback_value",
        "cancel_condition",
        "cancel_value",
        "checkpoint_each_iteration",
    } <= set(properties)


def test_codex_scenario_frontend_uses_server_preset_and_existing_customer_runtime() -> None:
    home = (ROOT / "platform/frontend/app/page.tsx").read_text(encoding="utf-8")
    detail = (ROOT / "platform/frontend/app/applications/[id]/page.tsx").read_text(encoding="utf-8")
    i18n = (ROOT / "platform/frontend/lib/i18n.ts").read_text(encoding="utf-8")

    assert "function isCodexWorkspaceRequirement" in home
    assert 'if (/(codex|像\\s*codex)/i.test(text)) return true' in home
    assert "applyCodexWorkspaceScenario" in home
    assert "/scenarios/codex_like_workspace_agent/apply" in home
    assert "await applyCodexWorkspaceScenario(app)" in home
    assert i18n.count("id: 'codex_workspace_agent'") == 2
    assert "Codex 式工作区智能体" in i18n
    assert "Codex-like workspace agent" in i18n
    assert "组件级证据" in i18n
    assert "component-level verification" in i18n

    for marker in (
        'data-customer-run-interface="start-controls"',
        'data-customer-run-interface="step-progress"',
        'data-customer-run-interface="result-card"',
        'className="permission-card"',
        'data-trace-guidance="summary"',
    ):
        assert marker in detail
