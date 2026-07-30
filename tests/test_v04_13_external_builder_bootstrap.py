from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from pydantic import SecretStr

from agent_platform import external_builder_bootstrap as bootstrap_module
from agent_platform.external_builder_bootstrap import (
    ExternalBuilderBootstrapError,
    ExternalBuilderBootstrapRequest,
    bootstrap_external_builder_async,
)
from agent_platform.formal_assignment_broker import (
    FormalAssignmentPublicDigests,
    FormalPublicWorkspace,
    PreparedFormalAssignment,
)
from agent_platform.lilies_models import BuildAssignment
from agent_platform.local_lilies_bridge import LocalLiliesBridgeStore
from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxAuthStore,
    PlatformBlackboxOperation,
    PlatformBlackboxScope,
    TaskCredentialGrant,
)


DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
DIGEST_C = f"sha256:{'c' * 64}"
DIGEST_D = f"sha256:{'d' * 64}"
DIGEST_E = f"sha256:{'e' * 64}"
DIGEST_F = f"sha256:{'f' * 64}"


class FakeFormalBroker:
    def __init__(self, prepared: PreparedFormalAssignment) -> None:
        self.prepared = prepared
        self.prepare_requests: list[Any] = []
        self.collaboration_requests: list[tuple[BuildAssignment, UUID]] = []
        self.closed_collaboration_requests: list[
            tuple[BuildAssignment, UUID]
        ] = []

    async def prepare_async(self, request: Any) -> PreparedFormalAssignment:
        self.prepare_requests.append(request)
        return self.prepared

    async def collaboration_credential_secret(
        self,
        assignment: BuildAssignment,
        session_id: UUID,
    ) -> SecretStr:
        self.collaboration_requests.append((assignment, session_id))
        return SecretStr(f"collaboration_{'z' * 64}")

    async def close_collaboration_authority(
        self,
        assignment: BuildAssignment,
        session_id: UUID,
    ) -> None:
        self.closed_collaboration_requests.append((assignment, session_id))


class RecordingAuthStore:
    def __init__(self, delegate: PlatformBlackboxAuthStore) -> None:
        self.delegate = delegate
        self.grants: list[TaskCredentialGrant] = []
        self.revocations: list[tuple[str, str]] = []

    async def issue_credential(
        self,
        grant: TaskCredentialGrant,
        **kwargs: Any,
    ) -> Any:
        self.grants.append(grant)
        return await self.delegate.issue_credential(grant, **kwargs)

    async def revoke_credential(self, credential_ref: str, *, reason: str) -> Any:
        self.revocations.append((credential_ref, reason))
        return await self.delegate.revoke_credential(
            credential_ref,
            reason=reason,
        )


class RecordingExternalBuilderStore:
    def __init__(self) -> None:
        self.registrations: list[dict[str, Any]] = []

    async def reserve_external_builder_assignment(self, **kwargs: Any) -> tuple[dict, bool]:
        replayed = bool(self.registrations)
        self.registrations.append(kwargs)
        return {}, replayed


class FailingExternalBuilderStore:
    async def reserve_external_builder_assignment(self, **kwargs: Any) -> None:
        del kwargs
        raise RuntimeError("external Builder reservation failed")


class RecordingWorkflowStore:
    def __init__(self) -> None:
        self.baselines: list[dict[str, str]] = []

    async def begin_formal_draft_provenance(self, **kwargs: str) -> dict[str, str]:
        self.baselines.append(kwargs)
        return kwargs


def _services(broker: FakeFormalBroker, auth_store: Any) -> SimpleNamespace:
    return SimpleNamespace(
        local_lilies_bridge=SimpleNamespace(
            formal_assignment_broker=broker,
            store=RecordingExternalBuilderStore(),
        ),
        platform_blackbox_auth=auth_store,
        workflow_store=RecordingWorkflowStore(),
    )


def _request(tmp_path: Path) -> ExternalBuilderBootstrapRequest:
    return ExternalBuilderBootstrapRequest(
        task_id="EXP-LILIES-001",
        revision=20,
        assignment_id=uuid4(),
        application_id=uuid4(),
        build_id=uuid4(),
        session_id=uuid4(),
        connection_id=uuid4(),
        environment_instance_id="environment:exp-lilies-001-r20",
        idempotency_key=f"external-builder:{uuid4().hex}",
        builder_actor="codex",
        handoff_path=tmp_path / "private" / "codex-builder-handoff.json",
    )


