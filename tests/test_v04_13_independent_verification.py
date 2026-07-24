from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent_platform.collaboration_models import (
    VerificationClaim,
    VerificationClaimPayload,
    VerificationResultPayload,
    VerificationVerdict,
    frozen_claim_context_digest,
)
from agent_platform import independent_verifier as verifier_module
from agent_platform.independent_verifier import (
    IndependentVerificationRejected,
    _json_values_equal,
    verify_frozen_claim,
)
from agent_platform.task_packages import (
    ArchiveClaimBinding,
    ArchiveStatus,
    TaskPackageError,
    TaskPackageManager,
    TaskPackageNotReady,
    ValidationMode,
    WorkspaceRole,
)
from agent_platform.workflow_models import ApplicationSnapshot
from tests.test_v04_13_task_packages import (
    ATTESTATION_BODY,
    ATTESTATION_SECRET_REF,
    _archive_files,
    _build_formal_assignment,
    _environment_secret_resolver,
    _real_health_endpoints,
    _unused_tcp_port,
)


TASK_ID = "V04-13-T01F-enterprise-documents"
REVISION = 1
RUN_ID = "business-run-0001"
TEST_RUN_ID = "workflow-test-run-0001"
ASSIGNMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
APPLICATION_ID = UUID("20000000-0000-4000-8000-000000000002")
CLAIM_ID = UUID("30000000-0000-4000-8000-000000000003")
CHANNEL_ID = UUID("40000000-0000-4000-8000-000000000004")
LEAK_MARKER = "HIDDEN-ORACLE-CANARY-7f39c8a2"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _source_project() -> dict[str, Any]:
    return {
        "name": "paperless-ngx",
        "repository_url": "https://example.invalid/paperless-ngx.git",
        "release": "v2.18.4",
        "commit_sha": "a" * 40,
        "image_digest": _digest_bytes(b"paperless-image"),
        "license": "GPL-3.0",
    }


