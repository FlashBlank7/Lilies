from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_platform.collaborative_development_models import (
    AcceptanceCheck,
    AgentRole,
    AgentRoleGrant,
    ApprovalMode,
    CommandReceipt,
    DevelopmentAssignment,
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
)
from agent_platform.collaborative_development_storage import (
    CollaborativeDevelopmentAuthorizationError,
    CollaborativeDevelopmentBudgetExceeded,
    CollaborativeDevelopmentConflict,
    CollaborativeDevelopmentInvalidState,
    CollaborativeDevelopmentStore,
    TrustedProviderCostAuthorization,
    TrustedProviderCostReceipt,
)
from agent_platform.lilies_development_tools import (
    DevelopmentToolAuthority,
    DevelopmentToolDenied,
    DevelopmentToolName,
    DevelopmentToolUsageReplay,
    DevelopmentWorkspaceTools,
    WorkspaceReadRequest,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
BASELINE = "d" * 40


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _assignment(
    tmp_path: Path,
    *,
    execution_mode: ExecutionMode = ExecutionMode.manual_dispatch,
    approval_mode: ApprovalMode = ApprovalMode.manual,
) -> DevelopmentAssignment:
    created = _now()
    common = {
        "baseline_commit": BASELINE,
        "grant_revision": 1,
        "allowed_paths": ("src", "tests"),
        "allowed_argv": (
            ("pytest", "-q"),
            ("python", "-m", "compileall", "src"),
        ),
        "allowed_hosts": (),
        "allowed_side_effects": (
            SideEffect.workspace_write,
            SideEffect.process_execute,
            SideEffect.git_commit,
        ),
        "secret_refs": (),
        "created_at": created,
    }
    return DevelopmentAssignment(
        assignment_id=uuid4(),
        goal="Implement and independently review a bounded parser change.",
        software_id="example-parser",
        baseline_commit=BASELINE,
        agent_roles=(
            AgentRoleGrant(
                agent_role=AgentRole.lilies,
                task_roles=(
                    DevelopmentTaskRole.implementer,
                    DevelopmentTaskRole.reviewer,
                ),
            ),
            AgentRoleGrant(
                agent_role=AgentRole.codex,
                task_roles=(DevelopmentTaskRole.implementer,),
            ),
        ),
        workspace_grants=(
            WorkspaceGrant(
                workspace_id=uuid4(),
                agent_role=AgentRole.lilies,
                workspace_root=str(tmp_path / "lilies-workspace"),
                **common,
            ),
            WorkspaceGrant(
                workspace_id=uuid4(),
                agent_role=AgentRole.codex,
                workspace_root=str(tmp_path / "codex-workspace"),
                **common,
            ),
        ),
        budget=DevelopmentBudget(
            max_work_items=20,
            max_commands=100,
            max_tool_calls=1_000,
            max_wall_seconds=7_200,
            max_cost_usd=20,
        ),
        deadline=created + timedelta(hours=2),
        approval_mode=approval_mode,
        execution_mode=execution_mode,
        created_at=created,
        updated_at=created,
    )


def _work_item(assignment: DevelopmentAssignment) -> DevelopmentWorkItem:
    created = _now()
    return DevelopmentWorkItem(
        work_item_id=uuid4(),
        assignment_id=assignment.assignment_id,
        kind=WorkItemKind.bug,
        objective="Reject an empty input without changing the public API.",
        acceptance=(
            "Empty input is rejected.",
            "The focused regression test passes.",
        ),
        assigned_role=AgentRole.codex,
        created_at=created,
        updated_at=created,
    )


def _command(*, argv: tuple[str, ...] = ("pytest", "-q")) -> CommandReceipt:
    started = _now()
    return CommandReceipt(
        argv=argv,
        cwd="src",
        exit_code=0,
        output_digest=DIGEST_A,
        started_at=started,
        finished_at=started + timedelta(milliseconds=20),
    )


def _result(
    assignment: DevelopmentAssignment,
    item: DevelopmentWorkItem,
    lease_id,
    *,
    argv: tuple[str, ...] = ("pytest", "-q"),
) -> DevelopmentResult:
    return DevelopmentResult(
        result_id=uuid4(),
        assignment_id=assignment.assignment_id,
        work_item_id=item.work_item_id,
        lease_id=lease_id,
        agent_role=AgentRole.codex,
        baseline_commit=assignment.baseline_commit,
        commit_sha="e" * 40,
        diff_digest=DIGEST_B,
        commands=(_command(argv=argv),),
        tests=(
            DevelopmentTestReceipt(
                name="focused parser regression",
                command_digest=DIGEST_A,
                exit_code=0,
                passed=True,
                output_digest=DIGEST_C,
            ),
        ),
        evidence_refs=(DIGEST_B,),
        reproduction_steps=("Run pytest -q in the granted workspace.",),
        created_at=_now(),
    )


def _accepted_review(
    assignment: DevelopmentAssignment,
    item: DevelopmentWorkItem,
    result: DevelopmentResult,
) -> LiliesReview:
    return LiliesReview(
        review_id=uuid4(),
        assignment_id=assignment.assignment_id,
        work_item_id=item.work_item_id,
        result_id=result.result_id,
        verdict=ReviewVerdict.accepted,
        acceptance_checks=(
            AcceptanceCheck(
                criterion=item.acceptance[0],
                passed=True,
                evidence_refs=(DIGEST_A,),
            ),
            AcceptanceCheck(
                criterion=item.acceptance[1],
                passed=True,
                evidence_refs=(DIGEST_C,),
            ),
        ),
        verification_commands=(_command(),),
        evidence_refs=(DIGEST_A, DIGEST_B, DIGEST_C),
        created_at=_now(),
    )


def test_contract_rejects_implicit_or_widened_authority(tmp_path: Path) -> None:
    assignment = _assignment(tmp_path)
    payload = assignment.model_dump(mode="python")
    payload["application_id"] = str(uuid4())
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DevelopmentAssignment.model_validate(payload)

    grant = assignment.workspace_grants[0].model_dump(mode="python")
    grant["allowed_paths"] = ("../protected",)
    with pytest.raises(ValidationError, match="stay inside"):
        WorkspaceGrant.model_validate(grant)

    grant = assignment.workspace_grants[0].model_dump(mode="python")
    grant["allowed_hosts"] = ("api.example.test",)
    with pytest.raises(ValidationError, match="network_access"):
        WorkspaceGrant.model_validate(grant)

    payload = assignment.model_dump(mode="python")
    payload["agent_roles"] = (payload["agent_roles"][0], payload["agent_roles"][0])
    with pytest.raises(ValidationError, match="exactly Lilies and Codex"):
        DevelopmentAssignment.model_validate(payload)


@pytest.mark.asyncio
async def test_assignment_is_wal_backed_idempotent_and_cas_guarded(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "development.db")
    await store.initialize()
    assignment = _assignment(tmp_path)

    created = await store.create_assignment(
        assignment,
        actor_id="user-local",
        idempotency_key="create-assignment-0001",
    )
    replay = await store.create_assignment(
        assignment,
        actor_id="user-local",
        idempotency_key="create-assignment-0001",
    )
    assert replay == created
    assert await store.journal_mode() == "wal"
    assert created.enterprise_denominator is False

    changed = assignment.model_copy(update={"goal": "A conflicting goal."})
    with pytest.raises(CollaborativeDevelopmentConflict, match="different request"):
        await store.create_assignment(
            changed,
            actor_id="user-local",
            idempotency_key="create-assignment-0001",
        )

    with pytest.raises(CollaborativeDevelopmentConflict, match="compare-and-set"):
        await store.set_execution_mode(
            assignment.assignment_id,
            ExecutionMode.autonomous,
            expected_revision=99,
            actor_id="user-local",
            idempotency_key="switch-execution-mode-0001",
        )
    unchanged = await store.get_assignment(assignment.assignment_id)
    assert unchanged.execution_mode == ExecutionMode.manual_dispatch
    assert unchanged.revision == 1

    autonomous = await store.set_execution_mode(
        assignment.assignment_id,
        ExecutionMode.autonomous,
        expected_revision=1,
        actor_id="user-local",
        idempotency_key="switch-execution-mode-0002",
    )
    awaiting = await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-local",
        idempotency_key="mode-switch-work-item-0001",
    )
    assert awaiting.status == WorkItemStatus.awaiting_dispatch
    assert await store.list_pending_outbox()
    manual = await store.set_execution_mode(
        assignment.assignment_id,
        ExecutionMode.manual_dispatch,
        expected_revision=autonomous.revision,
        actor_id="user-local",
        idempotency_key="switch-execution-mode-0003",
    )
    assert manual.execution_mode == ExecutionMode.manual_dispatch
    assert await store.list_pending_outbox() == []
    with pytest.raises(CollaborativeDevelopmentAuthorizationError, match="not been dispatched"):
        await store.acquire_lease(
            awaiting.work_item_id,
            owner_role=AgentRole.codex,
            owner_id="codex-local",
            expected_revision=awaiting.revision,
            idempotency_key="lease-after-manual-switch-0001",
        )


