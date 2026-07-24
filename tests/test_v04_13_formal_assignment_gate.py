from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_platform.lilies_config import LiliesSettings
from agent_platform.formal_workspace import (
    FormalWorkspaceRejected,
    validate_public_formal_workspace,
)
from agent_platform.lilies_models import (
    ApplicationTargetMode,
    BuildAssignment,
    CollaborationAccess,
    CollaborationScope,
    PlatformAccess,
    PlatformScope,
)
from agent_platform.lilies_service import LocalLiliesService, build_local_lilies_core
from agent_platform.lilies_storage import LiliesAccessDeniedError
from agent_platform.task_packages import (
    WORKSPACE_MANIFEST_FILE,
    WORKSPACE_POLICY_FILE,
    TaskPackageConflict,
    TaskPackageManager,
    TaskPackageSecurityError,
    WorkspaceRole,
    formal_platform_scopes,
)
from tests.test_v04_13_lilies_assignment_intake import (
    BlockingProvider,
    assignment_payload,
    paired_client,
    platform_session,
    provision_for_assignment,
)
from tests.test_v04_13_task_packages import (
    _environment_secret_resolver,
    _make_task_source,
    _run_real_preflight,
)


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _formal_assignment(
    tmp_path: Path,
) -> tuple[TaskPackageManager, Any, Path, Path, BuildAssignment]:
    manager = TaskPackageManager(
        tmp_path / "task-state",
        environment_secret_resolver=_environment_secret_resolver,
    )
    package = manager.freeze_revision(_make_task_source(tmp_path / "task-source"))
    assignment_id = uuid4()
    run_id = f"run-formal-gate-{uuid4().hex}"
    ready_path, _ = _run_real_preflight(
        manager,
        package,
        run_id=run_id,
        assignment_id=assignment_id,
        environment_instance_id="environment:formal-assignment-gate",
    )

    seed = BuildAssignment.model_validate(assignment_payload())
    platform = PlatformAccess.model_validate(
        {
            **seed.platform.model_dump(mode="json"),
            "base_url": "http://paperless.local:8001",
            "credential_ref": f"credential.formal.platform.{uuid4().hex}",
            "scopes": [
                scope.value
                for scope in formal_platform_scopes(package.allowed_actions.platform_actions)
            ],
        }
    )
    created_at = datetime.now(timezone.utc)
    collaboration = CollaborationAccess(
        channel_id=uuid4(),
        credential_ref=f"credential.formal.collaboration.{uuid4().hex}",
        scopes=list(CollaborationScope),
        expires_at=created_at + timedelta(seconds=package.budget.assignment_wall_clock_seconds),
    )
    workspace = tmp_path / "manager-issued-lilies-workspace"
    manager.materialize_task_workspace(
        package,
        workspace,
        role=WorkspaceRole.lilies,
        run_id=run_id,
        assignment_id=assignment_id,
        environment_ready_path=ready_path,
    )
    assignment = manager.build_formal_assignment(
        package,
        ready_path=ready_path,
        workspace_manifest_path=workspace / WORKSPACE_MANIFEST_FILE,
        run_id=run_id,
        assignment_id=assignment_id,
        idempotency_key=f"formal-assignment:{uuid4().hex}",
        target=seed.target,
        platform=platform,
        collaboration=collaboration,
        created_at=created_at,
    )
    return manager, package, ready_path, workspace, assignment


async def _provision_formal_credentials(
    service: LocalLiliesService,
    client_id: str,
    assignment: BuildAssignment,
) -> None:
    await provision_for_assignment(
        service,
        client_id,
        assignment,
        scopes=[scope.value for scope in assignment.platform.scopes],
    )
    collaboration = assignment.collaboration
    assert collaboration is not None
    await service.storage.provision_credential(
        "collaboration_channel",
        "private-formal-collaboration-token-value",
        scopes=[scope.value for scope in collaboration.scopes],
        credential_ref=collaboration.credential_ref,
        client_id=client_id,
        assignment_id=str(assignment.assignment_id),
        expires_at=collaboration.expires_at,
    )


def test_manager_builds_formal_assignment_with_exact_frozen_run_binding(
    tmp_path: Path,
) -> None:
    _, package, ready_path, _, assignment = _formal_assignment(tmp_path)
    task_ref = assignment.task_package

    assert task_ref is not None
    assert task_ref.task_id == package.task.task_id
    assert task_ref.revision == package.task.revision
    assert task_ref.public_summary_digest == package.record.public_summary_digest
    assert task_ref.run_id is not None
    assert task_ref.environment_ready_digest == _digest(ready_path.read_bytes())
    assert assignment.constraints.max_turns == package.budget.max_build_repair_turns
    assert assignment.constraints.max_tool_calls == package.budget.max_platform_tool_calls
    assert assignment.constraints.no_substitute_validation is True
    assert [fixture.digest for fixture in assignment.fixture_refs or []] == [
        fixture.digest for fixture in package.fixtures.files
    ]


