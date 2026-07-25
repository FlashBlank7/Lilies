#!/usr/bin/env python3
"""Generate real, digest-bound T01G collaborative-development evidence.

The generator deliberately does not invoke pytest as a proxy for product
behavior.  It creates unrelated plain-Git fixtures, prepares broker-attested
role workspaces, and drives the production assignment service and durable
worker through both manual and autonomous handoff modes.  It also starts the
standalone collaboration server and uses independent real CLI processes for a
two-result rework/accept review lifecycle through close, stop, and archive.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agent_platform.collaboration_qualification import (  # noqa: E402
    QualificationSurfaceResult,
    canonical_digest,
    qualification_source_revision,
)
from agent_platform.collaborative_development_auth import (  # noqa: E402
    DevelopmentPrincipal,
)
from agent_platform.collaborative_development_dispatcher import (  # noqa: E402
    CollaborativeDevelopmentDispatchJournal,
    DispatchHistoryRecord,
    DispatchOutcome,
    DispatchOutcomeStatus,
)
from agent_platform.collaborative_development_handler import (  # noqa: E402
    RoleBoundDispatchContext,
)
from agent_platform.collaborative_development_models import (  # noqa: E402
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
    utc_now,
)
from agent_platform.collaborative_development_service import (  # noqa: E402
    CollaborativeDevelopmentService,
)
from agent_platform.collaborative_development_storage import (  # noqa: E402
    CollaborativeDevelopmentStore,
)
from agent_platform.collaborative_development_worker import (  # noqa: E402
    AutonomousDevelopmentLifecycleBridge,
    AutonomousHandlerCompletion,
    run_dispatch_worker,
)
from agent_platform.development_workspace_broker import (  # noqa: E402
    DevelopmentReviewSnapshotReceipt,
    DevelopmentWorkspaceBroker,
    DevelopmentWorkspaceSpec,
    PreparedDevelopmentWorkspaces,
)
from agent_platform.lilies_development_tools import (  # noqa: E402
    DevelopmentToolAuthority,
    DevelopmentToolName,
    DevelopmentWorkspaceTools,
    GitDiffRequest,
    ProcessRunRequest,
    WorkspacePatchRequest,
)


STAGE_TASK_ID = "V04-13-T01G"
REUSABLE_FILENAME = "reusable-collaborative-development.json"
DISPATCH_FILENAME = "durable-autonomous-dispatch-history.json"
SURFACE_FILENAME = "standalone-development-api-cli.json"
CHECK_ARGV = (sys.executable, "check.py")
FULL_EXECUTED_LIFECYCLE = (
    "work_item",
    "result",
    "rework",
    "independent_lilies_review",
    "accept",
    "close",
    "stop",
    "archive",
)
QUALIFICATION_LIFECYCLE = (
    "work_item",
    "result",
    "rework",
    "independent_lilies_review",
    "accept",
    "close",
    "archive",
)
AUTHORITY_DIMENSIONS = (
    "workspace_paths",
    "argv",
    "network_hosts",
    "side_effects",
    "secret_refs",
    "budgets",
)


@dataclass(frozen=True)
class ScenarioDefinition:
    source_repository: Path
    broker: DevelopmentWorkspaceBroker
    prepared: PreparedDevelopmentWorkspaces
    assignment: DevelopmentAssignment
    database_path: Path
    journal_path: Path


@dataclass(frozen=True)
class LifecycleEvidence:
    record: dict[str, Any]
    durable_record: dict[str, Any]


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
        },
    )
    return completed.stdout.strip()


def _create_plain_git_fixture(root: Path, *, label: str) -> tuple[Path, str]:
    source = root / "plain-python-library"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src" / "mathlib.py").write_text(
        "def add(left, right):\n    return left - right\n",
        encoding="utf-8",
    )
    (source / "tests" / "check.py").write_text(
        "import sys\n"
        "sys.path.insert(0, '../src')\n"
        "from mathlib import add\n"
        "raise SystemExit(0 if add(2, 3) == 5 else 1)\n",
        encoding="utf-8",
    )
    _git(source, "init", "-q")
    _git(source, "config", "user.email", f"{label}@example.invalid")
    _git(source, "config", "user.name", f"T01G {label}")
    _git(source, "add", "src/mathlib.py", "tests/check.py")
    _git(source, "commit", "-q", "-m", "frozen unrelated fixture")
    return source, _git(source, "rev-parse", "HEAD")


def _scenario_definition(
    root: Path,
    *,
    label: str,
    execution_mode: ExecutionMode,
) -> ScenarioDefinition:
    source_repository, baseline = _create_plain_git_fixture(root, label=label)
    broker = DevelopmentWorkspaceBroker(root / "workspace-state")
    assignment_id = uuid4()
    prepared = broker.prepare(
        source_repository=source_repository,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.lilies,
                allowed_paths=("src", "tests"),
                allowed_argv=(CHECK_ARGV,),
                allowed_side_effects=(SideEffect.process_execute,),
            ),
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.codex,
                allowed_paths=("src", "tests"),
                allowed_argv=(CHECK_ARGV,),
                allowed_side_effects=(
                    SideEffect.workspace_write,
                    SideEffect.process_execute,
                ),
            ),
        ),
    )
    created_at = utc_now()
    assignment = DevelopmentAssignment(
        assignment_id=assignment_id,
        goal=(
            "Repair addition in an unrelated plain Python library and have "
            "Lilies independently execute the frozen acceptance check."
        ),
        software_id=f"plain-python-library-{label}",
        baseline_commit=baseline,
        agent_roles=(
            AgentRoleGrant(
                agent_role=AgentRole.lilies,
                task_roles=(
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
            max_work_items=4,
            max_commands=20,
            max_tool_calls=100,
            max_wall_seconds=3_600,
            max_cost_usd=5,
        ),
        deadline=created_at + timedelta(minutes=30),
        approval_mode=(
            ApprovalMode.manual
            if execution_mode == ExecutionMode.manual_dispatch
            else ApprovalMode.auto_forward
        ),
        execution_mode=execution_mode,
        created_at=created_at,
        updated_at=created_at,
    )
    data_directory = root / "data"
    database_path = data_directory / "collaborative-development.db"
    return ScenarioDefinition(
        source_repository=source_repository,
        broker=broker,
        prepared=prepared,
        assignment=assignment,
        database_path=database_path,
        journal_path=data_directory / "collaborative-development-dispatch.db",
    )


def _principal(assignment_id: UUID, role: AgentRole) -> DevelopmentPrincipal:
    return DevelopmentPrincipal(
        actor_role=role.value,
        actor_id=f"qualification-{role.value}",
        assignment_id=assignment_id,
    )


def _owner() -> DevelopmentPrincipal:
    return DevelopmentPrincipal(
        actor_role="user",
        actor_id="qualification-owner",
    )


def _role_grant(
    assignment: DevelopmentAssignment,
    role: AgentRole,
) -> WorkspaceGrant:
    matches = [
        grant for grant in assignment.workspace_grants if grant.agent_role == role
    ]
    if len(matches) != 1:
        raise RuntimeError(f"assignment has no unique {role.value} grant")
    return matches[0]


def _workspace_tools(
    grant: WorkspaceGrant,
    *,
    assignment_id: UUID,
    usage_meter: CollaborativeDevelopmentStore,
    mutable: bool,
) -> DevelopmentWorkspaceTools:
    enabled = [
        DevelopmentToolName.process_run,
        DevelopmentToolName.git_diff,
    ]
    if mutable:
        enabled.append(DevelopmentToolName.workspace_patch)
    return DevelopmentWorkspaceTools(
        DevelopmentToolAuthority(
            actor_role=grant.agent_role,
            workspace_grant=grant,
            enabled_tools=tuple(enabled),
            autonomous_handoff=True,
        ),
        assignment_id=assignment_id,
        usage_meter=usage_meter,
        metering_required=True,
    )


def _review_tools(
    grant: WorkspaceGrant,
    *,
    assignment_id: UUID,
    usage_meter: CollaborativeDevelopmentStore,
) -> DevelopmentWorkspaceTools:
    return DevelopmentWorkspaceTools(
        DevelopmentToolAuthority(
            actor_role=AgentRole.lilies,
            workspace_grant=grant,
            enabled_tools=(DevelopmentToolName.process_run,),
            autonomous_handoff=True,
        ),
        assignment_id=assignment_id,
        usage_meter=usage_meter,
        metering_required=True,
    )


def _manual_review_grant(
    original: WorkspaceGrant,
    receipt: DevelopmentReviewSnapshotReceipt,
) -> WorkspaceGrant:
    return WorkspaceGrant(
        workspace_id=receipt.review_snapshot_id,
        agent_role=AgentRole.lilies,
        workspace_root=receipt.review_workspace_root,
        baseline_commit=original.baseline_commit,
        grant_revision=original.grant_revision,
        allowed_paths=original.allowed_paths,
        allowed_argv=original.allowed_argv,
        allowed_hosts=(),
        allowed_side_effects=tuple(
            effect
            for effect in original.allowed_side_effects
            if effect == SideEffect.process_execute
        ),
        secret_refs=(),
        created_at=original.created_at,
    )


def _command_receipt(
    result: Any,
    *,
    started_at: Any,
    finished_at: Any,
) -> CommandReceipt:
    return CommandReceipt(
        argv=tuple(result.argv),
        cwd=result.cwd,
        exit_code=result.exit_code if result.exit_code is not None else 124,
        output_digest=result.output_digest,
        started_at=started_at,
        finished_at=finished_at,
    )


async def _execute_check(
    tools: DevelopmentWorkspaceTools,
    *,
    usage_id: str,
) -> tuple[CommandReceipt, DevelopmentTestReceipt]:
    started_at = utc_now()
    result = await tools.process_run(
        ProcessRunRequest(
            argv=CHECK_ARGV,
            cwd="tests",
            timeout_seconds=30,
            max_output_bytes=64_000,
            usage_id=usage_id,
        )
    )
    finished_at = utc_now()
    command = _command_receipt(
        result,
        started_at=started_at,
        finished_at=finished_at,
    )
    test = DevelopmentTestReceipt(
        name="plain Python addition acceptance",
        command_digest=canonical_digest(
            {
                "argv": list(command.argv),
                "cwd": command.cwd,
            }
        ),
        exit_code=command.exit_code,
        passed=command.exit_code == 0,
        output_digest=command.output_digest,
    )
    return command, test


async def _build_result(
    *,
    definition: ScenarioDefinition,
    assignment: DevelopmentAssignment,
    work_item: DevelopmentWorkItem,
    lease_id: UUID,
    grant: WorkspaceGrant,
    usage_meter: CollaborativeDevelopmentStore,
) -> DevelopmentResult:
    usage_prefix = f"result:{work_item.work_item_id}:{lease_id}"
    tools = _workspace_tools(
        grant,
        assignment_id=assignment.assignment_id,
        usage_meter=usage_meter,
        mutable=True,
    )
    command, test = await _execute_check(
        tools,
        usage_id=f"{usage_prefix}:process-run",
    )
    git_diff = await tools.git_diff(
        GitDiffRequest(
            cwd="src",
            usage_id=f"{usage_prefix}:git-diff",
        )
    )
    diff_digest = await asyncio.to_thread(
        definition.broker.calculate_diff_digest,
        workspace_root=Path(grant.workspace_root),
        baseline_commit=assignment.baseline_commit,
    )
    evidence_refs = tuple(
        dict.fromkeys(
            (
                diff_digest,
                git_diff.output_digest,
                command.output_digest,
            )
        )
    )
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
        evidence_refs=evidence_refs,
        reproduction_steps=(
            "Inspect the role-scoped Git diff and run the exact granted check.",
        ),
        created_at=utc_now(),
    )


async def _build_review(
    *,
    work_item: DevelopmentWorkItem,
    result: DevelopmentResult,
    receipt: DevelopmentReviewSnapshotReceipt,
    review_grant: WorkspaceGrant,
    usage_meter: CollaborativeDevelopmentStore,
) -> LiliesReview:
    command, test = await _execute_check(
        _review_tools(
            review_grant,
            assignment_id=work_item.assignment_id,
            usage_meter=usage_meter,
        ),
        usage_id=f"review:{result.result_id}:process-run",
    )
    evidence_refs = tuple(
        dict.fromkeys(
            (
                receipt.receipt_digest,
                result.diff_digest,
                command.output_digest,
            )
        )
    )
    return LiliesReview(
        review_id=uuid4(),
        assignment_id=work_item.assignment_id,
        work_item_id=work_item.work_item_id,
        result_id=result.result_id,
        verdict=(
            ReviewVerdict.accepted if test.passed else ReviewVerdict.rework
        ),
        acceptance_checks=(
            AcceptanceCheck(
                criterion=work_item.acceptance[0],
                passed=test.passed,
                evidence_refs=(command.output_digest,),
            ),
        ),
        verification_commands=(command,),
        evidence_refs=evidence_refs,
        next_requirements=(
            ()
            if test.passed
            else ("Replace subtraction with addition and rerun the frozen check.",)
        ),
        created_at=utc_now(),
    )


def _snapshot_evidence(
    receipt: DevelopmentReviewSnapshotReceipt,
) -> dict[str, Any]:
    return {
        "receipt_id": str(receipt.receipt_id),
        "review_snapshot_id": str(receipt.review_snapshot_id),
        "result_id": str(receipt.result_id),
        "baseline_commit": receipt.baseline_commit,
        "diff_digest": receipt.diff_digest,
        "snapshot_digest": receipt.snapshot_digest,
        "receipt_digest": receipt.receipt_digest,
        "changed_paths": list(receipt.changed_paths),
        "promotion_state": receipt.promotion_state,
        "source_repository_unchanged": receipt.source_repository_unchanged,
    }


def _event_evidence(events: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "seq": event.seq,
            "event_type": event.event_type,
            "actor_role": event.actor_role,
            "aggregate_revision": event.aggregate_revision,
            "record_digest": canonical_digest(
                event.model_dump(mode="json", exclude_none=True)
            ),
        }
        for event in events
    ]


def _tool_usage_evidence(records: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "reservation_id": str(record.reservation_id),
            "actor_role": record.actor_role.value,
            "usage_id": record.usage_id,
            "tool_name": record.tool_name,
            "request_digest": record.request_digest,
            "tool_calls": record.tool_calls,
            "commands": record.commands,
            "command_cwd": record.command_cwd,
            "status": record.status,
            "response_digest": record.response_digest,
            "output_digest": record.output_digest,
            "consumer_type": record.consumer_type,
            "consumer_id": (
                str(record.consumer_id)
                if record.consumer_id is not None
                else None
            ),
        }
        for record in records
    ]


def _history_json(
    history: list[DispatchHistoryRecord],
) -> list[dict[str, Any]]:
    return [
        item.model_dump(mode="json", exclude_none=True)
        for item in history
    ]


async def _run_worker_once(
    *,
    definition: ScenarioDefinition,
    service: CollaborativeDevelopmentService,
    handlers: dict[AgentRole, Any],
    dispatcher_id: str,
) -> DispatchHistoryRecord:
    batch = await run_dispatch_worker(
        database_path=definition.database_path,
        journal_path=definition.journal_path,
        handlers=handlers,
        once=True,
        poll_interval_seconds=0.05,
        limit=10,
        claim_ttl_seconds=30,
        dispatcher_id=dispatcher_id,
        lifecycle_bridge=AutonomousDevelopmentLifecycleBridge(
            service=service,
            workspace_broker=definition.broker,
            lease_ttl_seconds=30,
            cancellation_poll_seconds=0.01,
        ),
    )
    if len(batch.records) != 1:
        pending = await service.store.list_pending_outbox()
        items = await service.store.list_work_items(
            definition.assignment.assignment_id
        )
        raise RuntimeError(
            f"expected one durable dispatch record, received {len(batch.records)}; "
            f"pending={len(pending)}; work_item_statuses="
            f"{[item.status.value for item in items]}"
        )
    record = batch.records[0]
    if record.status != DispatchOutcomeStatus.delivered:
        raise RuntimeError(
            f"production worker did not deliver {record.outbox_kind}: "
            f"{record.status.value}"
        )
    return record


async def _run_lifecycle_scenario(
    root: Path,
    *,
    label: str,
    execution_mode: ExecutionMode,
) -> LifecycleEvidence:
    definition = _scenario_definition(
        root,
        label=label,
        execution_mode=execution_mode,
    )
    service = CollaborativeDevelopmentService(
        store=CollaborativeDevelopmentStore(definition.database_path),
        enabled=True,
        autonomous_enabled=True,
    )
    await service.initialize()
    owner = _owner()
    assignment = await service.create_assignment(
        principal=owner,
        assignment=definition.assignment,
        idempotency_key=f"{label}-assignment-create-0001",
    )
    initial_grants = assignment.workspace_grants
    initial_grant_digests = {
        grant.agent_role.value: canonical_digest(grant)
        for grant in initial_grants
    }
    item_created_at = utc_now()
    item = await service.create_work_item(
        principal=owner,
        item=DevelopmentWorkItem(
            work_item_id=uuid4(),
            assignment_id=assignment.assignment_id,
            kind=WorkItemKind.bug,
            objective="Make add(2, 3) return 5.",
            acceptance=("tests/check.py exits successfully.",),
            assigned_role=AgentRole.codex,
            created_at=item_created_at,
            updated_at=item_created_at,
        ),
        idempotency_key=f"{label}-work-item-create-0001",
    )
    checkpoints: list[dict[str, Any]] = [
        {
            "step": "work_item",
            "status": item.status.value,
            "revision": item.revision,
        }
    ]
    manual_waited_before_dispatch = False
    manual_waited_for_review = False
    manual_waited_after_rework = False
    if execution_mode == ExecutionMode.manual_dispatch:
        manual_waited_before_dispatch = (
            item.status == WorkItemStatus.proposed
            and await service.store.list_pending_outbox() == []
        )
        if not manual_waited_before_dispatch:
            raise RuntimeError("manual mode did not wait before initial dispatch")
        item = await service.dispatch_work_item(
            principal=owner,
            work_item_id=item.work_item_id,
            expected_revision=item.revision,
            idempotency_key=f"{label}-manual-dispatch-0001",
        )
        checkpoints.append(
            {
                "step": "manual_dispatch",
                "status": item.status.value,
                "revision": item.revision,
            }
        )
    elif item.status != WorkItemStatus.awaiting_dispatch:
        raise RuntimeError("autonomous mode did not durably queue the work item")

    codex_attempt = 0
    results: list[DevelopmentResult] = []
    snapshots: list[DevelopmentReviewSnapshotReceipt] = []
    reviews: list[LiliesReview] = []

    async def codex_handler(
        *,
        context: RoleBoundDispatchContext,
    ) -> AutonomousHandlerCompletion:
        nonlocal codex_attempt
        codex_attempt += 1
        lease = context.lease
        grant = context.workspace_grant
        work_item = context.work_item
        if lease is None:
            raise RuntimeError("Codex handler did not receive a fenced lease")
        if codex_attempt == 2:
            tools = _workspace_tools(
                grant,
                assignment_id=assignment.assignment_id,
                usage_meter=service.store,
                mutable=True,
            )
            await tools.workspace_patch(
                WorkspacePatchRequest(
                    path="src/mathlib.py",
                    old_string="return left - right",
                    new_string="return left + right",
                    usage_id=(
                        f"result:{work_item.work_item_id}:{lease.lease_id}:"
                        "workspace-patch"
                    ),
                )
            )
        elif codex_attempt != 1:
            raise RuntimeError("Codex handler was invoked more than twice")
        result = await _build_result(
            definition=definition,
            assignment=assignment,
            work_item=work_item,
            lease_id=lease.lease_id,
            grant=grant,
            usage_meter=service.store,
        )
        results.append(result)
        return AutonomousHandlerCompletion(
            outcome=DispatchOutcome(
                status=DispatchOutcomeStatus.delivered,
                evidence_refs=result.evidence_refs,
                detail=(
                    "Codex submitted one fenced result from the exact role grant."
                ),
            ),
            development_result=result,
        )

    async def lilies_handler(
        *,
        context: RoleBoundDispatchContext,
    ) -> AutonomousHandlerCompletion:
        receipt = context.review_snapshot
        result = context.source_result
        review_grant = context.workspace_grant
        work_item = context.work_item
        if receipt is None or result is None:
            raise RuntimeError("Lilies handler lacks an independent review snapshot")
        if (
            Path(review_grant.workspace_root)
            != Path(receipt.review_workspace_root)
            or review_grant.allowed_hosts
            or review_grant.secret_refs
            or SideEffect.workspace_write in review_grant.allowed_side_effects
        ):
            raise RuntimeError("Lilies review authority was not independently narrowed")
        review = await _build_review(
            work_item=work_item,
            result=result,
            receipt=receipt,
            review_grant=review_grant,
            usage_meter=service.store,
        )
        snapshots.append(receipt)
        reviews.append(review)
        return AutonomousHandlerCompletion(
            outcome=DispatchOutcome(
                status=DispatchOutcomeStatus.delivered,
                evidence_refs=review.evidence_refs,
                detail=(
                    "Lilies independently reran the check in a broker snapshot."
                ),
            ),
            lilies_review=review,
        )

    handlers = {
        AgentRole.codex: codex_handler,
        AgentRole.lilies: lilies_handler,
    }
    dispatcher_id = f"qualification-{label}-worker"
    first_result_record = await _run_worker_once(
        definition=definition,
        service=service,
        handlers=handlers,
        dispatcher_id=dispatcher_id,
    )
    after_first_result = await service.store.get_work_item(item.work_item_id)
    checkpoints.append(
        {
            "step": "result",
            "status": after_first_result.status.value,
            "revision": after_first_result.revision,
            "dispatch_id": str(first_result_record.dispatch_id),
        }
    )
    if after_first_result.status != WorkItemStatus.ready_for_lilies_review:
        raise RuntimeError("first result did not reach independent Lilies review")

    if execution_mode == ExecutionMode.manual_dispatch:
        manual_waited_for_review = (
            await service.store.list_pending_outbox() == []
        )
        if not manual_waited_for_review:
            raise RuntimeError("manual mode did not wait before Lilies review")
        first_result = await service.get_result(
            principal=_principal(assignment.assignment_id, AgentRole.lilies),
            result_id=results[0].result_id,
        )
        first_receipt = await asyncio.to_thread(
            definition.broker.materialize_review_snapshot,
            prepared=definition.prepared,
            result=first_result,
        )
        first_review = await _build_review(
            work_item=after_first_result,
            result=first_result,
            receipt=first_receipt,
            review_grant=_manual_review_grant(
                _role_grant(assignment, AgentRole.lilies),
                first_receipt,
            ),
            usage_meter=service.store,
        )
        snapshots.append(first_receipt)
        reviews.append(first_review)
        after_rework = await service.submit_review(
            principal=_principal(assignment.assignment_id, AgentRole.lilies),
            review=first_review,
            expected_work_item_revision=after_first_result.revision,
            idempotency_key=f"{label}-manual-review-0001",
        )
        first_review_dispatch_id: str | None = None
    else:
        first_review_record = await _run_worker_once(
            definition=definition,
            service=service,
            handlers=handlers,
            dispatcher_id=dispatcher_id,
        )
        after_rework = await service.store.get_work_item(item.work_item_id)
        first_review_dispatch_id = str(first_review_record.dispatch_id)
    checkpoints.extend(
        (
            {
                "step": "independent_lilies_review",
                "verdict": reviews[0].verdict.value,
                "snapshot_receipt_digest": snapshots[0].receipt_digest,
                "dispatch_id": first_review_dispatch_id,
                "delivery": (
                    "manual_reviewer_submission"
                    if first_review_dispatch_id is None
                    else "durable_autonomous_dispatch"
                ),
            },
            {
                "step": "rework",
                "status": after_rework.status.value,
                "revision": after_rework.revision,
            },
        )
    )
    if (
        reviews[0].verdict != ReviewVerdict.rework
        or after_rework.status != WorkItemStatus.awaiting_dispatch
    ):
        raise RuntimeError("the first independent review did not request rework")

    if execution_mode == ExecutionMode.manual_dispatch:
        manual_waited_after_rework = (
            await service.store.list_pending_outbox() == []
        )
        if not manual_waited_after_rework:
            raise RuntimeError("manual mode did not wait before rework dispatch")
        after_rework = await service.dispatch_work_item(
            principal=owner,
            work_item_id=item.work_item_id,
            expected_revision=after_rework.revision,
            idempotency_key=f"{label}-manual-dispatch-0002",
        )
        checkpoints.append(
            {
                "step": "manual_rework_dispatch",
                "status": after_rework.status.value,
                "revision": after_rework.revision,
            }
        )

    second_result_record = await _run_worker_once(
        definition=definition,
        service=service,
        handlers=handlers,
        dispatcher_id=dispatcher_id,
    )
    after_second_result = await service.store.get_work_item(item.work_item_id)
    checkpoints.append(
        {
            "step": "result",
            "attempt": 2,
            "status": after_second_result.status.value,
            "revision": after_second_result.revision,
            "dispatch_id": str(second_result_record.dispatch_id),
        }
    )
    if after_second_result.status != WorkItemStatus.ready_for_lilies_review:
        raise RuntimeError("corrected result did not reach Lilies review")

    if execution_mode == ExecutionMode.manual_dispatch:
        if await service.store.list_pending_outbox() != []:
            raise RuntimeError("manual mode did not wait before accepted review")
        second_result = await service.get_result(
            principal=_principal(assignment.assignment_id, AgentRole.lilies),
            result_id=results[1].result_id,
        )
        second_receipt = await asyncio.to_thread(
            definition.broker.materialize_review_snapshot,
            prepared=definition.prepared,
            result=second_result,
        )
        second_review = await _build_review(
            work_item=after_second_result,
            result=second_result,
            receipt=second_receipt,
            review_grant=_manual_review_grant(
                _role_grant(assignment, AgentRole.lilies),
                second_receipt,
            ),
            usage_meter=service.store,
        )
        snapshots.append(second_receipt)
        reviews.append(second_review)
        accepted = await service.submit_review(
            principal=_principal(assignment.assignment_id, AgentRole.lilies),
            review=second_review,
            expected_work_item_revision=after_second_result.revision,
            idempotency_key=f"{label}-manual-review-0002",
        )
        accepted_dispatch_id: str | None = None
    else:
        accepted_record = await _run_worker_once(
            definition=definition,
            service=service,
            handlers=handlers,
            dispatcher_id=dispatcher_id,
        )
        accepted = await service.store.get_work_item(item.work_item_id)
        accepted_dispatch_id = str(accepted_record.dispatch_id)
    checkpoints.extend(
        (
            {
                "step": "independent_lilies_review",
                "attempt": 2,
                "verdict": reviews[1].verdict.value,
                "snapshot_receipt_digest": snapshots[1].receipt_digest,
                "dispatch_id": accepted_dispatch_id,
                "delivery": (
                    "manual_reviewer_submission"
                    if accepted_dispatch_id is None
                    else "durable_autonomous_dispatch"
                ),
            },
            {
                "step": "accept",
                "status": accepted.status.value,
                "revision": accepted.revision,
            },
        )
    )
    if (
        reviews[1].verdict != ReviewVerdict.accepted
        or accepted.status != WorkItemStatus.accepted
    ):
        raise RuntimeError("corrected result was not independently accepted")

    closed = await service.close_work_item(
        principal=owner,
        work_item_id=item.work_item_id,
        expected_revision=accepted.revision,
        idempotency_key=f"{label}-close-work-item-0001",
    )
    checkpoints.append(
        {
            "step": "close",
            "status": closed.status.value,
            "revision": closed.revision,
        }
    )
    stopped = await service.stop_assignment(
        principal=owner,
        assignment_id=assignment.assignment_id,
        expected_revision=assignment.revision,
        idempotency_key=f"{label}-stop-assignment-0001",
    )
    checkpoints.append(
        {
            "step": "stop",
            "status": stopped.status.value,
            "revision": stopped.revision,
        }
    )
    archived = await service.archive_assignment(
        principal=owner,
        assignment_id=assignment.assignment_id,
        expected_revision=stopped.revision,
        idempotency_key=f"{label}-archive-assignment-0001",
    )
    checkpoints.append(
        {
            "step": "archive",
            "status": archived.status.value,
            "revision": archived.revision,
        }
    )

    store_events_before = await service.store.read_events(
        assignment.assignment_id,
        after=0,
        limit=5_000,
    )
    tool_usage_before = await service.store.list_development_tool_usage(
        assignment.assignment_id
    )
    journal_before = CollaborativeDevelopmentDispatchJournal(
        definition.journal_path
    )
    journal_before.initialize()
    dispatch_history_before = journal_before.history(assignment.assignment_id)

    reopened_store = CollaborativeDevelopmentStore(definition.database_path)
    await reopened_store.initialize()
    store_events_after = await reopened_store.read_events(
        assignment.assignment_id,
        after=0,
        limit=5_000,
    )
    tool_usage_after = await reopened_store.list_development_tool_usage(
        assignment.assignment_id
    )
    reopened_assignment = await reopened_store.get_assignment(
        assignment.assignment_id
    )
    reopened_work_item = await reopened_store.get_work_item(item.work_item_id)
    reopened_journal = CollaborativeDevelopmentDispatchJournal(
        definition.journal_path
    )
    reopened_journal.initialize()
    dispatch_history_after = reopened_journal.history(assignment.assignment_id)
    reopened_prepared = await asyncio.to_thread(
        definition.broker.load_prepared,
        assignment.assignment_id,
    )

    store_history_equal = [
        event.model_dump(mode="json", exclude_none=True)
        for event in store_events_before
    ] == [
        event.model_dump(mode="json", exclude_none=True)
        for event in store_events_after
    ]
    tool_usage_equal = [
        record.model_dump(mode="json", exclude_none=True)
        for record in tool_usage_before
    ] == [
        record.model_dump(mode="json", exclude_none=True)
        for record in tool_usage_after
    ]
    dispatch_history_equal = _history_json(
        dispatch_history_before
    ) == _history_json(dispatch_history_after)
    original_grants_unchanged = (
        reopened_assignment.workspace_grants == initial_grants
        and reopened_prepared.grants == initial_grants
        and all(
            history.grant_digest
            == initial_grant_digests[history.destination_role.value]
            for history in dispatch_history_after
        )
    )
    source_unchanged = (
        _git(definition.source_repository, "status", "--porcelain=v1") == ""
        and (
            definition.source_repository / "src" / "mathlib.py"
        ).read_text(encoding="utf-8")
        == "def add(left, right):\n    return left - right\n"
    )
    tool_usage_evidence = _tool_usage_evidence(tool_usage_after)
    result_consumer_ids = {
        item["consumer_id"]
        for item in tool_usage_evidence
        if item["consumer_type"] == "result"
    }
    review_consumer_ids = {
        item["consumer_id"]
        for item in tool_usage_evidence
        if item["consumer_type"] == "review"
    }
    if not (
        store_history_equal
        and tool_usage_equal
        and dispatch_history_equal
        and original_grants_unchanged
        and source_unchanged
        and len(dispatch_history_after)
        == (
            2
            if execution_mode == ExecutionMode.manual_dispatch
            else 4
        )
        and [review.verdict for review in reviews]
        == [ReviewVerdict.rework, ReviewVerdict.accepted]
        and [result.tests[0].passed for result in results] == [False, True]
        and len(tool_usage_evidence) == 7
        and {
            name: sum(
                item["tool_name"] == name for item in tool_usage_evidence
            )
            for name in ("process_run", "git_diff", "workspace_patch")
        }
        == {"process_run": 4, "git_diff": 2, "workspace_patch": 1}
        and sum(item["commands"] for item in tool_usage_evidence) == 6
        and all(item["status"] == "completed" for item in tool_usage_evidence)
        and result_consumer_ids
        == {str(result.result_id) for result in results}
        and review_consumer_ids
        == {str(review.review_id) for review in reviews}
    ):
        raise RuntimeError("durability, authority, or lifecycle invariant failed")

    record = {
        "status": "passed",
        "mode": execution_mode.value,
        "assignment_id": str(assignment.assignment_id),
        "software_id": assignment.software_id,
        "baseline_commit": assignment.baseline_commit,
        "enterprise_denominator": False,
        "workflow_application_required": False,
        "builder_required": False,
        "task_package_required": False,
        "oracle_required": False,
        "manual_waited_before_dispatch": manual_waited_before_dispatch,
        "manual_waited_for_review": manual_waited_for_review,
        "manual_waited_after_rework": manual_waited_after_rework,
        "checkpoints": checkpoints,
        "results": [
            {
                "result_id": str(result.result_id),
                "passed": result.tests[0].passed,
                "exit_code": result.tests[0].exit_code,
                "diff_digest": result.diff_digest,
                "output_digest": result.tests[0].output_digest,
            }
            for result in results
        ],
        "review_ids": [str(review.review_id) for review in reviews],
        "independent_review_snapshots": [
            _snapshot_evidence(receipt) for receipt in snapshots
        ],
        "review_verdicts": [review.verdict.value for review in reviews],
        "dispatch_history": _history_json(dispatch_history_after),
        "store_event_history": _event_evidence(store_events_after),
        "tool_usage_history": tool_usage_evidence,
        "restart_store_history_equal": store_history_equal,
        "restart_tool_usage_equal": tool_usage_equal,
        "restart_dispatch_history_equal": dispatch_history_equal,
        "original_grant_digests": initial_grant_digests,
        "original_grants_unchanged": original_grants_unchanged,
        "source_repository_unchanged": source_unchanged,
        "final_assignment_status": reopened_assignment.status.value,
        "final_work_item_status": reopened_work_item.status.value,
        "executed_lifecycle": list(FULL_EXECUTED_LIFECYCLE),
    }
    durable_record = {
        "status": "passed",
        "assignment_id": str(assignment.assignment_id),
        "execution_mode": execution_mode.value,
        "result_ids": [str(result.result_id) for result in results],
        "review_ids": [str(review.review_id) for review in reviews],
        "history": _history_json(dispatch_history_after),
        "store_event_history": _event_evidence(store_events_after),
        "tool_usage_history": tool_usage_evidence,
        "restart_history_equal": dispatch_history_equal,
        "restart_store_history_equal": store_history_equal,
        "restart_tool_usage_equal": tool_usage_equal,
        "original_grants_unchanged": original_grants_unchanged,
        "source_repository_unchanged": source_unchanged,
        "final_assignment_status": reopened_assignment.status.value,
        "final_work_item_status": reopened_work_item.status.value,
        "history_digest": canonical_digest(_history_json(dispatch_history_after)),
        "store_history_digest": canonical_digest(
            _event_evidence(store_events_after)
        ),
        "tool_usage_digest": canonical_digest(tool_usage_evidence),
    }
    return LifecycleEvidence(record=record, durable_record=durable_record)


def _private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _http_json(
    url: str,
    *,
    token: str | None = None,
    timeout: float = 2,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers=(
            {"Authorization": f"Bearer {token}"}
            if token is not None
            else {}
        ),
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
        if not isinstance(payload, dict):
            raise RuntimeError("standalone API response is not an object")
        return int(response.status), payload


def _wait_for_server(
    process: subprocess.Popen[bytes],
    base_url: str,
    *,
    timeout_seconds: float = 15,
) -> tuple[int, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("standalone collaboration server exited during startup")
        try:
            return _http_json(f"{base_url}/health", timeout=0.5)
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.05)
    raise RuntimeError("standalone collaboration server did not become ready") from last_error


def _cli_environment() -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(BACKEND_SRC),
        "LILIES_AUTONOMOUS_COLLABORATION_ENABLED": "false",
    }


def _run_cli(
    prefix: list[str],
    arguments: list[str],
    *,
    semantic_response: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    completed = subprocess.run(
        [*prefix, *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=30,
        env=_cli_environment(),
    )
    if completed.returncode != 0:
        error_text = completed.stderr.decode("utf-8", errors="replace")[:2_000]
        raise RuntimeError(
            f"standalone CLI {arguments[0]} failed with exit code "
            f"{completed.returncode}: {error_text}"
        )
    try:
        payload = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"standalone CLI {arguments[0]} returned invalid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"standalone CLI {arguments[0]} returned no object")
    sanitized = semantic_response(payload)
    return payload, {
        "command": arguments[0],
        "exit_code": completed.returncode,
        "process_boundary": "new_cli_subprocess",
        "semantic_response": sanitized,
        "response_digest": canonical_digest(sanitized),
    }


def _terminate_process(process: subprocess.Popen[bytes]) -> bytes:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate(timeout=5)
    return stdout + stderr


def _run_standalone_api_cli(root: Path) -> tuple[QualificationSurfaceResult, dict[str, Any]]:
    definition = _scenario_definition(
        root,
        label="api-cli",
        execution_mode=ExecutionMode.manual_dispatch,
    )
    owner_token = "owner-" + uuid4().hex + uuid4().hex
    signing_key = "signing-" + uuid4().hex + uuid4().hex
    owner_token_path = root / "credentials" / "owner-token"
    signing_key_path = root / "credentials" / "signing-key"
    _private_text(owner_token_path, owner_token)
    _private_text(signing_key_path, signing_key)
    assignment_path = root / "requests" / "assignment.json"
    assignment_path.parent.mkdir(parents=True, exist_ok=True)
    assignment_path.write_text(
        json.dumps(
            definition.assignment.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    work_created_at = utc_now()
    work_item = DevelopmentWorkItem(
        work_item_id=uuid4(),
        assignment_id=definition.assignment.assignment_id,
        kind=WorkItemKind.bug,
        objective="Make add(2, 3) return 5 through the standalone manual API.",
        acceptance=("tests/check.py exits successfully.",),
        assigned_role=AgentRole.codex,
        created_at=work_created_at,
        updated_at=work_created_at,
    )
    work_item_path = root / "requests" / "work-item.json"
    work_item_path.write_text(
        json.dumps(
            work_item.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    port = _unused_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_platform.collaborative_development_cli",
            "serve",
            "--data-dir",
            str(root / "server-data"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--owner-token-file",
            str(owner_token_path),
            "--signing-key-file",
            str(signing_key_path),
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_cli_environment(),
        start_new_session=True,
    )
    server_log = b""
    try:
        health_status, health = _wait_for_server(server, base_url)
        if (
            health_status != 200
            or health.get("status") != "ok"
            or health.get("workflow_platform_required") is not False
            or health.get("enterprise_denominator") is not False
        ):
            raise RuntimeError("standalone health contract was not satisfied")
        owner_cli_prefix = [
            sys.executable,
            "-m",
            "agent_platform.collaborative_development_cli",
            "--base-url",
            base_url,
            "--token-file",
            str(owner_token_path),
        ]
        cli_trace: list[dict[str, Any]] = []

        created_payload, created_trace = _run_cli(
            owner_cli_prefix,
            [
                "create",
                "--assignment-file",
                str(assignment_path),
                "--idempotency-key",
                "api-cli-create-assignment-0001",
            ],
            semantic_response=lambda payload: {
                "assignment_id": payload["assignment"]["assignment_id"],
                "status": payload["assignment"]["status"],
                "software_id": payload["assignment"]["software_id"],
                "role_tokens_issued": (
                    isinstance(payload.get("lilies_access_token"), str)
                    and isinstance(payload.get("codex_access_token"), str)
                    and payload["lilies_access_token"]
                    != payload["codex_access_token"]
                ),
                "enterprise_denominator": payload["enterprise_denominator"],
            },
        )
        if (
            created_payload["assignment"]["assignment_id"]
            != str(definition.assignment.assignment_id)
            or created_trace["semantic_response"]["role_tokens_issued"] is not True
        ):
            raise RuntimeError("standalone CLI create response was not bound")
        cli_trace.append(created_trace)
        durable_assignment = DevelopmentAssignment.model_validate(
            created_payload["assignment"]
        )
        lilies_token_path = root / "credentials" / "lilies-token"
        codex_token_path = root / "credentials" / "codex-token"
        _private_text(lilies_token_path, created_payload["lilies_access_token"])
        _private_text(codex_token_path, created_payload["codex_access_token"])
        role_cli_prefix = {
            AgentRole.lilies: [
                sys.executable,
                "-m",
                "agent_platform.collaborative_development_cli",
                "--base-url",
                base_url,
                "--token-file",
                str(lilies_token_path),
            ],
            AgentRole.codex: [
                sys.executable,
                "-m",
                "agent_platform.collaborative_development_cli",
                "--base-url",
                base_url,
                "--token-file",
                str(codex_token_path),
            ],
        }
        usage_store = CollaborativeDevelopmentStore(
            root / "server-data" / "collaborative-development.db"
        )
        asyncio.run(usage_store.initialize())

        _, status_trace = _run_cli(
            owner_cli_prefix,
            ["status", str(definition.assignment.assignment_id)],
            semantic_response=lambda payload: {
                "assignment_id": payload["assignment"]["assignment_id"],
                "status": payload["assignment"]["status"],
                "work_items": sum(payload["work_item_counts"].values()),
                "enterprise_denominator": payload["enterprise_denominator"],
            },
        )
        if (
            status_trace["semantic_response"]["status"] != "active"
            or status_trace["semantic_response"]["work_items"] != 0
        ):
            raise RuntimeError(
                "standalone CLI status did not observe the created assignment"
            )
        cli_trace.append(status_trace)

        proposed_payload, work_create_trace = _run_cli(
            owner_cli_prefix,
            [
                "work-create",
                str(definition.assignment.assignment_id),
                "--work-item-file",
                str(work_item_path),
                "--idempotency-key",
                "api-cli-create-work-item-0001",
            ],
            semantic_response=lambda payload: {
                "work_item_id": payload["work_item_id"],
                "assignment_id": payload["assignment_id"],
                "status": payload["status"],
                "revision": payload["revision"],
            },
        )
        cli_trace.append(work_create_trace)
        proposed = DevelopmentWorkItem.model_validate(proposed_payload)

        dispatched_payload, dispatch_trace = _run_cli(
            owner_cli_prefix,
            [
                "dispatch",
                str(work_item.work_item_id),
                "--expected-revision",
                str(proposed.revision),
                "--idempotency-key",
                "api-cli-dispatch-work-item-0001",
            ],
            semantic_response=lambda payload: {
                "work_item_id": payload["work_item_id"],
                "status": payload["status"],
                "revision": payload["revision"],
            },
        )
        if dispatch_trace["semantic_response"]["status"] != "awaiting_dispatch":
            raise RuntimeError("standalone CLI did not durably dispatch the work item")
        cli_trace.append(dispatch_trace)
        dispatched = DevelopmentWorkItem.model_validate(dispatched_payload)

        first_lease_payload, first_lease_trace = _run_cli(
            role_cli_prefix[AgentRole.codex],
            [
                "lease",
                str(work_item.work_item_id),
                "--expected-revision",
                str(dispatched.revision),
                "--ttl-seconds",
                "300",
                "--idempotency-key",
                "api-cli-first-lease-0001",
            ],
            semantic_response=lambda payload: {
                "assignment_id": payload["assignment_id"],
                "work_item_id": payload["work_item_id"],
                "lease_id": payload["lease_id"],
                "owner_role": payload["owner_role"],
                "fence": payload["fence"],
                "work_item_revision": payload["work_item_revision"],
            },
        )
        if first_lease_payload["owner_role"] != AgentRole.codex.value:
            raise RuntimeError("first standalone lease was not role bound to Codex")
        cli_trace.append(first_lease_trace)

        first_working_payload, first_start_trace = _run_cli(
            role_cli_prefix[AgentRole.codex],
            [
                "start",
                first_lease_payload["lease_id"],
                "--expected-work-item-revision",
                str(first_lease_payload["work_item_revision"]),
                "--idempotency-key",
                "api-cli-first-start-0001",
            ],
            semantic_response=lambda payload: {
                "work_item_id": payload["work_item_id"],
                "status": payload["status"],
                "revision": payload["revision"],
                "lease_revision": payload["lease_revision"],
            },
        )
        first_working = DevelopmentWorkItem.model_validate(first_working_payload)
        if first_working.status != WorkItemStatus.working:
            raise RuntimeError("first standalone Codex lease did not enter working")
        cli_trace.append(first_start_trace)

        first_result = asyncio.run(
            _build_result(
                definition=definition,
                assignment=durable_assignment,
                work_item=first_working,
                lease_id=UUID(first_lease_payload["lease_id"]),
                grant=_role_grant(durable_assignment, AgentRole.codex),
                usage_meter=usage_store,
            )
        )
        if any(test.passed for test in first_result.tests):
            raise RuntimeError("first standalone result did not preserve the failing check")
        first_result_path = root / "requests" / "first-result.json"
        first_result_path.write_text(
            first_result.model_dump_json(exclude_none=True),
            encoding="utf-8",
        )
        first_ready_payload, first_result_trace = _run_cli(
            role_cli_prefix[AgentRole.codex],
            [
                "result",
                str(work_item.work_item_id),
                "--result-file",
                str(first_result_path),
                "--expected-work-item-revision",
                str(first_working.revision),
                "--idempotency-key",
                "api-cli-first-result-0001",
            ],
            semantic_response=lambda payload: {
                "work_item_id": payload["work_item_id"],
                "status": payload["status"],
                "revision": payload["revision"],
            },
        )
        first_ready = DevelopmentWorkItem.model_validate(first_ready_payload)
        if first_ready.status != WorkItemStatus.ready_for_lilies_review:
            raise RuntimeError("first standalone result did not enter review")
        cli_trace.append(first_result_trace)

        snapshots_root = (
            Path(_role_grant(durable_assignment, AgentRole.codex).workspace_root).parent
            / "review-snapshots"
        )
        if snapshots_root.exists():
            raise RuntimeError("result submission unexpectedly materialized a review snapshot")
        first_result_payload, first_show_trace = _run_cli(
            role_cli_prefix[AgentRole.lilies],
            [
                "result-show",
                str(first_result.result_id),
            ],
            semantic_response=lambda payload: {
                "result_id": payload["result"]["result_id"],
                "assignment_id": payload["result"]["assignment_id"],
                "work_item_id": payload["result"]["work_item_id"],
                "agent_role": payload["result"]["agent_role"],
                "diff_digest": payload["result"]["diff_digest"],
                "test_passed": all(
                    test["passed"] for test in payload["result"]["tests"]
                ),
                "enterprise_denominator": payload["enterprise_denominator"],
            },
        )
        if snapshots_root.exists():
            raise RuntimeError("pure result read materialized a review snapshot")
        durable_first_result = DevelopmentResult.model_validate(
            first_result_payload["result"]
        )
        if durable_first_result.result_id != first_result.result_id:
            raise RuntimeError("first result handoff read a different durable result")
        cli_trace.append(first_show_trace)

        first_prepare_arguments = [
            "review-prepare",
            str(first_result.result_id),
            "--idempotency-key",
            "api-cli-first-review-prepare-0001",
        ]
        first_prepared_payload, first_prepare_trace = _run_cli(
            role_cli_prefix[AgentRole.lilies],
            first_prepare_arguments,
            semantic_response=lambda payload: _snapshot_evidence(
                DevelopmentReviewSnapshotReceipt.model_validate(
                    payload["review_snapshot"]
                )
            ),
        )
        first_replayed_payload, first_prepare_replay_trace = _run_cli(
            role_cli_prefix[AgentRole.lilies],
            first_prepare_arguments,
            semantic_response=lambda payload: _snapshot_evidence(
                DevelopmentReviewSnapshotReceipt.model_validate(
                    payload["review_snapshot"]
                )
            ),
        )
        if first_prepared_payload != first_replayed_payload:
            raise RuntimeError("first review preparation was not outcome-idempotent")
        first_receipt = DevelopmentReviewSnapshotReceipt.model_validate(
            first_prepared_payload["review_snapshot"]
        )
        cli_trace.extend((first_prepare_trace, first_prepare_replay_trace))

        first_review = asyncio.run(
            _build_review(
                work_item=first_ready,
                result=durable_first_result,
                receipt=first_receipt,
                review_grant=_manual_review_grant(
                    _role_grant(durable_assignment, AgentRole.lilies),
                    first_receipt,
                ),
                usage_meter=usage_store,
            )
        )
        if first_review.verdict != ReviewVerdict.rework:
            raise RuntimeError("first standalone Lilies review did not request rework")
        first_review_path = root / "requests" / "first-review.json"
        first_review_path.write_text(
            first_review.model_dump_json(exclude_none=True),
            encoding="utf-8",
        )
        rework_payload, first_review_trace = _run_cli(
            role_cli_prefix[AgentRole.lilies],
            [
                "review",
                str(work_item.work_item_id),
                "--review-file",
                str(first_review_path),
                "--expected-work-item-revision",
                str(first_ready.revision),
                "--idempotency-key",
                "api-cli-first-review-0001",
            ],
            semantic_response=lambda payload: {
                "work_item_id": payload["work_item_id"],
                "status": payload["status"],
                "revision": payload["revision"],
                "verdict": ReviewVerdict.rework.value,
            },
        )
        rework = DevelopmentWorkItem.model_validate(rework_payload)
        if rework.status != WorkItemStatus.awaiting_dispatch:
            raise RuntimeError("standalone rework did not wait for manual dispatch")
        cli_trace.append(first_review_trace)

        second_dispatched_payload, second_dispatch_trace = _run_cli(
            owner_cli_prefix,
            [
                "dispatch",
                str(work_item.work_item_id),
                "--expected-revision",
                str(rework.revision),
                "--idempotency-key",
                "api-cli-second-dispatch-0001",
            ],
            semantic_response=lambda payload: {
                "work_item_id": payload["work_item_id"],
                "status": payload["status"],
                "revision": payload["revision"],
            },
        )
        second_dispatched = DevelopmentWorkItem.model_validate(
            second_dispatched_payload
        )
        cli_trace.append(second_dispatch_trace)

        second_lease_payload, second_lease_trace = _run_cli(
            role_cli_prefix[AgentRole.codex],
            [
                "lease",
                str(work_item.work_item_id),
                "--expected-revision",
                str(second_dispatched.revision),
                "--ttl-seconds",
                "300",
                "--idempotency-key",
                "api-cli-second-lease-0001",
            ],
            semantic_response=lambda payload: {
                "assignment_id": payload["assignment_id"],
                "work_item_id": payload["work_item_id"],
                "lease_id": payload["lease_id"],
                "owner_role": payload["owner_role"],
                "fence": payload["fence"],
                "work_item_revision": payload["work_item_revision"],
            },
        )
        cli_trace.append(second_lease_trace)
        second_working_payload, second_start_trace = _run_cli(
            role_cli_prefix[AgentRole.codex],
            [
                "start",
                second_lease_payload["lease_id"],
                "--expected-work-item-revision",
                str(second_lease_payload["work_item_revision"]),
                "--idempotency-key",
                "api-cli-second-start-0001",
            ],
            semantic_response=lambda payload: {
                "work_item_id": payload["work_item_id"],
                "status": payload["status"],
                "revision": payload["revision"],
                "lease_revision": payload["lease_revision"],
            },
        )
        second_working = DevelopmentWorkItem.model_validate(second_working_payload)
        cli_trace.append(second_start_trace)

        codex_grant = _role_grant(durable_assignment, AgentRole.codex)
        asyncio.run(
            _workspace_tools(
                codex_grant,
                assignment_id=durable_assignment.assignment_id,
                usage_meter=usage_store,
                mutable=True,
            ).workspace_patch(
                WorkspacePatchRequest(
                    path="src/mathlib.py",
                    old_string="return left - right",
                    new_string="return left + right",
                    usage_id=(
                        f"result:{work_item.work_item_id}:"
                        f"{second_lease_payload['lease_id']}:workspace-patch"
                    ),
                )
            )
        )
        second_result = asyncio.run(
            _build_result(
                definition=definition,
                assignment=durable_assignment,
                work_item=second_working,
                lease_id=UUID(second_lease_payload["lease_id"]),
                grant=codex_grant,
                usage_meter=usage_store,
            )
        )
        if not all(test.passed for test in second_result.tests):
            raise RuntimeError("second standalone result did not pass the frozen check")
        second_result_path = root / "requests" / "second-result.json"
        second_result_path.write_text(
            second_result.model_dump_json(exclude_none=True),
            encoding="utf-8",
        )
        second_ready_payload, second_result_trace = _run_cli(
            role_cli_prefix[AgentRole.codex],
            [
                "result",
                str(work_item.work_item_id),
                "--result-file",
                str(second_result_path),
                "--expected-work-item-revision",
                str(second_working.revision),
                "--idempotency-key",
                "api-cli-second-result-0001",
            ],
            semantic_response=lambda payload: {
                "work_item_id": payload["work_item_id"],
                "status": payload["status"],
                "revision": payload["revision"],
            },
        )
        second_ready = DevelopmentWorkItem.model_validate(second_ready_payload)
        cli_trace.append(second_result_trace)

        second_result_payload, second_show_trace = _run_cli(
            role_cli_prefix[AgentRole.lilies],
            ["result-show", str(second_result.result_id)],
            semantic_response=lambda payload: {
                "result_id": payload["result"]["result_id"],
                "assignment_id": payload["result"]["assignment_id"],
                "work_item_id": payload["result"]["work_item_id"],
                "agent_role": payload["result"]["agent_role"],
                "diff_digest": payload["result"]["diff_digest"],
                "test_passed": all(
                    test["passed"] for test in payload["result"]["tests"]
                ),
                "enterprise_denominator": payload["enterprise_denominator"],
            },
        )
        durable_second_result = DevelopmentResult.model_validate(
            second_result_payload["result"]
        )
        cli_trace.append(second_show_trace)

        second_prepare_arguments = [
            "review-prepare",
            str(second_result.result_id),
            "--idempotency-key",
            "api-cli-second-review-prepare-0001",
        ]
        second_prepared_payload, second_prepare_trace = _run_cli(
            role_cli_prefix[AgentRole.lilies],
            second_prepare_arguments,
            semantic_response=lambda payload: _snapshot_evidence(
                DevelopmentReviewSnapshotReceipt.model_validate(
                    payload["review_snapshot"]
                )
            ),
        )
        second_replayed_payload, second_prepare_replay_trace = _run_cli(
            role_cli_prefix[AgentRole.lilies],
            second_prepare_arguments,
            semantic_response=lambda payload: _snapshot_evidence(
                DevelopmentReviewSnapshotReceipt.model_validate(
                    payload["review_snapshot"]
                )
            ),
        )
        if second_prepared_payload != second_replayed_payload:
            raise RuntimeError("second review preparation was not outcome-idempotent")
        second_receipt = DevelopmentReviewSnapshotReceipt.model_validate(
            second_prepared_payload["review_snapshot"]
        )
        cli_trace.extend((second_prepare_trace, second_prepare_replay_trace))

        second_review = asyncio.run(
            _build_review(
                work_item=second_ready,
                result=durable_second_result,
                receipt=second_receipt,
                review_grant=_manual_review_grant(
                    _role_grant(durable_assignment, AgentRole.lilies),
                    second_receipt,
                ),
                usage_meter=usage_store,
            )
        )
        if second_review.verdict != ReviewVerdict.accepted:
            raise RuntimeError("second standalone Lilies review was not accepted")
        second_review_path = root / "requests" / "second-review.json"
        second_review_path.write_text(
            second_review.model_dump_json(exclude_none=True),
            encoding="utf-8",
        )
        accepted_payload, second_review_trace = _run_cli(
            role_cli_prefix[AgentRole.lilies],
            [
                "review",
                str(work_item.work_item_id),
                "--review-file",
                str(second_review_path),
                "--expected-work-item-revision",
                str(second_ready.revision),
                "--idempotency-key",
                "api-cli-second-review-0001",
            ],
            semantic_response=lambda payload: {
                "work_item_id": payload["work_item_id"],
                "status": payload["status"],
                "revision": payload["revision"],
                "verdict": ReviewVerdict.accepted.value,
            },
        )
        accepted = DevelopmentWorkItem.model_validate(accepted_payload)
        if accepted.status != WorkItemStatus.accepted:
            raise RuntimeError("standalone accepted review did not persist")
        cli_trace.append(second_review_trace)

        closed_payload, close_trace = _run_cli(
            owner_cli_prefix,
            [
                "close",
                str(work_item.work_item_id),
                "--expected-revision",
                str(accepted.revision),
                "--idempotency-key",
                "api-cli-close-work-item-0001",
            ],
            semantic_response=lambda payload: {
                "work_item_id": payload["work_item_id"],
                "status": payload["status"],
                "revision": payload["revision"],
            },
        )
        closed = DevelopmentWorkItem.model_validate(closed_payload)
        if closed.status != WorkItemStatus.closed:
            raise RuntimeError("standalone accepted work item did not close")
        cli_trace.append(close_trace)

        api_status, api_payload = _http_json(
            f"{base_url}/api/v1/collaborative-development/assignments/"
            f"{definition.assignment.assignment_id}/status",
            token=owner_token,
        )
        api_status_semantic = {
            "assignment_id": api_payload["assignment"]["assignment_id"],
            "status": api_payload["assignment"]["status"],
            "work_item_counts": api_payload["work_item_counts"],
            "enterprise_denominator": api_payload["enterprise_denominator"],
        }
        if (
            api_status != 200
            or api_status_semantic["work_item_counts"].get("closed") != 1
        ):
            raise RuntimeError("direct API did not observe the closed work item")

        stopped_payload, stop_trace = _run_cli(
            owner_cli_prefix,
            [
                "stop",
                str(definition.assignment.assignment_id),
                "--expected-revision",
                str(durable_assignment.revision),
                "--idempotency-key",
                "api-cli-stop-assignment-0001",
            ],
            semantic_response=lambda payload: {
                "assignment_id": payload["assignment_id"],
                "status": payload["status"],
                "revision": payload["revision"],
            },
        )
        cli_trace.append(stop_trace)
        _, archive_trace = _run_cli(
            owner_cli_prefix,
            [
                "archive",
                str(definition.assignment.assignment_id),
                "--expected-revision",
                str(stopped_payload["revision"]),
                "--idempotency-key",
                "api-cli-archive-assignment-0001",
            ],
            semantic_response=lambda payload: {
                "assignment_id": payload["assignment_id"],
                "status": payload["status"],
                "revision": payload["revision"],
            },
        )
        cli_trace.append(archive_trace)
        _, final_cli_status_trace = _run_cli(
            owner_cli_prefix,
            ["status", str(definition.assignment.assignment_id)],
            semantic_response=lambda payload: {
                "assignment_id": payload["assignment"]["assignment_id"],
                "status": payload["assignment"]["status"],
                "work_item_counts": payload["work_item_counts"],
                "enterprise_denominator": payload["enterprise_denominator"],
            },
        )
        if (
            final_cli_status_trace["semantic_response"]["status"] != "archived"
            or final_cli_status_trace["semantic_response"]["work_item_counts"].get(
                "closed"
            )
            != 1
        ):
            raise RuntimeError(
                "standalone CLI did not observe archived assignment and closed work"
            )
        cli_trace.append(final_cli_status_trace)

        _, events_trace = _run_cli(
            owner_cli_prefix,
            [
                "events",
                str(definition.assignment.assignment_id),
                "--after",
                "0",
                "--limit",
                "100",
            ],
            semantic_response=lambda payload: {
                "assignment_id": payload["assignment_id"],
                "event_types": [
                    event["event_type"] for event in payload["events"]
                ],
                "next_cursor": payload["next_cursor"],
            },
        )
        cli_trace.append(events_trace)
        final_events_status, final_events = _http_json(
            f"{base_url}/api/v1/collaborative-development/assignments/"
            f"{definition.assignment.assignment_id}/events?after=0&limit=100",
            token=owner_token,
        )
        final_event_types = [
            event["event_type"] for event in final_events["events"]
        ]
        required_event_types = {
            "work_item.result_submitted",
            "work_item.rework",
            "work_item.accepted",
            "work_item.closed",
            "assignment.stopped",
            "assignment.archived",
        }
        if (
            final_events_status != 200
            or not required_event_types.issubset(set(final_event_types))
            or final_event_types.count("work_item.result_submitted") != 2
        ):
            raise RuntimeError("standalone API event history is incomplete")
        final_status_code, final_status_payload = _http_json(
            f"{base_url}/api/v1/collaborative-development/assignments/"
            f"{definition.assignment.assignment_id}/status",
            token=owner_token,
        )
        final_status_semantic = {
            "assignment_id": final_status_payload["assignment"]["assignment_id"],
            "status": final_status_payload["assignment"]["status"],
            "work_item_counts": final_status_payload["work_item_counts"],
            "enterprise_denominator": final_status_payload[
                "enterprise_denominator"
            ],
        }
        if (
            final_status_code != 200
            or final_status_semantic["status"] != "archived"
        ):
            raise RuntimeError(
                "standalone API did not observe the archived terminal state"
            )

        trace = {
            "status": "passed",
            "server": {
                "health_http_status": health_status,
                "service": health["service"],
                "workflow_platform_required": health["workflow_platform_required"],
                "enterprise_denominator": health["enterprise_denominator"],
            },
            "cli_operations": cli_trace,
            "state_transition_transport": (
                "independent_cli_processes_over_loopback_http"
            ),
            "state_transition_service_substitution": False,
            "role_evidence_generation": (
                "production_workspace_tools_in_qualification_orchestrator"
            ),
            "cli_process_count": len(cli_trace),
            "executed_lifecycle": list(FULL_EXECUTED_LIFECYCLE),
            "review_verdicts": [
                first_review.verdict.value,
                second_review.verdict.value,
            ],
            "result_test_passes": [
                all(test.passed for test in durable_first_result.tests),
                all(test.passed for test in durable_second_result.tests),
            ],
            "result_handoffs": [
                {
                    "result_id": str(durable_first_result.result_id),
                    "read_by_lilies_cli": True,
                    "review_snapshot": _snapshot_evidence(first_receipt),
                    "review_prepare_replayed": True,
                    "verdict": first_review.verdict.value,
                },
                {
                    "result_id": str(durable_second_result.result_id),
                    "read_by_lilies_cli": True,
                    "review_snapshot": _snapshot_evidence(second_receipt),
                    "review_prepare_replayed": True,
                    "verdict": second_review.verdict.value,
                },
            ],
            "direct_api_operations": [
                {
                    "method": "GET",
                    "resource": "assignment_status",
                    "http_status": api_status,
                    "semantic_response": api_status_semantic,
                    "response_digest": canonical_digest(api_status_semantic),
                },
                {
                    "method": "GET",
                    "resource": "durable_assignment_events",
                    "http_status": final_events_status,
                    "event_types": final_event_types,
                    "next_cursor": final_events["next_cursor"],
                    "response_digest": canonical_digest(
                        {
                            "event_types": final_event_types,
                            "next_cursor": final_events["next_cursor"],
                        }
                    ),
                },
                {
                    "method": "GET",
                    "resource": "archived_assignment_status",
                    "http_status": final_status_code,
                    "semantic_response": final_status_semantic,
                    "response_digest": canonical_digest(
                        final_status_semantic
                    ),
                },
            ],
            "successful_cli_commands": [
                item["command"] for item in cli_trace
            ],
            "final_assignment_status": "archived",
            "final_work_item_status": closed.status.value,
            "source_repository_unchanged": (
                _git(definition.source_repository, "status", "--porcelain") == ""
                and _git(definition.source_repository, "rev-parse", "HEAD")
                == durable_assignment.baseline_commit
            ),
            "credential_transport": "separate_ephemeral_mode_0600_token_files",
            "token_material_persisted": False,
        }
    finally:
        server_log = _terminate_process(server)

    trace["server_log_digest"] = canonical_digest(
        {
            "exit_code": server.returncode,
            "log_byte_count": len(server_log),
        }
    )
    surface = QualificationSurfaceResult(
        status="passed",
        source="standalone-collaborative-development:actual-loopback-api-and-cli",
        summary=(
            "Independent CLI processes over the real loopback API completed "
            "result, rework, a second result, accepted review, close, stop, "
            "and archive without workflow-platform substitution."
        ),
        observations=[trace],
        digest=canonical_digest([trace]),
    )
    return surface, trace


def _bind_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **record,
        "evidence_digest": canonical_digest(record),
    }


async def build_evidence(
    work_root: Path,
    *,
    source_revision: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manual = await _run_lifecycle_scenario(
        work_root / "manual",
        label="manual",
        execution_mode=ExecutionMode.manual_dispatch,
    )
    autonomous = await _run_lifecycle_scenario(
        work_root / "autonomous",
        label="autonomous",
        execution_mode=ExecutionMode.autonomous,
    )
    surface, surface_trace = await asyncio.to_thread(
        _run_standalone_api_cli,
        work_root / "api-cli",
    )

    if (
        manual.record["executed_lifecycle"] != list(FULL_EXECUTED_LIFECYCLE)
        or autonomous.record["executed_lifecycle"]
        != list(FULL_EXECUTED_LIFECYCLE)
        or not manual.record["original_grants_unchanged"]
        or not autonomous.record["original_grants_unchanged"]
    ):
        raise RuntimeError("reusable lifecycle evidence is incomplete")

    reusable_unsigned = {
        "kind": "reusable_collaborative_development",
        "stage_task_id": STAGE_TASK_ID,
        "source_revision": source_revision,
        "enterprise_denominator": False,
        "status": "passed",
        "roles": ["lilies", "codex"],
        "authority_dimensions": list(AUTHORITY_DIMENSIONS),
        "lifecycle": list(QUALIFICATION_LIFECYCLE),
        "executed_lifecycle": list(FULL_EXECUTED_LIFECYCLE),
        "manual": manual.record,
        "autonomous": autonomous.record,
        "standalone_api_cli": surface_trace,
        "standalone_api_cli_digest": surface.digest,
        "production_components": [
            "DevelopmentAssignment",
            "CollaborativeDevelopmentStore",
            "CollaborativeDevelopmentService",
            "AutonomousDevelopmentLifecycleBridge",
            "run_dispatch_worker",
            "DevelopmentWorkspaceBroker",
        ],
        "workflow_application_required": False,
        "builder_required": False,
        "original_grants_unchanged": (
            manual.record["original_grants_unchanged"]
            and autonomous.record["original_grants_unchanged"]
        ),
    }
    reusable = _bind_record(reusable_unsigned)

    durable_record = _bind_record(
        {
            **autonomous.durable_record,
            "source_revision": source_revision,
        }
    )
    durable_unsigned = {
        "kind": "durable_autonomous_dispatch_history",
        "stage_task_id": STAGE_TASK_ID,
        "source_revision": source_revision,
        "enterprise_denominator": False,
        "status": "passed",
        "record": durable_record,
    }
    durable = _bind_record(durable_unsigned)
    return (
        reusable,
        durable,
        surface.model_dump(mode="json", exclude_none=True),
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run actual manual/autonomous collaborative-development lifecycles "
            "and emit three independently digest-bound evidence files."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for the three qualification evidence JSON files.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_directory = args.output_dir.expanduser().resolve()
    try:
        source_revision = qualification_source_revision(ROOT)
        if source_revision == "unavailable":
            raise RuntimeError("qualification source revision is unavailable")
        with tempfile.TemporaryDirectory(
            prefix="lilies-v0413-development-qualification-"
        ) as raw_work_root:
            reusable, durable, surface = asyncio.run(
                build_evidence(
                    Path(raw_work_root).resolve(),
                    source_revision=source_revision,
                )
            )
        if qualification_source_revision(ROOT) != source_revision:
            raise RuntimeError(
                "qualification source changed while evidence was being collected"
            )
        outputs = {
            "reusable_collaborative_development": output_directory
            / REUSABLE_FILENAME,
            "durable_autonomous_dispatch_history": output_directory
            / DISPATCH_FILENAME,
            "standalone_api_cli": output_directory / SURFACE_FILENAME,
        }
        _write_json(outputs["reusable_collaborative_development"], reusable)
        _write_json(outputs["durable_autonomous_dispatch_history"], durable)
        _write_json(outputs["standalone_api_cli"], surface)
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
        urllib.error.URLError,
    ) as error:
        print(
            f"development qualification rejected: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            {
                "status": "passed",
                "enterprise_denominator": False,
                "outputs": {
                    key: str(path) for key, path in outputs.items()
                },
                "digests": {
                    "reusable_collaborative_development": reusable[
                        "evidence_digest"
                    ],
                    "durable_autonomous_dispatch_history": durable[
                        "evidence_digest"
                    ],
                    "standalone_api_cli": surface["digest"],
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