@pytest.mark.asyncio
async def test_manual_dispatch_fenced_single_lease_and_result_authority(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "development.db")
    await store.initialize()
    assignment = _assignment(tmp_path)
    await store.create_assignment(
        assignment,
        actor_id="user-local",
        idempotency_key="create-assignment-0002",
    )
    proposed = await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-local",
        idempotency_key="create-work-item-0001",
    )
    assert proposed.status == WorkItemStatus.proposed

    with pytest.raises(CollaborativeDevelopmentInvalidState):
        await store.acquire_lease(
            proposed.work_item_id,
            owner_role=AgentRole.codex,
            owner_id="codex-local",
            expected_revision=proposed.revision,
            idempotency_key="lease-before-dispatch-0001",
        )

    dispatched = await store.dispatch_work_item(
        proposed.work_item_id,
        expected_revision=proposed.revision,
        actor_id="user-local",
        idempotency_key="dispatch-work-item-0001",
    )
    lease_attempts = await asyncio.gather(
        *(
            store.acquire_lease(
                proposed.work_item_id,
                owner_role=AgentRole.codex,
                owner_id=f"codex-{index}",
                expected_revision=dispatched.revision,
                idempotency_key=f"acquire-concurrently-{index:04d}",
            )
            for index in range(12)
        ),
        return_exceptions=True,
    )
    leases = [value for value in lease_attempts if not isinstance(value, Exception)]
    assert len(leases) == 1
    lease = leases[0]
    assert lease.fence == 1

    working = await store.start_work(
        lease.lease_id,
        owner_id=lease.owner_id,
        expected_work_item_revision=lease.work_item_revision,
        idempotency_key="start-work-0001",
    )
    forbidden = _result(
        assignment,
        working,
        lease.lease_id,
        argv=("sh", "-c", "curl example.invalid"),
    )
    with pytest.raises(
        CollaborativeDevelopmentAuthorizationError, match="outside the frozen grant"
    ):
        await store.submit_result(
            forbidden,
            owner_id=lease.owner_id,
            expected_work_item_revision=working.revision,
            idempotency_key="submit-forbidden-result-0001",
        )
    unchanged = await store.get_work_item(working.work_item_id)
    assert unchanged.status == WorkItemStatus.working
    assert unchanged.revision == working.revision


