from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.workflow_storage import RevisionConflict
from tests.test_runtime import ScriptedProvider


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def _create(
    client: TestClient,
    *,
    mode: str = "guided",
    hard_gate: bool = False,
) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=_headers(),
        json={
            "name": f"{mode} lifecycle",
            "requirement": "Keep acceptance evidence visible across draft edits.",
            "delivery_mode": mode,
            "governed_hard_gate": hard_gate,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _mutate(client: TestClient, application_id: str, revision: int, data: dict) -> None:
    response = client.post(
        f"/api/v1/applications/{application_id}/draft",
        headers=_headers(),
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": "set_metadata",
            "data": data,
        },
    )
    assert response.status_code == 200, response.text


def test_quick_and_guided_missing_evidence_require_explicit_acknowledgement(
    tmp_path: Path,
) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        for mode in ("quick", "guided"):
            application_id = _create(client, mode=mode)
            decision = client.get(
                f"/api/v1/applications/{application_id}/publication-decision",
                headers=_headers(),
            ).json()
            assert decision["allowed"] is True
            assert decision["blocked"] is False
            assert decision["requires_confirmation"] is True
            assert decision["warning_codes"] == ["missing_evidence"]

            refused = client.post(
                f"/api/v1/applications/{application_id}/versions",
                headers=_headers(),
            )
            assert refused.status_code == 409
            refused_decision = refused.json()["detail"]["publication_decision"]
            assert refused_decision["requires_confirmation"] is True

            published = client.post(
                f"/api/v1/applications/{application_id}/versions",
                headers=_headers(),
                json={"acknowledge_warnings": True},
            )
            assert published.status_code == 200, published.text
            publication = published.json()["publication_decision"]
            assert publication["acknowledged_warnings"] is True
            assert publication["evidence_state"] == "missing"


def test_governed_hard_gate_blocks_but_advisory_governed_can_be_acknowledged(
    tmp_path: Path,
) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        advisory_id = _create(client, mode="governed", hard_gate=False)
        advisory = client.get(
            f"/api/v1/applications/{advisory_id}/publication-decision",
            headers=_headers(),
        ).json()
        assert advisory["requires_confirmation"] is True
        assert advisory["blocked"] is False
        allowed = client.post(
            f"/api/v1/applications/{advisory_id}/versions",
            headers=_headers(),
            json={"acknowledge_warnings": True},
        )
        assert allowed.status_code == 200, allowed.text

        blocked_id = _create(client, mode="governed", hard_gate=True)
        blocked = client.get(
            f"/api/v1/applications/{blocked_id}/publication-decision",
            headers=_headers(),
        ).json()
        assert blocked["blocked"] is True
        assert blocked["allowed"] is False
        assert blocked["policy"]["hard_gate_enabled"] is True
        rejected = client.post(
            f"/api/v1/applications/{blocked_id}/versions",
            headers=_headers(),
            json={"acknowledge_warnings": True},
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["publication_decision"]["blocked"] is True


def test_edit_preserves_prior_report_as_stale_and_revalidation_makes_it_current(
    tmp_path: Path,
) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create(client, mode="guided")
        draft = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=_headers()
        ).json()
        report = {"passed": True, "summary": {"total": 1, "passed": 1}}
        asyncio.run(app.state.services.workflow_store.mark_tested(
            application_id,
            draft["revision"],
            draft["content_hash"],
            report,
        ))
        current = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=_headers()
        ).json()
        assert current["evidence"]["state"] == "current"

        _mutate(client, application_id, current["revision"], {"description": "Behavior changed."})
        stale = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=_headers()
        ).json()
        assert stale["tested_hash"] == current["content_hash"]
        assert stale["validation_report"] == report
        assert stale["evidence"]["state"] == "stale"
        assert stale["evidence"]["invalidated_revision"] == stale["revision"]
        assert stale["evidence"]["change_summary"][-1]["operation"] == "set_metadata"
        assert stale["evidence"]["revalidate_endpoint"].endswith("/tests/run")

        stale_decision = client.get(
            f"/api/v1/applications/{application_id}/publication-decision",
            headers=_headers(),
        ).json()
        assert stale_decision["warning_codes"] == ["stale_evidence"]
        published = client.post(
            f"/api/v1/applications/{application_id}/versions",
            headers=_headers(),
            json={"acknowledge_warnings": True},
        )
        assert published.status_code == 200, published.text
        versions = client.get(
            f"/api/v1/applications/{application_id}/versions", headers=_headers()
        ).json()
        assert versions[0]["publication_decision"]["evidence_state"] == "stale"
        assert versions[0]["publication_decision"]["acknowledged_warnings"] is True

        asyncio.run(app.state.services.workflow_store.mark_tested(
            application_id,
            stale["revision"],
            stale["content_hash"],
            {"passed": True, "summary": {"total": 2, "passed": 2}},
        ))
        revalidated = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=_headers()
        ).json()
        assert revalidated["evidence"]["state"] == "current"
        assert revalidated["evidence"]["change_summary"] == []
        current_publish = client.post(
            f"/api/v1/applications/{application_id}/versions", headers=_headers()
        )
        assert current_publish.status_code == 200, current_publish.text


def test_restore_reinstates_the_published_evidence_state(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create(client, mode="quick")
        first = client.post(
            f"/api/v1/applications/{application_id}/versions",
            headers=_headers(),
            json={"acknowledge_warnings": True},
        )
        assert first.status_code == 200, first.text
        draft = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=_headers()
        ).json()
        asyncio.run(app.state.services.workflow_store.mark_tested(
            application_id,
            draft["revision"],
            draft["content_hash"],
            {"passed": True},
        ))
        _mutate(client, application_id, draft["revision"], {"description": "Temporary change"})

        restored = client.post(
            f"/api/v1/applications/{application_id}/versions/1/restore",
            headers=_headers(),
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["evidence_state"] == "missing"
        restored_draft = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=_headers()
        ).json()
        assert restored_draft["evidence"]["state"] == "missing"
        assert restored_draft["validation_report"] == {}


def test_concurrent_edit_rejects_late_test_evidence(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create(client)
        old = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=_headers()
        ).json()
        _mutate(client, application_id, old["revision"], {"description": "Concurrent edit"})
        with pytest.raises(RevisionConflict, match="draft changed while tests were running"):
            asyncio.run(app.state.services.workflow_store.mark_tested(
                application_id,
                old["revision"],
                old["content_hash"],
                {"passed": True},
            ))


def test_frontend_exposes_evidence_state_revalidation_and_publish_confirmation() -> None:
    root = Path(__file__).resolve().parents[1]
    studio = (root / "platform/frontend/app/applications/[id]/page.tsx").read_text()
    types = (root / "platform/frontend/lib/platform.ts").read_text()

    assert "data-evidence-state={evidenceState}" in studio
    assert "data-publication-decision=" in studio
    assert "data-draft-evidence={evidenceState}" in studio
    assert "acknowledge_warnings: acknowledgeWarnings" in studio
    assert "void runTests()" in studio
    assert "export type PublicationDecision" in types
    assert "last_validation_report" in types
    home = (root / "platform/frontend/app/page.tsx").read_text()
    assert "item.evidence?.state === 'current'" in home
    assert "item.evidence?.state === 'stale'" in home
