from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings


def future(days: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def load_evidence_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_139_e10_studio_governed_memory_operator_ui.py"
    spec = importlib.util.spec_from_file_location("v02_139_e10_memory_ui_evidence_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def permission(app_id: str, operations: list[str]) -> dict[str, object]:
    return {
        "actor_id": "studio-operator",
        "owner_id": app_id,
        "scope_id": "project-alpha",
        "purpose": "studio governed memory operator",
        "allowed_operations": operations,
    }


def source(source_id: str, content: str) -> dict[str, str]:
    return {
        "source_type": "operator_note",
        "source_id": source_id,
        "evidence_text": content,
    }


def create_memory(
    client: TestClient,
    headers: dict[str, str],
    app_id: str,
    source_id: str,
    content: str,
    *,
    expires_at: str | None = None,
) -> str:
    response = client.post(
        "/api/v1/platform/governed-memory",
        headers=headers,
        json={
            "permission": permission(app_id, ["create"]),
            "content": content,
            "source": source(source_id, content),
            "retention_class": "project",
            "expires_at": expires_at or future(days=10),
            "reason": "studio operator creates scoped memory",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_v02_139_api_lists_status_filtered_governed_memory_for_operator(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        api_token="test-token",
    )
    settings.prepare()
    app = create_app(settings)
    headers = {"Authorization": "Bearer test-token"}

    with TestClient(app) as client:
        created_app = client.post(
            "/api/v1/applications",
            headers=headers,
            json={"name": "v0.2.139 memory UI", "requirement": "Exercise governed memory operator UI."},
        )
        assert created_app.status_code == 201, created_app.text
        app_id = created_app.json()["id"]

        active_id = create_memory(client, headers, app_id, "active-note", "Active scoped memory.")
        revoked_id = create_memory(client, headers, app_id, "revoked-note", "Revoked scoped memory.")
        expiring_id = create_memory(
            client,
            headers,
            app_id,
            "expired-note",
            "Expired scoped memory.",
            expires_at=future(days=1),
        )

        revoke = client.post(
            f"/api/v1/platform/governed-memory/{revoked_id}/revoke",
            headers=headers,
            json={
                "permission": permission(app_id, ["revoke"]),
                "reason": "studio operator revoked memory",
            },
        )
        assert revoke.status_code == 200, revoke.text

        expire = client.post(
            "/api/v1/platform/governed-memory/expire",
            headers=headers,
            json={
                "permission": permission(app_id, ["expire"]),
                "reason": "studio operator expired due memory",
                "now": future(days=2),
            },
        )
        assert expire.status_code == 200, expire.text

        def listed(status_filter: str) -> list[dict[str, object]]:
            response = client.get(
                "/api/v1/platform/governed-memory",
                headers=headers,
                params={
                    "owner_id": app_id,
                    "scope_id": "project-alpha",
                    "actor_id": "studio-operator",
                    "purpose": "studio governed memory operator",
                    "reason": "studio operator inspects governed memory",
                    "status_filter": status_filter,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        active = listed("active")
        revoked = listed("revoked")
        expired = listed("expired")
        all_items = listed("all")

        assert [item["id"] for item in active] == [active_id]
        assert [item["id"] for item in revoked] == [revoked_id]
        assert [item["id"] for item in expired] == [expiring_id]
        assert {item["id"] for item in all_items} == {active_id, revoked_id, expiring_id}


def test_v02_139_evidence_script_reports_completed_operator_ui() -> None:
    module = load_evidence_module()
    evidence = module.build_evidence()

    assert evidence["status"] == "completed"
    assert evidence["checks"]["api_status_filtered_listing"] is True
    assert evidence["checks"]["ui_create_view_revoke_controls_present"] is True
    assert evidence["checks"]["audit_stream_visible"] is True
    assert evidence["checks"]["e02_external_blocker_preserved"] is True
    assert evidence["checks"]["global_completion_boundary_preserved"] is True