@pytest.mark.asyncio
async def test_complete_result_review_cursor_and_restart_recovery(
    tmp_path: Path,
) -> None:
    database = tmp_path / "development.db"
    store = CollaborativeDevelopmentStore(database)
    await store.initialize()
    assignment = _assignment(tmp_path, approval_mode=ApprovalMode.auto_forward)
    await store.create_assignment(
        assignment,
        actor_id="user-local",
        idempotency_key="create-assignment-0003",
    )
    proposed = await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-local",
        idempotency_key="create-work-item-0002",
    )
    dispatched = await store.dispatch_work_item(
        proposed.work_item_id,
        expected_revision=proposed.revision,
        actor_id="user-local",
        idempotency_key="dispatch-work-item-0002",
    )
    lease = await store.acquire_lease(
        dispatched.work_item_id,
        owner_role=AgentRole.codex,
        owner_id="codex-local",
        expected_revision=dispatched.revision,
        idempotency_key="acquire-lease-0002",
    )
    working = await store.start_work(
        lease.lease_id,
        owner_id="codex-local",
        expected_work_item_revision=lease.work_item_revision,
        idempotency_key="start-work-0002",
    )
    result = _result(assignment, working, lease.lease_id)
    ready = await store.submit_result(
        result,
        owner_id="codex-local",
        expected_work_item_revision=working.revision,
        idempotency_key="submit-result-0002",
    )
    replay = await store.submit_result(
        result,
        owner_id="codex-local",
        expected_work_item_revision=working.revision,
        idempotency_key="submit-result-0002",
    )
    assert replay == ready
    assert ready.status == WorkItemStatus.ready_for_lilies_review
    pending = await store.list_pending_outbox()
    assert any(item.kind == "lilies_review" for item in pending)

    trusted_review = _accepted_review(assignment, ready, result)
    with pytest.raises(
        CollaborativeDevelopmentConflict,
        match="not bound to the current result",
    ):
        await store.submit_review(
            trusted_review.model_copy(update={"evidence_refs": (DIGEST_A, DIGEST_C)}),
            reviewer_id="lilies-local",
            expected_work_item_revision=ready.revision,
            idempotency_key="submit-unbound-review-0001",
        )

    with pytest.raises(
        CollaborativeDevelopmentConflict,
        match="zero-exit verification",
    ):
        await store.submit_review(
            trusted_review.model_copy(
                update={
                    "verification_commands": (
                        trusted_review.verification_commands[0].model_copy(update={"exit_code": 1}),
                    )
                }
            ),
            reviewer_id="lilies-local",
            expected_work_item_revision=ready.revision,
            idempotency_key="submit-nonzero-review-0001",
        )

    accepted = await store.submit_review(
        trusted_review,
        reviewer_id="lilies-local",
        expected_work_item_revision=ready.revision,
        idempotency_key="submit-review-0001",
    )
    assert accepted.status == WorkItemStatus.accepted
    closed = await store.close_work_item(
        accepted.work_item_id,
        expected_revision=accepted.revision,
        actor_id="user-local",
        idempotency_key="close-work-item-0001",
    )
    assert closed.status == WorkItemStatus.closed

    events = await store.read_events(assignment.assignment_id)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    cursor = await store.ack_events(
        assignment.assignment_id,
        reader_role="lilies",
        reader_id="lilies-local",
        ack_seq=events[4].seq,
        expected_cursor_revision=0,
        idempotency_key="ack-development-events-0001",
    )
    assert cursor.revision == 1

    restarted = CollaborativeDevelopmentStore(database)
    await restarted.initialize()
    resumed = await restarted.read_events(assignment.assignment_id, after=cursor.ack_seq)
    assert resumed
    assert resumed[0].seq == cursor.ack_seq + 1
    assert (
        await restarted.get_reader_cursor(
            assignment.assignment_id,
            reader_role="lilies",
            reader_id="lilies-local",
        )
        == cursor
    )
    assert (await restarted.get_work_item(closed.work_item_id)).status == WorkItemStatus.closed


