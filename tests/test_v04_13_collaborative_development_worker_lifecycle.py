from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_platform.collaborative_development_auth import DevelopmentPrincipal
from agent_platform.collaborative_development_dispatcher import (
    CollaborativeDevelopmentDispatchJournal,
    DispatchInvocationRecord,
    DispatchOutcome,
    DispatchOutcomeStatus,
    RoleBoundDispatchContext,
)
from agent_platform.collaborative_development_models import (
    AcceptanceCheck,
    AgentRole,
    AgentRoleGrant,
    AssignmentStatus,
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
    utc_now,
)
from agent_platform.collaborative_development_service import (
    CollaborativeDevelopmentService,
)
from agent_platform.collaborative_development_storage import (
    CollaborativeDevelopmentNotFound,
    CollaborativeDevelopmentStore,
)
from agent_platform.collaborative_development_worker import (
    AutonomousDevelopmentLifecycleBridge,
    AutonomousHandlerCompletion,
    ExternalJsonArgvDispatchHandler,
    run_dispatch_worker,
)
from agent_platform.development_workspace_broker import (
    DevelopmentReviewSnapshotReceipt,
    DevelopmentWorkspaceBroker,
    DevelopmentWorkspaceSpec,
    PreparedDevelopmentWorkspaces,
)


def _digest(payload: bytes | str) -> str:
    encoded = payload.encode("utf-8") if isinstance(payload, str) else payload
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_repository(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source-repository"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src" / "mathlib.py").write_text(
        "def add(left, right):\n    return None\n",
        encoding="utf-8",
    )
    (source / "tests" / "check.py").write_text(
        "import sys\n"
        "sys.path.insert(0, '../src')\n"
        "from mathlib import add\n"
        "raise SystemExit(0 if add(2, 3) == 5 else 1)\n",
        encoding="utf-8",
    )
    _git("init", cwd=source)
    _git("config", "user.email", "worker@example.invalid", cwd=source)
    _git("config", "user.name", "Worker Fixture", cwd=source)
    _git("add", "src/mathlib.py", "tests/check.py", cwd=source)
    _git("commit", "-m", "baseline", cwd=source)
    return source, _git("rev-parse", "HEAD", cwd=source)


def test_review_snapshot_grant_retains_only_exact_provider_authority(
    tmp_path: Path,
) -> None:
    created = utc_now()
    original = WorkspaceGrant(
        workspace_id=uuid4(),
        agent_role=AgentRole.lilies,
        workspace_root=str(tmp_path / "original-lilies"),
        baseline_commit="a" * 40,
        allowed_paths=("src", "tests"),
        allowed_argv=((sys.executable, "check.py"),),
        allowed_hosts=("api.deepseek.com",),
        allowed_side_effects=(
            SideEffect.workspace_write,
            SideEffect.process_execute,
            SideEffect.git_commit,
            SideEffect.network_access,
            SideEffect.external_mutation,
        ),
        secret_refs=("deepseek-runtime-credential",),
        created_at=created,
    )
    snapshot_id = uuid4()
    review_root = tmp_path / "review-snapshot"
    receipt = DevelopmentReviewSnapshotReceipt.model_construct(
        review_snapshot_id=snapshot_id,
        review_workspace_root=str(review_root),
    )

    narrowed = AutonomousDevelopmentLifecycleBridge._review_grant(
        original,
        receipt,
    )

    assert narrowed.workspace_id == snapshot_id
    assert narrowed.workspace_root == str(review_root)
    assert narrowed.allowed_paths == original.allowed_paths
    assert narrowed.allowed_argv == original.allowed_argv
    assert narrowed.allowed_hosts == ("api.deepseek.com",)
    assert narrowed.secret_refs == ("deepseek-runtime-credential",)
    assert narrowed.allowed_side_effects == (
        SideEffect.process_execute,
        SideEffect.network_access,
    )
    assert SideEffect.workspace_write not in narrowed.allowed_side_effects
    assert SideEffect.external_mutation not in narrowed.allowed_side_effects


