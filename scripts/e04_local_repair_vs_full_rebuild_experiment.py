#!/usr/bin/env python3
"""Run the v0.2.37 E04 local repair vs full rebuild experiment."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator
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
from agent_platform.providers.base import ModelProvider, ProviderCapabilities  # noqa: E402

DEFAULT_RESULT_PATH = (
    ROOT
    / "docs"
    / "experiment-status"
    / "evidence"
    / "experiment_v0.2.37_e04_local_repair_vs_full_rebuild_2026_07_09.json"
)
RESULT_PATH = Path(os.getenv("E04_REPAIR_EXPERIMENT_RESULT_PATH", str(DEFAULT_RESULT_PATH)))
TIMEOUT_SECONDS = int(os.getenv("E04_FULL_REBUILD_TIMEOUT_SECONDS", "900"))

REQUIREMENT = (
    "Build a workflow that accepts input name and returns greeting exactly 'Hello {name}'. "
    "For name Ada the output must be exactly 'Hello Ada'. Include a mandatory acceptance test."
)


class UnusedProvider(ModelProvider):
    name = "unused"

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
        raise RuntimeError("E04 local repair fixture should not call a model provider")


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def request(client: TestClient, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    response = client.request(method, path, headers=headers(), **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"{method} {path}: expected JSON object")
    return data


def mutate(
    client: TestClient,
    app_id: str,
    revision: int,
    op: str,
    data: dict[str, Any],
) -> int:
    body = {
        "expected_revision": revision,
        "idempotency_key": f"e04-{op}-{uuid4()}",
        "op": op,
        "data": data,
    }
    return int(request(client, "POST", f"/api/v1/applications/{app_id}/draft", json=body)["revision"])


def build_local_repair_artifacts(data_dir: Path | None = None) -> dict[str, Any]:
    data_root = data_dir or ROOT / ".tmp" / "e04_local_repair_vs_full_rebuild" / "local"
    settings = Settings(
        api_token="workflow-test",
        data_dir=data_root / "data",
        workspace_root=data_root / "workspaces",
    )
    settings.prepare()
    app = create_app(settings, UnusedProvider())
    with TestClient(app) as client:
        app_id = request(
            client,
            "POST",
            "/api/v1/applications",
            json={"name": "E04 local repair fixture", "requirement": REQUIREMENT, "mode": "workflow"},
        )["id"]
        revision = 0
        for node in [
            {
                "id": "start",
                "type": "start",
                "title": "Input",
                "config": {"inputs": [{"name": "name", "type": "string"}]},
            },
            {
                "id": "template",
                "type": "template_transform",
                "title": "Greeting",
                "config": {
                    "template": "Hi {{ name }}",
                    "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}},
                },
            },
            {
                "id": "end",
                "type": "end",
                "title": "End",
                "config": {"outputs": {"greeting": {"$ref": {"node_id": "template", "path": ["text"]}}}},
            },
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {
                "id": "start-template",
                "source": "start",
                "target": "template",
                "source_port": "output",
                "target_port": "input",
            },
            {
                "id": "template-end",
                "source": "template",
                "target": "end",
                "source_port": "text",
                "target_port": "input",
            },
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Greeting contract",
            "requirement": "Name Ada must produce Hello Ada.",
            "frame": {
                "title": "Greeting output contract",
                "category": "content",
                "purpose": "Check that the template uses the exact required salutation.",
                "reviewer_guidance": "If this fails, inspect the template text before rebuilding the whole workflow.",
                "reference": "E04 fixture requirement",
                "failure_target": "template node text",
            },
            "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"}],
            "required_node_types": ["start", "template_transform", "end"],
            "feedback_hints": ["Patch the template text from Hi to Hello."],
        }})
        before = request(client, "POST", f"/api/v1/applications/{app_id}/tests/run")
        started = time.perf_counter()
        revision = mutate(client, app_id, revision, "update_node", {
            "node_id": "template",
            "changes": {"config": {"template": "Hello {{ name }}"}},
            "merge_config": True,
        })
        after = request(client, "POST", f"/api/v1/applications/{app_id}/tests/run")
        elapsed = round(time.perf_counter() - started, 4)
        draft = request(client, "GET", f"/api/v1/applications/{app_id}/draft")
    return {
        "application_id": app_id,
        "requirement": REQUIREMENT,
        "before_test_report": before,
        "after_test_report": after,
        "local_repair": {
            "status": "passed" if after.get("passed") else "failed",
            "operation_count": 1,
            "operations": [{"op": "update_node", "node_id": "template", "field": "config.template"}],
            "elapsed_seconds": elapsed,
            "final_revision": revision,
        },
        "draft_counts": {
            "nodes": len(draft["snapshot"]["workflow"]["nodes"]),
            "edges": len(draft["snapshot"]["workflow"]["edges"]),
            "tests": len(draft["snapshot"]["tests"]),
            "node_types": sorted({node["type"] for node in draft["snapshot"]["workflow"]["nodes"]}),
        },
    }


def run_paid_full_rebuild(data_dir: Path | None = None) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    default_settings = Settings()
    settings = Settings(
        api_token="workflow-test",
        data_dir=(data_dir or ROOT / ".tmp" / "e04_local_repair_vs_full_rebuild" / "rebuild") / "data",
        workspace_root=(data_dir or ROOT / ".tmp" / "e04_local_repair_vs_full_rebuild" / "rebuild") / "workspaces",
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or default_settings.deepseek_api_key,
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", default_settings.deepseek_base_url),
        deepseek_generator_model=os.getenv("DEEPSEEK_GENERATOR_MODEL", default_settings.deepseek_generator_model),
        deepseek_runtime_model=os.getenv("DEEPSEEK_RUNTIME_MODEL", default_settings.deepseek_runtime_model),
        deepseek_timeout_seconds=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", str(default_settings.deepseek_timeout_seconds))),
    )
    settings.prepare()
    if not settings.deepseek_api_key:
        return {"status": "skipped", "reason": "DEEPSEEK_API_KEY is not configured"}
    app = create_app(settings)
    with TestClient(app) as client:
        application = request(
            client,
            "POST",
            "/api/v1/applications",
            json={"name": "E04 full rebuild", "requirement": REQUIREMENT, "mode": "workflow"},
        )
        build = request(
            client,
            "POST",
            f"/api/v1/applications/{application['id']}/builds",
            json={
                "requirement": REQUIREMENT,
                "auto_publish": False,
                "max_turns": int(os.getenv("E04_FULL_REBUILD_MAX_TURNS", "30")),
                "max_repair_cycles": int(os.getenv("E04_FULL_REBUILD_MAX_REPAIR_CYCLES", "2")),
                "planning_mode": os.getenv("E04_FULL_REBUILD_PLANNING_MODE", "auto"),
            },
        )
        started = time.perf_counter()
        completed = wait_build(client, build["build_id"])
        elapsed = round(time.perf_counter() - started, 3)
        draft = request(client, "GET", f"/api/v1/applications/{application['id']}/draft")
        test_report: dict[str, Any] | None = None
        if draft["snapshot"]["tests"]:
            test_report = request(client, "POST", f"/api/v1/applications/{application['id']}/tests/run")
        builder_task = get_platform_task(client, build["build_id"])
    return {
        "status": "completed",
        "application_id": application["id"],
        "build_id": build["build_id"],
        "build_status": completed["status"],
        "build_error": completed.get("error", ""),
        "elapsed_seconds": elapsed,
        "builder_task": builder_task,
        "test_report": test_report,
        "draft_counts": {
            "nodes": len(draft["snapshot"]["workflow"]["nodes"]),
            "edges": len(draft["snapshot"]["workflow"]["edges"]),
            "tests": len(draft["snapshot"]["tests"]),
            "node_types": sorted({node["type"] for node in draft["snapshot"]["workflow"]["nodes"]}),
        },
    }


def wait_build(client: TestClient, build_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = request(client, "GET", f"/api/v1/builds/{build_id}")
        print("build", build_id, current["status"])
        if current["status"] in {"ready", "published", "needs_attention", "cancelled"}:
            return current
        time.sleep(3)
    raise TimeoutError(f"build timed out after {TIMEOUT_SECONDS} seconds")


def get_platform_task(client: TestClient, task_id: str) -> dict[str, Any] | None:
    try:
        return request(client, "GET", f"/api/v1/platform/harness/tasks/{task_id}")
    except Exception:
        return None


def compare(local: dict[str, Any], rebuild: dict[str, Any]) -> dict[str, Any]:
    local_success = bool(local["after_test_report"].get("passed"))
    rebuild_success = (
        rebuild.get("build_status") in {"ready", "published"}
        and bool((rebuild.get("test_report") or {}).get("passed", rebuild.get("build_status") in {"ready", "published"}))
    )
    usage_counts = ((rebuild.get("builder_task") or {}).get("usage_counts") or {})
    return {
        "local_success": local_success,
        "full_rebuild_success": rebuild_success,
        "local_elapsed_seconds": local["local_repair"]["elapsed_seconds"],
        "full_rebuild_elapsed_seconds": rebuild.get("elapsed_seconds"),
        "local_operation_count": local["local_repair"]["operation_count"],
        "full_rebuild_model_calls": usage_counts.get("model_call"),
        "full_rebuild_tool_calls": usage_counts.get("tool_call"),
        "narrow_conclusion": (
            "Local repair is cheaper and sufficient for this localized template failure."
            if local_success and (not rebuild_success or local["local_repair"]["elapsed_seconds"] <= float(rebuild.get("elapsed_seconds") or 999999))
            else "This run does not support preferring local repair for the fixed fixture."
        ),
    }


async def main() -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    local = build_local_repair_artifacts()
    rebuild = run_paid_full_rebuild()
    result = {
        "status": "completed",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "experiment_id": "E04_local_repair_vs_full_rebuild_v0.2.37",
        "question": "For a localized failing draft, is local repair cheaper/faster/more reliable than full Builder rebuild from the same requirement?",
        "evidence_level": "deterministic_local_repair_plus_paid_full_rebuild",
        "local_repair_arm": local,
        "full_rebuild_arm": rebuild,
        "comparison": compare(local, rebuild),
        "conclusion_boundary": (
            "This fixture covers a localized template failure in a mostly correct BlockFlow. "
            "It does not prove local repair is always better than full rebuild."
        ),
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(RESULT_PATH)
    print(result["status"])


if __name__ == "__main__":
    asyncio.run(main())
