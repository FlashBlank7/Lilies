from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import SecretStr, ValidationError

from agent_platform.collaboration_models import (
    ChannelStatus,
    CollaborationChannel,
)
from agent_platform.collaboration_storage import CollaborationStore
from agent_platform.connector_sdk import ConnectorService
from agent_platform.formal_assignment_broker import (
    FormalAssignmentBroker,
    PrepareFormalAssignmentRequest,
)
from agent_platform.formal_run_archiver import FormalRunArchiveCoordinator
from agent_platform.lilies_models import (
    AssignmentMode,
    BuildAssignment,
    CollaborationAccess,
    CollaborationScope,
    FormalWorkspaceStagingReceipt,
    FormalWorkspaceStagingRequest,
    PlatformAccess,
    PlatformScope,
    formal_assignment_digest,
)
from agent_platform.local_lilies_bridge import (
    BridgeAssignmentPhase,
    LocalLiliesBridge,
    LocalLiliesBridgeConflict,
    LocalLiliesBridgeDaemonRejected,
    LocalLiliesBridgeSecurityError,
    LocalLiliesBridgeStore,
    LocalLiliesBridgeUnavailable,
    StartFormalLocalLiliesBuildRequest,
    _safe_daemon_rejection_details,
)
from agent_platform.local_lilies_client import LocalLiliesRemoteError
from agent_platform.platform_blackbox_artifacts import PlatformBlackboxArtifactStore
from agent_platform.platform_blackbox_auth import PlatformBlackboxAuthStore
from agent_platform.platform_harness import PlatformHarness
from agent_platform.task_packages import (
    ArchiveStatus,
    TaskPackageConflict,
    TaskPackageManager,
)
from agent_platform.workflow_storage import WorkflowStorage
from tests.test_v04_13_assignment_bridge import (
    DIGEST,
    CrashOnce,
    FakeDaemonClient,
    InjectedCrash,
    empty_application,
    pair,
    platform_parts,
)
from tests.test_v04_13_task_packages import (
    _environment_secret_resolver,
    _make_task_source,
    _real_health_endpoints,
)


@dataclass
class FormalProviders:
    secret_calls: int = 0
    last_secret_session_id: UUID | None = None
    close_calls: int = 0
    close_receipt: CollaborationChannel | None = None

    @staticmethod
    def platform(
        request: PrepareFormalAssignmentRequest,
        scopes: tuple[PlatformScope, ...],
        _allowed_actions: object,
    ) -> PlatformAccess:
        credential_id = uuid5(
            NAMESPACE_URL,
            f"lilies:platform-task-credential:{request.assignment_id}",
        )
        return PlatformAccess(
            base_url="http://127.0.0.1:8001",
            contract_url="/api/v1/lilies/platform-contract",
            contract_digest=DIGEST,
            credential_ref=f"platform-task-credential:{credential_id}",
            scopes=list(scopes),
            application_ids=[request.application_id],
        )

    @staticmethod
    def collaboration(
        request: PrepareFormalAssignmentRequest,
        expires_at: datetime,
    ) -> CollaborationAccess:
        channel_id = uuid5(
            NAMESPACE_URL,
            f"lilies:formal-channel:{request.assignment_id}:{request.session_id}",
        )
        return CollaborationAccess(
            channel_id=channel_id,
            credential_ref=f"collaboration_{channel_id.hex}",
            scopes=list(CollaborationScope),
            expires_at=expires_at,
        )

    async def secret(
        self,
        assignment: BuildAssignment,
        session_id: UUID,
    ) -> SecretStr:
        self.secret_calls += 1
        self.last_secret_session_id = session_id
        access = assignment.collaboration
        assert access is not None
        expected_channel_id = uuid5(
            NAMESPACE_URL,
            f"lilies:formal-channel:{assignment.assignment_id}:{session_id}",
        )
        assert access.channel_id == expected_channel_id
        assert access.credential_ref == f"collaboration_{expected_channel_id.hex}"
        bearer = hashlib.sha256(
            f"formal-channel:{assignment.assignment_id}:{session_id}".encode()
        ).hexdigest()
        return SecretStr(f"collaboration_{bearer}")

    async def close(
        self,
        assignment: BuildAssignment,
        session_id: UUID,
    ) -> CollaborationChannel:
        self.close_calls += 1
        access = assignment.collaboration
        task_ref = assignment.task_package
        assert access is not None
        assert task_ref is not None
        if self.close_receipt is None:
            closed_at = datetime.now(timezone.utc)
            self.close_receipt = CollaborationChannel(
                channel_id=access.channel_id,
                task_id=task_ref.task_id,
                task_revision=task_ref.revision,
                assignment_id=assignment.assignment_id,
                lilies_session_id=session_id,
                application_ids=assignment.platform.application_ids,
                status=ChannelStatus.closed,
                revision=2,
                next_seq=1,
                created_at=assignment.created_at,
                closed_at=closed_at,
                retention_until=closed_at,
            )
        return self.close_receipt


