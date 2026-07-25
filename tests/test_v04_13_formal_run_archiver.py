from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

import agent_platform.formal_run_archiver as formal_run_archiver_module
import agent_platform.task_packages as task_packages_module
from agent_platform.collaboration_models import (
    ChannelStatus,
    ClaimStatus,
    CollaborationChannel,
    VerificationClaim,
    VerificationClaimPayload,
    VerificationClaimRequest,
    frozen_claim_context_digest,
)
from agent_platform.collaboration_storage import CollaborationStore
from agent_platform.formal_run_archiver import (
    FormalRunArchiveCoordinator,
    FormalRunArchiveError,
    FormalRunArchiveIntentInvalid,
    FormalRunArchiveInvalid,
    FormalRunArchivePreparationRequest,
    FormalRunArchivePreparationResult,
    FormalRunArchiveUnavailable,
    FormalTerminalArchiveResult,
)
from agent_platform.formal_source_provenance import (
    FormalSourceProvenanceCoordinator,
)
from agent_platform.forbidden_assistance_scanner import (
    ForbiddenAssistanceFinding,
    ForbiddenAssistanceScanRecord,
)
from agent_platform.local_lilies_bridge import (
    BridgeAssignmentStep,
    LocalLiliesBridge,
    LocalLiliesBridgeUnavailable,
    LocalLiliesBridgeStore,
    PairLocalLiliesRequest,
    StartFormalLocalLiliesBuildRequest,
)
from agent_platform.lilies_models import (
    BuildAssignment,
    CollaborationScope,
    DeliverableSpec,
)
from agent_platform.platform_blackbox_artifacts import (
    ArtifactBinding,
    ArtifactRegistrationRequest,
    HostReceiptRegistrationRequest,
    PlatformBlackboxArtifactStore,
)
from agent_platform.platform_blackbox_auth import (
    BlackboxAuthorizationRequest,
    PlatformBlackboxOperation,
)
from agent_platform.storage import Storage
from agent_platform.task_packages import (
    ArchiveClaimBinding,
    ArchiveStatus,
    ArchivedFormalReservation,
    TaskPackageNotReady,
)
from agent_platform.workflow_models import WorkflowRunState
from agent_platform.workflow_storage import WorkflowStorage
from tests.test_v04_13_assignment_bridge import (
    CrashOnce,
    DIGEST,
    InjectedCrash,
    empty_application,
    pair,
    platform_parts,
)
from tests.test_v04_13_formal_assignment_runtime import (
    _request,
    _runtime_parts,
)
from tests.test_v04_13_formal_local_lilies_bridge import (
    FormalDaemonClient,
    FormalProviders,
    formal_request,
    make_bridge,
    make_broker,
)
from tests.test_v04_13_task_packages import _real_health_endpoints


async def _completed_platform_run(
    *,
    storage: Storage,
    workflow: WorkflowStorage,
    run_id: str,
    application_id: UUID,
    assignment_id: UUID,
    session_id: UUID,
    workspace: Path,
    version: int | None = None,
    draft_revision: int | None = None,
) -> None:
    draft = await workflow.get_draft(str(application_id))
    state = WorkflowRunState(
        run_id=run_id,
        application_id=str(application_id),
        snapshot=draft["snapshot"],
        inputs={},
        workspace_path=str(workspace),
        workspace_boundary="assignment",
        assignment_id=str(assignment_id),
        session_id=str(session_id),
    )
    await workflow.create_run(
        state,
        version=version,
        draft_revision=(
            int(draft["revision"]) if version is None and draft_revision is None else draft_revision
        ),
    )
    await storage.append_event(
        run_id,
        "workflow.started",
        {"status": "running"},
    )
    await storage.append_event(
        run_id,
        "workflow.completed",
        {"status": "completed"},
    )
    await workflow.update_run(
        run_id,
        status="succeeded",
        state=state,
        outputs={"result": {"status": "completed"}},
    )


async def _failed_platform_run(
    *,
    storage: Storage,
    workflow: WorkflowStorage,
    run_id: str,
    application_id: UUID,
    assignment_id: UUID,
    session_id: UUID,
    workspace: Path,
) -> None:
    draft = await workflow.get_draft(str(application_id))
    state = WorkflowRunState(
        run_id=run_id,
        application_id=str(application_id),
        snapshot=draft["snapshot"],
        inputs={"attempt": "historical-failure"},
        workspace_path=str(workspace),
        workspace_boundary="assignment",
        assignment_id=str(assignment_id),
        session_id=str(session_id),
    )
    await workflow.create_run(
        state,
        version=None,
        draft_revision=int(draft["revision"]),
    )
    await storage.append_event(
        run_id,
        "workflow.started",
        {"status": "running", "attempt": "historical-failure"},
    )
    await storage.append_event(
        run_id,
        "workflow.failed",
        {"status": "failed", "error": {"code": "fixture_failure"}},
    )
    await workflow.update_run(
        run_id,
        status="failed",
        state=state,
        outputs={"partial": {"rows": 1}},
        error="fixture_failure",
    )


class _CompleteBlackboxAudit:
    def __init__(
        self,
        workflow: WorkflowStorage,
        application_id: UUID,
        assignment: BuildAssignment,
        *,
        tool_call_id: str,
        request_payload: dict[str, Any],
        tests_tool_call_id: str,
        test_run_ids: list[str],
        business_tool_call_id: str,
        business_run_ids: list[str],
    ) -> None:
        self.workflow = workflow
        self.application_id = application_id
        self.assignment = assignment
        self.tool_call_id = tool_call_id
        self.request_payload = request_payload
        self.tests_tool_call_id = tests_tool_call_id
        self.test_run_ids = list(test_run_ids)
        self.business_tool_call_id = business_tool_call_id
        self.business_run_ids = list(business_run_ids)
        self.credential_overrides: dict[str, Any] = {}

    async def export_assignment_snapshot(
        self,
        *,
        assignment_id: UUID,
        session_id: UUID,
    ) -> dict[str, Any]:
        workflow_export = await self.workflow.export_formal_run_snapshot(
            str(self.application_id),
            assignment_id=str(assignment_id),
            session_id=str(session_id),
        )
        mutations = workflow_export["formal_draft_provenance"]["mutations"]
        assert len(mutations) == 1
        mutation = mutations[0]
        tests_payload = {
            "application_id": str(self.application_id),
            "idempotency_key": "formal-archive-tests-run-0001",
        }
        business_payload = {
            "application_id": str(self.application_id),
            "idempotency_key": "formal-archive-business-run-0001",
            "inputs": {},
            "use_draft": True,
        }

        def payload_digest(payload: dict[str, Any]) -> str:
            return "sha256:" + hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()

        return {
            "schema_version": "1.0",
            "assignment_id": str(assignment_id),
            "session_id": str(session_id),
            "complete": True,
            "credentials": [
                {
                    "id": "00000000-0000-4000-8000-000000000211",
                    "credential_ref": self.assignment.platform.credential_ref,
                    "assignment_id": str(assignment_id),
                    "session_id": str(session_id),
                    "scopes": sorted(
                        scope.value for scope in self.assignment.platform.scopes
                    ),
                    "allowed_operations": sorted(
                        action.value
                        for action in self.assignment.constraints.allowed_actions
                    ),
                    "allowed_actions_digest": (
                        self.assignment.task_package.allowed_actions_digest
                    ),
                    "budget_digest": self.assignment.task_package.budget_digest,
                    "allowed_network_hosts": sorted(
                        host.casefold()
                        for host in self.assignment.constraints.allowed_hosts
                    ),
                    "model_access": self.assignment.constraints.model_access,
                    "file_access": self.assignment.constraints.file_access,
                    "connector_access": (
                        self.assignment.constraints.connector_access
                    ),
                    "readable_host_objects": sorted(
                        self.assignment.constraints.readable_host_objects
                    ),
                    "writable_host_operations": sorted(
                        self.assignment.constraints.writable_host_operations
                    ),
                    "permission_required_actions": sorted(
                        self.assignment.constraints.permission_required_actions
                    ),
                    "max_write_count": (
                        self.assignment.constraints.max_write_count
                    ),
                    "max_payload_bytes": (
                        self.assignment.constraints.max_payload_bytes
                    ),
                    "compensation_actions": sorted(
                        self.assignment.constraints.compensation_actions
                    ),
                    "max_report_evidence_rounds": (
                        self.assignment.constraints.max_report_evidence_rounds
                    ),
                    "stable_hidden_runs": (
                        self.assignment.constraints.stable_hidden_runs
                    ),
                    "expires_at": (
                        self.assignment.constraints.deadline_at.isoformat()
                    ),
                    "created_at": self.assignment.created_at.isoformat(),
                    "updated_at": self.assignment.created_at.isoformat(),
                    **self.credential_overrides,
                }
            ],
            "credential_applications": [
                {
                    "credential_id": (
                        "00000000-0000-4000-8000-000000000211"
                    ),
                    "application_id": str(self.application_id),
                    "granted_at": self.assignment.created_at.isoformat(),
                }
            ],
            "requests": [
                {
                    "request_id": mutation["request_id"],
                    "assignment_id": str(assignment_id),
                    "session_id": str(session_id),
                    "application_id": str(self.application_id),
                    "tool_call_id": self.tool_call_id,
                    "operation": "platform_draft_apply",
                    "idempotency_key": self.request_payload[
                        "idempotency_key"
                    ],
                    "payload": self.request_payload,
                    "payload_digest": mutation["request_payload_digest"],
                    "state": "completed",
                    "status_code": 200,
                    "response": {
                        "revision": int(mutation["after_revision"]),
                        "content_hash": str(mutation["after_content_hash"]),
                    },
                },
                {
                    "request_id": "00000000-0000-4000-8000-000000000202",
                    "assignment_id": str(assignment_id),
                    "session_id": str(session_id),
                    "application_id": str(self.application_id),
                    "tool_call_id": self.tests_tool_call_id,
                    "operation": "platform_tests_run",
                    "idempotency_key": tests_payload["idempotency_key"],
                    "payload": tests_payload,
                    "payload_digest": payload_digest(tests_payload),
                    "state": "completed",
                    "status_code": 200,
                    "response": {
                        "ok": True,
                        "operation": "platform_tests_run",
                        "data": {
                            "passed": True,
                            "tests": [
                                {"run_id": run_id}
                                for run_id in self.test_run_ids
                            ],
                        },
                    },
                },
                *[
                    {
                        "request_id": (
                            f"00000000-0000-4000-8000-{index + 203:012d}"
                        ),
                        "assignment_id": str(assignment_id),
                        "session_id": str(session_id),
                        "application_id": str(self.application_id),
                        "tool_call_id": (
                            self.business_tool_call_id
                            if index == 0
                            else f"{self.business_tool_call_id}-{index}"
                        ),
                        "operation": "platform_run_start",
                        "idempotency_key": (
                            business_payload["idempotency_key"]
                            if index == 0
                            else f"formal-archive-business-run-{index + 1:04d}"
                        ),
                        "payload": {
                            **business_payload,
                            "idempotency_key": (
                                business_payload["idempotency_key"]
                                if index == 0
                                else (
                                    "formal-archive-business-run-"
                                    f"{index + 1:04d}"
                                )
                            ),
                        },
                        "payload_digest": payload_digest(
                            {
                                **business_payload,
                                "idempotency_key": (
                                    business_payload["idempotency_key"]
                                    if index == 0
                                    else (
                                        "formal-archive-business-run-"
                                        f"{index + 1:04d}"
                                    )
                                ),
                            }
                        ),
                        "state": "completed",
                        "status_code": 202,
                        "response": {
                            "ok": True,
                            "operation": "platform_run_start",
                            "data": {"run_id": run_id},
                        },
                    }
                    for index, run_id in enumerate(self.business_run_ids)
                ],
            ],
            "audit": [],
            "security_events": [],
            "audit_min_seq": None,
            "audit_max_seq": None,
            "security_min_seq": None,
            "security_max_seq": None,
            "counts": {
                "credentials": 1,
                "credential_applications": 1,
                "requests": 2 + len(self.business_run_ids),
                "audit": 0,
                "security_events": 0,
            },
        }


