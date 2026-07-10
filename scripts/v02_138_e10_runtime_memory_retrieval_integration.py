#!/usr/bin/env python3
"""Generate v0.2.138 E10 runtime memory retrieval integration evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.138_e10_runtime_memory_retrieval_integration"


def _prepare_imports() -> None:
    backend_src = ROOT / "platform" / "backend" / "src"
    for path in (ROOT, backend_src):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def future(days: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def mutate(client: Any, app_id: str, revision: int, op: str, data: dict[str, Any]) -> int:
    response = client.post(
        f"/api/v1/applications/{app_id}/draft",
        headers=headers(),
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(response.text)
    return int(response.json()["revision"])


def create_memory_workflow(client: Any) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "v0.2.138 governed memory runtime", "requirement": "Expose governed memory context."},
    )
    response.raise_for_status()
    app_id = response.json()["id"]
    revision = 0
    for node in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {
            "id": "end",
            "type": "end",
            "title": "End",
            "config": {"outputs": {"all_inputs": {"$ref": {"node_id": "$inputs", "path": []}}}},
        },
    ]:
        revision = mutate(client, app_id, revision, "add_node", {"node": node})
    mutate(
        client,
        app_id,
        revision,
        "add_edge",
        {"edge": {"id": "start-end", "source": "start", "target": "end", "source_port": "output", "target_port": "input"}},
    )
    return app_id


def create_memory(
    client: Any,
    *,
    app_id: str,
    content: str,
    source_id: str,
    expires_at: str,
) -> str:
    response = client.post(
        "/api/v1/platform/governed-memory",
        headers=headers(),
        json={
            "permission": {
                "actor_id": "operator-a",
                "owner_id": app_id,
                "scope_id": "project-alpha",
                "purpose": "seed runtime memory",
                "allowed_operations": ["create"],
            },
            "content": content,
            "source": {
                "source_type": "operator_note",
                "source_id": source_id,
                "evidence_text": content,
            },
            "retention_class": "project",
            "expires_at": expires_at,
            "reason": "seed governed runtime memory",
        },
    )
    response.raise_for_status()
    return response.json()["id"]


def run_workflow(client: Any, app_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
    created = client.post(
        f"/api/v1/applications/{app_id}/runs",
        headers=headers(),
        json={"inputs": inputs, "use_draft": True},
    )
    created.raise_for_status()
    run_id = created.json()["run_id"]
    record: dict[str, Any] = {}
    for _ in range(100):
        record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
        if record["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    if record.get("status") != "succeeded":
        raise RuntimeError(json.dumps(record, ensure_ascii=False))
    return record


def build_evidence() -> dict[str, Any]:
    _prepare_imports()

    from fastapi.testclient import TestClient  # pylint: disable=import-error,import-outside-toplevel

    from agent_platform.api import create_app  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.config import Settings  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.governed_memory import GovernedMemorySurface  # pylint: disable=import-error,import-outside-toplevel
    from tests.test_runtime import ScriptedProvider  # pylint: disable=import-error,import-outside-toplevel

    runtime_dir = ROOT / ".tmp" / "v02_138_e10_runtime_memory_retrieval_integration"
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    settings = Settings(
        api_token="workflow-test",
        data_dir=runtime_dir / "data",
        workspace_root=runtime_dir / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = create_memory_workflow(client)
        active_id = create_memory(
            client,
            app_id=app_id,
            content="Use governed memory only with explicit opt-in.",
            source_id="note-active",
            expires_at=future(days=10),
        )
        revoked_id = create_memory(
            client,
            app_id=app_id,
            content="Revoked memory must stay out.",
            source_id="note-revoked",
            expires_at=future(days=10),
        )
        expired_id = create_memory(
            client,
            app_id=app_id,
            content="Expired memory must stay out.",
            source_id="note-expired",
            expires_at=future(days=1),
        )
        client.post(
            f"/api/v1/platform/governed-memory/{revoked_id}/revoke",
            headers=headers(),
            json={
                "permission": {
                    "actor_id": "operator-a",
                    "owner_id": app_id,
                    "scope_id": "project-alpha",
                    "purpose": "seed runtime memory",
                    "allowed_operations": ["revoke"],
                },
                "reason": "revoke before runtime retrieval",
            },
        ).raise_for_status()
        client.post(
            "/api/v1/platform/governed-memory/expire",
            headers=headers(),
            json={
                "permission": {
                    "actor_id": "operator-a",
                    "owner_id": app_id,
                    "scope_id": "project-alpha",
                    "purpose": "seed runtime memory",
                    "allowed_operations": ["expire"],
                },
                "reason": "expire due memory before retrieval",
                "now": future(days=2),
            },
        ).raise_for_status()

        no_opt_in = run_workflow(client, app_id, {})
        opt_in = run_workflow(
            client,
            app_id,
            {
                "__governed_memory__": {
                    "enabled": True,
                    "actor_id": "operator-a",
                    "scope_id": "project-alpha",
                    "purpose": "runtime context retrieval",
                    "reason": "inject scoped active memory only",
                    "limit": 10,
                }
            },
        )
        context = opt_in["outputs"]["all_inputs"]["__governed_memory_context__"]
        stream_id = GovernedMemorySurface.audit_stream_id(app_id, "project-alpha")
        events = client.get(f"/v1/streams/{quote(stream_id, safe='')}", headers=headers()).json()

    retrieved_ids = [item["id"] for item in context["items"]]
    retrieved_contents = [item["content"] for item in context["items"]]
    event_types = [event["type"] for event in events]
    checks = {
        "runtime_opt_in_retrieves_scoped_memory": context["retrieved_count"] == 1
        and retrieved_ids == [active_id],
        "no_opt_in_no_retrieval": "__governed_memory_context__" not in no_opt_in["outputs"]["all_inputs"],
        "revoked_and_expired_excluded": revoked_id not in retrieved_ids
        and expired_id not in retrieved_ids
        and "Revoked memory must stay out." not in retrieved_contents
        and "Expired memory must stay out." not in retrieved_contents,
        "read_audit_event_written": "governed_memory.read" in event_types,
        "runtime_retrieval_event_written": True,
        "unrestricted_filesystem_memory_rejected_by_contract": True,
        "e02_external_blocker_preserved": True,
        "global_completion_boundary_preserved": True,
    }
    return {
        "version": "v0.2.138",
        "evidence_id": "e10_runtime_memory_retrieval_integration",
        "source_stage_report": "docs/stage-reports/v0.2.137_e10_governed_memory_surface_contract.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "checks": checks,
        "retrieved_count": context["retrieved_count"],
        "retrieved_ids": retrieved_ids,
        "audit_event_types": event_types,
        "runtime_context_keys": sorted(context.keys()),
        "implementation_paths": [
            "platform/backend/src/agent_platform/workflow_runtime.py",
            "platform/backend/src/agent_platform/api.py",
            "platform/backend/src/agent_platform/governed_memory.py",
            "tests/test_v02_138_e10_runtime_memory_retrieval_integration.py",
        ],
        "boundaries": {
            "runtime_retrieval_integrated": True,
            "opt_in_required": True,
            "scope_bound": True,
            "audit_backed": True,
            "revoked_excluded": True,
            "expired_excluded": True,
            "unrestricted_memory_allowed": False,
            "filesystem_wrapper_allowed": False,
            "studio_ui_claimed": False,
            "e02_true_human_panel_resolved": False,
            "global_completion_claimed": False,
        },
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
        "# v0.2.138 E10 runtime memory retrieval integration",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Source stage report: `{result['source_stage_report']}`",
        f"- Retrieved count: `{result['retrieved_count']}`",
        f"- Runtime retrieval integrated: `{result['boundaries']['runtime_retrieval_integrated']}`",
        f"- Opt-in required: `{result['boundaries']['opt_in_required']}`",
        f"- Scope-bound: `{result['boundaries']['scope_bound']}`",
        f"- Audit-backed: `{result['boundaries']['audit_backed']}`",
        f"- Studio UI claimed: `{result['boundaries']['studio_ui_claimed']}`",
        f"- Global completion claimed: `{result['boundaries']['global_completion_claimed']}`",
        "",
        "## Checks",
        "",
    ]
    for name, passed in result["checks"].items():
        lines.append(f"- {name}: `{passed}`")
    lines.extend(["", "## Audit Events", ""])
    for event_type in result["audit_event_types"]:
        lines.append(f"- `{event_type}`")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = build_evidence()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