@pytest.mark.asyncio
async def test_autonomous_dispatch_and_expired_lease_recover_durably(
    tmp_path: Path,
) -> None:
    database = tmp_path / "development.db"
    store = CollaborativeDevelopmentStore(database)
    await store.initialize()
    assignment = _assignment(
        tmp_path,
        execution_mode=ExecutionMode.autonomous,
        approval_mode=ApprovalMode.auto_forward,
    )
    await store.create_assignment(
        assignment,
        actor_id="user-local",
        idempotency_key="create-assignment-0004",
    )
    awaiting = await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-local",
        idempotency_key="create-work-item-0003",
    )
    assert awaiting.status == WorkItemStatus.awaiting_dispatch
    assert len(await store.list_pending_outbox()) == 1

    lease = await store.acquire_lease(
        awaiting.work_item_id,
        owner_role=AgentRole.codex,
        owner_id="codex-local",
        expected_revision=awaiting.revision,
        ttl_seconds=1,
        idempotency_key="acquire-short-lease-0001",
    )
    expired = await store.recover_expired_leases(now=lease.expires_at + timedelta(seconds=1))
    assert [item.lease_id for item in expired] == [lease.lease_id]
    recovered = await store.get_work_item(awaiting.work_item_id)
    assert recovered.status == WorkItemStatus.awaiting_dispatch
    assert recovered.revision == lease.work_item_revision + 1

    restarted = CollaborativeDevelopmentStore(database)
    await restarted.initialize()
    reacquired = await restarted.acquire_lease(
        awaiting.work_item_id,
        owner_role=AgentRole.codex,
        owner_id="codex-restarted",
        expected_revision=recovered.revision,
        idempotency_key="reacquire-after-restart-0001",
    )
    assert reacquired.fence == lease.fence + 1
    events = await restarted.read_events(assignment.assignment_id)
    assert any(event.event_type == "work_item.lease_expired" for event in events)


@pytest.mark.asyncio
async def test_actual_tool_usage_budget_is_atomic_restart_safe_and_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "metered-development.db"
    first_store = CollaborativeDevelopmentStore(database)
    second_store = CollaborativeDevelopmentStore(database)
    await first_store.initialize()
    await second_store.initialize()
    candidate = _assignment(tmp_path)
    assignment = candidate.model_copy(
        update={
            "budget": candidate.budget.model_copy(update={"max_tool_calls": 3, "max_commands": 2})
        }
    )
    await first_store.create_assignment(
        assignment,
        actor_id="usage-owner",
        idempotency_key="usage-create-assignment-0001",
    )

    attempts = await asyncio.gather(
        *(
            (first_store if index % 2 == 0 else second_store).reserve_development_tool_usage(
                assignment_id=assignment.assignment_id,
                actor_role=AgentRole.lilies,
                usage_id=f"read-{index:04d}",
                tool_name="workspace_read",
                request_digest=DIGEST_A,
                command_argv=None,
                command_cwd=None,
            )
            for index in range(20)
        ),
        return_exceptions=True,
    )
    assert sum(value is True for value in attempts) == 3
    assert (
        sum(isinstance(value, CollaborativeDevelopmentBudgetExceeded) for value in attempts) == 17
    ), [(type(value).__name__, str(value)) for value in attempts if isinstance(value, Exception)]

    records = await first_store.list_development_tool_usage(assignment.assignment_id)
    assert len(records) == 3
    assert sum(record.tool_calls for record in records) == 3
    assert sum(record.commands for record in records) == 0

    restarted = CollaborativeDevelopmentStore(database)
    await restarted.initialize()
    first = records[0]
    assert not await restarted.reserve_development_tool_usage(
        assignment_id=assignment.assignment_id,
        actor_role=AgentRole.lilies,
        usage_id=first.usage_id,
        tool_name="workspace_read",
        request_digest=first.request_digest,
        command_argv=None,
        command_cwd=None,
    )
    with pytest.raises(
        CollaborativeDevelopmentConflict,
        match="different tool request",
    ):
        await restarted.reserve_development_tool_usage(
            assignment_id=assignment.assignment_id,
            actor_role=AgentRole.lilies,
            usage_id=first.usage_id,
            tool_name="workspace_read",
            request_digest=DIGEST_B,
            command_argv=None,
            command_cwd=None,
        )


