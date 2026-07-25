from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_platform.collaborative_development_api import (
    create_standalone_collaborative_development_app,
)
from agent_platform.collaborative_development_auth import DevelopmentCredentialIssuer
from agent_platform.collaborative_development_models import (
    AgentRole,
    AgentRoleGrant,
    CommandReceipt,
    DevelopmentAssignment,
    DevelopmentBudget,
    DevelopmentResult,
    DevelopmentTaskRole,
    DevelopmentWorkItem,
    ExecutionMode,
    SideEffect,
    TestReceipt as DevelopmentTestReceipt,
    WorkItemKind,
    utc_now,
)
from agent_platform.collaborative_development_service import (
    CollaborativeDevelopmentService,
)
from agent_platform.collaborative_development_storage import (
    CollaborativeDevelopmentStore,
)
from agent_platform.development_workspace_broker import (
    DevelopmentWorkspaceBroker,
    DevelopmentWorkspaceSpec,
)


OWNER_TOKEN = "owner-" + "o" * 40
SIGNING_KEY = "signing-" + "s" * 40


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "unrelated-software"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "config", "user.name", "Fixture")
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "src" / "parser.py").write_text(
        "def parse(value):\n    return value.strip()\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_parser.py").write_text(
        "from src.parser import parse\n\ndef test_parse():\n    assert parse(' ok ') == 'ok'\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture")
    return repository, _git(repository, "rev-parse", "HEAD")


