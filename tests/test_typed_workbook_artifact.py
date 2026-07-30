from __future__ import annotations

import base64
import hashlib
import zipfile
from functools import partial
from io import BytesIO
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_platform.api import create_app
from agent_platform.blocks import build_block_registry
from agent_platform.typed_workbook import (
    XLSX_MEDIA_TYPE,
    TypedWorkbookConfig,
    render_typed_workbook,
    write_typed_workbook_artifact,
)
from agent_platform.workflow_models import DraftOperation
from tests.test_runtime import ScriptedProvider
from tests.test_v04_13_lilies_platform_api import _issue, _request, _settings


def _neutral_spec(*, text: str = "within range") -> dict:
    return {
        "sheets": [
            {
                "name": "Observations",
                "columns": [
                    {"key": "record_id", "header": "Record ID", "type": "string"},
                    {
                        "key": "observed_on",
                        "header": "Observed On",
                        "type": "date",
                    },
                    {
                        "key": "observed_at",
                        "header": "Observed At",
                        "type": "datetime",
                    },
                    {"key": "value", "header": "Value", "type": "number"},
                    {"key": "accepted", "header": "Accepted", "type": "boolean"},
                    {"key": "note", "header": "Note", "type": "string"},
                ],
                "rows": [
                    {
                        "record_id": "R-001",
                        "observed_on": "2026-01-15",
                        "observed_at": "2026-01-15T09:30:00+09:00",
                        "value": 12.5,
                        "accepted": True,
                        "note": text,
                    }
                ],
            }
        ]
    }