@pytest.mark.asyncio
async def test_process_and_git_usage_reserve_tool_and_command_before_execution(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "command-meter.db")
    await store.initialize()
    candidate = _assignment(tmp_path)
    assignment = candidate.model_copy(
        update={
            "budget": candidate.budget.model_copy(update={"max_tool_calls": 10, "max_commands": 1})
        }
    )
    await store.create_assignment(
        assignment,
        actor_id="usage-owner",
        idempotency_key="command-create-assignment-0001",
    )
    assert await store.reserve_development_tool_usage(
        assignment_id=assignment.assignment_id,
        actor_role=AgentRole.codex,
        usage_id="command-0001",
        tool_name="process_run",
        request_digest=DIGEST_A,
        command_argv=("pytest", "-q"),
        command_cwd="src",
    )
    with pytest.raises(
        CollaborativeDevelopmentBudgetExceeded,
        match="command budget",
    ):
        await store.reserve_development_tool_usage(
            assignment_id=assignment.assignment_id,
            actor_role=AgentRole.codex,
            usage_id="command-0002",
            tool_name="git_diff",
            request_digest=DIGEST_B,
            command_argv=("git", "diff", "--unified=3", "--"),
            command_cwd="src",
        )
    records = await store.list_development_tool_usage(assignment.assignment_id)
    assert [(record.tool_calls, record.commands) for record in records] == [(1, 1)]

    root_candidate = _assignment(tmp_path)
    root_assignment = root_candidate.model_copy(
        update={
            "budget": root_candidate.budget.model_copy(
                update={"max_tool_calls": 1, "max_commands": 1}
            )
        }
    )
    await store.create_assignment(
        root_assignment,
        actor_id="usage-owner",
        idempotency_key="root-command-create-assignment-0001",
    )
    assert await store.reserve_development_tool_usage(
        assignment_id=root_assignment.assignment_id,
        actor_role=AgentRole.codex,
        usage_id="root-command-0001",
        tool_name="process_run",
        request_digest=DIGEST_A,
        command_argv=("codex", "exec"),
        command_cwd=".",
    )
    root_records = await store.list_development_tool_usage(root_assignment.assignment_id)
    assert root_records[0].command_cwd == "."

    zero_candidate = _assignment(tmp_path)
    zero_cost = zero_candidate.model_copy(
        update={"budget": zero_candidate.budget.model_copy(update={"max_cost_usd": 0})}
    )
    await store.create_assignment(
        zero_cost,
        actor_id="usage-owner",
        idempotency_key="zero-cost-create-assignment-0001",
    )
    assert await store.reserve_development_tool_usage(
        assignment_id=zero_cost.assignment_id,
        actor_role=AgentRole.lilies,
        usage_id="zero-cost-read-0001",
        tool_name="workspace_read",
        request_digest=DIGEST_A,
        command_argv=None,
        command_cwd=None,
    )


@pytest.mark.asyncio
async def test_workspace_tools_use_trusted_meter_and_block_usage_replay(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "tool-wrapper-meter.db")
    await store.initialize()
    assignment = _assignment(tmp_path)
    lilies_root = Path(assignment.workspace_grants[0].workspace_root)
    (lilies_root / "src").mkdir(parents=True)
    (lilies_root / "src/value.txt").write_text("metered\n", encoding="utf-8")
    await store.create_assignment(
        assignment,
        actor_id="usage-owner",
        idempotency_key="wrapper-create-assignment-0001",
    )
    grant = assignment.workspace_grants[0]
    authority = DevelopmentToolAuthority(
        actor_role=AgentRole.lilies,
        workspace_grant=grant,
        enabled_tools=(DevelopmentToolName.workspace_read,),
    )
    tools = DevelopmentWorkspaceTools(
        authority,
        assignment_id=assignment.assignment_id,
        usage_meter=store,
        metering_required=True,
    )
    request = WorkspaceReadRequest(
        path="src/value.txt",
        usage_id="model-tool-use-0001",
    )
    result = await tools.workspace_read(request)
    assert result.content == "metered"
    records = await store.list_development_tool_usage(assignment.assignment_id)
    assert len(records) == 1
    assert records[0].status == "completed"
    assert records[0].commands == 0
    assert records[0].response_digest is not None

    with pytest.raises(DevelopmentToolUsageReplay, match="not execute twice"):
        await tools.workspace_read(request)
    with pytest.raises(DevelopmentToolDenied, match="stable usage_id"):
        await tools.workspace_read(WorkspaceReadRequest(path="src/value.txt"))
    with pytest.raises(
        DevelopmentToolDenied,
        match="usage metering is required",
    ):
        DevelopmentWorkspaceTools(authority, metering_required=True)


