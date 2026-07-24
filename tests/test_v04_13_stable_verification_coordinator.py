from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from agent_platform.independent_verifier import verify_frozen_claim
from agent_platform.stable_verification import StableVerificationRejected
from agent_platform.stable_verification_cli import main as stability_main
from agent_platform.stable_verification_coordinator import (
    StableQualificationBundle,
    StableVerificationCoordinator,
)
from tests.test_v04_13_independent_verification import (
    REVISION,
    TASK_ID,
)
from tests.test_v04_13_stable_verification import (
    _additional_case,
    _base_stable_case,
)


def _install_fake_isolated_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, int, str]]:
    calls: list[tuple[str, int, str]] = []

    def resolve(
        *,
        state_root: Path,
        task_id: str,
        revision: int,
        claim: Any,
        broker_root: Path,
        timeout_seconds: float,
    ) -> Any:
        assert broker_root.is_absolute()
        assert timeout_seconds > 0
        calls.append((task_id, revision, str(claim.claim_id)))
        return verify_frozen_claim(
            state_root=state_root,
            task_id=task_id,
            revision=revision,
            claim=claim,
        )

    monkeypatch.setattr(
        "agent_platform.stable_verification_coordinator."
        "run_independent_verifier_subprocess",
        resolve,
    )
    return calls


def _coordinator(
    *,
    state_root: Path,
    broker_root: Path,
) -> StableVerificationCoordinator:
    return StableVerificationCoordinator(
        state_root=state_root,
        broker_root=broker_root,
        platform_seed_key_resolver=lambda context: (
            context.environment_instance_id.encode("utf-8").ljust(32, b"!")
        ),
    )


def test_coordinator_persists_trusted_attempts_and_exports_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_isolated_broker(monkeypatch)
    base, first = _base_stable_case(tmp_path)
    second = _additional_case(base, tmp_path, ordinal=2)
    coordinator = _coordinator(
        state_root=base.state_root,
        broker_root=tmp_path / "broker",
    )

    first_progress = coordinator.verify_and_record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
    )
    second_progress = coordinator.verify_and_record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=second.claim,
    )

    assert first_progress.stable_verdict is None
    assert second_progress.stable_verdict is not None
    assert len(calls) == 4
    reviewer = StableVerificationCoordinator(state_root=base.state_root)
    replay = reviewer.replay(task_id=TASK_ID, revision=REVISION)
    first_bundle = reviewer.export_qualification_bundle(
        task_id=TASK_ID,
        revision=REVISION,
    )
    second_bundle = reviewer.export_qualification_bundle(
        task_id=TASK_ID,
        revision=REVISION,
    )

    assert replay == second_progress.stable_verdict
    assert first_bundle == second_bundle
    assert first_bundle.bundle_digest == second_bundle.bundle_digest
    assert first_bundle.current_stable_verdict == replay
    assert len(first_bundle.prepared_attempts) == 2
    assert len(first_bundle.hidden_seed_evidence) == 2
    assert len(first_bundle.outcomes) == 2
    assert len(first_bundle.archive_manifest_digests) == 2
    assert {
        item.claim.claim_id for item in first_bundle.prepared_attempts
    } == {first.claim.claim_id, second.claim.claim_id}
    assert {
        item.trusted_result.verification_id
        for item in first_bundle.prepared_attempts
    } == {first.result.verification_id, second.result.verification_id}
    assert {
        item.evidence_digest for item in first_bundle.hidden_seed_evidence
    } == {
        item.hidden_seed_evidence_digest
        for item in first_bundle.prepared_attempts
    }
    attempts_root = coordinator.manager._root(
        TASK_ID,
        REVISION,
    ) / "attempts"
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o400
        for path in attempts_root.rglob("*.json")
    )