class _ConnectorBudgetAudit:
    def __init__(self) -> None:
        self._policies: dict[str, dict[str, Any]] = {}
        self.export_counts: dict[str, int] = {}
        self._include_write_after_count: dict[str, int] = {}
        self._writes: dict[str, list[dict[str, Any]]] = {}

    def record_write(
        self,
        assignment_id: UUID,
        *,
        execution_id: str,
        operation_id: str,
    ) -> None:
        self._writes.setdefault(str(assignment_id), []).append(
            {
                "execution_id": execution_id,
                "connector_id": "fixture.connector",
                "connector_version": 1,
                "tenant_id": "tenant:formal",
                "profile_id": "profile:test",
                "operation_id": operation_id,
                "operation_kind": "write",
                "idempotency_key": f"connector-write:{execution_id}",
                "payload_hash": "sha256:" + "9" * 64,
                "status": "succeeded",
                "side_effect_state": "applied",
                "authorization_ref_digest": None,
                "adapter_called": True,
                "created_at": "2026-07-24T00:00:00Z",
                "updated_at": "2026-07-24T00:00:01Z",
            }
        )

    def drift_on_next_recheck(self, assignment_id: UUID) -> None:
        key = str(assignment_id)
        self._include_write_after_count[key] = (
            self.export_counts.get(key, 0) + 3
        )

    async def freeze_assignment_budget(
        self,
        *,
        assignment_id: str,
        allowed_network_hosts: list[str],
        allowed_compensation_operations: list[str],
        max_write_count: int,
        max_payload_bytes: int,
    ) -> dict[str, Any]:
        policy = {
            "allowed_network_hosts": sorted(
                {
                    host.casefold().rstrip(".")
                    for host in allowed_network_hosts
                }
            ),
            "allowed_compensation_operations": sorted(
                set(allowed_compensation_operations)
            ),
            "max_write_count": max_write_count,
            "max_payload_bytes": max_payload_bytes,
        }
        existing = self._policies.setdefault(assignment_id, policy)
        if existing != policy:
            raise ValueError("connector assignment policy changed")
        return await self.export_assignment_budget(assignment_id)

    async def export_assignment_budget(
        self,
        assignment_id: str,
    ) -> dict[str, Any]:
        policy = self._policies[assignment_id]
        self.export_counts[assignment_id] = (
            self.export_counts.get(assignment_id, 0) + 1
        )
        writes = list(self._writes.get(assignment_id, []))
        if self.export_counts[assignment_id] >= (
            self._include_write_after_count.get(assignment_id, 1_000_000)
        ):
            writes.append(
                {
                    "execution_id": "connector-execution:toctou",
                    "connector_id": "fixture.connector",
                    "connector_version": 1,
                    "tenant_id": "tenant:formal",
                    "profile_id": "profile:test",
                    "operation_id": "fixture.write",
                    "operation_kind": "write",
                    "idempotency_key": "connector-toctou-0001",
                    "payload_hash": "sha256:" + "a" * 64,
                    "status": "succeeded",
                    "side_effect_state": "applied",
                    "authorization_ref_digest": None,
                    "adapter_called": True,
                    "created_at": "2026-07-24T00:00:00Z",
                    "updated_at": "2026-07-24T00:00:01Z",
                }
            )
        writes.sort(
            key=lambda item: (
                item["connector_id"],
                item["connector_version"],
                item["tenant_id"],
                item["operation_id"],
                item["idempotency_key"],
                item["execution_id"],
            )
        )
        document = {
            "schema_version": "1.0",
            "assignment_id": assignment_id,
            "policy_digest": hashlib.sha256(
                json.dumps(
                    policy,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            **policy,
            "write_count": len(writes),
            "writes": writes,
        }
        document["receipt_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return document


async def _record_formal_draft_edit(
    workflow: WorkflowStorage,
    *,
    application_id: UUID,
    assignment_id: UUID,
    session_id: UUID,
    tool_call_id: str,
    request_id: str = "00000000-0000-4000-8000-000000000201",
) -> dict[str, Any]:
    await workflow.begin_formal_draft_provenance(
        assignment_id=str(assignment_id),
        session_id=str(session_id),
        application_id=str(application_id),
    )
    draft = await workflow.get_draft(str(application_id))
    snapshot = draft["snapshot"].model_copy(deep=True)
    snapshot.description = (
        f"{snapshot.description} Formal Lilies authored this meaningful edit."
    ).strip()
    idempotency_key = f"formal-draft-edit-{assignment_id.hex[:24]}"
    data = {"description": snapshot.description}
    operation_payload = {
        "application_id": str(application_id),
        "expected_revision": int(draft["revision"]),
        "op": "set_metadata",
        "data": data,
    }
    operation_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            operation_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    request_payload = {
        "application_id": str(application_id),
        "idempotency_key": idempotency_key,
        "expected_revision": int(draft["revision"]),
        "op": "set_metadata",
        "data": data,
    }
    request_payload_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    result = await workflow.save_draft(
        str(application_id),
        snapshot,
        expected_revision=int(draft["revision"]),
        idempotency_key=idempotency_key,
        change_context={"operation": "set_metadata"},
        idempotency_digest=operation_digest,
        formal_mutation_context={
            "assignment_id": str(assignment_id),
            "session_id": str(session_id),
            "application_id": str(application_id),
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "operation": "set_metadata",
            "request_payload_digest": request_payload_digest,
        },
    )
    return {
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "request_payload": request_payload,
        "request_payload_digest": request_payload_digest,
        "result": result,
    }


def _clean_source_provenance(
    tmp_path: Path,
    *,
    assignment_id: UUID,
    channel_id: UUID,
    task_id: str,
    task_revision: int,
    run_id: str,
    captured_at: datetime,
    name: str = "formal-source-provenance",
) -> FormalSourceProvenanceCoordinator:
    repository = tmp_path / f"{name}-repository"
    repository.mkdir()
    subprocess.run(
        ["git", "-C", str(repository), "init", "--initial-branch=main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "formal@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Formal Test"],
        check=True,
    )
    source = repository / "platform/backend/src/agent_platform/baseline.py"
    source.parent.mkdir(parents=True)
    source.write_text("FORMAL_BASELINE = True\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "add", "--all"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "formal baseline"],
        check=True,
        capture_output=True,
    )
    coordinator = FormalSourceProvenanceCoordinator(
        repository_root=repository,
        state_root=tmp_path / f"{name}-state",
    )
    coordinator.freeze_baseline(
        task_id=task_id,
        task_revision=task_revision,
        run_id=run_id,
        assignment_id=assignment_id,
        channel_id=channel_id,
        captured_at=captured_at,
    )
    return coordinator


def _submitted_source_provenance(
    tmp_path: Path,
    daemon: FormalDaemonClient,
    assignment_id: UUID,
    *,
    name: str,
) -> FormalSourceProvenanceCoordinator:
    assignment = next(
        item
        for item in daemon.submitted_assignments
        if item.assignment_id == assignment_id
    )
    task = assignment.task_package
    access = assignment.collaboration
    assert task is not None
    assert access is not None
    return _clean_source_provenance(
        tmp_path,
        assignment_id=assignment.assignment_id,
        channel_id=access.channel_id,
        task_id=task.task_id,
        task_revision=task.revision,
        run_id=task.run_id,
        captured_at=assignment.created_at,
        name=name,
    )


@pytest.mark.asyncio
async def test_platform_owned_success_export_prepares_replayable_claim_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, collaboration, _, package, _ = await _runtime_parts(tmp_path)
    storage = Storage(tmp_path / "platform")
    await storage.initialize()
    workflow = WorkflowStorage(storage)
    await workflow.initialize()
    application_id = await empty_application(workflow, "formal-archive-success")
    request = _request().model_copy(update={"application_id": application_id})

    for _ in range(2):
        with pytest.raises(TaskPackageNotReady):
            await runtime.prepare_async(request)
    with _real_health_endpoints(package):
        prepared = await runtime.prepare_async(request)
    token = await runtime.collaboration_credential_secret(
        prepared.assignment,
        request.session_id,
    )
    access = prepared.assignment.collaboration
    task = prepared.assignment.task_package
    assert access is not None
    assert task is not None
    principal = await collaboration.authenticate_lilies(
        token.get_secret_value(),
        channel_id=access.channel_id,
        required_scope=CollaborationScope.report_write.value,
    )

    bridge_store = LocalLiliesBridgeStore(tmp_path / "platform" / "local-lilies-bridge.db")
    await bridge_store.initialize()
    pairing = PairLocalLiliesRequest(
        idempotency_key="formal-archive-pair-0001",
        base_url="http://127.0.0.1:8765",
        pairing_code="PAIR-CODE-ARCHIVE",
        expected_daemon_fingerprint=DIGEST,
    )
    await bridge_store.reserve_connection(
        connection_id=request.connection_id,
        request=pairing,
        base_url=pairing.base_url,
        request_digest=DIGEST,
        secret_ref="secret://formal-archive/daemon-token",
    )
    start = StartFormalLocalLiliesBuildRequest(
        idempotency_key=request.idempotency_key,
        connection_id=request.connection_id,
        task_id=request.task_id,
        revision=request.revision,
        environment_instance_id=request.environment_instance_id,
        user_notified=True,
    )
    await bridge_store.reserve_assignment(
        assignment_id=request.assignment_id,
        application_id=application_id,
        build_id=request.build_id,
        session_id=request.session_id,
        request=start,
        request_digest=DIGEST,
        request_json=start.model_dump_json(),
        task_token_secret_ref="secret://formal-archive/task-token",
        assignment_mode=prepared.assignment.mode,
    )
    await bridge_store.update_assignment(
        request.assignment_id,
        submission_json=prepared.assignment.model_dump_json(exclude_none=True),
        phase=BridgeAssignmentStep.completed,
        status="completed",
        daemon_status="completed",
    )
    test_run_id = "test-run:platform-owned-archive-001"
    second_test_run_id = "test-run:platform-owned-archive-002"
    business_run_id = "business-run:platform-owned-archive-001"
    failed_run_id = "failed-run:platform-owned-archive-001"
    test_workspace = tmp_path / "run-workspaces" / "test"
    second_test_workspace = tmp_path / "run-workspaces" / "test-2"
    business_workspace = tmp_path / "run-workspaces" / "business"
    failed_workspace = tmp_path / "run-workspaces" / "failed"
    test_workspace.mkdir(parents=True)
    second_test_workspace.mkdir(parents=True)
    business_workspace.mkdir(parents=True)
    failed_workspace.mkdir(parents=True)
    await _failed_platform_run(
        storage=storage,
        workflow=workflow,
        run_id=failed_run_id,
        application_id=application_id,
        assignment_id=request.assignment_id,
        session_id=request.session_id,
        workspace=failed_workspace,
    )
    scanner_tool_call_id = "tool-call:formal-draft-apply"
    scanner_tests_tool_call_id = "tool-call:formal-tests-run"
    scanner_business_tool_call_id = "tool-call:formal-business-run"
    draft_edit = await _record_formal_draft_edit(
        workflow,
        application_id=application_id,
        assignment_id=request.assignment_id,
        session_id=request.session_id,
        tool_call_id=scanner_tool_call_id,
    )
    await bridge_store.commit_relay_events(
        request.assignment_id,
        request.session_id,
        [
            {
                "seq": 1,
                "event": "assignment.accepted",
                "data": {
                    "assignment_id": str(request.assignment_id),
                    "mode": "formal_experiment",
                },
            },
            {
                "seq": 2,
                "event": "tool.started",
                "data": {
                    "tool_call_id": scanner_tool_call_id,
                    "tool": "platform_draft_apply",
                },
            },
            {
                "seq": 3,
                "event": "tool.completed",
                "data": {
                    "tool_call_id": scanner_tool_call_id,
                    "tool": "platform_draft_apply",
                    "is_error": False,
                },
            },
            {
                "seq": 4,
                "event": "tool.started",
                "data": {
                    "tool_call_id": scanner_tests_tool_call_id,
                    "tool": "platform_tests_run",
                },
            },
            {
                "seq": 5,
                "event": "tool.completed",
                "data": {
                    "tool_call_id": scanner_tests_tool_call_id,
                    "tool": "platform_tests_run",
                    "is_error": False,
                },
            },
            {
                "seq": 6,
                "event": "tool.started",
                "data": {
                    "tool_call_id": scanner_business_tool_call_id,
                    "tool": "platform_run_start",
                },
            },
            {
                "seq": 7,
                "event": "tool.completed",
                "data": {
                    "tool_call_id": scanner_business_tool_call_id,
                    "tool": "platform_run_start",
                    "is_error": False,
                },
            },
        ],
    )
    await bridge_store.update_assignment(
        request.assignment_id,
        ack_cursor=7,
        terminal_events_drained_at=datetime.now(timezone.utc).isoformat(),
    )

    await _completed_platform_run(
        storage=storage,
        workflow=workflow,
        run_id=test_run_id,
        application_id=application_id,
        assignment_id=request.assignment_id,
        session_id=request.session_id,
        workspace=test_workspace,
    )
    await _completed_platform_run(
        storage=storage,
        workflow=workflow,
        run_id=second_test_run_id,
        application_id=application_id,
        assignment_id=request.assignment_id,
        session_id=request.session_id,
        workspace=second_test_workspace,
    )
    draft = await workflow.get_draft(str(application_id))
    await workflow.mark_tested(
        str(application_id),
        int(draft["revision"]),
        str(draft["content_hash"]),
        {
            "passed": True,
            "validation": {
                "valid": True,
                "content_hash": draft["content_hash"],
            },
            "tests": [
                {
                    "test_id": "acceptance-001",
                    "run_id": test_run_id,
                    "run_status": "succeeded",
                    "passed": True,
                },
                {
                    "test_id": "acceptance-002",
                    "run_id": second_test_run_id,
                    "run_status": "succeeded",
                    "passed": True,
                },
            ],
        },
    )
    await _completed_platform_run(
        storage=storage,
        workflow=workflow,
        run_id=business_run_id,
        application_id=application_id,
        assignment_id=request.assignment_id,
        session_id=request.session_id,
        workspace=business_workspace,
    )
    artifact_payload = b'{"result":"matched"}\n'
    receipt_payload = b'{"receipt":"host-write-accepted"}\n'
    forged_receipt_payload = b'{"receipt":"workspace-file-only"}\n'
    (business_workspace / "result.json").write_bytes(artifact_payload)
    (business_workspace / "receipt.json").write_bytes(receipt_payload)
    (business_workspace / "forged-receipt.json").write_bytes(forged_receipt_payload)
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    artifact_binding = ArtifactBinding(
        assignment_id=request.assignment_id,
        session_id=request.session_id,
        application_id=application_id,
        run_id=business_run_id,
    )
    artifact = await artifacts.register_artifact(
        ArtifactRegistrationRequest(
            binding=artifact_binding,
            relative_path="result.json",
            media_type="application/json",
        ),
        artifact_root=business_workspace,
    )
    forged_receipt = await artifacts.register_artifact(
        ArtifactRegistrationRequest(
            binding=artifact_binding,
            relative_path="forged-receipt.json",
            media_type="application/json",
        ),
        artifact_root=business_workspace,
    )
    receipt = await artifacts.register_host_receipt(
        HostReceiptRegistrationRequest(
            binding=artifact_binding,
            relative_path="receipt.json",
            media_type="application/json",
            receipt_id="host-write-receipt-0001",
            operation="paperless.metadata.update",
        ),
        artifact_root=business_workspace,
    )
    (test_workspace / "test-only.json").write_bytes(
        b'{"test_only":true}\n'
    )
    test_only_artifact = await artifacts.register_artifact(
        ArtifactRegistrationRequest(
            binding=ArtifactBinding(
                assignment_id=request.assignment_id,
                session_id=request.session_id,
                application_id=application_id,
                run_id=test_run_id,
            ),
            relative_path="test-only.json",
            media_type="application/json",
        ),
        artifact_root=test_workspace,
    )
    source_provenance = _clean_source_provenance(
        tmp_path,
        assignment_id=prepared.assignment.assignment_id,
        channel_id=access.channel_id,
        task_id=task.task_id,
        task_revision=task.revision,
        run_id=task.run_id,
        captured_at=prepared.assignment.created_at,
    )
    blackbox_audit = _CompleteBlackboxAudit(
        workflow,
        application_id,
        prepared.assignment,
        tool_call_id=scanner_tool_call_id,
        request_payload=draft_edit["request_payload"],
        tests_tool_call_id=scanner_tests_tool_call_id,
        test_run_ids=[test_run_id, second_test_run_id],
        business_tool_call_id=scanner_business_tool_call_id,
        business_run_ids=[business_run_id],
    )
    connector_budget_audit = _ConnectorBudgetAudit()
    connector_budget_audit.record_write(
        prepared.assignment.assignment_id,
        execution_id="host-write-receipt-0001",
        operation_id="paperless.metadata.update",
    )
    coordinator = FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge_store,
        collaboration_store=collaboration.store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=blackbox_audit,
        connector_service=connector_budget_audit,
        source_provenance=source_provenance,
    )
    archive_request = FormalRunArchivePreparationRequest(
        expected_channel_revision=1,
        claim_id=uuid4(),
        test_run_ids=[test_run_id, second_test_run_id],
        business_run_ids=[business_run_id],
        artifact_ids=[
            artifact.artifact.artifact_id,
            forged_receipt.artifact.artifact_id,
        ],
        host_receipt_ids=[receipt.artifact.artifact_id],
        remaining_limits=["controlled local fixture only"],
        summary="Platform-owned formal archive completed from durable stores.",
        idempotency_key="formal-archive-success-0001",
    )

    connector_budget_export = await connector_budget_audit.freeze_assignment_budget(
        assignment_id=str(prepared.assignment.assignment_id),
        allowed_network_hosts=list(
            prepared.assignment.constraints.allowed_hosts
        ),
        allowed_compensation_operations=list(
            prepared.assignment.constraints.compensation_actions
        ),
        max_write_count=prepared.assignment.constraints.max_write_count,
        max_payload_bytes=prepared.assignment.constraints.max_payload_bytes,
    )
    assignment_with_two_required_deliverables = (
        prepared.assignment.model_copy(
            update={
                "deliverables": [
                    *prepared.assignment.deliverables,
                    DeliverableSpec(
                        name="required-workbook",
                        description="A required business workbook.",
                        media_type=(
                            "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet"
                        ),
                    ),
                ]
            }
        )
    )
    with pytest.raises(FormalRunArchiveIntentInvalid) as partial_deliverables:
        await coordinator._assert_complete_business_evidence(  # noqa: SLF001
            request=archive_request,
            assignment=assignment_with_two_required_deliverables,
            session_id=request.session_id,
            business_run_ids={business_run_id},
            connector_budget_export=connector_budget_export,
        )
    assert (
        partial_deliverables.value.code
        == "formal_archive_required_artifact_missing"
    )
    with pytest.raises(FormalRunArchiveIntentInvalid) as test_artifact_selected:
        await coordinator._assert_complete_business_evidence(  # noqa: SLF001
            request=archive_request.model_copy(
                update={
                    "artifact_ids": [
                        *archive_request.artifact_ids,
                        test_only_artifact.artifact.artifact_id,
                    ]
                }
            ),
            assignment=prepared.assignment,
            session_id=request.session_id,
            business_run_ids={business_run_id},
            connector_budget_export=connector_budget_export,
        )
    assert (
        test_artifact_selected.value.code
        == "formal_archive_evidence_inventory_incomplete"
    )
    pair_mismatch_audit = _ConnectorBudgetAudit()
    pair_mismatch_audit.record_write(
        prepared.assignment.assignment_id,
        execution_id="host-write-receipt-0001",
        operation_id="paperless.other.authorized.update",
    )
    pair_mismatch_export = await pair_mismatch_audit.freeze_assignment_budget(
        assignment_id=str(prepared.assignment.assignment_id),
        allowed_network_hosts=list(
            prepared.assignment.constraints.allowed_hosts
        ),
        allowed_compensation_operations=list(
            prepared.assignment.constraints.compensation_actions
        ),
        max_write_count=prepared.assignment.constraints.max_write_count,
        max_payload_bytes=prepared.assignment.constraints.max_payload_bytes,
    )
    pair_mismatch_assignment = prepared.assignment.model_copy(
        update={
            "constraints": prepared.assignment.constraints.model_copy(
                update={
                    "writable_host_operations": [
                        *prepared.assignment.constraints.writable_host_operations,
                        "paperless.other.authorized.update",
                    ]
                }
            )
        }
    )
    with pytest.raises(FormalRunArchiveIntentInvalid) as mismatched_pair:
        await coordinator._assert_complete_business_evidence(  # noqa: SLF001
            request=archive_request,
            assignment=pair_mismatch_assignment,
            session_id=request.session_id,
            business_run_ids={business_run_id},
            connector_budget_export=pair_mismatch_export,
        )
    assert (
        mismatched_pair.value.code
        == "formal_archive_host_receipt_denominator_mismatch"
    )

    with pytest.raises(FormalRunArchiveIntentInvalid) as incomplete_runs:
        await coordinator.validate_success_archive_intent(
            channel_id=access.channel_id,
            request=archive_request.model_copy(
                update={"test_run_ids": [test_run_id]}
            ),
        )
    assert (
        incomplete_runs.value.code
        == "formal_archive_current_run_set_incomplete"
    )
    with pytest.raises(FormalRunArchiveIntentInvalid) as missing_artifact:
        await coordinator.validate_success_archive_intent(
            channel_id=access.channel_id,
            request=archive_request.model_copy(update={"artifact_ids": []}),
        )
    assert (
        missing_artifact.value.code
        == "formal_archive_required_artifact_missing"
    )
    with pytest.raises(FormalRunArchiveIntentInvalid) as missing_receipt:
        await coordinator.validate_success_archive_intent(
            channel_id=access.channel_id,
            request=archive_request.model_copy(update={"host_receipt_ids": []}),
        )
    assert (
        missing_receipt.value.code
        == "formal_archive_host_receipt_missing"
    )
    with pytest.raises(FormalRunArchiveIntentInvalid) as invalid_receipt:
        await coordinator.validate_success_archive_intent(
            channel_id=access.channel_id,
            request=archive_request.model_copy(
                update={
                    "artifact_ids": [artifact.artifact.artifact_id],
                    "host_receipt_ids": [
                        forged_receipt.artifact.artifact_id
                    ],
                }
            ),
        )
    assert (
        invalid_receipt.value.code
        == "formal_archive_evidence_inventory_incomplete"
    )
    await coordinator.validate_success_archive_intent(
        channel_id=access.channel_id,
        request=archive_request,
    )

    with pytest.raises(FormalRunArchiveError, match="complete platform-owned"):
        await coordinator.prepare_success_archive(
            channel_id=access.channel_id,
            request=archive_request.model_copy(update={"test_run_ids": [test_run_id]}),
        )
    with pytest.raises(FormalRunArchiveError, match="complete platform-owned"):
        await coordinator.prepare_success_archive(
            channel_id=access.channel_id,
            request=archive_request.model_copy(
                update={
                    "test_run_ids": [business_run_id],
                    "business_run_ids": [test_run_id, second_test_run_id],
                }
            ),
        )
    with pytest.raises(FormalRunArchiveError, match="complete registered"):
        await coordinator.prepare_success_archive(
            channel_id=access.channel_id,
            request=archive_request.model_copy(
                update={
                    "artifact_ids": [artifact.artifact.artifact_id],
                    "host_receipt_ids": [forged_receipt.artifact.artifact_id],
                }
            ),
        )
    blackbox_audit.credential_overrides = {
        "allowed_operations": ["platform_contract_get"]
    }
    with pytest.raises(FormalRunArchiveError, match="frozen task policy"):
        await coordinator.prepare_success_archive(
            channel_id=access.channel_id,
            request=archive_request,
        )
    blackbox_audit.credential_overrides = {}
    original_archive_run = coordinator._manager.archive_run  # noqa: SLF001

    def fail_archive_io(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("injected temporary archive filesystem failure")

    monkeypatch.setattr(coordinator._manager, "archive_run", fail_archive_io)  # noqa: SLF001
    with pytest.raises(
        FormalRunArchiveUnavailable,
        match="temporarily unavailable",
    ):
        await coordinator.prepare_success_archive(
            channel_id=access.channel_id,
            request=archive_request,
        )
    monkeypatch.setattr(
        coordinator._manager,  # noqa: SLF001
        "archive_run",
        original_archive_run,
    )
    connector_budget_audit.drift_on_next_recheck(
        prepared.assignment.assignment_id
    )
    with pytest.raises(
        FormalRunArchiveUnavailable,
        match="changed during archive export",
    ):
        await coordinator.prepare_success_archive(
            channel_id=access.channel_id,
            request=archive_request,
        )
    connector_budget_audit._include_write_after_count.clear()  # noqa: SLF001

    invalid_task_state = tmp_path / "invalid-sealed-task-state"
    shutil.copytree(
        tmp_path / "sealed-task-state",
        invalid_task_state,
    )
    invalid_connector_budget_audit = _ConnectorBudgetAudit()
    invalid_connector_budget_audit.record_write(
        prepared.assignment.assignment_id,
        execution_id="host-write-receipt-0001",
        operation_id="paperless.metadata.update",
    )
    invalid_coordinator = FormalRunArchiveCoordinator(
        task_state_root=invalid_task_state,
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge_store,
        collaboration_store=collaboration.store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=coordinator._auth_store,  # noqa: SLF001
        connector_service=invalid_connector_budget_audit,
        source_provenance=source_provenance,
    )
    original_scan = formal_run_archiver_module.scan_forbidden_assistance

    def inject_forbidden_finding(*args: Any, **kwargs: Any) -> Any:
        scan = original_scan(*args, **kwargs)
        finding = ForbiddenAssistanceFinding(
            rule_id="test_forbidden_assistance",
            outcome="violation",
            source_ref="scanner-inputs/injected-test-finding",
            evidence_digest=DIGEST,
        )
        payload = scan.model_dump(mode="json", exclude_none=True)
        payload["findings"] = [finding.model_dump(mode="json")]
        payload["verdict"] = "failed"
        payload["scan_digest"] = "sha256:" + "0" * 64
        provisional = ForbiddenAssistanceScanRecord.model_validate(payload)
        digest_payload = provisional.model_dump(
            mode="json",
            exclude_none=True,
        )
        digest_payload.pop("scan_digest")
        scan_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return provisional.model_copy(
            update={"scan_digest": scan_digest},
        )

    monkeypatch.setattr(
        formal_run_archiver_module,
        "scan_forbidden_assistance",
        inject_forbidden_finding,
    )
    monkeypatch.setattr(
        task_packages_module,
        "scan_forbidden_assistance",
        inject_forbidden_finding,
    )
    with pytest.raises(FormalRunArchiveInvalid) as invalidated:
        await invalid_coordinator.prepare_success_archive(
            channel_id=access.channel_id,
            request=archive_request,
        )
    assert invalidated.value.result.status is ArchiveStatus.invalid
    assert invalidated.value.result.assignment_id == request.assignment_id
    invalid_replay = invalid_coordinator.replay(
        invalidated.value.result,
    )
    assert invalid_replay.source_status is ArchiveStatus.succeeded
    assert invalid_replay.status is ArchiveStatus.invalid
    monkeypatch.setattr(
        formal_run_archiver_module,
        "scan_forbidden_assistance",
        original_scan,
    )
    monkeypatch.setattr(
        task_packages_module,
        "scan_forbidden_assistance",
        original_scan,
    )
    archived = await coordinator.prepare_success_archive(
        channel_id=access.channel_id,
        request=archive_request,
    )
    replay = coordinator.replay(archived)
    replayed = await coordinator.prepare_success_archive(
        channel_id=access.channel_id,
        request=archive_request,
    )

    assert replay.status is ArchiveStatus.succeeded
    assert replayed.archive_manifest_digest == archived.archive_manifest_digest
    assert archived.claim_binding.application_id == application_id
    assert archived.claim_binding.test_run_ids == [
        test_run_id,
        second_test_run_id,
    ]
    assert archived.claim_binding.business_run_ids == [business_run_id]
    assert archived.claim_binding.published_version is None
    assert forged_receipt.artifact.evidence_kind == "artifact"
    assert receipt.artifact.evidence_kind == "host_receipt"
    assert receipt.artifact.provenance.source == "platform_host_write"
    assert receipt.artifact.provenance.run_id == business_run_id
    assert [item.digest for item in archived.artifact_refs] == (
        archived.claim_binding.artifact_digests
    )
    assert [item.digest for item in archived.host_receipt_refs] == (
        archived.claim_binding.host_receipt_digests
    )
    run_root = (
        tmp_path
        / "sealed-task-state"
        / "packages"
        / task.task_id
        / str(task.revision)
        / "runs"
        / task.run_id
    )
    collaboration_lines = [
        json.loads(line) for line in (run_root / "collaboration.jsonl").read_text().splitlines()
    ]
    platform_lines = [
        json.loads(line) for line in (run_root / "platform-events.jsonl").read_text().splitlines()
    ]
    evidence_index = json.loads((run_root / "evidence-index.json").read_text())
    archived_preflight = [
        json.loads(
            (
                run_root
                / "environment-preflight"
                / "environment-preflight.json"
            ).read_text()
        ),
        json.loads(
            (
                run_root
                / "environment-preflight"
                / "environment-preflight-attempt-0002.json"
            ).read_text()
        ),
        json.loads((run_root / "environment-ready.json").read_text()),
    ]
    assert [
        archived_preflight[0]["attempt"],
        archived_preflight[1]["attempt"],
        3 if archived_preflight[2]["ready"] else -1,
    ] == [1, 2, 3]
    assert {
        item.path
        for item in replay.files
        if item.path.startswith("environment-preflight/")
        or item.path == "environment-ready.json"
    } == {
        "environment-preflight/environment-preflight.json",
        (
            "environment-preflight/"
            "environment-preflight-attempt-0002.json"
        ),
        "environment-ready.json",
    }
    assert [item["kind"] for item in collaboration_lines] == [
        "message",
        "claim.prepared",
    ]
    archived_runs = {
        item["payload"]["platform_run_id"]: item["payload"]
        for item in platform_lines
        if item["kind"] == "run.started"
    }
    assert set(archived_runs) == {
        test_run_id,
        second_test_run_id,
        business_run_id,
        failed_run_id,
    }
    assert archived_runs[business_run_id]["outputs"] == {"result": {"status": "completed"}}
    assert archived_runs[business_run_id]["durable_events"][-1]["data"] == {"status": "completed"}
    assert archived_runs[failed_run_id]["status"] == "failed"
    assert archived_runs[failed_run_id]["outputs"] == {"partial": {"rows": 1}}
    assert archived_runs[failed_run_id]["error"] == "fixture_failure"
    assert archived_runs[failed_run_id]["durable_events"][-1]["data"] == {
        "status": "failed",
        "error": {"code": "fixture_failure"},
    }
    assert {item["kind"] for item in evidence_index["entries"]} == {"artifact", "host_receipt"}
    assert (run_root / f"artifacts/{artifact.artifact.artifact_id}.bin").read_bytes() == (
        artifact_payload
    )
    assert (
        run_root / f"host-receipts/{receipt.artifact.artifact_id}.bin"
    ).read_bytes() == receipt_payload

    binding = archived.claim_binding
    stored_claim = await collaboration.submit_verification_claim(
        principal=principal,
        channel_id=access.channel_id,
        request=VerificationClaimRequest(
            idempotency_key="formal-archive-claim-0001",
            expected_channel_revision=1,
            claim=archived.verification_claim,
        ),
    )
    assert stored_claim["claim_id"] == str(binding.claim_id)
    assert stored_claim["archive_manifest_digest"] == archived.archive_manifest_digest


@pytest.mark.asyncio
async def test_success_denominator_rejects_current_failed_business_attempt(
    tmp_path: Path,
) -> None:
    runtime, _, _, package, _ = await _runtime_parts(tmp_path)
    storage = Storage(tmp_path / "platform")
    await storage.initialize()
    workflow = WorkflowStorage(storage)
    await workflow.initialize()
    application_id = await empty_application(
        workflow,
        "formal-current-failure-denominator",
    )
    request = _request().model_copy(update={"application_id": application_id})
    with _real_health_endpoints(package):
        prepared = await runtime.prepare_async(request)
    draft = await workflow.get_draft(str(application_id))
    snapshot = draft["snapshot"]
    revision = int(draft["revision"])
    common = {
        "application_id": str(application_id),
        "version": None,
        "draft_revision": revision,
        "state": {
            "assignment_id": str(request.assignment_id),
            "session_id": str(request.session_id),
            "snapshot": snapshot.model_dump(mode="json", exclude_none=True),
        },
    }
    test_row = {
        **common,
        "id": "test-run:current-denominator",
        "status": "succeeded",
    }
    failed_row = {
        **common,
        "id": "business-run:current-failed-denominator",
        "status": "failed",
    }
    with pytest.raises(
        FormalRunArchiveError,
        match="every current business run",
    ):
        FormalRunArchiveCoordinator._classified_runs(  # noqa: SLF001
            run_rows=[test_row, failed_row],
            reported_test_run_ids={"test-run:current-denominator"},
            blackbox_test_run_ids={"test-run:current-denominator"},
            blackbox_business_run_ids={
                "business-run:current-failed-denominator"
            },
            assignment=prepared.assignment,
            session_id=request.session_id,
            draft_revision=revision,
            content_hash=f"sha256:{snapshot.content_hash()}",
            published_version=None,
        )

    historical_failed = {
        **failed_row,
        "draft_revision": revision + 1,
    }
    current_business = {
        **common,
        "id": "business-run:current-succeeded-denominator",
        "status": "succeeded",
    }
    _, business = FormalRunArchiveCoordinator._classified_runs(  # noqa: SLF001
        run_rows=[test_row, historical_failed, current_business],
        reported_test_run_ids={"test-run:current-denominator"},
        blackbox_test_run_ids={"test-run:current-denominator"},
        blackbox_business_run_ids={
            "business-run:current-failed-denominator",
            "business-run:current-succeeded-denominator",
        },
        assignment=prepared.assignment,
        session_id=request.session_id,
        draft_revision=revision,
        content_hash=f"sha256:{snapshot.content_hash()}",
        published_version=None,
    )
    assert [item["id"] for item in business] == [
        "business-run:current-succeeded-denominator"
    ]
    earlier_test = {
        **common,
        "id": "test-run:earlier-current-denominator",
        "status": "succeeded",
    }
    tests, business = FormalRunArchiveCoordinator._classified_runs(  # noqa: SLF001
        run_rows=[earlier_test, test_row, current_business],
        reported_test_run_ids={"test-run:current-denominator"},
        blackbox_test_run_ids={
            "test-run:earlier-current-denominator",
            "test-run:current-denominator",
        },
        blackbox_business_run_ids={
            "business-run:current-succeeded-denominator"
        },
        assignment=prepared.assignment,
        session_id=request.session_id,
        draft_revision=revision,
        content_hash=f"sha256:{snapshot.content_hash()}",
        published_version=None,
    )
    assert [item["id"] for item in tests] == [
        "test-run:earlier-current-denominator",
        "test-run:current-denominator",
    ]
    assert [item["id"] for item in business] == [
        "business-run:current-succeeded-denominator"
    ]
    with pytest.raises(
        FormalRunArchiveError,
        match="no public operation provenance",
    ):
        FormalRunArchiveCoordinator._classified_runs(  # noqa: SLF001
            run_rows=[
                test_row,
                {
                    **common,
                    "id": "run:unattributed-current-denominator",
                    "status": "succeeded",
                },
            ],
            reported_test_run_ids={"test-run:current-denominator"},
            blackbox_test_run_ids={"test-run:current-denominator"},
            blackbox_business_run_ids=set(),
            assignment=prepared.assignment,
            session_id=request.session_id,
            draft_revision=revision,
            content_hash=f"sha256:{snapshot.content_hash()}",
            published_version=None,
        )


@pytest.mark.asyncio
async def test_real_formal_intent_completion_archives_and_freezes_claim_automatically(
    tmp_path: Path,
) -> None:
    runtime, collaboration, harness, package, _ = await _runtime_parts(tmp_path)
    storage, workflow, _, auth = await platform_parts(tmp_path)
    daemon = FormalDaemonClient()
    bridge = LocalLiliesBridge(
        enabled=True,
        store=LocalLiliesBridgeStore(tmp_path / "platform" / "local-lilies-bridge.db"),
        workflow_storage=workflow,
        harness=harness,
        auth_store=auth,
        client=daemon,
        platform_base_url="http://127.0.0.1:8001",
        contract_digest_provider=lambda _scopes, _apps: DIGEST,
        formal_assignment_broker=runtime,
        formal_credential_secret_provider=runtime.collaboration_credential_secret,
        formal_channel_close_provider=runtime.close_collaboration_authority,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "formal-real-success-outbox")
    with _real_health_endpoints(package):
        running = await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )
    assignment = daemon.submitted_assignments[-1]
    task = assignment.task_package
    access = assignment.collaboration
    assert task is not None
    assert access is not None

    tool_call_id = "formal-real-draft-apply-tool-call"
    platform_token = daemon.credentials[assignment.platform.credential_ref]["secret"]
    draft = await workflow.get_draft(str(application_id))
    updated_snapshot = draft["snapshot"].model_copy(deep=True)
    updated_snapshot.description = (
        f"{updated_snapshot.description} Authored by the formal Lilies call."
    ).strip()
    idempotency_key = "formal-real-draft-apply-0001"
    request_id = uuid4()
    edit_data = {"description": updated_snapshot.description}
    request_payload = {
        "application_id": str(application_id),
        "idempotency_key": idempotency_key,
        "expected_revision": int(draft["revision"]),
        "op": "set_metadata",
        "data": edit_data,
    }
    authorized = await auth.authorize_request(
        platform_token,
        BlackboxAuthorizationRequest(
            request_id=request_id,
            assignment_id=running.assignment_id,
            session_id=running.session_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
            application_id=application_id,
            operation=PlatformBlackboxOperation.draft_apply,
            contract_digest=DIGEST,
            payload=request_payload,
        ),
    )
    operation_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            {
                "application_id": str(application_id),
                "expected_revision": int(draft["revision"]),
                "op": "set_metadata",
                "data": edit_data,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    edit_result = await workflow.save_draft(
        str(application_id),
        updated_snapshot,
        expected_revision=int(draft["revision"]),
        idempotency_key=idempotency_key,
        change_context={"operation": "set_metadata"},
        idempotency_digest=operation_digest,
        formal_mutation_context={
            "assignment_id": str(running.assignment_id),
            "session_id": str(running.session_id),
            "application_id": str(application_id),
            "request_id": str(request_id),
            "tool_call_id": tool_call_id,
            "operation": "set_metadata",
            "request_payload_digest": authorized.payload_digest,
        },
    )
    await auth.complete_request(
        authorized.authorization_id,
        status_code=200,
        result={
            "revision": int(edit_result["revision"]),
            "content_hash": str(edit_result["content_hash"]),
        },
    )

    test_run_id = "test-run:real-success-outbox"
    business_run_id = "business-run:real-success-outbox"
    test_workspace = tmp_path / "real-run-workspaces" / "test"
    business_workspace = tmp_path / "real-run-workspaces" / "business"
    test_workspace.mkdir(parents=True)
    business_workspace.mkdir(parents=True)
    tests_tool_call_id = "formal-real-tests-run-tool-call"
    tests_idempotency_key = "formal-real-tests-run-0001"
    tests_payload = {
        "application_id": str(application_id),
        "idempotency_key": tests_idempotency_key,
    }
    tests_authorized = await auth.authorize_request(
        platform_token,
        BlackboxAuthorizationRequest(
            request_id=uuid4(),
            assignment_id=running.assignment_id,
            session_id=running.session_id,
            tool_call_id=tests_tool_call_id,
            idempotency_key=tests_idempotency_key,
            application_id=application_id,
            operation=PlatformBlackboxOperation.tests_run,
            contract_digest=DIGEST,
            payload=tests_payload,
        ),
    )
    await _completed_platform_run(
        storage=storage,
        workflow=workflow,
        run_id=test_run_id,
        application_id=application_id,
        assignment_id=running.assignment_id,
        session_id=running.session_id,
        workspace=test_workspace,
    )
    draft = await workflow.get_draft(str(application_id))
    await workflow.mark_tested(
        str(application_id),
        int(draft["revision"]),
        str(draft["content_hash"]),
        {
            "passed": True,
            "validation": {
                "valid": True,
                "content_hash": draft["content_hash"],
            },
            "tests": [
                {
                    "test_id": "acceptance-real-outbox",
                    "run_id": test_run_id,
                    "run_status": "succeeded",
                    "passed": True,
                }
            ],
        },
    )
    await auth.complete_request(
        tests_authorized.authorization_id,
        status_code=200,
        result={
            "ok": True,
            "operation": PlatformBlackboxOperation.tests_run.value,
            "data": {
                "passed": True,
                "tests": [
                    {
                        "test_id": "acceptance-real-outbox",
                        "run_id": test_run_id,
                        "run_status": "succeeded",
                        "passed": True,
                    }
                ],
            },
        },
    )
    business_tool_call_id = "formal-real-run-start-tool-call"
    business_idempotency_key = "formal-real-run-start-0001"
    business_payload = {
        "application_id": str(application_id),
        "idempotency_key": business_idempotency_key,
        "inputs": {},
        "use_draft": True,
    }
    business_authorized = await auth.authorize_request(
        platform_token,
        BlackboxAuthorizationRequest(
            request_id=uuid4(),
            assignment_id=running.assignment_id,
            session_id=running.session_id,
            tool_call_id=business_tool_call_id,
            idempotency_key=business_idempotency_key,
            application_id=application_id,
            operation=PlatformBlackboxOperation.run_start,
            contract_digest=DIGEST,
            payload=business_payload,
        ),
    )
    await _completed_platform_run(
        storage=storage,
        workflow=workflow,
        run_id=business_run_id,
        application_id=application_id,
        assignment_id=running.assignment_id,
        session_id=running.session_id,
        workspace=business_workspace,
    )
    await auth.complete_request(
        business_authorized.authorization_id,
        status_code=202,
        result={
            "ok": True,
            "operation": PlatformBlackboxOperation.run_start.value,
            "data": {
                "run_id": business_run_id,
                "status": "running",
                "version": None,
                "draft_revision": int(draft["revision"]),
            },
        },
    )
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    (business_workspace / "result.json").write_bytes(b'{"result":"real-success-outbox"}\n')
    business_artifact = await artifacts.register_artifact(
        ArtifactRegistrationRequest(
            binding=ArtifactBinding(
                assignment_id=running.assignment_id,
                session_id=running.session_id,
                application_id=application_id,
                run_id=business_run_id,
            ),
            relative_path="result.json",
            media_type="application/json",
        ),
        artifact_root=business_workspace,
    )
    (business_workspace / "host-receipt.json").write_bytes(
        b'{"receipt":"real-success-host-write"}\n'
    )
    business_receipt = await artifacts.register_host_receipt(
        HostReceiptRegistrationRequest(
            binding=ArtifactBinding(
                assignment_id=running.assignment_id,
                session_id=running.session_id,
                application_id=application_id,
                run_id=business_run_id,
            ),
            relative_path="host-receipt.json",
            media_type="application/json",
            receipt_id="real-success-host-write-receipt",
            operation="paperless.metadata.update",
        ),
        artifact_root=business_workspace,
    )
    source_provenance = _clean_source_provenance(
        tmp_path,
        assignment_id=assignment.assignment_id,
        channel_id=access.channel_id,
        task_id=task.task_id,
        task_revision=task.revision,
        run_id=task.run_id,
        captured_at=assignment.created_at,
    )
    coordinator = FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge.store,
        collaboration_store=collaboration.store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=auth,
        connector_service=(
            connector_budget_audit := _ConnectorBudgetAudit()
        ),
        source_provenance=source_provenance,
    )
    connector_budget_audit.record_write(
        assignment.assignment_id,
        execution_id="real-success-host-write-receipt",
        operation_id="paperless.metadata.update",
    )
    bridge.formal_archive_intent_validator = (
        lambda channel_id, requested: (
            coordinator.validate_success_archive_intent(
                channel_id=channel_id,
                request=requested,
            )
        )
    )
    archive_errors: list[str] = []

    async def freeze_intent(
        channel: CollaborationChannel,
        requested: FormalRunArchivePreparationRequest,
        actor_id: str,
    ) -> Any:
        return await bridge.freeze_formal_run_archive_intent(
            channel=channel,
            request=requested,
            actor_id=actor_id,
        )

    async def archive_success(
        channel_id: UUID,
        requested: FormalRunArchivePreparationRequest,
    ) -> FormalRunArchivePreparationResult:
        try:
            return await coordinator.prepare_success_archive(
                channel_id=channel_id,
                request=requested,
            )
        except FormalRunArchiveError as error:
            archive_errors.append(str(error))
            raise

    async def persist_claim(
        channel_id: UUID,
        actor_id: str,
        requested: FormalRunArchivePreparationRequest,
        result: FormalRunArchivePreparationResult,
    ) -> Any:
        return await collaboration.finalize_formal_archive_claim(
            channel_id=channel_id,
            actor_id=actor_id,
            archive_request=requested,
            archive_result=result,
        )

    collaboration._formal_archive_provider = freeze_intent
    bridge.formal_success_archive_provider = archive_success
    bridge.formal_verification_claim_provider = persist_claim
    token = await runtime.collaboration_credential_secret(
        assignment,
        running.session_id,
    )
    principal = await collaboration.authenticate_lilies(
        token.get_secret_value(),
        channel_id=access.channel_id,
        required_scope=CollaborationScope.report_write.value,
    )
    channel = CollaborationChannel.model_validate(
        await collaboration.store.get_channel(access.channel_id)
    )
    archive_request = FormalRunArchivePreparationRequest(
        expected_channel_revision=channel.revision,
        claim_id=uuid4(),
        test_run_ids=[test_run_id],
        business_run_ids=[business_run_id],
        artifact_ids=[business_artifact.artifact.artifact_id],
        host_receipt_ids=[business_receipt.artifact.artifact_id],
        remaining_limits=["controlled local fixture only"],
        summary="The platform archives and freezes this claim after terminal drain.",
        idempotency_key="formal-real-success-intent-0001",
    )
    with pytest.raises(FormalRunArchiveIntentInvalid) as incomplete_intent:
        await bridge.freeze_formal_run_archive_intent(
            channel=channel,
            request=archive_request.model_copy(
                update={"artifact_ids": []}
            ),
            actor_id="frozen-lilies-actor",
        )
    assert (
        incomplete_intent.value.code
        == "formal_archive_required_artifact_missing"
    )
    before_valid_intent = await bridge.store.get_assignment(
        assignment.assignment_id
    )
    assert before_valid_intent["formal_archive_intent_digest"] is None
    intent = await collaboration.prepare_formal_run_archive(
        principal=principal,
        channel_id=channel.channel_id,
        request=archive_request,
    )
    replayed_intent = await collaboration.prepare_formal_run_archive(
        principal=principal,
        channel_id=channel.channel_id,
        request=archive_request,
    )
    assert replayed_intent == intent
    assert intent["state"] == "awaiting_daemon_completion"

    daemon.events = [
        {
            "seq": 1,
            "event": "assignment.accepted",
            "data": {"assignment_id": str(assignment.assignment_id)},
        },
        {
            "seq": 2,
            "event": "tool.started",
            "data": {
                "assignment_id": str(assignment.assignment_id),
                "tool_call_id": tool_call_id,
                "tool": "platform_draft_apply",
            },
        },
        {
            "seq": 3,
            "event": "tool.completed",
            "data": {
                "assignment_id": str(assignment.assignment_id),
                "tool_call_id": tool_call_id,
                "tool": "platform_draft_apply",
                "is_error": False,
            },
        },
        {
            "seq": 4,
            "event": "tool.started",
            "data": {
                "assignment_id": str(assignment.assignment_id),
                "tool_call_id": tests_tool_call_id,
                "tool": "platform_tests_run",
            },
        },
        {
            "seq": 5,
            "event": "tool.completed",
            "data": {
                "assignment_id": str(assignment.assignment_id),
                "tool_call_id": tests_tool_call_id,
                "tool": "platform_tests_run",
                "is_error": False,
            },
        },
        {
            "seq": 6,
            "event": "tool.started",
            "data": {
                "assignment_id": str(assignment.assignment_id),
                "tool_call_id": business_tool_call_id,
                "tool": "platform_run_start",
            },
        },
        {
            "seq": 7,
            "event": "tool.completed",
            "data": {
                "assignment_id": str(assignment.assignment_id),
                "tool_call_id": business_tool_call_id,
                "tool": "platform_run_start",
                "is_error": False,
            },
        },
        {
            "seq": 8,
            "event": "session.status_changed",
            "data": {
                "assignment_id": str(assignment.assignment_id),
                "from_status": "running",
                "to_status": "completed",
            },
        },
    ]
    daemon.sessions[str(running.session_id)]["status"] = "completed"

    relayed = await bridge.relay_events(running.assignment_id)
    persisted = await bridge.store.get_assignment(running.assignment_id)
    assert persisted["formal_archive_result_json"] is not None, (
        persisted["phase"],
        persisted["status"],
        persisted["last_error_code"],
        persisted["last_error_message"],
        archive_errors,
    )
    archived = FormalRunArchivePreparationResult.model_validate_json(
        persisted["formal_archive_result_json"]
    )
    stored_claim = VerificationClaim.model_validate(
        await collaboration.store.get_claim(archive_request.claim_id)
    )
    build = await workflow.get_build(str(running.build_id))

    assert relayed.assignment.phase.value == "completed"
    assert relayed.assignment.status == "verification_pending"
    assert persisted["formal_archive_completed_at"] is not None
    assert build["status"] == "verification_pending"
    assert stored_claim.status is ClaimStatus.frozen
    assert stored_claim.claim_id == archive_request.claim_id
    assert stored_claim.archive_manifest_digest == archived.archive_manifest_digest
    assert stored_claim.archive_manifest_digest == (
        archived.verification_claim.archive_manifest_digest
    )
    assert coordinator.replay(archived).status is ArchiveStatus.succeeded


@pytest.mark.parametrize(
    "crash_stage",
    [
        None,
        "transient_archive_race",
        "formal.success_archive.created_before_commit",
        "formal.verification_claim.committed_before_build_projection",
        "sealed_invalid_success",
        "sealed_invalid_success_checkpoint_crash",
    ],
)
@pytest.mark.asyncio
async def test_frozen_archive_intent_is_consumed_once_after_authenticated_completion(
    tmp_path: Path,
    crash_stage: str | None,
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
    application_id = await empty_application(workflow, "formal-success-outbox")
    with _real_health_endpoints(package):
        running = await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )
    assignment = daemon.submitted_assignments[-1]
    task = assignment.task_package
    access = assignment.collaboration
    assert task is not None
    assert access is not None
    collaboration_store = CollaborationStore(storage.db_path)
    await collaboration_store.initialize()
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    coordinator = FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge.store,
        collaboration_store=collaboration_store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=auth,
        connector_service=_ConnectorBudgetAudit(),
    )
    bridge.formal_terminal_archive_provider = coordinator.archive_terminal_assignment
    channel = CollaborationChannel(
        channel_id=access.channel_id,
        task_id=task.task_id,
        task_revision=task.revision,
        assignment_id=assignment.assignment_id,
        lilies_session_id=running.session_id,
        application_ids=assignment.platform.application_ids,
        status=ChannelStatus.active,
        revision=1,
        next_seq=1,
        created_at=assignment.created_at,
    )
    claim_id = uuid4()
    request = FormalRunArchivePreparationRequest(
        expected_channel_revision=1,
        claim_id=claim_id,
        test_run_ids=["test-run-001"],
        business_run_ids=["business-run-001"],
        remaining_limits=["controlled local fixture"],
        summary="Frozen before the daemon reports completion.",
        idempotency_key="formal-success-intent-0001",
    )
    binding = ArchiveClaimBinding(
        claim_id=claim_id,
        assignment_id=assignment.assignment_id,
        application_id=application_id,
        draft_revision=0,
        content_hash=DIGEST,
        test_run_ids=request.test_run_ids,
        business_run_ids=request.business_run_ids,
        remaining_limits=request.remaining_limits,
    )
    claim_payload_data: dict[str, Any] = {
        "schema_version": "1.1",
        "claim_id": str(claim_id),
        "application_id": str(application_id),
        "draft_revision": 0,
        "content_hash": DIGEST,
        "test_run_ids": request.test_run_ids,
        "business_run_ids": request.business_run_ids,
        "artifact_refs": [],
        "host_receipt_refs": [],
        "resolved_report_ids": [],
        "remaining_limits": request.remaining_limits,
        "task_package_digest": task.public_summary_digest,
        "environment_ready_digest": DIGEST,
        "archive_manifest_digest": DIGEST,
        "verification_process_digest": DIGEST,
        "validation_mode": "real_host",
        "claim": "ready_for_independent_verification",
    }
    claim_payload_data["frozen_context_digest"] = frozen_claim_context_digest(claim_payload_data)
    claim_payload = VerificationClaimPayload.model_validate(claim_payload_data)
    archived = FormalRunArchivePreparationResult(
        task_id=task.task_id,
        revision=task.revision,
        run_id=task.run_id,
        assignment_id=assignment.assignment_id,
        channel_id=channel.channel_id,
        public_summary_digest=task.public_summary_digest,
        environment_ready_digest=DIGEST,
        workspace_mount_digest=DIGEST,
        archive_manifest_digest=DIGEST,
        claim_binding=binding,
        verification_claim=claim_payload,
    )
    archive_calls = 0
    claim_calls = 0
    sealed_invalid_result = FormalTerminalArchiveResult(
        task_id=task.task_id,
        revision=task.revision,
        run_id=task.run_id,
        assignment_id=assignment.assignment_id,
        status=ArchiveStatus.invalid,
        archive_manifest_digest=DIGEST,
    )

    async def archive_success(
        requested_channel_id: UUID,
        requested: FormalRunArchivePreparationRequest,
    ) -> FormalRunArchivePreparationResult:
        nonlocal archive_calls
        archive_calls += 1
        assert requested_channel_id == channel.channel_id
        assert requested == request
        if crash_stage == "transient_archive_race" and archive_calls == 1:
            raise FormalRunArchiveUnavailable(
                "formal durable stores changed during archive export"
            )
        if crash_stage in {
            "sealed_invalid_success",
            "sealed_invalid_success_checkpoint_crash",
        }:
            raise FormalRunArchiveInvalid(
                "platform-owned success archive was marked invalid",
                result=sealed_invalid_result,
            )
        return archived

    async def persist_claim(
        requested_channel_id: UUID,
        actor_id: str,
        requested: FormalRunArchivePreparationRequest,
        result: FormalRunArchivePreparationResult,
    ) -> VerificationClaim:
        nonlocal claim_calls
        claim_calls += 1
        assert requested_channel_id == channel.channel_id
        assert actor_id == "lilies:formal-test"
        assert requested == request
        assert result == archived
        return VerificationClaim.model_validate(
            {
                **claim_payload.model_dump(mode="json", exclude_none=True),
                "channel_id": str(channel.channel_id),
                "assignment_id": str(assignment.assignment_id),
                "status": ClaimStatus.frozen.value,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    bridge.formal_success_archive_provider = archive_success
    bridge.formal_verification_claim_provider = persist_claim
    intent = await bridge.freeze_formal_run_archive_intent(
        channel=channel,
        request=request,
        actor_id="lilies:formal-test",
    )
    replayed_intent = await bridge.freeze_formal_run_archive_intent(
        channel=channel,
        request=request,
        actor_id="lilies:formal-test",
    )
    assert intent.replayed is False
    assert replayed_intent == intent

    daemon.events = [
        {
            "seq": 1,
            "event": "assignment.accepted",
            "data": {"assignment_id": str(assignment.assignment_id)},
        },
        {
            "seq": 2,
            "event": "session.status_changed",
            "data": {
                "assignment_id": str(assignment.assignment_id),
                "from_status": "running",
                "to_status": "completed",
            },
        },
    ]
    daemon.sessions[str(running.session_id)]["status"] = "completed"

    if crash_stage in {None, "sealed_invalid_success"}:
        completed = await bridge.relay_events(running.assignment_id)
    elif crash_stage == "transient_archive_race":
        with pytest.raises(
            LocalLiliesBridgeUnavailable,
            match="formal success archive is pending",
        ):
            await bridge.relay_events(running.assignment_id)
        pending = await bridge.store.get_assignment(running.assignment_id)
        assert pending["phase"] == "completed"
        assert pending["last_error_code"] == "formal_archive_pending"
        assert pending["formal_archive_result_json"] is None
        completed = await bridge.resume_assignment(running.assignment_id)
    elif crash_stage == "sealed_invalid_success_checkpoint_crash":
        fault_stage = "formal.invalid_success_archive.before_checkpoint"
        bridge.fault_hook = CrashOnce(fault_stage)
        with pytest.raises(InjectedCrash, match=fault_stage):
            await bridge.relay_events(running.assignment_id)
        interrupted = await bridge.store.get_assignment(running.assignment_id)
        assert interrupted["phase"] == "error"
        assert interrupted["status"] == "invalid"
        assert interrupted["formal_terminal_archive_completed_at"] is None

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
            lambda _assignment_id: sealed_invalid_result
        )
        restarted.formal_success_archive_provider = archive_success
        restarted.formal_verification_claim_provider = persist_claim
        await restarted.initialize()
        recovery = await restarted.recover_pending_assignments()
        assert recovery.scanned == 1
        assert recovery.failed == 1
        bridge = restarted
        completed = await bridge.get_assignment(running.assignment_id)
    else:
        bridge.fault_hook = CrashOnce(crash_stage)
        with pytest.raises(InjectedCrash, match=crash_stage):
            await bridge.relay_events(running.assignment_id)
        interrupted = await bridge.store.get_assignment(running.assignment_id)
        assert interrupted["phase"] == "completed"
        assert interrupted["terminal_events_drained_at"] is not None
        assert interrupted["formal_archive_completed_at"] is None
        if crash_stage == "formal.success_archive.created_before_commit":
            assert interrupted["formal_archive_result_json"] is None
        else:
            assert interrupted["formal_archive_result_json"] is not None
            assert interrupted["formal_claim_result_json"] is not None

        restarted = make_bridge(
            tmp_path,
            workflow=workflow,
            harness=harness,
            auth=auth,
            daemon=daemon,
            broker=broker,
            providers=providers,
        )
        restarted.formal_terminal_archive_provider = coordinator.archive_terminal_assignment
        restarted.formal_success_archive_provider = archive_success
        restarted.formal_verification_claim_provider = persist_claim
        await restarted.initialize()
        recovery = await restarted.recover_pending_assignments()
        assert recovery.scanned == 1
        assert recovery.recovered == 1
        bridge = restarted
        completed = await bridge.get_assignment(running.assignment_id)

    if crash_stage in {
        "sealed_invalid_success",
        "sealed_invalid_success_checkpoint_crash",
    }:
        persisted = await bridge.store.get_assignment(running.assignment_id)
        terminal_result = FormalTerminalArchiveResult.model_validate_json(
            persisted["formal_terminal_archive_result_json"]
        )
        build = await workflow.get_build(str(running.build_id))
        completed_assignment = (
            completed.assignment
            if hasattr(completed, "assignment")
            else completed
        )

        assert completed_assignment.phase.value == "failed"
        assert completed_assignment.status == "invalid"
        assert persisted["phase"] == "error"
        assert persisted["status"] == "invalid"
        assert persisted["last_error_code"] == "formal_archive_invalid"
        assert persisted["formal_archive_result_json"] is None
        assert persisted["formal_claim_result_json"] is None
        assert persisted["formal_terminal_archive_completed_at"] is not None
        assert terminal_result.status is ArchiveStatus.invalid
        assert terminal_result.archive_manifest_digest == DIGEST
        assert build["status"] == "invalid"
        assert archive_calls == 1
        assert claim_calls == 0
        assert (await bridge.recover_pending_assignments()).scanned == 0
        return

    replayed_completion = await bridge.relay_events(running.assignment_id)
    persisted = await bridge.store.get_assignment(running.assignment_id)

    completed_assignment = completed.assignment if hasattr(completed, "assignment") else completed
    assert completed_assignment.phase.value == "completed"
    assert replayed_completion.assignment.phase.value == "completed"
    assert replayed_completion.relay_cursor == replayed_completion.ack_cursor == 2
    assert persisted["terminal_events_drained_at"] is not None
    assert persisted["formal_archive_result_json"] is not None
    assert persisted["formal_claim_result_json"] is not None
    assert persisted["formal_archive_completed_at"] is not None
    assert persisted["status"] == "verification_pending"
    assert archive_calls == (
        2
        if crash_stage
        in {
            "transient_archive_race",
            "formal.success_archive.created_before_commit",
        }
        else 1
    )
    assert claim_calls == 1


@pytest.mark.parametrize("intent_state", ["missing", "corrupt"])
@pytest.mark.asyncio
async def test_completed_formal_archive_invalid_intent_fails_closed_without_retry_loop(
    tmp_path: Path,
    intent_state: str,
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
    application_id = await empty_application(workflow, f"formal-{intent_state}-intent")
    with _real_health_endpoints(package):
        running = await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )
    assignment = daemon.submitted_assignments[-1]
    task = assignment.task_package
    access = assignment.collaboration
    assert task is not None
    assert access is not None
    collaboration_store = CollaborationStore(storage.db_path)
    await collaboration_store.initialize()
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    coordinator = FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge.store,
        collaboration_store=collaboration_store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=auth,
        connector_service=_ConnectorBudgetAudit(),
        source_provenance=_submitted_source_provenance(
            tmp_path,
            daemon,
            running.assignment_id,
            name=f"invalid-intent-{intent_state}",
        ),
    )
    bridge.formal_terminal_archive_provider = coordinator.archive_terminal_assignment

    if intent_state == "corrupt":
        channel = CollaborationChannel(
            channel_id=access.channel_id,
            task_id=task.task_id,
            task_revision=task.revision,
            assignment_id=assignment.assignment_id,
            lilies_session_id=running.session_id,
            application_ids=assignment.platform.application_ids,
            status=ChannelStatus.active,
            revision=1,
            next_seq=1,
            created_at=assignment.created_at,
        )
        await bridge.freeze_formal_run_archive_intent(
            channel=channel,
            request=FormalRunArchivePreparationRequest(
                expected_channel_revision=1,
                claim_id=uuid4(),
                test_run_ids=["test-run-invalid-intent"],
                business_run_ids=["business-run-invalid-intent"],
                summary="This frozen intent is corrupted after its durable commit.",
                idempotency_key="formal-invalid-intent-0001",
            ),
            actor_id="lilies:formal-test",
        )
        await bridge.store.update_assignment(
            running.assignment_id,
            formal_archive_intent_json="{}",
        )

    daemon.events = [
        {
            "seq": 1,
            "event": "assignment.accepted",
            "data": {"assignment_id": str(assignment.assignment_id)},
        },
        {
            "seq": 2,
            "event": "session.status_changed",
            "data": {
                "assignment_id": str(assignment.assignment_id),
                "from_status": "running",
                "to_status": "completed",
            },
        },
    ]
    daemon.sessions[str(running.session_id)]["status"] = "completed"

    if intent_state == "missing":
        crash_stage = "formal.terminal_events_drained_before_archive"
        bridge.fault_hook = CrashOnce(crash_stage)
        with pytest.raises(InjectedCrash, match=crash_stage):
            await bridge.relay_events(running.assignment_id)
        interrupted = await bridge.store.get_assignment(running.assignment_id)
        assert interrupted["phase"] == "completed"
        assert interrupted["terminal_events_drained_at"] is not None
        assert interrupted["formal_archive_intent_json"] is None
        restarted = make_bridge(
            tmp_path,
            workflow=workflow,
            harness=harness,
            auth=auth,
            daemon=daemon,
            broker=broker,
            providers=providers,
        )
        restarted.formal_terminal_archive_provider = coordinator.archive_terminal_assignment
        await restarted.initialize()
        first_recovery = await restarted.recover_pending_assignments()
        assert first_recovery.scanned == 1
        assert first_recovery.failed == 1
        bridge = restarted
    else:
        relayed = await bridge.relay_events(running.assignment_id)
        assert relayed.assignment.phase.value == "failed"

    failed = await bridge.store.get_assignment(running.assignment_id)
    assert failed["phase"] == "error"
    assert failed["status"] == "failed"
    assert failed["last_error_code"] == (
        "formal_archive_intent_missing"
        if intent_state == "missing"
        else "formal_archive_intent_invalid"
    )
    assert failed["formal_archive_completed_at"] is None
    second_recovery = await bridge.recover_pending_assignments()
    assert second_recovery.scanned == 0


@pytest.mark.asyncio
async def test_preassignment_environment_failure_cancellation_is_archived(
    tmp_path: Path,
) -> None:
    storage, workflow, harness, auth = await platform_parts(tmp_path)
    providers = FormalProviders()
    daemon = FormalDaemonClient()
    broker, _package = make_broker(tmp_path, providers)
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
        "formal-preassignment-terminal",
    )
    request = formal_request(connection.connection_id)
    for _ in range(2):
        with pytest.raises(
            LocalLiliesBridgeUnavailable,
            match="may be re-probed",
        ):
            await bridge.start_formal_build(application_id, request)
    row = (
        await bridge.store.list_assignments_for_application(application_id)
    )[0]
    assert row["submission_json"] is None
    assert row["status"] == "environment_failed"

    collaboration_store = CollaborationStore(storage.db_path)
    await collaboration_store.initialize()
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    coordinator = FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge.store,
        collaboration_store=collaboration_store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=auth,
        connector_service=_ConnectorBudgetAudit(),
    )
    bridge.formal_terminal_archive_provider = (
        coordinator.archive_terminal_assignment
    )
    crash_stage = "formal.terminal_archive.created_before_commit"
    bridge.fault_hook = CrashOnce(crash_stage)
    with pytest.raises(InjectedCrash, match=crash_stage):
        await bridge.cancel_assignment(
            row["assignment_id"],
            idempotency_key="formal-preassignment-terminal-cancel-0001",
        )
    interrupted = await bridge.store.get_assignment(row["assignment_id"])
    first_archive = await coordinator.archive_terminal_assignment(
        UUID(row["assignment_id"])
    )
    assert interrupted["phase"] == "cancelled"
    assert interrupted["formal_terminal_archive_completed_at"] is None

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
    persisted = await restarted.store.get_assignment(row["assignment_id"])
    archived = await coordinator.archive_terminal_assignment(
        UUID(row["assignment_id"])
    )

    assert recovery.scanned == 1
    assert recovery.cancelled == 1
    assert persisted["formal_terminal_archive_completed_at"] is not None
    assert archived is not None
    assert archived == first_archive
    assert (await restarted.recover_pending_assignments()).scanned == 0
    replay = coordinator.replay(archived)
    assert replay.source_status is ArchiveStatus.cancelled
    assert replay.status is ArchiveStatus.invalid
    assert replay.forbidden_assistance_findings == [
        "scanner_inconclusive:preassignment:"
        "build_assignment_not_issued"
    ]
    run_root = (
        tmp_path
        / "sealed-task-state"
        / "packages"
        / archived.task_id
        / str(archived.revision)
        / "runs"
        / archived.run_id
    )
    assert (run_root / "reserved-assignment.json").is_file()
    assert not (run_root / "assignment.json").exists()
    assert (run_root / "preassignment-scan.json").is_file()
    assert (
        run_root
        / "environment-preflight"
        / "environment-preflight.json"
    ).is_file()
    assert (
        run_root
        / "environment-preflight"
        / "environment-preflight-attempt-0002.json"
    ).is_file()
    reservation = json.loads(
        (run_root / "reserved-assignment.json").read_text()
    )
    assert ArchivedFormalReservation.model_validate(
        {
            **reservation,
            "phase": "error",
            "status": "failed",
            "desired_state": "active",
        }
    ).phase == "error"
    for invalid_terminal_state in (
        {
            "phase": "cancelled",
            "status": "failed",
            "desired_state": "cancelled",
        },
        {
            "phase": "cancelled",
            "status": "cancelled",
            "desired_state": "active",
        },
        {
            "phase": "recorded",
            "status": "bad",
            "desired_state": "active",
        },
    ):
        with pytest.raises(ValueError):
            ArchivedFormalReservation.model_validate(
                {
                    **reservation,
                    **invalid_terminal_state,
                }
            )
    assert [
        item["path"] for item in reservation["preflight_evidence"]
    ] == [
        "environment-preflight/environment-preflight.json",
        (
            "environment-preflight/"
            "environment-preflight-attempt-0002.json"
        ),
    ]
    assert [
        json.loads((run_root / item["path"]).read_text())["attempt"]
        for item in reservation["preflight_evidence"]
    ] == [1, 2]
    assert json.loads(
        (run_root / "source-provenance/manifest.json").read_text()
    )["missing_reason"] == "source_baseline_not_established"


