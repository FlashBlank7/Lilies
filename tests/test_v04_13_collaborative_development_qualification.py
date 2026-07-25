from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_platform.collaborative_development_cli import build_parser
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
    utc_now,
)
from agent_platform.collaborative_development_storage import (
    CollaborativeDevelopmentStore,
)
from agent_platform.development_workspace_broker import (
    DevelopmentWorkspaceBroker,
    DevelopmentWorkspaceSpec,
)
from agent_platform.lilies_development_tools import (
    DevelopmentToolAuthority,
    DevelopmentToolName,
    DevelopmentWorkspaceTools,
    GitDiffRequest,
    ProcessRunRequest,
    WorkspacePatchRequest,
)
from agent_platform.lilies_identity import (
    build_lilies_development_system_prompt,
    build_lilies_system_prompt,
)


def _sha256(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "plain-python-library"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src" / "mathlib.py").write_text(
        "def add(left, right):\n    return left - right\n",
        encoding="utf-8",
    )
    (source / "tests" / "check.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parents[1]))\n"
        "from src.mathlib import add\n"
        "assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(source, "init")
    _git(source, "config", "user.email", "fixture@example.invalid")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "frozen unrelated fixture")
    return source, _git(source, "rev-parse", "HEAD")


def _assignment(
    tmp_path: Path,
    *,
    execution_mode: ExecutionMode = ExecutionMode.manual_dispatch,
    approval_mode: ApprovalMode = ApprovalMode.manual,
) -> tuple[DevelopmentAssignment, Path]:
    source, baseline = _fixture(tmp_path)
    assignment_id = uuid4()
    command = (sys.executable, "check.py")
    prepared = DevelopmentWorkspaceBroker(tmp_path / "workspaces").prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.lilies,
                allowed_paths=("src", "tests"),
                allowed_argv=(command,),
                allowed_side_effects=(
                    SideEffect.workspace_write,
                    SideEffect.process_execute,
                ),
            ),
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.codex,
                allowed_paths=("src", "tests"),
                allowed_argv=(command,),
                allowed_side_effects=(
                    SideEffect.workspace_write,
                    SideEffect.process_execute,
                ),
            ),
        ),
    )
    created = utc_now()
    return (
        DevelopmentAssignment(
            assignment_id=assignment_id,
            goal="Fix addition, test it, and have Lilies independently review it.",
            software_id="plain-python-library",
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
            workspace_grants=prepared.grants,
            budget=DevelopmentBudget(
                max_work_items=5,
                max_commands=20,
                max_tool_calls=100,
                max_wall_seconds=3_600,
                max_cost_usd=5,
            ),
            deadline=created + timedelta(hours=1),
            approval_mode=approval_mode,
            execution_mode=execution_mode,
            created_at=created,
            updated_at=created,
        ),
        source,
    )


def _grant(assignment: DevelopmentAssignment, role: AgentRole):
    return next(
        grant for grant in assignment.workspace_grants if grant.agent_role == role
    )


def _tools(assignment: DevelopmentAssignment, role: AgentRole) -> DevelopmentWorkspaceTools:
    return DevelopmentWorkspaceTools(
        DevelopmentToolAuthority(
            actor_role=role,
            workspace_grant=_grant(assignment, role),
            enabled_tools=(
                DevelopmentToolName.workspace_search,
                DevelopmentToolName.workspace_read,
                DevelopmentToolName.workspace_write,
                DevelopmentToolName.workspace_patch,
                DevelopmentToolName.process_run,
                DevelopmentToolName.git_status,
                DevelopmentToolName.git_diff,
            ),
        )
    )


def _command_receipt(result, *, started) -> CommandReceipt:
    return CommandReceipt(
        argv=result.argv,
        cwd=result.cwd,
        exit_code=result.exit_code if result.exit_code is not None else 124,
        output_digest=result.output_digest,
        started_at=started,
        finished_at=utc_now(),
    )