def _prepared(
    request: ExternalBuilderBootstrapRequest,
    tmp_path: Path,
) -> PreparedFormalAssignment:
    run_id = f"formal-run:{request.build_id}"
    credential_id = uuid5(
        NAMESPACE_URL,
        f"lilies:platform-task-credential:{request.assignment_id}",
    )
    channel_id = uuid5(
        NAMESPACE_URL,
        f"lilies:formal-channel:{request.assignment_id}:{request.session_id}",
    )
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=2)
    assignment = BuildAssignment.model_validate(
        {
            "schema_version": "1.0",
            "assignment_id": str(request.assignment_id),
            "idempotency_key": request.idempotency_key,
            "mode": "formal_experiment",
            "requirement": "Reconcile supplier documents through public platform APIs.",
            "business_context": {
                "customer_roles": ["procurement"],
                "business_goal": "Produce a receipt-backed reconciliation workbook.",
                "inputs": ["supplier documents"],
                "outputs": ["reconciliation workbook"],
                "constraints": ["exactly-once host writes"],
            },
            "task_package": {
                "task_id": request.task_id,
                "revision": request.revision,
                "public_summary_digest": DIGEST_A,
                "run_id": run_id,
                "environment_ready_digest": DIGEST_B,
                "environment_lock_digest": DIGEST_C,
                "allowed_actions_digest": DIGEST_D,
                "budget_digest": DIGEST_E,
                "environment_instance_id": request.environment_instance_id,
                "workspace_mount_digest": DIGEST_A,
                "workspace_policy_digest": DIGEST_B,
            },
            "target": {
                "mode": "existing",
                "application_id": str(request.application_id),
            },
            "platform": {
                "base_url": "http://127.0.0.1:8001",
                "contract_url": "/api/v1/lilies/platform-contract",
                "contract_digest": DIGEST_F,
                "credential_ref": f"platform-task-credential:{credential_id}",
                "scopes": [
                    "workflow.catalog:read",
                    "workflow.application:write",
                    "workflow.run:execute",
                ],
                "application_ids": [str(request.application_id)],
            },
            "constraints": {
                "deadline_at": deadline.isoformat(),
                "max_turns": 120,
                "max_budget_usd": 20,
                "max_tool_calls": 800,
                "network_policy": "allowlist",
                "allowed_hosts": ["paperless.internal", "inventree.internal"],
                "allowed_actions": [
                    "platform_contract_get",
                    "platform_application_get",
                    "platform_run_start",
                ],
                "prohibited_actions": [
                    "read_platform_source",
                    "read_hidden_oracle",
                    "write_task_package",
                ],
                "no_substitute_validation": True,
                "readable_host_objects": ["paperless.documents"],
                "writable_host_operations": ["inventree.purchase_order.update"],
                "model_access": True,
                "file_access": True,
                "connector_access": True,
                "permission_required_actions": [
                    "inventree.purchase_order.update",
                ],
                "max_write_count": 6,
                "max_payload_bytes": 1_048_576,
                "compensation_actions": ["inventree.purchase_order.restore"],
                "max_report_evidence_rounds": 3,
                "stable_hidden_runs": 3,
            },
            "fixture_refs": [
                {
                    "artifact_id": "fixture:supplier-documents",
                    "digest": DIGEST_C,
                    "media_type": "application/json",
                    "display_name": "Supplier documents",
                }
            ],
            "deliverables": [
                {
                    "name": "reconciliation-workbook",
                    "description": "Receipt-backed reconciliation workbook.",
                    "media_type": (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                }
            ],
            "collaboration": {
                "channel_id": str(channel_id),
                "credential_ref": f"collaboration:{channel_id}",
                "scopes": [
                    "collaboration.report:write",
                    "collaboration.response:read",
                ],
                "expires_at": deadline.isoformat(),
            },
            "created_at": now.isoformat(),
        }
    )
    workspace = FormalPublicWorkspace(
        path=str(tmp_path / "formal-public-workspaces" / request.assignment_id.hex),
        manifest_digest=DIGEST_A,
        policy_digest=DIGEST_B,
    )
    digests = FormalAssignmentPublicDigests(
        public_summary_digest=DIGEST_A,
        environment_ready_digest=DIGEST_B,
        environment_lock_digest=DIGEST_C,
        allowed_actions_digest=DIGEST_D,
        budget_digest=DIGEST_E,
    )
    unvalidated = PreparedFormalAssignment.model_construct(
        schema_version="1.0",
        run_id=run_id,
        assignment=assignment,
        workspace=workspace,
        digests=digests,
        bundle_digest=DIGEST_A,
    )
    content = unvalidated.model_dump(mode="json", exclude={"bundle_digest"})
    bundle_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                content,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    return PreparedFormalAssignment(
        run_id=run_id,
        assignment=assignment,
        workspace=workspace,
        digests=digests,
        bundle_digest=bundle_digest,
    )


@pytest.mark.asyncio
async def test_bootstrap_issues_exact_authority_and_private_actor_stamped_handoff(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    prepared = _prepared(request, tmp_path)
    broker = FakeFormalBroker(prepared)
    auth_delegate = PlatformBlackboxAuthStore(tmp_path / "platform.db")
    await auth_delegate.initialize()
    auth_store = RecordingAuthStore(auth_delegate)
    services = _services(broker, auth_store)
    task_token = (
        f"lpt_{_credential_id(request.assignment_id).hex}_{'A' * 43}"
    )

    receipt = await bootstrap_external_builder_async(
        services=services,
        request=request,
        task_token_factory=lambda _credential_id: task_token,
    )

    assert len(broker.prepare_requests) == 1
    assert broker.prepare_requests[0] == request.formal_request()
    assert broker.collaboration_requests == [
        (prepared.assignment, request.session_id)
    ]
    assert len(auth_store.grants) == 1
    grant = auth_store.grants[0]
    assert grant.assignment_id == request.assignment_id
    assert grant.session_id == request.session_id
    assert grant.scopes == [
        PlatformBlackboxScope.catalog_read,
        PlatformBlackboxScope.application_write,
        PlatformBlackboxScope.run_execute,
    ]
    assert grant.application_ids == [request.application_id]
    assert grant.allowed_operations == [
        PlatformBlackboxOperation.contract_get,
        PlatformBlackboxOperation.application_get,
        PlatformBlackboxOperation.run_start,
    ]
    assert grant.allowed_actions_digest == DIGEST_D
    assert grant.budget_digest == DIGEST_E
    assert grant.allowed_network_hosts == [
        "paperless.internal",
        "inventree.internal",
    ]
    assert grant.model_access is True
    assert grant.file_access is True
    assert grant.connector_access is True
    assert grant.readable_host_objects == ["paperless.documents"]
    assert grant.writable_host_operations == [
        "inventree.purchase_order.update"
    ]
    assert grant.permission_required_actions == [
        "inventree.purchase_order.update"
    ]
    assert grant.max_write_count == 6
    assert grant.max_payload_bytes == 1_048_576
    assert grant.compensation_actions == ["inventree.purchase_order.restore"]
    assert grant.max_report_evidence_rounds == 3
    assert grant.stable_hidden_runs == 3

    handoff = json.loads(request.handoff_path.read_text(encoding="utf-8"))
    assert stat.S_IMODE(request.handoff_path.stat().st_mode) == 0o600
    assert handoff["builder_actor"] == "codex"
    assert handoff["formal_archive_supported"] is True
    assert handoff["assignment"]["assignment_id"] == str(request.assignment_id)
    assert handoff["workspace"] == {
        "path": prepared.workspace.path,
        "manifest_digest": prepared.workspace.manifest_digest,
        "policy_digest": prepared.workspace.policy_digest,
    }
    assert handoff["platform"]["access_token"] == task_token
    assert handoff["collaboration"]["access_token"].startswith("collaboration_")
    handoff_text = request.handoff_path.read_text(encoding="utf-8")
    assert "/protected/" not in handoff_text
    assert "/oracle/" not in handoff_text
    assert "developer-workspace" not in handoff_text
    assert "owner_token" not in handoff_text
    assert "host_secret" not in handoff_text

    safe_receipt = receipt.model_dump_json()
    assert task_token not in safe_receipt
    assert f"collaboration_{'z' * 64}" not in safe_receipt
    assert receipt.builder_actor == "codex"
    assert receipt.formal_archive_supported is True
    assert receipt.handoff_digest.startswith("sha256:")
    assert services.workflow_store.baselines == [
        {
            "assignment_id": str(request.assignment_id),
            "session_id": str(request.session_id),
            "application_id": str(request.application_id),
        }
    ]
    assert len(services.local_lilies_bridge.store.registrations) == 1


@pytest.mark.asyncio
async def test_bootstrap_exactly_replays_completed_private_handoff(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    prepared = _prepared(request, tmp_path)
    broker = FakeFormalBroker(prepared)
    auth_delegate = PlatformBlackboxAuthStore(tmp_path / "platform.db")
    await auth_delegate.initialize()
    auth_store = RecordingAuthStore(auth_delegate)
    services = _services(broker, auth_store)
    task_token = (
        f"lpt_{_credential_id(request.assignment_id).hex}_{'A' * 43}"
    )

    first = await bootstrap_external_builder_async(
        services=services,
        request=request,
        task_token_factory=lambda _credential_id: task_token,
    )
    handoff = request.handoff_path.read_bytes()
    replay = await bootstrap_external_builder_async(
        services=services,
        request=request,
        task_token_factory=lambda _credential_id: task_token,
    )

    assert replay == first
    assert request.handoff_path.read_bytes() == handoff
    assert stat.S_IMODE(request.handoff_path.stat().st_mode) == 0o600
    assert len(broker.prepare_requests) == 2
    assert len(auth_store.grants) == 2


@pytest.mark.asyncio
async def test_bootstrap_recovers_after_credential_issue_before_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    prepared = _prepared(request, tmp_path)
    broker = FakeFormalBroker(prepared)
    auth_delegate = PlatformBlackboxAuthStore(tmp_path / "platform.db")
    await auth_delegate.initialize()
    auth_store = RecordingAuthStore(auth_delegate)
    services = _services(broker, auth_store)
    task_token = (
        f"lpt_{_credential_id(request.assignment_id).hex}_{'A' * 43}"
    )
    writer = bootstrap_module._write_private_json_once

    def fail_after_credential(_path: Path, _value: Any) -> str:
        raise ExternalBuilderBootstrapError("injected handoff write failure")

    monkeypatch.setattr(
        bootstrap_module,
        "_write_private_json_once",
        fail_after_credential,
    )
    with pytest.raises(
        ExternalBuilderBootstrapError,
        match="injected handoff write failure",
    ):
        await bootstrap_external_builder_async(
            services=services,
            request=request,
            task_token_factory=lambda _credential_id: task_token,
        )
    assert not request.handoff_path.exists()

    monkeypatch.setattr(
        bootstrap_module,
        "_write_private_json_once",
        writer,
    )
    receipt = await bootstrap_external_builder_async(
        services=services,
        request=request,
        task_token_factory=lambda _credential_id: task_token,
    )

    assert receipt.handoff_path == request.handoff_path
    assert request.handoff_path.is_file()
    assert len(auth_store.grants) == 2


@pytest.mark.asyncio
async def test_bootstrap_rejects_existing_handoff_before_broker_side_effects(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request.handoff_path.parent.mkdir(mode=0o700)
    request.handoff_path.write_text("do-not-overwrite", encoding="utf-8")
    broker = FakeFormalBroker(_prepared(request, tmp_path))
    services = SimpleNamespace(
        local_lilies_bridge=SimpleNamespace(formal_assignment_broker=broker),
        platform_blackbox_auth=object(),
    )

    with pytest.raises(
        ExternalBuilderBootstrapError,
        match="already exists",
    ):
        await bootstrap_external_builder_async(
            services=services,
            request=request,
        )

    assert broker.prepare_requests == []
    assert request.handoff_path.read_text(encoding="utf-8") == "do-not-overwrite"


@pytest.mark.asyncio
async def test_bootstrap_rejects_prepared_identity_drift_without_handoff(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    other_request = request.model_copy(update={"assignment_id": uuid4()})
    broker = FakeFormalBroker(_prepared(other_request, tmp_path))
    auth_delegate = PlatformBlackboxAuthStore(tmp_path / "platform.db")
    await auth_delegate.initialize()
    auth_store = RecordingAuthStore(auth_delegate)
    services = _services(broker, auth_store)

    with pytest.raises(
        ExternalBuilderBootstrapError,
        match="identity binding",
    ):
        await bootstrap_external_builder_async(
            services=services,
            request=request,
        )

    assert auth_store.grants == []
    assert not request.handoff_path.exists()


@pytest.mark.asyncio
async def test_bootstrap_reservation_failure_retires_issued_authority(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    broker = FakeFormalBroker(_prepared(request, tmp_path))
    auth_delegate = PlatformBlackboxAuthStore(tmp_path / "platform.db")
    await auth_delegate.initialize()
    auth_store = RecordingAuthStore(auth_delegate)
    services = SimpleNamespace(
        local_lilies_bridge=SimpleNamespace(
            formal_assignment_broker=broker,
            store=FailingExternalBuilderStore(),
        ),
        platform_blackbox_auth=auth_store,
        workflow_store=RecordingWorkflowStore(),
    )

    with pytest.raises(
        RuntimeError,
        match="external Builder reservation failed",
    ):
        await bootstrap_external_builder_async(
            services=services,
            request=request,
        )

    assert len(broker.closed_collaboration_requests) == 1
    assert broker.closed_collaboration_requests[0][1] == request.session_id
    assert len(auth_store.revocations) == 1
    credential = await auth_delegate.get_credential(
        auth_store.revocations[0][0]
    )
    assert credential.revoked_at is not None
    assert not request.handoff_path.exists()


@pytest.mark.asyncio
async def test_external_builder_lifecycle_is_separate_durable_and_sealable(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    assignment = _prepared(request, tmp_path).assignment
    store = LocalLiliesBridgeStore(tmp_path / "external-builder-lifecycle.db")
    await store.initialize()

    registered, replayed = await store.reserve_external_builder_assignment(
        assignment=assignment,
        session_id=request.session_id,
        connection_id=request.connection_id,
        request_json='{"builder_actor":"codex"}',
        request_digest=DIGEST_A,
        credential_ref=assignment.platform.credential_ref,
        collaboration_credential_ref=(
            assignment.collaboration.credential_ref  # type: ignore[union-attr]
        ),
        task_token_secret_ref=assignment.platform.credential_ref,
        builder_actor="codex",
    )

    assert replayed is False
    assert registered["execution_mode"] == "external_builder"
    assert registered["phase"] == "running"
    exported = await store.export_assignment(request.assignment_id)
    assert [event["event_type"] for event in exported["events"]] == [
        "assignment.accepted"
    ]

    sealed = await store.seal_external_builder_completion(request.assignment_id)
    assert sealed is not None
    assert sealed["phase"] == "completed"
    assert sealed["terminal_events_drained_at"] is not None
    completed = await store.export_assignment(request.assignment_id)
    assert [event["event_type"] for event in completed["events"]] == [
        "assignment.accepted",
        "external_builder.completed",
    ]


@pytest.mark.asyncio
async def test_external_builder_retirement_releases_application_for_successor(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    assignment = _prepared(request, tmp_path).assignment
    store = LocalLiliesBridgeStore(tmp_path / "external-builder-retirement.db")
    await store.initialize()
    await store.reserve_external_builder_assignment(
        assignment=assignment,
        session_id=request.session_id,
        connection_id=request.connection_id,
        request_json='{"builder_actor":"codex"}',
        request_digest=DIGEST_A,
        credential_ref=assignment.platform.credential_ref,
        collaboration_credential_ref=(
            assignment.collaboration.credential_ref  # type: ignore[union-attr]
        ),
        task_token_secret_ref=assignment.platform.credential_ref,
        builder_actor="codex",
    )

    retired = await store.retire_external_builder_assignment(
        request.assignment_id,
        session_id=request.session_id,
        application_id=request.application_id,
        credential_ref=assignment.platform.credential_ref,
        collaboration_credential_ref=(
            assignment.collaboration.credential_ref  # type: ignore[union-attr]
        ),
        reason="rotate to a clean successor attempt",
    )

    assert retired is not None
    assert retired["phase"] == "cancelled"
    assert retired["desired_state"] == "cancelled"
    exported = await store.export_assignment(request.assignment_id)
    assert exported["complete"] is True
    assert [event["event_type"] for event in exported["events"]] == [
        "assignment.accepted",
        "external_builder.cancelled",
    ]

    successor_request = _request(tmp_path).model_copy(
        update={"application_id": request.application_id}
    )
    successor = _prepared(successor_request, tmp_path).assignment
    registered, replayed = await store.reserve_external_builder_assignment(
        assignment=successor,
        session_id=successor_request.session_id,
        connection_id=successor_request.connection_id,
        request_json='{"builder_actor":"codex"}',
        request_digest=DIGEST_B,
        credential_ref=successor.platform.credential_ref,
        collaboration_credential_ref=(
            successor.collaboration.credential_ref  # type: ignore[union-attr]
        ),
        task_token_secret_ref=successor.platform.credential_ref,
        builder_actor="codex",
    )
    assert replayed is False
    assert registered["phase"] == "running"


def _credential_id(assignment_id: UUID) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"lilies:platform-task-credential:{assignment_id}",
    )
