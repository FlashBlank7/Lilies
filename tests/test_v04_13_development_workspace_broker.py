from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from agent_platform.collaborative_development_models import (
    AgentRole,
    CommandReceipt,
    DevelopmentResult,
    SideEffect,
    TestReceipt as DevelopmentTestReceipt,
    utc_now,
)
from agent_platform.development_workspace_broker import (
    DevelopmentReviewSnapshotReceipt,
    DevelopmentWorkspaceBroker,
    DevelopmentWorkspaceError,
    DevelopmentWorkspaceSpec,
)


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "config", "user.name", "Fixture")
    (repository / "calculator.py").write_text(
        "def add(left, right):\n    return left - right\n",
        encoding="utf-8",
    )
    _git(repository, "add", "calculator.py")
    _git(repository, "commit", "-m", "fixture")
    return repository, _git(repository, "rev-parse", "HEAD")


def _spec(role: AgentRole) -> DevelopmentWorkspaceSpec:
    return DevelopmentWorkspaceSpec(
        agent_role=role,
        allowed_paths=("calculator.py",),
        allowed_argv=(("pytest", "-q"),),
        allowed_side_effects=(
            SideEffect.workspace_write,
            SideEffect.process_execute,
        ),
    )


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _result(
    *,
    assignment_id,
    baseline: str,
    diff_digest: str,
) -> DevelopmentResult:
    created_at = utc_now()
    output_digest = _digest(b"test passed")
    command_digest = _digest(b"python\x00tests/check.py")
    return DevelopmentResult(
        result_id=uuid4(),
        assignment_id=assignment_id,
        work_item_id=uuid4(),
        lease_id=uuid4(),
        agent_role=AgentRole.codex,
        baseline_commit=baseline,
        diff_digest=diff_digest,
        commands=(
            CommandReceipt(
                argv=("python", "tests/check.py"),
                cwd="tests",
                exit_code=0,
                output_digest=output_digest,
                started_at=created_at,
                finished_at=created_at,
            ),
        ),
        tests=(
            DevelopmentTestReceipt(
                name="fixture",
                command_digest=command_digest,
                exit_code=0,
                passed=True,
                output_digest=output_digest,
            ),
        ),
        evidence_refs=(output_digest,),
        reproduction_steps=("Run the granted fixture check.",),
        created_at=created_at,
    )


def test_broker_creates_independent_idempotent_role_workspaces(tmp_path: Path) -> None:
    source, baseline = _fixture_repo(tmp_path)
    broker = DevelopmentWorkspaceBroker(tmp_path / "broker")
    assignment_id = uuid4()

    prepared = broker.prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(_spec(AgentRole.lilies), _spec(AgentRole.codex)),
    )
    replay = broker.prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(_spec(AgentRole.lilies), _spec(AgentRole.codex)),
    )

    assert prepared.manifest_digest == replay.manifest_digest
    assert prepared.baseline_commit == baseline
    roots = {grant.agent_role: Path(grant.workspace_root) for grant in prepared.grants}
    assert roots[AgentRole.lilies] != roots[AgentRole.codex]
    assert (roots[AgentRole.lilies] / ".git").exists()
    assert (roots[AgentRole.codex] / ".git").exists()
    (roots[AgentRole.codex] / "calculator.py").write_text(
        "changed\n",
        encoding="utf-8",
    )
    assert (
        (roots[AgentRole.lilies] / "calculator.py")
        .read_text(encoding="utf-8")
        .startswith("def add")
    )


def test_broker_grant_revision_is_atomic_cas_and_reload_idempotent(
    tmp_path: Path,
) -> None:
    source, baseline = _fixture_repo(tmp_path)
    state_root = tmp_path / "broker"
    broker = DevelopmentWorkspaceBroker(state_root)
    assignment_id = uuid4()
    prepared = broker.prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(_spec(AgentRole.lilies), _spec(AgentRole.codex)),
    )
    codex_grant = next(
        grant
        for grant in prepared.grants
        if grant.agent_role == AgentRole.codex
    )
    replacement = codex_grant.model_copy(
        update={
            "grant_revision": codex_grant.grant_revision + 1,
            "allowed_paths": ("calculator.py", "approved.py"),
        }
    )

    revised = broker.revise_prepared_grant(
        prepared=prepared,
        expected_manifest_digest=prepared.manifest_digest,
        replacement_grant=replacement,
    )
    assert revised.manifest_digest != prepared.manifest_digest
    assert broker.load_prepared(assignment_id) == revised

    stale_replay = broker.revise_prepared_grant(
        prepared=prepared,
        expected_manifest_digest=prepared.manifest_digest,
        replacement_grant=replacement,
    )
    assert stale_replay == revised

    restarted = DevelopmentWorkspaceBroker(state_root)
    reloaded = restarted.load_prepared(assignment_id)
    reload_replay = restarted.revise_prepared_grant(
        prepared=reloaded,
        expected_manifest_digest=reloaded.manifest_digest,
        replacement_grant=replacement,
    )
    assert reload_replay == revised

    competing = replacement.model_copy(
        update={"allowed_paths": ("calculator.py", "competing.py")}
    )
    with pytest.raises(
        DevelopmentWorkspaceError,
        match="advance exactly once|compare-and-set",
    ):
        restarted.revise_prepared_grant(
            prepared=prepared,
            expected_manifest_digest=prepared.manifest_digest,
            replacement_grant=competing,
        )


