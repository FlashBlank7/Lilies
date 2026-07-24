from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_platform.collaboration_models import (
    ApprovalDecision,
    CollaborationMessageEnvelope,
    DeveloperResponse,
)
from agent_platform.formal_source_provenance import (
    SOURCE_PROVENANCE_MANIFEST_PATH,
    ApprovedDeveloperResponseBinding,
    FormalSourceProvenanceConflict,
    FormalSourceProvenanceCoordinator,
    FormalSourceProvenanceSecurityError,
    approved_developer_response_bindings,
    capture_git_source_state,
    verify_source_provenance_archive,
    verify_source_provenance_archive_offline,
)


NOW = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    payload = value if isinstance(value, bytes) else _canonical_json(value)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _refresh_manifest_digests(manifest: dict[str, object]) -> bytes:
    commits = manifest["approved_commits"]
    assert isinstance(commits, list)
    for commit in commits:
        assert isinstance(commit, dict)
        commit_without_digest = {
            key: value
            for key, value in commit.items()
            if key != "provenance_digest"
        }
        commit["provenance_digest"] = _digest(commit_without_digest)
    manifest_without_digest = {
        key: value
        for key, value in manifest.items()
        if key != "manifest_digest"
    }
    manifest["manifest_digest"] = _digest(manifest_without_digest)
    return _canonical_json(manifest)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write(path: Path, payload: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "formal@example.invalid")
    _git(repository, "config", "user.name", "Formal Developer")
    _write(
        repository / "platform/backend/src/agent_platform/generic.py",
        "VALUE = 1\n",
    )
    _write(
        repository / "tests/test_generic.py",
        "def test_value():\n    assert 1 == 1\n",
    )
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "baseline")
    return repository


def _coordinator(
    tmp_path: Path,
    repository: Path,
    *,
    assignment_id: UUID,
    channel_id: UUID,
    content_guard=None,
) -> tuple[FormalSourceProvenanceCoordinator, object]:
    coordinator = FormalSourceProvenanceCoordinator(
        repository_root=repository,
        state_root=tmp_path / "source-provenance-state",
        content_guard=content_guard,
    )
    baseline = coordinator.freeze_baseline(
        task_id="EXP-LILIES-001",
        task_revision=1,
        run_id="formal-run:source-proof",
        assignment_id=assignment_id,
        channel_id=channel_id,
        captured_at=NOW,
    )
    return coordinator, baseline


def _binding(
    *,
    commit_sha: str,
    channel_id: UUID,
    sequence: int = 1,
    report_id: UUID | None = None,
    response_id: UUID | None = None,
) -> ApprovedDeveloperResponseBinding:
    return ApprovedDeveloperResponseBinding(
        channel_id=channel_id,
        report_id=report_id or uuid4(),
        approval_id=uuid4(),
        approval_message_id=uuid4(),
        approval_message_seq=sequence * 10,
        approval_authority="user",
        approval_payload_digest=DIGEST_A,
        approved_report_revision=4,
        response_id=response_id or uuid4(),
        response_message_id=uuid4(),
        response_message_seq=sequence * 10 + 2,
        response_report_revision=5,
        response_payload_digest=DIGEST_B,
        commit_sha=commit_sha,
    )


def _commit(repository: Path, relative: str, payload: str | bytes, message: str) -> str:
    _write(repository / relative, payload)
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _approval_message(
    *,
    channel_id: UUID,
    report_id: UUID,
    sequence: int,
    sender_role: str = "user",
) -> CollaborationMessageEnvelope:
    actor_id = "owner-operator"
    reason = "approved generic platform repair"
    if sender_role == "platform":
        actor_id = "platform-auto-forward"
        reason = "user-confirmed task-local auto-forward"
    approval = ApprovalDecision(
        approval_id=uuid4(),
        channel_id=channel_id,
        report_id=report_id,
        expected_report_revision=3,
        resulting_report_revision=4,
        decision="approve",
        actor_id=actor_id,
        reason=reason,
        idempotency_key=f"approval-key-{sequence:04d}",
        created_at=NOW,
    )
    return CollaborationMessageEnvelope(
        message_id=uuid4(),
        channel_id=channel_id,
        seq=sequence,
        message_type="approval",
        sender_role=sender_role,
        sender_id=actor_id,
        correlation_id=report_id,
        idempotency_key=f"approval-envelope-{sequence:04d}",
        visibility="user_and_lilies",
        payload_schema="collaboration.approval.v1",
        payload=approval.model_dump(mode="json"),
        created_at=NOW,
    )