@pytest.mark.asyncio
async def test_cancelled_formal_bridge_automatically_preserves_terminal_archive(
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
    application_id = await empty_application(workflow, "formal-terminal-archive")
    request = formal_request(connection.connection_id)
    for _ in range(2):
        with pytest.raises(
            LocalLiliesBridgeUnavailable,
            match="may be re-probed",
        ):
            await bridge.start_formal_build(application_id, request)
    with _real_health_endpoints(package):
        running = await bridge.start_formal_build(
            application_id,
            request,
        )
    collaboration_store = CollaborationStore(storage.db_path)
    await collaboration_store.initialize()
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    coordinator = FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge.store,
        collaboration_store=collaboration_store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=auth,
        connector_service=_ConnectorBudgetAudit(),
        source_provenance=_submitted_source_provenance(
            tmp_path,
            daemon,
            running.assignment_id,
            name="terminal-cancelled",
        ),
    )
    bridge.formal_terminal_archive_provider = coordinator.archive_terminal_assignment

    cancelled = await bridge.cancel_assignment(
        running.assignment_id,
        idempotency_key="formal-terminal-archive-cancel-0001",
    )
    archived = await coordinator.archive_terminal_assignment(running.assignment_id)
    persisted = await bridge.store.get_assignment(running.assignment_id)

    assert cancelled.phase.value == "cancelled"
    assert archived is not None
    assert archived.status is ArchiveStatus.cancelled
    replay = coordinator.replay(archived)
    assert replay.source_status is ArchiveStatus.cancelled
    assert replay.status is ArchiveStatus.invalid
    assert replay.forbidden_assistance_findings
    assert persisted["formal_terminal_archive_result_json"] is not None
    assert persisted["formal_terminal_archive_manifest_digest"] == archived.archive_manifest_digest
    assert persisted["formal_terminal_archive_completed_at"] is not None
    run_root = (
        tmp_path
        / "sealed-task-state"
        / "packages"
        / archived.task_id
        / str(archived.revision)
        / "runs"
        / archived.run_id
    )
    assert (run_root / "assignment.json").is_file()
    assert (run_root / "bridge-assignment.json").is_file()
    assert (run_root / "platform-events.jsonl").is_file()
    assert (run_root / "forbidden-assistance-scan.json").is_file()
    assert (run_root / "scanner-inputs/bridge.json").is_file()
    assert (run_root / "scanner-inputs/collaboration.json").is_file()
    assert (run_root / "scanner-inputs/workflow.json").is_file()
    assert (run_root / "scanner-inputs/blackbox-auth.json").is_file()
    assert (run_root / "scanner-inputs/artifact-inventory.json").is_file()
    assert (run_root / "source-provenance/manifest.json").is_file()
    assert json.loads(
        (run_root / "scanner-inputs/collaboration.json").read_text()
    )["complete"] is False
    archived_preflight = [
        json.loads(
            (
                run_root
                / "environment-preflight"
                / "environment-preflight.json"
            ).read_text()
        ),
        json.loads(
            (
                run_root
                / "environment-preflight"
                / "environment-preflight-attempt-0002.json"
            ).read_text()
        ),
        json.loads((run_root / "environment-ready.json").read_text()),
    ]
    assert [
        archived_preflight[0]["attempt"],
        archived_preflight[1]["attempt"],
        3 if archived_preflight[2]["ready"] else -1,
    ] == [1, 2, 3]
    assert (run_root / "result.json").is_file()


