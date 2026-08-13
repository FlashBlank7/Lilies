"""Tests for the file_read block: workflow reads a real CSV/JSON/text file from the workspace."""

from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings

H = {"Authorization": "Bearer workflow-test"}


def _run_file_read(tmp_path: Path, filename: str, content: bytes | None, file_format: str) -> dict:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "ws",
    )
    settings.prepare()
    app = create_app(settings)
    with TestClient(app) as client:
        app_id = client.post("/api/v1/applications", headers=H, json={
            "name": "file_read_test", "requirement": "read file",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app_id}/draft", headers=H).json()["revision"]

        def mutate(op: str, data: dict) -> None:
            nonlocal rev
            r = client.post(f"/api/v1/applications/{app_id}/draft", headers=H, json={
                "expected_revision": rev, "idempotency_key": f"fr-{op}-{rev}", "op": op, "data": data,
            })
            assert r.status_code == 200, r.text
            rev = r.json()["revision"]

        end_outputs = {
            "records": {"$ref": {"node_id": "read", "path": ["records"]}},
            "row_count": {"$ref": {"node_id": "read", "path": ["row_count"]}},
            "format": {"$ref": {"node_id": "read", "path": ["format"]}},
            "sha256": {"$ref": {"node_id": "read", "path": ["sha256"]}},
        }
        for node in [
            {"id": "start", "type": "start", "title": "s", "config": {"inputs": []}},
            {"id": "read", "type": "file_read", "title": "read", "config": {
                "path": filename, "format": file_format}},
            {"id": "end", "type": "end", "title": "e", "config": {"outputs": end_outputs}},
        ]:
            mutate("add_node", {"node": node})
        for edge in [
            {"id": "a", "source": "start", "target": "read", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "read", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            mutate("add_edge", {"edge": edge})

        ws = tmp_path / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        if content is not None:
            (ws / filename).write_bytes(content)

        created = client.post(f"/api/v1/applications/{app_id}/runs", headers=H, json={
            "inputs": {}, "use_draft": True, "workspace_path": str(ws),
        })
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}", headers=H).json()
            if run["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.1)
        return run


def test_file_read_reads_real_csv_with_type_coercion(tmp_path: Path) -> None:
    csv_content = (
        b"order_id,customer,product,units,unit_price\n"
        b"ORD-001,Acme,Widget A,10,12.5\n"
        b"ORD-002,Beta,Widget B,3,9.0\n"
    )
    run = _run_file_read(tmp_path, "sales.csv", csv_content, "csv")
    assert run["status"] == "succeeded", run.get("error")
    out = run["outputs"]
    assert out["row_count"] == 2
    assert out["format"] == "csv"
    assert len(out["sha256"]) == 64
    records = out["records"]
    assert records[0]["order_id"] == "ORD-001"
    assert records[0]["units"] == 10  # coerced to int
    assert records[0]["unit_price"] == 12.5  # coerced to float


def test_file_read_reads_json(tmp_path: Path) -> None:
    run = _run_file_read(tmp_path, "data.json", b'[{"a":1},{"a":2}]', "json")
    assert run["status"] == "succeeded", run.get("error")
    assert run["outputs"]["row_count"] == 2
    assert run["outputs"]["records"] == [{"a": 1}, {"a": 2}]


def test_file_read_auto_detects_format(tmp_path: Path) -> None:
    run = _run_file_read(tmp_path, "data.json", b'[{"a":1},{"a":2}]', "auto")
    assert run["status"] == "succeeded", run.get("error")
    assert run["outputs"]["format"] == "json"
    assert run["outputs"]["records"] == [{"a": 1}, {"a": 2}]


def test_file_read_missing_file_fails(tmp_path: Path) -> None:
    run = _run_file_read(tmp_path, "absent.csv", None, "csv")
    assert run["status"] == "failed"
    assert "file not found" in (run.get("error") or "")


def test_file_read_path_escape_rejected(tmp_path: Path) -> None:
    run = _run_file_read(tmp_path, "../../etc/passwd", None, "text")
    assert run["status"] == "failed"
    assert "escapes workspace" in (run.get("error") or "")
