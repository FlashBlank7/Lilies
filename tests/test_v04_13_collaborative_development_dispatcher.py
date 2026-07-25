from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agent_platform.collaborative_development_dispatcher import (
    CollaborativeDevelopmentDispatchJournal,
    CollaborativeDevelopmentDispatcher,
    DispatchInvocationRecord,
    DispatchOutcome,
    DispatchOutcomeStatus,
    RequestedAuthority,
    RoleBoundDispatchContext,
)
from agent_platform.collaborative_development_models import (
    AgentRole,
    AgentRoleGrant,
    DevelopmentAssignment,
    DevelopmentBudget,
    DevelopmentTaskRole,
    DevelopmentWorkItem,
    ExecutionMode,
    SideEffect,
    WorkItemKind,
    WorkspaceGrant,
    utc_now,
)
from agent_platform.collaborative_development_storage import (
    CollaborativeDevelopmentStore,
)


BASELINE = "a" * 40


def _assignment(
    tmp_path: Path,
    *,
    max_commands: int = 20,
    max_tool_calls: int = 100,
    max_cost_usd: float = 5,
) -> DevelopmentAssignment:
    created = utc_now()
    common = {
        "baseline_commit": BASELINE,
        "allowed_paths": ("src", "tests"),
        "allowed_argv": (("python", "-m", "pytest", "-q"),),
        "allowed_side_effects": (
            SideEffect.workspace_write,
            SideEffect.process_execute,
        ),
        "created_at": created,
    }
    return DevelopmentAssignment(
        assignment_id=uuid4(),
        goal="Fix a parser and let Lilies independently review it.",
        software_id="dispatcher-fixture",
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
                workspace_root=str(tmp_path / "lilies"),
                **common,
            ),
            WorkspaceGrant(
                workspace_id=uuid4(),
                agent_role=AgentRole.codex,
                workspace_root=str(tmp_path / "codex"),
                **common,
            ),
        ),
        budget=DevelopmentBudget(
            max_work_items=10,
            max_commands=max_commands,
            max_tool_calls=max_tool_calls,
            max_wall_seconds=3_600,
            max_cost_usd=max_cost_usd,
        ),
        deadline=created + timedelta(hours=1),
        execution_mode=ExecutionMode.autonomous,
        created_at=created,
        updated_at=created,
    )


def _work_item(assignment: DevelopmentAssignment) -> DevelopmentWorkItem:
    created = utc_now()
    return DevelopmentWorkItem(
        work_item_id=uuid4(),
        assignment_id=assignment.assignment_id,
        kind=WorkItemKind.bug,
        objective="Fix parser behavior.",
        acceptance=("Focused test passes.",),
        assigned_role=AgentRole.codex,
        created_at=created,
        updated_at=created,
    )


