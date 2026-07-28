from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import httpx
from fastapi import FastAPI

from agent_platform.collaboration_api import install_collaboration_api
from agent_platform.formal_source_provenance import (
    DEVELOPER_SOURCE_MANIFEST_FILE,
    SOURCE_PROVENANCE_MANIFEST_PATH,
    ApprovedDeveloperResponseBinding,
    FormalSourceProvenanceConflict,
    FormalSourceProvenanceCoordinator,
    FormalSourceProvenanceError,
    FormalSourceProvenanceSecurityError,
    verify_source_provenance_archive_offline,
)
from agent_platform.collaboration_models import (
    ApprovalDecisionRequest,
    CollaborationReport,
    CollaborationReportPayload,
    DeveloperLease,
    DeveloperResponseRequest,
    DeveloperSourcePromotionRequest,
    LeaseAcquireRequest,
    LeaseReleaseRequest,
    ReportSubmitRequest,
    SenderRole,
)
from agent_platform.collaboration_service import (
    CollaborationConflict,
    CollaborationPrincipal,
    CollaborationService,
)
from agent_platform.formal_assignment_runtime import (
    PlatformFormalAssignmentRuntime,
)
from agent_platform.lilies_models import CollaborationScope
from tests.test_v04_13_collaboration_sqlite_integration import (
    _developer_response_payload,
    _report_payload,
    _store_with_channel,
)


NOW = datetime(2026, 7, 24, 4, 0, tzinfo=timezone.utc)


def _git(repository: Path, *arguments: str, input_payload: bytes | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        input=input_payload,
    )
    return completed.stdout.decode("utf-8").strip()