def _response_message(
    *,
    channel_id: UUID,
    report_id: UUID,
    commit_sha: str,
    sequence: int,
) -> CollaborationMessageEnvelope:
    response = DeveloperResponse(
        response_id=uuid4(),
        channel_id=channel_id,
        report_id=report_id,
        report_revision=5,
        outcome="implemented",
        commit_sha=commit_sha,
        generic_capability_changes=[
            "Added a reusable typed platform primitive and its regression.",
        ],
        new_contract_digest=DIGEST_B,
        tests_run=[
            {
                "test_id": "test:generic-platform-primitive",
                "command": "pytest -q tests/test_generic.py",
                "exit_code": 0,
                "summary": "passed",
                "evidence_ref": {
                    "evidence_id": f"gitblob:{commit_sha}:{'c' * 40}",
                    "kind": "test_run",
                    "digest": DIGEST_A,
                    "media_type": "text/plain",
                    "label": "focused generic regression",
                    "captured_at": NOW.isoformat(),
                },
            }
        ],
        reprobe_steps=[
            {
                "order": 1,
                "action": "refresh the public platform contract",
                "expected": "the generic primitive is visible",
            }
        ],
        created_at=NOW,
    )
    return CollaborationMessageEnvelope(
        message_id=uuid4(),
        channel_id=channel_id,
        seq=sequence,
        message_type="developer_response",
        sender_role="codex",
        sender_id="codex-worker",
        correlation_id=report_id,
        idempotency_key=f"response-envelope-{sequence:04d}",
        visibility="user_and_lilies",
        payload_schema="collaboration.developer_response.v1",
        payload=response.model_dump(mode="json"),
        created_at=NOW,
    )