class FormalDaemonClient(FakeDaemonClient):
    def __init__(self) -> None:
        super().__init__()
        self.staging_receipts: dict[str, dict[str, Any]] = {}
        self.staging_requests: dict[str, FormalWorkspaceStagingRequest] = {}
        self.staging_calls = 0
        self.staging_side_effects = 0
        self.submitted_assignments: list[BuildAssignment] = []

    async def stage_formal_workspace(
        self,
        _: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        request = FormalWorkspaceStagingRequest.model_validate(payload)
        self.staging_calls += 1
        prior = self.staging_receipts.get(request.idempotency_key)
        if prior is not None:
            assert self.staging_requests[request.idempotency_key] == request
            return {**prior, "replayed": True}
        self.staging_side_effects += 1
        receipt = FormalWorkspaceStagingReceipt(
            session_id=UUID(session_id),
            idempotency_key=request.idempotency_key,
            assignment_id=request.assignment_id,
            assignment_digest=request.assignment_digest,
            task_package_digest=request.task_package_digest,
            workspace_mount_digest=request.workspace_mount_digest,
            workspace_policy_digest=request.workspace_policy_digest,
            bundle_digest=request.bundle.bundle_digest,
            file_count=len(request.bundle.entries),
            total_bytes=sum(entry.size_bytes for entry in request.bundle.entries),
            staged_at=datetime.now(timezone.utc),
        ).model_dump(mode="json")
        self.staging_requests[request.idempotency_key] = request
        self.staging_receipts[request.idempotency_key] = receipt
        return dict(receipt)

    async def submit_assignment(
        self,
        _: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        assignment = BuildAssignment.model_validate(payload)
        assert assignment.mode is AssignmentMode.formal_experiment
        assert assignment.collaboration is not None
        assert assignment.platform.credential_ref in self.credentials
        assert assignment.collaboration.credential_ref in self.credentials
        staging = next(
            request
            for request in self.staging_requests.values()
            if request.assignment_id == assignment.assignment_id
        )
        assert staging.assignment_digest == formal_assignment_digest(assignment)
        self.submitted_assignments.append(assignment)
        self.assignment_calls += 1
        prior = self.submission_receipts.get(assignment.idempotency_key)
        if prior is not None:
            return {**prior, "replayed": True}
        self.assignment_side_effects += 1
        now = datetime.now(timezone.utc).isoformat()
        receipt = {
            "schema_version": "1.0",
            "assignment_id": str(assignment.assignment_id),
            "session_id": session_id,
            "turn_id": str(uuid5(NAMESPACE_URL, f"formal-turn:{assignment.assignment_id}")),
            "start_message_id": str(
                uuid5(NAMESPACE_URL, f"formal-message:{assignment.assignment_id}")
            ),
            "status": "running",
            "event_cursor": 1,
            "accepted_at": now,
            "replayed": False,
        }
        self.submission_receipts[assignment.idempotency_key] = receipt
        self.sessions[session_id].update(
            {
                "status": "running",
                "assignment_id": str(assignment.assignment_id),
                "updated_at": now,
            }
        )
        return dict(receipt)


class RejectSubmissionOnceFormalDaemon(FormalDaemonClient):
    def __init__(self) -> None:
        super().__init__()
        self.reject_submission = True

    async def submit_assignment(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.reject_submission:
            raise LocalLiliesRemoteError(
                422,
                "public assignment schema is temporarily incompatible",
                {
                    "detail": [
                        {
                            "type": "missing",
                            "loc": ["body", "constraints", "deadline_at"],
                            "msg": "SENSITIVE prose must not cross the bridge",
                            "input": {"SENSITIVE": "value"},
                        }
                    ]
                },
            )
        return await super().submit_assignment(
            base_url,
            access_token,
            session_id,
            payload,
        )


class RejectStagingOnceFormalDaemon(FormalDaemonClient):
    def __init__(self) -> None:
        super().__init__()
        self.reject_staging = True

    async def stage_formal_workspace(
        self,
        base_url: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.reject_staging:
            raise LocalLiliesRemoteError(
                404,
                "formal workspace staging is temporarily unavailable",
            )
        return await super().stage_formal_workspace(
            base_url,
            access_token,
            session_id,
            payload,
        )


class RejectingFormalBroker:
    @staticmethod
    def prepare(_: PrepareFormalAssignmentRequest) -> Any:
        raise RuntimeError("sealed revision is unavailable")


def formal_request(connection_id: UUID) -> StartFormalLocalLiliesBuildRequest:
    return StartFormalLocalLiliesBuildRequest(
        idempotency_key="formal-build-request-000001",
        connection_id=connection_id,
        task_id="EXP-LILIES-TEST-001",
        revision=1,
        environment_instance_id="environment:paperless-formal-001",
        user_notified=True,
    )


def make_bridge(
    tmp_path: Path,
    *,
    workflow: WorkflowStorage,
    harness: PlatformHarness,
    auth: PlatformBlackboxAuthStore,
    daemon: FormalDaemonClient,
    broker: FormalAssignmentBroker,
    providers: FormalProviders,
    fault_hook: Any = None,
) -> LocalLiliesBridge:
    return LocalLiliesBridge(
        enabled=True,
        store=LocalLiliesBridgeStore(tmp_path / "platform" / "local-lilies-bridge.db"),
        workflow_storage=workflow,
        harness=harness,
        auth_store=auth,
        client=daemon,
        platform_base_url="http://127.0.0.1:8001",
        contract_digest_provider=lambda _scopes, _apps, _actions: DIGEST,
        formal_assignment_broker=broker,
        formal_credential_secret_provider=providers.secret,
        formal_channel_close_provider=providers.close,
        fault_hook=fault_hook,
    )


def make_broker(
    tmp_path: Path,
    providers: FormalProviders,
) -> tuple[FormalAssignmentBroker, Any]:
    task_state = tmp_path / "sealed-task-state"
    manager = TaskPackageManager(task_state)
    package = manager.freeze_revision(_make_task_source(tmp_path / "formal-task-source"))
    return (
        FormalAssignmentBroker(
            task_state_root=task_state,
            broker_state_root=tmp_path / "formal-broker-state",
            public_workspace_root=tmp_path / "formal-public-workspaces",
            platform_access_provider=providers.platform,
            collaboration_access_provider=providers.collaboration,
            environment_secret_resolver=_environment_secret_resolver,
        ),
        package,
    )


async def make_terminal_archive_coordinator(
    tmp_path: Path,
    *,
    storage: Any,
    workflow: WorkflowStorage,
    auth: PlatformBlackboxAuthStore,
    bridge: LocalLiliesBridge,
) -> FormalRunArchiveCoordinator:
    collaboration = CollaborationStore(storage.db_path)
    await collaboration.initialize()
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    connectors = ConnectorService(storage=storage, harness=bridge.harness)
    await connectors.initialize()
    return FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge.store,
        collaboration_store=collaboration,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=auth,
        connector_service=connectors,
    )


def test_formal_start_request_cannot_accept_authority_or_skip_notice() -> None:
    request = {
        "idempotency_key": "formal-build-request-000001",
        "connection_id": "44a1c188-d1ff-4ec2-a92e-2ad29be1a001",
        "task_id": "EXP-LILIES-TEST-001",
        "revision": 1,
        "environment_instance_id": "environment:paperless-formal-001",
        "user_notified": True,
    }
    for forbidden in (
        "requirement",
        "actions",
        "budgets",
        "scopes",
        "oracle",
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            StartFormalLocalLiliesBuildRequest.model_validate(
                {**request, forbidden: "caller authority is forbidden"}
            )
    with pytest.raises(ValidationError):
        StartFormalLocalLiliesBuildRequest.model_validate({**request, "user_notified": False})


@pytest.mark.asyncio
async def test_formal_bridge_runs_prepared_assignment_through_exact_staging(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, package = make_broker(tmp_path, providers)
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "formal-e2e")

    with _real_health_endpoints(package):
        result = await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )

    assert result.phase is BridgeAssignmentPhase.running
    assert daemon.staging_side_effects == 1
    assert daemon.assignment_side_effects == 1
    assert len(daemon.credentials) == 2
    assignment = daemon.submitted_assignments[-1]
    assert assignment.mode is AssignmentMode.formal_experiment
    assert assignment.target.application_id == application_id
    assert assignment.task_package is not None
    assert assignment.task_package.task_id == "EXP-LILIES-TEST-001"
    credential = await auth.get_credential(assignment.platform.credential_ref)
    assert credential.allowed_operations == sorted(
        assignment.constraints.allowed_actions,
        key=lambda action: action.value,
    )
    assert (
        credential.allowed_actions_digest
        == assignment.task_package.allowed_actions_digest
    )
    assert credential.budget_digest == assignment.task_package.budget_digest
    assert credential.max_write_count == assignment.constraints.max_write_count
    assert credential.max_payload_bytes == assignment.constraints.max_payload_bytes
    assert (
        credential.max_report_evidence_rounds
        == assignment.constraints.max_report_evidence_rounds
    )
    assert credential.stable_hidden_runs == assignment.constraints.stable_hidden_runs
    assert providers.last_secret_session_id == result.session_id
    persisted = await bridge.store.get_assignment(result.assignment_id)
    assert persisted["assignment_mode"] == AssignmentMode.formal_experiment.value
    assert persisted["formal_workspace_receipt_json"]
    assert persisted["collaboration_credential_ref"] == (
        assignment.collaboration.credential_ref if assignment.collaboration is not None else None
    )


@pytest.mark.asyncio
async def test_explicit_resume_retries_same_unbound_preacceptance_assignment(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = RejectSubmissionOnceFormalDaemon()
    broker, package = make_broker(tmp_path, providers)
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(
        workflow,
        "formal-preacceptance-resume",
    )

    with _real_health_endpoints(package):
        with pytest.raises(LocalLiliesBridgeDaemonRejected) as captured:
            await bridge.start_formal_build(
                application_id,
                formal_request(connection.connection_id),
            )
        assert captured.value.details["daemon_status_code"] == 422
        assert captured.value.details["validation_issues"] == [
            {
                "location": ["body", "constraints", "deadline_at"],
                "type": "missing",
            }
        ]
        assert "SENSITIVE" not in json.dumps(captured.value.public_detail())
        failed = (
            await bridge.store.list_assignments_for_application(application_id)
        )[0]
        assert failed["phase"] == "error"
        assert failed["last_error_code"] == "daemon_rejected"
        assert failed["submission_json"] is not None
        assert daemon.sessions[failed["session_id"]]["status"] == "ready"
        assert daemon.sessions[failed["session_id"]]["assignment_id"] is None

        await bridge.store.update_assignment(
            failed["assignment_id"],
            last_error_code=None,
            last_error_message=None,
        )
        daemon.reject_submission = False
        recovered = await bridge.resume_assignment(failed["assignment_id"])

    assert recovered.assignment_id == UUID(failed["assignment_id"])
    assert recovered.session_id == UUID(failed["session_id"])
    assert recovered.phase is BridgeAssignmentPhase.running
    assert daemon.assignment_side_effects == 1
    assert len(daemon.submitted_assignments) == 1


def test_daemon_rejection_projection_omits_unstructured_sensitive_payload() -> None:
    projected = _safe_daemon_rejection_details(
        LocalLiliesRemoteError(
            409,
            "SENSITIVE daemon message",
            {"detail": "SENSITIVE unstructured detail"},
        )
    )

    assert projected == {"daemon_status_code": 409}


@pytest.mark.asyncio
async def test_explicit_resume_retries_workspace_before_submission(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = RejectStagingOnceFormalDaemon()
    broker, package = make_broker(tmp_path, providers)
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(
        workflow,
        "formal-workspace-resume",
    )

    with _real_health_endpoints(package):
        with pytest.raises(LocalLiliesBridgeDaemonRejected):
            await bridge.start_formal_build(
                application_id,
                formal_request(connection.connection_id),
            )
        failed = (
            await bridge.store.list_assignments_for_application(application_id)
        )[0]
        assert failed["phase"] == "error"
        assert failed["formal_workspace_receipt_json"] is None
        assert daemon.sessions[failed["session_id"]]["status"] == "ready"
        assert daemon.sessions[failed["session_id"]]["assignment_id"] is None

        daemon.reject_staging = False
        recovered = await bridge.resume_assignment(failed["assignment_id"])

    assert recovered.phase is BridgeAssignmentPhase.running
    assert daemon.staging_side_effects == 1
    assert daemon.assignment_side_effects == 1
    assert len(daemon.submitted_assignments) == 1


@pytest.mark.asyncio
async def test_formal_prepare_rejection_is_persisted_instead_of_left_recorded(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=RejectingFormalBroker(),  # type: ignore[arg-type]
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "formal-rejected")

    with pytest.raises(
        LocalLiliesBridgeSecurityError,
        match="preparation was rejected",
    ):
        await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )

    row = (await bridge.store.list_assignments_for_application(application_id))[0]
    assert row["phase"] == "error"
    assert row["status"] == "failed"
    assert row["last_error_code"] == "formal_preparation_rejected"

    second_request = formal_request(connection.connection_id).model_copy(
        update={"idempotency_key": "formal-build-request-000002"}
    )
    with pytest.raises(LocalLiliesBridgeSecurityError):
        await bridge.start_formal_build(application_id, second_request)
    rows = await bridge.store.list_assignments_for_application(application_id)
    assert len(rows) == 2
    assert rows[0]["assignment_id"] != rows[1]["assignment_id"]

    cancelled = await bridge.cancel_assignment(
        row["assignment_id"],
        idempotency_key="formal-rejected-cancel-0001",
    )
    assert cancelled.phase is BridgeAssignmentPhase.cancelled


@pytest.mark.asyncio
async def test_formal_empty_application_rejection_recovers_reserved_archive_without_daemon(
    tmp_path: Path,
) -> None:
    storage, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, _ = make_broker(tmp_path, providers)
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "formal-non-empty")
    draft = await workflow.get_draft(str(application_id))
    await workflow.save_draft(
        str(application_id),
        draft["snapshot"],
        expected_revision=0,
        idempotency_key="formal-non-empty-draft-0001",
    )

    coordinator = await make_terminal_archive_coordinator(
        tmp_path,
        storage=storage,
        workflow=workflow,
        auth=auth,
        bridge=bridge,
    )
    bridge.formal_terminal_archive_provider = coordinator.archive_terminal_assignment
    with pytest.raises(
        LocalLiliesBridgeConflict,
        match="only start from an empty application",
    ):
        await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )

    rejected = (
        await bridge.store.list_assignments_for_application(application_id)
    )[0]
    assert rejected["phase"] == "error"
    assert rejected["last_error_code"] == "application_not_empty"
    assert rejected["submission_json"] is None
    assert rejected["daemon_status"] is None
    assert rejected["terminal_events_drained_at"] is not None
    assert daemon.sessions == {}

    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    restarted.formal_terminal_archive_provider = (
        coordinator.archive_terminal_assignment
    )
    await restarted.initialize()
    recovered = await restarted.recover_pending_assignments()
    persisted = await restarted.store.get_assignment(rejected["assignment_id"])
    archived = await coordinator.archive_terminal_assignment(
        UUID(rejected["assignment_id"])
    )

    assert recovered.scanned == 1
    assert recovered.failed == 1
    assert persisted["formal_terminal_archive_completed_at"] is not None
    assert archived is not None
    assert archived.status is ArchiveStatus.failed
    assert coordinator.replay(archived).source_status is ArchiveStatus.failed
    assert daemon.sessions == {}
    assert (await restarted.recover_pending_assignments()).scanned == 0


@pytest.mark.asyncio
async def test_formal_broker_rejection_recovers_reserved_archive_without_daemon(
    tmp_path: Path,
) -> None:
    storage, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    _, _ = make_broker(tmp_path, providers)
    rejecting_broker = RejectingFormalBroker()
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=rejecting_broker,  # type: ignore[arg-type]
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "formal-broker-rejected")
    coordinator = await make_terminal_archive_coordinator(
        tmp_path,
        storage=storage,
        workflow=workflow,
        auth=auth,
        bridge=bridge,
    )
    bridge.formal_terminal_archive_provider = coordinator.archive_terminal_assignment

    with pytest.raises(
        LocalLiliesBridgeSecurityError,
        match="preparation was rejected",
    ):
        await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )

    rejected = (
        await bridge.store.list_assignments_for_application(application_id)
    )[0]
    assert rejected["phase"] == "error"
    assert rejected["last_error_code"] == "formal_preparation_rejected"
    assert rejected["submission_json"] is None
    assert rejected["daemon_status"] is None
    assert rejected["terminal_events_drained_at"] is not None
    assert daemon.sessions == {}

    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=rejecting_broker,  # type: ignore[arg-type]
        providers=providers,
    )
    restarted.formal_terminal_archive_provider = (
        coordinator.archive_terminal_assignment
    )
    await restarted.initialize()
    recovered = await restarted.recover_pending_assignments()
    persisted = await restarted.store.get_assignment(rejected["assignment_id"])
    archived = await coordinator.archive_terminal_assignment(
        UUID(rejected["assignment_id"])
    )

    assert recovered.scanned == 1
    assert recovered.failed == 1
    assert persisted["formal_terminal_archive_completed_at"] is not None
    assert archived is not None
    assert archived.status is ArchiveStatus.failed
    assert coordinator.replay(archived).source_status is ArchiveStatus.failed
    assert daemon.sessions == {}
    assert (await restarted.recover_pending_assignments()).scanned == 0