def _write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _setup_projection(
    tmp_path: Path,
    *,
    assignment_id: UUID | None = None,
    channel_id: UUID | None = None,
) -> dict[str, object]:
    repository = tmp_path / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Promotion Test")
    _git(repository, "config", "user.email", "promotion@example.test")
    _write(
        repository / "platform/backend/src/agent_platform/example.py",
        "VALUE = 'baseline'\n",
    )
    _write(
        repository
        / "platform/backend/src/agent_platform/independent_verifier.py",
        "TRUST_ROOT = 'manager-owned'\n",
    )
    _write(repository / "tests/test_example.py", "def test_baseline():\n    assert True\n")
    _write(repository / "scripts/check.sh", "#!/bin/sh\nexit 0\n")
    _write(repository / "pyproject.toml", "[project]\nname = 'promotion-test'\n")
    _write(repository / "platform/data/local.json", '{"private":true}\n')
    _write(repository / "docs/note.md", "tracked user document\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    baseline = _git(repository, "rev-parse", "HEAD")

    # This mirrors the real repository boundary: uv.lock is user-owned and
    # untracked at projection time, so it must not appear in the snapshot.
    _write(repository / "uv.lock", "user untracked lock\n")

    assignment_id = assignment_id or uuid4()
    channel_id = channel_id or uuid4()
    report_id = uuid4()
    lease_id = uuid4()
    response_id = uuid4()
    state_root = tmp_path / "source-state"
    coordinator = FormalSourceProvenanceCoordinator(
        repository_root=repository,
        state_root=state_root,
    )
    projection_root = tmp_path / "projection"
    projection = coordinator.freeze_workspace_projection(
        task_id="T01F",
        task_revision=1,
        run_id="formal-run:test",
        assignment_id=assignment_id,
        channel_id=channel_id,
        captured_at=NOW,
        destination=projection_root,
    )
    workspace = tmp_path / "workspace"
    shutil.copytree(projection_root, workspace / "source")
    mount_payload = b'{"schema_version":"test"}'
    (workspace / ".lilies-mount-manifest.json").write_bytes(mount_payload)
    return {
        "repository": repository,
        "state_root": state_root,
        "coordinator": coordinator,
        "projection": projection,
        "workspace": workspace,
        "mount_digest": _digest(mount_payload),
        "assignment_id": assignment_id,
        "channel_id": channel_id,
        "report_id": report_id,
        "lease_id": lease_id,
        "response_id": response_id,
        "baseline": baseline,
    }


def _promote(
    context: dict[str, object],
    *,
    report_revision: int = 4,
    lease_id: UUID | None = None,
    lease_owner_id: str = "codex:test",
    response_id: UUID | None = None,
    idempotency_key: str = "promote:test:1",
):
    coordinator = context["coordinator"]
    projection = context["projection"]
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    return coordinator.promote_workspace_delta(
        assignment_id=context["assignment_id"],
        channel_id=context["channel_id"],
        report_id=context["report_id"],
        report_revision=report_revision,
        lease_id=lease_id or context["lease_id"],
        lease_owner_id=lease_owner_id,
        response_id=response_id or context["response_id"],
        idempotency_key=idempotency_key,
        workspace=context["workspace"],
        workspace_manifest_digest=context["mount_digest"],
        source_manifest_digest=projection.manifest_digest,
        created_at=NOW,
    )


def _binding(
    context: dict[str, object],
    commit_sha: str,
    *,
    response_report_revision: int = 4,
) -> ApprovedDeveloperResponseBinding:
    return ApprovedDeveloperResponseBinding(
        channel_id=context["channel_id"],
        report_id=context["report_id"],
        approval_id=uuid4(),
        approval_message_id=uuid4(),
        approval_message_seq=1,
        approval_authority="user",
        approval_payload_digest="sha256:" + "a" * 64,
        approved_report_revision=3,
        response_id=context["response_id"],
        response_message_id=uuid4(),
        response_message_seq=2,
        response_report_revision=response_report_revision,
        response_payload_digest="sha256:" + "b" * 64,
        commit_sha=commit_sha,
    )


def _confirm_in_subprocess(
    context: dict[str, object],
    *,
    commit_sha: str,
    report_revision: int = 4,
) -> dict[str, object]:
    payload = {
        "repository": str(context["repository"]),
        "state_root": str(context["state_root"]),
        "assignment_id": str(context["assignment_id"]),
        "channel_id": str(context["channel_id"]),
        "report_id": str(context["report_id"]),
        "report_revision": report_revision,
        "response_id": str(context["response_id"]),
        "commit_sha": commit_sha,
    }
    environment = os.environ.copy()
    source_root = str(Path.cwd() / "platform/backend/src")
    environment["PYTHONPATH"] = (
        source_root
        if not environment.get("PYTHONPATH")
        else f"{source_root}{os.pathsep}{environment['PYTHONPATH']}"
    )
    script = """
import json
import sys
from pathlib import Path
from uuid import UUID
from agent_platform.formal_source_provenance import FormalSourceProvenanceCoordinator

p = json.loads(sys.argv[1])
c = FormalSourceProvenanceCoordinator(
    repository_root=Path(p["repository"]),
    state_root=Path(p["state_root"]),
)
effective = c.promoted_response_is_effective(
    assignment_id=UUID(p["assignment_id"]),
    channel_id=UUID(p["channel_id"]),
    report_id=UUID(p["report_id"]),
    report_revision=p["report_revision"],
    response_id=UUID(p["response_id"]),
    commit_sha=p["commit_sha"],
)
print(json.dumps({
    "effective": effective,
    "process_token": str(c._process_instance_id),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps(payload)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(completed.stdout)


def _promote_through_api_in_subprocess(
    context: dict[str, object],
    *,
    database: Path,
    report_id: UUID,
    current: datetime,
    request: DeveloperSourcePromotionRequest,
) -> dict[str, object]:
    payload = {
        "database": str(database),
        "repository": str(context["repository"]),
        "state_root": str(context["state_root"]),
        "workspace": str(context["workspace"]),
        "mount_digest": str(context["mount_digest"]),
        "source_manifest_digest": str(
            context["projection"].manifest_digest
        ),
        "report_id": str(report_id),
        "current": current.isoformat(),
        "request": request.model_dump(mode="json"),
    }
    environment = os.environ.copy()
    source_root = str(Path.cwd() / "platform/backend/src")
    environment["PYTHONPATH"] = (
        source_root
        if not environment.get("PYTHONPATH")
        else f"{source_root}{os.pathsep}{environment['PYTHONPATH']}"
    )
    script = """
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI

from agent_platform.collaboration_api import install_collaboration_api
from agent_platform.collaboration_models import DeveloperSourcePromotionRequest
from agent_platform.collaboration_service import CollaborationService
from agent_platform.collaboration_storage import CollaborationStore
from agent_platform.formal_assignment_runtime import PlatformFormalAssignmentRuntime
from agent_platform.formal_source_provenance import FormalSourceProvenanceCoordinator


async def main():
    p = json.loads(sys.argv[1])
    store = CollaborationStore(Path(p["database"]))
    await store.initialize()
    coordinator = FormalSourceProvenanceCoordinator(
        repository_root=Path(p["repository"]),
        state_root=Path(p["state_root"]),
    )
    runtime = object.__new__(PlatformFormalAssignmentRuntime)
    runtime._source_provenance = coordinator

    async def developer_workspace_for_channel(_channel):
        class Workspace:
            path = p["workspace"]
            manifest_digest = p["mount_digest"]

        class Resolved:
            source_manifest_digest = p["source_manifest_digest"]
            workspace = Workspace()

        return Resolved()

    runtime.developer_workspace_for_channel = developer_workspace_for_channel

    async def runtime_promotion_provider(channel, report, lease, request):
        return await runtime.promote_developer_workspace(
            channel=channel,
            report=report,
            lease=lease,
            request=request,
        )

    service = CollaborationService(
        store=store,
        enabled=True,
        developer_token="developer-token-promotion-status-0001",
        now=lambda: datetime.fromisoformat(p["current"]),
        developer_source_promotion_provider=runtime_promotion_provider,
    )
    app = FastAPI()

    async def require_user_token():
        return None

    install_collaboration_api(
        app,
        service,
        require_user_token=require_user_token,
    )
    request = DeveloperSourcePromotionRequest.model_validate(p["request"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            (
                "/api/v1/developer/collaboration/reports/"
                + p["report_id"]
                + "/source-promotions"
            ),
            headers={
                "Authorization": (
                    "Bearer developer-token-promotion-status-0001"
                )
            },
            json=request.model_dump(mode="json"),
        )
    print(json.dumps({
        "status_code": response.status_code,
        "body": response.json(),
        "process_token": str(coordinator._process_instance_id),
    }))


asyncio.run(main())
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps(payload)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(completed.stdout)


def _archive_in_subprocess(
    context: dict[str, object],
    *,
    binding: ApprovedDeveloperResponseBinding,
    finalized_at: datetime,
) -> dict[str, object]:
    payload = {
        "repository": str(context["repository"]),
        "state_root": str(context["state_root"]),
        "assignment_id": str(context["assignment_id"]),
        "binding": binding.model_dump(mode="json"),
        "finalized_at": finalized_at.isoformat(),
    }
    environment = os.environ.copy()
    source_root = str(Path.cwd() / "platform/backend/src")
    environment["PYTHONPATH"] = (
        source_root
        if not environment.get("PYTHONPATH")
        else f"{source_root}{os.pathsep}{environment['PYTHONPATH']}"
    )
    script = """
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from agent_platform.formal_source_provenance import (
    ApprovedDeveloperResponseBinding,
    FormalSourceProvenanceCoordinator,
)

p = json.loads(sys.argv[1])
binding = ApprovedDeveloperResponseBinding.model_validate(p["binding"])
coordinator = FormalSourceProvenanceCoordinator(
    repository_root=Path(p["repository"]),
    state_root=Path(p["state_root"]),
)
effective = coordinator.promoted_response_is_effective(
    assignment_id=UUID(p["assignment_id"]),
    channel_id=binding.channel_id,
    report_id=binding.report_id,
    report_revision=binding.response_report_revision,
    response_id=binding.response_id,
    commit_sha=binding.commit_sha,
)
coordinator.record_promoted_response(
    assignment_id=UUID(p["assignment_id"]),
    binding=binding,
)
archive = coordinator.finalize_archive(
    assignment_id=UUID(p["assignment_id"]),
    expected_bindings=[binding],
    finalized_at=datetime.fromisoformat(p["finalized_at"]),
)
print(json.dumps({
    "effective": effective,
    "process_token": str(coordinator._process_instance_id),
    "manifest": archive.manifest.model_dump(mode="json"),
    "files": {
        path: content.hex()
        for path, content in archive.files.items()
    },
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps(payload)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    result = json.loads(completed.stdout)
    result["files"] = {
        path: bytes.fromhex(content)
        for path, content in result["files"].items()
    }
    return result


def test_projection_promotes_exact_delta_and_preserves_unrelated_dirty_state(
    tmp_path: Path,
) -> None:
    context = _setup_projection(tmp_path)
    repository = context["repository"]
    workspace = context["workspace"]
    projection = context["projection"]
    coordinator = context["coordinator"]
    assert isinstance(repository, Path)
    assert isinstance(workspace, Path)
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)

    projected_paths = {entry.path for entry in projection.entries}
    assert {
        "platform/backend/src/agent_platform/example.py",
        "tests/test_example.py",
        "scripts/check.sh",
    } <= projected_paths
    assert "pyproject.toml" not in projected_paths
    assert "uv.lock" not in projected_paths
    assert (
        "platform/backend/src/agent_platform/independent_verifier.py"
        not in projected_paths
    )
    assert (
        "platform/backend/src/agent_platform/stable_verification.py"
        not in projected_paths
    )
    assert (
        "platform/backend/src/agent_platform/stable_verification_coordinator.py"
        not in projected_paths
    )
    assert "docs/note.md" not in projected_paths
    assert "platform/data/local.json" not in projected_paths
    assert not (workspace / "source/.git").exists()

    _write(workspace / "source/tests/test_example.py", "def test_promoted():\n    assert 2 + 2 == 4\n")
    _write(repository / "docs/note.md", "user dirty document\n")
    _git(repository, "add", "docs/note.md")
    _write(repository / "scripts/check.sh", "#!/bin/sh\n# unrelated user edit\nexit 0\n")
    _write(repository / "personal.txt", "user untracked file\n")

    receipt = _promote(context)
    assert receipt.activation_state == "activated"
    assert receipt.reload_status == "not_required"
    assert receipt.parent_commit_sha == context["baseline"]
    assert _git(repository, "rev-parse", "HEAD") == receipt.commit_sha
    assert _git(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        receipt.commit_sha,
    ) == "tests/test_example.py"
    assert (repository / "docs/note.md").read_text(encoding="utf-8") == "user dirty document\n"
    assert "unrelated user edit" in (
        repository / "scripts/check.sh"
    ).read_text(encoding="utf-8")
    assert (repository / "personal.txt").read_text(encoding="utf-8") == "user untracked file\n"
    assert (repository / "uv.lock").read_text(encoding="utf-8") == "user untracked lock\n"
    status = _git(repository, "status", "--porcelain")
    assert "docs/note.md" in status
    assert "scripts/check.sh" in status
    assert "personal.txt" in status
    assert "uv.lock" in status
    assert "user dirty document" in _git(
        repository,
        "diff",
        "--cached",
        "--",
        "docs/note.md",
    )

    assert coordinator.promoted_response_is_effective(
        assignment_id=context["assignment_id"],
        channel_id=context["channel_id"],
        report_id=context["report_id"],
        report_revision=4,
        response_id=context["response_id"],
        commit_sha=receipt.commit_sha,
    )
    assert _promote(context) == receipt
    assert not coordinator.promoted_response_is_effective(
        assignment_id=context["assignment_id"],
        channel_id=context["channel_id"],
        report_id=context["report_id"],
        report_revision=4,
        response_id=uuid4(),
        commit_sha=receipt.commit_sha,
    )
    assert not coordinator.promoted_response_is_effective(
        assignment_id=context["assignment_id"],
        channel_id=context["channel_id"],
        report_id=context["report_id"],
        report_revision=4,
        response_id=context["response_id"],
        commit_sha=context["baseline"],
    )

    binding = _binding(context, receipt.commit_sha)
    coordinator.record_promoted_response(
        assignment_id=context["assignment_id"],
        binding=binding,
    )
    archive = coordinator.finalize_archive(
        assignment_id=context["assignment_id"],
        expected_bindings=[binding],
        finalized_at=NOW,
    )
    assert archive.manifest.developer_projection == projection
    assert archive.manifest.promotion_receipts == [receipt]
    verified = verify_source_provenance_archive_offline(
        archive_files=archive.files,
        expected_assignment_id=context["assignment_id"],
        expected_bindings=[binding],
        expected_manifest_digest=archive.manifest.manifest_digest,
    )
    assert verified == archive.manifest
    assert archive.manifest.projection_blob_objects
    projection_blob = archive.manifest.projection_blob_objects[0]
    tampered_files = dict(archive.files)
    tampered_files[projection_blob.archive_path] += b"tamper"
    with pytest.raises(FormalSourceProvenanceSecurityError):
        verify_source_provenance_archive_offline(
            archive_files=tampered_files,
            expected_assignment_id=context["assignment_id"],
            expected_bindings=[binding],
            expected_manifest_digest=archive.manifest.manifest_digest,
        )
    assert str(repository).encode() not in json.dumps(
        archive.manifest.model_dump(mode="json"),
        sort_keys=True,
    ).encode()


def test_backend_promotion_rejects_a_second_coordinator_in_the_same_process(
    tmp_path: Path,
) -> None:
    context = _setup_projection(tmp_path)
    workspace = context["workspace"]
    repository = context["repository"]
    coordinator = context["coordinator"]
    assert isinstance(workspace, Path)
    assert isinstance(repository, Path)
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    _write(
        workspace / "source/platform/backend/src/agent_platform/example.py",
        "VALUE = 'promoted'\n",
    )
    receipt = _promote(context)
    assert receipt.reload_status == "restart_required"
    resolver_arguments = {
        "assignment_id": context["assignment_id"],
        "channel_id": context["channel_id"],
        "report_id": context["report_id"],
        "report_revision": 4,
        "response_id": context["response_id"],
        "commit_sha": receipt.commit_sha,
    }
    assert not coordinator.promoted_response_is_effective(**resolver_arguments)

    same_process = FormalSourceProvenanceCoordinator(
        repository_root=repository,
        state_root=context["state_root"],
    )
    assert (
        same_process._process_instance_id
        == coordinator._process_instance_id
    )
    _write(
        repository / "platform/backend/src/agent_platform/example.py",
        "VALUE = 'concurrent drift'\n",
    )
    assert not same_process.promoted_response_is_effective(**resolver_arguments)
    reload_root = (
        context["state_root"]
        / "assignments"
        / str(context["assignment_id"])
        / "promotions"
        / str(context["response_id"])
        / "reload-confirmations"
    )
    assert not reload_root.exists()
    _write(
        repository / "platform/backend/src/agent_platform/example.py",
        "VALUE = 'promoted'\n",
    )
    assert not same_process.promoted_response_is_effective(**resolver_arguments)
    assert not reload_root.exists()


def test_backend_reload_requires_subprocess_exit_and_a_distinct_process_token(
    tmp_path: Path,
) -> None:
    context = _setup_projection(tmp_path)
    workspace = context["workspace"]
    projection = context["projection"]
    assert isinstance(workspace, Path)
    _write(
        workspace / "source/platform/backend/src/agent_platform/example.py",
        "VALUE = 'subprocess promotion'\n",
    )
    payload = {
        "repository": str(context["repository"]),
        "state_root": str(context["state_root"]),
        "workspace": str(workspace),
        "assignment_id": str(context["assignment_id"]),
        "channel_id": str(context["channel_id"]),
        "report_id": str(context["report_id"]),
        "lease_id": str(context["lease_id"]),
        "response_id": str(context["response_id"]),
        "workspace_manifest_digest": str(context["mount_digest"]),
        "source_manifest_digest": projection.manifest_digest,
        "created_at": NOW.isoformat(),
    }
    environment = os.environ.copy()
    source_root = str(Path.cwd() / "platform/backend/src")
    environment["PYTHONPATH"] = (
        source_root
        if not environment.get("PYTHONPATH")
        else f"{source_root}{os.pathsep}{environment['PYTHONPATH']}"
    )
    activate_script = """
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID
from agent_platform.formal_source_provenance import FormalSourceProvenanceCoordinator

p = json.loads(sys.argv[1])
c = FormalSourceProvenanceCoordinator(
    repository_root=Path(p["repository"]),
    state_root=Path(p["state_root"]),
)
r = c.promote_workspace_delta(
    assignment_id=UUID(p["assignment_id"]),
    channel_id=UUID(p["channel_id"]),
    report_id=UUID(p["report_id"]),
    report_revision=4,
    lease_id=UUID(p["lease_id"]),
    lease_owner_id="codex:subprocess",
    response_id=UUID(p["response_id"]),
    idempotency_key="promote:subprocess",
    workspace=Path(p["workspace"]),
    workspace_manifest_digest=p["workspace_manifest_digest"],
    source_manifest_digest=p["source_manifest_digest"],
    created_at=datetime.fromisoformat(p["created_at"]),
)
same = FormalSourceProvenanceCoordinator(
    repository_root=Path(p["repository"]),
    state_root=Path(p["state_root"]),
)
effective = same.promoted_response_is_effective(
    assignment_id=UUID(p["assignment_id"]),
    channel_id=UUID(p["channel_id"]),
    report_id=UUID(p["report_id"]),
    report_revision=4,
    response_id=UUID(p["response_id"]),
    commit_sha=r.commit_sha,
)
print(json.dumps({
    "receipt": r.model_dump(mode="json"),
    "same_process_effective": effective,
    "first_token": str(c._process_instance_id),
    "second_token": str(same._process_instance_id),
}))
"""
    activated = subprocess.run(
        [sys.executable, "-c", activate_script, json.dumps(payload)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    activation = json.loads(activated.stdout)
    assert not activation["same_process_effective"]
    assert activation["first_token"] == activation["second_token"]
    assert activation["receipt"]["reload_status"] == "restart_required"
    payload["commit_sha"] = activation["receipt"]["commit_sha"]

    confirmation = _confirm_in_subprocess(
        context,
        commit_sha=payload["commit_sha"],
    )
    assert confirmation["effective"]
    assert confirmation["process_token"] != activation["first_token"]
    second_confirmation = _confirm_in_subprocess(
        context,
        commit_sha=payload["commit_sha"],
    )
    assert second_confirmation["effective"]
    assert second_confirmation["process_token"] not in {
        activation["first_token"],
        confirmation["process_token"],
    }
    reload_root = (
        context["state_root"]
        / "assignments"
        / str(context["assignment_id"])
        / "promotions"
        / str(context["response_id"])
        / "reload-confirmations"
    )
    reload_paths = sorted(reload_root.glob("*.json"))
    assert [path.stem for path in reload_paths] == sorted(
        [
            confirmation["process_token"],
            second_confirmation["process_token"],
        ]
    )
    reload_payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in reload_paths
    ]
    assert {
        item["activation_process_instance_id"]
        for item in reload_payloads
    } == {activation["first_token"]}
    assert (
        {
            item["confirming_process_instance_id"]
            for item in reload_payloads
        }
        == {
            confirmation["process_token"],
            second_confirmation["process_token"],
        }
    )


@pytest.mark.parametrize("attack", ["manifest", "outside_projection"])
def test_promotion_rejects_workspace_authority_tampering(
    tmp_path: Path,
    attack: str,
) -> None:
    context = _setup_projection(tmp_path)
    workspace = context["workspace"]
    assert isinstance(workspace, Path)
    if attack == "manifest":
        manifest = workspace / "source" / DEVELOPER_SOURCE_MANIFEST_FILE
        manifest.chmod(0o600)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["branch_ref"] = "refs/heads/other"
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    else:
        _write(workspace / "source/docs/adapter.py", "SPECIAL = True\n")
    with pytest.raises(
        (FormalSourceProvenanceConflict, FormalSourceProvenanceSecurityError)
    ):
        _promote(context)


@pytest.mark.parametrize(
    "protected_path",
    [
        "pyproject.toml",
        "platform/backend/src/agent_platform/independent_verifier.py",
        "platform/backend/src/agent_platform/forbidden_assistance_scanner.py",
        "platform/backend/src/agent_platform/stable_verification.py",
        "platform/backend/src/agent_platform/stable_verification_cli.py",
        "platform/backend/src/agent_platform/stable_verification_coordinator.py",
        "platform/backend/src/agent_platform/task_packages.py",
    ],
)
def test_promotion_rejects_protected_verification_trust_root(
    tmp_path: Path,
    protected_path: str,
) -> None:
    context = _setup_projection(tmp_path)
    workspace = context["workspace"]
    projection = context["projection"]
    assert isinstance(workspace, Path)
    assert protected_path not in {
        entry.path for entry in projection.entries
    }

    _write(
        workspace / "source" / protected_path,
        "FORGED_PRODUCTION_VERDICT = 'independently_verified'\n",
    )

    with pytest.raises(
        FormalSourceProvenanceSecurityError,
        match="outside its projection",
    ):
        _promote(context)


def test_activation_rejects_affected_user_drift_and_wrong_parent(
    tmp_path: Path,
) -> None:
    conflict = _setup_projection(tmp_path / "conflict")
    conflict_workspace = conflict["workspace"]
    conflict_repository = conflict["repository"]
    assert isinstance(conflict_workspace, Path)
    assert isinstance(conflict_repository, Path)
    _write(
        conflict_workspace / "source/tests/test_example.py",
        "def test_workspace():\n    assert True\n",
    )
    _write(
        conflict_repository / "tests/test_example.py",
        "def test_user_dirty():\n    assert True\n",
    )
    with pytest.raises(FormalSourceProvenanceConflict):
        _promote(conflict)
    assert _git(conflict_repository, "rev-parse", "HEAD") == conflict["baseline"]
    assert "test_user_dirty" in (
        conflict_repository / "tests/test_example.py"
    ).read_text(encoding="utf-8")

    moved = _setup_projection(tmp_path / "moved")
    moved_workspace = moved["workspace"]
    moved_repository = moved["repository"]
    assert isinstance(moved_workspace, Path)
    assert isinstance(moved_repository, Path)
    _write(
        moved_workspace / "source/tests/test_example.py",
        "def test_workspace():\n    assert True\n",
    )
    baseline_tree = _git(
        moved_repository,
        "rev-parse",
        f"{moved['baseline']}^{{tree}}",
    )
    moved_head = _git(
        moved_repository,
        "commit-tree",
        baseline_tree,
        input_payload=b"divergent branch root\n",
    )
    _git(
        moved_repository,
        "update-ref",
        "refs/heads/main",
        moved_head,
        str(moved["baseline"]),
    )
    _git(moved_repository, "read-tree", "--reset", "-u", moved_head)
    with pytest.raises(FormalSourceProvenanceConflict):
        _promote(moved)
    assert _git(moved_repository, "rev-parse", "HEAD") == moved_head


def test_promotion_rebases_once_over_path_disjoint_fast_forward(
    tmp_path: Path,
) -> None:
    context = _setup_projection(tmp_path)
    repository = context["repository"]
    workspace = context["workspace"]
    assert isinstance(repository, Path)
    assert isinstance(workspace, Path)

    _write(repository / "docs/note.md", "unrelated committed evolution\n")
    _git(repository, "add", "docs/note.md")
    _git(repository, "commit", "-m", "advance unrelated source")
    advanced_head = _git(repository, "rev-parse", "HEAD")
    _write(
        workspace / "source/tests/test_example.py",
        "def test_rebased_workspace():\n    assert True\n",
    )

    receipt = _promote(context)

    assert receipt.parent_commit_sha == advanced_head
    assert _git(repository, "rev-parse", "HEAD") == receipt.commit_sha
    assert "unrelated committed evolution" in (
        repository / "docs/note.md"
    ).read_text(encoding="utf-8")
    assert "test_rebased_workspace" in (
        repository / "tests/test_example.py"
    ).read_text(encoding="utf-8")


def test_promotion_rejects_fast_forward_that_touched_target_path(
    tmp_path: Path,
) -> None:
    context = _setup_projection(tmp_path)
    repository = context["repository"]
    workspace = context["workspace"]
    assert isinstance(repository, Path)
    assert isinstance(workspace, Path)

    _write(
        repository / "tests/test_example.py",
        "def test_committed_user_change():\n    assert True\n",
    )
    _git(repository, "add", "tests/test_example.py")
    _git(repository, "commit", "-m", "advance affected source")
    advanced_head = _git(repository, "rev-parse", "HEAD")
    _write(
        workspace / "source/tests/test_example.py",
        "def test_workspace_change():\n    assert True\n",
    )

    with pytest.raises(
        FormalSourceProvenanceConflict,
        match="cannot rebase",
    ):
        _promote(context)

    assert _git(repository, "rev-parse", "HEAD") == advanced_head
    assert "test_committed_user_change" in (
        repository / "tests/test_example.py"
    ).read_text(encoding="utf-8")


def test_branch_cas_preserves_concurrent_third_commit_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _setup_projection(tmp_path)
    repository = context["repository"]
    workspace = context["workspace"]
    coordinator = context["coordinator"]
    projection = context["projection"]
    assert isinstance(repository, Path)
    assert isinstance(workspace, Path)
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    _write(
        workspace / "source/tests/test_example.py",
        "def test_recovered():\n    assert True\n",
    )
    _write(repository / "docs/note.md", "unrelated staged bytes\n")
    _git(repository, "add", "docs/note.md")
    original_run = coordinator._repository.run
    parent_tree = _git(repository, "rev-parse", f"{context['baseline']}^{{tree}}")
    third_commit = _git(
        repository,
        "commit-tree",
        parent_tree,
        "-p",
        str(context["baseline"]),
        input_payload=b"concurrent third-party commit\n",
    )
    injected = False

    def advance_branch_before_promotion_cas(arguments, **kwargs):
        nonlocal injected
        if (
            not injected
            and len(arguments) >= 2
            and arguments[0] == "update-ref"
            and arguments[1] == projection.branch_ref
        ):
            injected = True
            original_run(
                [
                    "update-ref",
                    projection.branch_ref,
                    third_commit,
                    str(context["baseline"]),
                ],
                limit=1024,
                environment_overrides={"GIT_OPTIONAL_LOCKS": "1"},
            )
        return original_run(arguments, **kwargs)

    monkeypatch.setattr(
        coordinator._repository,
        "run",
        advance_branch_before_promotion_cas,
    )
    with pytest.raises(FormalSourceProvenanceError):
        _promote(context)
    assert injected
    assert _git(repository, "rev-parse", "HEAD") == third_commit
    assert _git(repository, "rev-parse", projection.branch_ref) == third_commit
    assert (
        repository / "tests/test_example.py"
    ).read_text(encoding="utf-8") == "def test_baseline():\n    assert True\n"
    assert "unrelated staged bytes" in _git(
        repository,
        "diff",
        "--cached",
        "--",
        "docs/note.md",
    )


def test_concurrent_index_bytes_are_preserved_when_activation_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _setup_projection(tmp_path)
    repository = context["repository"]
    workspace = context["workspace"]
    coordinator = context["coordinator"]
    assert isinstance(repository, Path)
    assert isinstance(workspace, Path)
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    path = "tests/test_example.py"
    _write(
        workspace / "source" / path,
        "def test_workspace_target():\n    assert True\n",
    )
    original_replace = coordinator._replace_index_cas
    injected = False
    third_party = b"def test_user_staged():\n    assert True\n"

    def inject_index_write(*, index_path, expected, target):
        nonlocal injected
        replaced = original_replace(
            index_path=index_path,
            expected=expected,
            target=target,
        )
        if replaced and not injected:
            injected = True
            blob_sha = _git(
                repository,
                "hash-object",
                "-w",
                "--stdin",
                input_payload=third_party,
            )
            _git(
                repository,
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                blob_sha,
                path,
            )
        return replaced

    monkeypatch.setattr(
        coordinator,
        "_replace_index_cas",
        inject_index_write,
    )
    with pytest.raises(FormalSourceProvenanceConflict):
        _promote(context)
    assert _git(repository, "rev-parse", "HEAD") == context["baseline"]
    assert (
        repository / path
    ).read_text(encoding="utf-8") == "def test_baseline():\n    assert True\n"
    assert _git(repository, "show", f":{path}") == third_party.decode().strip()


def test_concurrent_worktree_bytes_are_preserved_when_activation_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _setup_projection(tmp_path)
    repository = context["repository"]
    workspace = context["workspace"]
    coordinator = context["coordinator"]
    assert isinstance(repository, Path)
    assert isinstance(workspace, Path)
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    path = "tests/test_example.py"
    _write(
        workspace / "source" / path,
        "def test_workspace_target():\n    assert True\n",
    )
    original_write = coordinator._write_worktree_endpoint
    injected = False
    third_party = "def test_user_concurrent():\n    assert True\n"

    def inject_worktree_write(*, intent, path, expected, endpoint):
        nonlocal injected
        original_write(
            intent=intent,
            path=path,
            expected=expected,
            endpoint=endpoint,
        )
        if not injected and path == "tests/test_example.py":
            injected = True
            _write(repository / path, third_party)

    monkeypatch.setattr(
        coordinator,
        "_write_worktree_endpoint",
        inject_worktree_write,
    )
    with pytest.raises(FormalSourceProvenanceConflict):
        _promote(context)
    assert _git(repository, "rev-parse", "HEAD") == context["baseline"]
    assert (repository / path).read_text(encoding="utf-8") == third_party
    assert _git(repository, "show", f":{path}") == (
        "def test_baseline():\n    assert True"
    )


def test_atomic_replacement_after_final_check_is_quarantined_and_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _setup_projection(tmp_path)
    repository = context["repository"]
    workspace = context["workspace"]
    coordinator = context["coordinator"]
    assert isinstance(repository, Path)
    assert isinstance(workspace, Path)
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    path = "tests/test_example.py"
    candidate = repository / path
    _write(
        workspace / "source" / path,
        "def test_workspace_target():\n    assert True\n",
    )
    original_move = coordinator._move_worktree_candidate_to_displacement
    third_party = "def test_atomic_user_inode():\n    assert True\n"
    third_inode: tuple[int, int] | None = None
    injected = False

    def replace_after_final_check(live_candidate, displacement):
        nonlocal injected, third_inode
        if not injected and live_candidate == candidate:
            injected = True
            third_path = candidate.with_name(".atomic-user-replacement")
            _write(third_path, third_party)
            metadata = third_path.lstat()
            third_inode = (metadata.st_dev, metadata.st_ino)
            # This is the external writer's atomic replacement in the exact
            # final-check -> mutation window.
            os.replace(third_path, live_candidate)
        original_move(live_candidate, displacement)

    monkeypatch.setattr(
        coordinator,
        "_move_worktree_candidate_to_displacement",
        replace_after_final_check,
    )
    with pytest.raises(FormalSourceProvenanceConflict):
        _promote(context)
    assert injected
    assert third_inode is not None
    current = candidate.lstat()
    assert (current.st_dev, current.st_ino) == third_inode
    assert candidate.read_text(encoding="utf-8") == third_party
    assert _git(repository, "rev-parse", "HEAD") == context["baseline"]
    assert _git(repository, "show", f":{path}") == (
        "def test_baseline():\n    assert True"
    )


def test_displacement_destination_race_never_replaces_external_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _setup_projection(tmp_path)
    repository = context["repository"]
    workspace = context["workspace"]
    coordinator = context["coordinator"]
    assert isinstance(repository, Path)
    assert isinstance(workspace, Path)
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    path = "tests/test_example.py"
    candidate = repository / path
    _write(
        workspace / "source" / path,
        "def test_workspace_target():\n    assert True\n",
    )
    original_move = coordinator._move_worktree_candidate_to_displacement
    external_bytes = "external displacement inode\n"
    external_path: Path | None = None
    external_inode: tuple[int, int] | None = None

    def occupy_destination_before_kernel_rename(live_candidate, displacement):
        nonlocal external_path, external_inode
        assert live_candidate == candidate
        external_path = displacement
        _write(displacement, external_bytes)
        metadata = displacement.lstat()
        external_inode = (metadata.st_dev, metadata.st_ino)
        original_move(live_candidate, displacement)

    monkeypatch.setattr(
        coordinator,
        "_move_worktree_candidate_to_displacement",
        occupy_destination_before_kernel_rename,
    )
    with pytest.raises(FormalSourceProvenanceConflict):
        _promote(context)
    assert external_path is not None
    assert external_inode is not None
    metadata = external_path.lstat()
    assert metadata.st_nlink > 0
    assert (metadata.st_dev, metadata.st_ino) == external_inode
    assert external_path.read_text(encoding="utf-8") == external_bytes
    assert candidate.read_text(encoding="utf-8") == (
        "def test_baseline():\n    assert True\n"
    )
    assert _git(repository, "rev-parse", "HEAD") == context["baseline"]


def test_activated_receipt_can_be_adopted_by_a_new_lease_without_a_new_commit(
    tmp_path: Path,
) -> None:
    context = _setup_projection(tmp_path)
    workspace = context["workspace"]
    coordinator = context["coordinator"]
    assert isinstance(workspace, Path)
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    _write(
        workspace / "source/tests/test_example.py",
        "def test_adopted():\n    assert True\n",
    )
    receipt = _promote(context)
    lease_id = uuid4()
    adopted = _promote(
        context,
        report_revision=5,
        lease_id=lease_id,
        lease_owner_id="codex:reacquired",
        idempotency_key="promote:test:adopted",
    )
    assert adopted == receipt
    assert coordinator.promoted_response_is_effective(
        assignment_id=context["assignment_id"],
        channel_id=context["channel_id"],
        report_id=context["report_id"],
        report_revision=5,
        response_id=context["response_id"],
        commit_sha=receipt.commit_sha,
    )
    binding = _binding(
        context,
        receipt.commit_sha,
        response_report_revision=5,
    )
    coordinator.record_promoted_response(
        assignment_id=context["assignment_id"],
        binding=binding,
    )
    archive = coordinator.finalize_archive(
        assignment_id=context["assignment_id"],
        expected_bindings=[binding],
        finalized_at=NOW,
    )
    assert len(archive.manifest.promotion_adoptions or []) == 1
    assert archive.manifest.promotion_adoptions[0].lease_id == lease_id
    assert (
        verify_source_provenance_archive_offline(
            archive_files=archive.files,
            expected_assignment_id=context["assignment_id"],
            expected_bindings=[binding],
            expected_manifest_digest=archive.manifest.manifest_digest,
        )
        == archive.manifest
    )


def test_adoption_rejects_workspace_drift(
    tmp_path: Path,
) -> None:
    context = _setup_projection(tmp_path)
    workspace = context["workspace"]
    assert isinstance(workspace, Path)
    _write(
        workspace / "source/tests/test_example.py",
        "def test_initial():\n    assert True\n",
    )
    _promote(context)
    _write(
        workspace / "source/tests/test_example.py",
        "def test_late_unpromoted_change():\n    assert True\n",
    )
    with pytest.raises(FormalSourceProvenanceConflict):
        _promote(
            context,
            report_revision=5,
            lease_id=uuid4(),
            lease_owner_id="codex:reacquired",
            idempotency_key="promote:test:drifted-adoption",
        )


def test_record_rejects_report_or_revision_not_bound_to_the_receipt(
    tmp_path: Path,
) -> None:
    context = _setup_projection(tmp_path)
    workspace = context["workspace"]
    coordinator = context["coordinator"]
    assert isinstance(workspace, Path)
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    _write(
        workspace / "source/tests/test_example.py",
        "def test_binding():\n    assert True\n",
    )
    receipt = _promote(context)
    binding = _binding(context, receipt.commit_sha)
    for rebound in (
        binding.model_copy(update={"report_id": uuid4()}),
        binding.model_copy(update={"response_report_revision": 5}),
    ):
        with pytest.raises(FormalSourceProvenanceConflict):
            coordinator.record_promoted_response(
                assignment_id=context["assignment_id"],
                binding=rebound,
            )


def test_offline_manifest_rejects_rebound_intent_and_reload_confirmation(
    tmp_path: Path,
) -> None:
    context = _setup_projection(tmp_path)
    workspace = context["workspace"]
    coordinator = context["coordinator"]
    assert isinstance(workspace, Path)
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    _write(
        workspace / "source/platform/backend/src/agent_platform/example.py",
        "VALUE = 'archive reload proof'\n",
    )
    receipt = _promote(context)
    first_confirmation = _confirm_in_subprocess(
        context,
        commit_sha=receipt.commit_sha,
    )
    second_confirmation = _confirm_in_subprocess(
        context,
        commit_sha=receipt.commit_sha,
    )
    assert first_confirmation["effective"]
    assert second_confirmation["effective"]
    assert (
        first_confirmation["process_token"]
        != second_confirmation["process_token"]
    )
    assert not coordinator.promoted_response_is_effective(
        assignment_id=context["assignment_id"],
        channel_id=context["channel_id"],
        report_id=context["report_id"],
        report_revision=4,
        response_id=context["response_id"],
        commit_sha=receipt.commit_sha,
    )
    binding = _binding(context, receipt.commit_sha)
    subprocess_archive = _archive_in_subprocess(
        context,
        binding=binding,
        finalized_at=NOW,
    )
    assert subprocess_archive["effective"]
    archive_files = subprocess_archive["files"]
    verified = verify_source_provenance_archive_offline(
        archive_files=archive_files,
        expected_assignment_id=context["assignment_id"],
        expected_bindings=[binding],
    )
    assert verified.reload_confirmations is not None
    confirming_processes = {
        str(item.confirming_process_instance_id)
        for item in verified.reload_confirmations
    }
    assert confirming_processes == {
        first_confirmation["process_token"],
        second_confirmation["process_token"],
        subprocess_archive["process_token"],
    }

    for section, field, replacement in (
        ("promotion_intents", "branch_ref", "refs/heads/rebound"),
        (
            "reload_confirmations",
            "hidden_ref",
            f"refs/lilies/formal/{context['assignment_id']}/{uuid4()}",
        ),
    ):
        files = dict(archive_files)
        manifest = json.loads(files[SOURCE_PROVENANCE_MANIFEST_PATH])
        nested = manifest[section][0]
        nested[field] = replacement
        digest_field = (
            "intent_digest"
            if section == "promotion_intents"
            else "confirmation_digest"
        )
        nested_without_digest = dict(nested)
        nested_without_digest.pop(digest_field)
        nested[digest_field] = _digest(_canonical_json(nested_without_digest))
        manifest_without_digest = dict(manifest)
        manifest_without_digest.pop("manifest_digest")
        manifest["manifest_digest"] = _digest(
            _canonical_json(manifest_without_digest)
        )
        files[SOURCE_PROVENANCE_MANIFEST_PATH] = _canonical_json(manifest)
        with pytest.raises(
            (ValueError, FormalSourceProvenanceSecurityError)
        ):
            verify_source_provenance_archive_offline(
                archive_files=files,
                expected_assignment_id=context["assignment_id"],
                expected_bindings=[binding],
            )


@pytest.mark.asyncio
async def test_runtime_api_exposes_restart_required_then_confirmed(
    tmp_path: Path,
) -> None:
    store, database, channel_row = await _store_with_channel(
        tmp_path / "collaboration"
    )
    assignment_id = UUID(channel_row["assignment_id"])
    channel_id = UUID(channel_row["channel_id"])
    context = _setup_projection(
        tmp_path / "source",
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    coordinator = context["coordinator"]
    workspace = context["workspace"]
    projection = context["projection"]
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    assert isinstance(workspace, Path)
    _write(
        workspace / "source/platform/backend/src/agent_platform/example.py",
        "VALUE = 'runtime public status'\n",
    )

    runtime = object.__new__(PlatformFormalAssignmentRuntime)
    runtime._source_provenance = coordinator

    async def developer_workspace_for_channel(_channel):
        class Workspace:
            path = str(workspace)
            manifest_digest = context["mount_digest"]

        class Resolved:
            source_manifest_digest = projection.manifest_digest
            workspace = Workspace()

        return Resolved()

    runtime.developer_workspace_for_channel = developer_workspace_for_channel

    async def runtime_promotion_provider(channel, report, lease, request):
        return await runtime.promote_developer_workspace(
            channel=channel,
            report=report,
            lease=lease,
            request=request,
        )

    current = datetime.now(timezone.utc)
    service = CollaborationService(
        store=store,
        enabled=True,
        developer_token="developer-token-promotion-status-0001",
        now=lambda: current,
        developer_source_promotion_provider=runtime_promotion_provider,
    )
    lilies = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=str(channel_row["lilies_session_id"]),
        scopes=frozenset(
            {
                CollaborationScope.report_write.value,
                CollaborationScope.response_read.value,
            }
        ),
        channel_id=channel_id,
        assignment_id=assignment_id,
    )
    user = CollaborationPrincipal(
        role=SenderRole.user,
        sender_id="studio-user",
        scopes=frozenset(),
    )
    developer = CollaborationPrincipal(
        role=SenderRole.codex,
        sender_id="codex-developer",
        scopes=frozenset({"collaboration.developer"}),
    )
    report_id = uuid4()
    await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="promotion-status-report-0001",
            expected_channel_revision=1,
            report=CollaborationReportPayload.model_validate(
                _report_payload(report_id)
            ),
        ),
    )
    await service.decide_report(
        principal=user,
        report_id=report_id,
        request=ApprovalDecisionRequest(
            idempotency_key="promotion-status-approval-0001",
            expected_report_revision=3,
            decision="approve",
        ),
    )
    acquired = await service.acquire_developer_lease(
        principal=developer,
        report_id=report_id,
        request=LeaseAcquireRequest(
            idempotency_key="promotion-status-lease-0001",
            expected_report_revision=4,
            owner_id=developer.sender_id,
            ttl_seconds=900,
        ),
    )
    current = datetime.fromisoformat(acquired["acquired_at"]) + timedelta(
        seconds=1
    )
    response_id = uuid4()
    request = DeveloperSourcePromotionRequest(
        idempotency_key="promotion-status-promote-0001",
        lease_id=UUID(acquired["lease_id"]),
        lease_owner_id=developer.sender_id,
        expected_report_revision=5,
        response_id=response_id,
        workspace_manifest_digest=context["mount_digest"],
        source_manifest_digest=projection.manifest_digest,
    )
    runtime_before = await runtime.promote_developer_workspace(
        channel=await service._channel(channel_id),
        report=CollaborationReport.model_validate(
            await store.get_report(report_id)
        ),
        lease=DeveloperLease.model_validate(
            await store.get_active_lease(report_id)
        ),
        request=request,
    )
    assert runtime_before["effective"] is False
    assert runtime_before["reload_status"] == "restart_required"
    service_before = await service.promote_developer_source(
        principal=developer,
        report_id=report_id,
        request=request,
    )
    assert service_before["effective"] is False
    app = FastAPI()

    async def require_user_token() -> None:
        return None

    install_collaboration_api(
        app,
        service,
        require_user_token=require_user_token,
    )
    transport = httpx.ASGITransport(app=app)
    headers = {
        "Authorization": "Bearer developer-token-promotion-status-0001"
    }
    url = (
        "/api/v1/developer/collaboration/reports/"
        f"{report_id}/source-promotions"
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        before_response = await client.post(
            url,
            headers=headers,
            json=request.model_dump(mode="json"),
        )
        assert before_response.status_code == 200, before_response.text
        before = before_response.json()
        assert before["effective"] is False
        assert before["reload_confirmed"] is False
        assert before["reload_status"] == "restart_required"

        restarted = await asyncio.to_thread(
            _promote_through_api_in_subprocess,
            context,
            database=database,
            report_id=report_id,
            current=current,
            request=request,
        )
        assert restarted["status_code"] == 200
        restarted_body = restarted["body"]
        assert restarted_body["commit_sha"] == before["commit_sha"]
        assert restarted_body["receipt_digest"] == before["receipt_digest"]
        assert restarted_body["effective"] is True
        assert restarted_body["reload_confirmed"] is True
        assert restarted_body["reload_status"] == "confirmed"
        assert restarted["process_token"] != str(
            coordinator._process_instance_id
        )

        after_response = await client.post(
            url,
            headers=headers,
            json=request.model_dump(mode="json"),
        )
        assert after_response.status_code == 200
        after = after_response.json()
        assert after["commit_sha"] == before["commit_sha"]
        assert after["receipt_digest"] == before["receipt_digest"]
        assert after["effective"] is False
        assert after["reload_confirmed"] is False
        assert after["reload_status"] == "restart_required"


@pytest.mark.asyncio
async def test_workspace_edit_promote_then_submit_response_is_the_only_service_path(
    tmp_path: Path,
) -> None:
    store, _database, channel_row = await _store_with_channel(
        tmp_path / "collaboration"
    )
    assignment_id = UUID(channel_row["assignment_id"])
    channel_id = UUID(channel_row["channel_id"])
    context = _setup_projection(
        tmp_path / "source",
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    coordinator = context["coordinator"]
    workspace = context["workspace"]
    projection = context["projection"]
    assert isinstance(coordinator, FormalSourceProvenanceCoordinator)
    assert isinstance(workspace, Path)
    _write(
        workspace / "source/tests/test_example.py",
        "def test_service_promoted():\n    assert True\n",
    )

    current = datetime.now(timezone.utc)

    def promote_provider(channel, report, lease, request):
        return coordinator.promote_workspace_delta(
            assignment_id=channel.assignment_id,
            channel_id=channel.channel_id,
            report_id=report.report_id,
            report_revision=report.revision,
            lease_id=lease.lease_id,
            lease_owner_id=lease.owner_id,
            response_id=request.response_id,
            idempotency_key=request.idempotency_key,
            workspace=workspace,
            workspace_manifest_digest=request.workspace_manifest_digest,
            source_manifest_digest=request.source_manifest_digest,
            created_at=current,
        )

    def promotion_resolver(channel, response):
        return coordinator.promoted_response_is_effective(
            assignment_id=channel.assignment_id,
            channel_id=channel.channel_id,
            report_id=response.report_id,
            report_revision=response.report_revision,
            response_id=response.response_id,
            commit_sha=response.commit_sha,
        )

    service = CollaborationService(
        store=store,
        enabled=True,
        now=lambda: current,
        developer_commit_resolver=lambda _commit_sha: True,
        developer_promotion_resolver=promotion_resolver,
        developer_evidence_resolver=lambda _commit_sha, _evidence: True,
        developer_source_promotion_provider=promote_provider,
    )
    lilies = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=str(channel_row["lilies_session_id"]),
        scopes=frozenset(
            {
                CollaborationScope.report_write.value,
                CollaborationScope.response_read.value,
            }
        ),
        channel_id=channel_id,
        assignment_id=assignment_id,
    )
    user = CollaborationPrincipal(
        role=SenderRole.user,
        sender_id="studio-user",
        scopes=frozenset(),
    )
    developer = CollaborationPrincipal(
        role=SenderRole.codex,
        sender_id="codex-developer",
        scopes=frozenset({"collaboration.developer"}),
    )
    report_id = uuid4()
    await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="promotion-service-report-0001",
            expected_channel_revision=1,
            report=CollaborationReportPayload.model_validate(
                _report_payload(report_id)
            ),
        ),
    )
    await service.decide_report(
        principal=user,
        report_id=report_id,
        request=ApprovalDecisionRequest(
            idempotency_key="promotion-service-approval-0001",
            expected_report_revision=3,
            decision="approve",
        ),
    )
    acquired = await service.acquire_developer_lease(
        principal=developer,
        report_id=report_id,
        request=LeaseAcquireRequest(
            idempotency_key="promotion-service-lease-0001",
            expected_report_revision=4,
            owner_id=developer.sender_id,
            ttl_seconds=900,
        ),
    )
    lease_id = UUID(acquired["lease_id"])
    current = datetime.fromisoformat(acquired["acquired_at"]) + timedelta(
        seconds=1
    )

    bypass_response_id = uuid4()
    bypass_payload = _developer_response_payload(
        response_id=bypass_response_id,
        channel_id=channel_id,
        report_id=report_id,
        report_revision=5,
        created_at=current,
    )
    bypass_payload["commit_sha"] = context["baseline"]
    with pytest.raises(CollaborationConflict) as bypass:
        await service.submit_developer_response(
            principal=developer,
            report_id=report_id,
            request=DeveloperResponseRequest(
                idempotency_key="promotion-service-bypass-0001",
                lease_id=lease_id,
                lease_owner_id=developer.sender_id,
                expected_report_revision=5,
                response={
                    key: value
                    for key, value in bypass_payload.items()
                    if key
                    not in {
                        "channel_id",
                        "report_id",
                        "report_revision",
                        "created_at",
                    }
                },
            ),
        )
    assert bypass.value.code == "developer_commit_not_promoted"

    response_id = uuid4()
    promotion_request = DeveloperSourcePromotionRequest(
        idempotency_key="promotion-service-promote-0001",
        lease_id=lease_id,
        lease_owner_id=developer.sender_id,
        expected_report_revision=5,
        response_id=response_id,
        workspace_manifest_digest=context["mount_digest"],
        source_manifest_digest=projection.manifest_digest,
    )
    promoted = await service.promote_developer_source(
        principal=developer,
        report_id=report_id,
        request=promotion_request,
    )
    assert (
        await service.promote_developer_source(
            principal=developer,
            report_id=report_id,
            request=promotion_request,
        )
        == promoted
    )
    released = await service.release_developer_lease(
        principal=developer,
        report_id=report_id,
        request=LeaseReleaseRequest(
            idempotency_key="promotion-service-release-0001",
            expected_lease_revision=1,
            owner_id=developer.sender_id,
            reason="Exercise durable promotion adoption.",
        ),
    )
    assert released["status"] == "released"
    available_report = await store.get_report(report_id)
    assert (
        available_report["status"],
        available_report["revision"],
    ) == ("approved_for_codex", 6)
    reacquired = await service.acquire_developer_lease(
        principal=developer,
        report_id=report_id,
        request=LeaseAcquireRequest(
            idempotency_key="promotion-service-lease-0002",
            expected_report_revision=6,
            owner_id=developer.sender_id,
            ttl_seconds=900,
        ),
    )
    replacement_lease_id = UUID(reacquired["lease_id"])
    assert replacement_lease_id != lease_id
    assert reacquired["report_revision"] == 7
    adopted_request = DeveloperSourcePromotionRequest(
        idempotency_key="promotion-service-promote-0002",
        lease_id=replacement_lease_id,
        lease_owner_id=developer.sender_id,
        expected_report_revision=7,
        response_id=response_id,
        workspace_manifest_digest=context["mount_digest"],
        source_manifest_digest=projection.manifest_digest,
    )
    adopted = await service.promote_developer_source(
        principal=developer,
        report_id=report_id,
        request=adopted_request,
    )
    assert adopted["commit_sha"] == promoted["commit_sha"]
    assert adopted["receipt_digest"] == promoted["receipt_digest"]
    adoption_records = coordinator._load_promotion_adoptions(
        assignment_id,
        response_id,
    )
    assert len(adoption_records) == 1
    assert adoption_records[0].report_id == report_id
    assert adoption_records[0].report_revision == 7
    assert adoption_records[0].lease_id == replacement_lease_id
    assert adoption_records[0].receipt_digest == promoted["receipt_digest"]
    active_lease = await store.get_active_lease(report_id)
    assert active_lease["lease_id"] == str(replacement_lease_id)
    assert active_lease["report_revision"] == 7

    response_payload = _developer_response_payload(
        response_id=response_id,
        channel_id=channel_id,
        report_id=report_id,
        report_revision=7,
        created_at=current,
    )
    response_payload["commit_sha"] = adopted["commit_sha"]
    submitted = await service.submit_developer_response(
        principal=developer,
        report_id=report_id,
        request=DeveloperResponseRequest(
            idempotency_key="promotion-service-response-0001",
            lease_id=replacement_lease_id,
            lease_owner_id=developer.sender_id,
            expected_report_revision=7,
            response={
                key: value
                for key, value in response_payload.items()
                if key
                not in {
                    "channel_id",
                    "report_id",
                    "report_revision",
                    "created_at",
                }
            },
        ),
    )
    assert submitted["response_id"] == str(response_id)
    assert submitted["commit_sha"] == adopted["commit_sha"]
    assert submitted["report_revision"] == 7
    durable_response = await store.get_developer_response(response_id)
    assert durable_response["report_revision"] == 7
    assert durable_response["commit_sha"] == adopted["commit_sha"]
