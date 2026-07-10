from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.governed_memory import (
    GovernedMemoryPermission,
    GovernedMemorySource,
    GovernedMemorySurface,
    GovernedMemoryViolation,
)
from agent_platform.storage import Storage


def run(coro):
    return asyncio.run(coro)


def initialized_storage(path: Path) -> Storage:
    storage = Storage(path)
    run(storage.initialize())
    return storage


def load_evidence_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_137_e10_governed_memory_surface_contract.py"
    spec = importlib.util.spec_from_file_location("v02_137_e10_memory_evidence_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def permission(*, operations: list[str] | None = None) -> GovernedMemoryPermission:
    return GovernedMemoryPermission(
        actor_id="operator-a",
        owner_id="owner-a",
        scope_id="project-alpha",
        purpose="governed project memory",
        allowed_operations=operations or ["create", "read", "update", "revoke", "expire"],
    )


def source(source_type: str = "operator_note", source_id: str = "note-1") -> GovernedMemorySource:
    return GovernedMemorySource(
        source_type=source_type,
        source_id=source_id,
        evidence_text="User approved retaining the deployment preference.",
    )


def future(days: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def past(days: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_v02_137_surface_contract_create_read_update_revoke_and_audit(tmp_path: Path) -> None:
    storage = initialized_storage(tmp_path / "storage")
    surface = GovernedMemorySurface(storage)
    scope = permission()

    item = run(
        surface.create(
            permission=scope,
            content="Prefer bounded paid model validation before claiming Builder quality.",
            source=source(),
            retention_class="project",
            expires_at=future(),
            reason="capture explicit operator preference",
        )
    )
    read = run(surface.read(item.id, permission=scope, reason="retrieve for next planning step"))
    updated = run(
        surface.update(
            item.id,
            permission=scope,
            content="Prefer bounded paid model validation with provider and budget evidence.",
            source=source(source_id="note-2"),
            reason="refine retained preference",
        )
    )
    revoked = run(surface.revoke(item.id, permission=scope, reason="operator revoked this memory"))

    assert read.content.startswith("Prefer bounded")
    assert updated.source.evidence_hash
    assert revoked.status == "revoked"
    with pytest.raises(GovernedMemoryViolation, match="revoked"):
        run(surface.read(item.id, permission=scope, reason="should not retrieve revoked memory"))

    events = run(storage.list_events(GovernedMemorySurface.audit_stream_id("owner-a", "project-alpha")))
    operations = [event.data["operation"] for event in events]
    assert operations == ["create", "read", "update", "revoke"]
    for event in events:
        assert event.data["actor_id"] == "operator-a"
        assert event.data["source"]["source_id"]
        assert event.data["reason"]
        assert event.data["timestamp"]


def test_v02_137_surface_contract_rejects_unscoped_and_filesystem_memory(tmp_path: Path) -> None:
    storage = initialized_storage(tmp_path / "storage")
    surface = GovernedMemorySurface(storage)

    with pytest.raises(GovernedMemoryViolation, match="filesystem"):
        run(
            surface.create(
                permission=permission(),
                content="Index everything under the repo.",
                source=source(source_type="filesystem", source_id="/Users/example/repo"),
                retention_class="project",
                expires_at=future(),
                reason="unsafe background memory",
            )
        )

    create_only = permission(operations=["create"])
    item = run(
        surface.create(
            permission=create_only,
            content="Scoped note.",
            source=source(source_id="note-3"),
            retention_class="session",
            expires_at=future(),
            reason="create scoped note",
        )
    )
    with pytest.raises(GovernedMemoryViolation, match="does not allow"):
        run(surface.read(item.id, permission=create_only, reason="read without permission"))


def test_v02_137_surface_contract_expires_due_records(tmp_path: Path) -> None:
    storage = initialized_storage(tmp_path / "storage")
    surface = GovernedMemorySurface(storage)
    scope = permission()

    item = run(
        surface.create(
            permission=scope,
            content="Temporary project note.",
            source=source(source_id="note-expiring"),
            retention_class="session",
            expires_at=future(days=1),
            reason="temporary capture",
        )
    )
    expired = run(surface.expire_due(owner_id="owner-a", permission=scope, reason="retention sweep", now=future(days=2)))

    assert [entry.id for entry in expired] == [item.id]
    with pytest.raises(GovernedMemoryViolation, match="expired"):
        run(surface.read(item.id, permission=scope, reason="expired read"))

    events = run(storage.list_events(GovernedMemorySurface.audit_stream_id("owner-a", "project-alpha")))
    assert events[-1].data["operation"] == "expire"


def test_v02_137_api_exposes_governed_memory_surface(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        api_token="test-token",
    )
    settings.prepare()
    app = create_app(settings)
    headers = {"Authorization": "Bearer test-token"}
    payload = {
        "permission": permission().model_dump(mode="json"),
        "content": "Remember only this scoped project preference.",
        "source": source().model_dump(mode="json"),
        "retention_class": "project",
        "expires_at": future(),
        "reason": "operator explicitly allowed scoped retention",
    }

    with TestClient(app) as client:
        created = client.post("/api/v1/platform/governed-memory", headers=headers, json=payload)
        assert created.status_code == 201
        memory_id = created.json()["id"]

        read = client.post(
            f"/api/v1/platform/governed-memory/{memory_id}/read",
            headers=headers,
            json={
                "permission": permission(operations=["read"]).model_dump(mode="json"),
                "reason": "read through product API",
            },
        )
        assert read.status_code == 200
        assert read.json()["content"] == payload["content"]

        rejected = client.post(
            "/api/v1/platform/governed-memory",
            headers=headers,
            json={
                **payload,
                "source": source(source_type="filesystem_index", source_id="/tmp").model_dump(mode="json"),
                "reason": "unsafe filesystem memory",
            },
        )
        assert rejected.status_code == 422


def test_v02_137_evidence_script_reports_completed_contract() -> None:
    module = load_evidence_module()
    evidence = module.build_evidence()

    assert evidence["status"] == "completed"
    assert evidence["checks"]["permission_scoped_create_read_update_revoke"] is True
    assert evidence["checks"]["audit_log_records_required_fields"] is True
    assert evidence["checks"]["revoke_excludes_retrieval"] is True
    assert evidence["checks"]["expire_marks_due_records"] is True
    assert evidence["checks"]["unrestricted_filesystem_memory_rejected"] is True
    assert evidence["checks"]["e02_external_blocker_preserved"] is True
    assert evidence["checks"]["global_completion_boundary_preserved"] is True
    assert evidence["boundaries"]["runtime_memory_retrieval_claimed"] is False
