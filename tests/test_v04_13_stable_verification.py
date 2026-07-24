from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_platform.collaboration_models import (
    VerificationClaim,
    VerificationResultPayload,
    VerificationVerdict,
    frozen_claim_context_digest,
)
from agent_platform.independent_verifier import verify_frozen_claim
from agent_platform.stable_verification import (
    StableVerificationLedger,
    StableVerificationManager,
    StableVerificationRejected,
)
from agent_platform.task_packages import (
    ArchiveClaimBinding,
    ArchiveStatus,
    TaskPackageManager,
    ValidationMode,
    WorkspaceRole,
)
from agent_platform.workflow_models import ApplicationSnapshot
from tests.test_v04_13_independent_verification import (
    REVISION,
    TASK_ID,
    VerificationCase,
    _build_case,
    _digest_bytes,
    _json_bytes,
)
from tests.test_v04_13_task_packages import (
    WORKSPACE_MANIFEST_FILE,
    _archive_files,
    _build_formal_assignment,
    _environment_secret_resolver,
    _real_health_endpoints,
)


@dataclass(frozen=True)
class _StableCase:
    state_root: Path
    claim: VerificationClaim
    result: VerificationResultPayload


def _claim(
    *,
    claim_id: UUID,
    channel_id: UUID,
    assignment_id: UUID,
    application_id: UUID,
    run_id: str,
    test_run_id: str,
    package_digest: str,
    environment_ready_digest: str,
    archive_manifest_digest: str,
    verification_process_digest: str,
    snapshot: ApplicationSnapshot,
    artifact_digest: str,
) -> VerificationClaim:
    payload: dict[str, Any] = {
        "schema_version": "1.1",
        "claim_id": str(claim_id),
        "application_id": str(application_id),
        "draft_revision": 7,
        "content_hash": f"sha256:{snapshot.content_hash()}",
        "published_version": 3,
        "test_run_ids": [test_run_id],
        "business_run_ids": [run_id],
        "artifact_refs": [
            {
                "evidence_id": f"artifact:{run_id}",
                "kind": "artifact",
                "digest": artifact_digest,
                "media_type": "application/json",
                "label": "business-result.json",
                "captured_at": "2026-07-23T00:00:00Z",
            }
        ],
        "host_receipt_refs": [],
        "resolved_report_ids": [],
        "remaining_limits": [],
        "task_package_digest": package_digest,
        "environment_ready_digest": environment_ready_digest,
        "archive_manifest_digest": archive_manifest_digest,
        "verification_process_digest": verification_process_digest,
        "validation_mode": "real_host",
        "claim": "ready_for_independent_verification",
    }
    payload["frozen_context_digest"] = frozen_claim_context_digest(payload)
    return VerificationClaim.model_validate(
        {
            **payload,
            "channel_id": str(channel_id),
            "assignment_id": str(assignment_id),
            "claim_revision": 1,
            "status": "frozen",
            "created_at": "2026-07-24T00:00:00Z",
        }
    )