@pytest.mark.asyncio
async def test_terminal_archive_preserves_failed_attempt_and_all_business_evidence(
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
    application_id = await empty_application(workflow, "formal-terminal-denominator")
    with _real_health_endpoints(package):
        running = await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )

    failed_run_id = "business-run:terminal-failed-attempt"
    failed_workspace = tmp_path / "terminal-failed-attempt"
    failed_workspace.mkdir(parents=True)
    await _failed_platform_run(
        storage=storage,
        workflow=workflow,
        run_id=failed_run_id,
        application_id=application_id,
        assignment_id=running.assignment_id,
        session_id=running.session_id,
        workspace=failed_workspace,
    )
    artifact_payload = b'{"partial":"failed-attempt-preserved"}\n'
    receipt_payload = b'{"receipt":"failed-host-attempt"}\n'
    (failed_workspace / "partial.json").write_bytes(artifact_payload)
    (failed_workspace / "receipt.json").write_bytes(receipt_payload)
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    binding = ArtifactBinding(
        assignment_id=running.assignment_id,
        session_id=running.session_id,
        application_id=application_id,
        run_id=failed_run_id,
    )
    artifact = await artifacts.register_artifact(
        ArtifactRegistrationRequest(
            binding=binding,
            relative_path="partial.json",
            media_type="application/json",
        ),
        artifact_root=failed_workspace,
    )
    receipt = await artifacts.register_host_receipt(
        HostReceiptRegistrationRequest(
            binding=binding,
            relative_path="receipt.json",
            media_type="application/json",
            receipt_id="failed-host-attempt-receipt",
            operation="inventree.part.update",
        ),
        artifact_root=failed_workspace,
    )
    collaboration_store = CollaborationStore(storage.db_path)
    await collaboration_store.initialize()
    coordinator = FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge.store,
        collaboration_store=collaboration_store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=auth,
        connector_service=_ConnectorBudgetAudit(),
        source_provenance=_submitted_source_provenance(
            tmp_path,
            daemon,
            running.assignment_id,
            name="terminal-denominator",
        ),
    )
    bridge.formal_terminal_archive_provider = coordinator.archive_terminal_assignment

    await bridge.cancel_assignment(
        running.assignment_id,
        idempotency_key="formal-terminal-denominator-cancel-0001",
    )
    archived = await coordinator.archive_terminal_assignment(running.assignment_id)
    assert archived is not None
    replay = coordinator.replay(archived)
    run_root = (
        tmp_path
        / "sealed-task-state"
        / "packages"
        / archived.task_id
        / str(archived.revision)
        / "runs"
        / archived.run_id
    )
    workflow_export = json.loads(
        (run_root / "scanner-inputs/workflow.json").read_text()
    )
    archived_result = json.loads((run_root / "result.json").read_text())
    platform_records = [
        json.loads(line)
        for line in (run_root / "platform-events.jsonl").read_text().splitlines()
    ]
    evidence_index = json.loads((run_root / "evidence-index.json").read_text())
    assistance_scan = json.loads(
        (run_root / "forbidden-assistance-scan.json").read_text()
    )

    assert replay.source_status is ArchiveStatus.cancelled
    assert replay.status is ArchiveStatus.invalid
    assert [item["id"] for item in workflow_export["runs"]] == [failed_run_id]
    assert workflow_export["runs"][0]["status"] == "failed"
    assert workflow_export["runs"][0]["outputs"] == {"partial": {"rows": 1}}
    assert workflow_export["runs"][0]["error"] == "fixture_failure"
    assert workflow_export["runs"][0]["events"][-1]["data"] == {
        "status": "failed",
        "error": {"code": "fixture_failure"},
    }
    assert archived_result["business_run_ids"] == [failed_run_id]
    failed_record = next(
        item
        for item in platform_records
        if item["payload"].get("platform_run_id") == failed_run_id
    )
    assert failed_record["payload"]["outputs"] == {"partial": {"rows": 1}}
    assert failed_record["payload"]["error"] == "fixture_failure"
    assert failed_record["payload"]["durable_events"][-1]["data"] == {
        "status": "failed",
        "error": {"code": "fixture_failure"},
    }
    assert {item["kind"] for item in evidence_index["entries"]} == {
        "artifact",
        "host_receipt",
    }
    assert (
        run_root / f"artifacts/{artifact.artifact.artifact_id}.bin"
    ).read_bytes() == artifact_payload
    assert (
        run_root / f"host-receipts/{receipt.artifact.artifact_id}.bin"
    ).read_bytes() == receipt_payload
    assert sorted(replay.forbidden_assistance_findings) == sorted(
        f"{item['rule_id']}:{item['source_ref']}"
        for item in assistance_scan["findings"]
    )