def _make_package_source(
    root: Path,
    *,
    oracle_expected_status: str = "completed",
    oracle_check_id: str = "business-status",
    health_port: int,
) -> Path:
    source = root / "task-source"
    attestation_port = _unused_tcp_port()
    while attestation_port == health_port:
        attestation_port = _unused_tcp_port()
    fixture_payload = _json_bytes({"document_id": 41, "title": "Quarterly report"})
    fixture_entry = {
        "path": "public-inputs/document.json",
        "digest": _digest_bytes(fixture_payload),
        "size_bytes": len(fixture_payload),
    }
    fixture_manifest = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "revision": REVISION,
        "files": [fixture_entry],
    }
    fixture_manifest_payload = _json_bytes(fixture_manifest)

    environment = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "revision": REVISION,
        "source_projects": [_source_project()],
        "compose_digest": _digest_bytes(b"compose-lock"),
        "ports": [
            {
                "service": "paperless",
                "host": "127.0.0.1",
                "port": health_port,
            },
            {
                "service": "environment-attestation",
                "host": "127.0.0.1",
                "port": attestation_port,
            },
        ],
        "network_name": "lilies-t01f",
        "volumes": ["paperless-data"],
        "initialization_commands": [{"name": "initialize", "digest": _digest_bytes(b"initialize")}],
        "seed_commands": [{"name": "seed", "digest": _digest_bytes(b"seed")}],
        "health_checks": [
            {
                "check_id": "paperless-health",
                "kind": "tcp",
                "host": "127.0.0.1",
                "port": health_port,
                "timeout_seconds": 1.0,
                "mandatory": True,
            },
            {
                "check_id": "environment-identity",
                "kind": "http",
                "url": f"http://127.0.0.1:{attestation_port}/identity",
                "expected_status": 200,
                "expected_body_digest": _digest_bytes(ATTESTATION_BODY),
                "timeout_seconds": 1.0,
                "mandatory": True,
            },
        ],
        "secret_refs": [ATTESTATION_SECRET_REF],
        "attestation_secret_ref": ATTESTATION_SECRET_REF,
        "python_version": "3.12.8",
        "node_version": "22.14.0",
        "docker_version": "27.5.1",
        "fixture_files": [fixture_entry],
        "fault_injections": [],
        "provenance": "real_host",
    }
    environment_payload = _json_bytes(environment)

    allowed_actions = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "revision": REVISION,
        "readable_host_objects": ["paperless:documents"],
        "writable_host_operations": ["paperless:create-document"],
        "platform_actions": ["platform_contract_get", "platform_tests_run"],
        "network_hosts": ["127.0.0.1"],
        "model_access": True,
        "file_access": True,
        "connector_access": True,
        "permission_required_actions": [],
        "max_write_count": 20,
        "max_payload_bytes": 1_048_576,
        "compensation_actions": ["paperless:delete-created-document"],
        "prohibited_actions": [
            "read_platform_source",
            "read_platform_database",
            "read_protected",
            "modify_task_package",
            "install_unknown_adapter",
        ],
        "validation_mode": "real_host",
    }
    budget = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "revision": REVISION,
        "max_build_repair_turns": 20,
        "max_model_cost_usd": 50.0,
        "assignment_wall_clock_seconds": 3600,
        "max_platform_tool_calls": 500,
        "max_report_evidence_rounds": 5,
        "stable_hidden_runs": 2,
    }
    task = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "revision": REVISION,
        "title": "Reconcile enterprise document intake",
        "cohort": "enterprise",
        "customer_role": "Operations manager",
        "business_goal": "Ingest and reconcile governed customer documents.",
        "source_projects": [_source_project()],
        "requirement_file": "requirement.md",
        "environment_lock_digest": _digest_bytes(environment_payload),
        "fixture_manifest_digest": _digest_bytes(fixture_manifest_payload),
        "allowed_actions_file": "allowed-actions.json",
        "budget_file": "budget.json",
        "deliverables": [
            {
                "name": "reconciliation-result",
                "description": "A governed document reconciliation result.",
                "media_type": "application/json",
            }
        ],
        "acceptance_summary": "The real host contains the reconciled document state.",
        "no_substitute_validation": True,
        "collaboration_enabled": True,
        "author": "task-author",
        "created_at": "2026-07-24T00:00:00Z",
    }
    oracle = {
        "schema_version": "1.0",
        "oracle_id": "document-oracle-v1",
        "task_id": TASK_ID,
        "revision": REVISION,
        "validation_mode": "real_host",
        "checks": [
            {
                "check_id": oracle_check_id,
                "kind": "json_equals",
                "evidence_selector": {
                    "kind": "artifact",
                    "label": "business-result.json",
                },
                "json_pointer": "/business_status",
                "expected": oracle_expected_status,
            }
        ],
    }

    _write(source / "task.yaml", _json_bytes(task))
    _write(
        source / "requirement.md",
        b"Reconcile the enterprise document on the real Paperless host.\n",
    )
    _write(source / "environment.lock", environment_payload)
    _write(source / "allowed-actions.json", _json_bytes(allowed_actions))
    _write(source / "budget.json", _json_bytes(budget))
    _write(source / "fixtures" / "manifest.json", fixture_manifest_payload)
    _write(source / "fixtures" / "public-inputs" / "document.json", fixture_payload)
    _write(source / "protected" / "oracle" / "oracle.json", _json_bytes(oracle))
    _write(
        source / "protected" / "leak-markers.json",
        _json_bytes({"markers": [LEAK_MARKER]}),
    )
    return source