def test_coordinator_exact_retry_after_failure_uses_persisted_attempt_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_isolated_broker(monkeypatch)
    base, first = _base_stable_case(tmp_path)
    failed = _additional_case(
        base,
        tmp_path,
        ordinal=2,
        actual_status="needs-review",
    )
    coordinator = _coordinator(
        state_root=base.state_root,
        broker_root=tmp_path / "broker",
    )
    coordinator.verify_and_record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
    )
    failed_progress = coordinator.verify_and_record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=failed.claim,
    )
    replayed = coordinator.verify_and_record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
    )

    assert failed_progress.consecutive_passes == 0
    assert replayed.consecutive_passes == 0
    assert replayed.stable_verdict is None
    bundle = coordinator.export_qualification_bundle(
        task_id=TASK_ID,
        revision=REVISION,
    )
    prepared_by_id = {
        item.attempt_id: item.attempt_digest
        for item in bundle.prepared_attempts
    }
    assert len(bundle.prepared_attempts) == 2
    assert len(bundle.outcomes) == 3
    assert all(
        prepared_by_id[outcome.attempt_id] == outcome.attempt_digest
        for outcome in bundle.outcomes
    )


@pytest.mark.parametrize("alias_kind", ["attempt", "outcome"])
def test_qualification_export_rejects_noncanonical_attempt_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias_kind: str,
) -> None:
    _install_fake_isolated_broker(monkeypatch)
    base, first = _base_stable_case(tmp_path)
    coordinator = _coordinator(
        state_root=base.state_root,
        broker_root=tmp_path / "broker",
    )
    coordinator.verify_and_record(
        task_id=TASK_ID,
        revision=REVISION,
        claim=first.claim,
    )
    attempts_root = coordinator.manager._root(
        TASK_ID,
        REVISION,
    ) / "attempts"
    attempt = next(attempts_root.iterdir())
    if alias_kind == "attempt":
        shutil.copytree(attempt, attempts_root / ("0" * 64))
    else:
        outcome = next((attempt / "outcomes").iterdir())
        shutil.copy2(outcome, outcome.with_name(("0" * 64) + ".json"))

    with pytest.raises(StableVerificationRejected, match="canonical"):
        coordinator.export_qualification_bundle(
            task_id=TASK_ID,
            revision=REVISION,
        )


def test_admin_cli_redacts_seed_secret_and_writes_read_only_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    _install_fake_isolated_broker(monkeypatch)
    base, first = _base_stable_case(tmp_path)
    claim_file = tmp_path / "claim.json"
    claim_file.write_bytes(
        json.dumps(
            first.claim.model_dump(mode="json", exclude_none=True),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    os.chmod(claim_file, 0o400)
    secret = b"platform-seed-identity-key-do-not-disclose"
    seed_key_file = tmp_path / "seed-key"
    seed_key_file.write_bytes(secret)
    os.chmod(seed_key_file, 0o600)
    arguments = [
        "record",
        "--state-root",
        str(base.state_root),
        "--broker-root",
        str(tmp_path / "broker"),
        "--task-id",
        TASK_ID,
        "--revision",
        str(REVISION),
        "--claim-file",
        str(claim_file),
        "--seed-identity-key-file",
        str(seed_key_file),
    ]

    assert stability_main(arguments) == 2
    rejected = capsysbinary.readouterr()
    assert secret not in rejected.out + rejected.err
    assert str(seed_key_file).encode() not in rejected.err

    os.chmod(seed_key_file, 0o400)
    assert stability_main(arguments) == 0
    recorded = capsysbinary.readouterr()
    assert secret not in recorded.out + recorded.err
    progress = json.loads(recorded.out)
    assert progress["consecutive_passes"] == 1

    bundle_out = tmp_path / "qualification-bundle.json"
    assert (
        stability_main(
            [
                "export",
                "--state-root",
                str(base.state_root),
                "--task-id",
                TASK_ID,
                "--revision",
                str(REVISION),
                "--output",
                str(bundle_out),
            ]
        )
        == 0
    )
    exported = capsysbinary.readouterr()
    bundle = StableQualificationBundle.model_validate_json(
        bundle_out.read_bytes()
    )
    assert exported.err == b""
    assert stat.S_IMODE(bundle_out.stat().st_mode) == 0o400
    assert secret not in bundle_out.read_bytes()
    assert bundle.prepared_attempts[0].claim.claim_id == first.claim.claim_id