def test_dispatch_journal_retightens_dynamic_wal_files_after_every_connection(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "private-dispatch" / "dispatch.db"
    journal = CollaborativeDevelopmentDispatchJournal(journal_path)
    journal.initialize()
    keeper = sqlite3.connect(journal_path)
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        invocation = DispatchInvocationRecord(
            outbox_id=uuid4(),
            attempt=1,
            claim_id=uuid4(),
            assignment_id=uuid4(),
            work_item_id=uuid4(),
            state="started",
            started_at=utc_now(),
        )
        assert journal.begin_invocation(invocation)
        files = (
            journal_path,
            Path(f"{journal_path}-wal"),
            Path(f"{journal_path}-shm"),
        )
        assert all(path.exists() for path in files)
        os.chmod(journal_path.parent, 0o777)
        for path in files:
            os.chmod(path, 0o666)

        assert journal.invocation(invocation.outbox_id, 1) == invocation

        assert stat.S_IMODE(journal_path.parent.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    finally:
        keeper.close()


@pytest.mark.asyncio
async def test_autonomous_dispatch_pauses_on_authority_expansion_and_keeps_grant(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "state.db")
    await store.initialize()
    assignment = _assignment(tmp_path)
    await store.create_assignment(
        assignment,
        actor_id="user",
        idempotency_key="dispatcher-assignment-0001",
    )
    await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-agent",
        idempotency_key="dispatcher-work-item-0001",
    )
    original_grants = assignment.workspace_grants

    async def requests_expansion(**_):
        return DispatchOutcome(
            status=DispatchOutcomeStatus.authorization_required,
            detail="The attempted command and network target are not authorized.",
            requested_authority=RequestedAuthority(
                paths=("../outside",),
                argv=(("sh", "-c", "curl https://example.invalid"),),
                hosts=("example.invalid",),
                side_effects=(
                    SideEffect.network_access,
                    SideEffect.external_mutation,
                ),
                reason="Attempted work exceeds the frozen assignment grant.",
            ),
        )

    journal = CollaborativeDevelopmentDispatchJournal(tmp_path / "dispatch.db")
    dispatcher = CollaborativeDevelopmentDispatcher(
        store=store,
        journal=journal,
        handlers={AgentRole.codex: requests_expansion},
    )
    await dispatcher.initialize()
    records = await dispatcher.dispatch_once()

    assert len(records) == 1
    assert records[0].status == DispatchOutcomeStatus.authorization_required
    requests = journal.authorization_requests(assignment.assignment_id)
    assert len(requests) == 1
    assert requests[0].status == "pending"
    assert requests[0].requested_authority.hosts == ("example.invalid",)
    persisted = await store.get_assignment(assignment.assignment_id)
    assert persisted.workspace_grants == original_grants
    assert (await store.list_pending_outbox()) == []

    restarted = CollaborativeDevelopmentDispatchJournal(tmp_path / "dispatch.db")
    restarted.initialize()
    assert restarted.history(assignment.assignment_id) == records
    assert restarted.authorization_requests(assignment.assignment_id) == requests


@pytest.mark.asyncio
async def test_dispatch_delivery_records_exact_grant_digest_once(tmp_path: Path) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "state.db")
    await store.initialize()
    assignment = _assignment(tmp_path)
    await store.create_assignment(
        assignment,
        actor_id="user",
        idempotency_key="dispatcher-assignment-0002",
    )
    await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-agent",
        idempotency_key="dispatcher-work-item-0002",
    )

    async def delivered(**_):
        return DispatchOutcome(
            status=DispatchOutcomeStatus.delivered,
            detail="Codex accepted the bounded work item.",
            evidence_refs=("sha256:" + "b" * 64,),
        )

    journal = CollaborativeDevelopmentDispatchJournal(tmp_path / "dispatch.db")
    dispatcher = CollaborativeDevelopmentDispatcher(
        store=store,
        journal=journal,
        handlers={AgentRole.codex: delivered},
    )
    await dispatcher.initialize()
    first = await dispatcher.dispatch_once()
    second = await dispatcher.dispatch_once()

    assert len(first) == 1
    assert first[0].status == DispatchOutcomeStatus.delivered
    assert second == []
    assert journal.history(assignment.assignment_id) == first


@pytest.mark.asyncio
async def test_embedded_handler_receives_only_its_deep_copied_role_projection(
    tmp_path: Path,
) -> None:
    original = _assignment(tmp_path)
    sensitive_grants: list[WorkspaceGrant] = []
    for grant in original.workspace_grants:
        role = grant.agent_role.value
        sensitive_grants.append(
            WorkspaceGrant.model_validate(
                {
                    **grant.model_dump(mode="python"),
                    "allowed_hosts": (f"{role}.internal.invalid",),
                    "allowed_side_effects": (
                        *grant.allowed_side_effects,
                        SideEffect.network_access,
                    ),
                    "secret_refs": (f"{role}-hidden-authority",),
                }
            )
        )
    assignment = DevelopmentAssignment.model_validate(
        {
            **original.model_dump(mode="python"),
            "workspace_grants": tuple(sensitive_grants),
        }
    )
    store = CollaborativeDevelopmentStore(tmp_path / "state.db")
    await store.initialize()
    await store.create_assignment(
        assignment,
        actor_id="user",
        idempotency_key="role-bound-assignment-0001",
    )
    await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-agent",
        idempotency_key="role-bound-work-item-0001",
    )
    observed_projection: dict[str, object] = {}

    async def probing_handler(*, context: RoleBoundDispatchContext):
        serialized = json.dumps(
            context.assignment.model_dump(mode="json"),
            sort_keys=True,
        )
        observed_projection.update(
            {
                "serialized": serialized,
                "workspace_root": context.workspace_grant.workspace_root,
                "secret_refs": context.workspace_grant.secret_refs,
            }
        )
        assert context.assignment.agent_role.agent_role == AgentRole.codex
        assert not hasattr(context.assignment, "agent_roles")
        assert not hasattr(context.assignment, "workspace_grants")
        assert str(tmp_path / "lilies") not in serialized
        assert "lilies.internal.invalid" not in serialized
        assert "lilies-hidden-authority" not in serialized
        assert "codex.internal.invalid" in serialized
        assert "codex-hidden-authority" in serialized

        # Even mutation of the handler's nested local copy cannot change the
        # trusted assignment or the grant used to bind dispatch history.
        context.workspace_grant.secret_refs = ("handler-local-mutation",)
        return DispatchOutcome(
            status=DispatchOutcomeStatus.delivered,
            detail="The role-scoped handler observed no other-role authority.",
        )

    dispatcher = CollaborativeDevelopmentDispatcher(
        store=store,
        journal=CollaborativeDevelopmentDispatchJournal(tmp_path / "dispatch.db"),
        handlers={AgentRole.codex: probing_handler},
    )
    await dispatcher.initialize()
    records = await dispatcher.dispatch_once()

    assert [record.status for record in records] == [DispatchOutcomeStatus.delivered]
    assert observed_projection["workspace_root"] == str(tmp_path / "codex")
    assert observed_projection["secret_refs"] == ("codex-hidden-authority",)
    persisted = await store.get_assignment(assignment.assignment_id)
    assert persisted.workspace_grants == assignment.workspace_grants
    assert persisted.budget == assignment.budget