def _claim_payload(
    *,
    package_digest: str,
    environment_ready_digest: str,
    archive_manifest_digest: str,
    verification_process_digest: str,
    snapshot: ApplicationSnapshot,
    artifact_digest: str,
) -> VerificationClaim:
    payload: dict[str, Any] = {
        "schema_version": "1.1",
        "claim_id": str(CLAIM_ID),
        "application_id": str(APPLICATION_ID),
        "draft_revision": 7,
        "content_hash": f"sha256:{snapshot.content_hash()}",
        "published_version": 3,
        "test_run_ids": [TEST_RUN_ID],
        "business_run_ids": [RUN_ID],
        "artifact_refs": [
            {
                "evidence_id": "artifact:business-result",
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
    payload.update(
        {
            "channel_id": str(CHANNEL_ID),
            "assignment_id": str(ASSIGNMENT_ID),
            "claim_revision": 1,
            "status": "frozen",
            "created_at": "2026-07-24T00:00:00Z",
        }
    )
    return VerificationClaim.model_validate(payload)


@dataclass(frozen=True)
class VerificationCase:
    state_root: Path
    archive_root: Path
    claim: VerificationClaim
    result_path: Path
    environment_ready_path: Path


def _build_case(
    tmp_path: Path,
    *,
    actual_status: str = "completed",
    oracle_expected_status: str = "completed",
    oracle_check_id: str = "business-status",
    archive_status: ArchiveStatus = ArchiveStatus.succeeded,
    validation_mode: ValidationMode = ValidationMode.real_host,
    leak_payload: bool = False,
    artifact_archive_path: str = (
        "artifacts/00000000-0000-4000-8000-000000000101.bin"
    ),
) -> VerificationCase:
    state_root = tmp_path / "task-state"
    manager = TaskPackageManager(
        state_root,
        environment_secret_resolver=_environment_secret_resolver,
    )
    source = _make_package_source(
        tmp_path,
        oracle_expected_status=oracle_expected_status,
        oracle_check_id=oracle_check_id,
        health_port=_unused_tcp_port(),
    )
    package = manager.freeze_revision(source)
    with _real_health_endpoints(package):
        ready_path, _ = manager.run_environment_preflight(
            package,
            run_id=RUN_ID,
            assignment_id=ASSIGNMENT_ID,
            environment_instance_id="paperless-instance-0001",
        )
    _, ready_digest = manager.require_environment_ready(
        package,
        ready_path,
        run_id=RUN_ID,
        assignment_id=ASSIGNMENT_ID,
    )
    workspace = tmp_path / "lilies-workspace"
    manager.materialize_task_workspace(
        package,
        workspace,
        role=WorkspaceRole.lilies,
        run_id=RUN_ID,
        assignment_id=ASSIGNMENT_ID,
        environment_ready_path=ready_path,
    )
    workspace_manifest = workspace / ".lilies-mount-manifest.json"
    assignment = _build_formal_assignment(
        manager,
        package,
        ready_path=ready_path,
        workspace=workspace,
        run_id=RUN_ID,
        assignment_id=ASSIGNMENT_ID,
    )

    snapshot = ApplicationSnapshot(
        name="Enterprise document reconciliation",
        description="A governed Paperless document workflow.",
        requirement="Reconcile the document and preserve audit evidence.",
    )
    content_hash = f"sha256:{snapshot.content_hash()}"
    artifact_payload = _json_bytes(
        {
            "business_status": (
                LEAK_MARKER if leak_payload else actual_status
            ),
            "source": "real-host-business-run",
        }
    )
    artifact_digest = _digest_bytes(artifact_payload)
    binding = ArchiveClaimBinding(
        claim_id=CLAIM_ID,
        assignment_id=ASSIGNMENT_ID,
        application_id=APPLICATION_ID,
        draft_revision=7,
        content_hash=content_hash,
        published_version=3,
        test_run_ids=[TEST_RUN_ID],
        business_run_ids=[RUN_ID],
        artifact_digests=[artifact_digest],
    )
    files = _archive_files(
        snapshot,
        binding,
        package=package,
        run_id=RUN_ID,
        assignment=assignment,
        business_status=LEAK_MARKER if leak_payload else actual_status,
        archive_status=archive_status,
        validation_mode=validation_mode,
        artifact_payload=artifact_payload,
        artifact_label="business-result.json",
        artifact_archive_path=artifact_archive_path,
    )
    assistance_scan = json.loads(
        files["forbidden-assistance-scan.json"].decode("utf-8")
    )
    forbidden_assistance_findings = [
        f"{item['rule_id']}:{item['source_ref']}"
        for item in assistance_scan["findings"]
    ]
    archive_root, _, archive_digest = manager.archive_run(
        package,
        run_id=RUN_ID,
        status=archive_status,
        validation_mode=validation_mode,
        environment_ready_path=ready_path,
        workspace_manifest_path=workspace_manifest,
        files=files,
        claim_binding=binding,
        forbidden_assistance_findings=forbidden_assistance_findings,
    )
    claim = _claim_payload(
        package_digest=package.record.public_summary_digest,
        environment_ready_digest=ready_digest,
        archive_manifest_digest=archive_digest,
        verification_process_digest=(
            package.record.verification_process_digest
        ),
        snapshot=snapshot,
        artifact_digest=artifact_digest,
    )
    return VerificationCase(
        state_root=state_root,
        archive_root=archive_root,
        claim=claim,
        result_path=archive_root / "result.json",
        environment_ready_path=ready_path,
    )


def _verify(case: VerificationCase) -> VerificationResultPayload:
    return verify_frozen_claim(
        state_root=case.state_root,
        task_id=TASK_ID,
        revision=REVISION,
        claim=case.claim,
    )


def _tree_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _digest_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_independent_verifier_accepts_frozen_real_host_business_evidence(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path)

    result = _verify(case)

    assert result.schema_version == "1.1"
    assert result.verdict is VerificationVerdict.independently_verified
    assert result.differences == []
    evidence_id = result.evidence_refs[0].evidence_id
    assert evidence_id.startswith("archive-check:oracle-check:")
    assert "business-status" not in evidence_id
    assert result.validation_mode == "real_host"


def test_oracle_resolves_random_archive_uuid_by_stable_evidence_selector(
    tmp_path: Path,
) -> None:
    random_path = f"artifacts/{uuid4()}.bin"
    case = _build_case(
        tmp_path,
        artifact_archive_path=random_path,
    )

    result = _verify(case)
    index = json.loads(
        (case.archive_root / "evidence-index.json").read_bytes()
    )

    assert result.verdict is VerificationVerdict.independently_verified
    assert index["entries"][0]["evidence_key"] == (
        "artifact:business-result.json"
    )
    assert index["entries"][0]["archive_path"] == random_path


def test_task_freeze_rejects_oracle_that_only_checks_result_status(
    tmp_path: Path,
) -> None:
    source = _make_package_source(
        tmp_path,
        health_port=_unused_tcp_port(),
    )
    oracle_path = source / "protected/oracle/oracle.json"
    oracle = json.loads(oracle_path.read_bytes())
    oracle["checks"][0].pop("evidence_selector")
    oracle["checks"][0]["archive_path"] = "result.json"
    oracle_path.write_bytes(_json_bytes(oracle))

    with pytest.raises(
        TaskPackageError,
        match="oracle identity or checks",
    ):
        TaskPackageManager(tmp_path / "task-state").freeze_revision(source)


def test_business_oracle_difference_returns_structured_failed_verdict(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path, actual_status="needs-review")

    result = _verify(case)

    assert result.verdict is VerificationVerdict.verification_failed
    assert len(result.differences) == 1
    difference = result.differences[0]
    assert difference.check_id.startswith("oracle-check:")
    assert "business-status" not in difference.check_id
    assert difference.expected == "oracle expected value was not satisfied"
    assert difference.actual == "needs-review"
    assert difference.evidence_refs == result.evidence_refs
    assert "completed" not in json.dumps(
        result.model_dump(mode="json"),
        sort_keys=True,
    )


def test_protocol_mock_archive_cannot_be_independently_verified(
    tmp_path: Path,
) -> None:
    case = _build_case(
        tmp_path,
        archive_status=ArchiveStatus.failed,
        validation_mode=ValidationMode.protocol_mock,
    )

    with pytest.raises(IndependentVerificationRejected):
        _verify(case)


def test_failed_real_host_health_issues_no_environment_ready(
    tmp_path: Path,
) -> None:
    manager = TaskPackageManager(
        tmp_path / "task-state",
        environment_secret_resolver=_environment_secret_resolver,
    )
    package = manager.freeze_revision(
        _make_package_source(
            tmp_path,
            health_port=_unused_tcp_port(),
        )
    )

    with pytest.raises(TaskPackageNotReady):
        manager.run_environment_preflight(
            package,
            run_id=RUN_ID,
            assignment_id=ASSIGNMENT_ID,
            environment_instance_id="paperless-instance-unhealthy",
        )

    preflight = tmp_path / "task-state" / "preflight" / TASK_ID / str(REVISION) / RUN_ID
    assert not (preflight / "environment-ready.json").exists()
    failure = json.loads((preflight / "environment-preflight.json").read_bytes())
    assert failure["ready"] is False
    assert failure["checks"][0]["passed"] is False


def test_archive_byte_drift_is_rejected_before_oracle_execution(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path)
    os.chmod(case.result_path, 0o600)
    case.result_path.write_bytes(_json_bytes({"status": "completed", "document_id": 999}))

    with pytest.raises(IndependentVerificationRejected):
        _verify(case)


def test_archive_containing_hidden_oracle_marker_is_not_claimable(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path, leak_payload=True)
    manifest = json.loads((case.archive_root / "archive-manifest.json").read_bytes())

    assert manifest["status"] == "invalid"
    assert manifest["security_findings"] == [
        (
            "protected_oracle_marker:"
            "artifacts/00000000-0000-4000-8000-000000000101.bin"
        ),
        "protected_oracle_marker:result.json",
    ]
    with pytest.raises(IndependentVerificationRejected):
        _verify(case)


def test_verifier_redacts_difference_that_would_disclose_hidden_oracle_marker(
    tmp_path: Path,
) -> None:
    case = _build_case(
        tmp_path,
        actual_status="public-observed-value",
        oracle_expected_status=LEAK_MARKER,
    )

    result = _verify(case)
    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    assert result.verdict is VerificationVerdict.verification_failed
    assert LEAK_MARKER not in serialized
    assert result.differences[0].expected == (
        "oracle expected value was not satisfied"
    )


def test_verifier_hashes_hidden_check_id_that_contains_an_oracle_marker(
    tmp_path: Path,
) -> None:
    case = _build_case(
        tmp_path,
        oracle_check_id=LEAK_MARKER,
    )

    result = _verify(case)
    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    assert result.verdict is VerificationVerdict.independently_verified
    assert LEAK_MARKER not in serialized


def test_public_check_id_cannot_be_dictionary_recovered_from_result_commitment(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path)

    result = _verify(case)

    public_id = result.evidence_refs[0].evidence_id.removeprefix(
        "archive-check:"
    )
    guessed = (
        "oracle-check:"
        + hashlib.sha256(
            f"{result.oracle_digest}\0business-status".encode("utf-8")
        ).hexdigest()[:32]
    )
    assert guessed != public_id


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (True, 1),
        (False, 0),
        ({"value": True}, {"value": 1}),
        ([1, False], [1, 0]),
    ],
)
def test_oracle_json_equality_does_not_coerce_boolean_and_number(
    actual: Any,
    expected: Any,
) -> None:
    assert _json_values_equal(actual, expected) is False


def test_oracle_bytes_must_match_the_frozen_package_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_case(tmp_path)
    oracle_path = (
        case.state_root
        / "packages"
        / TASK_ID
        / str(REVISION)
        / "protected"
        / "oracle"
        / "oracle.json"
    )
    changed = json.loads(oracle_path.read_bytes())
    changed["checks"][0]["expected"] = "needs-review"
    changed_payload = _json_bytes(changed)
    original_read = verifier_module._read_regular

    def swapped_read(path: Path, *, limit: int) -> bytes:
        if Path(path) == oracle_path:
            return changed_payload
        return original_read(path, limit=limit)

    monkeypatch.setattr(verifier_module, "_read_regular", swapped_read)

    with pytest.raises(
        IndependentVerificationRejected,
        match="frozen package record",
    ):
        _verify(case)


def test_cli_runs_in_fresh_process_writes_only_result_out_and_leaks_no_oracle(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path)
    claim_file = tmp_path / "claim.json"
    claim_payload = _json_bytes(case.claim.model_dump(mode="json", exclude_none=True))
    claim_file.write_bytes(claim_payload)
    output_dir = tmp_path / "verifier-output"
    output_dir.mkdir()
    result_out = output_dir / "result.json"
    isolated_cwd = tmp_path / "isolated-cwd"
    isolated_cwd.mkdir()
    state_before = _tree_digests(case.state_root)
    claim_before = _digest_bytes(claim_file.read_bytes())
    source_root = Path(__file__).resolve().parents[1] / "platform" / "backend" / "src"
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root)
        if not existing_pythonpath
        else f"{source_root}{os.pathsep}{existing_pythonpath}"
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_platform.independent_verifier",
            "verify",
            "--state-root",
            str(case.state_root),
            "--task-id",
            TASK_ID,
            "--revision",
            str(REVISION),
            "--claim-file",
            str(claim_file),
            "--result-out",
            str(result_out),
        ],
        cwd=isolated_cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    status = json.loads(completed.stdout)
    assert status["status"] == "verification_result_written"
    result = VerificationResultPayload.model_validate_json(result_out.read_bytes())
    assert result.verdict is VerificationVerdict.independently_verified
    assert sorted(path.name for path in output_dir.iterdir()) == ["result.json"]
    assert list(isolated_cwd.iterdir()) == []
    assert _tree_digests(case.state_root) == state_before
    assert _digest_bytes(claim_file.read_bytes()) == claim_before
    exposed = completed.stdout + completed.stderr + result_out.read_text(encoding="utf-8")
    assert LEAK_MARKER not in exposed
    assert "protected/oracle" not in exposed
    assert str(case.state_root / "packages" / TASK_ID / str(REVISION) / "protected") not in exposed