@pytest.mark.parametrize(
    "authority_drift",
    [
        "extra-scope",
        "shared-credential",
        "late-collaboration-expiry",
        "retarget-application",
    ],
)
def test_manager_reverse_authorization_rejects_caller_widened_authority(
    tmp_path: Path,
    authority_drift: str,
) -> None:
    manager, _, _, _, assignment = _formal_assignment(tmp_path)
    collaboration = assignment.collaboration
    assert collaboration is not None
    if authority_drift == "extra-scope":
        submitted = assignment.model_copy(
            update={
                "platform": assignment.platform.model_copy(
                    update={
                        "scopes": [
                            *assignment.platform.scopes,
                            PlatformScope.application_publish,
                        ]
                    }
                )
            }
        )
    elif authority_drift == "shared-credential":
        submitted = assignment.model_copy(
            update={
                "collaboration": collaboration.model_copy(
                    update={"credential_ref": assignment.platform.credential_ref}
                )
            }
        )
    elif authority_drift == "late-collaboration-expiry":
        submitted = assignment.model_copy(
            update={
                "collaboration": collaboration.model_copy(
                    update={"expires_at": assignment.constraints.deadline_at + timedelta(seconds=1)}
                )
            }
        )
    else:
        replacement = uuid4()
        submitted = assignment.model_copy(
            update={
                "target": assignment.target.model_copy(
                    update={
                        "mode": ApplicationTargetMode.existing,
                        "application_id": replacement,
                    }
                ),
                "platform": assignment.platform.model_copy(
                    update={"application_ids": [replacement]}
                ),
            }
        )

    with pytest.raises((TaskPackageConflict, TaskPackageSecurityError)):
        manager.authorize_formal_assignment(submitted)


@pytest.mark.parametrize(
    "drift",
    [
        "requirement",
        "max_turns",
        "environment_lock_digest",
        "allowed_actions_digest",
        "budget_digest",
        "environment_instance_id",
    ],
)
def test_public_formal_workspace_rejects_assignment_projection_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    _, _, _, workspace, assignment = _formal_assignment(tmp_path)
    if drift == "requirement":
        submitted = assignment.model_copy(
            update={"requirement": assignment.requirement + " Changed."}
        )
    elif drift == "max_turns":
        submitted = assignment.model_copy(
            update={
                "constraints": assignment.constraints.model_copy(
                    update={"max_turns": assignment.constraints.max_turns + 1}
                )
            }
        )
    else:
        task_ref = assignment.task_package
        assert task_ref is not None
        submitted = assignment.model_copy(
            update={
                "task_package": task_ref.model_copy(
                    update={
                        drift: (
                            "environment:forged-instance"
                            if drift == "environment_instance_id"
                            else "sha256:" + "f" * 64
                        )
                    }
                )
            }
        )

    with pytest.raises(FormalWorkspaceRejected):
        validate_public_formal_workspace(submitted, workspace)


def test_public_formal_workspace_rejects_symlink_root(
    tmp_path: Path,
) -> None:
    _, _, _, workspace, assignment = _formal_assignment(tmp_path)
    symlink = tmp_path / "workspace-symlink"
    symlink.symlink_to(workspace, target_is_directory=True)

    with pytest.raises(FormalWorkspaceRejected, match="real directory"):
        validate_public_formal_workspace(assignment, symlink)


@pytest.mark.parametrize(
    "tamper",
    [
        "extra-file",
        "symlink-file",
        "hardlink-file",
        "public-bytes",
        "reserved-directory",
    ],
)
def test_public_formal_workspace_rejects_physical_tree_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    _, _, _, workspace, assignment = _formal_assignment(tmp_path)
    if tamper == "extra-file":
        workspace.chmod(0o700)
        (workspace / "undeclared.txt").write_text(
            "not declared",
            encoding="utf-8",
        )
        workspace.chmod(0o500)
    elif tamper == "symlink-file":
        (workspace / "work" / "linked.txt").symlink_to(workspace / "requirement.md")
    elif tamper == "hardlink-file":
        os.link(
            workspace / "requirement.md",
            workspace / "work" / "hardlinked.txt",
        )
    elif tamper == "public-bytes":
        requirement = workspace / "requirement.md"
        requirement.chmod(0o600)
        requirement.write_text("tampered public requirement", encoding="utf-8")
        requirement.chmod(0o400)
    else:
        workspace.chmod(0o700)
        (workspace / ".git").mkdir()
        workspace.chmod(0o500)

    with pytest.raises(FormalWorkspaceRejected):
        validate_public_formal_workspace(assignment, workspace)


