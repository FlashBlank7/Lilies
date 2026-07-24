from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.collaboration_models import (
    ChannelSettingsRequest,
    DeveloperLease,
    LeaseAcquireRequest,
    ReportSubmitRequest,
    SenderRole,
)
from agent_platform.collaboration_service import (
    CollaborationConflict,
    CollaborationNotFound,
    CollaborationPrincipal,
    CollaborationService,
)
from agent_platform.collaboration_storage import CollaborationStore
from agent_platform.config import Settings
from agent_platform.formal_assignment_broker import PrepareFormalAssignmentRequest
from agent_platform.formal_assignment_runtime import (
    FormalAssignmentRuntimeError,
    PlatformFormalAssignmentRuntime,
)
from agent_platform.lilies_models import CollaborationScope
from agent_platform.task_packages import TaskPackageManager
from tests.test_v04_13_assignment_bridge import DIGEST, platform_parts
from tests.test_v04_13_task_packages import (
    ATTESTATION_SECRET,
    _make_task_source,
    _real_health_endpoints,
)
from tests.test_v04_13_collaboration_service import _report_payload
from tests.test_runtime import ScriptedProvider


async def _runtime_parts(
    tmp_path: Path,
) -> tuple[
    PlatformFormalAssignmentRuntime,
    CollaborationService,
    object,
    object,
    list[asyncio.AbstractEventLoop],
]:
    storage, _, harness, _ = await platform_parts(tmp_path)
    runtime_holder: dict[str, PlatformFormalAssignmentRuntime] = {}

    async def developer_workspace(channel: object) -> object:
        resolved = await runtime_holder["runtime"].developer_workspace_for_channel(channel)
        return {
            "schema_version": resolved.schema_version,
            "task_id": resolved.task_id,
            "task_revision": resolved.task_revision,
            "run_id": resolved.run_id,
            "assignment_id": str(resolved.assignment_id),
            "path": resolved.workspace.path,
            "manifest_digest": resolved.workspace.manifest_digest,
            "policy_digest": resolved.workspace.policy_digest,
        }

    collaboration = CollaborationService(
        store=CollaborationStore(storage.db_path),
        enabled=True,
        developer_token="developer-token-formal-runtime-0001",
        verifier_token="verifier-token-formal-runtime-00001",
        developer_workspace_provider=developer_workspace,
    )
    await collaboration.initialize()
    owner_id = "formal-environment"
    await harness.save_secret(
        owner_id=owner_id,
        name="formal-environment-attestation",
        value=ATTESTATION_SECRET.decode("utf-8"),
        description="test formal environment attestation",
    )

    source = _make_task_source(tmp_path / "task-source")
    task_state = tmp_path / "sealed-task-state"
    package = TaskPackageManager(task_state).freeze_revision(source)
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def contract_digest(_scopes: object, _applications: object) -> str:
        observed_loops.append(asyncio.get_running_loop())
        return DIGEST

    developer_source = tmp_path / "platform-source"
    (developer_source / "backend/src/agent_platform").mkdir(parents=True)
    (developer_source / "backend/src/agent_platform/runtime.py").write_text(
        "FORMAL_RUNTIME = True\n",
        encoding="utf-8",
    )
    runtime = PlatformFormalAssignmentRuntime(
        task_state_root=task_state,
        broker_state_root=tmp_path / "formal-broker-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        platform_base_url="http://127.0.0.1:8001",
        contract_digest_provider=contract_digest,
        harness=harness,
        collaboration=collaboration,
        developer_source_root=developer_source,
        developer_workspace_root=tmp_path / "formal-developer-workspaces",
    )
    runtime_holder["runtime"] = runtime
    return runtime, collaboration, harness, package, observed_loops


def _request() -> PrepareFormalAssignmentRequest:
    return PrepareFormalAssignmentRequest(
        task_id="EXP-LILIES-TEST-001",
        revision=1,
        assignment_id=uuid4(),
        application_id=uuid4(),
        build_id=uuid4(),
        session_id=uuid4(),
        connection_id=uuid4(),
        environment_instance_id="environment:formal-runtime-001",
        idempotency_key=f"formal-runtime:{uuid4().hex}",
    )


@pytest.mark.asyncio
async def test_runtime_resolves_loop_bound_contract_secret_and_channel(
    tmp_path: Path,
) -> None:
    runtime, collaboration, harness, package, observed_loops = await _runtime_parts(tmp_path)
    request = _request()
    platform_loop = asyncio.get_running_loop()

    with _real_health_endpoints(
        package,
        attestation_secret=ATTESTATION_SECRET,
    ):
        prepared = await runtime.prepare_async(request)

    assert observed_loops == [platform_loop]
    assert prepared.assignment.platform.contract_digest == DIGEST
    assert prepared.assignment.platform.application_ids == [request.application_id]
    token = await runtime.collaboration_credential_secret(
        prepared.assignment,
        request.session_id,
    )
    access = prepared.assignment.collaboration
    assert access is not None
    principal = await collaboration.authenticate_lilies(
        token.get_secret_value(),
        channel_id=access.channel_id,
        required_scope=CollaborationScope.report_write.value,
    )
    assert principal.assignment_id == request.assignment_id
    assert principal.channel_id == access.channel_id
    secrets = await harness.list_secrets(
        owner_id=f"local-lilies-assignment:{request.assignment_id}"
    )
    assert len(secrets) == 1
    assert secrets[0]["name"] == "formal-collaboration-token"
    assert secrets[0]["encrypted"] is True