@pytest.mark.asyncio
async def test_terminal_archive_replays_after_archive_created_before_checkpoint_crash(
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
    application_id = await empty_application(workflow, "formal-terminal-checkpoint-crash")
    with _real_health_endpoints(package):
        running = await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )
    collaboration_store = CollaborationStore(storage.db_path)
    await collaboration_store.initialize()
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    coordinator = FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge.store,
        collaboration_store=collaboration_store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=auth,
        connector_service=_ConnectorBudgetAudit(),
        source_provenance=_submitted_source_provenance(
            tmp_path,
            daemon,
            running.assignment_id,
            name="terminal-checkpoint",
        ),
    )
    archive_calls = 0

    async def archive_terminal(assignment_id: UUID) -> Any:
        nonlocal archive_calls
        archive_calls += 1
        return await coordinator.archive_terminal_assignment(assignment_id)

    crash_stage = "formal.terminal_archive.created_before_commit"
    bridge.formal_terminal_archive_provider = archive_terminal
    bridge.fault_hook = CrashOnce(crash_stage)

    with pytest.raises(InjectedCrash, match=crash_stage):
        await bridge.cancel_assignment(
            running.assignment_id,
            idempotency_key="formal-terminal-checkpoint-crash-0001",
        )
    interrupted = await bridge.store.get_assignment(running.assignment_id)
    first_archive = await coordinator.archive_terminal_assignment(running.assignment_id)

    assert interrupted["phase"] == "cancelled"
    assert interrupted["status"] == "cancelled"
    assert interrupted["terminal_events_drained_at"] is not None
    assert interrupted["formal_terminal_archive_result_json"] is None
    assert interrupted["formal_terminal_archive_completed_at"] is None
    assert first_archive is not None
    assert first_archive.status is ArchiveStatus.cancelled

    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    restarted.formal_terminal_archive_provider = archive_terminal
    await restarted.initialize()
    recovery = await restarted.recover_pending_assignments()
    recovered = await restarted.store.get_assignment(running.assignment_id)
    replayed_archive = await coordinator.archive_terminal_assignment(running.assignment_id)

    assert recovery.scanned == 1
    assert recovery.cancelled == 1
    assert archive_calls == 2
    assert recovered["phase"] == "cancelled"
    assert recovered["status"] == "cancelled"
    assert recovered["formal_terminal_archive_result_json"] is not None
    assert (
        recovered["formal_terminal_archive_manifest_digest"]
        == first_archive.archive_manifest_digest
    )
    assert recovered["formal_terminal_archive_completed_at"] is not None
    assert replayed_archive == first_archive
    assert (await restarted.recover_pending_assignments()).scanned == 0