def _additional_case(
    base: VerificationCase,
    tmp_path: Path,
    *,
    ordinal: int,
    actual_status: str = "completed",
    environment_instance_id: str | None = None,
) -> _StableCase:
    manager = TaskPackageManager(
        base.state_root,
        environment_secret_resolver=_environment_secret_resolver,
    )
    package = manager.load_frozen(TASK_ID, REVISION)
    run_id = f"business-run-{ordinal:04d}"
    test_run_id = f"workflow-test-run-{ordinal:04d}"
    assignment_id = UUID(int=0x10000000000040008000000000000000 + ordinal)
    application_id = UUID(int=0x20000000000040008000000000000000 + ordinal)
    claim_id = UUID(int=0x30000000000040008000000000000000 + ordinal)
    channel_id = UUID(int=0x40000000000040008000000000000000 + ordinal)
    with _real_health_endpoints(package):
        ready_path, _ = manager.run_environment_preflight(
            package,
            run_id=run_id,
            assignment_id=assignment_id,
            environment_instance_id=(
                environment_instance_id
                or f"paperless-instance-{ordinal:04d}"
            ),
        )
    _, ready_digest = manager.require_environment_ready(
        package,
        ready_path,
        run_id=run_id,
        assignment_id=assignment_id,
    )
    workspace = tmp_path / f"stable-workspace-{ordinal}"
    manager.materialize_task_workspace(
        package,
        workspace,
        role=WorkspaceRole.lilies,
        run_id=run_id,
        assignment_id=assignment_id,
        environment_ready_path=ready_path,
    )
    assignment = _build_formal_assignment(
        manager,
        package,
        ready_path=ready_path,
        workspace=workspace,
        run_id=run_id,
        assignment_id=assignment_id,
    )
    snapshot = ApplicationSnapshot(
        name=f"Enterprise document reconciliation {ordinal}",
        description="A governed Paperless document workflow.",
        requirement="Reconcile the document and preserve audit evidence.",
    )
    artifact_payload = _json_bytes(
        {
            "business_status": actual_status,
            "source": "real-host-business-run",
        }
    )
    artifact_digest = _digest_bytes(artifact_payload)
    binding = ArchiveClaimBinding(
        claim_id=claim_id,
        assignment_id=assignment_id,
        application_id=application_id,
        draft_revision=7,
        content_hash=f"sha256:{snapshot.content_hash()}",
        published_version=3,
        test_run_ids=[test_run_id],
        business_run_ids=[run_id],
        artifact_digests=[artifact_digest],
    )
    files = _archive_files(
        snapshot,
        binding,
        package=package,
        run_id=run_id,
        assignment=assignment,
        business_status=actual_status,
        archive_status=ArchiveStatus.succeeded,
        validation_mode=ValidationMode.real_host,
        artifact_payload=artifact_payload,
        artifact_label="business-result.json",
        artifact_archive_path=(
            f"artifacts/00000000-0000-4000-8000-{ordinal:012d}.bin"
        ),
    )
    scan = json.loads(files["forbidden-assistance-scan.json"])
    findings = [
        f"{item['rule_id']}:{item['source_ref']}"
        for item in scan["findings"]
    ]
    _, _, archive_digest = manager.archive_run(
        package,
        run_id=run_id,
        status=ArchiveStatus.succeeded,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=ready_path,
        workspace_manifest_path=workspace / WORKSPACE_MANIFEST_FILE,
        files=files,
        claim_binding=binding,
        forbidden_assistance_findings=findings,
    )
    claim = _claim(
        claim_id=claim_id,
        channel_id=channel_id,
        assignment_id=assignment_id,
        application_id=application_id,
        run_id=run_id,
        test_run_id=test_run_id,
        package_digest=package.record.public_summary_digest,
        environment_ready_digest=ready_digest,
        archive_manifest_digest=archive_digest,
        verification_process_digest=package.record.verification_process_digest,
        snapshot=snapshot,
        artifact_digest=artifact_digest,
    )
    result = verify_frozen_claim(
        state_root=base.state_root,
        task_id=TASK_ID,
        revision=REVISION,
        claim=claim,
    )
    return _StableCase(
        state_root=base.state_root,
        claim=claim,
        result=result,
    )


def _base_stable_case(tmp_path: Path) -> tuple[VerificationCase, _StableCase]:
    base = _build_case(tmp_path)
    return base, _StableCase(
        state_root=base.state_root,
        claim=base.claim,
        result=verify_frozen_claim(
            state_root=base.state_root,
            task_id=TASK_ID,
            revision=REVISION,
            claim=base.claim,
        ),
    )


def _seed(
    manager: StableVerificationManager,
    case: _StableCase,
) -> Path:
    return manager.resolve_hidden_seed_evidence(
        task_id=TASK_ID,
        revision=REVISION,
        claim=case.claim,
    )