@pytest.mark.asyncio
async def test_runtime_restart_replays_without_rotating_authority(
    tmp_path: Path,
) -> None:
    runtime, collaboration, harness, package, observed_loops = await _runtime_parts(tmp_path)
    request = _request()
    with _real_health_endpoints(package):
        first = await runtime.prepare_async(request)
    first_token = await runtime.collaboration_credential_secret(
        first.assignment,
        request.session_id,
    )

    async def replay_contract(_scopes: object, _applications: object) -> str:
        raise AssertionError("frozen broker replay must not rerender the contract")

    restarted = PlatformFormalAssignmentRuntime(
        task_state_root=tmp_path / "sealed-task-state",
        broker_state_root=tmp_path / "formal-broker-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        platform_base_url="http://127.0.0.1:8001",
        contract_digest_provider=replay_contract,
        harness=harness,
        collaboration=collaboration,
        developer_source_root=tmp_path / "platform-source",
        developer_workspace_root=tmp_path / "formal-developer-workspaces",
    )
    replay = await restarted.prepare_async(request)
    replay_token = await restarted.collaboration_credential_secret(
        replay.assignment,
        request.session_id,
    )

    assert replay == first
    assert replay_token.get_secret_value() == first_token.get_secret_value()
    assert len(observed_loops) == 1


@pytest.mark.asyncio
async def test_runtime_close_revokes_channel_bearer_and_blocks_late_activation(
    tmp_path: Path,
) -> None:
    runtime, collaboration, harness, package, _ = await _runtime_parts(tmp_path)
    request = _request()
    with _real_health_endpoints(package):
        prepared = await runtime.prepare_async(request)
    token = await runtime.collaboration_credential_secret(
        prepared.assignment,
        request.session_id,
    )
    access = prepared.assignment.collaboration
    assert access is not None

    closed = await runtime.close_collaboration_authority(
        prepared.assignment,
        request.session_id,
    )
    replay = await runtime.close_collaboration_authority(
        prepared.assignment,
        request.session_id,
    )

    assert replay == closed
    with pytest.raises(CollaborationNotFound):
        await collaboration.authenticate_lilies(
            token.get_secret_value(),
            channel_id=access.channel_id,
            required_scope=CollaborationScope.report_write.value,
        )
    secret_owner = f"local-lilies-assignment:{request.assignment_id}"
    assert await harness.list_secrets(owner_id=secret_owner) == []
    with pytest.raises(
        FormalAssignmentRuntimeError,
        match="activation failed closed",
    ):
        await runtime.collaboration_credential_secret(
            prepared.assignment,
            request.session_id,
        )
    assert await harness.list_secrets(owner_id=secret_owner) == []


@pytest.mark.asyncio
async def test_runtime_pre_activation_close_tombstone_rejects_late_channel(
    tmp_path: Path,
) -> None:
    runtime, _, harness, package, _ = await _runtime_parts(tmp_path)
    request = _request()
    with _real_health_endpoints(package):
        prepared = await runtime.prepare_async(request)

    await runtime.close_collaboration_authority(
        prepared.assignment,
        request.session_id,
    )

    with pytest.raises(
        FormalAssignmentRuntimeError,
        match="activation failed closed",
    ):
        await runtime.collaboration_credential_secret(
            prepared.assignment,
            request.session_id,
        )
    assert (
        await harness.list_secrets(owner_id=f"local-lilies-assignment:{request.assignment_id}")
        == []
    )


@pytest.mark.asyncio
async def test_runtime_rejects_untrusted_environment_secret_reference(
    tmp_path: Path,
) -> None:
    runtime, _, harness, package, _ = await _runtime_parts(tmp_path)
    deleted = await harness.delete_secret(
        owner_id="formal-environment",
        name="formal-environment-attestation",
    )
    assert deleted is True

    with _real_health_endpoints(package):
        with pytest.raises(Exception):
            await runtime.prepare_async(_request())


@pytest.mark.asyncio
async def test_runtime_rejects_collaboration_session_substitution(
    tmp_path: Path,
) -> None:
    runtime, _, _, package, _ = await _runtime_parts(tmp_path)
    request = _request()
    with _real_health_endpoints(package):
        prepared = await runtime.prepare_async(request)

    with pytest.raises(FormalAssignmentRuntimeError):
        await runtime.collaboration_credential_secret(
            prepared.assignment,
            uuid4(),
        )