@pytest.mark.asyncio
async def test_terminal_archive_retry_preserves_original_daemon_failure(
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
    application_id = await empty_application(workflow, "formal-terminal-retry")
    with _real_health_endpoints(package):
        running = await bridge.start_formal_build(
            application_id,
            formal_request(connection.connection_id),
        )
    collaboration_store = CollaborationStore(storage.db_path)
    await collaboration_store.initialize()
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    source_provenance = _submitted_source_provenance(
        tmp_path,
        daemon,
        running.assignment_id,
        name="terminal-retry",
    )
    coordinator = FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge.store,
        collaboration_store=collaboration_store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=auth,
        connector_service=_ConnectorBudgetAudit(),
        source_provenance=source_provenance,
    )
    archive_calls = 0

    async def archive_after_one_failure(assignment_id: UUID) -> Any:
        nonlocal archive_calls
        archive_calls += 1
        if archive_calls == 1:
            raise RuntimeError("injected archive transport failure")
        return await coordinator.archive_terminal_assignment(assignment_id)

    bridge.formal_terminal_archive_provider = archive_after_one_failure
    daemon.events = [
        {
            "seq": 1,
            "event": "session.status_changed",
            "data": {"from_status": "running", "to_status": "error"},
        }
    ]
    daemon.sessions[str(running.session_id)]["status"] = "error"

    with pytest.raises(LocalLiliesBridgeUnavailable, match="archive is pending"):
        await bridge.relay_events(running.assignment_id)
    failed_before_retry = await bridge.store.get_assignment(running.assignment_id)

    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    restarted.formal_terminal_archive_provider = archive_after_one_failure
    await restarted.initialize()
    recovery = await restarted.recover_pending_assignments()
    failed_after_retry = await restarted.store.get_assignment(running.assignment_id)
    archived = await coordinator.archive_terminal_assignment(running.assignment_id)

    assert recovery.scanned == 1
    assert recovery.failed == 1
    assert archive_calls == 2
    assert failed_before_retry["status"] == failed_after_retry["status"] == "failed"
    assert (
        failed_before_retry["last_error_code"]
        == failed_after_retry["last_error_code"]
        == "daemon_session_error"
    )
    assert failed_after_retry["formal_terminal_archive_result_json"] is not None
    assert failed_after_retry["formal_terminal_archive_completed_at"] is not None
    assert (await restarted.recover_pending_assignments()).scanned == 0
    assert archived is not None
    assert archived.status is ArchiveStatus.failed


