from __future__ import annotations

import importlib.util
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.governed_memory import GovernedMemorySurface
from tests.test_runtime import ScriptedProvider


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def future(days: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def mutate(client: TestClient, app_id: str, revision: int, op: str, data: dict) -> int:
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
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def create_memory_workflow(client: TestClient) -> str:
    app_id = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "Governed memory runtime", "requirement": "Expose governed memory context."},
    ).json()["id"]
    revision = 0
    for node in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {
            "id": "end",
            "type": "end",
            "title": "End",
            "config": {
                "outputs": {
                    "all_inputs": {"$ref": {"node_id": "$inputs", "path": []}},
                }
            },
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
    client: TestClient,
    *,
    app_id: str,
    content: str,
    source_id: str,
    scope_id: str = "project-alpha",
    expires_at: str | None = None,
) -> str:
    response = client.post(
        "/api/v1/platform/governed-memory",
        headers=headers(),
        json={
            "permission": {
                "actor_id": "operator-a",
                "owner_id": app_id,
                "scope_id": scope_id,
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
            "expires_at": expires_at or future(days=10),
            "reason": "seed governed runtime memory",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def run_workflow(client: TestClient, app_id: str, inputs: dict) -> dict:
    created = client.post(
        f"/api/v1/applications/{app_id}/runs",
        headers=headers(),
        json={"inputs": inputs, "use_draft": True},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    for _ in range(100):
        record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
        if record["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert record["status"] == "succeeded", record
    return record


def test_v02_138_runtime_opt_in_injects_scoped_governed_memory_and_audits(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = create_memory_workflow(client)
        create_memory(client, app_id=app_id, content="Use bounded paid-model validation before quality claims.", source_id="note-active")

        record = run_workflow(
            client,
            app_id,
            {
                "__governed_memory__": {
                    "enabled": True,
                    "actor_id": "operator-a",
                    "scope_id": "project-alpha",
                    "purpose": "runtime context retrieval",
                    "reason": "inject scoped memory for workflow run",
                    "limit": 5,
                }
            },
        )

        context = record["outputs"]["all_inputs"]["__governed_memory_context__"]
        assert context["enabled"] is True
        assert context["owner_id"] == app_id
        assert context["scope_id"] == "project-alpha"
        assert context["retrieved_count"] == 1
        assert context["items"][0]["content"] == "Use bounded paid-model validation before quality claims."

        stream_id = quote(GovernedMemorySurface.audit_stream_id(app_id, "project-alpha"), safe="")
        events = client.get(f"/v1/streams/{stream_id}", headers=headers()).json()
        assert "governed_memory.read" in [event["type"] for event in events]


def test_v02_138_runtime_does_not_retrieve_without_opt_in(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = create_memory_workflow(client)
        create_memory(client, app_id=app_id, content="This memory must not be ambient.", source_id="note-ambient")

        record = run_workflow(client, app_id, {})

        assert "__governed_memory_context__" not in record["outputs"]["all_inputs"]


def test_v02_138_runtime_excludes_revoked_and_expired_memory(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = create_memory_workflow(client)
        active_id = create_memory(
            client,
            app_id=app_id,
            content="Keep this active memory.",
            source_id="note-keep",
            expires_at=future(days=10),
        )
        revoked_id = create_memory(
            client,
            app_id=app_id,
            content="Do not retrieve revoked memory.",
            source_id="note-revoke",
            expires_at=future(days=10),
        )
        expiring_id = create_memory(
            client,
            app_id=app_id,
            content="Do not retrieve expired memory.",
            source_id="note-expire",
            expires_at=future(days=1),
        )

        revoke = client.post(
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
                "reason": "exclude revoked memory from retrieval",
            },
        )
        assert revoke.status_code == 200, revoke.text
        expire = client.post(
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
                "reason": "expire due memory",
                "now": future(days=2),
            },
        )
        assert expire.status_code == 200, expire.text
        assert expiring_id in [item["id"] for item in expire.json()["expired"]]

        record = run_workflow(
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

        context = record["outputs"]["all_inputs"]["__governed_memory_context__"]
        contents = [item["content"] for item in context["items"]]
        assert active_id in [item["id"] for item in context["items"]]
        assert contents == ["Keep this active memory."]
        assert "Do not retrieve revoked memory." not in contents
        assert "Do not retrieve expired memory." not in contents


def test_v02_138_evidence_script_reports_completed_runtime_integration() -> None:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_138_e10_runtime_memory_retrieval_integration.py"
    spec = importlib.util.spec_from_file_location("v02_138_e10_runtime_memory_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    evidence = module.build_evidence()

    assert evidence["status"] == "completed"
    assert evidence["checks"]["runtime_opt_in_retrieves_scoped_memory"] is True
    assert evidence["checks"]["no_opt_in_no_retrieval"] is True
    assert evidence["checks"]["revoked_and_expired_excluded"] is True
    assert evidence["checks"]["read_audit_event_written"] is True
    assert evidence["boundaries"]["unrestricted_memory_allowed"] is False
