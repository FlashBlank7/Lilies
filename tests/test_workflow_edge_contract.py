from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.blocks import build_block_registry
from agent_platform.config import Settings
from agent_platform.workflow_models import EdgeSpec, NodeSpec
from tests.test_runtime import ScriptedProvider


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer workflow-edge-test",
        "Content-Type": "application/json",
    }


def _mutate(
    client: TestClient,
    application_id: str,
    revision: int,
    op: str,
    data: dict[str, object],
):
    return client.post(
        f"/api/v1/applications/{application_id}/draft",
        headers=_headers(),
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )


def test_block_registry_validates_named_and_typed_incremental_ports() -> None:
    registry = build_block_registry()
    llm = NodeSpec(id="llm", type="llm", title="LLM", config={"prompt": "hello"})
    iteration = NodeSpec(
        id="iteration",
        type="iteration",
        title="Iteration",
        config={},
    )
    end = NodeSpec(id="end", type="end", title="End", config={"outputs": {}})

    missing = registry.validate_edge(
        iteration,
        end,
        EdgeSpec(
            id="bad-name",
            source="iteration",
            target="end",
            source_port="bogus",
            target_port="input",
        ),
    )
    assert missing == ["bad-name: unknown source port iteration.bogus"]

    # The default port name resolves to the block's primary output port.
    defaulted = registry.validate_edge(
        iteration,
        end,
        EdgeSpec(
            id="default-name",
            source="iteration",
            target="end",
            source_port="output",
            target_port="input",
        ),
    )
    assert defaulted == []

    incompatible = registry.validate_edge(
        llm,
        iteration,
        EdgeSpec(
            id="bad-type",
            source="llm",
            target="iteration",
            source_port="text",
            target_port="input",
        ),
    )
    assert incompatible == ["bad-type: incompatible ports string -> array"]

    assert registry.validate_edge(
        iteration,
        end,
        EdgeSpec(
            id="valid",
            source="iteration",
            target="end",
            source_port="items",
            target_port="input",
        ),
    ) == []


def test_add_edge_rejects_bad_port_before_persisting_it(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-edge-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=_headers(),
            json={"name": "Named ports", "requirement": "Check named workflow ports."},
        ).json()["id"]
        llm = _mutate(
            client,
            application_id,
            0,
            "add_node",
            {
                "node": {
                    "id": "llm",
                    "type": "llm",
                    "title": "LLM",
                    "config": {"prompt": "hello"},
                }
            },
        )
        assert llm.status_code == 200, llm.text
        end = _mutate(
            client,
            application_id,
            1,
            "add_node",
            {
                "node": {
                    "id": "end",
                    "type": "end",
                    "title": "End",
                    "config": {"outputs": {}},
                }
            },
        )
        assert end.status_code == 200, end.text

        invalid = _mutate(
            client,
            application_id,
            2,
            "add_edge",
            {
                "edge": {
                    "id": "llm-end",
                    "source": "llm",
                    "target": "end",
                    "source_port": "bogus",
                    "target_port": "input",
                }
            },
        )
        assert invalid.status_code == 422
        assert "unknown source port llm.bogus" in invalid.text
        unchanged = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=_headers(),
        ).json()
        assert unchanged["revision"] == 2
        assert unchanged["snapshot"]["workflow"]["edges"] == []

        valid = _mutate(
            client,
            application_id,
            2,
            "add_edge",
            {
                "edge": {
                    "id": "llm-end",
                    "source": "llm",
                    "target": "end",
                    "source_port": "text",
                    "target_port": "input",
                }
            },
        )
        assert valid.status_code == 200, valid.text
        assert valid.json()["revision"] == 3