@pytest.mark.asyncio
async def test_terminal_archive_failure_does_not_block_later_startup_recovery(
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
    first_application = await empty_application(workflow, "formal-terminal-first")
    second_application = await empty_application(workflow, "formal-terminal-second")
    with _real_health_endpoints(package):
        first = await bridge.start_formal_build(
            first_application,
            formal_request(connection.connection_id),
        )
        second = await bridge.start_formal_build(
            second_application,
            formal_request(connection.connection_id).model_copy(
                update={"idempotency_key": "formal-build-request-000002"}
            ),
        )
    source_provenance = _submitted_source_provenance(
        tmp_path,
        daemon,
        first.assignment_id,
        name="terminal-recovery-multi",
    )
    second_assignment = next(
        item
        for item in daemon.submitted_assignments
        if item.assignment_id == second.assignment_id
    )
    second_task = second_assignment.task_package
    second_access = second_assignment.collaboration
    assert second_task is not None
    assert second_access is not None
    source_provenance.freeze_baseline(
        task_id=second_task.task_id,
        task_revision=second_task.revision,
        run_id=second_task.run_id,
        assignment_id=second_assignment.assignment_id,
        channel_id=second_access.channel_id,
        captured_at=second_assignment.created_at,
    )
    collaboration_store = CollaborationStore(storage.db_path)
    await collaboration_store.initialize()
    artifacts = PlatformBlackboxArtifactStore(storage.db_path)
    await artifacts.initialize()
    coordinator = FormalRunArchiveCoordinator(
        task_state_root=tmp_path / "sealed-task-state",
        public_workspace_root=tmp_path / "formal-public-workspaces",
        bridge_store=bridge.store,
        collaboration_store=collaboration_store,
        workflow_storage=workflow,
        artifact_store=artifacts,
        auth_store=auth,
        connector_service=_ConnectorBudgetAudit(),
        source_provenance=source_provenance,
    )
    daemon.events = [
        {
            "seq": 1,
            "event": "session.status_changed",
            "data": {"from_status": "running", "to_status": "error"},
        }
    ]
    daemon.sessions[str(first.session_id)]["status"] = "error"
    daemon.sessions[str(second.session_id)]["status"] = "error"
    await bridge.relay_events(first.assignment_id)
    await bridge.relay_events(second.assignment_id)

    async def archive_with_one_unavailable(assignment_id: UUID) -> Any:
        if assignment_id == first.assignment_id:
            raise RuntimeError("first archive remains unavailable")
        return await coordinator.archive_terminal_assignment(assignment_id)

    restarted = make_bridge(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        broker=broker,
        providers=providers,
    )
    restarted.formal_terminal_archive_provider = archive_with_one_unavailable
    await restarted.initialize()

    recovery = await restarted.recover_pending_assignments()
    first_row = await restarted.store.get_assignment(first.assignment_id)
    second_row = await restarted.store.get_assignment(second.assignment_id)

    assert recovery.scanned == 2
    assert recovery.unavailable == 1
    assert recovery.failed == 1
    assert first_row["formal_terminal_archive_completed_at"] is None
    assert second_row["formal_terminal_archive_completed_at"] is not None
    assert second_row["last_error_code"] == "daemon_session_error"
