from __future__ import annotations

import os
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_platform.collaborative_development_auth import DevelopmentPrincipal
from agent_platform.collaborative_development_models import (
    AcceptanceCheck,
    AgentRole,
    AgentRoleGrant,
    ApprovalMode,
    CommandReceipt,
    DevelopmentAssignment,
    DevelopmentAssignmentProjection,
    DevelopmentBudget,
    DevelopmentResult,
    DevelopmentTaskRole,
    DevelopmentWorkItem,
    ExecutionMode,
    LiliesReview,
    ReviewVerdict,
    SideEffect,
    TestReceipt as DevelopmentTestReceipt,
    WorkItemKind,
    WorkItemStatus,
    WorkspaceGrant,
    utc_now,
)
from agent_platform.collaborative_development_service import (
    CollaborativeDevelopmentConflict as ServiceConflict,
    CollaborativeDevelopmentService,
)
from agent_platform.collaborative_development_dispatcher import canonical_digest
from agent_platform.collaborative_development_storage import (
    CollaborativeDevelopmentBudgetExceeded,
    CollaborativeDevelopmentConflict,
    CollaborativeDevelopmentInvalidState,
    CollaborativeDevelopmentStore,
)
from agent_platform.development_workspace_broker import (
    DevelopmentWorkspaceBroker,
    DevelopmentWorkspaceSpec,
)