@pytest.mark.asyncio
async def test_result_and_review_commands_bind_completed_meter_records_once(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "receipt-binding.db")
    await store.initialize()
    assignment = _assignment(tmp_path)
    await store.create_assignment(
        assignment,
        actor_id="usage-owner",
        idempotency_key="binding-create-assignment-0001",
    )
    proposed = await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-local",
        idempotency_key="binding-create-work-0001",
    )
    dispatched = await store.dispatch_work_item(
        proposed.work_item_id,
        expected_revision=proposed.revision,
        actor_id="usage-owner",
        idempotency_key="binding-dispatch-0001",
    )
    lease = await store.acquire_lease(
        dispatched.work_item_id,
        owner_role=AgentRole.codex,
        owner_id="codex-local",
        expected_revision=dispatched.revision,
        idempotency_key="binding-lease-0001",
    )
    working = await store.start_work(
        lease.lease_id,
        owner_id="codex-local",
        expected_work_item_revision=lease.work_item_revision,
        idempotency_key="binding-start-0001",
    )

    result_started = _now() - timedelta(milliseconds=5)
    assert await store.reserve_development_tool_usage(
        assignment_id=assignment.assignment_id,
        actor_role=AgentRole.codex,
        usage_id="binding-command-result-0001",
        tool_name="process_run",
        request_digest=DIGEST_A,
        command_argv=("pytest", "-q"),
        command_cwd="src",
    )
    await store.complete_development_tool_usage(
        assignment_id=assignment.assignment_id,
        actor_role=AgentRole.codex,
        usage_id="binding-command-result-0001",
        request_digest=DIGEST_A,
        response_digest=DIGEST_B,
        output_digest=DIGEST_A,
    )
    result_finished = _now() + timedelta(milliseconds=5)
    result_command = _command().model_copy(
        update={
            "started_at": result_started,
            "finished_at": result_finished,
        }
    )
    result = _result(assignment, working, lease.lease_id).model_copy(
        update={"commands": (result_command,)}
    )
    ready = await store.submit_result(
        result,
        owner_id="codex-local",
        expected_work_item_revision=working.revision,
        idempotency_key="binding-result-0001",
    )

    review_started = _now() - timedelta(milliseconds=5)
    assert await store.reserve_development_tool_usage(
        assignment_id=assignment.assignment_id,
        actor_role=AgentRole.lilies,
        usage_id="binding-command-review-0001",
        tool_name="process_run",
        request_digest=DIGEST_B,
        command_argv=("pytest", "-q"),
        command_cwd="src",
    )
    await store.complete_development_tool_usage(
        assignment_id=assignment.assignment_id,
        actor_role=AgentRole.lilies,
        usage_id="binding-command-review-0001",
        request_digest=DIGEST_B,
        response_digest=DIGEST_C,
        output_digest=DIGEST_A,
    )
    review_finished = _now() + timedelta(milliseconds=5)
    review_command = _command().model_copy(
        update={
            "started_at": review_started,
            "finished_at": review_finished,
        }
    )
    review = _accepted_review(assignment, ready, result).model_copy(
        update={"verification_commands": (review_command,)}
    )
    accepted = await store.submit_review(
        review,
        reviewer_id="lilies-local",
        expected_work_item_revision=ready.revision,
        idempotency_key="binding-review-0001",
    )
    assert accepted.status == WorkItemStatus.accepted
    records = await store.list_development_tool_usage(assignment.assignment_id)
    assert [
        (record.actor_role, record.consumer_type, record.consumer_id) for record in records
    ] == [
        (AgentRole.codex, "result", result.result_id),
        (AgentRole.lilies, "review", review.review_id),
    ]


@pytest.mark.asyncio
async def test_metering_requirement_never_falls_back_to_self_reported_commands(
    tmp_path: Path,
) -> None:
    database = tmp_path / "metering-required.db"
    store = CollaborativeDevelopmentStore(database)
    await store.initialize()
    assignment = _assignment(tmp_path)
    await store.create_assignment(
        assignment,
        actor_id="usage-owner",
        idempotency_key="metering-required-assignment-0001",
    )
    proposed = await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-local",
        idempotency_key="metering-required-work-0001",
    )
    dispatched = await store.dispatch_work_item(
        proposed.work_item_id,
        expected_revision=proposed.revision,
        actor_id="usage-owner",
        idempotency_key="metering-required-dispatch-0001",
    )
    lease = await store.acquire_lease(
        dispatched.work_item_id,
        owner_role=AgentRole.codex,
        owner_id="codex-local",
        expected_revision=dispatched.revision,
        idempotency_key="metering-required-lease-0001",
    )
    working = await store.start_work(
        lease.lease_id,
        owner_id="codex-local",
        expected_work_item_revision=lease.work_item_revision,
        idempotency_key="metering-required-start-0001",
    )

    await store.require_development_tool_metering(assignment.assignment_id)
    restarted = CollaborativeDevelopmentStore(database)
    await restarted.initialize()
    store = restarted
    assert await store.list_development_tool_usage(assignment.assignment_id) == []
    with pytest.raises(
        CollaborativeDevelopmentConflict,
        match="not bound to a completed trusted usage record",
    ):
        await store.submit_result(
            _result(assignment, working, lease.lease_id),
            owner_id="codex-local",
            expected_work_item_revision=working.revision,
            idempotency_key="metering-required-forged-result-0001",
        )

    result_started = _now() - timedelta(milliseconds=5)
    assert await store.reserve_development_tool_usage(
        assignment_id=assignment.assignment_id,
        actor_role=AgentRole.codex,
        usage_id="metering-required-result-command-0001",
        tool_name="process_run",
        request_digest=DIGEST_A,
        command_argv=("pytest", "-q"),
        command_cwd="src",
    )
    await store.complete_development_tool_usage(
        assignment_id=assignment.assignment_id,
        actor_role=AgentRole.codex,
        usage_id="metering-required-result-command-0001",
        request_digest=DIGEST_A,
        response_digest=DIGEST_B,
        output_digest=DIGEST_A,
    )
    result_finished = _now() + timedelta(milliseconds=5)
    result = _result(assignment, working, lease.lease_id).model_copy(
        update={
            "commands": (
                _command().model_copy(
                    update={
                        "started_at": result_started,
                        "finished_at": result_finished,
                    }
                ),
            )
        }
    )
    ready = await store.submit_result(
        result,
        owner_id="codex-local",
        expected_work_item_revision=working.revision,
        idempotency_key="metering-required-valid-result-0001",
    )

    with pytest.raises(
        CollaborativeDevelopmentConflict,
        match="not bound to a completed trusted usage record",
    ):
        await store.submit_review(
            _accepted_review(assignment, ready, result),
            reviewer_id="lilies-local",
            expected_work_item_revision=ready.revision,
            idempotency_key="metering-required-forged-review-0001",
        )
    assert (
        await store.get_work_item(ready.work_item_id)
    ).status == WorkItemStatus.ready_for_lilies_review