@pytest.mark.parametrize(
    ("max_commands", "max_tool_calls", "max_cost_usd"),
    [
        (0, 100, 5),
        (20, 0, 5),
        (20, 100, 0),
    ],
)
@pytest.mark.asyncio
async def test_zero_invocation_authority_never_enqueues_or_calls_handler(
    tmp_path: Path,
    max_commands: int,
    max_tool_calls: int,
    max_cost_usd: float,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "state.db")
    await store.initialize()
    assignment = _assignment(
        tmp_path,
        max_commands=max_commands,
        max_tool_calls=max_tool_calls,
        max_cost_usd=max_cost_usd,
    )
    await store.create_assignment(
        assignment,
        actor_id="user",
        idempotency_key="zero-budget-assignment-0001",
    )
    item = await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-agent",
        idempotency_key="zero-budget-work-item-0001",
    )
    invoked = False

    async def must_not_run(**_):
        nonlocal invoked
        invoked = True
        raise AssertionError("zero-budget handler was invoked")

    dispatcher = CollaborativeDevelopmentDispatcher(
        store=store,
        journal=CollaborativeDevelopmentDispatchJournal(tmp_path / "dispatch.db"),
        handlers={AgentRole.codex: must_not_run},
    )
    await dispatcher.initialize()

    assert item.status.value == "proposed"
    assert await store.list_pending_outbox() == []
    assert await dispatcher.dispatch_once() == []
    assert invoked is False
    assert await store.list_invocation_fences(assignment.assignment_id) == []


@pytest.mark.asyncio
async def test_handler_fences_do_not_consume_the_actual_tool_call_budget(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "state.db")
    await store.initialize()
    assignment = _assignment(tmp_path, max_tool_calls=1)
    await store.create_assignment(
        assignment,
        actor_id="user",
        idempotency_key="cumulative-assignment-0001",
    )
    for index in range(2):
        await store.create_work_item(
            _work_item(assignment),
            actor_role="lilies",
            actor_id="lilies-agent",
            idempotency_key=f"cumulative-work-item-{index:04d}",
        )
    invocations = 0

    async def delivered(**_):
        nonlocal invocations
        invocations += 1
        return DispatchOutcome(
            status=DispatchOutcomeStatus.delivered,
            detail="The bounded role handler accepted one work item.",
        )

    dispatcher = CollaborativeDevelopmentDispatcher(
        store=store,
        journal=CollaborativeDevelopmentDispatchJournal(tmp_path / "dispatch.db"),
        handlers={AgentRole.codex: delivered},
    )
    await dispatcher.initialize()
    records = await dispatcher.dispatch_once(limit=10)

    assert invocations == 2
    assert len(records) == 2
    assert {record.status for record in records} == {
        DispatchOutcomeStatus.delivered,
    }
    fences = await store.list_invocation_fences(assignment.assignment_id)
    assert len(fences) == 2
    assert await store.list_development_tool_usage(assignment.assignment_id) == []


@pytest.mark.asyncio
async def test_retry_and_process_restart_get_distinct_budget_neutral_fences(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.db"
    journal_path = tmp_path / "dispatch.db"
    store = CollaborativeDevelopmentStore(database_path)
    await store.initialize()
    assignment = _assignment(tmp_path, max_tool_calls=1)
    await store.create_assignment(
        assignment,
        actor_id="user",
        idempotency_key="restart-budget-assignment-0001",
    )
    await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-agent",
        idempotency_key="restart-budget-work-item-0001",
    )
    invocations = 0

    async def retrying_handler(**_):
        nonlocal invocations
        invocations += 1
        return DispatchOutcome(
            status=DispatchOutcomeStatus.retry,
            detail="The role runtime requested one bounded retry.",
            retry_after_seconds=1,
        )

    first_dispatcher = CollaborativeDevelopmentDispatcher(
        store=store,
        journal=CollaborativeDevelopmentDispatchJournal(journal_path),
        handlers={AgentRole.codex: retrying_handler},
    )
    await first_dispatcher.initialize()
    first = await first_dispatcher.dispatch_once()
    assert [record.status for record in first] == [DispatchOutcomeStatus.retry]
    assert invocations == 1

    await asyncio.sleep(1.05)
    restarted_store = CollaborativeDevelopmentStore(database_path)
    restarted_dispatcher = CollaborativeDevelopmentDispatcher(
        store=restarted_store,
        journal=CollaborativeDevelopmentDispatchJournal(journal_path),
        handlers={AgentRole.codex: retrying_handler},
    )
    await restarted_dispatcher.initialize()
    second = await restarted_dispatcher.dispatch_once()

    assert [record.status for record in second] == [DispatchOutcomeStatus.retry]
    assert invocations == 2
    fences = await restarted_store.list_invocation_fences(
        assignment.assignment_id
    )
    assert [fence.attempt for fence in fences] == [1, 2]
    assert (
        await restarted_store.list_development_tool_usage(
            assignment.assignment_id
        )
        == []
    )