def test_v11_result_schema_preserves_every_frozen_claim_binding(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path)

    result = _verify(case)

    assert result.task_package_digest == case.claim.task_package_digest
    assert result.environment_ready_digest == case.claim.environment_ready_digest
    assert result.archive_manifest_digest == case.claim.archive_manifest_digest
    assert result.frozen_context_digest == case.claim.frozen_context_digest
    assert result.validation_mode == case.claim.validation_mode == "real_host"
    assert result.verification_process_digest is not None
    assert result.verification_process_digest.startswith("sha256:")

    legacy_forge = result.model_dump(mode="json", exclude_none=True)
    legacy_forge["schema_version"] = "1.0"
    with pytest.raises(ValidationError):
        VerificationResultPayload.model_validate(legacy_forge)


def test_legacy_claim_is_rejected_by_formal_verifier(tmp_path: Path) -> None:
    case = _build_case(tmp_path)
    payload = case.claim.model_dump(mode="json", exclude_none=True)
    for field in (
        "channel_id",
        "assignment_id",
        "claim_revision",
        "status",
        "created_at",
    ):
        payload.pop(field)
    payload["schema_version"] = "1.0"
    for field in (
        "task_package_digest",
        "environment_ready_digest",
        "archive_manifest_digest",
        "verification_process_digest",
        "validation_mode",
        "frozen_context_digest",
    ):
        payload.pop(field)
    legacy = VerificationClaimPayload.model_validate(payload)

    with pytest.raises(IndependentVerificationRejected):
        verify_frozen_claim(
            state_root=case.state_root,
            task_id=TASK_ID,
            revision=REVISION,
            claim=legacy,
        )