@pytest.mark.asyncio
async def test_review_verification_commands_share_the_assignment_command_budget(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "review-command-budget.db")
    await store.initialize()
    candidate = _assignment(tmp_path)
    assignment = candidate.model_copy(
        update={"budget": candidate.budget.model_copy(update={"max_commands": 1})}
    )
    await store.create_assignment(
        assignment,
        actor_id="usage-owner",
        idempotency_key="review-budget-create-assignment-0001",
    )
    proposed = await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-local",
        idempotency_key="review-budget-create-work-0001",
    )
    dispatched = await store.dispatch_work_item(
        proposed.work_item_id,
        expected_revision=proposed.revision,
        actor_id="usage-owner",
        idempotency_key="review-budget-dispatch-0001",
    )
    lease = await store.acquire_lease(
        dispatched.work_item_id,
        owner_role=AgentRole.codex,
        owner_id="codex-local",
        expected_revision=dispatched.revision,
        idempotency_key="review-budget-lease-0001",
    )
    working = await store.start_work(
        lease.lease_id,
        owner_id="codex-local",
        expected_work_item_revision=lease.work_item_revision,
        idempotency_key="review-budget-start-0001",
    )
    result = _result(assignment, working, lease.lease_id)
    ready = await store.submit_result(
        result,
        owner_id="codex-local",
        expected_work_item_revision=working.revision,
        idempotency_key="review-budget-result-0001",
    )
    with pytest.raises(
        CollaborativeDevelopmentBudgetExceeded,
        match="exceeded by review",
    ):
        await store.submit_review(
            _accepted_review(assignment, ready, result),
            reviewer_id="lilies-local",
            expected_work_item_revision=ready.revision,
            idempotency_key="review-budget-review-0001",
        )
    assert (
        await store.get_work_item(ready.work_item_id)
    ).status == WorkItemStatus.ready_for_lilies_review