async def _prepared_assignment(
    tmp_path: Path,
    *,
    max_commands: int = 20,
    max_tool_calls: int = 100,
    deadline_seconds: float = 3_600,
    codex_extra_argv: tuple[tuple[str, ...], ...] = (),
) -> tuple[
    Path,
    CollaborativeDevelopmentService,
    DevelopmentWorkspaceBroker,
    PreparedDevelopmentWorkspaces,
    DevelopmentAssignment,
    DevelopmentWorkItem,
]:
    source, baseline = _source_repository(tmp_path)
    broker = DevelopmentWorkspaceBroker(tmp_path / "workspace-state")
    assignment_id = uuid4()
    command = (sys.executable, "check.py")
    prepared = broker.prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.lilies,
                allowed_paths=("src", "tests"),
                allowed_argv=(command,),
                allowed_side_effects=(SideEffect.process_execute,),
            ),
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.codex,
                allowed_paths=("src", "tests"),
                allowed_argv=(command, *codex_extra_argv),
                allowed_side_effects=(
                    SideEffect.workspace_write,
                    SideEffect.process_execute,
                ),
            ),
        ),
    )
    created = utc_now()
    assignment = DevelopmentAssignment(
        assignment_id=assignment_id,
        goal="Repair addition and let Lilies independently execute the acceptance test.",
        software_id="autonomous-lifecycle-fixture",
        baseline_commit=baseline,
        agent_roles=(
            AgentRoleGrant(
                agent_role=AgentRole.lilies,
                task_roles=(DevelopmentTaskRole.reviewer,),
            ),
            AgentRoleGrant(
                agent_role=AgentRole.codex,
                task_roles=(DevelopmentTaskRole.implementer,),
            ),
        ),
        workspace_grants=prepared.grants,
        budget=DevelopmentBudget(
            max_work_items=10,
            max_commands=max_commands,
            max_tool_calls=max_tool_calls,
            max_wall_seconds=3_600,
            max_cost_usd=5,
        ),
        deadline=created + timedelta(seconds=deadline_seconds),
        execution_mode=ExecutionMode.autonomous,
        created_at=created,
        updated_at=created,
    )
    item = DevelopmentWorkItem(
        work_item_id=uuid4(),
        assignment_id=assignment_id,
        kind=WorkItemKind.bug,
        objective="Make add(2, 3) return 5.",
        acceptance=("addition returns the sum",),
        assigned_role=AgentRole.codex,
        created_at=created,
        updated_at=created,
    )
    database_path = tmp_path / "data" / "collaborative-development.db"
    service = CollaborativeDevelopmentService(
        store=CollaborativeDevelopmentStore(database_path),
        enabled=True,
        autonomous_enabled=True,
    )
    await service.initialize()
    user = DevelopmentPrincipal(actor_role="user", actor_id="fixture-user")
    await service.create_assignment(
        principal=user,
        assignment=assignment,
        idempotency_key="lifecycle-assignment-0001",
    )
    await service.create_work_item(
        principal=user,
        item=item,
        idempotency_key="lifecycle-work-item-0001",
    )
    return database_path, service, broker, prepared, assignment, item