def test_payload_only_v11_claim_is_rejected_without_server_assignment(
    tmp_path: Path,
) -> None:
    case = _build_case(tmp_path)
    payload = case.claim.model_dump(mode="json", exclude_none=True)
    for field in (
        "channel_id",
        "assignment_id",
        "claim_revision",
        "status",
        "created_at",
    ):
        payload.pop(field)
    payload_only = VerificationClaimPayload.model_validate(payload)

    with pytest.raises(IndependentVerificationRejected):
        verify_frozen_claim(
            state_root=case.state_root,
            task_id=TASK_ID,
            revision=REVISION,
            claim=payload_only,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("application_id", "40000000-0000-4000-8000-000000000004"),
        ("task_package_digest", _digest_bytes(b"forged-package")),
        ("environment_ready_digest", _digest_bytes(b"forged-ready")),
        ("archive_manifest_digest", _digest_bytes(b"forged-archive")),
        (
            "verification_process_digest",
            _digest_bytes(b"forged-verification-policy"),
        ),
        ("business_run_ids", ["business-run-forged"]),
        ("assignment_id", "50000000-0000-4000-8000-000000000005"),
    ],
)
def test_rehashed_but_forged_claim_binding_is_rejected(
    tmp_path: Path,
    field: str,
    forged_value: Any,
) -> None:
    case = _build_case(tmp_path)
    payload = case.claim.model_dump(mode="json", exclude_none=True)
    payload[field] = forged_value
    payload["frozen_context_digest"] = frozen_claim_context_digest(payload)
    forged = VerificationClaim.model_validate(payload)

    with pytest.raises(IndependentVerificationRejected):
        verify_frozen_claim(
            state_root=case.state_root,
            task_id=TASK_ID,
            revision=REVISION,
            claim=forged,
        )
