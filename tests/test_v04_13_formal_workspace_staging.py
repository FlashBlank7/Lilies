from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_platform.lilies_api import create_lilies_app
from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_models import (
    FormalWorkspaceBundle,
    FormalWorkspaceFileEntry,
    FormalWorkspaceStagingReceipt,
    FormalWorkspaceStagingRequest,
    formal_assignment_digest,
)
from agent_platform.lilies_service import build_local_lilies_core
from agent_platform.lilies_storage import LiliesAccessDeniedError
from tests.test_v04_13_formal_assignment_gate import (
    _formal_assignment,
    _provision_formal_credentials,
)
from tests.test_v04_13_lilies_assignment_intake import (
    BlockingProvider,
    paired_client,
    platform_session,
)


WRITE = "lilies.session:write"


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _bundle_digest(entries: list[dict[str, Any]]) -> str:
    projection = {
        "schema_version": "1.0",
        "entries": [
            {
                "path": entry["path"],
                "kind": entry.get("kind", "file"),
                "digest": entry["digest"],
                "size_bytes": entry["size_bytes"],
            }
            for entry in entries
        ],
    }
    payload = json.dumps(
        projection,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return _digest(payload)


def _staging_request(
    workspace: Path,
    assignment: Any,
) -> FormalWorkspaceStagingRequest:
    entries: list[FormalWorkspaceFileEntry] = []
    for path in sorted(item for item in workspace.rglob("*") if item.is_file()):
        payload = path.read_bytes()
        entries.append(
            FormalWorkspaceFileEntry(
                path=path.relative_to(workspace).as_posix(),
                digest=_digest(payload),
                size_bytes=len(payload),
                content_base64=base64.b64encode(payload).decode("ascii"),
            )
        )
    bundle = FormalWorkspaceBundle(
        entries=entries,
        bundle_digest=FormalWorkspaceBundle.digest_entries(entries),
    )
    task_ref = assignment.task_package
    assert task_ref is not None
    assert task_ref.workspace_mount_digest is not None
    assert task_ref.workspace_policy_digest is not None
    return FormalWorkspaceStagingRequest(
        idempotency_key="formal-workspace-stage-000001",
        assignment_id=assignment.assignment_id,
        assignment_digest=formal_assignment_digest(assignment),
        task_package_digest=task_ref.public_summary_digest,
        workspace_mount_digest=task_ref.workspace_mount_digest,
        workspace_policy_digest=task_ref.workspace_policy_digest,
        bundle=bundle,
    )


def _pair(client: TestClient) -> dict[str, Any]:
    code = client.post(
        "/local/v1/pairings/code",
        json={"allowed_scopes": [WRITE], "ttl_seconds": 600},
    ).json()
    response = client.post(
        "/local/v1/pairings/exchange",
        json={
            "pairing_code": code["pairing_code"],
            "client_name": "formal-workspace-stager",
            "requested_scopes": [WRITE],
            "client_nonce": secrets.token_urlsafe(24),
        },
    )
    assert response.status_code == 200
    return response.json()


def _platform_session(client: TestClient, token: str, key: str) -> str:
    response = client.post(
        "/local/v1/sessions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "idempotency_key": key,
            "kind": "platform",
            "title": "Formal workspace staging",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def test_loopback_endpoint_stages_exact_bundle_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    _, _, _, source_workspace, assignment = _formal_assignment(
        tmp_path / "formal-source"
    )
    request = _staging_request(source_workspace, assignment)
    settings = LiliesSettings(
        data_dir=tmp_path / "lilies",
        workspace_root=tmp_path / "daemon-workspaces",
        model="test",
    )
    with TestClient(create_lilies_app(settings)) as client:
        pairing = _pair(client)
        token = pairing["access_token"]
        session_id = _platform_session(
            client,
            token,
            "formal-stage-session-000001",
        )
        path = f"/local/v1/sessions/{session_id}/formal-workspace"
        payload = request.model_dump(mode="json")

        assert client.post(path, json=payload).status_code == 401
        response = client.post(
            path,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

        assert response.status_code == 201, response.text
        receipt = FormalWorkspaceStagingReceipt.model_validate(response.json())
        assert receipt.assignment_id == assignment.assignment_id
        assert receipt.assignment_digest == formal_assignment_digest(assignment)
        assert receipt.bundle_digest == request.bundle.bundle_digest
        assert receipt.replayed is False
        assert str(source_workspace) not in json.dumps(payload)
        target = settings.resolved_workspace_root / session_id
        assert {
            item.relative_to(target).as_posix(): item.read_bytes()
            for item in target.rglob("*")
            if item.is_file()
        } == {
            item.relative_to(source_workspace).as_posix(): item.read_bytes()
            for item in source_workspace.rglob("*")
            if item.is_file()
        }
        assert (target / "work").is_dir()
        assert (target / "artifacts").is_dir()
        assert not list(
            settings.resolved_workspace_root.glob(
                f".{session_id}.formal-stage-*"
            )
        )

        replay = client.post(
            path,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        assert replay.status_code == 201
        assert replay.json() == {
            **response.json(),
            "replayed": True,
        }

        conflict = copy.deepcopy(payload)
        conflict["assignment_digest"] = "sha256:" + "f" * 64
        rejected = client.post(
            path,
            headers={"Authorization": f"Bearer {token}"},
            json=conflict,
        )
        assert rejected.status_code == 409


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("path", "/etc/passwd"),
        ("path", "../oracle.json"),
        ("path", ".git/config"),
        ("path", "platform-data/database.sqlite"),
        ("path", "protected/oracle.json"),
        ("path", "expected-state/result.json"),
        ("kind", "symlink"),
        ("kind", "hardlink"),
        ("kind", "device"),
        ("source_path", "/manager/host/workspace/task.yaml"),
    ],
)
def test_staging_request_rejects_host_paths_and_special_entries(
    tmp_path: Path,
    mutation: str,
    value: str,
) -> None:
    _, _, _, workspace, assignment = _formal_assignment(
        tmp_path / f"attack-{mutation}-{value.replace('/', '-')}"
    )
    payload = _staging_request(workspace, assignment).model_dump(mode="json")
    entry = payload["bundle"]["entries"][2]
    entry[mutation] = value
    payload["bundle"]["bundle_digest"] = _bundle_digest(
        payload["bundle"]["entries"]
    )

    with pytest.raises(ValueError):
        FormalWorkspaceStagingRequest.model_validate(payload)


def test_staging_rejects_case_collisions_and_nonempty_workspace(
    tmp_path: Path,
) -> None:
    _, _, _, source_workspace, assignment = _formal_assignment(
        tmp_path / "collision-source"
    )
    request = _staging_request(source_workspace, assignment)
    payload = request.model_dump(mode="json")
    duplicate = copy.deepcopy(payload["bundle"]["entries"][2])
    duplicate["path"] = duplicate["path"].upper()
    payload["bundle"]["entries"].append(duplicate)
    payload["bundle"]["entries"].sort(key=lambda item: item["path"])
    payload["bundle"]["bundle_digest"] = _bundle_digest(
        payload["bundle"]["entries"]
    )
    with pytest.raises(ValueError, match="collide"):
        FormalWorkspaceStagingRequest.model_validate(payload)

    settings = LiliesSettings(
        data_dir=tmp_path / "lilies",
        workspace_root=tmp_path / "daemon-workspaces",
        model="test",
    )
    with TestClient(create_lilies_app(settings)) as client:
        pairing = _pair(client)
        token = pairing["access_token"]
        session_id = _platform_session(
            client,
            token,
            "formal-stage-session-000002",
        )
        workspace = settings.resolved_workspace_root / session_id
        (workspace / "preexisting.txt").write_text("must not be replaced")

        response = client.post(
            f"/local/v1/sessions/{session_id}/formal-workspace",
            headers={"Authorization": f"Bearer {token}"},
            json=request.model_dump(mode="json"),
        )

        assert response.status_code == 409
        assert (workspace / "preexisting.txt").read_text() == "must not be replaced"


@pytest.mark.asyncio
async def test_staged_assignment_is_bound_and_workspace_is_revalidated(
    tmp_path: Path,
) -> None:
    _, _, _, source_workspace, assignment = _formal_assignment(
        tmp_path / "assignment-source"
    )
    provider = BlockingProvider()
    settings = LiliesSettings(
        data_dir=tmp_path / "lilies",
        workspace_root=tmp_path / "daemon-workspaces",
        model="test",
    )
    service = build_local_lilies_core(settings, provider=provider).service
    await service.initialize()
    client = await paired_client(service.storage, service.settings)
    client_id = str(client["client_id"])
    session_id = await platform_session(service, client_id)
    await _provision_formal_credentials(service, client_id, assignment)
    request = _staging_request(source_workspace, assignment)
    receipt = await service.stage_formal_workspace(
        session_id,
        request,
        client_id=client_id,
    )
    assert receipt["assignment_digest"] == formal_assignment_digest(assignment)

    workspace = settings.resolved_workspace_root / session_id
    requirement = workspace / "requirement.md"
    requirement.chmod(0o600)
    requirement.write_text("tampered after the staging receipt", encoding="utf-8")
    requirement.chmod(0o400)
    try:
        with pytest.raises(
            LiliesAccessDeniedError,
            match="frozen task-package authorization",
        ):
            await service.submit_assignment(
                session_id,
                assignment,
                client_id=client_id,
            )
        assert provider.calls == 0
        persisted = await service.storage.get_session(session_id)
        assert persisted["assignment_id"] is None
    finally:
        await service.shutdown(reason="formal_workspace_staging_test")