def _stable_manager(
    state_root: Path,
    *,
    seed_key_resolver: Any | None = None,
) -> StableVerificationManager:
    return StableVerificationManager(
        state_root,
        trusted_result_resolver=lambda task_id, revision, claim: (
            verify_frozen_claim(
                state_root=state_root,
                task_id=task_id,
                revision=revision,
                claim=claim,
            )
        ),
        platform_seed_key_resolver=(
            seed_key_resolver
            or (
                lambda context: hashlib.sha256(
                    context.environment_instance_id.encode("utf-8")
                ).digest()
            )
        ),
    )


def test_stable_verdict_requires_frozen_number_of_distinct_hidden_runs(
    tmp_path: Path,
) -> None:
    base, first = _base_stable_case(tmp_path)
    second = _additional_case(base, tmp_path, ordinal=2)
    manager = _stable_manager(base.state_root)
    _seed(manager, first)
    _seed(manager, second)

    first_progress = manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
        result=first.result,
    )

    assert first_progress.stable_hidden_runs == 2
    assert first_progress.consecutive_passes == 1
    assert first_progress.stable_verdict is None
    with pytest.raises(StableVerificationRejected, match="no current stable"):
        manager.replay_current_stable_verdict(
            task_id=TASK_ID,
            revision=REVISION,
        )

    second_progress = manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=second.claim,
        result=second.result,
    )
    replay = manager.replay_current_stable_verdict(
        task_id=TASK_ID,
        revision=REVISION,
    )

    assert second_progress.consecutive_passes == 2
    assert second_progress.stable_verdict == replay
    assert replay.stable_hidden_runs == 2
    assert replay.verdict == "stably_independently_verified"
    ledger_payload = (
        base.state_root
        / "stable-verification"
        / hashlib.sha256(TASK_ID.encode()).hexdigest()
        / str(REVISION)
        / "ledger.json"
    ).read_bytes()
    assert b"paperless-instance-0001" not in ledger_payload
    assert b"paperless-instance-0002" not in ledger_payload


def test_seed_and_record_replay_are_exactly_idempotent_across_restart(
    tmp_path: Path,
) -> None:
    base, first = _base_stable_case(tmp_path)
    manager = _stable_manager(base.state_root)
    seed = _seed(manager, first)
    seed_payload = seed.read_bytes()
    replayed_seed = _seed(manager, first)
    first_progress = manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
        result=first.result,
    )
    restarted = _stable_manager(base.state_root)
    replayed_progress = restarted.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
        result=first.result,
    )

    assert replayed_seed == seed
    assert replayed_seed.read_bytes() == seed_payload
    assert replayed_progress == first_progress


def test_missing_trusted_resolver_and_forged_pass_fail_closed(
    tmp_path: Path,
) -> None:
    base, first = _base_stable_case(tmp_path)
    trusted = _stable_manager(base.state_root)
    _seed(trusted, first)
    without_resolver = StableVerificationManager(base.state_root)
    with pytest.raises(StableVerificationRejected, match="trusted result resolver"):
        without_resolver.record(
            task_id=TASK_ID,
            revision=REVISION,
            claim=first.claim,
            result=first.result,
        )
    without_seed_resolver = StableVerificationManager(
        base.state_root,
        trusted_result_resolver=lambda task_id, revision, claim: (
            verify_frozen_claim(
                state_root=base.state_root,
                task_id=task_id,
                revision=revision,
                claim=claim,
            )
        ),
    )
    with pytest.raises(StableVerificationRejected, match="hidden-seed resolver"):
        without_seed_resolver.record(
            task_id=TASK_ID,
            revision=REVISION,
            claim=first.claim,
            result=first.result,
        )
    forged_payload = first.result.model_dump(mode="json")
    forged_payload["oracle_digest"] = "sha256:" + "f" * 64
    forged = VerificationResultPayload.model_validate(forged_payload)

    with pytest.raises(StableVerificationRejected, match="isolated verifier receipt"):
        trusted.record(
            task_id=TASK_ID,
            revision=REVISION,
            claim=first.claim,
            result=forged,
        )