BASELINE = "d" * 40
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def test_storage_permission_tightening_tolerates_disappeared_sqlite_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "collaborative-development.db"
    database.write_bytes(b"database")
    sidecar = Path(f"{database}-shm")
    sidecar.write_bytes(b"ephemeral")
    store = CollaborativeDevelopmentStore(database)
    real_open = os.open

    def open_with_disappearing_sidecar(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        if Path(path) == sidecar:
            sidecar.unlink(missing_ok=True)
            raise FileNotFoundError(2, "No such file or directory", str(sidecar))
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", open_with_disappearing_sidecar)
    store._enforce_storage_permissions()

    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert not sidecar.exists()


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_repository(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "security@example.invalid")
    _git(source, "config", "user.name", "Security Fixture")
    (source / "src").mkdir()
    (source / "tests").mkdir()
    (source / "src" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "baseline")
    return source, _git(source, "rev-parse", "HEAD")


def _role_grants(
    tmp_path: Path,
    *,
    created_at: datetime,
    baseline: str = BASELINE,
) -> tuple[WorkspaceGrant, WorkspaceGrant]:
    common = {
        "baseline_commit": baseline,
        "allowed_paths": ("src", "tests"),
        "allowed_argv": (("pytest", "-q"),),
        "allowed_side_effects": (
            SideEffect.workspace_write,
            SideEffect.process_execute,
        ),
        "created_at": created_at,
    }
    return (
        WorkspaceGrant(
            workspace_id=uuid4(),
            agent_role=AgentRole.lilies,
            workspace_root=str(tmp_path / "lilies"),
            **common,
        ),
        WorkspaceGrant(
            workspace_id=uuid4(),
            agent_role=AgentRole.codex,
            workspace_root=str(tmp_path / "codex"),
            **common,
        ),
    )


def _assignment(
    tmp_path: Path,
    *,
    assignment_id=None,
    created_at: datetime | None = None,
    deadline: datetime | None = None,
    baseline: str = BASELINE,
    grants: tuple[WorkspaceGrant, WorkspaceGrant] | None = None,
    execution_mode: ExecutionMode = ExecutionMode.manual_dispatch,
    goal: str = "Secure bounded development.",
    max_wall_seconds: int = 7_200,
) -> DevelopmentAssignment:
    created = created_at or utc_now()
    return DevelopmentAssignment(
        assignment_id=assignment_id or uuid4(),
        goal=goal,
        software_id="security-fixture",
        baseline_commit=baseline,
        agent_roles=(
            AgentRoleGrant(
                agent_role=AgentRole.lilies,
                task_roles=(
                    DevelopmentTaskRole.implementer,
                    DevelopmentTaskRole.reviewer,
                    DevelopmentTaskRole.coordinator,
                ),
            ),
            AgentRoleGrant(
                agent_role=AgentRole.codex,
                task_roles=(DevelopmentTaskRole.implementer,),
            ),
        ),
        workspace_grants=grants
        or _role_grants(
            tmp_path,
            created_at=created,
            baseline=baseline,
        ),
        budget=DevelopmentBudget(
            max_work_items=20,
            max_commands=100,
            max_tool_calls=1_000,
            max_wall_seconds=max_wall_seconds,
            max_cost_usd=10,
        ),
        deadline=deadline or created + timedelta(hours=1),
        approval_mode=ApprovalMode.manual,
        execution_mode=execution_mode,
        created_at=created,
        updated_at=created,
    )


def _work_item(
    assignment: DevelopmentAssignment,
    *,
    objective: str = "Make the bounded change.",
) -> DevelopmentWorkItem:
    now = utc_now()
    return DevelopmentWorkItem(
        work_item_id=uuid4(),
        assignment_id=assignment.assignment_id,
        kind=WorkItemKind.bug,
        objective=objective,
        acceptance=("The bounded check passes.",),
        assigned_role=AgentRole.codex,
        created_at=now,
        updated_at=now,
    )


def _command(*, exit_code: int = 0, output_digest: str = DIGEST_A) -> CommandReceipt:
    now = utc_now()
    return CommandReceipt(
        argv=("pytest", "-q"),
        cwd="src",
        exit_code=exit_code,
        output_digest=output_digest,
        started_at=now,
        finished_at=now,
    )


@pytest.mark.asyncio
async def test_assignment_time_is_server_frozen_and_wall_budget_uses_server_now(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "data" / "development.db")
    await store.initialize()
    client_created = utc_now() - timedelta(minutes=30)
    assignment = _assignment(
        tmp_path,
        created_at=client_created,
        deadline=client_created + timedelta(hours=1),
    )
    before = utc_now()
    created = await store.create_assignment(
        assignment,
        actor_id="owner",
        idempotency_key="server-time-create-0001",
    )
    after = utc_now()
    assert before <= created.created_at <= after
    assert created.updated_at == created.created_at
    assert created.created_at != client_created

    client_future = utc_now() + timedelta(minutes=10)
    future_claim = _assignment(
        tmp_path,
        created_at=client_future,
        deadline=client_future + timedelta(minutes=20),
        max_wall_seconds=3_600,
    )
    future_before = utc_now()
    normalized_future = await store.create_assignment(
        future_claim,
        actor_id="owner",
        idempotency_key="server-time-create-future-0001",
    )
    future_after = utc_now()
    assert future_before <= normalized_future.created_at <= future_after
    assert normalized_future.created_at != client_future

    future_created = utc_now() + timedelta(hours=1)
    over_budget = _assignment(
        tmp_path,
        created_at=future_created,
        deadline=future_created + timedelta(minutes=30),
        max_wall_seconds=3_600,
    )
    with pytest.raises(
        CollaborativeDevelopmentBudgetExceeded,
        match="server creation",
    ):
        await store.create_assignment(
            over_budget,
            actor_id="owner",
            idempotency_key="server-time-create-0002",
        )

    old_created = utc_now() - timedelta(hours=2)
    expired = _assignment(
        tmp_path,
        created_at=old_created,
        deadline=old_created + timedelta(hours=1),
    )
    with pytest.raises(
        CollaborativeDevelopmentInvalidState,
        match="server creation time",
    ):
        await store.create_assignment(
            expired,
            actor_id="owner",
            idempotency_key="server-time-create-0003",
        )


@pytest.mark.asyncio
async def test_store_redacts_every_json_surface_and_private_sqlite_files(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "private-data"
    database = data_dir / "development.db"
    store = CollaborativeDevelopmentStore(database)
    await store.initialize()
    secrets = (
        "Bearer REDACTION-ASSIGNMENT-SECRET",
        "Cookie: session=REDACTION-WORK-SECRET",
        "sk-REDACTIONRESULTSECRET1234567890",
        "Bearer REDACTION-REVIEW-SECRET",
        "Cookie: failure=REDACTION-OUTBOX-SECRET",
        "token: REDACTION-GENERIC-TOKEN-SECRET",
    )
    assignment = _assignment(
        tmp_path,
        execution_mode=ExecutionMode.autonomous,
        goal=f"Handle {secrets[0]} without persistence.",
    )
    await store.create_assignment(
        assignment,
        actor_id="owner",
        idempotency_key="redaction-assignment-0001",
    )
    item = await store.create_work_item(
        _work_item(assignment, objective=f"Do not persist {secrets[1]}"),
        actor_role="lilies",
        actor_id="lilies-agent",
        idempotency_key="redaction-work-item-0001",
    )
    claims = await store.claim_pending_outbox(
        claimed_by="security-dispatcher",
        limit=1,
    )
    assert len(claims) == 1
    failed = await store.mark_outbox_failed(
        claims[0].outbox.outbox_id,
        claim_id=claims[0].claim_id,
        error=secrets[4],
        retry_at=None,
    )
    assert failed.last_error == "[REDACTED]"

    lease = await store.acquire_lease(
        item.work_item_id,
        owner_role=AgentRole.codex,
        owner_id="codex-agent",
        expected_revision=item.revision,
        idempotency_key="redaction-lease-0001",
    )
    working = await store.start_work(
        lease.lease_id,
        owner_id="codex-agent",
        expected_work_item_revision=lease.work_item_revision,
        idempotency_key="redaction-start-0001",
    )
    result = DevelopmentResult(
        result_id=uuid4(),
        assignment_id=assignment.assignment_id,
        work_item_id=item.work_item_id,
        lease_id=lease.lease_id,
        agent_role=AgentRole.codex,
        baseline_commit=assignment.baseline_commit,
        diff_digest=DIGEST_B,
        commands=(_command(),),
        tests=(
            DevelopmentTestReceipt(
                name="bounded check",
                command_digest=DIGEST_A,
                exit_code=0,
                passed=True,
                output_digest=DIGEST_C,
            ),
        ),
        limitations=(f"Never retain {secrets[2]}",),
        evidence_refs=(DIGEST_B,),
        reproduction_steps=(f"Run pytest -q with {secrets[5]}",),
        created_at=utc_now(),
    )
    ready = await store.submit_result(
        result,
        owner_id="codex-agent",
        expected_work_item_revision=working.revision,
        idempotency_key="redaction-result-0001",
    )
    await store.submit_review(
        LiliesReview(
            review_id=uuid4(),
            assignment_id=assignment.assignment_id,
            work_item_id=item.work_item_id,
            result_id=result.result_id,
            verdict=ReviewVerdict.rework,
            acceptance_checks=(
                AcceptanceCheck(
                    criterion=item.acceptance[0],
                    passed=False,
                    evidence_refs=(DIGEST_C,),
                ),
            ),
            verification_commands=(_command(exit_code=1),),
            evidence_refs=(DIGEST_C,),
            next_requirements=(f"Retry without {secrets[3]}",),
            created_at=utc_now(),
        ),
        reviewer_id="lilies-agent",
        expected_work_item_revision=ready.revision,
        idempotency_key="redaction-review-0001",
    )

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        candidate = Path(f"{database}{suffix}")
        if candidate.exists():
            assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
    persisted = b"".join(
        path.read_bytes()
        for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
        if path.exists()
    )
    for secret in secrets:
        assert secret.encode() not in persisted


@pytest.mark.asyncio
async def test_broker_attestation_role_projection_and_dirty_rejection(
    tmp_path: Path,
) -> None:
    source, baseline = _source_repository(tmp_path)
    assignment_id = uuid4()
    broker = DevelopmentWorkspaceBroker(tmp_path / "workspace-state")
    prepared = broker.prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.lilies,
                allowed_paths=("src", "tests"),
                allowed_argv=(("pytest", "-q"),),
                allowed_side_effects=(SideEffect.process_execute,),
            ),
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.codex,
                allowed_paths=("src", "tests"),
                allowed_argv=(("pytest", "-q"),),
                allowed_side_effects=(
                    SideEffect.workspace_write,
                    SideEffect.process_execute,
                ),
                secret_refs=("codex.private-ref",),
            ),
        ),
    )
    assignment = _assignment(
        tmp_path,
        assignment_id=assignment_id,
        baseline=baseline,
        grants=prepared.grants,
    )
    service = CollaborativeDevelopmentService(
        store=CollaborativeDevelopmentStore(tmp_path / "data" / "development.db"),
        enabled=True,
    )
    await service.initialize()
    owner = DevelopmentPrincipal(actor_role="user", actor_id="owner")
    created_assignment = await service.create_assignment(
        principal=owner,
        assignment=assignment,
        idempotency_key="attested-assignment-0001",
    )
    codex = DevelopmentPrincipal(
        actor_role=AgentRole.codex.value,
        actor_id="codex-agent",
        assignment_id=assignment_id,
    )
    lilies = DevelopmentPrincipal(
        actor_role=AgentRole.lilies.value,
        actor_id="lilies-agent",
        assignment_id=assignment_id,
    )
    codex_view = await service.get_assignment(
        principal=codex,
        assignment_id=assignment_id,
    )
    assert isinstance(codex_view, DevelopmentAssignmentProjection)
    assert codex_view.agent_role.agent_role == AgentRole.codex
    assert codex_view.workspace_grant.agent_role == AgentRole.codex
    serialized = codex_view.model_dump_json()
    lilies_root = next(
        grant.workspace_root
        for grant in assignment.workspace_grants
        if grant.agent_role == AgentRole.lilies
    )
    assert lilies_root not in serialized
    assert "codex.private-ref" in serialized

    lilies_status = await service.status_summary(
        principal=lilies,
        assignment_id=assignment_id,
    )
    status_view = lilies_status["assignment"]
    assert isinstance(status_view, DevelopmentAssignmentProjection)
    assert status_view.workspace_grant.agent_role == AgentRole.lilies
    assert "codex.private-ref" not in status_view.model_dump_json()
    owner_view = await service.get_assignment(
        principal=owner,
        assignment_id=assignment_id,
    )
    assert isinstance(owner_view, DevelopmentAssignment)
    assert len(owner_view.workspace_grants) == 2

    created_codex = next(
        Path(grant.workspace_root)
        for grant in prepared.grants
        if grant.agent_role == AgentRole.codex
    )
    (created_codex / "src" / "work-after-create.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )
    replayed_assignment = await service.create_assignment(
        principal=owner,
        assignment=assignment,
        idempotency_key="attested-assignment-0001",
    )
    assert replayed_assignment == created_assignment

    dirty_assignment_id = uuid4()
    dirty_prepared = broker.prepare(
        source_repository=source,
        assignment_id=dirty_assignment_id,
        baseline_revision=baseline,
        specs=(
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.lilies,
                allowed_paths=("src",),
            ),
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.codex,
                allowed_paths=("src",),
            ),
        ),
    )
    dirty_codex = next(
        Path(grant.workspace_root)
        for grant in dirty_prepared.grants
        if grant.agent_role == AgentRole.codex
    )
    (dirty_codex / "src" / "untracked.py").write_text(
        "SENSITIVE = False\n",
        encoding="utf-8",
    )
    dirty_assignment = _assignment(
        tmp_path,
        assignment_id=dirty_assignment_id,
        baseline=baseline,
        grants=dirty_prepared.grants,
    )
    with pytest.raises(ServiceConflict, match="must be clean"):
        await service.create_assignment(
            principal=owner,
            assignment=dirty_assignment,
            idempotency_key="attested-assignment-0002",
        )