@pytest.mark.asyncio
async def test_formal_reprobe_broker_rejection_recovers_reserved_archive(
    tmp_path: Path,
) -> None:
    storage, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, _ = make_broker(tmp_path, providers)
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "formal-reprobe-rejected")
    request = formal_request(connection.connection_id)

    with pytest.raises(LocalLiliesBridgeUnavailable, match="may be re-probed"):
        await bridge.start_formal_build(application_id, request)
    transient = await bridge.store.get_assignment(
        (
            await bridge.store.list_assignments_for_application(application_id)
        )[0]["assignment_id"]
    )
    assert transient["phase"] == "recorded"
    assert transient["daemon_session_creation_started_at"] is None

    bridge.formal_assignment_broker = RejectingFormalBroker()  # type: ignore[assignment]
    with pytest.raises(
        LocalLiliesBridgeSecurityError,
        match="preparation was rejected",
    ):
        await bridge.start_formal_build(application_id, request)
    rejected = await bridge.store.get_assignment(transient["assignment_id"])
    assert rejected["phase"] == "error"
    assert rejected["submission_json"] is None
    assert rejected["daemon_session_creation_started_at"] is None
    assert rejected["terminal_events_drained_at"] is not None
    assert daemon.sessions == {}

    coordinator = await make_terminal_archive_coordinator(
        tmp_path,
        storage=storage,
        workflow=workflow,
        auth=auth,
        bridge=bridge,
    )
    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=RejectingFormalBroker(),  # type: ignore[arg-type]
        providers=providers,
    )
    restarted.formal_terminal_archive_provider = (
        coordinator.archive_terminal_assignment
    )
    await restarted.initialize()
    recovery = await restarted.recover_pending_assignments()
    persisted = await restarted.store.get_assignment(rejected["assignment_id"])
    archived = await coordinator.archive_terminal_assignment(
        UUID(rejected["assignment_id"])
    )

    assert recovery.scanned == 1
    assert recovery.failed == 1
    assert persisted["formal_terminal_archive_completed_at"] is not None
    assert archived is not None
    assert archived.status is ArchiveStatus.failed
    run_root = (
        tmp_path
        / "sealed-task-state"
        / "packages"
        / archived.task_id
        / str(archived.revision)
        / "runs"
        / archived.run_id
    )
    reservation = json.loads(
        (run_root / "reserved-assignment.json").read_text()
    )
    scan = json.loads((run_root / "preassignment-scan.json").read_text())
    assert reservation["preparation_state"] == "request_reserved"
    assert reservation["manager_prepared_assignment_digest"] is None
    assert reservation["daemon_assignment_delivery"] == "not_started"
    assert not (run_root / "manager-prepared-assignment.json").exists()
    assert scan["reason"] == "build_assignment_not_issued"
    assert (await restarted.recover_pending_assignments()).scanned == 0