def test_archives_exact_approved_commit_tree_blobs_and_binary_diff(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    assignment_id = uuid4()
    channel_id = uuid4()
    coordinator, baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    commit_sha = _commit(
        repository,
        "platform/backend/src/agent_platform/generic.py",
        b"VALUE = 2\nBINARY = b'\\x00\\xff'\n",
        "approved generic capability",
    )
    binding = _binding(commit_sha=commit_sha, channel_id=channel_id)

    recorded = coordinator.record_approved_response(
        assignment_id=assignment_id,
        binding=binding,
    )
    archive = coordinator.finalize_archive(
        assignment_id=assignment_id,
        expected_bindings=[binding],
        finalized_at=NOW,
    )
    verified = verify_source_provenance_archive(
        repository_root=repository,
        archive_files=archive.files,
        expected_assignment_id=assignment_id,
        expected_bindings=[binding],
        require_current_checkout=True,
    )
    offline = verify_source_provenance_archive_offline(
        archive_files=archive.files,
        expected_assignment_id=assignment_id,
        expected_bindings=[binding],
        expected_manifest_digest=archive.manifest.manifest_digest,
    )

    assert recorded.parent_commit_sha == baseline.source_state.head_commit_sha
    assert recorded.commit_sha == commit_sha
    assert recorded.tree_sha == _git(repository, "rev-parse", f"{commit_sha}^{{tree}}")
    assert recorded.changed_paths == [
        "platform/backend/src/agent_platform/generic.py"
    ]
    assert {blob.oid for blob in recorded.blob_objects} == {
        recorded.changes[0].old_blob_sha,
        recorded.changes[0].new_blob_sha,
    }
    assert archive.files[recorded.commit_object.archive_path]
    assert archive.files[recorded.binary_diff.archive_path]
    assert SOURCE_PROVENANCE_MANIFEST_PATH in archive.files
    assert verified.manifest_digest == archive.manifest.manifest_digest
    assert offline.manifest_digest == archive.manifest.manifest_digest
    assert archive.manifest.baseline_commit_object.archive_path in archive.files
    assert archive.manifest.tree_objects
    assert all(tree.archive_path in archive.files for tree in archive.manifest.tree_objects)
    assert all(
        ".git" not in path
        and "platform/backend/data" not in path
        and "protected" not in path
        for path in archive.files
    )


def test_binding_derivation_requires_active_user_or_task_auto_forward_approval(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    commit_sha = _commit(
        repository,
        "platform/backend/src/agent_platform/generic.py",
        "VALUE = 2\n",
        "approved change",
    )
    channel_id = uuid4()
    report_id = uuid4()
    manual = approved_developer_response_bindings(
        [
            _approval_message(
                channel_id=channel_id,
                report_id=report_id,
                sequence=10,
            ),
            _response_message(
                channel_id=channel_id,
                report_id=report_id,
                commit_sha=commit_sha,
                sequence=12,
            ),
        ],
        channel_id=channel_id,
    )
    automatic_report = uuid4()
    automatic = approved_developer_response_bindings(
        [
            _approval_message(
                channel_id=channel_id,
                report_id=automatic_report,
                sequence=20,
                sender_role="platform",
            ),
            _response_message(
                channel_id=channel_id,
                report_id=automatic_report,
                commit_sha=commit_sha,
                sequence=22,
            ),
        ],
        channel_id=channel_id,
    )

    assert manual[0].approval_authority == "user"
    assert automatic[0].approval_authority == "task_auto_forward"
    with pytest.raises(
        FormalSourceProvenanceConflict,
        match="no active user approval",
    ):
        approved_developer_response_bindings(
            [
                _response_message(
                    channel_id=channel_id,
                    report_id=uuid4(),
                    commit_sha=commit_sha,
                    sequence=30,
                )
            ],
            channel_id=channel_id,
        )


def test_dirty_or_untracked_source_is_rejected_at_start_and_response(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _write(repository / "untracked.txt", "not declared\n")
    state = capture_git_source_state(repository)
    assert state.clean is False
    assert state.untracked_file_count == 1
    with pytest.raises(
        FormalSourceProvenanceConflict,
        match="dirty or untracked",
    ):
        _coordinator(
            tmp_path,
            repository,
            assignment_id=uuid4(),
            channel_id=uuid4(),
        )

    (repository / "untracked.txt").unlink()
    assignment_id = uuid4()
    channel_id = uuid4()
    coordinator, _baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    commit_sha = _commit(
        repository,
        "platform/backend/src/agent_platform/generic.py",
        "VALUE = 2\n",
        "approved change",
    )
    _write(repository / "later-untracked.txt", "drift\n")
    with pytest.raises(
        FormalSourceProvenanceConflict,
        match="clean developer source HEAD",
    ):
        coordinator.record_approved_response(
            assignment_id=assignment_id,
            binding=_binding(commit_sha=commit_sha, channel_id=channel_id),
        )


def test_undeclared_commit_and_final_checkout_drift_are_detected(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    assignment_id = uuid4()
    channel_id = uuid4()
    coordinator, _baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    approved_commit = _commit(
        repository,
        "platform/backend/src/agent_platform/generic.py",
        "VALUE = 2\n",
        "approved change",
    )
    binding = _binding(commit_sha=approved_commit, channel_id=channel_id)
    coordinator.record_approved_response(
        assignment_id=assignment_id,
        binding=binding,
    )
    _commit(
        repository,
        "platform/backend/src/agent_platform/unreported.py",
        "UNREPORTED = True\n",
        "unreported change",
    )
    with pytest.raises(
        FormalSourceProvenanceConflict,
        match="undeclared final drift",
    ):
        coordinator.finalize_archive(
            assignment_id=assignment_id,
            expected_bindings=[binding],
            finalized_at=NOW,
        )

    _git(repository, "reset", "--hard", approved_commit)
    archive = coordinator.finalize_archive(
        assignment_id=assignment_id,
        expected_bindings=[binding],
        finalized_at=NOW,
    )
    _write(
        repository / "platform/backend/src/agent_platform/generic.py",
        "VALUE = 999\n",
    )
    with pytest.raises(
        FormalSourceProvenanceConflict,
        match="current developer source differs",
    ):
        verify_source_provenance_archive(
            repository_root=repository,
            archive_files=archive.files,
            expected_assignment_id=assignment_id,
            expected_bindings=[binding],
            require_current_checkout=True,
        )
    assert (
        verify_source_provenance_archive(
            repository_root=repository,
            archive_files=archive.files,
            expected_assignment_id=assignment_id,
            expected_bindings=[binding],
            require_current_checkout=False,
        ).manifest_digest
        == archive.manifest.manifest_digest
    )


def test_skipped_commit_is_rejected_even_when_declared_commit_is_head(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    assignment_id = uuid4()
    channel_id = uuid4()
    coordinator, _baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    _commit(
        repository,
        "platform/backend/src/agent_platform/hidden_change.py",
        "HIDDEN = True\n",
        "undeclared intermediate",
    )
    declared = _commit(
        repository,
        "platform/backend/src/agent_platform/generic.py",
        "VALUE = 3\n",
        "declared response",
    )

    with pytest.raises(
        FormalSourceProvenanceConflict,
        match="undeclared or merge commit",
    ):
        coordinator.record_approved_response(
            assignment_id=assignment_id,
            binding=_binding(commit_sha=declared, channel_id=channel_id),
        )


@pytest.mark.parametrize(
    "relative",
    [
        "data/platform.db",
        "platform/backend/data/workflows.db",
        "docs/experiments/lilies/protected/oracle/answers.json",
    ],
)
def test_runtime_data_and_oracle_paths_never_enter_source_archive(
    tmp_path: Path,
    relative: str,
) -> None:
    repository = _repository(tmp_path)
    assignment_id = uuid4()
    channel_id = uuid4()
    coordinator, _baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    commit_sha = _commit(repository, relative, "sensitive\n", "forbidden content")

    with pytest.raises(
        FormalSourceProvenanceSecurityError,
        match="reserved or unsafe path",
    ) as rejected:
        coordinator.record_approved_response(
            assignment_id=assignment_id,
            binding=_binding(commit_sha=commit_sha, channel_id=channel_id),
        )
    assert relative not in str(rejected.value)


def test_content_guard_blocks_protected_bytes_before_any_record_is_archived(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    assignment_id = uuid4()
    channel_id = uuid4()

    def guard(_label: str, payload: bytes) -> bool:
        return b"PROTECTED-ANSWER-42" not in payload

    coordinator, _baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
        content_guard=guard,
    )
    commit_sha = _commit(
        repository,
        "platform/backend/src/agent_platform/generic.py",
        "VALUE = 'PROTECTED-ANSWER-42'\n",
        "copied protected answer",
    )
    with pytest.raises(
        FormalSourceProvenanceSecurityError,
        match="content guard rejected",
    ):
        coordinator.record_approved_response(
            assignment_id=assignment_id,
            binding=_binding(commit_sha=commit_sha, channel_id=channel_id),
        )


def test_archive_rejects_binding_and_payload_tampering(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    assignment_id = uuid4()
    channel_id = uuid4()
    coordinator, _baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    commit_sha = _commit(
        repository,
        "platform/backend/src/agent_platform/generic.py",
        "VALUE = 2\n",
        "approved change",
    )
    binding = _binding(commit_sha=commit_sha, channel_id=channel_id)
    coordinator.record_approved_response(
        assignment_id=assignment_id,
        binding=binding,
    )
    changed_binding = binding.model_copy(update={"report_id": uuid4()})
    with pytest.raises(
        FormalSourceProvenanceConflict,
        match="exactly match approved",
    ):
        coordinator.finalize_archive(
            assignment_id=assignment_id,
            expected_bindings=[changed_binding],
            finalized_at=NOW,
        )
    archive = coordinator.finalize_archive(
        assignment_id=assignment_id,
        expected_bindings=[binding],
        finalized_at=NOW,
    )
    tampered = dict(archive.files)
    patch_path = archive.manifest.approved_commits[0].binary_diff.archive_path
    tampered[patch_path] += b"\nforged\n"
    with pytest.raises(
        FormalSourceProvenanceSecurityError,
        match="differs",
    ):
        verify_source_provenance_archive(
            repository_root=repository,
            archive_files=tampered,
            expected_assignment_id=assignment_id,
            expected_bindings=[binding],
            require_current_checkout=True,
        )


def test_offline_verifier_rejects_missing_and_forged_tree_objects(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    assignment_id = uuid4()
    channel_id = uuid4()
    coordinator, _baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    commit_sha = _commit(
        repository,
        "platform/backend/src/agent_platform/generic.py",
        "VALUE = 2\n",
        "approved tree change",
    )
    binding = _binding(commit_sha=commit_sha, channel_id=channel_id)
    coordinator.record_approved_response(
        assignment_id=assignment_id,
        binding=binding,
    )
    archive = coordinator.finalize_archive(
        assignment_id=assignment_id,
        expected_bindings=[binding],
        finalized_at=NOW,
    )
    root_oids = {
        archive.manifest.baseline.source_state.head_tree_sha,
        archive.manifest.approved_commits[0].tree_sha,
    }
    non_root = next(
        tree for tree in archive.manifest.tree_objects if tree.oid not in root_oids
    )

    missing_manifest = json.loads(
        archive.files[SOURCE_PROVENANCE_MANIFEST_PATH]
    )
    missing_manifest["tree_objects"] = [
        tree
        for tree in missing_manifest["tree_objects"]
        if tree["oid"] != non_root.oid
    ]
    missing = dict(archive.files)
    missing.pop(non_root.archive_path)
    missing[SOURCE_PROVENANCE_MANIFEST_PATH] = _refresh_manifest_digests(
        missing_manifest
    )
    with pytest.raises(
        FormalSourceProvenanceSecurityError,
        match="omits a reachable Git tree",
    ):
        verify_source_provenance_archive_offline(
            archive_files=missing,
            expected_assignment_id=assignment_id,
            expected_bindings=[binding],
        )

    forged = dict(archive.files)
    forged[non_root.archive_path] = b"forged-tree"
    with pytest.raises(
        FormalSourceProvenanceSecurityError,
        match="differs from its descriptor",
    ):
        verify_source_provenance_archive_offline(
            archive_files=forged,
            expected_assignment_id=assignment_id,
            expected_bindings=[binding],
        )


def test_offline_verifier_rejects_extra_tree_and_forged_changed_endpoint(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    assignment_id = uuid4()
    channel_id = uuid4()
    coordinator, _baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    commit_sha = _commit(
        repository,
        "platform/backend/src/agent_platform/generic.py",
        "VALUE = 2\n",
        "approved endpoint change",
    )
    binding = _binding(commit_sha=commit_sha, channel_id=channel_id)
    coordinator.record_approved_response(
        assignment_id=assignment_id,
        binding=binding,
    )
    archive = coordinator.finalize_archive(
        assignment_id=assignment_id,
        expected_bindings=[binding],
        finalized_at=NOW,
    )

    extra_manifest = json.loads(
        archive.files[SOURCE_PROVENANCE_MANIFEST_PATH]
    )
    object_format = archive.manifest.baseline.source_state.object_format
    empty_tree_oid = hashlib.new(
        object_format,
        b"tree 0\0",
    ).hexdigest()
    empty_tree_path = f"source-provenance/trees/{empty_tree_oid}.tree"
    extra_manifest["tree_objects"].append(
        {
            "schema_version": "1.0",
            "object_type": "tree",
            "oid": empty_tree_oid,
            "archive_path": empty_tree_path,
            "payload_digest": _digest(b""),
            "size_bytes": 0,
        }
    )
    extra = dict(archive.files)
    extra[empty_tree_path] = b""
    extra[SOURCE_PROVENANCE_MANIFEST_PATH] = _refresh_manifest_digests(
        extra_manifest
    )
    with pytest.raises(
        FormalSourceProvenanceSecurityError,
        match="unreachable Git tree",
    ):
        verify_source_provenance_archive_offline(
            archive_files=extra,
            expected_assignment_id=assignment_id,
            expected_bindings=[binding],
        )

    endpoint_manifest = json.loads(
        archive.files[SOURCE_PROVENANCE_MANIFEST_PATH]
    )
    change = endpoint_manifest["approved_commits"][0]["changes"][0]
    change["old_blob_sha"], change["new_blob_sha"] = (
        change["new_blob_sha"],
        change["old_blob_sha"],
    )
    endpoint = dict(archive.files)
    endpoint[SOURCE_PROVENANCE_MANIFEST_PATH] = _refresh_manifest_digests(
        endpoint_manifest
    )
    with pytest.raises(
        FormalSourceProvenanceSecurityError,
        match="blob endpoints differ from its trees",
    ):
        verify_source_provenance_archive_offline(
            archive_files=endpoint,
            expected_assignment_id=assignment_id,
            expected_bindings=[binding],
        )

    undeclared = dict(archive.files)
    undeclared["source-provenance/trees/undeclared.tree"] = b""
    with pytest.raises(
        FormalSourceProvenanceSecurityError,
        match="missing or undeclared payloads",
    ):
        verify_source_provenance_archive_offline(
            archive_files=undeclared,
            expected_assignment_id=assignment_id,
            expected_bindings=[binding],
        )


def test_binary_file_commit_is_replayable_with_real_blob_objects(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    assignment_id = uuid4()
    channel_id = uuid4()
    coordinator, _baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    commit_sha = _commit(
        repository,
        "platform/frontend/public/generic-fixture.bin",
        bytes(range(256)) * 32,
        "approved binary fixture",
    )
    binding = _binding(commit_sha=commit_sha, channel_id=channel_id)
    record = coordinator.record_approved_response(
        assignment_id=assignment_id,
        binding=binding,
    )
    archive = coordinator.finalize_archive(
        assignment_id=assignment_id,
        expected_bindings=[binding],
        finalized_at=NOW,
    )

    assert b"GIT binary patch" in archive.files[record.binary_diff.archive_path]
    assert len(record.blob_objects) == 1
    assert (
        archive.files[record.blob_objects[0].archive_path]
        == bytes(range(256)) * 32
    )
    verify_source_provenance_archive(
        repository_root=repository,
        archive_files=archive.files,
        expected_assignment_id=assignment_id,
        expected_bindings=[binding],
        require_current_checkout=True,
    )


def test_baseline_and_response_replay_are_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    assignment_id = UUID("00000000-0000-0000-0000-000000000111")
    channel_id = UUID("00000000-0000-0000-0000-000000000222")
    coordinator, baseline = _coordinator(
        tmp_path,
        repository,
        assignment_id=assignment_id,
        channel_id=channel_id,
    )
    assert (
        coordinator.freeze_baseline(
            task_id="EXP-LILIES-001",
            task_revision=1,
            run_id="formal-run:source-proof",
            assignment_id=assignment_id,
            channel_id=channel_id,
            captured_at=NOW,
        )
        == baseline
    )
    commit_sha = _commit(
        repository,
        "platform/backend/src/agent_platform/generic.py",
        "VALUE = 2\n",
        "approved change",
    )
    binding = _binding(commit_sha=commit_sha, channel_id=channel_id)
    first = coordinator.record_approved_response(
        assignment_id=assignment_id,
        binding=binding,
    )
    second = coordinator.record_approved_response(
        assignment_id=assignment_id,
        binding=binding,
    )
    assert first == second