@pytest.mark.asyncio
async def test_provider_cost_accepts_only_control_plane_verified_receipts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "provider-cost.db"
    untrusted = CollaborativeDevelopmentStore(database)
    await untrusted.initialize()
    candidate = _assignment(tmp_path)
    assignment = candidate.model_copy(
        update={"budget": candidate.budget.model_copy(update={"max_cost_usd": 1})}
    )
    await untrusted.create_assignment(
        assignment,
        actor_id="usage-owner",
        idempotency_key="provider-cost-create-assignment-0001",
    )
    authorization = TrustedProviderCostAuthorization(
        reservation_id=uuid4(),
        assignment_id=assignment.assignment_id,
        provider="controlled-provider",
        provider_request_id="request-0001",
        model="bounded-model",
        worst_case_cost_usd=0.75,
        evidence_digest=DIGEST_A,
        authorized_at=_now(),
    )
    receipt = TrustedProviderCostReceipt(
        receipt_id="provider-receipt-0001",
        reservation_id=authorization.reservation_id,
        assignment_id=assignment.assignment_id,
        provider="controlled-provider",
        provider_request_id="request-0001",
        model="bounded-model",
        cost_usd=0.25,
        input_tokens=100,
        output_tokens=50,
        evidence_digest=DIGEST_A,
        issued_at=_now(),
    )
    with pytest.raises(
        CollaborativeDevelopmentAuthorizationError,
        match="not trusted",
    ):
        await untrusted.reserve_trusted_provider_cost(authorization)

    trusted = CollaborativeDevelopmentStore(
        database,
        trusted_provider_cost_authorizer=lambda candidate: (
            candidate.provider == "controlled-provider" and candidate.evidence_digest == DIGEST_A
        ),
        trusted_provider_receipt_verifier=lambda candidate: (
            candidate.provider == "controlled-provider" and candidate.evidence_digest == DIGEST_A
        ),
    )
    await trusted.initialize()
    with pytest.raises(
        CollaborativeDevelopmentConflict,
        match="no prior trusted cost reservation",
    ):
        await trusted.record_trusted_provider_cost(receipt)

    assert await trusted.reserve_trusted_provider_cost(authorization)
    assert not await trusted.reserve_trusted_provider_cost(authorization)
    assert await trusted.record_trusted_provider_cost(receipt)
    assert not await trusted.record_trusted_provider_cost(receipt)
    reservations = await trusted.list_trusted_provider_cost_reservations(assignment.assignment_id)
    assert len(reservations) == 1
    assert reservations[0].status == "settled"
    assert reservations[0].receipt == receipt

    retained = authorization.model_copy(
        update={
            "reservation_id": uuid4(),
            "provider_request_id": "request-0002",
        }
    )
    assert await trusted.reserve_trusted_provider_cost(retained)
    over_receipt = receipt.model_copy(
        update={
            "receipt_id": "provider-receipt-0002",
            "reservation_id": retained.reservation_id,
            "provider_request_id": retained.provider_request_id,
            "cost_usd": 0.8,
        }
    )
    with pytest.raises(
        CollaborativeDevelopmentBudgetExceeded,
        match="worst-case reservation",
    ):
        await trusted.record_trusted_provider_cost(over_receipt)

    restarted = CollaborativeDevelopmentStore(
        database,
        trusted_provider_cost_authorizer=lambda _: True,
        trusted_provider_receipt_verifier=lambda _: True,
    )
    await restarted.initialize()
    blocked = authorization.model_copy(
        update={
            "reservation_id": uuid4(),
            "provider_request_id": "request-0003",
            "worst_case_cost_usd": 0.000000001,
        }
    )
    with pytest.raises(
        CollaborativeDevelopmentBudgetExceeded,
        match="cost budget",
    ):
        await restarted.reserve_trusted_provider_cost(blocked)

    zero_candidate = _assignment(tmp_path)
    zero_cost = zero_candidate.model_copy(
        update={"budget": zero_candidate.budget.model_copy(update={"max_cost_usd": 0})}
    )
    await restarted.create_assignment(
        zero_cost,
        actor_id="usage-owner",
        idempotency_key="provider-zero-cost-assignment-0001",
    )
    with pytest.raises(
        CollaborativeDevelopmentBudgetExceeded,
        match="cost budget",
    ):
        await restarted.reserve_trusted_provider_cost(
            authorization.model_copy(
                update={
                    "reservation_id": uuid4(),
                    "assignment_id": zero_cost.assignment_id,
                    "provider_request_id": "zero-cost-request",
                }
            )
        )


@pytest.mark.asyncio
async def test_zero_billed_provider_receipt_releases_only_its_reserved_cap(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(
        tmp_path / "zero-provider-receipt.db",
        trusted_provider_cost_authorizer=lambda _: True,
        trusted_provider_receipt_verifier=lambda _: True,
    )
    await store.initialize()
    candidate = _assignment(tmp_path)
    assignment = candidate.model_copy(
        update={"budget": candidate.budget.model_copy(update={"max_cost_usd": 1})}
    )
    await store.create_assignment(
        assignment,
        actor_id="usage-owner",
        idempotency_key="zero-provider-assignment-0001",
    )
    first_cap = TrustedProviderCostAuthorization(
        reservation_id=uuid4(),
        assignment_id=assignment.assignment_id,
        provider="subscription-provider",
        provider_request_id="subscription-request-0001",
        model="subscription-model",
        worst_case_cost_usd=1,
        evidence_digest=DIGEST_A,
        authorized_at=_now(),
    )
    assert await store.reserve_trusted_provider_cost(first_cap)
    zero_receipt = TrustedProviderCostReceipt(
        receipt_id="subscription-receipt-0001",
        reservation_id=first_cap.reservation_id,
        assignment_id=assignment.assignment_id,
        provider=first_cap.provider,
        provider_request_id=first_cap.provider_request_id,
        model=first_cap.model,
        cost_usd=0,
        input_tokens=1_234,
        output_tokens=567,
        evidence_digest=DIGEST_B,
        issued_at=_now(),
    )
    assert await store.record_trusted_provider_cost(zero_receipt)

    second_cap = first_cap.model_copy(
        update={
            "reservation_id": uuid4(),
            "provider_request_id": "subscription-request-0002",
        }
    )
    assert await store.reserve_trusted_provider_cost(second_cap)
    with pytest.raises(
        CollaborativeDevelopmentBudgetExceeded,
        match="cost budget",
    ):
        await store.reserve_trusted_provider_cost(
            first_cap.model_copy(
                update={
                    "reservation_id": uuid4(),
                    "provider_request_id": "subscription-request-0003",
                    "worst_case_cost_usd": 0.000000001,
                }
            )
        )
    with pytest.raises(ValidationError):
        TrustedProviderCostAuthorization.model_validate(
            {
                **first_cap.model_dump(mode="python"),
                "reservation_id": uuid4(),
                "provider_request_id": "subscription-zero-cap",
                "worst_case_cost_usd": 0,
            }
        )

    reservations = await store.list_trusted_provider_cost_reservations(assignment.assignment_id)
    assert reservations[0].status == "settled"
    assert reservations[0].receipt == zero_receipt
    assert reservations[1].status == "reserved"