def _create_application(client: TestClient) -> str:
    response = client.post(
        "/api/v1/applications",
        headers={"Authorization": "Bearer internal-test-token"},
        json={
            "name": "Typed artifact integration",
            "requirement": "Produce one bounded typed workbook artifact.",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _apply_operations(
    client: TestClient,
    application_id: str,
    operations: list[tuple[str, dict]],
) -> None:
    revision = 0
    for index, (operation, data) in enumerate(operations):
        result = client.portal.call(
            partial(
                client.app.state.services.applications.apply_operation,
                application_id,
                DraftOperation(
                    expected_revision=revision,
                    idempotency_key=f"typed-workbook-draft-{index:04d}",
                    op=operation,
                    data=data,
                ),
            )
        )
        revision = int(result["revision"])


def test_typed_workbook_renderer_is_deterministic_typed_and_formula_free() -> None:
    spec = _neutral_spec(text="=must remain text")
    first = render_typed_workbook(spec, formula_policy="literal")
    second = render_typed_workbook(spec, formula_policy="literal")
    assert first == second
    assert first.startswith(b"PK")

    with zipfile.ZipFile(BytesIO(first)) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == [
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/styles.xml",
            "xl/worksheets/sheet1.xml",
        ]
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                ElementTree.fromstring(archive.read(name))
        worksheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "<f" not in worksheet
        assert "=must remain text" in worksheet
        assert 't="inlineStr"' in worksheet
        assert 't="b"><v>1</v>' in worksheet
        assert 's="1"><v>' in worksheet
        assert 's="2"><v>' in worksheet


def test_typed_workbook_rejects_formula_injection_and_invalid_typed_rows() -> None:
    with pytest.raises(ValueError, match="looks like a spreadsheet formula"):
        render_typed_workbook(_neutral_spec(text="@unsafe"))

    invalid = _neutral_spec()
    invalid["sheets"][0]["rows"][0]["accepted"] = "yes"
    with pytest.raises(TypeError, match="must be a boolean"):
        render_typed_workbook(invalid)

    unknown = _neutral_spec()
    unknown["sheets"][0]["rows"][0]["undeclared"] = "value"
    with pytest.raises(ValidationError, match="undeclared columns"):
        render_typed_workbook(unknown)


def test_typed_workbook_path_safety_and_exact_replay(tmp_path: Path) -> None:
    workspace = tmp_path / "run"
    workspace.mkdir()
    lineage = [
        {
            "source_type": "workflow_input",
            "reference": "observations",
            "sha256": f"sha256:{'0' * 64}",
        }
    ]
    first = write_typed_workbook_artifact(
        workspace=workspace,
        spec=_neutral_spec(),
        filename="bounded-output.xlsx",
        formula_policy="reject",
        lineage=lineage,
        run_id="run-1",
        node_id="artifact-1",
        application_id="application-1",
    )
    replay = write_typed_workbook_artifact(
        workspace=workspace,
        spec=_neutral_spec(),
        filename="bounded-output.xlsx",
        formula_policy="reject",
        lineage=lineage,
        run_id="run-1",
        node_id="artifact-1",
        application_id="application-1",
    )
    assert first["replayed"] is False
    assert replay == {**first, "replayed": True}
    target = workspace / "artifacts" / "bounded-output.xlsx"
    assert first["sha256"] == f"sha256:{hashlib.sha256(target.read_bytes()).hexdigest()}"
    assert first["media_type"] == XLSX_MEDIA_TYPE
    assert first["lineage"]["workbook_spec_sha256"].startswith("sha256:")

    changed = _neutral_spec(text="different")
    with pytest.raises(FileExistsError, match="different content"):
        write_typed_workbook_artifact(
            workspace=workspace,
            spec=changed,
            filename="bounded-output.xlsx",
            formula_policy="reject",
            lineage=[],
            run_id="run-1",
            node_id="artifact-1",
            application_id="application-1",
        )

    with pytest.raises(ValidationError, match="plain ASCII .xlsx basename"):
        TypedWorkbookConfig.model_validate(
            {
                "spec": _neutral_spec(),
                "filename": "../escape.xlsx",
            }
        )

    unsafe_workspace = tmp_path / "unsafe"
    unsafe_workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (unsafe_workspace / "artifacts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="cannot be a symbolic link"):
        write_typed_workbook_artifact(
            workspace=unsafe_workspace,
            spec=_neutral_spec(),
            filename="safe-name.xlsx",
            formula_policy="reject",
            lineage=[],
            run_id="run-2",
            node_id="artifact-2",
            application_id="application-2",
        )
    assert list(outside.iterdir()) == []


def test_public_block_run_registers_and_reads_real_xlsx(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_application(client)
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        _apply_operations(
            client,
            application_id,
            [
                (
                    "add_node",
                    {
                        "node": {
                            "id": "start",
                            "type": "start",
                            "title": "Input",
                            "config": {
                                "inputs": [
                                    {
                                        "name": "workbook",
                                        "type": "object",
                                    }
                                ]
                            },
                        }
                    },
                ),
                (
                    "add_node",
                    {
                        "node": {
                            "id": "workbook",
                            "type": "typed_workbook",
                            "title": "Create workbook",
                            "config": {
                                "spec": {
                                    "$ref": {
                                        "node_id": "start",
                                        "path": ["workbook"],
                                    }
                                },
                                "filename": "neutral-output.xlsx",
                                "formula_policy": "reject",
                                "lineage": [
                                    {
                                        "source_type": "workflow_input",
                                        "reference": "workbook",
                                    }
                                ],
                            },
                        }
                    },
                ),
                (
                    "add_node",
                    {
                        "node": {
                            "id": "end",
                            "type": "end",
                            "title": "End",
                            "config": {
                                "outputs": {
                                    "artifact": {
                                        "$ref": {
                                            "node_id": "workbook",
                                            "path": ["artifact"],
                                        }
                                    }
                                }
                            },
                        }
                    },
                ),
                (
                    "add_edge",
                    {
                        "edge": {
                            "id": "start-workbook",
                            "source": "start",
                            "target": "workbook",
                            "source_port": "output",
                            "target_port": "input",
                        }
                    },
                ),
                (
                    "add_edge",
                    {
                        "edge": {
                            "id": "workbook-end",
                            "source": "workbook",
                            "target": "end",
                            "source_port": "output",
                            "target_port": "input",
                        }
                    },
                ),
            ],
        )

        manual = _request(
            client,
            "GET",
            "/api/v1/lilies/blocks/typed_workbook",
            headers,
            key="typed-workbook-manual-0001",
        )
        assert manual.status_code == 200, manual.text
        assert manual.json()["data"]["definition"]["type"] == "typed_workbook"
        assert "spec" in manual.json()["data"]["manual"]["config_schema"]["properties"]

        started = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key="typed-workbook-run-0001",
            json={"inputs": {"workbook": _neutral_spec()}, "use_draft": True},
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["data"]["run_id"]
        for index in range(100):
            result = _request(
                client,
                "GET",
                f"/api/v1/lilies/runs/{run_id}",
                headers,
                key=f"typed-workbook-run-poll-{index:04d}",
            )
            assert result.status_code == 200, result.text
            if result.json()["data"]["status"] in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                break
        data = result.json()["data"]
        assert data["status"] == "succeeded", result.text
        descriptor = data["outputs"]["artifact"]
        assert descriptor["media_type"] == XLSX_MEDIA_TYPE
        assert descriptor["lineage"]["generator"] == {
            "block_type": "typed_workbook",
            "block_version": 1,
        }
        assert descriptor["lineage"]["sources"] == [
            {
                "source_type": "workflow_input",
                "reference": "workbook",
                "sha256": None,
            }
        ]
        assert len(data["artifacts"]) == 1
        registered = data["artifacts"][0]
        assert registered["relative_path"] == "artifacts/neutral-output.xlsx"
        assert registered["media_type"] == XLSX_MEDIA_TYPE
        assert registered["sha256"] == descriptor["sha256"]

        downloaded = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/{registered['artifact_id']}",
            headers,
            key="typed-workbook-artifact-read-0001",
        )
        assert downloaded.status_code == 200, downloaded.text
        artifact = downloaded.json()["data"]
        assert artifact["encoding"] == "base64"
        payload = base64.b64decode(artifact["content"], validate=True)
        assert payload.startswith(b"PK")
        assert artifact["sha256"] == descriptor["sha256"]
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            assert archive.testzip() is None


def test_registry_exposes_generic_typed_workbook_without_host_catalog() -> None:
    registry = build_block_registry()
    definition = registry.get("typed_workbook")
    assert definition.block_kind == "business_workflow"
    assert definition.category == "output"
    manual = registry.manual("typed_workbook")
    serialized = str(manual).casefold()
    assert "xlsx" in serialized
    assert "formula" in serialized
    assert "connector_id" not in serialized