def test_broker_rejects_conflicting_replay_and_unsafe_roots(tmp_path: Path) -> None:
    source, baseline = _fixture_repo(tmp_path)
    with pytest.raises(ValueError):
        DevelopmentWorkspaceBroker(Path("/"))

    broker = DevelopmentWorkspaceBroker(tmp_path / "broker")
    assignment_id = uuid4()
    broker.prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(_spec(AgentRole.lilies), _spec(AgentRole.codex)),
    )
    conflicting = DevelopmentWorkspaceSpec(
        agent_role=AgentRole.codex,
        allowed_paths=("other.py",),
        allowed_argv=(("pytest", "-q"),),
        allowed_side_effects=(
            SideEffect.workspace_write,
            SideEffect.process_execute,
        ),
    )
    with pytest.raises(DevelopmentWorkspaceError):
        broker.prepare(
            source_repository=source,
            assignment_id=assignment_id,
            baseline_revision=baseline,
            specs=(_spec(AgentRole.lilies), conflicting),
        )


def test_result_materializes_as_fresh_digest_bound_lilies_review_snapshots(
    tmp_path: Path,
) -> None:
    source, baseline = _fixture_repo(tmp_path)
    broker = DevelopmentWorkspaceBroker(tmp_path / "broker")
    assignment_id = uuid4()
    prepared = broker.prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(_spec(AgentRole.lilies), _spec(AgentRole.codex)),
    )
    roots = {grant.agent_role: Path(grant.workspace_root) for grant in prepared.grants}
    codex_workspace = roots[AgentRole.codex]
    lilies_workspace = roots[AgentRole.lilies]
    source_before = (source / "calculator.py").read_bytes()
    source_status_before = _git(source, "status", "--porcelain")

    (codex_workspace / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    first_diff_digest = broker.calculate_diff_digest(
        workspace_root=codex_workspace,
        baseline_commit=baseline,
    )
    first_result = _result(
        assignment_id=assignment_id,
        baseline=baseline,
        diff_digest=first_diff_digest,
    )
    first_receipt = broker.materialize_review_snapshot(
        prepared=prepared,
        result=first_result,
    )
    first_snapshot = Path(first_receipt.review_workspace_root)

    assert first_receipt.promotion_state == "review_snapshot_only"
    assert first_receipt.source_repository_unchanged is True
    assert first_receipt.changed_paths == ("calculator.py",)
    assert first_receipt.diff_digest == first_result.diff_digest
    assert first_snapshot not in source.parents
    assert (
        (first_snapshot / "calculator.py")
        .read_text(encoding="utf-8")
        .endswith("return left + right\n")
    )
    assert (
        (lilies_workspace / "calculator.py")
        .read_text(encoding="utf-8")
        .endswith("return left - right\n")
    )
    assert (source / "calculator.py").read_bytes() == source_before
    assert _git(source, "status", "--porcelain") == source_status_before
    assert (
        DevelopmentReviewSnapshotReceipt.model_validate_json(
            (
                tmp_path
                / "broker"
                / str(assignment_id)
                / "review-receipts"
                / f"{first_result.result_id}.json"
            ).read_bytes()
        )
        == first_receipt
    )
    tampered_receipt = first_receipt.model_dump(mode="json")
    tampered_receipt["diff_digest"] = "sha256:" + ("f" * 64)
    with pytest.raises(ValueError, match="receipt digest"):
        DevelopmentReviewSnapshotReceipt.model_validate(tampered_receipt)
    assert (
        broker.materialize_review_snapshot(
            prepared=prepared,
            result=first_result,
        )
        == first_receipt
    )

    (codex_workspace / "calculator.py").write_text(
        "def add(left, right):\n    return left * right\n",
        encoding="utf-8",
    )
    second_result = _result(
        assignment_id=assignment_id,
        baseline=baseline,
        diff_digest=broker.calculate_diff_digest(
            workspace_root=codex_workspace,
            baseline_commit=baseline,
        ),
    )
    second_receipt = broker.materialize_review_snapshot(
        prepared=prepared,
        result=second_result,
    )
    second_snapshot = Path(second_receipt.review_workspace_root)

    assert second_snapshot != first_snapshot
    assert second_receipt.review_snapshot_id != first_receipt.review_snapshot_id
    assert (
        (second_snapshot / "calculator.py")
        .read_text(encoding="utf-8")
        .endswith("return left * right\n")
    )
    assert (
        (first_snapshot / "calculator.py")
        .read_text(encoding="utf-8")
        .endswith("return left + right\n")
    )
    assert (source / "calculator.py").read_bytes() == source_before


def test_review_materialization_rejects_baseline_diff_and_shared_scope_drift(
    tmp_path: Path,
) -> None:
    source, baseline = _fixture_repo(tmp_path)
    broker = DevelopmentWorkspaceBroker(tmp_path / "broker")
    assignment_id = uuid4()
    codex_spec = DevelopmentWorkspaceSpec(
        agent_role=AgentRole.codex,
        allowed_paths=("calculator.py", "codex-only.py"),
        allowed_argv=(("pytest", "-q"),),
        allowed_side_effects=(
            SideEffect.workspace_write,
            SideEffect.process_execute,
        ),
    )
    prepared = broker.prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(_spec(AgentRole.lilies), codex_spec),
    )
    codex_workspace = Path(
        next(
            grant.workspace_root for grant in prepared.grants if grant.agent_role == AgentRole.codex
        )
    )
    (codex_workspace / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    valid_digest = broker.calculate_diff_digest(
        workspace_root=codex_workspace,
        baseline_commit=baseline,
    )

    with pytest.raises(DevelopmentWorkspaceError, match="frozen baseline"):
        broker.materialize_review_snapshot(
            prepared=prepared,
            result=_result(
                assignment_id=assignment_id,
                baseline="f" * 40,
                diff_digest=valid_digest,
            ),
        )
    with pytest.raises(DevelopmentWorkspaceError, match="diff digest"):
        broker.materialize_review_snapshot(
            prepared=prepared,
            result=_result(
                assignment_id=assignment_id,
                baseline=baseline,
                diff_digest="sha256:" + ("f" * 64),
            ),
        )

    (codex_workspace / "codex-only.py").write_text("outside review scope\n")
    shared_scope_result = _result(
        assignment_id=assignment_id,
        baseline=baseline,
        diff_digest=broker.calculate_diff_digest(
            workspace_root=codex_workspace,
            baseline_commit=baseline,
        ),
    )
    with pytest.raises(DevelopmentWorkspaceError, match="Lilies review grant"):
        broker.materialize_review_snapshot(
            prepared=prepared,
            result=shared_scope_result,
        )


@pytest.mark.parametrize("escape_kind", ["symlink", "hardlink"])
def test_review_materialization_rejects_link_escape(
    tmp_path: Path,
    escape_kind: str,
) -> None:
    source, baseline = _fixture_repo(tmp_path)
    broker = DevelopmentWorkspaceBroker(tmp_path / "broker")
    assignment_id = uuid4()
    prepared = broker.prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(_spec(AgentRole.lilies), _spec(AgentRole.codex)),
    )
    codex_workspace = Path(
        next(
            grant.workspace_root for grant in prepared.grants if grant.agent_role == AgentRole.codex
        )
    )
    target = codex_workspace / "calculator.py"
    target.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    if escape_kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)

    result = _result(
        assignment_id=assignment_id,
        baseline=baseline,
        diff_digest="sha256:" + ("a" * 64),
    )
    with pytest.raises(DevelopmentWorkspaceError, match="symlink|single-link"):
        broker.materialize_review_snapshot(
            prepared=prepared,
            result=result,
        )
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_review_snapshot_replay_rejects_materialized_content_tampering(
    tmp_path: Path,
) -> None:
    source, baseline = _fixture_repo(tmp_path)
    broker = DevelopmentWorkspaceBroker(tmp_path / "broker")
    assignment_id = uuid4()
    prepared = broker.prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(_spec(AgentRole.lilies), _spec(AgentRole.codex)),
    )
    codex_workspace = Path(
        next(
            grant.workspace_root for grant in prepared.grants if grant.agent_role == AgentRole.codex
        )
    )
    (codex_workspace / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    result = _result(
        assignment_id=assignment_id,
        baseline=baseline,
        diff_digest=broker.calculate_diff_digest(
            workspace_root=codex_workspace,
            baseline_commit=baseline,
        ),
    )
    receipt = broker.materialize_review_snapshot(
        prepared=prepared,
        result=result,
    )
    (Path(receipt.review_workspace_root) / "calculator.py").write_text(
        "tampered\n",
        encoding="utf-8",
    )

    with pytest.raises(DevelopmentWorkspaceError, match="snapshot content"):
        broker.materialize_review_snapshot(
            prepared=prepared,
            result=result,
        )