def test_q24_q25_development_role_is_explicit_and_builder_surfaces_stay_clean(
    tmp_path: Path,
) -> None:
    assignment, _ = _assignment(tmp_path)
    payload = assignment.model_dump(mode="json")
    payload["application_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        DevelopmentAssignment.model_validate(payload)

    development_tools = tuple(item.value for item in DevelopmentToolName)
    development_prompt = build_lilies_development_system_prompt(
        workspace=_grant(assignment, AgentRole.lilies).workspace_root,
        tool_names=development_tools,
        assignment_goal=assignment.goal,
        task_role="reviewer",
        authority_summary=_sha256(
            str(_grant(assignment, AgentRole.lilies).model_dump(mode="json"))
        ),
    )
    ordinary_prompt = build_lilies_system_prompt(
        workspace=str(tmp_path / "ordinary"),
        tool_names=("platform_contract_get",),
        collaboration_active=False,
    )
    assert all(tool in development_prompt for tool in development_tools)
    assert all(tool not in ordinary_prompt for tool in development_tools)
    assert "Codex" in development_prompt
    assert "Codex" not in ordinary_prompt
    assert assignment.enterprise_denominator is False


@pytest.mark.asyncio
async def test_q26_manual_waits_and_autonomous_persists_dispatch(tmp_path: Path) -> None:
    assignment, _ = _assignment(tmp_path)
    store = CollaborativeDevelopmentStore(tmp_path / "development.db")
    await store.initialize()
    await store.create_assignment(
        assignment,
        actor_id="user",
        idempotency_key="q26-create-assignment-0001",
    )
    created = utc_now()
    item = await store.create_work_item(
        DevelopmentWorkItem(
            work_item_id=uuid4(),
            assignment_id=assignment.assignment_id,
            kind=WorkItemKind.bug,
            objective="Fix addition.",
            acceptance=("The executable check passes.",),
            assigned_role=AgentRole.codex,
            created_at=created,
            updated_at=created,
        ),
        actor_role="lilies",
        actor_id="lilies-agent",
        idempotency_key="q26-create-work-item-0001",
    )
    assert item.status == WorkItemStatus.proposed
    assert await store.list_pending_outbox() == []

    autonomous = await store.set_execution_mode(
        assignment.assignment_id,
        ExecutionMode.autonomous,
        expected_revision=assignment.revision,
        actor_id="user",
        idempotency_key="q26-enable-autonomous-0001",
    )
    assert autonomous.workspace_grants == assignment.workspace_grants
    assert len(await store.list_pending_outbox()) == 1

    second, _ = _assignment(tmp_path / "new")
    assert second.execution_mode == ExecutionMode.manual_dispatch
    assert second.approval_mode == ApprovalMode.manual


@pytest.mark.asyncio
async def test_q28_plain_git_fixture_completes_rework_accept_and_archive(
    tmp_path: Path,
) -> None:
    assignment, source = _assignment(
        tmp_path,
        execution_mode=ExecutionMode.autonomous,
        approval_mode=ApprovalMode.auto_forward,
    )
    store = CollaborativeDevelopmentStore(tmp_path / "development.db")
    await store.initialize()
    await store.create_assignment(
        assignment,
        actor_id="user",
        idempotency_key="q28-create-assignment-0001",
    )
    created = utc_now()
    item = await store.create_work_item(
        DevelopmentWorkItem(
            work_item_id=uuid4(),
            assignment_id=assignment.assignment_id,
            kind=WorkItemKind.bug,
            objective="Correct the add implementation.",
            acceptance=("tests/check.py exits successfully.",),
            assigned_role=AgentRole.codex,
            created_at=created,
            updated_at=created,
        ),
        actor_role="lilies",
        actor_id="lilies-agent",
        idempotency_key="q28-create-work-item-0001",
    )
    codex_tools = _tools(assignment, AgentRole.codex)
    lilies_tools = _tools(assignment, AgentRole.lilies)

    first_lease = await store.acquire_lease(
        item.work_item_id,
        owner_role=AgentRole.codex,
        owner_id="codex-agent",
        expected_revision=item.revision,
        idempotency_key="q28-first-lease-0001",
    )
    first_working = await store.start_work(
        first_lease.lease_id,
        owner_id="codex-agent",
        expected_work_item_revision=first_lease.work_item_revision,
        idempotency_key="q28-first-start-0001",
    )
    first_started = utc_now()
    first_test = await codex_tools.process_run(
        ProcessRunRequest(
            argv=(sys.executable, "check.py"),
            cwd="tests",
        )
    )
    assert first_test.exit_code != 0
    first_result = DevelopmentResult(
        result_id=uuid4(),
        assignment_id=assignment.assignment_id,
        work_item_id=item.work_item_id,
        lease_id=first_lease.lease_id,
        agent_role=AgentRole.codex,
        baseline_commit=assignment.baseline_commit,
        diff_digest=_sha256(b""),
        commands=(_command_receipt(first_test, started=first_started),),
        tests=(
            DevelopmentTestReceipt(
                name="plain Python fixture check",
                command_digest=_sha256("\0".join(first_test.argv)),
                exit_code=first_test.exit_code or 1,
                passed=False,
                output_digest=first_test.output_digest,
            ),
        ),
        evidence_refs=(first_test.output_digest,),
        reproduction_steps=("Run the exact granted check command.",),
        created_at=utc_now(),
    )
    ready = await store.submit_result(
        first_result,
        owner_id="codex-agent",
        expected_work_item_revision=first_working.revision,
        idempotency_key="q28-first-result-0001",
    )
    first_review_test_started = utc_now()
    first_review_test = await lilies_tools.process_run(
        ProcessRunRequest(argv=(sys.executable, "check.py"), cwd="tests")
    )
    rework = await store.submit_review(
        LiliesReview(
            review_id=uuid4(),
            assignment_id=assignment.assignment_id,
            work_item_id=item.work_item_id,
            result_id=first_result.result_id,
            verdict=ReviewVerdict.rework,
            acceptance_checks=(
                AcceptanceCheck(
                    criterion=item.acceptance[0],
                    passed=False,
                    evidence_refs=(first_review_test.output_digest,),
                ),
            ),
            verification_commands=(
                _command_receipt(first_review_test, started=first_review_test_started),
            ),
            evidence_refs=(first_review_test.output_digest,),
            next_requirements=("Correct subtraction to addition and rerun the check.",),
            created_at=utc_now(),
        ),
        reviewer_id="lilies-agent",
        expected_work_item_revision=ready.revision,
        idempotency_key="q28-rework-review-0001",
    )
    assert rework.status == WorkItemStatus.awaiting_dispatch

    second_lease = await store.acquire_lease(
        item.work_item_id,
        owner_role=AgentRole.codex,
        owner_id="codex-agent",
        expected_revision=rework.revision,
        idempotency_key="q28-second-lease-0001",
    )
    second_working = await store.start_work(
        second_lease.lease_id,
        owner_id="codex-agent",
        expected_work_item_revision=second_lease.work_item_revision,
        idempotency_key="q28-second-start-0001",
    )
    await codex_tools.workspace_patch(
        WorkspacePatchRequest(
            path="src/mathlib.py",
            old_string="return left - right",
            new_string="return left + right",
        )
    )
    diff = await codex_tools.git_diff(GitDiffRequest(cwd="src"))
    second_started = utc_now()
    second_test = await codex_tools.process_run(
        ProcessRunRequest(argv=(sys.executable, "check.py"), cwd="tests")
    )
    assert second_test.exit_code == 0
    second_result = DevelopmentResult(
        result_id=uuid4(),
        assignment_id=assignment.assignment_id,
        work_item_id=item.work_item_id,
        lease_id=second_lease.lease_id,
        agent_role=AgentRole.codex,
        baseline_commit=assignment.baseline_commit,
        diff_digest=diff.output_digest,
        commands=(_command_receipt(second_test, started=second_started),),
        tests=(
            DevelopmentTestReceipt(
                name="plain Python fixture check",
                command_digest=_sha256("\0".join(second_test.argv)),
                exit_code=0,
                passed=True,
                output_digest=second_test.output_digest,
            ),
        ),
        evidence_refs=(diff.output_digest, second_test.output_digest),
        reproduction_steps=("Inspect the Git diff and run the exact granted check.",),
        created_at=utc_now(),
    )
    second_ready = await store.submit_result(
        second_result,
        owner_id="codex-agent",
        expected_work_item_revision=second_working.revision,
        idempotency_key="q28-second-result-0001",
    )

    await lilies_tools.workspace_patch(
        WorkspacePatchRequest(
            path="src/mathlib.py",
            old_string="return left - right",
            new_string="return left + right",
        )
    )
    review_started = utc_now()
    review_test = await lilies_tools.process_run(
        ProcessRunRequest(argv=(sys.executable, "check.py"), cwd="tests")
    )
    assert review_test.exit_code == 0
    accepted = await store.submit_review(
        LiliesReview(
            review_id=uuid4(),
            assignment_id=assignment.assignment_id,
            work_item_id=item.work_item_id,
            result_id=second_result.result_id,
            verdict=ReviewVerdict.accepted,
            acceptance_checks=(
                AcceptanceCheck(
                    criterion=item.acceptance[0],
                    passed=True,
                    evidence_refs=(review_test.output_digest,),
                ),
            ),
            verification_commands=(
                _command_receipt(review_test, started=review_started),
            ),
            evidence_refs=(diff.output_digest, review_test.output_digest),
            created_at=utc_now(),
        ),
        reviewer_id="lilies-agent",
        expected_work_item_revision=second_ready.revision,
        idempotency_key="q28-accepted-review-0001",
    )
    closed = await store.close_work_item(
        item.work_item_id,
        expected_revision=accepted.revision,
        actor_id="user",
        idempotency_key="q28-close-work-item-0001",
    )
    stopped = await store.stop_assignment(
        assignment.assignment_id,
        expected_revision=assignment.revision,
        actor_id="user",
        idempotency_key="q28-stop-assignment-0001",
    )
    archived = await store.archive_assignment(
        assignment.assignment_id,
        expected_revision=stopped.revision,
        actor_id="user",
        idempotency_key="q28-archive-assignment-0001",
    )

    assert closed.status == WorkItemStatus.closed
    assert archived.status.value == "archived"
    assert _git(source, "status", "--short") == ""
    assert _git(Path(_grant(assignment, AgentRole.codex).workspace_root), "diff", "--stat")
    assert _git(Path(_grant(assignment, AgentRole.lilies).workspace_root), "diff", "--stat")
    assert build_parser().parse_args(["show", str(assignment.assignment_id)]).command == "show"

    restarted = CollaborativeDevelopmentStore(tmp_path / "development.db")
    await restarted.initialize()
    assert (
        await restarted.get_assignment(assignment.assignment_id)
    ).status.value == "archived"
    events = await restarted.read_events(assignment.assignment_id)
    assert events[-1].event_type == "assignment.archived"