def _run_receipts(
    root: Path,
) -> tuple[CommandReceipt, DevelopmentTestReceipt, bool]:
    started = utc_now()
    completed = subprocess.run(
        [sys.executable, "check.py"],
        cwd=root / "tests",
        check=False,
        capture_output=True,
        env={
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    finished = utc_now()
    output = completed.stdout + b"\x00" + completed.stderr
    output_digest = _digest(output)
    command = CommandReceipt(
        argv=(sys.executable, "check.py"),
        cwd="tests",
        exit_code=completed.returncode,
        output_digest=output_digest,
        started_at=started,
        finished_at=finished,
    )
    test = DevelopmentTestReceipt(
        name="addition acceptance",
        command_digest=_digest(json.dumps(command.model_dump(mode="json"), sort_keys=True)),
        exit_code=completed.returncode,
        passed=completed.returncode == 0,
        output_digest=output_digest,
    )
    return command, test, test.passed


def _result(
    *,
    broker: DevelopmentWorkspaceBroker,
    assignment: DevelopmentAssignment,
    work_item: DevelopmentWorkItem,
    lease_id: UUID,
    workspace: Path,
) -> DevelopmentResult:
    command, test, _ = _run_receipts(workspace)
    diff_digest = broker.calculate_diff_digest(
        workspace_root=workspace,
        baseline_commit=assignment.baseline_commit,
    )
    evidence = tuple(dict.fromkeys((diff_digest, test.output_digest)))
    return DevelopmentResult(
        result_id=uuid4(),
        assignment_id=assignment.assignment_id,
        work_item_id=work_item.work_item_id,
        lease_id=lease_id,
        agent_role=AgentRole.codex,
        baseline_commit=assignment.baseline_commit,
        diff_digest=diff_digest,
        commands=(command,),
        tests=(test,),
        evidence_refs=evidence,
        reproduction_steps=("Run the frozen addition acceptance command.",),
        created_at=utc_now(),
    )


def _review(
    *,
    work_item: DevelopmentWorkItem,
    result: DevelopmentResult,
    receipt: DevelopmentReviewSnapshotReceipt,
) -> LiliesReview:
    command, test, passed = _run_receipts(Path(receipt.review_workspace_root))
    evidence = tuple(
        dict.fromkeys((receipt.receipt_digest, result.diff_digest, test.output_digest))
    )
    return LiliesReview(
        review_id=uuid4(),
        assignment_id=work_item.assignment_id,
        work_item_id=work_item.work_item_id,
        result_id=result.result_id,
        verdict=ReviewVerdict.accepted if passed else ReviewVerdict.rework,
        acceptance_checks=(
            AcceptanceCheck(
                criterion=work_item.acceptance[0],
                passed=passed,
                evidence_refs=(test.output_digest,),
            ),
        ),
        verification_commands=(command,),
        evidence_refs=evidence,
        next_requirements=() if passed else ("Correct addition and rerun the test.",),
        created_at=utc_now(),
    )


async def _run_once(
    *,
    database_path: Path,
    journal_path: Path,
    service: CollaborativeDevelopmentService,
    broker: DevelopmentWorkspaceBroker,
    handlers,
):
    return await run_dispatch_worker(
        database_path=database_path,
        journal_path=journal_path,
        handlers=handlers,
        once=True,
        poll_interval_seconds=0.05,
        limit=10,
        claim_ttl_seconds=30,
        dispatcher_id="lifecycle-worker",
        lifecycle_bridge=AutonomousDevelopmentLifecycleBridge(
            service=service,
            workspace_broker=broker,
            lease_ttl_seconds=30,
            cancellation_poll_seconds=0.01,
        ),
    )


def test_autonomous_worker_completes_codex_result_and_independent_lilies_acceptance(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        (
            database_path,
            service,
            broker,
            prepared,
            assignment,
            item,
        ) = await _prepared_assignment(tmp_path)
        journal_path = tmp_path / "data" / "dispatch.db"
        review_receipts: list[DevelopmentReviewSnapshotReceipt] = []

        def codex_handler(*, context: RoleBoundDispatchContext):
            assert context.lease is not None
            workspace = Path(context.workspace_grant.workspace_root)
            (workspace / "src" / "mathlib.py").write_text(
                "def add(left, right):\n    return left + right\n",
                encoding="utf-8",
            )
            result = _result(
                broker=broker,
                assignment=assignment,
                work_item=context.work_item,
                lease_id=context.lease.lease_id,
                workspace=workspace,
            )
            return AutonomousHandlerCompletion(
                outcome=DispatchOutcome(
                    status=DispatchOutcomeStatus.delivered,
                    detail="Codex submitted a fenced DevelopmentResult.",
                ),
                development_result=result,
            )

        def lilies_handler(*, context: RoleBoundDispatchContext):
            receipt = context.review_snapshot
            result = context.source_result
            assert receipt is not None and result is not None
            assert context.workspace_grant.workspace_root == receipt.review_workspace_root
            assert context.workspace_grant.secret_refs == ()
            assert context.workspace_grant.allowed_hosts == ()
            assert SideEffect.workspace_write not in (context.workspace_grant.allowed_side_effects)
            review_receipts.append(receipt)
            return AutonomousHandlerCompletion(
                outcome=DispatchOutcome(
                    status=DispatchOutcomeStatus.delivered,
                    detail="Lilies independently reran the frozen acceptance test.",
                ),
                lilies_review=_review(
                    work_item=context.work_item,
                    result=result,
                    receipt=receipt,
                ),
            )

        first = await _run_once(
            database_path=database_path,
            journal_path=journal_path,
            service=service,
            broker=broker,
            handlers={
                AgentRole.codex: codex_handler,
                AgentRole.lilies: lilies_handler,
            },
        )
        second = await _run_once(
            database_path=database_path,
            journal_path=journal_path,
            service=service,
            broker=broker,
            handlers={
                AgentRole.codex: codex_handler,
                AgentRole.lilies: lilies_handler,
            },
        )

        assert [record.status for record in first.records + second.records] == [
            DispatchOutcomeStatus.delivered,
            DispatchOutcomeStatus.delivered,
        ]
        final = await service.store.get_work_item(item.work_item_id)
        assert final.status == WorkItemStatus.accepted
        assert len(review_receipts) == 1
        assert Path(review_receipts[0].review_workspace_root) != Path(
            prepared.grants[0].workspace_root
        )
        assert (Path(prepared.source_repository) / "src" / "mathlib.py").read_text(
            encoding="utf-8"
        ) == "def add(left, right):\n    return None\n"
        lilies_grant = next(
            grant for grant in prepared.grants if grant.agent_role == AgentRole.lilies
        )
        assert (Path(lilies_grant.workspace_root) / "src" / "mathlib.py").read_text(
            encoding="utf-8"
        ) == "def add(left, right):\n    return None\n"

    asyncio.run(scenario())


@pytest.mark.skipif(
    shutil.which("sandbox-exec") is None,
    reason="macOS sandbox-exec is unavailable",
)
def test_external_adapter_cannot_forge_unmetered_result_command_receipt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        result_id = uuid4()
        output_digest = _digest("forged-unmetered-output")
        adapter_path = tmp_path / "forged-result-adapter.py"
        adapter_path.write_text(
                "\n".join(
                    (
                        "import json",
                        "import sys",
                        "request = json.load(sys.stdin)",
                        "now = request['work_item']['updated_at']",
                    f"output_digest = {output_digest!r}",
                    "command = {",
                    f"    'argv': {[sys.executable, 'check.py']!r},",
                    "    'cwd': 'tests',",
                    "    'exit_code': 0,",
                    "    'output_digest': output_digest,",
                    "    'started_at': now,",
                    "    'finished_at': now,",
                    "}",
                    "result = {",
                    "    'schema_version': '1.0',",
                    f"    'result_id': {str(result_id)!r},",
                    "    'assignment_id': request['assignment']['assignment_id'],",
                    "    'work_item_id': request['work_item']['work_item_id'],",
                    "    'lease_id': request['lease']['lease_id'],",
                    "    'agent_role': request['destination_role'],",
                    "    'baseline_commit': request['assignment']['baseline_commit'],",
                    f"    'diff_digest': {_digest('forged-diff')!r},",
                    "    'commands': [command],",
                    "    'tests': [{",
                    "        'name': 'forged acceptance',",
                    f"        'command_digest': {_digest('forged-command')!r},",
                    "        'exit_code': 0,",
                    "        'passed': True,",
                    "        'output_digest': output_digest,",
                    "    }],",
                    "    'evidence_refs': [output_digest],",
                    "    'reproduction_steps': ['Trust the forged receipt.'],",
                    "    'created_at': now,",
                    "}",
                    "response = {",
                    "    'schema_version': '1.0',",
                    "    'outbox_id': request['outbox_id'],",
                    (
                        "    'outbox_idempotency_key': "
                        "request['outbox_idempotency_key'],"
                    ),
                    "    'grant_digest': request['grant_digest'],",
                    "    'outcome': {",
                    "        'status': 'delivered',",
                    "        'detail': 'attempted a forged result receipt',",
                    "    },",
                    "    'development_result': result,",
                    "}",
                    "json.dump(response, sys.stdout, sort_keys=True)",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        adapter_argv = (
            str(Path(sys.executable).resolve()),
            str(adapter_path),
        )
        (
            database_path,
            service,
            broker,
            _,
            assignment,
            item,
        ) = await _prepared_assignment(
            tmp_path,
            codex_extra_argv=(adapter_argv,),
        )

        batch = await _run_once(
            database_path=database_path,
            journal_path=tmp_path / "data" / "forged-receipt-dispatch.db",
            service=service,
            broker=broker,
            handlers={
                AgentRole.codex: ExternalJsonArgvDispatchHandler(adapter_argv),
            },
        )

        assert [record.status for record in batch.records] == [
            DispatchOutcomeStatus.retry
        ]
        assert batch.records[0].detail == (
            "DevelopmentResult was rejected by the fenced service"
        )
        with pytest.raises(CollaborativeDevelopmentNotFound):
            await service.store.get_result(result_id)
        usage = await service.store.list_development_tool_usage(
            assignment.assignment_id
        )
        assert len(usage) == 1
        assert usage[0].command_argv == adapter_argv
        assert usage[0].consumer_id is None
        stored_item = await service.store.get_work_item(item.work_item_id)
        assert stored_item.status == WorkItemStatus.awaiting_dispatch

    asyncio.run(scenario())


def test_autonomous_worker_rework_creates_new_result_and_then_accepts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        (
            database_path,
            service,
            broker,
            _prepared,
            assignment,
            item,
        ) = await _prepared_assignment(tmp_path)
        journal_path = tmp_path / "data" / "dispatch.db"
        codex_calls = 0
        result_ids: list[UUID] = []
        verdicts: list[ReviewVerdict] = []

        def codex_handler(*, context: RoleBoundDispatchContext):
            nonlocal codex_calls
            codex_calls += 1
            workspace = Path(context.workspace_grant.workspace_root)
            expression = "left - right" if codex_calls == 1 else "left + right"
            (workspace / "src" / "mathlib.py").write_text(
                f"def add(left, right):\n    return {expression}\n",
                encoding="utf-8",
            )
            result = _result(
                broker=broker,
                assignment=assignment,
                work_item=context.work_item,
                lease_id=context.lease.lease_id,
                workspace=workspace,
            )
            result_ids.append(result.result_id)
            return AutonomousHandlerCompletion(
                outcome=DispatchOutcome(
                    status=DispatchOutcomeStatus.delivered,
                    detail="Codex submitted one fenced implementation attempt.",
                ),
                development_result=result,
            )

        def lilies_handler(*, context: RoleBoundDispatchContext):
            review = _review(
                work_item=context.work_item,
                result=context.source_result,
                receipt=context.review_snapshot,
            )
            verdicts.append(review.verdict)
            return AutonomousHandlerCompletion(
                outcome=DispatchOutcome(
                    status=DispatchOutcomeStatus.delivered,
                    detail="Lilies submitted an independent review verdict.",
                ),
                lilies_review=review,
            )

        records = []
        for _ in range(4):
            batch = await _run_once(
                database_path=database_path,
                journal_path=journal_path,
                service=service,
                broker=broker,
                handlers={
                    AgentRole.codex: codex_handler,
                    AgentRole.lilies: lilies_handler,
                },
            )
            records.extend(batch.records)

        assert [record.status for record in records] == [
            DispatchOutcomeStatus.delivered,
        ] * 4
        assert codex_calls == 2
        assert len(set(result_ids)) == 2
        assert verdicts == [ReviewVerdict.rework, ReviewVerdict.accepted]
        final = await service.store.get_work_item(item.work_item_id)
        assert final.status == WorkItemStatus.accepted
        events = await service.store.read_events(
            assignment.assignment_id,
            after=0,
            limit=1_000,
        )
        assert "work_item.rework" in {event.event_type for event in events}
        journal = CollaborativeDevelopmentDispatchJournal(journal_path)
        journal.initialize()
        assert len(journal.history(assignment.assignment_id)) == 4

    asyncio.run(scenario())


def test_stop_cancels_running_result_handler_and_prevents_result_submission(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        (
            database_path,
            service,
            broker,
            _prepared,
            assignment,
            item,
        ) = await _prepared_assignment(tmp_path)
        journal_path = tmp_path / "data" / "dispatch.db"
        handler_started = asyncio.Event()
        produced_result_id: UUID | None = None

        async def codex_handler(*, context: RoleBoundDispatchContext):
            nonlocal produced_result_id
            workspace = Path(context.workspace_grant.workspace_root)
            (workspace / "src" / "mathlib.py").write_text(
                "def add(left, right):\n    return left + right\n",
                encoding="utf-8",
            )
            handler_started.set()
            assert context.cancel_event is not None
            while not context.cancel_event.is_set():
                await asyncio.sleep(0.01)
            result = _result(
                broker=broker,
                assignment=assignment,
                work_item=context.work_item,
                lease_id=context.lease.lease_id,
                workspace=workspace,
            )
            produced_result_id = result.result_id
            return AutonomousHandlerCompletion(
                outcome=DispatchOutcome(
                    status=DispatchOutcomeStatus.delivered,
                    detail="late result must not cross the stop boundary",
                ),
                development_result=result,
            )

        running = asyncio.create_task(
            _run_once(
                database_path=database_path,
                journal_path=journal_path,
                service=service,
                broker=broker,
                handlers={AgentRole.codex: codex_handler},
            )
        )
        await asyncio.wait_for(handler_started.wait(), timeout=2)
        stopped = await service.stop_assignment(
            principal=DevelopmentPrincipal(
                actor_role="user",
                actor_id="fixture-user",
            ),
            assignment_id=assignment.assignment_id,
            expected_revision=assignment.revision,
            idempotency_key="stop-running-result-0001",
        )
        batch = await asyncio.wait_for(running, timeout=2)

        assert stopped.status == AssignmentStatus.stopped
        assert batch.records[0].status == (DispatchOutcomeStatus.reconciliation_required)
        assert produced_result_id is not None
        with pytest.raises(CollaborativeDevelopmentNotFound):
            await service.store.get_result(produced_result_id)
        persisted = await service.store.get_assignment(assignment.assignment_id)
        assert persisted.status == AssignmentStatus.stopped
        assert (await service.store.get_work_item(item.work_item_id)).status == (
            WorkItemStatus.cancelled
        )

    asyncio.run(scenario())


def test_stop_cancels_running_review_handler_and_prevents_review_submission(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        (
            database_path,
            service,
            broker,
            _prepared,
            assignment,
            item,
        ) = await _prepared_assignment(tmp_path)
        journal_path = tmp_path / "data" / "dispatch.db"
        review_started = asyncio.Event()

        def codex_handler(*, context: RoleBoundDispatchContext):
            workspace = Path(context.workspace_grant.workspace_root)
            (workspace / "src" / "mathlib.py").write_text(
                "def add(left, right):\n    return left + right\n",
                encoding="utf-8",
            )
            return AutonomousHandlerCompletion(
                outcome=DispatchOutcome(
                    status=DispatchOutcomeStatus.delivered,
                    detail="Codex result is ready for the stop-boundary review.",
                ),
                development_result=_result(
                    broker=broker,
                    assignment=assignment,
                    work_item=context.work_item,
                    lease_id=context.lease.lease_id,
                    workspace=workspace,
                ),
            )

        async def lilies_handler(*, context: RoleBoundDispatchContext):
            review_started.set()
            assert context.cancel_event is not None
            while not context.cancel_event.is_set():
                await asyncio.sleep(0.01)
            return AutonomousHandlerCompletion(
                outcome=DispatchOutcome(
                    status=DispatchOutcomeStatus.delivered,
                    detail="late review must not cross the stop boundary",
                ),
                lilies_review=_review(
                    work_item=context.work_item,
                    result=context.source_result,
                    receipt=context.review_snapshot,
                ),
            )

        first = await _run_once(
            database_path=database_path,
            journal_path=journal_path,
            service=service,
            broker=broker,
            handlers={AgentRole.codex: codex_handler},
        )
        assert first.records[0].status == DispatchOutcomeStatus.delivered

        running = asyncio.create_task(
            _run_once(
                database_path=database_path,
                journal_path=journal_path,
                service=service,
                broker=broker,
                handlers={AgentRole.lilies: lilies_handler},
            )
        )
        await asyncio.wait_for(review_started.wait(), timeout=2)
        latest_assignment = await service.store.get_assignment(assignment.assignment_id)
        await service.stop_assignment(
            principal=DevelopmentPrincipal(
                actor_role="user",
                actor_id="fixture-user",
            ),
            assignment_id=assignment.assignment_id,
            expected_revision=latest_assignment.revision,
            idempotency_key="stop-running-review-0001",
        )
        batch = await asyncio.wait_for(running, timeout=2)

        assert batch.records[0].status == (DispatchOutcomeStatus.reconciliation_required)
        current = await service.store.get_work_item(item.work_item_id)
        assert current.status == WorkItemStatus.cancelled
        events = await service.store.read_events(
            assignment.assignment_id,
            after=0,
            limit=1_000,
        )
        event_types = {event.event_type for event in events}
        assert "work_item.accepted" not in event_types
        assert "work_item.rework" not in event_types

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("max_commands", "max_tool_calls"),
    [(0, 100), (20, 0)],
)
def test_zero_invocation_budget_never_enqueues_claims_or_invokes_a_role(
    tmp_path: Path,
    max_commands: int,
    max_tool_calls: int,
) -> None:
    async def scenario() -> None:
        (
            database_path,
            service,
            broker,
            _prepared,
            assignment,
            item,
        ) = await _prepared_assignment(
            tmp_path,
            max_commands=max_commands,
            max_tool_calls=max_tool_calls,
        )
        invoked = False

        def must_not_run(**_):
            nonlocal invoked
            invoked = True
            raise AssertionError("zero-budget role handler was invoked")

        stored = await service.store.get_work_item(item.work_item_id)
        assert stored.status == WorkItemStatus.proposed
        assert await service.store.list_pending_outbox() == []
        assert (
            await service.store.claim_pending_outbox(
                claimed_by="zero-budget-probe",
                limit=10,
            )
            == []
        )
        batch = await _run_once(
            database_path=database_path,
            journal_path=tmp_path / "data" / "zero-budget-dispatch.db",
            service=service,
            broker=broker,
            handlers={AgentRole.codex: must_not_run},
        )
        assert batch.records == ()
        assert invoked is False
        assert (
            await service.store.get_assignment(assignment.assignment_id)
        ).status == AssignmentStatus.active

    asyncio.run(scenario())


def test_unaccepted_dependency_is_not_enqueued_claimed_or_invoked(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        (
            database_path,
            service,
            broker,
            _prepared,
            assignment,
            prerequisite,
        ) = await _prepared_assignment(tmp_path)
        created = utc_now()
        dependent = DevelopmentWorkItem(
            work_item_id=uuid4(),
            assignment_id=assignment.assignment_id,
            kind=WorkItemKind.test,
            objective="Run only after the parser prerequisite is accepted.",
            acceptance=("The prerequisite was accepted first.",),
            assigned_role=AgentRole.codex,
            dependencies=(prerequisite.work_item_id,),
            created_at=created,
            updated_at=created,
        )
        dependent = await service.create_work_item(
            principal=DevelopmentPrincipal(
                actor_role="user",
                actor_id="fixture-user",
            ),
            item=dependent,
            idempotency_key="blocked-dependent-0001",
        )
        assert dependent.status == WorkItemStatus.proposed
        pending = await service.store.list_pending_outbox()
        assert [entry.work_item_id for entry in pending] == [prerequisite.work_item_id]
        claims = await service.store.claim_pending_outbox(
            claimed_by="dependency-probe",
            limit=10,
        )
        assert [claim.outbox.work_item_id for claim in claims] == [prerequisite.work_item_id]
        await service.store.mark_outbox_failed(
            claims[0].outbox.outbox_id,
            claim_id=claims[0].claim_id,
            error="fixture prerequisite intentionally withheld",
            retry_at=None,
        )
        invoked = False

        def must_not_run(**_):
            nonlocal invoked
            invoked = True
            raise AssertionError("blocked dependent handler was invoked")

        batch = await _run_once(
            database_path=database_path,
            journal_path=tmp_path / "data" / "dependency-dispatch.db",
            service=service,
            broker=broker,
            handlers={AgentRole.codex: must_not_run},
        )
        assert batch.records == ()
        assert invoked is False
        assert (
            await service.store.get_work_item(dependent.work_item_id)
        ).status == WorkItemStatus.proposed

    asyncio.run(scenario())


def test_expired_assignment_is_not_claimed_or_invoked(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        (
            database_path,
            service,
            broker,
            _prepared,
            _assignment,
            _item,
        ) = await _prepared_assignment(
            tmp_path,
            deadline_seconds=0.5,
        )
        await asyncio.sleep(0.6)
        invoked = False

        def must_not_run(**_):
            nonlocal invoked
            invoked = True
            raise AssertionError("expired assignment handler was invoked")

        assert (
            await service.store.claim_pending_outbox(
                claimed_by="deadline-probe",
                limit=10,
            )
            == []
        )
        batch = await _run_once(
            database_path=database_path,
            journal_path=tmp_path / "data" / "deadline-dispatch.db",
            service=service,
            broker=broker,
            handlers={AgentRole.codex: must_not_run},
        )
        assert batch.records == ()
        assert invoked is False

    asyncio.run(scenario())


@pytest.mark.skipif(
    shutil.which("sandbox-exec") is None,
    reason="macOS sandbox-exec is unavailable",
)
def test_stop_kills_external_adapter_process_group_before_late_side_effect(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        adapter_path = tmp_path / "cancellable-adapter.py"
        adapter_path.write_text(
            "\n".join(
                (
                    "import json",
                    "import pathlib",
                    "import sys",
                    "import time",
                    "request = json.load(sys.stdin)",
                    "root = pathlib.Path(request['role']['workspace_root'])",
                    (
                        "(root / 'tests' / 'adapter-started.txt')."
                        "write_text('started', encoding='utf-8')"
                    ),
                    "time.sleep(10)",
                    (
                        "(root / 'src' / 'late-adapter-side-effect.txt')."
                        "write_text('escaped', encoding='utf-8')"
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        adapter_argv = (str(Path(sys.executable).resolve()), str(adapter_path))
        (
            database_path,
            service,
            broker,
            prepared,
            assignment,
            item,
        ) = await _prepared_assignment(
            tmp_path,
            codex_extra_argv=(adapter_argv,),
        )
        codex_workspace = Path(
            next(
                grant.workspace_root
                for grant in prepared.grants
                if grant.agent_role == AgentRole.codex
            )
        )
        started_marker = codex_workspace / "tests" / "adapter-started.txt"
        late_marker = codex_workspace / "src" / "late-adapter-side-effect.txt"
        running = asyncio.create_task(
            _run_once(
                database_path=database_path,
                journal_path=tmp_path / "data" / "external-stop-dispatch.db",
                service=service,
                broker=broker,
                handlers={
                    AgentRole.codex: ExternalJsonArgvDispatchHandler(
                        adapter_argv,
                        timeout_seconds=30,
                    )
                },
            )
        )
        for _ in range(200):
            if started_marker.exists():
                break
            await asyncio.sleep(0.01)
        assert started_marker.exists()
        await service.stop_assignment(
            principal=DevelopmentPrincipal(
                actor_role="user",
                actor_id="fixture-user",
            ),
            assignment_id=assignment.assignment_id,
            expected_revision=assignment.revision,
            idempotency_key="stop-external-adapter-0001",
        )
        batch = await asyncio.wait_for(running, timeout=3)
        await asyncio.sleep(1.25)

        assert batch.records[0].status == (DispatchOutcomeStatus.reconciliation_required)
        assert not late_marker.exists()
        assert (
            await service.store.get_work_item(item.work_item_id)
        ).status == WorkItemStatus.cancelled
        events = await service.store.read_events(
            assignment.assignment_id,
            after=0,
            limit=1_000,
        )
        assert "work_item.result_submitted" not in {event.event_type for event in events}

    asyncio.run(scenario())


def test_review_unknown_outcome_restart_requires_idempotent_owner_requeue_then_closes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        (
            database_path,
            service,
            broker,
            _prepared,
            assignment,
            item,
        ) = await _prepared_assignment(tmp_path)
        journal_path = tmp_path / "data" / "review-restart-dispatch.db"
        user = DevelopmentPrincipal(
            actor_role="user",
            actor_id="fixture-user",
        )

        def codex_handler(*, context: RoleBoundDispatchContext):
            workspace = Path(context.workspace_grant.workspace_root)
            (workspace / "src" / "mathlib.py").write_text(
                "def add(left, right):\n    return left + right\n",
                encoding="utf-8",
            )
            return AutonomousHandlerCompletion(
                outcome=DispatchOutcome(
                    status=DispatchOutcomeStatus.delivered,
                    detail="Codex submitted the result that requires Lilies review.",
                ),
                development_result=_result(
                    broker=broker,
                    assignment=assignment,
                    work_item=context.work_item,
                    lease_id=context.lease.lease_id,
                    workspace=workspace,
                ),
            )

        result_batch = await _run_once(
            database_path=database_path,
            journal_path=journal_path,
            service=service,
            broker=broker,
            handlers={AgentRole.codex: codex_handler},
        )
        assert [record.status for record in result_batch.records] == [
            DispatchOutcomeStatus.delivered
        ]
        ready = await service.store.get_work_item(item.work_item_id)
        assert ready.status == WorkItemStatus.ready_for_lilies_review
        review_outbox = next(
            outbox
            for outbox in await service.store.list_pending_outbox()
            if outbox.kind == "lilies_review"
        )

        frozen_before = await service.store.get_assignment(assignment.assignment_id)
        usage_before = await service.store.list_development_tool_usage(assignment.assignment_id)

        crash_store = CollaborativeDevelopmentStore(database_path)
        await crash_store.initialize()
        claims = await crash_store.claim_pending_outbox(
            claimed_by="crashing-review-worker",
            claim_ttl_seconds=1,
            limit=10,
        )
        review_claim = next(
            claim for claim in claims if claim.outbox.outbox_id == review_outbox.outbox_id
        )
        crash_journal = CollaborativeDevelopmentDispatchJournal(journal_path)
        crash_journal.initialize()
        began = crash_journal.begin_invocation(
            DispatchInvocationRecord(
                outbox_id=review_outbox.outbox_id,
                attempt=1,
                claim_id=review_claim.claim_id,
                assignment_id=assignment.assignment_id,
                work_item_id=item.work_item_id,
                state="started",
                started_at=utc_now(),
            )
        )
        assert began is True
        fence = await crash_store.acquire_dispatch_invocation_fence(
            assignment_id=assignment.assignment_id,
            outbox_id=review_outbox.outbox_id,
            attempt=1,
            claim_id=review_claim.claim_id,
        )
        assert fence.acquired is True

        await asyncio.sleep(1.05)
        restarted_service = CollaborativeDevelopmentService(
            store=CollaborativeDevelopmentStore(database_path),
            enabled=True,
            autonomous_enabled=True,
        )
        await restarted_service.initialize()
        review_handler_calls = 0

        def must_not_replay_unknown_review(*, context: RoleBoundDispatchContext):
            nonlocal review_handler_calls
            review_handler_calls += 1
            raise AssertionError(f"unknown review attempt was automatically replayed: {context}")

        reconciliation_batch = await _run_once(
            database_path=database_path,
            journal_path=journal_path,
            service=restarted_service,
            broker=broker,
            handlers={AgentRole.lilies: must_not_replay_unknown_review},
        )
        assert [record.status for record in reconciliation_batch.records] == [
            DispatchOutcomeStatus.reconciliation_required
        ]
        assert review_handler_calls == 0

        listed = await restarted_service.list_review_reconciliations(
            principal=user,
            assignment_id=assignment.assignment_id,
        )
        assert listed["execution_mode"] == ExecutionMode.autonomous
        assert listed["dispatch_behavior"] == "eligible_for_next_autonomous_worker_poll"
        assert listed["automatic_unknown_outcome_replay"] is False
        [failed] = listed["reconciliations"]
        assert failed.outbox_id == review_outbox.outbox_id
        assert failed.status.value == "failed"
        assert failed.last_error == "reconciliation_required"
        assert failed.attempts == 1

        requeue_arguments = {
            "principal": user,
            "assignment_id": assignment.assignment_id,
            "outbox_id": failed.outbox_id,
            "expected_work_item_revision": ready.revision,
            "expected_failed_attempt": 1,
            "reason": (
                "The owner inspected the unknown review outcome and authorizes "
                "a fresh Lilies review attempt."
            ),
            "idempotency_key": "review-reconciliation-requeue-0001",
        }
        requeued = await restarted_service.requeue_review_reconciliation(**requeue_arguments)
        replayed = await restarted_service.requeue_review_reconciliation(**requeue_arguments)
        assert replayed == requeued
        assert requeued["grant_changed"] is False
        assert requeued["budget_reset"] is False
        assert requeued["requeued_outbox"].attempts == 1
        assert requeued["requeued_outbox"].status.value == "pending"
        pending_review = [
            outbox
            for outbox in await restarted_service.store.list_pending_outbox()
            if outbox.outbox_id == review_outbox.outbox_id
        ]
        assert len(pending_review) == 1
        events_after_requeue = await restarted_service.store.read_events(
            assignment.assignment_id,
            after=0,
            limit=1_000,
        )
        assert [
            event.event_type
            for event in events_after_requeue
            if event.event_type == "work_item.review_reconciliation_requeued"
        ] == ["work_item.review_reconciliation_requeued"]

        after_requeue = await restarted_service.store.get_assignment(assignment.assignment_id)
        assert after_requeue.workspace_grants == frozen_before.workspace_grants
        assert after_requeue.budget == frozen_before.budget
        assert (
            await restarted_service.store.list_development_tool_usage(assignment.assignment_id)
            == usage_before
        )

        def accepted_review(*, context: RoleBoundDispatchContext):
            nonlocal review_handler_calls
            review_handler_calls += 1
            return AutonomousHandlerCompletion(
                outcome=DispatchOutcome(
                    status=DispatchOutcomeStatus.delivered,
                    detail="Lilies accepted the explicitly requeued review.",
                ),
                lilies_review=_review(
                    work_item=context.work_item,
                    result=context.source_result,
                    receipt=context.review_snapshot,
                ),
            )

        accepted_batch = await _run_once(
            database_path=database_path,
            journal_path=journal_path,
            service=restarted_service,
            broker=broker,
            handlers={AgentRole.lilies: accepted_review},
        )
        assert [record.status for record in accepted_batch.records] == [
            DispatchOutcomeStatus.delivered
        ]
        assert accepted_batch.records[0].attempt == 2
        assert review_handler_calls == 1
        accepted = await restarted_service.store.get_work_item(item.work_item_id)
        assert accepted.status == WorkItemStatus.accepted
        closed = await restarted_service.close_work_item(
            principal=user,
            work_item_id=item.work_item_id,
            expected_revision=accepted.revision,
            idempotency_key="close-reconciled-review-0001",
        )
        assert closed.status == WorkItemStatus.closed
        assert (
            await restarted_service.list_review_reconciliations(
                principal=user,
                assignment_id=assignment.assignment_id,
            )
        )["reconciliations"] == []

        history = crash_journal.history(assignment.assignment_id)
        review_history = [
            record for record in history if record.outbox_id == review_outbox.outbox_id
        ]
        assert [(record.attempt, record.status) for record in review_history] == [
            (1, DispatchOutcomeStatus.reconciliation_required),
            (2, DispatchOutcomeStatus.delivered),
        ]

    asyncio.run(scenario())