@pytest.mark.asyncio
async def test_developer_lease_returns_the_exact_private_source_workspace(
    tmp_path: Path,
) -> None:
    runtime, collaboration, _, package, _ = await _runtime_parts(tmp_path)
    request = _request()
    with _real_health_endpoints(package):
        prepared = await runtime.prepare_async(request)
    access = prepared.assignment.collaboration
    assert access is not None
    token = await runtime.collaboration_credential_secret(
        prepared.assignment,
        request.session_id,
    )
    lilies = await collaboration.authenticate_lilies(
        token.get_secret_value(),
        channel_id=access.channel_id,
        required_scope=CollaborationScope.report_write.value,
    )
    user = CollaborationPrincipal(
        role=SenderRole.user,
        sender_id="studio-user",
        scopes=frozenset(),
    )
    channel = await collaboration.store.get_channel(access.channel_id)
    await collaboration.set_channel_approval_mode(
        principal=user,
        channel_id=access.channel_id,
        request=ChannelSettingsRequest(
            idempotency_key="formal-runtime-auto-forward-0001",
            expected_channel_revision=int(channel["revision"]),
            approval_mode="auto_forward",
            confirmed=True,
        ),
    )
    channel = await collaboration.store.get_channel(access.channel_id)
    report = await collaboration.submit_report(
        principal=lilies,
        channel_id=access.channel_id,
        request=ReportSubmitRequest(
            idempotency_key="formal-runtime-report-0001",
            expected_channel_revision=int(channel["revision"]),
            report=_report_payload(),
        ),
    )
    developer = collaboration.authenticate_developer(
        "developer-token-formal-runtime-0001",
        required_scope="collaboration.developer",
    )
    lease_request = LeaseAcquireRequest(
        idempotency_key="formal-runtime-lease-0001",
        expected_report_revision=int(report["revision"]),
        owner_id=developer.sender_id,
        ttl_seconds=900,
    )
    leased = await collaboration.acquire_developer_lease(
        principal=developer,
        report_id=UUID(str(report["report_id"])),
        request=lease_request,
    )
    parsed = DeveloperLease.model_validate(leased)
    workspace = parsed.developer_workspace
    assert workspace is not None
    assert workspace.task_id == request.task_id
    assert workspace.task_revision == request.revision
    assert workspace.assignment_id == request.assignment_id
    assert workspace.run_id == prepared.run_id
    source = Path(workspace.path) / "source/backend/src/agent_platform/runtime.py"
    assert source.read_text(encoding="utf-8") == "FORMAL_RUNTIME = True\n"

    replay = await collaboration.acquire_developer_lease(
        principal=developer,
        report_id=UUID(str(report["report_id"])),
        request=lease_request,
    )
    assert DeveloperLease.model_validate(replay) == parsed

    forbidden = Path(workspace.path) / "source/protected"
    forbidden.mkdir()
    (forbidden / "oracle.json").write_text("not allowed\n", encoding="utf-8")
    with pytest.raises(
        CollaborationConflict,
        match="could not be verified",
    ):
        await collaboration.acquire_developer_lease(
            principal=developer,
            report_id=UUID(str(report["report_id"])),
            request=lease_request,
        )


def test_production_services_install_the_formal_runtime_on_the_api_path(
    tmp_path: Path,
) -> None:
    api_token = "platform-formal-runtime-token"
    configured = Settings(
        api_token=api_token,
        data_dir=tmp_path / "platform-data",
        workspace_root=tmp_path / "platform-workspaces",
        lilies_local_agent_enabled=True,
        lilies_collaboration_enabled=True,
        lilies_collaboration_developer_token="formal-developer-token",
        lilies_collaboration_verifier_token="formal-verifier-token",
        scheduler_poll_seconds=3_600,
    )
    app = create_app(configured, ScriptedProvider())

    with TestClient(app) as client:
        bridge = client.app.state.services.local_lilies_bridge
        assert isinstance(
            bridge.formal_assignment_broker,
            PlatformFormalAssignmentRuntime,
        )
        assert bridge.formal_credential_secret_provider is not None
        assert bridge.formal_channel_close_provider is not None
        response = client.post(
            f"/api/v1/local-lilies/applications/{uuid4()}/formal-builds",
            headers={"Authorization": f"Bearer {api_token}"},
            json={
                "idempotency_key": "production-formal-runtime-0001",
                "connection_id": str(uuid4()),
                "task_id": "EXP-LILIES-TEST-001",
                "revision": 1,
                "environment_instance_id": "environment:production-formal-001",
                "user_notified": True,
            },
        )

    assert response.status_code == 404
    assert "not configured" not in response.text
