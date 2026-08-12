from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def _mutate(client: TestClient, application_id: str, revision: int, op: str, data: dict):
    return client.post(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )


def test_merge_config_nested_inside_changes_is_hoisted_not_rejected(tmp_path: Path) -> None:
    """Build d9a4dd84 turn 11: the model put merge_config inside changes and the
    draft edit failed NodeSpec validation. The intent is unambiguous — honor it."""

    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            scheduler_poll_seconds=3600,
        ),
        ScriptedProvider(),
    )
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "merge", "requirement": "Exercise nested merge_config."},
        ).json()["id"]
        revision = _mutate(client, application_id, 0, "add_node", {"node": {
            "id": "start",
            "type": "start",
            "title": "Start",
            "config": {"inputs": [{"name": "text", "type": "string"}]},
        }}).json()["revision"]
        revision = _mutate(client, application_id, revision, "add_node", {"node": {
            "id": "answer",
            "type": "answer",
            "title": "Answer",
            "config": {"answer": "ok", "x_note": "keep me"},
        }}).json()["revision"]

        # merge_config=False misplaced inside changes: full config replacement.
        replaced = _mutate(client, application_id, revision, "update_node", {
            "node_id": "answer",
            "changes": {"merge_config": False, "config": {"answer": "replaced"}},
        })
        assert replaced.status_code == 200, replaced.text
        snapshot = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS,
        ).json()["snapshot"]
        answer = next(n for n in snapshot["workflow"]["nodes"] if n["id"] == "answer")
        assert answer["config"] == {"answer": "replaced"}, "false must mean replace"

        # A top-level merge_config wins over a stray nested copy.
        merged = _mutate(client, application_id, replaced.json()["revision"], "update_node", {
            "node_id": "answer",
            "merge_config": True,
            "changes": {"merge_config": False, "config": {"x_note": "merged back"}},
        })
        assert merged.status_code == 200, merged.text
        snapshot = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS,
        ).json()["snapshot"]
        answer = next(n for n in snapshot["workflow"]["nodes"] if n["id"] == "answer")
        assert answer["config"] == {"answer": "replaced", "x_note": "merged back"}