@pytest.mark.asyncio
async def test_formal_reprobe_changed_application_archives_pre_daemon_assignment(
    tmp_path: Path,
) -> None:
    storage, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, package = make_broker(tmp_path, providers)
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "formal-reprobe-changed")
    request = formal_request(connection.connection_id)

    with pytest.raises(LocalLiliesBridgeUnavailable, match="may be re-probed"):
        await bridge.start_formal_build(application_id, request)
    transient = (
        await bridge.store.list_assignments_for_application(application_id)
    )[0]
    draft = await workflow.get_draft(str(application_id))
    await workflow.save_draft(
        str(application_id),
        draft["snapshot"],
        expected_revision=0,
        idempotency_key="formal-reprobe-changed-draft-0001",
    )

    with _real_health_endpoints(package):
        with pytest.raises(
            LocalLiliesBridgeConflict,
            match="application is still empty",
        ):
            await bridge.start_formal_build(application_id, request)
    rejected = await bridge.store.get_assignment(transient["assignment_id"])
    assert rejected["phase"] == "error"
    assert rejected["submission_json"] is not None
    assert rejected["daemon_session_creation_started_at"] is None
    assert rejected["daemon_status"] is None
    assert rejected["terminal_events_drained_at"] is not None
    assert daemon.sessions == {}
    assert daemon.staging_calls == 0
    assert daemon.staging_side_effects == 0
    assert daemon.submitted_assignments == []

    coordinator = await make_terminal_archive_coordinator(
        tmp_path,
        storage=storage,
        workflow=workflow,
        auth=auth,
        bridge=bridge,
    )
    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    restarted.formal_terminal_archive_provider = (
        coordinator.archive_terminal_assignment
    )
    await restarted.initialize()
    recovery = await restarted.recover_pending_assignments()
    persisted = await restarted.store.get_assignment(rejected["assignment_id"])
    archived = await coordinator.archive_terminal_assignment(
        UUID(rejected["assignment_id"])
    )

    assert recovery.scanned == 1
    assert recovery.failed == 1
    assert persisted["formal_terminal_archive_completed_at"] is not None
    assert archived is not None
    assert archived.status is ArchiveStatus.failed
    replay = coordinator.replay(archived)
    assert replay.forbidden_assistance_findings == [
        "scanner_inconclusive:pre_daemon:"
        "assignment_not_delivered_to_daemon"
    ]
    assert "manager-prepared-assignment.json" in {
        entry.path for entry in replay.files
    }
    run_root = (
        tmp_path
        / "sealed-task-state"
        / "packages"
        / archived.task_id
        / str(archived.revision)
        / "runs"
        / archived.run_id
    )
    prepared_payload = rejected["submission_json"].encode("utf-8")
    prepared_path = run_root / "manager-prepared-assignment.json"
    reservation = json.loads(
        (run_root / "reserved-assignment.json").read_text()
    )
    scan = json.loads((run_root / "preassignment-scan.json").read_text())
    bridge_export = json.loads(
        (run_root / "scanner-inputs/bridge.json").read_text()
    )
    collaboration_export = json.loads(
        (run_root / "scanner-inputs/collaboration.json").read_text()
    )
    blackbox_export = json.loads(
        (run_root / "scanner-inputs/blackbox-auth.json").read_text()
    )
    source_export = json.loads(
        (run_root / "source-provenance/manifest.json").read_text()
    )
    assert prepared_path.read_bytes() == prepared_payload
    assert reservation["preparation_state"] == "manager_prepared"
    assert reservation["manager_prepared_assignment_digest"] == (
        f"sha256:{hashlib.sha256(prepared_payload).hexdigest()}"
    )
    assert reservation["daemon_assignment_delivery"] == "not_started"
    assert reservation["daemon_session_creation_started_at"] is None
    assert reservation["daemon_status"] is None
    assert reservation["relay_cursor"] == 0
    assert reservation["ack_cursor"] == 0
    assert reservation["daemon_event_count"] == 0
    assert reservation["credential_ref"] is None
    assert reservation["collaboration_credential_ref"] is None
    assert reservation["formal_workspace_receipt_json"] is None
    assert scan["reason"] == "assignment_not_delivered_to_daemon"
    assert bridge_export["assignment"]["submission_json"] == rejected[
        "submission_json"
    ]
    assert (
        bridge_export["assignment"]["daemon_session_creation_started_at"]
        is None
    )
    assert bridge_export["assignment"]["daemon_status"] is None
    assert bridge_export["events"] == []
    assert bridge_export["counts"]["events"] == 0
    assert bridge_export["watermark"]["relay_cursor"] == 0
    assert bridge_export["watermark"]["ack_cursor"] == 0
    assert collaboration_export["complete"] is False
    assert (
        collaboration_export["missing_reason"]
        == "collaboration_channel_not_created"
    )
    assert collaboration_export["watermark"] == {
        "min_message_seq": None,
        "max_message_seq": None,
        "next_seq": 1,
        "max_report_evidence_rounds": None,
        "report_evidence_rounds_used_total": 0,
        "max_report_evidence_rounds_used": 0,
        "budget_exhausted_reports": 0,
    }
    assert collaboration_export["channel"] == {
        "channel_id": reservation["channel_id"],
        "assignment_id": reservation["assignment_id"],
        "lilies_session_id": reservation["session_id"],
        "next_seq": 1,
        "missing": True,
    }
    for name, count in collaboration_export["counts"].items():
        assert count == 0
        assert collaboration_export[name] == []
    for name in (
        "credentials",
        "credential_applications",
        "requests",
        "audit",
        "security_events",
    ):
        assert blackbox_export[name] == []
        assert blackbox_export["counts"][name] == 0
    assert (
        source_export["missing_reason"]
        == "assignment_not_delivered_to_daemon"
    )
    assert not (run_root / "assignment.json").exists()

    manager = TaskPackageManager(tmp_path / "sealed-task-state")

    def assert_rehashed_tamper_rejected(
        relative_path: str,
        payload: bytes,
        *,
        match: str,
    ) -> None:
        target = run_root / relative_path
        manifest_path = run_root / "archive-manifest.json"
        original_target = target.read_bytes()
        original_manifest = manifest_path.read_bytes()
        manifest_payload = json.loads(original_manifest)
        for entry in manifest_payload["files"]:
            if entry["path"] == relative_path:
                entry["digest"] = (
                    f"sha256:{hashlib.sha256(payload).hexdigest()}"
                )
                entry["size_bytes"] = len(payload)
                break
        else:
            raise AssertionError(
                f"archive manifest omitted {relative_path}"
            )
        rewritten_manifest = json.dumps(
            manifest_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        target.chmod(0o600)
        target.write_bytes(payload)
        target.chmod(0o400)
        manifest_path.chmod(0o600)
        manifest_path.write_bytes(rewritten_manifest)
        manifest_path.chmod(0o400)
        try:
            with pytest.raises(TaskPackageConflict, match=match):
                manager.replay_archive(run_root)
        finally:
            target.chmod(0o600)
            target.write_bytes(original_target)
            target.chmod(0o400)
            manifest_path.chmod(0o600)
            manifest_path.write_bytes(original_manifest)
            manifest_path.chmod(0o400)

    assert_rehashed_tamper_rejected(
        "manager-prepared-assignment.json",
        prepared_payload + b"\n",
        match="manager-prepared assignment bytes changed",
    )
    forged_bridge = json.loads(
        (run_root / "scanner-inputs/bridge.json").read_text()
    )
    forged_bridge["assignment"][
        "daemon_session_creation_started_at"
    ] = "2026-07-24T00:00:00+00:00"
    assert_rehashed_tamper_rejected(
        "scanner-inputs/bridge.json",
        json.dumps(
            forged_bridge,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        match="differs from its durable reservation",
    )
    forged_collaboration = dict(collaboration_export)
    forged_collaboration["counts"] = {
        **collaboration_export["counts"],
        "messages": 1,
    }
    forged_collaboration["messages"] = [
        {"seq": 1, "content": "forged pre-daemon message"}
    ]
    assert_rehashed_tamper_rejected(
        "scanner-inputs/collaboration.json",
        json.dumps(
            forged_collaboration,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        match="missing collaboration projection changed",
    )
    forged_collaboration = dict(collaboration_export)
    forged_collaboration["watermark"] = {
        "min_message_seq": None,
        "max_message_seq": None,
        "next_seq": 2,
    }
    assert_rehashed_tamper_rejected(
        "scanner-inputs/collaboration.json",
        json.dumps(
            forged_collaboration,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        match="missing collaboration projection changed",
    )
    request_only_reservation = dict(reservation)
    request_only_reservation["preparation_state"] = "request_reserved"
    assert_rehashed_tamper_rejected(
        "reserved-assignment.json",
        json.dumps(
            request_only_reservation,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        match="invalid typed evidence",
    )
    assert manager.replay_archive(run_root) == replay
    assert (await restarted.recover_pending_assignments()).scanned == 0


@pytest.mark.asyncio
async def test_formal_environment_failure_reprobes_the_same_assignment(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, package = make_broker(tmp_path, providers)
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "formal-reprobe")
    request = formal_request(connection.connection_id)

    with pytest.raises(
        LocalLiliesBridgeUnavailable,
        match="may be re-probed",
    ):
        await bridge.start_formal_build(application_id, request)
    failed = (await bridge.store.list_assignments_for_application(application_id))[0]
    assert failed["phase"] == "recorded"
    assert failed["status"] == "environment_failed"
    assert failed["last_error_code"] == "formal_environment_not_ready"

    with _real_health_endpoints(package):
        recovered = await bridge.start_formal_build(application_id, request)

    assert recovered.assignment_id == UUID(failed["assignment_id"])
    assert recovered.phase is BridgeAssignmentPhase.running
    assert len(await bridge.store.list_assignments_for_application(application_id)) == 1
    preflight = (
        tmp_path
        / "sealed-task-state"
        / "preflight"
        / request.task_id
        / str(request.revision)
        / f"formal-run:{recovered.build_id}"
    )
    assert (preflight / "environment-preflight.json").is_file()
    assert (preflight / "environment-ready.json").is_file()


@pytest.mark.asyncio
async def test_formal_daemon_session_error_still_requires_authenticated_tail_drain(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, package = make_broker(tmp_path, providers)
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "formal-session-error")
    with _real_health_endpoints(package):
        running = await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )

    daemon.sessions[str(running.session_id)]["status"] = "error"
    bridge.fault_hook = CrashOnce("resume.assignment_state_committed")
    with pytest.raises(
        InjectedCrash,
        match="resume.assignment_state_committed",
    ):
        await bridge.resume_assignment(running.assignment_id)
    interrupted = await bridge.store.get_assignment(running.assignment_id)
    assert interrupted["phase"] == "error"
    assert interrupted["submission_json"] is not None
    assert interrupted["daemon_status"] == "error"
    assert interrupted["terminal_events_drained_at"] is None

    daemon.unavailable = True
    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    await restarted.initialize()
    with pytest.raises(
        LocalLiliesBridgeUnavailable,
        match="terminal event drain is unavailable",
    ):
        await restarted.resume_assignment(running.assignment_id)

    persisted = await restarted.store.get_assignment(running.assignment_id)
    assert persisted["terminal_events_drained_at"] is None
    assert persisted["formal_terminal_archive_completed_at"] is None


@pytest.mark.asyncio
async def test_formal_session_creation_response_loss_never_seals_an_empty_tail(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, package = make_broker(tmp_path, providers)
    crashing = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
        fault_hook=CrashOnce("session.created"),
    )
    connection = await pair(crashing)
    application_id = await empty_application(workflow, "formal-session-response-loss")
    request = formal_request(connection.connection_id)

    with _real_health_endpoints(package):
        with pytest.raises(InjectedCrash, match="session.created"):
            await crashing.start_formal_build(application_id, request)
    interrupted = (
        await crashing.store.list_assignments_for_application(application_id)
    )[0]
    assert interrupted["phase"] == "recorded"
    assert interrupted["submission_json"] is not None
    assert interrupted["daemon_session_creation_started_at"] is not None
    assert interrupted["daemon_status"] is None
    assert interrupted["terminal_events_drained_at"] is None
    assert str(interrupted["session_id"]) in daemon.sessions

    crashing.fault_hook = None
    crashing.formal_assignment_broker = RejectingFormalBroker()  # type: ignore[assignment]
    with pytest.raises(
        LocalLiliesBridgeSecurityError,
        match="preparation was rejected",
    ):
        await crashing.start_formal_build(application_id, request)
    rejected = await crashing.store.get_assignment(interrupted["assignment_id"])
    assert rejected["phase"] == "error"
    assert rejected["daemon_session_creation_started_at"] is not None
    assert rejected["terminal_events_drained_at"] is None

    daemon.unavailable = True
    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=RejectingFormalBroker(),  # type: ignore[arg-type]
        providers=providers,
    )
    await restarted.initialize()
    with pytest.raises(
        LocalLiliesBridgeUnavailable,
        match="terminal event drain is unavailable",
    ):
        await restarted.resume_assignment(rejected["assignment_id"])
    persisted = await restarted.store.get_assignment(rejected["assignment_id"])
    assert persisted["terminal_events_drained_at"] is None
    assert persisted["formal_terminal_archive_completed_at"] is None


@pytest.mark.asyncio
async def test_formal_cancel_closes_channel_and_revokes_both_daemon_credentials(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, package = make_broker(tmp_path, providers)
    bridge = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "formal-cancel")
    with _real_health_endpoints(package):
        running = await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )
    assert len(daemon.credentials) == 2

    cancelled = await bridge.cancel_assignment(
        running.assignment_id,
        idempotency_key="formal-cancel-request-0001",
    )

    assert cancelled.phase is BridgeAssignmentPhase.cancelled
    assert daemon.credentials == {}
    assert providers.close_calls == 1
    persisted = await bridge.store.get_assignment(running.assignment_id)
    assert persisted["formal_channel_close_receipt_json"]


@pytest.mark.asyncio
async def test_formal_cancel_replays_close_receipt_crash_without_restoring_authority(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, package = make_broker(tmp_path, providers)
    crashing = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
        fault_hook=CrashOnce("formal.collaboration.closed"),
    )
    connection = await pair(crashing)
    application_id = await empty_application(workflow, "formal-close-crash")
    with _real_health_endpoints(package):
        running = await crashing.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )

    with pytest.raises(InjectedCrash, match="formal.collaboration.closed"):
        await crashing.cancel_assignment(
            running.assignment_id,
            idempotency_key="formal-close-crash-cancel-0001",
        )
    interrupted = await crashing.store.get_assignment(running.assignment_id)
    assert interrupted["desired_state"] == "cancelled"
    assert interrupted["formal_channel_close_receipt_json"] is None
    assert daemon.credentials == {}

    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    await restarted.initialize()
    cancelled = await restarted.cancel_assignment(
        running.assignment_id,
        idempotency_key="formal-close-crash-cancel-0001",
    )

    assert cancelled.phase is BridgeAssignmentPhase.cancelled
    assert providers.close_calls == 2
    persisted = await restarted.store.get_assignment(running.assignment_id)
    assert persisted["formal_channel_close_receipt_json"]


@pytest.mark.parametrize(
    "crash_stage",
    [
        "formal.assignment.prepared",
        "formal.collaboration.provisioned",
        "formal.workspace.staged",
        "assignment.submitted",
    ],
)
@pytest.mark.asyncio
async def test_formal_bridge_replays_crash_windows_without_duplicate_effects(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, package = make_broker(tmp_path, providers)
    crashing = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
        fault_hook=CrashOnce(crash_stage),
    )
    connection = await pair(crashing)
    application_id = await empty_application(workflow, crash_stage)
    request = formal_request(connection.connection_id)

    with _real_health_endpoints(package):
        with pytest.raises(InjectedCrash, match=crash_stage):
            await crashing.start_formal_build(application_id, request)

    row = (await crashing.store.list_assignments_for_application(application_id))[0]
    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    await restarted.initialize()
    result = await restarted.resume_assignment(row["assignment_id"])

    assert result.phase is BridgeAssignmentPhase.running
    assert daemon.staging_side_effects == 1
    assert daemon.assignment_side_effects == 1