def test_caller_cannot_forge_seed_or_environment_binding(
    tmp_path: Path,
) -> None:
    base, first = _base_stable_case(tmp_path)
    manager = _stable_manager(base.state_root)
    seed_path = _seed(manager, first)
    payload = json.loads(seed_path.read_bytes())
    payload["hidden_seed_digest"] = "sha256:" + "f" * 64
    payload["environment_instance_id"] = "caller-invented-environment"
    payload_without_digest = {
        key: value
        for key, value in payload.items()
        if key != "evidence_digest"
    }
    payload["evidence_digest"] = _digest_bytes(
        _json_bytes(payload_without_digest)
    )
    os.chmod(seed_path, 0o600)
    seed_path.write_bytes(_json_bytes(payload))
    os.chmod(seed_path, 0o400)

    with pytest.raises(
        StableVerificationRejected,
        match="identity was reused",
    ):
        manager.record(
            task_id=TASK_ID,
            revision=REVISION,
            claim=first.claim,
            result=first.result,
        )

    assert not hasattr(manager, "create_hidden_seed_evidence")


def test_stability_lock_symlink_is_rejected(
    tmp_path: Path,
) -> None:
    base, first = _base_stable_case(tmp_path)
    manager = _stable_manager(base.state_root)
    _seed(manager, first)
    lock_path = manager._lock_path(TASK_ID, REVISION)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    unsafe_target = tmp_path / "unsafe-lock-target"
    unsafe_target.write_bytes(b"")
    lock_path.symlink_to(unsafe_target)

    with pytest.raises(StableVerificationRejected, match="lock"):
        manager.record(
            task_id=TASK_ID,
            revision=REVISION,
            claim=first.claim,
            result=first.result,
        )


def test_duplicate_hidden_seed_rejects_and_resets_the_current_streak(
    tmp_path: Path,
) -> None:
    base, first = _base_stable_case(tmp_path)
    second = _additional_case(base, tmp_path, ordinal=2)
    manager = _stable_manager(
        base.state_root,
        seed_key_resolver=lambda _context: b"same-seed-key".ljust(32, b"!"),
    )
    _seed(manager, first)
    _seed(manager, second)
    manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
        result=first.result,
    )

    with pytest.raises(StableVerificationRejected, match="duplicate_hidden_seed"):
        manager.record(
            task_id=TASK_ID,
            revision=REVISION,
            claim=second.claim,
            result=second.result,
        )

    with pytest.raises(StableVerificationRejected, match="no current stable"):
        manager.replay_current_stable_verdict(
            task_id=TASK_ID,
            revision=REVISION,
        )


def test_failed_independent_result_interrupts_stability(
    tmp_path: Path,
) -> None:
    base, first = _base_stable_case(tmp_path)
    failed = _additional_case(
        base,
        tmp_path,
        ordinal=2,
        actual_status="needs-review",
    )
    third = _additional_case(base, tmp_path, ordinal=3)
    assert failed.result.verdict is VerificationVerdict.verification_failed
    manager = _stable_manager(base.state_root)
    cases = (first, failed, third)
    for case in cases:
        _seed(manager, case)
    first_progress = manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
        result=first.result,
    )
    failed_progress = manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=failed.claim,
        result=failed.result,
    )
    third_progress = manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=third.claim,
        result=third.result,
    )

    assert first_progress.consecutive_passes == 1
    assert failed_progress.consecutive_passes == 0
    assert third_progress.consecutive_passes == 1
    assert third_progress.stable_verdict is None