@pytest.mark.asyncio
async def test_shared_git_common_directory_and_ancestor_roots_are_rejected(
    tmp_path: Path,
) -> None:
    source, baseline = _source_repository(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    lilies_root = worktrees / "lilies"
    codex_root = worktrees / "codex"
    _git(source, "worktree", "add", "--detach", str(lilies_root), baseline)
    _git(source, "worktree", "add", "--detach", str(codex_root), baseline)
    created = utc_now()
    common = {
        "baseline_commit": baseline,
        "allowed_paths": ("src",),
        "created_at": created,
    }
    shared = _assignment(
        tmp_path,
        baseline=baseline,
        grants=(
            WorkspaceGrant(
                workspace_id=uuid4(),
                agent_role=AgentRole.lilies,
                workspace_root=str(lilies_root),
                **common,
            ),
            WorkspaceGrant(
                workspace_id=uuid4(),
                agent_role=AgentRole.codex,
                workspace_root=str(codex_root),
                **common,
            ),
        ),
    )
    service = CollaborativeDevelopmentService(
        store=CollaborativeDevelopmentStore(tmp_path / "data" / "development.db"),
        enabled=True,
    )
    await service.initialize()
    with pytest.raises(ServiceConflict, match="common directory"):
        await service.create_assignment(
            principal=DevelopmentPrincipal(actor_role="user", actor_id="owner"),
            assignment=shared,
            idempotency_key="shared-worktree-assignment-0001",
        )

    claimed_independent_id = uuid4()
    claimed_root = tmp_path / "claimed-independent" / str(claimed_independent_id)
    claimed_root.mkdir(parents=True)
    for role in (AgentRole.lilies, AgentRole.codex):
        subprocess.run(
            [
                "git",
                "clone",
                "--local",
                "--no-hardlinks",
                str(source),
                str(claimed_root / role.value),
            ],
            check=True,
            capture_output=True,
        )
    independent_common = {
        "baseline_commit": baseline,
        "allowed_paths": ("src",),
        "created_at": created,
    }
    claimed_independent = _assignment(
        tmp_path,
        assignment_id=claimed_independent_id,
        baseline=baseline,
        grants=(
            WorkspaceGrant(
                workspace_id=uuid4(),
                agent_role=AgentRole.lilies,
                workspace_root=str(claimed_root / AgentRole.lilies.value),
                **independent_common,
            ),
            WorkspaceGrant(
                workspace_id=uuid4(),
                agent_role=AgentRole.codex,
                workspace_root=str(claimed_root / AgentRole.codex.value),
                **independent_common,
            ),
        ),
    )
    with pytest.raises(ServiceConflict, match="broker attestation"):
        await service.create_assignment(
            principal=DevelopmentPrincipal(actor_role="user", actor_id="owner"),
            assignment=claimed_independent,
            idempotency_key="claimed-independent-assignment-0001",
        )

    payload = shared.model_dump(mode="python")
    payload["workspace_grants"][0]["workspace_root"] = str(worktrees)
    payload["workspace_grants"][1]["workspace_root"] = str(worktrees / "nested")
    with pytest.raises(ValidationError, match="must not contain"):
        DevelopmentAssignment.model_validate(payload)


def test_accepted_review_requires_zero_exit_verification() -> None:
    with pytest.raises(ValidationError, match="exit zero"):
        LiliesReview(
            review_id=uuid4(),
            assignment_id=uuid4(),
            work_item_id=uuid4(),
            result_id=uuid4(),
            verdict=ReviewVerdict.accepted,
            acceptance_checks=(
                AcceptanceCheck(
                    criterion="The bounded check passes.",
                    passed=True,
                    evidence_refs=(DIGEST_A,),
                ),
            ),
            verification_commands=(_command(exit_code=1),),
            evidence_refs=(DIGEST_A,),
            created_at=utc_now(),
        )


@pytest.mark.asyncio
async def test_stop_cancels_open_work_and_allows_truthful_read_only_archive(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "data" / "development.db")
    await store.initialize()
    assignment = _assignment(tmp_path)
    created = await store.create_assignment(
        assignment,
        actor_id="owner",
        idempotency_key="cancel-assignment-0001",
    )
    item = await store.create_work_item(
        _work_item(assignment),
        actor_role="user",
        actor_id="owner",
        idempotency_key="cancel-work-item-0001",
    )
    stopped = await store.stop_assignment(
        assignment.assignment_id,
        expected_revision=created.revision,
        actor_id="owner",
        idempotency_key="cancel-stop-0001",
    )
    cancelled = await store.get_work_item(item.work_item_id)
    assert cancelled.status == WorkItemStatus.cancelled
    assert cancelled.status != WorkItemStatus.closed
    archived = await store.archive_assignment(
        assignment.assignment_id,
        expected_revision=stopped.revision,
        actor_id="owner",
        idempotency_key="cancel-archive-0001",
    )
    assert archived.status.value == "archived"
    events = await store.read_events(assignment.assignment_id)
    assert any(event.event_type == "work_item.cancelled" for event in events)
    assert events[-1].event_type == "assignment.archived"
    with pytest.raises(CollaborativeDevelopmentInvalidState):
        await store.create_work_item(
            _work_item(assignment),
            actor_role="user",
            actor_id="owner",
            idempotency_key="cancel-work-item-after-archive",
        )


@pytest.mark.asyncio
async def test_grant_revision_is_fenced_and_resumes_only_authorization_outbox(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "data" / "development.db")
    await store.initialize()
    assignment = _assignment(
        tmp_path,
        execution_mode=ExecutionMode.autonomous,
    )
    created = await store.create_assignment(
        assignment,
        actor_id="owner",
        idempotency_key="grant-revision-assignment-0001",
    )
    await store.create_work_item(
        _work_item(assignment),
        actor_role="user",
        actor_id="owner",
        idempotency_key="grant-revision-work-0001",
    )
    claims = await store.claim_pending_outbox(
        claimed_by="grant-revision-dispatcher",
        limit=1,
    )
    assert len(claims) == 1
    failed = await store.mark_outbox_failed(
        claims[0].outbox.outbox_id,
        claim_id=claims[0].claim_id,
        error="authorization_required",
        retry_at=None,
    )
    current = next(
        grant for grant in created.workspace_grants if grant.agent_role == AgentRole.codex
    )
    replacement = current.model_copy(
        update={
            "grant_revision": current.grant_revision + 1,
            "allowed_paths": (*current.allowed_paths, "docs"),
        }
    )
    replacement_budget = created.budget.model_copy(
        update={"max_commands": created.budget.max_commands + 1}
    )
    expected_digest = canonical_digest(current)
    updated = await store.apply_workspace_grant_revision(
        assignment.assignment_id,
        outbox_id=failed.outbox_id,
        replacement_grant=replacement,
        replacement_budget=replacement_budget,
        expected_assignment_revision=created.revision,
        expected_grant_digest=expected_digest,
        actor_id="owner",
        idempotency_key="grant-revision-apply-0001",
    )
    assert updated.revision == created.revision + 1
    assert updated.budget == replacement_budget
    assert (
        next(grant for grant in updated.workspace_grants if grant.agent_role == AgentRole.codex)
        == replacement
    )
    assert not any(
        pending.outbox_id == failed.outbox_id for pending in await store.list_pending_outbox()
    )
    resumed = await store.resume_authorization_outbox(
        assignment.assignment_id,
        outbox_id=failed.outbox_id,
        expected_grant_digest=canonical_digest(replacement),
        actor_id="owner",
        idempotency_key="grant-revision-resume-0001",
    )
    assert resumed.status.value == "pending"
    assert resumed.last_error is None
    assert resumed.outbox_id == failed.outbox_id
    assert any(
        pending.outbox_id == failed.outbox_id for pending in await store.list_pending_outbox()
    )

    replay_assignment = await store.apply_workspace_grant_revision(
        assignment.assignment_id,
        outbox_id=failed.outbox_id,
        replacement_grant=replacement,
        replacement_budget=replacement_budget,
        expected_assignment_revision=created.revision,
        expected_grant_digest=expected_digest,
        actor_id="owner",
        idempotency_key="grant-revision-apply-0001",
    )
    assert replay_assignment == updated
    replay_outbox = await store.resume_authorization_outbox(
        assignment.assignment_id,
        outbox_id=failed.outbox_id,
        expected_grant_digest=canonical_digest(replacement),
        actor_id="owner",
        idempotency_key="grant-revision-resume-0001",
    )
    assert replay_outbox == resumed
    events = await store.read_events(assignment.assignment_id)
    assert sum(event.event_type == "assignment.workspace_grant_revised" for event in events) == 1
    assert (
        sum(event.event_type == "assignment.authorization_outbox_resumed" for event in events) == 1
    )

    with pytest.raises(CollaborativeDevelopmentConflict, match="compare-and-set"):
        await store.apply_workspace_grant_revision(
            assignment.assignment_id,
            outbox_id=failed.outbox_id,
            replacement_grant=replacement.model_copy(
                update={"grant_revision": replacement.grant_revision + 1}
            ),
            expected_assignment_revision=created.revision,
            expected_grant_digest=expected_digest,
            actor_id="owner",
            idempotency_key="grant-revision-apply-0002",
        )