@pytest.mark.asyncio
async def test_concurrent_dispatchers_fence_each_distinct_outbox_without_fake_usage(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.db"
    first_store = CollaborativeDevelopmentStore(database_path)
    await first_store.initialize()
    assignment = _assignment(tmp_path, max_tool_calls=1)
    await first_store.create_assignment(
        assignment,
        actor_id="user",
        idempotency_key="concurrent-budget-assignment-0001",
    )
    for index in range(2):
        await first_store.create_work_item(
            _work_item(assignment),
            actor_role="lilies",
            actor_id="lilies-agent",
            idempotency_key=f"concurrent-budget-work-item-{index:04d}",
        )

    second_store = CollaborativeDevelopmentStore(database_path)
    await second_store.initialize()
    invocations = 0

    async def delivered(**_):
        nonlocal invocations
        invocations += 1
        await asyncio.sleep(0.05)
        return DispatchOutcome(
            status=DispatchOutcomeStatus.delivered,
            detail="The single reserved handler call completed.",
        )

    first_dispatcher = CollaborativeDevelopmentDispatcher(
        store=first_store,
        journal=CollaborativeDevelopmentDispatchJournal(tmp_path / "dispatch-a.db"),
        handlers={AgentRole.codex: delivered},
        dispatcher_id="dispatcher-a",
    )
    second_dispatcher = CollaborativeDevelopmentDispatcher(
        store=second_store,
        journal=CollaborativeDevelopmentDispatchJournal(tmp_path / "dispatch-b.db"),
        handlers={AgentRole.codex: delivered},
        dispatcher_id="dispatcher-b",
    )
    await asyncio.gather(first_dispatcher.initialize(), second_dispatcher.initialize())
    batches = await asyncio.gather(
        first_dispatcher.dispatch_once(limit=1),
        second_dispatcher.dispatch_once(limit=1),
    )

    assert invocations == 2
    assert sum(len(batch) for batch in batches) == 2
    fences = await first_store.list_invocation_fences(
        assignment.assignment_id
    )
    assert len(fences) == 2
    assert (
        await first_store.list_development_tool_usage(
            assignment.assignment_id
        )
        == []
    )


@pytest.mark.asyncio
async def test_same_outbox_attempt_replay_reuses_durable_invocation_fence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.db"
    store = CollaborativeDevelopmentStore(database_path)
    await store.initialize()
    assignment = _assignment(tmp_path, max_tool_calls=2)
    await store.create_assignment(
        assignment,
        actor_id="user",
        idempotency_key="replay-budget-assignment-0001",
    )
    await store.create_work_item(
        _work_item(assignment),
        actor_role="lilies",
        actor_id="lilies-agent",
        idempotency_key="replay-budget-work-item-0001",
    )
    claims = await store.claim_pending_outbox(
        claimed_by="replay-budget-probe",
        limit=1,
    )
    assert len(claims) == 1
    claim = claims[0]

    first = await store.acquire_dispatch_invocation_fence(
        assignment_id=assignment.assignment_id,
        outbox_id=claim.outbox.outbox_id,
        attempt=1,
        claim_id=claim.claim_id,
    )
    replay = await store.acquire_dispatch_invocation_fence(
        assignment_id=assignment.assignment_id,
        outbox_id=claim.outbox.outbox_id,
        attempt=1,
        claim_id=claim.claim_id,
    )

    restarted_store = CollaborativeDevelopmentStore(database_path)
    await restarted_store.initialize()
    restarted_replay = await restarted_store.acquire_dispatch_invocation_fence(
        assignment_id=assignment.assignment_id,
        outbox_id=claim.outbox.outbox_id,
        attempt=1,
        claim_id=claim.claim_id,
    )

    assert first.acquired is True
    assert replay.acquired is False
    assert restarted_replay.acquired is False
    assert replay.fence == first.fence
    assert restarted_replay.fence == first.fence
    fences = await restarted_store.list_invocation_fences(
        assignment.assignment_id
    )
    assert fences == [first.fence]