def test_public_formal_workspace_accepts_safe_outputs_across_revalidation(
    tmp_path: Path,
) -> None:
    _, _, _, workspace, assignment = _formal_assignment(tmp_path)
    nested = workspace / "work" / "reconciliation"
    nested.mkdir(mode=0o700)
    output = nested / "result.json"
    output.write_text('{"status":"in_progress"}', encoding="utf-8")
    output.chmod(0o600)
    artifact = workspace / "artifacts" / "trace.txt"
    artifact.write_text("public trace evidence", encoding="utf-8")
    artifact.chmod(0o600)

    initial = validate_public_formal_workspace(assignment, workspace)
    output.write_text('{"status":"completed"}', encoding="utf-8")
    output.chmod(0o600)

    assert validate_public_formal_workspace(assignment, workspace) == initial


@pytest.mark.parametrize("control", ["manifest", "policy"])
def test_public_formal_workspace_rejects_forged_control_even_if_assignment_digest_matches(
    tmp_path: Path,
    control: str,
) -> None:
    _, _, _, workspace, assignment = _formal_assignment(tmp_path)
    task_ref = assignment.task_package
    assert task_ref is not None
    if control == "policy":
        path = workspace / WORKSPACE_POLICY_FILE
        value = json.loads(path.read_bytes())
        value["writable_prefixes"].append("protected")
        path.chmod(0o600)
        path.write_bytes(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
        path.chmod(0o400)
        submitted_ref = task_ref.model_copy(
            update={"workspace_policy_digest": _digest(path.read_bytes())}
        )
    else:
        path = workspace / WORKSPACE_MANIFEST_FILE
        value = json.loads(path.read_bytes())
        value["assignment_id"] = str(uuid4())
        path.chmod(0o600)
        path.write_bytes(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
        path.chmod(0o400)
        submitted_ref = task_ref.model_copy(
            update={"workspace_mount_digest": _digest(path.read_bytes())}
        )
    submitted = assignment.model_copy(update={"task_package": submitted_ref})

    with pytest.raises(FormalWorkspaceRejected):
        validate_public_formal_workspace(submitted, workspace)


def test_repeated_lilies_materialization_has_the_same_content_address(
    tmp_path: Path,
) -> None:
    manager, package, ready_path, workspace, assignment = _formal_assignment(tmp_path)
    task_ref = assignment.task_package
    assert task_ref is not None
    assert task_ref.run_id is not None
    second = tmp_path / "second-manager-issued-workspace"
    manager.materialize_task_workspace(
        package,
        second,
        role=WorkspaceRole.lilies,
        run_id=task_ref.run_id,
        assignment_id=assignment.assignment_id,
        environment_ready_path=ready_path,
    )

    assert _digest((workspace / WORKSPACE_MANIFEST_FILE).read_bytes()) == _digest(
        (second / WORKSPACE_MANIFEST_FILE).read_bytes()
    )
    assert validate_public_formal_workspace(
        assignment,
        second,
    ) == validate_public_formal_workspace(assignment, workspace)


@pytest.mark.asyncio
async def test_production_core_uses_only_public_formal_workspace_gate(
    tmp_path: Path,
) -> None:
    _, _, _, staged_workspace, assignment = _formal_assignment(tmp_path)
    provider = BlockingProvider()
    settings = LiliesSettings(
        data_dir=tmp_path / "lilies",
        workspace_root=tmp_path / "workspaces",
        model="test",
    )
    assert "task_package_state_root" not in LiliesSettings.model_fields
    core = build_local_lilies_core(settings, provider=provider)
    service = core.service
    assert service._formal_assignment_authorizer is validate_public_formal_workspace
    await service.initialize()
    client = await paired_client(service.storage, service.settings)
    client_id = str(client["client_id"])
    session_id = await platform_session(service, client_id)
    await _provision_formal_credentials(service, client_id, assignment)
    workspace = service.settings.resolved_workspace_root / session_id
    workspace.rmdir()
    shutil.copytree(staged_workspace, workspace, copy_function=shutil.copy2)

    try:
        receipt = await service.submit_assignment(
            session_id,
            assignment,
            client_id=client_id,
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        assert receipt["status"] == "running"
        assert not any(
            marker in json.dumps(assignment.model_dump(mode="json"))
            for marker in ("protected/oracle", "task-package-state", "platform-data")
        )
    finally:
        await service.shutdown(reason="production_public_formal_gate_test")


@pytest.mark.asyncio
@pytest.mark.parametrize("authorizer_mode", ["missing", "rejecting"])
async def test_strict_formal_gate_rejects_without_persisting_or_starting_turn(
    tmp_path: Path,
    authorizer_mode: str,
) -> None:
    _, _, _, _, assignment = _formal_assignment(tmp_path)
    provider = BlockingProvider()

    def rejecting_authorizer(
        _assignment: BuildAssignment,
        _workspace: Path,
    ) -> dict[str, str]:
        raise RuntimeError("frozen binding rejected")

    service = LocalLiliesService(
        LiliesSettings(
            data_dir=tmp_path / "lilies",
            workspace_root=tmp_path / "workspaces",
            model="test",
        ),
        provider=provider,
        require_frozen_formal_assignments=True,
        formal_assignment_authorizer=(
            rejecting_authorizer if authorizer_mode == "rejecting" else None
        ),
    )
    await service.initialize()
    client = await paired_client(service.storage, service.settings)
    client_id = str(client["client_id"])
    session_id = await platform_session(service, client_id)
    await _provision_formal_credentials(service, client_id, assignment)

    with pytest.raises(LiliesAccessDeniedError, match="frozen task-package"):
        await service.submit_assignment(
            session_id,
            assignment,
            client_id=client_id,
        )

    session = await service.storage.get_session(session_id)
    assert session["status"] == "ready"
    assert session["assignment_id"] is None
    assert "formal_assignment_evidence" not in session["config"]
    assert await service.storage.list_turns(session_id) == []
    assert await service.storage.list_messages(session_id) == []
    assert provider.calls == 0
    workspace = service.settings.resolved_workspace_root / session_id
    assert not (workspace / WORKSPACE_MANIFEST_FILE).exists()
    assert not (workspace / WORKSPACE_POLICY_FILE).exists()


@pytest.mark.asyncio
async def test_formal_gate_rejects_well_formed_but_wrong_authorizer_evidence(
    tmp_path: Path,
) -> None:
    _, _, _, staged_workspace, assignment = _formal_assignment(tmp_path)
    provider = BlockingProvider()

    def mismatched_authorizer(
        submitted: BuildAssignment,
        workspace: Path,
    ) -> dict[str, str]:
        evidence = validate_public_formal_workspace(submitted, workspace)
        return {
            **evidence,
            "task_package_digest": "sha256:" + "e" * 64,
        }

    service = LocalLiliesService(
        LiliesSettings(
            data_dir=tmp_path / "lilies",
            workspace_root=tmp_path / "workspaces",
            model="test",
        ),
        provider=provider,
        require_frozen_formal_assignments=True,
        formal_assignment_authorizer=mismatched_authorizer,
    )
    await service.initialize()
    client = await paired_client(service.storage, service.settings)
    client_id = str(client["client_id"])
    session_id = await platform_session(service, client_id)
    await _provision_formal_credentials(service, client_id, assignment)
    workspace = service.settings.resolved_workspace_root / session_id
    workspace.rmdir()
    shutil.copytree(staged_workspace, workspace, copy_function=shutil.copy2)

    try:
        with pytest.raises(
            LiliesAccessDeniedError,
            match="differs from its frozen run binding",
        ):
            await service.submit_assignment(
                session_id,
                assignment,
                client_id=client_id,
            )
        session = await service.storage.get_session(session_id)
        assert session["assignment_id"] is None
        assert "formal_assignment_evidence" not in session["config"]
        assert await service.storage.list_turns(session_id) == []
        assert provider.calls == 0
    finally:
        await service.shutdown(reason="formal_wrong_evidence_test")


@pytest.mark.asyncio
async def test_authorized_formal_assignment_persists_receipt_and_policy_before_turn(
    tmp_path: Path,
) -> None:
    _, _, _, staged_workspace, assignment = _formal_assignment(tmp_path)
    provider = BlockingProvider()
    authorizer_calls: list[str] = []
    task_ref = assignment.task_package
    assert task_ref is not None
    assert task_ref.environment_ready_digest is not None
    assert task_ref.workspace_mount_digest is not None
    assert task_ref.workspace_policy_digest is not None
    expected_evidence = {
        "task_package_digest": task_ref.public_summary_digest,
        "environment_ready_digest": task_ref.environment_ready_digest,
        "workspace_mount_digest": task_ref.workspace_mount_digest,
        "workspace_policy_digest": task_ref.workspace_policy_digest,
    }

    def authorize(
        submitted: BuildAssignment,
        workspace: Path,
    ) -> dict[str, str]:
        assert submitted == assignment
        authorizer_calls.append(str(submitted.assignment_id))
        return validate_public_formal_workspace(submitted, workspace)

    service = LocalLiliesService(
        LiliesSettings(
            data_dir=tmp_path / "lilies",
            workspace_root=tmp_path / "workspaces",
            model="test",
        ),
        provider=provider,
        require_frozen_formal_assignments=True,
        formal_assignment_authorizer=authorize,
    )
    await service.initialize()
    client = await paired_client(service.storage, service.settings)
    client_id = str(client["client_id"])
    session_id = await platform_session(service, client_id)
    await _provision_formal_credentials(service, client_id, assignment)
    workspace = service.settings.resolved_workspace_root / session_id
    workspace.rmdir()
    shutil.copytree(staged_workspace, workspace, copy_function=shutil.copy2)

    try:
        receipt = await service.submit_assignment(
            session_id,
            assignment,
            client_id=client_id,
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        session = await service.storage.get_session(session_id)
        assert receipt["status"] == "running"
        assert authorizer_calls == [str(assignment.assignment_id)] * 2
        assert session["assignment_id"] == str(assignment.assignment_id)
        assert session["config"]["formal_assignment_evidence"] == expected_evidence
        assert (workspace / WORKSPACE_MANIFEST_FILE).is_file()
        assert (workspace / WORKSPACE_POLICY_FILE).is_file()
        assert len(await service.storage.list_turns(session_id)) == 1
    finally:
        await service.shutdown(reason="formal_gate_test")


@pytest.mark.asyncio
async def test_turn_start_rejects_workspace_changed_after_intake(
    tmp_path: Path,
) -> None:
    _, _, _, staged_workspace, assignment = _formal_assignment(tmp_path)
    provider = BlockingProvider()
    service = LocalLiliesService(
        LiliesSettings(
            data_dir=tmp_path / "lilies",
            workspace_root=tmp_path / "workspaces",
            model="test",
        ),
        provider=provider,
        require_frozen_formal_assignments=True,
        formal_assignment_authorizer=validate_public_formal_workspace,
    )
    await service.initialize()
    client = await paired_client(service.storage, service.settings)
    client_id = str(client["client_id"])
    session_id = await platform_session(service, client_id)
    await _provision_formal_credentials(service, client_id, assignment)
    workspace = service.settings.resolved_workspace_root / session_id
    workspace.rmdir()
    shutil.copytree(staged_workspace, workspace, copy_function=shutil.copy2)

    try:
        receipt = await service.submit_assignment(
            session_id,
            assignment,
            client_id=client_id,
        )
        policy = workspace / WORKSPACE_POLICY_FILE
        original = policy.read_bytes()
        policy.chmod(0o600)
        policy.write_bytes(original + b" ")
        policy.chmod(0o400)
        turn_id = str(receipt["turn_id"])
        for _ in range(100):
            turn = await service.storage.get_turn(turn_id)
            if turn["status"] == "error":
                break
            await asyncio.sleep(0.01)

        assert turn["status"] == "error"
        assert provider.calls == 0
    finally:
        await service.shutdown(reason="formal_workspace_mutation_test")


@pytest.mark.asyncio
async def test_strict_formal_gate_does_not_change_customer_assignment_intake(
    tmp_path: Path,
) -> None:
    provider = BlockingProvider()
    service = LocalLiliesService(
        LiliesSettings(
            data_dir=tmp_path / "lilies",
            workspace_root=tmp_path / "workspaces",
            model="test",
        ),
        provider=provider,
        require_frozen_formal_assignments=True,
    )
    await service.initialize()
    client = await paired_client(service.storage, service.settings)
    client_id = str(client["client_id"])
    session_id = await platform_session(service, client_id)
    assignment = BuildAssignment.model_validate(assignment_payload())
    await provision_for_assignment(service, client_id, assignment)

    try:
        receipt = await service.submit_assignment(
            session_id,
            assignment,
            client_id=client_id,
        )
        session = await service.storage.get_session(session_id)

        assert receipt["status"] == "running"
        assert session["assignment_id"] == str(assignment.assignment_id)
        assert "formal_assignment_evidence" not in session["config"]
        assert len(await service.storage.list_turns(session_id)) == 1
    finally:
        await service.shutdown(reason="customer_gate_regression_test")