def _assignment(tmp_path: Path) -> DevelopmentAssignment:
    source, baseline = _fixture_repo(tmp_path)
    assignment_id = uuid4()
    prepared = DevelopmentWorkspaceBroker(tmp_path / "development-workspaces").prepare(
        source_repository=source,
        assignment_id=assignment_id,
        baseline_revision=baseline,
        specs=(
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.lilies,
                allowed_paths=("src", "tests"),
                allowed_argv=(("python", "-m", "pytest", "-q"),),
                allowed_side_effects=(
                    SideEffect.workspace_write,
                    SideEffect.process_execute,
                ),
            ),
            DevelopmentWorkspaceSpec(
                agent_role=AgentRole.codex,
                allowed_paths=("src", "tests"),
                allowed_argv=(("python", "-m", "pytest", "-q"),),
                allowed_side_effects=(
                    SideEffect.workspace_write,
                    SideEffect.process_execute,
                ),
            ),
        ),
    )
    created = utc_now()
    return DevelopmentAssignment(
        assignment_id=assignment_id,
        goal="Add and independently review empty-input handling.",
        software_id="unrelated-parser-fixture",
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
            max_work_items=10,
            max_commands=50,
            max_tool_calls=500,
            max_wall_seconds=3_600,
            max_cost_usd=10,
        ),
        deadline=created + timedelta(hours=1),
        created_at=created,
        updated_at=created,
    )


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@pytest.mark.asyncio
async def test_standalone_api_is_role_scoped_durable_and_builder_independent(
    tmp_path: Path,
) -> None:
    store = CollaborativeDevelopmentStore(tmp_path / "collaborative-development.db")
    service = CollaborativeDevelopmentService(
        store=store,
        enabled=True,
        autonomous_enabled=True,
    )
    await service.initialize()
    issuer = DevelopmentCredentialIssuer(SIGNING_KEY)
    app = create_standalone_collaborative_development_app(
        service=service,
        credential_issuer=issuer,
        owner_token=OWNER_TOKEN,
    )
    assignment = _assignment(tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://collaboration.test",
    ) as client:
        openapi = await client.get("/openapi.json")
        assert openapi.status_code == 200
        paths = openapi.json()["paths"]
        assert "/api/v1/collaborative-development/assignments" in paths
        assert (
            "/api/v1/collaborative-development/work-items/{work_item_id}/reviews"
            in paths
        )
        assert (
            "/api/v1/collaborative-development/results/{result_id}"
            in paths
        )
        assert (
            "/api/v1/collaborative-development/results/"
            "{result_id}/review-snapshot"
            in paths
        )
        assert (
            "/api/v1/collaborative-development/assignments/"
            "{assignment_id}/workspace-authority"
            in paths
        )
        assert (
            "/api/v1/collaborative-development/assignments/"
            "{assignment_id}/review-reconciliations"
            in paths
        )
        assert (
            "/api/v1/collaborative-development/assignments/"
            "{assignment_id}/review-reconciliations/{outbox_id}/requeue"
            in paths
        )

        hidden = await client.get(
            f"/api/v1/collaborative-development/assignments/{assignment.assignment_id}"
        )
        assert hidden.status_code == 404
        assert hidden.text == "Not Found"

        created = await client.post(
            "/api/v1/collaborative-development/assignments",
            headers=_headers(OWNER_TOKEN),
            json={
                "idempotency_key": "create-api-assignment-0001",
                "assignment": assignment.model_dump(mode="json"),
            },
        )
        assert created.status_code == 201, created.text
        envelope = created.json()
        assert envelope["enterprise_denominator"] is False
        assert "application_id" not in envelope["assignment"]
        assert "builder" not in created.text.casefold()
        codex_token = envelope["codex_access_token"]
        lilies_token = envelope["lilies_access_token"]

        codex_assignment = await client.get(
            f"/api/v1/collaborative-development/assignments/{assignment.assignment_id}",
            headers=_headers(codex_token),
        )
        assert codex_assignment.status_code == 200
        codex_projection = codex_assignment.json()
        assert "agent_roles" not in codex_projection
        assert "workspace_grants" not in codex_projection
        assert codex_projection["agent_role"]["agent_role"] == "codex"
        assert codex_projection["workspace_grant"]["agent_role"] == "codex"
        lilies_root = next(
            grant.workspace_root
            for grant in assignment.workspace_grants
            if grant.agent_role == AgentRole.lilies
        )
        assert lilies_root not in codex_assignment.text

        codex_status = await client.get(
            f"/api/v1/collaborative-development/assignments/{assignment.assignment_id}/status",
            headers=_headers(codex_token),
        )
        assert codex_status.status_code == 200
        assert codex_status.json()["assignment"]["agent_role"]["agent_role"] == "codex"
        assert "workspace_grants" not in codex_status.json()["assignment"]

        owner_assignment = await client.get(
            f"/api/v1/collaborative-development/assignments/{assignment.assignment_id}",
            headers=_headers(OWNER_TOKEN),
        )
        assert owner_assignment.status_code == 200
        assert len(owner_assignment.json()["workspace_grants"]) == 2

        owner_reconciliations = await client.get(
            f"/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}/review-reconciliations",
            headers=_headers(OWNER_TOKEN),
        )
        assert owner_reconciliations.status_code == 200
        assert owner_reconciliations.json()["execution_mode"] == "manual_dispatch"
        assert (
            owner_reconciliations.json()["dispatch_behavior"]
            == "eligible_when_operator_runs_dispatch_worker"
        )
        assert owner_reconciliations.json()["reconciliations"] == []
        hidden_reconciliations = await client.get(
            f"/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}/review-reconciliations",
            headers=_headers(codex_token),
        )
        assert hidden_reconciliations.status_code == 404

        unknown_outbox_id = uuid4()
        invalid_confirmation = await client.post(
            f"/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}/review-reconciliations/"
            f"{unknown_outbox_id}/requeue",
            headers=_headers(OWNER_TOKEN),
            json={
                "idempotency_key": "api-review-requeue-invalid-0001",
                "expected_work_item_revision": 1,
                "expected_failed_attempt": 1,
                "confirmation": "silently_replay",
                "reason": "This confirmation must be rejected.",
            },
        )
        assert invalid_confirmation.status_code == 422
        missing_reconciliation = await client.post(
            f"/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}/review-reconciliations/"
            f"{unknown_outbox_id}/requeue",
            headers=_headers(OWNER_TOKEN),
            json={
                "idempotency_key": "api-review-requeue-missing-0001",
                "expected_work_item_revision": 1,
                "expected_failed_attempt": 1,
                "confirmation": "requeue_unknown_review_attempt",
                "reason": "Explicitly authorize one inspected unknown review.",
            },
        )
        assert missing_reconciliation.status_code == 409

        item = DevelopmentWorkItem(
            work_item_id=uuid4(),
            assignment_id=assignment.assignment_id,
            kind=WorkItemKind.bug,
            objective="Reject empty input.",
            acceptance=("Empty input raises ValueError.",),
            assigned_role=AgentRole.codex,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        proposed = await client.post(
            f"/api/v1/collaborative-development/assignments/{assignment.assignment_id}/work-items",
            headers=_headers(lilies_token),
            json={
                "idempotency_key": "create-api-work-item-0001",
                "work_item": item.model_dump(mode="json"),
            },
        )
        assert proposed.status_code == 201, proposed.text
        proposed_body = proposed.json()
        assert proposed_body["status"] == "proposed"

        before_dispatch = await client.post(
            f"/api/v1/collaborative-development/work-items/{item.work_item_id}/lease",
            headers=_headers(codex_token),
            json={
                "idempotency_key": "lease-api-work-item-0001",
                "expected_revision": proposed_body["revision"],
                "ttl_seconds": 60,
            },
        )
        assert before_dispatch.status_code == 409

        dispatched = await client.post(
            f"/api/v1/collaborative-development/work-items/{item.work_item_id}/dispatch",
            headers=_headers(OWNER_TOKEN),
            json={
                "idempotency_key": "dispatch-api-work-item-0001",
                "expected_revision": proposed_body["revision"],
            },
        )
        assert dispatched.status_code == 200, dispatched.text

        leased = await client.post(
            f"/api/v1/collaborative-development/work-items/{item.work_item_id}/lease",
            headers=_headers(codex_token),
            json={
                "idempotency_key": "lease-api-work-item-0002",
                "expected_revision": dispatched.json()["revision"],
                "ttl_seconds": 60,
            },
        )
        assert leased.status_code == 200, leased.text
        assert leased.json()["owner_role"] == "codex"

        working = await client.post(
            f"/api/v1/collaborative-development/leases/"
            f"{leased.json()['lease_id']}/start",
            headers=_headers(codex_token),
            json={
                "idempotency_key": "start-api-work-item-0001",
                "expected_work_item_revision": leased.json()[
                    "work_item_revision"
                ],
            },
        )
        assert working.status_code == 200, working.text
        codex_grant = next(
            grant
            for grant in assignment.workspace_grants
            if grant.agent_role == AgentRole.codex
        )
        result_diff_digest = DevelopmentWorkspaceBroker(
            tmp_path / "development-workspaces"
        ).calculate_diff_digest(
            workspace_root=Path(codex_grant.workspace_root),
            baseline_commit=assignment.baseline_commit,
        )
        command_time = utc_now()
        command_digest = "sha256:" + "a" * 64
        result = DevelopmentResult(
            result_id=uuid4(),
            assignment_id=assignment.assignment_id,
            work_item_id=item.work_item_id,
            lease_id=leased.json()["lease_id"],
            agent_role=AgentRole.codex,
            baseline_commit=assignment.baseline_commit,
            diff_digest=result_diff_digest,
            commands=(
                CommandReceipt(
                    argv=("python", "-m", "pytest", "-q"),
                    cwd="tests",
                    exit_code=1,
                    output_digest=command_digest,
                    started_at=command_time,
                    finished_at=command_time,
                ),
            ),
            tests=(
                DevelopmentTestReceipt(
                    name="empty-input regression",
                    command_digest=command_digest,
                    exit_code=1,
                    passed=False,
                    output_digest=command_digest,
                ),
            ),
            evidence_refs=(result_diff_digest, command_digest),
            reproduction_steps=("Run the frozen regression command.",),
            created_at=command_time,
        )
        ready = await client.post(
            f"/api/v1/collaborative-development/work-items/"
            f"{item.work_item_id}/results",
            headers=_headers(codex_token),
            json={
                "idempotency_key": "submit-api-result-0001",
                "expected_work_item_revision": working.json()["revision"],
                "result": result.model_dump(mode="json"),
            },
        )
        assert ready.status_code == 200, ready.text
        assert ready.json()["status"] == "ready_for_lilies_review"

        snapshots = (
            tmp_path
            / "development-workspaces"
            / str(assignment.assignment_id)
            / "review-snapshots"
        )
        codex_result = await client.get(
            f"/api/v1/collaborative-development/results/{result.result_id}",
            headers=_headers(codex_token),
        )
        lilies_result = await client.get(
            f"/api/v1/collaborative-development/results/{result.result_id}",
            headers=_headers(lilies_token),
        )
        owner_result = await client.get(
            f"/api/v1/collaborative-development/results/{result.result_id}",
            headers=_headers(OWNER_TOKEN),
        )
        assert codex_result.status_code == 200
        assert lilies_result.status_code == 200
        assert codex_result.json() == lilies_result.json()
        assert codex_result.json()["result"]["result_id"] == str(result.result_id)
        assert owner_result.status_code == 404
        assert not snapshots.exists()

        other_root = tmp_path / "other-assignment"
        other_root.mkdir()
        other_assignment = _assignment(other_root)
        other_created = await client.post(
            "/api/v1/collaborative-development/assignments",
            headers=_headers(OWNER_TOKEN),
            json={
                "idempotency_key": "create-other-api-assignment-0001",
                "assignment": other_assignment.model_dump(mode="json"),
            },
        )
        assert other_created.status_code == 201, other_created.text
        other_lilies_token = other_created.json()["lilies_access_token"]
        cross_result = await client.get(
            f"/api/v1/collaborative-development/results/{result.result_id}",
            headers=_headers(other_lilies_token),
        )
        cross_prepare = await client.post(
            f"/api/v1/collaborative-development/results/"
            f"{result.result_id}/review-snapshot",
            headers=_headers(other_lilies_token),
            json={"idempotency_key": "prepare-cross-result-0001"},
        )
        missing_idempotency_key = await client.post(
            f"/api/v1/collaborative-development/results/"
            f"{result.result_id}/review-snapshot",
            headers=_headers(lilies_token),
            json={},
        )
        assert cross_result.status_code == 404
        assert cross_prepare.status_code == 404
        assert missing_idempotency_key.status_code == 422
        assert not snapshots.exists()

        codex_prepare = await client.post(
            f"/api/v1/collaborative-development/results/"
            f"{result.result_id}/review-snapshot",
            headers=_headers(codex_token),
            json={"idempotency_key": "prepare-api-result-0001"},
        )
        owner_prepare = await client.post(
            f"/api/v1/collaborative-development/results/"
            f"{result.result_id}/review-snapshot",
            headers=_headers(OWNER_TOKEN),
            json={"idempotency_key": "prepare-api-result-0001"},
        )
        assert codex_prepare.status_code == 404
        assert owner_prepare.status_code == 404
        prepared = await client.post(
            f"/api/v1/collaborative-development/results/"
            f"{result.result_id}/review-snapshot",
            headers=_headers(lilies_token),
            json={"idempotency_key": "prepare-api-result-0001"},
        )
        replayed_prepare = await client.post(
            f"/api/v1/collaborative-development/results/"
            f"{result.result_id}/review-snapshot",
            headers=_headers(lilies_token),
            json={"idempotency_key": "prepare-api-result-0001"},
        )
        assert prepared.status_code == 200, prepared.text
        assert replayed_prepare.status_code == 200, replayed_prepare.text
        assert prepared.json() == replayed_prepare.json()
        receipt = prepared.json()["review_snapshot"]
        assert receipt["result_id"] == str(result.result_id)
        assert receipt["reviewer_role"] == "lilies"
        assert receipt["promotion_state"] == "review_snapshot_only"
        assert snapshots.joinpath(str(result.result_id)).is_dir()

        other_assignment_token = issuer.issue(uuid4(), AgentRole.codex)
        cross_assignment = await client.get(
            f"/api/v1/collaborative-development/assignments/{assignment.assignment_id}",
            headers=_headers(other_assignment_token),
        )
        assert cross_assignment.status_code == 404

        events = await client.get(
            f"/api/v1/collaborative-development/assignments/{assignment.assignment_id}/events",
            headers=_headers(lilies_token),
        )
        assert events.status_code == 200
        assert [event["seq"] for event in events.json()["events"]] == list(
            range(1, events.json()["next_cursor"] + 1)
        )
        next_cursor = events.json()["next_cursor"]
        sse = await client.get(
            f"/api/v1/collaborative-development/assignments/{assignment.assignment_id}/events",
            headers={
                **_headers(lilies_token),
                "Accept": "text/event-stream",
                "Last-Event-ID": "0",
            },
        )
        assert sse.status_code == 200
        assert sse.headers["content-type"].startswith("text/event-stream")
        assert [
            int(line.removeprefix("id: "))
            for line in sse.text.splitlines()
            if line.startswith("id: ")
        ] == list(range(1, next_cursor + 1))
        assert "data: {" in sse.text

        cursor_conflict = await client.get(
            f"/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}/events?after=1",
            headers={
                **_headers(lilies_token),
                "Accept": "text/event-stream",
                "Last-Event-ID": "0",
            },
        )
        assert cursor_conflict.status_code == 409

        ack = await client.post(
            f"/api/v1/collaborative-development/assignments/{assignment.assignment_id}/acks",
            headers=_headers(lilies_token),
            json={
                "idempotency_key": "ack-api-events-0001",
                "ack_seq": next_cursor,
                "expected_cursor_revision": 0,
            },
        )
        assert ack.status_code == 200, ack.text
        ack_replay = await client.get(
            f"/api/v1/collaborative-development/assignments/{assignment.assignment_id}/events",
            headers=_headers(lilies_token),
        )
        assert ack_replay.status_code == 200
        assert ack_replay.json()["after"] == next_cursor
        assert ack_replay.json()["events"] == []

        mode = await client.post(
            f"/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}/execution-mode",
            headers=_headers(OWNER_TOKEN),
            json={
                "idempotency_key": "switch-api-mode-0001",
                "expected_revision": 1,
                "mode": ExecutionMode.autonomous.value,
            },
        )
        assert mode.status_code == 200
        assert mode.json()["execution_mode"] == "autonomous"
        assert mode.json()["workspace_grants"] == envelope["assignment"]["workspace_grants"]