def test_old_exact_replay_cannot_resurrect_a_stable_verdict_after_failure(
    tmp_path: Path,
) -> None:
    base, first = _base_stable_case(tmp_path)
    second = _additional_case(base, tmp_path, ordinal=2)
    failed = _additional_case(
        base,
        tmp_path,
        ordinal=3,
        actual_status="needs-review",
    )
    manager = _stable_manager(base.state_root)
    cases = (first, second, failed)
    for case in cases:
        _seed(manager, case)
    for case in cases[:2]:
        stable_progress = manager.record(
            task_id=TASK_ID,
            revision=REVISION,
            claim=case.claim,
            result=case.result,
        )
    assert stable_progress.stable_verdict is not None

    failed_progress = manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=failed.claim,
        result=failed.result,
    )
    replayed_old_success = manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=second.claim,
        result=second.result,
    )

    assert failed_progress.consecutive_passes == 0
    assert replayed_old_success.consecutive_passes == 0
    assert replayed_old_success.stable_verdict is None
    assert replayed_old_success.progress_digest == failed_progress.progress_digest
    with pytest.raises(StableVerificationRejected, match="no current stable"):
        manager.replay_current_stable_verdict(
            task_id=TASK_ID,
            revision=REVISION,
        )


def test_reusing_assignment_archive_or_claim_is_an_idempotent_non_qualification(
    tmp_path: Path,
) -> None:
    base, first = _base_stable_case(tmp_path)
    manager = _stable_manager(base.state_root)
    _seed(manager, first)
    first_progress = manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
        result=first.result,
    )
    replay = manager.record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
        result=first.result,
    )

    assert first_progress.consecutive_passes == 1
    assert replay.consecutive_passes == 1
    assert replay.stable_verdict is None


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("verification_process_digest", "sha256:" + "f" * 64),
        ("task_package_digest", "sha256:" + "e" * 64),
        ("frozen_context_digest", "sha256:" + "d" * 64),
    ],
)
def test_package_verifier_or_hash_drift_resets_a_prior_stable_verdict(
    tmp_path: Path,
    field: str,
    forged_value: str,
) -> None:
    base, first = _base_stable_case(tmp_path)
    second = _additional_case(base, tmp_path, ordinal=2)
    manager = _stable_manager(base.state_root)
    for case in (first, second):
        _seed(manager, case)
        progress = manager.record(
            task_id=TASK_ID,
            revision=REVISION,
            claim=case.claim,
            result=case.result,
        )
    assert progress.stable_verdict is not None
    forged_payload = second.result.model_dump(mode="json")
    forged_payload[field] = forged_value
    forged = VerificationResultPayload.model_validate(forged_payload)

    with pytest.raises(StableVerificationRejected, match="does not match"):
        manager.record(
            task_id=TASK_ID,
            revision=REVISION,
            claim=second.claim,
            result=forged,
        )
    with pytest.raises(StableVerificationRejected, match="no current stable"):
        manager.replay_current_stable_verdict(
            task_id=TASK_ID,
            revision=REVISION,
        )


def test_ledger_and_verdict_byte_drift_are_rejected_on_replay(
    tmp_path: Path,
) -> None:
    base, first = _base_stable_case(tmp_path)
    second = _additional_case(base, tmp_path, ordinal=2)
    manager = _stable_manager(base.state_root)
    for case in (first, second):
        _seed(manager, case)
        progress = manager.record(
            task_id=TASK_ID,
            revision=REVISION,
            claim=case.claim,
            result=case.result,
        )
    assert progress.stable_verdict is not None
    ledger_path = manager._ledger_path(TASK_ID, REVISION)
    ledger = StableVerificationLedger.model_validate_json(ledger_path.read_bytes())
    verdict_path = manager._verdict_path(
        TASK_ID,
        REVISION,
        str(ledger.latest_stable_verdict_digest),
    )
    original_verdict = verdict_path.read_bytes()
    os.chmod(verdict_path, 0o600)
    verdict_path.write_bytes(original_verdict + b"\n")
    os.chmod(verdict_path, 0o400)

    with pytest.raises(StableVerificationRejected):
        manager.replay_current_stable_verdict(
            task_id=TASK_ID,
            revision=REVISION,
        )
