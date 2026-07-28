from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from agent_platform.collaboration_api import install_collaboration_api
from agent_platform.collaboration_models import (
    ApprovalDecisionRequest,
    CollaborationChannel,
    CollaborationReport,
    CollaborationReportPayload,
    DeveloperLease,
    DeveloperResponseRequest,
    DeveloperSourcePromotionRequest,
    DeveloperWorkerReceiptReference,
    DeveloperWorkspaceBinding,
    LeaseAcquireRequest,
    ReportSubmitRequest,
    SenderRole,
)
from agent_platform.collaboration_service import (
    CollaborationConflict,
    CollaborationPrincipal,
    CollaborationService,
)
from agent_platform.developer_collaboration_cli import _dispatch, build_parser
from agent_platform.developer_collaboration_client import (
    DeveloperCollaborationClient,
)
from agent_platform.formal_developer_worker_broker import (
    DeveloperWorkerReceipt,
    DeveloperWorkerRunRequest,
    FormalDeveloperWorkerBroker,
    FormalDeveloperWorkerConflict,
    FormalDeveloperWorkerError,
    _WORKER_RUNTIME_RELATIVE_ROOTS,
    _canonical_json,
    _workspace_tree_digest,
)
from agent_platform.lilies_models import CollaborationScope
from agent_platform.task_packages import (
    WORKSPACE_MANIFEST_FILE,
    WORKSPACE_POLICY_FILE,
    WorkspaceMountEntry,
    WorkspaceMountManifest,
)
from tests.test_v04_13_collaboration_sqlite_integration import (
    _developer_response_payload,
    _report_payload,
    _store_with_channel,
)


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
ASSIGNMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
CHANNEL_ID = UUID("20000000-0000-4000-8000-000000000002")
REPORT_ID = UUID("30000000-0000-4000-8000-000000000003")
LEASE_ID = UUID("40000000-0000-4000-8000-000000000004")
RESPONSE_ID = UUID("50000000-0000-4000-8000-000000000005")
DIGEST_A = "sha256:" + "a" * 64


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def test_workspace_digest_excludes_only_broker_owned_runtime_directories(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "source"
    runtime = (
        workspace
        / "work"
        / ".developer-worker-home"
        / ".codex"
        / "tmp"
    )
    source.mkdir(parents=True)
    runtime.mkdir(parents=True)
    (source / "implementation.py").write_text("VALUE = 1\n", encoding="utf-8")
    (runtime / "codex-wrapper").symlink_to("/usr/bin/true")

    digest = _workspace_tree_digest(
        workspace,
        excluded_relative_roots=_WORKER_RUNTIME_RELATIVE_ROOTS,
    )

    assert digest.startswith("sha256:")
    (source / "forbidden-link").symlink_to("/usr/bin/true")
    with pytest.raises(
        FormalDeveloperWorkerError,
        match="cannot contain symbolic links",
    ):
        _workspace_tree_digest(
            workspace,
            excluded_relative_roots=_WORKER_RUNTIME_RELATIVE_ROOTS,
        )


def _write(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(payload)
    os.chmod(path, mode)


def _boundary(
    tmp_path: Path,
) -> tuple[
    CollaborationChannel,
    CollaborationReport,
    DeveloperLease,
    DeveloperWorkspaceBinding,
    FormalDeveloperWorkerBroker,
]:
    workspace = tmp_path / "workspace"
    for child in ("task", "source", "work"):
        (workspace / child).mkdir(parents=True, mode=0o700)
    task_payload = b"Implement the approved generic capability.\n"
    _write(workspace / "task/requirement.md", task_payload, mode=0o400)
    manifest = WorkspaceMountManifest(
        schema_version="1.0",
        task_id="T01F-WORKER",
        revision=1,
        role="developer",
        run_id="run:developer-worker-0001",
        assignment_id=ASSIGNMENT_ID,
        public_summary_digest=DIGEST_A,
        entries=[
            WorkspaceMountEntry(
                logical_source="task-package:requirement.md",
                target_path="task/requirement.md",
                digest=_digest(task_payload),
                size_bytes=len(task_payload),
                read_only=True,
            )
        ],
        denied_segments=[
            ".git",
            ".hg",
            ".svn",
            "__pycache__",
            "expected-state",
            "oracle",
            "platform-data",
            "platform_data",
            "protected",
        ],
        writable_prefixes=["source", "work"],
        created_at=NOW,
    )
    manifest_payload = _json_bytes(manifest.model_dump(mode="json"))
    policy_payload = _json_bytes(
        {
            "schema_version": "1.0",
            "denied_segments": [
                ".git",
                ".hg",
                ".lilies-mount-manifest.json",
                ".lilies-workspace-policy.json",
                ".svn",
                "__pycache__",
                "expected-state",
                "oracle",
                "platform-data",
                "platform_data",
                "protected",
            ],
            "writable_prefixes": ["source", "work"],
        }
    )
    _write(
        workspace / WORKSPACE_MANIFEST_FILE,
        manifest_payload,
        mode=0o400,
    )
    _write(
        workspace / WORKSPACE_POLICY_FILE,
        policy_payload,
        mode=0o400,
    )
    os.chmod(workspace / "task", 0o500)
    os.chmod(workspace, 0o500)
    binding = DeveloperWorkspaceBinding(
        task_id="T01F-WORKER",
        task_revision=1,
        run_id="run:developer-worker-0001",
        assignment_id=ASSIGNMENT_ID,
        path=str(workspace),
        manifest_digest=_digest(manifest_payload),
        policy_digest=_digest(policy_payload),
    )
    channel = CollaborationChannel(
        channel_id=CHANNEL_ID,
        task_id=binding.task_id,
        task_revision=binding.task_revision,
        assignment_id=ASSIGNMENT_ID,
        lilies_session_id=uuid4(),
        application_ids=[],
        status="active",
        created_at=NOW,
    )
    report = CollaborationReport.model_validate(
        {
            **_report_payload(REPORT_ID),
            "channel_id": str(CHANNEL_ID),
            "source_message_id": str(uuid4()),
            "route": "developer",
            "status": "implementing",
            "revision": 5,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    )
    lease = DeveloperLease(
        lease_id=LEASE_ID,
        report_id=REPORT_ID,
        report_revision=5,
        owner_id="codex-developer",
        status="active",
        acquired_at=NOW,
        heartbeat_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        developer_workspace=binding,
    )
    broker = FormalDeveloperWorkerBroker(
        state_root=tmp_path / "broker-state",
        runtime_executable=Path("/bin/bash"),
    )
    return channel, report, lease, binding, broker


def _request(*, idempotency_key: str, arguments: list[str]) -> DeveloperWorkerRunRequest:
    return DeveloperWorkerRunRequest(
        idempotency_key=idempotency_key,
        lease_id=LEASE_ID,
        lease_owner_id="codex-developer",
        expected_report_revision=5,
        response_id=RESPONSE_ID,
        arguments=arguments,
        timeout_seconds=30,
    )


def _require_macos_sandbox() -> None:
    if shutil.which("sandbox-exec") is None:
        pytest.skip("macOS sandbox-exec is unavailable on this host")


def test_real_worker_can_write_only_allowed_prefixes_and_receipt_replays(
    tmp_path: Path,
) -> None:
    _require_macos_sandbox()
    channel, report, lease, binding, broker = _boundary(tmp_path)
    external_secret = tmp_path / "platform-data" / "approval.sqlite"
    _write(external_secret, b"must-not-be-readable\n", mode=0o600)
    command = "\n".join(
        (
            f"if IFS= read -r hidden < {str(external_secret)!r}; then exit 40; fi",
            "if printf 'tampered' > task/requirement.md; then exit 41; fi",
            ('if [ -n "${LILIES_COLLABORATION_DEVELOPER_TOKEN+x}" ]; then exit 42; fi'),
            "printf 'sandboxed\\n' > source/worker-result.txt",
        )
    )
    request = _request(
        idempotency_key="developer-worker-real-sandbox-0001",
        arguments=["-c", command],
    )

    receipt = broker.run(
        channel=channel,
        report=report,
        lease=lease,
        workspace=binding,
        request=request,
    )

    assert receipt.successful
    assert receipt.sandboxed is True
    assert receipt.network_access == "denied"
    assert receipt.inherited_environment == "none"
    assert receipt.writable_prefixes == ["source", "work"]
    assert (Path(binding.path) / "source/worker-result.txt").read_text() == "sandboxed\n"
    assert (Path(binding.path) / "task/requirement.md").read_text().startswith("Implement")
    receipt_path = tmp_path / "broker-state" / "receipts" / f"{receipt.receipt_id}.json"
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400
    assert (
        broker.run(
            channel=channel,
            report=report,
            lease=lease,
            workspace=binding,
            request=request,
        )
        == receipt
    )
    restarted = FormalDeveloperWorkerBroker(
        state_root=tmp_path / "broker-state",
        runtime_executable=Path("/bin/bash"),
    )
    assert (
        restarted.run(
            channel=channel,
            report=report,
            lease=lease,
            workspace=binding,
            request=request,
        )
        == receipt
    )
    assert broker.validate_receipt(
        channel=channel,
        report=report,
        lease=lease,
        workspace=binding,
        response_id=RESPONSE_ID,
        receipt_id=receipt.receipt_id,
        receipt_digest=receipt.receipt_digest,
        require_success=True,
    )
    assert not broker.validate_receipt(
        channel=channel,
        report=report,
        lease=lease,
        workspace=binding,
        response_id=RESPONSE_ID,
        receipt_id=receipt.receipt_id,
        receipt_digest="sha256:" + "f" * 64,
        require_success=True,
    )


def test_worker_idempotency_conflict_and_post_receipt_workspace_change_are_rejected(
    tmp_path: Path,
) -> None:
    _require_macos_sandbox()
    channel, report, lease, binding, broker = _boundary(tmp_path)
    request = _request(
        idempotency_key="developer-worker-conflict-0001",
        arguments=["-c", "printf 'first\\n' > work/outcome.txt"],
    )
    receipt = broker.run(
        channel=channel,
        report=report,
        lease=lease,
        workspace=binding,
        request=request,
    )

    with pytest.raises(FormalDeveloperWorkerConflict, match="another request"):
        broker.run(
            channel=channel,
            report=report,
            lease=lease,
            workspace=binding,
            request=_request(
                idempotency_key=request.idempotency_key,
                arguments=["-c", "printf 'other\\n' > work/outcome.txt"],
            ),
        )

    _write(
        Path(binding.path) / "source/post-receipt.py",
        b"VALUE = 'outside broker'\n",
        mode=0o600,
    )
    assert not broker.validate_receipt(
        channel=channel,
        report=report,
        lease=lease,
        workspace=binding,
        response_id=RESPONSE_ID,
        receipt_id=receipt.receipt_id,
        receipt_digest=receipt.receipt_digest,
        require_success=True,
    )


def test_worker_fails_closed_without_macos_sandbox_before_recording_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, report, lease, binding, broker = _boundary(tmp_path)
    monkeypatch.setattr(
        "agent_platform.formal_developer_worker_broker.shutil.which",
        lambda _name: None,
    )

    with pytest.raises(FormalDeveloperWorkerError, match="sandbox-exec"):
        broker.run(
            channel=channel,
            report=report,
            lease=lease,
            workspace=binding,
            request=_request(
                idempotency_key="developer-worker-no-sandbox-0001",
                arguments=["-c", "exit 0"],
            ),
        )

    assert list((tmp_path / "broker-state" / "runs").iterdir()) == []
    assert list((tmp_path / "broker-state" / "receipts").iterdir()) == []


def test_receipt_digest_rejects_a_tampered_archive_payload(tmp_path: Path) -> None:
    _require_macos_sandbox()
    channel, report, lease, binding, broker = _boundary(tmp_path)
    receipt = broker.run(
        channel=channel,
        report=report,
        lease=lease,
        workspace=binding,
        request=_request(
            idempotency_key="developer-worker-receipt-digest-0001",
            arguments=["-c", "exit 0"],
        ),
    )
    payload = receipt.model_dump(mode="json")
    payload["workspace_after_digest"] = "sha256:" + "0" * 64

    with pytest.raises(ValidationError, match="receipt digest"):
        DeveloperWorkerReceipt.model_validate(payload)


@pytest.mark.asyncio
async def test_formal_receipt_gate_rejects_missing_and_forged_references(
    tmp_path: Path,
) -> None:
    channel, report, lease, _binding, _broker = _boundary(tmp_path)
    service = CollaborationService(
        store=object(),
        enabled=True,
        require_frozen_verification_evidence=True,
        developer_worker_receipt_resolver=lambda *_args: False,
    )

    with pytest.raises(CollaborationConflict) as missing:
        await service._require_developer_worker_receipt(
            channel=channel,
            report=report,
            lease=lease,
            response_id=RESPONSE_ID,
            reference=None,
            require_success=True,
        )
    assert missing.value.code == "developer_worker_receipt_required"

    with pytest.raises(CollaborationConflict) as forged:
        await service._require_developer_worker_receipt(
            channel=channel,
            report=report,
            lease=lease,
            response_id=RESPONSE_ID,
            reference=DeveloperWorkerReceiptReference(
                receipt_id=uuid4(),
                receipt_digest="sha256:" + "f" * 64,
            ),
            require_success=True,
        )
    assert forged.value.code == "developer_worker_receipt_untrusted"


@pytest.mark.asyncio
async def test_public_formal_worker_receipt_is_required_archived_and_replayed(
    tmp_path: Path,
) -> None:
    store, _database, channel_row = await _store_with_channel(tmp_path / "collaboration")
    channel_id = UUID(channel_row["channel_id"])
    assignment_id = UUID(channel_row["assignment_id"])
    current = datetime.now(timezone.utc)
    trusted_references: set[tuple[UUID, str]] = set()
    worker_calls = 0
    promotion_calls = 0

    def worker_provider(channel, report, lease, request):
        nonlocal worker_calls
        worker_calls += 1
        payload = {
            "schema_version": "1.0",
            "receipt_id": uuid4(),
            "assignment_id": channel.assignment_id,
            "channel_id": channel.channel_id,
            "report_id": report.report_id,
            "report_revision": report.revision,
            "lease_id": lease.lease_id,
            "lease_owner_id": lease.owner_id,
            "response_id": request.response_id,
            "task_id": channel.task_id,
            "task_revision": channel.task_revision,
            "run_id": "run:public-worker-archive-0001",
            "workspace_manifest_digest": DIGEST_A,
            "workspace_policy_digest": "sha256:" + "b" * 64,
            "source_manifest_digest": "sha256:" + "c" * 64,
            "request_digest": "sha256:" + "d" * 64,
            "runtime_executable_digest": "sha256:" + "e" * 64,
            "runtime_boundary_digest": "sha256:" + "1" * 64,
            "arguments_digest": "sha256:" + "2" * 64,
            "environment_digest": "sha256:" + "3" * 64,
            "sandbox_profile_digest": "sha256:" + "4" * 64,
            "workspace_before_digest": "sha256:" + "5" * 64,
            "workspace_after_digest": "sha256:" + "6" * 64,
            "writable_prefixes": ["source", "work"],
            "sandboxed": True,
            "network_access": "denied",
            "inherited_environment": "none",
            "started_at": current,
            "finished_at": current + timedelta(seconds=1),
            "worker_pid": 1234,
            "exit_code": 0,
            "timed_out": False,
            "stdout_digest": "sha256:" + "7" * 64,
            "stdout_bytes": 0,
            "stderr_digest": "sha256:" + "8" * 64,
            "stderr_bytes": 0,
            "boundary_intact": True,
        }
        receipt = DeveloperWorkerReceipt.model_validate(
            {
                **payload,
                "receipt_digest": _digest(_canonical_json(payload)),
            }
        )
        trusted_references.add((receipt.receipt_id, receipt.receipt_digest))
        return receipt

    def receipt_resolver(
        _channel,
        _report,
        _lease,
        _response_id,
        reference,
        require_success,
    ):
        return (
            require_success in {True, False}
            and (reference.receipt_id, reference.receipt_digest) in trusted_references
        )

    def promotion_provider(channel, report, _lease, request):
        nonlocal promotion_calls
        promotion_calls += 1
        return {
            "assignment_id": str(channel.assignment_id),
            "channel_id": str(channel.channel_id),
            "report_id": str(report.report_id),
            "response_id": str(request.response_id),
            "activation_state": "activated",
            "commit_sha": "c" * 40,
        }

    service = CollaborationService(
        store=store,
        enabled=True,
        developer_token="developer-worker-api-token-0001",
        now=lambda: current,
        developer_worker_provider=worker_provider,
        developer_worker_receipt_resolver=receipt_resolver,
        developer_source_promotion_provider=promotion_provider,
        require_frozen_verification_evidence=True,
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
            idempotency_key="worker-public-report-0001",
            expected_channel_revision=1,
            report=CollaborationReportPayload.model_validate(_report_payload(report_id)),
        ),
    )
    await service.decide_report(
        principal=user,
        report_id=report_id,
        request=ApprovalDecisionRequest(
            idempotency_key="worker-public-approval-0001",
            expected_report_revision=3,
            decision="approve",
        ),
    )
    acquired = await service.acquire_developer_lease(
        principal=developer,
        report_id=report_id,
        request=LeaseAcquireRequest(
            idempotency_key="worker-public-lease-0001",
            expected_report_revision=4,
            owner_id=developer.sender_id,
            ttl_seconds=900,
        ),
    )
    lease_id = UUID(acquired["lease_id"])
    current = datetime.fromisoformat(acquired["acquired_at"]) + timedelta(seconds=1)
    response_id = uuid4()
    forged_reference = DeveloperWorkerReceiptReference(
        receipt_id=uuid4(),
        receipt_digest="sha256:" + "f" * 64,
    )
    base_promotion = {
        "lease_id": lease_id,
        "lease_owner_id": developer.sender_id,
        "expected_report_revision": 5,
        "response_id": response_id,
        "workspace_manifest_digest": DIGEST_A,
        "source_manifest_digest": "sha256:" + "c" * 64,
    }
    with pytest.raises(CollaborationConflict) as missing_promotion:
        await service.promote_developer_source(
            principal=developer,
            report_id=report_id,
            request=DeveloperSourcePromotionRequest(
                idempotency_key="worker-public-missing-promotion-0001",
                **base_promotion,
            ),
        )
    assert missing_promotion.value.code == "developer_worker_receipt_required"
    with pytest.raises(CollaborationConflict) as forged_promotion:
        await service.promote_developer_source(
            principal=developer,
            report_id=report_id,
            request=DeveloperSourcePromotionRequest(
                idempotency_key="worker-public-forged-promotion-0001",
                developer_worker_receipt=forged_reference,
                **base_promotion,
            ),
        )
    assert forged_promotion.value.code == "developer_worker_receipt_untrusted"
    assert promotion_calls == 0

    response_payload = _developer_response_payload(
        response_id=response_id,
        channel_id=channel_id,
        report_id=report_id,
        report_revision=5,
        created_at=current,
    )
    response_payload["outcome"] = "not_reproduced"
    response_payload.pop("commit_sha")
    response_payload.pop("new_contract_digest")
    sender_response = {
        key: value
        for key, value in response_payload.items()
        if key not in {"channel_id", "report_id", "report_revision", "created_at"}
    }
    with pytest.raises(CollaborationConflict) as missing_response:
        await service.submit_developer_response(
            principal=developer,
            report_id=report_id,
            request=DeveloperResponseRequest(
                idempotency_key="worker-public-missing-response-0001",
                lease_id=lease_id,
                lease_owner_id=developer.sender_id,
                expected_report_revision=5,
                response=sender_response,
            ),
        )
    assert missing_response.value.code == "developer_worker_receipt_required"
    with pytest.raises(CollaborationConflict) as forged_response:
        await service.submit_developer_response(
            principal=developer,
            report_id=report_id,
            request=DeveloperResponseRequest(
                idempotency_key="worker-public-forged-response-0001",
                lease_id=lease_id,
                lease_owner_id=developer.sender_id,
                expected_report_revision=5,
                developer_worker_receipt=forged_reference,
                response=sender_response,
            ),
        )
    assert forged_response.value.code == "developer_worker_receipt_untrusted"

    worker_request = DeveloperWorkerRunRequest(
        idempotency_key="worker-public-run-0001",
        lease_id=lease_id,
        lease_owner_id=developer.sender_id,
        expected_report_revision=5,
        response_id=response_id,
        arguments=["exec", "Implement the approved report."],
        timeout_seconds=60,
    )
    app = FastAPI()

    async def require_user_token() -> None:
        return None

    install_collaboration_api(
        app,
        service,
        require_user_token=require_user_token,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        worker_url = f"/api/v1/developer/collaboration/reports/{report_id}/worker-runs"
        worker_headers = {"Authorization": "Bearer developer-worker-api-token-0001"}
        first_worker_response = await client.post(
            worker_url,
            headers=worker_headers,
            json=worker_request.model_dump(mode="json"),
        )
        assert first_worker_response.status_code == 201, first_worker_response.text
        worker_receipt = first_worker_response.json()
        replay_worker_response = await client.post(
            worker_url,
            headers=worker_headers,
            json=worker_request.model_dump(mode="json"),
        )
        assert replay_worker_response.status_code == 201
        assert replay_worker_response.json() == worker_receipt
    assert worker_calls == 1
    trusted_reference = DeveloperWorkerReceiptReference(
        receipt_id=UUID(worker_receipt["receipt_id"]),
        receipt_digest=worker_receipt["receipt_digest"],
    )
    promoted = await service.promote_developer_source(
        principal=developer,
        report_id=report_id,
        request=DeveloperSourcePromotionRequest(
            idempotency_key="worker-public-valid-promotion-0001",
            developer_worker_receipt=trusted_reference,
            **base_promotion,
        ),
    )
    assert promoted["commit_sha"] == "c" * 40
    submitted = await service.submit_developer_response(
        principal=developer,
        report_id=report_id,
        request=DeveloperResponseRequest(
            idempotency_key="worker-public-valid-response-0001",
            lease_id=lease_id,
            lease_owner_id=developer.sender_id,
            expected_report_revision=5,
            developer_worker_receipt=trusted_reference,
            response=sender_response,
        ),
    )
    assert submitted["response_id"] == str(response_id)

    export = await store.export_channel(channel_id)
    archived_receipts = [
        item for item in export["operation_receipts"] if item["operation"] == "developer.worker"
    ]
    assert len(archived_receipts) == 1
    assert archived_receipts[0]["response"]["receipt_digest"] == worker_receipt["receipt_digest"]


def test_developer_client_worker_keeps_bearer_out_of_child_request(
    tmp_path: Path,
) -> None:
    _require_macos_sandbox()
    channel, report, lease, binding, broker = _boundary(tmp_path)
    receipt = broker.run(
        channel=channel,
        report=report,
        lease=lease,
        workspace=binding,
        request=_request(
            idempotency_key="developer-worker-client-receipt-0001",
            arguments=["-c", "exit 0"],
        ),
    )
    access_token = "developer-client-private-token-value-0001"
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization")
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json=receipt.model_dump(mode="json", exclude_none=True),
        )

    client = DeveloperCollaborationClient(
        base_url="http://127.0.0.1:8000",
        access_token=access_token,
        transport=httpx.MockTransport(handler),
    )
    returned = client.run_worker(
        REPORT_ID,
        lease_id=LEASE_ID,
        expected_report_revision=5,
        response_id=RESPONSE_ID,
        idempotency_key="developer-worker-client-request-0001",
        arguments=["exec", "Implement only the approved report."],
        timeout_seconds=60,
    )

    assert returned == receipt
    assert observed["authorization"] == f"Bearer {access_token}"
    body = observed["body"]
    assert isinstance(body, dict)
    assert body["lease_owner_id"] == "codex-developer"
    assert access_token not in json.dumps(body)
    assert not any("token" in key.casefold() for key in body)


def test_developer_cli_dispatches_fixed_runtime_arguments_without_a_token() -> None:
    observed: dict[str, object] = {}

    class Client:
        def run_worker(self, report_id: UUID, **parameters: object) -> dict[str, object]:
            observed["report_id"] = report_id
            observed.update(parameters)
            return {"status": "dispatched"}

    arguments = build_parser().parse_args(
        [
            "worker",
            str(REPORT_ID),
            "--lease-id",
            str(LEASE_ID),
            "--expected-report-revision",
            "5",
            "--response-id",
            str(RESPONSE_ID),
            "--idempotency-key",
            "developer-worker-cli-request-0001",
            "--timeout-seconds",
            "60",
            "--argument",
            "exec",
            "--argument",
            "Implement only the approved report.",
        ]
    )

    assert _dispatch(arguments, Client()) == {"status": "dispatched"}  # type: ignore[arg-type]
    assert observed == {
        "report_id": REPORT_ID,
        "lease_id": LEASE_ID,
        "expected_report_revision": 5,
        "response_id": RESPONSE_ID,
        "idempotency_key": "developer-worker-cli-request-0001",
        "arguments": ["exec", "Implement only the approved report."],
        "timeout_seconds": 60,
    }
